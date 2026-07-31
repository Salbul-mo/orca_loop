from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_loop.contracts import parse_permission_report
from orca_loop.models import Role
from orca_loop.orca_client import (
    OrcaClient,
    OrcaProtocolError,
    OrcaTimeoutError,
)
from orca_loop.profiles import build_launch_profile


class OrcaClientTest(unittest.TestCase):
    def client(self) -> OrcaClient:
        return OrcaClient(
            executable=(
                "C:\\Windows\\System32\\cmd.exe"
                if __import__("os").name == "nt"
                else "/bin/sh"
            )
        )

    def test_stderr_keepalive_is_separate(self) -> None:
        process = MagicMock()
        process.communicate.return_value = (
            json.dumps({"ok": True, "result": {"value": 1}}).encode(),
            b"keepalive\n",
        )
        process.returncode = 0
        with patch("subprocess.Popen", return_value=process):
            response = self.client().call(("status",), timeout_ms=1000)
        self.assertEqual("keepalive\n", response.stderr)
        self.assertEqual('{"value":1}', response.result_json)

    def test_malformed_and_ok_false_are_rejected(self) -> None:
        for stdout in (
            b"not-json",
            json.dumps(
                {"ok": False, "error": {"message": "failed"}}
            ).encode(),
        ):
            process = MagicMock()
            process.communicate.return_value = (stdout, b"")
            process.returncode = 0
            with patch("subprocess.Popen", return_value=process):
                with self.assertRaises(OrcaProtocolError):
                    self.client().call(("status",), timeout_ms=1000)

    def test_timeout_raises_typed_error(self) -> None:
        process = MagicMock()
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(("orca",), 1),
            (b"", b""),
        ]
        process.poll.return_value = 1
        with patch("subprocess.Popen", return_value=process):
            with self.assertRaises(OrcaTimeoutError):
                self.client().call(("status",), timeout_ms=1)


class ProfileTest(unittest.TestCase):
    def test_strategy_d_profiles_match_live_contract(self) -> None:
        report_path = (
            Path.cwd()
            / "runs"
            / "20260731-permission-spike-03"
            / "control"
            / "permission-feasibility.json"
        )
        if not report_path.exists():
            self.skipTest("live permission report is not present")
        report = parse_permission_report(
            report_path.read_text(encoding="utf-8")
        )
        root = Path.cwd().resolve()
        step_input = root / "runs" / "profile-test" / "in"
        step_output = root / "runs" / "profile-test" / "out"
        step_input.mkdir(parents=True, exist_ok=True)
        step_output.mkdir(parents=True, exist_ok=True)
        for role in Role:
            profile = build_launch_profile(
                role,
                root,
                step_input,
                step_output,
                report,
                expected_orca_version="1.4.159",
            )
            if role is Role.IMPLEMENTER:
                self.assertEqual((root,), profile.writable_roots)
            else:
                self.assertEqual((), profile.writable_roots)
            self.assertNotIn(str(step_input.parent), profile.command)


if __name__ == "__main__":
    unittest.main()
