from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Callable, Mapping

from .contracts import canonical_json_bytes
from .models import (
    ActiveStep,
    Completion,
    CompletionKind,
    DispatchHandle,
    LaunchProfile,
    PreparedTask,
    RenderedContract,
    Role,
    StepStage,
    StepWorkspace,
    StagedInput,
    WorkerHandle,
    WorkerKey,
    WorkerPool,
)
from .orca_client import OrcaClient
from .transport import InputManifest, stage_inputs, verify_input_manifest


class DispatcherError(RuntimeError):
    """Base error for worker lifecycle failures."""


class WorkerProvisionError(DispatcherError):
    """Raised when the four-role worker pool cannot be established."""


class WorkerLostError(DispatcherError):
    """Raised when a bound worker terminal is no longer available."""


class StepBindingError(DispatcherError):
    """Raised when durable step task binding is inconsistent."""


class DispatchTimeoutError(DispatcherError):
    """Raised by callers that require an exception on dispatch timeout."""


class DispatchProvenanceError(DispatcherError):
    """Raised when task, dispatch, worker, or worktree provenance differs."""


REQUIRED_WORKERS = {
    WorkerKey.CLAUDE_PLANNER,
    WorkerKey.CLAUDE_CODE_REVIEW,
    WorkerKey.CODEX_IMPLEMENTER,
    WorkerKey.CODEX_REVIEW,
}
ROLE_WORKER = {
    Role.PLANNER: WorkerKey.CLAUDE_PLANNER,
    Role.PLAN_REVIEWER: WorkerKey.CODEX_REVIEW,
    Role.IMPLEMENTER: WorkerKey.CODEX_IMPLEMENTER,
    Role.CODE_REVIEWER: WorkerKey.CLAUDE_CODE_REVIEW,
    Role.CROSS_CONFIRMER: WorkerKey.CODEX_REVIEW,
}


def _result(response) -> dict[str, object]:
    try:
        value = json.loads(response.result_json)
    except json.JSONDecodeError as exc:
        raise DispatcherError("Orca result_json is malformed") from exc
    if not isinstance(value, dict):
        raise DispatcherError("Orca result must be an object")
    return value


def _field(
    value: Mapping[str, object],
    *names: str,
) -> object | None:
    for name in names:
        if name in value:
            return value[name]
    return None


def _terminal_handle(
    worker_key: WorkerKey,
    result: Mapping[str, object],
    worktree_selector: str,
) -> WorkerHandle:
    terminal_value = result.get("terminal", result)
    if not isinstance(terminal_value, dict):
        raise WorkerProvisionError(
            f"terminal create result is invalid for {worker_key.value}"
        )
    handle = _field(terminal_value, "handle", "terminalHandle")
    if not isinstance(handle, str) or not handle:
        raise WorkerProvisionError("terminal handle is missing")
    tab_id = _field(terminal_value, "tabId", "tab_id")
    leaf_id = _field(
        terminal_value,
        "leafId",
        "leaf_id",
        "paneId",
        "paneKey",
    )
    worktree_id = _field(
        terminal_value,
        "worktreeId",
        "worktree_id",
    )
    return WorkerHandle(
        worker_key=worker_key,
        terminal_handle=handle,
        worktree_id=(
            worktree_id
            if isinstance(worktree_id, str) and worktree_id
            else worktree_selector
        ),
        tab_id=tab_id if isinstance(tab_id, str) else "",
        leaf_id=leaf_id if isinstance(leaf_id, str) else "",
    )


