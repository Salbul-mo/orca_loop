from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Callable

from .contracts import (
    ContractViolationError,
    parse_implementation_artifact,
    parse_plan_document,
    parse_review_artifact,
    parse_worker_done,
)
from .dispatcher import (
    acknowledge_delivery,
    observe_dispatch,
    dispatch_and_wait,
    prepare_task,
    worker_for_role,
)
from .generation import commit_generation
from .guards import capture_file_state, guard_repository_delta
from .ledger import (
    InvalidRoundError,
    apply_implementation_artifact,
    apply_plan_document,
    apply_review_artifact,
    commit_round,
)
from .machine import TERMINAL_STATES, transition
from .models import (
    ResumeOutcome,
    DispatchObservation,
    ActiveStep,
    ArtifactKind,
    CompletionKind,
    ConsensusKind,
    ConsensusLedger,
    CoordinatorState,
    DestructiveApproval,
    DispatchHandle,
    EscalationCode,
    EscalationTrigger,
    ExpectedProvenance,
    GateKind,
    HumanDecision,
    ImplementationArtifact,
    LaunchProfile,
    LedgerUpdate,
    LedgerView,
    LoopConfig,
    LoopState,
    PlanDocument,
    RenderedContract,
    ResumeAction,
    ResumeDecision,
    Role,
    RoundEvidence,
    RunStatus,
    RunWorkspace,
    ScopePackage,
    Side,
    SignalKind,
    StateHistoryEntry,
    StepExecutionResult,
    StepStage,
    StepWorkspace,
    StagedInput,
    TestExecutionPolicy,
    TestGateStatus,
    TransitionSignal,
    Violation,
    WorkerPool,
)
from .orca_client import OrcaClient
from .reporting import (
    record_artifact_history,
    render_run_summary,
    render_stage_report,
)
from .roles import ARTIFACT_FILENAMES
from .snapshot import capture_snapshot
from .testrunner import run_tests
from .transport import promote_artifact


class OrcaLoopError(RuntimeError):
    """Base error for deterministic coordinator failures."""


class CoordinatorContractError(OrcaLoopError):
    """Raised when a coordinator input violates the state contract."""


class CoordinatorGuardError(OrcaLoopError):
    """Raised when repository or artifact scope guards reject a result."""

    def __init__(
        self,
        violations: tuple[Violation, ...],
        active: ActiveStep,
        permission_report_digest: str,
    ) -> None:
        self.violations = violations
        self.active = active
        self.permission_report_digest = permission_report_digest
        super().__init__(
            "; ".join(
                f"{item.code}:{item.path}:{item.detail}"
                for item in violations
            )
        )


class CoordinatorPermissionError(OrcaLoopError):
    """Raised for an allowlisted, typed worker permission observation."""

    def __init__(
        self,
        reason_code: str,
        active: ActiveStep,
        evidence_paths: tuple[str, ...],
        permission_report_digest: str,
    ) -> None:
        self.reason_code = reason_code
        self.active = active
        self.evidence_paths = evidence_paths
        self.permission_report_digest = permission_report_digest
        super().__init__(f"worker permission observation: {reason_code}")


PERMISSION_REASON_CODES = frozenset(
    {
        "OUTBOX_WRITE_DENIED",
        "SOURCE_DIRECTORY_READ_DENIED",
        "PROCESS_EXECUTION_DENIED",
    }
)


class ResumeAmbiguityError(OrcaLoopError):
    """Raised when durable and live state cannot be reconciled uniquely."""


