# Task Report: Orca 기반 Claude/Codex 교차 검수 자동화 루프

**Current Phase:** 3. Micro Blocking
**Revision:** 3
**Status:** Revision Requested — Awaiting Explicit User Approval
**작성일:** 2026-07-31
**Proposed Baseline:** `docs/phase1-system-design.md` Revision 7,
`docs/phase2-macro-blocking.md` Revision 7 (**모두 재승인 대기**)
**Runtime Contract:** Orca ADE `1.4.159`, `orca skills get orchestration`

---

## 1. Context & Objective

- **Problem:** 제안된 16개 Macro Block을 바로 구현하고 독립 검증할 수 있는 Python 작업 단위로
  세분화해야 한다.
- **Goal:** 모든 Micro Block에 정확한 type contract, 입력 검증, 상태 변경, 예외, 상세
  pseudocode, 테스트 및 rollback을 정의한다.
- **Scope:** `orca_harness` Python coordinator, Orca CLI adapter, artifact/ledger/state machine,
  test execution policy, decision gate 및 전체 테스트.
- **Out of Scope:** 실제 대상 프로젝트 기능 구현, 자동 merge/push/commit, DB·external test의
  무승인 실행, Orca runtime-global state reset.

### 1.1 Baseline consistency corrections

Phase 3는 다음 항목을 구현 세부사항으로 확정한다. 이들은 사용자 요구를 바꾸지 않으며,
Macro pseudocode의 race 또는 상태 유실 가능성을 제거한다.

| ID | 확정 내용 | 영향 |
|---|---|---|
| `C3-01` | coordinator가 `step_id`를 먼저 발급하고 입력을 staging한 뒤 task를 생성·dispatch한다. `dispatch_id`는 filesystem 이름으로 사용하지 않고 metadata로 바인딩한다 | `B-01`, `B-06`, `B-10`, `B-12`, `MR-10` |
| `C3-02` | `_execute_state()`와 `_evaluate()`는 `StepExecutionResult`로 갱신된 `ConsensusLedger`를 반환하며 caller가 이를 다음 generation의 유일한 ledger로 사용한다 | `B-07`, `B-12` |
| `C3-03` | LLM이 만든 test command는 coordinator 소유 policy의 **exact argv allowlist**와 일치할 때만 sanitized environment에서 실행한다. test 전후 snapshot delta도 검증한다 | `B-09`, `B-11`, `B-12`, `MR-9` |
| `C3-04` | `has_forbidden_alternative_plan_section()`은 keyword 판정이 아니라 `contracts.py`의 구조화된 reviewer output section/schema 검사로 구현한다 | `B-03`, `MR-4` |
| `C3-05` | Permission Feasibility Spike를 모든 구현보다 먼저 실행하고, 성립한 artifact write strategy가 없으면 `BLOCKED`로 종료한다 | `B-00`, `P3-R1` |
| `C3-06` | Micro `Preconditions`를 canonical DAG source로 사용하고 undefined ID, cycle, Phase 4 topological order 위반을 검증 실패로 처리한다 | `B-15` |
| `C3-07` | plan/code 합의 round 상한은 사용자 확정값 **5/5**다. 합의 시 조기 종료하고 동일 무진전 signature 2회면 `E-05`가 먼저 발화한다 | `B-07`, `B-14` |
| `C3-08` | `models.py`, `profiles.py`, `bootstrap.py`, `workspace.py`, `generation.py`, `locking.py`로 책임을 분리하며 Phase 2 각 Scope에 같은 모듈 경계를 선언한다 | `B-01`, `B-02`, `B-03`, `B-12`, `B-14` |
| `C3-09` | Phase 1 §8 boundary artifact를 필드 손실 없이 dataclass로 사상하고 wire camelCase는 명시적 alias table로만 변환한다 | `B-03`, `B-07`, `B-12` |

`C3-01`의 durable 순서는 다음으로 고정한다.

```text
STEP_PREPARED(step_id, input_digest)
→ TASK_CREATED(task_id)
→ STEP_DISPATCHED(task_id, dispatch_id)
→ WORKER_DONE_RECEIVED
→ ARTIFACT_VERIFIED
→ TRANSITION_COMMITTED
```

### 1.2 Source filename rule

Phase 1 §15 `Q-6`의 프로젝트별 면제를 baseline으로 적용하여 functional Python module은
`orca_loop/contracts.py`와 같은 import 가능한 이름을 사용한다. 이 Phase 3 문서와 같은
Codex 생성 문서에는 `codex-mhj_YY_MM_DD_<sequence>_` 접두사를 유지한다.

---

## 2. Shared Boundary Exact Type Contracts

아래 type은 모든 Micro Block에서 동일한 의미로 사용한다.

```python
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
    CLAUDE_PLANNER = "claude_planner"
    CLAUDE_CODE_REVIEW = "claude_code_review"
    CODEX_IMPLEMENTER = "codex_implementer"
    CODEX_REVIEW = "codex_review"

class Side(StrEnum):
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
```

JSON artifact parser는 `dict[str, object]`를 application 내부 type으로 전달하지 않는다.
모든 boundary JSON은 dataclass 또는 enum으로 변환된 뒤에만 coordinator에 진입한다.
`orca_loop/models.py`의 위 선언이 단일 기준이며, `contracts.py` parser와 schema fixture는
dataclass field 이름·enum 값을 대조하는 contract test로 drift를 차단한다.

**wire alias 규칙:** Phase 1의 기존 wire 계약을 보존하기 위해 parser별 alias table을
명시한다.

```text
worker_done:
  taskId→task_id, dispatchId→dispatch_id,
  reportPath→report_path, artifactDigest→artifact_digest
Finding / FindingDecision / AddressedFinding / ReopenedFinding:
  id→finding_id
Finding / FindingDecision / AddressedFinding / EscalationTrigger / ReopenedFinding:
  evidence→evidence_refs
```

그 외 artifact JSON은 snake_case다. alias table에 없는 key와 alias/canonical key 동시
출현은 거부한다. serializer는 Phase 1 wire 이름을 출력하며 parse→serialize→parse
contract test가 정보 손실 0을 검증한다.

`ReviewArtifact` parser는 `artifact_kind`별 verdict를 강제한다.

```text
PLAN_REVIEW  → PlanReviewVerdict
CODE_REVIEW / CROSS_REVIEW → CodeReviewVerdict
CROSS_REVIEW → agrees_with_reviewer is bool
others       → agrees_with_reviewer is None
```

---

## 3. Micro Block Dependency Order

아래 목록은 `Preconditions`에서 계산한 **topological layer**만 표시한다. 화살표로 수기 작성한
별도 edge graph는 두지 않으며, 각 Micro Block의 `Preconditions`가 유일한 의존성 진실
원천이다.

```text
Layer 0  M-B00-01
Layer 1  M-B01-01
Layer 2  M-B01-02
Layer 3  M-B01-03
Layer 4  M-B02-01, M-B03-01, M-B04-01, M-B06-01, M-B14-01
Layer 5  M-B02-02, M-B03-02, M-B11-01
Layer 6  M-B04-02, M-B05-01, M-B06-02, M-B07-01, M-B09-01,
         M-B10-01, M-B12-01
Layer 7  M-B07-02, M-B09-02, M-B10-02
Layer 8  M-B07-03, M-B08-01, M-B10-03, M-B11-02
Layer 9  M-B05-02, M-B08-02, M-B13-01
Layer 10 M-B13-02
Layer 11 M-B12-05, M-B13-03
Layer 12 M-B12-02
Layer 13 M-B12-03
Layer 14 M-B12-04
Layer 15 M-B14-02
Layer 16 M-B15-01
Layer 17 M-B15-02
Layer 18 M-B15-03
```

`M-B15-01`의 `tests/test_plan_traceability.py`가 모든
`Preconditions`의 `M-*` ID를 파싱해 canonical DAG를 재생성하고, 순환·미정의 ID 또는
Phase 4 order에서 선행 block이 뒤에 놓인 경우 실패한다.

---

## 4. Micro Blocks

### M-B00-01 — Permission Feasibility Spike

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B00-01` |
| **Parent Block** | `B-00` |
| **Name** | Permission feasibility first gate |
| **Rationale** | repository read/source mutation 차단/own out write의 동시 성립을 coordinator 구현 전에 증명해야 한다 |
| **Objective** | disposable fixture에서 `V-PERM-01..05`를 실행하고 유효 strategy 하나를 확정하거나 `BLOCKED` |
| **Target Files** | `permission_spike.py`, `tests/test_permission_feasibility.py`, runtime `runs/<run_id>/control/permission-feasibility.json` |
| **Preconditions** | Orca ready, explicit coordinator handle, disposable fixture marker |
| **Input Type** | `PermissionSpikeConfig` |
| **Input Validation** | production worktree 거부, fixture resolved path, Orca version 확인 |
| **Output Type** | `PermissionFeasibilityReport` |
| **Output Validation** | `PASS`이면 check 5개 전부 pass, strategy `A|B|C|D`, current Orca version 일치, canonical report digest 일치 |
| **Exceptions** | runtime/permission 불가를 `BLOCKED` report로 보존; malformed response는 `PermissionSpikeError` |
| **Side Effects** | 최소 `runs/<run_id>/control/`과 report, disposable Git fixture, Orca terminal/task/dispatch; 생성 ID 전부 기록 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B00-01` five-check report schema, `T-B00-02` partial pass가 후속 진행 차단, `T-B00-03` production path 거부 |
| **Rollback** | 생성 runtime ID를 report; fixture만 정리 가능, production 자동 변경 없음 |

