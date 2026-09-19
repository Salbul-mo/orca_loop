from __future__ import annotations

import hashlib
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from orca_loop.config import (
    ConfigurationError,
    PreflightError,
    PreflightResult,
    default_agent_runtime_config,
    parse_run_arguments,
    persist_agent_runtime_snapshot,
    persist_master_runtime_snapshot,
    prepare_agent_runtime,
    prepare_master_runtime,
    run_preflight,
)
from orca_loop.contracts import (
    ContractViolationError,
    parse_plan_document,
    to_wire_value,
)
from orca_loop.coordinator import (
    GenerationController,
    OrcaLoopError,
    WORKER_STATES,
    commit_step_transition,
    consensus_round,
    default_permission_profile,
    execute_evaluate,
    execute_human_gate,
    execute_test_gate,
    execute_worker_step,
    ledger_view,
    operational_retry_result,
    permission_policy_digest,
    role_for_state,
    validate_master_decision,
)
from orca_loop.dispatcher import provision_workers, worker_for_role
from orca_loop.escalation import (
    GateProtocolError,
    approve_escalation_keys,
    build_user_decision_report,
    create_gate,
    destructive_gate,
    wait_gate_resolution,
)
from orca_loop.generation import (
    AtomicWriteError,
    commit_generation,
    load_committed,
)
from orca_loop.ledger import InvalidRoundError, empty_ledger, unresolved_scope
from orca_loop.locking import (
    RunLockError,
    acquire_run_lock,
    release_run_lock,
)
from orca_loop.machine import TERMINAL_STATES
from orca_loop.master_runtime import (
    MasterInvoker,
    MasterRuntimeAdapterError,
    invoke_master,
    invoke_master_provider,
)
from orca_loop.models import (
    ActiveStep,
    ArtifactKind,
    CodeReviewVerdict,
    ConsensusKind,
    CoordinatorState,
    GateKind,
    HumanDecisionKind,
    LaunchProfile,
    LoopCounters,
    LoopState,
    MasterAction,
    PlanDocument,
    PlanReviewVerdict,
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
    WorkerKey,
    WorkerPool,
)
from orca_loop.orca_client import OrcaClient, OrcaCommandError
from orca_loop.profiles import build_launch_profile
from orca_loop.readonly import prepare_readonly_mirror
from orca_loop.roles import ARTIFACT_FILENAMES, render_role_contract
from orca_loop.snapshot import capture_snapshot, materialize_frozen_review
from orca_loop.workspace import (
    RunWorkspaceExistsError,
    create_run_workspace,
)


EXPECTED_ORCA_VERSION = "1.4.159"
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
        schema_version=1,
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
        permission_policy_digest=permission_policy_digest(),
        history=(),
    )


def _dummy_profiles(worktree: Path) -> dict[WorkerKey, LaunchProfile]:
    profile = LaunchProfile(
        ("not-executed", "-C", str(worktree.resolve())),
        (),
    )
    return {key: profile for key in WorkerKey}


def _initial_master_context(
    preflight: PreflightResult,
    controller: GenerationController,
) -> dict[str, object]:
    return {
        "stage": "initial_routing",
        "runId": controller.state.run_id,
        "currentState": controller.state.state.value,
        "request": preflight.arguments.config.request_path.read_text(
            encoding="utf-8"
        ),
        "allowedDispatches": [
            {
                "role": Role.PLANNER.value,
                "permissionProfile": "read_only",
            },
            {
                "role": Role.IMPLEMENTER.value,
                "permissionProfile": "workspace_write",
            },
        ],
    }


