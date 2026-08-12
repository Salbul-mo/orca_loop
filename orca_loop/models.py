from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class LoopState(StrEnum):
    INIT = "INIT"
    PLAN = "PLAN"
    PLAN_REVIEW = "PLAN_REVIEW"
    PLAN_CONSENSUS_EVALUATE = "PLAN_CONSENSUS_EVALUATE"
    PLAN_REVISE = "PLAN_REVISE"
    IMPLEMENT = "IMPLEMENT"
    FIX = "FIX"
    TEST_GATE = "TEST_GATE"
    CODE_REVIEW = "CODE_REVIEW"
    CROSS_CONFIRM = "CROSS_CONFIRM"
    CONSENSUS_EVALUATE = "CONSENSUS_EVALUATE"
    HUMAN_GATE = "HUMAN_GATE"
    READY_FOR_MERGE = "READY_FOR_MERGE"
    REJECTED = "REJECTED"
    USER_DECISION_REQUIRED = "USER_DECISION_REQUIRED"
    FAILED = "FAILED"


class StepStage(StrEnum):
    STEP_PENDING = "STEP_PENDING"
    STEP_PREPARED = "STEP_PREPARED"
    TASK_CREATED = "TASK_CREATED"
    STEP_DISPATCHED = "STEP_DISPATCHED"
    WORKER_DONE_RECEIVED = "WORKER_DONE_RECEIVED"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    TRANSITION_COMMITTED = "TRANSITION_COMMITTED"


class FindingStatus(StrEnum):
    OPEN = "OPEN"
    CHANGE_REQUIRED = "CHANGE_REQUIRED"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"
    RESOLVED = "RESOLVED"
    INFORMATIONAL = "INFORMATIONAL"


class DecisionValue(StrEnum):
    APPROVE = "APPROVE"
    CHANGE_REQUIRED = "CHANGE_REQUIRED"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"


class BlockingReason(StrEnum):
    B1 = "B1"
    B2 = "B2"
    B3 = "B3"
    B4 = "B4"
    B5 = "B5"


