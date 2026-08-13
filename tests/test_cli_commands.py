from __future__ import annotations

import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orca_loop.config import (
    ConfigurationError,
    PreflightError,
    PreflightResult,
    empty_test_policy,
    parse_notice_channels,
    parse_run_arguments,
    permission_report_problem,
    prepare_agent_runtime,
)
from orca_loop.catalog import load_catalog
from orca_loop.runspec import build_manifest, copy_request, write_manifest
from orca_loop.escalation import (
    ensure_user_decision_notice,
    write_user_decision_notice_delivery,
)
from orca_loop.generation import commit_generation
from orca_loop.ledger import empty_ledger
from orca_loop.models import (
    DEFAULT_NOTICE_CHANNELS,
    CoordinatorState,
    GateBinding,
    GateKind,
    LoopState,
    NoticeChannel,
    NoticeChannelDelivery,
    RunStatus,
    UserDecisionNoticeDeliveryStatus,
)
from orca_loop.failure import StopClass, StopEvent, record_stop_event
from orca_loop.locking import LockInfo
from orca_loop.orca_client import OrcaCommandError
from run_loop import (
    EXIT_PREFLIGHT,
    _expand_agent_shorthand,
    _force_fail,
    _resume_argv,
    _run_verdict,
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

    def test_status_exposes_per_channel_delivery_evidence(self) -> None:
        binding = GateBinding(
            gate_id="gate-1",
            task_id="task-1",
            report_digest="sha256:" + "d" * 64,
            gate_kind=GateKind.ESCALATION,
            allowed_options=("merge", "reject"),
        )
        state = replace(
            initial_state(),
            state=LoopState.USER_DECISION_REQUIRED,
            status=RunStatus.BLOCKED,
            orchestration_run_id="orca-run-1",
            gate_binding=binding,
        )
        commit_generation(self.control, state, empty_ledger("run-1"))
        pending = ensure_user_decision_notice(
            self.control,
            state=state,
            binding=binding,
            report_path=self.root / "runs" / "run-1" / "user-decision.md",
        )
        write_user_decision_notice_delivery(
            self.control,
            request_id=pending.request_id,
            channels=(
                NoticeChannelDelivery(
                    channel=NoticeChannel.OS_TOAST,
                    status=UserDecisionNoticeDeliveryStatus.FAILED,
                    attempted_at="2026-08-13T00:00:00+00:00",
                    detail="toast emission timed out",
                ),
                NoticeChannelDelivery(
                    channel=NoticeChannel.ORCA_BOARD,
                    status=UserDecisionNoticeDeliveryStatus.DELIVERED,
                    attempted_at="2026-08-13T00:00:00+00:00",
                    detail=None,
                ),
            ),
        )

        report = _status_report(self.root, "run-1")

        delivery = report["user_decision_notice_delivery"]
        assert isinstance(delivery, dict)
        self.assertEqual(pending.request_id, delivery["request_id"])
        channels = delivery["channels"]
        assert isinstance(channels, list)
        # Channels report in declaration order, not in the order they were written.
        self.assertEqual(
            ["ORCA_BOARD", "OS_TOAST"],
            [item["channel"] for item in channels],
        )
        self.assertEqual("FAILED", channels[1]["status"])
        self.assertEqual("toast emission timed out", channels[1]["detail"])
        self.assertNotIn("notice_problems", report)

    def test_status_migrates_a_legacy_delivery_record(self) -> None:
        binding = GateBinding(
            gate_id="gate-1",
            task_id="task-1",
            report_digest="sha256:" + "d" * 64,
            gate_kind=GateKind.ESCALATION,
            allowed_options=("merge", "reject"),
        )
        state = replace(
            initial_state(),
            state=LoopState.USER_DECISION_REQUIRED,
            status=RunStatus.BLOCKED,
            orchestration_run_id="orca-run-1",
            gate_binding=binding,
        )
        commit_generation(self.control, state, empty_ledger("run-1"))
        pending = ensure_user_decision_notice(
            self.control,
            state=state,
            binding=binding,
            report_path=self.root / "runs" / "run-1" / "user-decision.md",
        )
        (self.control / "user-decision-notice-delivery.json").write_text(
            json.dumps(
                {
                    "attempted_at": "2026-08-13T07:39:53.947899+00:00",
                    "error": None,
                    "request_id": pending.request_id,
                    "schema_version": 1,
                    "status": "DELIVERED",
                }
            ),
            encoding="utf-8",
        )

        report = _status_report(self.root, "run-1")

        delivery = report["user_decision_notice_delivery"]
        assert isinstance(delivery, dict)
        self.assertEqual(
            [{"channel": "ORCA_BOARD", "status": "DELIVERED",
              "attempted_at": "2026-08-13T07:39:53.947899+00:00",
              "detail": None}],
            delivery["channels"],
        )
        self.assertNotIn("notice_problems", report)


class NoticeChannelConfigTest(unittest.TestCase):
    def test_default_selection_is_every_channel(self) -> None:
        self.assertEqual(DEFAULT_NOTICE_CHANNELS, parse_notice_channels(None))

    def test_named_channels_keep_their_order_and_drop_repeats(self) -> None:
        self.assertEqual(
            (NoticeChannel.OS_TOAST, NoticeChannel.ORCA_BOARD),
            parse_notice_channels("os-toast,board,os-toast"),
        )

    def test_none_disables_every_channel(self) -> None:
        self.assertEqual((), parse_notice_channels("none"))

    def test_unknown_channel_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "unknown notice channel"):
            parse_notice_channels("board,sms")

    def test_empty_channel_names_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must not be empty"):
            parse_notice_channels("board,,os-toast")


