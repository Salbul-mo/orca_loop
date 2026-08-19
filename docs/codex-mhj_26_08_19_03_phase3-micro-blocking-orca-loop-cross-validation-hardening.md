# Task Report: Orca Loop 상호검증 및 Evidence Lineage 강화

**Current Phase:** 3. Micro Blocking

**Status:** Approved — 2026-08-19

**Date:** 2026-08-19

**Approved Baseline:** `docs/codex-mhj_26_08_19_02_phase2-macro-blocking-orca-loop-cross-validation-hardening.md`

**Baseline HEAD:** `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`

---

## 1. Context and Objective

### Problem

승인된 `B-01`부터 `B-13`까지를 Phase 4에서 바로 구현할 수 있는 최소 책임
단위로 분해해야 한다. 현재 구현은 `ReviewArtifact`를 reviewer 완료 직후 ledger에
적용하고, `_step_inputs()`가 모든 기존 artifact를 blanket staging하며,
`_round_evidence()`가 현재 state snapshot을 과거 review evidence에 결합한다.

### Goal

각 Micro Block에 concrete type, target symbol, validation rule, exception, side
effect, rollback, test를 고정한다. 승인 후 구현은 이 문서의 dependency order와
change surface를 넘지 않는다.

### Scope

- Blind review A/B의 sealed input, strict artifact, pending lifecycle
- Deterministic comparison과 conditional symmetric adjudication
- Provider diversity와 legacy manifest migration
- Test, review, consensus, gate snapshot lineage
- Reporting defect와 operator documentation 정합화
- Focused, recovery, full-regression validation

### Out of Scope

- Host-wide process sandbox 또는 container
- Semantic LLM deduplication
- `run_loop.py` 전면 application-layer 분해
- Plan lane provider diversity의 blocking 강제
- CLI version drift를 permission proof blocking condition으로 변경
- 실제 provider를 호출하는 live Orca E2E

---

## 2. Approved Invariants

| ID | Invariant |
| --- | --- |
| `RQ-01` | Blind A/B는 동일한 `CodeReviewRoundContext`를 사용한다. |
| `RQ-02` | Blind reviewer는 peer artifact와 peer-created finding을 받지 않는다. |
| `RQ-03` | Blind B의 scope는 Blind A 실행 전 scope와 동일하다. |
| `RQ-04` | 두 blind artifact 검증 전에는 shared ledger를 변경하지 않는다. |
| `RQ-05` | `APPROVE`에는 acceptance, file, test 전체 coverage와 evidence가 필요하다. |
| `RQ-06` | Conflict에만 동일 reveal package를 사용한 symmetric adjudication을 실행한다. |
| `RQ-07` | Adjudicator는 peer adjudication artifact를 받지 않는다. |
| `RQ-08` | 신규 run의 두 code-review worker provider는 기본적으로 달라야 한다. |
| `RQ-09` | Test, review, adjudication, consensus, merge snapshot은 동일해야 한다. |
| `RQ-10` | Independence 또는 provenance 증거가 없으면 fail closed한다. |

---

## 3. Concrete Type Catalog

이 절의 field 이름과 enum 값은 Phase 4 구현 계약이다. 모든 digest는
`sha256:<64 lowercase hex>` 형식이며, 모든 ID와 path는 nonempty UTF-8 string이다.
Repository path는 absolute path와 `..`를 거부하고 NFC-normalized POSIX relative
path로 저장한다.

### 3.1 New Enums

```text
ReviewPhase = BLIND | ADJUDICATION
ReviewLane = A | B
ReviewComparisonStatus = AGREED | ADJUDICATION_REQUIRED | INVALID
ReviewConflictKind = BASELINE_DECISION | UNILATERAL_FINDING |
                     FINDING_SIGNATURE | COVERAGE | VERIFICATION
AdjudicationDecision = CONFIRM | REJECT | DUPLICATE | VERIFY_REQUIRED
ConsensusIndependence = FULL | DEGRADED
ConsensusProviderPolicy = DIVERSE | EXPLICIT_SAME_PROVIDER |
                          LEGACY_UNSPECIFIED
DriftAction = REBASELINE | INVALIDATE_PLAN | INVALIDATE_CODE | BLOCK
PendingReviewStage = CONTEXT_READY | BLIND_A_READY | BLIND_PAIR_READY |
                     COMPARISON_READY | ADJUDICATION_A_READY |
                     ADJUDICATION_PAIR_READY
```

`ArtifactKind`에는 `CODE_REVIEW_A`, `CODE_REVIEW_B`, `REVIEW_ADJUDICATION_A`,
`REVIEW_ADJUDICATION_B`를 추가한다. 기존 `CODE_REVIEW`와 `CROSS_REVIEW` 값은
legacy history를 읽기 위해 유지하지만 신규 code-review round에는 쓰지 않는다.

### 3.2 Coverage Types

```text
AcceptanceEvaluation
  criterion_id: str
  decision: DecisionValue
  evidence_refs: tuple[str, ...]

FileEvaluation
  path: str
  operation: AffectedFileOperation
  rename_from: str | None
  decision: DecisionValue
  evidence_refs: tuple[str, ...]

TestEvaluation
  test_id: str
  test_gate_status: TestGateStatus  # PASS or NOT_RUN only
  decision: DecisionValue
  evidence_refs: tuple[str, ...]

PlanVerification
  category: affected_files | integration_points | public_interfaces |
            acceptance_verifiability | test_contract |
            repository_facts | security_and_contract_impact
  decision: DecisionValue
  evidence_refs: tuple[str, ...]
```

모든 evaluation의 ID tuple은 sealed context의 tuple과 순서까지 같아야 한다.
모든 `APPROVE` evaluation은 nonempty evidence를 가져야 한다.
`TestEvaluation.test_gate_status=NOT_RUN`은 실행 성공 evidence로 사용할 수 없다.

### 3.3 Test and Review Context Types

```text
TestCommandEvidence
  command_index: int
  command: TestCommand
  return_code: int | None
  timed_out: bool
  stdout_tail_digest: str
  stderr_tail_digest: str

TestEvidence
  schema_version: int
  run_id: str
  plan_version: int
  consensus_round: int
  test_gate_status: TestGateStatus
  test_policy_digest: str
  commands: tuple[TestCommandEvidence, ...]
  policy_violations: tuple[TestPolicyViolation, ...]
  before_snapshot_digest: str
  after_snapshot_digest: str | None
  authoritative_snapshot_digest: str
  test_ids: tuple[str, ...]
  attribution: TestFailureAttribution
  artifact_digest: str

CodeReviewRoundContext
  schema_version: int
  run_id: str
  consensus_round: int
  plan_version: int
  snapshot_digest: str
  implementation_artifact_digest: str
  test_evidence_digest: str
  frozen_diff_digest: str
  scope_manifest_digest: str
  readonly_mirror_digest: str
  baseline_finding_ids: tuple[str, ...]
  acceptance_criteria_ids: tuple[str, ...]
  affected_files: tuple[AffectedFile, ...]
  test_ids: tuple[str, ...]
  context_digest: str
```

`TestEvidence.artifact_digest`와 `CodeReviewRoundContext.context_digest`는 각각
자기 digest field를 제외한 canonical JSON bytes의 SHA-256이다. `PASS`의
`authoritative_snapshot_digest`는 `after_snapshot_digest`, `NOT_RUN`은
`before_snapshot_digest`와 같아야 한다.

### 3.4 Blind and Adjudication Types

```text
BlindReviewArtifact
  schema_version: int
  artifact_kind: CODE_REVIEW_A | CODE_REVIEW_B
  run_id: str
  task_id: str
  dispatch_id: str
  consensus_round: int
  plan_version: int
  snapshot_digest: str
  review_context_digest: str
  role: Role
  lane: ReviewLane
  verdict: CodeReviewVerdict
  reviewed_artifact_digest: str
  reviewed_finding_ids: tuple[str, ...]
  acceptance_evaluations: tuple[AcceptanceEvaluation, ...]
  file_evaluations: tuple[FileEvaluation, ...]
  test_evaluations: tuple[TestEvaluation, ...]
  review_summary: str
  finding_decisions: tuple[FindingDecision, ...]
  findings: tuple[Finding, ...]
  non_blocking_suggestions: tuple[InformationalFinding, ...]
  escalation_signals: tuple[EscalationTrigger, ...]

ReviewConflictCandidate
  candidate_id: str
  kind: ReviewConflictKind
  finding_ids: tuple[str, ...]
  acceptance_criteria_ids: tuple[str, ...]
  affected_files: tuple[str, ...]
  test_ids: tuple[str, ...]
  blind_a_decision: DecisionValue | None
  blind_b_decision: DecisionValue | None
  normalized_signature: str
  evidence_refs: tuple[str, ...]

ReviewComparison
  schema_version: int
  run_id: str
  consensus_round: int
  snapshot_digest: str
  review_context_digest: str
  pre_round_ledger_digest: str
  blind_a_artifact_digest: str
  blind_b_artifact_digest: str
  status: ReviewComparisonStatus
  agreed_finding_ids: tuple[str, ...]
  candidates: tuple[ReviewConflictCandidate, ...]
  comparison_digest: str

CandidateDecision
  candidate_id: str
  decision: AdjudicationDecision
  duplicate_of: str | None
  root_cause_assessment: str
  required_action: str | None
  evidence_refs: tuple[str, ...]

AdjudicationArtifact
  schema_version: int
  artifact_kind: REVIEW_ADJUDICATION_A | REVIEW_ADJUDICATION_B
  run_id: str
  task_id: str
  dispatch_id: str
  consensus_round: int
  snapshot_digest: str
  review_context_digest: str
  comparison_digest: str
  role: Role
  lane: ReviewLane
  candidate_decisions: tuple[CandidateDecision, ...]
```

Blind artifact에는 `agrees_with_reviewer` field가 없다. Lane A/B의
`FindingDecision.side`는 기존 persistent ledger 호환을 위해 각각 legacy
`Side.CLAUDE`와 `Side.CODEX`로 변환되며 runtime provider 의미를 갖지 않는다.

### 3.5 Pending State and Lineage Types

```text
PendingReviewRound
  consensus_round: int
  stage: PendingReviewStage
  review_context_digest: str
  pre_round_ledger_digest: str
  blind_a_input_manifest_digest: str | None
  blind_a_artifact_digest: str | None
  blind_b_input_manifest_digest: str | None
  blind_b_artifact_digest: str | None
  comparison_digest: str | None
  reveal_manifest_digest: str | None
  adjudication_a_artifact_digest: str | None
  adjudication_b_artifact_digest: str | None

ValidationLineage
  test_gate_snapshot_digest: str | None
  test_evidence_digest: str | None
  review_context_snapshot_digest: str | None
  review_context_digest: str | None
  blind_review_a_snapshot_digest: str | None
  blind_review_a_artifact_digest: str | None
  blind_review_b_snapshot_digest: str | None
  blind_review_b_artifact_digest: str | None
  review_comparison_digest: str | None
  adjudication_a_snapshot_digest: str | None
  adjudication_a_artifact_digest: str | None
  adjudication_b_snapshot_digest: str | None
  adjudication_b_artifact_digest: str | None
  consensus_snapshot_digest: str | None
```

`CoordinatorState`에는 `pending_review: PendingReviewRound | None`과
`validation_lineage: ValidationLineage`를 추가한다. `GateBinding`에는
`snapshot_digest`, `review_context_digest`, `validation_lineage_digest`를 추가한다.
Legacy binding에서 이 값이 없으면 status 조회는 가능하지만 merge qualification은
false다.

### 3.6 Operational Binding Types

```text
ReadonlyMirrorBinding
  path: Path
  tree_digest: str
  source_snapshot_digest: str

DriftDecision
  drifted: bool
  action: DriftAction | None
  new_digest: str | None
  target_state: LoopState | None
  detail: tuple[str, ...]

MergeQualification
  qualified: bool
  snapshot_digest: str
  review_context_digest: str
  validation_lineage_digest: str
  evidence_digests: tuple[str, ...]
```

`UserDecisionNoticeStatus`에는 `INVALIDATED`를 추가하고
`UserDecisionNotice`에는 `invalidated_at: str | None`과
`invalidation_reason: str | None`을 추가한다. `PENDING`은 세 terminal field
(`resolved_at`, `invalidated_at`, `invalidation_reason`)가 모두 `None`이어야 한다.
`RESOLVED`는 `resolved_at`만 non-null이고, `INVALIDATED`는 `invalidated_at`과
`invalidation_reason`만 non-null이어야 한다.

