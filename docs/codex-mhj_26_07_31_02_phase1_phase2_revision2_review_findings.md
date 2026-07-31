# Task Report: Orca 자동화 루프 Phase 1·2 Revision 2 재검토

**Current Phase:** Phase 1 System Design / Phase 2 Macro Blocking Review
**Status:** Revision Requested
**작성일:** 2026-07-31
**검토 대상:**

- `docs/phase1-system-design.md` Revision 2
- `docs/phase2-macro-blocking.md` Revision 2
- `docs/codex-mhj_26_07_31_01_phase1_phase2_review_findings.md`

---

## 1. Context and Objective

### 1.1 검토 목적

Revision 1 검토에서 제기된 finding과 사용자가 추가한 합의-loop 요구사항이 Revision 2의
실제 contract, state transition, pseudocode, validation에 일관되게 반영됐는지 검증한다.

다음 조건을 중점적으로 확인한다.

1. lifecycle message가 active `taskId`와 `dispatchId`에 묶이는가
2. reviewer가 동일 snapshot을 검토했음을 증명하는가
3. 합의된 finding이 다음 round에서 제외되는가
4. 합의되지 않은 finding과 dependency closure만 다음 round에 전달되는가
5. 합의 round가 최대 5회라는 사용자 요구와 실제 counter가 일치하는가
6. 5회 후 자동 승인하지 않고 사용자 판단으로 전환하는가
7. test, reviewer, cross-review 순서가 각 역할 contract와 일치하는가
8. crash 후 active dispatch를 중복 실행하지 않고 resume할 수 있는가
9. 역할별 permission profile이 source와 runtime artifact를 실제로 보호하는가

### 1.2 검토 제외 범위

- Phase 3 Micro Blocking 작성
- Python source 구현
- 실제 worker dispatch
- unit test 및 E2E 실행

---

## 2. Overall Verdict

Revision 2는 Revision 1보다 구조적으로 크게 개선됐다.

적절하게 개선된 부분:

- `taskId`와 `dispatchId` provenance 검증
- 대상 repository 밖의 `runs/<run_id>/` artifact workspace
- cumulative `HEAD` diff 대신 step delta 검사
- untracked file을 포함한 snapshot identity
- `merge`, `reject`, `revise` 분리
- `TEST_GATE=NOT_RUN`의 자동 성공 경로 제거
- unresolved scope package와 `RESOLVED` finding 제외
- 사용자 판단 문서 및 `USER_DECISION_REQUIRED`
- run lock과 durable step stage 도입

그러나 현재 문서에는 실제 loop의 수렴과 실행을 막는 P0 defect가 남아 있다.

```text
Phase 1 Revision 2: REVISE
Phase 2 Revision 2: REVISE
Phase 3 Micro Blocking: NOT APPROVED
```

---

## 3. Blocking Findings

### REV2-001 — P0 — `CODE_REVIEW` 승인 조건과 `TEST_GATE` 실행 순서가 모순됨

#### Evidence

- `phase1-system-design.md:379-385`
- `phase1-system-design.md:881-888`
- `phase2-macro-blocking.md:530-540`
- `phase2-macro-blocking.md:994-999`

`code_reviewer` contract는 `TEST_GATE == PASS`를 승인 조건으로 요구한다. 그러나 state
transition은 다음 순서다.

```text
IMPLEMENT 또는 FIX
→ CODE_REVIEW
→ TEST_GATE
→ CROSS_CONFIRM
```

Claude가 `CODE_REVIEW`를 수행할 때 `TEST_GATE`는 아직 실행되지 않았다. contract를 지키면
승인할 수 없고, 승인하면 contract를 위반한다.

#### Impact

- 정상 구현도 `CODE_REVIEW`에서 승인될 수 없다.
- reviewer가 실행되지 않은 test를 `PASS`로 가정하게 된다.
- approval obligation 검증이 reviewer를 잘못 contract violation으로 판정할 수 있다.

#### Required Change

상태 순서를 다음과 같이 변경한다.

```text
IMPLEMENT 또는 FIX
→ TEST_GATE
→ CODE_REVIEW
→ CROSS_CONFIRM
→ CONSENSUS_EVALUATE
```

Codex가 source를 수정할 때마다 기존 test 결과와 reviewer approval을 stale 처리하고
`TEST_GATE`부터 다시 실행한다.

