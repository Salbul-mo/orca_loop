# Task Report: Orca Loop 상호검증 및 Evidence Lineage 강화

**Current Phase:** 2. Macro Blocking

**Status:** Approved — 2026-08-19

**Date:** 2026-08-19

**Approved Baseline:** `docs/codex-mhj_26_08_19_01_phase1-system-design-orca-loop-cross-validation-hardening.md`
**Baseline HEAD:** `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`

---

## 1. Context and Objective

### 1.1 Goal

승인된 System Design Revision 2를 구현 가능한 상위 작업 경계로 분해한다. 각
block은 하나의 주된 책임, 명시적인 input/output, 실패 경계, 독립적인 검증
기준을 갖는다.

### 1.2 Scope

- Persistent schema와 backward-compatible state migration
- Coordinator-owned `test-evidence.json`과 sealed review context
- Blind review의 structured coverage contract
- Role-specific input staging과 shared round mirror
- Pending blind evidence와 state-machine lifecycle
- Deterministic pair comparison
- Conditional symmetric adjudication
- Provider diversity와 explicit escape hatch
- Validation lineage, resume drift invalidation, final merge invariant
- Human-readable reporting과 audit visibility
- Reviewer prompt와 Plan Reviewer 검증 품질
- Permission 및 운영 문서 정합화
- Focused, recovery, full-regression validation

### 1.3 Exclusions

- Host-wide container 또는 별도 OS user sandbox
- LLM 기반 semantic finding deduplication
- `run_loop.py` 전면 분해
- 자동 majority vote
- Resume 중 runtime configuration 변경
- CLI version drift를 permission proof의 blocking condition으로 복원

---

## 2. Dependency Overview

```text
B-01 Persistent Contracts and State Evolution
 ├─ B-02 Test Evidence and Sealed Review Context
 ├─ B-03 Structured Blind Review Contracts
 └─ B-08 Provider Diversity and Manifest Policy

B-02 + B-03
 └─ B-04 Blind Input Isolation and Shared Mirror
      └─ B-05 Pending Review Lifecycle and State Machine
           └─ B-06 Deterministic Review Comparison
                └─ B-07 Symmetric Adjudication

B-01 + B-02 + B-05 + B-06 + B-07
 └─ B-09 Evidence Invalidation, Resume, and Final Merge Guard

B-01..B-09
 ├─ B-10 Reporting and Audit Visibility
 ├─ B-11 Reviewer Prompt and Plan Review Quality
 └─ B-12 Operations and Permission Documentation

B-01..B-12
 └─ B-13 Integrated Validation and Recovery Regression
```

### 2.1 Block Summary

| Block ID | Name | Objective | Dependencies | Primary Output | Validation |
| --- | --- | --- | --- | --- | --- |
| `B-01` | Persistent Contracts and State Evolution | 모든 신규 evidence와 lifecycle state를 typed, durable, backward-compatible contract로 정의 | None | Models, parsers, state migration | Round-trip and legacy-load tests |
| `B-02` | Test Evidence and Sealed Review Context | 한 snapshot에 결합된 test evidence, mirror, diff, scope를 한 번만 봉인 | `B-01` | `TestEvidence`, `CodeReviewRoundContext` | Before/after drift and digest tests |
| `B-03` | Structured Blind Review Contracts | `APPROVE`를 전체 scope coverage와 evidence에 결합 | `B-01` | Blind/adjudication artifact contracts | Exact-coverage contract tests |
| `B-04` | Blind Input Isolation and Shared Mirror | 두 blind reviewer에게 동일 입력만 주고 상대 결과를 차단 | `B-02`, `B-03` | Role input allowlists and shared mirror binding | Staged-input equality/exclusion tests |
| `B-05` | Pending Review Lifecycle and State Machine | Blind A 결과가 B scope/ledger를 바꾸지 않도록 pair lifecycle을 durable하게 관리 | `B-01`, `B-04` | New states, pending round persistence | Crash-boundary and atomicity tests |
| `B-06` | Deterministic Review Comparison | 두 blind artifact의 합의·충돌을 LLM 없이 판정 | `B-03`, `B-05` | `review-comparison.json` and compare result | Agreement/conflict matrix tests |
| `B-07` | Symmetric Adjudication | 충돌 시 동일 reveal input으로 양쪽이 독립 재판정 | `B-03`, `B-05`, `B-06` | Two adjudication artifacts and final decisions | Input symmetry and conflict-routing tests |
| `B-08` | Provider Diversity and Manifest Policy | 신규 run의 provider 다양성을 기본 강제하고 예외를 durable하게 기록 | `B-01` | CLI/config/manifest independence policy | Preflight, resume, legacy tests |
| `B-09` | Evidence Invalidation, Resume, and Final Merge Guard | Drift가 stale evidence를 재사용하거나 merge로 진행하지 못하게 차단 | `B-01`, `B-02`, `B-05`, `B-06`, `B-07` | Lineage transitions and final invariant | State-group drift and TOCTOU tests |
| `B-10` | Reporting and Audit Visibility | 현재 schema를 정확히 렌더링하고 independence/provenance를 인간에게 노출 | `B-01`, `B-02`, `B-05`, `B-06`, `B-07`, `B-08`, `B-09` | Corrected stage/final reports | Current-contract rendering tests |
| `B-11` | Reviewer Prompt and Plan Review Quality | Blindness, coverage, bounded source verification을 role contract에 반영 | `B-03`, `B-04`, `B-07` | Updated reviewer contracts/prompts | Template and staged-input tests |
| `B-12` | Operations and Permission Documentation | 운영 문서를 코드 및 승인 정책에 맞춤 | `B-08`, `B-09`, `B-10`, `B-11` | Revised execution rules and environment comments | Static document assertions |
| `B-13` | Integrated Validation and Recovery Regression | 전체 workflow, recovery, compatibility를 최종 검증 | `B-01` through `B-12` | Final evidence report | Focused, full suite, doctor, diff checks |