---

## 4. Micro Block Index

| Micro Block | Parent | Objective | Primary Target | Test ID Range |
| --- | --- | --- | --- | --- |
| `M-B01-01` | `B-01` | Domain enum과 frozen DTO 정의 | `orca_loop/models.py` | `T-B01-01`~`03` |
| `M-B01-02` | `B-01` | State generation migration | `orca_loop/generation.py` | `T-B01-04`~`06` |
| `M-B01-03` | `B-01` | Strict serialization과 digest 검증 | `orca_loop/contracts.py` | `T-B01-07`~`09` |
| `M-B02-01` | `B-02` | `TestEvidence` 영속화 | `orca_loop/testrunner.py` | `T-B02-01`~`04` |
| `M-B02-02` | `B-02` | Sealed context 생성 | `orca_loop/snapshot.py` | `T-B02-05`~`08` |
| `M-B02-03` | `B-02` | Shared mirror digest와 lifecycle | `orca_loop/readonly.py` | `T-B02-09`~`11` |
| `M-B03-01` | `B-03` | Structured coverage contract | `orca_loop/contracts.py` | `T-B03-01`~`05` |
| `M-B03-02` | `B-03` | Blind artifact strict parser | `orca_loop/contracts.py` | `T-B03-06`~`10` |
| `M-B03-03` | `B-03` | Adjudication strict parser | `orca_loop/contracts.py` | `T-B03-11`~`14` |
| `M-B04-01` | `B-04` | Role/phase input allowlist | `run_loop.py` | `T-B04-01`~`04` |
| `M-B04-02` | `B-04` | Blind logical manifest equality | `orca_loop/dispatcher.py` | `T-B04-05`~`07` |
| `M-B04-03` | `B-04` | Shared mirror dispatch binding | `run_loop.py` | `T-B04-08`~`10` |
| `M-B05-01` | `B-05` | New state transition table | `orca_loop/machine.py` | `T-B05-01`~`04` |
| `M-B05-02` | `B-05` | Pending evidence promotion | `orca_loop/coordinator.py` | `T-B05-05`~`08` |
| `M-B05-03` | `B-05` | Crash-safe resume | `run_loop.py` | `T-B05-09`~`13` |
| `M-B06-01` | `B-06` | Candidate normalization | `orca_loop/ledger.py` | `T-B06-01`~`04` |
| `M-B06-02` | `B-06` | Deterministic comparison | `orca_loop/ledger.py` | `T-B06-05`~`10` |
| `M-B06-03` | `B-06` | Atomic pair ledger application | `orca_loop/coordinator.py` | `T-B06-11`~`14` |
| `M-B07-01` | `B-07` | Symmetric reveal package | `run_loop.py` | `T-B07-01`~`04` |
| `M-B07-02` | `B-07` | Adjudicator dispatch isolation | `orca_loop/coordinator.py` | `T-B07-05`~`08` |
| `M-B07-03` | `B-07` | Candidate merge matrix | `orca_loop/ledger.py` | `T-B07-09`~`15` |
| `M-B08-01` | `B-08` | Provider diversity preflight | `orca_loop/config.py` | `T-B08-01`~`04` |
| `M-B08-02` | `B-08` | Manifest policy migration | `orca_loop/runspec.py` | `T-B08-05`~`09` |
| `M-B08-03` | `B-08` | Dry-run/status diagnostics | `run_loop.py` | `T-B08-10`~`12` |
| `M-B09-01` | `B-09` | State-group drift classifier | `run_loop.py` | `T-B09-01`~`06` |
| `M-B09-02` | `B-09` | Evidence invalidation | `orca_loop/coordinator.py` | `T-B09-07`~`12` |
| `M-B09-03` | `B-09` | Final merge qualification guard | `run_loop.py` | `T-B09-13`~`18` |
| `M-B09-04` | `B-09` | Stale gate/notice invalidation | `orca_loop/escalation.py` | `T-B09-19`~`22` |
| `M-B10-01` | `B-10` | Current schema renderer 수정 | `orca_loop/reporting.py` | `T-B10-01`~`04` |
| `M-B10-02` | `B-10` | Blind/adjudication reports | `orca_loop/reporting.py` | `T-B10-05`~`08` |
| `M-B10-03` | `B-10` | Summary와 final evidence report | `orca_loop/escalation.py` | `T-B10-09`~`13` |
| `M-B11-01` | `B-11` | Blind reviewer prompt | `prompts/code_reviewer.md` | `T-B11-01`~`04` |
| `M-B11-02` | `B-11` | Adjudication prompt와 routing | `orca_loop/roles.py` | `T-B11-05`~`09` |
| `M-B11-03` | `B-11` | Bounded Plan Review matrix | `prompts/plan_reviewer.md` | `T-B11-10`~`13` |
| `M-B12-01` | `B-12` | Operator guide 동기화 | `orca_loop_execution_rules.md` | `T-B12-01`~`04` |
| `M-B12-02` | `B-12` | Permission environment 설명 정합화 | `orca_loop/environment.py` | `T-B12-05`~`07` |
| `M-B12-03` | `B-12` | Documentation policy assertions | `tests/test_environment.py` | `T-B12-08`~`10` |
| `M-B13-01` | `B-13` | Focused component regression | `tests/` | `T-B13-01`~`03` |
| `M-B13-02` | `B-13` | Crash/resume integration matrix | `tests/test_resume.py` | `T-B13-04`~`08` |
| `M-B13-03` | `B-13` | Full final validation | repository-wide | `T-B13-09`~`12` |

---

## 5. B-01 — Persistent Contracts and State Evolution

### M-B01-01 — Domain enums and frozen DTOs

- **Parent:** `B-01`
- **Rationale:** 후속 block이 ad-hoc dictionary를 만들지 않게 durable vocabulary를 먼저 고정한다.
- **Objective:** Section 3의 enum과 dataclass가 frozen type으로 정의되고 invalid combination을 constructor 또는 parser boundary에서 거부한다.
- **Target Files:** `orca_loop/models.py`, `tests/test_contracts.py`, `tests/test_generation.py`가 없으므로 generation 검증은 `tests/test_coordinator.py`에 둔다.
- **Preconditions:** Approved `RQ-01`~`RQ-10`; production dependency 추가 없음.
- **Input Type / Validation:** Section 3의 exact fields; digest, identifier, path, enum invariant를 적용한다.
- **Output Type / Validation:** `TestEvidence`, `CodeReviewRoundContext`, `BlindReviewArtifact`, `ReviewComparison`, `AdjudicationArtifact`, `PendingReviewRound`, `ValidationLineage`; 모든 dataclass는 `frozen=True`다.
- **Exceptions:** 직접 I/O가 없으므로 domain construction error는 기존 `ContractViolationError` 또는 persistence decode의 `AtomicWriteError`로 번역한다.
- **Side Effects:** 없음.

#### Detailed Pseudocode

```text
1. Add exact enums without removing legacy wire values.
2. Define leaf evaluation and candidate dataclasses first.
3. Define aggregate artifact and context dataclasses from leaf types.
4. Give only legacy-migration fields explicit immutable defaults.
5. Reject mutable list, dict, Path payloads at parsing boundaries.
6. Assert lane, artifact kind, optional digest, and stage combinations.
7. Return frozen values to all consumers.
```

- **Tests:** `T-B01-01` enum exact-value test; `T-B01-02` frozen mutation rejection; `T-B01-03` aggregate equality and tuple-only field test.
- **Rollback:** 새 enum/dataclass와 그 imports만 제거한다. 기존 model field와 enum 값은 수정하지 않는다.

### M-B01-02 — Coordinator state schema migration

- **Parent:** `B-01`
- **Rationale:** Legacy generation은 읽혀야 하지만 missing lineage로 merge되면 안 된다.
- **Objective:** schema v1 state를 empty lineage와 `pending_review=None`으로 읽고 다음 commit에서 current schema로 기록한다.
- **Target Files:** `orca_loop/models.py`, `orca_loop/generation.py`, `orca_loop/coordinator.py`, `tests/test_coordinator.py`, `tests/test_resume.py`.
- **Preconditions:** `M-B01-01` complete; committed `state.N.json`, `ledger.N.json`, `commit.json` digest semantics 유지.
- **Input Type / Validation:** JSON object with `schema_version` in supported state versions; unknown version and unexpected fields fail closed.
- **Output Type / Validation:** Current `CoordinatorState`; v1 maps to `ValidationLineage` with every field `None` and no pending round.
- **Exceptions:** Unknown version or invalid optional-field combination raises `AtomicWriteError`; generation mismatch remains `GenerationMismatchError`.
- **Side Effects:** 기존 generation 파일을 rewrite하지 않는다. 다음 normal commit만 current schema를 쓴다.

#### Detailed Pseudocode

```text
1. Read and digest-verify committed state bytes.
2. Inspect schema_version before dataclass decoding.
3. If v1, inject empty validation_lineage and null pending_review in memory.
4. If current, decode every exact typed field.
5. Otherwise raise AtomicWriteError without changing commit.json.
6. Validate pending stage against populated digest fields.
7. Return migrated state and original ledger.
8. On next GenerationController.commit, serialize current schema atomically.
```

- **Tests:** `T-B01-04` v1 load; `T-B01-05` unsupported version rejection; `T-B01-06` migrated next-generation reread and digest verification.
- **Rollback:** current schema writer를 제거해도 legacy files는 untouched다. Migration 이전에 생성된 generation은 그대로 복구 가능하다.

### M-B01-03 — Strict wire serialization and self-digest verification

- **Parent:** `B-01`
- **Rationale:** Context와 evidence identity가 parser별 JSON ordering에 좌우되면 안 된다.
- **Objective:** 신규 coordinator/worker artifact를 exact schema로 parse하고 canonical self-digest를 검증한다.
- **Target Files:** `orca_loop/contracts.py`, `orca_loop/generation.py`, `tests/test_contracts.py`.
- **Preconditions:** `M-B01-01`, `M-B01-02` complete.
- **Input Type / Validation:** UTF-8 strict JSON object, bounded bytes, exact root fields, no duplicate semantic IDs, canonical digest format.
- **Output Type / Validation:** Typed artifact 또는 context; serialize 후 parse 결과가 equality를 만족한다.
- **Exceptions:** Shape error는 `ContractViolationError`, provenance/digest mismatch는 `ProvenanceError`, atomic persistence error는 `AtomicWriteError`.
- **Side Effects:** Coordinator-owned artifact는 `write_atomic_bytes()`로 기록하고 reread한다.

#### Detailed Pseudocode

```text
1. Decode exactly one JSON object and reject trailing data.
2. Check required and unexpected root fields.
3. Parse every scalar, enum, tuple, nested record, and digest.
4. Build canonical wire value excluding the self-digest field.
5. Compute SHA-256 and compare with the supplied self-digest.
6. Construct the frozen typed value.
7. Serialize with sorted keys and compact separators.
8. Parse serialized bytes again and require typed equality.
9. Persist only after every validation succeeds.
```

- **Tests:** `T-B01-07` canonical round trip; `T-B01-08` unknown/missing/duplicate field rejection; `T-B01-09` self-digest tamper rejection.
- **Rollback:** 신규 parser/serializer entry points만 제거한다. 기존 plan, implementation, permission contract는 유지한다.

---

## 6. B-02 — Test Evidence and Sealed Review Context

### M-B02-01 — Durable coordinator-owned TestEvidence

- **Parent:** `B-02`
- **Rationale:** 현재 `execute_test_gate()`는 `TestGateResult`를 status 하나로 축약한다.
- **Objective:** test execution 직후 `artifacts/test_evidence.json`을 atomic write하고 lineage의 test fields를 같은 generation에 commit한다.
- **Target Files:** `orca_loop/testrunner.py`, `orca_loop/coordinator.py`, `run_loop.py`, `orca_loop/contracts.py`, `tests/test_testrunner.py`, `tests/test_coordinator.py`.
- **Preconditions:** `M-B01-01`~`03`; policy validation이 test process 실행보다 먼저 완료돼야 한다.
- **Input Type / Validation:** `TestGateResult`, `run_id`, positive `plan_version`, positive code round, exact `test_ids`; output tails는 기존 bounded tail에서 digest만 계산한다.
- **Output Type / Validation:** `TestEvidence`; `PASS`와 `NOT_RUN`만 review context로 이어지고 `FAIL`과 `POLICY_VIOLATION`도 audit evidence는 남긴다.
- **Exceptions:** Policy violation은 기존 signal path; persistence failure는 `AtomicWriteError`; snapshot mismatch는 `SnapshotChangedError`.
- **Side Effects:** `artifacts/test_evidence.json` 및 immutable history 기록, state lineage update.

