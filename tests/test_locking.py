from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from orca_loop.locking import (
    LockInfo,
    RunLockError,
    acquire_run_lock,
    inspect_lock,
    lock_path,
    pid_alive,
    release_run_lock,
)


def _dead_pid() -> int:
    process = subprocess.Popen(
        (sys.executable, "-c", "pass"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    process.wait()
    return process.pid


class PidLivenessTest(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        import os

        self.assertTrue(pid_alive(os.getpid()))

    def test_exited_process_is_not_alive(self) -> None:
        self.assertFalse(pid_alive(_dead_pid()))

    def test_non_positive_pid_is_not_alive(self) -> None:
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-1))


class LockReclaimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.worktree = self.root / "worktree"
        self.worktree.mkdir()

    def _write_lock(self, value: object) -> Path:
        path = lock_path(self.root, self.worktree)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = value if isinstance(value, str) else json.dumps(value)
        path.write_text(raw, encoding="utf-8")
        return path

    def test_inspect_returns_none_without_lock(self) -> None:
        self.assertIsNone(inspect_lock(self.root, self.worktree))

    def test_stale_lock_is_reclaimed_when_requested(self) -> None:
        pid = _dead_pid()
        self._write_lock(
            {
                "schema_version": 1,
                "token": "stale",
                "run_id": "old-run",
                "pid": pid,
                "started_at_ns": time.time_ns() - 3_600_000_000_000,
                "worktree": str(self.worktree),
            }
        )
        info = inspect_lock(self.root, self.worktree)
        assert info is not None
        self.assertTrue(info.readable)
        self.assertFalse(info.alive)
        self.assertGreater(info.age_seconds, 3000)

        reclaimed: list[LockInfo] = []
        lock = acquire_run_lock(
            self.root,
            self.worktree,
            "new-run",
            reclaim_stale=True,
            on_reclaim=reclaimed.append,
        )
        self.addCleanup(release_run_lock, lock)
        self.assertEqual(len(reclaimed), 1)
        self.assertEqual(reclaimed[0].run_id, "old-run")
        self.assertEqual(lock.run_id, "new-run")

    def test_live_lock_is_never_reclaimed(self) -> None:
        first = acquire_run_lock(self.root, self.worktree, "run-1")
        self.addCleanup(release_run_lock, first)
        with self.assertRaises(RunLockError):
            acquire_run_lock(
                self.root,
                self.worktree,
                "run-2",
                reclaim_stale=True,
            )

    def test_unreadable_lock_requires_force(self) -> None:
        self._write_lock("{ not json")
        info = inspect_lock(self.root, self.worktree)
        assert info is not None
        self.assertFalse(info.readable)
        self.assertTrue(info.alive)
        with self.assertRaises(RunLockError):
            acquire_run_lock(
                self.root,
                self.worktree,
                "run-1",
                reclaim_stale=True,
            )
        lock = acquire_run_lock(
            self.root,
            self.worktree,
            "run-1",
            force=True,
        )
        self.addCleanup(release_run_lock, lock)
        self.assertEqual(lock.run_id, "run-1")

    def test_default_acquire_does_not_reclaim(self) -> None:
        self._write_lock(
            {
                "schema_version": 1,
                "token": "stale",
                "run_id": "old-run",
                "pid": _dead_pid(),
                "started_at_ns": time.time_ns(),
                "worktree": str(self.worktree),
            }
        )
        with self.assertRaises(RunLockError):
            acquire_run_lock(self.root, self.worktree, "run-1")

    def test_error_message_names_the_owner(self) -> None:
        first = acquire_run_lock(self.root, self.worktree, "run-1")
        self.addCleanup(release_run_lock, first)
        with self.assertRaises(RunLockError) as caught:
            acquire_run_lock(self.root, self.worktree, "run-2")
        message = str(caught.exception)
        self.assertIn("run-1", message)
        self.assertIn("--force-unlock", message)


if __name__ == "__main__":
    unittest.main()
