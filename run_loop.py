from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from orca_loop.catalog import describe_catalog, load_catalog
from orca_loop.environment import capture_environment, describe_environment
from orca_loop.config import (
    ConfigurationError,
    PreflightError,
    PreflightResult,
    default_agent_runtime_config,
    discover_permission_report,
    orca_version_from_status,
    parse_run_arguments,
    permission_report_candidates,
    permission_report_problem,
    persist_agent_runtime_snapshot,
    prepare_agent_runtime,
    run_preflight,
    record_permission_refresh_marker,
)
from orca_loop.contracts import (
    ContractViolationError,
    digest_value,
    parse_adjudication_artifact,
    parse_blind_review_artifact,
    parse_plan_document,
    parse_review_comparison,
    parse_review_context,
    parse_test_evidence,
    serialize_json,
)
from orca_loop.coordinator import (
    CoordinatorGuardError,
    reconcile_worker,
    CoordinatorPermissionError,
    GenerationController,
    OrcaLoopError,
    WORKER_STATES,
    commit_step_transition,
    consensus_round,
    execute_evaluate,
    execute_human_gate,
    execute_test_gate,
    execute_worker_step,
    operational_retry_result,
    reconcile_resume,
    role_for_state,
)
from orca_loop.dispatcher import (
    DispatcherError,
    observe_dispatch,
    provision_workers,
    worker_for_role,
)
from orca_loop.escalation import (
    DecisionReportError,
    GateProtocolError,
    approve_escalation_keys,
    build_user_decision_report,
    create_gate,
    destructive_gate,
    ensure_user_decision_notice,
    find_gate_for_report,
    invalidate_user_decision_notice,
    read_user_decision_notice,
    read_user_decision_notice_delivery,
    resolve_user_decision_notice,
    wait_gate_resolution,
)
from orca_loop.notify import NoticeAnnouncer, NoticeTarget
from orca_loop.failure import (
    FORCE_FAIL_EVENT_KIND,
    STOP_REASON_LIMIT,
    BudgetExhausted,
    StopClass,
    StopEvent,
    classify_stop,
    read_latest_stop_event,
    record_stop_event,
)
from orca_loop.session import append_event
from orca_loop.generation import (
    AtomicWriteError,
    GenerationError,
    commit_generation,
    load_committed,
    write_atomic_bytes,
)
from orca_loop.ledger import (
    apply_review_artifact,
    commit_round,
    empty_ledger,
    unresolved_scope,
)
from orca_loop.locking import (
    LockInfo,
    RunLockError,
    acquire_run_lock,
    inspect_lock,
    pid_alive,
    release_run_lock,
)
from orca_loop.machine import TERMINAL_STATES
from orca_loop.models import (
    MutationKind,
    ResumeOutcome,
    MutationRecord,
    ActiveStep,
    AdjudicationDecision,
    AdjudicationArtifact,
    ArtifactKind,
    BlindReviewArtifact,
    CodeReviewRoundContext,
    ConsensusKind,
    ConsensusLedger,
    CoordinatorState,
    DecisionValue,
    ExpectedProvenance,
    GateKind,
    GateBinding,
    HumanDecisionKind,
    LaunchProfile,
    LoopCounters,
    NoticeChannel,
    LoopState,
    MergeQualification,
    PlanDocument,
    PendingReviewRound,
    PendingReviewStage,
    ResumeDecision,
    ReviewArtifact,
    ReviewComparison,
    ReviewComparisonStatus,
    ReviewConflictCandidate,
    ReviewConflictKind,
    ReviewLane,
    ReviewPhase,
    Role,
    RoleContext,
    RoundEvidence,
    RunStatus,
    ScopeManifest,
    ScopePackage,
    Side,
    SignalKind,
    StepExecutionResult,
    StepStage,
    StagedInput,
    TestGateStatus,
    TransitionSignal,
    UserDecisionNotice,
    ValidationLineage,
    WorkerKey,
    WorkerPool,
)
from orca_loop.orca_client import (
    OrcaClient,
    OrcaCommandError,
    commit_mutation,
    execute_mutation,
)
from orca_loop.profiles import build_launch_profile
from orca_loop.readonly import prepare_readonly_mirror
from orca_loop.reporting import (
    record_artifact_history,
    render_failure_report,
    render_stage_report,
    resume_command_line,
)
from orca_loop.runspec import (
    ManifestError,
    manifest_identity_problems,
    build_manifest,
    copy_request,
    read_manifest,
    update_terminals,
    verify_inputs,
    write_manifest,
)
from orca_loop.session import (
    TerminalBinding,
    append_event,
    ensure_coordinator_terminal,
    ensure_worker_pool,
    terminal_alive,
)
from orca_loop.roles import ARTIFACT_FILENAMES, render_role_contract
from orca_loop.snapshot import capture_snapshot, materialize_frozen_review
from orca_loop.workspace import (
    RunWorkspaceExistsError,
    WorkspaceError,
    create_run_workspace,
)


# The Orca build this harness was last exercised against. Drift from it is
# reported as a note, not a failure: the permission proof is pinned to the
# environment fingerprint (agent CLIs, enforcement code, platform), which is
# what actually decides whether a read-only worktree stays read-only.
EXPECTED_ORCA_VERSION = "1.4.179"
# Placeholder used only while rehearsing a launch, so a dry run never leaks
# a coordinator terminal it will not use.
DRY_RUN_HANDLE = "dry-run-no-terminal"
EXIT_READY = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_PREFLIGHT = 2
EXIT_USER_REQUIRED = 3
EXIT_REJECTED = 4


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_plan(run_root: Path):
    path = run_root / "artifacts" / "plan.json"
    if not path.is_file():
        return None
    return parse_plan_document(path.read_text(encoding="utf-8"))


def _worktree_selector(path: Path) -> str:
    return f"path:{path.resolve()}"


def _initial_state(preflight: PreflightResult) -> CoordinatorState:
    config = preflight.arguments.config
    snapshot = capture_snapshot(config.worktree_path)
    return CoordinatorState(
        schema_version=2,
        generation=0,
        run_id=preflight.arguments.run_id,
        state=LoopState.INIT,
        step_stage=StepStage.STEP_PENDING,
        status=RunStatus.IN_PROGRESS,
        worktree_selector=_worktree_selector(config.worktree_path),
        coordinator_handle=config.coordinator_handle,
        worker_handles=(),
        active=None,
        plan_version=0,
        counters=LoopCounters(0, 0),
        base_head=preflight.base_head,
        snapshot_digest=snapshot.snapshot_digest,
        test_gate_status=None,
        test_policy_digest=preflight.test_policy.policy_digest,
        permission_report_digest=(
            preflight.permission_report.report_digest
        ),
        history=(),
    )


def _result_object(response) -> dict[str, object]:
    try:
        value = json.loads(response.result_json)
    except json.JSONDecodeError as exc:
        raise OrcaLoopError("Orca orchestration response is malformed") from exc
    if not isinstance(value, dict):
        raise OrcaLoopError("Orca orchestration response must be an object")
    return value


def _run_id_from_response(response) -> str:
    value = _result_object(response)
    nested = value.get("run")
    candidate = nested if isinstance(nested, dict) else value
    for key in ("id", "runId", "run_id"):
        run_id = candidate.get(key)
        if isinstance(run_id, str) and run_id:
            return run_id
    raise OrcaLoopError("Orca orchestration response has no Run ID")