#### Acceptance Criteria

- `code_reviewer`에게 전달되는 `test_gate_result`가 항상 `PASS`다.
- `FAIL`은 reviewer dispatch 전에 `FIX` 또는 `USER_DECISION_REQUIRED`로 전이한다.
- `NOT_RUN`은 reviewer dispatch 전에 `USER_DECISION_REQUIRED`로 전이한다.
- 수정 후 이전 `PASS`를 재사용하지 않는다.

---

### REV2-002 — P0 — 기존 finding을 `RESOLVED`로 만드는 payload contract가 없음

#### Evidence

- `phase1-system-design.md:567-639`
- `phase1-system-design.md:644-667`
- `phase2-macro-blocking.md:450-470`
- `phase2-macro-blocking.md:806-812`

`apply_review()`은 현재 payload의 `blocking_findings`만 순회한다. 수정이 완료돼 reviewer가
`APPROVE`하면 blocking finding 목록은 비어 있다. 따라서 이전 round의 `OPEN`,
`CHANGE_REQUIRED`, `VERIFY_REQUIRED` finding을 승인했다는 정보가 원장에 들어오지 않는다.

현재 payload에는 다음 정보가 없다.

- 이번 reviewer가 실제로 검증한 finding ID
- 각 finding에 대한 `APPROVE`, `CHANGE_REQUIRED`, `VERIFY_REQUIRED` decision
- finding을 `RESOLVED`로 만들 evidence

`implementation.addressed_findings`는 존재하지만 구현 완료 신고일 뿐 dual approval이 아니다.

#### Impact

- 한 번 생성된 finding이 원장에 계속 unresolved로 남을 수 있다.
- `PLAN_CONSENSUS_EVALUATE.unresolved_zero`에 도달하지 못한다.
- `CONSENSUS_EVALUATE.consensus_reached`에 도달하지 못한다.
- 정상적으로 수정된 finding이 `E-05` 반복 finding으로 잘못 escalation될 수 있다.

#### Required Change

review payload에 scoped per-finding decision을 추가한다.

```json
{
  "reviewed_finding_ids": [
    "CODE-004"
  ],
  "finding_decisions": [
    {
      "id": "CODE-004",
      "decision": "APPROVE",
      "snapshot_digest": "sha256:current_snapshot",
      "evidence": [
        "T-AUTH-07:PASS"
      ]
    }
  ]
}
```

원장 handler를 역할별로 분리한다.

```text
apply_plan()
apply_plan_review()
apply_implementation()
apply_code_review()
apply_cross_review()
```

전이 규칙:

```text
implementer.addressed_findings
    → CHANGE_REQUIRED 또는 OPEN
    → VERIFY_REQUIRED

Claude APPROVE + Codex APPROVE
+ 동일 snapshot_digest
+ required evidence 존재
    → RESOLVED
```

#### Acceptance Criteria

- 이전 round의 finding이 빈 finding 목록만으로 조용히 사라지지 않는다.
- scoped finding마다 양측 decision을 기록한다.
- 한쪽 approval만으로 `RESOLVED`가 되지 않는다.
- 두 approval과 evidence가 충족되면 실제로 `RESOLVED`가 된다.

---

### REV2-003 — P0 — “유효 합의 round”와 실제 counter 증가 위치가 다름

#### Evidence

- `phase1-system-design.md:793-817`
- `phase2-macro-blocking.md:427-432`
- `phase2-macro-blocking.md:521-547`

문서는 양측이 같은 version/snapshot을 검토하고 두 payload가 유효해야 한 round로 계수한다고
정의한다. 그러나 state transition은 다음 신호 각각에서 round counter를 증가시킨다.

- `PLAN_REVIEW.REVISE`
- `PLAN_CONSENSUS_EVALUATE.unresolved_remain`
- `CODE_REVIEW.CHANGES_REQUESTED`
- `TEST_GATE.FAIL`
- `CROSS_CONFIRM.CHANGES_REQUESTED`
- `CONSENSUS_EVALUATE.unresolved_remain`
- human `revise`

하나의 end-to-end consensus cycle이 여러 round를 소비할 수 있다.

#### Impact

