# Task Report: Orca Loop 상호검증 및 Evidence Lineage 강화

**Current Phase:** 1. System Design — Revision 2

**Status:** Approved — 2026-08-19

**Date:** 2026-08-19

**Applies to:** `C:\Users\mhj\Desktop\mhj_workspace\orca_harness`
**Baseline HEAD:** `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`

---

## 1. Context and Objective

### 1.1 Problem

현재 Orca Loop의 code-review lane은 이름과 prompt상으로는 `Code Reviewer`와
`Cross Confirmer`가 같은 frozen snapshot을 상호검증하는 구조다. 그러나 실제
input staging과 ledger 갱신 순서를 보면 두 번째 reviewer가 첫 번째 reviewer의
판단에 노출된다.

구체적으로 다음 문제가 있다.

- `_step_inputs()`는 존재하는 `plan.json`, `implementation.json`,
  `code_review.json`, `cross_review.json` 등을 역할 구분 없이 staging한다.
- `CROSS_CONFIRM`의 `reviewed_artifact_digest`는 첫 번째
  `code_review.json`을 가리킨다.
- 첫 번째 review가 shared consensus ledger에 즉시 반영되므로 두 번째
  reviewer의 `ScopePackage`와 `DELIVERED_FINDING_IDS`가 첫 번째 reviewer의
  finding에 의해 달라진다.
- 두 reviewer의 read-only mirror와 `frozen.diff`가 각 worker 실행 시점에
  별도로 생성된다.
- Reviewer에게 전달되는 test evidence는 주로 `PASS` 또는 `NOT_RUN` 상태이며,
  command, policy, snapshot을 포함한 durable evidence artifact가 없다.
- Resume 시 evidence가 생성된 snapshot과 현재 worktree snapshot이 달라져도
  일부 read-only state에서 자동 rebaseline되어 기존 evidence가 재사용될 수
  있다.

따라서 prompt에 "먼저 독립적으로 검토하라"고 추가하는 것만으로는 anchoring과
confirmation bias를 충분히 줄일 수 없다. 독립성은 prompt가 아니라 staging,
state, ledger commit, artifact provenance로 보장해야 한다.

### 1.2 Goal

다음 결과를 달성한다.

1. 두 code reviewer가 동일하게 봉인된 input을 상대 결과 없이 검토한다.
2. 첫 reviewer 결과가 두 번째 reviewer의 scope나 delivered finding을 바꾸지
   못한다.
3. 두 blind review가 모두 끝난 뒤 coordinator가 결과를 원자적으로 비교한다.
4. 충돌이나 한쪽만 제기한 finding이 있을 때만 symmetric adjudication을
   수행한다.
5. `APPROVE`에는 전체 acceptance criterion, affected file, test ID에 대한
   structured evidence가 필요하다.
6. 신규 run의 code-review lane은 기본적으로 서로 다른 provider를 사용한다.
7. 최종 merge 대상 snapshot과 test, review, adjudication, consensus evidence가
   동일함을 강제한다.
8. Reporting contract와 운영 문서를 현재 코드 및 승인된 permission 정책에
   맞춘다.

### 1.3 Out of Scope

- Host 전체를 격리하는 container 또는 별도 OS user sandbox
- Coordinator 내부에서 LLM을 사용한 semantic finding deduplication
- `run_loop.py` application layer 전면 분해
- 자동 majority vote
- Resume 중 provider, model, effort 변경
- Permission refresh를 CLI version drift만으로 강제하는 정책

---

## 2. Behavioral Baseline

### 2.1 Repository State

- Branch: `main`
- HEAD: `f8e4b0ebadc0b22c31d0face5d14b6ae1375b1c9`
- `git status --short`: clean

### 2.2 Executed Validation