def _write_orchestration_binding(
    control_dir: Path,
    *,
    run_id: str,
    coordinator_handle: str,
) -> None:
    write_atomic_bytes(
        control_dir / "orchestration-binding.json",
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "orchestration_run_id": run_id,
                    "coordinator_handle": coordinator_handle,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _create_orchestration_run(
    client: OrcaClient,
    control_dir: Path,
    *,
    coordinator_handle: str,
    harness_run_id: str,
    generation: int,
) -> tuple[str, MutationRecord]:
    # Losing this response without a durable intent would strand an Orca Run
    # nobody owns and create a second one on the next attempt.
    response, record = execute_mutation(
        client,
        control_dir,
        kind=MutationKind.RUN_CREATE,
        argv=(
            "orchestration",
            "run-create",
            "--from",
            coordinator_handle,
            "--objective",
            f"Orca Loop harness run {harness_run_id}",
        ),
        timeout_ms=30_000,
        run_id=harness_run_id,
        generation=generation,
        external_id_keys=("run",),
    )
    return _run_id_from_response(response), record


def _bind_orchestration_run(
    client: OrcaClient,
    *,
    orchestration_run_id: str,
    coordinator_handle: str,
) -> None:
    # run-use only rebinds an existing Run, so replaying it creates nothing.
    # It stays a direct call: there is no external object to duplicate.
    client.call(
        (
            "orchestration",
            "run-use",
            "--id",
            orchestration_run_id,
            "--from",
            coordinator_handle,
        ),
        timeout_ms=30_000,
    )
    response = client.call(
        ("orchestration", "run-current", "--from", coordinator_handle),
        timeout_ms=30_000,
    )
    if _run_id_from_response(response) != orchestration_run_id:
        raise OrcaLoopError("Orca Run binding did not persist for coordinator")


def _dummy_profiles(
    worktree: Path,
    permission_digest: str,
) -> dict[WorkerKey, LaunchProfile]:
    profile = LaunchProfile(
        ("not-executed", "-C", str(worktree.resolve())),
        (),
        permission_digest,
    )
    return {key: profile for key in WorkerKey}


def _initialize(
    preflight: PreflightResult,
    client: OrcaClient,
) -> tuple[GenerationController, WorkerPool]:
    arguments = preflight.arguments
    workspace, _ = create_run_workspace(
        arguments.harness_root,
        arguments.run_id,
        "init",
        resume=False,
    )
    state = _initial_state(preflight)
    ledger = empty_ledger(arguments.run_id)
    commit_generation(workspace.control_dir, state, ledger)
    runtime = preflight.agent_runtime or default_agent_runtime_config()
    source_path = (
        None
        if arguments.agent_runtime_request is None
        else arguments.agent_runtime_request.source_path
    )
    persist_agent_runtime_snapshot(
        workspace.control_dir,
        arguments.run_id,
        runtime,
        source_path,
    )
    request_copy, request_digest = copy_request(
        workspace.control_dir,
        arguments.config.request_path,
    )
    controller = GenerationController(workspace, state, ledger)
    orchestration_run_id, run_mutation = _create_orchestration_run(
        client,
        workspace.control_dir,
        coordinator_handle=state.coordinator_handle,
        harness_run_id=state.run_id,
        generation=controller.state.generation,
    )
    controller.state = replace(
        controller.state,
        orchestration_run_id=orchestration_run_id,
    )
    controller.commit(
        stage=StepStage.STEP_PENDING,
        active=None,
        reason="Orca Run binding recorded",
    )
    _write_orchestration_binding(
        workspace.control_dir,
        run_id=orchestration_run_id,
        coordinator_handle=state.coordinator_handle,
    )
    # The Run is only settled once the binding it produced is durable.
    commit_mutation(workspace.control_dir, run_mutation)
    pool = provision_workers(
        client,
        state.worktree_selector,
        _dummy_profiles(
            arguments.config.worktree_path,
            state.permission_report_digest,
        ),
        coordinator_handle=state.coordinator_handle,
    )
    controller.commit(
        stage=StepStage.STEP_PENDING,
        active=None,
        reason="four independent worker terminals provisioned",
        ledger=controller.ledger,
    )
    controller.state = replace(
        controller.state,
        worker_handles=pool.workers,
    )
    controller.commit(
        stage=StepStage.STEP_PENDING,
        active=None,
        reason="worker pool provenance recorded",
    )
    write_manifest(
        workspace.control_dir,
        replace(
            build_manifest(
                preflight,
                request_copy=request_copy,
                request_digest=request_digest,
                coordinator_handle=state.coordinator_handle,
                pool=pool,
            ),
            orchestration_run_id=orchestration_run_id,
        ),
    )
    commit_step_transition(
        controller,
        StepExecutionResult(
            TransitionSignal(
                SignalKind.OK,
                "initialization completed",
                (),
            ),
            controller.ledger,
            None,
        ),
        arguments.config,
    )
    return controller, pool


class ResumeBlockedError(RuntimeError):
    """Raised when a resume needs an explicit user decision to continue."""


PLAN_REBASELINE_STATES = frozenset({LoopState.PLAN, LoopState.PLAN_REVISE})
PLAN_EVIDENCE_STATES = frozenset(
    {LoopState.PLAN_REVIEW, LoopState.PLAN_CONSENSUS_EVALUATE}
)
WRITE_STATES = frozenset({LoopState.IMPLEMENT, LoopState.FIX})


@dataclass(frozen=True)
class DriftDecision:
    drifted: bool
    rebaselined: bool
    new_digest: str | None
    detail: tuple[str, ...]
    target_state: LoopState | None = None
    invalidate_evidence: bool = False


def _snapshot_difference(
    state: CoordinatorState,
    current,
) -> tuple[str, ...]:
    lines = [
        f"recorded snapshot: {state.snapshot_digest}",
        f"current snapshot:  {current.snapshot_digest}",
        f"recorded base HEAD: {state.base_head}",
        f"current base HEAD:  {current.base_head}",
    ]
    if current.untracked:
        lines.append(
            "untracked files now present: "
            + ", ".join(path for path, _ in current.untracked[:20])
        )
    return tuple(lines)


def _resolve_drift(
    state: CoordinatorState,
    worktree: Path,
    *,
    accept: bool,
) -> DriftDecision:
    current = capture_snapshot(worktree)
    if current.snapshot_digest == state.snapshot_digest:
        return DriftDecision(False, False, None, ())
    detail = _snapshot_difference(state, current)
    if state.state in PLAN_REBASELINE_STATES:
        return DriftDecision(True, True, current.snapshot_digest, detail)
    if accept:
        source_state = (
            state.blocked_from_state
            if state.state is LoopState.USER_DECISION_REQUIRED
            and state.blocked_from_state is not None
            else state.state
        )
        if source_state in PLAN_EVIDENCE_STATES:
            target = LoopState.PLAN_REVISE
        elif source_state in WRITE_STATES:
            target = source_state
        else:
            target = LoopState.TEST_GATE
        return DriftDecision(
            True,
            True,
            current.snapshot_digest,
            detail,
            target_state=target,
            invalidate_evidence=True,
        )
    raise ResumeBlockedError(
        "worktree changed since the last committed generation while the run "
        f"was in {state.state.value}; existing plan or validation evidence "
        "cannot be relabeled to the new snapshot. Review the changes, then "
        "resume with --accept-worktree-drift to invalidate stale evidence. "
        + "; ".join(detail)
    )


def _binding_facts(step_root: Path) -> tuple[bool, bool]:
    path = step_root / "binding.json"
    if not path.is_file():
        return False, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, False
    if not isinstance(value, dict):
        return False, False
    return (
        isinstance(value.get("task_id"), str) and bool(value.get("task_id")),
        isinstance(value.get("dispatch_id"), str)
        and bool(value.get("dispatch_id")),
    )


def _abandon_step(
    workspace,
    active: ActiveStep,
    reason: str,
) -> None:
    """Mark an in-flight step as abandoned without deleting its evidence."""
    step_root = workspace.steps_dir / active.step_id
    if not step_root.is_dir():
        return
    outputs = (
        sorted(
            str(item)
            for item in (step_root / "out").iterdir()
            if item.is_file()
        )
        if (step_root / "out").is_dir()
        else []
    )
    payload = {
        "abandoned_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "reason": reason,
        "step_id": active.step_id,
        "role": active.role.value,
        "task_id": active.task_id,
        "dispatch_id": active.dispatch_id,
        "preserved_outputs": outputs,
    }
    try:
        (step_root / "ABANDONED").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        return


def _resume(
    preflight: PreflightResult,
    client: OrcaClient,
) -> tuple[GenerationController, WorkerPool]:
    arguments = preflight.arguments
    workspace, _ = create_run_workspace(
        arguments.harness_root,
        arguments.run_id,
        "resume",
        resume=True,
    )
    state, ledger, _ = load_committed(workspace.control_dir)
    if state.run_id != arguments.run_id:
        raise OrcaLoopError("resume run ID does not match committed state")
    # A manifest copied from another run names a foreign worktree and foreign
    # terminals.  Refuse it before anything acts on those paths.
    recorded = read_manifest(workspace.control_dir)
    if recorded is not None:
        identity = manifest_identity_problems(
            recorded,
            requested_run_id=arguments.run_id,
            harness_root=arguments.harness_root,
        )
        if identity:
            raise ManifestError(
                "run manifest does not describe this run: "
                + "; ".join(identity)
            )
    if state.state in {LoopState.FAILED, LoopState.REJECTED}:
        raise OrcaLoopError(
            f"run ended in {state.state.value}; start a new run instead of "
            "resuming it"
        )
    runtime_path = workspace.control_dir / "agent-runtime.json"
    if not runtime_path.exists():
        runtime = preflight.agent_runtime or default_agent_runtime_config()
        source_path = (
            None
            if arguments.agent_runtime_request is None
            else arguments.agent_runtime_request.source_path
        )
        persist_agent_runtime_snapshot(
            workspace.control_dir,
            arguments.run_id,
            runtime,
            source_path,
        )

    selector = _worktree_selector(arguments.config.worktree_path)
    current_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
    if current_handle:
        # Resume adopts the attested terminal it is actually running in.  It
        # must never silently fabricate a replacement: an unattested terminal
        # cannot stand in for the coordinator, so a dead handle is BLOCKED.
        if not terminal_alive(client, current_handle):
            raise ResumeBlockedError(
                "the current Orca terminal is not live; resume from an "
                "attested coordinator terminal"
            )
        coordinator = TerminalBinding(
            current_handle,
            current_handle != state.coordinator_handle,
        )
    else:
        coordinator = ensure_coordinator_terminal(
            client,
            worktree_selector=selector,
            run_id=state.run_id,
            recorded_handles=(
                state.coordinator_handle,
                arguments.config.coordinator_handle,
            ),
        )
    binding = ensure_worker_pool(
        client,
        worktree_selector=selector,
        recorded={
            item.worker_key: item.terminal_handle
            for item in state.worker_handles
        },
        coordinator_handle=coordinator.handle,
    )
    drift = _resolve_drift(
        state,
        arguments.config.worktree_path,
        accept=arguments.accept_worktree_drift,
    )

    controller = GenerationController(workspace, state, ledger)
    if not state.orchestration_run_id:
        raise ResumeBlockedError(
            "this run predates durable Orca Run binding; start a new run"
        )
    _bind_orchestration_run(
        client,
        orchestration_run_id=state.orchestration_run_id,
        coordinator_handle=coordinator.handle,
    )
    if coordinator.rebound or binding.changed or drift.rebaselined:
        reasons = []
        if coordinator.rebound:
            reasons.append("coordinator terminal rebound")
        if binding.changed:
            reasons.append(
                "worker terminals rebound: "
                + ",".join(item.value for item in binding.rebound)
            )
        if drift.rebaselined:
            reasons.append("worktree snapshot re-baselined")
        if drift.invalidate_evidence and state.gate_binding is not None:
            invalidate_user_decision_notice(
                workspace.control_dir,
                reason="worktree drift invalidated gate-bound evidence",
            )
        controller.state = replace(
            controller.state,
            coordinator_handle=coordinator.handle,
            worker_handles=binding.pool.workers,
        )
        controller.commit(
            stage=controller.state.step_stage,
            active=controller.state.active,
            reason="resume: " + "; ".join(reasons),
            snapshot_digest=drift.new_digest,
            state_value=(
                controller.state.state
                if drift.target_state is None
                else drift.target_state
            ),
            validation_lineage=(
                None
                if not drift.invalidate_evidence
                else ValidationLineage()
            ),
            clear_pending_review=drift.invalidate_evidence,
            clear_gate=drift.invalidate_evidence,
            clear_blocked_context=drift.invalidate_evidence,
        )
        append_event(
            workspace.control_dir,
            "resume_rebound",
            {
                "generation": controller.state.generation,
                "coordinator_rebound": coordinator.rebound,
                "coordinator_handle": coordinator.handle,
                "workers_rebound": [
                    item.value for item in binding.rebound
                ],
                "worktree_rebaselined": drift.rebaselined,
                "drift_detail": list(drift.detail),
            },
        )
    manifest = read_manifest(workspace.control_dir)
    if manifest is not None:
        update_terminals(
            workspace.control_dir,
            manifest,
            coordinator_handle=coordinator.handle,
            pool=binding.pool,
        )

    active = controller.state.active
    task_exists, dispatch_exists = (
        (False, False)
        if active is None
        else _binding_facts(workspace.steps_dir / active.step_id)
    )
    output_exists = False
    if active is not None:
        output_dir = workspace.steps_dir / active.step_id / "out"
        output_exists = output_dir.is_dir() and any(
            item.is_file() for item in output_dir.iterdir()
        )
    # Local binding files cannot tell a finished worker from one that is still
    # editing the worktree: the worker runs in its own terminal and outlives
    # the coordinator that dispatched it.  Ask Orca before touching the step.
    observation = (
        None
        if active is None or active.task_id is None
        else observe_dispatch(client, task_id=active.task_id)
    )
    outcome = reconcile_worker(
        controller.state,
        observation,
        output_exists=output_exists,
    )
    if outcome in {
        ResumeOutcome.ADOPT_WAIT,
        ResumeOutcome.ABANDON_AND_BLOCK,
    }:
        append_event(
            workspace.control_dir,
            "resume_blocked",
            {
                "outcome": outcome.value,
                "step_id": None if active is None else active.step_id,
                "task_id": None if active is None else active.task_id,
                "dispatch_id": None if active is None else active.dispatch_id,
                "dispatch_status": (
                    None if observation is None else observation.status
                ),
                "assignee_handle": (
                    None if observation is None else observation.assignee_handle
                ),
                "assignee_alive": (
                    None if observation is None else observation.assignee_alive
                ),
            },
        )
        detail = (
            "its worker is still live"
            if outcome is ResumeOutcome.ADOPT_WAIT
            else "its worker outcome cannot be proven finished"
        )
        raise ResumeBlockedError(
            f"step {'' if active is None else active.step_id} cannot be "
            f"resumed because {detail}. Re-running it would put a second "
            f"editor in the worktree. Stop or close the worker terminal "
            f"{'' if observation is None else observation.assignee_handle} "
            "and resume again."
        )
    decision = reconcile_resume(
        controller.state,
        controller.ledger,
        task_exists=task_exists,
        dispatch_exists=dispatch_exists,
        output_exists=output_exists,
    )
    _apply_resume_decision(controller, decision, workspace)
    return controller, binding.pool


def _apply_resume_decision(
    controller: GenerationController,
    decision: ResumeDecision,
    workspace,
) -> None:
    """Return the run to a clean step boundary.

    An in-flight step is abandoned rather than adopted: the dispatch it was
    waiting on died with the previous coordinator, and the guard baselines
    that would be needed to accept its output (the pre-step snapshot and file
    state) are not persisted. Re-running the step is correct by construction,
    and the abandoned step keeps its inputs, outputs and logs on disk.
    """
    state = controller.state
    if state.state in TERMINAL_STATES or state.state in {
        LoopState.HUMAN_GATE,
        LoopState.USER_DECISION_REQUIRED,
    }:
        return
    if (
        state.step_stage is StepStage.TRANSITION_COMMITTED
        and state.active is None
    ):
        return
    reason = f"resume from {state.step_stage.value} ({decision.action.value})"
    if state.active is not None:
        _abandon_step(workspace, state.active, reason)
        append_event(
            workspace.control_dir,
            "step_abandoned",
            {
                "step_id": state.active.step_id,
                "role": state.active.role.value,
                "task_id": state.active.task_id,
                "dispatch_id": state.active.dispatch_id,
                "resume_action": decision.action.value,
            },
        )
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason=reason,
    )


def _user_scope(
    controller: GenerationController,
) -> ScopePackage:
    scope = unresolved_scope(controller.ledger)
    decision = controller.state.human_decision
    retry_note = ()
    if (
        controller.state.counters.operational_retries > 0
        and controller.state.history
    ):
        retry_note = (
            "CONTRACT REMINDER: "
            + controller.state.history[-1].reason,
        )
    if scope.finding_ids:
        return ScopePackage(
            finding_ids=scope.finding_ids,
            acceptance_criteria_ids=scope.acceptance_criteria_ids,
            affected_files=scope.affected_files,
            test_ids=scope.test_ids,
            targeted_test_results=scope.targeted_test_results,
            disagreement_excerpts=(
                scope.disagreement_excerpts + retry_note
            ),
        )
    if decision is None:
        return ScopePackage(
            finding_ids=scope.finding_ids,
            acceptance_criteria_ids=scope.acceptance_criteria_ids,
            affected_files=scope.affected_files,
            test_ids=scope.test_ids,
            targeted_test_results=scope.targeted_test_results,
            disagreement_excerpts=retry_note,
        )
    if decision.decision is HumanDecisionKind.MERGE:
        plan = _load_plan(controller.workspace.root)
        if plan is not None:
            return ScopePackage(
                finding_ids=(),
                acceptance_criteria_ids=tuple(
                    item.criterion_id
                    for item in plan.acceptance_criteria
                ),
                affected_files=tuple(
                    item.path for item in plan.affected_files
                ),
                test_ids=plan.test_contract.test_ids,
                targeted_test_results=(),
                disagreement_excerpts=(
                    retry_note
                    if decision.decision_note is None
                    else retry_note
                    + (f"USER: {decision.decision_note}",)
                ),
            )
    return ScopePackage(
        finding_ids=(),
        acceptance_criteria_ids=(
            decision.affected_acceptance_criteria
        ),
        affected_files=(),
        test_ids=(),
        targeted_test_results=(),
        disagreement_excerpts=(
            retry_note
            if decision.decision_note is None
            else retry_note + (f"USER: {decision.decision_note}",)
        ),
    )


