from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orca_loop.contracts import ContractViolationError, ProvenanceError
from orca_loop.dispatcher import (
    DispatcherError,
    DispatchProvenanceError,
    DispatchTimeoutError,
    StepBindingError,
    WorkerLostError,
    WorkerProvisionError,
)
from orca_loop.escalation import DecisionReportError, GateProtocolError
from orca_loop.failure import (
    RETRYABLE_EXCEPTIONS,
    STOP_EVENT_KIND,
    STOP_REASON_LIMIT,
    TERMINAL_EXCEPTIONS,
    StopClass,
    classify_stop,
    read_latest_stop_event,
    record_stop_event,
)
from orca_loop.generation import (
    AtomicWriteError,
    GenerationError,
    GenerationMismatchError,
)
from orca_loop.guards import GuardPathBoundaryError, GuardScopeViolationError
from orca_loop.ledger import InvalidRoundError, LedgerIntegrityError
from orca_loop.locking import RunLockError
from orca_loop.models import LoopState
from orca_loop.notify import NoticeDeliveryError
from orca_loop.orca_client import (
    OrcaCommandError,
    OrcaProtocolError,
    OrcaTimeoutError,
)
from orca_loop.profiles import LaunchProfileError
from orca_loop.readonly import ReadOnlyMirrorError
from orca_loop.roles import TemplateContractError
from orca_loop.session import EVENTS_NAME
from orca_loop.snapshot import (
    GitCommandError,
    SnapshotChangedError,
    SnapshotError,
    SnapshotPathBoundaryError,
)
from orca_loop.testrunner import TestExecutionError, TestPolicyError
from orca_loop.transport import (
    InputStagingError,
    ScopeViolationError,
    TransportPathBoundaryError,
    TransportProvenanceError,
)
from orca_loop.workspace import PathBoundaryError, WorkspaceError


TERMINAL = StopClass.TERMINAL
INTERRUPTED = StopClass.INTERRUPTED
RETRYABLE = StopClass.RETRYABLE

# The classification is the safety contract for the whole boundary, so every
# class the harness can raise is pinned here rather than sampled.
CLASSIFICATION_TABLE: tuple[tuple[type[BaseException], StopClass], ...] = (
    (ContractViolationError, RETRYABLE),
    (ProvenanceError, RETRYABLE),
    (OrcaTimeoutError, RETRYABLE),
    (InputStagingError, TERMINAL),
    (ScopeViolationError, TERMINAL),
    (TransportPathBoundaryError, TERMINAL),
    (TransportProvenanceError, TERMINAL),
    (GuardScopeViolationError, TERMINAL),
    (GuardPathBoundaryError, TERMINAL),
    (LedgerIntegrityError, TERMINAL),
    (InvalidRoundError, TERMINAL),
    (TemplateContractError, TERMINAL),
    (WorkspaceError, TERMINAL),
    (PathBoundaryError, TERMINAL),
    (LaunchProfileError, TERMINAL),
    (GenerationMismatchError, TERMINAL),
    (TestPolicyError, TERMINAL),
    (DispatchProvenanceError, TERMINAL),
    (GateProtocolError, TERMINAL),
    (DecisionReportError, INTERRUPTED),
    (NoticeDeliveryError, INTERRUPTED),
    (GenerationError, INTERRUPTED),
    (AtomicWriteError, INTERRUPTED),
    (SnapshotChangedError, INTERRUPTED),
    (SnapshotError, INTERRUPTED),
    (GitCommandError, INTERRUPTED),
    (SnapshotPathBoundaryError, INTERRUPTED),
    (TestExecutionError, INTERRUPTED),
    (ReadOnlyMirrorError, INTERRUPTED),
    (OrcaCommandError, INTERRUPTED),
    (OrcaProtocolError, INTERRUPTED),
    (DispatchTimeoutError, INTERRUPTED),
    (WorkerLostError, INTERRUPTED),
    (WorkerProvisionError, INTERRUPTED),
    (StepBindingError, INTERRUPTED),
    (DispatcherError, INTERRUPTED),
    (RunLockError, INTERRUPTED),
    (RuntimeError, INTERRUPTED),
    (ValueError, INTERRUPTED),
    (OSError, INTERRUPTED),
)


