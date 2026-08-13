from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .contracts import (
    ContractViolationError,
    digest_value,
    parse_human_decision,
    serialize_json,
)
from .ledger import UNRESOLVED_STATUSES, unresolved_scope
from .models import (
    AffectedFile,
    MutationKind,
    MutationRecord,
    AffectedFileOperation,
    ConsensusLedger,
    CoordinatorState,
    DecisionReport,
    DestructiveApproval,
    EscalationTrigger,
    FindingDecision,
    GateBinding,
    GateKind,
    HumanDecision,
    HumanDecisionKind,
    LoopState,
    PlanDocument,
    ScopeManifest,
    Side,
    SignalKind,
    SnapshotIdentity,
    TestGateStatus,
    TransitionSignal,
    UserDecisionNotice,
    UserDecisionNoticeDelivery,
    UserDecisionNoticeDeliveryStatus,
    UserDecisionNoticeStatus,
)
from .orca_client import (
    OrcaClient,
    OrcaProtocolError,
    commit_mutation,
    execute_mutation,
)
from .generation import AtomicWriteError, write_atomic_bytes


class DecisionReportError(RuntimeError):
    """Raised when an evidence-bound decision report cannot be built."""


class GateProtocolError(RuntimeError):
    """Raised when an Orca decision gate violates its binding."""


USER_DECISION_NOTICE_SCHEMA_VERSION = 1
USER_DECISION_NOTICE_NAME = "user-decision-request.json"
USER_DECISION_NOTICE_DELIVERY_NAME = "user-decision-notice-delivery.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notice_request_id(
    run_id: str,
    binding: GateBinding,
) -> str:
    source = "\n".join((run_id, binding.gate_id, binding.report_digest))
    return "notice-" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:24]


def _notice_path(control_dir: Path) -> Path:
    return control_dir / USER_DECISION_NOTICE_NAME


def _notice_delivery_path(control_dir: Path) -> Path:
    return control_dir / USER_DECISION_NOTICE_DELIVERY_NAME