---

## 3. B-01 — Persistent Contracts and State Evolution

### Rationale

Blind pair, adjudication, test evidence, validation lineage는 crash 후에도 동일하게
복원돼야 한다. 먼저 typed persistent contract를 고정하지 않으면 후속 block이
서로 다른 임시 dictionary나 artifact shape에 의존하게 된다.

### Objective

신규 domain model, enum, artifact kind, coordinator state field를 정의하고 기존
committed state를 loss 없이 읽는 migration boundary를 제공한다.

### Scope

- `orca_loop/models.py`
- `orca_loop/contracts.py`
- `orca_loop/generation.py`
- 신규 loop state, role phase, artifact kind, comparison/adjudication decision
- `ValidationLineage`
- `PendingReviewRound`
- Coordinator state schema evolution

### Exclusions

- 실제 review context 생성
- Worker dispatch
- Ledger comparison semantics
- Human-readable rendering

### Dependencies

None.

### Input

- Approved invariants `RQ-01` through `RQ-10`
- Existing `CoordinatorState`, `ConsensusLedger`, `ReviewArtifact`,
  `TestGateResult`
- Existing generation digest and atomic commit rules

### Output

- Frozen dataclasses and enums for all new durable values
- Strict parse/serialize functions
- Legacy state migration with empty lineage
- New state schema version and supported-version validation

### Side Effects

- New generations contain additional state fields.
- Legacy states are upgraded in memory and written in the new schema on the
  next commit.

### Failure Modes

- Unknown schema version: fail closed with typed generation/contract error.
- Missing required new-run field: reject artifact or state.
- Legacy final-gate state without lineage: load succeeds, merge qualification
  remains false.
- Unknown enum or extra strict-contract field: reject without partial parsing.

### Validation

- `V-B01-01`: Every new dataclass round-trips through canonical JSON.
- `V-B01-02`: Legacy state without lineage loads with explicit empty lineage.
- `V-B01-03`: Unsupported schema versions fail closed.
- `V-B01-04`: New state and ledger generations retain digest integrity.
- `V-B01-05`: Dataclasses remain frozen and enum values are exact.

### High-Level Pseudocode

```text
define typed durable models
define strict enum and field contracts
decode committed state
if schema is legacy-supported:
    construct explicit empty validation lineage
    mark state for next-commit schema upgrade
elif schema is current:
    validate every required field
else:
    reject state
serialize canonically
verify persisted digest and typed reread
```

---

## 4. B-02 — Test Evidence and Sealed Review Context

### Rationale

두 reviewer의 input identity를 보장하려면 test result, source snapshot, mirror,
diff, scope를 worker 실행 전에 한 번만 생성하고 하나의 digest로 결합해야 한다.

### Objective

`TEST_GATE` 결과를 durable evidence로 저장하고, before/after snapshot이 동일한
구간에서 round-specific review context와 shared read-only mirror를 봉인한다.

### Scope

- `orca_loop/testrunner.py`
- `orca_loop/snapshot.py`
- `orca_loop/readonly.py`
- `orca_loop/coordinator.py`
- `run_loop.py`의 coordinator-owned context preparation path
- `test-evidence.json`, `review-context.json`, shared mirror, frozen diff,
  scope manifest lifecycle

### Exclusions

- Reviewer artifact parsing
- Pair comparison
- Adjudication
- Provider policy

### Dependencies

`B-01`.

### Input

- `PlanDocument.test_contract`
- `TestExecutionPolicy`
- `TestGateResult`
- Current worktree path and snapshot
- Approved plan scope and destructive approval digest

### Output

- Canonical `TestEvidence` and digest
- Canonical `CodeReviewRoundContext` and digest
- One immutable round mirror
- Frozen diff and scope-manifest digests

### Side Effects

- Writes coordinator-owned artifacts under the active run.
- Creates one read-only mirror directory per code-review round.
- Records context identity in committed coordinator state.

### Failure Modes

- Source changes during context preparation: discard context and return a typed
  resumable drift stop.
- Mirror copy or ACL failure: stop before reviewer dispatch.
- Test evidence write/digest reread failure: stop without recording a usable
  context.
- `NOT_RUN`: persist explicit status and pre-test snapshot without claiming
  execution.
- Test policy violation: do not create a review context.

### Validation

- `V-B02-01`: `PASS` records the policy-validated after snapshot.
- `V-B02-02`: `NOT_RUN` records the captured before snapshot and no fake result.
- `V-B02-03`: Context before/after drift is detected.
- `V-B02-04`: Mirror, diff, scope, implementation, and test digests are bound
  into one context digest.
- `V-B02-05`: One context produces one reusable mirror for the complete round.
- `V-B02-06`: Context artifacts survive crash and reread.