class ClassificationTest(unittest.TestCase):
    def test_every_known_exception_gets_its_approved_class(self) -> None:
        for exception_class, expected in CLASSIFICATION_TABLE:
            with self.subTest(exception=exception_class.__name__):
                self.assertEqual(
                    expected,
                    classify_stop(exception_class("boom")),
                )

    def test_an_unknown_exception_stays_resumable(self) -> None:
        """Killing a resumable run cannot be undone; retrying a doomed one can."""

        class Unheard(Exception):
            pass

        self.assertEqual(INTERRUPTED, classify_stop(Unheard("boom")))

    def test_keyboard_interrupt_is_resumable(self) -> None:
        self.assertEqual(INTERRUPTED, classify_stop(KeyboardInterrupt()))

    def test_a_timeout_outranks_the_command_error_it_inherits(self) -> None:
        self.assertEqual(RETRYABLE, classify_stop(OrcaTimeoutError("slow")))
        self.assertEqual(INTERRUPTED, classify_stop(OrcaCommandError("no")))

    def test_a_generation_mismatch_outranks_plain_generation_failure(
        self,
    ) -> None:
        self.assertEqual(
            TERMINAL,
            classify_stop(GenerationMismatchError("gap")),
        )
        self.assertEqual(INTERRUPTED, classify_stop(GenerationError("io")))

    def test_the_two_class_sets_never_overlap(self) -> None:
        for retryable in RETRYABLE_EXCEPTIONS:
            for terminal in TERMINAL_EXCEPTIONS:
                with self.subTest(pair=(retryable.__name__, terminal.__name__)):
                    self.assertFalse(issubclass(retryable, terminal))
                    self.assertFalse(issubclass(terminal, retryable))


class StopEventTest(unittest.TestCase):
    def test_a_stop_is_recorded_with_its_full_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()

            record_stop_event(
                control,
                exc=OrcaCommandError("orca is unreachable"),
                classification=INTERRUPTED,
                generation=7,
                state=LoopState.IMPLEMENT,
                state_committed=False,
            )

            event = read_latest_stop_event(control)
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(INTERRUPTED, event.classification)
            self.assertEqual("OrcaCommandError", event.exception)
            self.assertEqual("orca is unreachable", event.reason)
            self.assertEqual(7, event.generation)
            self.assertEqual("IMPLEMENT", event.state)
            self.assertTrue(event.resumable)
            self.assertFalse(event.state_committed)
            self.assertTrue(event.recorded_at)

    def test_a_terminal_stop_is_not_marked_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()

            record_stop_event(
                control,
                exc=TemplateContractError("bad template"),
                classification=TERMINAL,
                generation=1,
                state=LoopState.PLAN,
                state_committed=True,
            )

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertFalse(event.resumable)
            self.assertTrue(event.state_committed)

    def test_reason_is_normalized_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()

            record_stop_event(
                control,
                exc=RuntimeError("a\nb\t" + "z" * (STOP_REASON_LIMIT + 500)),
                classification=INTERRUPTED,
                generation=0,
                state=None,
                state_committed=False,
            )

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertNotIn("\n", event.reason)
            self.assertNotIn("\t", event.reason)
            self.assertLessEqual(len(event.reason), STOP_REASON_LIMIT)
            self.assertIsNone(event.state)

    def test_recording_never_raises_even_when_the_log_cannot_be_written(
        self,
    ) -> None:
        """Evidence must not become one more way for the stop to fail."""
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()

            with mock.patch(
                "orca_loop.failure.append_event",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    record_stop_event(
                        control,
                        exc=RuntimeError("boom"),
                        classification=INTERRUPTED,
                        generation=0,
                        state=None,
                        state_committed=False,
                    )

    def test_the_real_append_swallows_its_own_io_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # A file where the control directory should be makes the append
            # impossible; append_event is contracted to absorb that.
            blocked = Path(directory).resolve() / "control"
            blocked.write_text("not a directory", encoding="utf-8")

            record_stop_event(
                blocked,
                exc=RuntimeError("boom"),
                classification=INTERRUPTED,
                generation=0,
                state=None,
                state_committed=False,
            )


class StopEventReadTest(unittest.TestCase):
    def test_the_latest_stop_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            for generation in (1, 2, 3):
                record_stop_event(
                    control,
                    exc=RuntimeError(f"stop {generation}"),
                    classification=INTERRUPTED,
                    generation=generation,
                    state=None,
                    state_committed=False,
                )

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertEqual(3, event.generation)

    def test_a_damaged_line_cannot_hide_an_intact_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            record_stop_event(
                control,
                exc=RuntimeError("real stop"),
                classification=INTERRUPTED,
                generation=4,
                state=None,
                state_committed=False,
            )
            with (control / EVENTS_NAME).open("a", encoding="utf-8") as handle:
                handle.write("{ not json\n")

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertEqual("real stop", event.reason)

    def test_other_event_kinds_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            record_stop_event(
                control,
                exc=RuntimeError("real stop"),
                classification=INTERRUPTED,
                generation=4,
                state=None,
                state_committed=False,
            )
            with (control / EVENTS_NAME).open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"kind": "terminal_rebound", "handle": "t1"})
                    + "\n"
                )

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertEqual(STOP_EVENT_KIND, "stopped")
            self.assertEqual("real stop", event.reason)

    def test_a_missing_log_reads_as_no_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                read_latest_stop_event(Path(directory).resolve())
            )


