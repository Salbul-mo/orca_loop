from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from orca_loop.escalation import (
    GateProtocolError,
    build_user_decision_report,
    create_gate,
    destructive_gate,
    ensure_user_decision_notice,
    find_gate_for_report,
    read_user_decision_notice,
    read_user_decision_notice_delivery,
    resolve_user_decision_notice,
    route_gate,
    wait_gate_resolution,
    write_user_decision_notice_delivery,
)
from orca_loop.ledger import empty_ledger
from orca_loop.models import (
    AffectedFile,
    AffectedFileOperation,
    ConsensusLedger,
    CoordinatorState,
    DecisionValue,
    FindingDecision,
    FindingRecord,
    FindingStatus,
    GateBinding,
    GateKind,
    HumanDecision,
    NoticeChannel,
    NoticeChannelDelivery,
    HumanDecisionKind,
    LoopCounters,
    LoopState,
    PlanDocument,
    Role,
    RunStatus,
    ScopeManifest,
    SignalKind,
    SnapshotIdentity,
    Side,
    StepStage,
    TestContract,
    UserDecisionNoticeDeliveryStatus,
    UserDecisionNoticeStatus,
)
from tests.fakes import FakeOrcaClient
from tests.test_ledger import DIGEST_A, decision, finding


def state() -> CoordinatorState:
    return CoordinatorState(
        schema_version=1,
        generation=0,
        run_id="run-1",
        state=LoopState.USER_DECISION_REQUIRED,
        step_stage=StepStage.TRANSITION_COMMITTED,
        status=RunStatus.BLOCKED,
        worktree_selector="path:C:\\fixture",
        coordinator_handle="term-coordinator",
        worker_handles=(),
        active=None,
        plan_version=1,
        counters=LoopCounters(0, 0),
        base_head="abc",
        snapshot_digest=DIGEST_A,
        test_gate_status=None,
        test_policy_digest=None,
        permission_report_digest=DIGEST_A,
        history=(),
    )


