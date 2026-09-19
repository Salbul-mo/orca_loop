from __future__ import annotations

import os
import unittest
from pathlib import Path

from orca_loop.orca_client import OrcaClient


def _live_enabled() -> bool:
    return os.environ.get("ORCA_E2E") == "1"


if _live_enabled():
    class LiveOrcaE2ETest(unittest.TestCase):
        def test_disposable_fixture_and_orca_are_live(self) -> None:
            fixture_value = os.environ.get("ORCA_E2E_FIXTURE")
            coordinator = os.environ.get("ORCA_E2E_COORDINATOR")
            self.assertTrue(fixture_value)
            self.assertTrue(coordinator)
            fixture = Path(str(fixture_value)).resolve()
            self.assertTrue(fixture.is_dir(), "live E2E requires a fixture directory")
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