### High-Level Pseudocode

```text
run coordinator-owned test gate
persist bounded TestEvidence
if status is not PASS or NOT_RUN:
    route through existing test failure or policy path
capture snapshot_before
create round mirror
create frozen diff and scope manifest
capture snapshot_after
if snapshot_before != snapshot_after:
    invalidate prepared context
    stop resumably
construct CodeReviewRoundContext from exact component digests
persist context atomically
commit context identity to state
```

---

## 5. B-03 — Structured Blind Review Contracts

### Rationale

`verdict=APPROVE`와 빈 finding만으로는 reviewer가 전체 scope를 실제로
검토했는지 증명하지 못한다. Approval은 criterion, file, test별 판정과 evidence에
결합돼야 한다.

### Objective

Blind review와 adjudication artifact의 exact schema, coverage completeness,
evidence floor, provenance 검증을 정의한다.

### Scope

- `orca_loop/models.py`
- `orca_loop/contracts.py`
- Review coverage types
- Blind review artifact version
- Adjudication artifact contract
- Comparison input/output contract

### Exclusions

- Prompt wording
- Worker input staging
- State transitions
- Provider validation

### Dependencies

`B-01`.

### Input

- Sealed context scope identifiers
- Existing `Finding`, `FindingDecision`, `BlockingReason`, `ImpactClass`
- Existing artifact provenance fields

### Output

- Exact `acceptance_evaluations`, `file_evaluations`, `test_evaluations`
  contracts
- Blind lane and context digest fields
- Exact adjudication candidate decision contract
- Deterministic normalized signature inputs

### Side Effects

None outside artifact parsing and validation.

### Failure Modes

- Missing or extra coverage item: reject complete artifact.
- Evidence-free `APPROVE`: reject.
- `NOT_RUN` represented as verified execution: reject.
- Wrong context, snapshot, round, lane, or plan version: provenance error.
- Duplicate IDs or conflicting duplicate fields: reject.
- Unknown candidate or incomplete adjudication coverage: reject.

### Validation

- `V-B03-01`: Exact scope coverage is accepted.
- `V-B03-02`: Missing, extra, duplicate, or reordered exact-ID violations are
  rejected according to the approved ordering contract.
- `V-B03-03`: Evidence-free approvals are rejected.
- `V-B03-04`: `NOT_RUN` cannot become positive verification evidence.
- `V-B03-05`: Blind and adjudication provenance mismatches are rejected.
- `V-B03-06`: Artifact size and unknown-field limits remain enforced.

### High-Level Pseudocode

```text
decode one strict artifact object
validate schema, kind, lane, round, snapshot, context digest
compare acceptance IDs with sealed context IDs
compare file paths and operations with sealed context scope
compare test IDs with sealed context test IDs
for each evaluation:
    validate decision enum
    require admissible evidence for APPROVE
validate delivered baseline finding decisions
validate new findings and unique IDs
return immutable typed artifact
```

---

## 6. B-04 — Blind Input Isolation and Shared Mirror

### Rationale

현재 blanket staging은 두 번째 reviewer에게 첫 번째 result와 갱신된 scope를
노출한다. Prompt 독립성보다 role-specific input allowlist가 먼저 강제돼야 한다.

### Objective

두 blind reviewer가 동일 sealed input만 받고, 상대 artifact와 round 중 생성된
finding을 받지 않도록 staging과 launch context를 분리한다.

### Scope

- `run_loop.py`의 `_step_inputs()`, `_profile_root()`, worker context 구성
- `orca_loop/roles.py`
- `orca_loop/dispatcher.py` staging manifest usage
- Shared round mirror resolution
- Role-to-input allowlist

### Exclusions

- Host-level malicious process isolation
- Pair comparison
- Adjudication decision semantics
- Ledger mutation

### Dependencies

`B-02`, `B-03`.

### Input

- `CodeReviewRoundContext`
- Role or review phase
- Approved staged-input allowlist
- Shared mirror path

### Output

- Identical blind A/B logical input manifests
- Reviewer-specific contract with the same `ScopePackage`
- Adjudicator reveal input manifests
- Explicit exclusion proof for peer artifacts

### Side Effects

- Writes step-local staged input copies and manifests.
- Reuses the sealed mirror rather than creating a new generation mirror for
  each reviewer.

### Failure Modes

- A required sealed input is absent or digest-mismatched: reject dispatch.
- Peer artifact appears in blind input: coordinator contract failure.
- Blind A/B manifests differ outside task/dispatch provenance: reject round.
- Shared mirror is missing, writable, or no longer bound to context: stop.
- Role has no explicit allowlist: fail closed instead of blanket staging.

### Validation

- `V-B04-01`: Blind B input excludes blind A output.
- `V-B04-02`: Blind A/B component digests are identical.
- `V-B04-03`: Both reviewers receive the pre-round scope.
- `V-B04-04`: Adjudicators receive both blind artifacts but no peer
  adjudication artifact.
- `V-B04-05`: Unknown roles cannot inherit all artifacts.
- `V-B04-06`: Shared mirror remains read-only.

### High-Level Pseudocode