def _tree_digest(root: Path) -> str:
    base = root.resolve()
    entries: list[dict[str, str]] = []
    for path in sorted(
        (item for item in base.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(base).as_posix().encode("utf-8"),
    ):
        if path.is_symlink():
            raise OrcaLoopError(f"review mirror contains symlink: {path}")
        entries.append(
            {
                "path": path.relative_to(base).as_posix(),
                "digest": _digest(path),
            }
        )
    return digest_value(entries)


def _review_round_dir(controller: GenerationController) -> Path:
    return (
        controller.workspace.review_dir
        / f"round-{max(1, controller.ledger.code_round + 1)}"
    )


def _ledger_content_digest(ledger: ConsensusLedger) -> str:
    value = json.loads(serialize_json(ledger))
    value.pop("generation", None)
    return digest_value(value)


def _load_review_context(
    controller: GenerationController,
) -> CodeReviewRoundContext:
    path = controller.workspace.artifact_dir / "review_context.json"
    if not path.is_file():
        raise OrcaLoopError("review context is missing")
    before_digest = _digest(path)
    context = parse_review_context(path.read_text(encoding="utf-8"))
    if _digest(path) != before_digest:
        raise OrcaLoopError("review context changed while reading")
    return context


def _prepare_review_context(
    controller: GenerationController,
    preflight: PreflightResult,
) -> StepExecutionResult:
    if controller.state.pending_review is not None:
        raise OrcaLoopError("a pending review round already exists")
    plan = _load_plan(controller.workspace.root)
    if plan is None:
        raise OrcaLoopError("review context requires a promoted plan")
    implementation_path = controller.workspace.artifact_dir / "implementation.json"
    test_path = controller.workspace.artifact_dir / "test_evidence.json"
    if not implementation_path.is_file() or not test_path.is_file():
        raise OrcaLoopError("review context requires implementation and test evidence")
    test_evidence = parse_test_evidence(test_path.read_text(encoding="utf-8"))
    worktree = preflight.arguments.config.worktree_path
    before = capture_snapshot(worktree)
    if before.snapshot_digest != controller.state.snapshot_digest:
        raise OrcaLoopError("worktree changed before review context preparation")
    if test_evidence.authoritative_snapshot_digest != before.snapshot_digest:
        raise OrcaLoopError("test evidence does not bind the current snapshot")
    round_value = max(1, controller.ledger.code_round + 1)
    round_dir = _review_round_dir(controller)
    round_dir.mkdir(parents=True, exist_ok=True)
    frozen = materialize_frozen_review(
        worktree,
        before,
        plan.affected_files,
        round_dir,
        destructive_approval_digest=(
            None
            if controller.state.destructive_approval is None
            else controller.state.destructive_approval.decision_digest
        ),
    )
    mirror_parent = _readonly_mirror_root(
        worktree,
        round_dir / "mirror",
        controller.state.run_id,
    )
    mirror_generation = controller.state.generation + 1
    while (mirror_parent / f"repository-{mirror_generation}").exists():
        mirror_generation += 1
    mirror_path = prepare_readonly_mirror(
        worktree,
        mirror_parent,
        mirror_generation,
    )
    mirror_digest = _tree_digest(mirror_path)
    binding_value = {
        "schema_version": 1,
        "round": round_value,
        "path": str(mirror_path.resolve()),
        "source_snapshot_digest": before.snapshot_digest,
        "tree_digest": mirror_digest,
    }
    write_atomic_bytes(
        round_dir / "mirror-binding.json",
        (json.dumps(binding_value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
    scope = _user_scope(controller)
    value: dict[str, object] = {
        "schema_version": 1,
        "run_id": controller.state.run_id,
        "consensus_round": round_value,
        "plan_version": controller.state.plan_version,
        "snapshot_digest": before.snapshot_digest,
        "implementation_artifact_digest": _digest(implementation_path),
        "test_evidence_digest": _digest(test_path),
        "frozen_diff_digest": _digest(frozen.diff_path),
        "scope_manifest_digest": _digest(frozen.manifest_path),
        "readonly_mirror_digest": mirror_digest,
        "baseline_finding_ids": list(scope.finding_ids),
        "acceptance_criteria_ids": [item.criterion_id for item in plan.acceptance_criteria],
        "affected_files": [
            {
                "path": item.path,
                "operation": item.operation.value,
                "rename_from": item.rename_from,
            }
            for item in plan.affected_files
        ],
        "test_ids": list(plan.test_contract.test_ids),
    }
    value["context_digest"] = digest_value(value)
    context = parse_review_context(json.dumps(value, ensure_ascii=False))
    context_path = controller.workspace.artifact_dir / "review_context.json"
    write_atomic_bytes(
        context_path,
        (serialize_json(context) + "\n").encode("utf-8"),
    )
    if parse_review_context(context_path.read_text(encoding="utf-8")) != context:
        raise OrcaLoopError("persisted review context differs after write")
    after = capture_snapshot(worktree)
    if after != before:
        raise OrcaLoopError("worktree changed during review context preparation")
    pending = PendingReviewRound(
        consensus_round=round_value,
        stage=PendingReviewStage.CONTEXT_READY,
        review_context_digest=context.context_digest,
        pre_round_ledger_digest=_ledger_content_digest(controller.ledger),
    )
    lineage = replace(
        controller.state.validation_lineage,
        review_context_snapshot_digest=before.snapshot_digest,
        review_context_digest=context.context_digest,
        blind_review_a_snapshot_digest=None,
        blind_review_a_artifact_digest=None,
        blind_review_b_snapshot_digest=None,
        blind_review_b_artifact_digest=None,
        review_comparison_digest=None,
        adjudication_a_snapshot_digest=None,
        adjudication_a_artifact_digest=None,
        adjudication_b_snapshot_digest=None,
        adjudication_b_artifact_digest=None,
        consensus_snapshot_digest=None,
    )
    controller.commit(
        stage=StepStage.ARTIFACT_VERIFIED,
        active=None,
        reason="sealed code review context prepared",
        pending_review=pending,
        validation_lineage=lineage,
    )
    return StepExecutionResult(
        TransitionSignal(
            SignalKind.CONTEXT_PREPARED,
            "sealed review context prepared",
            scope.finding_ids,
        ),
        controller.ledger,
        controller.state.test_gate_status,
    )


def _load_blind_review(
    controller: GenerationController,
    kind: ArtifactKind,
) -> BlindReviewArtifact:
    context = _load_review_context(controller)
    test_evidence = parse_test_evidence(
        (controller.workspace.artifact_dir / "test_evidence.json").read_text(
            encoding="utf-8"
        )
    )
    path = controller.workspace.artifact_dir / f"{kind.value}.json"
    pending = controller.state.pending_review
    expected_digest = (
        None
        if pending is None
        else (
            pending.blind_a_artifact_digest
            if kind is ArtifactKind.CODE_REVIEW_A
            else pending.blind_b_artifact_digest
        )
    )
    if not path.is_file() or expected_digest is None or _digest(path) != expected_digest:
        raise OrcaLoopError(f"promoted {kind.value} provenance mismatch")
    raw_text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OrcaLoopError(f"promoted {kind.value} is malformed") from exc
    if not isinstance(raw, dict):
        raise OrcaLoopError(f"promoted {kind.value} must be an object")
    expected = ExpectedProvenance(
        run_id=controller.state.run_id,
        task_id=str(raw.get("task_id", "")),
        dispatch_id=str(raw.get("dispatch_id", "")),
        consensus_round=context.consensus_round,
        snapshot_digest=context.snapshot_digest,
    )
    return parse_blind_review_artifact(
        raw_text,
        kind,
        expected,
        expected_plan_version=context.plan_version,
        expected_context_digest=context.context_digest,
        expected_reviewed_artifact_digest=context.implementation_artifact_digest,
        delivered_finding_ids=context.baseline_finding_ids,
        acceptance_criteria_ids=context.acceptance_criteria_ids,
        affected_files=context.affected_files,
        test_ids=context.test_ids,
        test_gate_status=test_evidence.test_gate_status,
    )


def _finding_semantic_digest(finding) -> str:
    value = json.loads(serialize_json(finding))
    value.pop("finding_id", None)
    return digest_value(value)


def _candidate(
    index: int,
    kind: ReviewConflictKind,
    *,
    finding_ids: tuple[str, ...] = (),
    acceptance_ids: tuple[str, ...] = (),
    affected_files: tuple[str, ...] = (),
    test_ids: tuple[str, ...] = (),
    a: DecisionValue | None = None,
    b: DecisionValue | None = None,
    signature: str,
    evidence: tuple[str, ...],
) -> ReviewConflictCandidate:
    return ReviewConflictCandidate(
        candidate_id=f"CAND-{index:04d}",
        kind=kind,
        finding_ids=finding_ids,
        acceptance_criteria_ids=acceptance_ids,
        affected_files=affected_files,
        test_ids=test_ids,
        blind_a_decision=a,
        blind_b_decision=b,
        normalized_signature=signature,
        evidence_refs=evidence,
    )


def _compare_blind_pair(
    controller: GenerationController,
    blind_a: BlindReviewArtifact,
    blind_b: BlindReviewArtifact,
) -> ReviewComparison:
    context = _load_review_context(controller)
    pending = controller.state.pending_review
    if pending is None:
        raise OrcaLoopError("blind pair has no pending review round")
    candidates: list[ReviewConflictCandidate] = []
    agreed: list[str] = []

    def add(kind, **kwargs) -> None:
        candidates.append(_candidate(len(candidates) + 1, kind, **kwargs))

    a_decisions = {item.finding_id: item for item in blind_a.finding_decisions}
    b_decisions = {item.finding_id: item for item in blind_b.finding_decisions}
    for finding_id in context.baseline_finding_ids:
        a_item = a_decisions[finding_id]
        b_item = b_decisions[finding_id]
        if a_item.decision is b_item.decision:
            agreed.append(finding_id)
        else:
            add(
                ReviewConflictKind.BASELINE_DECISION,
                finding_ids=(finding_id,),
                a=a_item.decision,
                b=b_item.decision,
                signature=digest_value(
                    [finding_id, a_item.decision.value, b_item.decision.value]
                ),
                evidence=a_item.evidence_refs + b_item.evidence_refs,
            )
    for a_item, b_item in zip(
        blind_a.acceptance_evaluations,
        blind_b.acceptance_evaluations,
        strict=True,
    ):
        if a_item.decision is not b_item.decision:
            add(
                ReviewConflictKind.COVERAGE,
                acceptance_ids=(a_item.criterion_id,),
                a=a_item.decision,
                b=b_item.decision,
                signature=digest_value(
                    [a_item.criterion_id, a_item.decision.value, b_item.decision.value]
                ),
                evidence=a_item.evidence_refs + b_item.evidence_refs,
            )
    for a_item, b_item in zip(
        blind_a.file_evaluations,
        blind_b.file_evaluations,
        strict=True,
    ):
        if a_item.decision is not b_item.decision:
            add(
                ReviewConflictKind.COVERAGE,
                affected_files=(a_item.path,),
                a=a_item.decision,
                b=b_item.decision,
                signature=digest_value(
                    [a_item.path, a_item.decision.value, b_item.decision.value]
                ),
                evidence=a_item.evidence_refs + b_item.evidence_refs,
            )
    for a_item, b_item in zip(
        blind_a.test_evaluations,
        blind_b.test_evaluations,
        strict=True,
    ):
        if a_item.decision is not b_item.decision:
            add(
                ReviewConflictKind.VERIFICATION,
                test_ids=(a_item.test_id,),
                a=a_item.decision,
                b=b_item.decision,
                signature=digest_value(
                    [a_item.test_id, a_item.decision.value, b_item.decision.value]
                ),
                evidence=a_item.evidence_refs + b_item.evidence_refs,
            )
    a_findings = {item.finding_id: item for item in blind_a.findings}
    b_findings = {item.finding_id: item for item in blind_b.findings}
    for finding_id in sorted(
        set(a_findings) | set(b_findings),
        key=lambda item: item.encode("utf-8"),
    ):
        a_finding = a_findings.get(finding_id)
        b_finding = b_findings.get(finding_id)
        if a_finding is None or b_finding is None:
            present = a_finding or b_finding
            assert present is not None
            add(
                ReviewConflictKind.UNILATERAL_FINDING,
                finding_ids=(finding_id,),
                a=(None if a_finding is None else a_decisions[finding_id].decision),
                b=(None if b_finding is None else b_decisions[finding_id].decision),
                signature=_finding_semantic_digest(present),
                evidence=present.evidence_refs,
            )
        elif (
            _finding_semantic_digest(a_finding)
            != _finding_semantic_digest(b_finding)
        ):
            add(
                ReviewConflictKind.FINDING_SIGNATURE,
                finding_ids=(finding_id,),
                a=a_decisions[finding_id].decision,
                b=b_decisions[finding_id].decision,
                signature=digest_value(
                    [
                        _finding_semantic_digest(a_finding),
                        _finding_semantic_digest(b_finding),
                    ]
                ),
                evidence=a_finding.evidence_refs + b_finding.evidence_refs,
            )
        elif a_decisions[finding_id].decision is not b_decisions[finding_id].decision:
            add(
                ReviewConflictKind.BASELINE_DECISION,
                finding_ids=(finding_id,),
                a=a_decisions[finding_id].decision,
                b=b_decisions[finding_id].decision,
                signature=_finding_semantic_digest(a_finding),
                evidence=a_finding.evidence_refs + b_finding.evidence_refs,
            )
        else:
            agreed.append(finding_id)
    value: dict[str, object] = {
        "schema_version": 1,
        "run_id": controller.state.run_id,
        "consensus_round": context.consensus_round,
        "snapshot_digest": context.snapshot_digest,
        "review_context_digest": context.context_digest,
        "pre_round_ledger_digest": pending.pre_round_ledger_digest,
        "blind_a_artifact_digest": pending.blind_a_artifact_digest,
        "blind_b_artifact_digest": pending.blind_b_artifact_digest,
        "status": (
            ReviewComparisonStatus.AGREED.value
            if not candidates
            else ReviewComparisonStatus.ADJUDICATION_REQUIRED.value
        ),
        "agreed_finding_ids": sorted(set(agreed), key=lambda item: item.encode("utf-8")),
        "candidates": [json.loads(serialize_json(item)) for item in candidates],
    }
    value["comparison_digest"] = digest_value(value)
    return parse_review_comparison(json.dumps(value, ensure_ascii=False))


def _as_review_artifact(
    artifact: BlindReviewArtifact,
    *,
    findings=None,
    decisions=None,
) -> ReviewArtifact:
    return ReviewArtifact(
        schema_version=artifact.schema_version,
        artifact_kind=(
            ArtifactKind.CODE_REVIEW
            if artifact.lane is ReviewLane.A
            else ArtifactKind.CROSS_REVIEW
        ),
        run_id=artifact.run_id,
        task_id=artifact.task_id,
        dispatch_id=artifact.dispatch_id,
        consensus_round=artifact.consensus_round,
        snapshot_digest=artifact.snapshot_digest,
        role=artifact.role,
        verdict=artifact.verdict,
        reviewed_plan_version=artifact.plan_version,
        reviewed_artifact_digest=artifact.reviewed_artifact_digest,
        reviewed_finding_ids=artifact.reviewed_finding_ids,
        finding_decisions=(
            artifact.finding_decisions if decisions is None else tuple(decisions)
        ),
        findings=artifact.findings if findings is None else tuple(findings),
        non_blocking_suggestions=artifact.non_blocking_suggestions,
        escalation_signals=artifact.escalation_signals,
        agrees_with_reviewer=None,
    )


def _apply_code_round(
    controller: GenerationController,
    blind_a: BlindReviewArtifact,
    blind_b: BlindReviewArtifact,
    config,
    *,
    a_review: ReviewArtifact | None = None,
    b_review: ReviewArtifact | None = None,
) -> tuple[ConsensusLedger, tuple]:
    context = _load_review_context(controller)
    pending = controller.state.pending_review
    if pending is None:
        raise OrcaLoopError("code round has no pending evidence")
    current_ledger_digest = _ledger_content_digest(controller.ledger)
    if current_ledger_digest != pending.pre_round_ledger_digest:
        raise OrcaLoopError("ledger changed while review evidence was pending")
    first = apply_review_artifact(
        controller.ledger,
        _as_review_artifact(blind_a) if a_review is None else a_review,
        Side.CLAUDE,
    )
    second = apply_review_artifact(
        first.ledger,
        _as_review_artifact(blind_b) if b_review is None else b_review,
        Side.CODEX,
    )
    evidence = RoundEvidence(
        kind=ConsensusKind.CODE,
        consensus_round=context.consensus_round,
        reviewed_plan_version=None,
        reviewed_snapshot_digest=context.snapshot_digest,
        artifact_digests=(
            pending.blind_a_artifact_digest or "",
            pending.blind_b_artifact_digest or "",
        ),
        changed_during_round=False,
        both_artifacts_valid=True,
    )
    committed = commit_round(
        second.ledger,
        evidence,
        plan_limit=config.plan_consensus_round_limit,
        code_limit=config.code_consensus_round_limit,
        expected_snapshot_digest=context.snapshot_digest,
    )
    if not committed.committed_round:
        raise OrcaLoopError("code review pair did not form a valid round")
    return (
        committed.ledger,
        first.escalations + second.escalations + committed.escalations,
    )


def _execute_review_compare(
    controller: GenerationController,
    config,
) -> StepExecutionResult:
    pending = controller.state.pending_review
    if pending is None:
        raise OrcaLoopError("review comparison has no pending round")
    context = _load_review_context(controller)
    if capture_snapshot(config.worktree_path).snapshot_digest != context.snapshot_digest:
        raise OrcaLoopError("worktree changed during the code review round")
    blind_a = _load_blind_review(controller, ArtifactKind.CODE_REVIEW_A)
    blind_b = _load_blind_review(controller, ArtifactKind.CODE_REVIEW_B)
    comparison_path = controller.workspace.artifact_dir / "review_comparison.json"
    if pending.stage is PendingReviewStage.BLIND_PAIR_READY:
        comparison = _compare_blind_pair(controller, blind_a, blind_b)
        write_atomic_bytes(
            comparison_path,
            (serialize_json(comparison) + "\n").encode("utf-8"),
        )
        comparison_raw = (serialize_json(comparison) + "\n").encode("utf-8")
        record_artifact_history(
            controller.workspace.root,
            "review_comparison",
            controller.state.generation + 1,
            comparison_raw,
        )
        render_stage_report(
            controller.workspace.root,
            "review_comparison",
            comparison_raw.decode("utf-8"),
            controller.state.generation + 1,
        )
        reveal_digest = digest_value(
            {
                "review_context": comparison.review_context_digest,
                "blind_a": comparison.blind_a_artifact_digest,
                "blind_b": comparison.blind_b_artifact_digest,
                "comparison": comparison.comparison_digest,
            }
        )
        next_pending = replace(
            pending,
            stage=PendingReviewStage.COMPARISON_READY,
            comparison_digest=comparison.comparison_digest,
            reveal_manifest_digest=reveal_digest,
        )
        lineage = replace(
            controller.state.validation_lineage,
            review_comparison_digest=comparison.comparison_digest,
        )
        if comparison.status is ReviewComparisonStatus.AGREED:
            next_ledger, escalations = _apply_code_round(
                controller,
                blind_a,
                blind_b,
                config,
            )
            lineage = replace(
                lineage,
                consensus_snapshot_digest=comparison.snapshot_digest,
            )
            controller.commit(
                stage=StepStage.ARTIFACT_VERIFIED,
                active=None,
                reason="blind review pair agreed and was applied atomically",
                ledger=next_ledger,
                pending_review=next_pending,
                validation_lineage=lineage,
            )
            return StepExecutionResult(
                TransitionSignal(
                    SignalKind.ESCALATE if escalations else SignalKind.AGREED,
                    (
                        "; ".join(item.reason for item in escalations)
                        if escalations
                        else "blind review pair agreed"
                    ),
                    _user_scope(controller).finding_ids,
                ),
                controller.ledger,
                controller.state.test_gate_status,
                escalations,
            )
        controller.commit(
            stage=StepStage.ARTIFACT_VERIFIED,
            active=None,
            reason="blind review pair requires adjudication",
            pending_review=next_pending,
            validation_lineage=lineage,
        )
        return StepExecutionResult(
            TransitionSignal(
                SignalKind.CONFLICT,
                "blind review candidates require symmetric adjudication",
                tuple(
                    finding_id
                    for item in comparison.candidates
                    for finding_id in item.finding_ids
                ),
            ),
            controller.ledger,
            controller.state.test_gate_status,
        )
    if pending.stage is not PendingReviewStage.ADJUDICATION_PAIR_READY:
        raise OrcaLoopError("review comparison pending stage is invalid")
    comparison = parse_review_comparison(comparison_path.read_text(encoding="utf-8"))
    adjudications: list[AdjudicationArtifact] = []
    for kind, expected_digest in (
        (ArtifactKind.REVIEW_ADJUDICATION_A, pending.adjudication_a_artifact_digest),
        (ArtifactKind.REVIEW_ADJUDICATION_B, pending.adjudication_b_artifact_digest),
    ):
        path = controller.workspace.artifact_dir / f"{kind.value}.json"
        if expected_digest is None or not path.is_file() or _digest(path) != expected_digest:
            raise OrcaLoopError(f"{kind.value} provenance mismatch")
        raw_text = path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        expected = ExpectedProvenance(
            run_id=controller.state.run_id,
            task_id=str(raw.get("task_id", "")),
            dispatch_id=str(raw.get("dispatch_id", "")),
            consensus_round=comparison.consensus_round,
            snapshot_digest=comparison.snapshot_digest,
        )
        adjudications.append(
            parse_adjudication_artifact(
                raw_text,
                kind,
                expected,
                expected_context_digest=comparison.review_context_digest,
                comparison=comparison,
                valid_duplicate_targets=tuple(
                    item.finding.finding_id for item in controller.ledger.findings
                ),
            )
        )
    a_adj, b_adj = adjudications
    if any(
        a_item.decision is not b_item.decision
        or a_item.duplicate_of != b_item.duplicate_of
        for a_item, b_item in zip(
            a_adj.candidate_decisions,
            b_adj.candidate_decisions,
            strict=True,
        )
    ):
        return StepExecutionResult(
            TransitionSignal(
                SignalKind.ESCALATE,
                "adjudicators disagreed on candidate disposition",
                tuple(
                    finding_id
                    for item in comparison.candidates
                    for finding_id in item.finding_ids
                ),
            ),
            controller.ledger,
            controller.state.test_gate_status,
        )
    rejected_ids: set[str] = set()
    duplicate_ids: set[str] = set()
    canonical_a_ids: set[str] = set()
    for candidate, disposition in zip(
        comparison.candidates,
        a_adj.candidate_decisions,
        strict=True,
    ):
        if disposition.decision is AdjudicationDecision.REJECT:
            rejected_ids.update(candidate.finding_ids)
        elif disposition.decision is AdjudicationDecision.DUPLICATE:
            duplicate_ids.update(
                item
                for item in candidate.finding_ids
                if item != disposition.duplicate_of
            )
        elif candidate.kind is ReviewConflictKind.FINDING_SIGNATURE:
            canonical_a_ids.update(candidate.finding_ids)
    filtered = rejected_ids | duplicate_ids
    baseline_ids = set(_load_review_context(controller).baseline_finding_ids)
    filtered_new = filtered - baseline_ids
    rejected_baseline = rejected_ids & baseline_ids

    def adjudicated_decisions(items):
        return tuple(
            replace(item, decision=DecisionValue.APPROVE)
            if item.finding_id in rejected_baseline
            else item
            for item in items
            if item.finding_id not in filtered_new
        )

    a_review = _as_review_artifact(
        blind_a,
        findings=(
            item for item in blind_a.findings if item.finding_id not in filtered_new
        ),
        decisions=adjudicated_decisions(blind_a.finding_decisions),
    )
    b_review = _as_review_artifact(
        blind_b,
        findings=(
            next(
                candidate
                for candidate in blind_a.findings
                if candidate.finding_id == item.finding_id
            )
            if item.finding_id in canonical_a_ids
            else item
            for item in blind_b.findings
            if item.finding_id not in filtered_new
        ),
        decisions=adjudicated_decisions(blind_b.finding_decisions),
    )
    next_ledger, escalations = _apply_code_round(
        controller,
        blind_a,
        blind_b,
        config,
        a_review=a_review,
        b_review=b_review,
    )
    lineage = replace(
        controller.state.validation_lineage,
        consensus_snapshot_digest=comparison.snapshot_digest,
    )
    controller.commit(
        stage=StepStage.ARTIFACT_VERIFIED,
        active=None,
        reason="adjudicated review pair applied atomically",
        ledger=next_ledger,
        validation_lineage=lineage,
    )
    return StepExecutionResult(
        TransitionSignal(
            SignalKind.ESCALATE if escalations else SignalKind.AGREED,
            (
                "; ".join(item.reason for item in escalations)
                if escalations
                else "adjudicated review pair committed"
            ),
            _user_scope(controller).finding_ids,
        ),
        controller.ledger,
        controller.state.test_gate_status,
        escalations,
    )


def _step_inputs(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
) -> tuple[StagedInput, ...]:
    state = controller.state.state
    artifacts = controller.workspace.artifact_dir
    if state in {
        LoopState.CODE_REVIEW_A,
        LoopState.CODE_REVIEW_B,
        LoopState.ADJUDICATE_A,
        LoopState.ADJUDICATE_B,
    }:
        context = _load_review_context(controller)
        round_dir = _review_round_dir(controller)
        sealed = (
            ("request.md", preflight.arguments.config.request_path, None),
            ("plan.json", artifacts / "plan.json", None),
            ("plan_review.json", artifacts / "plan_review.json", None),
            (
                "implementation.json",
                artifacts / "implementation.json",
                context.implementation_artifact_digest,
            ),
            (
                "test-evidence.json",
                artifacts / "test_evidence.json",
                context.test_evidence_digest,
            ),
            (
                "review-context.json",
                artifacts / "review_context.json",
                None,
            ),
            (
                "frozen.diff",
                round_dir / "frozen.diff",
                context.frozen_diff_digest,
            ),
            (
                "scope-manifest.json",
                round_dir / "scope-manifest.json",
                context.scope_manifest_digest,
            ),
        )
        values: list[StagedInput] = []
        for name, path, expected_digest in sealed:
            if not path.is_file():
                raise OrcaLoopError(f"required review input is missing: {name}")
            if expected_digest is not None and _digest(path) != expected_digest:
                raise OrcaLoopError(f"sealed review input digest mismatch: {name}")
            values.append(StagedInput(name, path, None))
        if state in {LoopState.ADJUDICATE_A, LoopState.ADJUDICATE_B}:
            for name, path in (
                ("code_review_a.json", artifacts / "code_review_a.json"),
                ("code_review_b.json", artifacts / "code_review_b.json"),
                (
                    "review-comparison.json",
                    artifacts / "review_comparison.json",
                ),
            ):
                if not path.is_file():
                    raise OrcaLoopError(f"required adjudication input is missing: {name}")
                values.append(StagedInput(name, path, None))
        return tuple(values)
    values = [
        StagedInput(
            "request.md",
            preflight.arguments.config.request_path,
            None,
        )
    ]
    for filename in (
        "plan.json",
        "plan_review.json",
        "implementation.json",
        "code_review.json",
        "cross_review.json",
    ):
        path = controller.workspace.artifact_dir / filename
        if path.is_file():
            values.append(StagedInput(filename, path, None))
    plan = _load_plan(controller.workspace.root)
    if role in {Role.CODE_REVIEWER, Role.CROSS_CONFIRMER} and plan is not None:
        frozen = materialize_frozen_review(
            preflight.arguments.config.worktree_path,
            capture_snapshot(preflight.arguments.config.worktree_path),
            plan.affected_files,
            controller.workspace.review_dir,
            destructive_approval_digest=(
                None
                if controller.state.destructive_approval is None
                else controller.state.destructive_approval.decision_digest
            ),
        )
        values.extend(
            (
                StagedInput("frozen.diff", frozen.diff_path, None),
                StagedInput(
                    "scope-manifest.json",
                    frozen.manifest_path,
                    None,
                ),
            )
        )
    return tuple(values)


def _profile_root(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
) -> Path:
    if role is Role.IMPLEMENTER:
        return preflight.arguments.config.worktree_path
    if controller.state.state in {
        LoopState.CODE_REVIEW_A,
        LoopState.CODE_REVIEW_B,
        LoopState.ADJUDICATE_A,
        LoopState.ADJUDICATE_B,
    }:
        context = _load_review_context(controller)
        binding_path = _review_round_dir(controller) / "mirror-binding.json"
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrcaLoopError("shared review mirror binding is invalid") from exc
        if not isinstance(binding, dict) or set(binding) != {
            "schema_version",
            "round",
            "path",
            "source_snapshot_digest",
            "tree_digest",
        }:
            raise OrcaLoopError("shared review mirror binding schema mismatch")
        mirror = Path(str(binding["path"])).resolve()
        if (
            binding["schema_version"] != 1
            or binding["round"] != context.consensus_round
            or binding["source_snapshot_digest"] != context.snapshot_digest
            or binding["tree_digest"] != context.readonly_mirror_digest
            or not mirror.is_dir()
            or _tree_digest(mirror) != context.readonly_mirror_digest
        ):
            raise OrcaLoopError("shared review mirror provenance mismatch")
        return mirror
    review_root = _readonly_mirror_root(
        preflight.arguments.config.worktree_path,
        controller.workspace.review_dir,
        controller.state.run_id,
    )
    return prepare_readonly_mirror(
        preflight.arguments.config.worktree_path,
        review_root,
        controller.state.generation + 1,
    )


def _readonly_mirror_root(
    worktree: Path,
    preferred_root: Path,
    run_id: str,
) -> Path:
    """Choose a mirror parent that can never be nested in its source.

    Normal runs store reviewer mirrors under the harness run directory.  When
    the harness documents or reviews itself, that directory is inside the
    target worktree and ``prepare_readonly_mirror`` correctly refuses it.
    Keep that guard intact and relocate only this self-target case to a stable
    sibling directory keyed by the durable run ID.
    """
    source = worktree.resolve()
    candidate = preferred_root.resolve()
    try:
        candidate.relative_to(source)
    except ValueError:
        return candidate
    return source.parent / ".orca-loop-review" / run_id


def _execute_worker(
    controller: GenerationController,
    pool: WorkerPool,
    preflight: PreflightResult,
    client: OrcaClient,
) -> object | None:
    state = controller.state.state
    role = role_for_state(state)
    step_id = (
        f"g{controller.state.generation + 1:04d}-"
        f"{state.value.lower().replace('_', '-')}"
    )
    _, step = create_run_workspace(
        preflight.arguments.harness_root,
        preflight.arguments.run_id,
        step_id,
        resume=True,
    )
    scope = _user_scope(controller)
    profile_root = _profile_root(
        controller,
        preflight,
        role,
    )
    worker = worker_for_role(pool, role)
    runtime_config = (
        preflight.agent_runtime or default_agent_runtime_config()
    )
    runtime_by_worker = {
        item.worker_key: item for item in runtime_config.agents
    }
    runtime_options = runtime_by_worker.get(worker.worker_key)
    if runtime_options is None:
        raise OrcaLoopError(
            f"agent runtime is missing worker {worker.worker_key.value}"
        )
    profile = build_launch_profile(
        role,
        profile_root,
        step.input_dir,
        step.output_dir,
        preflight.permission_report,
        expected_orca_version=preflight.orca_version,
        runtime_options=runtime_options,
    )
    review_phase = None
    review_lane = None
    review_context_digest = None
    comparison_digest = None
    reveal_manifest_digest = None
    if state in {LoopState.CODE_REVIEW_A, LoopState.CODE_REVIEW_B}:
        review_phase = ReviewPhase.BLIND
        review_lane = (
            ReviewLane.A if state is LoopState.CODE_REVIEW_A else ReviewLane.B
        )
    elif state in {LoopState.ADJUDICATE_A, LoopState.ADJUDICATE_B}:
        review_phase = ReviewPhase.ADJUDICATION
        review_lane = (
            ReviewLane.A if state is LoopState.ADJUDICATE_A else ReviewLane.B
        )
    if review_phase is not None:
        pending = controller.state.pending_review
        if pending is None:
            raise OrcaLoopError("review worker has no pending round")
        review_context_digest = pending.review_context_digest
        comparison_digest = pending.comparison_digest
        reveal_manifest_digest = pending.reveal_manifest_digest
    context = RoleContext(
        role=role,
        provider=runtime_options.provider,
        run_id=controller.state.run_id,
        consensus_round=consensus_round(
            controller.state.state,
            controller.ledger,
        ),
        worktree_path=profile_root,
        step_dir=step.root,
        coordinator_handle=controller.state.coordinator_handle,
        plan_version=controller.state.plan_version,
        snapshot_digest=controller.state.snapshot_digest,
        scope_package=scope,
        test_gate_result=controller.state.test_gate_status,
        test_policy=(
            preflight.test_policy
            if role in {Role.PLANNER, Role.PLAN_REVIEWER}
            else None
        ),
        delivered_finding_ids=scope.finding_ids,
        review_phase=review_phase,
        review_lane=review_lane,
        review_context_digest=review_context_digest,
        comparison_digest=comparison_digest,
        reveal_manifest_digest=reveal_manifest_digest,
    )
    contract = render_role_contract(
        context,
        preflight.arguments.harness_root
        / "prompts"
        / f"{role.value}.md",
    )
    plan = _load_plan(controller.workspace.root)
    def validate_artifact(artifact: object) -> None:
        if isinstance(artifact, PlanDocument):
            if artifact.plan_version != controller.state.plan_version + 1:
                raise ContractViolationError(
                    "plan_version must increment by exactly one"
                )
            request_digest = _digest(
                preflight.arguments.config.request_path
            )
            if artifact.request_digest != request_digest:
                raise ContractViolationError(
                    "plan request_digest does not match the staged request"
                )
            if (
                artifact.test_policy_digest
                != preflight.test_policy.policy_digest
            ):
                raise ContractViolationError(
                    "plan test_policy_digest does not match coordinator policy"
                )
        if isinstance(artifact, ReviewArtifact):
            if (
                artifact.reviewed_plan_version
                != controller.state.plan_version
            ):
                raise ContractViolationError(
                    "reviewed_plan_version does not match current plan"
                )
            expected_paths = {
                Role.PLAN_REVIEWER: (
                    controller.workspace.artifact_dir / "plan.json"
                ),
                Role.CODE_REVIEWER: (
                    controller.workspace.artifact_dir
                    / "implementation.json"
                ),
                Role.CROSS_CONFIRMER: (
                    controller.workspace.artifact_dir
                    / "code_review.json"
                ),
            }
            expected_path = expected_paths[role]
            if (
                not expected_path.is_file()
                or artifact.reviewed_artifact_digest
                != _digest(expected_path)
            ):
                raise ContractViolationError(
                    "reviewed_artifact_digest does not match staged artifact"
                )

    result, artifact = execute_worker_step(
        controller=controller,
        step=step,
        client=client,
        pool=pool,
        profile=profile,
        contract=contract,
        additional_inputs=_step_inputs(
            controller,
            preflight,
            role,
        ),
        worktree=preflight.arguments.config.worktree_path,
        scope=scope,
        affected_files=(
            () if plan is None else plan.affected_files
        ),
        destructive_approval=controller.state.destructive_approval,
        runner_path=preflight.arguments.harness_root / "worker_runner.py",
        orca_executable=client.executable,
        step_timeout_ms=preflight.arguments.config.step_timeout_ms,
        validate_artifact=validate_artifact,
    )
    commit_step_transition(
        controller,
        result,
        preflight.arguments.config,
    )
    return artifact


def _round_evidence(
    controller: GenerationController,
    kind: ConsensusKind,
) -> RoundEvidence:
    artifacts = controller.workspace.artifact_dir
    if kind is ConsensusKind.PLAN:
        first = artifacts / "plan.json"
        second = artifacts / "plan_review.json"
        reviewed_plan_version = controller.state.plan_version
        reviewed_snapshot = None
        round_value = controller.ledger.plan_round + 1
    else:
        raise OrcaLoopError(
            "CODE round evidence is committed by blind-pair comparison"
        )
    both_valid = first.is_file() and second.is_file()
    digests = (
        _digest(first) if first.is_file() else "",
        _digest(second) if second.is_file() else "",
    )
    return RoundEvidence(
        kind=kind,
        consensus_round=round_value,
        reviewed_plan_version=reviewed_plan_version,
        reviewed_snapshot_digest=reviewed_snapshot,
        artifact_digests=digests,
        changed_during_round=False,
        both_artifacts_valid=both_valid,
    )


def _create_decision_task(
    client: OrcaClient,
    run_id: str,
    orchestration_run_id: str,
    control_dir: Path,
    generation: int,
) -> tuple[str, MutationRecord]:
    response, record = execute_mutation(
        client,
        control_dir,
        kind=MutationKind.TASK_CREATE,
        argv=(
            "orchestration",
            "task-create",
            "--run",
            orchestration_run_id,
            "--task-title",
            f"{run_id} user decision",
            "--display-name",
            f"{run_id} user decision",
            "--spec",
            "Review the bound user-decision.md report.",
        ),
        timeout_ms=30_000,
        run_id=run_id,
        generation=generation,
        step_id="user-decision",
        external_id_keys=("task",),
    )
    try:
        result = json.loads(response.result_json)
    except json.JSONDecodeError as exc:
        raise OrcaLoopError(
            "decision task response is malformed"
        ) from exc
    task = result.get("task") if isinstance(result, dict) else None
    task_id = task.get("id") if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not task_id:
        raise OrcaLoopError("decision task response has no task ID")
    # Do not commit the mutation yet.  The Task only becomes locally owned
    # when the Gate binding that uses it is in committed coordinator state.
    # A crash before then must replay this exact request ID, not create another
    # user-decision Task.
    return task_id, record


def _gate_options(
    controller: GenerationController,
    plan,
) -> tuple[str, ...]:
    if controller.state.state is LoopState.HUMAN_GATE:
        return ("merge", "reject", "revise_code", "revise_design")
    if any(
        trigger.code.value == "E-03"
        for trigger in controller.state.pending_escalations
    ):
        return ("merge", "reject", "revise_design")
    blocked = controller.state.blocked_from_state
    if blocked is LoopState.PLAN_CONSENSUS_EVALUATE:
        return ("revise_design", "reject")
    if blocked in {
        LoopState.CONSENSUS_EVALUATE,
        LoopState.TEST_GATE,
    }:
        return ("revise_code", "reject")
    if plan is not None and any(
        item.operation.value in {"delete", "rename"}
        for item in plan.affected_files
    ):
        return ("merge", "reject", "revise_design")
    return ("revise_code", "revise_design", "reject")


def _notice_comment(notice: UserDecisionNotice) -> str:
    """Produce bounded single-line Orca board metadata from trusted notice data."""
    report_path = Path(notice.report_path).name
    options = ",".join(notice.allowed_options)
    comment = (
        "USER DECISION REQUIRED | "
        f"run={notice.run_id} | gate={notice.gate_id} | "
        f"options={options} | report={report_path}"
    )
    return " ".join(comment.split())[:500]


def _notice_target(
    controller: GenerationController,
    *,
    workspace_status: str,
    comment: str,
) -> NoticeTarget:
    return NoticeTarget(
        control_dir=controller.workspace.control_dir,
        worktree_selector=controller.state.worktree_selector,
        coordinator_handle=controller.state.coordinator_handle,
        workspace_status=workspace_status,
        comment=comment,
    )


def _publish_user_decision_notice(
    controller: GenerationController,
    client: OrcaClient,
    *,
    report_path: Path,
    channels: tuple[NoticeChannel, ...],
) -> UserDecisionNotice:
    """Announce a pending decision once per request across every channel.

    ``_resume_gate`` republishes on every resume, so the announcer's per-channel
    idempotency is what keeps a long wait from repeatedly stealing focus.
    """
    binding = controller.state.gate_binding
    if binding is None:
        raise OrcaLoopError("cannot publish a user decision notice without a gate")
    notice = ensure_user_decision_notice(
        controller.workspace.control_dir,
        state=controller.state,
        binding=binding,
        report_path=report_path,
    )
    NoticeAnnouncer(client, channels=channels).announce(
        notice,
        _notice_target(
            controller,
            workspace_status="in-review",
            comment=_notice_comment(notice),
        ),
    )
    return notice


def _close_user_decision_notice(
    controller: GenerationController,
    client: OrcaClient,
    *,
    binding: GateBinding,
) -> None:
    """Settle the board only; re-alerting a decided gate would be noise."""
    notice = resolve_user_decision_notice(
        controller.workspace.control_dir,
        binding=binding,
    )
    if notice is None:
        return
    workspace_status = (
        "completed"
        if controller.state.state in {LoopState.READY_FOR_MERGE, LoopState.REJECTED}
        else "in-progress"
    )
    NoticeAnnouncer(client, channels=(NoticeChannel.ORCA_BOARD,)).announce(
        notice,
        _notice_target(
            controller,
            workspace_status=workspace_status,
            comment=(
                "Orca Loop user decision recorded | "
                f"run={notice.run_id} | state={controller.state.state.value}"
            ),
        ),
        force=frozenset({NoticeChannel.ORCA_BOARD}),
    )


def _qualify_merge(
    controller: GenerationController,
    worktree: Path,
) -> MergeQualification:
    state = controller.state
    lineage = state.validation_lineage
    live = capture_snapshot(worktree).snapshot_digest
    required_snapshots = {
        "state": state.snapshot_digest,
        "test": lineage.test_gate_snapshot_digest,
        "context": lineage.review_context_snapshot_digest,
        "blind_a": lineage.blind_review_a_snapshot_digest,
        "blind_b": lineage.blind_review_b_snapshot_digest,
        "consensus": lineage.consensus_snapshot_digest,
    }
    missing = [name for name, value in required_snapshots.items() if value is None]
    mismatched = [
        name
        for name, value in required_snapshots.items()
        if value is not None and value != live
    ]
    if missing or mismatched:
        raise ResumeBlockedError(
            "merge qualification snapshot lineage is incomplete or stale: "
            f"missing={missing}, mismatched={mismatched}"
        )
    artifacts = controller.workspace.artifact_dir
    test_path = artifacts / "test_evidence.json"
    context_path = artifacts / "review_context.json"
    blind_a_path = artifacts / "code_review_a.json"
    blind_b_path = artifacts / "code_review_b.json"
    comparison_path = artifacts / "review_comparison.json"
    for path in (
        test_path,
        context_path,
        blind_a_path,
        blind_b_path,
        comparison_path,
    ):
        if not path.is_file():
            raise ResumeBlockedError(f"merge qualification artifact is missing: {path.name}")
    try:
        test_evidence = parse_test_evidence(
            test_path.read_text(encoding="utf-8")
        )
        context = parse_review_context(
            context_path.read_text(encoding="utf-8")
        )
        comparison = parse_review_comparison(
            comparison_path.read_text(encoding="utf-8")
        )
    except (OSError, ContractViolationError) as exc:
        raise ResumeBlockedError(
            f"merge qualification artifact is invalid: {exc}"
        ) from exc
    digest_checks = {
        "test_evidence": (
            lineage.test_evidence_digest,
            test_evidence.artifact_digest,
        ),
        "review_context": (
            lineage.review_context_digest,
            context.context_digest,
        ),
        "blind_a": (
            lineage.blind_review_a_artifact_digest,
            _digest(blind_a_path),
        ),
        "blind_b": (
            lineage.blind_review_b_artifact_digest,
            _digest(blind_b_path),
        ),
        "comparison": (
            lineage.review_comparison_digest,
            comparison.comparison_digest,
        ),
    }
    pending = state.pending_review
    if pending is not None and pending.adjudication_a_artifact_digest is not None:
        for lane, expected in (
            ("a", lineage.adjudication_a_artifact_digest),
            ("b", lineage.adjudication_b_artifact_digest),
        ):
            path = artifacts / f"review_adjudication_{lane}.json"
            if not path.is_file():
                raise ResumeBlockedError(
                    f"merge qualification adjudication {lane} is missing"
                )
            digest_checks[f"adjudication_{lane}"] = (expected, _digest(path))
            snapshot_value = getattr(
                lineage,
                f"adjudication_{lane}_snapshot_digest",
            )
            if snapshot_value != live:
                raise ResumeBlockedError(
                    f"merge qualification adjudication {lane} snapshot is stale"
                )
    bad_digests = [
        name
        for name, (expected, actual) in digest_checks.items()
        if expected is None or expected != actual
    ]
    if bad_digests:
        raise ResumeBlockedError(
            "merge qualification artifact digest mismatch: "
            + ", ".join(bad_digests)
        )
    lineage_digest = digest_value(json.loads(serialize_json(lineage)))
    evidence_digests = tuple(
        actual for _, actual in digest_checks.values()
    )
    return MergeQualification(
        qualified=True,
        snapshot_digest=live,
        review_context_digest=context.context_digest,
        validation_lineage_digest=lineage_digest,
        evidence_digests=evidence_digests,
    )


def _ensure_gate(
    controller: GenerationController,
    preflight: PreflightResult,
    client: OrcaClient,
) -> None:
    if controller.state.gate_binding is not None:
        return
    report = build_user_decision_report(
        output_path=controller.workspace.root / "user-decision.md",
        request_text=(
            preflight.arguments.config.request_path.read_text(
                encoding="utf-8"
            )
        ),
        ledger=controller.ledger,
        triggers=controller.state.pending_escalations,
        state=controller.state,
        worktree_path=preflight.arguments.config.worktree_path,
        test_status=controller.state.test_gate_status,
    )
    plan = _load_plan(controller.workspace.root)
    destructive_pending = (
        controller.state.state is not LoopState.HUMAN_GATE
        and plan is not None
        and any(
            item.operation.value in {"delete", "rename"}
            for item in plan.affected_files
        )
        and controller.state.destructive_approval is None
    )
    kind = (
        GateKind.FINAL
        if controller.state.state is LoopState.HUMAN_GATE
        else (
            GateKind.DESTRUCTIVE
            if destructive_pending
            else GateKind.ESCALATION
        )
    )
    qualification = None
    if kind is GateKind.FINAL:
        try:
            qualification = _qualify_merge(
                controller,
                preflight.arguments.config.worktree_path,
            )
        except ResumeBlockedError as exc:
            live = capture_snapshot(preflight.arguments.config.worktree_path)
            controller.commit(
                stage=StepStage.TRANSITION_COMMITTED,
                active=None,
                reason=f"final gate qualification invalidated: {exc}",
                state_value=LoopState.TEST_GATE,
                status=RunStatus.IN_PROGRESS,
                snapshot_digest=live.snapshot_digest,
                validation_lineage=ValidationLineage(),
                clear_gate=True,
                clear_pending_review=True,
                clear_blocked_context=True,
            )
            return
    binding = find_gate_for_report(
        client,
        report=report,
        gate_kind=kind,
        timeout_ms=30_000,
        orchestration_run_id=controller.state.orchestration_run_id,
    )
    if binding is not None:
        if not binding.allowed_options:
            binding = replace(
                binding,
                allowed_options=_gate_options(controller, plan),
            )
        if qualification is not None:
            binding = replace(
                binding,
                snapshot_digest=qualification.snapshot_digest,
                review_context_digest=qualification.review_context_digest,
                validation_lineage_digest=(
                    qualification.validation_lineage_digest
                ),
            )
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="existing user decision gate recovered",
            status=RunStatus.BLOCKED,
            gate_binding=binding,
        )
        _publish_user_decision_notice(
            controller,
            client,
            report_path=report.path,
            channels=preflight.arguments.config.notice_channels,
        )
        return
    task_id, task_mutation = _create_decision_task(
        client,
        controller.state.run_id,
        controller.state.orchestration_run_id or "",
        controller.workspace.control_dir,
        controller.state.generation,
    )
    binding, gate_mutation = create_gate(
        client,
        task_id=task_id,
        report=report,
        gate_kind=kind,
        question=(
            "Choose the final disposition."
            if kind is GateKind.FINAL
            else "Resolve the bounded disagreement or stop the run."
        ),
        options=_gate_options(
            controller,
            plan,
        ),
        timeout_ms=30_000,
        control_dir=controller.workspace.control_dir,
        generation=controller.state.generation,
        run_id=controller.state.run_id,
        commit_after_binding=False,
    )
    if qualification is not None:
        binding = replace(
            binding,
            snapshot_digest=qualification.snapshot_digest,
            review_context_digest=qualification.review_context_digest,
            validation_lineage_digest=qualification.validation_lineage_digest,
        )
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason="user decision gate created",
        status=RunStatus.BLOCKED,
        gate_binding=binding,
    )
    # Both external objects are now represented by the committed GateBinding.
    # Mark their journal records complete only after that durable boundary.
    commit_mutation(controller.workspace.control_dir, task_mutation)
    if gate_mutation is not None:
        commit_mutation(controller.workspace.control_dir, gate_mutation)
    _publish_user_decision_notice(
        controller,
        client,
        report_path=report.path,
        channels=preflight.arguments.config.notice_channels,
    )


def _resume_gate(
    controller: GenerationController,
    preflight: PreflightResult,
    client: OrcaClient,
) -> bool:
    binding = controller.state.gate_binding
    if binding is None:
        return False
    _publish_user_decision_notice(
        controller,
        client,
        report_path=controller.workspace.root / "user-decision.md",
        channels=preflight.arguments.config.notice_channels,
    )
    decision = wait_gate_resolution(
        client,
        binding=binding,
        timeout_ms=30_000,
        orchestration_run_id=controller.state.orchestration_run_id,
    )
    if decision is None:
        return False
    if controller.state.state is LoopState.HUMAN_GATE:
        if decision.decision is HumanDecisionKind.MERGE:
            qualification_problem = None
            try:
                qualification = _qualify_merge(
                    controller,
                    preflight.arguments.config.worktree_path,
                )
                if (
                    binding.snapshot_digest != qualification.snapshot_digest
                    or binding.review_context_digest
                    != qualification.review_context_digest
                    or binding.validation_lineage_digest
                    != qualification.validation_lineage_digest
                ):
                    qualification_problem = (
                        "final gate was created for different validation evidence"
                    )
            except ResumeBlockedError as exc:
                qualification_problem = str(exc)
            if qualification_problem is not None:
                invalidate_user_decision_notice(
                    controller.workspace.control_dir,
                    reason=qualification_problem,
                )
                live = capture_snapshot(
                    preflight.arguments.config.worktree_path
                )
                controller.commit(
                    stage=StepStage.TRANSITION_COMMITTED,
                    active=None,
                    reason=(
                        "stale final gate invalidated: "
                        + qualification_problem
                    ),
                    state_value=LoopState.TEST_GATE,
                    status=RunStatus.IN_PROGRESS,
                    snapshot_digest=live.snapshot_digest,
                    validation_lineage=ValidationLineage(),
                    clear_gate=True,
                    clear_pending_review=True,
                    clear_blocked_context=True,
                )
                return True
        result = execute_human_gate(
            controller.ledger,
            decision,
            gate_kind=GateKind.FINAL,
        )
        commit_step_transition(
            controller,
            result,
            preflight.arguments.config,
        )
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="final human decision provenance recorded",
            human_decision=decision,
            clear_gate=True,
            clear_blocked_context=True,
        )
        _close_user_decision_notice(controller, client, binding=binding)
        return True

    blocked = controller.state.blocked_from_state
    if decision.decision is HumanDecisionKind.REJECT:
        target = LoopState.REJECTED
        status = RunStatus.REJECTED
    elif decision.decision is HumanDecisionKind.REVISE_DESIGN:
        target = LoopState.PLAN_REVISE
        status = RunStatus.IN_PROGRESS
    elif decision.decision is HumanDecisionKind.REVISE_CODE:
        target = LoopState.FIX
        status = RunStatus.IN_PROGRESS
    elif (
        decision.decision is HumanDecisionKind.MERGE
        and any(
            item.code.value == "E-03"
            for item in controller.state.pending_escalations
        )
    ):
        plan = _load_plan(controller.workspace.root)
        updated_ledger = approve_escalation_keys(
            controller.ledger,
            controller.state.pending_escalations,
        )
        approval = controller.state.destructive_approval
        if plan is not None and any(
            item.operation.value in {"delete", "rename"}
            for item in plan.affected_files
        ):
            snapshot = capture_snapshot(
                preflight.arguments.config.worktree_path
            )
            approval, signal = destructive_gate(
                run_id=controller.state.run_id,
                plan=plan,
                manifest=ScopeManifest(
                    snapshot.snapshot_digest,
                    plan.affected_files,
                    None,
                ),
                snapshot=snapshot,
                binding=binding,
                decision=decision,
            )
            if signal.kind is not SignalKind.OK:
                return False
        target = (
            LoopState.IMPLEMENT
            if blocked is LoopState.PLAN_CONSENSUS_EVALUATE
            else LoopState.HUMAN_GATE
        )
        status = RunStatus.IN_PROGRESS
        controller.ledger = updated_ledger
        controller.state = replace(
            controller.state,
            destructive_approval=approval,
        )
    else:
        raise OrcaLoopError(
            "gate resolution is not valid for the blocked state"
        )
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason="user decision resumed the bounded workflow",
        signal=(
            SignalKind.REJECT
            if target is LoopState.REJECTED
            else (
                SignalKind.REVISE_DESIGN
                if target is LoopState.PLAN_REVISE
                else (
                    SignalKind.REVISE_CODE
                    if target is LoopState.FIX
                    else SignalKind.OK
                )
            )
        ),
        state_value=target,
        status=status,
        ledger=controller.ledger,
        human_decision=decision,
        clear_gate=True,
        clear_blocked_context=True,
    )
    _close_user_decision_notice(controller, client, binding=binding)
    return True