- 사용자가 요구한 5개 유효 round가 보장되지 않는다.
- 실제로는 2~3개의 agent interaction만으로 상한을 소진할 수 있다.
- `ConsensusLedger`와 state-machine budget이 서로 다른 값을 가질 수 있다.
- resume 후 어떤 counter가 authoritative한지 결정할 수 없다.

#### Required Change

1. round counter의 authoritative source를 `ConsensusLedger` 하나로 제한한다.
2. `is_valid_round()`이 `true`인 경우에만 한 번 증가시킨다.
3. `CONSENSUS_EVALUATE`에서만 round count를 commit한다.
4. 다음 항목은 consensus round를 소비하지 않는다.
   - operational retry
   - implementation step
   - test failure
   - malformed payload
   - worker restart
5. state machine은 ledger의 committed round를 읽고 limit만 판정한다.

#### Acceptance Criteria

- 한 consensus cycle에서 counter가 최대 1만 증가한다.
- 다섯 번째 유효 round까지 자동 승인 또는 조기 상한 소진이 없다.
- state와 ledger round 값이 항상 일치한다.

---

### REV2-004 — P0 — self-contained plan을 `worker_done --payload`로 전달할 수 없음

#### Evidence

- `phase1-system-design.md:284-301`
- `phase1-system-design.md:504-531`
- `phase1-system-design.md:1061`
- `phase2-macro-blocking.md:308-325`

planner와 reviewer 산출물을 `orca orchestration send --payload <json>`으로 전달하도록
설계했다. Windows command line은 문서 자체가 기록한 것처럼 약 32,767자 제한이 있다.

현재 Revision 2 `phase1-system-design.md`는 73,210 bytes다. self-contained plan을 JSON
문자열과 quoting을 포함해 command argument로 전달하면 이 제한을 초과할 가능성이 높다.
scope 축소는 두 번째 round 이후에는 유효하지만 최초 plan 전달 문제를 해결하지 못한다.

#### Impact

- planner가 정상 plan을 작성해도 `worker_done`을 전송하지 못할 수 있다.
- payload가 잘리거나 quoting이 깨질 수 있다.
- coordinator가 malformed JSON으로 오판하고 operational retry를 소진할 수 있다.

#### Required Change

큰 artifact는 파일 transport를 사용한다.

```text
runs/<run_id>/outbox/<dispatch_id>/<artifact-name>
```

worker는 dispatch 전용 outbox에만 기록한다. `worker_done` payload에는 다음 small metadata만
전달한다.

```json
{
  "taskId": "task_current",
  "dispatchId": "dispatch_current",
  "reportPath": "runs/current/outbox/dispatch_current/plan.md",
  "artifactDigest": "sha256:artifact_digest"
}
```

coordinator는 다음 순서로 artifact를 승격한다.

1. active task/dispatch provenance 검증
2. report path가 dispatch 전용 outbox 내부인지 검증
3. artifact digest 검증
4. artifact schema 검증
5. canonical `artifacts/` 경로로 atomic move

#### Acceptance Criteria

- 100 KiB 이상의 plan artifact가 Windows command argument를 사용하지 않고 전달된다.
- `worker_done` payload는 bounded metadata만 포함한다.
- 다른 dispatch의 outbox를 참조할 수 없다.

---

### REV2-005 — P1 — native Orca `escalation`이 사용자 판단이 아니라 `FAILED`로 처리됨

#### Evidence

- `phase1-system-design.md:920-943`
- `phase1-system-design.md:971-986`
- `phase2-macro-blocking.md:632-650`
- `phase2-macro-blocking.md:783-812`

원칙 4는 escalation 발생 시 `USER_DECISION_REQUIRED`를 요구한다. 그러나 coordinator
pseudocode는 native `ESCALATION` completion을 다음과 같이 처리한다.

```text
return abort("worker_escalation")
```

Phase 1 Error Strategy도 `escalation` message를 `FAILED` 조건으로 분류한다.

#### Impact

- worker가 정상적으로 사용자 판단을 요청해도 실패 상태가 된다.
- `user-decision.md`가 생성되지 않을 수 있다.
- payload의 `escalation_signals`와 native message의 처리 결과가 달라진다.

#### Required Change

```text
native ESCALATION
→ validate taskId/dispatchId
→ normalize escalation reason/evidence
→ USER_DECISION_REQUIRED
→ user-decision.md
→ decision gate
```