def provision_workers(
    client: OrcaClient,
    worktree_selector: str,
    profiles: Mapping[WorkerKey, LaunchProfile],
    *,
    coordinator_handle: str,
) -> WorkerPool:
    if not worktree_selector:
        raise WorkerProvisionError(
            "worktree_selector must be explicit"
        )
    if set(profiles) != REQUIRED_WORKERS:
        raise WorkerProvisionError(
            "profiles must contain exactly four worker keys"
        )
    workers: list[WorkerHandle] = []
    for key in sorted(profiles, key=lambda item: item.value):
        response = client.call(
            (
                "terminal",
                "create",
                "--worktree",
                worktree_selector,
                "--title",
                f"ORCA LOOP {key.value}",
            ),
            timeout_ms=60_000,
        )
        worker = _terminal_handle(
            key,
            _result(response),
            worktree_selector,
        )
        if worker.terminal_handle == coordinator_handle:
            raise WorkerProvisionError(
                "worker handle equals coordinator handle"
            )
        client.call(
            (
                "terminal",
                "show",
                "--terminal",
                worker.terminal_handle,
            ),
            timeout_ms=10_000,
        )
        workers.append(worker)
    handles = {item.terminal_handle for item in workers}
    if len(handles) != 4:
        raise WorkerProvisionError("worker terminal handles are not unique")
    mapping = {item.worker_key: item for item in workers}
    if (
        mapping[WorkerKey.CLAUDE_PLANNER].terminal_handle
        == mapping[WorkerKey.CLAUDE_CODE_REVIEW].terminal_handle
    ):
        raise WorkerProvisionError(
            "planner and code reviewer must use separate sessions"
        )
    return WorkerPool(tuple(workers))


def worker_for_role(pool: WorkerPool, role: Role) -> WorkerHandle:
    key = ROLE_WORKER[role]
    for worker in pool.workers:
        if worker.worker_key is key:
            return worker
    raise WorkerLostError(f"worker is missing: {key.value}")


def _write_binding(step: StepWorkspace, value: dict[str, object]) -> None:
    path = step.root / "binding.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(canonical_json_bytes(value) + b"\n")
    temporary.replace(path)


def prepare_task(
    client: OrcaClient,
    step: StepWorkspace,
    contract: RenderedContract,
    worker: WorkerHandle,
    role: Role,
    *,
    additional_inputs: tuple[StagedInput, ...],
    commit_stage: Callable[[StepStage, ActiveStep], None],
) -> tuple[PreparedTask, InputManifest]:
    if (step.root / "binding.json").exists():
        raise StepBindingError("step already has a task binding")
    if any(step.output_dir.iterdir()):
        raise StepBindingError("step output directory must be empty")
    manifest = stage_inputs(
        step,
        (
            StagedInput(
                name="contract.md",
                source_path=None,
                inline_bytes=contract.text.encode("utf-8"),
            ),
            *additional_inputs,
        ),
    )
    verify_input_manifest(step, manifest)
    active = ActiveStep(
        step_id=step.step_id,
        task_id=None,
        dispatch_id=None,
        role=role,
        worker=worker,
    )
    commit_stage(StepStage.STEP_PREPARED, active)
    run_id = step.root.parents[1].name
    contract_path = (step.input_dir / "contract.md").resolve()
    response = client.call(
        (
            "orchestration",
            "task-create",
            "--task-title",
            f"{run_id} {step.step_id} {role.value}",
            "--display-name",
            f"{role.value} {step.step_id}",
            "--spec",
            (
                f"Run {run_id}, step {step.step_id}. "
                f"Follow the existing contract at {contract_path}."
            ),
        ),
        timeout_ms=30_000,
    )
    result = _result(response)
    task_value = result.get("task")
    if not isinstance(task_value, dict):
        raise StepBindingError("task-create response is missing task")
    task_id = task_value.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise StepBindingError("task-create response is missing task ID")
    active = ActiveStep(
        step_id=step.step_id,
        task_id=task_id,
        dispatch_id=None,
        role=role,
        worker=worker,
    )
    _write_binding(
        step,
        {
            "schema_version": 1,
            "step_id": step.step_id,
            "task_id": task_id,
            "dispatch_id": None,
            "worker_handle": worker.terminal_handle,
            "role": role.value,
            "contract_digest": contract.digest,
            "input_manifest_digest": manifest.manifest_digest,
        },
    )
    commit_stage(StepStage.TASK_CREATED, active)
    return (
        PreparedTask(
            step_id=step.step_id,
            task_id=task_id,
            worker=worker,
            role=role,
            contract_digest=contract.digest,
        ),
        manifest,
    )