def _run_loop(
    controller: GenerationController,
    pool: WorkerPool,
    preflight: PreflightResult,
    client: OrcaClient,
) -> CoordinatorState:
    config = preflight.arguments.config
    started = time.monotonic()
    transitions = 0
    while transitions < config.max_transition_count:
        if (time.monotonic() - started) * 1000 >= config.total_timeout_ms:
            return _stop_for_budget(
                controller,
                "total coordinator timeout exceeded",
            )
        state = controller.state.state
        if state in {LoopState.HUMAN_GATE, LoopState.USER_DECISION_REQUIRED}:
            if controller.state.gate_binding is not None:
                if _resume_gate(
                    controller,
                    preflight,
                    client,
                ):
                    transitions += 1
                    continue
                return controller.state
            _ensure_gate(controller, preflight, client)
            if controller.state.state not in {
                LoopState.HUMAN_GATE,
                LoopState.USER_DECISION_REQUIRED,
            }:
                transitions += 1
                continue
            if (
                controller.state.gate_binding is not None
                and _resume_gate(
                    controller,
                    preflight,
                    client,
                )
            ):
                transitions += 1
                continue
            return controller.state
        if state in TERMINAL_STATES:
            return controller.state
        # A worker step issues task-create and dispatch mutations, so a
        # transient failure inside it leaves their effect unknown. Only a
        # contract violation, which is raised after the mutations settle, may
        # be retried in place there.
        in_worker = state in WORKER_STATES
        try:
            if state in WORKER_STATES:
                _execute_worker(
                    controller,
                    pool,
                    preflight,
                    client,
                )
                transitions += 1
                continue
            if state is LoopState.REVIEW_CONTEXT_PREPARE:
                result = _prepare_review_context(controller, preflight)
            elif state is LoopState.REVIEW_COMPARE:
                result = _execute_review_compare(controller, config)
            elif state is LoopState.PLAN_CONSENSUS_EVALUATE:
                plan = _load_plan(controller.workspace.root)
                if plan is None:
                    raise OrcaLoopError(
                        "plan evaluation has no promoted plan"
                    )
                result = execute_evaluate(
                    state=state,
                    ledger=controller.ledger,
                    evidence=_round_evidence(
                        controller,
                        ConsensusKind.PLAN,
                    ),
                    config=config,
                    plan=plan,
                    destructive_approval=(
                        controller.state.destructive_approval
                    ),
                )
            elif state is LoopState.CONSENSUS_EVALUATE:
                result = execute_evaluate(
                    state=state,
                    ledger=controller.ledger,
                    evidence=None,
                    config=config,
                    plan=_load_plan(controller.workspace.root),
                    destructive_approval=(
                        controller.state.destructive_approval
                    ),
                )
            elif state is LoopState.TEST_GATE:
                plan = _load_plan(controller.workspace.root)
                if plan is None:
                    raise OrcaLoopError("test gate has no promoted plan")
                result = execute_test_gate(
                    ledger=controller.ledger,
                    plan=plan,
                    policy=preflight.test_policy,
                    worktree=config.worktree_path,
                    workspace=controller.workspace,
                    run_id=controller.state.run_id,
                    plan_version=controller.state.plan_version,
                    consensus_round_value=max(
                        1,
                        controller.ledger.code_round + 1,
                    ),
                    generation=controller.state.generation + 1,
                )
            else:
                raise OrcaLoopError(
                    f"unsupported coordinator state: {state.value}"
                )
            commit_step_transition(controller, result, config)
            transitions += 1
        except Exception as exc:
            if classify_stop(exc) is not StopClass.RETRYABLE:
                raise
            if in_worker and not isinstance(exc, ContractViolationError):
                raise
            reason = (
                exc.reason
                if isinstance(exc, ContractViolationError)
                else " ".join(str(exc).split())[:STOP_REASON_LIMIT]
            )
            retry = operational_retry_result(
                ledger=controller.ledger,
                counters=controller.state.counters,
                limit=config.operational_retry_limit,
                reason=reason,
                finding_ids=_user_scope(controller).finding_ids,
            )
            commit_step_transition(controller, retry, config)
            transitions += 1
    return _stop_for_budget(controller, "maximum transition count exceeded")


