from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from dataclasses import replace
from typing import Callable, Mapping

from .contracts import canonical_json_bytes
from .generation import (
    find_receipt,
    mark_promoted,
    read_inbox,
    record_receipt,
)
from .models import (
    ActiveStep,
    DeliveryReceipt,
    DispatchObservation,
    InboxClassification,
    MessageEnvelope,
    MutationKind,
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
from .orca_client import (
    OrcaClient,
    OrcaCommandError,
    commit_mutation,
    execute_mutation,
)
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


def _control_dir(step: StepWorkspace) -> Path:
    """The run's control directory, two levels above steps/<step-id>."""
    return step.root.parents[1] / "control"


def _write_delivery_receipt(
    step: StepWorkspace,
    delivery_id: str,
    messages: tuple[dict[str, object], ...],
    quarantine: str | None = None,
) -> None:
    """Persist a full delivery before the coordinator acknowledges it."""
    receipt_dir = step.root / "inbox"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(
        character if character.isalnum() or character in "-_" else "_"
        for character in delivery_id
    )
    if not safe_id:
        raise DispatchProvenanceError("delivery ID has no safe filename")
    target = receipt_dir / f"{safe_id}.json"
    raw = canonical_json_bytes(
        {
            "schema_version": 1,
            "delivery_id": delivery_id,
            "messages": list(messages),
            "quarantine": quarantine,
        }
    ) + b"\n"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)


def _write_artifact_ready(
    step: StepWorkspace,
    payload: dict[str, object],
) -> None:
    """Persist the artifact-ready payload that precedes a worker_done.

    Orca refuses ``--payload`` alongside the structured flags a worker_done
    requires, so the digest and schema version arrive on a separate status
    message.  Its delivery may be acknowledged long before the worker_done
    lands, so the payload has to survive on disk rather than in memory.
    """
    target = step.root / "artifact-ready.json"
    raw = canonical_json_bytes(
        {"schema_version": 1, "payload": payload}
    ) + b"\n"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(target)


def _read_artifact_ready(step: StepWorkspace) -> dict[str, object] | None:
    target = step.root / "artifact-ready.json"
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    payload = value.get("payload")
    return payload if isinstance(payload, dict) else None


def prepare_task(
    client: OrcaClient,
    step: StepWorkspace,
    contract: RenderedContract,
    worker: WorkerHandle,
    role: Role,
    *,
    orchestration_run_id: str,
    generation: int = 0,
    additional_inputs: tuple[StagedInput, ...],
    commit_stage: Callable[[StepStage, ActiveStep], None],
) -> tuple[PreparedTask, InputManifest]:
    if not orchestration_run_id:
        raise StepBindingError("orchestration Run ID is required")
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
    # A lost task-create response would otherwise leave an orphan Task and
    # create a second one for the same step on the next attempt.
    response, mutation = execute_mutation(
        client,
        _control_dir(step),
        kind=MutationKind.TASK_CREATE,
        argv=(
            "orchestration",
            "task-create",
            "--run",
            orchestration_run_id,
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
        run_id=run_id,
        generation=generation,
        step_id=step.step_id,
        external_id_keys=("task",),
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
            "orchestration_run_id": orchestration_run_id,
        },
    )
    commit_stage(StepStage.TASK_CREATED, active)
    # The Task is settled only now that its binding is durable locally.
    commit_mutation(_control_dir(step), mutation)
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


