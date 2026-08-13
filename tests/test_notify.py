from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree

from orca_loop.escalation import read_user_decision_notice_delivery
from orca_loop.models import (
    DEFAULT_NOTICE_CHANNELS,
    GateKind,
    LoopState,
    NoticeChannel,
    UserDecisionNotice,
    UserDecisionNoticeDeliveryStatus,
    UserDecisionNoticeStatus,
)
from orca_loop.notify import (
    TOAST_BODY_LIMIT,
    TOAST_TITLE_LIMIT,
    TOAST_XML_ENV,
    ChannelOutcome,
    NoticeAnnouncer,
    NoticeTarget,
    PowerShellToastEmitter,
    build_toast_xml,
)
from orca_loop.orca_client import OrcaCommandError
from tests.fakes import FakeOrcaClient


def notice(
    *,
    request_id: str = "notice-1",
    run_id: str = "run-1",
    report_path: str = "C:/fixture/user-decision.md",
) -> UserDecisionNotice:
    return UserDecisionNotice(
        schema_version=1,
        request_id=request_id,
        status=UserDecisionNoticeStatus.PENDING,
        run_id=run_id,
        orchestration_run_id="orca-run-1",
        gate_id="gate-1",
        gate_kind=GateKind.ESCALATION,
        blocked_state=LoopState.USER_DECISION_REQUIRED,
        report_path=report_path,
        report_digest="sha256:" + "d" * 64,
        allowed_options=("merge", "reject"),
        reason="user decision gate created",
        created_at="2026-08-13T00:00:00+00:00",
        resolved_at=None,
    )


def target(control: Path, *, handle: str = "term_abc") -> NoticeTarget:
    return NoticeTarget(
        control_dir=control,
        worktree_selector="path:C:/fixture",
        coordinator_handle=handle,
        workspace_status="in-review",
        comment="USER DECISION REQUIRED",
    )


class RecordingToastEmitter:
    """Keeps real toasts out of the suite while recording what was rendered."""

    def __init__(
        self,
        outcome: ChannelOutcome | None = None,
    ) -> None:
        self.payloads: list[str] = []
        self.outcome = outcome or ChannelOutcome(
            UserDecisionNoticeDeliveryStatus.DELIVERED,
            None,
        )

    def emit(self, *, xml: str) -> ChannelOutcome:
        self.payloads.append(xml)
        return self.outcome


class FailingClient:
    def __init__(self, message: str = "orca unavailable") -> None:
        self.message = message
        self.calls: list[tuple[tuple[str, ...], int]] = []

    def call(self, argv, *, timeout_ms: int):
        self.calls.append((tuple(argv), timeout_ms))
        raise OrcaCommandError(self.message)


class ChannelArgvTest(unittest.TestCase):
    def test_each_orca_channel_uses_the_supported_command_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            report = control / "user-decision.md"
            report.write_text("decision", encoding="utf-8")
            client = FakeOrcaClient(lambda _argv, _timeout: {})
            emitter = RecordingToastEmitter()

            NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=DEFAULT_NOTICE_CHANNELS,
                toast_emitter=emitter,
            ).announce(notice(report_path=str(report)), target(control))

            argvs = [call[0] for call in client.calls]
            self.assertEqual(
                (
                    "worktree", "set", "--worktree", "path:C:/fixture",
                    "--workspace-status", "in-review",
                    "--comment", "USER DECISION REQUIRED",
                ),
                argvs[0],
            )
            self.assertEqual(
                ("file", "open", str(report), "--worktree", "path:C:/fixture"),
                argvs[1],
            )
            self.assertEqual(
                ("terminal", "switch", "--terminal", "term_abc"),
                argvs[2],
            )
            self.assertEqual([30_000] * 3, [call[1] for call in client.calls])
            self.assertEqual(1, len(emitter.payloads))

    def test_absent_preconditions_skip_without_calling_orca(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            client = FakeOrcaClient(lambda _argv, _timeout: {})

            delivery = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=(
                    NoticeChannel.ORCA_FILE_OPEN,
                    NoticeChannel.ORCA_TERMINAL_FOCUS,
                ),
                toast_emitter=RecordingToastEmitter(),
            ).announce(
                notice(report_path=str(control / "missing.md")),
                target(control, handle=""),
            )

            self.assertEqual([], client.calls)
            statuses = {item.channel: item.status for item in delivery.channels}
            self.assertEqual(
                UserDecisionNoticeDeliveryStatus.SKIPPED,
                statuses[NoticeChannel.ORCA_FILE_OPEN],
            )
            self.assertEqual(
                UserDecisionNoticeDeliveryStatus.SKIPPED,
                statuses[NoticeChannel.ORCA_TERMINAL_FOCUS],
            )