#### Detailed Pseudocode

```text
1. Validate commands against TestExecutionPolicy.
2. Capture before snapshot and run allowed commands when present.
3. Capture after snapshot under existing output-path policy.
4. Convert each command result to TestCommandEvidence.
5. Select authoritative snapshot: after for PASS, before for NOT_RUN.
6. Build and self-digest TestEvidence without claiming NOT_RUN execution.
7. Atomically write and reread test_evidence.json.
8. Commit test_gate_snapshot_digest and test_evidence_digest.
9. Clear all review, comparison, adjudication, and consensus lineage.
10. Return the existing test-gate transition signal.
```

- **Tests:** `T-B02-01` PASS evidence; `T-B02-02` NOT_RUN boundary; `T-B02-03` FAIL/policy evidence; `T-B02-04` bounded output digest and tamper detection.
- **Rollback:** Evidence write와 lineage update를 제거하면 기존 status-only transition으로 돌아간다. Test command execution semantics는 변경하지 않는다.

### M-B02-02 — Sealed CodeReviewRoundContext builder

- **Parent:** `B-02`
- **Rationale:** Diff, scope, test, implementation, mirror가 동일 snapshot에서 생성됐음을 한 digest로 증명해야 한다.
- **Objective:** `REVIEW_CONTEXT_PREPARE`에서 한 번만 context를 만들고 source drift가 있으면 어떤 reviewer도 dispatch하지 않는다.
- **Target Files:** `orca_loop/snapshot.py`, `orca_loop/coordinator.py`, `run_loop.py`, `orca_loop/contracts.py`, `tests/test_snapshot.py`, `tests/test_coordinator.py`.
- **Preconditions:** `M-B02-01`; valid plan and implementation artifact; no active pending round.
- **Input Type / Validation:** `SnapshotIdentity`, `PlanDocument`, implementation digest, `TestEvidence`, pre-round `ConsensusLedger`, destructive approval digest.
- **Output Type / Validation:** `CodeReviewRoundContext`, `frozen.diff`, `scope-manifest.json`; all component digests and context digest reread verified.
- **Exceptions:** Before/during/after drift raises `SnapshotChangedError`; missing artifact raises `CoordinatorContractError`; path escape raises `SnapshotPathBoundaryError`.
- **Side Effects:** Round-specific immutable context directory와 `artifacts/review_context.json` 생성.

#### Detailed Pseudocode

```text
1. Capture snapshot_before and require it equals TestEvidence authoritative snapshot.
2. Load and digest plan, implementation, test evidence, and pre-round ledger.
3. Materialize frozen.diff and canonical scope-manifest.json.
4. Create the round mirror through M-B02-03.
5. Compute every component digest from persisted bytes.
6. Capture snapshot_after.
7. If snapshot_before differs from snapshot_after, discard usability and raise.
8. Build CodeReviewRoundContext with pre-round scope tuples.
9. Compute context_digest excluding its own field.
10. Atomically write, reread, and compare the context.
11. Commit pending_review at CONTEXT_READY and review-context lineage.
```

- **Tests:** `T-B02-05` complete context; `T-B02-06` before/during drift; `T-B02-07` component tamper; `T-B02-08` baseline scope ordering.
- **Rollback:** 새 round directory는 run-local recoverable artifact다. Context state commit 전 failure는 다음 resume에서 새 context를 만든다.

### M-B02-03 — Shared read-only mirror digest and lifecycle

- **Parent:** `B-02`
- **Rationale:** 각 reviewer별 새 mirror는 logical input identity를 약화한다.
- **Objective:** round당 mirror 하나를 만들고 deterministic tree digest와 read-only enforcement를 context에 bind한다.
- **Target Files:** `orca_loop/readonly.py`, `run_loop.py`, `tests/test_readonly.py`.
- **Preconditions:** source snapshot fixed; review root outside target worktree.
- **Input Type / Validation:** absolute source and review root, nonnegative round identity, expected snapshot digest; symlink and excluded path policy는 기존 규칙 유지.
- **Output Type / Validation:** Section 3.6의 exact `ReadonlyMirrorBinding`; dataclass는 `frozen=True`다.
- **Exceptions:** Copy, ACL, symlink, boundary, digest mismatch는 `ReadOnlyMirrorError`.
- **Side Effects:** `.git`, `.venv`, `runs`, cache를 제외한 repository mirror 생성 후 ACL 또는 mode lock.

#### Detailed Pseudocode

```text
1. Verify source and destination boundaries.
2. Create a unique context-specific destination.
3. Copy allowed files while rejecting symlinks.
4. Hash sorted relative path plus canonical file bytes for every copied file.
5. Compare the copied tree digest with a source tree digest using the same filter.
6. Apply Windows RX ACL or POSIX 444/555 modes.
7. Verify a representative write attempt is denied where the platform test supports it.
8. Return one binding and reuse its path for all four possible review calls.
```

- **Tests:** `T-B02-09` deterministic tree digest; `T-B02-10` source/mirror mismatch and symlink rejection; `T-B02-11` same binding reused by A/B.
- **Rollback:** round mirror directory는 run cleanup 대상이지만 자동 삭제하지 않는다. 기존 per-generation mirror path로 코드 rollback 가능하다.

---

## 7. B-03 — Structured Blind Review Contracts

### M-B03-01 — Exact structured coverage validation

- **Parent:** `B-03`
- **Rationale:** Root verdict만으로 전체 scope 검토를 증명할 수 없다.
- **Objective:** acceptance, file, test evaluation tuple이 sealed context를 정확히 cover하고 evidence floor를 만족한다.
- **Target Files:** `orca_loop/models.py`, `orca_loop/contracts.py`, `tests/test_contracts.py`.
- **Preconditions:** `M-B01-01`, `M-B02-02`.
- **Input Type / Validation:** Section 3.2 values and `CodeReviewRoundContext`; ordering은 context tuple 그대로다.
- **Output Type / Validation:** Typed evaluation tuples; extra, missing, duplicate, reordered ID를 모두 reject한다.
- **Exceptions:** Coverage error는 field와 expected/actual ID를 포함한 `ContractViolationError`; context mismatch는 `ProvenanceError`.
- **Side Effects:** 없음.

#### Detailed Pseudocode

```text
1. Parse each evaluation array as a tuple.
2. Require acceptance IDs equal context.acceptance_criteria_ids.
3. Require file path, operation, rename_from triples equal context.affected_files.
4. Require test IDs equal context.test_ids.
5. Reject duplicate IDs and unknown enum values.
6. Require evidence_refs for every APPROVE decision.
7. If test gate is NOT_RUN, reject evidence text that claims command PASS.
8. Return immutable evaluation tuples.
```

- **Tests:** `T-B03-01` exact coverage; `T-B03-02` missing/extra; `T-B03-03` duplicate/reorder; `T-B03-04` evidence-free approve; `T-B03-05` NOT_RUN overstatement.
- **Rollback:** 신규 coverage fields/parser만 제거한다. Existing plan review contract는 `M-B11-03` 전까지 유지된다.

### M-B03-02 — Blind review artifact parser and provenance

- **Parent:** `B-03`
- **Rationale:** Blind artifact는 legacy confirmer field 없이 context와 lane을 직접 증명해야 한다.
- **Objective:** A/B artifact를 exact schema로 검증하고 peer-independent typed value를 반환한다.
- **Target Files:** `orca_loop/contracts.py`, `orca_loop/models.py`, `orca_loop/coordinator.py`, `tests/test_contracts.py`.
- **Preconditions:** `M-B03-01`; expected task/dispatch/context provenance available.
- **Input Type / Validation:** strict JSON `BlindReviewArtifact`; `artifact_kind`, `role`, `lane`, task, dispatch, round, plan, snapshot, context, implementation digest가 expected와 같아야 한다.
- **Output Type / Validation:** `BlindReviewArtifact`; approval이면 모든 evaluation과 finding decision이 `APPROVE`이고 blocking finding이 없어야 한다.
- **Exceptions:** Shape는 `ContractViolationError`, task/snapshot/context/round mismatch는 `ProvenanceError`.
- **Side Effects:** 검증된 raw bytes를 pending artifact path에 promote하지만 ledger는 변경하지 않는다.

#### Detailed Pseudocode

```text
1. Decode exact root schema and reject agrees_with_reviewer.
2. Validate expected kind, role, and lane mapping.
3. Validate run, task, dispatch, round, plan, snapshot, and context digests.
4. Parse exact coverage through M-B03-01.
5. Require reviewed_finding_ids equal baseline_finding_ids.
6. Validate decisions for baseline and newly emitted findings.
7. Enforce approval and changes-requested obligations.
8. Enforce finding IDs, evidence, escalation, and size bounds.
9. Return the typed artifact for pending promotion only.
```

- **Tests:** `T-B03-06` valid A/B; `T-B03-07` peer/agreement field rejection; `T-B03-08` lane-role-kind mismatch; `T-B03-09` approval obligation; `T-B03-10` context/digest mismatch.
- **Rollback:** 신규 parser call path를 제거하고 legacy parser를 복원할 수 있다. Legacy artifact values는 삭제하지 않는다.

### M-B03-03 — Adjudication artifact strict parser

- **Parent:** `B-03`
- **Rationale:** Candidate 일부만 판정하거나 서로 다른 comparison을 본 결과는 사용할 수 없다.
- **Objective:** 두 adjudicator가 동일 candidate tuple을 순서대로 모두 판정하고 admissible evidence를 제시하도록 강제한다.
- **Target Files:** `orca_loop/contracts.py`, `orca_loop/models.py`, `tests/test_contracts.py`.
- **Preconditions:** valid `ReviewComparison` with `ADJUDICATION_REQUIRED`.
- **Input Type / Validation:** `AdjudicationArtifact`; candidate IDs exact tuple, comparison/context/snapshot provenance exact.
- **Output Type / Validation:** typed candidate decisions; `DUPLICATE`는 same comparison 또는 existing ledger의 canonical target만 허용한다.
- **Exceptions:** Incomplete candidate coverage는 `ContractViolationError`; provenance mismatch는 `ProvenanceError`.
- **Side Effects:** 검증된 artifact를 pending path에 promote하며 ledger는 변경하지 않는다.

#### Detailed Pseudocode

```text
1. Decode exact root and lane-specific artifact kind.
2. Validate run, task, dispatch, round, snapshot, context, and comparison digest.
3. Require candidate_decisions IDs equal comparison candidates in canonical order.
4. For CONFIRM, require root cause, required action, and evidence.
5. For REJECT, require evidence and null duplicate_of.
6. For DUPLICATE, require a valid canonical duplicate_of target.
7. For VERIFY_REQUIRED, require evidence describing the missing verification.
8. Reject peer adjudication references in staged provenance.
9. Return the immutable artifact.
```

- **Tests:** `T-B03-11` full valid coverage; `T-B03-12` missing/extra candidate; `T-B03-13` invalid duplicate target; `T-B03-14` context/comparison mismatch.
- **Rollback:** Adjudication parser와 kinds만 제거한다. Direct-agreement path는 독립적으로 유지 가능하다.

---

## 8. B-04 — Blind Input Isolation and Shared Mirror

### M-B04-01 — Explicit role and phase input allowlist

- **Parent:** `B-04`
- **Rationale:** 현재 `_step_inputs()`는 존재하는 모든 artifact를 role에 관계없이 staging한다.
- **Objective:** role과 `ReviewPhase`별 allowlist 외 파일을 stage하지 않고 unknown phase를 fail closed한다.
- **Target Files:** `run_loop.py`, `orca_loop/roles.py`, `orca_loop/models.py`, `tests/test_dispatcher.py`, `tests/test_coordinator.py`.
- **Preconditions:** `M-B02-02`, `M-B03-02`, `M-B03-03`.
- **Input Type / Validation:** `Role`, `ReviewPhase | None`, `CodeReviewRoundContext | None`; required artifact path와 digest가 context와 일치해야 한다.
- **Output Type / Validation:** `tuple[StagedInput, ...]` with exact logical names; blind A/B allowlist는 Phase 1 Section 6.1과 동일하다.
- **Exceptions:** Missing required input, forbidden peer file, undefined role-phase는 `CoordinatorContractError`.
- **Side Effects:** Step input directory에 bounded copies와 manifest를 쓴다.

#### Detailed Pseudocode

