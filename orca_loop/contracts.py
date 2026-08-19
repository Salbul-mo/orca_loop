from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
import re
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, TypeVar

from .models import (
    AcceptanceEvaluation,
    AcceptanceCriterion,
    AdjudicationArtifact,
    AdjudicationDecision,
    AddressedFinding,
    AgentAccessMode,
    AgentProvider,
    AgentRuntimeConfig,
    AgentRuntimeOptions,
    AgentRuntimeSnapshot,
    AffectedFile,
    AffectedFileOperation,
    ArtifactKind,
    BlockingReason,
    BlindReviewArtifact,
    CandidateDecision,
    CodeReviewRoundContext,
    CodeReviewVerdict,
    DecisionValue,
    EscalationCode,
    EscalationTrigger,
    ExpectedProvenance,
    Finding,
    FindingDecision,
    FileEvaluation,
    HumanDecision,
    HumanDecisionKind,
    ImpactClass,
    ImplementationArtifact,
    ImplementationStatus,
    InformationalFinding,
    PermissionCheck,
    PermissionEnvironment,
    PermissionFeasibilityReport,
    PermissionStrategy,
    PlanDocument,
    PlanVerification,
    PlanVerificationCategory,
    PlanReviewVerdict,
    ProviderCapability,
    ReviewArtifact,
    ReviewComparison,
    ReviewComparisonStatus,
    ReviewConflictCandidate,
    ReviewConflictKind,
    ReviewLane,
    Role,
    Severity,
    Side,
    TestCommand,
    TestCommandEvidence,
    TestContract,
    TestEvidence,
    TestEvaluation,
    TestExecutionPolicy,
    TestFailureAttribution,
    TestKind,
    TestGateStatus,
    TestPolicyViolation,
    ValidationStatus,
    WorkerKey,
    WorkerDonePayload,
)


MAX_ARTIFACT_BYTES = 1_048_576
SCHEMA_VERSION = 1
AGENT_RUNTIME_SCHEMA_VERSION = 2
LEGACY_AGENT_RUNTIME_SCHEMA_VERSION = 1
MANDATORY_PERMISSION_CHECK_IDS = tuple(
    f"V-PERM-0{index}" for index in range(1, 6)
)
OPTIONAL_PERMISSION_CHECK_IDS = ("V-PERM-06",)
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")
FENCED_JSON_PATTERN = re.compile(
    r"```json\s*(\{.*\})\s*```",
    re.IGNORECASE | re.DOTALL,
)

WORKER_DONE_ALIASES = {
    "taskId": "task_id",
    "dispatchId": "dispatch_id",
    "reportPath": "report_path",
    "artifactDigest": "artifact_digest",
}
FINDING_ALIASES = {"id": "finding_id", "evidence": "evidence_refs"}
DECISION_ALIASES = {"id": "finding_id", "evidence": "evidence_refs"}
ADDRESSED_ALIASES = {"id": "finding_id", "evidence": "evidence_refs"}
ESCALATION_ALIASES = {"evidence": "evidence_refs"}
INFORMATIONAL_ALIASES = {"id": "finding_id", "evidence": "evidence_refs"}


class ContractViolationError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProvenanceError(ContractViolationError):
    """Raised when artifact provenance differs from the active dispatch."""