class UserDecisionNoticeTest(unittest.TestCase):
    def test_pending_notice_is_durable_idempotent_and_resolvable(self) -> None:
        binding = GateBinding(
            gate_id="gate-1",
            task_id="task-1",
            report_digest="sha256:" + "d" * 64,
            gate_kind=GateKind.ESCALATION,
            allowed_options=("merge", "reject", "revise_design"),
        )
        current = replace(
            state(),
            orchestration_run_id="orca-run-1",
            gate_binding=binding,
        )
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            first = ensure_user_decision_notice(
                control,
                state=current,
                binding=binding,
                report_path=control.parent / "user-decision.md",
            )
            second = ensure_user_decision_notice(
                control,
                state=current,
                binding=binding,
                report_path=control.parent / "user-decision.md",
            )
            self.assertEqual(first, second)
            loaded = read_user_decision_notice(control)
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(UserDecisionNoticeStatus.PENDING, loaded.status)
            resolved = resolve_user_decision_notice(control, binding=binding)
            self.assertIsNotNone(resolved)
            assert resolved is not None
            self.assertEqual(UserDecisionNoticeStatus.RESOLVED, resolved.status)
            self.assertIsNotNone(resolved.resolved_at)

    def test_notice_delivery_has_strict_success_and_failure_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            delivery = write_user_decision_notice_delivery(
                control,
                request_id="notice-1",
                channels=(
                    NoticeChannelDelivery(
                        channel=NoticeChannel.ORCA_BOARD,
                        status=UserDecisionNoticeDeliveryStatus.DELIVERED,
                        attempted_at="2026-08-13T00:00:00+00:00",
                        detail=None,
                    ),
                    NoticeChannelDelivery(
                        channel=NoticeChannel.OS_TOAST,
                        status=UserDecisionNoticeDeliveryStatus.SKIPPED,
                        attempted_at="2026-08-13T00:00:00+00:00",
                        detail="platform does not support OS toast",
                    ),
                ),
            )
            self.assertEqual(2, len(delivery.channels))
            self.assertEqual(
                delivery,
                read_user_decision_notice_delivery(control),
            )
            with self.assertRaisesRegex(Exception, "requires detail"):
                write_user_decision_notice_delivery(
                    control,
                    request_id="notice-1",
                    channels=(
                        NoticeChannelDelivery(
                            channel=NoticeChannel.ORCA_BOARD,
                            status=UserDecisionNoticeDeliveryStatus.FAILED,
                            attempted_at="2026-08-13T00:00:00+00:00",
                            detail=None,
                        ),
                    ),
                )
            with self.assertRaisesRegex(Exception, "duplicate notice channel"):
                write_user_decision_notice_delivery(
                    control,
                    request_id="notice-1",
                    channels=(
                        NoticeChannelDelivery(
                            channel=NoticeChannel.ORCA_BOARD,
                            status=UserDecisionNoticeDeliveryStatus.DELIVERED,
                            attempted_at="2026-08-13T00:00:00+00:00",
                            detail=None,
                        ),
                        NoticeChannelDelivery(
                            channel=NoticeChannel.ORCA_BOARD,
                            status=UserDecisionNoticeDeliveryStatus.DELIVERED,
                            attempted_at="2026-08-13T00:00:00+00:00",
                            detail=None,
                        ),
                    ),
                )

    def test_legacy_delivery_record_migrates_to_the_board_channel(self) -> None:
        """Schema 1 files exist on disk, so the reader must promote them."""
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            (control / "user-decision-notice-delivery.json").write_text(
                json.dumps(
                    {
                        "attempted_at": "2026-08-13T07:39:53.947899+00:00",
                        "error": None,
                        "request_id": "notice-c45f93eaef1a23c72bdc02b8",
                        "schema_version": 1,
                        "status": "DELIVERED",
                    }
                ),
                encoding="utf-8",
            )

            delivery = read_user_decision_notice_delivery(control)

            self.assertIsNotNone(delivery)
            assert delivery is not None
            self.assertEqual(2, delivery.schema_version)
            self.assertEqual("notice-c45f93eaef1a23c72bdc02b8", delivery.request_id)
            self.assertEqual(1, len(delivery.channels))
            channel = delivery.channels[0]
            self.assertEqual(NoticeChannel.ORCA_BOARD, channel.channel)
            self.assertEqual(
                UserDecisionNoticeDeliveryStatus.DELIVERED,
                channel.status,
            )
            self.assertIsNone(channel.detail)

    def test_legacy_failed_delivery_keeps_its_error_as_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            (control / "user-decision-notice-delivery.json").write_text(
                json.dumps(
                    {
                        "attempted_at": "2026-08-13T07:39:53.947899+00:00",
                        "error": "metadata unavailable",
                        "request_id": "notice-1",
                        "schema_version": 1,
                        "status": "FAILED",
                    }
                ),
                encoding="utf-8",
            )

            delivery = read_user_decision_notice_delivery(control)

            assert delivery is not None
            self.assertEqual("metadata unavailable", delivery.channels[0].detail)

    def test_unsupported_delivery_schema_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            control = Path(directory).resolve()
            (control / "user-decision-notice-delivery.json").write_text(
                json.dumps({"schema_version": 3}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "unsupported"):
                read_user_decision_notice_delivery(control)


def ledger_with_unresolved(evidence_path: str) -> ConsensusLedger:
    item = finding()
    item = type(item)(
        **{
            **item.__dict__,
            "evidence_refs": (evidence_path,),
        }
    )
    record = FindingRecord(
        finding=item,
        status=FindingStatus.CHANGE_REQUIRED,
        opened_round=1,
        resolved_round=None,
        max_status_reached=FindingStatus.CHANGE_REQUIRED,
        unresolved_signature_history=(),
        resolved_snapshot_digest=None,
        decisions=tuple(
            decision("F-1", side, value, 1)
            for side, value in (
                (Side.CLAUDE, DecisionValue.APPROVE),
                (Side.CODEX, DecisionValue.CHANGE_REQUIRED),
            )
        ),
        resolution=None,
    )
    return type(empty_ledger("run-1"))(
        **{
            **empty_ledger("run-1").__dict__,
            "findings": (record,),
            "plan_round": 2,
        }
    )


class DecisionReportTest(unittest.TestCase):
    def test_report_contains_all_twelve_sections_and_unresolved_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "evidence.json").write_text("{}", encoding="utf-8")
            report = build_user_decision_report(
                output_path=root / "user-decision.md",
                request_text="Implement the requested workflow.",
                ledger=ledger_with_unresolved("evidence.json"),
                triggers=(),
                state=state(),
                worktree_path=root,
                test_status=None,
            )
            text = report.path.read_text(encoding="utf-8")
            for number in range(1, 13):
                self.assertIn(f"## {number}.", text)
            self.assertEqual(("F-1",), report.finding_ids)
            self.assertIn("CLAUDE=APPROVE,CODEX=CHANGE_REQUIRED", text)