```text
1. Resolve one explicit allowlist from role and phase.
2. Load only named source paths.
3. Verify each coordinator-owned artifact digest against context.
4. Reject any peer artifact name in a blind allowlist.
5. For adjudication, include both blind artifacts and comparison only.
6. For implementer, include only final accepted ledger scope.
7. Return the ordered StagedInput tuple.
8. Let dispatcher create and verify the physical copies.
```

- **Tests:** `T-B04-01` blind exact list; `T-B04-02` peer exclusion; `T-B04-03` adjudication exact list; `T-B04-04` unknown phase failure.
- **Rollback:** allowlist dispatcher를 제거하면 기존 `_step_inputs()`로 돌아갈 수 있지만 Phase 4 완료 조건은 충족하지 못한다.

### M-B04-02 — Blind logical input manifest equality

- **Parent:** `B-04`
- **Rationale:** Task/dispatch ID 때문에 physical manifest는 다르더라도 review content는 같아야 한다.
- **Objective:** A/B의 logical component digest tuple을 비교하고 차이가 있으면 B dispatch 전에 round를 중단한다.
- **Target Files:** `orca_loop/dispatcher.py`, `run_loop.py`, `orca_loop/models.py`, `tests/test_dispatcher.py`.
- **Preconditions:** `M-B04-01`; A input manifest가 pending state에 기록돼 있다.
- **Input Type / Validation:** `InputManifest`; task-specific contract file은 comparison에서 제외하고 sealed content files만 canonical name/digest로 비교한다.
- **Output Type / Validation:** `logical_manifest_digest: str`; A/B digest equality.
- **Exceptions:** Missing entry, duplicate logical name, digest mismatch는 `CoordinatorContractError`.
- **Side Effects:** `PendingReviewRound.blind_a_input_manifest_digest` 또는 B field를 generation commit한다.

#### Detailed Pseudocode

```text
1. Read the staged InputManifest after bounded copies are complete.
2. Select sealed logical entries and sort by UTF-8 name bytes.
3. Exclude role contract, task ID, dispatch ID, and output path metadata.
4. Hash canonical name and digest pairs.
5. Store A logical digest before A dispatch.
6. Compute B logical digest before B dispatch.
7. Require A digest equals B digest and context component set.
8. On mismatch, reject dispatch and preserve both manifests for diagnosis.
```

- **Tests:** `T-B04-05` physical metadata differs but logical digest matches; `T-B04-06` peer file changes digest; `T-B04-07` missing component rejects B.
- **Rollback:** pending manifest fields를 제거하면 staging artifacts는 그대로 보존되며 외부 side effect는 없다.

### M-B04-03 — Shared mirror binding at dispatch

- **Parent:** `B-04`
- **Rationale:** `_profile_root()`가 generation별 mirror를 만들면 context의 shared mirror 보장이 깨진다.
- **Objective:** Blind와 adjudication role이 context에 기록된 동일 mirror path를 사용하고 dispatch 직전에 read-only/digest를 재확인한다.
- **Target Files:** `run_loop.py`, `orca_loop/readonly.py`, `orca_loop/profiles.py`, `tests/test_readonly.py`, `tests/test_worker_runner.py`.
- **Preconditions:** `M-B02-03`; context and pending round valid.
- **Input Type / Validation:** mirror binding, expected tree digest, role/phase; implementer는 실제 worktree를 계속 사용한다.
- **Output Type / Validation:** reviewer `RoleContext.worktree_path` equals shared mirror path; provider profile remains read-only.
- **Exceptions:** Missing/writable/tampered mirror raises `ReadOnlyMirrorError`; role misuse raises `CoordinatorContractError`.
- **Side Effects:** Worker process cwd/profile root 설정만 변경한다.

#### Detailed Pseudocode

```text
1. If role is implementer, return the real worktree unchanged.
2. Otherwise load the context mirror binding.
3. Recompute mirror tree digest without changing permissions.
4. Require digest and source snapshot binding to match context.
5. Verify mirror path is outside target worktree.
6. Build the existing read-only LaunchProfile on that path.
7. Return the same path for Blind A, Blind B, Adjudicator A, and B.
```

- **Tests:** `T-B04-08` all review phases same root; `T-B04-09` tampered mirror blocked; `T-B04-10` implementer root unchanged.
- **Rollback:** shared-root resolution만 되돌리고 mirror directories는 audit evidence로 남긴다.

---

## 9. B-05 — Pending Review Lifecycle and State Machine

### M-B05-01 — Review lifecycle states and transition table

- **Parent:** `B-05`
- **Rationale:** Blind pair와 adjudication durable boundary가 explicit state여야 resume이 결정론적이다.
- **Objective:** 모든 신규 state/signal pair의 유일한 next state와 terminal behavior를 정의한다.
- **Target Files:** `orca_loop/models.py`, `orca_loop/machine.py`, `orca_loop/coordinator.py`, `tests/test_machine.py`.
- **Preconditions:** `M-B01-01`; 기존 plan/test/human transition 의미 유지.
- **Input Type / Validation:** `LoopState`, `TransitionSignal`, `LedgerView`, `LoopCounters`; limits positive.
- **Output Type / Validation:** `TransitionResult`; direct agreement는 adjudication state를 방문하지 않는다.
- **Exceptions:** Undefined pair는 `UndefinedTransitionError`.
- **Side Effects:** 없음.

#### Detailed Pseudocode

```text
1. TEST_GATE PASS or NOT_RUN transitions to REVIEW_CONTEXT_PREPARE.
2. CONTEXT_PREPARED transitions to CODE_REVIEW_A.
3. CODE_REVIEW_A ARTIFACT_OK transitions to CODE_REVIEW_B.
4. CODE_REVIEW_B ARTIFACT_OK transitions to REVIEW_COMPARE.
5. REVIEW_COMPARE AGREED transitions to CONSENSUS_EVALUATE.
6. REVIEW_COMPARE CONFLICT transitions to ADJUDICATE_A.
7. ADJUDICATE_A ARTIFACT_OK transitions to ADJUDICATE_B.
8. ADJUDICATE_B ARTIFACT_OK transitions to CONSENSUS_EVALUATE.
9. Preserve retry, escalation, abort, FIX, HUMAN_GATE limits.
10. Reject every unlisted state and signal combination.
```

- **Tests:** `T-B05-01` direct path; `T-B05-02` conflict path; `T-B05-03` undefined pairs; `T-B05-04` bounded termination and existing transitions.
- **Rollback:** enum과 table entries를 함께 제거해야 하며 partial rollback은 허용하지 않는다.

### M-B05-02 — Pending artifact promotion without ledger mutation

- **Parent:** `B-05`
- **Rationale:** 기존 `apply_worker_artifact()`는 artifact verification과 ledger update를 결합한다.
- **Objective:** Blind/adjudication worker completion은 typed artifact와 pending state만 commit하고 shared ledger는 pair application 전과 byte-equivalent하게 유지한다.
- **Target Files:** `orca_loop/coordinator.py`, `orca_loop/generation.py`, `run_loop.py`, `tests/test_coordinator.py`, `tests/test_ledger.py`.
- **Preconditions:** `M-B03-02`, `M-B03-03`, `M-B05-01`.
- **Input Type / Validation:** worker raw artifact, `ExpectedProvenance`, context, current `PendingReviewRound`, pre-round ledger digest.
- **Output Type / Validation:** promoted artifact digest and advanced pending stage; ledger content except generation field is unchanged.
- **Exceptions:** Artifact error uses existing retry path; pre-round ledger mismatch raises `CoordinatorContractError`.
- **Side Effects:** Canonical pending artifact와 history 기록, one generation commit.

#### Detailed Pseudocode

```text
1. Select blind or adjudication parser from current state.
2. Parse and validate raw worker artifact.
3. Atomically promote it to the state-specific canonical path.
4. Reread bytes and calculate artifact digest.
5. Verify current ledger digest equals pending pre-round ledger digest.
6. Update only the corresponding pending digest and stage.
7. Update matching validation-lineage artifact and snapshot fields.
8. Commit state with the existing ledger findings unchanged.
9. Emit ARTIFACT_OK for machine transition.
```

- **Tests:** `T-B05-05` A does not mutate ledger; `T-B05-06` B sees original scope; `T-B05-07` pending digest reread; `T-B05-08` ledger mismatch failure.
- **Rollback:** pending promotion path를 제거하면 신규 artifacts는 orphan audit files일 뿐 shared ledger에는 영향이 없다.

### M-B05-03 — Crash-safe pending round resume

- **Parent:** `B-05`
- **Rationale:** Context, A, B, comparison, adjudication 각 경계에서 process가 종료될 수 있다.
- **Objective:** committed pending evidence를 재사용하고 verified artifact를 redispatch하거나 ledger에 두 번 적용하지 않는다.
- **Target Files:** `run_loop.py`, `orca_loop/coordinator.py`, `orca_loop/generation.py`, `tests/test_resume.py`, `tests/test_worker_reconcile.py`.
- **Preconditions:** `M-B05-02`; existing mutation journal and worker reconciliation contract 유지.
- **Input Type / Validation:** committed state/ledger, pending stage, artifact files, Orca task/dispatch observation.
- **Output Type / Validation:** deterministic `ResumeDecision`; missing or contradictory evidence는 user decision 또는 typed resumable stop.
- **Exceptions:** Ambiguous live worker는 `ResumeAmbiguityError`/`ResumeBlockedError`; digest mismatch는 `AtomicWriteError` 또는 `ProvenanceError`.
- **Side Effects:** 필요할 때만 abandoned step event를 기록하며 duplicate external mutation을 만들지 않는다.

#### Detailed Pseudocode

```text
1. Load committed generation, pending round, and mutation journal.
2. Verify context and every stage-required artifact digest.
3. Reconcile any active task and dispatch with Orca authoritative state.
4. If a verified artifact already exists, advance from that committed boundary.
5. If a worker is live, adopt wait or block; never create a replacement worker.
6. If a settled step lacks promoted output, use existing bounded recovery.
7. Recreate only an unstarted next step with the original context.
8. Before pair application, require pre-round ledger digest unchanged.
9. Record one resume event and continue from the unique next state.
```

- **Tests:** `T-B05-09` crash after context; `T-B05-10` after A; `T-B05-11` after B/comparison; `T-B05-12` after each adjudicator; `T-B05-13` no duplicate dispatch/application.
- **Rollback:** 기존 generation과 mutation journal은 보존한다. 신규 state가 이미 committed된 run은 old binary로 resume하지 않고 current binary로 재검증한다.

---

## 10. B-06 — Deterministic Review Comparison

### M-B06-01 — Candidate normalization and exact signatures

- **Parent:** `B-06`
- **Rationale:** Semantic LLM deduplication 없이 repeatable candidate identity가 필요하다.
- **Objective:** Baseline finding, unilateral finding, coverage conflict를 stable `candidate_id`로 canonicalize한다.
- **Target Files:** `orca_loop/ledger.py`, `orca_loop/contracts.py`, `tests/test_ledger.py`.
- **Preconditions:** two valid blind artifacts and sealed context.
- **Input Type / Validation:** `Finding`, evaluation, baseline record; Unicode NFKC, whitespace와 Markdown punctuation normalization은 기존 helper를 재사용한다.
- **Output Type / Validation:** `ReviewConflictCandidate`; ordering은 `candidate_id.encode("utf-8")` ascending.
- **Exceptions:** Same finding ID with different exact signature는 `FINDING_SIGNATURE` candidate이며 자동 병합하지 않는다. Missing required action은 `LedgerIntegrityError`.
- **Side Effects:** 없음.

#### Detailed Pseudocode

```text
1. Key baseline findings by exact finding_id.
2. Build a normalized signature from root cause, required action, scope IDs, files, and tests.
3. Do not include a new finding_id in the semantic candidate signature.
4. Detect one-sided signatures as UNILATERAL_FINDING.
5. Detect same ID with different signature as FINDING_SIGNATURE.
6. Detect baseline decision differences and coverage differences by exact IDs.
7. Hash conflict kind plus normalized target fields into candidate_id.
8. Sort candidates deterministically and retain original evidence refs.
```

- **Tests:** `T-B06-01` stable normalization; `T-B06-02` one-sided finding; `T-B06-03` same-ID collision; `T-B06-04` deterministic order.
- **Rollback:** candidate helper만 제거하며 기존 `finding_signature()`는 유지한다.

### M-B06-02 — Pair comparison and canonical artifact

