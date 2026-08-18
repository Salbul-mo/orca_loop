from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import replace

from .models import (
    BlockingReason,
    ConsensusKind,
    ConsensusLedger,
    DecisionValue,
    EscalationCode,
    EscalationTrigger,
    Finding,
    FindingDecision,
    FindingRecord,
    FindingStatus,
    ImpactClass,
    ImplementationArtifact,
    LedgerUpdate,
    PlanDocument,
    ReopenedFinding,
    ReviewArtifact,
    RoundEvidence,
    ScopePackage,
    Side,
    SignatureObservation,
    TestFailureAttribution,
)


class LedgerIntegrityError(RuntimeError):
    """Raised when finding history violates ledger invariants."""


class InvalidRoundError(LedgerIntegrityError):
    """Raised when evidence cannot commit the requested consensus round."""


UNRESOLVED_STATUSES = {
    FindingStatus.OPEN,
    FindingStatus.CHANGE_REQUIRED,
    FindingStatus.VERIFY_REQUIRED,
}
STATUS_RANK = {
    FindingStatus.OPEN: 0,
    FindingStatus.CHANGE_REQUIRED: 1,
    FindingStatus.VERIFY_REQUIRED: 2,
}
MARKDOWN_PUNCTUATION = re.compile(r"""[.,;:!?"'`]""")
MARKDOWN_MARKERS = re.compile(r"[*_~]+")
WHITESPACE = re.compile(r"\s+")


def empty_ledger(run_id: str) -> ConsensusLedger:
    if not run_id:
        raise LedgerIntegrityError("run_id must be nonempty")
    return ConsensusLedger(
        schema_version=1,
        run_id=run_id,
        generation=0,
        findings=(),
        plan_round=0,
        code_round=0,
        informational=(),
        reopened=(),
        approved_escalation_keys=(),
    )


def normalize_signature_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = normalized.replace("```", "")
    normalized = MARKDOWN_MARKERS.sub("", normalized)
    normalized = MARKDOWN_PUNCTUATION.sub("", normalized)
    return WHITESPACE.sub(" ", normalized).strip()


