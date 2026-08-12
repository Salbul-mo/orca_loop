from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orca_loop.reporting import (
    record_artifact_history,
    render_failure_report,
    render_run_summary,
    render_stage_report,
    resume_command_line,
)
from orca_loop.config import build_argument_parser


PLAN = {
    "schema_version": 1,
    "plan_version": 2,
    "interpretation": "지도 뷰를 추가한다",
    "rationale": "슬라이드 요구사항",
    "current_state_evidence": ["MapView.java:1"],
    "affected_files": [
        {"path": "src/MapView.java", "operation": "add", "rename_from": None}
    ],
    "implementation_steps": ["뷰 추가", "라우팅 연결"],
    "data_api_schema_changes": "없음",
    "acceptance_criteria": [
        {"criterion_id": "AC-1", "statement": "지도가 렌더링된다"}
    ],
    "test_contract": {"commands": [["gradlew", "test"]], "test_ids": ["T-1"]},
    "risks": ["렌더링 성능"],
    "out_of_scope": ["인증"],
}

REVIEW = {
    "schema_version": 1,
    "artifact_kind": "code_review",
    "verdict": "CHANGES_REQUIRED",
    "consensus_round": 1,
    "reviewed_plan_version": 2,
    "findings": [
        {
            "finding_id": "F-1",
            "severity": "P0",
            "blocking_reason": "B1",
            "impact_class": "architecture",
            "file": "src/MapView.java",
            "line": 42,
            "root_cause": "널 검사 누락",
            "description": "좌표가 없을 때 예외",
            "required_fix": "널 검사 추가",
            "required_change": None,
            "reopens": None,
        }
    ],
    "non_blocking_suggestions": [{"description": "주석 보강"}],
}

IMPLEMENTATION = {
    "schema_version": 1,
    "status": "COMPLETED",
    "consensus_round": 1,
    "plan_change_required": False,
    "summary": "지도 뷰 구현 완료",
    "changed_files": ["src/MapView.java"],
    "addressed_findings": [{"finding_id": "F-1", "resolution": "널 검사 추가"}],
}


class FakeStatus:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeHistoryEntry:
    def __init__(self, generation: int) -> None:
        self.generation = generation
        self.state = FakeStatus("PLAN")
        self.step_stage = FakeStatus("TRANSITION_COMMITTED")
        self.signal = FakeStatus("ok")
        self.reason = f"transition {generation}"


class FakeState:
    def __init__(self) -> None:
        self.run_id = "run-1"
        self.generation = 7
        self.status = FakeStatus("IN_PROGRESS")
        self.state = FakeStatus("PLAN_REVIEW")
        self.test_gate_status = None
        self.history = tuple(FakeHistoryEntry(index) for index in range(1, 13))


class FakeLedger:
    plan_round = 2
    code_round = 0
    findings = ()


class ReportingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.harness = Path(self.temporary.name).resolve()
        self.run_root = self.harness / "runs" / "run-1"
        (self.run_root / "artifacts").mkdir(parents=True)
        (self.run_root / "logs").mkdir(parents=True)

    def reports(self, name: str) -> Path:
        return self.run_root / "reports" / name


class ArtifactHistoryTest(ReportingTestCase):
    def test_each_generation_is_kept(self) -> None:
        for generation in (4, 9, 14):
            record_artifact_history(
                self.run_root,
                "plan",
                generation,
                json.dumps(PLAN).encode("utf-8"),
            )
        history = sorted(
            (self.run_root / "artifacts" / "history").glob("plan.g*.json")
        )
        self.assertEqual(3, len(history))
        self.assertEqual("plan.g0004.json", history[0].name)

    def test_history_content_is_byte_identical(self) -> None:
        raw = json.dumps(PLAN, ensure_ascii=False).encode("utf-8")
        path = record_artifact_history(self.run_root, "plan", 4, raw)
        assert path is not None
        self.assertEqual(raw, path.read_bytes())