```text
select explicit allowlist for role and phase
load sealed review context
resolve shared mirror from context
for each allowed input:
    verify source exists and digest matches context
stage bounded copy into step input
assert every forbidden peer artifact is absent
build staged-input manifest
for blind B:
    compare logical manifest with blind A baseline manifest
dispatch only after all assertions pass
```

---

## 7. B-05 — Pending Review Lifecycle and State Machine

### Rationale

Blind A를 기존 방식으로 즉시 ledger에 적용하면 B의 scope가 달라진다. Pair가
완성되기 전까지 worker result는 durable하되 consensus state와 분리돼야 한다.

### Objective

Review preparation, blind A, blind B, comparison, optional adjudication을
deterministic state machine에 추가하고 pending pair를 crash-safe하게 관리한다.

### Scope

- `orca_loop/machine.py`
- `orca_loop/coordinator.py`
- `run_loop.py`
- `orca_loop/generation.py`
- Resume reconciliation for every new state
- Pending artifact identities and pair lifecycle

### Exclusions

- Comparison content rules
- Adjudication candidate semantics
- Human-readable reports

### Dependencies

`B-01`, `B-04`.

### Input

- Sealed context
- Blind A/B worker completions
- Existing counters, retry limits, and generation state

### Output

- Durable `REVIEW_CONTEXT_PREPARE`, `CODE_REVIEW_A`, `CODE_REVIEW_B`,
  `REVIEW_COMPARE`, `ADJUDICATE_A`, `ADJUDICATE_B` lifecycle
- Pending pair evidence outside shared ledger
- Exact transition table and resume action for every new state/stage

### Side Effects

- Adds committed state generations and pending review artifact references.
- May abandon and retry an in-flight worker step using existing recovery rules.

### Failure Modes

- Crash after A: preserve A, resume B with unchanged context.
- Crash after B before compare: reuse both verified artifacts without
  redispatch.
- Duplicate completion: recognize existing promotion and do not apply twice.
- Context mismatch after resume: invalidate pair and route through drift policy.
- Operational retry exhaustion: use existing bounded escalation/abort behavior.
- Undefined state/signal combination: fail closed.

### Validation

- `V-B05-01`: Blind A verification does not mutate consensus findings.
- `V-B05-02`: Blind B receives the frozen pre-round scope after A completes.
- `V-B05-03`: Every new state has deterministic transitions.
- `V-B05-04`: Crash boundaries after context, A, B, comparison, and each
  adjudication recover without duplicate dispatch.
- `V-B05-05`: Pair application is atomic at generation scope.
- `V-B05-06`: Transition termination remains bounded.

### High-Level Pseudocode

```text
TEST_GATE success -> REVIEW_CONTEXT_PREPARE
prepare and commit sealed context
dispatch blind A
validate and persist A as pending evidence only
dispatch blind B using original context and scope
validate and persist B as pending evidence only
transition to REVIEW_COMPARE
if compare says agreement:
    atomically apply final pair to ledger
    transition to CONSENSUS_EVALUATE
else:
    transition through ADJUDICATE_A and ADJUDICATE_B
resume each boundary from committed pending evidence
```

---

## 8. B-06 — Deterministic Review Comparison

### Rationale

두 blind result를 다시 LLM에게 바로 넘기면 독립 검토의 결과를 coordinator가
통제하지 못한다. 먼저 exact identifiers, decisions, coverage, provenance 차이를
결정론적으로 계산해야 한다.

### Objective

Direct agreement, unilateral finding, decision conflict, coverage conflict,
signature collision을 deterministic하게 분류하고 canonical comparison artifact를
생성한다.

### Scope

- `orca_loop/ledger.py`
- `orca_loop/coordinator.py`
- Pair comparison model and serializer
- Candidate identity and signature collision rules
- Atomic blind-pair application path

### Exclusions

- Semantic equivalence inference
- Model adjudication
- Provider validation

### Dependencies

`B-03`, `B-05`.

### Input

- Verified blind A/B artifacts
- Sealed review context
- Pre-round consensus ledger

### Output

- Canonical `review-comparison.json`
- `AGREED`, `ADJUDICATION_REQUIRED`, or `INVALID` result
- Exact conflict/candidate list
- Atomic ledger update only for a complete valid pair

### Side Effects

- Persists comparison artifact and digest.
- Applies pair decisions to ledger only on the defined commit path.

### Failure Modes

- Context, snapshot, plan, round, or scope mismatch: `INVALID` and fail closed.
- Same ID with different signature: adjudication candidate, never silent merge.
- Different IDs with possible semantic duplication: separate candidates for
  adjudication.
- Missing decision for a baseline finding: invalid pair.
- Unilateral non-blocking suggestion: preserve informationally without forcing
  adjudication.

### Validation

- `V-B06-01`: Identical complete approvals produce direct agreement.
- `V-B06-02`: Unilateral blocking finding requires adjudication.
- `V-B06-03`: Shared-finding decision conflict requires adjudication.
- `V-B06-04`: Coverage conflict requires adjudication.
- `V-B06-05`: Provenance mismatch returns invalid, not disagreement.
- `V-B06-06`: Comparison is deterministic for reordered JSON input.
- `V-B06-07`: No semantic deduplication is inferred.

### High-Level Pseudocode

