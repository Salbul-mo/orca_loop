from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orca_loop.dispatcher import WorkerProvisionError
from orca_loop.models import WorkerKey
from orca_loop.orca_client import OrcaCommandError
from orca_loop.session import (
    append_event,
    ensure_coordinator_terminal,
    ensure_worker_pool,
    read_events,
    terminal_alive,
)
from tests.fakes import FakeOrcaClient


SELECTOR = "path:C:/repo"


class TerminalScript:
    """Fake Orca whose terminal survival is scripted per handle."""

    def __init__(self, alive: set[str]) -> None:
        self.alive = set(alive)
        self.created: list[str] = []
        self.counter = 0

    def __call__(self, argv: tuple[str, ...], timeout_ms: int):
        if argv[:2] == ("terminal", "show"):
            handle = argv[argv.index("--terminal") + 1]
            if handle not in self.alive:
                raise OrcaCommandError(f"terminal is gone: {handle}")
            return {"terminal": {"handle": handle}}
        if argv[:2] == ("terminal", "create"):
            self.counter += 1
            handle = f"term-new-{self.counter}"
            self.alive.add(handle)
            self.created.append(handle)
            return {
                "terminal": {
                    "handle": handle,
                    "tabId": f"tab-{self.counter}",
                    "leafId": f"leaf-{self.counter}",
                }
            }
        raise AssertionError(f"unexpected call: {argv}")


def recorded_workers() -> dict[WorkerKey, str]:
    return {worker: f"term-{worker.value}" for worker in WorkerKey}


class TerminalAliveTest(unittest.TestCase):
    def test_alive_and_dead_handles(self) -> None:
        script = TerminalScript({"term-live"})
        client = FakeOrcaClient(script)
        self.assertTrue(terminal_alive(client, "term-live"))
        self.assertFalse(terminal_alive(client, "term-dead"))
        self.assertFalse(terminal_alive(client, ""))


class CoordinatorTest(unittest.TestCase):
    def test_live_coordinator_is_reused(self) -> None:
        script = TerminalScript({"term-coordinator"})
        client = FakeOrcaClient(script)
        binding = ensure_coordinator_terminal(
            client,
            worktree_selector=SELECTOR,
            run_id="run-1",
            recorded_handles=("term-coordinator",),
        )
        self.assertEqual("term-coordinator", binding.handle)
        self.assertFalse(binding.rebound)
        self.assertEqual([], script.created)

    def test_dead_coordinator_is_recreated(self) -> None:
        script = TerminalScript(set())
        client = FakeOrcaClient(script)
        binding = ensure_coordinator_terminal(
            client,
            worktree_selector=SELECTOR,
            run_id="run-1",
            recorded_handles=("term-coordinator",),
        )
        self.assertTrue(binding.rebound)
        self.assertEqual("term-new-1", binding.handle)
        self.assertEqual(["term-new-1"], script.created)


class WorkerPoolTest(unittest.TestCase):
    def test_all_live_workers_are_reused(self) -> None:
        script = TerminalScript(set(recorded_workers().values()))
        client = FakeOrcaClient(script)
        binding = ensure_worker_pool(
            client,
            worktree_selector=SELECTOR,
            recorded=recorded_workers(),
            coordinator_handle="term-coordinator",
        )
        self.assertFalse(binding.changed)
        self.assertEqual([], script.created)
        self.assertEqual(4, len(binding.pool.workers))

    def test_only_dead_workers_are_recreated(self) -> None:
        recorded = recorded_workers()
        alive = {
            recorded[WorkerKey.CLAUDE_PLANNER],
            recorded[WorkerKey.CODEX_REVIEW],
        }
        script = TerminalScript(alive)
        client = FakeOrcaClient(script)
        binding = ensure_worker_pool(
            client,
            worktree_selector=SELECTOR,
            recorded=recorded,
            coordinator_handle="term-coordinator",
        )
        self.assertEqual(2, len(script.created))
        self.assertEqual(
            {WorkerKey.CLAUDE_CODE_REVIEW, WorkerKey.CODEX_IMPLEMENTER},
            set(binding.rebound),
        )
        by_worker = {
            item.worker_key: item.terminal_handle
            for item in binding.pool.workers
        }
        self.assertEqual(
            recorded[WorkerKey.CLAUDE_PLANNER],
            by_worker[WorkerKey.CLAUDE_PLANNER],
        )
        self.assertEqual(
            recorded[WorkerKey.CODEX_REVIEW],
            by_worker[WorkerKey.CODEX_REVIEW],
        )

    def test_missing_record_creates_full_pool(self) -> None:
        script = TerminalScript(set())
        client = FakeOrcaClient(script)
        binding = ensure_worker_pool(
            client,
            worktree_selector=SELECTOR,
            recorded={},
            coordinator_handle="term-coordinator",
        )
        self.assertEqual(4, len(script.created))
        self.assertEqual(4, len(set(binding.rebound)))

    def test_worker_may_not_equal_coordinator(self) -> None:
        class Colliding(TerminalScript):
            def __call__(self, argv, timeout_ms):
                if argv[:2] == ("terminal", "create"):
                    return {"terminal": {"handle": "term-coordinator"}}
                return super().__call__(argv, timeout_ms)

        client = FakeOrcaClient(Colliding(set()))
        with self.assertRaises(WorkerProvisionError):
            ensure_worker_pool(
                client,
                worktree_selector=SELECTOR,
                recorded={},
                coordinator_handle="term-coordinator",
            )

    def test_recorded_handle_equal_to_coordinator_is_replaced(self) -> None:
        recorded = recorded_workers()
        recorded[WorkerKey.CODEX_REVIEW] = "term-coordinator"
        script = TerminalScript(
            set(recorded.values()) | {"term-coordinator"}
        )
        client = FakeOrcaClient(script)
        binding = ensure_worker_pool(
            client,
            worktree_selector=SELECTOR,
            recorded=recorded,
            coordinator_handle="term-coordinator",
        )
        self.assertIn(WorkerKey.CODEX_REVIEW, binding.rebound)


class EventLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name).resolve() / "control"

    def test_events_append_without_losing_earlier_lines(self) -> None:
        append_event(self.control, "terminal_rebound", {"worker": "planner"})
        append_event(self.control, "drift_rebaselined", {"generation": 7})
        events = read_events(self.control)
        self.assertEqual(2, len(events))
        self.assertEqual("terminal_rebound", events[0]["kind"])
        self.assertEqual("drift_rebaselined", events[1]["kind"])
        self.assertIn("recorded_at", events[0])

    def test_reading_absent_log_is_empty(self) -> None:
        self.assertEqual((), read_events(self.control))

    def test_corrupt_line_is_skipped(self) -> None:
        append_event(self.control, "one", {})
        with (self.control / "resume-events.jsonl").open(
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write("not json\n")
        append_event(self.control, "two", {})
        events = read_events(self.control)
        self.assertEqual(["one", "two"], [item["kind"] for item in events])


if __name__ == "__main__":
    unittest.main()
