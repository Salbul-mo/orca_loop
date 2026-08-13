"""Classification and durable evidence for the ways a run stops.

The loop previously ended in three unrelated ways: a state transition, a
markdown report, or an uncaught traceback.  Nothing tied them together, so an
operator had to decide by hand whether a stopped run could be resumed.

This module supplies the missing judgement.  Every stop is classified, and
every stop leaves an event behind, so ``status`` can answer the question
instead of a person.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .contracts import ContractViolationError
from .dispatcher import DispatchProvenanceError
from .generation import GenerationMismatchError
from .guards import GuardPathBoundaryError, GuardScopeViolationError
from .escalation import GateProtocolError
from .ledger import LedgerIntegrityError
from .models import LoopState
from .orca_client import OrcaTimeoutError
from .profiles import LaunchProfileError
from .roles import TemplateContractError
from .session import EVENTS_NAME, append_event
from .testrunner import TestPolicyError
from .transport import InputStagingError
from .workspace import WorkspaceError


STOP_EVENT_KIND = "stopped"
FORCE_FAIL_EVENT_KIND = "force_failed"
STOP_REASON_LIMIT = 2_000


class BudgetExhausted(RuntimeError):
    """Raised in name only when a run outlives its time or transition budget.

    Running long is not a broken contract, so this classifies as interrupted
    and the run stays resumable. The budget still bounds a single process.
    """


class StopClass(StrEnum):
    """Whether a stopped run can be resumed, and whether it can be retried."""

    TERMINAL = "TERMINAL"
    INTERRUPTED = "INTERRUPTED"
    RETRYABLE = "RETRYABLE"


# Checked first, so a narrow retryable error is never swallowed by the wider
# interrupted class it inherits from.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ContractViolationError,      # ProvenanceError inherits from this
    OrcaTimeoutError,            # narrower than OrcaCommandError
)

# Failures that repeat on a rerun because they describe a broken contract
# rather than a bad moment.  Everything absent from this tuple is treated as
# interrupted: killing a resumable run cannot be undone, while resuming a
# doomed one merely wastes an attempt.
TERMINAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    InputStagingError,           # Scope/PathBoundary/TransportProvenance
    GuardScopeViolationError,
    GuardPathBoundaryError,
    LedgerIntegrityError,        # InvalidRoundError
    TemplateContractError,
    WorkspaceError,              # PathBoundaryError
    LaunchProfileError,
    GenerationMismatchError,     # narrower than GenerationError
    TestPolicyError,
    DispatchProvenanceError,     # narrower than DispatcherError
    GateProtocolError,
)


def classify_stop(exc: BaseException) -> StopClass:
    """Judge a stop, defaulting to the answer that keeps the run recoverable."""
    if isinstance(exc, RETRYABLE_EXCEPTIONS):
        return StopClass.RETRYABLE
    if isinstance(exc, TERMINAL_EXCEPTIONS):
        return StopClass.TERMINAL
    return StopClass.INTERRUPTED


def _bounded_reason(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:STOP_REASON_LIMIT]


def record_stop_event(
    control_dir: Path,
    *,
    exc: BaseException,
    classification: StopClass,
    generation: int,
    state: LoopState | None,
    state_committed: bool,
) -> None:
    """Record why the loop stopped.

    Never raises. Recovery can fail on its own — committing FAILED needs a
    contiguous generation — and the evidence must survive that.
    """
    append_event(
        control_dir,
        STOP_EVENT_KIND,
        {
            "classification": classification.value,
            "exception": type(exc).__name__,
            "reason": _bounded_reason(exc),
            "generation": generation,
            "state": None if state is None else state.value,
            "resumable": classification is not StopClass.TERMINAL,
            "state_committed": state_committed,
        },
    )


@dataclass(frozen=True)
class StopEvent:
    classification: StopClass
    exception: str
    reason: str
    generation: int
    state: str | None
    resumable: bool
    state_committed: bool
    recorded_at: str


def _parse_stop_event(value: object) -> StopEvent | None:
    if not isinstance(value, dict):
        return None
    if value.get("kind") != STOP_EVENT_KIND:
        return None
    try:
        classification = StopClass(value["classification"])
    except (KeyError, TypeError, ValueError):
        return None
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return None
    state = value.get("state")
    if state is not None and not isinstance(state, str):
        return None
    return StopEvent(
        classification=classification,
        exception=str(value.get("exception", "")),
        reason=str(value.get("reason", "")),
        generation=generation,
        state=state,
        resumable=bool(value.get("resumable", False)),
        state_committed=bool(value.get("state_committed", False)),
        recorded_at=str(value.get("recorded_at", "")),
    )


def read_latest_stop_event(control_dir: Path) -> StopEvent | None:
    """Return the most recent stop event, skipping anything unreadable.

    The event log is evidence, so a damaged line must not be able to hide a
    later intact one, nor stop the report that reads it.
    """
    path = control_dir / EVENTS_NAME
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = _parse_stop_event(value)
        if event is not None:
            return event
    return None
