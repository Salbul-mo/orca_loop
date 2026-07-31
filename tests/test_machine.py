from __future__ import annotations

import random
import unittest

from orca_loop.machine import (
    TERMINAL_STATES,
    UndefinedTransitionError,
    transition,
)
from orca_loop.models import (
    LedgerView,
    LoopCounters,
    LoopState,
    SignalKind,
    TransitionSignal,
)


def signal(kind: SignalKind) -> TransitionSignal:
    return TransitionSignal(kind, kind.value, ())


class MachineTest(unittest.TestCase):
    def test_escalation_and_abort_have_priority(self) -> None:
        ledger = LedgerView(0, 0, 1, ())
        counters = LoopCounters(0, 0)
        for state in LoopState:
            if state in TERMINAL_STATES:
                continue
            self.assertEqual(
                LoopState.USER_DECISION_REQUIRED,
                transition(
                    state,
                    signal(SignalKind.ESCALATE),
                    ledger,
                    counters,
                ).next_state,
            )
            self.assertEqual(
                LoopState.FAILED,
                transition(
                    state,
                    signal(SignalKind.ABORT),
                    ledger,
                    counters,
                ).next_state,
            )

    def test_test_failures_do_not_reach_review(self) -> None:
        ledger = LedgerView(0, 0, 0, ())
        first = transition(
            LoopState.TEST_GATE,
            signal(SignalKind.FAIL),
            ledger,
            LoopCounters(0, 0),
            test_fix_attempt_limit=3,
        )
        self.assertEqual(LoopState.FIX, first.next_state)
        self.assertEqual(1, first.counters_after.test_fix_attempts)
        last = transition(
            LoopState.TEST_GATE,
            signal(SignalKind.FAIL),
            ledger,
            LoopCounters(2, 0),
            test_fix_attempt_limit=3,
        )
        self.assertEqual(
            LoopState.USER_DECISION_REQUIRED,
            last.next_state,
        )

    def test_test_pass_resets_only_test_fix_attempts(self) -> None:
        result = transition(
            LoopState.TEST_GATE,
            signal(SignalKind.PASS),
            LedgerView(0, 0, 0, ()),
            LoopCounters(2, 1),
        )
        self.assertEqual(LoopState.CODE_REVIEW, result.next_state)
        self.assertEqual(LoopCounters(0, 1), result.counters_after)

    def test_terminal_state_rejects_input(self) -> None:
        with self.assertRaises(UndefinedTransitionError):
            transition(
                LoopState.REJECTED,
                signal(SignalKind.OK),
                LedgerView(0, 0, 0, ()),
                LoopCounters(0, 0),
            )

    def test_ten_thousand_deterministic_sequences_terminate(self) -> None:
        for seed in range(10_000):
            generator = random.Random(seed)
            state = LoopState.INIT
            counters = LoopCounters(0, 0)
            plan_round = 0
            code_round = 0
            unresolved = 0
            for _ in range(128):
                if state in TERMINAL_STATES:
                    break
                if generator.randrange(97) == 0:
                    kind = SignalKind.ESCALATE
                elif state is LoopState.INIT:
                    kind = SignalKind.OK
                elif state in {
                    LoopState.PLAN,
                    LoopState.PLAN_REVISE,
                    LoopState.IMPLEMENT,
                    LoopState.FIX,
                    LoopState.PLAN_REVIEW,
                    LoopState.CODE_REVIEW,
                    LoopState.CROSS_CONFIRM,
                }:
                    kind = SignalKind.ARTIFACT_OK
                elif state is LoopState.PLAN_CONSENSUS_EVALUATE:
                    unresolved = 0 if plan_round >= seed % 3 else 1
                    if unresolved:
                        plan_round += 1
                        kind = SignalKind.UNRESOLVED_REMAIN
                    else:
                        kind = SignalKind.UNRESOLVED_ZERO
                elif state is LoopState.TEST_GATE:
                    kind = (
                        SignalKind.PASS
                        if generator.randrange(4)
                        else SignalKind.NOT_RUN
                    )
                elif state is LoopState.CONSENSUS_EVALUATE:
                    unresolved = 0 if code_round >= seed % 4 else 1
                    if unresolved:
                        code_round += 1
                        kind = SignalKind.UNRESOLVED_REMAIN
                    else:
                        kind = SignalKind.UNRESOLVED_ZERO
                elif state is LoopState.HUMAN_GATE:
                    kind = (
                        SignalKind.MERGE
                        if generator.randrange(2)
                        else SignalKind.REJECT
                    )
                else:
                    self.fail(f"no generator rule for {state.value}")
                ledger = LedgerView(
                    plan_round,
                    code_round,
                    unresolved,
                    (),
                )
                result = transition(
                    state,
                    signal(kind),
                    ledger,
                    counters,
                )
                if (
                    result.next_state is LoopState.READY_FOR_MERGE
                    and ledger.unresolved_count
                ):
                    self.fail("unresolved work reached READY_FOR_MERGE")
                state = result.next_state
                counters = result.counters_after
            self.assertIn(state, TERMINAL_STATES)


if __name__ == "__main__":
    unittest.main()