def _commit_initial_route(
    controller: GenerationController,
    preflight: PreflightResult,
    *,
    master_invoke: MasterInvoker | None = None,
) -> None:
    arguments = preflight.arguments
    if preflight.master_runtime is None:
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
        return
    if controller.state.state is not LoopState.INIT:
        raise OrcaLoopError(
            "initial Master routing requires coordinator state INIT"
        )
    invoke = master_invoke
    if invoke is None:
        invoke = lambda options, prompt: invoke_master_provider(
            options,
            prompt,
            timeout_ms=arguments.config.step_timeout_ms,
        )
    try:
        decision = invoke_master(
            preflight.master_runtime,
            arguments.harness_root / "prompts" / "master.md",
            _initial_master_context(preflight, controller),
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise OrcaLoopError(f"initial Master routing failed: {exc}") from exc
    decision = validate_master_decision(decision)
    if decision.action is not MasterAction.DISPATCH:
        raise OrcaLoopError(
            "initial Master routing requires action dispatch"
        )
    route_by_role = {
        Role.PLANNER: LoopState.PLAN,
        Role.IMPLEMENTER: LoopState.IMPLEMENT,
    }
    target = route_by_role.get(decision.role)
    if target is None:
        role_value = None if decision.role is None else decision.role.value
        raise OrcaLoopError(
            "initial Master routing role is not allowed: "
            f"{role_value}"
        )
    controller.commit(
        stage=StepStage.STEP_PENDING,
        active=None,
        reason=f"initial Master dispatch: {decision.reason}",
        signal=SignalKind.OK,
        state_value=target,
    )


def _initialize(
    preflight: PreflightResult,
    client: OrcaClient,
    *,
    master_invoke: MasterInvoker | None = None,
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
    if preflight.master_runtime is not None:
        master_source_path = (
            None
            if arguments.master_runtime_request is None
            else arguments.master_runtime_request.source_path
        )
        persist_master_runtime_snapshot(
            workspace.control_dir,
            arguments.run_id,
            preflight.master_runtime,
            master_source_path,
        )
    controller = GenerationController(workspace, state, ledger)
    pool = provision_workers(
        client,
        state.worktree_selector,
        _dummy_profiles(arguments.config.worktree_path),
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
    _commit_initial_route(
        controller,
        preflight,
        master_invoke=master_invoke,
    )
    return controller, pool


def _resume(
    preflight: PreflightResult,
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
    if state.coordinator_handle != arguments.config.coordinator_handle:
        raise OrcaLoopError(
            "resume coordinator handle does not match committed state"
        )
    if state.permission_policy_digest != permission_policy_digest():
        raise OrcaLoopError(
            "resume permission policy does not match committed state"
        )
    snapshot = capture_snapshot(arguments.config.worktree_path)
    if snapshot.snapshot_digest != state.snapshot_digest:
        raise OrcaLoopError(
            "resume worktree snapshot does not match committed state"
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
    master_runtime_path = workspace.control_dir / "master-runtime.json"
    if (
        not master_runtime_path.exists()
        and preflight.master_runtime is not None
    ):
        master_source_path = (
            None
            if arguments.master_runtime_request is None
            else arguments.master_runtime_request.source_path
        )
        persist_master_runtime_snapshot(
            workspace.control_dir,
            arguments.run_id,
            preflight.master_runtime,
            master_source_path,
        )
    pool = WorkerPool(state.worker_handles)
    if len(pool.workers) != 4:
        raise OrcaLoopError("committed worker pool is incomplete")
    return GenerationController(workspace, state, ledger), pool


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
    return prepare_readonly_mirror(
        preflight.arguments.config.worktree_path,
        controller.workspace.review_dir,
        controller.state.generation + 1,
    )


def _worker_completion_master_context(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
    result: StepExecutionResult,
    artifact: object,
    *,
    allow_plan_review_implement: bool = False,
    allow_cross_confirm_finish: bool = False,
) -> dict[str, object]:
    state = controller.state.state
    if state in {LoopState.PLAN, LoopState.PLAN_REVISE}:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.PLAN_REVIEWER.value,
                "permissionProfile": "read_only",
            },
        ]
        if _plan_review_can_be_skipped(result, artifact):
            allowed.append(
                {
                    "action": MasterAction.DISPATCH.value,
                    "role": Role.IMPLEMENTER.value,
                    "permissionProfile": "workspace_write",
                }
            )
        allowed.append(
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            }
        )
    elif state is LoopState.PLAN_REVIEW and allow_plan_review_implement:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.IMPLEMENTER.value,
                "permissionProfile": "workspace_write",
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ]
    elif state in {LoopState.IMPLEMENT, LoopState.FIX}:
        allowed = [
            {
                "action": MasterAction.TEST.value,
                "role": None,
                "permissionProfile": None,
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ]
    elif state is LoopState.CODE_REVIEW:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.CROSS_CONFIRMER.value,
                "permissionProfile": "read_only",
            },
        ]
        if _code_review_can_finish(controller, result, artifact):
            allowed.append(
                {
                    "action": MasterAction.FINISH.value,
                    "role": None,
                    "permissionProfile": None,
                }
            )
        allowed.append(
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            }
        )
    elif state is LoopState.CROSS_CONFIRM and allow_cross_confirm_finish:
        allowed = [
            {
                "action": MasterAction.FINISH.value,
                "role": None,
                "permissionProfile": None,
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ]
    else:
        allowed = []
    return {
        "stage": "worker_completion",
        "runId": controller.state.run_id,
        "currentState": state.value,
        "completedRole": role.value,
        "request": preflight.arguments.config.request_path.read_text(
            encoding="utf-8"
        ),
        "artifact": to_wire_value(artifact),
        "resultSignal": result.signal.kind.value,
        "allowedDecisions": allowed,
    }


def _plan_review_can_be_skipped(
    result: StepExecutionResult,
    artifact: object,
) -> bool:
    if not isinstance(artifact, PlanDocument):
        return False
    if ledger_view(result.ledger).unresolved_count != 0:
        return False
    if artifact.data_api_schema_changes.strip() not in {"", "없음", "none", "None"}:
        return False
    return not any(
        item.operation.value in {"delete", "rename"}
        for item in artifact.affected_files
    )


def _code_review_can_finish(
    controller: GenerationController,
    result: StepExecutionResult,
    artifact: object,
) -> bool:
    if result.signal.kind is not SignalKind.ARTIFACT_OK:
        return False
    if result.test_gate_status is not TestGateStatus.PASS:
        return False
    if not isinstance(artifact, ReviewArtifact):
        return False
    if (
        artifact.artifact_kind is not ArtifactKind.CODE_REVIEW
        or artifact.role is not Role.CODE_REVIEWER
        or artifact.verdict is not CodeReviewVerdict.APPROVE
    ):
        return False
    if (
        artifact.reviewed_finding_ids
        or artifact.finding_decisions
        or artifact.findings
        or artifact.non_blocking_suggestions
        or artifact.escalation_signals
        or result.escalations
    ):
        return False
    plan = _load_plan(controller.workspace.root)
    return plan is not None and _plan_review_can_be_skipped(result, plan)


def _plan_review_implement_preview(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    artifact: object,
) -> StepExecutionResult | None:
    if result.signal.kind is not SignalKind.ARTIFACT_OK:
        return None
    if not isinstance(artifact, ReviewArtifact):
        return None
    if (
        artifact.artifact_kind is not ArtifactKind.PLAN_REVIEW
        or artifact.role is not Role.PLAN_REVIEWER
        or artifact.verdict is not PlanReviewVerdict.APPROVE
    ):
        return None
    if (
        artifact.reviewed_finding_ids
        or artifact.finding_decisions
        or artifact.findings
        or artifact.non_blocking_suggestions
        or artifact.escalation_signals
        or result.escalations
    ):
        return None
    plan = _load_plan(controller.workspace.root)
    if plan is None:
        return None
    try:
        preview = execute_evaluate(
            state=LoopState.PLAN_CONSENSUS_EVALUATE,
            ledger=result.ledger,
            evidence=_round_evidence(controller, ConsensusKind.PLAN),
            config=preflight.arguments.config,
            plan=plan,
            destructive_approval=controller.state.destructive_approval,
        )
    except InvalidRoundError:
        return None
    if (
        preview.signal.kind is not SignalKind.UNRESOLVED_ZERO
        or preview.escalations
    ):
        return None
    return replace(preview, test_gate_status=result.test_gate_status)


def _cross_confirm_finish_preview(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    artifact: object,
) -> StepExecutionResult | None:
    if result.signal.kind is not SignalKind.ARTIFACT_OK:
        return None
    if result.test_gate_status is not TestGateStatus.PASS:
        return None
    if not isinstance(artifact, ReviewArtifact):
        return None
    if (
        artifact.artifact_kind is not ArtifactKind.CROSS_REVIEW
        or artifact.role is not Role.CROSS_CONFIRMER
        or artifact.verdict is not CodeReviewVerdict.APPROVE
        or artifact.agrees_with_reviewer is not True
    ):
        return None
    if (
        artifact.reviewed_finding_ids
        or artifact.finding_decisions
        or artifact.findings
        or artifact.non_blocking_suggestions
        or artifact.escalation_signals
        or result.escalations
    ):
        return None
    plan = _load_plan(controller.workspace.root)
    if plan is None or not _plan_review_can_be_skipped(result, plan):
        return None
    preview = execute_evaluate(
        state=LoopState.CONSENSUS_EVALUATE,
        ledger=result.ledger,
        evidence=_round_evidence(controller, ConsensusKind.CODE),
        config=preflight.arguments.config,
        plan=plan,
        destructive_approval=controller.state.destructive_approval,
    )
    if (
        preview.signal.kind is not SignalKind.UNRESOLVED_ZERO
        or preview.escalations
    ):
        return None
    return replace(preview, test_gate_status=result.test_gate_status)


def _validate_worker_completion_master_decision(
    state: LoopState,
    decision,
    *,
    allow_direct_implementation: bool = False,
    allow_plan_review_implement: bool = False,
    allow_code_review_finish: bool = False,
    allow_cross_confirm_finish: bool = False,
) -> SignalKind | LoopState | None:
    decision = validate_master_decision(decision)
    if decision.action is MasterAction.ESCALATE:
        return SignalKind.ESCALATE
    if state in {LoopState.PLAN, LoopState.PLAN_REVISE}:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.PLAN_REVIEWER
        ):
            return None
        if (
            allow_direct_implementation
            and decision.action is MasterAction.DISPATCH
            and decision.role is Role.IMPLEMENTER
        ):
            return LoopState.IMPLEMENT
        if not allow_direct_implementation:
            raise OrcaLoopError(
                "Master decision after planning must dispatch plan_reviewer or escalate"
            )
        raise OrcaLoopError(
            "Master decision after planning must dispatch an allowed next worker "
            "or escalate"
        )
    if state is LoopState.PLAN_REVIEW:
        if (
            allow_plan_review_implement
            and decision.action is MasterAction.DISPATCH
            and decision.role is Role.IMPLEMENTER
        ):
            return LoopState.IMPLEMENT
        if allow_plan_review_implement:
            raise OrcaLoopError(
                "Master decision after clean plan review must dispatch implementer "
                "or escalate"
            )
        raise OrcaLoopError(
            "Master routing after plan review requires verified consensus preview"
        )
    if state in {LoopState.IMPLEMENT, LoopState.FIX}:
        if decision.action is MasterAction.TEST:
            return None
        raise OrcaLoopError(
            "Master decision after implementation must request test or escalate"
        )
    if state is LoopState.CODE_REVIEW:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.CROSS_CONFIRMER
        ):
            return None
        if allow_code_review_finish and decision.action is MasterAction.FINISH:
            return LoopState.HUMAN_GATE
        if not allow_code_review_finish:
            raise OrcaLoopError(
                "Master decision after code review must dispatch cross_confirmer or escalate"
            )
        raise OrcaLoopError(
            "Master decision after code review must choose an allowed "
            "confirmation/final action or escalate"
        )
    if state is LoopState.CROSS_CONFIRM:
        if allow_cross_confirm_finish and decision.action is MasterAction.FINISH:
            return LoopState.HUMAN_GATE
        if allow_cross_confirm_finish:
            raise OrcaLoopError(
                "Master decision after clean cross-confirm must finish through "
                "the human gate or escalate"
            )
        raise OrcaLoopError(
            "Master routing after cross-confirm requires verified consensus preview"
        )
    raise OrcaLoopError(
        f"worker-completion Master routing is unsupported from {state.value}"
    )