def _stop_for_budget(
    controller: GenerationController,
    reason: str,
) -> CoordinatorState:
    """Stop the run because this process ran out of budget, not because it broke.

    The budget bounds one coordinator process; a run that merely took longer
    than that is still sound work in progress, so it keeps its state and stays
    resumable. The active step is preserved rather than cleared, so a resume
    still fences whatever worker was bound to it.
    """
    state = controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=controller.state.active,
        reason=reason,
        signal=SignalKind.OPERATIONAL_RETRY,
        status=RunStatus.BLOCKED,
    )
    record_stop_event(
        controller.workspace.control_dir,
        exc=BudgetExhausted(reason),
        classification=StopClass.INTERRUPTED,
        generation=state.generation,
        state=state.state,
        state_committed=True,
    )
    return state


def run_coordinator(
    preflight: PreflightResult,
    client: OrcaClient,
) -> CoordinatorState:
    if preflight.arguments.resume:
        controller, pool = _resume(preflight, client)
    else:
        controller, pool = _initialize(preflight, client)
    # The controller exists only here, so this is the only place that can both
    # judge a stop and record it against the run's own durable state.
    try:
        return _run_loop(controller, pool, preflight, client)
    except BaseException as exc:
        classification = classify_stop(exc)
        committed = False
        if classification is StopClass.TERMINAL:
            try:
                controller.commit(
                    stage=StepStage.TRANSITION_COMMITTED,
                    active=None,
                    reason=f"stopped: {type(exc).__name__}",
                    signal=SignalKind.ABORT,
                    state_value=LoopState.FAILED,
                    status=RunStatus.FAILED,
                )
                committed = True
            except (GenerationError, OSError):
                # A stop mid-commit can leave the generation discontiguous, so
                # the recovery commit fails the same way the run just did.
                # The event below is what survives that.
                committed = False
        record_stop_event(
            controller.workspace.control_dir,
            exc=exc,
            classification=classification,
            generation=controller.state.generation,
            state=controller.state.state,
            state_committed=committed,
        )
        if classification is StopClass.TERMINAL:
            return controller.state
        raise