provenance 위반, malformed escalation contract처럼 실제 계약 오류만 `FAILED`로 처리한다.

#### Acceptance Criteria

- valid native escalation은 `FAILED`가 되지 않는다.
- payload escalation과 native escalation이 같은 사용자 escalation 흐름을 사용한다.

---

### REV2-006 — P1 — `STEP_DISPATCHED`가 worker 완료 후에 기록됨

#### Evidence

- `phase2-macro-blocking.md:620-650`
- `phase2-macro-blocking.md:747-789`

`dispatcher.run_step()`은 task 생성, dispatch, 완료 대기까지 수행한다. coordinator는
`run_step()`이 반환된 후 `STEP_DISPATCHED`를 기록한다.

worker가 실행 중일 때 coordinator process가 종료되면 active `task_id`와 `dispatch_id`가
durable state에 남지 않는다.

#### Impact

- resume이 active dispatch를 찾지 못한다.
- 이미 실행 중이거나 완료된 task를 재dispatch할 수 있다.
- duplicate source modification이 발생할 수 있다.

#### Required Change

dispatcher API를 분리한다.

```text
create_and_dispatch()
    → task_id, dispatch_id

persist STEP_DISPATCHED
    → active task_id, dispatch_id, role, terminal identity

wait_for_completion()
```

추가로 `state.json`과 `ledger.json`을 각각 atomic replace하는 것은 두 파일 전체의 atomic
transaction이 아니다. 동일 generation number를 기록하고 commit manifest를 마지막에
atomic replace해야 한다.

#### Acceptance Criteria

- worker 실행 중 crash 후 active dispatch를 재식별한다.
- 완료된 dispatch를 재실행하지 않는다.
- state와 ledger generation 불일치를 resume 시 탐지한다.

---

### REV2-007 — P1 — reviewer에게 전체 `run_dir` 쓰기 권한이 부여됨

#### Evidence

- `phase1-system-design.md:282-301`
- `phase2-macro-blocking.md:174-188`

Codex `--add-dir <DIR>`는 해당 directory를 primary workspace와 함께 writable directory로
추가한다. reviewer profile에 `--add-dir <run_dir>`가 포함돼 있으므로 `state.json`,
`ledger.json`, 다른 reviewer artifact까지 수정할 수 있다.

이는 다음 주장과 충돌한다.

```text
읽기 전용 역할은 파일을 쓰지 않는다.
coordinator만 artifact를 기록한다.
```

#### Impact

- reviewer가 state 또는 ledger를 변경할 수 있다.
- coordinator-only artifact ownership을 기술적으로 증명하지 못한다.
- target worktree delta 검사만으로 run directory mutation을 발견하지 못한다.

#### Required Change

- reviewer에게 전체 run directory write permission을 주지 않는다.
- write가 필요하면 dispatch-specific outbox만 허용한다.
- `state.json`, `ledger.json`, canonical artifacts는 worker writable root 밖에 둔다.
- coordinator가 worker step 전후 outbox와 run metadata delta를 검사한다.

#### Acceptance Criteria

- reviewer가 `state.json`과 `ledger.json`을 수정하려 하면 실행 전에 거부된다.
- reviewer는 자신의 dispatch outbox 외 파일을 만들거나 변경할 수 없다.

---

### REV2-008 — P1 — 승인된 plan의 test command를 coordinator 권한으로 직접 실행함

#### Evidence

- `phase1-system-design.md:501-530`
- `phase1-system-design.md:1035-1046`
- `phase2-macro-blocking.md:737-745`
- `phase2-macro-blocking.md:814-820`

`Test Contract.commands[]`는 string 목록이고 coordinator가 `run_shell_all()`로 실행한다.
shell metacharacter, destructive Git command, DB mutation, external side effect를 제한하는
machine-level contract가 없다.

#### Impact

- LLM이 생성한 arbitrary shell command가 coordinator 권한으로 실행된다.
- post-diff guard가 DB나 network side effect를 복구하지 못한다.
- `git clean`, `git reset`, commit, push 같은 동작을 최종 diff만으로 탐지하지 못할 수 있다.

#### Required Change

Test Contract를 structured command로 변경한다.