def _route_worker_completion(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
    result: StepExecutionResult,
    artifact: object | None,
    *,
    master_invoke: MasterInvoker | None = None,
) -> StepExecutionResult:
    plan_review_preview = (
        _plan_review_implement_preview(controller, preflight, result, artifact)
        if (
            preflight.master_runtime is not None
            and controller.state.state is LoopState.PLAN_REVIEW
            and artifact is not None
        )
        else None
    )
    cross_confirm_preview = (
        _cross_confirm_finish_preview(controller, preflight, result, artifact)
        if (
            preflight.master_runtime is not None
            and controller.state.state is LoopState.CROSS_CONFIRM
            and artifact is not None
        )
        else None
    )
    if (
        preflight.master_runtime is None
        or result.signal.kind is not SignalKind.ARTIFACT_OK
        or artifact is None
        or controller.state.state
        not in {
            LoopState.PLAN,
            LoopState.PLAN_REVISE,
            LoopState.PLAN_REVIEW,
            LoopState.IMPLEMENT,
            LoopState.FIX,
            LoopState.CODE_REVIEW,
            LoopState.CROSS_CONFIRM,
        }
        or (
            controller.state.state is LoopState.PLAN_REVIEW
            and plan_review_preview is None
        )
        or (
            controller.state.state is LoopState.CROSS_CONFIRM
            and cross_confirm_preview is None
        )
    ):
        return result
    invoke = master_invoke
    if invoke is None:
        invoke = lambda options, prompt: invoke_master_provider(
            options,
            prompt,
            timeout_ms=preflight.arguments.config.step_timeout_ms,
        )
    try:
        decision = invoke_master(
            preflight.master_runtime,
            preflight.arguments.harness_root / "prompts" / "master.md",
            _worker_completion_master_context(
                controller,
                preflight,
                role,
                result,
                artifact,
                allow_plan_review_implement=(plan_review_preview is not None),
                allow_cross_confirm_finish=(cross_confirm_preview is not None),
            ),
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise OrcaLoopError(
            f"worker-completion Master routing failed: {exc}"
        ) from exc
    override = _validate_worker_completion_master_decision(
        controller.state.state,
        decision,
        allow_direct_implementation=_plan_review_can_be_skipped(
            result,
            artifact,
        ),
        allow_plan_review_implement=(plan_review_preview is not None),
        allow_code_review_finish=_code_review_can_finish(
            controller,
            result,
            artifact,
        ),
        allow_cross_confirm_finish=(cross_confirm_preview is not None),
    )
    if override is None:
        return result
    if override is LoopState.IMPLEMENT:
        if controller.state.state is LoopState.PLAN_REVIEW:
            assert plan_review_preview is not None
            controller.commit(
                stage=StepStage.TRANSITION_COMMITTED,
                active=None,
                reason=(
                    "Master collapsed clean plan-review consensus evaluation "
                    "and dispatched implementation: "
                    f"{decision.reason}"
                ),
                signal=plan_review_preview.signal.kind,
                state_value=LoopState.IMPLEMENT,
                status=RunStatus.IN_PROGRESS,
                ledger=plan_review_preview.ledger,
                counters=controller.state.counters,
                test_gate_status=result.test_gate_status,
            )
            return result
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=(
                "Master skipped plan review for safe verified plan: "
                f"{decision.reason}"
            ),
            signal=result.signal.kind,
            state_value=LoopState.IMPLEMENT,
            status=RunStatus.IN_PROGRESS,
            ledger=result.ledger,
            test_gate_status=result.test_gate_status,
        )
        return result
    if override is LoopState.HUMAN_GATE:
        if controller.state.state is LoopState.CROSS_CONFIRM:
            assert cross_confirm_preview is not None
            controller.commit(
                stage=StepStage.TRANSITION_COMMITTED,
                active=None,
                reason=(
                    "Master collapsed clean cross-confirm consensus evaluation "
                    "and requested final human disposition: "
                    f"{decision.reason}"
                ),
                signal=cross_confirm_preview.signal.kind,
                state_value=LoopState.HUMAN_GATE,
                status=RunStatus.IN_PROGRESS,
                ledger=cross_confirm_preview.ledger,
                counters=controller.state.counters,
                test_gate_status=result.test_gate_status,
            )
            return result
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=(
                "Master skipped cross-confirm for clean low-risk code review "
                f"and requested final human disposition: {decision.reason}"
            ),
            signal=result.signal.kind,
            state_value=LoopState.HUMAN_GATE,
            status=RunStatus.IN_PROGRESS,
            ledger=result.ledger,
            counters=controller.state.counters,
            test_gate_status=result.test_gate_status,
        )
        return result
    routed_result = plan_review_preview or cross_confirm_preview or result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master escalated after {role.value}: {decision.reason}",
            routed_result.signal.finding_ids,
        ),
        routed_result.ledger,
        routed_result.test_gate_status,
        routed_result.escalations,
    )


