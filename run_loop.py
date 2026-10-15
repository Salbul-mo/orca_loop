from __future__ import annotations

import hashlib
import json
import os
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
    parse_implementation_artifact,
    parse_plan_document,
    parse_review_artifact,
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
    write_atomic_bytes,
)
from orca_loop.ledger import empty_ledger, unresolved_scope
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
    ExpectedProvenance,
    GateKind,
    HumanDecisionKind,
    LaunchProfile,
    LoopCounters,
    LoopState,
    MasterAction,
    MasterDecision,
    PlanDocument,
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
from orca_loop.local_runtime import LocalCoordinatorClient
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


class RecoverableMasterProviderError(OrcaLoopError):
    """A provider outage that can be resumed without discarding verified work."""

    def __init__(
        self,
        phase: str,
        detail: str,
        *,
        resume_action: str,
    ) -> None:
        super().__init__(f"{phase} Master routing failed: {detail}")
        self.phase = phase
        self.resume_action = resume_action


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
        raise RecoverableMasterProviderError(
            "initial",
            str(exc),
            resume_action="retry_initial_master",
        ) from exc
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



def _master_guidance_path(controller: GenerationController) -> Path:
    return (
        controller.workspace.control_dir
        / f"master-guidance-g{controller.state.generation:04d}.json"
    )


def _persist_master_guidance(
    controller: GenerationController,
    *,
    source_state: LoopState,
    source_role: Role,
    target_state: LoopState,
    decision,
    escalations,
) -> None:
    target_role = role_for_state(target_state)
    payload = {
        "schemaVersion": 1,
        "runId": controller.state.run_id,
        "generation": controller.state.generation,
        "sourceState": source_state.value,
        "sourceRole": source_role.value,
        "targetState": target_state.value,
        "targetRole": target_role.value,
        "decision": to_wire_value(decision),
        "workerSupervisionRequests": [
            to_wire_value(item) for item in escalations
        ],
        "authority": (
            "Bounded Master guidance. It may resolve the listed worker "
            "supervision requests but cannot expand user scope, permissions, "
            "or coordinator policy."
        ),
    }
    output = _master_guidance_path(controller)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)


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
    guidance = _master_guidance_path(controller)
    if guidance.is_file():
        values.append(StagedInput("master-guidance.json", guidance, None))
    if (
        controller.state.history
        and controller.state.history[-1].signal
        is SignalKind.OPERATIONAL_RETRY
    ):
        retry = controller.state.history[-1]
        retry_payload = {
            "failureLayer": "artifact_contract",
            "previousValidationError": retry.reason,
            "requiredAction": (
                "Repair the artifact contract first. Return the exact JSON "
                "artifact only; do not repeat prose or the invalid shape."
            ),
            "operationalRetry": controller.state.counters.operational_retries,
        }
        values.append(
            StagedInput(
                "artifact-validation-error.json",
                None,
                (
                    json.dumps(
                        retry_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )
        )
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
        trusted_runs_root=preflight.arguments.harness_root / "runs",
    )


def _worker_completion_master_context(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
    result: StepExecutionResult,
    artifact: object,
) -> dict[str, object]:
    state = controller.state.state
    supervision_requested = bool(result.escalations)
    escalation_codes = {item.code.value for item in result.escalations}

    escalate = {
        "action": MasterAction.ESCALATE.value,
        "role": None,
        "permissionProfile": None,
    }
    if state in {LoopState.PLAN, LoopState.PLAN_REVISE}:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.PLAN_REVIEWER.value,
                "permissionProfile": "read_only",
            },
        ]
        if supervision_requested:
            allowed.insert(
                0,
                {
                    "action": MasterAction.DISPATCH.value,
                    "role": Role.PLANNER.value,
                    "permissionProfile": "read_only",
                },
            )
        elif _plan_review_can_be_skipped(result, artifact):
            allowed.append(
                {
                    "action": MasterAction.DISPATCH.value,
                    "role": Role.IMPLEMENTER.value,
                    "permissionProfile": "workspace_write",
                }
            )
        allowed.append(escalate)
    elif state is LoopState.PLAN_REVIEW and supervision_requested:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.PLANNER.value,
                "permissionProfile": "read_only",
            },
            escalate,
        ]
    elif state in {LoopState.IMPLEMENT, LoopState.FIX}:
        if supervision_requested:
            allowed = [
                {
                    "action": MasterAction.DISPATCH.value,
                    "role": Role.PLANNER.value,
                    "permissionProfile": "read_only",
                },
            ]
            if "E-08" not in escalation_codes:
                allowed.extend(
                    [
                        {
                            "action": MasterAction.DISPATCH.value,
                            "role": Role.IMPLEMENTER.value,
                            "permissionProfile": "workspace_write",
                        },
                        {
                            "action": MasterAction.TEST.value,
                            "role": None,
                            "permissionProfile": None,
                        },
                    ]
                )
            allowed.append(escalate)
        else:
            allowed = [
                {
                    "action": MasterAction.TEST.value,
                    "role": None,
                    "permissionProfile": None,
                },
                escalate,
            ]
    elif state is LoopState.CODE_REVIEW:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.CROSS_CONFIRMER.value,
                "permissionProfile": "read_only",
            },
        ]
        if (
            not supervision_requested
            and _code_review_can_finish(controller, result, artifact)
        ):
            allowed.append(
                {
                    "action": MasterAction.FINISH.value,
                    "role": None,
                    "permissionProfile": None,
                }
            )
        allowed.append(escalate)
    elif state is LoopState.CROSS_CONFIRM and supervision_requested:
        allowed = [
            {
                "action": MasterAction.DISPATCH.value,
                "role": Role.IMPLEMENTER.value,
                "permissionProfile": "workspace_write",
            },
            escalate,
        ]
    else:
        allowed = []
    return {
        "stage": (
            "worker_supervision"
            if supervision_requested
            else "worker_completion"
        ),
        "runId": controller.state.run_id,
        "currentState": state.value,
        "completedRole": role.value,
        "request": preflight.arguments.config.request_path.read_text(
            encoding="utf-8"
        ),
        "artifact": to_wire_value(artifact),
        "resultSignal": result.signal.kind.value,
        "supervisionRequested": supervision_requested,
        "workerSupervisionRequests": [
            to_wire_value(item) for item in result.escalations
        ],
        "supervisionRule": (
            "Resolve autonomously when evidence/policy is sufficient. "
            "Escalate only for genuinely human-owned decisions."
        ),
        "allowedDecisions": allowed,
    }