def _dispatch_value(
    result: Mapping[str, object],
) -> tuple[str, str]:
    dispatch = result.get("dispatch")
    if not isinstance(dispatch, dict):
        raise DispatchProvenanceError(
            "dispatch response is missing dispatch"
        )
    dispatch_id = dispatch.get("id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise DispatchProvenanceError("dispatch ID is missing")
    preamble = result.get("preamble", "")
    if not isinstance(preamble, str):
        preamble = ""
    return dispatch_id, preamble


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _runner_command(job: dict[str, object], runner_path: Path) -> str:
    encoded = base64.urlsafe_b64encode(
        canonical_json_bytes(job)
    ).decode("ascii")
    return (
        "& "
        + _powershell_quote(sys.executable)
        + " "
        + _powershell_quote(str(runner_path.resolve()))
        + " --job-base64 "
        + _powershell_quote(encoded)
    )


def _profile_working_directory(profile: LaunchProfile) -> str:
    command = profile.command
    for option in ("-C", "--cd", "--add-dir"):
        if option not in command:
            continue
        index = command.index(option)
        if index + 1 >= len(command):
            break
        candidate = Path(command[index + 1]).resolve()
        if candidate.is_dir():
            return str(candidate)
    raise DispatchProvenanceError(
        "launch profile has no valid agent working directory"
    )


def _messages(result: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw = result.get("messages", [])
    if not isinstance(raw, list):
        raise DispatchProvenanceError(
            "orchestration check messages must be an array"
        )
    return tuple(item for item in raw if isinstance(item, dict))


def _delivery_id(result: Mapping[str, object]) -> str | None:
    value = _field(result, "deliveryId", "delivery_id")
    return value if isinstance(value, str) and value else None


def _message_value(message: Mapping[str, object]) -> dict[str, object]:
    nested = message.get("message")
    return dict(nested) if isinstance(nested, dict) else dict(message)


def _message_ids(
    message: Mapping[str, object],
) -> tuple[str | None, str | None, dict[str, object]]:
    value = _message_value(message)
    payload = value.get("payload")
    if isinstance(payload, dict):
        payload_value = dict(payload)
    elif isinstance(payload, str):
        try:
            decoded_payload = json.loads(payload)
        except json.JSONDecodeError:
            decoded_payload = None
        payload_value = (
            dict(decoded_payload)
            if isinstance(decoded_payload, dict)
            else {}
        )
    else:
        payload_value = {}
    task_id = _field(value, "task_id", "taskId")
    if task_id is None:
        task_id = _field(payload_value, "task_id", "taskId")
    dispatch_id = _field(value, "dispatch_id", "dispatchId")
    if dispatch_id is None:
        dispatch_id = _field(
            payload_value,
            "dispatch_id",
            "dispatchId",
        )
    return (
        task_id if isinstance(task_id, str) else None,
        dispatch_id if isinstance(dispatch_id, str) else None,
        payload_value,
    )


def dispatch_and_wait(
    client: OrcaClient,
    prepared: PreparedTask,
    step: StepWorkspace,
    profile: LaunchProfile,
    *,
    coordinator_handle: str,
    orca_executable: str,
    runner_path: Path,
    step_timeout_ms: int,
    artifact_filename: str,
    commit_dispatched: Callable[[DispatchHandle], None],
    foreign_message: Callable[[dict[str, object]], None] | None = None,
) -> tuple[DispatchHandle, Completion]:
    response = client.call(
        (
            "orchestration",
            "dispatch",
            "--task",
            prepared.task_id,
            "--to",
            prepared.worker.terminal_handle,
            "--from",
            coordinator_handle,
            "--return-preamble",
        ),
        timeout_ms=30_000,
    )
    dispatch_id, preamble = _dispatch_value(_result(response))
    handle = DispatchHandle(
        step_id=prepared.step_id,
        task_id=prepared.task_id,
        dispatch_id=dispatch_id,
        worker=prepared.worker,
        role=prepared.role,
        worktree_id=prepared.worker.worktree_id,
        tab_id=prepared.worker.tab_id,
        leaf_id=prepared.worker.leaf_id,
    )
    binding_path = step.root / "binding.json"
    try:
        binding = json.loads(binding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StepBindingError("invalid task binding") from exc
    if not isinstance(binding, dict) or binding.get("task_id") != prepared.task_id:
        raise StepBindingError("task binding provenance mismatch")
    binding["dispatch_id"] = dispatch_id
    _write_binding(step, binding)
    commit_dispatched(handle)

    job = {
        "profile_command": list(profile.command),
        "agent_cwd": _profile_working_directory(profile),
        "contract_path": str((step.input_dir / "contract.md").resolve()),
        "output_path": str(
            (step.output_dir / artifact_filename).resolve()
        ),
        "task_id": prepared.task_id,
        "dispatch_id": dispatch_id,
        "coordinator_handle": coordinator_handle,
        "worker_handle": prepared.worker.terminal_handle,
        "orca_executable": orca_executable,
        "timeout_ms": step_timeout_ms,
        "preamble": preamble,
        # runs/<run-id>/logs; the runner persists the agent command line and
        # both output streams there whatever the exit code turns out to be.
        "log_dir": str((step.root.parents[1] / "logs").resolve()),
        "step_id": step.step_id,
    }
    command = _runner_command(job, runner_path)
    client.call(
        (
            "terminal",
            "send",
            "--terminal",
            prepared.worker.terminal_handle,
            "--text",
            command,
            "--enter",
        ),
        timeout_ms=30_000,
    )

    deadline = time.monotonic() + step_timeout_ms / 1000
    while time.monotonic() < deadline:
        remaining_ms = max(
            1,
            int((deadline - time.monotonic()) * 1000),
        )
        window = min(60_000, remaining_ms)
        checked = client.call(
            (
                "orchestration",
                "check",
                "--terminal",
                coordinator_handle,
                "--types",
                "worker_done,escalation,decision_gate",
                "--wait",
                "--timeout-ms",
                str(window),
            ),
            timeout_ms=min(65_000, window + 5_000),
        )
        checked_result = _result(checked)
        delivery_id = _delivery_id(checked_result)
        matched_completion: Completion | None = None
        for raw_message in _messages(checked_result):
            value = _message_value(raw_message)
            message_type = _field(value, "type", "message_type")
            task_id, message_dispatch_id, payload = _message_ids(value)
            if (
                task_id != prepared.task_id
                or message_dispatch_id != dispatch_id
            ):
                if foreign_message is not None:
                    foreign_message(dict(raw_message))
                continue
            if message_type == "worker_done":
                normalized_payload = dict(payload)
                normalized_payload.pop("outcome", None)
                normalized_payload.setdefault("taskId", prepared.task_id)
                normalized_payload.setdefault("dispatchId", dispatch_id)
                report_path = _field(value, "report_path", "reportPath")
                if (
                    isinstance(report_path, str)
                    and "reportPath" not in normalized_payload
                    and "report_path" not in normalized_payload
                ):
                    normalized_payload["reportPath"] = report_path
                matched_completion = Completion(
                    CompletionKind.WORKER_DONE,
                    prepared.task_id,
                    dispatch_id,
                    json.dumps(
                        normalized_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            elif message_type == "escalation":
                matched_completion = Completion(
                    CompletionKind.ESCALATION,
                    prepared.task_id,
                    dispatch_id,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            elif message_type == "decision_gate":
                matched_completion = Completion(
                    CompletionKind.DECISION_GATE,
                    prepared.task_id,
                    dispatch_id,
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
        if delivery_id is not None:
            client.call(
                (
                    "orchestration",
                    "check",
                    "--terminal",
                    coordinator_handle,
                    "--ack",
                    delivery_id,
                ),
                timeout_ms=30_000,
            )
        if matched_completion is not None:
            return handle, matched_completion
    return (
        handle,
        Completion(
            CompletionKind.STEP_TIMEOUT,
            prepared.task_id,
            dispatch_id,
            None,
        ),
    )
