from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


class RunLockError(RuntimeError):
    """Raised when exclusive coordinator lock ownership is uncertain."""


@dataclass(frozen=True)
class RunLock:
    path: Path
    token: str
    run_id: str
    pid: int
    started_at_ns: int


def lock_path(harness_root: Path, worktree: Path) -> Path:
    digest = hashlib.sha256(
        str(worktree.resolve()).casefold().encode("utf-8")
    ).hexdigest()
    return harness_root.resolve() / "runs" / ".locks" / f"{digest}.lock"


@contextmanager
def _lock_guard(path: Path):
    guard = path.with_name(path.name + ".guard")
    guard.parent.mkdir(parents=True, exist_ok=True)
    with guard.open("a+b") as handle:
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


def _process_identity(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
            ctypes.POINTER(wintypes.FILETIME),
        )
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            )
            if not ok:
                return None
            ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return f"windows-filetime:{ticks}"
        finally:
            kernel32.CloseHandle(handle)
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.is_file():
        try:
            raw = proc_stat.read_text(encoding="utf-8")
            rest = raw[raw.rfind(")") + 2 :].split()
            return f"linux-startticks:{rest[19]}"
        except (OSError, IndexError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return None
    return f"pid-live:{pid}"


def _lock_is_stale(value: object) -> bool:
    if not isinstance(value, dict):
        raise RunLockError("coordinator lock payload is invalid")
    pid = value.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RunLockError("coordinator lock pid is invalid")
    current_identity = _process_identity(pid)
    if current_identity is None:
        return True
    stored_identity = value.get("process_identity")
    if stored_identity is None:
        return False
    if not isinstance(stored_identity, str) or not stored_identity:
        raise RunLockError("coordinator lock process identity is invalid")
    return stored_identity != current_identity


def acquire_run_lock(
    harness_root: Path,
    worktree: Path,
    run_id: str,
) -> RunLock:
    path = lock_path(harness_root, worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    started_at_ns = time.time_ns()
    pid = os.getpid()
    process_identity = _process_identity(pid)
    if process_identity is None:
        raise RunLockError("cannot establish coordinator process identity")
    value = {
        "schema_version": 2,
        "token": token,
        "run_id": run_id,
        "pid": pid,
        "process_identity": process_identity,
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
    with _lock_guard(path):
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RunLockError(
                    f"cannot validate existing coordinator lock: {path}"
                ) from exc
            if not _lock_is_stale(existing):
                raise RunLockError(
                    f"worktree already has a coordinator lock: {path}"
                )
            try:
                path.unlink()
            except OSError as exc:
                raise RunLockError(
                    f"failed to recover stale coordinator lock: {path}"
                ) from exc
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
        pid=pid,
        started_at_ns=started_at_ns,
    )


def release_run_lock(lock: RunLock) -> None:
    with _lock_guard(lock.path):
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