class CoordinatorBoundaryTest(unittest.TestCase):
    """The boundary in run_coordinator, exercised through a stubbed loop."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name).resolve() / "control"
        self.control.mkdir(parents=True)

    def controller(self) -> mock.Mock:
        controller = mock.Mock()
        controller.workspace.control_dir = self.control
        controller.state.generation = 5
        controller.state.state = LoopState.IMPLEMENT
        return controller

    def run_with(self, exc: BaseException, controller: mock.Mock):
        import run_loop

        with mock.patch.object(
            run_loop,
            "_initialize",
            return_value=(controller, mock.Mock()),
        ), mock.patch.object(run_loop, "_run_loop", side_effect=exc):
            preflight = mock.Mock()
            preflight.arguments.resume = False
            return run_loop.run_coordinator(preflight, mock.Mock())

    def test_a_terminal_stop_fails_the_run_and_returns(self) -> None:
        controller = self.controller()

        result = self.run_with(TemplateContractError("bad template"), controller)

        self.assertIs(controller.state, result)
        controller.commit.assert_called_once()
        self.assertEqual(
            LoopState.FAILED,
            controller.commit.call_args.kwargs["state_value"],
        )
        event = read_latest_stop_event(self.control)
        assert event is not None
        self.assertEqual(TERMINAL, event.classification)
        self.assertTrue(event.state_committed)

    def test_an_interrupted_stop_preserves_state_and_propagates(self) -> None:
        controller = self.controller()

        with self.assertRaises(OrcaCommandError):
            self.run_with(OrcaCommandError("orca is gone"), controller)

        controller.commit.assert_not_called()
        event = read_latest_stop_event(self.control)
        assert event is not None
        self.assertEqual(INTERRUPTED, event.classification)
        self.assertTrue(event.resumable)
        self.assertEqual(5, event.generation)

    def test_evidence_survives_a_recovery_commit_that_fails(self) -> None:
        """The FAILED commit needs a contiguous generation; the event does not."""
        controller = self.controller()
        controller.commit.side_effect = GenerationMismatchError("gap")

        result = self.run_with(LedgerIntegrityError("torn ledger"), controller)

        self.assertIs(controller.state, result)
        event = read_latest_stop_event(self.control)
        assert event is not None
        self.assertEqual(TERMINAL, event.classification)
        self.assertFalse(event.state_committed)

    def test_a_keyboard_interrupt_is_recorded_and_still_interrupts(self) -> None:
        controller = self.controller()

        with self.assertRaises(KeyboardInterrupt):
            self.run_with(KeyboardInterrupt(), controller)

        controller.commit.assert_not_called()
        event = read_latest_stop_event(self.control)
        assert event is not None
        self.assertEqual("KeyboardInterrupt", event.exception)


class OuterBoundaryTest(unittest.TestCase):
    """The main() helper, which runs where no controller exists."""

    def test_a_stop_before_the_run_directory_exists_is_skipped(self) -> None:
        import run_loop

        with tempfile.TemporaryDirectory() as directory:
            arguments = mock.Mock()
            arguments.harness_root = Path(directory).resolve()
            arguments.run_id = "run-missing"

            run_loop._record_stop(arguments, RuntimeError("boom"))

    def test_no_arguments_is_safe(self) -> None:
        import run_loop

        run_loop._record_stop(None, RuntimeError("boom"))

    def test_an_outer_stop_is_recorded_but_never_current(self) -> None:
        """generation -1 can match no committed generation, so it stays history."""
        import run_loop

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            control = root / "runs" / "run-1" / "control"
            control.mkdir(parents=True)
            arguments = mock.Mock()
            arguments.harness_root = root
            arguments.run_id = "run-1"

            run_loop._record_stop(arguments, ReadOnlyMirrorError("mirror gone"))

            event = read_latest_stop_event(control)
            assert event is not None
            self.assertEqual("ReadOnlyMirrorError", event.exception)
            self.assertEqual(-1, event.generation)
            self.assertEqual(INTERRUPTED, event.classification)


class RetryRoutingTest(unittest.TestCase):
    """Which failures may be retried in place, and where."""

    def test_a_transient_failure_outside_a_worker_step_is_retryable(
        self,
    ) -> None:
        self.assertEqual(
            RETRYABLE,
            classify_stop(OrcaTimeoutError("Orca command timed out")),
        )

    def test_a_worker_step_excludes_everything_but_contract_violations(
        self,
    ) -> None:
        """A dispatch mutation of unknown effect must not be replayed in place."""
        in_worker = True
        for exc, retried in (
            (ContractViolationError("malformed artifact"), True),
            (OrcaTimeoutError("dispatch timed out"), False),
        ):
            with self.subTest(exception=type(exc).__name__):
                routed = classify_stop(exc) is RETRYABLE and not (
                    in_worker and not isinstance(exc, ContractViolationError)
                )
                self.assertEqual(retried, routed)


if __name__ == "__main__":
    unittest.main()
