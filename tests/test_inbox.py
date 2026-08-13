from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from orca_loop.dispatcher import (
    DispatchProvenanceError,
    classify_delivery,
    parse_delivery,
)
from orca_loop.generation import (
    find_receipt,
    is_promoted,
    mark_promoted,
    read_inbox,
    record_receipt,
)
from orca_loop.models import (
    DeliveryReceipt,
    InboxClassification,
)


def _row(
    message_id: str,
    message_type: str = "worker_done",
    task_id: str = "task-1",
    dispatch_id: str = "ctx-1",
    **extra: object,
) -> dict[str, object]:
    return {
        "id": message_id,
        "type": message_type,
        "from_handle": "term-worker",
        "run_id": "run_1",
        "payload": json.dumps(
            {"taskId": task_id, "dispatchId": dispatch_id, **extra}
        ),
    }


class DeliveryParsingTest(unittest.TestCase):
    """M-B03-01: every delivered row is understood or the batch fails."""

    def test_live_envelope_shape_round_trips(self) -> None:
        delivery_id, envelopes = parse_delivery(
            {
                "deliveryId": "delivery-1",
                "messages": [_row("msg-1", artifactDigest="sha256:" + "a" * 64)],
            }
        )
        self.assertEqual("delivery-1", delivery_id)
        self.assertEqual(1, len(envelopes))
        envelope = envelopes[0]
        self.assertEqual("msg-1", envelope.message_id)
        self.assertEqual("worker_done", envelope.message_type)
        self.assertEqual("term-worker", envelope.from_handle)
        self.assertEqual("run_1", envelope.run_id)
        self.assertEqual("task-1", envelope.task_id)
        self.assertEqual("ctx-1", envelope.dispatch_id)

    def test_row_without_an_id_is_rejected(self) -> None:
        row = _row("msg-1")
        del row["id"]
        with self.assertRaisesRegex(DispatchProvenanceError, "has no ID"):
            parse_delivery({"deliveryId": "d-1", "messages": [row]})

    def test_row_without_a_type_is_rejected(self) -> None:
        row = _row("msg-1")
        del row["type"]
        with self.assertRaisesRegex(DispatchProvenanceError, "has no type"):
            parse_delivery({"deliveryId": "d-1", "messages": [row]})

    def test_non_object_row_is_not_silently_skipped(self) -> None:
        with self.assertRaisesRegex(
            DispatchProvenanceError,
            "is not an object",
        ):
            parse_delivery({"deliveryId": "d-1", "messages": ["nope"]})

    def test_envelope_and_payload_id_conflict_is_rejected(self) -> None:
        row = _row("msg-1")
        row["task_id"] = "task-other"
        with self.assertRaisesRegex(
            DispatchProvenanceError,
            "conflicts with its payload",
        ):
            parse_delivery({"deliveryId": "d-1", "messages": [row]})

    def test_every_row_produces_exactly_one_envelope(self) -> None:
        _, envelopes = parse_delivery(
            {
                "deliveryId": "d-1",
                "messages": [_row("m-1"), _row("m-2"), _row("m-3")],
            }
        )
        self.assertEqual(3, len(envelopes))


class InboxClassificationTest(unittest.TestCase):
    """M-B03-02: nothing is dropped, and nothing is promoted twice."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.control = Path(self.temporary.name).resolve() / "control"
        self.control.mkdir(parents=True)

    def _classify(self, rows):
        _, envelopes = parse_delivery(
            {"deliveryId": "d-1", "messages": rows}
        )
        return envelopes, classify_delivery(
            self.control,
            envelopes,
            task_id="task-1",
            dispatch_id="ctx-1",
        )

    def test_mixed_batch_classifies_every_row(self) -> None:
        envelopes, classifications = self._classify(
            [
                _row("m-mine"),
                _row("m-foreign", task_id="task-9", dispatch_id="ctx-9"),
            ]
        )
        self.assertEqual(len(envelopes), len(classifications))
        self.assertEqual(
            [InboxClassification.ACCEPTED, InboxClassification.DEFERRED],
            list(classifications),
        )

    def test_already_promoted_message_is_a_duplicate_not_a_drop(self) -> None:
        # A crash between promoting a worker_done and acknowledging its
        # delivery replays that message.  It must be recognised, not reapplied
        # and not silently discarded.
        mark_promoted(self.control, ("m-mine",))
        _, classifications = self._classify([_row("m-mine")])
        self.assertEqual(
            [InboxClassification.DUPLICATE],
            list(classifications),
        )

    def test_promotion_index_survives_a_reread(self) -> None:
        mark_promoted(self.control, ("m-1", "m-2"))
        self.assertTrue(is_promoted(self.control, "m-1"))
        self.assertTrue(is_promoted(self.control, "m-2"))
        self.assertFalse(is_promoted(self.control, "m-3"))

    def test_promotion_index_does_not_duplicate_entries(self) -> None:
        mark_promoted(self.control, ("m-1",))
        mark_promoted(self.control, ("m-1",))
        self.assertEqual(
            ("m-1",),
            read_inbox(self.control).promoted_message_ids,
        )

    def test_receipt_is_durable_and_updatable(self) -> None:
        envelopes, classifications = self._classify([_row("m-1")])
        receipt = DeliveryReceipt(
            schema_version=1,
            delivery_id="d-1",
            messages=envelopes,
            classifications=classifications,
            acked=False,
        )
        record_receipt(self.control, receipt)
        stored = find_receipt(self.control, "d-1")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertFalse(stored.acked)

        # Acknowledging replaces the row rather than appending a second one.
        from dataclasses import replace

        record_receipt(self.control, replace(stored, acked=True))
        self.assertEqual(1, len(read_inbox(self.control).receipts))
        reread = find_receipt(self.control, "d-1")
        assert reread is not None
        self.assertTrue(reread.acked)

    def test_malformed_payload_is_quarantined_not_acted_on(self) -> None:
        # Observed live: a payload can arrive with its JSON quoting stripped.
        # It must be recorded and acknowledged, never guessed at, and never
        # left to replay forever.
        row = _row("m-bad")
        row["payload"] = "{schema_version:1,taskId:task-1}"
        _, classifications = self._classify([row])
        self.assertEqual(
            [InboxClassification.QUARANTINED],
            list(classifications),
        )

    def test_quarantined_payload_is_preserved_verbatim(self) -> None:
        row = _row("m-bad")
        row["payload"] = "{not json at all"
        _, envelopes = parse_delivery(
            {"deliveryId": "d-1", "messages": [row]}
        )
        self.assertEqual("{not json at all", envelopes[0].payload_json)

    def test_receipt_requires_one_classification_per_message(self) -> None:
        envelopes, _ = self._classify([_row("m-1"), _row("m-2")])
        with self.assertRaises(Exception):
            record_receipt(
                self.control,
                DeliveryReceipt(
                    schema_version=1,
                    delivery_id="d-1",
                    messages=envelopes,
                    classifications=(InboxClassification.ACCEPTED,),
                    acked=False,
                ),
            )


if __name__ == "__main__":
    unittest.main()