class RunVerdictTest(unittest.TestCase):
    def state_in(self, value: LoopState) -> CoordinatorState:
        return replace(initial_state(), state=value)

    def live_lock(self, run_id: str = "run-1") -> LockInfo:
        return LockInfo(
            path=Path("lock"),
            readable=True,
            run_id=run_id,
            pid=1234,
            alive=True,
            age_seconds=1.0,
            worktree="C:/fixture",
        )

    def stop(self, *, resumable: bool) -> StopEvent:
        return StopEvent(
            classification=(
                StopClass.INTERRUPTED if resumable else StopClass.TERMINAL
            ),
            exception="OrcaCommandError",
            reason="orca is unreachable",
            generation=0,
            state="IMPLEMENT",
            resumable=resumable,
            state_committed=False,
            recorded_at="2026-08-13T00:00:00+00:00",
        )

    def test_a_finished_run_reads_as_completed(self) -> None:
        for value in (LoopState.READY_FOR_MERGE, LoopState.REJECTED):
            with self.subTest(state=value):
                self.assertEqual(
                    "COMPLETED",
                    _run_verdict(self.state_in(value), None, None, "run-1"),
                )

    def test_a_gate_reads_as_blocked_on_user(self) -> None:
        for value in (
            LoopState.HUMAN_GATE,
            LoopState.USER_DECISION_REQUIRED,
        ):
            with self.subTest(state=value):
                self.assertEqual(
                    "BLOCKED_ON_USER",
                    _run_verdict(self.state_in(value), None, None, "run-1"),
                )

    def test_a_failed_run_reads_as_terminal(self) -> None:
        self.assertEqual(
            "STOPPED_TERMINAL",
            _run_verdict(self.state_in(LoopState.FAILED), None, None, "run-1"),
        )

    def test_a_live_lock_reads_as_running(self) -> None:
        self.assertEqual(
            "RUNNING",
            _run_verdict(
                self.state_in(LoopState.IMPLEMENT),
                None,
                self.live_lock(),
                "run-1",
            ),
        )

    def test_in_progress_without_a_lock_reads_as_stopped(self) -> None:
        """The gap this whole boundary exists to close: a dead coordinator."""
        self.assertEqual(
            "STOPPED_RESUMABLE",
            _run_verdict(
                self.state_in(LoopState.IMPLEMENT),
                None,
                None,
                "run-1",
            ),
        )

    def test_a_foreign_lock_does_not_mean_this_run_is_running(self) -> None:
        self.assertEqual(
            "STOPPED_RESUMABLE",
            _run_verdict(
                self.state_in(LoopState.IMPLEMENT),
                None,
                self.live_lock("other-run"),
                "run-1",
            ),
        )

    def test_a_resumable_stop_outranks_a_stale_live_lock(self) -> None:
        self.assertEqual(
            "STOPPED_RESUMABLE",
            _run_verdict(
                self.state_in(LoopState.IMPLEMENT),
                self.stop(resumable=True),
                self.live_lock(),
                "run-1",
            ),
        )


class StatusStopEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / "runs" / "run-1" / "control"
        self.control.mkdir(parents=True)

    def commit_state(self, state: CoordinatorState) -> None:
        commit_generation(self.control, state, empty_ledger("run-1"))

    def test_a_stop_at_the_current_generation_is_reported(self) -> None:
        state = replace(
            initial_state(),
            state=LoopState.IMPLEMENT,
            orchestration_run_id="orca-run-1",
        )
        self.commit_state(state)
        record_stop_event(
            self.control,
            exc=OrcaCommandError("orca is unreachable"),
            classification=StopClass.INTERRUPTED,
            generation=state.generation,
            state=state.state,
            state_committed=False,
        )

        report = _status_report(self.root, "run-1")

        self.assertIn("stop", report)
        stop = report["stop"]
        assert isinstance(stop, dict)
        self.assertEqual("OrcaCommandError", stop["exception"])
        self.assertTrue(stop["resumable"])
        self.assertEqual("STOPPED_RESUMABLE", report["verdict"])

    def test_a_stop_from_an_earlier_generation_is_history(self) -> None:
        """Resuming past a stop must retire it without deleting the evidence."""
        state = replace(
            initial_state(),
            state=LoopState.IMPLEMENT,
            orchestration_run_id="orca-run-1",
        )
        self.commit_state(state)
        record_stop_event(
            self.control,
            exc=OrcaCommandError("older failure"),
            classification=StopClass.INTERRUPTED,
            generation=state.generation - 1,
            state=state.state,
            state_committed=False,
        )

        report = _status_report(self.root, "run-1")

        self.assertNotIn("stop", report)

    def test_a_run_without_any_stop_still_gets_a_verdict(self) -> None:
        state = replace(
            initial_state(),
            state=LoopState.READY_FOR_MERGE,
            orchestration_run_id="orca-run-1",
        )
        self.commit_state(state)

        report = _status_report(self.root, "run-1")

        self.assertNotIn("stop", report)
        self.assertEqual("COMPLETED", report["verdict"])


class ForceFailTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.control = self.root / "runs" / "run-1" / "control"
        self.control.mkdir(parents=True)

    def test_an_unknown_run_is_refused(self) -> None:
        code = _force_fail(self.root, ["--run-id", "missing", "--reason", "x"])
        self.assertEqual(EXIT_PREFLIGHT, code)

    def test_an_empty_reason_is_refused(self) -> None:
        code = _force_fail(self.root, ["--run-id", "run-1", "--reason", "  "])
        self.assertEqual(EXIT_PREFLIGHT, code)

    def test_an_already_finished_run_is_refused(self) -> None:
        commit_generation(
            self.control,
            replace(initial_state(), state=LoopState.REJECTED),
            empty_ledger("run-1"),
        )

        code = _force_fail(
            self.root,
            ["--run-id", "run-1", "--reason", "operator stopped it"],
        )

        self.assertEqual(EXIT_PREFLIGHT, code)


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