def _test_result_master_context(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    plan: PlanDocument,
) -> dict[str, object]:
    if result.signal.kind in {SignalKind.PASS, SignalKind.NOT_RUN}:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.CODE_REVIEWER.value,
                "permissionProfile": "read_only",
            },
        ]
        if _test_result_can_finish(result, plan):
            allowed.append(
                {
                    "action": MasterAction.FINISH.value,
                    "role": None,
                    "permissionProfile": None,
                }
            )
        allowed.append(
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            }
        )
    elif result.signal.kind is SignalKind.FAIL:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.IMPLEMENTER.value,
                "permissionProfile": "workspace_write",
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ]
    else:
        allowed = []
    return {
        "stage": "test_result",
        "runId": controller.state.run_id,
        "currentState": controller.state.state.value,
        "request": preflight.arguments.config.request_path.read_text(
            encoding="utf-8"
        ),
        "plan": to_wire_value(plan),
        "testStatus": (
            None
            if result.test_gate_status is None
            else result.test_gate_status.value
        ),
        "resultSignal": result.signal.kind.value,
        "testFixAttempts": controller.state.counters.test_fix_attempts,
        "allowedDecisions": allowed,
    }


def _test_result_can_finish(
    result: StepExecutionResult,
    plan: PlanDocument,
) -> bool:
    return (
        result.signal.kind is SignalKind.PASS
        and result.test_gate_status is TestGateStatus.PASS
        and _plan_review_can_be_skipped(result, plan)
    )