```text
validate pair context and provenance equality
validate both complete coverage sets
compare every baseline finding decision
collect A-only and B-only blocking findings
detect same-ID signature collisions
compare acceptance, file, and test evaluations
if structural or provenance invalidity exists:
    return INVALID
if any actionable difference exists:
    persist exact comparison
    return ADJUDICATION_REQUIRED
persist agreement comparison
atomically apply pair decisions
return AGREED
```

---

## 9. B-07 — Symmetric Adjudication

### Rationale

한쪽 confirmer만 상대 review를 보고 판정하면 다시 비대칭 anchoring이 생긴다.
충돌 시 양쪽이 동일 reveal input을 보고 상대 adjudication 결과 없이 독립적으로
재판정해야 한다.

### Objective

두 adjudicator에게 동일 candidate set을 전달하고, `CONFIRM`, `REJECT`,
`DUPLICATE`, `VERIFY_REQUIRED` 결과를 pair로 비교해 final actionable findings를
결정한다.

### Scope

- Adjudication worker states and dispatch profiles
- Adjudication contract validation
- Candidate disposition resolution
- Final pair-to-ledger conversion
- Existing escalation integration

### Exclusions

- Third-model majority voting
- Semantic candidate grouping by coordinator
- Automatic approval of persistent conflicts

### Dependencies

`B-03`, `B-05`, `B-06`.

### Input

- Blind A/B artifacts
- `review-comparison.json`
- Sealed review context
- Candidate IDs and evidence

### Output

- Adjudication A/B artifacts
- Confirmed, rejected, duplicate, or unresolved candidate disposition
- Final ledger update and escalation triggers

### Side Effects

- Adds two worker calls only when comparison requires adjudication.
- Persists both adjudication artifacts and digests.
- May route to `FIX` or `USER_DECISION_REQUIRED`.

### Failure Modes

- One adjudicator sees peer adjudication output: input-contract violation.
- Candidate coverage missing or extra: reject artifact.
- Duplicate targets differ: keep unresolved.
- Both reject: retain audit evidence but exclude implementation scope.
- Persistent conflict: never approve; route using existing blocking and impact
  rules.
- Adjudication timeout/retry exhaustion: existing bounded stop behavior.

### Validation

- `V-B07-01`: Both adjudicators receive identical reveal inputs.
- `V-B07-02`: Peer adjudication artifact is absent from each input.
- `V-B07-03`: Dual `CONFIRM` produces one actionable candidate.
- `V-B07-04`: Dual `REJECT` excludes candidate but preserves history.
- `V-B07-05`: Agreed duplicate target canonicalizes safely.
- `V-B07-06`: Conflicting dispositions remain unresolved.
- `V-B07-07`: Normal agreement path makes no adjudication calls.

### High-Level Pseudocode

```text
build one reveal manifest from sealed context, blind A, blind B, comparison
dispatch adjudicator A without B adjudication output
persist validated A result as pending evidence
dispatch adjudicator B with the same reveal manifest and without A result
persist validated B result
for each candidate:
    combine A and B dispositions deterministically
    confirm, reject, deduplicate, or retain unresolved
apply final dispositions atomically to ledger
derive existing escalation triggers
transition to CONSENSUS_EVALUATE
```

---

## 10. B-08 — Provider Diversity and Manifest Policy

### Rationale

Wire lane names do not prove runtime diversity. New runs need different review
providers by default, while intentional same-provider runs need an explicit and
durable exception rather than a silent override.

### Objective

Resolved runtime providers를 preflight에서 검증하고
`--allow-same-provider-consensus`를 manifest, resume, dry-run, status에
일관되게 보존한다.

### Scope

- `orca_loop/config.py`
- `orca_loop/runspec.py`
- `orca_loop/catalog.py` or runtime resolution boundary
- `run_loop.py` CLI, dry-run, status
- New-run and legacy-run independence status

### Exclusions

- Provider 자동 변경
- Model 또는 effort diversity 강제
- Resume configuration 변경
- Permission proof의 provider capability semantics 변경

### Dependencies

`B-01`.

### Input

- Resolved four-worker runtime configuration
- Explicit CLI escape-hatch flag
- Existing or legacy run manifest

### Output

- `FULL` or `DEGRADED` consensus-independence status
- Persisted same-provider authorization
- Preflight decision and visible diagnostics

### Side Effects

- New-run preflight may block before worker provisioning.
- Manifest schema gains an additive policy field.
- Dry-run/status/final reporting exposes the resolved decision.

### Failure Modes

- Same provider without explicit flag: `BLOCKED` before mutation.
- Escape flag omitted on resume: restore manifest value; do not reinterpret.
- Resume attempts to change the policy: reject configuration drift.
- Legacy same-provider run: remain resumable and report `DEGRADED`.
- Provider/model permission capability missing: existing permission preflight
  remains authoritative.

### Validation

- `V-B08-01`: Different review providers pass without an escape flag.
- `V-B08-02`: Same providers fail new-run preflight by default.
- `V-B08-03`: Explicit escape passes and persists.
- `V-B08-04`: Resume restores the exact recorded policy.
- `V-B08-05`: Legacy same-provider run is degraded, not silently changed.
- `V-B08-06`: No code path changes provider automatically.

### High-Level Pseudocode

```text
resolve all worker providers without mutation
compare primary and secondary code-review providers
if different:
    independence = FULL
elif explicit same-provider flag is true:
    independence = DEGRADED
else:
    block new run before Orca mutation
persist flag and independence status in manifest
on resume:
    restore persisted values
    reject CLI drift
emit status in dry-run and reports
```