def _plan_review_can_be_skipped(
    result: StepExecutionResult,
    artifact: object,
) -> bool:
    if not isinstance(artifact, PlanDocument):
        return False
    if result.escalations or artifact.escalation_signals:
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


def _validate_worker_completion_master_decision(
    state: LoopState,
    decision,
    *,
    allow_direct_implementation: bool = False,
    allow_code_review_finish: bool = False,
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
            return LoopState.READY_FOR_MERGE
        if not allow_code_review_finish:
            raise OrcaLoopError(
                "Master decision after code review must dispatch cross_confirmer or escalate"
            )
        raise OrcaLoopError(
            "Master decision after code review must choose an allowed "
            "confirmation/final action or escalate"
        )
    raise OrcaLoopError(
        f"worker-completion Master routing is unsupported from {state.value}"
    )


def _validate_worker_supervision_master_decision(
    state: LoopState,
    decision,
    result: StepExecutionResult,
) -> SignalKind | LoopState:
    decision = validate_master_decision(decision)
    codes = {item.code.value for item in result.escalations}
    if decision.action is MasterAction.ESCALATE:
        return SignalKind.ESCALATE
    if state in {LoopState.PLAN, LoopState.PLAN_REVISE}:
        if decision.action is MasterAction.DISPATCH:
            if decision.role is Role.PLANNER:
                return LoopState.PLAN_REVISE
            if decision.role is Role.PLAN_REVIEWER:
                return LoopState.PLAN_REVIEW
    elif state is LoopState.PLAN_REVIEW:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.PLANNER
        ):
            return LoopState.PLAN_REVISE
    elif state in {LoopState.IMPLEMENT, LoopState.FIX}:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.PLANNER
        ):
            return LoopState.PLAN_REVISE
        if "E-08" not in codes:
            if (
                decision.action is MasterAction.DISPATCH
                and decision.role is Role.IMPLEMENTER
            ):
                return LoopState.FIX
            if decision.action is MasterAction.TEST:
                return LoopState.TEST_GATE
    elif state is LoopState.CODE_REVIEW:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.CROSS_CONFIRMER
        ):
            return LoopState.CROSS_CONFIRM
    elif state is LoopState.CROSS_CONFIRM:
        if (
            decision.action is MasterAction.DISPATCH
            and decision.role is Role.IMPLEMENTER
        ):
            return LoopState.FIX
    raise OrcaLoopError(
        "Master supervision decision is not authorized for "
        f"{state.value}: {decision.action.value}"
    )


def _commit_master_supervision_resolution(
    controller: GenerationController,
    *,
    source_state: LoopState,
    source_role: Role,
    target_state: LoopState,
    decision,
    result: StepExecutionResult,
) -> None:
    controller.commit(
        stage=StepStage.TRANSITION_COMMITTED,
        active=None,
        reason=(
            f"Master resolved worker supervision after {source_role.value}: "
            f"{decision.reason}"
        ),
        signal=SignalKind.OK,
        state_value=target_state,
        status=RunStatus.IN_PROGRESS,
        ledger=result.ledger,
        counters=controller.state.counters,
        test_gate_status=result.test_gate_status,
        pending_escalations=(),
    )
    if target_state in WORKER_STATES:
        _persist_master_guidance(
            controller,
            source_state=source_state,
            source_role=source_role,
            target_state=target_state,
            decision=decision,
            escalations=result.escalations,
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
    supervision_requested = bool(result.escalations)
    normal_master_state = controller.state.state in {
        LoopState.PLAN,
        LoopState.PLAN_REVISE,
        LoopState.IMPLEMENT,
        LoopState.FIX,
        LoopState.CODE_REVIEW,
    }
    supervision_master_state = controller.state.state in {
        LoopState.PLAN,
        LoopState.PLAN_REVIEW,
        LoopState.PLAN_REVISE,
        LoopState.IMPLEMENT,
        LoopState.FIX,
        LoopState.CODE_REVIEW,
        LoopState.CROSS_CONFIRM,
    }
    if (
        preflight.master_runtime is None
        or artifact is None
        or (
            supervision_requested
            and (
                result.signal.kind is not SignalKind.ESCALATE
                or not supervision_master_state
            )
        )
        or (
            not supervision_requested
            and (
                result.signal.kind is not SignalKind.ARTIFACT_OK
                or not normal_master_state
            )
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
            ),
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise RecoverableMasterProviderError(
            "worker-completion",
            str(exc),
            resume_action="retry_master_only",
        ) from exc

    if supervision_requested:
        source_state = controller.state.state
        override = _validate_worker_supervision_master_decision(
            source_state,
            decision,
            result,
        )
        if override is SignalKind.ESCALATE:
            return StepExecutionResult(
                TransitionSignal(
                    SignalKind.ESCALATE,
                    (
                        f"Master requires human supervision after {role.value}: "
                        f"{decision.reason}"
                    ),
                    result.signal.finding_ids,
                    requires_user_decision=True,
                ),
                result.ledger,
                result.test_gate_status,
                result.escalations,
            )
        _commit_master_supervision_resolution(
            controller,
            source_state=source_state,
            source_role=role,
            target_state=override,
            decision=decision,
            result=result,
        )
        return result

    override = _validate_worker_completion_master_decision(
        controller.state.state,
        decision,
        allow_direct_implementation=_plan_review_can_be_skipped(
            result,
            artifact,
        ),
        allow_code_review_finish=_code_review_can_finish(
            controller,
            result,
            artifact,
        ),
    )
    if override is None:
        return result
    if override is LoopState.IMPLEMENT:
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
    if override is LoopState.READY_FOR_MERGE:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=(
                "Master autonomously completed the clean low-risk code review: "
                f"{decision.reason}"
            ),
            signal=result.signal.kind,
            state_value=LoopState.READY_FOR_MERGE,
            status=RunStatus.READY,
            ledger=result.ledger,
            counters=controller.state.counters,
            test_gate_status=result.test_gate_status,
            pending_escalations=(),
            clear_blocked_context=True,
        )
        return result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master escalated after {role.value}: {decision.reason}",
            result.signal.finding_ids,
        ),
        result.ledger,
        result.test_gate_status,
        result.escalations,
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
            return LoopState.READY_FOR_MERGE
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
        raise RecoverableMasterProviderError(
            "test-result",
            str(exc),
            resume_action="resume_state",
        ) from exc
    override = _validate_test_result_master_decision(
        result.signal.kind,
        decision,
        allow_finish=_test_result_can_finish(result, plan),
    )
    if override is None:
        return result
    if override is LoopState.READY_FOR_MERGE:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=(
                "Master autonomously completed the safe verified change: "
                f"{decision.reason}"
            ),
            signal=result.signal.kind,
            state_value=LoopState.READY_FOR_MERGE,
            status=RunStatus.READY,
            ledger=result.ledger,
            counters=LoopCounters(
                0,
                controller.state.counters.operational_retries,
            ),
            test_gate_status=result.test_gate_status,
            pending_escalations=(),
            clear_blocked_context=True,
        )
        return result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master escalated after test gate: {decision.reason}",
            result.signal.finding_ids,
            requires_user_decision=True,
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


