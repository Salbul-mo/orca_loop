from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Sequence


MAX_ARTIFACT_BYTES = 1_048_576
FENCED_JSON = re.compile(
    r"```json\s*(\{.*\})\s*```",
    re.IGNORECASE | re.DOTALL,
)


class WorkerRunnerError(RuntimeError):
    """Raised when the deterministic worker wrapper cannot complete."""


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
        values: list[dict[str, object]] = []
        for index, character in enumerate(candidate):
            if character != "{":
                continue
            try:
                item, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                values.append(item)
        if not values:
            raise WorkerRunnerError(
                "agent output does not contain a JSON object"
            )
        value = values[-1]
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


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)


def _send(
    job: dict[str, object],
    *,
    message_type: str,
    subject: str,
    payload: dict[str, object],
    report_path: Path | None,
) -> None:
    command = [
        str(job["orca_executable"]),
        "orchestration",
        "send",
        "--to",
        str(job["coordinator_handle"]),
        "--from",
        str(job["worker_handle"]),
        "--subject",
        subject,
        "--type",
        message_type,
        "--task-id",
        str(job["task_id"]),
        "--dispatch-id",
        str(job["dispatch_id"]),
        "--payload",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]
    if report_path is not None:
        command.extend(("--report-path", str(report_path)))
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
        raise WorkerRunnerError(
            f"failed to send {message_type}: {completed.stderr[-4096:]}"
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
    }
    if not isinstance(value, dict) or set(value) != required:
        raise WorkerRunnerError("worker job schema mismatch")
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
        str(job["preamble"]).rstrip()
        + "\n\n"
        + contract_path.read_text(encoding="utf-8")
    )
    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    process = subprocess.Popen(
        tuple(job["profile_command"]),
        cwd=Path(str(job["agent_cwd"])).resolve(),
        shell=False,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
        creationflags=creationflags,
    )
    try:
        stdout_raw, stderr_raw = process.communicate(
            input=prompt.encode("utf-8"),
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as exc:
        _terminate_tree(process)
        process.communicate()
        raise WorkerRunnerError(
            f"agent timed out after {timeout_ms} ms"
        ) from exc
    stdout = stdout_raw.decode("utf-8", "replace")
    stderr = stderr_raw.decode("utf-8", "replace")
    if process.returncode != 0:
        raise WorkerRunnerError(
            f"agent exited {process.returncode}: {stderr[-4096:]}"
        )
    artifact = extract_agent_artifact(stdout)
    artifact_raw = artifact.encode("utf-8") + b"\n"
    _write_atomic(output_path, artifact_raw)
    digest = "sha256:" + hashlib.sha256(artifact_raw).hexdigest()
    _send(
        job,
        message_type="worker_done",
        subject="worker completed artifact",
        payload={"artifactDigest": digest},
        report_path=output_path,
    )
    return {
        "status": "PASS",
        "report_path": str(output_path),
        "artifact_digest": digest,
        "stderr_tail": stderr[-4096:],
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
        if job is not None:
            try:
                _send(
                    job,
                    message_type="escalation",
                    subject="worker runner blocked",
                    payload={"reason": str(exc)},
                    report_path=None,
                )
            except WorkerRunnerError as send_error:
                print(
                    f"worker escalation send failed: {send_error}",
                    file=sys.stderr,
                )
        print(
            json.dumps(
                {"status": "BLOCKED", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