```text
verify disposable marker and repository boundary
create only runs/<run_id>/control required for the pre-bootstrap report
for strategy in A, B, C, D:
  launch four isolated role sessions
  run repository read, forbidden source write, own out write checks
  run implementer approved source write check
  capture before/after digests and runtime provenance
  if all five checks pass:
    bind current Orca appVersion
    canonicalize report JSON without report_digest using sorted-key compact UTF-8 JSON
    set report_digest to SHA-256 of those bytes
    atomically persist under runs/<run_id>/control/permission-feasibility.json
    return
persist BLOCKED report and stop before M-B01-01
```

### M-B01-01 — Repository Bootstrap

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B01-01` |
| **Parent Block** | `B-01` |
| **Name** | Repository bootstrap and package skeleton |
| **Rationale** | coordinator 자체를 Git과 `unittest`로 검증할 최소 구조가 필요하다 |
| **Objective** | `.git/`, `.gitignore`, `orca_loop/__init__.py`, `prompts/`, `tests/`가 존재하고 `import orca_loop`가 성공 |
| **Target Files** | `.gitignore`, `orca_loop/__init__.py` |
| **Preconditions** | `M-B00-01` `PASS`, workspace path가 존재하고 쓰기 가능 |
| **Input Type** | `Path harness_root` |
| **Input Validation** | 절대경로, directory, workspace root와 동일한 resolved path |
| **Output Type** | `BootstrapReport(repo_initialized: bool, package_importable: bool)` |
| **Output Validation** | `git status --porcelain` exit `0`, Python import exit `0` |
| **Exceptions** | `BootstrapError`, `PermissionError`, 원인 보존 |
| **Side Effects** | `git init`, directory와 bootstrap 파일 생성 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B01-01` temp directory 신규 bootstrap, `T-B01-02` idempotent 재실행 |
| **Rollback** | 생성 목록을 report에 기록하며 자동 삭제하지 않음 |

```text
resolve and validate harness_root
if .git absent: execute ["git", "init"] with shell=False
write .gitignore only when absent
create orca_loop, prompts, tests, docs, runs
write empty orca_loop/__init__.py only when absent
execute Python import check
return BootstrapReport
```

### M-B01-02 — Orca Repository Registration

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B01-02` |
| **Parent Block** | `B-01` |
| **Name** | Orca repository registration verification |
| **Rationale** | local `git init`만으로 Orca의 기존 `kind=="folder"` record가 자동으로 Git repository가 되지는 않는다 |
| **Objective** | `orca repo add --path <root> --json` 후 same-path record가 정확히 하나이고 `kind=="git"`임을 검증 |
| **Target Files** | `orca_loop/bootstrap.py`, `tests/test_bootstrap.py` |
| **Preconditions** | `M-B01-01`, Orca executable resolved |
| **Input Type** | `Path harness_root`, `str orca_executable` |
| **Input Validation** | harness root는 Git repository, resolved path exact match |
| **Output Type** | `BootstrapReport(repo_initialized: bool, package_importable: bool, repo_id: str, kind: str)` |
| **Output Validation** | same-path record count `1`, nonempty `repo_id`, `kind=="git"` |
| **Exceptions** | `OrcaRepositoryRegistrationError`; duplicate/`folder` 잔존은 `BLOCKED` |
| **Side Effects** | `orca repo add`로 Orca repository record 생성 또는 갱신 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B01-03` folder→git 등록, `T-B01-04` duplicate 감지, `T-B01-05` kind folder면 BLOCKED, `T-B01-06` 자동 삭제 명령 없음 |
| **Rollback** | 중복 record를 자동 삭제하지 않고 response와 record IDs를 report |

```text
run orca repo add --path harness_root --json with shell=False
run orca repo list --json
select records whose resolved path equals harness_root
if record count is not one: raise BLOCKED without deleting any record
if record kind is not git: raise BLOCKED
return BootstrapReport with repo_id and kind
```

### M-B01-03 — Run and Step Workspace

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B01-03` |
| **Parent Block** | `B-01` |
| **Name** | Stable run/step workspace |
| **Rationale** | `dispatch_id` 발급 전 입력을 staging해야 race가 사라진다 |
| **Objective** | coordinator 발급 `step_id`로 격리된 `in/`·`out/`을 만들고 control과 분리 |
| **Target Files** | `orca_loop/workspace.py` |
| **Preconditions** | `M-B01-02`, 유효한 `run_id` |
| **Input Type** | `Path harness_root`, `str run_id`, `str step_id`, `bool resume` |
| **Input Validation** | ID regex `[A-Za-z0-9_-]{1,80}`, path traversal 금지; non-resume 기존 경로는 valid B-00 pre-bootstrap report만 든 exact skeleton만 허용 |
| **Output Type** | `RunWorkspace`, `StepWorkspace(step_id: str, root: Path, input_dir: Path, output_dir: Path)` |
| **Output Validation** | 모든 경로가 `runs/<run_id>` 내부이고 `control/`과 겹치지 않음 |
| **Exceptions** | `RunWorkspaceExistsError`, `PathBoundaryError` |
| **Side Effects** | `runs/<run_id>/{control,artifacts,review,steps,logs}` 및 `steps/<step_id>/{in,out}` 생성 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B01-07` traversal 거부, `T-B01-08` resume 재사용, `T-B01-09` control 격리, `T-B01-10` valid pre-bootstrap skeleton 승계, `T-B01-11` 그 외 기존 run 거부 |
| **Rollback** | 자동 삭제 없음; 생성 경로를 report에 남김 |

```text
validate run_id and step_id
resolve run root under harness_root/runs
reject existing run unless resume or it is the exact B-00 skeleton:
  only control/permission-feasibility.json exists and run_id/path/digest all validate
create fixed run subdirectories
create steps/step_id/in and steps/step_id/out
assert input/output/control pairwise non-overlap
return typed workspace objects
```

### M-B02-01 — Orca CLI Resolution and JSON Call

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B02-01` |
| **Parent Block** | `B-02` |
| **Name** | Version-bound Orca CLI adapter |
| **Rationale** | stdout JSON과 stderr keepalive를 분리하고 executable drift를 차단 |
| **Objective** | 모든 Orca command가 argv 기반, timeout 적용, `ok:false` 검출 |
| **Target Files** | `orca_loop/orca_client.py` |
| **Preconditions** | `M-B01-03`, Orca executable이 PATH 또는 approved environment variable에 존재 |
| **Input Type** | `tuple[str, ...] argv`, `int timeout_ms` |
| **Input Validation** | 빈 argv 금지, timeout `1..14_400_000`, executable resolution 단 한 번 |
| **Output Type** | `OrcaResponse` |
| **Output Validation** | top-level JSON object, `ok is not False` |
| **Exceptions** | `OrcaCommandError`, `OrcaTimeoutError`, `OrcaProtocolError` |
| **Side Effects** | 지정 Orca RPC만 실행 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B02-01` stderr keepalive, `T-B02-02` malformed JSON, `T-B02-03` `ok:false`, `T-B02-04` timeout |
| **Rollback** | read command는 불필요; mutation command 실패는 response ID와 argv를 기록 |

```text
resolve once:
  ORCA_CLI_COMMAND when set
  else orca-dev when ORCA_DEV_REPO_ROOT is set
  else orca-ide on Linux outside an Orca-managed terminal
  else orca
never fall through to another executable after a selected command fails
append "--json" only when argv does not already contain it
run [resolved_executable, *argv] with shell=False
capture stdout and stderr independently
on timeout terminate process tree and raise OrcaTimeoutError
parse stdout as one JSON document
reject non-object or ok=false
return OrcaResponse
```

### M-B02-02 — Worker Launch Profiles

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B02-02` |
| **Parent Block** | `B-02` |
| **Name** | Role-specific launch command builder |
| **Rationale** | role permission 차이를 data contract로 고정 |
| **Objective** | 5개 role에 대해 deterministic argv 생성 |
| **Target Files** | `orca_loop/config.py`, `orca_loop/profiles.py` |
| **Preconditions** | `M-B00-01` `PASS`, `M-B02-01`, `codex --help`와 `claude --help` flag 확인 |
| **Input Type** | `Role`, `Path worktree`, `Path step_input`, `Path step_output`, `PermissionFeasibilityReport` |
| **Input Validation** | 모든 경로 절대·존재, role enum, control canonical path에서 로드한 permission report digest/Orca version/`PASS` strategy |
| **Output Type** | `LaunchProfile(command: tuple[str, ...], writable_roots: tuple[Path, ...])` |
| **Output Validation** | 선택 strategy의 spike evidence와 argv가 일치하고 reviewer는 source write 미허용, implementer만 approved source write |
| **Exceptions** | `LaunchProfileError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B02-05` role별 golden argv, `T-B02-06` run root 전체 add 금지 |
| **Rollback** | configuration object 폐기 |

```text
match role
build command from the one PermissionStrategy proven by M-B00-01
never silently fall back to an unverified strategy
keep planner and code_reviewer in separate WorkerKey sessions
store declared writable roots for post-step guard
return immutable LaunchProfile
```

### M-B03-01 — Domain Enums and Dataclasses

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B03-01` |
| **Parent Block** | `B-03` |
| **Name** | Internal domain contracts |
| **Rationale** | unstructured JSON이 state machine에 유입되는 것을 방지 |
| **Objective** | state, role, finding, decision, artifact, test types 정의 |
| **Target Files** | `orca_loop/models.py` |
| **Preconditions** | `M-B01-03`, Python `>=3.11` |
| **Input Type** | 없음 |
| **Input Validation** | enum 값은 Phase 1 contract와 정확히 일치 |
| **Output Type** | frozen dataclass와 `StrEnum` definitions |
| **Output Validation** | mypy 없이도 constructor invariant test 통과 |
| **Exceptions** | invalid constructor는 `ValueError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B03-01` enum roundtrip, `T-B03-02` frozen mutation 거부 |
| **Rollback** | 파일 제거; 외부 persistent state 없음 |