WORKER_STATES = {
    LoopState.PLAN,
    LoopState.PLAN_REVISE,
    LoopState.PLAN_REVIEW,
    LoopState.IMPLEMENT,
    LoopState.FIX,
    LoopState.CODE_REVIEW,
    LoopState.CROSS_CONFIRM,
}
EVALUATE_STATES = {
    LoopState.PLAN_CONSENSUS_EVALUATE,
    LoopState.CONSENSUS_EVALUATE,
}
ROLE_BY_STATE = {
    LoopState.PLAN: Role.PLANNER,
    LoopState.PLAN_REVISE: Role.PLANNER,
    LoopState.PLAN_REVIEW: Role.PLAN_REVIEWER,
    LoopState.IMPLEMENT: Role.IMPLEMENTER,
    LoopState.FIX: Role.IMPLEMENTER,
    LoopState.CODE_REVIEW: Role.CODE_REVIEWER,
    LoopState.CROSS_CONFIRM: Role.CROSS_CONFIRMER,
}
ARTIFACT_BY_ROLE = {
    Role.PLANNER: ArtifactKind.PLAN,
    Role.PLAN_REVIEWER: ArtifactKind.PLAN_REVIEW,
    Role.IMPLEMENTER: ArtifactKind.IMPLEMENTATION,
    Role.CODE_REVIEWER: ArtifactKind.CODE_REVIEW,
    Role.CROSS_CONFIRMER: ArtifactKind.CROSS_REVIEW,
}


def ledger_view(ledger: ConsensusLedger) -> LedgerView:
    return LedgerView(
        plan_round=ledger.plan_round,
        code_round=ledger.code_round,
        unresolved_count=sum(
            record.status.value
            in {"OPEN", "CHANGE_REQUIRED", "VERIFY_REQUIRED"}
            for record in ledger.findings
        ),
        approved_escalation_keys=ledger.approved_escalation_keys,
    )


def consensus_round(state: LoopState, ledger: ConsensusLedger) -> int:
    if state in {
        LoopState.PLAN,
        LoopState.PLAN_REVISE,
        LoopState.PLAN_REVIEW,
    }:
        return ledger.plan_round + 1
    if state in {
        LoopState.IMPLEMENT,
        LoopState.FIX,
        LoopState.CODE_REVIEW,
        LoopState.CROSS_CONFIRM,
    }:
        return max(1, ledger.code_round + 1)
    raise CoordinatorContractError(
        f"state has no consensus round: {state.value}"
    )


def role_for_state(state: LoopState) -> Role:
    try:
        return ROLE_BY_STATE[state]
    except KeyError as exc:
        raise CoordinatorContractError(
            f"state is not a worker state: {state.value}"
        ) from exc


def _parser_and_handler(
    role: Role,
    *,
    expected: ExpectedProvenance,
    delivered_finding_ids: tuple[str, ...],
) -> tuple[
    ArtifactKind,
    Callable[[str], object],
    Callable[[ConsensusLedger, object], LedgerUpdate],
]:
    if role is Role.PLANNER:
        parser = lambda raw: parse_plan_document(
            raw,
            delivered_finding_ids=delivered_finding_ids,
            expected_snapshot_digest=expected.snapshot_digest,
            expected_round=expected.consensus_round,
        )
        handler = lambda ledger, artifact: apply_plan_document(
            ledger,
            artifact,  # type: ignore[arg-type]
        )
    elif role is Role.IMPLEMENTER:
        parser = lambda raw: parse_implementation_artifact(
            raw,
            expected,
            delivered_finding_ids=delivered_finding_ids,
        )
        handler = lambda ledger, artifact: apply_implementation_artifact(
            ledger,
            artifact,  # type: ignore[arg-type]
        )
    else:
        kind = ARTIFACT_BY_ROLE[role]
        parser = lambda raw: parse_review_artifact(
            raw,
            kind,
            expected,
            delivered_finding_ids=delivered_finding_ids,
        )
        side = (
            Side.CLAUDE
            if role is Role.CODE_REVIEWER
            else Side.CODEX
        )
        handler = lambda ledger, artifact: apply_review_artifact(
            ledger,
            artifact,  # type: ignore[arg-type]
            side,
        )
    return ARTIFACT_BY_ROLE[role], parser, handler


def apply_worker_artifact(
    state: LoopState,
    ledger: ConsensusLedger,
    raw_text: str,
    *,
    expected: ExpectedProvenance,
    delivered_finding_ids: tuple[str, ...],
) -> tuple[object, LedgerUpdate]:
    role = role_for_state(state)
    _, parser, handler = _parser_and_handler(
        role,
        expected=expected,
        delivered_finding_ids=delivered_finding_ids,
    )
    artifact = parser(raw_text)
    return artifact, handler(ledger, artifact)