- **Parent:** `B-06`
- **Rationale:** Adjudication 여부를 third LLM이 아니라 coordinator가 결정해야 한다.
- **Objective:** complete agreement, conflict, invalid provenance를 exact matrix로 판정하고 `review_comparison.json`을 생성한다.
- **Target Files:** `orca_loop/ledger.py`, `orca_loop/coordinator.py`, `orca_loop/contracts.py`, `run_loop.py`, `tests/test_ledger.py`, `tests/test_coordinator.py`.
- **Preconditions:** `M-B06-01`; pending stage `BLIND_PAIR_READY`; pre-round ledger unchanged.
- **Input Type / Validation:** `BlindReviewArtifact` A/B, context, ledger; all provenance and coverage already valid but rechecked as pair.
- **Output Type / Validation:** `ReviewComparison`; `AGREED` iff candidate tuple empty, `ADJUDICATION_REQUIRED` iff nonempty, malformed pair is exception and never persisted as usable `INVALID` result.
- **Exceptions:** Provenance or coverage contradiction raises `InvalidRoundError`; atomic write failure raises `AtomicWriteError`.
- **Side Effects:** `artifacts/review_comparison.json`과 pending comparison digest commit.

#### Detailed Pseudocode

```text
1. Require both artifacts reference the same context, snapshot, plan, and round.
2. Require pending blind artifact digests match persisted bytes.
3. Require both baseline finding tuples and coverage tuples are complete.
4. Compare baseline decisions, evaluations, and normalized new findings.
5. Build candidates through M-B06-01.
6. Set AGREED only when no candidate remains.
7. Otherwise set ADJUDICATION_REQUIRED with the exact candidate tuple.
8. Build self-digested ReviewComparison.
9. Atomically write, reread, and commit pending COMPARISON_READY.
10. Emit AGREED or CONFLICT transition signal.
```

- **Tests:** `T-B06-05` complete agreement; `T-B06-06` baseline conflict; `T-B06-07` coverage conflict; `T-B06-08` unilateral finding; `T-B06-09` provenance invalid; `T-B06-10` deterministic JSON digest.
- **Rollback:** comparison artifact 생성 path를 제거한다. Pending blind artifacts와 pre-round ledger는 unchanged라 안전하게 round를 다시 시작할 수 있다.

### M-B06-03 — Atomic direct-agreement ledger application

- **Parent:** `B-06`
- **Rationale:** Direct agreement도 A와 B를 서로 다른 generation에 적용하면 partial pair가 된다.
- **Objective:** `AGREED` pair를 local ledger copy에 순서대로 적용하고 한 generation에서만 publish한다.
- **Target Files:** `orca_loop/ledger.py`, `orca_loop/coordinator.py`, `run_loop.py`, `tests/test_ledger.py`, `tests/test_coordinator.py`.
- **Preconditions:** valid comparison status `AGREED`; no adjudication fields populated.
- **Input Type / Validation:** pre-round `ConsensusLedger`, A/B artifacts, comparison and pending digests.
- **Output Type / Validation:** `LedgerUpdate` with both lane decisions and `committed_round=True`; state lineage consensus snapshot recorded in same commit.
- **Exceptions:** Intermediate apply error discards local copy and raises `LedgerIntegrityError`; digest mismatch rejects whole pair.
- **Side Effects:** Shared ledger와 state를 one `commit_generation()` transaction으로 갱신한다.
- **Existing Call-Site Treatment:** `execute_evaluate()`의 CODE-lane `commit_round()` 호출과 `expected_snapshot_digest=evidence.reviewed_snapshot_digest` 자기참조 검사는 제거한다. PLAN-lane `PLAN_CONSENSUS_EVALUATE`의 `commit_round()` 호출만 유지하고, CODE-lane `CONSENSUS_EVALUATE`는 `M-B06-03`에서 이미 원자 적용된 ledger의 unresolved count만 평가한다.

#### Detailed Pseudocode

```text
1. Verify comparison status is AGREED and candidate tuple is empty.
2. Verify pre-round ledger digest and all pending artifact digests.
3. Copy the pre-round ledger in memory.
4. Apply Blind A decisions to the local copy only.
5. Apply Blind B decisions to the resulting local copy only.
6. Run existing status, progress, and escalation calculations.
7. Increment code round exactly once.
8. Set consensus_snapshot_digest to the context snapshot.
9. Clear pending_review only in the same commit that publishes the ledger.
10. Transition to CONSENSUS_EVALUATE.
```

- **Tests:** `T-B06-11` one atomic generation; `T-B06-12` both sides present; `T-B06-13` intermediate error leaves shared ledger unchanged; `T-B06-14` code round increments once.
- **Rollback:** atomic application function을 제거해도 pre-round generation과 pending evidence가 남아 수동 재검증 가능하다.

---

## 11. B-07 — Symmetric Adjudication

### M-B07-01 — Canonical symmetric reveal package

- **Parent:** `B-07`
- **Rationale:** Adjudicator별 input을 따로 만들면 anchoring 제거와 input symmetry를 증명할 수 없다.
- **Objective:** sealed context, blind A/B, comparison으로 하나의 logical reveal manifest를 만들고 양쪽에 동일하게 제공한다.
- **Target Files:** `run_loop.py`, `orca_loop/dispatcher.py`, `orca_loop/models.py`, `tests/test_dispatcher.py`.
- **Preconditions:** `M-B06-02`; comparison status `ADJUDICATION_REQUIRED`.
- **Input Type / Validation:** verified context, blind artifacts, comparison; each persisted digest must equal pending state.
- **Output Type / Validation:** ordered `StagedInput` tuple and `reveal_manifest_digest`; peer adjudication output는 포함되지 않는다.
- **Exceptions:** Missing component or digest mismatch raises `CoordinatorContractError`; staging tamper raises existing dispatcher contract error.
- **Side Effects:** Adjudication step input directories와 pending reveal digest 기록.

#### Detailed Pseudocode

```text
1. Load the sealed review inputs by their context-bound paths.
2. Load Blind A, Blind B, and ReviewComparison by pending digests.
3. Build one canonical logical entry tuple.
4. Exclude adjudication_a.json and adjudication_b.json.
5. Hash canonical name and digest pairs as reveal_manifest_digest.
6. Stage the same logical tuple for Adjudicator A.
7. Stage the same logical tuple independently for Adjudicator B.
8. Require both logical digests equal the pending reveal digest.
```

- **Tests:** `T-B07-01` exact reveal list; `T-B07-02` A/B digest equality; `T-B07-03` peer output exclusion; `T-B07-04` tamper rejection.
- **Rollback:** reveal staging files는 run-local audit evidence로 남긴다. Shared ledger는 아직 변경되지 않는다.

### M-B07-02 — Isolated adjudicator dispatch and pending promotion

- **Parent:** `B-07`
- **Rationale:** Sequential execution 중 A 결과가 B prompt 또는 staged input으로 유입되면 symmetric review가 아니다.
- **Objective:** 기존 두 review worker를 같은 comparison에 각각 dispatch하되 B 시작 전 A artifact를 allowlist에서 차단한다.
- **Target Files:** `orca_loop/coordinator.py`, `run_loop.py`, `orca_loop/roles.py`, `tests/test_coordinator.py`, `tests/test_worker_reconcile.py`.
- **Preconditions:** `M-B07-01`, `M-B03-03`, provider/runtime binding preserved.
- **Input Type / Validation:** `RoleContext` with `ReviewPhase.ADJUDICATION`, lane, context digest, comparison digest, reveal digest.
- **Output Type / Validation:** verified A/B `AdjudicationArtifact` and pending stages; no ledger change before both are ready.
- **Exceptions:** Artifact retry uses bounded operational retry; live worker ambiguity blocks replacement; peer artifact presence is coordinator contract failure.
- **Side Effects:** 두 worker call은 conflict round에서만 발생하고 각각 pending artifact를 기록한다.

#### Detailed Pseudocode

```text
1. Dispatch lane A with the canonical reveal package.
2. Parse and promote A, then commit ADJUDICATION_A_READY without ledger change.
3. Build lane B inputs from the original reveal package, not the artifact directory scan.
4. Assert lane A adjudication output is absent from B inputs and prompt.
5. Dispatch lane B on the same shared mirror.
6. Parse and promote B, then commit ADJUDICATION_PAIR_READY.
7. Verify both artifacts reference the same context, comparison, snapshot, and candidates.
8. Route to deterministic merge; do not ask either model for a final vote.
```

- **Tests:** `T-B07-05` normal agreement makes zero adjudication calls; `T-B07-06` conflict makes exactly two; `T-B07-07` B excludes A result; `T-B07-08` crash resume does not duplicate calls.
- **Rollback:** 신규 dispatch states를 함께 제거해야 한다. Pending artifacts를 shared ledger로 자동 변환하지 않는다.

### M-B07-03 — Candidate disposition merge matrix

- **Parent:** `B-07`
- **Rationale:** 두 adjudication 결과를 majority나 coordinator 해석 없이 결합해야 한다.
- **Objective:** candidate별 exact pair matrix로 actionable, rejected, duplicate, unresolved 상태를 계산하고 한 generation에서 ledger에 적용한다.
- **Target Files:** `orca_loop/ledger.py`, `orca_loop/coordinator.py`, `tests/test_ledger.py`, `tests/test_coordinator.py`.
- **Preconditions:** valid adjudication pair; pre-round ledger and all pending digests unchanged.
- **Input Type / Validation:** `ReviewConflictCandidate`, lane A/B `CandidateDecision`, blind pair, pre-round ledger.
- **Output Type / Validation:** final `LedgerUpdate`, audit disposition tuple, escalations; code round increments once.
- **Exceptions:** Invalid duplicate target raises `LedgerIntegrityError`; digest/provenance mismatch raises `InvalidRoundError`.
- **Side Effects:** Final ledger, lineage, reports를 one committed generation에서 갱신한다.

#### Detailed Pseudocode

```text
1. Match both adjudication decisions to each candidate_id.
2. If both CONFIRM, include one canonical finding and both lane decisions.
3. If both REJECT, keep the candidate in audit history and exclude it from implementation scope.
4. If both DUPLICATE with the same duplicate_of, attach evidence to that canonical finding.
5. If either says VERIFY_REQUIRED, retain an unresolved VERIFY_REQUIRED finding.
6. For every other disagreement, retain an unresolved candidate with both decisions.
7. Apply agreed non-conflicting blind decisions to a local ledger copy.
8. Apply final candidate dispositions to the same local copy.
9. Run existing B1-B5, impact_class, repeated-signature, and round-limit escalation logic.
10. Commit ledger, consensus snapshot, and cleared pending state atomically.
```

- **Tests:** `T-B07-09` dual confirm; `T-B07-10` dual reject audit-only; `T-B07-11` same-target duplicate; `T-B07-12` conflicting duplicate; `T-B07-13` VERIFY_REQUIRED; `T-B07-14` existing escalation semantics; `T-B07-15` atomic failure rollback.
- **Rollback:** failed merge는 pre-round committed ledger를 유지한다. Audit files는 삭제하지 않으며 retry에 사용한다.

---

## 12. B-08 — Provider Diversity and Manifest Policy

### M-B08-01 — New-run provider diversity preflight

- **Parent:** `B-08`
- **Rationale:** `WorkerKey`와 `Side` 이름은 runtime provider를 증명하지 않는다.
- **Objective:** resolved `CLAUDE_CODE_REVIEW`와 `CODEX_REVIEW` provider가 다르지 않으면 explicit escape hatch 없는 신규 run을 mutation 전에 차단한다.
- **Target Files:** `orca_loop/config.py`, `run_loop.py`, `tests/test_cli.py`, `tests/test_cli_commands.py`.
- **Preconditions:** Agent runtime resolution complete; Orca task/terminal mutation not started.
- **Input Type / Validation:** `AgentRuntimeConfig`, boolean `allow_same_provider_consensus`; both review worker records required.
- **Output Type / Validation:** `ConsensusIndependence.FULL` 또는 authorized `DEGRADED`; planning pair diversity는 informational note다.
- **Exceptions:** Missing review worker or unauthorized same provider raises `PreflightError`.
- **Side Effects:** 실패 시 없음. 성공 후 기존 start lifecycle을 계속한다.

#### Detailed Pseudocode

```text
1. Add --allow-same-provider-consensus as a store_true start option.
2. Resolve all worker providers through the existing runtime catalog.
3. Read the two code-review worker providers by WorkerKey.
4. If providers differ, return FULL and DIVERSE policy.
5. If equal and flag is true, return DEGRADED and EXPLICIT_SAME_PROVIDER policy.
6. If equal and flag is false, raise PreflightError before Orca mutation.
7. Emit a non-blocking planning-pair diversity note when applicable.
```

