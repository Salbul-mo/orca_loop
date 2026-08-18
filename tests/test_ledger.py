from __future__ import annotations

import unittest
from dataclasses import replace

from orca_loop.ledger import (
    apply_review_artifact,
    commit_round,
    empty_ledger,
    unresolved_scope,
)
from orca_loop.models import (
    ArtifactKind,
    BlockingReason,
    CodeReviewVerdict,
    ConsensusKind,
    ConsensusLedger,
    DecisionValue,
    EscalationCode,
    Finding,
    FindingDecision,
    FindingRecord,
    FindingStatus,
    ImpactClass,
    LedgerUpdate,
    PlanReviewVerdict,
    ReviewArtifact,
    Role,
    RoundEvidence,
    Severity,
    Side,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def finding(
    finding_id: str = "F-1",
    *,
    depends_on: tuple[str, ...] = (),
    blocking_reason: BlockingReason = BlockingReason.B1,
    impact_class: ImpactClass = ImpactClass.NONE,
    root_cause: str = "The state transition is missing.",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=Severity.P1,
        blocking_reason=blocking_reason,
        impact_class=impact_class,
        file="src/example.py",
        line=1,
        root_cause=root_cause,
        description="The requested transition is not implemented.",
        required_fix="Implement the transition.",
        required_change=None,
        acceptance_criteria_ids=("AC-1",),
        affected_files=("src/example.py",),
        test_ids=("T-1",),
        depends_on=depends_on,
        evidence_refs=("review.json",),
        reopens=None,
    )


def decision(
    finding_id: str,
    side: Side,
    value: DecisionValue,
    round_value: int,
) -> FindingDecision:
    return FindingDecision(
        finding_id=finding_id,
        side=side,
        decision=value,
        snapshot_digest=DIGEST_A,
        round=round_value,
        evidence_refs=("evidence",),
    )


def review(
    *,
    side: Side,
    decisions: tuple[FindingDecision, ...],
    findings: tuple[Finding, ...] = (),
    reviewed: tuple[str, ...] = (),
    round_value: int = 1,
) -> ReviewArtifact:
    kind = (
        ArtifactKind.PLAN_REVIEW
        if side is Side.CODEX
        else ArtifactKind.CROSS_REVIEW
    )
    verdict = (
        PlanReviewVerdict.REVISE
        if kind is ArtifactKind.PLAN_REVIEW
        else CodeReviewVerdict.CHANGES_REQUESTED
    )
    return ReviewArtifact(
        schema_version=1,
        artifact_kind=kind,
        run_id="run-1",
        task_id=f"task-{side.value.lower()}",
        dispatch_id=f"dispatch-{side.value.lower()}",
        consensus_round=round_value,
        snapshot_digest=DIGEST_A,
        role=(
            Role.PLAN_REVIEWER
            if side is Side.CODEX
            else Role.CROSS_CONFIRMER
        ),
        verdict=verdict,
        reviewed_plan_version=1,
        reviewed_artifact_digest=DIGEST_B,
        reviewed_finding_ids=reviewed,
        finding_decisions=decisions,
        findings=findings,
        non_blocking_suggestions=(),
        escalation_signals=(),
        agrees_with_reviewer=(
            True if kind is ArtifactKind.CROSS_REVIEW else None
        ),
    )


def commit_plan(ledger: ConsensusLedger, round_value: int) -> LedgerUpdate:
    return commit_round(
        ledger,
        RoundEvidence(
            ConsensusKind.PLAN,
            round_value,
            1,
            None,
            (DIGEST_A, DIGEST_B),
            False,
            True,
        ),
        plan_limit=5,
        code_limit=5,
        expected_plan_version=1,
    )


def opened_ledger(
    item: Finding,
    *,
    claude_decision: DecisionValue | None = None,
) -> ConsensusLedger:
    ledger = apply_review_artifact(
        empty_ledger("run-1"),
        review(
            side=Side.CODEX,
            findings=(item,),
            decisions=(
                decision(
                    item.finding_id,
                    Side.CODEX,
                    DecisionValue.CHANGE_REQUIRED,
                    1,
                ),
            ),
        ),
        Side.CODEX,
    ).ledger
    if claude_decision is None:
        return ledger
    return apply_review_artifact(
        ledger,
        review(
            side=Side.CLAUDE,
            reviewed=(item.finding_id,),
            decisions=(
                decision(
                    item.finding_id,
                    Side.CLAUDE,
                    claude_decision,
                    1,
                ),
            ),
        ),
        Side.CLAUDE,
    ).ledger


def keys(update: LedgerUpdate) -> tuple[str, ...]:
    return tuple(item.deduplication_key for item in update.escalations)


class LedgerLifecycleTest(unittest.TestCase):
    def test_single_side_never_resolves_but_dual_approval_does(self) -> None:
        ledger = empty_ledger("run-1")
        first = apply_review_artifact(
            ledger,
            review(
                side=Side.CODEX,
                findings=(finding(),),
                decisions=(
                    decision(
                        "F-1",
                        Side.CODEX,
                        DecisionValue.APPROVE,
                        1,
                    ),
                ),
            ),
            Side.CODEX,
        ).ledger
        self.assertEqual(FindingStatus.OPEN, first.findings[0].status)
        second = apply_review_artifact(
            first,
            review(
                side=Side.CLAUDE,
                reviewed=("F-1",),
                decisions=(
                    decision(
                        "F-1",
                        Side.CLAUDE,
                        DecisionValue.APPROVE,
                        1,
                    ),
                ),
            ),
            Side.CLAUDE,
        ).ledger
        self.assertEqual(FindingStatus.RESOLVED, second.findings[0].status)
        empty = apply_review_artifact(
            second,
            review(
                side=Side.CODEX,
                decisions=(),
                findings=(),
                reviewed=(),
                round_value=2,
            ),
            Side.CODEX,
        ).ledger
        self.assertEqual(1, len(empty.findings))

    def test_same_unresolved_signature_escalates_on_second_valid_round(self) -> None:
        ledger = apply_review_artifact(
            empty_ledger("run-1"),
            review(
                side=Side.CODEX,
                findings=(finding(),),
                decisions=(
                    decision(
                        "F-1",
                        Side.CODEX,
                        DecisionValue.CHANGE_REQUIRED,
                        1,
                    ),
                ),
            ),
            Side.CODEX,
        ).ledger
        first = commit_round(
            ledger,
            RoundEvidence(
                ConsensusKind.PLAN,
                1,
                1,
                None,
                (DIGEST_A, DIGEST_B),
                False,
                True,
            ),
            plan_limit=5,
            code_limit=5,
            expected_plan_version=1,
        )
        self.assertTrue(first.committed_round)
        self.assertFalse(first.escalations)
        second = commit_round(
            first.ledger,
            RoundEvidence(
                ConsensusKind.PLAN,
                2,
                1,
                None,
                (DIGEST_A, DIGEST_B),
                False,
                True,
            ),
            plan_limit=5,
            code_limit=5,
            expected_plan_version=1,
        )
        self.assertEqual("E-05", second.escalations[0].code.value)
        retry = commit_round(
            first.ledger,
            RoundEvidence(
                ConsensusKind.PLAN,
                2,
                1,
                None,
                (DIGEST_A, DIGEST_B),
                True,
                True,
            ),
            plan_limit=5,
            code_limit=5,
            expected_plan_version=1,
        )
        self.assertFalse(retry.committed_round)
        self.assertEqual(1, retry.ledger.plan_round)

    def test_dependency_closure_terminates_cycle_and_excludes_resolved(self) -> None:
        record_a = FindingRecord(
            finding("F-A", depends_on=("F-B",)),
            FindingStatus.CHANGE_REQUIRED,
            1,
            None,
            FindingStatus.CHANGE_REQUIRED,
            (),
            None,
            (),
            None,
        )
        record_b = FindingRecord(
            finding("F-B", depends_on=("F-A",)),
            FindingStatus.RESOLVED,
            1,
            2,
            FindingStatus.RESOLVED,
            (),
            DIGEST_A,
            (),
            "resolved",
        )
        ledger = empty_ledger("run-1")
        ledger = ledger.__class__(
            **{
                **ledger.__dict__,
                "findings": (record_a, record_b),
            }
        )
        scope = unresolved_scope(ledger)
        self.assertEqual(("F-A",), scope.finding_ids)
        self.assertEqual(("AC-1",), scope.acceptance_criteria_ids)


class LedgerEscalationTest(unittest.TestCase):
    def test_b5_finding_escalates_after_two_rounds(self) -> None:
        ledger = opened_ledger(finding(blocking_reason=BlockingReason.B5))
        first = commit_plan(ledger, 1)
        self.assertTrue(first.committed_round)
        self.assertNotIn("E-05:B5:F-1", keys(first))
        second = commit_plan(first.ledger, 2)
        self.assertIn("E-05:B5:F-1", keys(second))
        self.assertEqual(
            {EscalationCode.E05},
            {item.code for item in second.escalations},
        )

    def test_b5_escalation_survives_root_cause_rewording(self) -> None:
        ledger = opened_ledger(finding(blocking_reason=BlockingReason.B5))
        first = commit_plan(ledger, 1)
        record = first.ledger.findings[0]
        reworded = replace(
            first.ledger,
            findings=(
                replace(
                    record,
                    finding=replace(
                        record.finding,
                        root_cause=(
                            "Still undecidable, now for another reason."
                        ),
                    ),
                ),
            ),
        )
        second = commit_plan(reworded, 2)
        self.assertEqual(("E-05:B5:F-1",), keys(second))

    def test_b1_finding_does_not_trigger_b5_escalation(self) -> None:
        first = commit_plan(opened_ledger(finding()), 1)
        second = commit_plan(first.ledger, 2)
        self.assertNotIn("E-05:B5:F-1", keys(second))
        self.assertTrue(
            any(item.startswith("E-05:F-1:") for item in keys(second))
        )

    def test_b4_finding_escalates_e04_without_security_impact_class(
        self,
    ) -> None:
        ledger = opened_ledger(
            finding(blocking_reason=BlockingReason.B4),
            claude_decision=DecisionValue.APPROVE,
        )
        update = commit_plan(ledger, 1)
        self.assertEqual(("E-04:F-1",), keys(update))
        self.assertEqual(
            ImpactClass.NONE,
            update.ledger.findings[0].finding.impact_class,
        )

    def test_b4_finding_without_conflict_does_not_escalate(self) -> None:
        ledger = opened_ledger(
            finding(blocking_reason=BlockingReason.B4),
            claude_decision=DecisionValue.CHANGE_REQUIRED,
        )
        self.assertEqual((), keys(commit_plan(ledger, 1)))

    def test_security_auth_escalation_is_unchanged(self) -> None:
        ledger = opened_ledger(
            finding(impact_class=ImpactClass.SECURITY_AUTH),
            claude_decision=DecisionValue.APPROVE,
        )
        self.assertEqual(("E-04:F-1",), keys(commit_plan(ledger, 1)))

    def test_contract_impact_class_escalates_e03(self) -> None:
        for value in (ImpactClass.DB_SCHEMA, ImpactClass.EXTERNAL_API):
            with self.subTest(impact_class=value.value):
                update = commit_plan(
                    opened_ledger(finding(impact_class=value)),
                    1,
                )
                self.assertEqual(("E-03:F-1",), keys(update))
                self.assertEqual(
                    EscalationCode.E03,
                    update.escalations[0].code,
                )


if __name__ == "__main__":
    unittest.main()