```text
define every enum from Phase 1
define frozen dataclasses with concrete field types
validate IDs and digests in __post_init__
reject unknown enum values at parser boundary
```

### M-B03-02 — Boundary Artifact Parsers

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B03-02` |
| **Parent Block** | `B-03` |
| **Name** | Strict JSON and plan parser |
| **Rationale** | 승인 판정은 schema가 검증된 artifact만 사용해야 한다 |
| **Objective** | plan/review/implementation/worker_done/test policy를 typed object로 변환 |
| **Target Files** | `orca_loop/contracts.py` |
| **Preconditions** | `M-B03-01` |
| **Input Type** | `str raw_text`, `ArtifactKind expected_kind`, `ExpectedProvenance` |
| **Input Validation** | UTF-8, size `1..1_048_576`, required/unknown field 검사, enum·ID·digest 형식 |
| **Output Type** | `PlanDocument | ReviewArtifact | ImplementationArtifact | WorkerDonePayload` |
| **Output Validation** | provenance, `reviewed_finding_ids`, verdict-by-kind, escalation/finding schema completeness |
| **Exceptions** | `ContractViolationError(reason: str)`, `ProvenanceError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B03-03` 정상 artifact, `T-B03-04` unknown field, `T-B03-05` missing finding decision, `T-B03-06` oversized payload, `T-B03-07` alternative-plan section, `T-B03-08` cross-review agreement, `T-B03-09` camelCase alias |
| **Rollback** | 없음 |

```text
decode JSON or extract required fenced JSON section
require exact schema_version
reject unknown keys unless schema marks optional
construct typed nested objects
validate task, dispatch, run, snapshot provenance
validate reviewed_finding_ids equals delivered unresolved IDs
validate every blocking Finding has B1..B5, impact_class and exactly one required action
validate plan/code verdict enum by ArtifactKind
validate CROSS_REVIEW agrees_with_reviewer and snapshot equality
reject reviewer output sections implementation_steps, proposed_architecture, replacement_plan
do not reject ordinary rationale prose containing "alternative" or "recommend"
apply approval-obligation checks
return typed artifact
```

### M-B04-01 — Snapshot Capture and Canonical Digest

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B04-01` |
| **Parent Block** | `B-04` |
| **Name** | Git snapshot identity |
| **Rationale** | 같은 구현을 검토했다는 증명이 필요 |
| **Objective** | tracked, staged, untracked를 포함한 deterministic digest 생성 |
| **Target Files** | `orca_loop/snapshot.py` |
| **Preconditions** | `M-B01-03`, target worktree가 Git repository이고 `HEAD` 존재 |
| **Input Type** | `Path worktree` |
| **Input Validation** | resolved path, `.git` context 확인 |
| **Output Type** | `SnapshotIdentity` |
| **Output Validation** | 아래 byte-level canonicalization과 정확히 일치 |
| **Exceptions** | `SnapshotError`, `GitCommandError` |
| **Side Effects** | Git read commands |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B04-01` ordering 독립, `T-B04-02` CRLF/LF 동일, `T-B04-03` binary 변화 탐지 |
| **Rollback** | 불필요 |

```text
canonical_component(tag, data):
  return u32be(len(utf8(tag))) + utf8(tag) + u64be(len(data)) + data

canonical_content(raw):
  if NUL exists or strict UTF-8 decode fails: return b"B" + raw
  text = strict UTF-8 decode(raw).replace("\r\n", "\n")
  return b"T" + UTF-8(text)

base_head = git rev-parse HEAD stripped ASCII lowercase hex
tracked = canonical_content(bytes from git diff --binary)
staged = canonical_content(bytes from git diff --cached --binary)
untracked paths = git ls-files --others --exclude-standard -z
normalize each path to repository-relative POSIX Unicode NFC
sort paths by normalized UTF-8 byte sequence
for each path:
  entry = canonical_component("path", utf8(path))
        + canonical_component("content", canonical_content(file_bytes))

snapshot_bytes = utf8("orca-snapshot-v1") + NUL
               + canonical_component("base_head", ascii(base_head))
               + canonical_component("tracked_diff", tracked)
               + canonical_component("staged_diff", staged)
               + each canonical_component("untracked", entry) in sorted order

tracked_diff_digest = "sha256:" + hex(sha256(tracked))
staged_diff_digest = "sha256:" + hex(sha256(staged))
snapshot_digest = "sha256:" + hex(sha256(snapshot_bytes))
return SnapshotIdentity
```

### M-B04-02 — Frozen Diff and Scope Manifest

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B04-02` |
| **Parent Block** | `B-04` |
| **Name** | Review snapshot materialization |
| **Rationale** | code reviewer 판단 근거를 불변 파일로 제공 |
| **Objective** | snapshot digest와 일치하는 diff 및 affected scope 생성 |
| **Target Files** | `orca_loop/snapshot.py` |
| **Preconditions** | `M-B03-02`, `M-B04-01`, approved `PlanDocument` |
| **Input Type** | `Path worktree`, `SnapshotIdentity`, `tuple[AffectedFile, ...]` |
| **Input Validation** | affected path normalize; delete/rename은 계획 명시와 사용자 파괴적 작업 승인 evidence 필요 |
| **Output Type** | `FrozenReview(diff_path: Path, manifest_path: Path, digest: str)` |
| **Output Validation** | written digest가 input snapshot과 일치 |
| **Exceptions** | `SnapshotChangedError`, `PathBoundaryError` |
| **Side Effects** | `runs/<run_id>/review/*` 기록 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B04-04` capture 중 변경 탐지, `T-B04-05` manifest roundtrip |
| **Rollback** | 새 temporary file 삭제 후 canonical 미변경 |

```text
capture before identity
write complete binary-safe diff to temporary file
write normalized scope manifest to temporary file
capture after identity
if before != after raise SnapshotChangedError
atomic replace canonical review files
return FrozenReview
```

### M-B05-01 — Role Contract Templates

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B05-01` |
| **Parent Block** | `B-05` |
| **Name** | Five immutable role templates |
| **Rationale** | 역할·금지·승인의무를 dispatch마다 동일하게 적용 |
| **Objective** | planner, plan reviewer, implementer, code reviewer, cross confirmer template 완성 |
| **Target Files** | `prompts/planner.md`, `prompts/plan_reviewer.md`, `prompts/implementer.md`, `prompts/code_reviewer.md`, `prompts/cross_confirmer.md` |
| **Preconditions** | `M-B03-02`, Phase 1 §6 contract |
| **Input Type** | template placeholders의 명시적 schema |
| **Input Validation** | unknown placeholder 금지 |
| **Output Type** | UTF-8 Markdown template 5개 |
| **Output Validation** | role, input, prohibition, artifact path, worker_done instruction 모두 존재; planner/plan reviewer에 동일 test policy 입력 4종 존재 |
| **Exceptions** | `TemplateContractError` |
| **Side Effects** | prompt files 생성 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B05-01` required marker, `T-B05-02` unknown placeholder, `T-B05-03` test policy digest/allowlist 동일성 |
| **Rollback** | 개별 template 파일 제거 |

```text
define allowed placeholders per role
include live preamble IDs as lifecycle authority
require output only under STEP_OUTPUT_DIR
require every delivered unresolved finding decision
include approval obligation and escalation codes
inject ALLOWED_TEST_COMMANDS, TEST_POLICY_DIGEST, APPROVED_TEST_KINDS
inject ALLOWED_TEST_OUTPUT_PATHS into planner and plan reviewer
require planner to select commands instead of inventing argv
rendering is forbidden until all placeholders supplied
```

### M-B05-02 — Minimal Scope Package Renderer

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B05-02` |
| **Parent Block** | `B-05` |
| **Name** | Minimal role context renderer |
| **Rationale** | resolved discussion을 제외하고 필요한 template context만 전달 |
| **Objective** | unresolved closure와 coordinator-owned policy/context를 정확히 렌더링 |
| **Target Files** | `orca_loop/roles.py` |
| **Preconditions** | `M-B05-01`, `M-B07-03` interface |
| **Input Type** | `RoleContext`, `ScopePackage`, `Path template_path` |
| **Input Validation** | RESOLVED ID 미포함, path/digest 존재 |
| **Output Type** | `RenderedContract(text: str, digest: str)` |
| **Output Validation** | no unresolved placeholder, max `256 KiB` |
| **Exceptions** | `TemplateContractError` |
| **Side Effects** | 없음; caller가 staging |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B05-04` resolved 제외, `T-B05-05` closure 포함, `T-B05-06` 모든 RoleContext placeholder, `T-B05-07` policy digest drift 거부 |
| **Rollback** | 없음 |

```text
validate scope package
render exact template placeholders
hash rendered UTF-8 bytes
return RenderedContract
```

### M-B06-01 — Input Staging and Integrity Manifest

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B06-01` |
| **Parent Block** | `B-06` |
| **Name** | Pre-dispatch input staging |
| **Rationale** | worker가 contract 생성 전에 시작되는 race를 차단 |
| **Objective** | dispatch 전에 모든 input과 digest manifest가 완성 |
| **Target Files** | `orca_loop/transport.py` |
| **Preconditions** | `M-B01-03`, step가 미dispatch 상태 |
| **Input Type** | `StepWorkspace`, `tuple[StagedInput, ...]` where `StagedInput(name: str, source: Path | bytes)` |
| **Input Validation** | basename only, duplicate 금지, source 존재, input total `<=10 MiB` |
| **Output Type** | `InputManifest(entries: tuple[DigestEntry, ...], manifest_digest: str)` |
| **Output Validation** | 모든 staged file digest 재계산 일치 |
| **Exceptions** | `InputStagingError`, `PathBoundaryError` |
| **Side Effects** | `steps/<step_id>/in/*` atomic write |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B06-01` atomic staging, `T-B06-02` duplicate/traversal, `T-B06-03` tamper 탐지 |
| **Rollback** | incomplete temp file만 삭제; prior canonical input 유지 |

```text
reject if task_id or dispatch_id already bound
validate all input names before writing
write each input to temporary sibling and atomic replace
calculate sorted SHA-256 entries
write inputs.sha256 last
verify manifest immediately
return InputManifest
```

### M-B06-02 — Outbox Verification and Promotion

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B06-02` |
| **Parent Block** | `B-06` |
| **Name** | Provenance-bound artifact promotion |
| **Rationale** | payload 본문 없이 대용량 artifact를 안전하게 수용 |
| **Objective** | active step output만 digest/schema 검증 후 canonical 승격 |
| **Target Files** | `orca_loop/transport.py` |
| **Preconditions** | `M-B03-02`, `M-B06-01`; typed `DispatchHandle` fixture 사용 가능 |
| **Input Type** | `WorkerDonePayload`, `DispatchHandle`, `StepWorkspace`, artifact parser callable |
| **Input Validation** | task/dispatch exact match, report path output dir 내부, symlink 금지 |
| **Output Type** | `PromotedArtifact(path: Path, digest: str, parsed: ArtifactType)` |
| **Output Validation** | input manifest 재검증 후 destination digest 일치 |
| **Exceptions** | `ProvenanceError`, `ContractViolationError`, `ScopeViolationError` |
| **Side Effects** | output artifact를 `artifacts/`에 atomic replace |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B06-04` 100 KiB, `T-B06-05` escape/symlink, `T-B06-06` digest mismatch |
| **Rollback** | destination replace 전 실패 시 canonical 불변 |

```text
verify payload task and dispatch against active handle
resolve report path and require output-dir containment
verify output digest
read and parse artifact
verify staged inputs remain unchanged
write canonical temporary file and atomic replace
return typed promoted artifact
```

### M-B07-01 — Finding Lifecycle and Side Decisions

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B07-01` |
| **Parent Block** | `B-07` |
| **Name** | Immutable per-finding update |
| **Rationale** | 한쪽 승인 또는 빈 목록으로 finding이 사라지는 것을 차단 |
| **Objective** | role별 decision으로 새 ledger를 생성 |
| **Target Files** | `orca_loop/ledger.py` |
| **Preconditions** | `M-B03-02` |
| **Input Type** | `ConsensusLedger`, typed artifact, `Side`, current version/digest |
| **Input Validation** | delivered IDs complete, ID collision/reopen 규칙 |
| **Output Type** | `LedgerUpdate(ledger: ConsensusLedger, escalations: tuple[EscalationTrigger, ...])` |
| **Output Validation** | input ledger 불변, dual approval 전 RESOLVED 없음 |
| **Exceptions** | `LedgerIntegrityError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B07-01` single-side 미해결, `T-B07-02` dual approval, `T-B07-03` empty findings 유지 |
| **Rollback** | 이전 immutable ledger 재사용 |