class GateLifecycleTest(unittest.TestCase):
    def test_create_wait_and_route_gate(self) -> None:
        digest = "sha256:" + "d" * 64

        def handler(argv: tuple[str, ...], _: int) -> dict[str, object]:
            if argv[1] == "gate-create":
                return {
                    "gate": {"id": "gate-1", "task_id": "task-1"},
                    "mutation": {"replayed": False},
                }
            return {
                "gates": [
                    {
                        "id": "gate-1",
                        "resolution": {
                            "decision": "merge",
                            "decision_note": None,
                            "affected_acceptance_criteria": [],
                            "affected_finding_ids": [],
                            "report_digest": digest,
                        },
                    }
                ]
            }

        client = FakeOrcaClient(handler)
        report = type("Report", (), {
            "path": Path("user-decision.md"),
            "digest": digest,
            "finding_ids": (),
        })()
        binding, mutation = create_gate(
            client,
            task_id="task-1",
            report=report,
            gate_kind=GateKind.FINAL,
            question="Choose the final disposition.",
            options=("merge", "reject", "revise_code", "revise_design"),
            timeout_ms=1000,
        )
        self.assertIsNone(mutation)
        decision_value = wait_gate_resolution(
            client,
            binding=binding,
            timeout_ms=1000,
        )
        self.assertEqual(
            SignalKind.MERGE,
            route_gate(decision_value, gate_kind=GateKind.FINAL).kind,
        )

    def test_find_gate_for_report_recovers_nested_current_shape(self) -> None:
        digest = "sha256:" + "d" * 64
        report = type("Report", (), {
            "path": Path(r"C:\run\user-decision.md"),
            "digest": digest,
            "finding_ids": (),
        })()
        client = FakeOrcaClient(
            lambda _argv, _timeout: {
                "runId": "run-1",
                "gates": [
                    {
                        "id": "gate-1",
                        "task_id": "task-1",
                        "question": (
                            "Resolve the bounded disagreement. Review "
                            f"{report.path} for the final decision. "
                            f"Report digest: {digest}."
                        ),
                        "status": "pending",
                    }
                ],
            }
        )

        binding = find_gate_for_report(
            client,
            report=report,
            gate_kind=GateKind.ESCALATION,
            timeout_ms=1000,
        )

        self.assertIsNotNone(binding)
        self.assertEqual("gate-1", binding.gate_id)
        self.assertEqual("task-1", binding.task_id)
        self.assertEqual(digest, binding.report_digest)

    def test_find_gate_for_report_ignores_stale_report_path_match(self) -> None:
        digest = "sha256:" + "d" * 64
        stale_digest = "sha256:" + "e" * 64
        report = type("Report", (), {
            "path": Path(r"C:\run\user-decision.md"),
            "digest": digest,
            "finding_ids": (),
        })()
        client = FakeOrcaClient(
            lambda _argv, _timeout: {
                "gates": [
                    {
                        "id": "gate-1",
                        "task_id": "task-1",
                        "question": (
                            "Resolve the bounded disagreement. Review "
                            f"{report.path} for the final decision. "
                            f"Report digest: {stale_digest}."
                        ),
                        "status": "resolved",
                    }
                ],
            }
        )

        binding = find_gate_for_report(
            client,
            report=report,
            gate_kind=GateKind.ESCALATION,
            timeout_ms=1000,
        )

        self.assertIsNone(binding)

    def test_stale_report_digest_is_rejected(self) -> None:
        client = FakeOrcaClient(
            lambda _argv, _timeout: {
                "gates": [
                    {
                        "id": "gate-1",
                        "resolution": json.dumps(
                            {
                                "decision": "reject",
                                "decision_note": None,
                                "affected_acceptance_criteria": [],
                                "affected_finding_ids": [],
                                "report_digest": "sha256:" + "e" * 64,
                            }
                        ),
                    }
                ]
            }
        )
        binding = create_binding(
            "sha256:" + "d" * 64,
        )
        with self.assertRaisesRegex(GateProtocolError, "stale"):
            wait_gate_resolution(
                client,
                binding=binding,
                timeout_ms=1000,
            )

    def test_unresolved_gate_reads_as_pending_rather_than_an_error(self) -> None:
        client = FakeOrcaClient(lambda _argv, _timeout: {"gates": []})

        decision_value = wait_gate_resolution(
            client,
            binding=create_binding("sha256:" + "d" * 64),
            timeout_ms=1000,
        )

        self.assertIsNone(decision_value)

    def test_ambiguous_gate_identity_is_not_mistaken_for_pending(self) -> None:
        """Two resolved gates for one binding must surface, not read as waiting."""
        client = FakeOrcaClient(
            lambda _argv, _timeout: {
                "gates": [
                    {"id": "gate-1", "resolution": "merge"},
                    {"id": "gate-1", "resolution": "reject"},
                ]
            }
        )

        with self.assertRaisesRegex(GateProtocolError, "found 2"):
            wait_gate_resolution(
                client,
                binding=create_binding("sha256:" + "d" * 64),
                timeout_ms=1000,
            )

    def test_simple_terminal_gate_options_use_bound_report_digest(self) -> None:
        digest = "sha256:" + "d" * 64
        binding = create_binding(digest)

        for resolution, expected in (
            ("merge", HumanDecisionKind.MERGE),
            ("reject", HumanDecisionKind.REJECT),
        ):
            with self.subTest(resolution=resolution):
                client = FakeOrcaClient(
                    lambda _argv, _timeout, value=resolution: {
                        "gates": [
                            {
                                "id": "gate-1",
                                "resolution": value,
                            }
                        ]
                    }
                )

                decision_value = wait_gate_resolution(
                    client,
                    binding=binding,
                    timeout_ms=1000,
                )

                self.assertEqual(expected, decision_value.decision)
                self.assertEqual(digest, decision_value.report_digest)
                self.assertEqual((), decision_value.affected_finding_ids)

    def test_simple_revision_gate_option_still_requires_scope(self) -> None:
        client = FakeOrcaClient(
            lambda _argv, _timeout: {
                "gates": [
                    {
                        "id": "gate-1",
                        "resolution": "revise_design",
                    }
                ]
            }
        )

        with self.assertRaisesRegex(GateProtocolError, "JSON object"):
            wait_gate_resolution(
                client,
                binding=create_binding("sha256:" + "d" * 64),
                timeout_ms=1000,
            )