- **Tests:** `T-B08-01` different provider pass; `T-B08-02` same provider block; `T-B08-03` explicit override; `T-B08-04` no automatic provider substitution.
- **Rollback:** CLI flag와 preflight check를 함께 제거한다. Existing provider/model/effort options는 변경하지 않는다.

### M-B08-02 — Run manifest policy and legacy migration

- **Parent:** `B-08`
- **Rationale:** Resume이 operator의 현재 CLI default로 policy를 재해석하면 안 된다.
- **Objective:** manifest current schema에 provider policy와 independence status를 기록하고 legacy run을 `LEGACY_UNSPECIFIED`로 복원한다.
- **Target Files:** `orca_loop/runspec.py`, `orca_loop/config.py`, `tests/test_runspec.py`, `tests/test_resume.py`.
- **Preconditions:** `M-B08-01`; manifest identity/input digest rules 유지.
- **Input Type / Validation:** manifest schema v1/v2/current; policy enum과 status exact.
- **Output Type / Validation:** current `RunManifest`; legacy different-provider는 `FULL`, legacy same-provider는 resumable `DEGRADED` and `LEGACY_UNSPECIFIED`.
- **Exceptions:** Unknown schema/policy or inconsistent persisted status raises `ManifestError`.
- **Side Effects:** 신규 manifest write; legacy file는 다음 authorized update에서만 current schema로 rewrite한다.

#### Detailed Pseudocode

```text
1. Bump manifest schema and retain v1/v2 parsers.
2. Persist consensus_provider_policy and consensus_independence for new runs.
3. On legacy parse, derive provider equality from recorded agents.
4. Assign LEGACY_UNSPECIFIED and derived FULL or DEGRADED.
5. Restore the recorded policy through manifest_to_arguments on resume.
6. Ignore a missing resume CLI flag because persisted policy is authoritative.
7. Reject any attempt to override the policy of an existing run.
8. Serialize and reread the current manifest exactly.
```

- **Tests:** `T-B08-05` current round trip; `T-B08-06` legacy diverse; `T-B08-07` legacy same provider degraded; `T-B08-08` resume restore; `T-B08-09` resume drift rejection.
- **Rollback:** current manifests require current code; existing v1/v2 files remain readable. No automatic downgrade write is attempted.

### M-B08-03 — Dry-run, status, and diagnostic visibility

- **Parent:** `B-08`
- **Rationale:** Explicit exception이 durable하더라도 operator에게 보이지 않으면 독립성을 오해할 수 있다.
- **Objective:** dry-run/status/final report에 worker provider/model/effort, policy, `FULL`/`DEGRADED`를 표시한다.
- **Target Files:** `run_loop.py`, `orca_loop/reporting.py`, `tests/test_cli_commands.py`, `tests/test_reporting.py`.
- **Preconditions:** `M-B08-02`.
- **Input Type / Validation:** `PreflightResult` 또는 `RunManifest`; missing legacy value를 숨기지 않는다.
- **Output Type / Validation:** JSON-safe diagnostic fields와 Markdown summary; `DEGRADED`는 warning으로 명시한다.
- **Exceptions:** Status는 malformed manifest를 기존 failure boundary로 보고하며 policy를 추측하지 않는다.
- **Side Effects:** Console/JSON/report output only.

#### Detailed Pseudocode

```text
1. Resolve review worker runtime records from preflight or manifest.
2. Build a diagnostic object with both provider, model, effort values.
3. Add policy and independence status.
4. Emit the same object in dry-run and status JSON.
5. Render DEGRADED with its explicit or legacy reason.
6. Never label wire Side values as providers.
```

- **Tests:** `T-B08-10` FULL output; `T-B08-11` explicit DEGRADED output; `T-B08-12` legacy reason output.
- **Rollback:** diagnostics fields는 additive이므로 renderer와 assertions만 되돌린다.

---

## 13. B-09 — Evidence Invalidation, Resume, and Final Merge Guard

### M-B09-01 — Evidence-aware drift classifier

- **Parent:** `B-09`
- **Rationale:** Read-only 여부는 기존 evidence validity를 판단하지 못한다.
- **Objective:** current state와 `blocked_from_state`를 approved drift table에 매핑하고 단순 blanket rebaseline을 제거한다.
- **Target Files:** `run_loop.py`, `orca_loop/models.py`, `tests/test_resume.py`.
- **Preconditions:** `ValidationLineage` available; terminal state behavior 유지.
- **Input Type / Validation:** `CoordinatorState`, current `SnapshotIdentity`, boolean accept; `USER_DECISION_REQUIRED`는 effective blocked state가 필수다.
- **Output Type / Validation:** `DriftDecision`에 `DriftAction`, rollback target, current digest, detail을 포함한다.
- **Exceptions:** Evidence-bearing drift without acceptance raises `ResumeBlockedError`; unknown group fails closed.
- **Side Effects:** Classifier 자체는 없음.

#### Detailed Pseudocode

```text
1. Capture current snapshot and compare with state.snapshot_digest.
2. If equal, return no-drift continuation.
3. Resolve USER_DECISION_REQUIRED through blocked_from_state.
4. PLAN and PLAN_REVISE return REBASELINE.
5. PLAN_REVIEW and PLAN_CONSENSUS_EVALUATE return BLOCK unless accepted, then INVALIDATE_PLAN.
6. IMPLEMENT and FIX return BLOCK unless accepted, then REBASELINE with downstream clear.
7. TEST_GATE and every later validation state return BLOCK unless accepted, then INVALIDATE_CODE.
8. Unknown or insufficient legacy state returns BLOCK.
9. Preserve exact old and new digest detail in the decision.
```

- **Tests:** `T-B09-01` plan author auto rebaseline; `T-B09-02` plan evidence block; `T-B09-03` accepted plan invalidation; `T-B09-04` implement/fix explicit path; `T-B09-05` validation-state block; `T-B09-06` blocked-state routing.
- **Rollback:** classifier를 되돌리면 safety regression이므로 Phase 4 전체 rollback으로만 허용한다.

### M-B09-02 — Lineage invalidation and rollback transitions

- **Parent:** `B-09`
- **Rationale:** Accepted drift는 old evidence digest를 new snapshot으로 relabel하지 않아야 한다.
- **Objective:** action별로 exact evidence fields와 pending/gate state를 clear하고 approved target state로 한 generation commit한다.
- **Target Files:** `orca_loop/coordinator.py`, `run_loop.py`, `orca_loop/generation.py`, `tests/test_resume.py`, `tests/test_coordinator.py`.
- **Preconditions:** `M-B09-01`; current snapshot captured once for commit.
- **Input Type / Validation:** `DriftDecision`, current state/ledger; existing artifact files remain audit history.
- **Output Type / Validation:** new `CoordinatorState`; no old lineage digest equals new snapshot unless it was independently regenerated.
- **Exceptions:** Commit failure leaves prior generation authoritative; stale gate invalidation failure stops before state transition.
- **Side Effects:** New generation, resume event, stale local gate/notice invalidation.

#### Detailed Pseudocode

```text
1. For REBASELINE in PLAN states, update state snapshot only.
2. For INVALIDATE_PLAN, clear plan-review applicability, gate binding, pending escalations, and route to PLAN_REVISE.
3. For accepted IMPLEMENT or FIX drift, keep the authoring state and clear all validation lineage and pending review.
4. For INVALIDATE_CODE, clear test through consensus lineage and pending review.
5. Invalidate any final gate through M-B09-04.
6. Route INVALIDATE_CODE to TEST_GATE with the current snapshot.
7. Commit state and unchanged ledger atomically.
8. Append an event containing action, old digest, new digest, and cleared fields.
```

- **Tests:** `T-B09-07` no evidence relabel; `T-B09-08` plan route; `T-B09-09` implement/fix clear; `T-B09-10` code route; `T-B09-11` commit failure preserves prior generation; `T-B09-12` artifact history retained.
- **Rollback:** prior committed generation remains recoverable. Artifact files are never destructively removed.

### M-B09-03 — Final gate and merge qualification guard

- **Parent:** `B-09`
- **Rationale:** Gate 생성 전 검사만으로는 gate 대기 중 drift인 TOCTOU를 막을 수 없다.
- **Objective:** final gate 생성 직전과 merge decision 적용 직전에 동일 guard를 실행하고 모든 lineage/artifact digest를 검증한다.
- **Target Files:** `run_loop.py`, `orca_loop/coordinator.py`, `orca_loop/contracts.py`, `tests/test_resume.py`, `tests/test_escalation.py`.
- **Preconditions:** consensus unresolved count zero; current non-legacy lineage complete.
- **Input Type / Validation:** worktree snapshot, state, lineage, context/test/review/comparison/adjudication artifact files, gate binding.
- **Output Type / Validation:** Section 3.6의 exact `MergeQualification`; `qualified=True`는 모든 digest와 live snapshot 검증 후에만 허용한다.
- **Exceptions:** Missing/mismatched evidence raises typed resumable `EvidenceDriftError`; legacy incomplete lineage is unqualified.
- **Side Effects:** Pre-gate success binds snapshot/context/lineage digest into `GateBinding`; pre-merge success alone permits transition.

#### Detailed Pseudocode

```text
1. Capture the live worktree snapshot.
2. Require live equals state, test gate, review context, Blind A, and Blind B snapshots.
3. If adjudication was used, require both adjudication snapshots equal live.
4. Require consensus snapshot equals live.
5. Reread every required artifact and verify its recorded digest.
6. Hash the complete ValidationLineage and evidence digest tuple.
7. Before gate creation, store snapshot, context, and lineage digest in GateBinding.
8. After a merge decision arrives, capture live snapshot again.
9. Re-run all checks and require GateBinding values still match.
10. Only then call the existing merge transition.
```

- **Tests:** `T-B09-13` complete qualification; `T-B09-14` each snapshot mismatch; `T-B09-15` each artifact tamper; `T-B09-16` drift after gate; `T-B09-17` legacy gate revalidation; `T-B09-18` non-merge decisions preserve existing routes.
- **Rollback:** guard failure는 state를 merge-ready로 변경하지 않는다. Old gate는 `M-B09-04`로 invalidate한다.

### M-B09-04 — Stale gate and durable notice invalidation

- **Parent:** `B-09`
- **Rationale:** Local binding만 clear하면 `user-decision-request.json`이 계속 pending authority로 남는다.
- **Objective:** evidence drift로 무효가 된 gate/notice를 human decision과 구분되는 `INVALIDATED` 상태로 기록하고 이후 resolution을 transition에 사용하지 않는다.
- **Target Files:** `orca_loop/models.py`, `orca_loop/escalation.py`, `run_loop.py`, `tests/test_escalation.py`, `tests/test_resume.py`.
- **Preconditions:** existing gate binding 또는 pending notice; exact request/gate/report identity.
- **Input Type / Validation:** binding, invalidation reason, old/new snapshot digest; notice schema current or supported legacy.
- **Output Type / Validation:** `UserDecisionNoticeStatus.INVALIDATED`, terminal timestamp, bounded reason; local binding cleared after notice write succeeds.
- **Exceptions:** Identity mismatch raises `GateProtocolError`; write failure blocks invalidation transition.
- **Side Effects:** Notice file update와 best-effort board comment; external stale gate를 destructive하게 삭제하지 않는다.

#### Detailed Pseudocode

```text
1. Load the notice and require it matches the active binding.
2. If already RESOLVED or INVALIDATED, verify idempotent identity and return.
3. Write INVALIDATED with timestamp and evidence-drift reason atomically.
4. Record old and new snapshot digests without a synthetic human decision.
5. Clear local gate binding in the following state generation.
6. Ignore any later external resolution because no active binding matches it.
7. Create a new gate only after new validation lineage is complete.
```

- **Tests:** `T-B09-19` pending to invalidated; `T-B09-20` idempotent repeat; `T-B09-21` late resolution ignored; `T-B09-22` write failure leaves binding authoritative.
- **Rollback:** notice schema rollback requires keeping current reader support. Existing invalidated notices must not be interpreted as pending.

---

## 14. B-10 — Reporting and Audit Visibility

### M-B10-01 — Current contract renderer corrections