```text
copy ledger records
register new findings after collision checks
apply every required side decision to copied record
compute status from both current-version decisions
append E-06 for valid reopen
return new LedgerUpdate
```

### M-B07-02 — Round, Signature, Material Progress and Escalation

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B07-02` |
| **Parent Block** | `B-07` |
| **Name** | Deterministic valid-round commit |
| **Rationale** | round 중복 계수와 `E-05` 회피를 차단 |
| **Objective** | EVALUATE에서만 최대 1 증가하고 2회 동일 무진전 문제를 탐지 |
| **Target Files** | `orca_loop/ledger.py` |
| **Preconditions** | `M-B07-01` |
| **Input Type** | `ConsensusLedger`, `ConsensusKind`, `RoundEvidence` |
| **Input Validation** | 같은 version/digest, 양측 artifact valid, changed-during-round false |
| **Output Type** | `LedgerUpdate` |
| **Output Validation** | round delta `0|1`, plan/code 각각 injected `LoopConfig` limit 이하 |
| **Exceptions** | `InvalidRoundError`, `LedgerIntegrityError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B07-04` one-cycle one-count, `T-B07-05` retry 미계수, `T-B07-06` E-05 정확히 2회째 |
| **Rollback** | 이전 ledger 재사용 |

```text
if evidence invalid return unchanged ledger with committed=false
increment selected round in copied ledger
for each unresolved record compute normalized signature
compute material_progress from Phase 1 deterministic predicate
append observation
if last two observations have same signature and no progress append E-05
detect E-01, E-02, E-04..E-08; E-03은 PlanDocument까지 보는 M-B12-03 owner
return copied ledger and deduplicated escalations
```

### M-B07-03 — Unresolved Dependency Closure

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B07-03` |
| **Parent Block** | `B-07` |
| **Name** | Minimal next-round scope |
| **Rationale** | 합의 완료 내용의 재전송과 token 낭비를 방지 |
| **Objective** | unresolved finding과 명시적 relation closure만 반환 |
| **Target Files** | `orca_loop/ledger.py` |
| **Preconditions** | `M-B07-02`, valid ledger |
| **Input Type** | `ConsensusLedger` |
| **Input Validation** | dependency ID 존재, cycle 허용하되 방문 집합 사용 |
| **Output Type** | `ScopePackage` with exact finding/AC/file/test/conflict tuples |
| **Output Validation** | RESOLVED/INFORMATIONAL 제외, closure idempotent |
| **Exceptions** | `LedgerIntegrityError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B07-07` transitive closure, `T-B07-08` cycle termination, `T-B07-09` resolved 제외 |
| **Rollback** | 없음 |

```text
seed queue with OPEN, CHANGE_REQUIRED, VERIFY_REQUIRED IDs
walk depends_on with visited set
collect stable-sorted records
union exact acceptance criteria, affected files, test IDs
include only last-round conflict excerpts
return immutable ScopePackage
```

### M-B08-01 — Pure Transition Table

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B08-01` |
| **Parent Block** | `B-08` |
| **Name** | Total state/signal transition |
| **Rationale** | agent 자유 판단 없이 coordinator 경로를 결정 |
| **Objective** | 모든 valid `(state, signal)` 조합과 invalid rejection 정의 |
| **Target Files** | `orca_loop/machine.py` |
| **Preconditions** | `M-B03-01`, `M-B07-02` |
| **Input Type** | `LoopState`, `TransitionSignal`, `LedgerView`, `LoopCounters` |
| **Input Validation** | terminal state 입력 거부, counter nonnegative |
| **Output Type** | `TransitionResult` |
| **Output Validation** | input objects 불변, 정의된 next state |
| **Exceptions** | `UndefinedTransitionError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B08-01` table parameterization, `T-B08-02` escalation 우선, `T-B08-03` reject exit 경로 |
| **Rollback** | 없음 |

```text
if signal is escalation return USER_DECISION_REQUIRED
if signal is abort return FAILED
lookup exact rule
apply only non-ledger counters
read ledger rounds for limit checks without mutation
construct TransitionResult
```

### M-B08-02 — Termination and Counter Invariants

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B08-02` |
| **Parent Block** | `B-08` |
| **Name** | Finite-loop proof tests |
| **Rationale** | 무한 수정 루프와 자동 통과를 차단 |
| **Objective** | 10,000 deterministic signal sequence가 terminal state 도달 |
| **Target Files** | `tests/test_machine.py` |
| **Preconditions** | `M-B08-01` |
| **Input Type** | generated valid signal sequences with seed `0..9999` |
| **Input Validation** | state별 valid signal 집합만 생성 |
| **Output Type** | `unittest` assertions |
| **Output Validation** | max 128 transition 이내 terminal, unresolved auto-approve 없음 |
| **Exceptions** | `AssertionError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | 자체 block가 `T-B08-04` |
| **Rollback** | test 파일 제거 |

```text
for each seed construct deterministic generator
start INIT with round limits 5/5
apply only valid signals
commit ledger round only at evaluate state
assert terminal within bound
assert unresolved never reaches READY_FOR_MERGE
```

### M-B09-01 — Coordinator-Owned Test Policy

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B09-01` |
| **Parent Block** | `B-09` |
| **Name** | Exact test command authorization |
| **Rationale** | LLM `kind`와 denylist만으로 arbitrary execution을 막을 수 없다 |
| **Objective** | `--test-policy` JSON의 exact `(argv,cwd,timeout_ms,kind)` entry와 일치할 때만 승인 |
| **Target Files** | `orca_loop/testrunner.py`, `orca_loop/config.py` |
| **Preconditions** | `M-B03-02` |
| **Input Type** | `tuple[TestCommand, ...]`, `TestExecutionPolicy`, `Path worktree` |
| **Input Validation** | exact equality, cwd containment, timeout 상한, approved kind |
| **Output Type** | `PolicyValidation(approved: bool, violations: tuple[TestPolicyViolation, ...])` |
| **Output Validation** | allowlist 외 command 0개 실행 |
| **Exceptions** | malformed policy는 `TestPolicyError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B09-01` exact match, `T-B09-02` appended arg 거부, `T-B09-03` interpreter 우회 거부, `T-B09-04` canonical policy digest |
| **Rollback** | 없음 |

