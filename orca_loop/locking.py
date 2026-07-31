from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
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


def acquire_run_lock(
    harness_root: Path,
    worktree: Path,
    run_id: str,
) -> RunLock:
    path = lock_path(harness_root, worktree)
    path.parent.mkdir(parents=True, exist_ok=True)
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
