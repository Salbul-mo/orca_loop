from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from orca_loop.config import (
    PreflightResult,
    empty_test_policy,
    parse_run_arguments,
)
from orca_loop.coordinator import OrcaLoopError
from orca_loop.models import (
    ActiveStep,
    LoopState,
    PermissionCheck,
    PermissionFeasibilityReport,
    PermissionStrategy,
    Role,
    RunStatus,
    StepStage,
    ValidationStatus,
    WorkerKey,
)
from orca_loop.runspec import read_manifest
from orca_loop.session import read_events
from orca_loop.workspace import create_run_workspace
from run_loop import ResumeBlockedError, _initialize, _resume
from tests.fakes import FakeOrcaClient


class ResumeHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {"ORCA_TERMINAL_HANDLE": ""},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.request = self.root / "request.md"
        self.permission = self.root / "permission.json"
        self.request.write_text("request", encoding="utf-8")
        self.permission.write_text("{}", encoding="utf-8")
        # The harness root and the target worktree are the same directory in
        # this fixture, so runs/ must be ignored exactly as it is in the real
        # harness; otherwise writing run state would itself look like drift.
        (self.root / ".gitignore").write_text("runs/\n", encoding="utf-8")
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test")
        self._git("add", "request.md", "permission.json", ".gitignore")
        self._git("commit", "-m", "fixture")
        self.created: list[str] = []
        self.dead: set[str] = set()
        # Authoritative Orca view of the active step's Dispatch. None means
        # Orca has no record, which resume must treat as unproven, not done.
        self.dispatch_status: str | None = None
        self.assignee_handle: str = ""
        self.controller, self.pool = _initialize(
            self.preflight(),
            self.client(),
        )

    def _git(self, *args: str) -> None:
        subprocess.run(
            ("git", *args),
            cwd=self.root,
            capture_output=True,
            check=True,
        )

    def arguments(self, *extra: str, resume: bool = False):
        values = [
            "--run-id",
            "run-1",
            "--request",
            str(self.request),
            "--worktree",
            str(self.root),
            "--coordinator-handle",
            "term-coordinator",
            "--permission-report",
            str(self.permission),
            *extra,
        ]
        if resume:
            values.append("--resume")
        return parse_run_arguments(tuple(values), harness_root=self.root)

    def preflight(self, *extra: str, resume: bool = False) -> PreflightResult:
        report = PermissionFeasibilityReport(
            schema_version=1,
            run_id="spike",
            status=ValidationStatus.PASS,
            strategy=PermissionStrategy.READONLY_REPOSITORY,
            checks=(
                PermissionCheck(
                    "V-PERM-01",
                    ValidationStatus.PASS,
                    ("evidence",),
                ),
            ),
            evidence=("evidence",),
            orca_version="1.4.164",
            canonical_path=str(self.permission),
            report_digest="sha256:" + "a" * 64,
        )
        return PreflightResult(
            self.arguments(*extra, resume=resume),
            empty_test_policy(),
            report,
            "1.4.164",
            "a" * 40,
        )

    def client(self) -> FakeOrcaClient:
        def handler(argv: tuple[str, ...], _: int) -> dict[str, object]:
            if argv[:2] == ("terminal", "create"):
                handle = f"term-{len(self.created) + 1}"
                self.created.append(handle)
                return {
                    "terminal": {
                        "handle": handle,
                        "tabId": f"tab-{handle}",
                        "leafId": f"leaf-{handle}",
                        "worktreeId": "worktree-1",
                    }
                }
            if argv[:2] == ("terminal", "show"):
                handle = argv[argv.index("--terminal") + 1]
                if handle in self.dead:
                    from orca_loop.orca_client import OrcaCommandError

                    raise OrcaCommandError(f"terminal is gone: {handle}")
                return {"terminal": {"status": "running"}}
            if argv[:2] == ("orchestration", "dispatch-show"):
                if self.dispatch_status is None:
                    return {}
                return {
                    "dispatch": {
                        "id": "ctx-1",
                        "task_id": "task-1",
                        "status": self.dispatch_status,
                        "assignee_handle": self.assignee_handle,
                        "failure_count": 0,
                        "completed_at": None,
                    }
                }
            if argv[:2] == ("orchestration", "run-create"):
                return {"run": {"id": "orca-run-1"}}
            if argv[:2] == ("orchestration", "run-use"):
                return {"run": {"id": "orca-run-1"}}
            if argv[:2] == ("orchestration", "run-current"):
                return {"run": {"id": "orca-run-1"}}
            raise AssertionError(f"unexpected Orca call: {argv}")

        return FakeOrcaClient(handler)  # type: ignore[return-value]

    @property
    def control(self) -> Path:
        return self.controller.workspace.control_dir

    def resume(self, *extra: str):
        return _resume(self.preflight(*extra, resume=True), self.client())


