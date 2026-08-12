from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from orca_loop.generation import (
    AtomicWriteError,
    GenerationMismatchError,
    commit_generation,
    load_committed,
)
from orca_loop.config import validate_loop_config
from orca_loop.coordinator import (
    CoordinatorGuardError,
    CoordinatorPermissionError,
    apply_worker_artifact,
    execute_evaluate,
    operational_retry_result,
    reconcile_resume,
)
from orca_loop.contracts import ContractViolationError
from orca_loop.ledger import empty_ledger
from orca_loop.models import (
    AcceptanceCriterion,
    ActiveStep,
    AffectedFile,
    AffectedFileOperation,
    ArtifactKind,
    ConsensusKind,
    CoordinatorState,
    ExpectedProvenance,
    HumanDecision,
    HumanDecisionKind,
    LoopConfig,
    LoopCounters,
    LoopState,
    PlanDocument,
    ResumeAction,
    Role,
    RoundEvidence,
    RunStatus,
    ScopePackage,
    SignalKind,
    StepStage,
    TestContract,
    Violation,
    WorkerHandle,
    WorkerKey,
)
from tests.test_ledger import DIGEST_B, review, finding, decision
from orca_loop.ledger import apply_review_artifact, commit_round
from orca_loop.models import DecisionValue, Side
from run_loop import _user_scope


DIGEST_A = "sha256:" + "a" * 64


def initial_state() -> CoordinatorState:
    return CoordinatorState(
        schema_version=1,
        generation=0,
        run_id="run-1",
        state=LoopState.INIT,
        step_stage=StepStage.STEP_PENDING,
        status=RunStatus.IN_PROGRESS,
        worktree_selector="path:C:/repo",
        coordinator_handle="term-coordinator",
        worker_handles=(),
        active=None,
        plan_version=0,
        counters=LoopCounters(0, 0),
        base_head="a" * 40,
        snapshot_digest=DIGEST_A,
        test_gate_status=None,
        test_policy_digest=None,
        permission_report_digest=DIGEST_A,
        history=(),
    )


class GenerationStoreTest(unittest.TestCase):
    def test_typed_permission_errors_keep_worker_attribution(self) -> None:
        active = ActiveStep(
            step_id="step-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            role=Role.PLANNER,
            worker=WorkerHandle(
                WorkerKey.CLAUDE_PLANNER,
                "term-worker",
                "worktree",
                "tab",
                "leaf",
            ),
        )
        violation = Violation(
            "readonly_source_delta",
            "README.md",
            "planner changed repository content",
        )
        guard_error = CoordinatorGuardError((violation,), active, DIGEST_A)
        permission_error = CoordinatorPermissionError(
            "OUTBOX_WRITE_DENIED",
            active,
            ("C:/logs/stderr.log",),
            DIGEST_A,
        )
        self.assertEqual((violation,), guard_error.violations)
        self.assertIs(active, guard_error.active)
        self.assertEqual("OUTBOX_WRITE_DENIED", permission_error.reason_code)
        self.assertEqual(WorkerKey.CLAUDE_PLANNER, permission_error.active.worker.worker_key)
    def test_commit_is_monotonic_and_roundtrips_typed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary).resolve()
            state = initial_state()
            ledger = empty_ledger("run-1")
            manifest = commit_generation(control, state, ledger)
            loaded_state, loaded_ledger, loaded_manifest = load_committed(
                control
            )
            self.assertEqual(state, loaded_state)
            self.assertEqual(ledger, loaded_ledger)
            self.assertEqual(manifest, loaded_manifest)
            with self.assertRaises(GenerationMismatchError):
                commit_generation(
                    control,
                    replace(state, generation=2),
                    replace(ledger, generation=2),
                )

    def test_crash_before_manifest_keeps_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary).resolve()
            state = initial_state()
            ledger = empty_ledger("run-1")
            commit_generation(control, state, ledger)

            def crash() -> None:
                raise RuntimeError("simulated crash")

            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                commit_generation(
                    control,
                    replace(state, generation=1),
                    replace(ledger, generation=1),
                    before_manifest=crash,
                )
            loaded_state, loaded_ledger, _ = load_committed(control)
            self.assertEqual(0, loaded_state.generation)
            self.assertEqual(0, loaded_ledger.generation)

    def test_digest_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            control = Path(temporary).resolve()
            state = initial_state()
            commit_generation(
                control,
                state,
                empty_ledger("run-1"),
            )
            (control / "state.0.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                AtomicWriteError,
                "state digest mismatch",
            ):
                load_committed(control)