class AnnouncerIdempotencyTest(unittest.TestCase):
    def test_a_delivered_channel_is_not_announced_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            client = FakeOrcaClient(lambda _argv, _timeout: {})
            emitter = RecordingToastEmitter()
            announcer = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=(NoticeChannel.ORCA_BOARD, NoticeChannel.OS_TOAST),
                toast_emitter=emitter,
            )
            pending = notice()

            announcer.announce(pending, target(control))
            announcer.announce(pending, target(control))
            announcer.announce(pending, target(control))

            self.assertEqual(1, len(client.calls))
            self.assertEqual(1, len(emitter.payloads))

    def test_forced_channel_is_reannounced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            client = FakeOrcaClient(lambda _argv, _timeout: {})
            announcer = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=(NoticeChannel.ORCA_BOARD,),
            )
            pending = notice()

            announcer.announce(pending, target(control))
            announcer.announce(
                pending,
                target(control),
                force=frozenset({NoticeChannel.ORCA_BOARD}),
            )

            self.assertEqual(2, len(client.calls))

    def test_a_failed_channel_is_retried_on_the_next_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            failing = FailingClient()
            announcer = NoticeAnnouncer(
                failing,  # type: ignore[arg-type]
                channels=(NoticeChannel.ORCA_BOARD,),
            )
            pending = notice()

            announcer.announce(pending, target(control))
            announcer.announce(pending, target(control))

            self.assertEqual(2, len(failing.calls))

    def test_evidence_from_another_request_is_not_inherited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            client = FakeOrcaClient(lambda _argv, _timeout: {})
            announcer = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=(NoticeChannel.ORCA_BOARD,),
            )

            announcer.announce(notice(request_id="notice-1"), target(control))
            delivery = announcer.announce(
                notice(request_id="notice-2"),
                target(control),
            )

            self.assertEqual(2, len(client.calls))
            self.assertEqual("notice-2", delivery.request_id)


class AnnouncerFailureIsolationTest(unittest.TestCase):
    def test_one_failing_channel_does_not_block_the_others(self) -> None:
        def handler(argv, _timeout):
            if argv[:2] == ("file", "open"):
                raise OrcaCommandError("editor unavailable")
            return {}

        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            report = control / "user-decision.md"
            report.write_text("decision", encoding="utf-8")
            client = FakeOrcaClient(handler)

            delivery = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=(
                    NoticeChannel.ORCA_FILE_OPEN,
                    NoticeChannel.ORCA_TERMINAL_FOCUS,
                ),
            ).announce(notice(report_path=str(report)), target(control))

            statuses = {item.channel: item.status for item in delivery.channels}
            self.assertEqual(
                UserDecisionNoticeDeliveryStatus.FAILED,
                statuses[NoticeChannel.ORCA_FILE_OPEN],
            )
            self.assertEqual(
                UserDecisionNoticeDeliveryStatus.DELIVERED,
                statuses[NoticeChannel.ORCA_TERMINAL_FOCUS],
            )

    def test_every_channel_failing_still_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            emitter = RecordingToastEmitter(
                ChannelOutcome(
                    UserDecisionNoticeDeliveryStatus.FAILED,
                    "toast unavailable",
                )
            )

            delivery = NoticeAnnouncer(
                FailingClient(),  # type: ignore[arg-type]
                channels=(NoticeChannel.ORCA_BOARD, NoticeChannel.OS_TOAST),
                toast_emitter=emitter,
            ).announce(notice(), target(control))

            self.assertEqual(2, len(delivery.channels))
            self.assertTrue(
                all(
                    item.status is UserDecisionNoticeDeliveryStatus.FAILED
                    for item in delivery.channels
                )
            )
            self.assertEqual(delivery, read_user_decision_notice_delivery(control))

    def test_delivery_evidence_is_written_once_per_announcement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            client = FakeOrcaClient(lambda _argv, _timeout: {})
            announcer = NoticeAnnouncer(
                client,  # type: ignore[arg-type]
                channels=DEFAULT_NOTICE_CHANNELS,
                toast_emitter=RecordingToastEmitter(),
            )

            with mock.patch(
                "orca_loop.notify.write_user_decision_notice_delivery",
                wraps=__import__(
                    "orca_loop.notify",
                    fromlist=["write_user_decision_notice_delivery"],
                ).write_user_decision_notice_delivery,
            ) as writer:
                announcer.announce(notice(), target(control))

            self.assertEqual(1, writer.call_count)