def exit_code(state: CoordinatorState) -> int:
    if state.state is LoopState.READY_FOR_MERGE:
        return EXIT_READY
    if state.state is LoopState.REJECTED:
        return EXIT_REJECTED
    if state.state in {
        LoopState.HUMAN_GATE,
        LoopState.USER_DECISION_REQUIRED,
    } or state.status is RunStatus.BLOCKED:
        return EXIT_USER_REQUIRED
    return EXIT_RUNTIME_FAILURE


def _record_stop(arguments, exc: BaseException) -> None:
    """Record a stop that happened outside the coordinator's own boundary.

    No controller exists here, so the real generation is unknown. ``-1`` can
    never equal a committed generation, which keeps these events out of the
    status verdict while still preserving them as evidence.
    """
    if arguments is None:
        return
    control = arguments.harness_root / "runs" / arguments.run_id / "control"
    if not control.is_dir():
        return
    record_stop_event(
        control,
        exc=exc,
        classification=classify_stop(exc),
        generation=-1,
        state=None,
        state_committed=False,
    )


def _report_failure(
    arguments,
    reason: str,
    detail: Sequence[str] = (),
) -> None:
    """Leave a readable stop report inside the run directory when one exists."""
    if arguments is None:
        return
    run_root = arguments.harness_root / "runs" / arguments.run_id
    if not run_root.is_dir():
        return
    render_failure_report(
        run_root,
        reason=reason,
        harness_root=arguments.harness_root,
        run_id=arguments.run_id,
        detail=detail,
    )