def _dispatch_value(result: Mapping[str, object]) -> str:
    """Read the Dispatch ID.

    ``--return-preamble`` stays on the dispatch argv because the journalled
    mutation replays that exact argv, but the preamble itself is not consumed:
    this harness dispatches without ``--inject`` and delivers the contract over
    the runner's stdin, so the wrapper owns lifecycle signalling.
    """
    dispatch = result.get("dispatch")
    if not isinstance(dispatch, dict):
        raise DispatchProvenanceError(
            "dispatch response is missing dispatch"
        )
    dispatch_id = dispatch.get("id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        raise DispatchProvenanceError("dispatch ID is missing")
    return dispatch_id


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


def _is_json_object(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


def _mark_receipt_acked(control_dir: Path, delivery_id: str) -> None:
    receipt = find_receipt(control_dir, delivery_id)
    if receipt is None or receipt.acked:
        return
    record_receipt(control_dir, replace(receipt, acked=True))


def parse_delivery(
    result: Mapping[str, object],
) -> tuple[str | None, tuple[MessageEnvelope, ...]]:
    """Turn one check result into exact envelopes, skipping nothing.

    A row that cannot be understood is an error rather than a silent drop: the
    delivery replays until acknowledged, so dropping a row here would either
    lose it or spin forever.
    """
    delivery_id = _delivery_id(result)
    raw = result.get("messages", [])
    if not isinstance(raw, list):
        raise DispatchProvenanceError(
            "orchestration check messages must be an array"
        )
    envelopes: list[MessageEnvelope] = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict):
            raise DispatchProvenanceError(
                f"delivered message {index} is not an object"
            )
        value = _message_value(row)
        task_id, dispatch_id, payload = _message_ids(row)
        message_type = _field(value, "type", "message_type")
        if not isinstance(message_type, str) or not message_type:
            raise DispatchProvenanceError(
                f"delivered message {index} has no type"
            )
        message_id = _field(value, "id", "message_id", "messageId")
        if not isinstance(message_id, str) or not message_id:
            # Orca always stamps an ID; a row without one cannot be tracked
            # across a replay, so it is not safe to promote or acknowledge.
            raise DispatchProvenanceError(
                f"delivered message {index} has no ID"
            )
        from_handle = _field(value, "from_handle", "fromHandle", "from")
        run_id = _field(value, "run_id", "runId")
        # Both places may carry the IDs; disagreement means the envelope and
        # its payload describe different work.
        top_task = _field(value, "task_id", "taskId")
        top_dispatch = _field(value, "dispatch_id", "dispatchId")
        payload_task = _field(payload, "task_id", "taskId")
        payload_dispatch = _field(payload, "dispatch_id", "dispatchId")
        if (
            isinstance(top_task, str)
            and isinstance(payload_task, str)
            and top_task != payload_task
        ):
            raise DispatchProvenanceError(
                f"delivered message {index} task ID conflicts with its payload"
            )
        if (
            isinstance(top_dispatch, str)
            and isinstance(payload_dispatch, str)
            and top_dispatch != payload_dispatch
        ):
            raise DispatchProvenanceError(
                f"delivered message {index} dispatch ID conflicts with payload"
            )
        # Orca folds --report-path into the payload, but a build that reports
        # it beside the payload must not lose it.
        top_report = _field(value, "report_path", "reportPath")
        if (
            isinstance(top_report, str)
            and "reportPath" not in payload
            and "report_path" not in payload
        ):
            payload = {**payload, "reportPath": top_report}
        # A payload that is not a JSON object is preserved verbatim so the
        # receipt records what actually arrived; classification quarantines it
        # rather than guessing at its meaning.
        raw_payload = value.get("payload")
        malformed = isinstance(raw_payload, str) and not _is_json_object(
            raw_payload
        )
        envelopes.append(
            MessageEnvelope(
                message_id=message_id,
                message_type=message_type,
                from_handle=(
                    from_handle if isinstance(from_handle, str) else ""
                ),
                run_id=run_id if isinstance(run_id, str) else "",
                task_id=task_id,
                dispatch_id=dispatch_id,
                payload_json=(
                    raw_payload
                    if malformed
                    else json.dumps(
                        payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                ),
            )
        )
    if len(envelopes) != len(raw):
        raise DispatchProvenanceError("delivery row count invariant broken")
    return delivery_id, tuple(envelopes)


def classify_delivery(
    control_dir: Path,
    envelopes: tuple[MessageEnvelope, ...],
    *,
    task_id: str,
    dispatch_id: str,
) -> tuple[InboxClassification, ...]:
    """Give every delivered row a decision, including the ones we ignore."""
    promoted = set(read_inbox(control_dir).promoted_message_ids)
    classifications: list[InboxClassification] = []
    for envelope in envelopes:
        if envelope.message_id in promoted:
            # Already became a domain event before the ACK was lost.
            classifications.append(InboxClassification.DUPLICATE)
        elif not _is_json_object(envelope.payload_json):
            # Kept and acknowledged, never acted on: raising here would let a
            # single malformed row replay forever and block the run.
            classifications.append(InboxClassification.QUARANTINED)
        elif (
            envelope.task_id != task_id
            or envelope.dispatch_id != dispatch_id
        ):
            # Another step's or another run's traffic: recorded, not applied.
            classifications.append(InboxClassification.DEFERRED)
        else:
            classifications.append(InboxClassification.ACCEPTED)
    return tuple(classifications)


def dispatch_and_wait(
    client: OrcaClient,
    prepared: PreparedTask,
    step: StepWorkspace,
    profile: LaunchProfile,
    *,
    orchestration_run_id: str,
    generation: int = 0,
    coordinator_handle: str,
    orca_executable: str,
    runner_path: Path,
    step_timeout_ms: int,
    artifact_filename: str,
    commit_dispatched: Callable[[DispatchHandle], None],
    foreign_message: Callable[[dict[str, object]], None] | None = None,
) -> tuple[DispatchHandle, Completion]:
    if not orchestration_run_id:
        raise DispatchProvenanceError("orchestration Run ID is required")
    # Re-dispatching the same Task would place a second worker on it, so the
    # dispatch is replayed by request ID rather than reissued.
    response, mutation = execute_mutation(
        client,
        _control_dir(step),
        kind=MutationKind.DISPATCH,
        argv=(
            "orchestration",
            "dispatch",
            "--run",
            orchestration_run_id,
            "--task",
            prepared.task_id,
            "--to",
            prepared.worker.terminal_handle,
            "--from",
            coordinator_handle,
            "--return-preamble",
        ),
        timeout_ms=30_000,
        run_id=step.root.parents[1].name,
        generation=generation,
        step_id=step.step_id,
        external_id_keys=("dispatch",),
    )
    dispatch_id = _dispatch_value(_result(response))
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
    # The Dispatch is settled once its ID is durable in the step binding.
    commit_mutation(_control_dir(step), mutation)

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
        # runs/<run-id>/logs; the runner persists the agent command line and
        # both output streams there whatever the exit code turns out to be.
        "log_dir": str((step.root.parents[1] / "logs").resolve()),
        "step_id": step.step_id,
        "orchestration_run_id": orchestration_run_id,
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
                "--run",
                orchestration_run_id,
                "--terminal",
                coordinator_handle,
                "--types",
                "worker_done,escalation,decision_gate,status",
                "--wait",
                "--timeout-ms",
                str(window),
            ),
            timeout_ms=min(65_000, window + 5_000),
        )
        checked_result = _result(checked)
        messages = _messages(checked_result)
        control_dir = _control_dir(step)
        # Parse and classify the whole batch, then keep it durable, before any
        # of it is acted on.  The delivery replays until acknowledged, so this
        # is what makes "process every message" survive a crash.
        delivery_id, envelopes = parse_delivery(checked_result)
        classifications = classify_delivery(
            control_dir,
            envelopes,
            task_id=prepared.task_id,
            dispatch_id=dispatch_id,
        )
        if delivery_id is not None:
            record_receipt(
                control_dir,
                DeliveryReceipt(
                    schema_version=1,
                    delivery_id=delivery_id,
                    messages=envelopes,
                    classifications=classifications,
                    acked=False,
                ),
            )
            _write_delivery_receipt(step, delivery_id, messages)
        matched_completion: Completion | None = None
        promoted_ids: list[str] = []
        for envelope, classification in zip(envelopes, classifications):
            if classification is InboxClassification.DEFERRED:
                if foreign_message is not None:
                    foreign_message({"message": envelope.message_id})
                continue
            if classification in {
                InboxClassification.DUPLICATE,
                InboxClassification.QUARANTINED,
            }:
                # Already promoted before an acknowledgement was lost, or not
                # understandable at all.  Recorded and acknowledged, never
                # promoted a second time and never acted on.
                continue
            # QUARANTINED records preserve malformed payload bytes so they can
            # be audited.  Never parse those bytes after classification: doing
            # so would raise before the ACK and recreate the replay deadlock
            # the quarantine path is meant to prevent.
            if classification is InboxClassification.QUARANTINED:
                continue
            payload = json.loads(envelope.payload_json)
            message_type = envelope.message_type
            if message_type == "status":
                # Stage one of the settlement handshake.  Keep it durable and
                # keep waiting: only the worker_done ends the step.
                if "artifactDigest" in payload or "artifact_digest" in payload:
                    _write_artifact_ready(step, dict(payload))
                    promoted_ids.append(envelope.message_id)
                continue
            if message_type == "worker_done":
                normalized_payload = dict(payload)
                outcome = normalized_payload.pop("outcome", None)
                if outcome == "failed":
                    matched_completion = Completion(
                        CompletionKind.ESCALATION,
                        prepared.task_id,
                        dispatch_id,
                        json.dumps(
                            {
                                "reason": normalized_payload.get(
                                    "reason",
                                    "worker runner reported failure",
                                ),
                                "evidence_paths": normalized_payload.get(
                                    "evidence_paths",
                                    (),
                                ),
                                **(
                                    {
                                        "reason_code": normalized_payload[
                                            "reason_code"
                                        ]
                                    }
                                    if isinstance(
                                        normalized_payload.get("reason_code"),
                                        str,
                                    )
                                    else {}
                                ),
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        delivery_id,
                    )
                    promoted_ids.append(envelope.message_id)
                    continue
                if outcome not in {None, "succeeded"}:
                    # A bound Run replays the same Delivery until it is
                    # acknowledged, so failing closed without acknowledging
                    # would replay this message on every resume and block the
                    # run forever.  Quarantine the durable receipt, release the
                    # mailbox, and then fail.
                    reason = "worker_done outcome is invalid"
                    if delivery_id is not None:
                        _write_delivery_receipt(
                            step,
                            delivery_id,
                            messages,
                            quarantine=reason,
                        )
                        acknowledge_delivery(
                            client,
                            orchestration_run_id=orchestration_run_id,
                            coordinator_handle=coordinator_handle,
                            delivery_id=delivery_id,
                        )
                    raise DispatchProvenanceError(reason)
                # Stage two only settles the Dispatch.  The digest and schema
                # version the contract needs came in on the artifact-ready
                # status message, so rejoin them here.
                ready = _read_artifact_ready(step)
                if ready is None:
                    reason = "worker_done arrived without an artifact-ready"
                    if delivery_id is not None:
                        _write_delivery_receipt(
                            step,
                            delivery_id,
                            messages,
                            quarantine=reason,
                        )
                        acknowledge_delivery(
                            client,
                            orchestration_run_id=orchestration_run_id,
                            coordinator_handle=coordinator_handle,
                            delivery_id=delivery_id,
                        )
                    raise DispatchProvenanceError(reason)
                for key, item in ready.items():
                    normalized_payload.setdefault(key, item)
                normalized_payload.setdefault("taskId", prepared.task_id)
                normalized_payload.setdefault("dispatchId", dispatch_id)
                report_path = _field(payload, "report_path", "reportPath")
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
                    delivery_id,
                )
                promoted_ids.append(envelope.message_id)
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
                    delivery_id,
                )
                promoted_ids.append(envelope.message_id)
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
                    delivery_id,
                )
                promoted_ids.append(envelope.message_id)
        # Promotions are durable before the acknowledgement, so a crash in
        # between replays the delivery and finds it already classified.
        mark_promoted(control_dir, tuple(promoted_ids))
        # For a worker completion, acknowledge only after the coordinator has
        # durably committed WORKER_DONE_RECEIVED.  Other messages have a
        # durable receipt and can be acknowledged now.
        if delivery_id is not None and (
            matched_completion is None
            or matched_completion.kind is not CompletionKind.WORKER_DONE
        ):
            client.call(
                (
                    "orchestration",
                    "check",
                    "--run",
                    orchestration_run_id,
                    "--terminal",
                    coordinator_handle,
                    "--ack",
                    delivery_id,
                ),
                timeout_ms=30_000,
            )
            _mark_receipt_acked(control_dir, delivery_id)
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