---

## 11. B-09 — Evidence Invalidation, Resume, and Final Merge Guard

### Rationale

Snapshot drift는 repository write 여부가 아니라 evidence validity로 판단해야 한다.
또한 final gate 생성과 merge decision 처리 사이의 live drift도 차단해야 한다.

### Objective

State group별 drift action, validation-lineage invalidation, legacy
requalification, final merge invariant를 구현한다.

### Scope

- `run_loop.py` resume drift resolution and gate handling
- `orca_loop/coordinator.py` lineage updates and final guard
- `orca_loop/failure.py` typed resumable evidence drift
- `orca_loop/escalation.py` stale gate/notice settlement where required
- Legacy final-gate revalidation

### Exclusions

- New human decision options unrelated to drift
- Destructive command execution
- Automatic acceptance of external changes

### Dependencies

`B-01`, `B-02`, `B-05`, `B-06`, `B-07`.

### Input

- Current worktree snapshot
- Committed validation lineage
- Current state and `blocked_from_state`
- Explicit `--accept-worktree-drift`
- Gate binding and human decision

### Output

- `REBASELINE`, `INVALIDATE_PLAN`, `INVALIDATE_CODE`, or `BLOCK` drift action
- Correct state rollback target
- Cleared stale lineage and gate binding
- Final merge qualification result

### Side Effects

- May commit a rebaseline and route to `PLAN_REVISE` or `TEST_GATE`.
- May settle a stale local notice and clear its binding.
- Records resumable stop evidence.

### Failure Modes

- Drift without explicit acceptance in evidence-bearing state: block without
  evidence mutation.
- Explicit acceptance: invalidate evidence; never rewrite old digest as new.
- Live drift while gate is being resolved: reject merge and stop resumably.
- Legacy missing lineage: require revalidation.
- Unknown state group: fail closed.
- Stale external gate resolves later: no matching local lineage/binding, so its
  decision cannot transition the run.

### Validation

- `V-B09-01`: Planning author states may rebaseline automatically.
- `V-B09-02`: Plan evidence states block and explicitly route to
  `PLAN_REVISE` after acceptance.
- `V-B09-03`: Code evidence states block and explicitly route to `TEST_GATE`.
- `V-B09-04`: `IMPLEMENT`/`FIX` retain explicit drift acceptance behavior and
  clear downstream lineage.
- `V-B09-05`: Every final lineage digest must equal current snapshot.
- `V-B09-06`: Guard runs before gate creation and immediately before merge.
- `V-B09-07`: Legacy final gate cannot merge without revalidation.
- `V-B09-08`: `NOT_RUN` remains visible and never becomes test execution PASS.

### High-Level Pseudocode

```text
capture current snapshot
compare with committed state and validation lineage
if no drift:
    continue
classify state by evidence ownership
if state permits authoring rebaseline:
    commit new baseline
elif explicit acceptance is absent:
    stop resumably with exact digest evidence
elif state owns plan evidence:
    clear plan review evidence and stale gate
    commit PLAN_REVISE with current snapshot
else:
    clear test, review, adjudication, consensus evidence and stale gate
    commit TEST_GATE with current snapshot

before final gate and before merge:
    require current == state == every applicable lineage snapshot
    require every applicable artifact digest
    otherwise raise typed resumable evidence drift
```

---

## 12. B-10 — Reporting and Audit Visibility

### Rationale

Machine evidence가 강해져도 human report가 current schema를 렌더링하지 못하거나
blind/adjudication 경로를 숨기면 운영자가 최종 판단을 검증할 수 없다.

### Objective

기존 reporting schema mismatch를 수정하고 review independence, coverage,
provider status, lineage를 stage/final report에 노출한다.

### Scope

- `orca_loop/reporting.py`
- `orca_loop/escalation.py` decision report sections
- `run_loop.py` status/dry-run output where applicable
- Existing reporting tests and fixtures

### Exclusions

- Reporting failure를 workflow terminal failure로 변경
- External dashboard 구현
- Raw unbounded test output 노출

### Dependencies

`B-01`, `B-02`, `B-05`, `B-06`, `B-07`, `B-08`, `B-09`.

### Input

- Current strict plan and implementation artifacts
- Test evidence
- Review context, blind pair, comparison, adjudication artifacts
- Provider independence status
- Validation lineage

### Output

- Correct plan and implementation Markdown
- Blind/adjudication stage reports
- Final evidence and independence summary
- Explicit `PASS`, `NOT_RUN`, degraded, and unverified boundaries

### Side Effects

- Writes best-effort immutable report history and current Markdown reports.
- Reporting errors remain logged without changing coordinator success.

### Failure Modes

- Valid current contract causes render error: regression failure.
- Unknown optional future artifact: log and skip without workflow failure.
- Missing evidence: render `미확인` or `NOT_RUN`, never infer PASS.
- Same-provider override: show `DEGRADED` and recorded reason.

### Validation

- `V-B10-01`: `verification_method` renders correctly.
- `V-B10-02`: `evidence_refs` renders for addressed findings.
- `V-B10-03`: Test command objects render `argv`, `cwd`, `timeout_ms`, `kind`.
- `V-B10-04`: Valid current artifacts do not create `reporting.log`.
- `V-B10-05`: Final report names both providers and blind context digest.
- `V-B10-06`: Adjudication use and unresolved conflicts are visible.
- `V-B10-07`: Snapshot lineage mismatch cannot be displayed as merge-ready.

