from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from permission_spike import (
    IMPLEMENTATION_EXPECTED,
    PermissionSpikeError,
    PermissionStrategy,
    ValidationStatus,
    build_report,
    canonical_json_bytes,
    create_fixture,
    record_worker_result,
    serialize_report,
    sha256_bytes,
)


class PermissionFeasibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.harness_root = Path(self.temporary.name)
        self.run_id = "test-run"

    def write_result(
        self,
        role: str,
        *,
        read_value: str | None = None,
        source_write_blocked: bool = False,
        out_write_succeeded: bool = False,
        implementation_write_succeeded: bool = False,
        status: str = "PASS",
    ) -> None:
        output = (
            self.harness_root
            / "runs"
            / self.run_id
            / "permission-steps"
            / role
            / "out"
            / "result.json"
        )
        output.write_text(
            json.dumps(
                {
                    "role": role,
                    "status": status,
                    "read_value": read_value,
                    "source_write_blocked": source_write_blocked,
                    "out_write_succeeded": out_write_succeeded,
                    "implementation_write_succeeded": (
                        implementation_write_succeeded
                    ),
                    "runtime_ids": [f"task-{role}", f"dispatch-{role}"],
                    "evidence": [f"{role} fixture evidence"],
                }
            ),
            encoding="utf-8",
        )

    def prepare_passing_fixture(self) -> None:
        create_fixture(self.harness_root, self.run_id)
        self.write_result(
            "claude_planner",
            read_value="permission spike source baseline",
            source_write_blocked=True,
            out_write_succeeded=True,
        )
        self.write_result(
            "claude_code_review",
            source_write_blocked=True,
            out_write_succeeded=True,
        )
        self.write_result(
            "codex_review",
            source_write_blocked=True,
            out_write_succeeded=True,
        )
        self.write_result(
            "codex_implementer",
            implementation_write_succeeded=True,
        )
        (
            self.harness_root
            / "runs"
            / self.run_id
            / "permission-fixture"
            / "implementation_target.txt"
        ).write_text(IMPLEMENTATION_EXPECTED, encoding="utf-8", newline="\n")

    def test_passing_report_has_canonical_digest(self) -> None:
        self.prepare_passing_fixture()
        report = build_report(
            self.harness_root,
            self.run_id,
            PermissionStrategy.ADD_DIR,
            "1.4.159",
        )
        self.assertEqual(ValidationStatus.PASS, report.status)
        value = serialize_report(report)
        digest_input = dict(value)
        digest_input.pop("report_digest")
        self.assertEqual(
            sha256_bytes(canonical_json_bytes(digest_input)),
            report.report_digest,
        )

    def test_missing_worker_result_blocks_report(self) -> None:
        create_fixture(self.harness_root, self.run_id)
        report = build_report(
            self.harness_root,
            self.run_id,
            PermissionStrategy.ADD_DIR,
            "1.4.159",
        )
        self.assertEqual(ValidationStatus.BLOCKED, report.status)
        self.assertIsNone(report.strategy)

    def test_source_mutation_fails_read_only_checks(self) -> None:
        self.prepare_passing_fixture()
        source = (
            self.harness_root
            / "runs"
            / self.run_id
            / "permission-fixture"
            / "source.txt"
        )
        source.write_text("mutated\n", encoding="utf-8", newline="\n")
        report = build_report(
            self.harness_root,
            self.run_id,
            PermissionStrategy.ADD_DIR,
            "1.4.159",
        )
        statuses = {item.check_id: item.status for item in report.checks}
        self.assertEqual(ValidationStatus.FAIL, statuses["V-PERM-02"])
        self.assertEqual(ValidationStatus.FAIL, statuses["V-PERM-04"])

    def test_existing_nonempty_run_is_rejected(self) -> None:
        run_root = self.harness_root / "runs" / self.run_id
        run_root.mkdir(parents=True)
        (run_root / "foreign.txt").write_text("not a spike", encoding="utf-8")
        with self.assertRaises(PermissionSpikeError):
            create_fixture(self.harness_root, self.run_id)

    def test_invalid_run_id_is_rejected(self) -> None:
        with self.assertRaises(PermissionSpikeError):
            create_fixture(self.harness_root, "../escape")

    def test_coordinator_can_record_bounded_worker_result(self) -> None:
        create_fixture(self.harness_root, self.run_id)
        output = record_worker_result(
            self.harness_root,
            self.run_id,
            "codex_review",
            ValidationStatus.BLOCKED,
            None,
            True,
            False,
            False,
            ("term_123",),
            ("read-only profile rejected add-dir",),
        )
        value = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("codex_review", value["role"])
        self.assertEqual("BLOCKED", value["status"])
        self.assertTrue(value["source_write_blocked"])


if __name__ == "__main__":
    unittest.main()
