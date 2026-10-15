from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from .contracts import ContractViolationError, parse_human_decision
from .locking import process_identity
from .models import OrcaResponse
from .orca_client import OrcaCommandError, OrcaProtocolError


LOCAL_RUNTIME_VERSION = "1.4.159"


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _option(argv: Sequence[str], name: str) -> str | None:
    try:
        index = argv.index(name)
    except ValueError:
        return None
    if index + 1 >= len(argv):
        raise OrcaCommandError(f"missing value for {name}")
    return argv[index + 1]


class LocalCoordinatorClient:
    """Orca transport replacement used by the Web GPT-only loop.

    Contracts, snapshots, provenance validation, consensus and human gates stay
    in the existing coordinator. This class replaces only terminal/task/message
    transport with local logical terminals and a filesystem completion queue.
    """

    executable = "<local-runtime>"

    def __init__(
        self,
        *,
        cwd: Path,
        run_id: str,
        coordinator_handle: str,
    ) -> None:
        self.cwd = cwd.resolve()
        self.run_id = run_id
        self.coordinator_handle = coordinator_handle
        self.runtime_root = (
            self.cwd / "runs" / ".local-runtime" / run_id
        ).resolve()
        self.completion_dir = self.runtime_root / "messages"
        self.gate_dir = self.runtime_root / "gates"
        self.notification_dir = self.runtime_root / "notifications"
        self.notification_ack_dir = self.runtime_root / "notification-acks"
        self.state_path = self.runtime_root / "state.json"
        self._worker_processes: dict[str, subprocess.Popen[bytes]] = {}

    def _initial_state(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "terminals": {
                self.coordinator_handle: {
                    "handle": self.coordinator_handle,
                    "kind": "coordinator",
                }
            },
            "tasks": {},
            "dispatches": {},
            "worker_processes": {},
            "terminal_counter": 0,
        }

    def _response(self, value: dict[str, object]) -> OrcaResponse:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return OrcaResponse(raw, raw, "")

    def _load_state(self) -> dict[str, object]:
        if not self.state_path.exists():
            return self._initial_state()
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrcaCommandError("local runtime state is unreadable") from exc
        if not isinstance(value, dict):
            raise OrcaCommandError("local runtime state must be an object")
        terminals = value.setdefault("terminals", {})
        value.setdefault("worker_processes", {})
        if isinstance(terminals, dict):
            terminals.setdefault(
                self.coordinator_handle,
                {
                    "handle": self.coordinator_handle,
                    "kind": "coordinator",
                },
            )
        return value

    def _save_state(self, value: dict[str, object]) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    @staticmethod
    def _write_json_atomic(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    @staticmethod
    def _terminate_pid_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(pid), "/T", "/F"),
                shell=False,
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    def _remove_worker_process_record(self, terminal_handle: str) -> None:
        state = self._load_state()
        records = state.setdefault("worker_processes", {})
        if not isinstance(records, dict):
            raise OrcaCommandError("local worker process state is invalid")
        if records.pop(terminal_handle, None) is not None:
            self._save_state(state)

    def recover_orphan_worker_processes(self) -> tuple[str, ...]:
        """Kill recorded workers only after the caller owns the run lock."""

        state = self._load_state()
        records = state.setdefault("worker_processes", {})
        if not isinstance(records, dict):
            raise OrcaCommandError("local worker process state is invalid")
        recovered: list[str] = []
        changed = False
        for terminal_handle, raw in tuple(records.items()):
            if not isinstance(terminal_handle, str) or not isinstance(raw, dict):
                records.pop(terminal_handle, None)
                changed = True
                continue
            pid = raw.get("pid")
            stored_identity = raw.get("process_identity")
            if (
                not isinstance(pid, int)
                or isinstance(pid, bool)
                or pid <= 0
                or not isinstance(stored_identity, str)
                or not stored_identity
            ):
                records.pop(terminal_handle, None)
                changed = True
                continue
            current_identity = process_identity(pid)
            if current_identity is None or current_identity != stored_identity:
                records.pop(terminal_handle, None)
                changed = True
                continue
            self._terminate_pid_tree(pid)
            deadline = time.monotonic() + 5
            while process_identity(pid) == stored_identity and time.monotonic() < deadline:
                time.sleep(0.05)
            if process_identity(pid) == stored_identity:
                raise OrcaCommandError(
                    f"failed to terminate orphan worker process {pid} for {terminal_handle}"
                )
            records.pop(terminal_handle, None)
            recovered.append(terminal_handle)
            changed = True
        if changed:
            self._save_state(state)
        return tuple(recovered)

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            process.wait()
            return
        if os.name == "nt":
            subprocess.run(
                ("taskkill", "/PID", str(process.pid), "/T", "/F"),
                shell=False,
                capture_output=True,
                check=False,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def finish_worker_process(
        self,
        terminal_handle: str,
        *,
        terminate: bool = False,
    ) -> None:
        process = self._worker_processes.get(terminal_handle)
        if process is None:
            return
        try:
            if terminate:
                self._terminate_process_tree(process)
            else:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._terminate_process_tree(process)
        finally:
            self._worker_processes.pop(terminal_handle, None)
            self._remove_worker_process_record(terminal_handle)

    def _finish_message_worker(self, value: dict[str, object]) -> None:
        message = value.get("message", value)
        if not isinstance(message, dict):
            return
        worker = message.get("from")
        if isinstance(worker, str) and worker:
            self.finish_worker_process(worker)

    def _terminal(self, argv: tuple[str, ...]) -> OrcaResponse:
        action = argv[1] if len(argv) > 1 else ""
        state = self._load_state()
        terminals = state.get("terminals")
        if not isinstance(terminals, dict):
            raise OrcaCommandError("local terminal state is invalid")
        if action == "create":
            worktree = _option(argv, "--worktree")
            if not worktree:
                raise OrcaCommandError("local terminal create requires worktree")
            counter = int(state.get("terminal_counter", 0)) + 1
            state["terminal_counter"] = counter
            handle = f"local-{self.run_id}-worker-{counter}"
            terminal = {
                "handle": handle,
                "worktreeId": worktree,
                "tabId": f"local-tab-{counter}",
                "leafId": f"local-leaf-{counter}",
                "title": _option(argv, "--title") or "",
            }
            terminals[handle] = terminal
            self._save_state(state)
            return self._response({"terminal": terminal})
        if action == "show":
            handle = _option(argv, "--terminal")
            if not handle or handle not in terminals:
                raise OrcaCommandError(
                    f"local terminal does not exist: {handle or '<missing>'}"
                )
            terminal = terminals[handle]
            return self._response(
                {
                    "terminal": (
                        terminal
                        if isinstance(terminal, dict)
                        else {"handle": handle}
                    )
                }
            )
        if action == "send":
            handle = _option(argv, "--terminal")
            text = _option(argv, "--text")
            if not handle or handle not in terminals:
                raise OrcaCommandError("local terminal send target is unknown")
            if not text:
                raise OrcaCommandError("local terminal send text is empty")
            shell = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
            executable = (
                str(shell)
                if shell.is_file()
                else (shutil.which("pwsh") or shutil.which("powershell"))
            )
            if not executable:
                raise OrcaCommandError(
                    "PowerShell is unavailable for local worker launch"
                )
            creationflags = (
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            )
            existing = self._worker_processes.get(handle)
            if existing is not None:
                if existing.poll() is None:
                    raise OrcaCommandError(
                        "local terminal already has a running worker process"
                    )
                existing.wait()
                self._worker_processes.pop(handle, None)
                self._remove_worker_process_record(handle)
            process = subprocess.Popen(
                (
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    text,
                ),
                cwd=self.cwd,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            self._worker_processes[handle] = process
            identity = process_identity(process.pid)
            if identity is None:
                self._terminate_process_tree(process)
                self._worker_processes.pop(handle, None)
                raise OrcaCommandError(
                    "cannot establish local worker process identity"
                )
            records = state.setdefault("worker_processes", {})
            if not isinstance(records, dict):
                self._terminate_process_tree(process)
                self._worker_processes.pop(handle, None)
                raise OrcaCommandError("local worker process state is invalid")
            terminal = terminals.get(handle)
            dispatch_id = (
                terminal.get("lastDispatchId")
                if isinstance(terminal, dict)
                else None
            )
            records[handle] = {
                "pid": process.pid,
                "process_identity": identity,
                "dispatch_id": dispatch_id,
                "started_at_ns": time.time_ns(),
            }
            try:
                self._save_state(state)
            except Exception:
                self._terminate_process_tree(process)
                self._worker_processes.pop(handle, None)
                raise
            return self._response({"sent": True, "terminal": handle})
        raise OrcaCommandError(f"unsupported local terminal action: {action}")

    def _task_create(self, argv: tuple[str, ...]) -> OrcaResponse:
        state = self._load_state()
        tasks = state.setdefault("tasks", {})
        if not isinstance(tasks, dict):
            raise OrcaCommandError("local task state is invalid")
        task_id = "local-task-" + uuid.uuid4().hex
        tasks[task_id] = {
            "id": task_id,
            "title": _option(argv, "--task-title") or "",
            "displayName": _option(argv, "--display-name") or "",
            "spec": _option(argv, "--spec") or "",
        }
        self._save_state(state)
        return self._response({"task": {"id": task_id}})

    def _dispatch(self, argv: tuple[str, ...]) -> OrcaResponse:
        task_id = _option(argv, "--task")
        target = _option(argv, "--to")
        source = _option(argv, "--from")
        state = self._load_state()
        tasks = state.get("tasks")
        terminals = state.get("terminals")
        if not isinstance(tasks, dict) or not task_id or task_id not in tasks:
            raise OrcaCommandError(
                "local dispatch references an unknown task"
            )
        if (
            not isinstance(terminals, dict)
            or not target
            or target not in terminals
        ):
            raise OrcaCommandError(
                "local dispatch references an unknown worker"
            )
        if source != self.coordinator_handle:
            raise OrcaCommandError(
                "local dispatch source is not the coordinator"
            )
        dispatch_id = "local-dispatch-" + uuid.uuid4().hex
        dispatches = state.setdefault("dispatches", {})
        if not isinstance(dispatches, dict):
            raise OrcaCommandError("local dispatch state is invalid")
        dispatches[dispatch_id] = {
            "id": dispatch_id,
            "taskId": task_id,
            "to": target,
            "from": source,
        }
        target_terminal = terminals.get(target)
        if isinstance(target_terminal, dict):
            target_terminal["lastDispatchId"] = dispatch_id
        self._save_state(state)
        preamble = (
            "LOCAL ORCA DISPATCH PROVENANCE\n"
            f"task_id={task_id}\n"
            f"dispatch_id={dispatch_id}\n"
            "Use these task_id and dispatch_id values verbatim in the JSON artifact."
        )
        return self._response(
            {"dispatch": {"id": dispatch_id}, "preamble": preamble}
        )

    def _check(self, argv: tuple[str, ...]) -> OrcaResponse:
        raw_types = _option(argv, "--types") or ""
        accepted = {item for item in raw_types.split(",") if item}
        raw_wait = _option(argv, "--timeout-ms")
        wait_ms = int(raw_wait) if raw_wait and raw_wait.isdigit() else 0
        deadline = time.monotonic() + wait_ms / 1000
        while True:
            messages: list[dict[str, object]] = []
            for path in sorted(self.completion_dir.glob("*.json")):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                message = value.get("message", value)
                message_type = (
                    message.get("type")
                    if isinstance(message, dict)
                    else None
                )
                if accepted and message_type not in accepted:
                    continue
                messages.append(value)
                archive = self.completion_dir / "archive"
                archive.mkdir(exist_ok=True)
                path.replace(archive / path.name)
                self._finish_message_worker(value)
            if messages or time.monotonic() >= deadline:
                return self._response({"messages": messages})
            time.sleep(0.1)

    @staticmethod
    def _supervision_bridge_config() -> tuple[str, str] | None:
        token = os.environ.get("ORCA_WEB_AGENT_TOKEN", "")
        if len(token) < 32:
            token = os.environ.get("MCP_WEB_AGENT_CONTROL_TOKEN", "")
        if len(token) < 32:
            return None
        raw_endpoint = os.environ.get(
            "ORCA_WEB_AGENT_BRIDGE_URL",
            "http://127.0.0.1:8765",
        )
        try:
            parsed = urllib.parse.urlsplit(raw_endpoint)
        except ValueError:
            return None
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.port is None
        ):
            return None
        return raw_endpoint.rstrip("/"), token

    def _publish_supervision(
        self,
        notification: dict[str, object],
    ) -> bool:
        """Best-effort presentation bridge; never authoritative for gate state."""

        config = self._supervision_bridge_config()
        if config is None:
            return False
        base_endpoint, token = config
        payload = {
            "id": notification.get("gateId"),
            "runId": notification.get("runId"),
            "gateId": notification.get("gateId"),
            "question": notification.get("question"),
            "options": notification.get("options"),
            "reportPath": notification.get("reportPath"),
            "decisionSupport": notification.get("decisionSupport"),
            "resolution": notification.get("resolution"),
            "status": notification.get("status"),
        }
        request = urllib.request.Request(
            base_endpoint + "/automation/supervision",
            data=json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "x-orca-web-agent-token": token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return 200 <= response.status < 300
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            print(
                "[ORCA supervision alert warning] "
                "browser notification bridge unavailable",
                file=sys.stderr,
                flush=True,
            )
            return False

    def _probe_supervision_notification(
        self,
        notification: dict[str, object],
    ) -> bool | None:
        """Check whether remote-mcp still has the authoritative presentation copy.

        True means the remote state is present and compatible, False means it is
        definitely missing/stale and should be re-published, and None means the
        bridge could not be checked so the durable local ACK remains usable.
        """

        config = self._supervision_bridge_config()
        if config is None:
            return None
        base_endpoint, token = config
        gate_id = notification.get("gateId")
        if not isinstance(gate_id, str) or not gate_id:
            return False
        encoded_gate_id = urllib.parse.quote(gate_id, safe="")
        request = urllib.request.Request(
            base_endpoint
            + f"/automation/supervision/{encoded_gate_id}/decision",
            headers={"x-orca-web-agent-token": token},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                status = response.status
                if status == 204:
                    return notification.get("status") != "resolved"
                if status != 200:
                    return None
                try:
                    remote = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return None
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            return None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
        if not isinstance(remote, dict):
            return None
        if (
            remote.get("id") != gate_id
            or remote.get("gateId") != gate_id
            or remote.get("runId") != self.run_id
        ):
            return False
        local_status = notification.get("status")
        remote_status = remote.get("status")
        if local_status == "resolved":
            return (
                remote_status == "resolved"
                and isinstance(remote.get("decision"), dict)
            )
        return remote_status in {"pending", "answered", "resolved"}

    @staticmethod
    def _notification_digest(notification: dict[str, object]) -> str:
        raw = json.dumps(
            notification,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(raw).hexdigest()

    def _reconcile_supervision_notification(
        self,
        path: Path,
        *,
        gate_locked: bool = False,
    ) -> bool:
        if not gate_locked:
            lock_path = self.gate_dir / f"{path.stem}.resolve.lock"
            with _exclusive_file_lock(lock_path):
                return self._reconcile_supervision_notification(
                    path,
                    gate_locked=True,
                )
        try:
            notification = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrcaProtocolError(
                "local supervision notification is invalid"
            ) from exc
        if not isinstance(notification, dict):
            raise OrcaProtocolError(
                "local supervision notification must be an object"
            )
        gate_id = notification.get("gateId")
        if (
            not isinstance(gate_id, str)
            or not gate_id
            or notification.get("runId") != self.run_id
        ):
            raise OrcaProtocolError(
                "local supervision notification provenance mismatch"
            )
        gate_path = self.gate_dir / f"{gate_id}.json"
        try:
            gate = json.loads(gate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrcaProtocolError(
                "local supervision gate provenance is unavailable"
            ) from exc
        if (
            not isinstance(gate, dict)
            or gate.get("id") != gate_id
            or gate.get("taskId") != notification.get("taskId")
        ):
            raise OrcaProtocolError(
                "local supervision gate provenance mismatch"
            )
        changed = False
        for key in ("status", "resolution"):
            source = gate.get(key)
            if notification.get(key) != source:
                if source is None:
                    notification.pop(key, None)
                else:
                    notification[key] = source
                changed = True
        if changed:
            self._write_json_atomic(path, notification)

        digest = self._notification_digest(notification)
        ack_path = self.notification_ack_dir / f"{gate_id}.json"
        if ack_path.is_file():
            try:
                ack = json.loads(ack_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                ack = None
            if isinstance(ack, dict) and ack.get("digest") == digest:
                remote_present = self._probe_supervision_notification(notification)
                if remote_present is not False:
                    return True
        if not self._publish_supervision(notification):
            return False
        self._write_json_atomic(
            ack_path,
            {
                "gateId": gate_id,
                "status": notification.get("status"),
                "digest": digest,
            },
        )
        return True

    def _reconcile_supervision_notifications(self) -> None:
        for path in sorted(self.notification_dir.glob("*.json")):
            self._reconcile_supervision_notification(path)

    def _gate_create(self, argv: tuple[str, ...]) -> OrcaResponse:
        task_id = _option(argv, "--task")
        if not task_id:
            raise OrcaCommandError("local gate requires a task")
        gate_id = "local-gate-" + uuid.uuid4().hex
        try:
            options = json.loads(_option(argv, "--options") or "[]")
        except json.JSONDecodeError as exc:
            raise OrcaCommandError("local gate options are malformed") from exc
        if (
            not isinstance(options, list)
            or not options
            or any(not isinstance(item, str) or not item for item in options)
        ):
            raise OrcaCommandError("local gate options must be nonempty strings")
        raw_support = _option(argv, "--decision-support-json")
        decision_support: dict[str, object] | None = None
        if raw_support is not None:
            if len(raw_support.encode("utf-8")) > 65_536:
                raise OrcaCommandError("local gate decision support is too large")
            try:
                parsed_support = json.loads(raw_support)
            except json.JSONDecodeError as exc:
                raise OrcaCommandError(
                    "local gate decision support is malformed"
                ) from exc
            if not isinstance(parsed_support, dict):
                raise OrcaCommandError(
                    "local gate decision support must be an object"
                )
            decision_support = parsed_support
        report_path = _option(argv, "--report-path")
        report_digest = _option(argv, "--report-digest")
        gate = {
            "id": gate_id,
            "runId": self.run_id,
            "taskId": task_id,
            "question": _option(argv, "--question") or "",
            "options": options,
            "reportPath": report_path,
            "reportDigest": report_digest,
            "decisionSupport": decision_support,
            "status": "pending",
        }
        self.gate_dir.mkdir(parents=True, exist_ok=True)
        path = self.gate_dir / f"{gate_id}.json"
        self._write_json_atomic(path, gate)
        notification = {
            "schemaVersion": 1,
            "type": "human_supervision_required",
            "runId": self.run_id,
            "gateId": gate_id,
            "taskId": task_id,
            "question": gate["question"],
            "options": options,
            "reportPath": report_path,
            "decisionSupport": decision_support,
            "status": "pending",
        }
        self.notification_dir.mkdir(parents=True, exist_ok=True)
        notification_path = self.notification_dir / f"{gate_id}.json"
        self._write_json_atomic(notification_path, notification)
        print(
            "[ORCA HUMAN SUPERVISION REQUIRED] "
            f"run={self.run_id} gate={gate_id}: {gate['question']}",
            file=sys.stderr,
            flush=True,
        )
        self._reconcile_supervision_notification(notification_path)
        return self._response(
            {
                "id": gate_id,
                "taskId": task_id,
                "notification": notification,
            }
        )

    def resolve_gate(
        self,
        *,
        gate_id: str,
        resolution: dict[str, object],
        expected_report_digest: str | None = None,
    ) -> dict[str, object]:
        path = self.gate_dir / f"{gate_id}.json"
        lock_path = self.gate_dir / f"{gate_id}.resolve.lock"
        with _exclusive_file_lock(lock_path):
            try:
                gate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise OrcaCommandError(
                    f"local gate does not exist: {gate_id}"
                ) from exc
            if not isinstance(gate, dict) or gate.get("id") != gate_id:
                raise OrcaProtocolError("local gate payload is invalid")
            if gate.get("runId") not in {None, self.run_id}:
                raise OrcaProtocolError("local gate run provenance mismatch")
            if gate.get("status") != "pending":
                raise OrcaProtocolError("local gate is not pending")

            options = gate.get("options")
            if not isinstance(options, list):
                raise OrcaProtocolError("local gate options are invalid")
            try:
                decision = parse_human_decision(
                    json.dumps(
                        resolution,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            except ContractViolationError as exc:
                raise OrcaProtocolError(
                    f"local gate resolution is invalid: {exc}"
                ) from exc
            if decision.decision.value not in options:
                raise OrcaProtocolError(
                    "local gate resolution is not an allowed option"
                )

            stored_digest = gate.get("reportDigest")
            if stored_digest is not None and not isinstance(stored_digest, str):
                raise OrcaProtocolError("local gate report digest is invalid")
            if (
                stored_digest is not None
                and expected_report_digest is not None
                and stored_digest != expected_report_digest
            ):
                raise OrcaProtocolError("local gate report provenance mismatch")
            required_digest = (
                stored_digest
                if isinstance(stored_digest, str) and stored_digest
                else expected_report_digest
            )
            if (
                required_digest is not None
                and decision.report_digest != required_digest
            ):
                raise OrcaProtocolError("stale human decision report digest")

            notification_path = self.notification_dir / f"{gate_id}.json"
            if notification_path.is_file():
                try:
                    notification = json.loads(
                        notification_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    raise OrcaProtocolError(
                        "local supervision notification is invalid"
                    ) from exc
                if (
                    not isinstance(notification, dict)
                    or notification.get("runId") != self.run_id
                    or notification.get("gateId") != gate_id
                    or notification.get("taskId") != gate.get("taskId")
                ):
                    raise OrcaProtocolError(
                        "local supervision notification provenance mismatch"
                    )

            gate["status"] = "resolved"
            gate["resolution"] = resolution
            self._write_json_atomic(path, gate)
            if notification_path.is_file():
                self._reconcile_supervision_notification(
                    notification_path,
                    gate_locked=True,
                )
            return gate

    def _gate_list(self, argv: tuple[str, ...]) -> OrcaResponse:
        self._reconcile_supervision_notifications()
        task_id = _option(argv, "--task")
        status = _option(argv, "--status")
        gates: list[dict[str, object]] = []
        for path in sorted(self.gate_dir.glob("*.json")):
            try:
                gate = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(gate, dict):
                continue
            if task_id and gate.get("taskId") != task_id:
                continue
            if status and gate.get("status") != status:
                continue
            gates.append(gate)
        return self._response({"gates": gates})

    def call(
        self,
        argv: Sequence[str],
        *,
        timeout_ms: int,
    ) -> OrcaResponse:
        del timeout_ms
        args = tuple(argv)
        if args == ("status",):
            return self._response(
                {
                    "runtime": {
                        "state": "ready",
                        "reachable": True,
                        "appVersion": LOCAL_RUNTIME_VERSION,
                    },
                    "graph": {"state": "ready"},
                    "transport": "local",
                }
            )
        if args[:1] == ("terminal",):
            return self._terminal(args)
        if args[:2] == ("orchestration", "task-create"):
            return self._task_create(args)
        if args[:2] == ("orchestration", "dispatch"):
            return self._dispatch(args)
        if args[:2] == ("orchestration", "check"):
            return self._check(args)
        if args[:2] == ("orchestration", "gate-create"):
            return self._gate_create(args)
        if args[:2] == ("orchestration", "gate-list"):
            return self._gate_list(args)
        raise OrcaCommandError(
            "unsupported local coordinator command: " + " ".join(args)
        )