- **Parent:** `B-10`
- **Rationale:** Valid current artifacts can silently fail best-effort rendering.
- **Objective:** three confirmed schema mismatches를 고치고 valid artifact가 `reporting.log`를 만들지 않게 한다.
- **Target Files:** `orca_loop/reporting.py`, `tests/test_reporting.py`.
- **Preconditions:** 기존 best-effort failure isolation 유지.
- **Input Type / Validation:** decoded plan/implementation JSON object; command object fields exact.
- **Output Type / Validation:** Markdown with `verification_method`, `evidence_refs`, explicit command fields.
- **Exceptions:** Future malformed optional value는 best-effort log; current valid shape는 exception 없음.
- **Side Effects:** Stage Markdown rewrite only.

#### Detailed Pseudocode

```text
1. Render acceptance criteria from criterion_id and verification_method.
2. Render addressed findings from finding_id and joined evidence_refs.
3. Render each test command as argv JSON, cwd, timeout_ms, and kind.
4. Preserve deterministic item order.
5. Write through the existing atomic text helper.
6. Assert no reporting.log entry is created for valid current data.
```

- **Tests:** `T-B10-01` criterion text; `T-B10-02` addressed evidence; `T-B10-03` command object; `T-B10-04` no reporting error.
- **Rollback:** 세 renderer hunk와 fixtures만 되돌린다.

### M-B10-02 — Blind, comparison, and adjudication stage reports

- **Parent:** `B-10`
- **Rationale:** Operator가 direct agreement와 adjudication 경로를 구분할 수 있어야 한다.
- **Objective:** 신규 artifact별 immutable history와 current Markdown report를 제공한다.
- **Target Files:** `orca_loop/reporting.py`, `run_loop.py`, `tests/test_reporting.py`.
- **Preconditions:** `M-B03-02`, `M-B06-02`, `M-B07-03`.
- **Input Type / Validation:** strict artifact raw JSON and generation.
- **Output Type / Validation:** coverage counts, context/snapshot digest, conflict candidates, dispositions, providers를 포함한 stage reports.
- **Exceptions:** Missing optional future section은 `미확인`; `NOT_RUN`은 PASS로 표현하지 않는다.
- **Side Effects:** `reports/`와 `artifacts/history/` files.

#### Detailed Pseudocode

```text
1. Register report filenames for Blind A, Blind B, comparison, and both adjudications.
2. Render lane and runtime provider as separate fields.
3. Render context and snapshot provenance.
4. Render acceptance, file, and test coverage totals and decisions.
5. Render comparison status and each exact candidate.
6. Render adjudication dispositions and unresolved conflicts.
7. Label rejected candidates as audit-only.
8. Preserve best-effort behavior after durable commits.
```

- **Tests:** `T-B10-05` blind coverage report; `T-B10-06` direct comparison; `T-B10-07` adjudication report; `T-B10-08` NOT_RUN boundary.
- **Rollback:** 신규 report registrations와 renderers만 제거하며 machine evidence는 유지한다.

### M-B10-03 — Run summary and final decision evidence

- **Parent:** `B-10`
- **Rationale:** Human gate report가 merge snapshot과 independence를 직접 보여줘야 한다.
- **Objective:** status/summary/user-decision report에 lineage, provider status, review path, qualification을 표시한다.
- **Target Files:** `orca_loop/reporting.py`, `orca_loop/escalation.py`, `run_loop.py`, `tests/test_reporting.py`, `tests/test_escalation.py`.
- **Preconditions:** `M-B08-03`, `M-B09-03`.
- **Input Type / Validation:** state, ledger, manifest policy, validation lineage, merge qualification.
- **Output Type / Validation:** final report에서 current/test/review/consensus/gate digest와 artifact verification status가 명시된다.
- **Exceptions:** Missing lineage renders `UNQUALIFIED`, never `READY`.
- **Side Effects:** Summary and gate report rewrite; report digest then binds to gate.

#### Detailed Pseudocode

```text
1. Read current state, ledger, manifest, and lineage.
2. Render both review worker provider, model, and effort values.
3. Render FULL or DEGRADED with policy reason.
4. Render direct comparison or adjudication path.
5. Render every applicable snapshot digest in one table.
6. Render artifact digest verification and test status.
7. Mark missing or mismatched evidence UNQUALIFIED.
8. Build the gate report only from a successful MergeQualification.
9. Digest the report and bind it through the existing gate protocol.
```

- **Tests:** `T-B10-09` FULL direct path; `T-B10-10` DEGRADED adjudicated path; `T-B10-11` lineage table; `T-B10-12` unqualified legacy; `T-B10-13` report/gate digest binding.
- **Rollback:** reporting changes are additive; merge guard remains authoritative even if rendering is rolled back.

---

## 15. B-11 — Reviewer Prompt and Plan Review Quality

### M-B11-01 — Blind reviewer prompt contracts

- **Parent:** `B-11`
- **Rationale:** Secondary prompt의 prior-review agreement language가 anchoring을 유도한다.
- **Objective:** 두 blind prompt가 peer를 언급하지 않고 identical evidence-first review 절차와 structured output을 요구한다.
- **Target Files:** `prompts/code_reviewer.md`, `prompts/cross_confirmer.md`, `orca_loop/roles.py`, `tests/test_contracts.py`.
- **Preconditions:** `M-B03-02`, `M-B04-01`.
- **Input Type / Validation:** blind `RoleContext`; only blind allowlist placeholders.
- **Output Type / Validation:** complete strict `BlindReviewArtifact`; `agrees_with_reviewer` absent.
- **Exceptions:** Peer keyword/input reference or unresolved placeholder raises `TemplateContractError`.
- **Side Effects:** Worker contract text changes only.

#### Detailed Pseudocode

```text
1. Replace confirmer agreement instructions with independent evidence-first instructions.
2. Require repository, frozen diff, test evidence, and scope coverage review before verdict.
3. Require exact acceptance, file, test evaluation arrays.
4. Require findings not present in baseline scope to be emitted independently.
5. State that lane and provider are different concepts.
6. Preserve read-only, no-test, no-permission-change prohibitions.
7. Validate all placeholders against the blind allowlist.
```

- **Tests:** `T-B11-01` no peer artifact reference; `T-B11-02` exact coverage instructions; `T-B11-03` role/provider distinction; `T-B11-04` prohibition retention.
- **Rollback:** prompt files와 corresponding parser expectation을 같은 rollback unit으로 취급한다.

### M-B11-02 — Adjudication prompts and phase-aware routing

- **Parent:** `B-11`
- **Rationale:** 같은 worker가 blind와 adjudication 역할을 수행하므로 role-only template selection은 부족하다.
- **Objective:** `Role + ReviewPhase + ReviewLane`으로 template, artifact filename, placeholders를 고르고 양쪽 prompt가 동일 reveal contract를 갖게 한다.
- **Target Files:** `orca_loop/roles.py`, `run_loop.py`, `prompts/code_reviewer.md`, `prompts/cross_confirmer.md`, `tests/test_coordinator.py`, `tests/test_dispatcher.py`.
- **Preconditions:** `M-B07-01`; 새 unprefixed prompt file을 만들지 않고 existing prompt에 phase sections를 두거나 existing file mapping을 재사용한다.
- **Input Type / Validation:** extended `RoleContext` with phase/lane/context/comparison/reveal digests.
- **Output Type / Validation:** rendered contract and exact state-specific artifact filename.
- **Exceptions:** Invalid phase-role-lane combination or missing candidate placeholder raises `TemplateContractError`.
- **Side Effects:** Contract bytes와 digest가 step input에 기록된다.

#### Detailed Pseudocode

```text
1. Extend RoleContext with nullable review phase, lane, context, comparison, and reveal digests.
2. Require all fields for review roles and reject them for unrelated roles.
3. Select blind or adjudication section deterministically.
4. Map lane A/B to state-specific artifact filenames.
5. Render the exact candidate tuple for adjudication.
6. Require evidence for every candidate decision.
7. Assert no peer adjudication output placeholder exists.
8. Hash and stage the rendered contract.
```

- **Tests:** `T-B11-05` phase mapping; `T-B11-06` exact filenames; `T-B11-07` identical reveal placeholders; `T-B11-08` peer exclusion; `T-B11-09` unresolved placeholder rejection.
- **Rollback:** RoleContext additions에는 defaults를 유지해 non-review roles와 legacy tests를 보존한다.

### M-B11-03 — Bounded Plan Reviewer verification matrix

- **Parent:** `B-11`
- **Rationale:** `affected_files`는 implementer write boundary이므로 staged plan 주장만으로 승인하면 부족하다.
- **Objective:** Plan Reviewer가 approved bounded categories를 source에서 검증하고 complete `PlanVerification` tuple을 제출한다.
- **Target Files:** `prompts/plan_reviewer.md`, `orca_loop/models.py`, `orca_loop/contracts.py`, `tests/test_contracts.py`, `tests/test_plan_traceability.py`.
- **Preconditions:** plan artifact staged; read-only mirror available; planner와 reviewer의 비대칭 책임 유지.
- **Input Type / Validation:** exact seven `PlanVerification` categories, `DecisionValue`, evidence refs.
- **Output Type / Validation:** plan review artifact current schema; `APPROVE` requires every category APPROVE with evidence.
- **Exceptions:** Missing category or replacement-plan content violates contract; factual evidence mismatch uses actionable finding.
- **Side Effects:** Plan review artifact schema/prompt change; repository remains read-only.

#### Detailed Pseudocode

```text
1. Stage the plan and bounded repository mirror.
2. Verify affected_files completeness and operations against repository structure.
3. Verify integration points and existing public interfaces.
4. Verify acceptance criteria and exact test policy/test contract equality.
5. Verify repository factual claims and security/data/API/schema impact.
6. Record one PlanVerification for every required category.
7. Emit findings for unsupported or incomplete claims.
8. Reject APPROVE unless all categories approve with evidence.
9. Prohibit replacement architecture or rewritten plan output.
```

- **Tests:** `T-B11-10` complete matrix; `T-B11-11` missing category; `T-B11-12` evidence-free approve; `T-B11-13` prompt bounded source-read and no replacement plan.
- **Rollback:** plan-review schema와 prompt를 함께 되돌린다. Existing plan artifact schema는 유지한다.

---

## 16. B-12 — Operations and Permission Documentation

### M-B12-01 — Code-authoritative operator guide update

- **Parent:** `B-12`
- **Rationale:** 현재 guide의 coordinator terminal과 runtime confirmation 설명은 actual code보다 오래됐다.
- **Objective:** `start`, worker provisioning, resume, provider policy, evidence drift, final gate 절차를 current implementation 기준으로 개정한다.
- **Target Files:** `orca_loop_execution_rules.md`.
- **Preconditions:** `B-08`~`B-11` implementation behavior fixed.
- **Input Type / Validation:** actual CLI help, `_start_argv()`, manifest restore, state transition behavior.
- **Output Type / Validation:** exact commands/options/states; lane/provider와 `PASS`/`NOT_RUN` boundary가 분리된다.
- **Exceptions:** 문서가 code invariant와 다르면 validation failure이며 runtime exception은 없다.
- **Side Effects:** Documentation only.

#### Detailed Pseudocode

```text
1. Derive start and resume syntax from the current parser and argv builders.
2. State that the current Orca terminal is the coordinator terminal.
3. State that four worker terminals are provisioned by the harness.
4. Describe runtime defaults separately from optional operator override.
5. Document blind review, conditional adjudication, and provider escape hatch.
6. Document state-specific drift invalidation and gate recreation.
7. Document NOT_RUN and host-sandbox boundaries without overclaiming.
8. Cross-check every command and state name against source.
```

- **Tests:** `T-B12-01` coordinator terminal statement; `T-B12-02` worker provisioning; `T-B12-03` provider policy; `T-B12-04` drift/NOT_RUN text.
- **Rollback:** documentation diff만 되돌릴 수 있으나 code와 불일치한 old guide를 최종 산출물로 허용하지 않는다.

### M-B12-02 — Permission environment documentation alignment

- **Parent:** `B-12`
- **Rationale:** `environment.py` docstring은 CLI major.minor blocking을 설명하지만 code와 approved policy는 warning-only다.
- **Objective:** `compare_environment()` behavior를 바꾸지 않고 module/function comments를 platform/enforcement blocking, CLI drift informational로 수정한다.
- **Target Files:** `orca_loop/environment.py`, `tests/test_environment.py`.
- **Preconditions:** User decision 16 fixed; typed observed permission failure marker policy unchanged.
- **Input Type / Validation:** `PermissionEnvironment`; current comparison and notes output.
- **Output Type / Validation:** Accurate docstrings; `_minor()`가 unused면 behavior-neutral removal 여부는 Phase 4 diff에서 최소 변경으로 결정한다.
- **Exceptions:** 없음.
- **Side Effects:** Runtime behavior 없음.

