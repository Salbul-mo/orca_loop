from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_loop.contracts import digest_value, parse_permission_report
from orca_loop.models import (
    AgentProvider,
    AgentRuntimeOptions,
    PermissionFeasibilityReport,
    Role,
    WorkerKey,
)
from orca_loop.orca_client import (
    OrcaClient,
    OrcaProtocolError,
    OrcaTimeoutError,
)
from orca_loop.profiles import LaunchProfileError, build_launch_profile


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
    def permission_report(self, root: Path) -> PermissionFeasibilityReport:
        path = root / "permission.json"
        value = {
            "schema_version": 1,
            "run_id": "profile-test",
            "status": "PASS",
            "strategy": "D",
            "checks": [
                {
                    "check_id": f"V-PERM-0{index}",
                    "status": "PASS",
                    "evidence": ["deterministic fixture"],
                }
                for index in range(1, 6)
            ],
            "evidence": ["deterministic fixture"],
            "orca_version": "1.4.159",
            "canonical_path": str(path.resolve()),
        }
        value["report_digest"] = digest_value(value)
        path.write_text(json.dumps(value), encoding="utf-8")
        return parse_permission_report(path.read_text(encoding="utf-8"))

    def test_runtime_options_generate_provider_specific_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worktree = root / "worktree"
            input_dir = root / "in"
            output_dir = root / "out"
            worktree.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            report = self.permission_report(root)

            claude = build_launch_profile(
                Role.PLANNER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
                runtime_options=AgentRuntimeOptions(
                    WorkerKey.CLAUDE_PLANNER,
                    AgentProvider.CLAUDE,
                    "claude-test",
                    "high",
                ),
            )
            self.assertIn("--model", claude.command)
            self.assertIn("claude-test", claude.command)
            self.assertIn("--effort", claude.command)
            self.assertIn("high", claude.command)

            codex = build_launch_profile(
                Role.IMPLEMENTER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
                runtime_options=AgentRuntimeOptions(
                    WorkerKey.CODEX_IMPLEMENTER,
                    AgentProvider.CODEX,
                    "codex-test",
                    'x"high\\value',
                ),
            )
            self.assertIn("codex-test", codex.command)
            self.assertIn(
                'model_reasoning_effort="x\\"high\\\\value"',
                codex.command,
            )
            self.assertIn(
                "--dangerously-bypass-approvals-and-sandbox",
                codex.command,
            )

            codex_planner = build_launch_profile(
                Role.PLANNER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
                runtime_options=AgentRuntimeOptions(
                    WorkerKey.CLAUDE_PLANNER,
                    AgentProvider.CODEX,
                    None,
                    None,
                ),
            )
            self.assertEqual(("codex", "exec"), codex_planner.command[:2])
            self.assertEqual((), codex_planner.writable_roots)

            claude_implementer = build_launch_profile(
                Role.IMPLEMENTER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
                runtime_options=AgentRuntimeOptions(
                    WorkerKey.CODEX_IMPLEMENTER,
                    AgentProvider.CLAUDE,
                    None,
                    None,
                ),
            )
            self.assertEqual("claude", claude_implementer.command[0])
            self.assertEqual((worktree,), claude_implementer.writable_roots)

    def test_null_runtime_is_backward_compatible_and_mismatch_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worktree = root / "worktree"
            input_dir = root / "in"
            output_dir = root / "out"
            worktree.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            report = self.permission_report(root)
            baseline = build_launch_profile(
                Role.CROSS_CONFIRMER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
            )
            inherited = build_launch_profile(
                Role.CROSS_CONFIRMER,
                worktree,
                input_dir,
                output_dir,
                report,
                expected_orca_version="1.4.159",
                runtime_options=AgentRuntimeOptions(
                    WorkerKey.CODEX_REVIEW,
                    AgentProvider.CODEX,
                    None,
                    None,
                ),
            )
            self.assertEqual(baseline.command, inherited.command)
            self.assertEqual(baseline.writable_roots, inherited.writable_roots)
            with self.assertRaisesRegex(LaunchProfileError, "does not match"):
                build_launch_profile(
                    Role.PLAN_REVIEWER,
                    worktree,
                    input_dir,
                    output_dir,
                    report,
                    expected_orca_version="1.4.159",
                    runtime_options=AgentRuntimeOptions(
                        WorkerKey.CODEX_IMPLEMENTER,
                        AgentProvider.CODEX,
                        "wrong",
                        "high",
                    ),
                )

    def test_every_worker_slot_accepts_both_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worktree = root / "worktree"
            input_dir = root / "in"
            output_dir = root / "out"
            worktree.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()
            report = self.permission_report(root)
            slots = {
                WorkerKey.CLAUDE_PLANNER: Role.PLANNER,
                WorkerKey.CLAUDE_CODE_REVIEW: Role.CODE_REVIEWER,
                WorkerKey.CODEX_IMPLEMENTER: Role.IMPLEMENTER,
                WorkerKey.CODEX_REVIEW: Role.PLAN_REVIEWER,
            }
            for worker, role in slots.items():
                for provider in AgentProvider:
                    with self.subTest(worker=worker, provider=provider):
                        profile = build_launch_profile(
                            role,
                            worktree,
                            input_dir,
                            output_dir,
                            report,
                            expected_orca_version="1.4.159",
                            runtime_options=AgentRuntimeOptions(
                                worker,
                                provider,
                                None,
                                None,
                            ),
                        )
                        expected_command = (
                            "claude"
                            if provider is AgentProvider.CLAUDE
                            else "codex"
                        )
                        self.assertEqual(expected_command, profile.command[0])
                        expected_roots = (
                            (worktree,)
                            if role is Role.IMPLEMENTER
                            else ()
                        )
                        self.assertEqual(
                            expected_roots,
                            profile.writable_roots,
                        )

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
