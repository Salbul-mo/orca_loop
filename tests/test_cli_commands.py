from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orca_loop.config import (
    ConfigurationError,
    PreflightError,
    PreflightResult,
    empty_test_policy,
    parse_run_arguments,
    permission_report_problem,
    prepare_agent_runtime,
)
from orca_loop.catalog import load_catalog
from orca_loop.runspec import build_manifest, copy_request, write_manifest
from orca_loop.escalation import ensure_user_decision_notice
from orca_loop.generation import commit_generation
from orca_loop.ledger import empty_ledger
from orca_loop.models import GateBinding, GateKind, LoopState, RunStatus
from run_loop import (
    _expand_agent_shorthand,
    _resume_argv,
    _split_command,
    _status_report,
)
from tests.test_runspec import permission_report, sample_pool
from tests.test_coordinator import initial_state


class SplitCommandTest(unittest.TestCase):
    def test_named_subcommands(self) -> None:
        for name in ("start", "resume", "status", "doctor"):
            command, rest = _split_command([name, "--run-id", "run-1"])
            self.assertEqual(name, command)
            self.assertEqual(["--run-id", "run-1"], rest)

    def test_bare_flags_default_to_start(self) -> None:
        command, rest = _split_command(["--run-id", "run-1", "--dry-run"])
        self.assertEqual("start", command)
        self.assertEqual(["--run-id", "run-1", "--dry-run"], rest)

    def test_empty_argv_defaults_to_start(self) -> None:
        self.assertEqual(("start", []), _split_command([]))


class AgentShorthandTest(unittest.TestCase):
    def test_model_and_effort_are_expanded(self) -> None:
        self.assertEqual(
            [
                "--agent-model",
                "claude_planner=sonnet",
                "--agent-effort",
                "claude_planner=medium",
            ],
            _expand_agent_shorthand(["claude_planner=sonnet/medium"]),
        )

    def test_model_only_is_allowed(self) -> None:
        self.assertEqual(
            ["--agent-model", "codex_review=gpt-5.6-terra"],
            _expand_agent_shorthand(["codex_review=gpt-5.6-terra"]),
        )

    def test_missing_separator_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            _expand_agent_shorthand(["sonnet/medium"])

    def test_empty_effort_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            _expand_agent_shorthand(["claude_planner=sonnet/"])

    def test_expansion_parses_as_real_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            request = root / "request.md"
            report = root / "permission.json"
            request.write_text("request", encoding="utf-8")
            report.write_text("{}", encoding="utf-8")
            arguments = parse_run_arguments(
                (
                    "--run-id",
                    "run-1",
                    "--request",
                    str(request),
                    "--worktree",
                    str(root),
                    "--coordinator-handle",
                    "term-1",
                    "--permission-report",
                    str(report),
                    *_expand_agent_shorthand(
                        ["claude_planner=sonnet/medium"]
                    ),
                ),
                harness_root=root,
            )
            request_value = arguments.agent_runtime_request
            assert request_value is not None
            self.assertTrue(request_value.model_overrides)
            self.assertTrue(request_value.effort_overrides)


class ResumeArgvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / "runs" / "run-1" / "control"
        self.control.mkdir(parents=True)
        self.request = self.root / "request.md"
        self.request.write_text("request", encoding="utf-8")
        self.report = self.root / "permission.json"
        self.report.write_text("{}", encoding="utf-8")
        arguments = parse_run_arguments(
            (
                "--run-id",
                "run-1",
                "--request",
                str(self.request),
                "--worktree",
                str(self.root),
                "--coordinator-handle",
                "term-coordinator",
                "--permission-report",
                str(self.report),
                "--step-timeout-ms",
                "3600000",
                "--total-timeout-ms",
                "14400000",
            ),
            harness_root=self.root,
        )
        preflight = prepare_agent_runtime(
            PreflightResult(
                arguments,
                empty_test_policy(),
                permission_report(self.root),
                "1.4.164",
                "a" * 40,
            ),
            interactive=False,
            stderr=io.StringIO(),
            catalog=load_catalog(self.root, home=self.root),
        )
        copy_path, digest = copy_request(self.control, self.request)
        write_manifest(
            self.control,
            build_manifest(
                preflight,
                request_copy=copy_path,
                request_digest=digest,
                coordinator_handle="term-coordinator",
                pool=sample_pool(),
            ),
        )

    def test_resume_needs_only_the_run_id(self) -> None:
        values = _resume_argv(["--run-id", "run-1"], self.root)
        self.assertIn("--resume", values)
        self.assertIn("--step-timeout-ms", values)
        self.assertEqual(
            "3600000",
            values[values.index("--step-timeout-ms") + 1],
        )
        arguments = parse_run_arguments(tuple(values), harness_root=self.root)
        self.assertTrue(arguments.resume)
        self.assertEqual("run-1", arguments.run_id)

    def test_drift_flag_is_forwarded(self) -> None:
        values = _resume_argv(
            ["--run-id", "run-1", "--accept-worktree-drift"],
            self.root,
        )
        arguments = parse_run_arguments(tuple(values), harness_root=self.root)
        self.assertTrue(arguments.accept_worktree_drift)

    def test_unknown_run_is_reported(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "run-missing"):
            _resume_argv(["--run-id", "run-missing"], self.root)

    def test_extra_arguments_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "does not accept"):
            _resume_argv(["--run-id", "run-1", "--worktree", "x"], self.root)


class StatusReportTest(ResumeArgvTest):
    def test_unknown_run_is_blocked(self) -> None:
        report = _status_report(self.root, "run-missing")
        self.assertEqual("BLOCKED", report["status"])

    def test_status_does_not_modify_the_run(self) -> None:
        before = sorted(
            str(item.relative_to(self.root))
            for item in self.root.rglob("*")
            if item.is_file()
        )
        _status_report(self.root, "run-1")
        after = sorted(
            str(item.relative_to(self.root))
            for item in self.root.rglob("*")
            if item.is_file()
        )
        self.assertEqual(before, after)

    def test_status_exposes_a_matching_pending_user_decision(self) -> None:
        binding = GateBinding(
            gate_id="gate-1",
            task_id="task-1",
            report_digest="sha256:" + "d" * 64,
            gate_kind=GateKind.ESCALATION,
            allowed_options=("merge", "reject", "revise_design"),
        )
        state = replace(
            initial_state(),
            state=LoopState.USER_DECISION_REQUIRED,
            status=RunStatus.BLOCKED,
            orchestration_run_id="orca-run-1",
            gate_binding=binding,
        )
        commit_generation(self.control, state, empty_ledger("run-1"))
        ensure_user_decision_notice(
            self.control,
            state=state,
            binding=binding,
            report_path=self.root / "runs" / "run-1" / "user-decision.md",
        )

        report = _status_report(self.root, "run-1")

        self.assertEqual("BLOCKED", report["status"])
        self.assertIn(
            "run is awaiting a human gate decision",
            report["blockers"],
        )
        self.assertIn("pending_user_decision", report)
        pending = report["pending_user_decision"]
        assert isinstance(pending, dict)
        self.assertEqual("gate-1", pending["gate_id"])
        self.assertEqual(
            ["merge", "reject", "revise_design"],
            pending["allowed_options"],
        )


class PermissionDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_unreadable_report_is_reported_not_raised(self) -> None:
        path = self.root / "permission-feasibility.json"
        path.write_text("{ not json", encoding="utf-8")
        problem = permission_report_problem(path, "1.4.164")
        self.assertIsNotNone(problem)
        self.assertIn("unreadable", str(problem))


if __name__ == "__main__":
    unittest.main()
