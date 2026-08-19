# Task Report: Orca Loop Cross-Validation Hardening

**Current Phase:** 4. Code Implementation
**Status:** Validation Passed

---

### 1. Context and Objective

- **Goal:** Worker 간 상호검증을 blind dual review와 symmetric adjudication으로 강화하고, review/test/consensus/human-gate evidence가 동일한 worktree snapshot에 결합된 경우에만 merge를 허용한다.
- **Scope:** 승인된 Phase 1~3 문서의 `B-01`~`B-13` 구현, 사용자가 지정한 permission environment warning-only 정책 유지, code-authoritative 운영 문서 정합화, reporting schema mismatch 수정.
- **Baseline:** 변경 전 `py -3 -m unittest discover -s tests -v`는 382 tests `PASS`였다.

### 2. Deliverables (Current Phase Focused)

#### 2.1 Blind dual review and adjudication

- `REVIEW_CONTEXT_PREPARE -> CODE_REVIEW_A -> CODE_REVIEW_B -> REVIEW_COMPARE` 상태 흐름을 추가했다.
- 두 blind reviewer는 같은 `CodeReviewRoundContext`, 같은 read-only mirror, 같은 logical input manifest를 사용한다.
- Blind B 입력 allowlist에서 Blind A artifact와 기존 peer review artifact를 제외한다.
- 두 결과가 일치하면 pair를 ledger에 원자 적용하고, 충돌하면 `ADJUDICATE_A -> ADJUDICATE_B`를 모두 통과한다.
- 각 adjudicator는 두 blind artifact와 comparison을 보지만 상대 adjudication artifact는 보지 않는다.
- acceptance criterion, affected file, test contract 전체 coverage와 evidence를 strict contract로 검증한다.
- non-`APPROVE` evaluation은 해당 criterion/file/test를 직접 참조하는 actionable finding이 없으면 거부한다.
- 새 run의 두 code-review worker provider가 같으면 기본 차단한다. 명시적 `--allow-same-provider-consensus`만 허용하며 manifest와 report에 `DEGRADED`로 고정한다.

#### 2.2 Atomic ledger and the prior vacuous check

- CODE lane의 기존 `execute_evaluate()` 내 `commit_round()` 호출과 `expected_snapshot_digest=evidence.reviewed_snapshot_digest` 자기참조 검사를 제거했다.
- CODE lane `commit_round()`는 verified Blind A/B pair 또는 agreed adjudication pair를 적용하는 atomic comparison 경로에서만 호출한다.
- `CONSENSUS_EVALUATE`는 이미 커밋된 CODE ledger의 unresolved finding count만 평가한다.
- PLAN lane `PLAN_CONSENSUS_EVALUATE`의 기존 `commit_round()` 동작은 유지했다.

#### 2.3 Snapshot lineage and resume safety

- Coordinator-owned `test_evidence.json`, `review_context.json`, `review_comparison.json`, adjudication artifacts, `ValidationLineage`, `PendingReviewRound`를 추가했다.
- 각 code-review round는 기존 mirror를 재사용하지 않고 새 `repository-<generation>` read-only mirror를 생성한 후 tree digest를 context에 결합한다.
- validation state의 resume drift는 기본 차단한다. 사용자가 drift를 명시 수락하면 기존 review/consensus/gate evidence를 폐기하고 `TEST_GATE`로 되돌린다.
- `PLAN_REVIEW` 또는 `PLAN_CONSENSUS_EVALUATE` drift 수락은 `PLAN_REVISE`로 되돌린다.
- merge 직전 live snapshot, test, context, blind A/B, comparison, adjudication, consensus lineage를 다시 검증한다.
- stale 또는 tampered human gate는 `INVALIDATED` 처리하고 merge하지 않은 채 `TEST_GATE`로 되돌린다.

#### 2.4 Contracts, reporting, and operations

