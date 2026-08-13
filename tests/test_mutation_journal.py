from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orca_loop.generation import (
    GenerationMismatchError,
    read_mutations,
    write_mutation,
)
from orca_loop.models import (
    MutationKind,
    MutationPhase,
    MutationRecord,
)
from orca_loop.orca_client import (
    OrcaCommandError,
    OrcaTimeoutError,
    commit_mutation,
    execute_mutation,
)
from tests.fakes import FakeOrcaClient


class MutationJournalTest(unittest.TestCase):
    """B-02: an unknown mutation result must never duplicate its effect."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name).resolve() / "control"
        self.control.mkdir(parents=True)
        self.argv = ("orchestration", "task-create", "--run", "run_1")

    def _client(self, result: dict[str, object]):
        calls: list[tuple[str, ...]] = []

        def handler(argv, _):
            calls.append(argv)
            return result

        return FakeOrcaClient(handler), calls

    def _timeout_client(self):
        def handler(argv, _):
            raise OrcaTimeoutError("response lost")

        return FakeOrcaClient(handler)

    def _execute(self, client, argv=None, generation=0):
        return execute_mutation(
            client,
            self.control,
            kind=MutationKind.TASK_CREATE,
            argv=self.argv if argv is None else argv,
            timeout_ms=1_000,
            run_id="run-1",
            generation=generation,
            external_id_keys=("task",),
        )

    def test_intent_is_durable_before_the_effect_is_attempted(self) -> None:
        observed: list[tuple[MutationPhase, ...]] = []

        def handler(argv, _):
            # The record has to be on disk while the effect is still in flight.
            observed.append(
                tuple(item.phase for item in read_mutations(self.control))
            )
            return {"task": {"id": "task_1"}}

        _, record = self._execute(FakeOrcaClient(handler))
        self.assertEqual([(MutationPhase.INTENT,)], observed)
        self.assertEqual(MutationPhase.APPLIED, record.phase)
        self.assertEqual("task_1", record.external_id)

    def test_call_carries_the_recorded_retry_request(self) -> None:
        client, calls = self._client({"task": {"id": "task_1"}})
        _, record = self._execute(client)
        self.assertIn("--retry-request", calls[0])
        self.assertEqual(
            record.request_id,
            calls[0][calls[0].index("--retry-request") + 1],
        )

    def test_replay_after_lost_response_reuses_the_request_id(self) -> None:
        with self.assertRaises(OrcaTimeoutError):
            self._execute(self._timeout_client())
        pending = read_mutations(self.control)
        self.assertEqual(1, len(pending))
        self.assertEqual(MutationPhase.INTENT, pending[0].phase)

        # The next process replays the same request, so Orca hands back the
        # original object instead of creating a second one.
        client, calls = self._client({"task": {"id": "task_1"}})
        _, record = self._execute(client)
        self.assertEqual(pending[0].request_id, record.request_id)
        self.assertEqual(
            pending[0].request_id,
            calls[0][calls[0].index("--retry-request") + 1],
        )
        self.assertEqual(1, len(read_mutations(self.control)))

    def test_unresolved_mutation_with_different_argv_fails_closed(self) -> None:
        with self.assertRaises(OrcaTimeoutError):
            self._execute(self._timeout_client())
        client, _ = self._client({"task": {"id": "task_1"}})
        with self.assertRaisesRegex(OrcaCommandError, "different argv"):
            self._execute(client, argv=(*self.argv, "--task-title", "other"))

    def test_committed_mutation_is_not_replayed(self) -> None:
        client, _ = self._client({"task": {"id": "task_1"}})
        _, first = self._execute(client)
        commit_mutation(self.control, first)
        _, second = self._execute(client, generation=1)
        self.assertNotEqual(first.request_id, second.request_id)

    def test_phase_cannot_move_backwards(self) -> None:
        record = MutationRecord(
            schema_version=1,
            request_id="req-1",
            kind=MutationKind.TASK_CREATE,
            phase=MutationPhase.COMMITTED,
            run_id="run-1",
            generation=0,
            step_id=None,
            canonical_argv=self.argv,
        )
        write_mutation(self.control, record)
        with self.assertRaisesRegex(GenerationMismatchError, "cannot move"):
            write_mutation(
                self.control,
                replace(record, phase=MutationPhase.INTENT),
            )

    def test_recorded_argv_is_immutable(self) -> None:
        record = MutationRecord(
            schema_version=1,
            request_id="req-1",
            kind=MutationKind.TASK_CREATE,
            phase=MutationPhase.INTENT,
            run_id="run-1",
            generation=0,
            step_id=None,
            canonical_argv=self.argv,
        )
        write_mutation(self.control, record)
        with self.assertRaisesRegex(GenerationMismatchError, "immutable"):
            write_mutation(
                self.control,
                replace(record, canonical_argv=("orchestration", "dispatch")),
            )

    def test_argv_must_not_carry_its_own_retry_request(self) -> None:
        client, _ = self._client({})
        with self.assertRaisesRegex(OrcaCommandError, "own --retry-request"):
            self._execute(client, argv=(*self.argv, "--retry-request", "r-1"))

    def test_corrupt_store_is_rejected_rather_than_ignored(self) -> None:
        (self.control / "orchestration-operations.json").write_text(
            "{not json",
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            read_mutations(self.control)


if __name__ == "__main__":
    unittest.main()