def _record_permission_refresh(arguments, error) -> tuple[str, ...]:
    """Persist only directly observed permission-contract failures."""
    if arguments is None:
        return ("permission marker skipped: run arguments unavailable",)
    if isinstance(error, CoordinatorGuardError):
        if not any(item.code == "readonly_source_delta" for item in error.violations):
            return ()
        reason_code = "READONLY_SOURCE_DELTA"
        evidence_paths: tuple[str, ...] = ()
    elif isinstance(error, CoordinatorPermissionError):
        reason_code = error.reason_code
        evidence_paths = error.evidence_paths
    else:
        return ()
    try:
        record_permission_refresh_marker(
            arguments.harness_root,
            run_id=arguments.run_id,
            reason_code=reason_code,
            worker_key=error.active.worker.worker_key.value,
            step_id=error.active.step_id,
            blocked_report_digest=error.permission_report_digest,
            evidence_paths=evidence_paths,
        )
    except (AtomicWriteError, OSError, ValueError) as marker_error:
        return (f"MARKER_WRITE_FAILED: {marker_error}",)
    return ()


SUBCOMMANDS = ("start", "resume", "status", "doctor", "force-fail")


def _split_command(
    argv: Sequence[str] | None,
) -> tuple[str, list[str]]:
    """Select the subcommand, defaulting to ``start``.

    Every pre-subcommand invocation form keeps working: a plain flag list is
    read as ``start`` with exactly the arguments it always had.
    """
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in SUBCOMMANDS:
        return values[0], values[1:]
    return "start", values


def _expand_agent_shorthand(values: Sequence[str]) -> list[str]:
    """Turn ``--agent KEY=MODEL/EFFORT`` into the explicit override flags."""
    expanded: list[str] = []
    for item in values:
        key, separator, rest = item.partition("=")
        if not separator:
            raise ConfigurationError(
                "--agent must use WORKER_KEY=MODEL[/EFFORT]"
            )
        model, slash, effort = rest.partition("/")
        model = model.strip()
        effort = effort.strip()
        if not model:
            raise ConfigurationError("--agent model must be nonempty")
        expanded.extend(("--agent-model", f"{key.strip()}={model}"))
        if slash:
            if not effort:
                raise ConfigurationError(
                    "--agent effort must be nonempty when a slash is used"
                )
            expanded.extend(("--agent-effort", f"{key.strip()}={effort}"))
    return expanded


def _start_argv(
    values: Sequence[str],
    client: OrcaClient,
    harness_root: Path,
) -> list[str]:
    """Resolve the inputs an operator used to assemble by hand."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--agent", action="append", default=[])
    pre.add_argument("--no-create-terminals", action="store_true")
    known, rest = pre.parse_known_args(list(values))

    peek = argparse.ArgumentParser(add_help=False)
    peek.add_argument("--run-id")
    peek.add_argument("--worktree")
    peek.add_argument("--coordinator-handle")
    peek.add_argument("--permission-report")
    peek.add_argument("--dry-run", action="store_true")
    peeked, _ = peek.parse_known_args(rest)

    resolved = list(rest) + _expand_agent_shorthand(known.agent)
    if peeked.permission_report is None:
        resolved.extend(
            (
                "--permission-report",
                str(
                    discover_permission_report(
                        harness_root,
                        EXPECTED_ORCA_VERSION,
                    )
                ),
            )
        )
    if peeked.coordinator_handle is None:
        if known.no_create_terminals:
            raise ConfigurationError(
                "--no-create-terminals requires --coordinator-handle"
            )
        if peeked.dry_run:
            # A dry run validates configuration only; creating a terminal it
            # would never use leaks one per rehearsal.
            resolved.extend(("--coordinator-handle", DRY_RUN_HANDLE))
        else:
            current_handle = os.environ.get("ORCA_TERMINAL_HANDLE", "")
            if not current_handle:
                raise ConfigurationError(
                    "start must run inside the intended Orca coordinator "
                    "terminal or provide --coordinator-handle"
                )
            resolved.extend(("--coordinator-handle", current_handle))
    return resolved


def _resume_argv(
    values: Sequence[str],
    harness_root: Path,
) -> list[str]:
    """Rebuild a resume launch from the manifest, given only the run ID."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--accept-worktree-drift", action="store_true")
    parser.add_argument("--force-unlock", action="store_true")
    parser.add_argument("--strict-agent-runtime", action="store_true")
    known, rest = parser.parse_known_args(list(values))
    if rest:
        raise ConfigurationError(
            f"resume does not accept these arguments: {' '.join(rest)}"
        )
    control = harness_root / "runs" / known.run_id / "control"
    manifest = read_manifest(control)
    if manifest is None:
        raise ConfigurationError(
            f"run {known.run_id} has no {control / 'run-manifest.json'}; "
            "resume it with the original explicit flags instead"
        )
    problems = verify_inputs(manifest)
    if problems:
        raise ManifestError("; ".join(problems))
    limits = manifest.limits
    resolved = [
        "--run-id",
        manifest.run_id,
        "--request",
        manifest.request_copy,
        "--worktree",
        manifest.worktree_path,
        "--coordinator-handle",
        manifest.coordinator_handle,
        "--permission-report",
        manifest.permission_report.path,
        "--resume",
    ]
    if manifest.test_policy is not None:
        resolved.extend(("--test-policy", manifest.test_policy.path))
    for key in (
        "test_fix_attempt_limit",
        "operational_retry_limit",
        "max_transition_count",
        "step_timeout_ms",
        "total_timeout_ms",
    ):
        resolved.extend((f"--{key.replace('_', '-')}", str(limits[key])))
    if known.accept_worktree_drift:
        resolved.append("--accept-worktree-drift")
    if known.force_unlock:
        resolved.append("--force-unlock")
    if known.strict_agent_runtime:
        resolved.append("--strict-agent-runtime")
    resolved.extend(
        (
            "--consensus-provider-policy",
            manifest.consensus_provider_policy.value,
        )
    )
    return resolved


def _run_verdict(
    state: CoordinatorState,
    stop: StopEvent | None,
    lock: LockInfo | None,
    run_id: str,
) -> str:
    """State what the run is actually doing right now.

    The last branch is the one that used to have no answer: a run left in
    IN_PROGRESS with nobody holding its lock is a dead coordinator, not work
    still in flight.
    """
    if state.state in {LoopState.READY_FOR_MERGE, LoopState.REJECTED}:
        return "COMPLETED"
    if state.state in {LoopState.HUMAN_GATE, LoopState.USER_DECISION_REQUIRED}:
        return "BLOCKED_ON_USER"
    if state.state is LoopState.FAILED:
        return "STOPPED_TERMINAL"
    if stop is not None and stop.resumable:
        return "STOPPED_RESUMABLE"
    if lock is not None and lock.alive and lock.run_id == run_id:
        return "RUNNING"
    return "STOPPED_RESUMABLE"


