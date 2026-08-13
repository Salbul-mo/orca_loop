"""Best-effort operator notification for pending user decision gates.

Every channel here is advisory.  ``user-decision-request.json`` stays the sole
authority for whether a decision is pending, so a channel that fails is
recorded and stepped over rather than allowed to stall the run.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from xml.sax.saxutils import escape

from .escalation import (
    DecisionReportError,
    read_user_decision_notice_delivery,
    write_user_decision_notice_delivery,
)
from .models import (
    NoticeChannel,
    NoticeChannelDelivery,
    UserDecisionNotice,
    UserDecisionNoticeDelivery,
    UserDecisionNoticeDeliveryStatus,
)
from .orca_client import OrcaClient, OrcaCommandError


ORCA_CHANNEL_TIMEOUT_MS = 30_000
TOAST_TIMEOUT_MS = 10_000
TOAST_TITLE_LIMIT = 120
TOAST_BODY_LIMIT = 300
CHANNEL_DETAIL_LIMIT = 2_000
TOAST_XML_ENV = "ORCA_LOOP_TOAST_XML"

# The payload is handed over through the environment so that no notice-derived
# text ever reaches argv or the script body.  Run identifiers originate from
# user-supplied run names, so they are treated as untrusted here.
_TOAST_SCRIPT = (
    "$ErrorActionPreference='Stop'; "
    "$AppId='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe'; "
    "[Windows.UI.Notifications.ToastNotificationManager,"
    "Windows.UI.Notifications,ContentType=WindowsRuntime] > $null; "
    "[Windows.Data.Xml.Dom.XmlDocument,"
    "Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime] > $null; "
    "$d = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    f"$d.LoadXml($env:{TOAST_XML_ENV}); "
    "[Windows.UI.Notifications.ToastNotificationManager]"
    "::CreateToastNotifier($AppId).Show("
    "(New-Object Windows.UI.Notifications.ToastNotification $d))"
)


class NoticeDeliveryError(RuntimeError):
    """Raised only when durable delivery evidence cannot be persisted."""


@dataclass(frozen=True)
class ChannelOutcome:
    status: UserDecisionNoticeDeliveryStatus
    detail: str | None


@dataclass(frozen=True)
class NoticeTarget:
    """Everything a channel may need, resolved once by the caller."""

    control_dir: Path
    worktree_selector: str
    coordinator_handle: str
    workspace_status: str
    comment: str


class ToastEmitter(Protocol):
    def emit(self, *, xml: str) -> ChannelOutcome: ...


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _bounded_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _delivered() -> ChannelOutcome:
    return ChannelOutcome(UserDecisionNoticeDeliveryStatus.DELIVERED, None)


def _failed(detail: str) -> ChannelOutcome:
    return ChannelOutcome(
        UserDecisionNoticeDeliveryStatus.FAILED,
        _bounded_line(detail, CHANNEL_DETAIL_LIMIT) or "channel failed",
    )


def _skipped(detail: str) -> ChannelOutcome:
    return ChannelOutcome(UserDecisionNoticeDeliveryStatus.SKIPPED, detail)


def build_toast_xml(notice: UserDecisionNotice) -> str:
    """Render a toast payload that stays well-formed for any notice content."""
    title = _bounded_line(
        f"Orca Loop: USER DECISION REQUIRED ({notice.gate_kind.value})",
        TOAST_TITLE_LIMIT,
    )
    body = _bounded_line(
        f"run={notice.run_id} | gate={notice.gate_id} | "
        f"options={','.join(notice.allowed_options)} | "
        f"report={Path(notice.report_path).name}",
        TOAST_BODY_LIMIT,
    )
    return (
        '<toast scenario="reminder"><visual>'
        '<binding template="ToastGeneric">'
        f"<text>{escape(title)}</text>"
        f"<text>{escape(body)}</text>"
        "</binding></visual>"
        '<audio src="ms-winsoundevent:Notification.Looping.Alarm2"/>'
        "</toast>"
    )


class PowerShellToastEmitter:
    """Emit a Windows toast through a bounded, fixed-argv PowerShell call."""

    def emit(self, *, xml: str) -> ChannelOutcome:
        if not xml:
            return _skipped("toast payload is empty")
        if platform.system() != "Windows":
            return _skipped("platform does not support OS toast")
        executable = shutil.which("powershell")
        if executable is None:
            return _skipped("powershell is not on PATH")
        environment = dict(os.environ)
        environment[TOAST_XML_ENV] = xml
        argv = (
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _TOAST_SCRIPT,
        )
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                env=environment,
                capture_output=True,
                timeout=TOAST_TIMEOUT_MS / 1000,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _failed("toast emission timed out")
        except OSError as exc:
            return _failed(f"toast emission failed: {exc}")
        if completed.returncode != 0:
            return _failed(
                "toast emission exited "
                f"{completed.returncode}: "
                f"{completed.stderr.decode('utf-8', errors='replace')}"
            )
        return _delivered()


class NoticeAnnouncer:
    """Run the configured channels once per notice and record the evidence."""

    def __init__(
        self,
        client: OrcaClient,
        *,
        channels: tuple[NoticeChannel, ...],
        toast_emitter: ToastEmitter | None = None,
    ) -> None:
        self.client = client
        self.channels = channels
        self._toast_emitter = toast_emitter

    def announce(
        self,
        notice: UserDecisionNotice,
        target: NoticeTarget,
        *,
        force: frozenset[NoticeChannel] = frozenset(),
    ) -> UserDecisionNoticeDelivery:
        if not notice.request_id:
            raise NoticeDeliveryError("notice delivery requires a request id")
        if not target.worktree_selector:
            raise NoticeDeliveryError("notice delivery requires a worktree selector")
        merged = self._existing_channels(notice, target)
        for channel in self.channels:
            recorded = merged.get(channel)
            if (
                recorded is not None
                and recorded.status is UserDecisionNoticeDeliveryStatus.DELIVERED
                and channel not in force
            ):
                continue
            outcome = self._dispatch(channel, notice, target)
            merged[channel] = NoticeChannelDelivery(
                channel=channel,
                status=outcome.status,
                attempted_at=_utc_now(),
                detail=outcome.detail,
            )
        try:
            return write_user_decision_notice_delivery(
                target.control_dir,
                request_id=notice.request_id,
                channels=tuple(merged.values()),
            )
        except DecisionReportError as exc:
            raise NoticeDeliveryError(
                f"failed to persist notice delivery evidence: {exc}"
            ) from exc

    def _existing_channels(
        self,
        notice: UserDecisionNotice,
        target: NoticeTarget,
    ) -> dict[NoticeChannel, NoticeChannelDelivery]:
        """Load prior evidence, but never inherit another gate's evidence."""
        try:
            existing = read_user_decision_notice_delivery(target.control_dir)
        except DecisionReportError:
            return {}
        if existing is None or existing.request_id != notice.request_id:
            return {}
        return {item.channel: item for item in existing.channels}

    def _dispatch(
        self,
        channel: NoticeChannel,
        notice: UserDecisionNotice,
        target: NoticeTarget,
    ) -> ChannelOutcome:
        try:
            if channel is NoticeChannel.ORCA_BOARD:
                return self._announce_board(target)
            if channel is NoticeChannel.ORCA_FILE_OPEN:
                return self._announce_file_open(notice, target)
            if channel is NoticeChannel.ORCA_TERMINAL_FOCUS:
                return self._announce_terminal_focus(target)
            return self._announce_os_toast(notice)
        except OrcaCommandError as exc:
            return _failed(str(exc))

    def _announce_board(self, target: NoticeTarget) -> ChannelOutcome:
        if not target.workspace_status or not target.comment:
            return _skipped("board metadata is incomplete")
        self.client.call(
            (
                "worktree",
                "set",
                "--worktree",
                target.worktree_selector,
                "--workspace-status",
                target.workspace_status,
                "--comment",
                target.comment,
            ),
            timeout_ms=ORCA_CHANNEL_TIMEOUT_MS,
        )
        return _delivered()

    def _announce_file_open(
        self,
        notice: UserDecisionNotice,
        target: NoticeTarget,
    ) -> ChannelOutcome:
        report = Path(notice.report_path)
        if not notice.report_path or not report.is_file():
            return _skipped("decision report is not present")
        self.client.call(
            (
                "file",
                "open",
                str(report),
                "--worktree",
                target.worktree_selector,
            ),
            timeout_ms=ORCA_CHANNEL_TIMEOUT_MS,
        )
        return _delivered()

    def _announce_terminal_focus(self, target: NoticeTarget) -> ChannelOutcome:
        if not target.coordinator_handle:
            return _skipped("coordinator handle is unavailable")
        self.client.call(
            ("terminal", "switch", "--terminal", target.coordinator_handle),
            timeout_ms=ORCA_CHANNEL_TIMEOUT_MS,
        )
        return _delivered()

    def _announce_os_toast(self, notice: UserDecisionNotice) -> ChannelOutcome:
        emitter = self._toast_emitter
        if emitter is None:
            emitter = PowerShellToastEmitter()
        try:
            return emitter.emit(xml=build_toast_xml(notice))
        except OSError as exc:
            return _failed(f"toast emitter failed: {exc}")
