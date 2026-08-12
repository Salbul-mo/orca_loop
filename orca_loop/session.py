"""Terminal survival checks and rebinding for resumed runs.

A crashed coordinator leaves five terminal handles behind — one runner and
four workers — and Orca does not keep them alive across an app restart.
Treating a dead handle as a fatal error made a run permanently unresumable,
so this module verifies each recorded handle and recreates only the ones that
are actually gone, recording every rebind as durable evidence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .dispatcher import WorkerProvisionError, _result, _terminal_handle
from .models import WorkerHandle, WorkerKey, WorkerPool
from .orca_client import OrcaClient, OrcaCommandError


EVENTS_NAME = "resume-events.jsonl"
TERMINAL_TIMEOUT_MS = 10_000
CREATE_TIMEOUT_MS = 60_000


@dataclass(frozen=True)
class TerminalBinding:
    handle: str
    rebound: bool


@dataclass(frozen=True)
class PoolBinding:
    pool: WorkerPool
    rebound: tuple[WorkerKey, ...]

    @property
    def changed(self) -> bool:
        return bool(self.rebound)


def append_event(
    control_dir: Path,
    kind: str,
    detail: Mapping[str, object],
) -> None:
    """Append one resume event. Never raises: this is evidence, not control."""
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "kind": kind,
        **dict(detail),
    }
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
        with (control_dir / EVENTS_NAME).open(
            "a",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(line)
    except OSError:
        return


def read_events(control_dir: Path) -> tuple[dict[str, object], ...]:
    path = control_dir / EVENTS_NAME
    if not path.is_file():
        return ()
    records: list[dict[str, object]] = []
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ()
    for line in raw_text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return tuple(records)


def terminal_alive(client: OrcaClient, handle: str) -> bool:
    if not handle:
        return False
    try:
        client.call(
            ("terminal", "show", "--terminal", handle),
            timeout_ms=TERMINAL_TIMEOUT_MS,
        )
    except OrcaCommandError:
        return False
    return True


def create_terminal(
    client: OrcaClient,
    worktree_selector: str,
    title: str,
    worker_key: WorkerKey | None = None,
) -> WorkerHandle:
    response = client.call(
        (
            "terminal",
            "create",
            "--worktree",
            worktree_selector,
            "--title",
            title,
        ),
        timeout_ms=CREATE_TIMEOUT_MS,
    )
    return _terminal_handle(
        worker_key if worker_key is not None else WorkerKey.CLAUDE_PLANNER,
        _result(response),
        worktree_selector,
    )


def ensure_coordinator_terminal(
    client: OrcaClient,
    *,
    worktree_selector: str,
    run_id: str,
    recorded_handles: tuple[str, ...],
) -> TerminalBinding:
    """Return a live coordinator handle, recreating it when all are gone.

    Candidates are tried in order, so the handle committed by the previous
    process wins over one supplied on the command line.
    """
    seen: set[str] = set()
    for handle in recorded_handles:
        if not handle or handle in seen:
            continue
        seen.add(handle)
        if terminal_alive(client, handle):
            return TerminalBinding(handle, False)
    created = create_terminal(
        client,
        worktree_selector,
        f"ORCA LOOP {run_id}",
    )
    return TerminalBinding(created.terminal_handle, True)


def ensure_worker_pool(
    client: OrcaClient,
    *,
    worktree_selector: str,
    recorded: Mapping[WorkerKey, str],
    coordinator_handle: str,
) -> PoolBinding:
    """Return four live worker terminals, recreating only the dead ones."""
    workers: list[WorkerHandle] = []
    rebound: list[WorkerKey] = []
    for worker in WorkerKey:
        handle = recorded.get(worker, "")
        if handle and handle != coordinator_handle and terminal_alive(
            client,
            handle,
        ):
            workers.append(
                WorkerHandle(
                    worker_key=worker,
                    terminal_handle=handle,
                    worktree_id=worktree_selector,
                    tab_id="",
                    leaf_id="",
                )
            )
            continue
        created = create_terminal(
            client,
            worktree_selector,
            f"ORCA LOOP {worker.value}",
            worker,
        )
        if created.terminal_handle == coordinator_handle:
            raise WorkerProvisionError(
                "worker handle equals coordinator handle"
            )
        client.call(
            ("terminal", "show", "--terminal", created.terminal_handle),
            timeout_ms=TERMINAL_TIMEOUT_MS,
        )
        workers.append(created)
        rebound.append(worker)

    handles = {item.terminal_handle for item in workers}
    if len(handles) != len(WorkerKey):
        raise WorkerProvisionError("worker terminal handles are not unique")
    mapping = {item.worker_key: item for item in workers}
    if (
        mapping[WorkerKey.CLAUDE_PLANNER].terminal_handle
        == mapping[WorkerKey.CLAUDE_CODE_REVIEW].terminal_handle
    ):
        raise WorkerProvisionError(
            "planner and code reviewer must use separate sessions"
        )
    return PoolBinding(WorkerPool(tuple(workers)), tuple(rebound))