| Command or Check | Status | Result |
| --- | --- | --- |
| `py -3 -m unittest discover -s tests -v` | **PASS** | 382 tests, `OK`, 50.387 seconds |
| `py -3 run_loop.py doctor` | **PASS** | Orca runtime/graph ready, usable permission report found |
| `git diff --check` | **PASS** | No whitespace errors |
| Current reporting-contract reproduction | **PASS** | `render_stage_report()` returned `None`; dict command caused `TypeError` |
| `HUMAN_GATE` resume-drift reproduction | **PASS** | Review snapshot A was replaced by current snapshot B in round evidence |
| Corrected `rg -e` source inspection | **PASS** | Current cross-review staging and digest coupling verified |
| Initial composite `rg` inspection | **FAIL** | PowerShell regex escaping error; corrected command passed |
| Live Orca worker/E2E run | **NOT RUN** | Phase 1 design validation did not start token-consuming workers |

### 2.3 Reproduced Snapshot Gap

The observed values were equivalent to:

```text
REVIEWED_SNAPSHOT=A
RESUMED_SNAPSHOT=B
ROUND_EVIDENCE_SNAPSHOT=B
STATE=HUMAN_GATE
```

This proves that the current code can construct new round evidence using the
current state snapshot even though the staged review artifacts were produced
for an earlier snapshot.

---

## 3. Goals and Non-Goals

### 3.1 Required Invariants

| ID | Invariant |
| --- | --- |
| `RQ-01` | Both blind reviewers receive the same sealed review context. |
| `RQ-02` | Neither blind reviewer receives the other reviewer's artifact or new findings. |
| `RQ-03` | The second reviewer uses the scope frozen before the first reviewer starts. |
| `RQ-04` | Blind artifacts are not applied to the shared ledger until both are verified. |
| `RQ-05` | Approval requires complete acceptance, file, and test coverage with evidence. |
| `RQ-06` | A conflict triggers symmetric adjudication using identical revealed inputs. |
| `RQ-07` | Neither adjudicator receives the other adjudicator's result. |
| `RQ-08` | New runs require different providers for the two code-review lanes by default. |
| `RQ-09` | Test, review, adjudication, consensus, and merge snapshots must match. |
| `RQ-10` | Missing independence or provenance evidence fails closed. |

### 3.2 Non-Goals

This design does not claim hostile-process secrecy. Both workers currently run
as the same OS user, so an intentionally malicious process could attempt to
search outside its staged input. The design provides workflow-level blindness
through sealed inputs and coordinator contracts. Strong host-level secrecy
requires a separate sandbox design.

---

## 4. Proposed Workflow

```text
IMPLEMENT / FIX
      ↓
TEST_GATE
      ↓
REVIEW_CONTEXT_PREPARE ── Coordinator
      │
      │  snapshot, mirror, diff, scope, test evidence sealed
      ↓
CODE_REVIEW_A ─────────── Primary blind review
      ↓
CODE_REVIEW_B ─────────── Secondary blind review
      │                    Review A is not staged
      ↓
REVIEW_COMPARE ────────── Coordinator
      ├─ complete agreement ─────────────┐
      │                                  ↓
      └─ conflict or unilateral finding  CONSENSUS_EVALUATE
                    ↓                    ↓
             ADJUDICATE_A             HUMAN_GATE / FIX
                    ↓
             ADJUDICATE_B
                    ↓
             CONSENSUS_EVALUATE
```

Execution remains sequential so the existing deterministic recovery model can
be preserved. Logical independence comes from sealing the input before either
review starts, not from concurrent execution.

---

## 5. Sealed Review Context

After `TEST_GATE`, the coordinator creates exactly one
`CodeReviewRoundContext`.

```text
CodeReviewRoundContext
├─ schema_version
├─ run_id
├─ consensus_round
├─ plan_version
├─ snapshot_digest
├─ implementation_artifact_digest
├─ test_evidence_digest
├─ frozen_diff_digest
├─ scope_manifest_digest
├─ readonly_mirror_digest
├─ baseline_finding_ids
├─ acceptance_criteria_ids
├─ affected_files
├─ test_ids
└─ context_digest
```

### 5.1 Preparation Flow

