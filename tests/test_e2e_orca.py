from __future__ import annotations

import os
import unittest
from pathlib import Path

from orca_loop.contracts import parse_permission_report
from orca_loop.models import (
    PermissionStrategy,
    ValidationStatus,
)
from orca_loop.orca_client import OrcaClient


def _live_enabled() -> bool:
    return os.environ.get("ORCA_E2E") == "1"


if _live_enabled():
    class LiveOrcaE2ETest(unittest.TestCase):
        def test_verified_strategy_and_disposable_fixture_are_live(
            self,
        ) -> None:
            fixture_value = os.environ.get("ORCA_E2E_FIXTURE")
            report_value = os.environ.get("ORCA_E2E_PERMISSION_REPORT")
            coordinator = os.environ.get("ORCA_E2E_COORDINATOR")
            self.assertTrue(fixture_value)
            self.assertTrue(report_value)
            self.assertTrue(coordinator)
            fixture = Path(str(fixture_value)).resolve()
            report_path = Path(str(report_value)).resolve()
            self.assertTrue(
                (fixture / ".orca-permission-fixture").is_file(),
                "live E2E requires the disposable fixture marker",
            )
            report = parse_permission_report(
                report_path.read_text(encoding="utf-8")
            )
            self.assertEqual(ValidationStatus.PASS, report.status)
            self.assertEqual(
                PermissionStrategy.READONLY_REPOSITORY,
                report.strategy,
            )
            self.assertEqual(
                {
                    "V-PERM-01",
                    "V-PERM-02",
                    "V-PERM-03",
                    "V-PERM-04",
                    "V-PERM-05",
                },
                {item.check_id for item in report.checks},
            )
            client = OrcaClient(cwd=fixture)
            client.call(("status",), timeout_ms=10_000)
            client.call(
                (
                    "terminal",
                    "show",
                    "--terminal",
                    str(coordinator),
                ),
                timeout_ms=10_000,
            )