T = TypeVar("T")


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest_value(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def self_digest(value: Mapping[str, object], field_name: str) -> str:
    if field_name not in value:
        raise ContractViolationError(
            f"self-digested object is missing {field_name}"
        )
    digest_input = dict(value)
    digest_input.pop(field_name)
    return digest_value(digest_input)


def _decode(raw_text: str) -> dict[str, object]:
    if not isinstance(raw_text, str):
        raise ContractViolationError("artifact must be text")
    size = len(raw_text.encode("utf-8"))
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        raise ContractViolationError(
            f"artifact size must be 1..{MAX_ARTIFACT_BYTES} bytes"
        )
    candidate = raw_text.strip()
    if not candidate.startswith("{"):
        match = FENCED_JSON_PATTERN.search(candidate)
        if not match:
            raise ContractViolationError(
                "artifact must be a JSON object or contain one fenced JSON object"
            )
        candidate = match.group(1)
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ContractViolationError(f"malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractViolationError("artifact root must be an object")
    return value


def _normalize_aliases(
    value: Mapping[str, object],
    aliases: Mapping[str, str],
) -> dict[str, object]:
    normalized = dict(value)
    for alias, canonical in aliases.items():
        if alias in normalized and canonical in normalized:
            raise ContractViolationError(
                f"both alias {alias!r} and canonical key {canonical!r} exist"
            )
        if alias in normalized:
            normalized[canonical] = normalized.pop(alias)
    return normalized


def _exact(
    value: Mapping[str, object],
    required: set[str],
    *,
    optional: set[str] | None = None,
    context: str,
) -> dict[str, object]:
    optional = optional or set()
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ContractViolationError(
            f"{context} missing fields: {sorted(missing)}"
        )
    if unknown:
        raise ContractViolationError(
            f"{context} unknown fields: {sorted(unknown)}"
        )
    return dict(value)


def _schema(value: Mapping[str, object], context: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ContractViolationError(
            f"{context}.schema_version must be {SCHEMA_VERSION}"
        )


def _string(value: object, context: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ContractViolationError(f"{context} must be a string")
    return value


def _optional_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    return _string(value, context)


def _boolean(value: object, context: str) -> bool:
    if not isinstance(value, bool):
        raise ContractViolationError(f"{context} must be boolean")
    return value


def _integer(
    value: object,
    context: str,
    *,
    minimum: int | None = None,
) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractViolationError(f"{context} must be integer")
    if minimum is not None and value < minimum:
        raise ContractViolationError(
            f"{context} must be >= {minimum}"
        )
    return value


def _enum(enum_type: type[T], value: object, context: str) -> T:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError) as exc:
        raise ContractViolationError(
            f"{context} has invalid enum value {value!r}"
        ) from exc


def _tuple(
    value: object,
    parser: Callable[[object, str], T],
    context: str,
) -> tuple[T, ...]:
    if not isinstance(value, list):
        raise ContractViolationError(f"{context} must be an array")
    return tuple(
        parser(item, f"{context}[{index}]")
        for index, item in enumerate(value)
    )


def _strings(value: object, context: str) -> tuple[str, ...]:
    return _tuple(value, _string, context)


def _digest(value: object, context: str) -> str:
    digest = _string(value, context)
    if not DIGEST_PATTERN.fullmatch(digest):
        raise ContractViolationError(
            f"{context} must be sha256:<64 lowercase hex>"
        )
    return digest


def _identifier(value: object, context: str) -> str:
    identifier = _string(value, context)
    if not ID_PATTERN.fullmatch(identifier):
        raise ContractViolationError(f"{context} has invalid identifier")
    return identifier


def _strict_json_object(raw_text: str, context: str) -> dict[str, object]:
    if not isinstance(raw_text, str):
        raise ContractViolationError(f"{context} must be text")
    size = len(raw_text.encode("utf-8"))
    if size < 1 or size > MAX_ARTIFACT_BYTES:
        raise ContractViolationError(
            f"{context} size must be 1..{MAX_ARTIFACT_BYTES} bytes"
        )
    try:
        value = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ContractViolationError(f"malformed JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractViolationError(f"{context} root must be an object")
    return value


def _runtime_string(value: object, context: str) -> str | None:
    if value is None:
        return None
    parsed = _string(value, context)
    if parsed != parsed.strip():
        raise ContractViolationError(
            f"{context} must not have leading or trailing whitespace"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in parsed):
        raise ContractViolationError(
            f"{context} must not contain control characters"
        )
    return parsed


def default_agent_provider(worker: WorkerKey) -> AgentProvider:
    if worker in {
        WorkerKey.CLAUDE_PLANNER,
        WorkerKey.CLAUDE_CODE_REVIEW,
    }:
        return AgentProvider.CLAUDE
    return AgentProvider.CODEX


def _agent_runtime_schema(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractViolationError(f"{context}.schema_version must be int")
    if value not in {
        LEGACY_AGENT_RUNTIME_SCHEMA_VERSION,
        AGENT_RUNTIME_SCHEMA_VERSION,
    }:
        raise ContractViolationError(
            f"{context}.schema_version must be "
            f"{LEGACY_AGENT_RUNTIME_SCHEMA_VERSION} or "
            f"{AGENT_RUNTIME_SCHEMA_VERSION}"
        )
    return value


def _parse_agent_runtime_agents(
    value: object,
    context: str,
    schema_version: int,
) -> tuple[AgentRuntimeOptions, ...]:
    if not isinstance(value, dict):
        raise ContractViolationError(f"{context} must be an object")
    expected = {worker.value for worker in WorkerKey}
    if set(value) != expected:
        raise ContractViolationError(
            f"{context} fields mismatch: expected {sorted(expected)}, "
            f"got {sorted(value)}"
        )
    agents: list[AgentRuntimeOptions] = []
    for worker in WorkerKey:
        item = value[worker.value]
        if not isinstance(item, dict):
            raise ContractViolationError(
                f"{context}.{worker.value} must be an object"
            )
        expected_fields = (
            {"model", "effort"}
            if schema_version == LEGACY_AGENT_RUNTIME_SCHEMA_VERSION
            else {"provider", "model", "effort"}
        )
        raw = _exact(item, expected_fields, context=f"{context}.{worker.value}")
        provider = (
            default_agent_provider(worker)
            if schema_version == LEGACY_AGENT_RUNTIME_SCHEMA_VERSION
            else _enum(
                AgentProvider,
                raw["provider"],
                f"{context}.{worker.value}.provider",
            )
        )
        agents.append(
            AgentRuntimeOptions(
                worker_key=worker,
                provider=provider,
                model=_runtime_string(
                    raw["model"],
                    f"{context}.{worker.value}.model",
                ),
                effort=_runtime_string(
                    raw["effort"],
                    f"{context}.{worker.value}.effort",
                ),
            )
        )
    return tuple(agents)


def _agent_runtime_value(
    agents: tuple[AgentRuntimeOptions, ...],
) -> dict[str, object]:
    if len(agents) != len(WorkerKey):
        raise ContractViolationError(
            "agent runtime must contain exactly four workers"
        )
    mapping = {item.worker_key: item for item in agents}
    if set(mapping) != set(WorkerKey) or len(mapping) != len(agents):
        raise ContractViolationError(
            "agent runtime workers must be unique and complete"
        )
    return {
        "schema_version": AGENT_RUNTIME_SCHEMA_VERSION,
        "agents": {
            worker.value: {
                "provider": mapping[worker].provider.value,
                "model": mapping[worker].model,
                "effort": mapping[worker].effort,
            }
            for worker in WorkerKey
        },
    }


def _legacy_agent_runtime_value(
    agents: tuple[AgentRuntimeOptions, ...],
) -> dict[str, object]:
    mapping = {item.worker_key: item for item in agents}
    if set(mapping) != set(WorkerKey) or len(mapping) != len(agents):
        raise ContractViolationError(
            "agent runtime workers must be unique and complete"
        )
    if any(
        mapping[worker].provider is not default_agent_provider(worker)
        for worker in WorkerKey
    ):
        raise ContractViolationError(
            "legacy agent runtime cannot encode provider overrides"
        )
    return {
        "schema_version": LEGACY_AGENT_RUNTIME_SCHEMA_VERSION,
        "agents": {
            worker.value: {
                "model": mapping[worker].model,
                "effort": mapping[worker].effort,
            }
            for worker in WorkerKey
        },
    }


def build_agent_runtime_config(
    agents: tuple[AgentRuntimeOptions, ...],
) -> AgentRuntimeConfig:
    value = _agent_runtime_value(agents)
    parsed_agents = _parse_agent_runtime_agents(
        value["agents"],
        "agent_runtime.agents",
        AGENT_RUNTIME_SCHEMA_VERSION,
    )
    return AgentRuntimeConfig(
        schema_version=AGENT_RUNTIME_SCHEMA_VERSION,
        agents=parsed_agents,
        configuration_digest=digest_value(value),
    )


def parse_agent_runtime_config(raw_text: str) -> AgentRuntimeConfig:
    raw = _exact(
        _strict_json_object(raw_text, "agent_runtime"),
        {"schema_version", "agents"},
        context="agent_runtime",
    )
    schema_version = _agent_runtime_schema(
        raw["schema_version"],
        "agent_runtime",
    )
    agents = _parse_agent_runtime_agents(
        raw["agents"],
        "agent_runtime.agents",
        schema_version,
    )
    return build_agent_runtime_config(agents)


def serialize_agent_runtime_config(config: AgentRuntimeConfig) -> str:
    if config.schema_version != AGENT_RUNTIME_SCHEMA_VERSION:
        raise ContractViolationError(
            "agent_runtime.schema_version must be "
            f"{AGENT_RUNTIME_SCHEMA_VERSION}"
        )
    verified = build_agent_runtime_config(config.agents)
    if config.configuration_digest != verified.configuration_digest:
        raise ContractViolationError(
            "agent runtime configuration digest mismatch"
        )
    value = _agent_runtime_value(verified.agents)
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def build_agent_runtime_snapshot(
    run_id: str,
    config: AgentRuntimeConfig,
    source_config_path: str | None,
) -> AgentRuntimeSnapshot:
    _identifier(run_id, "agent_runtime_snapshot.run_id")
    validated_source_path = _runtime_string(
        source_config_path,
        "agent_runtime_snapshot.source_config_path",
    )
    if validated_source_path is not None and not Path(
        validated_source_path
    ).is_absolute():
        raise ContractViolationError(
            "agent_runtime_snapshot.source_config_path must be absolute"
        )
    verified = build_agent_runtime_config(config.agents)
    if verified.configuration_digest != config.configuration_digest:
        raise ContractViolationError(
            "agent runtime configuration digest mismatch"
        )
    return AgentRuntimeSnapshot(
        schema_version=AGENT_RUNTIME_SCHEMA_VERSION,
        run_id=run_id,
        agents=verified.agents,
        configuration_digest=verified.configuration_digest,
        source_config_path=validated_source_path,
    )


def serialize_agent_runtime_snapshot(snapshot: AgentRuntimeSnapshot) -> str:
    config = build_agent_runtime_config(snapshot.agents)
    verified = build_agent_runtime_snapshot(
        snapshot.run_id,
        config,
        snapshot.source_config_path,
    )
    if snapshot.configuration_digest != verified.configuration_digest:
        raise ContractViolationError(
            "agent runtime snapshot digest mismatch"
        )
    value = _agent_runtime_value(verified.agents)
    value.update(
        {
            "run_id": verified.run_id,
            "configuration_digest": verified.configuration_digest,
            "source_config_path": verified.source_config_path,
        }
    )
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_agent_runtime_snapshot(
    raw_text: str,
    expected_run_id: str,
) -> AgentRuntimeSnapshot:
    raw = _exact(
        _strict_json_object(raw_text, "agent_runtime_snapshot"),
        {
            "schema_version",
            "run_id",
            "agents",
            "configuration_digest",
            "source_config_path",
        },
        context="agent_runtime_snapshot",
    )
    schema_version = _agent_runtime_schema(
        raw["schema_version"],
        "agent_runtime_snapshot",
    )
    run_id = _identifier(raw["run_id"], "agent_runtime_snapshot.run_id")
    if run_id != expected_run_id:
        raise ContractViolationError(
            "agent runtime snapshot run_id mismatch"
        )
    agents = _parse_agent_runtime_agents(
        raw["agents"],
        "agent_runtime_snapshot.agents",
        schema_version,
    )
    config = build_agent_runtime_config(agents)
    claimed = _digest(
        raw["configuration_digest"],
        "agent_runtime_snapshot.configuration_digest",
    )
    expected_digest = (
        digest_value(_legacy_agent_runtime_value(agents))
        if schema_version == LEGACY_AGENT_RUNTIME_SCHEMA_VERSION
        else config.configuration_digest
    )
    if claimed != expected_digest:
        raise ContractViolationError(
            "agent runtime snapshot digest mismatch"
        )
    source_path = _runtime_string(
        raw["source_config_path"],
        "agent_runtime_snapshot.source_config_path",
    )
    return build_agent_runtime_snapshot(run_id, config, source_path)


def _object(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractViolationError(f"{context} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise ContractViolationError(f"{context} keys must be strings")
    return dict(value)


def _parse_affected(value: object, context: str) -> AffectedFile:
    raw = _exact(
        _object(value, context),
        {"path", "operation", "rename_from"},
        context=context,
    )
    path = _string(raw["path"], f"{context}.path")
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise ContractViolationError(
            f"{context}.path must be repository-relative"
        )
    operation = _enum(
        AffectedFileOperation,
        raw["operation"],
        f"{context}.operation",
    )
    rename_from = _optional_string(
        raw["rename_from"],
        f"{context}.rename_from",
    )
    if operation is AffectedFileOperation.RENAME and rename_from is None:
        raise ContractViolationError(
            f"{context}.rename_from is required for rename"
        )
    if operation is not AffectedFileOperation.RENAME and rename_from is not None:
        raise ContractViolationError(
            f"{context}.rename_from is only valid for rename"
        )
    return AffectedFile(path, operation, rename_from)


def _parse_test_command(value: object, context: str) -> TestCommand:
    raw = _exact(
        _object(value, context),
        {"argv", "cwd", "timeout_ms", "kind"},
        context=context,
    )
    argv = _strings(raw["argv"], f"{context}.argv")
    if not argv or any(not item for item in argv):
        raise ContractViolationError(f"{context}.argv must be nonempty")
    return TestCommand(
        argv=argv,
        cwd=_string(raw["cwd"], f"{context}.cwd"),
        timeout_ms=_integer(
            raw["timeout_ms"],
            f"{context}.timeout_ms",
            minimum=1,
        ),
        kind=_enum(TestKind, raw["kind"], f"{context}.kind"),
    )


def _parse_test_contract(value: object, context: str) -> TestContract:
    raw = _exact(
        _object(value, context),
        {"commands", "test_ids"},
        context=context,
    )
    return TestContract(
        commands=_tuple(
            raw["commands"],
            _parse_test_command,
            f"{context}.commands",
        ),
        test_ids=_strings(raw["test_ids"], f"{context}.test_ids"),
    )


def _parse_acceptance(value: object, context: str) -> AcceptanceCriterion:
    raw = _exact(
        _object(value, context),
        {"criterion_id", "verification_method"},
        context=context,
    )
    return AcceptanceCriterion(
        criterion_id=_identifier(
            raw["criterion_id"],
            f"{context}.criterion_id",
        ),
        verification_method=_string(
            raw["verification_method"],
            f"{context}.verification_method",
        ),
    )


def parse_plan_document(
    raw_text: str,
    *,
    delivered_finding_ids: tuple[str, ...] | None = None,
    expected_snapshot_digest: str | None = None,
    expected_round: int | None = None,
) -> PlanDocument:
    raw = _exact(
        _decode(raw_text),
        {
            "schema_version",
            "plan_version",
            "request_digest",
            "source_instruction",
            "interpretation",
            "rationale",
            "current_state_evidence",
            "affected_files",
            "implementation_steps",
            "data_api_schema_changes",
            "error_handling",
            "test_contract",
            "test_policy_digest",
            "acceptance_criteria",
            "risks",
            "out_of_scope",
            "reviewed_finding_ids",
            "finding_decisions",
        },
        context="plan",
    )
    _schema(raw, "plan")
    criteria = _tuple(
        raw["acceptance_criteria"],
        _parse_acceptance,
        "plan.acceptance_criteria",
    )
    criterion_ids = tuple(item.criterion_id for item in criteria)
    if len(set(criterion_ids)) != len(criterion_ids):
        raise ContractViolationError(
            "plan.acceptance_criteria IDs must be unique"
        )
    reviewed_ids = _strings(
        raw["reviewed_finding_ids"],
        "plan.reviewed_finding_ids",
    )
    decisions = _tuple(
        raw["finding_decisions"],
        _parse_decision,
        "plan.finding_decisions",
    )
    if (
        delivered_finding_ids is not None
        and reviewed_ids != delivered_finding_ids
    ):
        raise ContractViolationError(
            "plan.reviewed_finding_ids must exactly equal delivered finding IDs"
        )
    if {item.finding_id for item in decisions} != set(reviewed_ids):
        raise ContractViolationError(
            "plan finding decisions must exactly cover reviewed finding IDs"
        )
    if len(decisions) != len(reviewed_ids):
        raise ContractViolationError(
            "plan finding decisions must contain one decision per finding"
        )
    if any(item.side is not Side.CLAUDE for item in decisions):
        raise ContractViolationError(
            "plan finding decisions must use the primary consensus lane "
            "(legacy CLAUDE wire value)"
        )
    if expected_snapshot_digest is not None and any(
        item.snapshot_digest != expected_snapshot_digest
        for item in decisions
    ):
        raise ProvenanceError("plan finding decision snapshot mismatch")
    if expected_round is not None and any(
        item.round != expected_round for item in decisions
    ):
        raise ProvenanceError("plan finding decision round mismatch")
    return PlanDocument(
        schema_version=SCHEMA_VERSION,
        plan_version=_integer(
            raw["plan_version"],
            "plan.plan_version",
            minimum=1,
        ),
        request_digest=_digest(
            raw["request_digest"],
            "plan.request_digest",
        ),
        source_instruction=_string(
            raw["source_instruction"],
            "plan.source_instruction",
        ),
        interpretation=_string(
            raw["interpretation"],
            "plan.interpretation",
        ),
        rationale=_string(raw["rationale"], "plan.rationale"),
        current_state_evidence=_strings(
            raw["current_state_evidence"],
            "plan.current_state_evidence",
        ),
        affected_files=_tuple(
            raw["affected_files"],
            _parse_affected,
            "plan.affected_files",
        ),
        implementation_steps=_strings(
            raw["implementation_steps"],
            "plan.implementation_steps",
        ),
        data_api_schema_changes=_string(
            raw["data_api_schema_changes"],
            "plan.data_api_schema_changes",
            nonempty=False,
        ),
        error_handling=_strings(
            raw["error_handling"],
            "plan.error_handling",
        ),
        test_contract=_parse_test_contract(
            raw["test_contract"],
            "plan.test_contract",
        ),
        test_policy_digest=_digest(
            raw["test_policy_digest"],
            "plan.test_policy_digest",
        ),
        acceptance_criteria=criteria,
        risks=_strings(raw["risks"], "plan.risks"),
        out_of_scope=_strings(
            raw["out_of_scope"],
            "plan.out_of_scope",
        ),
        reviewed_finding_ids=reviewed_ids,
        finding_decisions=decisions,
    )


def _parse_escalation(value: object, context: str) -> EscalationTrigger:
    raw = _normalize_aliases(
        _object(value, context),
        ESCALATION_ALIASES,
    )
    raw = _exact(
        raw,
        {"code", "reason", "evidence_refs", "deduplication_key"},
        context=context,
    )
    return EscalationTrigger(
        code=_enum(EscalationCode, raw["code"], f"{context}.code"),
        reason=_string(raw["reason"], f"{context}.reason"),
        evidence_refs=_strings(
            raw["evidence_refs"],
            f"{context}.evidence_refs",
        ),
        deduplication_key=_identifier(
            raw["deduplication_key"],
            f"{context}.deduplication_key",
        ),
    )


def _parse_finding(value: object, context: str) -> Finding:
    raw = _normalize_aliases(_object(value, context), FINDING_ALIASES)
    raw = _exact(
        raw,
        {
            "finding_id",
            "severity",
            "blocking_reason",
            "impact_class",
            "file",
            "line",
            "root_cause",
            "description",
            "required_fix",
            "required_change",
            "acceptance_criteria_ids",
            "affected_files",
            "test_ids",
            "depends_on",
            "evidence_refs",
            "reopens",
        },
        context=context,
    )
    required_fix = _optional_string(
        raw["required_fix"],
        f"{context}.required_fix",
    )
    required_change = _optional_string(
        raw["required_change"],
        f"{context}.required_change",
    )
    if (required_fix is None) == (required_change is None):
        raise ContractViolationError(
            f"{context} must have exactly one required action"
        )
    grounds = _strings(
        raw["acceptance_criteria_ids"],
        f"{context}.acceptance_criteria_ids",
    ) + _strings(raw["evidence_refs"], f"{context}.evidence_refs")
    if not grounds:
        raise ContractViolationError(
            f"{context} must cite acceptance_criteria_ids or evidence_refs"
        )
    line_value = raw["line"]
    line = (
        None
        if line_value is None
        else _integer(line_value, f"{context}.line", minimum=1)
    )
    return Finding(
        finding_id=_identifier(
            raw["finding_id"],
            f"{context}.finding_id",
        ),
        severity=_enum(Severity, raw["severity"], f"{context}.severity"),
        blocking_reason=_enum(
            BlockingReason,
            raw["blocking_reason"],
            f"{context}.blocking_reason",
        ),
        impact_class=_enum(
            ImpactClass,
            raw["impact_class"],
            f"{context}.impact_class",
        ),
        file=_optional_string(raw["file"], f"{context}.file"),
        line=line,
        root_cause=_string(raw["root_cause"], f"{context}.root_cause"),
        description=_string(
            raw["description"],
            f"{context}.description",
        ),
        required_fix=required_fix,
        required_change=required_change,
        acceptance_criteria_ids=_strings(
            raw["acceptance_criteria_ids"],
            f"{context}.acceptance_criteria_ids",
        ),
        affected_files=_strings(
            raw["affected_files"],
            f"{context}.affected_files",
        ),
        test_ids=_strings(raw["test_ids"], f"{context}.test_ids"),
        depends_on=_strings(
            raw["depends_on"],
            f"{context}.depends_on",
        ),
        evidence_refs=_strings(
            raw["evidence_refs"],
            f"{context}.evidence_refs",
        ),
        reopens=_optional_string(
            raw["reopens"],
            f"{context}.reopens",
        ),
    )


def _parse_decision(value: object, context: str) -> FindingDecision:
    raw = _normalize_aliases(_object(value, context), DECISION_ALIASES)
    raw = _exact(
        raw,
        {
            "finding_id",
            "side",
            "decision",
            "snapshot_digest",
            "round",
            "evidence_refs",
        },
        context=context,
    )
    return FindingDecision(
        finding_id=_identifier(
            raw["finding_id"],
            f"{context}.finding_id",
        ),
        side=_enum(Side, raw["side"], f"{context}.side"),
        decision=_enum(
            DecisionValue,
            raw["decision"],
            f"{context}.decision",
        ),
        snapshot_digest=_digest(
            raw["snapshot_digest"],
            f"{context}.snapshot_digest",
        ),
        round=_integer(raw["round"], f"{context}.round", minimum=1),
        evidence_refs=_strings(
            raw["evidence_refs"],
            f"{context}.evidence_refs",
        ),
    )


def _parse_informational(
    value: object,
    context: str,
) -> InformationalFinding:
    raw = _normalize_aliases(
        _object(value, context),
        INFORMATIONAL_ALIASES,
    )
    raw = _exact(
        raw,
        {"finding_id", "description", "evidence_refs"},
        context=context,
    )
    return InformationalFinding(
        finding_id=_identifier(
            raw["finding_id"],
            f"{context}.finding_id",
        ),
        description=_string(
            raw["description"],
            f"{context}.description",
        ),
        evidence_refs=_strings(
            raw["evidence_refs"],
            f"{context}.evidence_refs",
        ),
    )


def _parse_acceptance_evaluation(
    value: object,
    context: str,
) -> AcceptanceEvaluation:
    raw = _exact(
        _object(value, context),
        {"criterion_id", "decision", "evidence_refs"},
        context=context,
    )
    result = AcceptanceEvaluation(
        criterion_id=_identifier(raw["criterion_id"], f"{context}.criterion_id"),
        decision=_enum(DecisionValue, raw["decision"], f"{context}.decision"),
        evidence_refs=_strings(raw["evidence_refs"], f"{context}.evidence_refs"),
    )
    if result.decision is DecisionValue.APPROVE and not result.evidence_refs:
        raise ContractViolationError(f"{context} APPROVE requires evidence")
    return result


def _parse_file_evaluation(value: object, context: str) -> FileEvaluation:
    raw = _exact(
        _object(value, context),
        {"path", "operation", "rename_from", "decision", "evidence_refs"},
        context=context,
    )
    result = FileEvaluation(
        path=_string(raw["path"], f"{context}.path"),
        operation=_enum(
            AffectedFileOperation,
            raw["operation"],
            f"{context}.operation",
        ),
        rename_from=_optional_string(raw["rename_from"], f"{context}.rename_from"),
        decision=_enum(DecisionValue, raw["decision"], f"{context}.decision"),
        evidence_refs=_strings(raw["evidence_refs"], f"{context}.evidence_refs"),
    )
    if result.decision is DecisionValue.APPROVE and not result.evidence_refs:
        raise ContractViolationError(f"{context} APPROVE requires evidence")
    return result


def _parse_test_evaluation(value: object, context: str) -> TestEvaluation:
    raw = _exact(
        _object(value, context),
        {"test_id", "test_gate_status", "decision", "evidence_refs"},
        context=context,
    )
    status = _enum(
        TestGateStatus,
        raw["test_gate_status"],
        f"{context}.test_gate_status",
    )
    if status not in {TestGateStatus.PASS, TestGateStatus.NOT_RUN}:
        raise ContractViolationError(
            f"{context}.test_gate_status must be PASS or NOT_RUN"
        )
    result = TestEvaluation(
        test_id=_identifier(raw["test_id"], f"{context}.test_id"),
        test_gate_status=status,
        decision=_enum(DecisionValue, raw["decision"], f"{context}.decision"),
        evidence_refs=_strings(raw["evidence_refs"], f"{context}.evidence_refs"),
    )
    if result.decision is DecisionValue.APPROVE and not result.evidence_refs:
        raise ContractViolationError(f"{context} APPROVE requires evidence")
    return result


def _parse_plan_verification(value: object, context: str) -> PlanVerification:
    raw = _exact(
        _object(value, context),
        {"category", "decision", "evidence_refs"},
        context=context,
    )
    result = PlanVerification(
        category=_enum(
            PlanVerificationCategory,
            raw["category"],
            f"{context}.category",
        ),
        decision=_enum(DecisionValue, raw["decision"], f"{context}.decision"),
        evidence_refs=_strings(raw["evidence_refs"], f"{context}.evidence_refs"),
    )
    if result.decision is DecisionValue.APPROVE and not result.evidence_refs:
        raise ContractViolationError(f"{context} APPROVE requires evidence")
    return result


def _parse_test_policy_violation(
    value: object,
    context: str,
) -> TestPolicyViolation:
    raw = _exact(
        _object(value, context),
        {"code", "command_index", "detail"},
        context=context,
    )
    index_value = raw["command_index"]
    return TestPolicyViolation(
        code=_string(raw["code"], f"{context}.code"),
        command_index=(
            None
            if index_value is None
            else _integer(index_value, f"{context}.command_index", minimum=0)
        ),
        detail=_string(raw["detail"], f"{context}.detail"),
    )


def _parse_test_command_evidence(
    value: object,
    context: str,
) -> TestCommandEvidence:
    raw = _exact(
        _object(value, context),
        {
            "command_index",
            "command",
            "return_code",
            "timed_out",
            "stdout_tail_digest",
            "stderr_tail_digest",
        },
        context=context,
    )
    return_code = raw["return_code"]
    return TestCommandEvidence(
        command_index=_integer(
            raw["command_index"],
            f"{context}.command_index",
            minimum=0,
        ),
        command=_parse_test_command(raw["command"], f"{context}.command"),
        return_code=(
            None
            if return_code is None
            else _integer(return_code, f"{context}.return_code")
        ),
        timed_out=_boolean(raw["timed_out"], f"{context}.timed_out"),
        stdout_tail_digest=_digest(
            raw["stdout_tail_digest"],
            f"{context}.stdout_tail_digest",
        ),
        stderr_tail_digest=_digest(
            raw["stderr_tail_digest"],
            f"{context}.stderr_tail_digest",
        ),
    )


def parse_test_evidence(raw_text: str) -> TestEvidence:
    raw = _exact(
        _strict_json_object(raw_text, "test_evidence"),
        {
            "schema_version",
            "run_id",
            "plan_version",
            "consensus_round",
            "test_gate_status",
            "test_policy_digest",
            "commands",
            "policy_violations",
            "before_snapshot_digest",
            "after_snapshot_digest",
            "authoritative_snapshot_digest",
            "test_ids",
            "attribution",
            "artifact_digest",
        },
        context="test_evidence",
    )
    _schema(raw, "test_evidence")
    supplied_digest = _digest(
        raw["artifact_digest"],
        "test_evidence.artifact_digest",
    )
    if supplied_digest != self_digest(raw, "artifact_digest"):
        raise ProvenanceError("test evidence self-digest mismatch")
    status = _enum(
        TestGateStatus,
        raw["test_gate_status"],
        "test_evidence.test_gate_status",
    )
    before = _digest(
        raw["before_snapshot_digest"],
        "test_evidence.before_snapshot_digest",
    )
    after = (
        None
        if raw["after_snapshot_digest"] is None
        else _digest(
            raw["after_snapshot_digest"],
            "test_evidence.after_snapshot_digest",
        )
    )
    authoritative = _digest(
        raw["authoritative_snapshot_digest"],
        "test_evidence.authoritative_snapshot_digest",
    )
    expected_authoritative = after if status is TestGateStatus.PASS else before
    if status is TestGateStatus.PASS and after is None:
        raise ContractViolationError("PASS test evidence requires after snapshot")
    if status is TestGateStatus.NOT_RUN and after is not None:
        raise ContractViolationError("NOT_RUN test evidence cannot have after snapshot")
    if authoritative != expected_authoritative:
        raise ProvenanceError("test evidence authoritative snapshot mismatch")
    return TestEvidence(
        schema_version=SCHEMA_VERSION,
        run_id=_identifier(raw["run_id"], "test_evidence.run_id"),
        plan_version=_integer(raw["plan_version"], "test_evidence.plan_version", minimum=1),
        consensus_round=_integer(
            raw["consensus_round"],
            "test_evidence.consensus_round",
            minimum=1,
        ),
        test_gate_status=status,
        test_policy_digest=_digest(
            raw["test_policy_digest"],
            "test_evidence.test_policy_digest",
        ),
        commands=_tuple(
            raw["commands"],
            _parse_test_command_evidence,
            "test_evidence.commands",
        ),
        policy_violations=_tuple(
            raw["policy_violations"],
            _parse_test_policy_violation,
            "test_evidence.policy_violations",
        ),
        before_snapshot_digest=before,
        after_snapshot_digest=after,
        authoritative_snapshot_digest=authoritative,
        test_ids=_strings(raw["test_ids"], "test_evidence.test_ids"),
        attribution=_enum(
            TestFailureAttribution,
            raw["attribution"],
            "test_evidence.attribution",
        ),
        artifact_digest=supplied_digest,
    )


def parse_review_context(raw_text: str) -> CodeReviewRoundContext:
    raw = _exact(
        _strict_json_object(raw_text, "review_context"),
        {
            "schema_version",
            "run_id",
            "consensus_round",
            "plan_version",
            "snapshot_digest",
            "implementation_artifact_digest",
            "test_evidence_digest",
            "frozen_diff_digest",
            "scope_manifest_digest",
            "readonly_mirror_digest",
            "baseline_finding_ids",
            "acceptance_criteria_ids",
            "affected_files",
            "test_ids",
            "context_digest",
        },
        context="review_context",
    )
    _schema(raw, "review_context")
    supplied_digest = _digest(raw["context_digest"], "review_context.context_digest")
    if supplied_digest != self_digest(raw, "context_digest"):
        raise ProvenanceError("review context self-digest mismatch")
    return CodeReviewRoundContext(
        schema_version=SCHEMA_VERSION,
        run_id=_identifier(raw["run_id"], "review_context.run_id"),
        consensus_round=_integer(
            raw["consensus_round"],
            "review_context.consensus_round",
            minimum=1,
        ),
        plan_version=_integer(raw["plan_version"], "review_context.plan_version", minimum=1),
        snapshot_digest=_digest(raw["snapshot_digest"], "review_context.snapshot_digest"),
        implementation_artifact_digest=_digest(
            raw["implementation_artifact_digest"],
            "review_context.implementation_artifact_digest",
        ),
        test_evidence_digest=_digest(
            raw["test_evidence_digest"],
            "review_context.test_evidence_digest",
        ),
        frozen_diff_digest=_digest(
            raw["frozen_diff_digest"],
            "review_context.frozen_diff_digest",
        ),
        scope_manifest_digest=_digest(
            raw["scope_manifest_digest"],
            "review_context.scope_manifest_digest",
        ),
        readonly_mirror_digest=_digest(
            raw["readonly_mirror_digest"],
            "review_context.readonly_mirror_digest",
        ),
        baseline_finding_ids=_strings(
            raw["baseline_finding_ids"],
            "review_context.baseline_finding_ids",
        ),
        acceptance_criteria_ids=_strings(
            raw["acceptance_criteria_ids"],
            "review_context.acceptance_criteria_ids",
        ),
        affected_files=_tuple(
            raw["affected_files"],
            _parse_affected,
            "review_context.affected_files",
        ),
        test_ids=_strings(raw["test_ids"], "review_context.test_ids"),
        context_digest=supplied_digest,
    )


def parse_blind_review_artifact(
    raw_text: str,
    expected_kind: ArtifactKind,
    expected: ExpectedProvenance,
    *,
    expected_plan_version: int,
    expected_context_digest: str,
    expected_reviewed_artifact_digest: str,
    delivered_finding_ids: tuple[str, ...],
    acceptance_criteria_ids: tuple[str, ...],
    affected_files: tuple[AffectedFile, ...],
    test_ids: tuple[str, ...],
    test_gate_status: TestGateStatus,
) -> BlindReviewArtifact:
    if expected_kind not in {ArtifactKind.CODE_REVIEW_A, ArtifactKind.CODE_REVIEW_B}:
        raise ContractViolationError("invalid blind review artifact kind")
    raw = _exact(
        _strict_json_object(raw_text, "blind_review"),
        {
            "schema_version",
            "artifact_kind",
            "run_id",
            "task_id",
            "dispatch_id",
            "consensus_round",
            "plan_version",
            "snapshot_digest",
            "review_context_digest",
            "role",
            "lane",
            "verdict",
            "reviewed_artifact_digest",
            "reviewed_finding_ids",
            "acceptance_evaluations",
            "file_evaluations",
            "test_evaluations",
            "review_summary",
            "finding_decisions",
            "findings",
            "non_blocking_suggestions",
            "escalation_signals",
        },
        context="blind_review",
    )
    _schema(raw, "blind_review")
    _validate_provenance(raw, expected)
    artifact_kind = _enum(
        ArtifactKind,
        raw["artifact_kind"],
        "blind_review.artifact_kind",
    )
    if artifact_kind is not expected_kind:
        raise ContractViolationError("blind review artifact_kind mismatch")
    expected_lane = ReviewLane.A if expected_kind is ArtifactKind.CODE_REVIEW_A else ReviewLane.B
    expected_role = Role.CODE_REVIEWER if expected_lane is ReviewLane.A else Role.CROSS_CONFIRMER
    lane = _enum(ReviewLane, raw["lane"], "blind_review.lane")
    role = _enum(Role, raw["role"], "blind_review.role")
    if lane is not expected_lane or role is not expected_role:
        raise ContractViolationError("blind review lane, role, and kind mismatch")
    if raw["plan_version"] != expected_plan_version:
        raise ProvenanceError("blind review plan version mismatch")
    context_digest = _digest(
        raw["review_context_digest"],
        "blind_review.review_context_digest",
    )
    if context_digest != expected_context_digest:
        raise ProvenanceError("blind review context digest mismatch")
    reviewed_digest = _digest(
        raw["reviewed_artifact_digest"],
        "blind_review.reviewed_artifact_digest",
    )
    if reviewed_digest != expected_reviewed_artifact_digest:
        raise ProvenanceError("blind review implementation digest mismatch")
    reviewed_ids = _strings(raw["reviewed_finding_ids"], "blind_review.reviewed_finding_ids")
    if reviewed_ids != delivered_finding_ids:
        raise ContractViolationError("blind reviewed finding IDs must equal baseline IDs")
    acceptance = _tuple(
        raw["acceptance_evaluations"],
        _parse_acceptance_evaluation,
        "blind_review.acceptance_evaluations",
    )
    if tuple(item.criterion_id for item in acceptance) != acceptance_criteria_ids:
        raise ContractViolationError("blind acceptance coverage mismatch")
    files = _tuple(
        raw["file_evaluations"],
        _parse_file_evaluation,
        "blind_review.file_evaluations",
    )
    expected_files = tuple((item.path, item.operation, item.rename_from) for item in affected_files)
    actual_files = tuple((item.path, item.operation, item.rename_from) for item in files)
    if actual_files != expected_files:
        raise ContractViolationError("blind file coverage mismatch")
    tests = _tuple(
        raw["test_evaluations"],
        _parse_test_evaluation,
        "blind_review.test_evaluations",
    )
    if tuple(item.test_id for item in tests) != test_ids:
        raise ContractViolationError("blind test coverage mismatch")
    if any(item.test_gate_status is not test_gate_status for item in tests):
        raise ProvenanceError("blind test status does not match test evidence")
    findings = _tuple(raw["findings"], _parse_finding, "blind_review.findings")
    uncovered_acceptance = {
        item.criterion_id
        for item in acceptance
        if item.decision is not DecisionValue.APPROVE
        and not any(
            item.criterion_id in finding.acceptance_criteria_ids
            for finding in findings
        )
    }
    uncovered_files = {
        item.path
        for item in files
        if item.decision is not DecisionValue.APPROVE
        and not any(item.path in finding.affected_files for finding in findings)
    }
    uncovered_tests = {
        item.test_id
        for item in tests
        if item.decision is not DecisionValue.APPROVE
        and not any(item.test_id in finding.test_ids for finding in findings)
    }
    if uncovered_acceptance or uncovered_files or uncovered_tests:
        raise ContractViolationError(
            "non-approve blind evaluations require actionable findings: "
            f"acceptance={sorted(uncovered_acceptance)}, "
            f"files={sorted(uncovered_files)}, tests={sorted(uncovered_tests)}"
        )
    decisions = _tuple(
        raw["finding_decisions"],
        _parse_decision,
        "blind_review.finding_decisions",
    )
    obligated = set(reviewed_ids) | {item.finding_id for item in findings}
    decision_ids = {item.finding_id for item in decisions}
    if decision_ids != obligated or len(decisions) != len(obligated):
        raise ContractViolationError("blind finding decisions are incomplete or duplicated")
    expected_side = Side.CLAUDE if lane is ReviewLane.A else Side.CODEX
    if any(
        item.side is not expected_side
        or item.snapshot_digest != expected.snapshot_digest
        or item.round != expected.consensus_round
        for item in decisions
    ):
        raise ProvenanceError("blind finding decision provenance mismatch")
    verdict = _enum(CodeReviewVerdict, raw["verdict"], "blind_review.verdict")
    evaluation_decisions = tuple(item.decision for item in (*acceptance, *files, *tests))
    blocking = any(item.decision is not DecisionValue.APPROVE for item in decisions)
    if verdict is CodeReviewVerdict.APPROVE and (
        findings
        or blocking
        or any(item is not DecisionValue.APPROVE for item in evaluation_decisions)
    ):
        raise ContractViolationError("blind APPROVE has an unmet approval obligation")
    if verdict is CodeReviewVerdict.CHANGES_REQUESTED and not (
        findings
        or blocking
        or any(item is not DecisionValue.APPROVE for item in evaluation_decisions)
    ):
        raise ContractViolationError("blind changes verdict has no actionable basis")
    return BlindReviewArtifact(
        schema_version=SCHEMA_VERSION,
        artifact_kind=artifact_kind,
        run_id=_identifier(raw["run_id"], "blind_review.run_id"),
        task_id=_identifier(raw["task_id"], "blind_review.task_id"),
        dispatch_id=_identifier(raw["dispatch_id"], "blind_review.dispatch_id"),
        consensus_round=_integer(raw["consensus_round"], "blind_review.consensus_round", minimum=1),
        plan_version=_integer(raw["plan_version"], "blind_review.plan_version", minimum=1),
        snapshot_digest=_digest(raw["snapshot_digest"], "blind_review.snapshot_digest"),
        review_context_digest=context_digest,
        role=role,
        lane=lane,
        verdict=verdict,
        reviewed_artifact_digest=reviewed_digest,
        reviewed_finding_ids=reviewed_ids,
        acceptance_evaluations=acceptance,
        file_evaluations=files,
        test_evaluations=tests,
        review_summary=_string(raw["review_summary"], "blind_review.review_summary"),
        finding_decisions=decisions,
        findings=findings,
        non_blocking_suggestions=_tuple(
            raw["non_blocking_suggestions"],
            _parse_informational,
            "blind_review.non_blocking_suggestions",
        ),
        escalation_signals=_tuple(
            raw["escalation_signals"],
            _parse_escalation,
            "blind_review.escalation_signals",
        ),
    )


def _parse_review_candidate(value: object, context: str) -> ReviewConflictCandidate:
    raw = _exact(
        _object(value, context),
        {
            "candidate_id",
            "kind",
            "finding_ids",
            "acceptance_criteria_ids",
            "affected_files",
            "test_ids",
            "blind_a_decision",
            "blind_b_decision",
            "normalized_signature",
            "evidence_refs",
        },
        context=context,
    )
    return ReviewConflictCandidate(
        candidate_id=_identifier(raw["candidate_id"], f"{context}.candidate_id"),
        kind=_enum(ReviewConflictKind, raw["kind"], f"{context}.kind"),
        finding_ids=_strings(raw["finding_ids"], f"{context}.finding_ids"),
        acceptance_criteria_ids=_strings(
            raw["acceptance_criteria_ids"],
            f"{context}.acceptance_criteria_ids",
        ),
        affected_files=_strings(raw["affected_files"], f"{context}.affected_files"),
        test_ids=_strings(raw["test_ids"], f"{context}.test_ids"),
        blind_a_decision=(
            None
            if raw["blind_a_decision"] is None
            else _enum(DecisionValue, raw["blind_a_decision"], f"{context}.blind_a_decision")
        ),
        blind_b_decision=(
            None
            if raw["blind_b_decision"] is None
            else _enum(DecisionValue, raw["blind_b_decision"], f"{context}.blind_b_decision")
        ),
        normalized_signature=_string(
            raw["normalized_signature"],
            f"{context}.normalized_signature",
        ),
        evidence_refs=_strings(raw["evidence_refs"], f"{context}.evidence_refs"),
    )


def parse_review_comparison(raw_text: str) -> ReviewComparison:
    raw = _exact(
        _strict_json_object(raw_text, "review_comparison"),
        {
            "schema_version",
            "run_id",
            "consensus_round",
            "snapshot_digest",
            "review_context_digest",
            "pre_round_ledger_digest",
            "blind_a_artifact_digest",
            "blind_b_artifact_digest",
            "status",
            "agreed_finding_ids",
            "candidates",
            "comparison_digest",
        },
        context="review_comparison",
    )
    _schema(raw, "review_comparison")
    supplied_digest = _digest(raw["comparison_digest"], "review_comparison.comparison_digest")
    if supplied_digest != self_digest(raw, "comparison_digest"):
        raise ProvenanceError("review comparison self-digest mismatch")
    candidates = _tuple(raw["candidates"], _parse_review_candidate, "review_comparison.candidates")
    candidate_ids = tuple(item.candidate_id for item in candidates)
    if candidate_ids != tuple(sorted(set(candidate_ids), key=lambda item: item.encode("utf-8"))):
        raise ContractViolationError("review comparison candidate IDs must be unique and sorted")
    status = _enum(ReviewComparisonStatus, raw["status"], "review_comparison.status")
    if (status is ReviewComparisonStatus.AGREED) != (not candidates):
        raise ContractViolationError("review comparison status conflicts with candidates")
    return ReviewComparison(
        schema_version=SCHEMA_VERSION,
        run_id=_identifier(raw["run_id"], "review_comparison.run_id"),
        consensus_round=_integer(raw["consensus_round"], "review_comparison.consensus_round", minimum=1),
        snapshot_digest=_digest(raw["snapshot_digest"], "review_comparison.snapshot_digest"),
        review_context_digest=_digest(raw["review_context_digest"], "review_comparison.review_context_digest"),
        pre_round_ledger_digest=_digest(raw["pre_round_ledger_digest"], "review_comparison.pre_round_ledger_digest"),
        blind_a_artifact_digest=_digest(raw["blind_a_artifact_digest"], "review_comparison.blind_a_artifact_digest"),
        blind_b_artifact_digest=_digest(raw["blind_b_artifact_digest"], "review_comparison.blind_b_artifact_digest"),
        status=status,
        agreed_finding_ids=_strings(raw["agreed_finding_ids"], "review_comparison.agreed_finding_ids"),
        candidates=candidates,
        comparison_digest=supplied_digest,
    )


def _parse_candidate_decision(value: object, context: str) -> CandidateDecision:
    raw = _exact(
        _object(value, context),
        {
            "candidate_id",
            "decision",
            "duplicate_of",
            "root_cause_assessment",
            "required_action",
            "evidence_refs",
        },
        context=context,
    )
    decision = _enum(AdjudicationDecision, raw["decision"], f"{context}.decision")
    duplicate_of = _optional_string(raw["duplicate_of"], f"{context}.duplicate_of")
    required_action = _optional_string(raw["required_action"], f"{context}.required_action")
    evidence = _strings(raw["evidence_refs"], f"{context}.evidence_refs")
    if not evidence:
        raise ContractViolationError(f"{context} requires evidence")
    if decision is AdjudicationDecision.DUPLICATE and duplicate_of is None:
        raise ContractViolationError(f"{context} DUPLICATE requires duplicate_of")
    if decision is not AdjudicationDecision.DUPLICATE and duplicate_of is not None:
        raise ContractViolationError(f"{context} duplicate_of is only valid for DUPLICATE")
    if decision is AdjudicationDecision.CONFIRM and required_action is None:
        raise ContractViolationError(f"{context} CONFIRM requires required_action")
    return CandidateDecision(
        candidate_id=_identifier(raw["candidate_id"], f"{context}.candidate_id"),
        decision=decision,
        duplicate_of=duplicate_of,
        root_cause_assessment=_string(
            raw["root_cause_assessment"],
            f"{context}.root_cause_assessment",
        ),
        required_action=required_action,
        evidence_refs=evidence,
    )


def parse_adjudication_artifact(
    raw_text: str,
    expected_kind: ArtifactKind,
    expected: ExpectedProvenance,
    *,
    expected_context_digest: str,
    comparison: ReviewComparison,
    valid_duplicate_targets: tuple[str, ...],
) -> AdjudicationArtifact:
    if expected_kind not in {
        ArtifactKind.REVIEW_ADJUDICATION_A,
        ArtifactKind.REVIEW_ADJUDICATION_B,
    }:
        raise ContractViolationError("invalid adjudication artifact kind")
    raw = _exact(
        _strict_json_object(raw_text, "adjudication"),
        {
            "schema_version",
            "artifact_kind",
            "run_id",
            "task_id",
            "dispatch_id",
            "consensus_round",
            "snapshot_digest",
            "review_context_digest",
            "comparison_digest",
            "role",
            "lane",
            "candidate_decisions",
        },
        context="adjudication",
    )
    _schema(raw, "adjudication")
    _validate_provenance(raw, expected)
    artifact_kind = _enum(ArtifactKind, raw["artifact_kind"], "adjudication.artifact_kind")
    if artifact_kind is not expected_kind:
        raise ContractViolationError("adjudication artifact_kind mismatch")
    expected_lane = ReviewLane.A if expected_kind is ArtifactKind.REVIEW_ADJUDICATION_A else ReviewLane.B
    expected_role = Role.CODE_REVIEWER if expected_lane is ReviewLane.A else Role.CROSS_CONFIRMER
    lane = _enum(ReviewLane, raw["lane"], "adjudication.lane")
    role = _enum(Role, raw["role"], "adjudication.role")
    if lane is not expected_lane or role is not expected_role:
        raise ContractViolationError("adjudication lane, role, and kind mismatch")
    context_digest = _digest(raw["review_context_digest"], "adjudication.review_context_digest")
    if context_digest != expected_context_digest:
        raise ProvenanceError("adjudication context digest mismatch")
    comparison_digest = _digest(raw["comparison_digest"], "adjudication.comparison_digest")
    if comparison_digest != comparison.comparison_digest:
        raise ProvenanceError("adjudication comparison digest mismatch")
    decisions = _tuple(
        raw["candidate_decisions"],
        _parse_candidate_decision,
        "adjudication.candidate_decisions",
    )
    expected_ids = tuple(item.candidate_id for item in comparison.candidates)
    if tuple(item.candidate_id for item in decisions) != expected_ids:
        raise ContractViolationError("adjudication candidate coverage mismatch")
    allowed_duplicates = set(valid_duplicate_targets) | {
        finding_id
        for candidate in comparison.candidates
        for finding_id in candidate.finding_ids
    }
    if any(
        item.decision is AdjudicationDecision.DUPLICATE
        and item.duplicate_of not in allowed_duplicates
        for item in decisions
    ):
        raise ContractViolationError("adjudication duplicate target is invalid")
    return AdjudicationArtifact(
        schema_version=SCHEMA_VERSION,
        artifact_kind=artifact_kind,
        run_id=_identifier(raw["run_id"], "adjudication.run_id"),
        task_id=_identifier(raw["task_id"], "adjudication.task_id"),
        dispatch_id=_identifier(raw["dispatch_id"], "adjudication.dispatch_id"),
        consensus_round=_integer(raw["consensus_round"], "adjudication.consensus_round", minimum=1),
        snapshot_digest=_digest(raw["snapshot_digest"], "adjudication.snapshot_digest"),
        review_context_digest=context_digest,
        comparison_digest=comparison_digest,
        role=role,
        lane=lane,
        candidate_decisions=decisions,
    )


def _validate_provenance(
    raw: Mapping[str, object],
    expected: ExpectedProvenance,
) -> None:
    checks = {
        "run_id": expected.run_id,
        "task_id": expected.task_id,
        "dispatch_id": expected.dispatch_id,
        "consensus_round": expected.consensus_round,
        "snapshot_digest": expected.snapshot_digest,
    }
    mismatches = [
        f"{field}: expected {expected_value!r}, got {raw.get(field)!r}"
        for field, expected_value in checks.items()
        if raw.get(field) != expected_value
    ]
    if mismatches:
        raise ProvenanceError("; ".join(mismatches))


def parse_review_artifact(
    raw_text: str,
    expected_kind: ArtifactKind,
    expected: ExpectedProvenance,
    *,
    delivered_finding_ids: tuple[str, ...],
    require_plan_verifications: bool = False,
) -> ReviewArtifact:
    if expected_kind not in {
        ArtifactKind.PLAN_REVIEW,
        ArtifactKind.CODE_REVIEW,
        ArtifactKind.CROSS_REVIEW,
    }:
        raise ContractViolationError(
            f"invalid review artifact kind: {expected_kind.value}"
        )
    decoded = _decode(raw_text)
    review_fields = {
        "schema_version",
        "artifact_kind",
        "run_id",
        "task_id",
        "dispatch_id",
        "consensus_round",
        "snapshot_digest",
        "role",
        "verdict",
        "reviewed_plan_version",
        "reviewed_artifact_digest",
        "reviewed_finding_ids",
        "finding_decisions",
        "findings",
        "non_blocking_suggestions",
        "escalation_signals",
        "agrees_with_reviewer",
    }
    if "plan_verifications" in decoded:
        review_fields.add("plan_verifications")
    if (
        expected_kind is ArtifactKind.PLAN_REVIEW
        and require_plan_verifications
        and "plan_verifications" not in decoded
    ):
        raise ContractViolationError(
            "new plan review requires plan_verifications"
        )
    raw = _exact(
        decoded,
        review_fields,
        context="review",
    )
    _schema(raw, "review")
    artifact_kind = _enum(
        ArtifactKind,
        raw["artifact_kind"],
        "review.artifact_kind",
    )
    if artifact_kind is not expected_kind:
        raise ContractViolationError(
            "review artifact_kind does not match expected kind"
        )
    _validate_provenance(raw, expected)
    reviewed_ids = _strings(
        raw["reviewed_finding_ids"],
        "review.reviewed_finding_ids",
    )
    if reviewed_ids != delivered_finding_ids:
        raise ContractViolationError(
            "reviewed_finding_ids must exactly equal delivered finding IDs"
        )
    decisions = _tuple(
        raw["finding_decisions"],
        _parse_decision,
        "review.finding_decisions",
    )
    findings = _tuple(raw["findings"], _parse_finding, "review.findings")
    decision_ids = {item.finding_id for item in decisions}
    obligated = set(reviewed_ids) | {item.finding_id for item in findings}
    if decision_ids != obligated or len(decisions) != len(obligated):
        raise ContractViolationError(
            "missing finding decisions or extra/duplicate decisions: "
            f"expected {sorted(obligated)}, got {sorted(decision_ids)}"
        )
    if any(
        decision.snapshot_digest != expected.snapshot_digest
        or decision.round != expected.consensus_round
        for decision in decisions
    ):
        raise ProvenanceError(
            "finding decision snapshot or round does not match review"
        )
    if artifact_kind is ArtifactKind.PLAN_REVIEW:
        verdict: PlanReviewVerdict | CodeReviewVerdict = _enum(
            PlanReviewVerdict,
            raw["verdict"],
            "review.verdict",
        )
    else:
        verdict = _enum(
            CodeReviewVerdict,
            raw["verdict"],
            "review.verdict",
        )
    expected_role = {
        ArtifactKind.PLAN_REVIEW: Role.PLAN_REVIEWER,
        ArtifactKind.CODE_REVIEW: Role.CODE_REVIEWER,
        ArtifactKind.CROSS_REVIEW: Role.CROSS_CONFIRMER,
    }[artifact_kind]
    role = _enum(Role, raw["role"], "review.role")
    if role is not expected_role:
        raise ContractViolationError(
            "review role does not match artifact_kind"
        )
    expected_side = (
        Side.CLAUDE
        if artifact_kind is ArtifactKind.CODE_REVIEW
        else Side.CODEX
    )
    if any(item.side is not expected_side for item in decisions):
        raise ContractViolationError(
            "finding decision side does not match review role"
        )
    approval = (
        verdict is PlanReviewVerdict.APPROVE
        if artifact_kind is ArtifactKind.PLAN_REVIEW
        else verdict is CodeReviewVerdict.APPROVE
    )
    has_blocking_decision = any(
        item.decision is not DecisionValue.APPROVE
        for item in decisions
    )
    plan_verifications = (
        _tuple(
            raw["plan_verifications"],
            _parse_plan_verification,
            "review.plan_verifications",
        )
        if artifact_kind is ArtifactKind.PLAN_REVIEW
        and "plan_verifications" in raw
        else ()
    )
    if artifact_kind is ArtifactKind.PLAN_REVIEW and plan_verifications:
        expected_categories = tuple(PlanVerificationCategory)
        if tuple(item.category for item in plan_verifications) != expected_categories:
            raise ContractViolationError(
                "plan review verification categories must be complete and ordered"
            )
    verification_blocking = any(
        item.decision is not DecisionValue.APPROVE
        for item in plan_verifications
    )
    if approval and (findings or has_blocking_decision or verification_blocking):
        raise ContractViolationError(
            "approval_obligation: APPROVE cannot carry blocking findings "
            "or non-APPROVE decisions"
        )
    if (
        not approval
        and not findings
        and not has_blocking_decision
        and not verification_blocking
    ):
        raise ContractViolationError(
            "approval_obligation: change verdict requires an actionable "
            "finding or non-APPROVE decision"
        )
    agreement_value = raw["agrees_with_reviewer"]
    if artifact_kind is ArtifactKind.CROSS_REVIEW:
        agrees = _boolean(
            agreement_value,
            "review.agrees_with_reviewer",
        )
    else:
        if agreement_value is not None:
            raise ContractViolationError(
                "agrees_with_reviewer must be null outside cross review"
            )
        agrees = None
    plan_version_value = raw["reviewed_plan_version"]
    plan_version = (
        None
        if plan_version_value is None
        else _integer(
            plan_version_value,
            "review.reviewed_plan_version",
            minimum=1,
        )
    )
    return ReviewArtifact(
        schema_version=SCHEMA_VERSION,
        artifact_kind=artifact_kind,
        run_id=_identifier(raw["run_id"], "review.run_id"),
        task_id=_identifier(raw["task_id"], "review.task_id"),
        dispatch_id=_identifier(
            raw["dispatch_id"],
            "review.dispatch_id",
        ),
        consensus_round=_integer(
            raw["consensus_round"],
            "review.consensus_round",
            minimum=1,
        ),
        snapshot_digest=_digest(
            raw["snapshot_digest"],
            "review.snapshot_digest",
        ),
        role=role,
        verdict=verdict,
        reviewed_plan_version=plan_version,
        reviewed_artifact_digest=_digest(
            raw["reviewed_artifact_digest"],
            "review.reviewed_artifact_digest",
        ),
        reviewed_finding_ids=reviewed_ids,
        finding_decisions=decisions,
        findings=findings,
        non_blocking_suggestions=_tuple(
            raw["non_blocking_suggestions"],
            _parse_informational,
            "review.non_blocking_suggestions",
        ),
        escalation_signals=_tuple(
            raw["escalation_signals"],
            _parse_escalation,
            "review.escalation_signals",
        ),
        agrees_with_reviewer=agrees,
        plan_verifications=plan_verifications,
    )


def _parse_addressed(value: object, context: str) -> AddressedFinding:
    raw = _normalize_aliases(_object(value, context), ADDRESSED_ALIASES)
    raw = _exact(
        raw,
        {"finding_id", "evidence_refs"},
        context=context,
    )
    return AddressedFinding(
        finding_id=_identifier(
            raw["finding_id"],
            f"{context}.finding_id",
        ),
        evidence_refs=_strings(
            raw["evidence_refs"],
            f"{context}.evidence_refs",
        ),
    )


def parse_implementation_artifact(
    raw_text: str,
    expected: ExpectedProvenance,
    *,
    delivered_finding_ids: tuple[str, ...],
) -> ImplementationArtifact:
    raw = _exact(
        _decode(raw_text),
        {
            "schema_version",
            "run_id",
            "task_id",
            "dispatch_id",
            "consensus_round",
            "snapshot_digest",
            "status",
            "addressed_findings",
            "changed_files",
            "summary",
            "test_failure_attribution",
            "plan_change_required",
            "escalation_signals",
        },
        context="implementation",
    )
    _schema(raw, "implementation")
    _validate_provenance(raw, expected)
    addressed = _tuple(
        raw["addressed_findings"],
        _parse_addressed,
        "implementation.addressed_findings",
    )
    addressed_ids = {item.finding_id for item in addressed}
    if not addressed_ids.issubset(set(delivered_finding_ids)):
        raise ContractViolationError(
            "implementation addressed an undelivered finding"
        )
    return ImplementationArtifact(
        schema_version=SCHEMA_VERSION,
        run_id=_identifier(raw["run_id"], "implementation.run_id"),
        task_id=_identifier(raw["task_id"], "implementation.task_id"),
        dispatch_id=_identifier(
            raw["dispatch_id"],
            "implementation.dispatch_id",
        ),
        consensus_round=_integer(
            raw["consensus_round"],
            "implementation.consensus_round",
            minimum=1,
        ),
        snapshot_digest=_digest(
            raw["snapshot_digest"],
            "implementation.snapshot_digest",
        ),
        status=_enum(
            ImplementationStatus,
            raw["status"],
            "implementation.status",
        ),
        addressed_findings=addressed,
        changed_files=_strings(
            raw["changed_files"],
            "implementation.changed_files",
        ),
        summary=_string(raw["summary"], "implementation.summary"),
        test_failure_attribution=_enum(
            TestFailureAttribution,
            raw["test_failure_attribution"],
            "implementation.test_failure_attribution",
        ),
        plan_change_required=_boolean(
            raw["plan_change_required"],
            "implementation.plan_change_required",
        ),
        escalation_signals=_tuple(
            raw["escalation_signals"],
            _parse_escalation,
            "implementation.escalation_signals",
        ),
    )


def parse_worker_done(
    raw_text: str,
    expected: ExpectedProvenance,
) -> WorkerDonePayload:
    raw = _normalize_aliases(_decode(raw_text), WORKER_DONE_ALIASES)
    raw = _exact(
        raw,
        {
            "schema_version",
            "task_id",
            "dispatch_id",
            "report_path",
            "artifact_digest",
        },
        context="worker_done",
    )
    _schema(raw, "worker_done")
    if raw["task_id"] != expected.task_id:
        raise ProvenanceError("worker_done task_id mismatch")
    if raw["dispatch_id"] != expected.dispatch_id:
        raise ProvenanceError("worker_done dispatch_id mismatch")
    report_path = _string(
        raw["report_path"],
        "worker_done.report_path",
    )
    if not Path(report_path).is_absolute():
        raise ContractViolationError(
            "worker_done.report_path must be absolute"
        )
    return WorkerDonePayload(
        schema_version=SCHEMA_VERSION,
        task_id=_identifier(raw["task_id"], "worker_done.task_id"),
        dispatch_id=_identifier(
            raw["dispatch_id"],
            "worker_done.dispatch_id",
        ),
        report_path=report_path,
        artifact_digest=_digest(
            raw["artifact_digest"],
            "worker_done.artifact_digest",
        ),
    )


def parse_test_policy(raw_text: str) -> TestExecutionPolicy:
    raw = _exact(
        _decode(raw_text),
        {
            "allowed_commands",
            "allowed_env_keys",
            "allowed_output_paths",
            "approved_kinds",
            "policy_digest",
        },
        context="test_policy",
    )
    digest_input = dict(raw)
    claimed = _digest(
        digest_input.pop("policy_digest"),
        "test_policy.policy_digest",
    )
    computed = digest_value(digest_input)
    if claimed != computed:
        raise ContractViolationError(
            "test_policy.policy_digest does not match canonical content"
        )
    commands = _tuple(
        raw["allowed_commands"],
        _parse_test_command,
        "test_policy.allowed_commands",
    )
    kinds = _tuple(
        raw["approved_kinds"],
        lambda item, context: _enum(TestKind, item, context),
        "test_policy.approved_kinds",
    )
    return TestExecutionPolicy(
        allowed_commands=commands,
        allowed_env_keys=_strings(
            raw["allowed_env_keys"],
            "test_policy.allowed_env_keys",
        ),
        allowed_output_paths=_strings(
            raw["allowed_output_paths"],
            "test_policy.allowed_output_paths",
        ),
        approved_kinds=kinds,
        policy_digest=claimed,
    )


def _parse_permission_environment(
    value: object,
) -> PermissionEnvironment:
    raw = _exact(
        _object(value, "permission_report.environment"),
        {"platform", "claude_cli", "codex_cli", "enforcement_digest"},
        context="permission_report.environment",
    )
    return PermissionEnvironment(
        platform=_string(
            raw["platform"],
            "permission_report.environment.platform",
        ),
        claude_cli=(
            None
            if raw["claude_cli"] is None
            else _string(
                raw["claude_cli"],
                "permission_report.environment.claude_cli",
            )
        ),
        codex_cli=(
            None
            if raw["codex_cli"] is None
            else _string(
                raw["codex_cli"],
                "permission_report.environment.codex_cli",
            )
        ),
        enforcement_digest=_digest(
            raw["enforcement_digest"],
            "permission_report.environment.enforcement_digest",
        ),
    )


def parse_permission_report(raw_text: str) -> PermissionFeasibilityReport:
    decoded = _decode(raw_text)
    required = {
        "schema_version",
        "run_id",
        "status",
        "strategy",
        "checks",
        "evidence",
        "orca_version",
        "canonical_path",
        "report_digest",
    }
    # `environment` is optional so that reports written before the
    # environment fingerprint existed still parse; those fall back to the
    # older exact Orca version comparison.
    if isinstance(decoded, dict) and "environment" in decoded:
        required = required | {"environment"}
    if isinstance(decoded, dict) and "created_at" in decoded:
        required = required | {"created_at"}
    raw = _exact(
        decoded,
        required,
        context="permission_report",
    )
    _schema(raw, "permission_report")
    digest_input = dict(raw)
    claimed = _digest(
        digest_input.pop("report_digest"),
        "permission_report.report_digest",
    )
    if digest_value(digest_input) != claimed:
        raise ContractViolationError(
            "permission report digest mismatch"
        )

    def parse_check(value: object, context: str) -> PermissionCheck:
        check = _exact(
            _object(value, context),
            {"check_id", "status", "evidence"},
            context=context,
        )
        return PermissionCheck(
            check_id=_identifier(
                check["check_id"],
                f"{context}.check_id",
            ),
            status=_enum(
                ValidationStatus,
                check["status"],
                f"{context}.status",
            ),
            evidence=_strings(
                check["evidence"],
                f"{context}.evidence",
            ),
        )

    status = _enum(
        ValidationStatus,
        raw["status"],
        "permission_report.status",
    )
    strategy_value = raw["strategy"]
    strategy = (
        None
        if strategy_value is None
        else _enum(
            PermissionStrategy,
            strategy_value,
            "permission_report.strategy",
        )
    )
    checks = _tuple(raw["checks"], parse_check, "permission_report.checks")
    check_ids = tuple(item.check_id for item in checks)
    valid_check_sequences = {
        MANDATORY_PERMISSION_CHECK_IDS,
        MANDATORY_PERMISSION_CHECK_IDS + OPTIONAL_PERMISSION_CHECK_IDS,
    }
    if check_ids not in valid_check_sequences:
        raise ContractViolationError(
            "permission report checks must be ordered V-PERM-01..05 "
            "with optional V-PERM-06"
        )
    if status is ValidationStatus.PASS and (
        strategy is None
        or any(item.status is not ValidationStatus.PASS for item in checks)
    ):
        raise ContractViolationError(
            "PASS permission report requires strategy and all checks PASS"
        )
    created_at = None
    if "created_at" in raw:
        created_at = _string(raw["created_at"], "permission_report.created_at")
        try:
            parsed_created_at = datetime.fromisoformat(created_at)
        except ValueError as exc:
            raise ContractViolationError(
                "permission_report.created_at must be ISO-8601 UTC"
            ) from exc
        if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() != timezone.utc.utcoffset(parsed_created_at):
            raise ContractViolationError(
                "permission_report.created_at must use UTC offset"
            )
    return PermissionFeasibilityReport(
        schema_version=SCHEMA_VERSION,
        run_id=_identifier(raw["run_id"], "permission_report.run_id"),
        status=status,
        strategy=strategy,
        checks=checks,
        evidence=_strings(
            raw["evidence"],
            "permission_report.evidence",
        ),
        orca_version=_string(
            raw["orca_version"],
            "permission_report.orca_version",
        ),
        canonical_path=_string(
            raw["canonical_path"],
            "permission_report.canonical_path",
        ),
        report_digest=claimed,
        environment=(
            None
            if "environment" not in raw
            else _parse_permission_environment(raw["environment"])
        ),
        created_at=created_at,
    )


def permission_capabilities(
    report: PermissionFeasibilityReport,
) -> frozenset[ProviderCapability]:
    checks = {item.check_id: item.status for item in report.checks}
    capabilities: set[ProviderCapability] = set()
    if (
        checks.get("V-PERM-02") is ValidationStatus.PASS
        and checks.get("V-PERM-03") is ValidationStatus.PASS
    ):
        capabilities.add(
            ProviderCapability(
                AgentProvider.CLAUDE,
                AgentAccessMode.READ_ONLY,
            )
        )
    if checks.get("V-PERM-04") is ValidationStatus.PASS:
        capabilities.add(
            ProviderCapability(
                AgentProvider.CODEX,
                AgentAccessMode.READ_ONLY,
            )
        )
    if checks.get("V-PERM-05") is ValidationStatus.PASS:
        capabilities.add(
            ProviderCapability(
                AgentProvider.CODEX,
                AgentAccessMode.WRITABLE,
            )
        )
    if checks.get("V-PERM-06") is ValidationStatus.PASS:
        capabilities.add(
            ProviderCapability(
                AgentProvider.CLAUDE,
                AgentAccessMode.WRITABLE,
            )
        )
    return frozenset(capabilities)


def parse_human_decision(raw_text: str) -> HumanDecision:
    raw = _exact(
        _decode(raw_text),
        {
            "decision",
            "decision_note",
            "affected_acceptance_criteria",
            "affected_finding_ids",
            "report_digest",
        },
        context="human_decision",
    )
    decision = _enum(
        HumanDecisionKind,
        raw["decision"],
        "human_decision.decision",
    )
    note = _optional_string(
        raw["decision_note"],
        "human_decision.decision_note",
    )
    criteria = _strings(
        raw["affected_acceptance_criteria"],
        "human_decision.affected_acceptance_criteria",
    )
    findings = _strings(
        raw["affected_finding_ids"],
        "human_decision.affected_finding_ids",
    )
    if decision in {
        HumanDecisionKind.REVISE_CODE,
        HumanDecisionKind.REVISE_DESIGN,
    } and (not note or (not criteria and not findings)):
        raise ContractViolationError(
            "revision decision requires a note and affected criteria or findings"
        )
    return HumanDecision(
        decision=decision,
        decision_note=note,
        affected_acceptance_criteria=criteria,
        affected_finding_ids=findings,
        report_digest=_digest(
            raw["report_digest"],
            "human_decision.report_digest",
        ),
    )


WIRE_ALIASES_BY_TYPE: dict[type[object], dict[str, str]] = {
    WorkerDonePayload: {
        "task_id": "taskId",
        "dispatch_id": "dispatchId",
        "report_path": "reportPath",
        "artifact_digest": "artifactDigest",
    },
    Finding: {"finding_id": "id", "evidence_refs": "evidence"},
    FindingDecision: {"finding_id": "id", "evidence_refs": "evidence"},
    AddressedFinding: {
        "finding_id": "id",
        "evidence_refs": "evidence",
    },
    EscalationTrigger: {"evidence_refs": "evidence"},
    InformationalFinding: {
        "finding_id": "id",
        "evidence_refs": "evidence",
    },
}


def to_wire_value(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        aliases = WIRE_ALIASES_BY_TYPE.get(type(value), {})
        output: dict[str, object] = {}
        for field in fields(value):
            key = aliases.get(field.name, field.name)
            output[key] = to_wire_value(getattr(value, field.name))
        return output
    if isinstance(value, tuple):
        return [to_wire_value(item) for item in value]
    if isinstance(value, list):
        return [to_wire_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): to_wire_value(item)
            for key, item in value.items()
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractViolationError(
        f"cannot serialize value of type {type(value).__name__}"
    )


def serialize_json(value: object) -> str:
    return json.dumps(
        to_wire_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