1. Capture the target worktree snapshot.
2. Persist coordinator-owned test evidence.
3. Create one round-specific read-only repository mirror.
4. Create `frozen.diff` and `scope-manifest.json` from the same snapshot.
5. Capture the target worktree snapshot again.
6. Reject the context if the before and after snapshot digests differ.
7. Persist the context and all component digests atomically.
8. Reuse the same context and mirror for both blind reviews and any
   adjudication steps.

The `context_digest` becomes the authority for review input identity.

---

## 6. Role-Specific Input Contracts

The current blanket `_step_inputs()` behavior is replaced with a role-specific
allowlist.

### 6.1 Primary Blind Reviewer

```text
request.md
plan.json
plan_review.json
implementation.json
test-evidence.json
review-context.json
frozen.diff
scope-manifest.json
```

### 6.2 Secondary Blind Reviewer

The secondary reviewer receives the exact same logical inputs and component
digests as the primary reviewer.

It must not receive:

```text
code_review_a.json
code_review_b.json
cross_review.json from an earlier design
findings created by the primary reviewer in the active round
a ScopePackage recalculated after the primary review
```

### 6.3 Adjudicator A and B

Both adjudicators receive:

```text
all sealed review inputs
code_review_a.json
code_review_b.json
review-comparison.json
```

Adjudicator A does not receive B's adjudication artifact. Adjudicator B does not
receive A's adjudication artifact.

### 6.4 Implementer and Fixer

The implementer receives only the findings and evidence accepted by the final
pair comparison or adjudication result. Rejected candidates and intermediate
review prose do not expand implementation scope.

---

## 7. Pending Review Evidence and Atomic Ledger Application

The first blind artifact must not be applied directly to the shared consensus
ledger.

```text
Blind review A verified
        ↓
pending review-round evidence only
        ↓
Blind review B verified
        ↓
deterministic pair validation
        ↓
atomic ledger application in one generation
```

This produces the following guarantees.

- Review A cannot change Review B's delivered finding IDs.
- Review A cannot change Review B's acceptance, file, or test scope.
- A coordinator crash after Review A can resume Review B using the original
  context digest.
- Review B never needs to infer which ledger entries existed before Review A.
- A pair is either fully applied or not applied.

Pending blind evidence is durable for recovery but is not consensus state.

---

## 8. Structured Review Coverage

A root-level `APPROVE` verdict is insufficient without structured coverage.
Each blind reviewer returns:

```text
acceptance_evaluations:
  {criterion_id, decision, evidence_refs}[]

file_evaluations:
  {path, operation, decision, evidence_refs}[]

test_evaluations:
  {test_id, decision, evidence_refs}[]

review_summary
findings
finding_decisions
```

### 8.1 Contract Rules

- `acceptance_evaluations` must exactly cover the approved plan's acceptance
  criteria.
- `file_evaluations` must exactly cover the approved `affected_files` contract.
- `test_evaluations` must exactly cover the plan's test IDs.
- Every `APPROVE` evaluation needs nonempty evidence.
- A bare `PASS` string is not evidence. References must identify source,
  `frozen.diff`, `test-evidence.json`, or another staged artifact location.
- `NOT_RUN` cannot be represented as verified behavior.
- A missing evaluation rejects the complete artifact.
- Optional style preferences remain non-blocking suggestions.

This changes approval from "no finding was emitted" to "the complete approved
scope was evaluated and supported by evidence."

---

## 9. Durable Test Evidence

`TestGateResult` is persisted as coordinator-owned `test-evidence.json` instead
of reducing it to one status value.

```text
TestEvidence
├─ schema_version
├─ test_gate_status
├─ test_policy_digest
├─ command definitions
├─ return codes
├─ timeout flags
├─ policy violations
├─ before_snapshot_digest
├─ after_snapshot_digest
├─ test_ids
├─ bounded output digests
└─ artifact_digest
```

For `PASS`, the authoritative test snapshot is the policy-validated
`after_snapshot`. For `NOT_RUN`, it is the captured `before_snapshot`. The
artifact must not describe `NOT_RUN` as successful execution.

Full test stdout and stderr are not copied without bounds. Command results and
bounded output digests are persisted, while detailed tails remain under the
existing local logging boundary.