class GenerationController:
    def __init__(
        self,
        workspace: RunWorkspace,
        state: CoordinatorState,
        ledger: ConsensusLedger,
    ) -> None:
        self.workspace = workspace
        self.state = state
        self.ledger = ledger

    def commit(
        self,
        *,
        stage: StepStage,
        active: ActiveStep | None,
        reason: str,
        signal: SignalKind = SignalKind.OK,
        state_value: LoopState | None = None,
        status: RunStatus | None = None,
        plan_version: int | None = None,
        test_gate_status: TestGateStatus | None = None,
        snapshot_digest: str | None = None,
        ledger: ConsensusLedger | None = None,
        counters=None,
        gate_binding=None,
        human_decision=None,
        destructive_approval=None,
        blocked_from_state: LoopState | None = None,
        pending_escalations: tuple[EscalationTrigger, ...] | None = None,
        clear_gate: bool = False,
        clear_blocked_context: bool = False,
    ) -> CoordinatorState:
        next_generation = self.state.generation + 1
        next_ledger = replace(
            self.ledger if ledger is None else ledger,
            generation=next_generation,
        )
        next_state_value = (
            self.state.state if state_value is None else state_value
        )
        history = self.state.history + (
            StateHistoryEntry(
                generation=next_generation,
                state=next_state_value,
                step_stage=stage,
                signal=signal,
                reason=reason,
            ),
        )
        next_state = replace(
            self.state,
            generation=next_generation,
            state=next_state_value,
            step_stage=stage,
            active=active,
            status=self.state.status if status is None else status,
            plan_version=(
                self.state.plan_version
                if plan_version is None
                else plan_version
            ),
            test_gate_status=(
                self.state.test_gate_status
                if test_gate_status is None
                else test_gate_status
            ),
            snapshot_digest=(
                self.state.snapshot_digest
                if snapshot_digest is None
                else snapshot_digest
            ),
            counters=(
                self.state.counters if counters is None else counters
            ),
            gate_binding=(
                None
                if clear_gate
                else (
                    self.state.gate_binding
                    if gate_binding is None
                    else gate_binding
                )
            ),
            human_decision=(
                self.state.human_decision
                if human_decision is None
                else human_decision
            ),
            destructive_approval=(
                self.state.destructive_approval
                if destructive_approval is None
                else destructive_approval
            ),
            blocked_from_state=(
                None
                if clear_blocked_context
                else (
                    self.state.blocked_from_state
                    if blocked_from_state is None
                    else blocked_from_state
                )
            ),
            pending_escalations=(
                ()
                if clear_blocked_context
                else (
                    self.state.pending_escalations
                    if pending_escalations is None
                    else pending_escalations
                )
            ),
            history=history,
        )
        commit_generation(
            self.workspace.control_dir,
            next_state,
            next_ledger,
        )
        self.state = next_state
        self.ledger = next_ledger
        # Refresh the readable summary only after the durable commit has
        # succeeded; render_run_summary swallows its own failures so that
        # reporting can never turn a committed generation into an error.
        render_run_summary(
            self.workspace.root,
            next_state,
            next_ledger,
            harness_root=self.workspace.root.parents[1],
        )
        return next_state