class StageReportTest(ReportingTestCase):
    def test_plan_report_lists_scope(self) -> None:
        path = render_stage_report(
            self.run_root,
            "plan",
            json.dumps(PLAN),
            7,
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")
        self.assertIn("계획 (Planner)", text)
        self.assertIn("src/MapView.java", text)
        self.assertIn("AC-1", text)
        self.assertIn("gradlew test", text)

    def test_review_report_lists_findings(self) -> None:
        path = render_stage_report(
            self.run_root,
            "code_review",
            json.dumps(REVIEW),
            8,
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")
        self.assertIn("F-1", text)
        self.assertIn("P0", text)
        self.assertIn("널 검사 추가", text)

    def test_implementation_report_lists_changed_files(self) -> None:
        path = render_stage_report(
            self.run_root,
            "implementation",
            json.dumps(IMPLEMENTATION),
            9,
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")
        self.assertIn("src/MapView.java", text)
        self.assertIn("지도 뷰 구현 완료", text)

    def test_unknown_kind_is_skipped(self) -> None:
        self.assertIsNone(
            render_stage_report(self.run_root, "unknown", "{}", 1)
        )

    def test_malformed_artifact_does_not_raise(self) -> None:
        self.assertIsNone(
            render_stage_report(self.run_root, "plan", "{ not json", 1)
        )
        log = self.run_root / "logs" / "reporting.log"
        self.assertTrue(log.is_file())
        self.assertIn("render_stage_report", log.read_text(encoding="utf-8"))


class RunSummaryTest(ReportingTestCase):
    def summary(self) -> str:
        path = render_run_summary(
            self.run_root,
            FakeState(),
            FakeLedger(),
            harness_root=self.harness,
        )
        assert path is not None
        return path.read_text(encoding="utf-8")

    def test_summary_reports_state_and_stages(self) -> None:
        (self.run_root / "artifacts" / "plan.json").write_text(
            json.dumps(PLAN),
            encoding="utf-8",
        )
        text = self.summary()
        self.assertIn("PLAN_REVIEW", text)
        self.assertIn("generation: 7", text)
        self.assertIn("`artifacts/plan.json`", text)
        self.assertIn("| 계획 검토 (Plan Reviewer) | 미완료", text)

    def test_summary_caps_transition_history(self) -> None:
        text = self.summary()
        self.assertNotIn("g0001 ", text)
        self.assertIn("g0012", text)

    def test_summary_carries_a_usable_resume_command(self) -> None:
        text = self.summary()
        command = resume_command_line(self.harness, "run-1")
        self.assertIn(command, text)
        parser = build_argument_parser()
        namespace = parser.parse_args(
            [
                "--run-id",
                "run-1",
                "--request",
                "r.md",
                "--worktree",
                ".",
                "--coordinator-handle",
                "t",
                "--permission-report",
                "p.json",
                "--resume",
            ]
        )
        self.assertTrue(namespace.resume)

    def test_revision_count_is_shown(self) -> None:
        (self.run_root / "artifacts" / "plan.json").write_text(
            json.dumps(PLAN),
            encoding="utf-8",
        )
        for generation in (4, 9, 14):
            record_artifact_history(
                self.run_root,
                "plan",
                generation,
                b"{}",
            )
        self.assertIn("(3회)", self.summary())


class FailureReportTest(ReportingTestCase):
    def test_failure_report_lists_evidence_and_resume(self) -> None:
        (self.run_root / "logs" / "step-g0004-plan.stderr.log").write_text(
            "error: unknown model",
            encoding="utf-8",
        )
        path = render_failure_report(
            self.run_root,
            reason="agent exited 1",
            harness_root=self.harness,
            run_id="run-1",
            detail=("recorded snapshot: sha256:aaa",),
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")
        self.assertIn("agent exited 1", text)
        self.assertIn("step-g0004-plan.stderr.log", text)
        self.assertIn("resume --run-id run-1", text)
        self.assertIn("recorded snapshot", text)
        self.assertIn("--accept-worktree-drift", text)


if __name__ == "__main__":
    unittest.main()