---

## 10. Deterministic Review Comparison

`REVIEW_COMPARE` is coordinator-owned and does not call an LLM.

### 10.1 Direct Pair Acceptance

A blind pair can proceed without adjudication only when all of the following
hold.

- Both artifacts reference the same `review_context_digest`.
- Snapshot and plan version match the sealed context.
- Both artifacts exactly cover the baseline findings.
- Acceptance, file, and test coverage is complete.
- There is no decision conflict for a shared finding.
- Neither side has a unilateral blocking finding.
- Artifact and staged-input provenance is valid.

### 10.2 Adjudication Triggers

Adjudication is required when any of the following occurs.

- Only one reviewer raises a new blocking finding.
- The reviewers disagree about a baseline finding.
- The same finding ID has different normalized signatures.
- Acceptance, file, or test evaluations conflict.
- One reviewer reports adequate verification while the other reports
  `VERIFY_REQUIRED`.
- A finding may duplicate another finding but the equivalence cannot be proven
  deterministically.

The coordinator records exact differences in `review-comparison.json`. It does
not rewrite root causes or decide that differently worded findings are
semantically equal.

---

## 11. Symmetric Adjudication

Adjudication is executed only for a conflicting pair. Both adjudicators receive
the same two blind artifacts and the same deterministic comparison artifact.

Each candidate decision contains:

```text
candidate_id
decision: CONFIRM | REJECT | DUPLICATE | VERIFY_REQUIRED
duplicate_of
root_cause_assessment
required_action
evidence_refs
```

### 11.1 Adjudication Rules

- Both adjudicators may revise their blind decision, but must explain the
  evidence that changed it.
- Neither adjudicator sees the other adjudication output.
- A candidate confirmed by both becomes an actionable finding.
- A candidate rejected by both remains in audit history but does not enter
  implementation scope.
- `DUPLICATE` is accepted only if both sides select the same canonical
  candidate.
- Remaining disagreement never becomes an automatic approval.
- Existing `B1` through `B5`, `impact_class`, and escalation rules continue to
  route unresolved issues to `FIX` or `USER_DECISION_REQUIRED`.
- No majority vote is used.

Normal review rounds continue to use two model calls. Only conflicting rounds
use the two additional adjudication calls.

---

## 12. Provider Diversity

For new runs, the default code-review invariant is:

```text
code_reviewer_a.provider != code_reviewer_b.provider
```

The planning pair should also normally satisfy:

```text
planner.provider != plan_reviewer.provider
```

Provider diversity is not inferred from the `CLAUDE` and `CODEX` wire lane
names. It is checked from resolved runtime configuration.

An explicit escape hatch is provided for intentional same-provider runs:

```text
--allow-same-provider-consensus
```

The choice is persisted in:

- `run-manifest.json`
- dry-run output
- run summary
- final human report

An explicitly approved same-provider run remains executable but is reported as:

```text
consensus_independence=DEGRADED
reason=same runtime provider explicitly approved
```

Resume never changes a recorded provider combination. Legacy same-provider runs
are reported as `DEGRADED` rather than silently changing provider or model.

---

## 13. Plan Review Quality

Planning is asymmetric because the Planner creates the artifact that the Plan
Reviewer must inspect. It cannot use the same blind-pair mechanism without
adding a second independent planner.

The Plan Reviewer is instead required to provide structured coverage for:

- `affected_files` completeness
- integration points
- existing public interfaces
- acceptance criterion verifiability
- test policy and test contract equality
- repository factual claims
- security, data, API, and schema impact

The Plan Reviewer may read source to verify those bounded claims but must not
write a replacement plan or architecture. A plan `APPROVE` with missing
coverage is a contract violation.

---

## 14. Validation Lineage and Resume Safety

`CoordinatorState` records a backward-compatible validation lineage.

