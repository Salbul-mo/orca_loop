from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class RunLockError(RuntimeError):
    """Raised when exclusive coordinator lock ownership is uncertain."""


@dataclass(frozen=True)
class RunLock:
    path: Path
    token: str
    run_id: str
    pid: int
    started_at_ns: int


@dataclass(frozen=True)
class LockInfo:
    """Read-only view of an existing coordinator lock file."""

    path: Path
    readable: bool
    run_id: str | None
    pid: int | None
    alive: bool
    age_seconds: float
    worktree: str | None


def lock_path(harness_root: Path, worktree: Path) -> Path:
    digest = hashlib.sha256(
        str(worktree.resolve()).casefold().encode("utf-8")
    ).hexdigest()
    return harness_root.resolve() / "runs" / ".locks" / f"{digest}.lock"


def pid_alive(pid: int) -> bool:
    """Report whether a process exists without signalling it.

    ``os.kill(pid, 0)`` must never be used on Windows: for signals other
    than the console control events it calls TerminateProcess, which would
    kill the very process this check is inspecting.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# 100-nanosecond intervals between the Windows FILETIME epoch (1601-01-01) and
# the Unix epoch, used to bring GetProcessTimes onto time.time_ns()'s scale.
_FILETIME_EPOCH_DELTA_100NS = 116_444_736_000_000_000

# A lock is written moments after its owner starts, but the two clocks are read
# through different APIs.  Only treat the owner as a different process when it
# started comfortably after the lock was recorded.
PID_REUSE_TOLERANCE_NS = 2_000_000_000


def process_started_at_ns(pid: int) -> int | None:
    """Return the process creation time, or ``None`` when it is unknowable.

    Only Windows is answered precisely here.  Every other platform returns
    ``None`` so the caller falls back to bare liveness rather than acting on a
    guess.
    """
    if pid <= 0 or os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
    finally:
        kernel32.CloseHandle(handle)
    if not ok:
        return None
    filetime = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return (filetime - _FILETIME_EPOCH_DELTA_100NS) * 100


def _owner_alive(pid: int | None, started_at_ns: int | None) -> bool:
    """Judge lock ownership, treating a reused PID as a dead owner.

    ``pid_alive`` alone cannot tell the original coordinator from an unrelated
    process that later inherited its PID, which would strand the lock forever.
    """
    if pid is None:
        return True
    if not pid_alive(pid):
        return False
    if started_at_ns is None:
        return True
    owner_started = process_started_at_ns(pid)
    if owner_started is None:
        return True
    return owner_started <= started_at_ns + PID_REUSE_TOLERANCE_NS


def inspect_lock(harness_root: Path, worktree: Path) -> LockInfo | None:
    """Describe the lock currently held for ``worktree``, if any."""
    path = lock_path(harness_root, worktree)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = None
    if not isinstance(value, dict):
        try:
            age = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            age = 0.0
        return LockInfo(
            path=path,
            readable=False,
            run_id=None,
            pid=None,
            alive=True,
            age_seconds=age,
            worktree=None,
        )
    raw_pid = value.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else None
    started = value.get("started_at_ns")
    started_at_ns = (
        started
        if isinstance(started, int) and not isinstance(started, bool)
        else None
    )
    if started_at_ns is not None:
        age = max(0.0, (time.time_ns() - started_at_ns) / 1_000_000_000)
    else:
        try:
            age = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            age = 0.0
    run_id = value.get("run_id")
    worktree_value = value.get("worktree")
    return LockInfo(
        path=path,
        readable=True,
        run_id=run_id if isinstance(run_id, str) else None,
        pid=pid,
        alive=_owner_alive(pid, started_at_ns),
        age_seconds=age,
        worktree=(
            worktree_value if isinstance(worktree_value, str) else None
        ),
    )


def _describe(info: LockInfo) -> str:
    if not info.readable:
        return f"unreadable lock file {info.path}"
    return (
        f"run_id={info.run_id!r}, pid={info.pid}, "
        f"alive={info.alive}, age={info.age_seconds:.0f}s"
    )


def acquire_run_lock(
    harness_root: Path,
    worktree: Path,
    run_id: str,
    *,
    reclaim_stale: bool = False,
    force: bool = False,
    on_reclaim: Callable[[LockInfo], None] | None = None,
) -> RunLock:
    """Acquire the exclusive coordinator lock for ``worktree``.

    ``reclaim_stale`` removes a lock whose recorded process is no longer
    running. A lock owned by a live process is never reclaimed unless
    ``force`` is set, so a false negative can only refuse a run, never
    allow two coordinators to share one worktree.
    """
    path = lock_path(harness_root, worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        info = inspect_lock(harness_root, worktree)
        reclaimable = (
            info is not None
            and reclaim_stale
            and info.readable
            and info.pid is not None
            and not info.alive
        )
        if info is not None and (force or reclaimable):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise RunLockError(
                    f"failed to reclaim stale lock: {path}"
                ) from exc
            if on_reclaim is not None:
                on_reclaim(info)
        elif info is not None:
            raise RunLockError(
                f"worktree already has a coordinator lock: {path} "
                f"({_describe(info)}); use --force-unlock only when the "
                "owning coordinator is known to be gone"
            )
    token = uuid.uuid4().hex
    started_at_ns = time.time_ns()
    value = {
        "schema_version": 1,
        "token": token,
        "run_id": run_id,
        "pid": os.getpid(),
        "started_at_ns": started_at_ns,
        "worktree": str(worktree.resolve()),
    }
    raw = (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise RunLockError(
            f"worktree already has a coordinator lock: {path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink()
        except OSError as cleanup_error:
            raise RunLockError(
                f"failed to clean incomplete lock: {path}"
            ) from cleanup_error
        raise
    return RunLock(
        path=path,
        token=token,
        run_id=run_id,
        pid=os.getpid(),
        started_at_ns=started_at_ns,
    )


def release_run_lock(lock: RunLock) -> None:
    try:
        value = json.loads(lock.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunLockError(
            f"cannot verify owned lock before release: {lock.path}"
        ) from exc
    if (
        not isinstance(value, dict)
        or value.get("token") != lock.token
        or value.get("run_id") != lock.run_id
        or value.get("pid") != lock.pid
    ):
        raise RunLockError(
            "lock ownership changed; refusing to remove it"
        )
    try:
        lock.path.unlink()
    except OSError as exc:
        raise RunLockError(
            f"failed to release owned lock: {lock.path}"
        ) from exc