```text
load optional policy from --test-policy before workers start
if path absent construct an empty policy and preserve TEST_GATE=NOT_RUN
parse exact commands, env keys, output paths and approved kinds
canonical JSON = UTF-8 JSON with sorted keys, separators "," and ":", ensure_ascii=false
policy_digest = "sha256:" + hex(SHA-256(canonical JSON bytes))
for each plan command require equality with one policy command
reject absolute cwd, traversal, timeout expansion and unknown kind
return all violations without executing a process
```

### M-B09-02 — Sanitized Test Execution and Delta Guard

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B09-02` |
| **Parent Block** | `B-09` |
| **Name** | Bounded test process execution |
| **Rationale** | parent secret 상속과 test source mutation을 차단·탐지 |
| **Objective** | approved command만 sanitized env에서 실행하고 tracked source delta가 없음을 검증 |
| **Target Files** | `orca_loop/testrunner.py` |
| **Preconditions** | `M-B09-01`, `M-B04-01` |
| **Input Type** | approved `tuple[TestCommand, ...]`, `TestExecutionPolicy`, `Path worktree` |
| **Input Validation** | policy approval true |
| **Output Type** | `TestGateResult` |
| **Output Validation** | exit status와 delta guard 일치, stdout/stderr tail 각각 `<=32 KiB` |
| **Exceptions** | spawn 실패는 `TestExecutionError`; timeout은 FAIL result |
| **Side Effects** | approved test process 및 allowed output paths |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B09-05` env secret 제거, `T-B09-06` timeout tree kill, `T-B09-07` source mutation 탐지, `T-B09-08` attribution |
| **Rollback** | test output 자동 삭제 안 함; 변경 목록 보고, source 자동 restore 금지 |

```text
capture before snapshot
construct env from fixed OS minimum plus policy allowed keys
for each approved command run shell=False with process-group isolation
on timeout terminate complete process group
capture bounded output and return code
capture after snapshot
reject tracked/staged/deleted delta caused during test
permit only policy-declared untracked output paths
derive attribution = none, implementation, environment or ambiguous from result and delta evidence
return PASS, FAIL, NOT_RUN or POLICY_VIOLATION with attribution
```

### M-B10-01 — Worker Provisioning

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B10-01` |
| **Parent Block** | `B-10` |
| **Name** | Four isolated worker terminal provisioning |
| **Rationale** | Claude planner가 작성 의도를 code review session context로 공유하지 않도록 역할 세션을 분리 |
| **Objective** | Claude planner, Claude code reviewer, Codex implementer, Codex reviewer terminal이 `tui-idle` |
| **Target Files** | `orca_loop/dispatcher.py` |
| **Preconditions** | `M-B02-01`, `M-B02-02`, runtime ready |
| **Input Type** | `str worktree_selector`, `dict[WorkerKey, LaunchProfile]` |
| **Input Validation** | selector 명시, coordinator handle과 worker handle 중복 금지 |
| **Output Type** | `WorkerPool` with 4 concrete handles |
| **Output Validation** | terminal list에서 동일 worktree identity 확인 |
| **Exceptions** | `WorkerProvisionError`, `WorkerLostError` |
| **Side Effects** | Orca terminal 4개 생성 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B10-01` fake terminal responses, `T-B10-02` stale handle replacement |
| **Rollback** | 이 run이 만든 terminal ID만 사용자 요청 시 close; 자동 close 기본값 false |

```text
create one terminal per WorkerKey in requested worktree
assert CLAUDE_PLANNER handle differs from CLAUDE_CODE_REVIEW
read startup handles
wait tui-idle with 60-second bounded window
verify worktree identity for each handle
return WorkerPool
```

### M-B10-02 — Prepared Task Creation and Durable Binding

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B10-02` |
| **Parent Block** | `B-10` |
| **Name** | Race-free task preparation |
| **Rationale** | input staging 후 dispatch하고 crash window를 단계별로 복구 |
| **Objective** | `STEP_PREPARED`와 `TASK_CREATED`가 dispatch 전에 durable commit |
| **Target Files** | `orca_loop/dispatcher.py`, `orca_loop/coordinator.py` |
| **Preconditions** | `M-B06-02`, `M-B12-01` generation store interface |
| **Input Type** | `StepWorkspace`, `RenderedContract`, `WorkerHandle`, current state/ledger |
| **Input Validation** | input manifest valid, output empty, no prior task binding |
| **Output Type** | `PreparedTask(step_id: str, task_id: str, worker_handle: str)` |
| **Output Validation** | task spec가 existing contract path와 run/step ID 포함 |
| **Exceptions** | `OrcaCommandError`, `StepBindingError` |
| **Side Effects** | Orca task 생성, two durable generation commits |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B10-03` contract 존재 전 dispatch 없음, `T-B10-04` task-created crash resume |
| **Rollback** | task를 삭제/reset하지 않음; unused task ID를 failure report에 기록 |

```text
stage and verify all inputs
commit STEP_PREPARED with step_id and input manifest digest
create Orca task whose spec references existing contract path
extract task_id
commit TASK_CREATED with task_id
return PreparedTask
```

### M-B10-03 — Dispatch, Wait and Provenance

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B10-03` |
| **Parent Block** | `B-10` |
| **Name** | Injected dispatch lifecycle |
| **Rationale** | active task/dispatch만 completion authority로 인정 |
| **Objective** | dispatch 후 즉시 durable binding하고 rolling wait로 한 completion 반환 |
| **Target Files** | `orca_loop/dispatcher.py` |
| **Preconditions** | `M-B10-01`, `M-B10-02`, terminal `tui-idle` |
| **Input Type** | `PreparedTask`, `str coordinator_handle`, `int step_timeout_ms` |
| **Input Validation** | task status pending/ready, concrete worker handle |
| **Output Type** | `tuple[DispatchHandle, Completion]` |
| **Output Validation** | lifecycle payload task/dispatch exact match |
| **Exceptions** | `WorkerLostError`, `DispatchTimeoutError`, `ProvenanceError` |
| **Side Effects** | Orca dispatch, messages consume, durable `STEP_DISPATCHED` commit |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B10-05` foreign message 무시, `T-B10-06` timeout checkpoint, `T-B10-07` native escalation |
| **Rollback** | active dispatch를 자동 취소하지 않음; resume 대상으로 유지 |

```text
dispatch task with --inject to concrete worker
extract dispatch_id and terminal identity
commit STEP_DISPATCHED immediately
loop check --wait with bounded windows
ignore foreign lifecycle messages after logging
on worker_done or escalation verify both IDs
return handle and typed Completion
```