def _status_report(harness_root: Path, run_id: str) -> dict[str, object]:
    """Describe a run without changing a single byte of it.

    Status is derived from the blockers that would actually stop a resume, so
    it never reports PASS over a problem an operator has to fix first.
    """
    run_root = harness_root / "runs" / run_id
    control = run_root / "control"
    if not control.is_dir():
        return {"status": "BLOCKED", "error": f"unknown run: {run_id}"}
    value: dict[str, object] = {"run_id": run_id}
    blockers: list[str] = []
    try:
        state, ledger, _ = load_committed(control)
    except (AtomicWriteError, GenerationError) as exc:
        return {"status": "BLOCKED", "error": str(exc)}
    value.update(
        {
            "state": state.state.value,
            "run_status": state.status.value,
            "generation": state.generation,
            "step_stage": state.step_stage.value,
            "plan_version": state.plan_version,
            "plan_round": ledger.plan_round,
            "code_round": ledger.code_round,
            "unresolved_findings": sum(
                1
                for record in ledger.findings
                if record.status.value != "RESOLVED"
            ),
            "artifacts": sorted(
                item.name
                for item in (run_root / "artifacts").glob("*.json")
            ),
            "reports": sorted(
                item.name for item in (run_root / "reports").glob("*.md")
            ),
            "resume_command": resume_command_line(harness_root, run_id),
        }
    )
    try:
        notice = read_user_decision_notice(control)
    except DecisionReportError as exc:
        notice = None
        notice_problem = f"invalid user decision notice: {exc}"
        value["notice_problems"] = [notice_problem]
        blockers.append(notice_problem)
    else:
        notice_problems: list[str] = []
        if notice is not None:
            binding = state.gate_binding
            if notice.run_id != state.run_id:
                notice_problems.append("user decision notice run_id is stale")
            elif notice.orchestration_run_id != state.orchestration_run_id:
                notice_problems.append(
                    "user decision notice Orca Run binding is stale"
                )
            elif binding is not None and (
                notice.gate_id != binding.gate_id
                or notice.report_digest != binding.report_digest
            ):
                notice_problems.append("user decision notice does not match gate binding")
            elif binding is None and notice.status.value == "PENDING":
                notice_problems.append("pending user decision notice has no gate binding")
            if notice.status.value == "PENDING" and not notice_problems:
                value["pending_user_decision"] = {
                    "request_id": notice.request_id,
                    "gate_id": notice.gate_id,
                    "gate_kind": notice.gate_kind.value,
                    "allowed_options": list(notice.allowed_options),
                    "reason": notice.reason,
                    "report_path": notice.report_path,
                    "resume_command": resume_command_line(harness_root, run_id),
                }
        try:
            delivery = read_user_decision_notice_delivery(control)
        except DecisionReportError as exc:
            notice_problems.append(
                f"invalid user decision notice delivery: {exc}"
            )
        else:
            if delivery is not None:
                if notice is None or delivery.request_id != notice.request_id:
                    notice_problems.append(
                        "user decision notice delivery does not match notice"
                    )
                order = list(NoticeChannel)
                value["user_decision_notice_delivery"] = {
                    "request_id": delivery.request_id,
                    "attempted_at": delivery.attempted_at,
                    "channels": [
                        {
                            "channel": record.channel.value,
                            "status": record.status.value,
                            "attempted_at": record.attempted_at,
                            "detail": record.detail,
                        }
                        for record in sorted(
                            delivery.channels,
                            key=lambda item: order.index(item.channel),
                        )
                    ],
                }
        if notice_problems:
            value["notice_problems"] = notice_problems
            blockers.extend(notice_problems)
    if state.state in {LoopState.FAILED, LoopState.REJECTED}:
        blockers.append(f"run ended in {state.state.value}")
    elif state.state in {
        LoopState.HUMAN_GATE,
        LoopState.USER_DECISION_REQUIRED,
    }:
        blockers.append("run is awaiting a human gate decision")
    if not state.orchestration_run_id:
        blockers.append(
            "run predates durable Orca Run binding; it cannot be resumed"
        )
    try:
        manifest = read_manifest(control)
    except ManifestError as exc:
        manifest = None
        blockers.append(str(exc))
    if manifest is None:
        value["worktree"] = None
    else:
        value["worktree"] = manifest.worktree_path
        value["consensus_provider_policy"] = (
            manifest.consensus_provider_policy.value
        )
        value["consensus_independence"] = (
            manifest.consensus_independence.value
        )
        value["validation_lineage"] = json.loads(
            serialize_json(state.validation_lineage)
        )
        value["agents"] = {
            record.worker_key.value: {
                "provider": record.provider.value,
                "model": record.model,
                "effort": record.effort,
            }
            for record in manifest.agents
        }
        identity = manifest_identity_problems(
            manifest,
            requested_run_id=run_id,
            harness_root=harness_root,
        )
        input_problems = list(verify_inputs(manifest))
        value["input_problems"] = input_problems
        value["identity_problems"] = list(identity)
        blockers.extend(identity)
        blockers.extend(input_problems)
    # Only look for a lock once a real worktree is known: an empty path would
    # resolve to the process working directory and report a foreign lock.
    worktree = value.get("worktree")
    lock = None
    if isinstance(worktree, str) and worktree:
        lock = inspect_lock(harness_root, Path(worktree))
        if lock is not None:
            value["lock"] = {
                "path": str(lock.path),
                "run_id": lock.run_id,
                "pid": lock.pid,
                "alive": lock.alive,
            }
            if lock.run_id != run_id and lock.alive:
                blockers.append(
                    f"worktree is locked by run {lock.run_id} (pid {lock.pid})"
                )
    stop = read_latest_stop_event(control)
    # A stop recorded against an earlier generation was already resumed past,
    # so it describes history rather than the run's current condition.
    if stop is not None and stop.generation == state.generation:
        value["stop"] = {
            "classification": stop.classification.value,
            "exception": stop.exception,
            "reason": stop.reason,
            "resumable": stop.resumable,
            "state_committed": stop.state_committed,
            "recorded_at": stop.recorded_at,
        }
    else:
        stop = None
    value["verdict"] = _run_verdict(state, stop, lock, run_id)
    value["blockers"] = blockers
    value["resumable"] = not blockers
    value["status"] = "BLOCKED" if blockers else "PASS"
    return value


def _force_fail(harness_root: Path, values: Sequence[str]) -> int:
    """End a stopped run on the operator's authority.

    The boundary deliberately preserves state for anything it cannot prove is
    terminal, so an operator needs a way to close a run out by hand.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--reason", required=True)
    try:
        known, _ = parser.parse_known_args(list(values))
    except SystemExit:
        _emit_error("force-fail requires --run-id and --reason")
        return EXIT_PREFLIGHT
    if not known.reason.strip():
        _emit_error("force-fail --reason must be nonempty")
        return EXIT_PREFLIGHT
    control = harness_root / "runs" / known.run_id / "control"
    if not control.is_dir():
        _emit_error(f"unknown run: {known.run_id}")
        return EXIT_PREFLIGHT
    try:
        state, ledger, _ = load_committed(control)
    except (AtomicWriteError, GenerationError) as exc:
        _emit_error(str(exc))
        return EXIT_PREFLIGHT
    if state.state in TERMINAL_STATES:
        _emit_error(f"run already ended in {state.state.value}")
        return EXIT_PREFLIGHT
    try:
        manifest = read_manifest(control)
    except ManifestError as exc:
        _emit_error(str(exc))
        return EXIT_PREFLIGHT
    lock = inspect_lock(harness_root, Path(manifest.worktree_path))
    if lock is not None and lock.alive and lock.run_id == known.run_id:
        _emit_error(
            f"run {known.run_id} still holds its lock (pid {lock.pid}); "
            "stop the coordinator first"
        )
        return EXIT_PREFLIGHT
    step_id = (
        state.active.step_id if state.active is not None else "g0000-force-fail"
    )
    try:
        workspace, _ = create_run_workspace(
            harness_root,
            known.run_id,
            step_id,
            resume=True,
        )
        final = GenerationController(workspace, state, ledger).commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=f"force-fail: {known.reason.strip()}",
            signal=SignalKind.ABORT,
            state_value=LoopState.FAILED,
            status=RunStatus.FAILED,
        )
    except (GenerationError, WorkspaceError, OSError) as exc:
        _emit_error(str(exc))
        return EXIT_PREFLIGHT
    append_event(
        control,
        FORCE_FAIL_EVENT_KIND,
        {
            "reason": known.reason.strip()[:STOP_REASON_LIMIT],
            "generation": final.generation,
            "previous_state": state.state.value,
        },
    )
    _emit(
        {
            "status": final.status.value,
            "state": final.state.value,
            "run_id": final.run_id,
            "generation": final.generation,
        }
    )
    return EXIT_READY


def _emit_error(message: str) -> None:
    print(
        json.dumps(
            {"status": "BLOCKED", "error": message},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=sys.stderr,
    )


def _doctor_report(harness_root: Path) -> dict[str, object]:
    value: dict[str, object] = {"status": "PASS", "harness_root": str(harness_root)}
    try:
        client = OrcaClient(cwd=harness_root)
        response = client.call(("status",), timeout_ms=10_000)
        status = json.loads(response.result_json)
        value["orca"] = {
            "executable": client.executable,
            "runtime": (status.get("runtime") or {}).get("state"),
            "graph": (status.get("graph") or {}).get("state"),
            "version": orca_version_from_status(status),
            "expected_version": EXPECTED_ORCA_VERSION,
        }
    except (CoordinatorGuardError, CoordinatorPermissionError) as exc:
        marker_detail = _record_permission_refresh(arguments, exc)
        _report_failure(arguments, str(exc), marker_detail)
        print(
            json.dumps(
                {"status": "FAIL", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE
    except (
        OrcaCommandError,
        PreflightError,
        json.JSONDecodeError,
        AttributeError,
    ) as exc:
        value["status"] = "BLOCKED"
        value["orca"] = {"error": str(exc)}
    value["agent_cli"] = {
        name: shutil.which(name) or "not found"
        for name in ("claude", "codex")
    }
    catalog = load_catalog(harness_root)
    value["catalog"] = list(describe_catalog(catalog))
    live_version = str(
        (value.get("orca") or {}).get("version") or EXPECTED_ORCA_VERSION
    )
    value["environment"] = describe_environment(
        capture_environment(harness_root)
    )
    reports = []
    for path in permission_report_candidates(harness_root):
        problem = permission_report_problem(
            path,
            live_version,
            harness_root=harness_root,
        )
        reports.append(
            {"path": str(path), "usable": problem is None, "problem": problem}
        )
    value["permission_reports"] = reports
    if not any(item["usable"] for item in reports):
        value["status"] = "BLOCKED"
    locks = []
    lock_dir = harness_root / "runs" / ".locks"
    if lock_dir.is_dir():
        for path in sorted(lock_dir.glob("*.lock")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                locks.append({"path": str(path), "readable": False})
                continue
            pid = raw.get("pid")
            locks.append(
                {
                    "path": str(path),
                    "readable": True,
                    "run_id": raw.get("run_id"),
                    "pid": pid,
                    "alive": (
                        pid_alive(pid) if isinstance(pid, int) else None
                    ),
                }
            )
    value["locks"] = locks
    return value


def _emit(value: Mapping[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _print_subcommand_help(command: str) -> None:
    usage = {
        "start": (
            "usage: run_loop.py start --run-id ID --request PATH --worktree PATH "
            "--agent KEY=MODEL/EFFORT [options]"
        ),
        "resume": "usage: run_loop.py resume --run-id ID [--accept-worktree-drift] [--force-unlock]",
        "status": "usage: run_loop.py status --run-id ID",
        "doctor": "usage: run_loop.py doctor",
        "force-fail": (
            "usage: run_loop.py force-fail --run-id ID --reason TEXT"
        ),
    }
    print(usage[command])


def main(argv: Sequence[str] | None = None) -> int:
    harness_root = Path(__file__).resolve().parent
    command, values = _split_command(argv)
    if values and all(item in {"-h", "--help"} for item in values):
        _print_subcommand_help(command)
        return EXIT_READY
    if command == "doctor":
        report = _doctor_report(harness_root)
        _emit(report)
        return EXIT_READY if report["status"] == "PASS" else EXIT_PREFLIGHT
    if command == "status":
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--run-id", required=True)
        known, _ = parser.parse_known_args(values)
        report = _status_report(harness_root, known.run_id)
        _emit(report)
        return EXIT_READY if report["status"] == "PASS" else EXIT_PREFLIGHT
    if command == "force-fail":
        return _force_fail(harness_root, values)

    lock = None
    arguments = None
    try:
        client = OrcaClient(cwd=harness_root)
        if command == "resume":
            values = _resume_argv(values, harness_root)
        else:
            values = _start_argv(values, client, harness_root)
        arguments = parse_run_arguments(
            values,
            harness_root=harness_root,
        )
        preflight = run_preflight(
            arguments,
            client,
            expected_orca_version=EXPECTED_ORCA_VERSION,
            verify_coordinator=(
                not arguments.resume
                and arguments.config.coordinator_handle != DRY_RUN_HANDLE
            ),
        )
        preflight = prepare_agent_runtime(preflight)
        if arguments.dry_run:
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "mode": "dry-run",
                        "run_id": arguments.run_id,
                        "orca_version": preflight.orca_version,
                        "permission_report": str(
                            arguments.permission_report_path
                        ),
                        "plan_consensus_round_limit": 5,
                        "code_consensus_round_limit": 5,
                        "consensus_provider_policy": (
                            preflight.consensus_provider_policy.value
                        ),
                        "consensus_independence": (
                            preflight.consensus_independence.value
                        ),
                        "agents": {
                            item.worker_key.value: {
                                "provider": item.provider.value,
                                "requested_model": item.model.requested,
                                "model": item.model.value,
                                "model_resolution": item.model.method,
                                "requested_effort": item.effort.requested,
                                "effort": item.effort.value,
                                "effort_resolution": item.effort.method,
                            }
                            for item in preflight.agent_resolutions
                        },
                        "warnings": [
                            f"{item.worker_key.value}: {warning}"
                            for item in preflight.agent_resolutions
                            for warning in item.warnings
                        ],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return EXIT_READY
        lock = acquire_run_lock(
            arguments.harness_root,
            arguments.config.worktree_path,
            arguments.run_id,
            reclaim_stale=True,
            force=arguments.force_unlock,
            on_reclaim=lambda info: print(
                json.dumps(
                    {
                        "status": "INFO",
                        "event": "stale_lock_reclaimed",
                        "lock": str(info.path),
                        "previous_run_id": info.run_id,
                        "previous_pid": info.pid,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                file=sys.stderr,
            ),
        )
        final = run_coordinator(preflight, client)
        print(
            json.dumps(
                {
                    "status": final.status.value,
                    "state": final.state.value,
                    "run_id": final.run_id,
                    "generation": final.generation,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return exit_code(final)
    except (
        ConfigurationError,
        PreflightError,
        RunWorkspaceExistsError,
        ManifestError,
    ) as exc:
        print(
            json.dumps(
                {"status": "BLOCKED", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_PREFLIGHT
    except ResumeBlockedError as exc:
        _report_failure(arguments, str(exc))
        print(
            json.dumps(
                {"status": "BLOCKED", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_USER_REQUIRED
    except (
        OrcaLoopError,
        DispatcherError,
        OrcaCommandError,
        AtomicWriteError,
        GateProtocolError,
        RunLockError,
    ) as exc:
        _report_failure(arguments, str(exc))
        print(
            json.dumps(
                {"status": "FAIL", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE
    except KeyboardInterrupt:
        _record_stop(arguments, KeyboardInterrupt("interrupted"))
        _report_failure(arguments, "interrupted")
        print(
            json.dumps(
                {"status": "FAIL", "error": "interrupted"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE
    except BaseException as exc:
        # Last boundary. Without it the classes that no handler above names
        # leave a traceback and no evidence at all.
        _record_stop(arguments, exc)
        _report_failure(arguments, str(exc))
        print(
            json.dumps(
                {
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_RUNTIME_FAILURE
    finally:
        if lock is not None:
            release_run_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