def plan() -> PlanDocument:
    return PlanDocument(
        schema_version=1,
        plan_version=1,
        request_digest=DIGEST_A,
        source_instruction="Implement the request.",
        interpretation="Bounded implementation.",
        rationale="Required by the request.",
        current_state_evidence=("README.md",),
        affected_files=(
            AffectedFile(
                "src/example.py",
                AffectedFileOperation.MODIFY,
                None,
            ),
        ),
        implementation_steps=("Implement the bounded change.",),
        data_api_schema_changes="없음",
        error_handling=("Return a typed error.",),
        test_contract=TestContract((), ()),
        test_policy_digest=DIGEST_A,
        acceptance_criteria=(
            AcceptanceCriterion("AC-1", "Run T-1."),
        ),
        risks=("Regression risk.",),
        out_of_scope=("Unrelated refactor.",),
        reviewed_finding_ids=(),
        finding_decisions=(),
    )


def config(root: Path) -> LoopConfig:
    request = root / "request.md"
    request.write_text("request", encoding="utf-8")
    return validate_loop_config(
        LoopConfig(
            worktree_path=root,
            request_path=request,
            coordinator_handle="term-coordinator",
            test_policy_path=None,
            plan_consensus_round_limit=5,
            code_consensus_round_limit=5,
            test_fix_attempt_limit=3,
            operational_retry_limit=2,
            max_transition_count=128,
            step_timeout_ms=1000,
            total_timeout_ms=2000,
        )
    )