```text
ValidationLineage
├─ test_gate_snapshot_digest
├─ test_evidence_digest
├─ review_context_snapshot_digest
├─ review_context_digest
├─ blind_review_a_snapshot_digest
├─ blind_review_a_artifact_digest
├─ blind_review_b_snapshot_digest
├─ blind_review_b_artifact_digest
├─ review_comparison_digest
├─ adjudication_a_snapshot_digest
├─ adjudication_a_artifact_digest
├─ adjudication_b_snapshot_digest
├─ adjudication_b_artifact_digest
└─ consensus_snapshot_digest
```

### 14.1 Evidence Invalidation

- `IMPLEMENT` or `FIX` clears all downstream validation lineage.
- `TEST_GATE` records test evidence and clears review lineage.
- A new review context clears blind, comparison, adjudication, and consensus
  lineage.
- A new blind pair clears prior adjudication and consensus lineage.
- Consensus records a snapshot only after all required pair or adjudication
  evidence is valid.

### 14.2 Resume Drift Policy

| State Group | Default on Drift | With Explicit `--accept-worktree-drift` |
| --- | --- | --- |
| `PLAN`, `PLAN_REVISE` | Rebaseline | Rebaseline |
| `PLAN_REVIEW`, `PLAN_CONSENSUS_EVALUATE` | `BLOCKED` | Invalidate plan evidence and return to `PLAN_REVISE` |
| `IMPLEMENT`, `FIX` | `BLOCKED` | Rebaseline current write state and clear downstream evidence |
| `TEST_GATE` and later validation states | `BLOCKED` | Clear validation evidence and stale final gate, then return to `TEST_GATE` |
| `USER_DECISION_REQUIRED` | Use `blocked_from_state` | Plan path returns to `PLAN_REVISE`; code path returns to `TEST_GATE` |

The option accepts the current worktree as a new baseline. It never changes old
evidence so that it appears valid for the new snapshot.

---

## 15. Final Merge Invariant

Before final gate creation and immediately before applying a `merge` decision,
the coordinator checks:

```text
current_worktree_snapshot
= state.snapshot_digest
= test_gate_snapshot_digest
= review_context_snapshot_digest
= blind_review_a_snapshot_digest
= blind_review_b_snapshot_digest
= adjudication_a_snapshot_digest    # when adjudication was required
= adjudication_b_snapshot_digest    # when adjudication was required
= consensus_snapshot_digest
```

It also verifies all required artifact digests recorded in
`ValidationLineage`.

Any mismatch raises a typed resumable evidence-drift stop. The run does not
enter `READY_FOR_MERGE`, and a stale gate resolution cannot bypass the check.

Legacy states without sufficient lineage must repeat validation before merge.
The coordinator must not reconstruct missing provenance by assumption.

---

## 16. Reporting Corrections

The human-readable renderer is aligned with the current strict contracts.

- Render `acceptance_criteria[].verification_method` instead of `statement`.
- Render `addressed_findings[].evidence_refs` instead of `resolution`.
- Render `TestCommand.argv`, `cwd`, `timeout_ms`, and `kind` explicitly.
- Replace outdated reporting test fixtures with current contract-shaped data.
- Verify that a valid artifact produces a report without creating
  `logs/reporting.log`.
- Show provider/model/effort for both review lanes.
- Show whether blind comparison or adjudication produced the final consensus.
- Show `review_context_digest`, snapshot lineage, coverage counts, and
  independence status in the final report.

Reporting remains best-effort and non-fatal, but all known current contract
shapes receive regression coverage.

---

## 17. Permission and Operations Documentation

The current permission behavior remains authoritative.

- `platform` or `enforcement_digest` drift is blocking.
- Claude/Codex CLI version or availability drift is warning-only.
- A refresh marker is created only after an observed typed permission failure.
- `environment.py` documentation is corrected without changing
  `compare_environment()` behavior.

`orca_loop_execution_rules.md` is aligned to current code.

- `start` does not create the coordinator terminal.
- The current `ORCA_TERMINAL_HANDLE` or explicit `--coordinator-handle` is used.
- Four worker terminals are automatically provisioned.
- Provider, model, and effort defaults exist in code.
- Explicit operator confirmation is an operational recommendation, not a parser
  requirement.