def finding_signature(finding: Finding) -> str:
    action = finding.required_change or finding.required_fix
    if not action:
        raise LedgerIntegrityError(
            f"finding {finding.finding_id} has no required action"
        )
    raw = "\x1f".join(
        (
            finding.finding_id,
            normalize_signature_text(finding.root_cause),
            "\x1e".join(sorted(finding.acceptance_criteria_ids)),
            normalize_signature_text(action),
        )
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _latest_side_decisions(
    decisions: tuple[FindingDecision, ...],
) -> dict[Side, FindingDecision]:
    latest: dict[Side, FindingDecision] = {}
    for decision in decisions:
        current = latest.get(decision.side)
        if current is None or decision.round >= current.round:
            latest[decision.side] = decision
    return latest


def _status_from_decisions(
    decisions: tuple[FindingDecision, ...],
) -> tuple[FindingStatus, str | None, str | None]:
    latest = _latest_side_decisions(decisions)
    values = {item.decision for item in latest.values()}
    if DecisionValue.CHANGE_REQUIRED in values:
        return FindingStatus.CHANGE_REQUIRED, None, None
    if DecisionValue.VERIFY_REQUIRED in values:
        return FindingStatus.VERIFY_REQUIRED, None, None
    claude = latest.get(Side.CLAUDE)
    codex = latest.get(Side.CODEX)
    if (
        claude
        and codex
        and claude.decision is DecisionValue.APPROVE
        and codex.decision is DecisionValue.APPROVE
        and claude.snapshot_digest == codex.snapshot_digest
        and claude.evidence_refs
        and codex.evidence_refs
    ):
        return (
            FindingStatus.RESOLVED,
            claude.snapshot_digest,
            "dual approval with matching snapshot and evidence",
        )
    return FindingStatus.OPEN, None, None


def _max_status(
    previous: FindingStatus,
    current: FindingStatus,
) -> FindingStatus:
    if current is FindingStatus.RESOLVED:
        return FindingStatus.RESOLVED
    if previous is FindingStatus.RESOLVED:
        return previous
    return (
        current
        if STATUS_RANK[current] > STATUS_RANK[previous]
        else previous
    )


def _dedupe_escalations(
    values: list[EscalationTrigger],
    approved_keys: tuple[str, ...],
) -> tuple[EscalationTrigger, ...]:
    output: list[EscalationTrigger] = []
    seen = set(approved_keys)
    for value in values:
        if value.deduplication_key in seen:
            continue
        seen.add(value.deduplication_key)
        output.append(value)
    return tuple(output)


def _reopen_trigger(
    finding: Finding,
    existing: FindingRecord,
) -> EscalationTrigger:
    return EscalationTrigger(
        code=EscalationCode.E06,
        reason=(
            f"resolved finding {existing.finding.finding_id} was reopened "
            f"by {finding.finding_id}"
        ),
        evidence_refs=finding.evidence_refs,
        deduplication_key=(
            f"E-06:{existing.finding.finding_id}:{finding.finding_id}"
        ),
    )


def apply_review_artifact(
    ledger: ConsensusLedger,
    artifact: ReviewArtifact,
    side: Side,
) -> LedgerUpdate:
    records = {
        record.finding.finding_id: record
        for record in ledger.findings
    }
    escalations = list(artifact.escalation_signals)
    reopened = list(ledger.reopened)

    for finding in artifact.findings:
        existing = records.get(finding.finding_id)
        if existing is None:
            if finding.reopens:
                reopened_record = records.get(finding.reopens)
                if (
                    reopened_record is None
                    or reopened_record.status is not FindingStatus.RESOLVED
                ):
                    raise LedgerIntegrityError(
                        f"reopens target is not resolved: {finding.reopens}"
                    )
                escalations.append(_reopen_trigger(finding, reopened_record))
                reopened.append(
                    ReopenedFinding(
                        finding_id=finding.finding_id,
                        reopens=finding.reopens,
                        reason=finding.description,
                        evidence_refs=finding.evidence_refs,
                    )
                )
            records[finding.finding_id] = FindingRecord(
                finding=finding,
                status=FindingStatus.OPEN,
                opened_round=artifact.consensus_round,
                resolved_round=None,
                max_status_reached=FindingStatus.OPEN,
                unresolved_signature_history=(),
                resolved_snapshot_digest=None,
                decisions=(),
                resolution=None,
            )
        elif existing.finding != finding:
            raise LedgerIntegrityError(
                f"finding ID collision with changed content: {finding.finding_id}"
            )

    decision_ids = {decision.finding_id for decision in artifact.finding_decisions}
    required_ids = set(artifact.reviewed_finding_ids) | {
        finding.finding_id for finding in artifact.findings
    }
    if decision_ids != required_ids:
        raise LedgerIntegrityError(
            "review decisions must exactly cover reviewed and new findings"
        )
    for decision in artifact.finding_decisions:
        if decision.side is not side:
            raise LedgerIntegrityError(
                f"decision side {decision.side.value} does not match {side.value}"
            )
        record = records.get(decision.finding_id)
        if record is None:
            raise LedgerIntegrityError(
                f"decision references unknown finding {decision.finding_id}"
            )
        decisions = tuple(
            item
            for item in record.decisions
            if not (
                item.side is decision.side
                and item.round == decision.round
            )
        ) + (decision,)
        status, resolved_digest, resolution = _status_from_decisions(decisions)
        resolved_round = (
            artifact.consensus_round
            if status is FindingStatus.RESOLVED
            else record.resolved_round
        )
        records[decision.finding_id] = replace(
            record,
            status=status,
            resolved_round=resolved_round,
            max_status_reached=_max_status(
                record.max_status_reached,
                status,
            ),
            resolved_snapshot_digest=(
                resolved_digest
                if resolved_digest is not None
                else record.resolved_snapshot_digest
            ),
            decisions=decisions,
            resolution=resolution or record.resolution,
        )

    updated = replace(
        ledger,
        findings=tuple(
            records[key]
            for key in sorted(records, key=lambda item: item.encode("utf-8"))
        ),
        informational=ledger.informational
        + artifact.non_blocking_suggestions,
        reopened=tuple(reopened),
    )
    return LedgerUpdate(
        ledger=updated,
        escalations=_dedupe_escalations(
            escalations,
            ledger.approved_escalation_keys,
        ),
        committed_round=False,
    )


def apply_plan_document(
    ledger: ConsensusLedger,
    plan: PlanDocument,
) -> LedgerUpdate:
    records = {
        record.finding.finding_id: record
        for record in ledger.findings
    }
    decision_ids = {
        decision.finding_id for decision in plan.finding_decisions
    }
    if decision_ids != set(plan.reviewed_finding_ids):
        raise LedgerIntegrityError(
            "plan decisions must exactly cover reviewed finding IDs"
        )
    for decision in plan.finding_decisions:
        if decision.side is not Side.CLAUDE:
            raise LedgerIntegrityError(
                "plan decisions must use the primary consensus lane "
                "(legacy CLAUDE wire value)"
            )
        record = records.get(decision.finding_id)
        if record is None or record.status not in UNRESOLVED_STATUSES:
            raise LedgerIntegrityError(
                "plan decision references non-unresolved finding "
                f"{decision.finding_id}"
            )
        decisions = tuple(
            item
            for item in record.decisions
            if not (
                item.side is Side.CLAUDE
                and item.round == decision.round
            )
        ) + (decision,)
        status, resolved_digest, resolution = _status_from_decisions(
            decisions
        )
        records[decision.finding_id] = replace(
            record,
            status=status,
            resolved_round=(
                decision.round
                if status is FindingStatus.RESOLVED
                else record.resolved_round
            ),
            max_status_reached=_max_status(
                record.max_status_reached,
                status,
            ),
            resolved_snapshot_digest=(
                resolved_digest
                if resolved_digest is not None
                else record.resolved_snapshot_digest
            ),
            decisions=decisions,
            resolution=resolution or record.resolution,
        )
    return LedgerUpdate(
        ledger=replace(
            ledger,
            findings=tuple(
                records[key]
                for key in sorted(
                    records,
                    key=lambda item: item.encode("utf-8"),
                )
            ),
        ),
        escalations=(),
        committed_round=False,
    )


def apply_implementation_artifact(
    ledger: ConsensusLedger,
    artifact: ImplementationArtifact,
) -> LedgerUpdate:
    records = {
        record.finding.finding_id: record
        for record in ledger.findings
    }
    for addressed in artifact.addressed_findings:
        record = records.get(addressed.finding_id)
        if record is None or record.status is FindingStatus.RESOLVED:
            raise LedgerIntegrityError(
                f"cannot address finding {addressed.finding_id}"
            )
        implementer_decision = FindingDecision(
            finding_id=addressed.finding_id,
            side=Side.CODEX,
            decision=DecisionValue.VERIFY_REQUIRED,
            snapshot_digest=artifact.snapshot_digest,
            round=artifact.consensus_round,
            evidence_refs=addressed.evidence_refs,
        )
        decisions = record.decisions + (implementer_decision,)
        records[addressed.finding_id] = replace(
            record,
            status=FindingStatus.VERIFY_REQUIRED,
            max_status_reached=_max_status(
                record.max_status_reached,
                FindingStatus.VERIFY_REQUIRED,
            ),
            decisions=decisions,
        )
    escalations = list(artifact.escalation_signals)
    if artifact.test_failure_attribution is TestFailureAttribution.AMBIGUOUS:
        escalations.append(
            EscalationTrigger(
                code=EscalationCode.E07,
                reason="test failure attribution is ambiguous",
                evidence_refs=(),
                deduplication_key=(
                    f"E-07:{artifact.snapshot_digest}:"
                    f"{artifact.consensus_round}"
                ),
            )
        )
    if artifact.plan_change_required:
        escalations.append(
            EscalationTrigger(
                code=EscalationCode.E08,
                reason="approved plan must change before implementation",
                evidence_refs=(),
                deduplication_key=(
                    f"E-08:{artifact.snapshot_digest}:"
                    f"{artifact.consensus_round}"
                ),
            )
        )
    return LedgerUpdate(
        ledger=replace(
            ledger,
            findings=tuple(
                records[key]
                for key in sorted(
                    records,
                    key=lambda item: item.encode("utf-8"),
                )
            ),
        ),
        escalations=_dedupe_escalations(
            escalations,
            ledger.approved_escalation_keys,
        ),
        committed_round=False,
    )


def _round_decision_improved(
    record: FindingRecord,
    previous_round: int,
    current_round: int,
) -> bool:
    previous = {
        decision.side: decision.decision
        for decision in record.decisions
        if decision.round == previous_round
    }
    current = {
        decision.side: decision.decision
        for decision in record.decisions
        if decision.round == current_round
    }
    return any(
        previous.get(side) is DecisionValue.CHANGE_REQUIRED
        and current.get(side) is DecisionValue.APPROVE
        for side in set(previous) | set(current)
    )


def _material_progress(
    record: FindingRecord,
    current_signature: str,
    current_round: int,
) -> bool:
    if not record.unresolved_signature_history:
        return False
    previous = record.unresolved_signature_history[-1]
    current_rank = STATUS_RANK[record.status]
    prior_max_rank = max(
        STATUS_RANK[item.status]
        for item in record.unresolved_signature_history
    )
    if current_rank < prior_max_rank:
        return False
    signature_changed = previous.signature != current_signature
    scope_shrunk = (
        set(record.finding.acceptance_criteria_ids)
        < set(previous.acceptance_criteria_ids)
    )
    status_advanced = current_rank > prior_max_rank
    side_improved = _round_decision_improved(
        record,
        previous.round,
        current_round,
    )
    return signature_changed or scope_shrunk or status_advanced or side_improved


def _conflicting_sides(record: FindingRecord, round_value: int) -> bool:
    decisions = {
        item.decision
        for item in record.decisions
        if item.round == round_value
    }
    return (
        DecisionValue.APPROVE in decisions
        and (
            DecisionValue.CHANGE_REQUIRED in decisions
            or DecisionValue.VERIFY_REQUIRED in decisions
        )
    )


def commit_round(
    ledger: ConsensusLedger,
    evidence: RoundEvidence,
    *,
    plan_limit: int,
    code_limit: int,
    expected_plan_version: int | None = None,
    expected_snapshot_digest: str | None = None,
) -> LedgerUpdate:
    if (
        not evidence.both_artifacts_valid
        or evidence.changed_during_round
        or not all(evidence.artifact_digests)
    ):
        return LedgerUpdate(ledger, (), False)
    if evidence.kind is ConsensusKind.PLAN:
        if (
            evidence.reviewed_plan_version is None
            or (
                expected_plan_version is not None
                and evidence.reviewed_plan_version != expected_plan_version
            )
        ):
            raise InvalidRoundError("plan round version mismatch")
        next_round = ledger.plan_round + 1
        if next_round > plan_limit:
            raise InvalidRoundError("plan consensus round limit exceeded")
        updated = replace(ledger, plan_round=next_round)
    else:
        if (
            evidence.reviewed_snapshot_digest is None
            or (
                expected_snapshot_digest is not None
                and evidence.reviewed_snapshot_digest
                != expected_snapshot_digest
            )
        ):
            raise InvalidRoundError("code round snapshot mismatch")
        next_round = ledger.code_round + 1
        if next_round > code_limit:
            raise InvalidRoundError("code consensus round limit exceeded")
        updated = replace(ledger, code_round=next_round)

    records: list[FindingRecord] = []
    escalations: list[EscalationTrigger] = []
    for record in updated.findings:
        if record.status not in UNRESOLVED_STATUSES:
            records.append(record)
            continue
        signature = finding_signature(record.finding)
        progress = _material_progress(record, signature, next_round)
        observation = SignatureObservation(
            round=next_round,
            signature=signature,
            status=record.status,
            acceptance_criteria_ids=record.finding.acceptance_criteria_ids,
            affected_files=record.finding.affected_files,
            root_cause=record.finding.root_cause,
            required_action=(
                record.finding.required_change
                or record.finding.required_fix
                or ""
            ),
            material_progress=progress,
        )
        history = record.unresolved_signature_history + (observation,)
        current_record = replace(
            record,
            unresolved_signature_history=history,
            max_status_reached=_max_status(
                record.max_status_reached,
                record.status,
            ),
        )
        if (
            len(history) >= 2
            and history[-2].signature == history[-1].signature
            and not history[-1].material_progress
        ):
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E05,
                    reason=(
                        "same unresolved signature repeated in two "
                        "valid rounds without material progress"
                    ),
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=(
                        f"E-05:{record.finding.finding_id}:"
                        f"{history[-1].signature}"
                    ),
                )
            )
        if (
            record.finding.blocking_reason is BlockingReason.B5
            and len(history) >= 2
        ):
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E05,
                    reason=(
                        "reviewer reported insufficient basis to decide "
                        "in two valid rounds"
                    ),
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=(
                        f"E-05:B5:{record.finding.finding_id}"
                    ),
                )
            )
        if record.finding.impact_class is ImpactClass.REQUIREMENT_INTERPRETATION:
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E02,
                    reason="requirement interpretation disagreement",
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=f"E-02:{record.finding.finding_id}",
                )
            )
        if (
            record.finding.impact_class is ImpactClass.ARCHITECTURE
            and len(history) >= 2
            and _conflicting_sides(record, next_round)
        ):
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E01,
                    reason="architecture disagreement persisted",
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=f"E-01:{record.finding.finding_id}",
                )
            )
        if (
            (
                record.finding.impact_class is ImpactClass.SECURITY_AUTH
                or record.finding.blocking_reason is BlockingReason.B4
            )
            and _conflicting_sides(record, next_round)
        ):
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E04,
                    reason="security or authentication policy disagreement",
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=f"E-04:{record.finding.finding_id}",
                )
            )
        if record.finding.impact_class in {
            ImpactClass.DB_SCHEMA,
            ImpactClass.EXTERNAL_API,
        }:
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E03,
                    reason=(
                        "data, API, or schema contract change requires "
                        "user approval"
                    ),
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=f"E-03:{record.finding.finding_id}",
                )
            )
        records.append(current_record)
    updated = replace(updated, findings=tuple(records))
    return LedgerUpdate(
        ledger=updated,
        escalations=_dedupe_escalations(
            escalations,
            ledger.approved_escalation_keys,
        ),
        committed_round=True,
    )