def _validate_test_result_master_decision(
    signal: SignalKind,
    decision,
    *,
    allow_finish: bool = False,
) -> SignalKind | LoopState | None:
    decision = validate_master_decision(decision)
    if decision.action is MasterAction.ESCALATE:
        return SignalKind.ESCALATE
    if signal in {SignalKind.PASS, SignalKind.NOT_RUN}:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.CODE_REVIEWER
        ):
            return None
        if (
            allow_finish
            and signal is SignalKind.PASS
            and decision.action is MasterAction.FINISH
        ):
            return LoopState.HUMAN_GATE
        if not allow_finish:
            raise OrcaLoopError(
                "Master decision after successful test gate must dispatch "
                "code_reviewer or escalate"
            )
        raise OrcaLoopError(
            "Master decision after successful test gate must choose an allowed "
            "review/final action or escalate"
        )
    if signal is SignalKind.FAIL:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.IMPLEMENTER
        ):
            return None
        raise OrcaLoopError(
            "Master decision after failed test gate must dispatch "
            "implementer or escalate"
        )
    raise OrcaLoopError(
        f"test-result Master routing is unsupported for {signal.value}"
    )


def _route_test_result(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    plan: PlanDocument,
    *,
    master_invoke: MasterInvoker | None = None,
) -> StepExecutionResult:
    if (
        preflight.master_runtime is None
        or controller.state.state is not LoopState.TEST_GATE
        or result.signal.kind
        not in {SignalKind.PASS, SignalKind.NOT_RUN, SignalKind.FAIL}
    ):
        return result
    invoke = master_invoke
    if invoke is None:
        invoke = lambda options, prompt: invoke_master_provider(
            options,
            prompt,
            timeout_ms=preflight.arguments.config.step_timeout_ms,
        )
    try:
        decision = invoke_master(
            preflight.master_runtime,
            preflight.arguments.harness_root / "prompts" / "master.md",
            _test_result_master_context(
                controller,
                preflight,
                result,
                plan,
            ),
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise OrcaLoopError(
            f"test-result Master routing failed: {exc}"
        ) from exc
    override = _validate_test_result_master_decision(
        result.signal.kind,
        decision,
        allow_finish=_test_result_can_finish(result, plan),
    )
    if override is None:
        return result
    if override is LoopState.HUMAN_GATE:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=(
                "Master skipped code review for safe passing change and "
                f"requested final human disposition: {decision.reason}"
            ),
            signal=result.signal.kind,
            state_value=LoopState.HUMAN_GATE,
            status=RunStatus.IN_PROGRESS,
            ledger=result.ledger,
            counters=LoopCounters(
                0,
                controller.state.counters.operational_retries,
            ),
            test_gate_status=result.test_gate_status,
        )
        return result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master escalated after test gate: {decision.reason}",
            result.signal.finding_ids,
        ),
        result.ledger,
        result.test_gate_status,
        result.escalations,
    )