### High-Level Pseudocode

```text
parse current artifact shape defensively
render exact plan, test, review, and implementation fields
render reviewer runtime identities and independence status
render sealed context and lineage digests
render direct-agreement or adjudication path
render coverage and test status without overstatement
atomically write report
on rendering exception:
    append bounded reporting error
    leave workflow state unchanged
```

---

## 13. B-11 — Reviewer Prompt and Plan Review Quality

### Rationale

Coordinator isolation이 있어도 worker contract가 blind stage, coverage, evidence,
adjudication 책임을 정확히 설명하지 않으면 valid artifact 생성률과 검토 품질이
낮아진다.

### Objective

Code-review prompt를 blind A/B와 adjudication phase에 맞추고 Plan Reviewer에
bounded repository verification과 structured coverage 의무를 추가한다.

### Scope

- `prompts/code_reviewer.md`
- `prompts/cross_confirmer.md` or successor secondary-review contract
- Adjudication prompt assets selected by explicit role/phase mapping
- `prompts/plan_reviewer.md`
- `orca_loop/roles.py` placeholder and template validation

### Exclusions

- Prompt만으로 host-level secrecy를 주장
- Reviewer에게 test 실행 또는 source modification 허용
- Plan Reviewer가 replacement architecture 작성

### Dependencies

`B-03`, `B-04`, `B-07`.

### Input

- Role/phase
- Sealed context and allowed staged inputs
- Structured artifact schema
- Comparison candidates for adjudication

### Output

- Fully rendered strict role contracts
- Blind-review instructions without peer references
- Symmetric adjudication instructions
- Plan review coverage and bounded source-read instructions

### Side Effects

- Changes worker prompt content and expected output schema.
- Does not change repository permissions.

### Failure Modes

- Unresolved template marker: reject before dispatch.
- Blind prompt mentions or requires peer artifact: template contract failure.
- Adjudicator prompt omits candidate coverage: artifact retry.
- Plan Reviewer invents replacement design: `B5`/contract enforcement remains.
- Prompt role and artifact kind mismatch: reject.

### Validation

- `V-B11-01`: Every role/phase template renders without unresolved markers.
- `V-B11-02`: Blind prompts reference only allowlisted inputs.
- `V-B11-03`: Adjudication prompts describe identical reveal inputs.
- `V-B11-04`: Plan Reviewer verifies affected files, integration points, public
  interfaces, tests, and security within bounded scope.
- `V-B11-05`: Read-only/test prohibitions remain explicit.
- `V-B11-06`: Lane name and runtime provider are explicitly distinguished.

### High-Level Pseudocode

```text
select template by role and review phase
populate runtime, context, scope, and artifact placeholders
validate all placeholders resolved
validate template input references are allowlisted
for blind phase:
    omit peer result and agreement language
for adjudication phase:
    require candidate-by-candidate evidence decision
for plan review:
    require bounded repository verification matrix
dispatch only a complete strict contract
```

---

## 14. B-12 — Operations and Permission Documentation

### Rationale

운영 문서는 stale terminal, runtime default, permission refresh 설명을 포함하고
있다. Code-authoritative 정책을 문서에 반영하지 않으면 새 안전장치를 운영자가
잘못 해석할 수 있다.

### Objective

Coordinator/worker terminal lifecycle, runtime defaults, provider independence,
resume invalidation, permission warning-only 정책을 현재 코드와 일치시킨다.

### Scope

- `orca_loop_execution_rules.md`
- `orca_loop/environment.py` module documentation
- 관련 operator-facing usage text
- Static documentation regression assertions where appropriate

### Exclusions

- `compare_environment()` behavior 변경
- CLI drift blocking 복원
- Permission spike 자동 실행
- Host sandbox 구현

### Dependencies

`B-08`, `B-09`, `B-10`, `B-11`.

### Input

- Current `start`, `resume`, `status`, `doctor` code behavior
- Approved warning-only permission policy
- Provider diversity and drift invalidation behavior

### Output

- Code-aligned operations guide
- Accurate permission environment documentation
- Explicit threat-model and evidence-status boundaries

### Side Effects

Documentation files only; no runtime behavior change.

### Failure Modes

- Documentation claims `start` creates coordinator terminal: static failure.
- Documentation claims CLI version drift blocks: static failure.
- Documentation equates lane name with provider: static failure.
- Documentation describes `NOT_RUN` as PASS: static failure.

### Validation

- `V-B12-01`: Coordinator terminal source matches `_start_argv()` behavior.
- `V-B12-02`: Four worker terminal provisioning is described accurately.
- `V-B12-03`: Runtime defaults and explicit operator confirmation are
  distinguished.
- `V-B12-04`: CLI version/availability drift is warning-only.
- `V-B12-05`: Typed permission failure refresh policy is preserved.
- `V-B12-06`: Blind workflow and host sandbox boundaries are separated.

### High-Level Pseudocode

```text
extract authoritative behavior from current implementation
replace stale terminal and runtime statements
document provider diversity and explicit escape hatch
document state-specific drift invalidation
document permission warning-only and typed-failure refresh rules
add static assertions for critical policy sentences
run diff and documentation checks
```

