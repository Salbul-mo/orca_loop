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
    prepare_agent_runtime,
    run_preflight,
)
from orca_loop.contracts import (
    ContractViolationError,
    parse_plan_document,
)
from orca_loop.coordinator import (
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
    role_for_state,
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
from orca_loop.ledger import empty_ledger, unresolved_scope
from orca_loop.locking import (
    RunLockError,
    acquire_run_lock,
    release_run_lock,
)
from orca_loop.machine import TERMINAL_STATES
from orca_loop.models import (
    ActiveStep,
    ArtifactKind,
    ConsensusKind,
    CoordinatorState,
    GateKind,
    HumanDecisionKind,
    LaunchProfile,
    LoopCounters,
    LoopState,
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
        permission_report_digest=(
            preflight.permission_report.report_digest
        ),
        history=(),
    )


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
    controller = GenerationController(workspace, state, ledger)
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
