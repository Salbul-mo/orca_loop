from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from orca_loop.contracts import build_master_runtime_config
from orca_loop.master_runtime import (
    MasterRuntimeAdapterError,
    build_master_command,
    invoke_master,
    invoke_master_provider,
    render_master_prompt,
)
from orca_loop.models import (
    AgentProvider,
    MasterAction,
    MasterRuntimeOptions,
    PermissionProfile,
    Role,
)


class MasterRuntimeAdapterTest(unittest.TestCase):
    def config(self):
        return build_master_runtime_config(
            MasterRuntimeOptions(
                provider=AgentProvider.CLAUDE,
                model="master-model",
                effort="high",
            )
        )

    def test_render_prompt_appends_canonical_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "master.md"
            prompt.write_text("# Master\n", encoding="utf-8")
            rendered = render_master_prompt(
                prompt,
                {"z": 2, "a": {"value": 1}},
            )
            self.assertEqual(
                "# Master\n\n## Decision context\n\n"
                '{"a":{"value":1},"z":2}\n',
                rendered,
            )

    def test_invoke_master_passes_runtime_options_and_parses_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "master.md"
            prompt.write_text("# Master\n", encoding="utf-8")
            observed = []

            def invoke(options, rendered):
                observed.append((options, rendered))
                return (
                    '{"action":"dispatch","role":"implementer",'
                    '"permissionProfile":"workspace_write",'
                    '"reason":"source changes are required"}'
                )

            decision = invoke_master(
                self.config(),
                prompt,
                {"request": "change source"},
                invoke,
            )
            self.assertEqual(MasterAction.DISPATCH, decision.action)
            self.assertEqual(Role.IMPLEMENTER, decision.role)
            self.assertEqual(
                PermissionProfile.WORKSPACE_WRITE,
                decision.permission_profile,
            )
            self.assertEqual(1, len(observed))
            self.assertEqual(self.config().master, observed[0][0])
            self.assertIn('{"request":"change source"}', observed[0][1])

    def test_invalid_provider_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "master.md"
            prompt.write_text("# Master\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MasterRuntimeAdapterError,
                "invalid decision",
            ):
                invoke_master(
                    self.config(),
                    prompt,
                    {},
                    lambda _options, _prompt: '{"action":"dispatch"}',
                )

    def test_provider_failure_is_wrapped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prompt = Path(directory) / "master.md"
            prompt.write_text("# Master\n", encoding="utf-8")

            def fail(_options, _prompt):
                raise OSError("provider failed")

            with self.assertRaisesRegex(
                MasterRuntimeAdapterError,
                "provider invocation failed",
            ):
                invoke_master(self.config(), prompt, {}, fail)

    def test_master_commands_never_use_worker_bypass_permissions(self) -> None:
        claude = build_master_command(
            MasterRuntimeOptions(
                provider=AgentProvider.CLAUDE,
                model="claude-master",
                effort="high",
            )
        )
        codex = build_master_command(
            MasterRuntimeOptions(
                provider=AgentProvider.CODEX,
                model="codex-master",
                effort="medium",
            )
        )
        joined = " ".join((*claude, *codex))
        self.assertNotIn("bypassPermissions", joined)
        self.assertNotIn("dangerously-bypass-approvals-and-sandbox", joined)
        self.assertNotIn("workspace-write", joined)
        self.assertNotIn("danger-full-access", joined)
        self.assertIn("--restricted", claude)
        self.assertIn("--tools", claude)
        self.assertIn("--permission-mode", claude)
        self.assertIn("plan", claude)
        self.assertIn("--sandbox", codex)
        self.assertIn("read-only", codex)
        self.assertIn("--ask-for-approval", codex)
        self.assertIn("never", codex)
        self.assertIn("--skip-git-repo-check", codex)
        self.assertIn("--ephemeral", codex)

    def test_provider_invoker_uses_stdin_and_isolated_working_directory(self) -> None:
        observed = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, input=None, timeout=None):
                observed["input"] = input
                observed["timeout"] = timeout
                return (b'{"action":"finish","role":null,"permissionProfile":null,"reason":"done"}', b"")

            def poll(self):
                return self.returncode

        def fake_popen(command, **kwargs):
            observed["command"] = command
            observed["cwd"] = Path(kwargs["cwd"])
            observed["shell"] = kwargs["shell"]
            return FakeProcess()

        options = MasterRuntimeOptions(
            provider=AgentProvider.CODEX,
            model=None,
            effort=None,
        )
        with patch("orca_loop.master_runtime.subprocess.Popen", fake_popen):
            output = invoke_master_provider(
                options,
                "master prompt",
                timeout_ms=5_000,
            )
        self.assertIn('"action":"finish"', output)
        self.assertEqual(b"master prompt", observed["input"])
        self.assertEqual(5.0, observed["timeout"])
        self.assertFalse(observed["shell"])
        self.assertTrue(str(observed["cwd"]).endswith(tuple(observed["cwd"].parts[-1:])))
        self.assertNotIn("workspace-write", observed["command"])


if __name__ == "__main__":
    unittest.main()
