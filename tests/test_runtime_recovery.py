from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_loop
from orca_loop.config import parse_run_arguments
from orca_loop.coordinator import commit_step_transition
from orca_loop.ledger import empty_ledger
from orca_loop.master_runtime import MasterRuntimeAdapterError, invoke_master_provider
from orca_loop.models import (
    LoopCounters,
    LoopState,
    MasterAction,
    PermissionProfile,
    Role,
    RunStatus,
    SignalKind,
    StepExecutionResult,
    StepStage,
    TransitionSignal,
)
from tools import run_web_gpt_loop as web_loop


class RuntimeRecoveryRegressionTest(unittest.TestCase):
    def test_master_provider_retries_transient_adapter_failures(self) -> None:
        failures = [
            MasterRuntimeAdapterError("bridge_transport: first"),
            MasterRuntimeAdapterError("result_retrieval: second"),
            "ok",
        ]
        with (
            patch(
                "orca_loop.master_runtime._invoke_master_provider_once",
                side_effect=failures,
            ) as invoke_once,
            patch("orca_loop.master_runtime.time.sleep"),
        ):
            value = invoke_master_provider(object(), "prompt", timeout_ms=5000)
        self.assertEqual("ok", value)
        self.assertEqual(3, invoke_once.call_count)

    def test_artifact_verified_resume_does_not_dispatch_worker_again(self) -> None:
        controller = SimpleNamespace(
            state=SimpleNamespace(
                state=LoopState.PLAN,
                step_stage=StepStage.ARTIFACT_VERIFIED,
            )
        )
        with (
            patch(
                "run_loop._resume_verified_worker_routing",
                return_value="verified-plan",
            ) as resume_routing,
            patch("run_loop.create_run_workspace") as create_workspace,
        ):
            value = run_loop._execute_worker(
                controller,
                None,
                None,
                None,
            )
        self.assertEqual("verified-plan", value)
        resume_routing.assert_called_once()
        create_workspace.assert_not_called()

    def test_planning_only_consensus_finishes_without_implementation_gate(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.state = SimpleNamespace(
                    state=LoopState.PLAN_CONSENSUS_EVALUATE,
                    counters=LoopCounters(0, 0),
                )
                self.committed = None

            def commit(self, **kwargs):
                self.committed = kwargs
                return kwargs

        controller = FakeController()
        result = StepExecutionResult(
            TransitionSignal(
                SignalKind.UNRESOLVED_ZERO,
                "clean plan consensus",
                (),
            ),
            empty_ledger("run-1"),
            None,
            (),
        )
        returned = commit_step_transition(
            controller,
            result,
            SimpleNamespace(planning_only=True),
        )
        self.assertIs(returned, controller.committed)
        self.assertEqual(
            LoopState.FINISHED_PLAN,
            controller.committed["state_value"],
        )
        self.assertEqual(RunStatus.READY, controller.committed["status"])

    def test_zero_wait_supervision_still_polls_once_and_resolves_answer(self) -> None:
        state = SimpleNamespace(
            gate_binding=SimpleNamespace(gate_id="gate-1")
        )
        with (
            patch(
                "tools.run_web_gpt_loop.load_committed",
                return_value=(state, None, None),
            ),
            patch(
                "tools.run_web_gpt_loop._bridge_supervision_decision",
                return_value={"decision": "revise_design", "note": "answer"},
            ) as bridge,
            patch(
                "tools.run_web_gpt_loop._resolve_bridge_supervision",
                return_value={"decision": "revise_design"},
            ) as resolve,
        ):
            answered = web_loop._wait_for_bridge_supervision("run-1", 0)
        self.assertTrue(answered)
        bridge.assert_called_once_with("run-1", "gate-1")
        resolve.assert_called_once()

    def test_manual_master_override_cli_is_typed_and_requires_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = root / "request.md"
            request.write_text("test", encoding="utf-8")
            worktree = root / "repo"
            worktree.mkdir()
            args = parse_run_arguments(
                [
                    "--run-id",
                    "manual-master-test",
                    "--request",
                    str(request),
                    "--worktree",
                    str(worktree),
                    "--coordinator-handle",
                    "local-coordinator",
                    "--resume",
                    "--master-decision",
                    "dispatch",
                    "--master-role",
                    "plan_reviewer",
                    "--master-permission-profile",
                    "read_only",
                    "--master-reason",
                    "provider outage recovery",
                ],
                harness_root=root,
            )
        decision = args.manual_master_decision
        self.assertIsNotNone(decision)
        self.assertEqual(MasterAction.DISPATCH, decision.action)
        self.assertEqual(Role.PLAN_REVIEWER, decision.role)
        self.assertEqual(PermissionProfile.READ_ONLY, decision.permission_profile)


if __name__ == "__main__":
    unittest.main()