### M-B11-01 — Path Normalization and Step Delta

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B11-01` |
| **Parent Block** | `B-11` |
| **Name** | Repository-relative change guard |
| **Rationale** | read-only 역할 mutation과 implementer scope 이탈 탐지 |
| **Objective** | before/after digest delta를 normalized path로 분류 |
| **Target Files** | `orca_loop/guards.py` |
| **Preconditions** | `M-B04-01` |
| **Input Type** | two `SnapshotIdentity`, `Role`, `tuple[AffectedFile, ...]`, `DestructiveApproval | None` |
| **Input Validation** | absolute/traversal/symlink path 거부 |
| **Output Type** | `GuardReport(ok: bool, violations: tuple[Violation, ...])` |
| **Output Validation** | read-only delta empty, implement delta가 approved operation subset, 무단 deletion/rename zero |
| **Exceptions** | invalid guard input은 `PathBoundaryError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B11-01` prefix bypass, `T-B11-02` unplanned deletion, `T-B11-03` approved file delete, `T-B11-04` rename source/target, `T-B11-05` directory delete approval |
| **Rollback** | 자동 restore 금지 |

```text
normalize every repo path by segments
compare before and after file digest maps
collect changed, added, deleted
for read-only role reject any delta
for implementer require every delta to match approved path and operation
for delete or rename require explicit destructive approval evidence
always reject unplanned, directory-wide or large deletion without separate gate
return GuardReport
```

### M-B11-02 — Step Sandbox and Test Output Guard

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B11-02` |
| **Parent Block** | `B-11` |
| **Name** | Input/outbox/test side-effect guard |
| **Rationale** | launch permission 실패를 postcondition으로 검출 |
| **Objective** | input immutable, report output-contained, test output allowlisted |
| **Target Files** | `orca_loop/guards.py` |
| **Preconditions** | `M-B06-01`, `M-B09-02`, `M-B11-01` |
| **Input Type** | `StepWorkspace`, `InputManifest`, optional `TestExecutionPolicy`, delta |
| **Input Validation** | manifest digest valid |
| **Output Type** | `GuardReport` |
| **Output Validation** | violation type가 `input_tampered|outbox_escape|test_output_scope` 중 하나 |
| **Exceptions** | `ScopeViolationError` |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B11-06` input tamper, `T-B11-07` other step output, `T-B11-08` test output |
| **Rollback** | 자동 restore 금지 |

```text
recompute input manifest
verify every artifact path is under current output directory
if test delta exists require every allowed untracked path match policy
return all violations without mutating filesystem
```

### M-B12-01 — Atomic Generation Store

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B12-01` |
| **Parent Block** | `B-12` |
| **Name** | State/ledger generation transaction |
| **Rationale** | 두 JSON 파일의 cross-file commit point 필요 |
| **Objective** | manifest가 가리키는 generation만 committed로 간주 |
| **Target Files** | `orca_loop/generation.py` |
| **Preconditions** | `M-B01-03`, `M-B03-02`, typed serializable state/ledger |
| **Input Type** | `CoordinatorState`, `ConsensusLedger`, `StepStage` |
| **Input Validation** | generation exactly current+1, same run ID |
| **Output Type** | `CommitManifest(generation: int, state_digest: str, ledger_digest: str)` |
| **Output Validation** | reread digest 일치 |
| **Exceptions** | `GenerationMismatchError`, `AtomicWriteError` |
| **Side Effects** | state/ledger/commit JSON atomic replace |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B12-01` crash before manifest, `T-B12-02` digest mismatch, `T-B12-03` monotonic generation |
| **Rollback** | last valid manifest generation 로드; 파일 자동 삭제 없음 |

```text
serialize state and ledger deterministically
write and fsync generation temporary files
atomic replace state generation then ledger generation
compute both digests
write and fsync manifest temporary
atomic replace commit manifest last
return manifest
```

### M-B12-02 — Coordinator Step Execution with Ledger Propagation

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B12-02` |
| **Parent Block** | `B-12` |
| **Name** | Single side-effect step |
| **Rationale** | stale ledger가 다음 generation을 덮어쓰는 결함 방지 |
| **Objective** | every step가 `StepExecutionResult`로 정확한 updated ledger 반환 |
| **Target Files** | `orca_loop/coordinator.py` |
| **Preconditions** | `M-B06-02`, `M-B10-03`, `M-B11-02`, `M-B12-01`, `M-B12-05`, `M-B13-02` |
| **Input Type** | `LoopState`, `LoopCounters`, `ConsensusLedger` |
| **Input Validation** | nonterminal state, generation loaded |
| **Output Type** | `StepExecutionResult` |
| **Output Validation** | returned ledger generation content가 ARTIFACT_VERIFIED commit과 동일; CompletionKind 4종 모두 명시 분기 |
| **Exceptions** | typed `OrcaLoopError` subclasses |
| **Side Effects** | step workspace, task/dispatch, artifact, generation commits |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B12-04` two-step finding persistence, `T-B12-05` stale ledger overwrite 방지, `T-B12-06` completion 4분기 |
| **Rollback** | generation store의 last committed manifest로 resume |

```text
prepare step workspace and inputs
create task and durable task binding
dispatch, durable dispatch binding, wait completion
if completion is STEP_TIMEOUT:
  return ABORT without artifact promotion
if completion is ESCALATION:
  normalize native escalation and return ESCALATE
if completion is DECISION_GATE:
  route authorized question or return USER_DECISION_REQUIRED
if completion is WORKER_DONE:
  verify provenance, promote and guard artifact
on parser/promotion contract failure invoke M-B12-05 operational retry
updated = ledger.dispatch_handler(state)(artifact)
commit ARTIFACT_VERIFIED using updated.ledger
return StepExecutionResult(signal, updated.ledger, unchanged test status)
```

Coordinator loop는 반드시 다음 assignment를 수행한다.

```text
step_result = execute_state(state, counters, ledger)
ledger = step_result.ledger
transition = machine.transition(state, step_result.signal, ledger.view(), counters)
state = transition.next_state
counters = transition.counters_after
commit TRANSITION_COMMITTED with this same ledger
```

### M-B12-03 — Non-Worker State Execution

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B12-03` |
| **Parent Block** | `B-12` |
| **Name** | Evaluate, test and human gate states |
| **Rationale** | round commit, test status, human/destructive gate를 worker step과 분리 |
| **Objective** | EVALUATE/TEST_GATE/HUMAN_GATE를 전부 소유하고 updated ledger와 정확한 signal 반환 |
| **Target Files** | `orca_loop/coordinator.py` |
| **Preconditions** | `M-B07-02`, `M-B09-02`, `M-B12-02`, `M-B13-02`, `M-B13-03` |
| **Input Type** | evaluate state, TEST_GATE or HUMAN_GATE, current ledger/config |
| **Input Validation** | state별 required artifacts present |
| **Output Type** | `StepExecutionResult` |
| **Output Validation** | evaluate round delta `0|1`; FAIL은 CODE_REVIEW로 전달 안 됨; HUMAN_GATE resolution provenance 보존 |
| **Exceptions** | `InvalidRoundError`, `TestExecutionError` |
| **Side Effects** | test 실행 또는 generation commit |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B12-07` evaluate ledger 반환, `T-B12-08` NOT_RUN review 진행, `T-B12-09` FAIL→FIX, `T-B12-10` HUMAN_GATE 4분기, `T-B12-11` E-03 pre-implement gate |
| **Rollback** | prior generation |

```text
if evaluate:
  updated = ledger.commit_round(kind, evidence)
  if plan consensus reached and data_api_schema_changes != "없음":
    create E-03 trigger unless its deduplication key is in approved_escalation_keys
  if plan consensus reached and plan contains delete or rename:
    require M-B13-03 DestructiveApproval before IMPLEMENT
  choose escalation, resolved-zero or unresolved signal from updated.ledger
  return StepExecutionResult(signal, updated.ledger, test status)
if TEST_GATE:
  run exact-policy tests
  map PASS to CODE_REVIEW
  map NOT_RUN to CODE_REVIEW with persistent warning
  map FAIL to FIX
  map POLICY_VIOLATION to USER_DECISION_REQUIRED
  return unchanged ledger
if HUMAN_GATE:
  load or create FINAL GateBinding through M-B13-02
  validate HumanDecision and report digest
  return merge, reject, revise_code or revise_design with unchanged ledger
```

### M-B12-04 — Resume Reconciliation

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B12-04` |
| **Parent Block** | `B-12` |
| **Name** | Crash-safe runtime reconciliation |
| **Rationale** | task create/dispatch 각 crash boundary에서 중복 worker 실행 방지 |
| **Objective** | durable stage와 live Orca state를 대조해 단일 next action 결정 |
| **Target Files** | `orca_loop/coordinator.py` |
| **Preconditions** | `M-B12-03`, Orca runtime reachable |
| **Input Type** | `RunWorkspace`, committed state/ledger, live task/dispatch/terminal lists |
| **Input Validation** | digest and run ID match |
| **Output Type** | `ResumeDecision(action: ResumeAction, state, ledger, active)` |
| **Output Validation** | ambiguity는 항상 USER_DECISION_REQUIRED |
| **Exceptions** | malformed local state는 `GenerationMismatchError` |
| **Side Effects** | read-only Orca queries; selected action later executes |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B12-12` five crash stages, `T-B12-13` foreign task, `T-B12-14` completed artifact |
| **Rollback** | previous committed generation |

```text
load manifest-selected state and ledger
query task-list, dispatch-show and terminal list
switch durable StepStage
STEP_PREPARED: create task
TASK_CREATED: dispatch existing task unless live dispatch already exists
STEP_DISPATCHED: wait existing dispatch
WORKER_DONE_RECEIVED: promote existing output
ARTIFACT_VERIFIED: transition using committed ledger
if more than one valid interpretation return USER_DECISION_REQUIRED
```

### M-B12-05 — Operational Retry and Contract Reminder

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B12-05` |
| **Parent Block** | `B-12` |
| **Name** | Round-neutral contract retry |
| **Rationale** | malformed artifact와 승인 의무 누락을 합의 round로 잘못 계수하지 않으면서 무한 재요청을 차단 |
| **Objective** | 계약 위반 1회는 reminder와 함께 동일 state를 재dispatch하고 반복 위반은 정확한 terminal signal로 전환 |
| **Target Files** | `orca_loop/coordinator.py`, `orca_loop/contracts.py` |
| **Preconditions** | `M-B03-02`, `M-B10-03`, `M-B12-01`, `M-B13-02` |
| **Input Type** | `CoordinatorState`, `ConsensusLedger`, `LoopCounters`, `ContractViolationError`, failed `DispatchHandle` |
| **Input Validation** | active run/task/dispatch provenance, `operational_retries < configured limit` 판정 |
| **Output Type** | `StepExecutionResult` |
| **Output Validation** | retry 시 ledger round 불변, `operational_retries`만 1 증가, 새 dispatch provenance 저장 |
| **Exceptions** | malformed provenance는 `ProvenanceError`; redispatch 실패는 `WorkerLostError` |
| **Side Effects** | reminder contract staging, 새 task/dispatch, generation commit |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B12-15` retry round 미소비, `T-B12-16` second malformed payload→A-01, `T-B12-17` repeated approval violation→U-06 |
| **Rollback** | 이전 dispatch를 취소/reset하지 않고 failed provenance를 history에 보존 |

```text
classify contract violation
if operational retry budget remains:
  increment only LoopCounters.operational_retries
  stage bounded reminder naming exact violated contract
  create and dispatch a new task through existing provenance flow
  return OPERATIONAL_RETRY with unchanged ledger rounds