def _terminal_alive(client: OrcaClient, handle: str) -> bool:
    """Local copy: session.py imports this module, so it cannot be imported."""
    if not handle:
        return False
    try:
        client.call(
            ("terminal", "show", "--terminal", handle),
            timeout_ms=10_000,
        )
    except OrcaCommandError:
        return False
    return True


def observe_dispatch(
    client: OrcaClient,
    *,
    task_id: str,
    timeout_ms: int = 30_000,
) -> DispatchObservation | None:
    """Ask Orca what actually happened to a step's Dispatch.

    The ``worker-*`` family only covers workers started by ``worker-start``;
    this harness dispatches to terminals it created itself, so ``dispatch-show``
    is the authoritative source for those.  Returns ``None`` when Orca has no
    record, which the caller must treat as unknown rather than as finished.
    """
    if not task_id:
        raise DispatchProvenanceError("task ID is required to observe")
    try:
        response = client.call(
            ("orchestration", "dispatch-show", "--task", task_id),
            timeout_ms=timeout_ms,
        )
    except OrcaCommandError:
        # A transient lookup failure is not evidence that nothing is running.
        return None
    value = _result(response).get("dispatch")
    if not isinstance(value, dict):
        return None
    dispatch_id = value.get("id")
    status = value.get("status")
    if not isinstance(dispatch_id, str) or not isinstance(status, str):
        raise DispatchProvenanceError("dispatch-show result is malformed")
    assignee = value.get("assignee_handle")
    assignee_handle = assignee if isinstance(assignee, str) else ""
    failure_count = value.get("failure_count")
    completed_at = value.get("completed_at")
    return DispatchObservation(
        dispatch_id=dispatch_id,
        task_id=task_id,
        status=status,
        assignee_handle=assignee_handle,
        assignee_alive=(
            bool(assignee_handle) and _terminal_alive(client, assignee_handle)
        ),
        failure_count=(
            failure_count if isinstance(failure_count, int) else 0
        ),
        completed_at=(
            completed_at if isinstance(completed_at, str) else None
        ),
    )


def acknowledge_delivery(
    client: OrcaClient,
    *,
    orchestration_run_id: str,
    coordinator_handle: str,
    delivery_id: str | None,
) -> None:
    if delivery_id is None:
        return
    client.call(
        (
            "orchestration",
            "check",
            "--run",
            orchestration_run_id,
            "--terminal",
            coordinator_handle,
            "--ack",
            delivery_id,
        ),
        timeout_ms=30_000,
    )