class ToastPayloadTest(unittest.TestCase):
    def test_hostile_notice_text_stays_well_formed(self) -> None:
        hostile = notice(run_id='run"><script>&x')

        xml = build_toast_xml(hostile)

        parsed = ElementTree.fromstring(xml)
        texts = [element.text or "" for element in parsed.iter("text")]
        self.assertEqual(2, len(texts))
        self.assertIn('run"><script>&x', texts[1])
        self.assertNotIn("<script>", xml)

    def test_control_characters_are_normalized_and_bounded(self) -> None:
        hostile = notice(run_id="a\nb\tc" + "z" * 500)

        xml = build_toast_xml(hostile)

        parsed = ElementTree.fromstring(xml)
        title, body = (element.text or "" for element in parsed.iter("text"))
        self.assertNotIn("\n", body)
        self.assertNotIn("\t", body)
        self.assertLessEqual(len(title), TOAST_TITLE_LIMIT)
        self.assertLessEqual(len(body), TOAST_BODY_LIMIT)

    def test_payload_names_the_gate_and_its_options(self) -> None:
        xml = build_toast_xml(notice())

        parsed = ElementTree.fromstring(xml)
        title, body = (element.text or "" for element in parsed.iter("text"))
        self.assertIn("USER DECISION REQUIRED", title)
        self.assertIn("gate=gate-1", body)
        self.assertIn("options=merge,reject", body)
        self.assertIn("report=user-decision.md", body)


class PowerShellToastEmitterTest(unittest.TestCase):
    def test_notice_text_never_reaches_argv(self) -> None:
        """The payload travels by environment so run names cannot reach argv."""
        hostile = notice(run_id="run-1'; Remove-Item C:\\ -Recurse; #")
        xml = build_toast_xml(hostile)
        completed = mock.Mock(returncode=0, stderr=b"")

        with mock.patch("orca_loop.notify.platform.system", return_value="Windows"), \
             mock.patch(
                 "orca_loop.notify.shutil.which",
                 return_value="C:/Windows/powershell.exe",
             ), \
             mock.patch(
                 "orca_loop.notify.subprocess.run",
                 return_value=completed,
             ) as runner:
            outcome = PowerShellToastEmitter().emit(xml=xml)

        self.assertEqual(
            UserDecisionNoticeDeliveryStatus.DELIVERED,
            outcome.status,
        )
        argv = runner.call_args.args[0]
        self.assertNotIn("Remove-Item", " ".join(argv))
        self.assertNotIn("run-1", " ".join(argv))
        self.assertFalse(runner.call_args.kwargs["shell"])
        self.assertEqual(xml, runner.call_args.kwargs["env"][TOAST_XML_ENV])

    def test_non_windows_platforms_are_skipped(self) -> None:
        with mock.patch("orca_loop.notify.platform.system", return_value="Linux"):
            outcome = PowerShellToastEmitter().emit(xml="<toast/>")

        self.assertEqual(UserDecisionNoticeDeliveryStatus.SKIPPED, outcome.status)
        self.assertIn("platform", outcome.detail or "")

    def test_missing_powershell_is_skipped(self) -> None:
        with mock.patch("orca_loop.notify.platform.system", return_value="Windows"), \
             mock.patch("orca_loop.notify.shutil.which", return_value=None):
            outcome = PowerShellToastEmitter().emit(xml="<toast/>")

        self.assertEqual(UserDecisionNoticeDeliveryStatus.SKIPPED, outcome.status)

    def test_a_timeout_is_reported_as_failure(self) -> None:
        import subprocess

        with mock.patch("orca_loop.notify.platform.system", return_value="Windows"), \
             mock.patch(
                 "orca_loop.notify.shutil.which",
                 return_value="C:/Windows/powershell.exe",
             ), \
             mock.patch(
                 "orca_loop.notify.subprocess.run",
                 side_effect=subprocess.TimeoutExpired("powershell", 10),
             ):
            outcome = PowerShellToastEmitter().emit(xml="<toast/>")

        self.assertEqual(UserDecisionNoticeDeliveryStatus.FAILED, outcome.status)
        self.assertIn("timed out", outcome.detail or "")

    def test_a_nonzero_exit_is_reported_as_failure(self) -> None:
        completed = mock.Mock(returncode=1, stderr=b"toast notifier unavailable")

        with mock.patch("orca_loop.notify.platform.system", return_value="Windows"), \
             mock.patch(
                 "orca_loop.notify.shutil.which",
                 return_value="C:/Windows/powershell.exe",
             ), \
             mock.patch("orca_loop.notify.subprocess.run", return_value=completed):
            outcome = PowerShellToastEmitter().emit(xml="<toast/>")

        self.assertEqual(UserDecisionNoticeDeliveryStatus.FAILED, outcome.status)
        self.assertIn("toast notifier unavailable", outcome.detail or "")


if __name__ == "__main__":
    unittest.main()