- State/manifest schema migration을 추가해 legacy run을 읽되, legacy final gate는 새 validation evidence 없이 merge하지 못하게 했다.
- Plan report의 `acceptance_criteria.verification_method`, implementation report의 `addressed_findings.evidence_refs`, object-shaped `TestCommand` rendering을 수정했다.
- Plan Reviewer prompt에 bounded repository verification matrix를 추가했다.
- `orca_loop_execution_rules.md`를 실제 코드 기준으로 개정했다: 현재 Orca terminal이 coordinator이며 harness가 네 worker terminal을 provision한다.
- Agent CLI availability/version drift는 의도대로 informational warning-only를 유지했다. `platform`/`enforcement_digest` drift와 typed observed permission failure만 blocking/refresh 근거다.

### 3. Validation and Risks

#### 3.1 Final validation evidence

| Status | Command / Check | Result |
|---|---|---|
| `PASS` | `$files = Get-ChildItem -Path orca_loop -Filter *.py ...; py -3 -m py_compile run_loop.py @files` | 모든 Python module compile 성공 |
| `PASS` | `py -3 -m unittest discover -s tests -v` | 최종 재실행 394 tests, 56.013s, `OK` |
| `PASS` | `py -3 run_loop.py doctor` | Orca graph/runtime ready, current permission report usable, overall `status=PASS` |
| `PASS` | `git diff --check` | whitespace error 없음; Git의 LF-to-CRLF informational warning만 발생 |
| `PASS` | Added-line placeholder scan | 새 functional code에 `TODO`, `FIXME`, `pass`, `NotImplementedError` 없음 |
| `PASS` | CODE/PLAN `commit_round()` call-site audit | CODE atomic pair path 1개, PLAN evaluate path 1개, ledger definition 1개 확인 |
| `PASS` | Phase 3 correction audit | `M-B06-03` CODE call removal 문장 및 §18 `Preconditions` authority 문장 확인 |
| `PASS` | Corrected documentation search | `rg -g "codex-mhj_26_08_19_0*.md" ... docs` 성공 |
| `FAIL` | `py -3 -m py_compile orca_loop\*.py` | Windows shell이 wildcard를 확장하지 않아 발생한 명령 지정 오류; explicit file expansion으로 재실행해 `PASS` |
| `FAIL` | Nonexistent `tests.test_roles` 지정 시도 | 존재하지 않는 test module을 지정한 검증 명령 오류; discovery 전체 394 tests로 재검증해 `PASS` |
| `FAIL` | `rg ... docs/codex-mhj_26_08_19_0*.md` | Windows path argument에서 wildcard를 직접 사용한 명령 오류; `-g` glob으로 재실행해 `PASS` |
| `NOT RUN` | Live Orca four-worker end-to-end run | 실제 provider 호출, 비용, 외부 terminal mutation이 필요한 별도 운영 검증이므로 이번 구현 검증에서는 실행하지 않음 |
| `NOT RUN` | Real human merge decision | 실제 run의 pending gate가 없으며 merge/commit/push는 요청 범위가 아님 |

#### 3.2 Risks and boundaries

- 394-test suite는 state, contract, migration, provider policy, drift rollback, stale gate, reporting을 검증하지만 실제 Claude/Codex 응답 품질이나 장시간 Orca terminal lifecycle을 증명하지는 않는다.
- Read-only mirror와 repository guard는 target repository mutation을 통제한다. Host 전체 격리는 제공하지 않으므로 비신뢰 provider 실행에는 별도 OS/container sandbox가 필요하다.
- `run_loop.py` application layer가 커졌다는 유지보수 부담은 남아 있다. 이번 범위에서는 안전 불변조건을 먼저 구현했으며 후속 분해는 동작 변경과 분리해야 한다.
- 변경은 working tree에만 있으며 local commit과 push는 수행하지 않았다.

### 4. Approval Status

- [x] Current phase approved
- [x] Revision requested items reflected
- [x] Permission granted to proceed to Code Implementation
- [x] Code Implementation completed
- [x] Automated and static validation passed
