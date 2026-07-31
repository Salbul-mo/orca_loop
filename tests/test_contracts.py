from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from orca_loop.contracts import (
    ContractViolationError,
    canonical_json_bytes,
    digest_value,
    parse_human_decision,
    parse_permission_report,
    parse_plan_document,
    parse_review_artifact,
    parse_worker_done,
    serialize_json,
)
from orca_loop.models import (
    ArtifactKind,
    DispatchHandle,
    ExpectedProvenance,
    LoopCounters,
    Role,
    RoleContext,
    ScopePackage,
    StagedInput,
    TestExecutionPolicy,
    WorkerHandle,
    WorkerKey,
    TestGateStatus,
    WorkerDonePayload,
)
from orca_loop.transport import (
    InputStagingError,
    ScopeViolationError,
    promote_artifact,
    stage_inputs,
    verify_input_manifest,
)
from orca_loop.workspace import create_run_workspace
from orca_loop.roles import TemplateContractError, render_role_contract
from orca_loop.config import empty_test_policy


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def plan_value() -> dict[str, object]:
    return {
        "schema_version": 1,
        "plan_version": 1,
        "request_digest": DIGEST_A,
        "source_instruction": "Implement the accepted request.",
        "interpretation": "Small coherent implementation.",
        "rationale": "The selected alternative is minimal.",
        "current_state_evidence": ["src/example.py:1"],
        "affected_files": [
            {
                "path": "src/example.py",
                "operation": "modify",
                "rename_from": None,
            }
        ],
        "implementation_steps": ["Update behavior."],
        "data_api_schema_changes": "",
        "error_handling": ["Reject invalid input."],
        "test_contract": {
            "commands": [
                {
                    "argv": ["py", "-m", "unittest"],
                    "cwd": ".",
                    "timeout_ms": 30000,
                    "kind": "unit",
                }
            ],
            "test_ids": ["T-1"],
        },
        "test_policy_digest": DIGEST_B,
        "acceptance_criteria": [
            {
                "criterion_id": "AC-1",
                "verification_method": "Run T-1.",
            }
        ],
        "risks": [],
        "out_of_scope": [],
        "reviewed_finding_ids": [],
        "finding_decisions": [],
    }


def review_value(kind: str = "plan_review") -> dict[str, object]:
    verdict = "REVISE" if kind == "plan_review" else "CHANGES_REQUESTED"
    return {
        "schema_version": 1,
        "artifact_kind": kind,
        "run_id": "run-1",
        "task_id": "task-1",
        "dispatch_id": "dispatch-1",
        "consensus_round": 1,
        "snapshot_digest": DIGEST_A,
        "role": (
            "plan_reviewer"
            if kind == "plan_review"
            else (
                "cross_confirmer"
                if kind == "cross_review"
                else "code_reviewer"
            )
        ),
        "verdict": verdict,
        "reviewed_plan_version": 1,
        "reviewed_artifact_digest": DIGEST_B,
        "reviewed_finding_ids": ["F-1"],
        "finding_decisions": [
            {
                "id": "F-1",
                "side": "CODEX",
                "decision": "CHANGE_REQUIRED",
                "snapshot_digest": DIGEST_A,
                "round": 1,
                "evidence": ["review.json"],
            }
        ],
        "findings": [],
        "non_blocking_suggestions": [],
        "escalation_signals": [],
        "agrees_with_reviewer": (
            True if kind == "cross_review" else None
        ),
    }


class ModelContractTest(unittest.TestCase):
    def test_enums_roundtrip_and_dataclasses_are_frozen(self) -> None:
        self.assertEqual(
            TestGateStatus.PASS,
            TestGateStatus(TestGateStatus.PASS.value),
        )
        counters = LoopCounters(0, 0)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            counters.test_fix_attempts = 1  # type: ignore[misc]


class ArtifactContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.expected = ExpectedProvenance(
            run_id="run-1",
            task_id="task-1",
            dispatch_id="dispatch-1",
            consensus_round=1,
            snapshot_digest=DIGEST_A,
        )

    def test_plan_accepts_rationale_word_but_rejects_unknown_section(self) -> None:
        parsed = parse_plan_document(json.dumps(plan_value()))
        self.assertEqual(1, parsed.plan_version)
        value = plan_value()
        value["replacement_plan"] = ["unapproved"]
        with self.assertRaises(ContractViolationError):
            parse_plan_document(json.dumps(value))

    def test_worker_done_alias_roundtrip(self) -> None:
        raw = {
            "schema_version": 1,
            "taskId": "task-1",
            "dispatchId": "dispatch-1",
            "reportPath": str(Path.cwd() / "report.json"),
            "artifactDigest": DIGEST_B,
        }
        parsed = parse_worker_done(json.dumps(raw), self.expected)
        self.assertIsInstance(parsed, WorkerDonePayload)
        reparsed = parse_worker_done(serialize_json(parsed), self.expected)
        self.assertEqual(parsed, reparsed)

    def test_review_requires_decisions_and_cross_agreement(self) -> None:
        value = review_value()
        value["finding_decisions"] = []
        with self.assertRaisesRegex(
            ContractViolationError,
            "missing finding decisions",
        ):
            parse_review_artifact(
                json.dumps(value),
                ArtifactKind.PLAN_REVIEW,
                self.expected,
                delivered_finding_ids=("F-1",),
            )
        cross = review_value("cross_review")
        parsed = parse_review_artifact(
            json.dumps(cross),
            ArtifactKind.CROSS_REVIEW,
            self.expected,
            delivered_finding_ids=("F-1",),
        )
        self.assertTrue(parsed.agrees_with_reviewer)

    def test_oversized_artifact_is_rejected(self) -> None:
        with self.assertRaisesRegex(ContractViolationError, "artifact size"):
            parse_plan_document("{" + ("x" * 1_048_576) + "}")

    def test_revision_human_decision_requires_actionable_scope(self) -> None:
        value = {
            "decision": "revise_code",
            "decision_note": None,
            "affected_acceptance_criteria": [],
            "affected_finding_ids": [],
            "report_digest": DIGEST_A,
        }
        with self.assertRaises(ContractViolationError):
            parse_human_decision(json.dumps(value))

    def test_live_permission_report_matches_exact_contract(self) -> None:
        path = (
            Path.cwd()
            / "runs"
            / "20260731-permission-spike-03"
            / "control"
            / "permission-feasibility.json"
        )
        if not path.exists():
            self.skipTest("live permission report is not present")
        parsed = parse_permission_report(path.read_text(encoding="utf-8"))
        self.assertEqual("D", parsed.strategy.value if parsed.strategy else None)
        value = json.loads(path.read_text(encoding="utf-8"))
        digest_input = dict(value)
        claimed = digest_input.pop("report_digest")
        self.assertEqual(claimed, digest_value(digest_input))
        self.assertEqual(
            claimed.removeprefix("sha256:"),
            __import__("hashlib").sha256(
                canonical_json_bytes(digest_input)
            ).hexdigest(),
        )


class TransportContractTest(unittest.TestCase):
    def test_staging_detects_tamper_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            _, step = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            with self.assertRaises(InputStagingError):
                stage_inputs(
                    step,
                    (
                        StagedInput("same", None, b"a"),
                        StagedInput("same", None, b"b"),
                    ),
                )
            manifest = stage_inputs(
                step,
                (StagedInput("request.json", None, b"{}"),),
            )
            (step.input_dir / "request.json").write_bytes(b"tampered")
            with self.assertRaises(InputStagingError):
                verify_input_manifest(step, manifest)


