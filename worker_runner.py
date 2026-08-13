from __future__ import annotations

import argparse
import base64
import errno
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


MAX_ARTIFACT_BYTES = 1_048_576
RUNNER_RECORD_SCHEMA_VERSION = 1
FENCED_JSON = re.compile(
    r"```json\s*(\{.*\})\s*```",
    re.IGNORECASE | re.DOTALL,
)


class WorkerRunnerError(RuntimeError):
    """Raised when the deterministic worker wrapper cannot complete."""


class PermissionObservationError(WorkerRunnerError):
    """A locally observed OS permission failure with a stable reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def _is_access_denied(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or exc.errno in {
        errno.EACCES,
        errno.EPERM,
    }


def _probe_source_directory(path: Path) -> None:
    """Check directory-read access without invoking an agent or writing data."""
    try:
        with os.scandir(path) as entries:
            next(entries, None)
    except OSError as exc:
        if _is_access_denied(exc):
            raise PermissionObservationError(
                "SOURCE_DIRECTORY_READ_DENIED",
                f"source directory read denied: {path}",
            ) from exc


def _terminate_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ("taskkill", "/PID", str(process.pid), "/T", "/F"),
            shell=False,
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return


def _json_object_from_text(text: str) -> dict[str, object]:
    candidate = text.strip()
    match = FENCED_JSON.search(candidate)
    if match:
        candidate = match.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        values: list[tuple[int, int, dict[str, object]]] = []
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                item, end = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                values.append((end, index, item))
        if not values:
            raise WorkerRunnerError(
                "agent output does not contain a JSON object"
            )
        _, _, value = max(
            values,
            key=lambda item: (item[0], -item[1]),
        )
    if not isinstance(value, dict):
        raise WorkerRunnerError("artifact root must be a JSON object")
    return value


def extract_agent_artifact(stdout: str) -> str:
    stripped = stdout.strip()
    if not stripped:
        raise WorkerRunnerError("agent stdout is empty")
    lines = [line for line in stripped.splitlines() if line.strip()]
    agent_messages: list[str] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("result"), str):
            agent_messages.append(event["result"])
        item = event.get("item")
        if (
            isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            agent_messages.append(item["text"])
    source = agent_messages[-1] if agent_messages else stripped
    value = _json_object_from_text(source)
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if not 1 <= len(raw.encode("utf-8")) <= MAX_ARTIFACT_BYTES:
        raise WorkerRunnerError("extracted artifact has invalid size")
    return raw


def bind_artifact_provenance(
    artifact: str,
    job: dict[str, object],
) -> str:
    value = json.loads(artifact)
    has_task = "task_id" in value
    has_dispatch = "dispatch_id" in value
    if not has_task and not has_dispatch:
        return artifact
    if has_task != has_dispatch:
        raise WorkerRunnerError(
            "artifact provenance must include task_id and dispatch_id together"
        )
    if (
        value["task_id"] != str(job["task_id"])
        or value["dispatch_id"] != str(job["dispatch_id"])
    ):
        raise WorkerRunnerError("artifact provenance conflicts with job binding")
    raw = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if not 1 <= len(raw.encode("utf-8")) <= MAX_ARTIFACT_BYTES:
        raise WorkerRunnerError("bound artifact has invalid size")
    return raw


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _agent_environment() -> dict[str, str]:
    """Remove wrapper routing authority before starting the provider CLI."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("ORCA_")
        and "DISPATCH_CAPABILITY" not in key.upper()
    }