def _final_master_context(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    plan: PlanDocument | None,
) -> dict[str, object]:
    return {
        "stage": "final_decision",
        "runId": controller.state.run_id,
        "currentState": controller.state.state.value,
        "request": preflight.arguments.config.request_path.read_text(
            encoding="utf-8"
        ),
        "plan": None if plan is None else to_wire_value(plan),
        "ledger": to_wire_value(result.ledger),
        "testStatus": (
            None
            if controller.state.test_gate_status is None
            else controller.state.test_gate_status.value
        ),
        "resultSignal": result.signal.kind.value,
        "allowedDecisions": [
            {
                "action": MasterAction.FINISH.value,
                "role": None,
                "permissionProfile": None,
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ],
    }


def _validate_final_master_decision(decision) -> SignalKind | None:
    decision = validate_master_decision(decision)
    if decision.action is MasterAction.FINISH:
        return None
    if decision.action is MasterAction.ESCALATE:
        return SignalKind.ESCALATE
    raise OrcaLoopError(
        "Master final decision must finish through the human gate or escalate"
    )


def _route_final_decision(
    controller: GenerationController,
    preflight: PreflightResult,
    result: StepExecutionResult,
    plan: PlanDocument | None,
    *,
    master_invoke: MasterInvoker | None = None,
) -> StepExecutionResult:
    if (
        preflight.master_runtime is None
        or controller.state.state is not LoopState.CONSENSUS_EVALUATE
        or result.signal.kind is not SignalKind.UNRESOLVED_ZERO
    ):
        return result
    invoke = master_invoke
    if invoke is None:
        invoke = lambda options, prompt: invoke_master_provider(
            options,
            prompt,
            timeout_ms=preflight.arguments.config.step_timeout_ms,
        )
    try:
        decision = invoke_master(
            preflight.master_runtime,
            preflight.arguments.harness_root / "prompts" / "master.md",
            _final_master_context(
                controller,
                preflight,
                result,
                plan,
            ),
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise OrcaLoopError(
            f"final Master routing failed: {exc}"
        ) from exc
    override = _validate_final_master_decision(decision)
    if override is None:
        return result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master escalated before final human gate: {decision.reason}",
            result.signal.finding_ids,
        ),
        result.ledger,
        result.test_gate_status,
        result.escalations,
    )