class CleanResumeTest(ResumeHarness):
    def test_all_terminals_alive_reuses_them(self) -> None:
        before = list(self.created)
        controller, pool = self.resume()
        self.assertEqual(before, self.created)
        self.assertEqual(LoopState.PLAN, controller.state.state)
        self.assertEqual(
            {item.terminal_handle for item in self.pool.workers},
            {item.terminal_handle for item in pool.workers},
        )

    def test_run_id_mismatch_is_rejected(self) -> None:
        preflight = self.preflight(resume=True)
        other = replace(
            preflight,
            arguments=replace(preflight.arguments, run_id="run-other"),
        )
        (self.root / "runs" / "run-other" / "control").mkdir(parents=True)
        with self.assertRaises(Exception):
            _resume(other, self.client())

    def test_failed_run_is_not_resumable(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="failed for test",
            state_value=LoopState.FAILED,
            status=RunStatus.FAILED,
        )
        with self.assertRaisesRegex(OrcaLoopError, "start a new run"):
            self.resume()

    def test_rejected_run_is_not_resumable(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="rejected for test",
            state_value=LoopState.REJECTED,
            status=RunStatus.REJECTED,
        )
        with self.assertRaisesRegex(OrcaLoopError, "start a new run"):
            self.resume()


class TerminalRebindResumeTest(ResumeHarness):
    def test_dead_workers_are_rebound_and_recorded(self) -> None:
        original = {
            item.worker_key: item.terminal_handle
            for item in self.pool.workers
        }
        self.dead.add(original[WorkerKey.CODEX_IMPLEMENTER])
        self.dead.add(original[WorkerKey.CLAUDE_PLANNER])
        controller, pool = self.resume()
        rebound = {
            item.worker_key: item.terminal_handle for item in pool.workers
        }
        self.assertNotEqual(
            original[WorkerKey.CODEX_IMPLEMENTER],
            rebound[WorkerKey.CODEX_IMPLEMENTER],
        )
        self.assertEqual(
            original[WorkerKey.CODEX_REVIEW],
            rebound[WorkerKey.CODEX_REVIEW],
        )
        self.assertEqual(pool.workers, controller.state.worker_handles)
        events = read_events(self.control)
        self.assertTrue(
            any(item["kind"] == "resume_rebound" for item in events)
        )

    def test_dead_coordinator_is_rebound_and_persisted(self) -> None:
        self.dead.add("term-coordinator")
        controller, _ = self.resume()
        self.assertNotEqual(
            "term-coordinator",
            controller.state.coordinator_handle,
        )
        manifest = read_manifest(self.control)
        assert manifest is not None
        self.assertEqual(
            controller.state.coordinator_handle,
            manifest.coordinator_handle,
        )

    def test_attested_terminal_is_adopted_and_marked_rebound(self) -> None:
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term-attested"}):
            controller, _ = self.resume()
        self.assertEqual(
            "term-attested",
            controller.state.coordinator_handle,
        )
        events = read_events(self.control)
        self.assertTrue(
            any(item["kind"] == "resume_rebound" for item in events)
        )

    def test_dead_attested_terminal_blocks_instead_of_replacing(self) -> None:
        self.dead.add("term-attested")
        before = list(self.created)
        with patch.dict(os.environ, {"ORCA_TERMINAL_HANDLE": "term-attested"}):
            with self.assertRaisesRegex(
                ResumeBlockedError,
                "not live",
            ):
                self.resume()
        # An unattested replacement terminal must never stand in for the
        # coordinator, so resume creates nothing at all.
        self.assertEqual(before, self.created)


