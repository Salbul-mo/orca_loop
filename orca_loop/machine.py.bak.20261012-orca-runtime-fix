from __future__ import annotations

from .models import (
    LedgerView,
    LoopCounters,
    LoopState,
    SignalKind,
    TransitionResult,
    TransitionSignal,
)


class UndefinedTransitionError(RuntimeError):
    """Raised when a state and signal pair has no authorized transition."""


TERMINAL_STATES = {
    LoopState.READY_FOR_MERGE,
    LoopState.REJECTED,
    LoopState.USER_DECISION_REQUIRED,
    LoopState.FAILED,
}


def transition(
    state: LoopState,
    signal: TransitionSignal,
    ledger: LedgerView,
    counters: LoopCounters,
    *,
    plan_round_limit: int = 5,
    code_round_limit: int = 5,
    test_fix_attempt_limit: int = 3,
    operational_retry_limit: int = 3,
) -> TransitionResult:
    if state in TERMINAL_STATES:
        raise UndefinedTransitionError(
            f"terminal state {state.value} does not accept signals"
        )
    if (
        counters.test_fix_attempts < 0
        or counters.operational_retries < 0
    ):
        raise UndefinedTransitionError("counters must be nonnegative")
    if min(
        plan_round_limit,
        code_round_limit,
        test_fix_attempt_limit,
        operational_retry_limit,
    ) < 1:
        raise UndefinedTransitionError("all limits must be positive")

    if signal.kind is SignalKind.ESCALATE:
        return TransitionResult(
            LoopState.USER_DECISION_REQUIRED,
            counters,
            signal.reason,
        )
    if signal.kind is SignalKind.ABORT:
        return TransitionResult(
            LoopState.FAILED,
            counters,
            signal.reason,
        )
    if signal.kind is SignalKind.OPERATIONAL_RETRY:
        updated = LoopCounters(
            counters.test_fix_attempts,
            counters.operational_retries + 1,
        )
        return TransitionResult(
            (
                LoopState.USER_DECISION_REQUIRED
                if updated.operational_retries >= operational_retry_limit
                else state
            ),
            updated,
            signal.reason,
        )

    direct: dict[tuple[LoopState, SignalKind], LoopState] = {
        (LoopState.INIT, SignalKind.OK): LoopState.PLAN,
        (LoopState.PLAN, SignalKind.ARTIFACT_OK): LoopState.PLAN_REVIEW,
        (
            LoopState.PLAN_REVIEW,
            SignalKind.ARTIFACT_OK,
        ): LoopState.PLAN_CONSENSUS_EVALUATE,
        (
            LoopState.PLAN_REVISE,
            SignalKind.ARTIFACT_OK,
        ): LoopState.PLAN_REVIEW,
        (LoopState.IMPLEMENT, SignalKind.ARTIFACT_OK): LoopState.TEST_GATE,
        (LoopState.FIX, SignalKind.ARTIFACT_OK): LoopState.TEST_GATE,
        (LoopState.TEST_GATE, SignalKind.PASS): LoopState.CODE_REVIEW,
        (LoopState.TEST_GATE, SignalKind.NOT_RUN): LoopState.CODE_REVIEW,
        (
            LoopState.TEST_GATE,
            SignalKind.POLICY_VIOLATION,
        ): LoopState.USER_DECISION_REQUIRED,
        (
            LoopState.CODE_REVIEW,
            SignalKind.ARTIFACT_OK,
        ): LoopState.CROSS_CONFIRM,
        (
            LoopState.CROSS_CONFIRM,
            SignalKind.ARTIFACT_OK,
        ): LoopState.CONSENSUS_EVALUATE,
        (LoopState.HUMAN_GATE, SignalKind.MERGE): LoopState.READY_FOR_MERGE,
        (LoopState.HUMAN_GATE, SignalKind.REJECT): LoopState.REJECTED,
        (LoopState.HUMAN_GATE, SignalKind.REVISE_CODE): LoopState.FIX,
        (
            LoopState.HUMAN_GATE,
            SignalKind.REVISE_DESIGN,
        ): LoopState.PLAN_REVISE,
    }
    target = direct.get((state, signal.kind))
    if target is not None:
        updated_counters = (
            LoopCounters(
                0,
                counters.operational_retries,
            )
            if (
                state is LoopState.TEST_GATE
                and signal.kind is SignalKind.PASS
            )
            else counters
        )
        return TransitionResult(
            target,
            updated_counters,
            signal.reason,
        )

    if (
        state is LoopState.PLAN_CONSENSUS_EVALUATE
        and signal.kind is SignalKind.UNRESOLVED_ZERO
    ):
        if ledger.unresolved_count != 0:
            raise UndefinedTransitionError(
                "unresolved_zero signal conflicts with ledger"
            )
        return TransitionResult(
            LoopState.IMPLEMENT,
            counters,
            signal.reason,
        )
    if (
        state is LoopState.PLAN_CONSENSUS_EVALUATE
        and signal.kind is SignalKind.UNRESOLVED_REMAIN
    ):
        if ledger.unresolved_count <= 0:
            raise UndefinedTransitionError(
                "unresolved_remain signal conflicts with ledger"
            )
        target = (
            LoopState.PLAN_REVISE
            if ledger.plan_round < plan_round_limit
            else LoopState.USER_DECISION_REQUIRED
        )
        return TransitionResult(target, counters, signal.reason)
    if (
        state is LoopState.CONSENSUS_EVALUATE
        and signal.kind is SignalKind.UNRESOLVED_ZERO
    ):
        if ledger.unresolved_count != 0:
            raise UndefinedTransitionError(
                "unresolved_zero signal conflicts with ledger"
            )
        return TransitionResult(
            LoopState.HUMAN_GATE,
            counters,
            signal.reason,
        )
    if (
        state is LoopState.CONSENSUS_EVALUATE
        and signal.kind is SignalKind.UNRESOLVED_REMAIN
    ):
        if ledger.unresolved_count <= 0:
            raise UndefinedTransitionError(
                "unresolved_remain signal conflicts with ledger"
            )
        target = (
            LoopState.FIX
            if ledger.code_round < code_round_limit
            else LoopState.USER_DECISION_REQUIRED
        )
        return TransitionResult(target, counters, signal.reason)
    if state is LoopState.TEST_GATE and signal.kind is SignalKind.FAIL:
        updated = LoopCounters(
            counters.test_fix_attempts + 1,
            counters.operational_retries,
        )
        target = (
            LoopState.FIX
            if updated.test_fix_attempts < test_fix_attempt_limit
            else LoopState.USER_DECISION_REQUIRED
        )
        return TransitionResult(target, updated, signal.reason)
    raise UndefinedTransitionError(
        f"undefined transition: {state.value} + {signal.kind.value}"
    )