def execute_worker_step(
    *,
    controller: GenerationController,
    step: StepWorkspace,
    client: OrcaClient,
    pool: WorkerPool,
    profile: LaunchProfile,
    contract: RenderedContract,
    additional_inputs: tuple[StagedInput, ...],
    worktree: Path,
    scope: ScopePackage,
    affected_files,
    destructive_approval: DestructiveApproval | None,
    runner_path: Path,
    orca_executable: str,
    step_timeout_ms: int,
    validate_artifact: Callable[[object], None] | None = None,
) -> tuple[StepExecutionResult, object | None]:
    loop_state = controller.state.state
    role = role_for_state(loop_state)
    worker = worker_for_role(pool, role)
    before_snapshot = capture_snapshot(worktree)
    before_files = capture_file_state(worktree)

    def commit_stage(stage: StepStage, active: ActiveStep) -> None:
        controller.commit(
            stage=stage,
            active=active,
            reason=f"{loop_state.value} {stage.value}",
        )

    prepared, manifest = prepare_task(
        client,
        step,
        contract,
        worker,
        role,
        orchestration_run_id=controller.state.orchestration_run_id or "",
        generation=controller.state.generation,
        additional_inputs=additional_inputs,
        commit_stage=commit_stage,
    )

    def commit_dispatched(handle: DispatchHandle) -> None:
        commit_stage(
            StepStage.STEP_DISPATCHED,
            ActiveStep(
                step_id=handle.step_id,
                task_id=handle.task_id,
                dispatch_id=handle.dispatch_id,
                role=handle.role,
                worker=handle.worker,
            ),
        )

    handle, completion = dispatch_and_wait(
        client,
        prepared,
        step,
        profile,
        orchestration_run_id=controller.state.orchestration_run_id or "",
        generation=controller.state.generation,
        coordinator_handle=controller.state.coordinator_handle,
        orca_executable=orca_executable,
        runner_path=runner_path,
        step_timeout_ms=step_timeout_ms,
        artifact_filename=ARTIFACT_FILENAMES[role],
        commit_dispatched=commit_dispatched,
    )
    active = ActiveStep(
        step_id=handle.step_id,
        task_id=handle.task_id,
        dispatch_id=handle.dispatch_id,
        role=handle.role,
        worker=handle.worker,
    )
    if completion.kind is CompletionKind.STEP_TIMEOUT:
        return (
            StepExecutionResult(
                TransitionSignal(
                    SignalKind.ABORT,
                    "worker step timed out",
                    scope.finding_ids,
                ),
                controller.ledger,
                controller.state.test_gate_status,
            ),
            None,
        )
    if completion.kind is CompletionKind.ESCALATION:
        payload_text = completion.payload_json or ""
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            reason_code = payload.get("reason_code")
            evidence_paths = payload.get("evidence_paths", ())
            if (
                isinstance(reason_code, str)
                and reason_code in PERMISSION_REASON_CODES
                and isinstance(evidence_paths, list)
                and all(isinstance(item, str) for item in evidence_paths)
            ):
                raise CoordinatorPermissionError(
                    reason_code,
                    active,
                    tuple(evidence_paths),
                    controller.state.permission_report_digest,
                )
        return (
            StepExecutionResult(
                TransitionSignal(
                    SignalKind.ESCALATE,
                    completion.payload_json or "native escalation",
                    scope.finding_ids,
                ),
                controller.ledger,
                controller.state.test_gate_status,
            ),
            None,
        )
    if completion.kind is CompletionKind.DECISION_GATE:
        return (
            StepExecutionResult(
                TransitionSignal(
                    SignalKind.ESCALATE,
                    "worker requested an unapproved decision gate",
                    scope.finding_ids,
                ),
                controller.ledger,
                controller.state.test_gate_status,
            ),
            None,
        )
    if completion.payload_json is None:
        raise CoordinatorContractError(
            "worker_done completion has no payload"
        )
    controller.commit(
        stage=StepStage.WORKER_DONE_RECEIVED,
        active=active,
        reason=f"{loop_state.value} worker_done received",
    )
    acknowledge_delivery(
        client,
        orchestration_run_id=controller.state.orchestration_run_id or "",
        coordinator_handle=controller.state.coordinator_handle,
        delivery_id=completion.delivery_id,
    )
    expected = ExpectedProvenance(
        run_id=controller.state.run_id,
        task_id=handle.task_id,
        dispatch_id=handle.dispatch_id,
        consensus_round=consensus_round(
            loop_state,
            controller.ledger,
        ),
        snapshot_digest=controller.state.snapshot_digest,
    )
    payload = parse_worker_done(completion.payload_json, expected)
    artifact_kind, parser, handler = _parser_and_handler(
        role,
        expected=expected,
        delivered_finding_ids=scope.finding_ids,
    )
    promoted = promote_artifact(
        payload,
        handle,
        step,
        manifest,
        controller.workspace.artifact_dir,
        artifact_kind,
        parser,
    )
    artifact = parser(promoted.raw_text)
    if validate_artifact is not None:
        validate_artifact(artifact)
    # Keep the promoted artifact addressable after the next revision
    # overwrites artifacts/<kind>.json, and render it for a human reader.
    next_generation = controller.state.generation + 1
    record_artifact_history(
        controller.workspace.root,
        artifact_kind.value,
        next_generation,
        promoted.raw_text.encode("utf-8"),
    )
    render_stage_report(
        controller.workspace.root,
        artifact_kind.value,
        promoted.raw_text,
        next_generation,
    )
    after_snapshot = capture_snapshot(worktree)
    after_files = capture_file_state(worktree)
    guard = guard_repository_delta(
        before_snapshot,
        after_snapshot,
        role,
        affected_files,
        destructive_approval,
        before_files=before_files,
        after_files=after_files,
    )
    if not guard.ok:
        raise CoordinatorGuardError(
            guard.violations,
            active,
            controller.state.permission_report_digest,
        )
    update = handler(controller.ledger, artifact)
    plan_version = (
        artifact.plan_version
        if isinstance(artifact, PlanDocument)
        else controller.state.plan_version
    )
    controller.commit(
        stage=StepStage.ARTIFACT_VERIFIED,
        active=active,
        reason=f"{loop_state.value} artifact verified",
        ledger=update.ledger,
        plan_version=plan_version,
        snapshot_digest=after_snapshot.snapshot_digest,
    )
    signal = TransitionSignal(
        (
            SignalKind.ESCALATE
            if update.escalations
            else SignalKind.ARTIFACT_OK
        ),
        (
            "; ".join(item.reason for item in update.escalations)
            if update.escalations
            else f"{artifact_kind.value} artifact accepted"
        ),
        scope.finding_ids,
    )
    return (
        StepExecutionResult(
            signal,
            controller.ledger,
            controller.state.test_gate_status,
            update.escalations,
        ),
        artifact,
    )


