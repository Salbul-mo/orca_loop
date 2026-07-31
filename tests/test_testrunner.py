from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orca_loop.contracts import digest_value
from orca_loop.models import (
    TestCommand,
    TestExecutionPolicy,
    TestGateStatus,
    TestKind,
)
from orca_loop.testrunner import run_tests, validate_test_commands


class TestRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self._git("init")
        self._git("config", "user.name", "test-runner")
        self._git("config", "user.email", "test-runner@invalid.local")
        (self.root / ".gitignore").write_text(
            "test-output/\n",
            encoding="utf-8",
        )
        (self.root / "source.txt").write_text(
            "baseline\n",
            encoding="utf-8",
        )
        self._git("add", "--", ".")
        self._git("commit", "-m", "baseline")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ("git", "-C", str(self.root), *args),
            check=True,
            capture_output=True,
        )

    def policy(
        self,
        command: TestCommand,
        *,
        allowed_output_paths: tuple[str, ...] = (),
    ) -> TestExecutionPolicy:
        value = {
            "allowed_commands": [
                {
                    "argv": list(command.argv),
                    "cwd": command.cwd,
                    "timeout_ms": command.timeout_ms,
                    "kind": command.kind.value,
                }
            ],
            "allowed_env_keys": [],
            "allowed_output_paths": list(allowed_output_paths),
            "approved_kinds": [command.kind.value],
        }
        return TestExecutionPolicy(
            (command,),
            (),
            allowed_output_paths,
            (command.kind,),
            digest_value(value),
        )

    def test_exact_command_only(self) -> None:
        command = TestCommand(
            (sys.executable, "-c", "print('ok')"),
            ".",
            10_000,
            TestKind.UNIT,
        )
        policy = self.policy(command)
        self.assertTrue(
            validate_test_commands((command,), policy, self.root).approved
        )
        appended = TestCommand(
            command.argv + ("extra",),
            command.cwd,
            command.timeout_ms,
            command.kind,
        )
        self.assertFalse(
            validate_test_commands(
                (appended,),
                policy,
                self.root,
            ).approved
        )

    def test_sanitized_environment_and_pass(self) -> None:
        os.environ["ORCA_HARNESS_SECRET"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "ORCA_HARNESS_SECRET", None)
        command = TestCommand(
            (
                sys.executable,
                "-c",
                (
                    "import os,sys;"
                    "sys.exit(1 if 'ORCA_HARNESS_SECRET' in os.environ else 0)"
                ),
            ),
            ".",
            10_000,
            TestKind.UNIT,
        )
        result = run_tests((command,), self.policy(command), self.root)
        self.assertEqual(TestGateStatus.PASS, result.status)

    def test_timeout_is_fail_and_source_mutation_is_policy_violation(self) -> None:
        timeout = TestCommand(
            (
                sys.executable,
                "-c",
                "import time; time.sleep(5)",
            ),
            ".",
            100,
            TestKind.UNIT,
        )
        timed = run_tests((timeout,), self.policy(timeout), self.root)
        self.assertEqual(TestGateStatus.FAIL, timed.status)
        self.assertTrue(timed.command_results[0].timed_out)

        mutate = TestCommand(
            (
                sys.executable,
                "-c",
                "open('source.txt','w',encoding='utf-8').write('changed\\n')",
            ),
            ".",
            10_000,
            TestKind.UNIT,
        )
        mutated = run_tests((mutate,), self.policy(mutate), self.root)
        self.assertEqual(TestGateStatus.POLICY_VIOLATION, mutated.status)
        self.assertTrue(mutated.policy_violations)

    def test_empty_policy_preserves_not_run(self) -> None:
        empty_value = {
            "allowed_commands": [],
            "allowed_env_keys": [],
            "allowed_output_paths": [],
            "approved_kinds": [],
        }
        policy = TestExecutionPolicy(
            (),
            (),
            (),
            (),
            digest_value(empty_value),
        )
        result = run_tests((), policy, self.root)
        self.assertEqual(TestGateStatus.NOT_RUN, result.status)
        self.assertIsNone(result.after_snapshot)


if __name__ == "__main__":
    unittest.main()
