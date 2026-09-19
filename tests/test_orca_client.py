from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orca_loop.models import (
    AgentProvider,
    AgentRuntimeOptions,
    PermissionProfile,
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
    def test_runtime_options_generate_provider_specific_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worktree = root / "worktree"
            input_dir = root / "in"
            output_dir = root / "out"
            worktree.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()

            claude = build_launch_profile(
                Role.PLANNER,
                PermissionProfile.READ_ONLY,
                worktree,
                input_dir,
                output_dir,
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
                PermissionProfile.WORKSPACE_WRITE,
                worktree,
                input_dir,
                output_dir,
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
                PermissionProfile.READ_ONLY,
                worktree,
                input_dir,
                output_dir,
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
                PermissionProfile.WORKSPACE_WRITE,
                worktree,
                input_dir,
                output_dir,
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
            baseline = build_launch_profile(
                Role.CROSS_CONFIRMER,
                PermissionProfile.READ_ONLY,
                worktree,
                input_dir,
                output_dir,
            )
            inherited = build_launch_profile(
                Role.CROSS_CONFIRMER,
                PermissionProfile.READ_ONLY,
                worktree,
                input_dir,
                output_dir,
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
                    PermissionProfile.READ_ONLY,
                    worktree,
                    input_dir,
                    output_dir,
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
                            (
                                PermissionProfile.WORKSPACE_WRITE
                                if role is Role.IMPLEMENTER
                                else PermissionProfile.READ_ONLY
                            ),
                            worktree,
                            input_dir,
                            output_dir,
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

    def test_role_profiles_enforce_writable_roots(self) -> None:
        root = Path.cwd().resolve()
        step_input = root / "runs" / "profile-test" / "in"
        step_output = root / "runs" / "profile-test" / "out"
        step_input.mkdir(parents=True, exist_ok=True)
        step_output.mkdir(parents=True, exist_ok=True)
        for role in Role:
            profile = build_launch_profile(
                role,
                (
                    PermissionProfile.WORKSPACE_WRITE
                    if role is Role.IMPLEMENTER
                    else PermissionProfile.READ_ONLY
                ),
                root,
                step_input,
                step_output,
            )
            if role is Role.IMPLEMENTER:
                self.assertEqual((root,), profile.writable_roots)
            else:
                self.assertEqual((), profile.writable_roots)
            self.assertNotIn(str(step_input.parent), profile.command)

    def test_permission_profile_controls_writable_roots_not_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            worktree = root / "worktree"
            input_dir = root / "in"
            output_dir = root / "out"
            worktree.mkdir()
            input_dir.mkdir()
            output_dir.mkdir()

            planner_with_write = build_launch_profile(
                Role.PLANNER,
                PermissionProfile.WORKSPACE_WRITE,
                worktree,
                input_dir,
                output_dir,
            )
            implementer_read_only = build_launch_profile(
                Role.IMPLEMENTER,
                PermissionProfile.READ_ONLY,
                worktree,
                input_dir,
                output_dir,
            )

            self.assertEqual((worktree,), planner_with_write.writable_roots)
            self.assertEqual((), implementer_read_only.writable_roots)


if __name__ == "__main__":
    unittest.main()