class InFlightStepResumeTest(ResumeHarness):
    def _dispatch_step(self, state: LoopState = LoopState.PLAN) -> str:
        step_id = "g0099-plan"
        _, step = create_run_workspace(
            self.root,
            "run-1",
            step_id,
            resume=True,
        )
        (step.root / "binding.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "step_id": step_id,
                    "task_id": "task-1",
                    "dispatch_id": "ctx-1",
                    "worker_handle": self.pool.workers[0].terminal_handle,
                    "role": "planner",
                    "contract_digest": "sha256:" + "b" * 64,
                    "input_manifest_digest": "sha256:" + "c" * 64,
                }
            ),
            encoding="utf-8",
        )
        (step.output_dir / "plan.json").write_text("{}", encoding="utf-8")
        self.controller.commit(
            stage=StepStage.STEP_DISPATCHED,
            active=ActiveStep(
                step_id=step_id,
                task_id="task-1",
                dispatch_id="ctx-1",
                role=Role.PLANNER,
                worker=self.pool.workers[0],
            ),
            reason="dispatched for test",
            state_value=state,
        )
        return step_id

    def _worker_is_gone(self) -> None:
        """Orca says the Dispatch is still open but its terminal is gone."""
        self.dispatch_status = "dispatched"
        self.assignee_handle = self.pool.workers[0].terminal_handle
        self.dead.add(self.assignee_handle)

    def test_dead_worker_step_is_abandoned_not_adopted(self) -> None:
        step_id = self._dispatch_step()
        self._worker_is_gone()
        controller, _ = self.resume()
        marker = (
            self.controller.workspace.steps_dir / step_id / "ABANDONED"
        )
        self.assertTrue(marker.is_file())
        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual("task-1", payload["task_id"])
        self.assertEqual("ctx-1", payload["dispatch_id"])
        self.assertTrue(payload["preserved_outputs"])
        self.assertIsNone(controller.state.active)
        self.assertIs(
            StepStage.TRANSITION_COMMITTED,
            controller.state.step_stage,
        )
        self.assertEqual(LoopState.PLAN, controller.state.state)

    def test_live_worker_blocks_resume_instead_of_being_replaced(self) -> None:
        """The worker runs in its own terminal and outlives the coordinator.

        Re-running its step would put a second editor in the same worktree, so
        resume must stop rather than abandon and replace it.
        """
        step_id = self._dispatch_step()
        self.dispatch_status = "dispatched"
        self.assignee_handle = self.pool.workers[0].terminal_handle
        with self.assertRaisesRegex(ResumeBlockedError, "still live"):
            self.resume()
        marker = (
            self.controller.workspace.steps_dir / step_id / "ABANDONED"
        )
        self.assertFalse(marker.exists())
        kinds = [item["kind"] for item in read_events(self.control)]
        self.assertIn("resume_blocked", kinds)

    def test_unknown_dispatch_blocks_rather_than_assuming_finished(
        self,
    ) -> None:
        step_id = self._dispatch_step()
        self.dispatch_status = None
        with self.assertRaisesRegex(ResumeBlockedError, "cannot be proven"):
            self.resume()
        self.assertFalse(
            (
                self.controller.workspace.steps_dir / step_id / "ABANDONED"
            ).exists()
        )

    def test_settled_dispatch_without_output_blocks(self) -> None:
        self._dispatch_step()
        self.dispatch_status = "completed"
        self.assignee_handle = self.pool.workers[0].terminal_handle
        step_root = (
            self.controller.workspace.steps_dir
            / self.controller.state.active.step_id
        )
        for item in (step_root / "out").iterdir():
            item.unlink()
        with self.assertRaisesRegex(ResumeBlockedError, "cannot be proven"):
            self.resume()

    def test_abandoned_step_keeps_inputs_and_outputs(self) -> None:
        step_id = self._dispatch_step()
        self._worker_is_gone()
        self.resume()
        step_root = self.controller.workspace.steps_dir / step_id
        self.assertTrue((step_root / "out" / "plan.json").is_file())
        self.assertTrue((step_root / "binding.json").is_file())

    def test_abandonment_is_recorded_as_an_event(self) -> None:
        self._dispatch_step()
        self._worker_is_gone()
        self.resume()
        kinds = [item["kind"] for item in read_events(self.control)]
        self.assertIn("step_abandoned", kinds)

    def test_clean_boundary_is_left_untouched(self) -> None:
        generation = self.controller.state.generation
        controller, _ = self.resume()
        self.assertEqual(generation, controller.state.generation)