def _validate_final_master_decision(decision) -> SignalKind | LoopState:
    decision = validate_master_decision(decision)
    if decision.action is MasterAction.FINISH:
        return LoopState.READY_FOR_MERGE
    if decision.action is MasterAction.ESCALATE:
        return SignalKind.ESCALATE
    raise OrcaLoopError(
        "Master final decision must finish autonomously or escalate"
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
        raise RecoverableMasterProviderError(
            "final",
            str(exc),
            resume_action="resume_state",
        ) from exc
    override = _validate_final_master_decision(decision)
    if override is LoopState.READY_FOR_MERGE:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=f"Master autonomously completed the verified run: {decision.reason}",
            signal=result.signal.kind,
            state_value=LoopState.READY_FOR_MERGE,
            status=RunStatus.READY,
            ledger=result.ledger,
            counters=controller.state.counters,
            test_gate_status=controller.state.test_gate_status,
            pending_escalations=(),
            clear_blocked_context=True,
        )
        return result
    return StepExecutionResult(
        TransitionSignal(
            override,
            f"Master requires user authority before completion: {decision.reason}",
            result.signal.finding_ids,
            requires_user_decision=True,
        ),
        result.ledger,
        result.test_gate_status,
        result.escalations,
    )


def _load_verified_artifact(
    controller: GenerationController,
    role: Role,
) -> object:
    path = controller.workspace.artifact_dir / ARTIFACT_FILENAMES[role]
    if not path.is_file():
        raise OrcaLoopError(
            f"verified artifact is missing during resume: {path}"
        )
    raw_text = path.read_text(encoding="utf-8")
    if role is Role.PLANNER:
        return parse_plan_document(raw_text)

    active = controller.state.active
    if (
        active is None
        or active.task_id is None
        or active.dispatch_id is None
        or active.role is not role
    ):
        raise OrcaLoopError(
            "ARTIFACT_VERIFIED resume has no matching active dispatch provenance"
        )
    try:
        raw = json.loads(raw_text)
        consensus_round_value = int(raw["consensus_round"])
        snapshot_digest = str(raw["snapshot_digest"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ContractViolationError(
            "verified artifact cannot be reloaded for routing"
        ) from exc
    expected = ExpectedProvenance(
        run_id=controller.state.run_id,
        task_id=active.task_id,
        dispatch_id=active.dispatch_id,
        consensus_round=consensus_round_value,
        snapshot_digest=snapshot_digest,
    )
    if role is Role.IMPLEMENTER:
        return parse_implementation_artifact(
            raw_text,
            expected,
            delivered_finding_ids=_user_scope(controller).finding_ids,
        )
    kind_by_role = {
        Role.PLAN_REVIEWER: ArtifactKind.PLAN_REVIEW,
        Role.CODE_REVIEWER: ArtifactKind.CODE_REVIEW,
        Role.CROSS_CONFIRMER: ArtifactKind.CROSS_REVIEW,
    }
    kind = kind_by_role.get(role)
    if kind is None:
        raise OrcaLoopError(
            f"unsupported verified artifact role during resume: {role.value}"
        )
    reviewed = raw.get("reviewed_finding_ids", [])
    delivered = (
        tuple(str(item) for item in reviewed)
        if isinstance(reviewed, list)
        else _user_scope(controller).finding_ids
    )
    return parse_review_artifact(
        raw_text,
        kind,
        expected,
        delivered_finding_ids=delivered,
    )


def _resume_verified_worker_routing(
    controller: GenerationController,
    preflight: PreflightResult,
    role: Role,
    *,
    master_invoke: MasterInvoker | None,
) -> object:
    artifact = _load_verified_artifact(controller, role)
    escalations = controller.state.pending_escalations
    if not escalations:
        escalations = tuple(getattr(artifact, "escalation_signals", ()))
    result = StepExecutionResult(
        TransitionSignal(
            (
                SignalKind.ESCALATE
                if escalations
                else SignalKind.ARTIFACT_OK
            ),
            (
                "; ".join(item.reason for item in escalations)
                if escalations
                else "verified artifact routing resumed without worker re-dispatch"
            ),
            _user_scope(controller).finding_ids,
        ),
        controller.ledger,
        controller.state.test_gate_status,
        escalations,
    )
    source_state = controller.state.state
    result = _route_worker_completion(
        controller,
        preflight,
        role,
        result,
        artifact,
        master_invoke=master_invoke,
    )
    if controller.state.state is source_state:
        commit_step_transition(
            controller,
            result,
            preflight.arguments.config,
        )
    return artifact


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
    if controller.state.step_stage is StepStage.ARTIFACT_VERIFIED:
        return _resume_verified_worker_routing(
            controller,
            preflight,
            role,
            master_invoke=master_invoke,
        )
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
    kind: GateKind,
) -> tuple[str, ...]:
    if kind is GateKind.FINAL:
        return ("merge", "reject", "revise_code", "revise_design")
    if kind is GateKind.DESTRUCTIVE:
        return ("merge", "reject", "revise_design")
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


def _decision_support_text(value: object, *, limit: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _gate_decision_support(
    controller: GenerationController,
    kind: GateKind,
    options: tuple[str, ...],
    plan: PlanDocument | None,
) -> dict[str, object]:
    triggers = controller.state.pending_escalations
    primary = triggers[0] if triggers else None
    blocked = controller.state.blocked_from_state
    evidence: list[str] = []

    if primary is not None:
        evidence.extend(
            _decision_support_text(item, limit=360)
            for item in primary.evidence_refs
            if str(item).strip()
        )
    if plan is not None:
        evidence.extend(
            _decision_support_text(item, limit=360)
            for item in plan.current_state_evidence
            if str(item).strip()
        )

    destructive_items: list[str] = []
    if plan is not None:
        destructive_items = [
            f"{item.operation.value}: {item.path}"
            for item in plan.affected_files
            if item.operation.value in {"delete", "rename"}
        ]
        if kind is GateKind.DESTRUCTIVE:
            evidence.extend(destructive_items)

    deduped_evidence: list[str] = []
    for item in evidence:
        if item and item not in deduped_evidence:
            deduped_evidence.append(item)
        if len(deduped_evidence) >= 8:
            break

    if kind is GateKind.FINAL:
        problem = (
            "자동 검토 단계는 끝났지만, 최종 결과를 실제 결과물로 확정할지 "
            "사용자 승인이 남아 있습니다."
        )
        why = (
            "최종 승인 여부는 결과물 수용에 관한 제품/업무 결정이므로 "
            "ORCA가 대신 확정할 수 없습니다."
        )
    elif kind is GateKind.DESTRUCTIVE:
        targets = ", ".join(destructive_items[:6]) or "삭제/이름 변경 대상"
        problem = (
            "현재 계획에 되돌리기 어려운 파일 삭제 또는 이름 변경이 포함되어 "
            f"있습니다. 대상: {targets}"
        )
        why = (
            "파괴적 변경은 계획에 포함되어 있어도 실행 직전에 사용자가 범위를 "
            "확인하고 별도로 승인해야 합니다."
        )
    elif primary is not None:
        problem = _decision_support_text(primary.reason, limit=1000)
        why = (
            f"{primary.code.value} 판단은 자동 확정이 금지된 경계입니다. "
            "현재 실행은 사용자의 명시적 결정 전까지 중단됩니다."
        )
    else:
        history_reason = (
            controller.state.history[-1].reason
            if controller.state.history
            else "자동으로 확정할 수 없는 판단이 남아 있습니다."
        )
        problem = _decision_support_text(history_reason, limit=1000)
        why = (
            "현재 상태에서 ORCA가 임의로 선택하면 계획 또는 구현 범위를 "
            "넘을 수 있어 사람의 판단이 필요합니다."
        )

    option_details: list[dict[str, str]] = []
    for decision in options:
        if decision == "merge":
            if kind is GateKind.FINAL:
                title = "승인하고 완료"
                description = "현재 결과를 최종 승인합니다."
                consequence = "ORCA가 최종 승인 상태로 진행합니다."
            elif kind is GateKind.DESTRUCTIVE:
                title = "파괴적 변경 승인"
                description = "표시된 삭제/이름 변경을 허용합니다."
                consequence = "승인된 대상에 한해 구현 단계가 계속됩니다."
            else:
                title = "현재 방향 승인"
                description = "현재 판단 또는 계약 방향을 승인합니다."
                consequence = "중단 지점부터 현재 방향으로 작업을 계속합니다."
        elif decision == "reject":
            title = "승인하지 않음"
            description = "현재 변경 또는 실행 방향을 승인하지 않습니다."
            consequence = "현재 run을 종료하고 자동 진행을 중단합니다."
        elif decision == "revise_design":
            title = "설계 수정 요청"
            description = "구현 전에 계획·계약 해석·구조를 다시 조정합니다."
            consequence = "Planner 단계로 돌아가 새 설계를 검토합니다."
        elif decision == "revise_code":
            title = "코드 수정 요청"
            description = "설계는 유지하되 구현 결과를 수정합니다."
            consequence = "구현 수정 단계로 돌아간 뒤 다시 검토합니다."
        else:
            title = decision
            description = "이 선택지를 적용합니다."
            consequence = "ORCA가 해당 결정에 따라 다음 상태를 계산합니다."
        option_details.append(
            {
                "decision": decision,
                "title": title,
                "description": description,
                "consequence": consequence,
            }
        )

    recommended: str | None = None
    if (
        kind is GateKind.ESCALATION
        and primary is not None
        and primary.code.value == "E-03"
        and "revise_design" in options
    ):
        recommended = "revise_design"
        recommendation = (
            "외부 API·스키마·계약의 변경 권한이 불명확한 상태에서는 현재 방향을 "
            "바로 승인하지 말고 설계 수정 요청을 선택하는 것을 권장합니다. "
            "외부 기준을 유지하거나, 변경 권한을 명시적으로 확인한 뒤 다시 승인하는 "
            "편이 후속 재작업과 계약 불일치 위험이 가장 낮습니다."
        )
    elif kind is GateKind.FINAL:
        test_status = (
            None
            if controller.state.test_gate_status is None
            else controller.state.test_gate_status.value
        )
        if test_status == "PASS" and "merge" in options:
            recommended = "merge"
            recommendation = (
                "테스트가 통과했고 추가 escalation이 없으므로 현재 결과 승인에 "
                "무리가 없다는 판단입니다. 결과 범위가 요청과 일치하는지만 확인한 "
                "뒤 승인하는 것을 권장합니다."
            )
        else:
            recommended = "revise_code" if "revise_code" in options else None
            recommendation = (
                "최종 승인 전에 남은 검증 상태를 다시 확인하는 것을 권장합니다. "
                "구현상 문제가 있다면 코드 수정 요청을 선택하세요."
            )
    elif kind is GateKind.DESTRUCTIVE:
        recommendation = (
            "표시된 삭제/이름 변경 대상이 의도한 범위와 정확히 일치하는 경우에만 "
            "승인하는 것을 권장합니다. 하나라도 불명확하거나 불필요하면 설계 수정 "
            "요청으로 되돌리는 편이 안전합니다."
        )
    elif blocked is LoopState.PLAN_CONSENSUS_EVALUATE and "revise_design" in options:
        recommended = "revise_design"
        recommendation = (
            "계획 단계의 불확실성이므로 구현을 진행하기 전에 설계를 수정해 "
            "판단 근거를 명확히 하는 것을 권장합니다."
        )
    elif "revise_code" in options:
        recommended = "revise_code"
        recommendation = (
            "구현 또는 검증 단계의 문제이므로 현재 결과를 승인하기보다 코드 수정 후 "
            "재검토하는 것을 권장합니다."
        )
    else:
        recommendation = (
            "근거를 확인한 뒤 현재 범위를 명시적으로 수용할 수 있을 때만 승인하세요. "
            "확신이 없으면 승인하지 않고 보완 정보를 먼저 확보하는 편이 안전합니다."
        )

    approval_effect = next(
        (
            item["consequence"]
            for item in option_details
            if item["decision"] == "merge"
        ),
        "승인 선택지가 없는 gate입니다.",
    )

    return {
        "problem": problem,
        "whyDecisionNeeded": why,
        "evidence": deduped_evidence,
        "options": option_details,
        "recommendedDecision": recommended,
        "recommendation": recommendation,
        "approvalEffect": approval_effect,
        "noDecisionEffect": (
            "결정하지 않으면 현재 run은 USER_DECISION_REQUIRED 상태로 유지되며 "
            "ORCA는 해당 경계를 넘어 자동 진행하지 않습니다."
        ),
    }


def _gate_question(
    controller: GenerationController,
    kind: GateKind,
    options: tuple[str, ...],
) -> str:
    merge_meaning = "현재 방향을 승인하고 계속"
    if kind is GateKind.FINAL:
        merge_meaning = "최종 결과를 승인하고 종료 단계로 진행"
    elif kind is GateKind.DESTRUCTIVE:
        merge_meaning = "삭제/이름 변경 작업만 승인하고 계속"
    elif any(
        item.code.value == "E-03"
        for item in controller.state.pending_escalations
    ):
        merge_meaning = (
            "현재 방향을 승인하고 계속 "
            "(외부 계약/제품 권한 판단 승인 후 중단 지점부터 재개)"
        )
    option_meanings = {
        "merge": merge_meaning,
        "reject": "승인하지 않고 이 실행을 종료",
        "revise_code": "코드를 수정한 뒤 다시 검토",
        "revise_design": "설계를 수정한 뒤 다시 검토",
    }
    rendered_options = " / ".join(
        f"{item}={option_meanings.get(item, item)}" for item in options
    )

    if kind is GateKind.FINAL:
        return (
            "최종 결정 필요: 현재 결과를 최종 승인할지 선택하세요. "
            f"선택: {rendered_options}"
        )
    if kind is GateKind.DESTRUCTIVE:
        return (
            "결정 필요: 삭제 또는 이름 변경 같은 파괴적 변경을 승인할지 "
            f"선택하세요. 선택: {rendered_options}"
        )

    triggers = controller.state.pending_escalations
    if any(item.code.value == "E-03" for item in triggers):
        trigger = next(
            item for item in triggers if item.code.value == "E-03"
        )
        reason = " ".join(trigger.reason.split())
        if len(reason) > 320:
            reason = reason[:317].rstrip() + "..."
        return (
            "결정 필요(E-03): 외부 API/스키마/계약 변경을 승인할지 "
            f"선택하세요. 실제 판단 사유: {reason} "
            "이 승인은 파괴적 파일 변경 승인과 별개입니다. "
            f"선택: {rendered_options}"
        )

    if triggers:
        trigger = triggers[0]
        reason = " ".join(trigger.reason.split())
        if len(reason) > 220:
            reason = reason[:217].rstrip() + "..."
        return (
            f"결정 필요({trigger.code.value}): {reason} "
            f"선택: {rendered_options}"
        )

    return (
        "결정 필요: 자동으로 확정할 수 없는 사항이 있습니다. "
        f"선택: {rendered_options}"
    )


def _master_explicitly_requested_user_authority(
    controller: GenerationController,
) -> bool:
    if not controller.state.history:
        return False
    latest = controller.state.history[-1]
    return (
        latest.signal is SignalKind.ESCALATE
        and latest.requires_user_decision
    )


def _destructive_user_authority_required(
    controller: GenerationController,
) -> bool:
    if controller.state.blocked_from_state is not LoopState.PLAN_CONSENSUS_EVALUATE:
        return False
    if controller.state.pending_escalations:
        return False
    if controller.state.destructive_approval is not None:
        return False
    plan = _load_plan(controller.workspace.root)
    return plan is not None and any(
        item.operation.value in {"delete", "rename"}
        for item in plan.affected_files
    )


def _blocked_master_allowed_decisions(
    controller: GenerationController,
    preflight: PreflightResult,
) -> list[dict[str, object]]:
    blocked = controller.state.blocked_from_state
    latest_signal = (
        None
        if not controller.state.history
        else controller.state.history[-1].signal
    )
    view = ledger_view(controller.ledger)
    config = preflight.arguments.config

    dispatches: list[tuple[Role, str]] = []
    round_limit_exhausted = (
        latest_signal is SignalKind.UNRESOLVED_REMAIN
        and (
            (
                blocked is LoopState.PLAN_CONSENSUS_EVALUATE
                and view.plan_round >= config.plan_consensus_round_limit
            )
            or (
                blocked is LoopState.CONSENSUS_EVALUATE
                and view.code_round >= config.code_consensus_round_limit
            )
        )
    )

    if not round_limit_exhausted:
        if blocked in {LoopState.PLAN, LoopState.PLAN_REVISE}:
            dispatches = [(Role.PLANNER, "read_only")]
        elif blocked is LoopState.PLAN_REVIEW:
            dispatches = [
                (Role.PLANNER, "read_only"),
                (Role.PLAN_REVIEWER, "read_only"),
            ]
        elif blocked is LoopState.PLAN_CONSENSUS_EVALUATE:
            dispatches = [(Role.PLANNER, "read_only")]
        elif blocked in {LoopState.IMPLEMENT, LoopState.FIX}:
            dispatches = [
                (Role.PLANNER, "read_only"),
                (Role.IMPLEMENTER, "workspace_write"),
            ]
        elif blocked is LoopState.TEST_GATE:
            dispatches = [
                (Role.PLANNER, "read_only"),
                (Role.IMPLEMENTER, "workspace_write"),
            ]
        elif blocked is LoopState.CODE_REVIEW:
            dispatches = [
                (Role.IMPLEMENTER, "workspace_write"),
                (Role.CODE_REVIEWER, "read_only"),
            ]
        elif blocked is LoopState.CROSS_CONFIRM:
            dispatches = [
                (Role.IMPLEMENTER, "workspace_write"),
                (Role.CROSS_CONFIRMER, "read_only"),
            ]
        elif blocked is LoopState.CONSENSUS_EVALUATE:
            dispatches = [(Role.IMPLEMENTER, "workspace_write")]

    allowed = [
        {
            "action": MasterAction.DISPATCH.value,
            "role": role.value,
            "permissionProfile": permission,
        }
        for role, permission in dispatches
    ]
    allowed.extend(
        [
            {
                "action": MasterAction.ABORT.value,
                "role": None,
                "permissionProfile": None,
            },
            {
                "action": MasterAction.ESCALATE.value,
                "role": None,
                "permissionProfile": None,
            },
        ]
    )
    return allowed


def _blocked_master_context(
    controller: GenerationController,
    preflight: PreflightResult,
) -> dict[str, object]:
    latest = None if not controller.state.history else controller.state.history[-1]
    view = ledger_view(controller.ledger)
    return {
        "stage": "blocked_before_user_gate",
        "runId": controller.state.run_id,
        "currentState": controller.state.state.value,
        "blockedFromState": (
            None
            if controller.state.blocked_from_state is None
            else controller.state.blocked_from_state.value
        ),
        "request": preflight.arguments.config.request_path.read_text(encoding="utf-8"),
        "blockingSignal": None if latest is None else latest.signal.value,
        "blockingReason": None if latest is None else latest.reason,
        "workerSupervisionRequests": [
            to_wire_value(item) for item in controller.state.pending_escalations
        ],
        "counters": to_wire_value(controller.state.counters),
        "consensus": {
            "planRound": view.plan_round,
            "planRoundLimit": preflight.arguments.config.plan_consensus_round_limit,
            "codeRound": view.code_round,
            "codeRoundLimit": preflight.arguments.config.code_consensus_round_limit,
            "unresolvedCount": view.unresolved_count,
            "testFixAttemptLimit": preflight.arguments.config.test_fix_attempt_limit,
            "operationalRetryLimit": preflight.arguments.config.operational_retry_limit,
        },
        "authorityRule": (
            "Resolve technical/operational/rework decisions autonomously. "
            "Escalate only when the remaining decision is genuinely user-owned. "
            "A recommendation is advisory and never counts as user approval. "
            "If no safe authorized technical recovery remains, abort rather than "
            "asking the user to approve an unsafe shortcut."
        ),
        "allowedDecisions": _blocked_master_allowed_decisions(controller, preflight),
    }


def _decision_key(decision: MasterDecision) -> tuple[str, str | None, str | None]:
    return (
        decision.action.value,
        None if decision.role is None else decision.role.value,
        (
            None
            if decision.permission_profile is None
            else decision.permission_profile.value
        ),
    )


def _allowed_decision_keys(
    allowed: Sequence[dict[str, object]],
) -> set[tuple[str, str | None, str | None]]:
    return {
        (
            str(item["action"]),
            None if item.get("role") is None else str(item["role"]),
            (
                None
                if item.get("permissionProfile") is None
                else str(item["permissionProfile"])
            ),
        )
        for item in allowed
    }


def _persist_blocked_master_guidance(
    controller: GenerationController,
    *,
    source_state: LoopState | None,
    target_state: LoopState,
    decision: MasterDecision,
) -> None:
    payload = {
        "schemaVersion": 1,
        "runId": controller.state.run_id,
        "generation": controller.state.generation,
        "sourceState": None if source_state is None else source_state.value,
        "sourceRole": None,
        "targetState": target_state.value,
        "targetRole": role_for_state(target_state).value,
        "decision": to_wire_value(decision),
        "workerSupervisionRequests": [],
        "authority": (
            "Master recovery guidance after a technical block. It cannot expand "
            "user scope, permissions, or coordinator policy."
        ),
    }
    output = _master_guidance_path(controller)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\
",
        encoding="utf-8",
    )
    temporary.replace(output)


def _route_blocked_before_user_gate(
    controller: GenerationController,
    preflight: PreflightResult,
    *,
    master_invoke: MasterInvoker | None = None,
) -> bool:
    if controller.state.state is not LoopState.USER_DECISION_REQUIRED:
        return False
    if preflight.master_runtime is None:
        return False
    if _destructive_user_authority_required(controller):
        return False
    if _master_explicitly_requested_user_authority(controller):
        return False

    context = _blocked_master_context(controller, preflight)
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
            context,
            invoke,
        )
    except MasterRuntimeAdapterError as exc:
        raise RecoverableMasterProviderError(
            "blocked-recovery",
            str(exc),
            resume_action="retry_master_before_user_gate",
        ) from exc

    decision = validate_master_decision(decision)
    allowed = context["allowedDecisions"]
    assert isinstance(allowed, list)
    if _decision_key(decision) not in _allowed_decision_keys(allowed):
        raise OrcaLoopError(
            "Master blocked-recovery decision is not allowed by Coordinator policy"
        )

    blocked = controller.state.blocked_from_state
    if decision.action is MasterAction.ESCALATE:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=f"Master requires user authority before user gate: {decision.reason}",
            signal=SignalKind.ESCALATE,
            requires_user_decision=True,
            state_value=LoopState.USER_DECISION_REQUIRED,
            status=RunStatus.BLOCKED,
        )
        return False
    if decision.action is MasterAction.ABORT:
        controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason=f"Master aborted exhausted technical recovery: {decision.reason}",
            signal=SignalKind.ABORT,
            state_value=LoopState.FAILED,
            status=RunStatus.FAILED,
            clear_blocked_context=True,
        )
        return True
    if decision.action is not MasterAction.DISPATCH or decision.role is None:
        raise OrcaLoopError("blocked-recovery Master decision must dispatch, abort, or escalate")

    target_by_role = {
        Role.PLANNER: LoopState.PLAN_REVISE,
        Role.PLAN_REVIEWER: LoopState.PLAN_REVIEW,
        Role.IMPLEMENTER: LoopState.FIX,
        Role.CODE_REVIEWER: LoopState.CODE_REVIEW,
        Role.CROSS_CONFIRMER: LoopState.CROSS_CONFIRM,
    }
    target = target_by_role.get(decision.role)
    if target is None:
        raise OrcaLoopError("blocked-recovery Master dispatch role is unsupported")
    controller.commit(
        stage=StepStage.STEP_PENDING,
        active=None,
        reason=f"Master recovered technical block autonomously: {decision.reason}",
        signal=SignalKind.OK,
        state_value=target,
        status=RunStatus.IN_PROGRESS,
        clear_gate=True,
        clear_blocked_context=True,
    )
    _persist_blocked_master_guidance(
        controller,
        source_state=blocked,
        target_state=target,
        decision=decision,
    )
    return True


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
        and controller.state.blocked_from_state
        is LoopState.PLAN_CONSENSUS_EVALUATE
        and not controller.state.pending_escalations
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
            GateKind.ESCALATION
            if controller.state.pending_escalations
            else (
                GateKind.DESTRUCTIVE
                if destructive_pending
                else GateKind.ESCALATION
            )
        )
    )
    options = _gate_options(
        controller,
        plan,
        kind,
    )
    binding = create_gate(
        client,
        task_id=task_id,
        report=report,
        gate_kind=kind,
        question=_gate_question(
            controller,
            kind,
            options,
        ),
        options=options,
        decision_support=_gate_decision_support(
            controller,
            kind,
            options,
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


def _resume_target_after_approval(
    blocked: LoopState | None,
) -> LoopState:
    mapping = {
        LoopState.PLAN: LoopState.PLAN_REVIEW,
        LoopState.PLAN_REVISE: LoopState.PLAN_REVIEW,
        LoopState.PLAN_REVIEW: LoopState.PLAN_CONSENSUS_EVALUATE,
        LoopState.PLAN_CONSENSUS_EVALUATE: LoopState.IMPLEMENT,
        LoopState.IMPLEMENT: LoopState.TEST_GATE,
        LoopState.FIX: LoopState.TEST_GATE,
        LoopState.CODE_REVIEW: LoopState.CROSS_CONFIRM,
        LoopState.CROSS_CONFIRM: LoopState.CONSENSUS_EVALUATE,
        # After a user-owned escalation is approved at final consensus, return
        # to final evaluation so the Master can finish autonomously. Do not
        # manufacture a second routine final-approval gate.
        LoopState.CONSENSUS_EVALUATE: LoopState.CONSENSUS_EVALUATE,
    }
    target = mapping.get(blocked)
    if target is None:
        raise OrcaLoopError(
            "approved gate has no deterministic continuation for "
            f"{None if blocked is None else blocked.value}"
        )
    return target


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
    ledger = controller.ledger
    destructive_approval = controller.state.destructive_approval

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
        and binding.gate_kind is GateKind.DESTRUCTIVE
    ):
        plan = _load_plan(controller.workspace.root)
        if plan is None:
            raise OrcaLoopError(
                "destructive approval gate has no promoted plan"
            )
        snapshot = capture_snapshot(
            preflight.arguments.config.worktree_path
        )
        destructive_approval, signal = destructive_gate(
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
            raise OrcaLoopError(
                "destructive approval did not produce an OK signal"
            )
        target = _resume_target_after_approval(blocked)
        status = RunStatus.IN_PROGRESS
    elif (
        decision.decision is HumanDecisionKind.MERGE
        and binding.gate_kind is GateKind.ESCALATION
        and controller.state.pending_escalations
    ):
        ledger = approve_escalation_keys(
            controller.ledger,
            controller.state.pending_escalations,
        )
        plan = _load_plan(controller.workspace.root)
        destructive_pending = (
            blocked is LoopState.PLAN_CONSENSUS_EVALUATE
            and plan is not None
            and controller.state.destructive_approval is None
            and any(
                item.operation.value in {"delete", "rename"}
                for item in plan.affected_files
            )
        )
        if destructive_pending:
            controller.commit(
                stage=StepStage.TRANSITION_COMMITTED,
                active=None,
                reason=(
                    "external contract/product decision approved; "
                    "destructive implementation approval remains"
                ),
                signal=SignalKind.OK,
                state_value=LoopState.USER_DECISION_REQUIRED,
                status=RunStatus.BLOCKED,
                ledger=ledger,
                human_decision=decision,
                blocked_from_state=blocked,
                pending_escalations=(),
                clear_gate=True,
            )
            return True
        target = _resume_target_after_approval(blocked)
        status = RunStatus.IN_PROGRESS
    else:
        raise OrcaLoopError(
            "gate resolution is not valid for the gate kind and blocked state"
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
        ledger=ledger,
        human_decision=decision,
        destructive_approval=destructive_approval,
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
            if (
                state is LoopState.USER_DECISION_REQUIRED
                and _route_blocked_before_user_gate(
                    controller,
                    preflight,
                    master_invoke=master_invoke,
                )
            ):
                transitions += 1
                continue
            _ensure_gate(controller, preflight, client)
            return controller.state
        if state in TERMINAL_STATES:
            return controller.state
        try:
            if state is LoopState.INIT:
                _commit_initial_route(
                    controller,
                    preflight,
                    master_invoke=master_invoke,
                )
                transitions += 1
                continue
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


def _record_manual_master_override(
    controller: GenerationController,
    decision: MasterDecision,
) -> None:
    target = (
        controller.workspace.control_dir
        / f"manual-master-override.{controller.state.generation}.json"
    )
    payload = {
        "schemaVersion": 1,
        "runId": controller.state.run_id,
        "generation": controller.state.generation,
        "state": controller.state.state.value,
        "stepStage": controller.state.step_stage.value,
        "source": "cli_manual_master_override",
        "decision": to_wire_value(decision),
    }
    raw = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if target.exists():
        if not target.is_file() or target.is_symlink():
            raise OrcaLoopError(
                f"manual Master provenance target is invalid: {target}"
            )
        if target.read_bytes() != raw:
            raise OrcaLoopError(
                "manual Master provenance already exists with different content"
            )
        return
    write_atomic_bytes(target, raw)


def _manual_master_invoker(
    decision: MasterDecision,
    *,
    timeout_ms: int,
) -> MasterInvoker:
    raw = json.dumps(
        to_wire_value(decision),
        ensure_ascii=False,
        sort_keys=True,
    )
    used = False

    def invoke(options, prompt: str) -> str:
        nonlocal used
        if not used:
            used = True
            return raw
        return invoke_master_provider(
            options,
            prompt,
            timeout_ms=timeout_ms,
        )

    return invoke


def run_coordinator(
    preflight: PreflightResult,
    client: OrcaClient,
) -> CoordinatorState:
    if preflight.arguments.resume:
        controller, pool = _resume(preflight)
    else:
        controller, pool = _initialize(preflight, client)
    manual = preflight.arguments.manual_master_decision
    master_invoke = None
    if manual is not None:
        manual_checkpoint_ok = (
            controller.state.step_stage is StepStage.ARTIFACT_VERIFIED
            or controller.state.state is LoopState.INIT
        )
        if not manual_checkpoint_ok:
            raise OrcaLoopError(
                "manual Master override requires an INIT or "
                "ARTIFACT_VERIFIED resume checkpoint"
            )
        validate_master_decision(manual)
        _record_manual_master_override(controller, manual)
        master_invoke = _manual_master_invoker(
            manual,
            timeout_ms=preflight.arguments.config.step_timeout_ms,
        )
    return _run_loop(
        controller,
        pool,
        preflight,
        client,
        master_invoke=master_invoke,
    )


def exit_code(state: CoordinatorState) -> int:
    if state.state in {
        LoopState.READY_FOR_MERGE,
        LoopState.FINISHED_PLAN,
    }:
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
        if os.environ.get("ORCA_LOOP_RUNTIME") == "local":
            client = LocalCoordinatorClient(
                cwd=harness_root,
                run_id=arguments.run_id,
                coordinator_handle=arguments.config.coordinator_handle,
            )
        else:
            try:
                client = OrcaClient(cwd=harness_root)
            except OrcaCommandError as exc:
                raise PreflightError(str(exc)) from exc
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
    except RecoverableMasterProviderError as exc:
        print(
            json.dumps(
                {
                    "status": "BLOCKED_PROVIDER",
                    "failureLayer": "master_provider",
                    "recoverable": True,
                    "resumeAction": exc.resume_action,
                    "phase": exc.phase,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_USER_REQUIRED
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