def execute_evaluate(
    *,
    state: LoopState,
    ledger: ConsensusLedger,
    evidence: RoundEvidence,
    config: LoopConfig,
    plan: PlanDocument | None,
    destructive_approval: DestructiveApproval | None,
) -> StepExecutionResult:
    if state not in EVALUATE_STATES:
        raise CoordinatorContractError(
            f"not an evaluate state: {state.value}"
        )
    kind = (
        ConsensusKind.PLAN
        if state is LoopState.PLAN_CONSENSUS_EVALUATE
        else ConsensusKind.CODE
    )
    update = commit_round(
        ledger,
        evidence,
        plan_limit=config.plan_consensus_round_limit,
        code_limit=config.code_consensus_round_limit,
        expected_plan_version=(
            None if plan is None else plan.plan_version
        ),
        expected_snapshot_digest=evidence.reviewed_snapshot_digest,
    )
    if not update.committed_round:
        return StepExecutionResult(
            TransitionSignal(
                SignalKind.OPERATIONAL_RETRY,
                "consensus evidence did not form a valid round",
                (),
            ),
            update.ledger,
            None,
            (),
        )
    escalations = list(update.escalations)
    view = ledger_view(update.ledger)
    if (
        kind is ConsensusKind.PLAN
        and view.unresolved_count == 0
        and plan is not None
    ):
        if plan.data_api_schema_changes.strip() not in {"", "없음", "none", "None"}:
            key = (
                f"E-03:{plan.plan_version}:"
                f"{evidence.artifact_digests[0]}"
            )
            if key not in update.ledger.approved_escalation_keys:
                escalations.append(
                    EscalationTrigger(
                        EscalationCode.E03,
                        "data, API, or schema contract change requires user approval",
                        plan.current_state_evidence,
                        key,
                    )
                )
        destructive = tuple(
            item
            for item in plan.affected_files
            if item.operation.value in {"delete", "rename"}
        )
        if destructive and destructive_approval is None:
            escalations.append(
                EscalationTrigger(
                    EscalationCode.E03,
                    "planned delete or rename requires destructive approval",
                    tuple(item.path for item in destructive),
                    (
                        f"E-03:destructive:{plan.plan_version}:"
                        f"{evidence.artifact_digests[0]}"
                    ),
                )
            )
    if escalations:
        return StepExecutionResult(
            TransitionSignal(
                SignalKind.ESCALATE,
                "; ".join(item.reason for item in escalations),
                tuple(
                    record.finding.finding_id
                    for record in update.ledger.findings
                    if record.status.value != "RESOLVED"
                ),
            ),
            update.ledger,
            None,
            tuple(escalations),
        )
    signal_kind = (
        SignalKind.UNRESOLVED_ZERO
        if view.unresolved_count == 0
        else SignalKind.UNRESOLVED_REMAIN
    )
    return StepExecutionResult(
        TransitionSignal(
            signal_kind,
            (
                "consensus reached"
                if signal_kind is SignalKind.UNRESOLVED_ZERO
                else "unresolved findings remain"
            ),
            tuple(
                record.finding.finding_id
                for record in update.ledger.findings
                if record.status.value != "RESOLVED"
            ),
        ),
        update.ledger,
        None,
        (),
    )