class DriftResumeTest(ResumeHarness):
    def _dirty(self) -> None:
        (self.root / "new-file.txt").write_text("added", encoding="utf-8")

    def test_read_only_state_rebaselines_automatically(self) -> None:
        recorded = self.controller.state.snapshot_digest
        self._dirty()
        controller, _ = self.resume()
        self.assertNotEqual(recorded, controller.state.snapshot_digest)
        events = read_events(self.control)
        rebound = [
            item for item in events if item["kind"] == "resume_rebound"
        ]
        self.assertTrue(rebound)
        self.assertTrue(rebound[-1]["worktree_rebaselined"])

    def test_write_state_blocks_without_flag(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="implement for test",
            state_value=LoopState.IMPLEMENT,
        )
        self._dirty()
        with self.assertRaisesRegex(ResumeBlockedError, "accept-worktree-drift"):
            self.resume()

    def test_write_state_proceeds_with_flag(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="implement for test",
            state_value=LoopState.IMPLEMENT,
        )
        recorded = self.controller.state.snapshot_digest
        self._dirty()
        controller, _ = self.resume("--accept-worktree-drift")
        self.assertNotEqual(recorded, controller.state.snapshot_digest)
        self.assertEqual(LoopState.IMPLEMENT, controller.state.state)

    def test_validation_state_drift_blocks_then_rolls_back_to_test_gate(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="human gate for test",
            state_value=LoopState.HUMAN_GATE,
        )
        self._dirty()
        with self.assertRaisesRegex(ResumeBlockedError, "cannot be relabeled"):
            self.resume()
        controller, _ = self.resume("--accept-worktree-drift")
        self.assertIs(LoopState.TEST_GATE, controller.state.state)
        self.assertIsNone(controller.state.pending_review)
        self.assertIsNone(
            controller.state.validation_lineage.test_gate_snapshot_digest
        )

    def test_plan_review_drift_requires_acceptance_and_returns_to_revision(self) -> None:
        self.controller.commit(
            stage=StepStage.TRANSITION_COMMITTED,
            active=None,
            reason="plan review for test",
            state_value=LoopState.PLAN_REVIEW,
        )
        self._dirty()
        with self.assertRaises(ResumeBlockedError):
            self.resume()
        controller, _ = self.resume("--accept-worktree-drift")
        self.assertIs(LoopState.PLAN_REVISE, controller.state.state)

    def test_unchanged_worktree_does_not_rebaseline(self) -> None:
        recorded = self.controller.state.snapshot_digest
        controller, _ = self.resume()
        self.assertEqual(recorded, controller.state.snapshot_digest)


if __name__ == "__main__":
    unittest.main()