class EvidenceLog:
    """Durable per-step record of what the agent ran and what it printed.

    Every write is best-effort: a failure to persist evidence must never
    replace the agent error that the caller actually needs to see.
    """

    def __init__(self, log_dir: Path, step_id: str) -> None:
        self.stdout_path = log_dir / f"step-{step_id}.stdout.log"
        self.stderr_path = log_dir / f"step-{step_id}.stderr.log"
        self.record_path = log_dir / f"step-{step_id}.runner.json"

    @classmethod
    def from_job(cls, job: dict[str, object]) -> "EvidenceLog | None":
        log_dir = job.get("log_dir")
        step_id = job.get("step_id")
        if not isinstance(log_dir, str) or not log_dir:
            return None
        if not isinstance(step_id, str) or not step_id:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", step_id):
            return None
        return cls(Path(log_dir).resolve(), step_id)

    def write_record(self, value: dict[str, object]) -> None:
        raw = (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        try:
            _write_atomic(self.record_path, raw)
        except OSError:
            return

    def write_streams(self, stdout: bytes, stderr: bytes) -> None:
        for path, raw in (
            (self.stdout_path, stdout),
            (self.stderr_path, stderr),
        ):
            try:
                _write_atomic(path, raw)
            except OSError:
                continue

    def paths(self) -> dict[str, str]:
        return {
            "stdoutLog": str(self.stdout_path),
            "stderrLog": str(self.stderr_path),
            "runnerRecord": str(self.record_path),
        }


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _send(
    job: dict[str, object],
    *,
    message_type: str,
    subject: str,
    payload: dict[str, object],
    report_path: Path | None,
) -> None:
    message_payload = dict(payload)
    message_payload.setdefault("schema_version", 1)
    message_payload.setdefault("taskId", str(job["task_id"]))
    message_payload.setdefault("dispatchId", str(job["dispatch_id"]))
    if message_type == "worker_done":
        message_payload.setdefault("outcome", "succeeded")
    if report_path is not None:
        message_payload.setdefault("reportPath", str(report_path))
    command = [
        str(job["orca_executable"]),
        "orchestration",
        "send",
        "--subject",
        subject,
        "--type",
        message_type,
    ]
    if message_type == "worker_done":
        # Orca refuses --payload together with the structured payload flags,
        # and it requires --outcome on a worker_done.  A worker_done therefore
        # cannot carry the artifact digest or schema version at all: those ride
        # the preceding artifact-ready status message instead.
        outcome = message_payload["outcome"]
        if outcome not in {"succeeded", "failed"}:
            raise WorkerRunnerError(
                f"worker_done outcome must be succeeded or failed: {outcome!r}"
            )
        command.extend(
            (
                "--task-id",
                str(job["task_id"]),
                "--dispatch-id",
                str(job["dispatch_id"]),
                "--outcome",
                str(outcome),
            )
        )
        if report_path is not None:
            command.extend(("--report-path", str(report_path)))
    else:
        # Every other type carries its identity inside the JSON payload, which
        # is the only way to move fields Orca has no typed flag for.
        command.extend(
            (
                "--payload",
                json.dumps(
                    message_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
    orchestration_run_id = job["orchestration_run_id"]
    command.extend(("--run", str(orchestration_run_id)))
    command.append("--json")
    completed = subprocess.run(
        tuple(command),
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        details = "\n".join(
            item for item in (completed.stderr, completed.stdout) if item
        ).strip()[-4096:]
        raise WorkerRunnerError(
            f"failed to send {message_type}: {details}"
        )
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise WorkerRunnerError(
            "Orca send returned malformed JSON"
        ) from exc
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise WorkerRunnerError(
            f"Orca send failed: {response!r}"
        )


def _load_job(encoded: str) -> dict[str, object]:
    try:
        raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        value = json.loads(raw)
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkerRunnerError("invalid encoded job") from exc
    required = {
        "profile_command",
        "agent_cwd",
        "contract_path",
        "output_path",
        "task_id",
        "dispatch_id",
        "coordinator_handle",
        "worker_handle",
        "orca_executable",
        "timeout_ms",
        "preamble",
        "log_dir",
        "step_id",
        "orchestration_run_id",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise WorkerRunnerError("worker job schema mismatch")
    if (
        not isinstance(value["orchestration_run_id"], str)
        or not value["orchestration_run_id"]
    ):
        raise WorkerRunnerError(
            "orchestration_run_id must be a nonempty string"
        )
    if (
        not isinstance(value["profile_command"], list)
        or not value["profile_command"]
        or not all(
            isinstance(item, str) and item
            for item in value["profile_command"]
        )
    ):
        raise WorkerRunnerError("profile_command must be nonempty strings")
    agent_cwd = Path(str(value["agent_cwd"])).resolve()
    if not agent_cwd.is_dir():
        raise WorkerRunnerError(
            f"agent_cwd must be an existing directory: {agent_cwd}"
        )
    return value


def run_job(job: dict[str, object]) -> dict[str, object]:
    contract_path = Path(str(job["contract_path"])).resolve()
    output_path = Path(str(job["output_path"])).resolve()
    if not contract_path.is_file():
        raise WorkerRunnerError(
            f"contract does not exist: {contract_path}"
        )
    timeout_ms = job["timeout_ms"]
    if not isinstance(timeout_ms, int) or timeout_ms < 1:
        raise WorkerRunnerError("timeout_ms must be a positive integer")
    prompt = (
        contract_path.read_text(encoding="utf-8").rstrip()
        + "\n\n## Wrapper-supplied artifact provenance\n\n"
        + f"- task_id: `{job['task_id']}`\n"
        + f"- dispatch_id: `{job['dispatch_id']}`\n\n"
        + "Use these values verbatim when the output artifact schema contains "
        + "`task_id` and `dispatch_id`. Do not send Orca lifecycle messages; "
        + "the deterministic wrapper owns artifact persistence and signaling.\n"
    )
    evidence = EvidenceLog.from_job(job)
    record: dict[str, object] = {
        "schema_version": RUNNER_RECORD_SCHEMA_VERSION,
        "step_id": job.get("step_id"),
        "task_id": str(job["task_id"]),
        "dispatch_id": str(job["dispatch_id"]),
        "worker_handle": str(job["worker_handle"]),
        "command": list(job["profile_command"]),
        "agent_cwd": str(Path(str(job["agent_cwd"])).resolve()),
        "contract_path": str(contract_path),
        "output_path": str(output_path),
        "timeout_ms": timeout_ms,
        "started_at": _utc_now(),
        "status": "RUNNING",
    }
    if evidence is not None:
        record.update(evidence.paths())
        evidence.write_record(record)

    started = time.monotonic()
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    try:
        process = subprocess.Popen(
            tuple(job["profile_command"]),
            cwd=Path(str(job["agent_cwd"])).resolve(),
            shell=False,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
            env=_agent_environment(),
        )
    except OSError as exc:
        if _is_access_denied(exc):
            raise PermissionObservationError(
                "PROCESS_EXECUTION_DENIED",
                "agent process execution denied",
            ) from exc
        raise WorkerRunnerError(f"agent process failed to start: {exc}") from exc
    timed_out = False
    try:
        stdout_raw, stderr_raw = process.communicate(
            input=prompt.encode("utf-8"),
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired:
        _terminate_tree(process)
        timed_out = True
        try:
            stdout_raw, stderr_raw = process.communicate()
        except (OSError, ValueError):
            stdout_raw, stderr_raw = b"", b""
    stdout_raw = stdout_raw or b""
    stderr_raw = stderr_raw or b""
    stdout = stdout_raw.decode("utf-8", "replace")
    stderr = stderr_raw.decode("utf-8", "replace")

    # Persist the agent output before any verdict is formed, so a failing
    # step leaves the same evidence a succeeding one does.
    if evidence is not None:
        evidence.write_streams(stdout_raw, stderr_raw)
    record.update(
        {
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "finished_at": _utc_now(),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_bytes": len(stdout_raw),
            "stderr_bytes": len(stderr_raw),
        }
    )

    try:
        if timed_out:
            raise WorkerRunnerError(
                f"agent timed out after {timeout_ms} ms"
            )
        if process.returncode != 0:
            _probe_source_directory(
                Path(str(job["agent_cwd"])).resolve()
            )
            raise WorkerRunnerError(
                f"agent exited {process.returncode}: {stderr[-4096:]}"
            )
        try:
            artifact = extract_agent_artifact(stdout)
        except WorkerRunnerError as extraction_error:
            if not output_path.is_file():
                raise
            try:
                artifact = extract_agent_artifact(
                    output_path.read_text(encoding="utf-8")
                )
            except (OSError, WorkerRunnerError) as fallback_error:
                raise extraction_error from fallback_error
        artifact = bind_artifact_provenance(artifact, job)
        artifact_raw = artifact.encode("utf-8") + b"\n"
        try:
            _write_atomic(output_path, artifact_raw)
        except OSError as exc:
            if _is_access_denied(exc):
                raise PermissionObservationError(
                    "OUTBOX_WRITE_DENIED",
                    f"artifact outbox write denied: {output_path}",
                ) from exc
            raise WorkerRunnerError(
                f"artifact outbox write failed: {exc}"
            ) from exc
        digest = "sha256:" + hashlib.sha256(artifact_raw).hexdigest()
        # Stage 1 carries everything a worker_done cannot: the schema version
        # and the digest the coordinator cross-checks against its own hash.
        _send(
            job,
            message_type="status",
            subject="worker artifact ready",
            payload={"artifactDigest": digest},
            report_path=output_path,
        )
        # Stage 2 is the terminal settlement signal for the Dispatch.
        _send(
            job,
            message_type="worker_done",
            subject="worker completed artifact",
            payload={"outcome": "succeeded"},
            report_path=output_path,
        )
    except WorkerRunnerError as exc:
        record.update({"status": "FAILED", "error": str(exc)})
        if evidence is not None:
            evidence.write_record(record)
        raise
    record.update({"status": "PASS", "artifact_digest": digest})
    if evidence is not None:
        evidence.write_record(record)
    return {
        "status": "PASS",
        "report_path": str(output_path),
        "artifact_digest": digest,
        "stderr_tail": stderr[-4096:],
        **({} if evidence is None else evidence.paths()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-base64", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    job: dict[str, object] | None = None
    try:
        job = _load_job(args.job_base64)
        result = run_job(job)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except WorkerRunnerError as exc:
        evidence = None if job is None else EvidenceLog.from_job(job)
        paths = {} if evidence is None else evidence.paths()
        if job is not None:
            try:
                # The escalation carries the reason and evidence, which a
                # worker_done has no typed flags for; the worker_done that
                # follows is what actually settles the Dispatch as failed.
                _send(
                    job,
                    message_type="escalation",
                    subject="worker runner blocked",
                    payload={
                        "reason": str(exc),
                        "step_id": str(job["step_id"]),
                        "evidence_paths": list(paths.values()),
                        **(
                            {"reason_code": exc.reason_code}
                            if isinstance(exc, PermissionObservationError)
                            else {}
                        ),
                        **paths,
                    },
                    report_path=None,
                )
                _send(
                    job,
                    message_type="worker_done",
                    subject="worker runner failed",
                    payload={"outcome": "failed"},
                    report_path=None,
                )
            except WorkerRunnerError as send_error:
                print(
                    f"worker escalation send failed: {send_error}",
                    file=sys.stderr,
                )
        print(
            json.dumps(
                {"status": "BLOCKED", "error": str(exc), **paths},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