```json
{
  "commands": [
    {
      "argv": [
        "py",
        "-3",
        "-m",
        "unittest",
        "discover",
        "tests"
      ],
      "cwd": ".",
      "timeout_ms": 1800000,
      "kind": "unit"
    }
  ],
  "test_ids": [
    "T-AUTH-07"
  ]
}
```

정책:

- 기본 `shell=False`
- shell metacharacter 거부
- repository 밖 `cwd` 거부
- destructive Git/DB/network command 거부
- DB 또는 external integration test는 별도 사용자 승인

#### Acceptance Criteria

- command injection 문자열이 실행되지 않는다.
- 승인되지 않은 DB 또는 external side effect test는 `USER_DECISION_REQUIRED`가 된다.

---

### REV2-009 — P1 — 5회 합의 요구와 `E-05` 2회 escalation이 충돌함

#### Evidence

- `phase1-system-design.md:934-965`
- `phase1-system-design.md:1128-1139`
- `phase2-macro-blocking.md:434-445`
- `phase2-macro-blocking.md:1041-1059`

사용자는 미합의 부분을 최대 약 5회 반복한 뒤에도 합의하지 못하면 사용자 판단을 요청하도록
지시했다. 현재 `E-05`는 동일 finding이 두 round에서 `OPEN`이면 즉시 escalation한다.

문서도 `Q-1`과 `MR-6`에서 이 충돌을 인정하고 있다.

#### Impact

- 5회 합의 loop가 대부분 두 번째 round에서 종료된다.
- `plan_consensus_round_limit=5`, `code_consensus_round_limit=5`의 의미가 약해진다.
- unresolved scope만 집중 검토한다는 사용자 요구가 충분히 실행되지 않는다.

#### Required Change

다음 중 하나를 사용자가 명시적으로 확정해야 한다.

```text
Option A
- 동일 finding도 최대 5개 유효 round까지 검토
- 5회 후 USER_DECISION_REQUIRED

Option B
- 동일 finding이 material progress 없이 2개 유효 round 반복되면 즉시 escalation
- E-05가 5회 기본 상한보다 우선
```

현재 대화에서 사용자가 명시한 5회 기준을 적용한다면 `Option A`가 baseline이다.

#### Acceptance Criteria

- Q-1을 미확정으로 남기지 않는다.
- chosen policy와 state-machine test가 정확히 일치한다.
- round 상한 도달이 자동 승인을 만들지 않는다.

---

### REV2-010 — P2 — Human `revise`가 설계 수정인지 구현 수정인지 알 수 없음

#### Evidence

- `phase2-macro-blocking.md:851-878`

human gate option은 `merge`, `reject`, `revise`뿐이다. pseudocode는
`affects_design(res)`로 `REVISE_DESIGN`과 `REVISE_CODE`를 나누지만 `res == "revise"`에는
판단 근거가 없다.

#### Required Change

다음처럼 option을 분리한다.

```text
merge
reject
revise_code
revise_design
```

또는 `revise` 선택 후 두 번째 structured gate에서 대상 phase와 finding을 선택하게 한다.

#### Acceptance Criteria

- user resolution만으로 next state가 결정된다.
- coordinator가 수정 수준을 추측하지 않는다.

---

### REV2-011 — P2 — Macro Block 필수 필드 `PASS` 주장이 실제 문서와 다름

#### Evidence

- `phase2-macro-blocking.md:352-364`
- `phase2-macro-blocking.md:952-1007`
- `phase2-macro-blocking.md:1013-1022`

문서는 13개 Macro Block이 필수 필드를 모두 갖췄다고 `PASS` 처리한다. 정적 검사 결과:

- `B-05`: `Dependencies` 필드 누락
- `B-13`: 명시적인 `High-Level Pseudocode` section 누락

#### Required Change

`B-05`에 다음 필드를 추가한다.

```text
Dependencies: B-01
```

`B-13`의 필수 증명 test code를 명시적인 `High-Level Pseudocode` section 아래에 배치한다.

#### Acceptance Criteria

- 13개 block 모두 required field 검사 통과
- Validation table의 `PASS`가 실제 검사 결과와 일치

---

## 4. Previous Finding Resolution Matrix