def _strict_notice_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DecisionReportError(
            f"invalid user decision notice: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise DecisionReportError("user decision notice root must be an object")
    return value


def _exact_notice_fields(
    value: dict[str, object],
    expected: set[str],
    context: str,
) -> None:
    if set(value) != expected:
        raise DecisionReportError(
            f"{context} fields mismatch: expected {sorted(expected)}, "
            f"got {sorted(value)}"
        )


def _notice_string(
    value: object,
    name: str,
    *,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise DecisionReportError(f"user decision notice {name} must be nonempty")
    return value


def _notice_options(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise DecisionReportError(
            "user decision notice allowed_options must be a nonempty array"
        )
    if any(not isinstance(item, str) or not item for item in value):
        raise DecisionReportError(
            "user decision notice allowed_options entries must be nonempty"
        )
    return tuple(value)


def _notice_timestamp(value: object, name: str) -> str:
    timestamp = _notice_string(value, name)
    assert timestamp is not None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError as exc:
        raise DecisionReportError(
            f"user decision notice {name} must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise DecisionReportError(
            f"user decision notice {name} must include timezone"
        )
    return timestamp


def _parse_notice(path: Path) -> UserDecisionNotice:
    value = _strict_notice_object(path)
    _exact_notice_fields(
        value,
        {
            "schema_version", "request_id", "status", "run_id",
            "orchestration_run_id", "gate_id", "gate_kind", "blocked_state",
            "report_path", "report_digest", "allowed_options", "reason",
            "created_at", "resolved_at",
        },
        "user decision notice",
    )
    if value["schema_version"] != USER_DECISION_NOTICE_SCHEMA_VERSION:
        raise DecisionReportError("unsupported user decision notice schema")
    try:
        status = UserDecisionNoticeStatus(value["status"])
        gate_kind = GateKind(value["gate_kind"])
        blocked_state = LoopState(value["blocked_state"])
    except (TypeError, ValueError) as exc:
        raise DecisionReportError("user decision notice has invalid enum") from exc
    created_at = _notice_timestamp(value["created_at"], "created_at")
    resolved_at = (
        None
        if value["resolved_at"] is None
        else _notice_timestamp(value["resolved_at"], "resolved_at")
    )
    if status is UserDecisionNoticeStatus.PENDING and resolved_at is not None:
        raise DecisionReportError("pending user decision notice has resolved_at")
    if status is UserDecisionNoticeStatus.RESOLVED and resolved_at is None:
        raise DecisionReportError("resolved user decision notice lacks resolved_at")
    return UserDecisionNotice(
        schema_version=USER_DECISION_NOTICE_SCHEMA_VERSION,
        request_id=_notice_string(value["request_id"], "request_id") or "",
        status=status,
        run_id=_notice_string(value["run_id"], "run_id") or "",
        orchestration_run_id=(
            _notice_string(value["orchestration_run_id"], "orchestration_run_id")
            or ""
        ),
        gate_id=_notice_string(value["gate_id"], "gate_id") or "",
        gate_kind=gate_kind,
        blocked_state=blocked_state,
        report_path=_notice_string(value["report_path"], "report_path") or "",
        report_digest=_notice_string(value["report_digest"], "report_digest") or "",
        allowed_options=_notice_options(value["allowed_options"]),
        reason=_notice_string(value["reason"], "reason") or "",
        created_at=created_at,
        resolved_at=resolved_at,
    )


def read_user_decision_notice(control_dir: Path) -> UserDecisionNotice | None:
    path = _notice_path(control_dir)
    return None if not path.exists() else _parse_notice(path)


def _write_notice(path: Path, value: object) -> None:
    try:
        write_atomic_bytes(path, (serialize_json(value) + "\n").encode("utf-8"))
    except AtomicWriteError as exc:
        raise DecisionReportError(f"failed to persist user decision notice: {path}") from exc


def ensure_user_decision_notice(
    control_dir: Path,
    *,
    state: CoordinatorState,
    binding: GateBinding,
    report_path: Path,
) -> UserDecisionNotice:
    if not state.orchestration_run_id:
        raise DecisionReportError("user decision notice requires Orca Run binding")
    request_id = _notice_request_id(state.run_id, binding)
    existing = read_user_decision_notice(control_dir)
    if existing is not None and existing.status is UserDecisionNoticeStatus.PENDING:
        if existing.request_id != request_id:
            raise DecisionReportError("conflicting pending user decision notice")
        return existing
    reason = (
        state.history[-1].reason
        if state.history
        else "user decision gate is pending"
    )
    notice = UserDecisionNotice(
        schema_version=USER_DECISION_NOTICE_SCHEMA_VERSION,
        request_id=request_id,
        status=UserDecisionNoticeStatus.PENDING,
        run_id=state.run_id,
        orchestration_run_id=state.orchestration_run_id,
        gate_id=binding.gate_id,
        gate_kind=binding.gate_kind,
        blocked_state=state.state,
        report_path=str(report_path),
        report_digest=binding.report_digest,
        allowed_options=binding.allowed_options,
        reason=reason,
        created_at=_utc_now(),
        resolved_at=None,
    )
    _write_notice(_notice_path(control_dir), notice)
    return notice


def resolve_user_decision_notice(
    control_dir: Path,
    *,
    binding: GateBinding,
) -> UserDecisionNotice | None:
    notice = read_user_decision_notice(control_dir)
    if notice is None:
        return None
    expected = _notice_request_id(notice.run_id, binding)
    if notice.request_id != expected:
        raise DecisionReportError("user decision notice does not match gate binding")
    if notice.status is UserDecisionNoticeStatus.RESOLVED:
        return notice
    resolved = replace(
        notice,
        status=UserDecisionNoticeStatus.RESOLVED,
        resolved_at=_utc_now(),
    )
    _write_notice(_notice_path(control_dir), resolved)
    return resolved


def _parse_notice_delivery(path: Path) -> UserDecisionNoticeDelivery:
    value = _strict_notice_object(path)
    _exact_notice_fields(
        value,
        {"schema_version", "request_id", "status", "attempted_at", "error"},
        "user decision notice delivery",
    )
    if value["schema_version"] != USER_DECISION_NOTICE_SCHEMA_VERSION:
        raise DecisionReportError("unsupported user decision notice delivery schema")
    try:
        status = UserDecisionNoticeDeliveryStatus(value["status"])
    except (TypeError, ValueError) as exc:
        raise DecisionReportError("user decision notice delivery has invalid status") from exc
    attempted_at = _notice_timestamp(value["attempted_at"], "attempted_at")
    error = _notice_string(value["error"], "error", allow_none=True)
    if status is UserDecisionNoticeDeliveryStatus.DELIVERED and error is not None:
        raise DecisionReportError("delivered user decision notice has error")
    if status is UserDecisionNoticeDeliveryStatus.FAILED and error is None:
        raise DecisionReportError("failed user decision notice lacks error")
    return UserDecisionNoticeDelivery(
        schema_version=USER_DECISION_NOTICE_SCHEMA_VERSION,
        request_id=_notice_string(value["request_id"], "request_id") or "",
        status=status,
        attempted_at=attempted_at,
        error=error,
    )


def read_user_decision_notice_delivery(
    control_dir: Path,
) -> UserDecisionNoticeDelivery | None:
    path = _notice_delivery_path(control_dir)
    return None if not path.exists() else _parse_notice_delivery(path)


def write_user_decision_notice_delivery(
    control_dir: Path,
    *,
    request_id: str,
    status: UserDecisionNoticeDeliveryStatus,
    error: str | None,
) -> UserDecisionNoticeDelivery:
    if not request_id:
        raise DecisionReportError("user decision notice delivery requires request_id")
    if status is UserDecisionNoticeDeliveryStatus.DELIVERED and error is not None:
        raise DecisionReportError("delivered user decision notice cannot contain error")
    if status is UserDecisionNoticeDeliveryStatus.FAILED and not error:
        raise DecisionReportError("failed user decision notice requires error")
    delivery = UserDecisionNoticeDelivery(
        schema_version=USER_DECISION_NOTICE_SCHEMA_VERSION,
        request_id=request_id,
        status=status,
        attempted_at=_utc_now(),
        error=error,
    )
    _write_notice(_notice_delivery_path(control_dir), delivery)
    return delivery


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except OSError as exc:
        raise DecisionReportError(
            f"failed to write decision report: {path}"
        ) from exc


def _latest_decisions(
    decisions: tuple[FindingDecision, ...],
) -> dict[Side, FindingDecision]:
    latest: dict[Side, FindingDecision] = {}
    for decision in decisions:
        prior = latest.get(decision.side)
        if prior is None or decision.round >= prior.round:
            latest[decision.side] = decision
    return latest


def _position(
    side: Side,
    decisions: Mapping[Side, FindingDecision],
) -> str:
    decision = decisions.get(side)
    if decision is None:
        return "No recorded position."
    evidence = ", ".join(decision.evidence_refs) or "No evidence recorded"
    return (
        f"{decision.decision.value} at round {decision.round}; "
        f"evidence: {evidence}"
    )


def _evidence_exists(reference: str, worktree_path: Path) -> bool:
    candidate = Path(reference)
    if not candidate.is_absolute():
        candidate = worktree_path / candidate
    try:
        candidate.resolve().relative_to(worktree_path.resolve())
    except ValueError:
        return False
    return candidate.exists()


def build_user_decision_report(
    *,
    output_path: Path,
    request_text: str,
    ledger: ConsensusLedger,
    triggers: tuple[EscalationTrigger, ...],
    state: CoordinatorState,
    worktree_path: Path,
    test_status: TestGateStatus | None,
) -> DecisionReport:
    scope = unresolved_scope(ledger)
    unresolved_ids = set(scope.finding_ids)
    unresolved = [
        record
        for record in ledger.findings
        if record.finding.finding_id in unresolved_ids
        and record.status in UNRESOLVED_STATUSES
    ]
    resolved = [
        record
        for record in ledger.findings
        if record.status.value == "RESOLVED"
    ]
    missing_evidence = sorted(
        {
            reference
            for record in unresolved
            for reference in record.finding.evidence_refs
            if not _evidence_exists(reference, worktree_path)
        }
    )
    if any(not record.finding.evidence_refs for record in unresolved):
        raise DecisionReportError(
            "every unresolved finding must contain evidence references"
        )

    lines = [
        "# User Decision Report",
        "",
        "## 1. Original Request and Current Provenance",
        "",
        request_text.strip(),
        "",
        f"- Run ID: `{state.run_id}`",
        f"- Plan version: `{state.plan_version}`",
        f"- Snapshot digest: `{state.snapshot_digest}`",
        "",
        "## 2. Consensus Round Totals",
        "",
        f"- Plan rounds: `{ledger.plan_round}`",
        f"- Code rounds: `{ledger.code_round}`",
        "",
        "## 3. Resolved Items",
        "",
    ]
    if resolved:
        lines.extend(
            f"- `{record.finding.finding_id}`: "
            f"{record.resolution or 'resolved by recorded dual approval'}"
            for record in resolved
        )
    else:
        lines.append("- None")

    lines.extend(
        [
            "",
            "## 4. Unresolved Findings",
            "",
            "| Finding | Status | Severity | Required action |",
            "|---|---|---|---|",
        ]
    )
    if unresolved:
        for record in unresolved:
            action = (
                record.finding.required_change
                or record.finding.required_fix
                or "Additional evidence required"
            )
            lines.append(
                f"| `{record.finding.finding_id}` | "
                f"`{record.status.value}` | "
                f"`{record.finding.severity.value}` | {action} |"
            )
    else:
        lines.append("| None | - | - | - |")

    for heading, side in (
        ("5. Primary Consensus Lane (legacy CLAUDE)", Side.CLAUDE),
        ("6. Secondary Consensus Lane (legacy CODEX)", Side.CODEX),
    ):
        lines.extend(["", f"## {heading}", ""])
        if not unresolved:
            lines.append("- No unresolved findings.")
        for record in unresolved:
            positions = _latest_decisions(record.decisions)
            lines.append(
                f"- `{record.finding.finding_id}`: "
                f"{_position(side, positions)}"
            )

    lines.extend(["", "## 7. Common Ground", ""])
    for record in unresolved:
        positions = _latest_decisions(record.decisions)
        common = (
            "Both consensus lanes recorded the same decision."
            if (
                Side.CLAUDE in positions
                and Side.CODEX in positions
                and positions[Side.CLAUDE].decision
                is positions[Side.CODEX].decision
            )
            else (
                "Both consensus lanes recognize this finding as part of the "
                "bounded unresolved scope."
            )
        )
        lines.append(f"- `{record.finding.finding_id}`: {common}")
    if not unresolved:
        lines.append("- All recorded findings are resolved.")

    lines.extend(["", "## 8. Exact Disagreements", ""])
    if scope.disagreement_excerpts:
        lines.extend(f"- {item}" for item in scope.disagreement_excerpts)
    elif unresolved:
        lines.append(
            "- No explicit opposing decisions were recorded; approval or "
            "verification evidence remains incomplete."
        )
    else:
        lines.append("- None")

    lines.extend(["", "## 9. Source, Test, and Evidence", ""])
    lines.append(
        f"- Test gate: `{test_status.value if test_status else 'NOT_REACHED'}`"
    )
    lines.append(
        "- Affected files: "
        + (", ".join(f"`{item}`" for item in scope.affected_files) or "None")
    )
    lines.append(
        "- Test IDs: "
        + (", ".join(f"`{item}`" for item in scope.test_ids) or "None")
    )
    if missing_evidence:
        lines.append(
            "- Missing evidence paths: "
            + ", ".join(f"`{item}`" for item in missing_evidence)
        )
    for trigger in triggers:
        lines.append(
            f"- `{trigger.code.value}`: {trigger.reason}; evidence: "
            f"{', '.join(trigger.evidence_refs) or 'None'}"
        )

    lines.extend(["", "## 10. Evidence-Backed Options", ""])
    option_count = 0
    for record in unresolved:
        action = record.finding.required_change or record.finding.required_fix
        if not action or not record.finding.evidence_refs:
            continue
        option_count += 1
        lines.append(
            f"- `OPTION_{option_count}` for "
            f"`{record.finding.finding_id}`: {action}"
        )
    if option_count == 0:
        lines.append(
            "- No evidence-backed option is available. Obtain the missing "
            "source/test evidence and a concrete required action before choosing."
        )

    lines.extend(["", "## 11. Option Impact Analysis", ""])
    option_index = 0
    for record in unresolved:
        action = record.finding.required_change or record.finding.required_fix
        if not action or not record.finding.evidence_refs:
            continue
        option_index += 1
        lines.extend(
            [
                f"### OPTION_{option_index}",
                "",
                f"- Behavior: {action}",
                "- Advantages: resolves the recorded blocking requirement.",
                f"- Risks: {record.finding.description}",
                "- Affected files: "
                + (
                    ", ".join(
                        f"`{item}`" for item in record.finding.affected_files
                    )
                    or "None recorded"
                ),
                "- Required tests: "
                + (
                    ", ".join(
                        f"`{item}`" for item in record.finding.test_ids
                    )
                    or "Additional verification must be specified"
                ),
            ]
        )
    if option_index == 0:
        lines.append(
            "- Required information: valid evidence paths and a bounded "
            "implementation or verification proposal."
        )

    lines.extend(
        [
            "",
            "## 12. State if No Decision Is Made",
            "",
            "`USER_DECISION_REQUIRED`: automatic approval is prohibited, "
            "source modification is prohibited, and the next owner is the user.",
            "",
        ]
    )
    text = "\n".join(lines)
    _atomic_write(output_path.resolve(), text)
    raw = output_path.resolve().read_bytes()
    return DecisionReport(
        path=output_path.resolve(),
        digest="sha256:" + hashlib.sha256(raw).hexdigest(),
        finding_ids=scope.finding_ids,
    )


def _result_object(response_json: str) -> dict[str, object]:
    try:
        value = json.loads(response_json)
    except json.JSONDecodeError as exc:
        raise GateProtocolError("Orca gate result is not JSON") from exc
    if not isinstance(value, dict):
        raise GateProtocolError("Orca gate result must be an object")
    return value


def _first_string(
    value: Mapping[str, object],
    *names: str,
) -> str | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def create_gate(
    client: OrcaClient,
    *,
    task_id: str,
    report: DecisionReport,
    gate_kind: GateKind,
    question: str,
    options: tuple[str, ...],
    timeout_ms: int,
    control_dir: Path | None = None,
    generation: int = 0,
    run_id: str = "",
    commit_after_binding: bool = True,
) -> tuple[GateBinding, MutationRecord | None]:
    if not task_id or not question or not options:
        raise GateProtocolError(
            "gate task, question and options must be nonempty"
        )
    # gate-create takes no --run: the gate inherits its Run from --task, which
    # the coordinator already created inside the bound Run.
    argv = (
        "orchestration",
        "gate-create",
        "--task",
        task_id,
        "--question",
        (
            f"{question} Review {report.path} for "
            f"{','.join(report.finding_ids) or 'the final decision'}. "
            f"Report digest: {report.digest}."
        ),
        "--options",
        json.dumps(options, ensure_ascii=False),
    )
    if control_dir is None:
        response = client.call(argv, timeout_ms=timeout_ms)
        mutation = None
    else:
        # A lost gate-create response would block the same task behind two
        # gates, so the request ID replays the original one instead.
        response, mutation = execute_mutation(
            client,
            control_dir,
            kind=MutationKind.GATE_CREATE,
            argv=argv,
            timeout_ms=timeout_ms,
            run_id=run_id,
            generation=generation,
            step_id="user-decision",
            external_id_keys=("gate",),
        )
    value = _result_object(response.result_json)
    nested_gate = value.get("gate")
    gate_value = nested_gate if isinstance(nested_gate, dict) else value
    gate_id = _first_string(gate_value, "id", "gateId", "gate_id")
    returned_task = _first_string(
        gate_value,
        "taskId",
        "task_id",
        "task",
    )
    if gate_id is None:
        raise GateProtocolError("gate-create result has no gate ID")
    if returned_task is not None and returned_task != task_id:
        raise GateProtocolError("gate-create returned a foreign task ID")
    if (
        control_dir is not None
        and mutation is not None
        and commit_after_binding
    ):
        commit_mutation(control_dir, mutation)
    return (
        GateBinding(
            gate_id=gate_id,
            task_id=task_id,
            report_digest=report.digest,
            gate_kind=gate_kind,
            allowed_options=options,
        ),
        mutation,
    )


def find_gate_for_report(
    client: OrcaClient,
    *,
    report: DecisionReport,
    gate_kind: GateKind,
    timeout_ms: int,
    orchestration_run_id: str | None = None,
) -> GateBinding | None:
    command = ["orchestration", "gate-list"]
    if orchestration_run_id:
        command.extend(("--run", orchestration_run_id))
    response = client.call(
        tuple(command),
        timeout_ms=timeout_ms,
    )
    value = _result_object(response.result_json)
    raw_gates = value.get("gates", value.get("items", []))
    if not isinstance(raw_gates, list):
        raise GateProtocolError("gate-list result has no gates array")
    report_path = str(report.path)
    matches: list[tuple[str, str, tuple[str, ...]]] = []
    for item in raw_gates:
        if not isinstance(item, dict):
            continue
        gate_id = _first_string(item, "id", "gateId", "gate_id")
        task_id = _first_string(item, "taskId", "task_id", "task")
        question = item.get("question")
        if (
            gate_id is not None
            and task_id is not None
            and isinstance(question, str)
            and report_path in question
            and report.digest in question
        ):
            raw_options = item.get("options")
            options = (
                tuple(raw_options)
                if isinstance(raw_options, list)
                and all(isinstance(option, str) and option for option in raw_options)
                else ()
            )
            matches.append((gate_id, task_id, options))
    if not matches:
        return None
    if len(matches) != 1:
        raise GateProtocolError(
            "expected at most one gate for the decision report"
        )
    gate_id, task_id, options = matches[0]
    return GateBinding(
        gate_id=gate_id,
        task_id=task_id,
        report_digest=report.digest,
        gate_kind=gate_kind,
        allowed_options=options,
    )


def wait_gate_resolution(
    client: OrcaClient,
    *,
    binding: GateBinding,
    timeout_ms: int,
    orchestration_run_id: str | None = None,
) -> HumanDecision:
    command = [
        "orchestration",
        "gate-list",
        "--task",
        binding.task_id,
        "--status",
        "resolved",
    ]
    if orchestration_run_id:
        command.extend(("--run", orchestration_run_id))
    response = client.call(
        tuple(command),
        timeout_ms=timeout_ms,
    )
    value = _result_object(response.result_json)
    raw_gates = value.get("gates", value.get("items", []))
    if not isinstance(raw_gates, list):
        raise GateProtocolError("gate-list result has no gates array")
    matches = [
        item
        for item in raw_gates
        if isinstance(item, dict)
        and _first_string(item, "id", "gateId", "gate_id")
        == binding.gate_id
    ]
    if len(matches) != 1:
        raise GateProtocolError(
            "expected exactly one resolved gate for the binding"
        )
    resolution = matches[0].get("resolution")
    if isinstance(resolution, dict):
        raw_resolution = json.dumps(resolution, ensure_ascii=False)
    elif isinstance(resolution, str):
        simple_decision = resolution.strip()
        if simple_decision in {
            HumanDecisionKind.MERGE.value,
            HumanDecisionKind.REJECT.value,
        }:
            decision = HumanDecision(
                decision=HumanDecisionKind(simple_decision),
                decision_note=None,
                affected_acceptance_criteria=(),
                affected_finding_ids=(),
                report_digest=binding.report_digest,
            )
            if (
                binding.allowed_options
                and decision.decision.value not in binding.allowed_options
            ):
                raise GateProtocolError(
                    "gate resolution is not one of the bound options"
                )
            return decision
        raw_resolution = resolution
    else:
        raise GateProtocolError("resolved gate has no usable resolution")
    try:
        decision = parse_human_decision(raw_resolution)
    except ContractViolationError as exc:
        raise GateProtocolError(str(exc)) from exc
    if decision.report_digest != binding.report_digest:
        raise GateProtocolError("gate resolution report digest is stale")
    if (
        binding.allowed_options
        and decision.decision.value not in binding.allowed_options
    ):
        raise GateProtocolError(
            "gate resolution is not one of the bound options"
        )
    return decision


def route_gate(
    decision: HumanDecision,
    *,
    gate_kind: GateKind,
) -> TransitionSignal:
    mapping = {
        HumanDecisionKind.MERGE: SignalKind.MERGE,
        HumanDecisionKind.REJECT: SignalKind.REJECT,
        HumanDecisionKind.REVISE_CODE: SignalKind.REVISE_CODE,
        HumanDecisionKind.REVISE_DESIGN: SignalKind.REVISE_DESIGN,
    }
    if gate_kind is GateKind.DESTRUCTIVE:
        if decision.decision is HumanDecisionKind.MERGE:
            return TransitionSignal(
                SignalKind.OK,
                "destructive operations approved",
                decision.affected_finding_ids,
            )
        return TransitionSignal(
            SignalKind.ESCALATE,
            "destructive operations were not approved",
            decision.affected_finding_ids,
        )
    return TransitionSignal(
        mapping[decision.decision],
        decision.decision_note or decision.decision.value,
        decision.affected_finding_ids,
    )


def destructive_gate(
    *,
    run_id: str,
    plan: PlanDocument,
    manifest: ScopeManifest,
    snapshot: SnapshotIdentity,
    binding: GateBinding | None,
    decision: HumanDecision | None,
) -> tuple[DestructiveApproval | None, TransitionSignal]:
    if not run_id:
        raise GateProtocolError("destructive approval run_id is required")
    operations = tuple(
        item
        for item in plan.affected_files
        if item.operation
        in {AffectedFileOperation.DELETE, AffectedFileOperation.RENAME}
    )
    if not operations:
        return None, TransitionSignal(
            SignalKind.OK,
            "no destructive operations are planned",
            (),
        )
    if manifest.snapshot_digest != snapshot.snapshot_digest:
        raise GateProtocolError("destructive manifest snapshot is stale")
    if tuple(
        item
        for item in manifest.affected_files
        if item.operation
        in {AffectedFileOperation.DELETE, AffectedFileOperation.RENAME}
    ) != operations:
        raise GateProtocolError(
            "destructive manifest is not the exact planned operation set"
        )
    if binding is None or decision is None:
        return None, TransitionSignal(
            SignalKind.ESCALATE,
            "destructive operations require explicit user approval",
            (),
        )
    if (
        binding.gate_kind is not GateKind.DESTRUCTIVE
        or decision.report_digest != binding.report_digest
    ):
        raise GateProtocolError("destructive gate provenance mismatch")
    routed = route_gate(decision, gate_kind=GateKind.DESTRUCTIVE)
    if routed.kind is not SignalKind.OK:
        return None, routed
    decision_digest = digest_value(
        json.loads(serialize_json(decision))
    )
    approval = DestructiveApproval(
        run_id=run_id,
        plan_version=plan.plan_version,
        plan_digest=digest_value(json.loads(serialize_json(plan))),
        snapshot_digest=snapshot.snapshot_digest,
        approved_operations=operations,
        gate_id=binding.gate_id,
        decision_digest=decision_digest,
    )
    return approval, routed


def approve_escalation_keys(
    ledger: ConsensusLedger,
    triggers: tuple[EscalationTrigger, ...],
) -> ConsensusLedger:
    keys = tuple(
        dict.fromkeys(
            ledger.approved_escalation_keys
            + tuple(item.deduplication_key for item in triggers)
        )
    )
    return replace(ledger, approved_escalation_keys=keys)