#### Detailed Pseudocode

```text
1. Replace the major.minor blocking claim in the module documentation.
2. State that platform and enforcement_digest invalidate proof.
3. State that CLI availability/version differences produce notes only.
4. Preserve compare_environment and environment_notes control flow.
5. Preserve observed typed permission failure marker semantics.
6. Run focused environment tests to prove behavior did not change.
```

- **Tests:** `T-B12-05` platform block; `T-B12-06` enforcement block; `T-B12-07` CLI drift note-only.
- **Rollback:** docstring/comment-only changes are independently reversible.

### M-B12-03 — Static documentation policy assertions

- **Parent:** `B-12`
- **Rationale:** Operator documentation이 다시 stale해지는 것을 빠르게 탐지해야 한다.
- **Objective:** safety-critical statements를 source behavior와 함께 검증하는 bounded static tests를 추가한다.
- **Target Files:** `tests/test_environment.py`, `tests/test_cli_commands.py`, `tests/test_plan_traceability.py`, `orca_loop_execution_rules.md`.
- **Preconditions:** `M-B12-01`, `M-B12-02`.
- **Input Type / Validation:** UTF-8 document text and parser/help output.
- **Output Type / Validation:** Assertions for current terminal source, provider flag, warning-only CLI drift, blind input/gate semantics.
- **Exceptions:** Missing document or required statement is a test failure.
- **Side Effects:** Test reads only.

#### Detailed Pseudocode

```text
1. Read the operator guide as UTF-8.
2. Assert required option and state names match parser constants.
3. Assert stale claims about creating a coordinator terminal are absent.
4. Assert CLI drift is described as informational.
5. Assert lane names are not described as runtime providers.
6. Assert NOT_RUN is not described as executed PASS.
7. Keep assertions semantic and bounded, not full-document snapshots.
```

- **Tests:** `T-B12-08` operator lifecycle assertions; `T-B12-09` permission wording; `T-B12-10` provider/NOT_RUN wording.
- **Rollback:** 신규 static tests와 corresponding documentation change를 같은 rollback unit으로 취급한다.

---

## 17. B-13 — Integrated Validation and Recovery Regression

### M-B13-01 — Focused contract and component regression

- **Parent:** `B-13`
- **Rationale:** Full suite 전에 각 new trust boundary를 빠르게 격리해 검증해야 한다.
- **Objective:** `T-B01-*`부터 `T-B12-*`까지 mapping된 focused tests가 모두 통과한다.
- **Target Files:** `tests/test_contracts.py`, `tests/test_coordinator.py`, `tests/test_dispatcher.py`, `tests/test_ledger.py`, `tests/test_machine.py`, `tests/test_readonly.py`, `tests/test_reporting.py`, `tests/test_runspec.py`, `tests/test_environment.py`.
- **Preconditions:** Corresponding production/document micro blocks complete.
- **Input Type / Validation:** Temporary repositories, strict JSON fixtures, fake Orca client; live tokens/network 사용 없음.
- **Output Type / Validation:** unittest PASS with each trust boundary independently asserted.
- **Exceptions:** First unexplained failure enters staged-development failure investigation gate.
- **Side Effects:** Temporary directories only.

#### Detailed Pseudocode

```text
1. Run model, contract, and migration tests.
2. Run test evidence, context, mirror, and input-isolation tests.
3. Run state, pending lifecycle, comparison, and adjudication tests.
4. Run provider, drift, final guard, reporting, prompt, and documentation tests.
5. Stop on a caused failure and identify the first expected/observed divergence.
6. Fix only the responsible micro block and rerun its focused module.
7. Record exact commands and final-state outputs.
```

- **Tests:** `T-B13-01` contract group; `T-B13-02` orchestration group; `T-B13-03` policy/reporting group.
- **Rollback:** Failing production micro block is reverted or corrected before proceeding; tests are not weakened to accept unsafe behavior.

### M-B13-02 — Crash, resume, and final-gate integration matrix

- **Parent:** `B-13`
- **Rationale:** Cross-block durability는 unit-level parser tests만으로 증명되지 않는다.
- **Objective:** Every new committed boundary와 drift state group을 fake orchestration integration으로 재현한다.
- **Target Files:** `tests/test_resume.py`, `tests/test_worker_reconcile.py`, `tests/test_coordinator.py`, `tests/fakes.py`.
- **Preconditions:** `M-B13-01` focused tests pass.
- **Input Type / Validation:** deterministic fake task/dispatch/gate responses, temporary Git worktree, persisted generations.
- **Output Type / Validation:** no duplicate dispatch, no partial ledger pair, no stale merge, exact resume state.
- **Exceptions:** Ambiguous fake state must yield the same typed block as production.
- **Side Effects:** Temporary run/control/artifact trees.

#### Detailed Pseudocode

```text
1. Execute a direct-agreement run through final gate.
2. Repeat with process stops after context, Blind A, Blind B, and comparison.
3. Execute a conflict run and stop after each adjudicator.
4. Resume each run and count task, dispatch, artifact, generation, and ledger applications.
5. Inject drift at plan review, test gate, comparison, consensus, and human gate.
6. Verify default block and explicit invalidation routes.
7. Resolve a stale gate after drift and prove it cannot transition the run.
8. Load legacy state and prove final merge requires fresh validation.
```

- **Tests:** `T-B13-04` direct recovery; `T-B13-05` adjudication recovery; `T-B13-06` drift matrix; `T-B13-07` stale gate; `T-B13-08` legacy requalification.
- **Rollback:** Integration fixture는 production data를 수정하지 않는다. Failure 시 Phase 4 completion을 중단한다.

### M-B13-03 — Full final validation and evidence matrix

- **Parent:** `B-13`
- **Rationale:** 마지막 change 이후 fresh full-suite evidence가 필요하다.
- **Objective:** approved validation commands를 final filesystem state에서 실행하고 block/test traceability를 implementation report에 기록한다.
- **Target Files:** repository-wide changed files and Phase 4 implementation report artifact.
- **Preconditions:** 모든 focused/integration test PASS; final code/doc change complete.
- **Input Type / Validation:** final worktree, baseline HEAD, approved block/test IDs.
- **Output Type / Validation:** exact command result classification and changed-file scope audit.
- **Exceptions:** Environment limitation은 `BLOCKED`, executed failure는 `FAIL`; live E2E absence는 `NOT RUN`.
- **Side Effects:** Test caches와 temporary files only; no live worker call.

#### Detailed Pseudocode

```text
1. Capture final git status and diff.
2. Map every changed file to approved Micro Block IDs.
3. Run py -3 -m unittest discover -s tests -v.
4. Run py -3 run_loop.py doctor.
5. Run git diff --check.
6. Recheck current state contracts and documentation assertions.
7. Compare full suite with the 382-test Phase 1 baseline.
8. Classify every command PASS, FAIL, BLOCKED, or NOT RUN.
9. Record live Orca E2E as NOT RUN unless separately authorized and executed.
10. Produce the Phase 4 report only from fresh final-state evidence.
```

- **Tests:** `T-B13-09` full unittest; `T-B13-10` doctor; `T-B13-11` diff/scope audit; `T-B13-12` evidence classification completeness.
- **Rollback:** Full validation은 mutation rollback을 수행하지 않는다. 실패 원인의 micro block으로 돌아가 최소 수정 후 전체 fresh validation을 반복한다.

---

## 18. Dependency and Implementation Order

```text
M-B01-01 -> M-B01-02 -> M-B01-03
     |
     +-> M-B02-01 -> M-B02-02 -> M-B02-03
     +-> M-B03-01 -> M-B03-02 -> M-B03-03
     +-> M-B08-01 -> M-B08-02 -> M-B08-03

M-B02-03 + M-B03-03
     -> M-B04-01 -> M-B04-02 -> M-B04-03
     -> M-B05-01 -> M-B05-02 -> M-B05-03
     -> M-B06-01 -> M-B06-02 -> M-B06-03
     -> M-B07-01 -> M-B07-02 -> M-B07-03
     -> M-B09-01 -> M-B09-04 -> M-B09-02 -> M-B09-03

M-B06-03 + M-B07-03 + M-B08-03 + M-B09-04
     -> M-B10-01 -> M-B10-02 -> M-B10-03
     -> M-B11-01 -> M-B11-02 -> M-B11-03
     -> M-B12-01 -> M-B12-02 -> M-B12-03
     -> M-B13-01 -> M-B13-02 -> M-B13-03
```

Phase 4에서는 같은 layer 안의 파일 변경도 위 순서대로 통합한다. Persistent
contract와 migration test가 PASS하기 전에는 state machine consumer를 수정하지
않는다. §18 그래프는 보수적인 parent-block 완료 순서를 나타내며, 실제 최소
착수 조건과 Phase 4 scheduling authority는 각 Micro Block의 `Preconditions`
필드다.

---

## 19. Change Surface

### Production and Prompt Targets

- `orca_loop/models.py`
- `orca_loop/contracts.py`
- `orca_loop/generation.py`
- `orca_loop/testrunner.py`
- `orca_loop/snapshot.py`
- `orca_loop/readonly.py`
- `orca_loop/dispatcher.py`
- `orca_loop/machine.py`
- `orca_loop/coordinator.py`
- `orca_loop/ledger.py`
- `orca_loop/config.py`
- `orca_loop/runspec.py`
- `orca_loop/reporting.py`
- `orca_loop/escalation.py`
- `orca_loop/environment.py`
- `orca_loop/roles.py`
- `run_loop.py`
- `prompts/code_reviewer.md`
- `prompts/cross_confirmer.md`
- `prompts/plan_reviewer.md`
- `orca_loop_execution_rules.md`

### Test Targets

- `tests/test_contracts.py`
- `tests/test_coordinator.py`
- `tests/test_dispatcher.py`
- `tests/test_environment.py`
- `tests/test_escalation.py`
- `tests/test_ledger.py`
- `tests/test_machine.py`
- `tests/test_plan_traceability.py`
- `tests/test_readonly.py`
- `tests/test_reporting.py`
- `tests/test_resume.py`
- `tests/test_runspec.py`
- `tests/test_snapshot.py`
- `tests/test_testrunner.py`
- `tests/test_worker_reconcile.py`
- `tests/test_worker_runner.py`
- `tests/fakes.py`

신규 production dependency와 신규 unprefixed production file은 만들지 않는다.
Implementation report만 다음 sequence의 prefixed artifact로 추가한다.

---

## 20. Validation and Risks

### Validation Performed in This Phase

- **PASS:** Approved Phase 2 artifact와 Phase 1 invariant 재확인.
- **PASS:** Current `HEAD`가 `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`임을 확인.
- **PASS:** Current source symbols, state transitions, contract parser, resume drift,
  report renderer, runtime manifest, permission environment를 static inspection.
- **PASS:** 13 Macro Blocks를 40 Micro Blocks와 `T-*` validation 범위로 매핑.
- **NOT RUN:** Source implementation.
- **NOT RUN:** Focused and full regression tests.
- **NOT RUN:** Live Orca worker/E2E.

### Risks

- `CoordinatorState`, `RunManifest`, notice schema를 동시에 변경하므로 migration
  test가 consumer 변경보다 먼저 PASS해야 한다.
- Conflict round는 두 additional model call을 사용한다.
- Exact normalized signature가 semantic equivalence를 놓치면 adjudication 빈도가
  증가할 수 있지만 자동 오병합보다 안전하다.
- Shared mirror tree digest 계산은 large repository에서 추가 I/O를 유발한다.
- Legacy final gate는 fresh validation 전까지 merge되지 않는다.

### Assumptions

- 기존 single-active-step coordinator와 mutation journal은 유지한다.
- 기존 B1~B5, `impact_class`, round limit, human gate routing은 유지한다.
- Test command execution과 repository delta guard semantics는 변경하지 않는다.
- CLI version/availability drift는 warning-only이며 typed observed permission failure
  marker policy가 refresh authority다.

### Open Questions

없음. Phase 4에서 approved behavior, public CLI semantics, persistent field meaning,
dependency set, state-machine contract 변경이 필요하면 earliest affected phase로
돌아가 재승인을 받는다.

---

## 21. Approval

- [x] Micro Blocking approved
- [ ] Revision requested
- [x] Permission granted to begin implementation

**Next phase:** Phase 4 — Code Implementation