def create_binding(report_digest: str):
    from orca_loop.models import GateBinding

    return GateBinding(
        gate_id="gate-1",
        task_id="task-1",
        report_digest=report_digest,
        gate_kind=GateKind.FINAL,
    )


class DestructiveGateTest(unittest.TestCase):
    def test_no_destructive_operation_needs_no_gate(self) -> None:
        plan = PlanDocument(
            schema_version=1,
            plan_version=1,
            request_digest=DIGEST_A,
            source_instruction="request",
            interpretation="interpretation",
            rationale="rationale",
            current_state_evidence=("evidence",),
            affected_files=(
                AffectedFile(
                    "src/a.py",
                    AffectedFileOperation.MODIFY,
                    None,
                ),
            ),
            implementation_steps=("change",),
            data_api_schema_changes="없음",
            error_handling=("raise",),
            test_contract=TestContract((), ()),
            test_policy_digest=DIGEST_A,
            acceptance_criteria=(),
            risks=("risk",),
            out_of_scope=("other",),
            reviewed_finding_ids=(),
            finding_decisions=(),
        )
        snapshot = SnapshotIdentity("head", DIGEST_A, DIGEST_A, (), DIGEST_A)
        manifest = ScopeManifest(DIGEST_A, plan.affected_files, None)
        approval, signal = destructive_gate(
            run_id="run-1",
            plan=plan,
            manifest=manifest,
            snapshot=snapshot,
            binding=None,
            decision=None,
        )
        self.assertIsNone(approval)
        self.assertEqual(SignalKind.OK, signal.kind)

    def test_revision_decision_requires_scope_in_contract_parser(self) -> None:
        decision_value = HumanDecision(
            HumanDecisionKind.REVISE_CODE,
            "Change the response contract.",
            ("AC-1",),
            (),
            DIGEST_A,
        )
        self.assertEqual(
            SignalKind.REVISE_CODE,
            route_gate(decision_value, gate_kind=GateKind.FINAL).kind,
        )
