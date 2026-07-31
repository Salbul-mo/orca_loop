from __future__ import annotations

import unittest

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
    DecisionValue,
    Finding,
    FindingDecision,
    FindingRecord,
    FindingStatus,
    ImpactClass,
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
) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=Severity.P1,
        blocking_reason=BlockingReason.B1,
        impact_class=ImpactClass.NONE,
        file="src/example.py",
        line=1,
        root_cause="The state transition is missing.",
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


if __name__ == "__main__":
    unittest.main()