| Previous Finding | Revision 2 판정 | 근거 |
|---|---|---|
| `PLAN-001` lifecycle provenance | **Mostly Resolved** | active task/dispatch 검증 반영. native escalation 처리 수정 필요 |
| `PLAN-002` snapshot/step delta | **Resolved at design level** | 외부 run workspace, snapshot identity, step delta 반영 |
| `PLAN-003` human gate | **Mostly Resolved** | merge/reject 분리. revise 세분화 필요 |
| `PLAN-004` permission enforcement | **Partially Resolved** | launch profile 추가. run directory write와 implementer/test command 통제 미완 |
| `PLAN-005` `NOT_RUN` | **Resolved pending Q-2** | 자동 success 제거. 사용자 정책 확정 필요 |
| `PLAN-006` freshness/convergence | **Partially Resolved** | provenance 도입. finding resolution contract 미완 |
| `PLAN-007` crash-safe resume | **Partially Resolved** | durable stage 도입. dispatch persistence 순서와 multi-file commit 미완 |
| `PROCESS-001` approval status | **Resolved for reporting** | Phase 2가 unapproved baseline임을 명시 |
| `PROCESS-002` Macro fields | **Partially Resolved** | 기존 B-09 수정. B-05와 B-13 누락 발견 |
| Unresolved-only scope | **Resolved at design level** | `RESOLVED` 제외와 dependency closure 반영 |
| 5-round user escalation | **Partially Resolved** | user report 반영. round counter와 E-05 충돌 잔존 |

---

## 5. Required Revision Order

Revision 3에서는 다음 순서를 권장한다.

1. `TEST_GATE`와 `CODE_REVIEW` 순서 수정
2. per-finding resolution contract와 역할별 ledger handler 정의
3. valid round 단일 계수 규칙 정의
4. large artifact outbox transport 정의
5. native escalation을 `USER_DECISION_REQUIRED`로 통합
6. dispatch persistence와 generation commit 정의
7. reviewer writable root 축소
8. structured Test Contract와 execution policy 정의
9. `E-05`와 5-round 정책 확정
10. human revise option과 Macro 필드 보완

---

## 6. Validation and Risks

### 6.1 Validation Performed

| 항목 | 상태 | 결과 |
|---|---|---|
| Phase 1 Revision 2 문서 검사 | `PASS` | 1,313 lines, 73,210 bytes |
| Phase 2 Revision 2 문서 검사 | `PASS` | 1,068 lines, 64,722 bytes |
| current Orca status | `PASS` | runtime ready, `appVersion=1.4.159` |
| version-matched orchestration guide | `PASS` | active dispatch provenance와 lifecycle contract 확인 |
| `orca orchestration task-list --brief --json` | `PASS` | task 0 |
| `orca repo list --json` | `PASS` | `orca_harness=kind:folder` |
| `codex --help` | `PASS` | `--sandbox`, `--ask-for-approval`, `--add-dir` 확인 |
| `claude --help` | `PASS` | permission/tool flags 확인 |
| Macro Block required field 검사 | `PASS` | `B-05.Dependencies`, `B-13 High-Level Pseudocode` 누락 탐지 |
| source implementation | `NOT RUN` | read-only review 범위 |
| unit/E2E tests | `NOT RUN` | Phase 4 대상 |

### 6.2 Diagnostic Command Failures

| 항목 | 상태 | 설명 |
|---|---|---|
| 최초 memory 병렬 조회 | `FAIL` | 관련 memory hit 없음의 `rg` exit code 1이 묶음 실패로 반환. 분리 재실행은 `PASS` |
| 정규식 검색 1건 | `FAIL` | PowerShell quoting 오류. 단순화 후 재실행 `PASS` |
| `--add-dir` 검색 1건 | `FAIL` | pattern이 option으로 해석됨. `-e` 사용 후 재실행 `PASS` |

위 진단 실패는 repository나 문서 실행 실패가 아니라 검토용 검색 command 오류다.

---

## 7. Approval Status

- [ ] Phase 1 System Design Revision 2 approved
- [x] Phase 1 Revision 3 requested
- [ ] Phase 2 Macro Blocking Revision 2 approved
- [x] Phase 2 Revision 3 requested
- [ ] Permission granted to proceed to Phase 3

**Next action:** Claude가 `REV2-001`~`REV2-011`을 Phase 1 Revision 3에 반영하고,
사용자가 Phase 1을 명시적으로 승인한 뒤 Phase 2 Revision 3을 확정해야 한다.