def unresolved_scope(ledger: ConsensusLedger) -> ScopePackage:
    records = {
        record.finding.finding_id: record
        for record in ledger.findings
    }
    missing = {
        dependency
        for record in ledger.findings
        for dependency in record.finding.depends_on
        if dependency not in records
    }
    if missing:
        raise LedgerIntegrityError(
            f"finding dependencies do not exist: {sorted(missing)}"
        )
    queue = [
        record.finding.finding_id
        for record in ledger.findings
        if record.status in UNRESOLVED_STATUSES
    ]
    visited: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        queue.extend(records[current].finding.depends_on)

    selected = [
        records[finding_id]
        for finding_id in sorted(visited, key=lambda item: item.encode("utf-8"))
        if records[finding_id].status in UNRESOLVED_STATUSES
    ]
    criteria = sorted(
        {
            value
            for record in selected
            for value in record.finding.acceptance_criteria_ids
        }
    )
    files = sorted(
        {
            value
            for record in selected
            for value in record.finding.affected_files
        }
    )
    tests = sorted(
        {
            value
            for record in selected
            for value in record.finding.test_ids
        }
    )
    excerpts: list[str] = []
    for record in selected:
        latest = _latest_side_decisions(record.decisions)
        if len({item.decision for item in latest.values()}) > 1:
            excerpts.append(
                f"{record.finding.finding_id}:"
                + ",".join(
                    f"{side.value}={latest[side].decision.value}"
                    for side in sorted(latest, key=lambda item: item.value)
                )
            )
    return ScopePackage(
        finding_ids=tuple(
            record.finding.finding_id for record in selected
        ),
        acceptance_criteria_ids=tuple(criteria),
        affected_files=tuple(files),
        test_ids=tuple(tests),
        targeted_test_results=(),
        disagreement_excerpts=tuple(excerpts),
    )
