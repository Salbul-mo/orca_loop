from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orca_loop.config import (
    permission_refresh_marker_path,
    permission_refresh_problem,
    permission_environment_problems,
    permission_report_notes,
    permission_report_problem,
    record_permission_refresh_marker,
)
from orca_loop.contracts import digest_value, parse_permission_report
from orca_loop.environment import (
    ENFORCEMENT_SOURCES,
    capture_environment,
    compare_environment,
    environment_notes,
    describe_environment,
    enforcement_digest,
)
from orca_loop.models import (
    PermissionCheck,
    PermissionEnvironment,
    PermissionFeasibilityReport,
    PermissionStrategy,
    ValidationStatus,
)


BASE = PermissionEnvironment(
    platform="Windows",
    claude_cli="2.1.227",
    codex_cli="0.146.0",
    enforcement_digest="sha256:" + "a" * 64,
)


def report_value(
    root: Path,
    environment: PermissionEnvironment | None,
    orca_version: str = "1.4.179",
    created_at: str | None = None,
    canonical_path: str | None = None,
) -> str:
    canonical = root / "control" / "permission-feasibility.json"
    value: dict[str, object] = {
        "schema_version": 1,
        "run_id": "spike",
        "status": "PASS",
        "strategy": "D",
        "checks": [
            {
                "check_id": f"V-PERM-0{index}",
                "status": "PASS",
                "evidence": ["evidence"],
            }
            for index in range(1, 6)
        ],
        "evidence": ["evidence"],
        "orca_version": orca_version,
        "canonical_path": canonical_path or str(canonical),
    }
    if environment is not None:
        value["environment"] = describe_environment(environment)
    if created_at is not None:
        value["created_at"] = created_at
    value["report_digest"] = digest_value(value)
    return json.dumps(value, ensure_ascii=False)


class EnforcementDigestTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "orca_loop").mkdir()
        for name in ENFORCEMENT_SOURCES:
            (self.root / name).write_text("baseline\n", encoding="utf-8")

    def test_digest_is_stable(self) -> None:
        self.assertEqual(
            enforcement_digest(self.root),
            enforcement_digest(self.root),
        )

    def test_digest_changes_with_enforcement_code(self) -> None:
        before = enforcement_digest(self.root)
        (self.root / ENFORCEMENT_SOURCES[0]).write_text(
            "changed\n",
            encoding="utf-8",
        )
        self.assertNotEqual(before, enforcement_digest(self.root))

    def test_line_endings_do_not_change_the_digest(self) -> None:
        before = enforcement_digest(self.root)
        (self.root / ENFORCEMENT_SOURCES[0]).write_bytes(b"baseline\r\n")
        self.assertEqual(before, enforcement_digest(self.root))

    def test_real_harness_capture_has_all_fields(self) -> None:
        captured = capture_environment(
            Path(__file__).resolve().parents[1]
        )
        self.assertTrue(captured.platform)
        self.assertTrue(captured.enforcement_digest.startswith("sha256:"))


class CompareEnvironmentTest(unittest.TestCase):
    def test_identical_environment_has_no_problems(self) -> None:
        self.assertEqual((), compare_environment(BASE, BASE))

    def test_patch_release_is_tolerated(self) -> None:
        current = replace(BASE, claude_cli="2.1.999")
        self.assertEqual((), compare_environment(BASE, current))

    def test_minor_release_is_informational(self) -> None:
        current = replace(BASE, claude_cli="2.2.0")
        self.assertEqual((), compare_environment(BASE, current))
        self.assertTrue(
            any("claude CLI version" in item for item in environment_notes(BASE, current))
        )

    def test_codex_minor_release_is_informational(self) -> None:
        current = replace(BASE, codex_cli="0.147.0")
        self.assertEqual((), compare_environment(BASE, current))
        self.assertTrue(
            any("codex CLI version" in item for item in environment_notes(BASE, current))
        )

    def test_strict_mode_does_not_change_permission_proof(self) -> None:
        current = replace(BASE, claude_cli="2.1.228")
        self.assertEqual((), compare_environment(BASE, current, strict=True))

    def test_enforcement_change_is_blocking(self) -> None:
        current = replace(BASE, enforcement_digest="sha256:" + "b" * 64)
        problems = compare_environment(BASE, current)
        self.assertTrue(
            any("enforcement code changed" in item for item in problems)
        )

    def test_platform_change_is_blocking(self) -> None:
        current = replace(BASE, platform="Linux")
        problems = compare_environment(BASE, current)
        self.assertTrue(any("platform changed" in item for item in problems))

    def test_missing_cli_is_informational(self) -> None:
        current = replace(BASE, codex_cli=None)
        self.assertEqual((), compare_environment(BASE, current))
        self.assertTrue(
            any("availability changed" in item for item in environment_notes(BASE, current))
        )

    def test_operator_guide_matches_current_safety_policy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        guide = (root / "orca_loop_execution_rules.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--allow-same-provider-consensus", guide)
        self.assertIn("consensus_independence=DEGRADED", guide)
        self.assertIn("CLI availability·version 차이는", guide)
        self.assertIn("Blind A와 B", guide)
        self.assertIn("`NOT_RUN`은 테스트를 실행했다는 뜻이 아니며", guide)
        self.assertIn("현재 Orca terminal", guide)
        self.assertIn("worker terminal 네 개", guide)


class ReportGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.harness = Path(self.temporary.name).resolve()
        (self.harness / "orca_loop").mkdir()
        for name in ENFORCEMENT_SOURCES:
            (self.harness / name).write_text("baseline\n", encoding="utf-8")
        self.run_root = self.harness / "runs" / "spike"
        (self.run_root / "control").mkdir(parents=True)
        self.path = self.run_root / "control" / "permission-feasibility.json"

    def write(
        self,
        environment: PermissionEnvironment | None,
        orca_version: str = "1.4.179",
    ) -> None:
        self.path.write_text(
            report_value(self.run_root, environment, orca_version),
            encoding="utf-8",
        )

    def current_environment(self) -> PermissionEnvironment:
        return capture_environment(self.harness)

    def test_matching_environment_is_usable_despite_orca_drift(self) -> None:
        self.write(self.current_environment(), orca_version="1.4.164")
        self.assertIsNone(
            permission_report_problem(
                self.path,
                "1.4.900",
                harness_root=self.harness,
            )
        )

    def test_orca_drift_is_reported_as_a_note(self) -> None:
        self.write(self.current_environment(), orca_version="1.4.164")
        report = parse_permission_report(
            self.path.read_text(encoding="utf-8")
        )
        notes = permission_report_notes(report, "1.4.900")
        self.assertTrue(notes)
        self.assertIn("informational", notes[0])

    def _record_marker(self) -> None:
        record_permission_refresh_marker(
            self.harness,
            run_id="failed-run",
            reason_code="READONLY_SOURCE_DELTA",
            worker_key="claude_planner",
            step_id="step-1",
            blocked_report_digest="sha256:" + "a" * 64,
            evidence_paths=(),
        )

    def test_new_report_clears_refresh_marker(self) -> None:
        self._record_marker()
        self.path.write_text(
            report_value(
                self.run_root,
                self.current_environment(),
                created_at="2030-01-01T00:00:00+00:00",
            ),
            encoding="utf-8",
        )
        report = parse_permission_report(self.path.read_text(encoding="utf-8"))
        self.assertIsNone(permission_refresh_problem(self.harness, report))
        self.assertFalse(permission_refresh_marker_path(self.harness).exists())

    def test_bad_canonical_path_cannot_clear_refresh_marker(self) -> None:
        self._record_marker()
        self.path.write_text(
            report_value(
                self.run_root,
                self.current_environment(),
                created_at="2030-01-01T00:00:00+00:00",
                canonical_path=str(self.path.with_name("copied-report.json")),
            ),
            encoding="utf-8",
        )
        self.assertIn(
            "canonical_path points elsewhere",
            permission_report_problem(
                self.path,
                "1.4.179",
                harness_root=self.harness,
            ) or "",
        )
        self.assertTrue(permission_refresh_marker_path(self.harness).exists())

    def test_changed_enforcement_code_blocks_the_report(self) -> None:
        self.write(self.current_environment())
        (self.harness / ENFORCEMENT_SOURCES[0]).write_text(
            "tampered\n",
            encoding="utf-8",
        )
        problem = permission_report_problem(
            self.path,
            "1.4.179",
            harness_root=self.harness,
        )
        self.assertIsNotNone(problem)
        self.assertIn("enforcement code changed", str(problem))

    def test_legacy_report_still_uses_exact_orca_version(self) -> None:
        self.write(None, orca_version="1.4.164")
        report = parse_permission_report(
            self.path.read_text(encoding="utf-8")
        )
        self.assertIsNone(report.environment)
        self.assertEqual(
            (),
            permission_environment_problems(
                report,
                self.harness,
                "1.4.164",
            ),
        )
        problems = permission_environment_problems(
            report,
            self.harness,
            "1.4.179",
        )
        self.assertTrue(any("legacy report" in item for item in problems))

    def test_environment_round_trips_through_the_parser(self) -> None:
        environment = self.current_environment()
        self.write(environment)
        report = parse_permission_report(
            self.path.read_text(encoding="utf-8")
        )
        self.assertEqual(environment, report.environment)


class RealReportTest(unittest.TestCase):
    def test_shipped_report_carries_an_environment(self) -> None:
        harness = Path(__file__).resolve().parents[1]
        path = (
            harness
            / "runs"
            / "20260811-permission-spike-01"
            / "control"
            / "permission-feasibility.json"
        )
        if not path.is_file():
            self.skipTest("permission spike report is not present")
        report = parse_permission_report(path.read_text(encoding="utf-8"))
        assert report.environment is not None
        self.assertEqual(
            enforcement_digest(harness),
            report.environment.enforcement_digest,
        )
        self.assertIs(
            PermissionStrategy.READONLY_REPOSITORY,
            report.strategy,
        )
        self.assertIs(ValidationStatus.PASS, report.status)
        self.assertIn(
            "V-PERM-06",
            [item.check_id for item in report.checks],
        )


if __name__ == "__main__":
    unittest.main()