class Severity(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class ImpactClass(StrEnum):
    NONE = "none"
    ARCHITECTURE = "architecture"
    REQUIREMENT_INTERPRETATION = "requirement_interpretation"
    DB_SCHEMA = "db_schema"
    EXTERNAL_API = "external_api"
    SECURITY_AUTH = "security_auth"


class Role(StrEnum):
    PLANNER = "planner"
    PLAN_REVIEWER = "plan_reviewer"
    IMPLEMENTER = "implementer"
    CODE_REVIEWER = "code_reviewer"
    CROSS_CONFIRMER = "cross_confirmer"


class WorkerKey(StrEnum):
    """Stable worker-slot IDs whose names do not select a provider."""

    CLAUDE_PLANNER = "claude_planner"
    CLAUDE_CODE_REVIEW = "claude_code_review"
    CODEX_IMPLEMENTER = "codex_implementer"
    CODEX_REVIEW = "codex_review"


class AgentProvider(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"


class AgentAccessMode(StrEnum):
    READ_ONLY = "read_only"
    WRITABLE = "writable"


class Side(StrEnum):
    """Legacy wire values for consensus lanes, not runtime providers."""

    CLAUDE = "CLAUDE"
    CODEX = "CODEX"
    USER = "USER"


class TestKind(StrEnum):
    UNIT = "unit"
    INTEGRATION = "integration"
    DB = "db"
    EXTERNAL = "external"


class TestGateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    POLICY_VIOLATION = "POLICY_VIOLATION"


class ArtifactKind(StrEnum):
    PLAN = "plan"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTATION = "implementation"
    CODE_REVIEW = "code_review"
    CROSS_REVIEW = "cross_review"


class CompletionKind(StrEnum):
    WORKER_DONE = "WORKER_DONE"
    ESCALATION = "ESCALATION"
    DECISION_GATE = "DECISION_GATE"
    STEP_TIMEOUT = "STEP_TIMEOUT"


class SignalKind(StrEnum):
    OK = "ok"
    ARTIFACT_OK = "artifact_ok"
    UNRESOLVED_ZERO = "unresolved_zero"
    UNRESOLVED_REMAIN = "unresolved_remain"
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    MERGE = "merge"
    REJECT = "reject"
    REVISE_CODE = "revise_code"
    REVISE_DESIGN = "revise_design"
    ESCALATE = "escalate"
    ABORT = "abort"
    OPERATIONAL_RETRY = "operational_retry"


class GateKind(StrEnum):
    FINAL = "FINAL"
    ESCALATION = "ESCALATION"
    DESTRUCTIVE = "DESTRUCTIVE"


class HumanDecisionKind(StrEnum):
    MERGE = "merge"
    REJECT = "reject"
    REVISE_CODE = "revise_code"
    REVISE_DESIGN = "revise_design"


class AffectedFileOperation(StrEnum):
    ADD = "add"
    MODIFY = "modify"
    DELETE = "delete"
    RENAME = "rename"


class PermissionStrategy(StrEnum):
    ADD_DIR = "A"
    COORDINATOR_STDOUT = "B"
    ARTIFACT_HELPER = "C"
    READONLY_REPOSITORY = "D"


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"


class RunStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    READY = "READY"
    REJECTED = "REJECTED"


class PlanReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    REVISE = "REVISE"


class CodeReviewVerdict(StrEnum):
    APPROVE = "APPROVE"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ImplementationStatus(StrEnum):
    IMPLEMENTED = "IMPLEMENTED"
    HALTED_FOR_ESCALATION = "HALTED_FOR_ESCALATION"


class TestFailureAttribution(StrEnum):
    NONE = "none"
    IMPLEMENTATION = "implementation"
    ENVIRONMENT = "environment"
    AMBIGUOUS = "ambiguous"


class ConsensusKind(StrEnum):
    PLAN = "plan"
    CODE = "code"


class EscalationCode(StrEnum):
    E01 = "E-01"
    E02 = "E-02"
    E03 = "E-03"
    E04 = "E-04"
    E05 = "E-05"
    E06 = "E-06"
    E07 = "E-07"
    E08 = "E-08"


class ResumeAction(StrEnum):
    CREATE_TASK = "CREATE_TASK"
    DISPATCH_TASK = "DISPATCH_TASK"
    WAIT_DISPATCH = "WAIT_DISPATCH"
    PROMOTE_ARTIFACT = "PROMOTE_ARTIFACT"
    APPLY_TRANSITION = "APPLY_TRANSITION"
    USER_DECISION_REQUIRED = "USER_DECISION_REQUIRED"


@dataclass(frozen=True)
class AffectedFile:
    path: str
    operation: AffectedFileOperation
    rename_from: str | None


@dataclass(frozen=True)
class DestructiveApproval:
    run_id: str
    plan_version: int
    plan_digest: str
    snapshot_digest: str
    approved_operations: tuple[AffectedFile, ...]
    gate_id: str
    decision_digest: str


@dataclass(frozen=True)
class PermissionCheck:
    check_id: str
    status: ValidationStatus
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class PermissionEnvironment:
    """What a permission proof actually depends on.

    Orca creates terminals and routes messages; it does not mediate file
    access. The read-only guarantee comes from the OS ACL applied by
    ``readonly.py`` and from the launch flags in ``profiles.py``, exercised by
    the agent CLIs. Those are the values worth pinning.
    """

    platform: str
    claude_cli: str | None
    codex_cli: str | None
    enforcement_digest: str


@dataclass(frozen=True)
class PermissionFeasibilityReport:
    schema_version: int
    run_id: str
    status: ValidationStatus
    strategy: PermissionStrategy | None
    checks: tuple[PermissionCheck, ...]
    evidence: tuple[str, ...]
    orca_version: str
    canonical_path: str
    report_digest: str
    environment: PermissionEnvironment | None = None
    created_at: str | None = None


@dataclass(frozen=True)
class PermissionSpikeConfig:
    run_id: str
    harness_root: Path
    fixture_path: Path
    coordinator_handle: str
    orca_version: str


@dataclass(frozen=True)
class SnapshotIdentity:
    base_head: str
    tracked_diff_digest: str
    staged_diff_digest: str
    untracked: tuple[tuple[str, str], ...]
    snapshot_digest: str


@dataclass(frozen=True)
class WorkerDonePayload:
    schema_version: int
    task_id: str
    dispatch_id: str
    report_path: str
    artifact_digest: str


@dataclass(frozen=True)
class AgentRuntimeOptions:
    worker_key: WorkerKey
    provider: AgentProvider
    model: str | None
    effort: str | None


@dataclass(frozen=True)
class AgentRuntimeConfig:
    schema_version: int
    agents: tuple[AgentRuntimeOptions, ...]
    configuration_digest: str


@dataclass(frozen=True)
class AgentRuntimeSnapshot:
    schema_version: int
    run_id: str
    agents: tuple[AgentRuntimeOptions, ...]
    configuration_digest: str
    source_config_path: str | None


@dataclass(frozen=True)
class ProviderCapability:
    provider: AgentProvider
    access_mode: AgentAccessMode


@dataclass(frozen=True)
class ExpectedProvenance:
    run_id: str
    task_id: str
    dispatch_id: str
    consensus_round: int
    snapshot_digest: str


@dataclass(frozen=True)
class WorkerHandle:
    worker_key: WorkerKey
    terminal_handle: str
    worktree_id: str
    tab_id: str
    leaf_id: str


@dataclass(frozen=True)
class WorkerPool:
    workers: tuple[WorkerHandle, ...]


@dataclass(frozen=True)
class DispatchHandle:
    step_id: str
    task_id: str
    dispatch_id: str
    worker: WorkerHandle
    role: Role
    worktree_id: str
    tab_id: str
    leaf_id: str


@dataclass(frozen=True)
class Completion:
    kind: CompletionKind
    task_id: str
    dispatch_id: str
    payload_json: str | None


@dataclass(frozen=True)
class TestCommand:
    argv: tuple[str, ...]
    cwd: str
    timeout_ms: int
    kind: TestKind


@dataclass(frozen=True)
class TestContract:
    commands: tuple[TestCommand, ...]
    test_ids: tuple[str, ...]


@dataclass(frozen=True)
class TestExecutionPolicy:
    allowed_commands: tuple[TestCommand, ...]
    allowed_env_keys: tuple[str, ...]
    allowed_output_paths: tuple[str, ...]
    approved_kinds: tuple[TestKind, ...]
    policy_digest: str


@dataclass(frozen=True)
class AcceptanceCriterion:
    criterion_id: str
    verification_method: str


@dataclass(frozen=True)
class EscalationTrigger:
    code: EscalationCode
    reason: str
    evidence_refs: tuple[str, ...]
    deduplication_key: str


@dataclass(frozen=True)
class Finding:
    finding_id: str
    severity: Severity
    blocking_reason: BlockingReason
    impact_class: ImpactClass
    file: str | None
    line: int | None
    root_cause: str
    description: str
    required_fix: str | None
    required_change: str | None
    acceptance_criteria_ids: tuple[str, ...]
    affected_files: tuple[str, ...]
    test_ids: tuple[str, ...]
    depends_on: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    reopens: str | None


@dataclass(frozen=True)
class FindingDecision:
    finding_id: str
    side: Side
    decision: DecisionValue
    snapshot_digest: str
    round: int
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class SignatureObservation:
    round: int
    signature: str
    status: FindingStatus
    acceptance_criteria_ids: tuple[str, ...]
    affected_files: tuple[str, ...]
    root_cause: str
    required_action: str
    material_progress: bool


@dataclass(frozen=True)
class FindingRecord:
    finding: Finding
    status: FindingStatus
    opened_round: int
    resolved_round: int | None
    max_status_reached: FindingStatus
    unresolved_signature_history: tuple[SignatureObservation, ...]
    resolved_snapshot_digest: str | None
    decisions: tuple[FindingDecision, ...]
    resolution: str | None


@dataclass(frozen=True)
class InformationalFinding:
    finding_id: str
    description: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReopenedFinding:
    finding_id: str
    reopens: str
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ConsensusLedger:
    schema_version: int
    run_id: str
    generation: int
    findings: tuple[FindingRecord, ...]
    plan_round: int
    code_round: int
    informational: tuple[InformationalFinding, ...]
    reopened: tuple[ReopenedFinding, ...]
    approved_escalation_keys: tuple[str, ...]


@dataclass(frozen=True)
class LedgerView:
    plan_round: int
    code_round: int
    unresolved_count: int
    approved_escalation_keys: tuple[str, ...]


@dataclass(frozen=True)
class RoundEvidence:
    kind: ConsensusKind
    consensus_round: int
    reviewed_plan_version: int | None
    reviewed_snapshot_digest: str | None
    artifact_digests: tuple[str, str]
    changed_during_round: bool
    both_artifacts_valid: bool


@dataclass(frozen=True)
class LedgerUpdate:
    ledger: ConsensusLedger
    escalations: tuple[EscalationTrigger, ...]
    committed_round: bool


@dataclass(frozen=True)
class LoopCounters:
    test_fix_attempts: int
    operational_retries: int


@dataclass(frozen=True)
class TransitionSignal:
    kind: SignalKind
    reason: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlanDocument:
    schema_version: int
    plan_version: int
    request_digest: str
    source_instruction: str
    interpretation: str
    rationale: str
    current_state_evidence: tuple[str, ...]
    affected_files: tuple[AffectedFile, ...]
    implementation_steps: tuple[str, ...]
    data_api_schema_changes: str
    error_handling: tuple[str, ...]
    test_contract: TestContract
    test_policy_digest: str
    acceptance_criteria: tuple[AcceptanceCriterion, ...]
    risks: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    reviewed_finding_ids: tuple[str, ...]
    finding_decisions: tuple[FindingDecision, ...]


@dataclass(frozen=True)
class ReviewArtifact:
    schema_version: int
    artifact_kind: ArtifactKind
    run_id: str
    task_id: str
    dispatch_id: str
    consensus_round: int
    snapshot_digest: str
    role: Role
    verdict: PlanReviewVerdict | CodeReviewVerdict
    reviewed_plan_version: int | None
    reviewed_artifact_digest: str
    reviewed_finding_ids: tuple[str, ...]
    finding_decisions: tuple[FindingDecision, ...]
    findings: tuple[Finding, ...]
    non_blocking_suggestions: tuple[InformationalFinding, ...]
    escalation_signals: tuple[EscalationTrigger, ...]
    agrees_with_reviewer: bool | None


@dataclass(frozen=True)
class AddressedFinding:
    finding_id: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class ImplementationArtifact:
    schema_version: int
    run_id: str
    task_id: str
    dispatch_id: str
    consensus_round: int
    snapshot_digest: str
    status: ImplementationStatus
    addressed_findings: tuple[AddressedFinding, ...]
    changed_files: tuple[str, ...]
    summary: str
    test_failure_attribution: TestFailureAttribution
    plan_change_required: bool
    escalation_signals: tuple[EscalationTrigger, ...]


@dataclass(frozen=True)
class HumanDecision:
    decision: HumanDecisionKind
    decision_note: str | None
    affected_acceptance_criteria: tuple[str, ...]
    affected_finding_ids: tuple[str, ...]
    report_digest: str


@dataclass(frozen=True)
class ScopePackage:
    finding_ids: tuple[str, ...]
    acceptance_criteria_ids: tuple[str, ...]
    affected_files: tuple[str, ...]
    test_ids: tuple[str, ...]
    targeted_test_results: tuple[str, ...]
    disagreement_excerpts: tuple[str, ...]


@dataclass(frozen=True)
class RoleContext:
    role: Role
    provider: AgentProvider
    run_id: str
    consensus_round: int
    worktree_path: Path
    step_dir: Path
    coordinator_handle: str
    plan_version: int
    snapshot_digest: str
    scope_package: ScopePackage
    test_gate_result: TestGateStatus | None
    test_policy: TestExecutionPolicy | None
    delivered_finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class StepExecutionResult:
    signal: TransitionSignal
    ledger: ConsensusLedger
    test_gate_status: TestGateStatus | None
    escalations: tuple[EscalationTrigger, ...] = ()


@dataclass(frozen=True)
class TransitionResult:
    next_state: LoopState
    counters_after: LoopCounters
    reason: str


@dataclass(frozen=True)
class Violation:
    code: str
    path: str | None
    detail: str


@dataclass(frozen=True)
class GuardReport:
    ok: bool
    violations: tuple[Violation, ...]


@dataclass(frozen=True)
class TestPolicyViolation:
    code: str
    command_index: int | None
    detail: str


@dataclass(frozen=True)
class PolicyValidation:
    approved: bool
    violations: tuple[TestPolicyViolation, ...]


@dataclass(frozen=True)
class TestCommandResult:
    command: TestCommand
    return_code: int | None
    timed_out: bool
    stdout_tail: str
    stderr_tail: str


@dataclass(frozen=True)
class TestGateResult:
    status: TestGateStatus
    command_results: tuple[TestCommandResult, ...]
    policy_violations: tuple[TestPolicyViolation, ...]
    policy_digest: str | None
    before_snapshot: SnapshotIdentity
    after_snapshot: SnapshotIdentity | None
    attribution: TestFailureAttribution


@dataclass(frozen=True)
class DigestEntry:
    path: str
    digest: str


@dataclass(frozen=True)
class StagedInput:
    name: str
    source_path: Path | None
    inline_bytes: bytes | None


@dataclass(frozen=True)
class InputManifest:
    entries: tuple[DigestEntry, ...]
    manifest_digest: str


@dataclass(frozen=True)
class StepWorkspace:
    step_id: str
    root: Path
    input_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class RunWorkspace:
    run_id: str
    root: Path
    control_dir: Path
    artifact_dir: Path
    review_dir: Path
    steps_dir: Path
    log_dir: Path


@dataclass(frozen=True)
class BootstrapReport:
    repo_initialized: bool
    package_importable: bool
    repo_id: str
    kind: str


@dataclass(frozen=True)
class LaunchProfile:
    command: tuple[str, ...]
    writable_roots: tuple[Path, ...]
    permission_report_digest: str


@dataclass(frozen=True)
class OrcaResponse:
    result_json: str
    stdout: str
    stderr: str


@dataclass(frozen=True)
class PreparedTask:
    step_id: str
    task_id: str
    worker: WorkerHandle
    role: Role
    contract_digest: str


@dataclass(frozen=True)
class ScopeManifest:
    snapshot_digest: str
    affected_files: tuple[AffectedFile, ...]
    destructive_approval_digest: str | None


@dataclass(frozen=True)
class FrozenReview:
    diff_path: Path
    manifest_path: Path
    snapshot_digest: str


@dataclass(frozen=True)
class RenderedContract:
    text: str
    digest: str


@dataclass(frozen=True)
class PromotedArtifact:
    canonical_path: Path
    raw_text: str
    artifact_digest: str
    artifact_kind: ArtifactKind


@dataclass(frozen=True)
class DecisionReport:
    path: Path
    digest: str
    finding_ids: tuple[str, ...]


@dataclass(frozen=True)
class GateBinding:
    gate_id: str
    task_id: str
    report_digest: str
    gate_kind: GateKind


@dataclass(frozen=True)
class ActiveStep:
    step_id: str
    task_id: str | None
    dispatch_id: str | None
    role: Role
    worker: WorkerHandle | None


@dataclass(frozen=True)
class StateHistoryEntry:
    generation: int
    state: LoopState
    step_stage: StepStage
    signal: SignalKind
    reason: str


@dataclass(frozen=True)
class CoordinatorState:
    schema_version: int
    generation: int
    run_id: str
    state: LoopState
    step_stage: StepStage
    status: RunStatus
    worktree_selector: str
    coordinator_handle: str
    worker_handles: tuple[WorkerHandle, ...]
    active: ActiveStep | None
    plan_version: int
    counters: LoopCounters
    base_head: str
    snapshot_digest: str
    test_gate_status: TestGateStatus | None
    test_policy_digest: str | None
    permission_report_digest: str
    history: tuple[StateHistoryEntry, ...]
    gate_binding: GateBinding | None = None
    human_decision: HumanDecision | None = None
    destructive_approval: DestructiveApproval | None = None
    blocked_from_state: LoopState | None = None
    pending_escalations: tuple[EscalationTrigger, ...] = ()


@dataclass(frozen=True)
class CommitManifest:
    committed_generation: int
    state_digest: str
    ledger_digest: str


@dataclass(frozen=True)
class ResumeDecision:
    action: ResumeAction
    state: CoordinatorState
    ledger: ConsensusLedger
    active: ActiveStep | None


@dataclass(frozen=True)
class E2EConfig:
    fixture_path: Path
    coordinator_handle: str
    permission_report_digest: str


@dataclass(frozen=True)
class LoopConfig:
    worktree_path: Path
    request_path: Path
    coordinator_handle: str
    test_policy_path: Path | None
    plan_consensus_round_limit: int
    code_consensus_round_limit: int
    test_fix_attempt_limit: int
    operational_retry_limit: int
    max_transition_count: int
    step_timeout_ms: int
    total_timeout_ms: int