if reason is approval_obligation or missing_finding_decision:
  create escalation report through M-B13-02 and return ESCALATE for U-06
return ABORT for A-01
```

### M-B13-01 — User Decision Report

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B13-01` |
| **Parent Block** | `B-13` |
| **Name** | Evidence-bound escalation report |
| **Rationale** | 사용자가 미합의 내용을 빠르게 결정할 수 있어야 한다 |
| **Objective** | Phase 1 §17의 12항목과 unresolved finding별 options 생성 |
| **Target Files** | `orca_loop/escalation.py` |
| **Preconditions** | `M-B03-02`, `M-B04-01`, `M-B07-03`, ledger와 snapshot provenance valid |
| **Input Type** | `ConsensusLedger`, triggers, `CoordinatorState`, test status |
| **Input Validation** | resolved discussion 전문 제외, evidence path 존재 |
| **Output Type** | `DecisionReport(path: Path, digest: str, finding_ids: tuple[str, ...])` |
| **Output Validation** | 모든 unresolved ID, 양측 입장, 공통점, 차이, options 포함 |
| **Exceptions** | `DecisionReportError` |
| **Side Effects** | `user-decision.md` atomic write |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B13-01` unresolved completeness, `T-B13-02` resolved summary only |
| **Rollback** | previous report를 generation artifact로 보존 |

```text
collect unresolved closure
render source request and current provenance
render each side decision and evidence
derive options only from recorded proposals
if no evidence-backed option state required information instead
write atomic report and return digest
```

### M-B13-02 — Gate Routing and Resume

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B13-02` |
| **Parent Block** | `B-13` |
| **Name** | Human decision gate lifecycle |
| **Rationale** | gate 생성 후 terminal state에서 결정이 유실되지 않아야 한다 |
| **Objective** | final gate와 escalation gate를 구분하고 resolution을 resume signal로 변환 |
| **Target Files** | `orca_loop/escalation.py` |
| **Preconditions** | `M-B02-01`, `M-B03-02`, `M-B07-03`, `M-B13-01`, active or dedicated decision task ID |
| **Input Type** | `DecisionReport`, `GateKind`, `OrcaClient`, optional `HumanDecision` |
| **Input Validation** | option enum이 gate kind와 일치; revise는 nonempty note와 affected AC/finding 중 하나 이상 필수 |
| **Output Type** | `GateBinding(gate_id, task_id, report_digest)`, validated `HumanDecision`, optional `TransitionSignal` |
| **Output Validation** | caller가 generation commit에 기록할 완전한 immutable binding/decision |
| **Exceptions** | `GateProtocolError`, unresolved timeout은 terminal BLOCKED |
| **Side Effects** | Orca gate 생성/조회; generation commit은 coordinator 소유 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B13-03` merge/reject/revise 분기, `T-B13-04` escalation resume, `T-B13-05` stale report digest, `T-B13-06` revise note 누락 거부, `T-B13-07` unresolved 0에서 user scope 생성 |
| **Rollback** | gate 자동 삭제 없음 |

```text
create dedicated decision task when no active suitable task exists
create gate with report path, digest and bounded options
return GateBinding to coordinator; coordinator persists it before waiting or exiting
on resume list gate by task and require one resolved gate
verify report digest
parse resolution as HumanDecision
if revise: require decision_note and at least one affected AC or finding ID
create user-directed scope when unresolved finding count is zero
map decision to merge, reject, revise_code, revise_design or stop
return validated decision, report digest and signal; coordinator records them
```

### M-B13-03 — Destructive Operation Approval Gate

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B13-03` |
| **Parent Block** | `B-13` |
| **Name** | Plan-bound delete and rename approval |
| **Rationale** | `M-B04-02`와 `M-B11-01`이 소비하는 `DestructiveApproval`을 승인된 계획과 사용자 결정으로 생성해야 한다 |
| **Objective** | 계획에 명시된 delete/rename만 별도 `DESTRUCTIVE` gate로 제시하고 승인 결과를 snapshot-bound artifact로 저장 |
| **Target Files** | `orca_loop/escalation.py`, `orca_loop/models.py` |
| **Preconditions** | `M-B04-02`, `M-B13-02`, approved `PlanDocument` |
| **Input Type** | `PlanDocument`, `ScopeManifest`, `SnapshotIdentity`, decision task ID |
| **Input Validation** | delete/rename 1개 이상, directory/large delete 표시, plan version/snapshot/report digest 일치 |
| **Output Type** | `GateBinding`, `DestructiveApproval | None`, `TransitionSignal` |
| **Output Validation** | 승인 operation이 계획의 delete/rename exact subset이고 decision digest가 gate resolution과 일치 |
| **Exceptions** | `GateProtocolError`, stale plan/snapshot은 `ProvenanceError` |
| **Side Effects** | Orca destructive gate 생성/조회; approval persistence와 generation commit은 coordinator 소유 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B13-08` no destructive op→gate 없음, `T-B13-09` exact approval, `T-B13-10` stale plan 거부, `T-B13-11` directory delete 별도 표시 |
| **Rollback** | gate와 approval history를 보존하고 source 변경은 수행하지 않음 |

```text
extract planned delete and rename operations
if none return no approval and OK
render exact paths, operations, plan version, snapshot and risk summary
create GateKind.DESTRUCTIVE and persist GateBinding
on approval construct DestructiveApproval bound to gate and decision digest
on rejection or timeout return USER_DECISION_REQUIRED without dispatching implementer
```

### M-B14-01 — CLI Configuration and Preflight

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B14-01` |
| **Parent Block** | `B-14` |
| **Name** | Typed arguments and environment preflight |
| **Rationale** | worker 생성 전 모든 결정 가능한 실패를 차단 |
| **Objective** | `LoopConfig` 생성, versions/paths/handles/policy 검증 |
| **Target Files** | `orca_loop/config.py`, `run_loop.py` |
| **Preconditions** | `M-B01-03`, Python `>=3.11` |
| **Input Type** | `Sequence[str] argv`, process environment |
| **Input Validation** | round limits `1..5` default `5`, explicit coordinator handle, timeout ranges, optional `--test-policy` readable JSON path |
| **Output Type** | `LoopConfig` |
| **Output Validation** | target worktree clean, HEAD valid, canonical policy digest computed, Orca `1.4.159` unless drift approved |
| **Exceptions** | `PreflightError` mapped exit `2` |
| **Side Effects** | read-only commands only |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B14-01` parser defaults, `T-B14-02` dirty worktree, `T-B14-03` version drift, `T-B14-04` test policy path/digest |
| **Rollback** | 불필요 |

`LoopConfig`의 합의 관련 field는 다음으로 고정한다.

```python
plan_consensus_round_limit: int = 5
code_consensus_round_limit: int = 5
max_transition_count: int = 128
```

계획·구현 합의는 각각 **최대 5개 유효 round**이며, 합의가 먼저 이루어지거나
`E-05`가 발화하면 5회를 모두 소진하지 않고 종료한다.

```text
parse argv
resolve all paths
resolve optional --test-policy path without treating test commands themselves as CLI input
validate Python and Orca versions
validate coordinator handle and target worktree
validate clean Git state and HEAD
load exact test policy
return LoopConfig before creating any worker
```

### M-B14-02 — Run Lock and Main Exit Contract

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B14-02` |
| **Parent Block** | `B-14` |
| **Name** | Single-run entry point |
| **Rationale** | 같은 worktree에 coordinator 두 개가 실행되면 provenance가 깨진다 |
| **Objective** | exclusive lock 후 coordinator 실행, exit `0..4` 정확히 반환 |
| **Target Files** | `run_loop.py`, `orca_loop/locking.py` |
| **Preconditions** | `M-B14-01`, `M-B12-04`, `M-B13-02` |
| **Input Type** | `LoopConfig` |
| **Input Validation** | lock owner PID/start time/run ID schema |
| **Output Type** | process exit code |
| **Output Validation** | READY `0`, runtime failure `1`, preflight `2`, user required `3`, rejected `4` |
| **Exceptions** | top boundary catches only `OrcaLoopError`, KeyboardInterrupt maps `1` |
| **Side Effects** | lock, logs, coordinator execution |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | `T-B14-05` second lock, `T-B14-06` exit mapping, `T-B14-07` dry run no Orca mutation |
| **Rollback** | owned lock만 finally에서 해제 |

```text
preflight and acquire exclusive lock
construct coordinator
run or resume
map final state to exact exit code
write final report
release only lock whose token matches this process
```