---

## 15. B-13 — Integrated Validation and Recovery Regression

### Rationale

이번 변경은 persistent state, state machine, worker input, ledger, gate를 함께
변경한다. Focused unit test만으로는 cross-block recovery와 final invariant를
증명할 수 없다.

### Objective

각 block validation을 통합하고, 정상 합의·충돌·crash/resume·drift·legacy
경로를 재현한 뒤 full regression과 environment diagnostics를 수행한다.

### Scope

- Existing focused test modules
- State-machine transition coverage
- Crash-boundary and resume fixtures
- Test, review, adjudication, human-gate integration fixture
- Full unit suite, `doctor`, `git diff --check`, final status inspection

### Exclusions

- 실행하지 않은 실제 provider/E2E를 PASS로 보고
- 외부 application repository 검증
- Production deployment

### Dependencies

`B-01` through `B-12`.

### Input

- Final code and documentation state
- Approved Phase 1 invariants
- All block validation IDs
- Baseline of 382 passing tests

### Output

- Focused validation evidence by block
- Full regression result
- Legacy compatibility and recovery evidence
- Final implementation report inputs

### Side Effects

- Test fixtures may create temporary repositories and run directories.
- `doctor` performs read-only runtime/environment diagnostics.
- No live model call unless separately and explicitly authorized.

### Failure Modes

- Focused regression failure: investigate before full suite claim.
- New full-suite failure: distinguish from Phase 1 baseline.
- Environment or dependency prevents a command: report `BLOCKED` with exact
  capability.
- Live E2E not authorized: report `NOT RUN`.
- Final diff exceeds approved scope: stop before completion.

### Validation

- `V-B13-01`: All `V-B01-*` through `V-B12-*` checks pass.
- `V-B13-02`: Direct blind agreement reaches final gate without adjudication.
- `V-B13-03`: Unilateral finding invokes both adjudicators.
- `V-B13-04`: Crash after each new durable boundary resumes without duplicate
  dispatch or ledger application.
- `V-B13-05`: Drift at review, consensus, and final gate cannot reuse evidence.
- `V-B13-06`: Legacy state can load but cannot merge without revalidation.
- `V-B13-07`: Full unit suite passes against final state.
- `V-B13-08`: `doctor` and `git diff --check` pass.

### High-Level Pseudocode

```text
run contract and migration tests
run test-evidence and sealed-context tests
run input-isolation and pending-ledger tests
run comparison and adjudication matrices
run provider policy tests
run resume drift and final-gate tests
run reporting, prompt, and documentation checks
run crash-boundary integration fixtures
run full unit suite
run doctor and diff checks
inspect final Git diff and classify every result
```

---

## 16. Implementation Order

The approved dependency order is:

```text
Layer 1: B-01
Layer 2: B-02, B-03, B-08
Layer 3: B-04
Layer 4: B-05
Layer 5: B-06
Layer 6: B-07
Layer 7: B-09
Layer 8: B-10, B-11
Layer 9: B-12
Layer 10: B-13
```

Blocks in the same layer may be designed independently, but Phase 4 changes
must be integrated in dependency order so persistent contracts exist before
their consumers.

---

## 17. Cross-Block Completion Conditions

The Macro Blocking phase is complete only when all of the following remain
true in the subsequent Micro Blocking design.

1. No blind reviewer receives peer output or peer-created scope.
2. Blind A cannot mutate the consensus ledger before blind B finishes.
3. Both reviewers use one sealed context and shared mirror.
4. Approval requires exact coverage and evidence.
5. Adjudication is symmetric and conditional.
6. Same-provider consensus requires explicit durable authorization.
7. Resume drift invalidates evidence rather than relabeling it.
8. Final merge checks every applicable snapshot and artifact digest.
9. CLI drift remains warning-only for permission proof.
10. Reporting distinguishes direct evidence, `NOT_RUN`, degraded independence,
    and unverified host isolation.

---

## 18. Validation and Risks

### Validation Performed in This Phase

- **PASS:** Approved Phase 1 artifact reread in full.
- **PASS:** Baseline HEAD remains
  `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`.
- **PASS:** Phase 1 approval was explicitly supplied by the user.
- **NOT RUN:** Source implementation, focused tests, full regression, live Orca
  worker run.

### Risks

- `B-01`, `B-05`, `B-09` jointly affect persistent recovery and require strict
  migration tests.
- `B-03` contract expansion may increase malformed-artifact retries until
  prompts are updated in `B-11`.
- `B-07` adds two model calls only on conflict, increasing worst-case round
  duration.
- `B-08` changes new-run preflight behavior and must preserve legacy resume.
- Shared mirror lifecycle must not weaken existing read-only enforcement.

### Assumptions

- The single-active-step coordinator remains authoritative.
- Existing permission report and typed refresh-marker policy remains unchanged.
- Existing finding escalation semantics remain valid unless Micro Blocking
  reveals a direct contract contradiction.

### Open Questions

None. Any Micro Blocking discovery that changes approved behavior, public
interfaces, persistent data semantics, dependency set, or state-machine
contract requires returning to the earliest affected approved phase.

---

## 19. Approval

- [x] Macro Blocking approved
- [ ] Revision requested

**Next phase:** Phase 3 — Micro Blocking