def execute_test_gate(
    *,
    ledger: ConsensusLedger,
    plan: PlanDocument,
    policy: TestExecutionPolicy,
    worktree: Path,
) -> StepExecutionResult:
    result = run_tests(
        plan.test_contract.commands,
        policy,
        worktree,
    )
    signal = TransitionSignal(
        SignalKind(result.status.value),
        f"test gate result: {result.status.value}",
        (),
    )
    return StepExecutionResult(signal, ledger, result.status)


def execute_human_gate(
    ledger: ConsensusLedger,
    decision: HumanDecision,
    *,
    gate_kind: GateKind = GateKind.FINAL,
) -> StepExecutionResult:
    from .escalation import route_gate

    return StepExecutionResult(
        route_gate(decision, gate_kind=gate_kind),
        ledger,
        None,
    )


def operational_retry_result(
    *,
    ledger: ConsensusLedger,
    counters,
    limit: int,
    reason: str,
    finding_ids: tuple[str, ...],
) -> StepExecutionResult:
    """Decide how a retryable step failure moves the loop.

    Takes the reason as text rather than an exception so that transient
    runtime failures can reach the same retry budget as contract violations.
    """
    if counters.operational_retries < limit - 1:
        signal = SignalKind.OPERATIONAL_RETRY
    elif any(
        marker in reason
        for marker in (
            "approval_obligation",
            "missing finding decisions",
            "missing_finding_decision",
        )
    ):
        signal = SignalKind.ESCALATE
    else:
        signal = SignalKind.ABORT
    return StepExecutionResult(
        TransitionSignal(signal, reason, finding_ids),
        ledger,
        None,
    )


def reconcile_worker(
    state: CoordinatorState,
    observation: DispatchObservation | None,
    *,
    output_exists: bool,
) -> ResumeOutcome:
    """Decide what an interrupted step's worker is actually doing.

    The worker runs `worker_runner.py` inside its own Orca terminal, so it
    outlives the coordinator process that dispatched it.  Local binding files
    cannot tell a finished worker from one still editing the worktree, so this
    reads authoritative Orca state and fails closed when it cannot.
    """
    active = state.active
    if active is None or active.dispatch_id is None:
        return ResumeOutcome.NO_ACTIVE_STEP
    if observation is None:
        # Orca has no record, or the lookup failed.  Either way a live editor
        # cannot be ruled out, so no replacement may be launched.
        return ResumeOutcome.ABANDON_AND_BLOCK
    if observation.dispatch_id != active.dispatch_id:
        return ResumeOutcome.ABANDON_AND_BLOCK
    status = observation.status.lower()
    if status in {"completed", "succeeded", "failed"}:
        # The worker settled on its own; its artifact is usable if it landed.
        return (
            ResumeOutcome.RECOVER_SETTLED
            if output_exists
            else ResumeOutcome.ABANDON_AND_BLOCK
        )
    if status in {"dispatched", "running", "ready"}:
        if observation.assignee_alive:
            # A live worker owns the worktree; re-running the step here would
            # put two editors in it.
            return ResumeOutcome.ADOPT_WAIT
        # The terminal is gone, so nothing can still be writing.
        return ResumeOutcome.STOP_AND_RETRY
    if status in {"stopped", "cancelled", "abandoned"}:
        return ResumeOutcome.STOP_AND_RETRY
    return ResumeOutcome.ABANDON_AND_BLOCK


