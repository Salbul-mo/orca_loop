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
    parse_plan_document,
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
    read_user_decision_notice,
    read_user_decision_notice_delivery,
    resolve_user_decision_notice,
    wait_gate_resolution,
)
from orca_loop.notify import NoticeAnnouncer, NoticeTarget
from orca_loop.generation import (
    AtomicWriteError,
    GenerationError,
    commit_generation,
    load_committed,
    write_atomic_bytes,
)
from orca_loop.ledger import empty_ledger, unresolved_scope
from orca_loop.locking import (
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
    ArtifactKind,
    ConsensusKind,
    CoordinatorState,
    GateKind,
    GateBinding,
    HumanDecisionKind,
    LaunchProfile,
    LoopCounters,
    NoticeChannel,
    LoopState,
    PlanDocument,
    ResumeDecision,
    ReviewArtifact,
    Role,
    RoleContext,
    RoundEvidence,
    RunStatus,
    ScopeManifest,
    ScopePackage,
    SignalKind,
    StepExecutionResult,
    StepStage,
    StagedInput,
    TestGateStatus,
    TransitionSignal,
    UserDecisionNotice,
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
from orca_loop.reporting import render_failure_report, resume_command_line
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


# States whose step only reads the worktree. Resuming one after the tree
# changed cannot corrupt anything, so the recorded snapshot is re-baselined
# automatically; a write step is not resumed over a changed tree without an
# explicit --accept-worktree-drift.
READ_ONLY_RESUME_STATES = frozenset(
    {
        LoopState.PLAN,
        LoopState.PLAN_REVISE,
        LoopState.PLAN_REVIEW,
        LoopState.CODE_REVIEW,
        LoopState.CROSS_CONFIRM,
        LoopState.PLAN_CONSENSUS_EVALUATE,
        LoopState.CONSENSUS_EVALUATE,
        LoopState.TEST_GATE,
        LoopState.HUMAN_GATE,
        LoopState.USER_DECISION_REQUIRED,
    }
)


@dataclass(frozen=True)
class DriftDecision:
    drifted: bool
    rebaselined: bool
    new_digest: str | None
    detail: tuple[str, ...]


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
    if state.state in READ_ONLY_RESUME_STATES or accept:
        return DriftDecision(True, True, current.snapshot_digest, detail)
    raise ResumeBlockedError(
        "worktree changed since the last committed generation while the run "
        f"was in {state.state.value}, which writes to the worktree. Review "
        "the changes, then resume with --accept-worktree-drift to continue "
        "from the current tree. "
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


def _step_inputs(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
) -> tuple[StagedInput, ...]:
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
    if (
        role in {Role.CODE_REVIEWER, Role.CROSS_CONFIRMER}
        and plan is not None
    ):
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
        first = artifacts / "code_review.json"
        second = artifacts / "cross_review.json"
        reviewed_plan_version = None
        reviewed_snapshot = controller.state.snapshot_digest
        round_value = controller.ledger.code_round + 1
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
            controller.commit(
                stage=StepStage.TRANSITION_COMMITTED,
                active=None,
                reason="total coordinator timeout exceeded",
                signal=SignalKind.ABORT,
                state_value=LoopState.FAILED,
                status=RunStatus.FAILED,
            )
            return controller.state
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
            if state is LoopState.PLAN_CONSENSUS_EVALUATE:
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
                    evidence=_round_evidence(
                        controller,
                        ConsensusKind.CODE,
                    ),
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
                )
            else:
                raise OrcaLoopError(
                    f"unsupported coordinator state: {state.value}"
                )
            commit_step_transition(controller, result, config)
            transitions += 1
        except ContractViolationError as exc:
            retry = operational_retry_result(
                ledger=controller.ledger,
                counters=controller.state.counters,
                limit=config.operational_retry_limit,
                error=exc,
                finding_ids=_user_scope(controller).finding_ids,
            )
            commit_step_transition(controller, retry, config)
            transitions += 1
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason="maximum transition count exceeded",
        signal=SignalKind.ABORT,
        state_value=LoopState.FAILED,
        status=RunStatus.FAILED,
    )
    return controller.state


def run_coordinator(
    preflight: PreflightResult,
    client: OrcaClient,
) -> CoordinatorState:
    if preflight.arguments.resume:
        controller, pool = _resume(preflight, client)
    else:
        controller, pool = _initialize(preflight, client)
    return _run_loop(controller, pool, preflight, client)


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


SUBCOMMANDS = ("start", "resume", "status", "doctor")


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
    return resolved


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
    value["blockers"] = blockers
    value["resumable"] = not blockers
    value["status"] = "BLOCKED" if blockers else "PASS"
    return value


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
        _report_failure(arguments, "interrupted")
        print(
            json.dumps(
                {"status": "FAIL", "error": "interrupted"},
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