- Consensus lane names do not prove runtime provider diversity.
- Repository read-only enforcement and host-wide sandboxing are documented as
  different security boundaries.

---

## 18. Error and Exception Strategy

- Invalid review context or artifact provenance is rejected before ledger
  application.
- Incomplete review coverage is a retryable artifact contract violation within
  the existing operational retry budget.
- Snapshot drift is a typed resumable stop, not proof of repository corruption.
- Missing legacy lineage blocks merge and routes through revalidation.
- An unresolved adjudication does not become approval through timeout or round
  exhaustion.
- Existing escalation and human-decision boundaries remain authoritative.
- Test policy violations continue to prevent code review.

---

## 19. Security and Compatibility

### 19.1 Security

- Reviewers retain read-only repository mirrors.
- Implementer write access remains scoped by repository delta guards.
- Reviewer artifacts are excluded from blind staged inputs.
- The coordinator owns test execution, context sealing, comparison, and ledger
  application.
- Host-level artifact secrecy is not claimed.

### 19.2 Compatibility

- Public application APIs are unaffected.
- Existing CLI provider/model/effort options remain available.
- `--allow-same-provider-consensus` is additive.
- Resume configuration remains immutable.
- Legacy state decoding supplies empty lineage values, but final merge requires
  new validation evidence.
- Existing same-provider runs remain resumable and are marked `DEGRADED`.
- Existing permission refresh semantics remain unchanged.

---

## 20. Validation Strategy

### 20.1 Focused Tests

The implementation must add or update regression tests covering:

1. Secondary blind input does not contain the primary artifact.
2. Both blind reviewers receive the same sealed component digests.
3. Review A cannot change Review B's `ScopePackage` or finding IDs.
4. A crash after Review A resumes Review B with the same context.
5. Blind artifacts remain outside the shared ledger until pair comparison.
6. Snapshot or context mismatch rejects the pair.
7. Complete agreement skips adjudication.
8. A unilateral finding or decision conflict invokes both adjudicators.
9. Neither adjudicator receives the other adjudication artifact.
10. Missing acceptance, file, or test coverage rejects approval.
11. Evidence-free approval is rejected.
12. `NOT_RUN` cannot be reported as verified behavior.
13. Same-provider new runs are blocked by default.
14. The explicit same-provider escape hatch is durable and visibly degraded.
15. Final snapshot-lineage mismatch rejects merge.
16. Legacy final-gate runs require revalidation.
17. Current reporting contracts render without reporting errors.
18. CLI drift remains warning-only for permission proof.

### 20.2 Final Validation Commands

```powershell
py -3 -m unittest discover -s tests -v
py -3 run_loop.py doctor
git diff --check
git status --short
```

Any live Orca E2E run is reported separately from unit and static validation.
It must not be marked `PASS` unless actually executed successfully.

---

## 21. Risks and Open Questions

### 21.1 Risks

- Artifact schemas and state transitions become more complex.
- A conflicting review round uses two additional model calls.
- Different reviewers may describe the same defect with different IDs,
  increasing adjudication frequency.
- Legacy final-gate runs must repeat validation.
- Workflow-level blindness does not prevent a malicious same-user process from
  searching the host filesystem.

### 21.2 Assumptions

- The current deterministic, single-active-step coordinator model is retained.
- Different providers provide useful diversity but do not by themselves prove
  correctness.
- Coordinator comparison remains deterministic and does not make semantic
  equivalence decisions.
- Existing `B1` through `B5`, `impact_class`, and human escalation semantics
  remain valid.

### 21.3 Resolved Policy Questions

- CLI version and availability drift remains warning-only.
- Current code is authoritative when operations documentation is stale.
- Same-provider consensus is allowed only through an explicit, durable escape
  hatch for new runs.
- Normal review rounds retain two model calls; adjudication is conditional.

---

## 22. Approval

- [x] System Design Revision 2 approved
- [ ] Revision requested

**Next phase:** Phase 2 — Macro Blocking