### M-B15-01 — Pure Unit Test Suite

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B15-01` |
| **Parent Block** | `B-15` |
| **Name** | Contract/ledger/machine unit tests |
| **Rationale** | 핵심 판정 로직은 Orca 없이 결정론적으로 검증 가능 |
| **Objective** | contracts, snapshot, ledger, machine, policy, guards unit suite 통과 |
| **Target Files** | `tests/test_contracts.py`, `tests/test_snapshot.py`, `tests/test_ledger.py`, `tests/test_machine.py`, `tests/test_testrunner.py`, `tests/test_guards.py`, `tests/test_plan_traceability.py` |
| **Preconditions** | `M-B03-02`, `M-B04-02`, `M-B07-03`, `M-B08-02`, `M-B09-02`, `M-B11-02`, `M-B14-02`, Phase 3 Revision 3 file |
| **Input Type** | fixtures and temporary Git repositories |
| **Input Validation** | deterministic fixtures |
| **Output Type** | `unittest` result |
| **Output Validation** | failures/errors/skips `0` unless platform-specific live test 제외 |
| **Exceptions** | test assertion |
| **Side Effects** | temporary directory only |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | 해당 test files 자체; `T-B15-01` `Preconditions` edge parsing과 topological order |
| **Rollback** | temp cleanup |

```text
build fixture factories
test every documented validation ID
use subTest for transition table
assert input models remain immutable
assert no real Orca command is called
parse every Micro Block Preconditions into the canonical DAG
reject cycles, undefined IDs and any Phase 4 order that places a dependency later
```

### M-B15-02 — Fake Orca Integration and Crash Tests

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B15-02` |
| **Parent Block** | `B-15` |
| **Name** | Coordinator integration tests |
| **Rationale** | lifecycle와 crash 복구를 real runtime mutation 없이 검증 |
| **Objective** | fake client로 full success, revise, escalation, 5 crash stage 실행 |
| **Target Files** | `tests/fakes.py`, `tests/test_orca_client.py`, `tests/test_dispatcher.py`, `tests/test_coordinator.py`, `tests/test_escalation.py`, `tests/test_cli.py` |
| **Preconditions** | `M-B12-04`, `M-B13-02`, `M-B14-02`, `M-B15-01` |
| **Input Type** | scripted Orca responses and temporary repositories |
| **Input Validation** | every expected command consumed exactly once |
| **Output Type** | `unittest` result and recorded argv |
| **Output Validation** | no unconsumed response, no duplicate dispatch |
| **Exceptions** | test assertion |
| **Side Effects** | temporary directory only |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | 해당 integration test files 자체 |
| **Rollback** | temp cleanup |

```text
script terminal, task, dispatch, message and gate responses
run coordinator until terminal state
assert exact command sequence and generation history
inject crash after each durable stage and resume
assert active dispatch never duplicated
```

### M-B15-03 — Live Orca Permission and E2E Gates

| Field | Contract |
|---|---|
| **Micro Block ID** | `M-B15-03` |
| **Parent Block** | `B-15` |
| **Name** | Opt-in live E2E validation |
| **Rationale** | agent sandbox flags와 lifecycle은 live runtime에서만 증명 가능 |
| **Objective** | 확정 strategy의 `V-PERM-01..07`, worker_done provenance, outbox, gate를 실제 Orca에서 회귀 검증 |
| **Target Files** | `tests/test_e2e_orca.py` |
| **Preconditions** | `M-B00-01` `PASS`, `M-B15-02`, `--e2e`, Orca ready, test worktree, explicit coordinator handle |
| **Input Type** | `E2EConfig` |
| **Input Validation** | production worktree 거부, disposable fixture marker 필수 |
| **Output Type** | `unittest` result plus created Orca resource report |
| **Output Validation** | source mutation 거부, own outbox write 성공, control/foreign step 보호, foreign lifecycle 무시 |
| **Exceptions** | unavailable runtime은 `BLOCKED`, assertion failure는 `FAIL` |
| **Side Effects** | test terminals/tasks/gates; 생성 ID 전부 기록 |
| **Detailed Pseudocode** | 아래 참조 |
| **Tests** | live test 자체 |
| **Rollback** | 생성 terminal은 test 종료 시 close 가능; runtime task reset은 자동 실행 금지 |

```text
verify disposable marker and Orca version
create isolated fixture worktree terminals
dispatch read-only mutation attempts and outbox write attempts
verify actual filesystem deltas and lifecycle provenance
exercise decision gate
report every created runtime ID
never call orchestration reset
```

---

## 5. Phase 4 Implementation Order

Phase 4는 다음 순서로 구현·검증한다.

```text
1. M-B00-01                 # live Permission Feasibility Spike; 실패 시 BLOCKED
2. M-B01-01..03
3. M-B03-01..02
4. M-B04-01..02
5. M-B06-01..02
6. M-B07-01..03
7. M-B08-01..02
8. M-B09-01..02
9. M-B02-01..02
10. M-B05-01..02
11. M-B11-01..02
12. M-B10-01
13. M-B12-01               # GenerationStore가 Prepared Task보다 먼저
14. M-B10-02..03
15. M-B13-01..03
16. M-B12-05
17. M-B12-02..04
18. M-B14-01..02
19. M-B15-01..03
```

각 순서에서 targeted `unittest`를 실행하고 다음 그룹으로 진행한다. live Orca mutation은
최초 `M-B00-01`의 disposable fixture permission gate와 마지막 `M-B15-03`의 확정 strategy
회귀 E2E에서만 수행한다.

---

## 6. Validation and Risks

### 6.1 Phase 3 validation contract

| Validation | Expected |
|---|---|
| Micro Block ID uniqueness | 중복 0 |
| Parent coverage | `B-00`~`B-15` 전부 1개 이상 |
| Required 16 fields | 모든 Micro Block 완비 |
| Target file traceability | 모든 Phase 2 target module이 1개 이상 Micro Block에 매핑 |
| `C3-01` dispatch race | input staging이 task/dispatch보다 먼저 |
| `C3-02` ledger propagation | caller가 returned ledger를 assignment |
| `C3-03` test safety | exact allowlist + sanitized env + delta guard |
| `C3-05` permission first gate | `M-B00-01`이 구현 순서 1이고 실패 시 후속 block 없음 |
| Preconditions DAG | undefined ID 0, cycle 0, 제시한 Phase 4 order가 유효한 topological order |
| reviewer test status | `PASS | NOT_RUN`만 전달, `FAIL`은 `FIX`, `NOT_RUN`은 Human Gate까지 보존 |
| 5-round/E-05 | 최대 5, 조기 합의, 동일 무진전 signature 2회 즉시 escalation |

### 6.1.1 Performed static validation

| Validation | Status | Evidence |
|---|---|---|
| Micro Block structure | **PASS** | 39 blocks, duplicate ID 0, required 16-field 누락 0 |
| Parent coverage | **PASS** | `B-00`~`B-15`, missing 0 |
| Preconditions DAG/order | **PASS** | 39 defined blocks, undefined dependency 0, cycle node 0, Phase 4 order violation 0 |
| Review에서 지적된 shared type | **PASS** | 43 required identifier scan, missing 0; artifact-specific verdict와 wire alias 명시 |
| Cross-document stale contract | **PASS** | old 3-worker key, single shared verdict enum, duplicated round counter, always-PASS, parent-env, old alternative-plan function 표현 0 |
| Markdown/trailing whitespace | **PASS** | Phase 1·2·3 fence parity 정상, duplicate consecutive heading 0, trailing whitespace 0 |
| Permission live behavior | **NOT RUN** | Phase 4 첫 `M-B00-01` |
| Code/unit/E2E | **NOT RUN** | Phase 4 승인 전 |

### 6.2 Risks

| ID | Risk | Phase 4 Gate |
|---|---|---|
| `P3-R1` | Claude/Codex가 최소 outbox write 권한과 repository read 권한을 동시에 제공하지 못할 수 있음 | **첫 단계 `M-B00-01`** `V-PERM-01..05`; 성립 strategy 없으면 `BLOCKED`, 후속 구현 금지 |
| `P3-R2` | live Orca response shape가 help text보다 추가 wrapper를 포함할 수 있음 | captured fixture 추가, unknown top-level metadata 허용하되 result schema strict |
| `P3-R3` | user test policy가 없으면 target test를 자동 실행할 수 없음 | `NOT_RUN` 유지 후 Q-2 흐름 |
| `P3-R4` | Windows process-tree timeout termination이 자식 프로세스를 남길 수 있음 | process group/job object 검증, 실패 시 `BLOCKED` |
| `P3-R5` | Orca runtime response에 문서화되지 않은 metadata가 추가될 수 있음 | unknown top-level metadata는 보존하고 required result schema만 strict 검증 |

### 6.3 Open Questions

구현을 시작하기 전에 필요한 추가 기능 선택은 없다. `P3-R1`은 Phase 4 최초 차단 Gate,
`P3-R4`는 관련 구현 단계에서 실제 실행으로 판정한다. Permission strategy는 추측하거나
사전 고정하지 않고 `M-B00-01` 결과로만 선택한다.

---

## 7. Approval

- [ ] Micro Blocking approved
- [x] Revision requested
- [ ] Permission granted to begin implementation

Phase 1 Revision 7, Phase 2 Revision 7, Phase 3 Revision 3이 함께 명시 승인되어야 Phase 4를
시작한다. 합의 round 상한은 사용자 지시대로 계획/구현 각각 **5**로 유지한다.

**Next phase after explicit approval:** Phase 4 — `M-B00-01` Permission Feasibility Spike