def reconcile_resume(
    state: CoordinatorState,
    ledger: ConsensusLedger,
    *,
    task_exists: bool,
    dispatch_exists: bool,
    output_exists: bool,
) -> ResumeDecision:
    if state.run_id != ledger.run_id or state.generation != ledger.generation:
        raise ResumeAmbiguityError(
            "state and ledger provenance do not match"
        )
    active = state.active
    stage = state.step_stage
    if stage is StepStage.STEP_PENDING:
        action = ResumeAction.CREATE_TASK
    elif stage is StepStage.STEP_PREPARED:
        action = ResumeAction.CREATE_TASK
    elif stage is StepStage.TASK_CREATED:
        if active is None or active.task_id is None:
            raise ResumeAmbiguityError(
                "TASK_CREATED has no durable task binding"
            )
        action = (
            ResumeAction.WAIT_DISPATCH
            if dispatch_exists
            else ResumeAction.DISPATCH_TASK
        )
    elif stage is StepStage.STEP_DISPATCHED:
        if (
            active is None
            or active.task_id is None
            or active.dispatch_id is None
            or not task_exists
            or not dispatch_exists
        ):
            action = ResumeAction.USER_DECISION_REQUIRED
        else:
            action = ResumeAction.WAIT_DISPATCH
    elif stage is StepStage.WORKER_DONE_RECEIVED:
        action = (
            ResumeAction.PROMOTE_ARTIFACT
            if output_exists
            else ResumeAction.USER_DECISION_REQUIRED
        )
    elif stage is StepStage.ARTIFACT_VERIFIED:
        action = ResumeAction.APPLY_TRANSITION
    elif stage is StepStage.TRANSITION_COMMITTED:
        action = (
            ResumeAction.USER_DECISION_REQUIRED
            if state.state in TERMINAL_STATES
            else ResumeAction.CREATE_TASK
        )
    else:
        raise ResumeAmbiguityError(f"unknown durable stage: {stage}")
    return ResumeDecision(action, state, ledger, active)


def commit_step_transition(
    controller: GenerationController,
    result: StepExecutionResult,
    config: LoopConfig,
) -> CoordinatorState:
    outcome = transition(
        controller.state.state,
        result.signal,
        ledger_view(result.ledger),
        controller.state.counters,
        plan_round_limit=config.plan_consensus_round_limit,
        code_round_limit=config.code_consensus_round_limit,
        test_fix_attempt_limit=config.test_fix_attempt_limit,
        operational_retry_limit=config.operational_retry_limit,
    )
    status = RunStatus.IN_PROGRESS
    if outcome.next_state is LoopState.READY_FOR_MERGE:
        status = RunStatus.READY
    elif outcome.next_state is LoopState.REJECTED:
        status = RunStatus.REJECTED
    elif outcome.next_state is LoopState.USER_DECISION_REQUIRED:
        status = RunStatus.BLOCKED
    elif outcome.next_state is LoopState.FAILED:
        status = RunStatus.FAILED
    return controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason=outcome.reason,
        signal=result.signal.kind,
        state_value=outcome.next_state,
        status=status,
        ledger=result.ledger,
        counters=outcome.counters_after,
        test_gate_status=result.test_gate_status,
        blocked_from_state=(
            controller.state.state
            if outcome.next_state is LoopState.USER_DECISION_REQUIRED
            else None
        ),
        pending_escalations=(
            result.escalations
            if outcome.next_state is LoopState.USER_DECISION_REQUIRED
            else ()
        ),
    )