class CoordinatorCoreTest(unittest.TestCase):
    def test_merge_decision_delivers_full_approved_plan_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            artifact_dir = root / "artifacts"
            artifact_dir.mkdir()
            from orca_loop.contracts import serialize_json

            value = plan()
            (artifact_dir / "plan.json").write_text(
                serialize_json(value),
                encoding="utf-8",
            )
            controller = SimpleNamespace(
                ledger=empty_ledger("run-1"),
                workspace=SimpleNamespace(root=root),
                state=SimpleNamespace(
                    human_decision=HumanDecision(
                        HumanDecisionKind.MERGE,
                        "Approved bounded plan.",
                        (),
                        (),
                        DIGEST_A,
                    ),
                    counters=LoopCounters(0, 0),
                    history=(),
                ),
            )

            scope = _user_scope(controller)

            self.assertEqual(("AC-1",), scope.acceptance_criteria_ids)
            self.assertEqual(("src/example.py",), scope.affected_files)

    def test_plan_artifact_records_explicit_claude_decision(self) -> None:
        base = apply_review_artifact(
            empty_ledger("run-1"),
            review(
                side=Side.CODEX,
                findings=(finding(),),
                decisions=(
                    decision(
                        "F-1",
                        Side.CODEX,
                        DecisionValue.APPROVE,
                        1,
                    ),
                ),
            ),
            Side.CODEX,
        ).ledger
        value = plan()
        value = replace(
            value,
            reviewed_finding_ids=("F-1",),
            finding_decisions=(
                decision(
                    "F-1",
                    Side.CLAUDE,
                    DecisionValue.APPROVE,
                    1,
                ),
            ),
        )
        from orca_loop.contracts import serialize_json

        _, update = apply_worker_artifact(
            LoopState.PLAN_REVISE,
            base,
            serialize_json(value),
            expected=ExpectedProvenance(
                "run-1",
                "task-1",
                "dispatch-1",
                1,
                DIGEST_A,
            ),
            delivered_finding_ids=("F-1",),
        )
        self.assertEqual("RESOLVED", update.ledger.findings[0].status.value)

    def test_e05_escalates_before_five_round_limit(self) -> None:
        ledger = apply_review_artifact(
            empty_ledger("run-1"),
            review(
                side=Side.CODEX,
                findings=(finding(),),
                decisions=(
                    decision(
                        "F-1",
                        Side.CODEX,
                        DecisionValue.CHANGE_REQUIRED,
                        1,
                    ),
                ),
            ),
            Side.CODEX,
        ).ledger
        first = commit_round(
            ledger,
            RoundEvidence(
                ConsensusKind.PLAN,
                1,
                1,
                None,
                (DIGEST_A, DIGEST_B),
                False,
                True,
            ),
            plan_limit=5,
            code_limit=5,
            expected_plan_version=1,
        ).ledger
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            result = execute_evaluate(
                state=LoopState.PLAN_CONSENSUS_EVALUATE,
                ledger=first,
                evidence=RoundEvidence(
                    ConsensusKind.PLAN,
                    2,
                    1,
                    None,
                    (DIGEST_A, DIGEST_B),
                    False,
                    True,
                ),
                config=config(root),
                plan=plan(),
                destructive_approval=None,
            )
        self.assertEqual(SignalKind.ESCALATE, result.signal.kind)
        self.assertIn("same unresolved signature", result.signal.reason)

    def test_operational_retry_is_round_neutral(self) -> None:
        ledger = empty_ledger("run-1")
        result = operational_retry_result(
            ledger=ledger,
            counters=LoopCounters(0, 0),
            limit=2,
            error=ContractViolationError("malformed artifact"),
            finding_ids=(),
        )
        self.assertEqual(SignalKind.OPERATIONAL_RETRY, result.signal.kind)
        self.assertEqual(0, result.ledger.plan_round)
        final = operational_retry_result(
            ledger=ledger,
            counters=LoopCounters(0, 1),
            limit=2,
            error=ContractViolationError(
                "missing_finding_decision"
            ),
            finding_ids=("F-1",),
        )
        self.assertEqual(SignalKind.ESCALATE, final.signal.kind)

    def test_resume_maps_each_durable_stage_without_duplicate_dispatch(
        self,
    ) -> None:
        state_value = initial_state()
        ledger = empty_ledger("run-1")
        active = ActiveStep(
            "step-1",
            "task-1",
            "dispatch-1",
            Role.PLAN_REVIEWER,
            None,
        )
        cases = (
            (
                replace(
                    state_value,
                    step_stage=StepStage.STEP_PREPARED,
                ),
                False,
                False,
                False,
                ResumeAction.CREATE_TASK,
            ),
            (
                replace(
                    state_value,
                    step_stage=StepStage.TASK_CREATED,
                    active=replace(active, dispatch_id=None),
                ),
                True,
                False,
                False,
                ResumeAction.DISPATCH_TASK,
            ),
            (
                replace(
                    state_value,
                    step_stage=StepStage.STEP_DISPATCHED,
                    active=active,
                ),
                True,
                True,
                False,
                ResumeAction.WAIT_DISPATCH,
            ),
            (
                replace(
                    state_value,
                    step_stage=StepStage.WORKER_DONE_RECEIVED,
                    active=active,
                ),
                True,
                True,
                True,
                ResumeAction.PROMOTE_ARTIFACT,
            ),
            (
                replace(
                    state_value,
                    step_stage=StepStage.ARTIFACT_VERIFIED,
                    active=active,
                ),
                True,
                True,
                True,
                ResumeAction.APPLY_TRANSITION,
            ),
        )
        for current, task_exists, dispatch_exists, output_exists, expected in cases:
            with self.subTest(stage=current.step_stage):
                decision_value = reconcile_resume(
                    current,
                    ledger,
                    task_exists=task_exists,
                    dispatch_exists=dispatch_exists,
                    output_exists=output_exists,
                )
                self.assertEqual(expected, decision_value.action)


if __name__ == "__main__":
    unittest.main()
