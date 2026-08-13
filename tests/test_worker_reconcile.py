from __future__ import annotations

import unittest
from dataclasses import replace

from orca_loop.coordinator import reconcile_worker
from orca_loop.models import (
    ActiveStep,
    DispatchObservation,
    LoopCounters,
    LoopState,
    ResumeOutcome,
    Role,
    RunStatus,
    StepStage,
    WorkerHandle,
    WorkerKey,
)
from orca_loop.models import CoordinatorState


DIGEST = "sha256:" + "0" * 64


def _state(active: ActiveStep | None) -> CoordinatorState:
    return CoordinatorState(
        schema_version=2,
        generation=0,
        run_id="run-1",
        state=LoopState.PLAN,
        step_stage=StepStage.STEP_DISPATCHED,
        status=RunStatus.IN_PROGRESS,
        worktree_selector="path:C:\\repo",
        coordinator_handle="term-coordinator",
        worker_handles=(),
        active=active,
        plan_version=0,
        counters=LoopCounters(0, 0),
        base_head="0" * 40,
        snapshot_digest=DIGEST,
        test_gate_status=None,
        test_policy_digest=DIGEST,
        permission_report_digest=DIGEST,
        history=(),
        orchestration_run_id="run_orca",
    )


ACTIVE = ActiveStep(
    step_id="step-1",
    task_id="task-1",
    dispatch_id="ctx-1",
    role=Role.PLANNER,
    worker=WorkerHandle(
        WorkerKey.CLAUDE_PLANNER,
        "term-worker",
        "wt-1",
        "tab-1",
        "leaf-1",
    ),
)


def _observation(**overrides: object) -> DispatchObservation:
    base = DispatchObservation(
        dispatch_id="ctx-1",
        task_id="task-1",
        status="dispatched",
        assignee_handle="term-worker",
        assignee_alive=True,
        failure_count=0,
        completed_at=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class ReconcileWorkerTest(unittest.TestCase):
    """B-05: never re-run a step whose worker may still own the worktree."""

    def _decide(self, observation, *, output_exists=True, active=ACTIVE):
        return reconcile_worker(
            _state(active),
            observation,
            output_exists=output_exists,
        )

    def test_no_active_step(self) -> None:
        self.assertIs(
            ResumeOutcome.NO_ACTIVE_STEP,
            self._decide(None, active=None),
        )

    def test_live_worker_is_adopted_not_replaced(self) -> None:
        self.assertIs(
            ResumeOutcome.ADOPT_WAIT,
            self._decide(_observation(assignee_alive=True)),
        )

    def test_dead_worker_terminal_allows_retry(self) -> None:
        self.assertIs(
            ResumeOutcome.STOP_AND_RETRY,
            self._decide(_observation(assignee_alive=False)),
        )

    def test_settled_dispatch_with_output_recovers(self) -> None:
        self.assertIs(
            ResumeOutcome.RECOVER_SETTLED,
            self._decide(
                _observation(status="completed", assignee_alive=False),
            ),
        )

    def test_settled_dispatch_without_output_blocks(self) -> None:
        self.assertIs(
            ResumeOutcome.ABANDON_AND_BLOCK,
            self._decide(
                _observation(status="completed", assignee_alive=False),
                output_exists=False,
            ),
        )

    def test_missing_observation_blocks(self) -> None:
        # A transient lookup failure is not evidence that nothing is running.
        self.assertIs(
            ResumeOutcome.ABANDON_AND_BLOCK,
            self._decide(None),
        )

    def test_foreign_dispatch_id_blocks(self) -> None:
        self.assertIs(
            ResumeOutcome.ABANDON_AND_BLOCK,
            self._decide(_observation(dispatch_id="ctx-other")),
        )

    def test_unknown_status_blocks(self) -> None:
        self.assertIs(
            ResumeOutcome.ABANDON_AND_BLOCK,
            self._decide(_observation(status="something_new")),
        )

    def test_stopped_dispatch_allows_retry(self) -> None:
        self.assertIs(
            ResumeOutcome.STOP_AND_RETRY,
            self._decide(
                _observation(status="stopped", assignee_alive=False),
            ),
        )

    def test_status_matching_is_case_insensitive(self) -> None:
        self.assertIs(
            ResumeOutcome.ADOPT_WAIT,
            self._decide(_observation(status="DISPATCHED")),
        )


if __name__ == "__main__":
    unittest.main()
