from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
import subprocess
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
from orca_loop.contracts import (
    ContractViolationError,
    digest_value,
    parse_review_comparison,
    parse_review_context,
    parse_test_evidence,
    serialize_json,
)
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
    ValidationLineage,
    Violation,
    WorkerHandle,
    WorkerKey,
)
from tests.test_ledger import DIGEST_B, review, finding, decision
from orca_loop.ledger import apply_review_artifact, commit_round
from orca_loop.models import DecisionValue, Side
from run_loop import _qualify_merge, _step_inputs, _user_scope
from orca_loop.snapshot import capture_snapshot


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
    def test_blind_b_input_allowlist_excludes_peer_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            artifacts = root / "artifacts"
            review_dir = root / "review"
            round_dir = review_dir / "round-1"
            artifacts.mkdir()
            round_dir.mkdir(parents=True)
            request = root / "request.md"
            request.write_text("request", encoding="utf-8")
            for name in ("plan.json", "plan_review.json", "implementation.json"):
                (artifacts / name).write_text("{}\n", encoding="utf-8")
            (artifacts / "code_review_a.json").write_text("peer\n", encoding="utf-8")
            (artifacts / "test_evidence.json").write_text("{}\n", encoding="utf-8")
            (round_dir / "frozen.diff").write_text("diff\n", encoding="utf-8")
            (round_dir / "scope-manifest.json").write_text("{}\n", encoding="utf-8")

            def file_digest(path: Path) -> str:
                return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

            context_value = {
                "schema_version": 1,
                "run_id": "run-1",
                "consensus_round": 1,
                "plan_version": 1,
                "snapshot_digest": DIGEST_A,
                "implementation_artifact_digest": file_digest(
                    artifacts / "implementation.json"
                ),
                "test_evidence_digest": file_digest(
                    artifacts / "test_evidence.json"
                ),
                "frozen_diff_digest": file_digest(round_dir / "frozen.diff"),
                "scope_manifest_digest": file_digest(
                    round_dir / "scope-manifest.json"
                ),
                "readonly_mirror_digest": DIGEST_A,
                "baseline_finding_ids": [],
                "acceptance_criteria_ids": [],
                "affected_files": [],
                "test_ids": [],
            }
            context_value["context_digest"] = digest_value(context_value)
            context = parse_review_context(json.dumps(context_value))
            (artifacts / "review_context.json").write_text(
                serialize_json(context) + "\n",
                encoding="utf-8",
            )
            controller = SimpleNamespace(
                state=SimpleNamespace(state=LoopState.CODE_REVIEW_B),
                ledger=SimpleNamespace(code_round=0),
                workspace=SimpleNamespace(
                    artifact_dir=artifacts,
                    review_dir=review_dir,
                ),
            )
            preflight = SimpleNamespace(
                arguments=SimpleNamespace(
                    config=SimpleNamespace(request_path=request)
                )
            )
            names = tuple(
                item.name
                for item in _step_inputs(
                    controller,
                    preflight,
                    Role.CROSS_CONFIRMER,
                )
            )
            self.assertEqual(
                (
                    "request.md",
                    "plan.json",
                    "plan_review.json",
                    "implementation.json",
                    "test-evidence.json",
                    "review-context.json",
                    "frozen.diff",
                    "scope-manifest.json",
                ),
                names,
            )
            self.assertNotIn("code_review_a.json", names)

    def test_merge_qualification_rejects_live_snapshot_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            subprocess.run(("git", "init"), cwd=root, check=True, capture_output=True)
            subprocess.run(
                ("git", "config", "user.email", "test@example.com"),
                cwd=root,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ("git", "config", "user.name", "Test"),
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / "source.txt").write_text("baseline", encoding="utf-8")
            (root / ".gitignore").write_text("evidence/\n", encoding="utf-8")
            subprocess.run(
                ("git", "add", "source.txt", ".gitignore"),
                cwd=root,
                check=True,
            )
            subprocess.run(
                ("git", "commit", "-m", "fixture"),
                cwd=root,
                check=True,
                capture_output=True,
            )
            snapshot = capture_snapshot(root).snapshot_digest
            artifacts = root / "evidence"
            artifacts.mkdir()
            test_value = {
                "schema_version": 1,
                "run_id": "run-1",
                "plan_version": 1,
                "consensus_round": 1,
                "test_gate_status": "NOT_RUN",
                "test_policy_digest": DIGEST_A,
                "commands": [],
                "policy_violations": [],
                "before_snapshot_digest": snapshot,
                "after_snapshot_digest": None,
                "authoritative_snapshot_digest": snapshot,
                "test_ids": [],
                "attribution": "none",
            }
            test_value["artifact_digest"] = digest_value(test_value)
            test_evidence = parse_test_evidence(json.dumps(test_value))
            (artifacts / "test_evidence.json").write_text(
                serialize_json(test_evidence) + "\n",
                encoding="utf-8",
            )
            context_value = {
                "schema_version": 1,
                "run_id": "run-1",
                "consensus_round": 1,
                "plan_version": 1,
                "snapshot_digest": snapshot,
                "implementation_artifact_digest": DIGEST_A,
                "test_evidence_digest": DIGEST_A,
                "frozen_diff_digest": DIGEST_A,
                "scope_manifest_digest": DIGEST_A,
                "readonly_mirror_digest": DIGEST_A,
                "baseline_finding_ids": [],
                "acceptance_criteria_ids": [],
                "affected_files": [],
                "test_ids": [],
            }
            context_value["context_digest"] = digest_value(context_value)
            context = parse_review_context(json.dumps(context_value))
            (artifacts / "review_context.json").write_text(
                serialize_json(context) + "\n",
                encoding="utf-8",
            )
            blind_digests = []
            for lane in ("a", "b"):
                path = artifacts / f"code_review_{lane}.json"
                path.write_text("{}\n", encoding="utf-8")
                blind_digests.append(
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                )
            comparison_value = {
                "schema_version": 1,
                "run_id": "run-1",
                "consensus_round": 1,
                "snapshot_digest": snapshot,
                "review_context_digest": context.context_digest,
                "pre_round_ledger_digest": DIGEST_A,
                "blind_a_artifact_digest": blind_digests[0],
                "blind_b_artifact_digest": blind_digests[1],
                "status": "AGREED",
                "agreed_finding_ids": [],
                "candidates": [],
            }
            comparison_value["comparison_digest"] = digest_value(comparison_value)
            comparison = parse_review_comparison(json.dumps(comparison_value))
            (artifacts / "review_comparison.json").write_text(
                serialize_json(comparison) + "\n",
                encoding="utf-8",
            )
            lineage = ValidationLineage(
                test_gate_snapshot_digest=snapshot,
                test_evidence_digest=test_evidence.artifact_digest,
                review_context_snapshot_digest=snapshot,
                review_context_digest=context.context_digest,
                blind_review_a_snapshot_digest=snapshot,
                blind_review_a_artifact_digest=blind_digests[0],
                blind_review_b_snapshot_digest=snapshot,
                blind_review_b_artifact_digest=blind_digests[1],
                review_comparison_digest=comparison.comparison_digest,
                consensus_snapshot_digest=snapshot,
            )
            controller = SimpleNamespace(
                state=replace(
                    initial_state(),
                    snapshot_digest=snapshot,
                    validation_lineage=lineage,
                ),
                workspace=SimpleNamespace(artifact_dir=artifacts),
            )
            self.assertTrue(_qualify_merge(controller, root).qualified)
            (root / "source.txt").write_text("drift", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "snapshot lineage"):
                _qualify_merge(controller, root)

    def test_code_evaluate_only_reads_atomically_committed_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = replace(empty_ledger("run-1"), code_round=1)
            result = execute_evaluate(
                state=LoopState.CONSENSUS_EVALUATE,
                ledger=ledger,
                evidence=None,
                config=config(Path(temporary).resolve()),
                plan=None,
                destructive_approval=None,
            )
            self.assertEqual(1, result.ledger.code_round)
            self.assertIs(SignalKind.UNRESOLVED_ZERO, result.signal.kind)

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
            reason=ContractViolationError("malformed artifact").reason,
            finding_ids=(),
        )
        self.assertEqual(SignalKind.OPERATIONAL_RETRY, result.signal.kind)
        self.assertEqual(0, result.ledger.plan_round)
        final = operational_retry_result(
            ledger=ledger,
            counters=LoopCounters(0, 1),
            limit=2,
            reason=ContractViolationError("missing_finding_decision").reason,
            finding_ids=("F-1",),
        )
        self.assertEqual(SignalKind.ESCALATE, final.signal.kind)

    def test_a_transient_reason_reaches_the_same_retry_budget(self) -> None:
        """Runtime failures were previously unable to reach this path at all."""
        result = operational_retry_result(
            ledger=empty_ledger("run-1"),
            counters=LoopCounters(0, 0),
            limit=3,
            reason="Orca command timed out after 30000 ms",
            finding_ids=(),
        )

        self.assertEqual(SignalKind.OPERATIONAL_RETRY, result.signal.kind)
        self.assertEqual(
            "Orca command timed out after 30000 ms",
            result.signal.reason,
        )

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