class RoleTemplateTest(unittest.TestCase):
    def context(
        self,
        root: Path,
        role: Role,
        policy: TestExecutionPolicy,
    ) -> RoleContext:
        step = root / "steps" / "step-1"
        (step / "in").mkdir(parents=True, exist_ok=True)
        (step / "out").mkdir(parents=True, exist_ok=True)
        return RoleContext(
            role=role,
            run_id="run-1",
            consensus_round=1,
            worktree_path=root,
            step_dir=step,
            coordinator_handle="term-coordinator",
            plan_version=1,
            snapshot_digest=DIGEST_A,
            scope_package=ScopePackage((), (), (), (), (), ()),
            test_gate_result=(
                TestGateStatus.PASS
                if role in {
                    Role.CODE_REVIEWER,
                    Role.CROSS_CONFIRMER,
                }
                else None
            ),
            test_policy=(
                policy
                if role in {Role.PLANNER, Role.PLAN_REVIEWER}
                else None
            ),
            delivered_finding_ids=(),
        )

    def test_all_role_templates_render_without_unresolved_markers(self) -> None:
        policy = empty_test_policy()
        templates = {
            Role.PLANNER: "planner.md",
            Role.PLAN_REVIEWER: "plan_reviewer.md",
            Role.IMPLEMENTER: "implementer.md",
            Role.CODE_REVIEWER: "code_reviewer.md",
            Role.CROSS_CONFIRMER: "cross_confirmer.md",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            rendered = {}
            for role, filename in templates.items():
                contract = render_role_contract(
                    self.context(root, role, policy),
                    Path.cwd() / "prompts" / filename,
                )
                self.assertNotIn("{{", contract.text)
                self.assertIn("worker_done", contract.text)
                rendered[role] = contract.text
            self.assertIn(
                policy.policy_digest,
                rendered[Role.PLANNER],
            )
            self.assertIn(
                policy.policy_digest,
                rendered[Role.PLAN_REVIEWER],
            )

    def test_unknown_placeholder_is_rejected(self) -> None:
        policy = empty_test_policy()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            template = root / "bad.md"
            template.write_text(
                "{{UNKNOWN_PLACEHOLDER}}",
                encoding="utf-8",
            )
            with self.assertRaises(TemplateContractError):
                render_role_contract(
                    self.context(root, Role.PLANNER, policy),
                    template,
                )

    def test_promotion_is_bound_to_outbox_and_digest(self) -> None:
        import hashlib

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            run, step = create_run_workspace(
                root,
                "run-1",
                "step-1",
                resume=False,
            )
            manifest = stage_inputs(
                step,
                (StagedInput("request.json", None, b"{}"),),
            )
            raw = b'{"ok":true}'
            report = step.output_dir / "result.json"
            report.write_bytes(raw)
            worker = WorkerHandle(
                WorkerKey.CODEX_REVIEW,
                "term-1",
                "worktree-1",
                "tab-1",
                "leaf-1",
            )
            active = DispatchHandle(
                "step-1",
                "task-1",
                "dispatch-1",
                worker,
                Role.PLAN_REVIEWER,
                "worktree-1",
                "tab-1",
                "leaf-1",
            )
            payload = WorkerDonePayload(
                1,
                "task-1",
                "dispatch-1",
                str(report),
                "sha256:" + hashlib.sha256(raw).hexdigest(),
            )
            promoted = promote_artifact(
                payload,
                active,
                step,
                manifest,
                run.artifact_dir,
                ArtifactKind.PLAN_REVIEW,
                lambda text: json.loads(text),
            )
            self.assertEqual(raw, promoted.canonical_path.read_bytes())
            escaped = WorkerDonePayload(
                1,
                "task-1",
                "dispatch-1",
                str(root / "foreign.json"),
                payload.artifact_digest,
            )
            (root / "foreign.json").write_bytes(raw)
            with self.assertRaises(ScopeViolationError):
                promote_artifact(
                    escaped,
                    active,
                    step,
                    manifest,
                    run.artifact_dir,
                    ArtifactKind.PLAN_REVIEW,
                    lambda text: json.loads(text),
                )


if __name__ == "__main__":
    unittest.main()