def _execute_worker(
    controller: GenerationController,
    pool: WorkerPool,
    preflight: PreflightResult,
    client: OrcaClient,
    *,
    master_invoke: MasterInvoker | None = None,
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
    permission_profile = default_permission_profile(role)
    profile = build_launch_profile(
        role,
        permission_profile,
        profile_root,
        step.input_dir,
        step.output_dir,
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
    result = _route_worker_completion(
        controller,
        preflight,
        role,
        result,
        artifact,
        master_invoke=master_invoke,
    )
    if controller.state.state is state:
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
) -> str:
    response = client.call(
        (
            "orchestration",
            "task-create",
            "--task-title",
            f"{run_id} user decision",
            "--display-name",
            f"{run_id} user decision",
            "--spec",
            "Review the bound user-decision.md report.",
        ),
        timeout_ms=30_000,
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
    return task_id


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
    task_id = _create_decision_task(
        client,
        controller.state.run_id,
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
    binding = create_gate(
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
    )
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason="user decision gate created",
        status=RunStatus.BLOCKED,
        gate_binding=binding,
    )


def _resume_gate(
    controller: GenerationController,
    preflight: PreflightResult,
    client: OrcaClient,
) -> bool:
    binding = controller.state.gate_binding
    if binding is None:
        return False
    try:
        decision = wait_gate_resolution(
            client,
            binding=binding,
            timeout_ms=30_000,
        )
    except GateProtocolError as exc:
        if "exactly one resolved gate" in str(exc):
            return False
        raise
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
    return True


def _run_loop(
    controller: GenerationController,
    pool: WorkerPool,
    preflight: PreflightResult,
    client: OrcaClient,
    *,
    master_invoke: MasterInvoker | None = None,
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
                    master_invoke=master_invoke,
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
                plan = _load_plan(controller.workspace.root)
                result = execute_evaluate(
                    state=state,
                    ledger=controller.ledger,
                    evidence=_round_evidence(
                        controller,
                        ConsensusKind.CODE,
                    ),
                    config=config,
                    plan=plan,
                    destructive_approval=(
                        controller.state.destructive_approval
                    ),
                )
                result = _route_final_decision(
                    controller,
                    preflight,
                    result,
                    plan,
                    master_invoke=master_invoke,
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
                result = _route_test_result(
                    controller,
                    preflight,
                    result,
                    plan,
                    master_invoke=master_invoke,
                )
            else:
                raise OrcaLoopError(
                    f"unsupported coordinator state: {state.value}"
                )
            if controller.state.state is state:
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
        controller, pool = _resume(preflight)
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


def main(argv: Sequence[str] | None = None) -> int:
    harness_root = Path(__file__).resolve().parent
    lock = None
    try:
        arguments = parse_run_arguments(
            argv,
            harness_root=harness_root,
        )
        client = OrcaClient(cwd=harness_root)
        preflight = run_preflight(
            arguments,
            client,
            expected_orca_version=EXPECTED_ORCA_VERSION,
        )
        preflight = prepare_agent_runtime(preflight)
        preflight = prepare_master_runtime(preflight)
        if arguments.dry_run:
            print(
                json.dumps(
                    {
                        "status": "PASS",
                        "mode": "dry-run",
                        "run_id": arguments.run_id,
                        "orca_version": preflight.orca_version,
                        "plan_consensus_round_limit": 5,
                        "code_consensus_round_limit": 5,
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
    except (
        OrcaLoopError,
        OrcaCommandError,
        AtomicWriteError,
        GateProtocolError,
        RunLockError,
    ) as exc:
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
