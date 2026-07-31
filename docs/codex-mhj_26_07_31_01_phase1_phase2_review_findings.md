# Task Report: Orca Claude/Codex 자동화 루프 Phase 1·2 검토

**Current Phase:** Phase 1 System Design / Phase 2 Macro Blocking Review
**Status:** Revision Requested
**작성일:** 2026-07-31
**검토 대상:**

- `docs/phase1-system-design.md`
- `docs/phase2-macro-blocking.md`

---

## 1. Context and Objective

### 1.1 검토 목적

Claude가 작성한 Phase 1 System Design과 Phase 2 Macro Blocking이 다음 사용자 요구를
안전하고 결정론적으로 구현할 수 있는지 검토한다.

1. Claude가 계획을 작성한다.
2. Codex가 계획을 검토한다.
3. Claude가 지적사항을 반영한다.
4. 합의된 계획으로 Codex가 구현한다.
5. Claude가 구현을 검토한다.
6. Codex가 지적사항을 반영한다.
7. Claude와 Codex가 동일한 결과물에 대해 합의한다.
8. 자동 테스트가 통과한 결과만 사용자 최종 판단 대상으로 전달한다.
9. 최종 merge, push, 파괴적 변경은 자동 실행하지 않는다.

### 1.2 이번 검토에서 추가된 요구

기존 문서의 계획 수정 2회, 구현 수정 3회 제한을 다음 원칙으로 변경해야 한다.

- 합의를 이루지 못했다고 자동 승인하지 않는다.
- 계획 합의와 구현 합의는 각각 기본 최대 5개 유효 round를 허용한다.
- 이미 합의된 finding은 이후 round의 검토 입력에서 제외한다.
- 다음 round에는 합의되지 않은 finding과 그 finding의 영향 범위만 전달한다.
- 5개 유효 round 후에도 합의하지 못한 finding이 있으면 자동 통과시키지 않는다.
- 상태를 `USER_DECISION_REQUIRED`로 전환하고 사용자 판단용 문서를 생성한다.
- 사용자 결정이 내려질 때까지 전체 작업 상태는 `BLOCKED`로 유지한다.

### 1.3 검토 범위

- Phase 1과 Phase 2의 구조적 정합성
- Orca lifecycle message와 task/dispatch provenance
- Git baseline, 변경 범위, review snapshot 고정
- 역할별 권한 강제
- 계획·구현·테스트·합의 상태 전이
- 반복 제한과 사용자 escalation
- 재개 가능성과 artifact freshness

### 1.4 검토 제외 범위

- Phase 3 Micro Blocking 작성
- Python source 구현
- 실제 Claude/Codex worker dispatch
- E2E 자동화 실행

---

## 2. Overall Verdict

### 2.1 타당한 방향

다음 설계 방향은 유지할 가치가 있다.

- Python deterministic coordinator가 상태 전이와 반복 예산을 관리한다.
- Orca는 terminal, task, dispatch, message, decision gate를 관리한다.
- Claude와 Codex는 계획·검토·구현 역할을 분리한다.
- 동일 feature worktree를 순차적으로 사용한다.
- 자유 서술이 아니라 JSON verdict로 전이한다.
- 최종 merge와 push는 human gate 뒤에 둔다.

### 2.2 최종 판정

```text
Phase 1 System Design: REVISE
Phase 2 Macro Blocking: REVISE
Phase 3 Micro Blocking: 진행 금지
```

현재 설계에는 잘못된 `worker_done`을 현재 작업 완료로 오인하거나, 정상 구현 diff를
scope violation으로 판정하거나, 사용자의 `reject` 선택을 `DONE`으로 처리할 수 있는
blocking defect가 있다.

---

## 3. Blocking Findings

### PLAN-001 — P0 — Active dispatch와 무관한 lifecycle message를 수락할 수 있음

#### 근거

- `phase2-macro-blocking.md:419-428`
- `wait_for_completion()`은 첫 번째 `worker_done`, `escalation`, `decision_gate`를 반환한다.
- message의 `taskId`와 `dispatchId`가 현재 step과 일치하는지 검증하지 않는다.
- `timeout_alive`가 반환되어도 coordinator는 이를 별도 실패 또는 재대기 신호로 처리하지 않는다.

#### 영향

- 이전 step이나 다른 run의 `worker_done`을 현재 step 완료로 오인할 수 있다.
- worker가 실제로 끝나지 않았는데 stale artifact를 읽고 다음 상태로 진행할 수 있다.
- orchestration task/dispatch provenance를 상태 전이의 근거로 사용할 수 없다.

#### Required Change

1. `wait_for_completion()`에 현재 `task_id`와 `dispatch_id`를 모두 전달한다.
2. lifecycle message의 payload에서 `taskId`, `dispatchId`를 추출한다.
3. 두 값이 현재 active dispatch와 정확히 일치하는 message만 완료 신호로 인정한다.
4. 불일치 message는 현재 step 완료로 처리하지 않는다.
5. `dispatch-show --task <task_id>` 결과와 message provenance를 대조한다.
6. `timeout_alive`는 성공이 아니며 다음 중 하나로 처리한다.
   - 전체 timeout 내에서 rolling wait를 계속한다.
   - 전체 timeout을 초과하면 `FAILED(reason="step_timeout")`로 전이한다.
7. worker의 `ask`로 생성된 `decision_gate`는 자동 추측으로 답하지 않는다.
   설정과 승인된 계약으로 답할 수 없는 질문이면 `USER_DECISION_REQUIRED`로 전이한다.

#### Acceptance Criteria

- 다른 `taskId` 또는 `dispatchId`의 `worker_done`은 현재 step을 완료시키지 않는다.
- `timeout_alive`만으로 `artifact_ok`가 생성되지 않는다.
- active dispatch가 확인되지 않으면 상태는 `FAILED` 또는 `USER_DECISION_REQUIRED`다.

---

### PLAN-002 — P0 — Git baseline, scope guard, review snapshot 모델이 충돌함

#### 근거

- `phase2-macro-blocking.md:62-107`
- `phase2-macro-blocking.md:456-488`
- `phase1-system-design.md:325-353`
- `phase1-system-design.md:431`

`B-01`은 `git init`을 수행하지만 초기 commit을 만들지 않는다. 반면 `B-07`은
`git diff HEAD`와 `git rev-parse HEAD`가 정상 동작한다고 가정한다.

또한 implementation diff가 worktree에 남아 있는 상태에서 `CODE_REVIEW`의 허용 경로를
`.orca-loop/**`로 제한한다. 이 경우 Claude가 source를 전혀 수정하지 않아도 기존의 정상
implementation diff가 scope violation으로 탐지된다.

`reviewed_commit`은 `HEAD`만 나타내지만 implementation은 commit하지 않으므로 Claude와
Codex가 실제로 동일한 uncommitted 결과물을 검토했는지 증명하지 못한다.

#### 영향

- unborn `HEAD`에서 guard가 실패한다.
- 정상적인 implementation 이후 `CODE_REVIEW`가 항상 실패할 수 있다.
- 두 reviewer 사이에서 source가 바뀌어도 동일한 `reviewed_commit`으로 승인될 수 있다.
- 기존 사용자 변경과 loop가 만든 변경을 구분하지 못한다.

#### Required Change

1. preflight에서 유효한 `base_head`가 존재하는지 확인한다.
2. target worktree가 clean하지 않으면 자동 실행하지 않는다.
   기존 변경을 보존해야 하는 경우 사용자가 별도 clean child worktree를 선택하도록 요청한다.
3. 각 step 시작 시 `before_snapshot_digest`를 기록한다.
4. 각 step 종료 시 `after_snapshot_digest`와 delta를 계산한다.
5. read-only step에서는 `before_snapshot_digest == after_snapshot_digest`여야 한다.
   단, coordinator가 작성한 runtime artifact는 별도 artifact directory에서 관리한다.
6. implementation step에서는 승인된 `affected_files` 안의 delta만 허용한다.
7. `reviewed_commit`을 다음 값을 포함하는 `reviewed_snapshot_digest`로 교체한다.
   - `base_head`
   - tracked diff
   - staged diff
   - untracked file path와 content digest
8. Claude review, 자동 테스트, Codex cross review가 동일한 snapshot digest를 사용하도록 한다.
9. `affected_files`는 repository-relative normalized path로 검증한다.
   absolute path, `..`, empty path, 단순 문자열 prefix 우회는 거부한다.

#### Acceptance Criteria

- initial commit이 없는 repository는 worker 생성 전에 `BLOCKED` 처리된다.
- 구현 이후 read-only reviewer가 source를 수정하지 않으면 guard가 통과한다.
- reviewer 사이에 source가 바뀌면 기존 승인은 stale로 무효화된다.

---

### PLAN-003 — P0 — Human gate의 `reject`와 `revise`가 `DONE`이 될 수 있음

#### 근거

- `phase2-macro-blocking.md:312-315`
- `phase2-macro-blocking.md:580-587`

gate options는 `merge`, `reject`, `revise`지만 상태 전이는
`HUMAN_GATE + resolved -> DONE` 하나만 정의되어 있다.

#### 영향

- 사용자가 `reject`를 선택해도 성공 exit code가 반환될 수 있다.
- 사용자가 수정을 요구해도 loop가 종료될 수 있다.
- human decision의 의미가 상태에 보존되지 않는다.

#### Required Change

```text
HUMAN_GATE + merge
    -> READY_FOR_MERGE

HUMAN_GATE + reject
    -> REJECTED

HUMAN_GATE + revise
    -> FIX 또는 영향을 받는 이전 설계 phase
```

- `READY_FOR_MERGE`는 merge가 실행됐다는 뜻이 아니다.
- coordinator는 `git commit`, `merge`, `push`를 실행하지 않는다.
- `revise`에는 사용자 finding과 affected scope를 기록한다.
- 사용자 결정이 architecture, DB, public API를 변경하면 해당 설계 phase로 돌아가 재승인한다.

#### Acceptance Criteria

- `reject`가 `DONE`이나 exit code `0`으로 변환되지 않는다.
- `revise`가 지정된 수정 상태로 돌아간다.
- 최종 report에 사용자가 선택한 resolution이 기록된다.

---

### PLAN-004 — P1 — 역할별 권한이 선언만 되어 있고 실행 시 강제되지 않음

#### 근거

- `phase2-macro-blocking.md:377-399`

worker는 단순히 `claude` 또는 `codex`로 실행된다. planner와 reviewer를 읽기 전용이라고
표시했지만 실제 CLI sandbox, permission mode, allowed tool 범위가 지정되지 않았다.

#### 영향

- read-only reviewer가 source를 수정하거나 삭제할 수 있다.
- post-diff guard는 이미 실행된 DB 변경, network call, `git clean`, `git reset`을 복구하지 못한다.
- 권한 표가 실질적인 안전 통제가 아니라 prompt-level 요청에 머문다.

#### Required Change

1. 역할별 검증된 launch profile을 정의한다.
2. planner와 reviewer는 application source write를 실행 전에 차단한다.
3. read-only worker의 structured result는 Orca message payload로 coordinator에 전달한다.
4. coordinator만 runtime artifact를 기록한다.
5. implementer도 destructive command, merge, push, DB mutation을 자동 실행할 수 없게 한다.
6. 실제 Claude/Codex CLI version에서 profile이 적용됐는지 E2E로 검증한다.

#### Acceptance Criteria

- reviewer가 source write를 시도하면 파일 변경 전에 거부된다.
- reviewer는 검토 JSON을 coordinator에 전달할 수 있다.
- destructive command가 실행된 뒤 guard가 발견하는 방식에 의존하지 않는다.

---

### PLAN-005 — P1 — `TEST_GATE=SKIPPED`가 성공 경로로 취급됨

#### 근거

- `phase2-macro-blocking.md:308-314`
- `phase2-macro-blocking.md:515-521`

현재 전이에서는 `PASS`와 `SKIPPED`가 모두 `CROSS_CONFIRM`으로 진행된다.

#### 영향

- 테스트를 실행하지 않은 결과가 합의와 human merge 대상으로 전달된다.
- `SKIPPED`와 `PASS`의 의미가 사실상 동일해진다.
- 자동 테스트를 포함한 사용자 요구를 충족하지 못한다.

#### Required Change

```text
TEST_GATE + PASS
    -> CONSENSUS_PREPARE

TEST_GATE + FAIL
    -> FIX

TEST_GATE + SKIPPED
    -> USER_DECISION_REQUIRED(status=NOT_RUN)
```

- test command를 계획 artifact의 승인된 test contract로부터 얻는다.
- test command가 없으면 자동 승인하지 않는다.
- 사용자가 명시적으로 test 미실행을 수용해도 final report에는 `NOT_RUN`을 유지한다.
- `Ready for human merge` 자동 판정에는 full test `PASS`가 필요하다.

#### Acceptance Criteria

- `SKIPPED`는 자동 성공 경로에 포함되지 않는다.
- 실행하지 않은 테스트를 `PASS`로 표시하지 않는다.
- test failure는 영향 범위가 포함된 `FIX`로 돌아간다.

---

### PLAN-006 — P1 — Artifact freshness와 convergence 검사가 coordinator에 연결되지 않음

#### 근거

- `phase2-macro-blocking.md:169-223`
- `phase2-macro-blocking.md:478-495`
- `phase2-macro-blocking.md:541-567`

`B-03`은 review JSON을 검증하지만 `plan.md`와 `implementation-summary.md`의 freshness와
구조를 검증하지 않는다. coordinator는 `new_ids=[]`로 convergence guard를 호출한 뒤
finding ID를 저장하므로 반복 finding 검사가 실제로 수행되지 않는다.

#### 영향

- 이전 run의 stale artifact가 현재 결과로 사용될 수 있다.
- planner나 implementer가 산출물을 작성하지 않아도 다음 단계로 진행할 수 있다.
- 동일한 미해결 finding이 반복돼도 탐지되지 않는다.

#### Required Change

모든 artifact에 다음 식별자를 포함한다.

```json
{
  "schema_version": 1,
  "run_id": "20260731-142530",
  "task_id": "task_current",
  "dispatch_id": "dispatch_current",
  "source_version": 3,
  "snapshot_digest": "sha256:current_snapshot"
}
```

추가 규칙:

1. dispatch 전 기존 output artifact를 현재 run에서 사용할 수 없도록 분리한다.
2. `plan.md`는 plan version, request digest, affected files, tests를 검증한다.
3. implementation step은 non-empty approved delta와 summary를 모두 요구한다.
4. verdict를 먼저 parse한다.
5. parse한 `new_finding_ids`를 convergence 검사에 전달한다.
6. 검사가 통과한 뒤에만 `seen_finding_ids`를 갱신한다.

#### Acceptance Criteria

- stale `run_id`, `dispatch_id`, snapshot digest를 가진 artifact가 거부된다.
- required artifact가 없으면 `artifact_ok`가 생성되지 않는다.
- 동일 unresolved finding이 반복되면 합의 protocol에 따라 유지되며 조용히 성공하지 않는다.

---

### PLAN-007 — P1 — `--resume`이 crash-safe하지 않고 concurrent run을 차단하지 않음

#### 근거

- `phase2-macro-blocking.md:500-587`
- `phase2-macro-blocking.md:642-655`

현재 resume은 `state.json`을 읽는 것만 정의한다. 실제 Orca task/dispatch 상태와 artifact
상태를 reconciliation하는 규칙이 없다. 고정 artifact 경로를 사용하면서 같은 worktree의
두 coordinator 실행도 차단하지 않는다.

#### Required Change

1. state를 다음 durable step으로 세분화한다.

```text
STEP_PENDING
STEP_DISPATCHED
WORKER_DONE_RECEIVED
ARTIFACT_VERIFIED
TRANSITION_COMMITTED
```

2. `state.json`은 temporary file 작성 후 atomic replace로 갱신한다.
3. target worktree별 run lock을 사용한다.
4. resume 시 `task-list`, `dispatch-show`, terminal list, artifact digest를 대조한다.
5. stale terminal handle은 worktree와 terminal identity로 재해석한다.
6. 이미 완료된 dispatch를 다시 실행하지 않는다.
7. reconciliation이 모호하면 자동 추측하지 않고 `USER_DECISION_REQUIRED`로 전이한다.

#### Acceptance Criteria

- 각 crash boundary에서 resume 테스트가 존재한다.
- 동일 worktree에 두 coordinator가 동시에 artifact를 쓰지 못한다.
- resume이 중복 source modification이나 중복 dispatch를 발생시키지 않는다.

---

### PROCESS-001 — P1 — Phase approval 기록과 현재 `AGENTS.md`가 일치하지 않음

#### 근거

- `phase1-system-design.md:566-571`
- `phase1-system-design.md:176-178`
- `phase2-macro-blocking.md:10-15`

Phase 1은 `Waiting for Explicit User Approval`이지만 Phase 2는 승인된 System Design을
baseline으로 선언한다. Phase 1은 `claude-mhj_...` prefix 면제를 기록했지만 현재 제공된
`AGENTS.md`의 mandatory prefix는 `codex-mhj_YY_MM_DD_<sequence>_`이다.

#### Required Change

- 실제 explicit approval 기록을 문서 상태에 반영한다.
- approval 기록이 없으면 Phase 1 revision과 재승인부터 수행한다.
- filename prefix 면제의 대상, 범위, 승인자를 다시 확인한다.
- 확정되지 않은 사항이 있으므로 `Open Questions: 없음`을 제거한다.

---

### PROCESS-002 — P2 — `B-09` 필수 필드 누락

`B-09`에는 Macro Block 필수 필드인 `Dependencies`가 없다.

#### Required Change

```text
Dependencies: B-08
```

---

## 4. Revised Consensus Protocol

### 4.1 핵심 원칙

합의 loop는 “제한 횟수에 도달하면 승인”하는 장치가 아니다. 횟수 제한은 무한 실행을
막고 사용자에게 결정을 돌려주기 위한 escalation threshold다.

```text
합의 성공
    = unresolved blocking finding 0건
    + Claude와 Codex가 동일 snapshot을 승인
    + required tests PASS
    + scope guard PASS

5개 유효 round 소진
    = 자동 승인 금지
    + USER_DECISION_REQUIRED
    + status BLOCKED
    + 사용자 판단 문서 생성
```

### 4.2 Round limit

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `plan_consensus_round_limit` | 5 | 계획 finding에 대한 유효 합의 round 상한 |
| `code_consensus_round_limit` | 5 | 구현 finding에 대한 유효 합의 round 상한 |
| `operational_retry_limit` | 1 | malformed JSON 같은 전달 실패 재요청 |

operational retry는 합의 round로 계산하지 않는다. 다음 조건을 모두 충족해야 유효 round로
계산한다.

1. Claude와 Codex가 같은 plan version 또는 snapshot digest를 검토했다.
2. 두 결과 artifact가 schema validation을 통과했다.
3. 두 결과의 active `taskId`와 `dispatchId`가 확인됐다.
4. round 도중 source 또는 plan이 변경되지 않았다.

### 4.3 Finding lifecycle

각 finding은 stable ID와 다음 상태 중 하나를 가진다.

| 상태 | 의미 | 다음 round 포함 |
|---|---|---|
| `OPEN` | 양측이 아직 합의하지 못함 | 포함 |
| `CHANGE_REQUIRED` | 필요한 수정에는 합의했지만 수정·검증이 남음 | 포함 |
| `VERIFY_REQUIRED` | 수정 완료 후 증거 확인이 남음 | 포함 |
| `RESOLVED` | 양측이 동일 snapshot과 증거로 합의 | 제외 |
| `USER_DECISION_REQUIRED` | 5 round 후에도 합의 실패 | 자동 round 중단 |

`RESOLVED` finding은 immutable consensus ledger에 기록하고 이후 prompt 본문에서 제외한다.

새로운 증거로 resolved finding을 다시 열어야 할 경우 기존 finding을 조용히 수정하지 않는다.
다음 형식의 새 finding을 생성한다.

```json
{
  "id": "CODE-004-R1",
  "reopens": "CODE-004",
  "reason": "A regression test invalidated the prior resolution.",
  "evidence": ["tests/test_auth.py::test_reset_after_success"]
}
```

### 4.4 다음 round 범위 축소

다음 round의 context에는 아래 정보만 포함한다.

1. `OPEN`, `CHANGE_REQUIRED`, `VERIFY_REQUIRED` finding
2. 해당 finding이 직접 참조하는 acceptance criteria
3. 해당 finding의 `affected_files`
4. 해당 finding의 `depends_on`으로 연결된 finding
5. 관련 targeted test 결과
6. 직전 round에서 양측이 달리 판단한 핵심 문장
7. 현재 plan version 또는 snapshot digest

다음 내용은 반복 전달하지 않는다.

- 이미 `RESOLVED`인 finding의 전체 토론
- 변경되지 않은 전체 plan
- 전체 repository diff
- 이전 round의 자유 형식 대화 전문
- 현재 finding과 관련 없는 non-blocking suggestion

관련 범위는 문자열 유사도가 아니라 다음 명시적 관계로 계산한다.

```text
finding.depends_on
finding.affected_files
finding.acceptance_criteria_ids
finding.test_ids
```

dependency closure는 직접 관계부터 계산한다. 추가 범위가 필요하면 reviewer가 새 evidence와
함께 관계를 선언해야 한다.

### 4.5 Consensus round flow

```text
CONSENSUS_PREPARE
    ↓
현재 unresolved finding set과 scope manifest 생성
    ↓
CLAUDE_CONSENSUS_REVIEW
    ↓
CODEX_CONSENSUS_REVIEW
    ↓
CONSENSUS_EVALUATE
    ├─ 모든 blocking finding RESOLVED
    │      └─ full TEST_GATE
    │             ├─ PASS → HUMAN_GATE
    │             └─ FAIL → 관련 finding만 OPEN으로 생성 → FIX
    │
    ├─ 수정 필요 + round < 5
    │      └─ FIX → targeted tests → 다음 round
    │
    ├─ 의견 불일치 + round < 5
    │      └─ unresolved scope만 다음 round
    │
    └─ unresolved 존재 + round == 5
           └─ USER_DECISION_REQUIRED
```

Codex가 source를 수정한 경우 기존 Claude/Codex 승인은 모두 snapshot stale이 된다.
다만 다음 round에는 수정으로 영향을 받은 unresolved finding과 dependency closure만 전달한다.

### 4.6 Per-finding consensus contract

```json
{
  "schema_version": 1,
  "run_id": "20260731-142530",
  "round": 3,
  "snapshot_digest": "sha256:current_snapshot",
  "findings": [
    {
      "id": "CODE-004",
      "status": "OPEN",
      "affected_files": [
        "src/auth/service.py"
      ],
      "acceptance_criteria_ids": [
        "AC-AUTH-03"
      ],
      "test_ids": [
        "T-AUTH-07"
      ],
      "depends_on": [],
      "claude_position": {
        "verdict": "CHANGE_REQUIRED",
        "reason": "Successful authentication does not reset the failure counter."
      },
      "codex_position": {
        "verdict": "AGREE",
        "reason": "The missing reset is reproduced by T-AUTH-07."
      },
      "proposed_resolution": "Reset the counter in the successful authentication transaction and rerun T-AUTH-07."
    }
  ]
}
```

finding이 `RESOLVED`가 되려면 양측이 같은 resolution과 같은 snapshot에 동의하고 required
evidence가 존재해야 한다. 한쪽의 `APPROVE`만으로 resolved 처리하지 않는다.

---

## 5. User Escalation after Five Rounds

### 5.1 전이 규칙

5번째 유효 round가 끝났는데 unresolved finding이 존재하면:

```text
state = USER_DECISION_REQUIRED
status = BLOCKED
automatic_approval = false
source_modification = prohibited
next_owner = USER
```

coordinator는 추가 수정, 임의 선택, majority vote, fallback approval을 수행하지 않는다.

### 5.2 사용자 판단 문서

다음 위치에 사용자 판단 문서를 생성한다.

```text
.orca-loop/runs/<run_id>/user-decision.md
```

실제 filename은 대상 repository의 `AGENTS.md` naming rule을 따라야 한다.

문서는 다음 내용을 포함한다.

1. 원래 요구사항과 현재 plan version 또는 snapshot digest
2. 총 consensus round 수
3. 합의 완료 항목의 ID와 resolution 요약
4. 미합의 finding만 포함한 상세 표
5. 각 finding에 대한 Claude 입장
6. 각 finding에 대한 Codex 입장
7. 양측의 공통 합의점
8. 정확한 불일치 지점
9. 관련 source, test, evidence
10. 사용자가 선택할 수 있는 구체적인 options
11. option별 장점, 위험, 영향 범위, 추가 작업
12. 결정을 내리지 않을 경우의 상태

### 5.3 사용자 판단 표 형식

| Finding | 공통 합의 | Claude 입장 | Codex 입장 | Option | 영향 | 필요한 사용자 결정 |
|---|---|---|---|---|---|---|
| `CODE-004` | 현재 동작이 acceptance criterion을 충족하지 않음 | transaction 안에서 reset | 별도 recovery step에서 reset | `A` 또는 `B` | transaction consistency와 retry behavior | reset 위치 선택 |

각 option은 다음 형식을 사용한다.

```text
Option A
- Behavior:
- Advantages:
- Risks:
- Affected files:
- Required tests:

Option B
- Behavior:
- Advantages:
- Risks:
- Affected files:
- Required tests:
```

선택지를 만들 근거가 부족하면 가짜 option을 만들지 않고 필요한 추가 정보와 확인 방법을
명시한다.

### 5.4 Orca decision gate

decision gate에는 모든 논의를 다시 넣지 않는다. 사용자 판단 문서 경로, unresolved finding
ID, 선택지만 전달한다.

```text
Question:
Consensus was not reached after 5 valid rounds.
Review <user-decision-report-path> and choose a resolution for CODE-004.

Options:
- OPTION_A
- OPTION_B
- STOP
```

사용자 결정은 `state.json`과 consensus ledger에 기록한다. 결정 이후에는 선택된 finding과
dependency closure만 다시 `FIX` 또는 해당 설계 phase로 전달한다.

---

## 6. Required Phase 1 Revisions

Phase 1 System Design에는 최소한 다음 변경이 필요하다.

1. 기존 `plan_revisions=2`, `code_revisions=3` 합의 제한을 기본 5회로 변경한다.
2. 반복 한도 도달 시 `FAILED` 또는 자동 승인이 아니라 `USER_DECISION_REQUIRED`로 전이한다.
3. `CONSENSUS_PREPARE`, `CLAUDE_CONSENSUS_REVIEW`,
   `CODEX_CONSENSUS_REVIEW`, `CONSENSUS_EVALUATE` 상태를 추가한다.
4. `RESOLVED` finding 제외 규칙과 reopen 규칙을 정의한다.
5. 동일 snapshot에 대한 dual approval을 합의 조건으로 정의한다.
6. `SKIPPED` test의 자동 성공 전이를 제거한다.
7. human gate의 `merge`, `reject`, `revise` 전이를 분리한다.
8. user decision report contract를 Input & Output Contracts에 추가한다.
9. per-step snapshot과 message provenance를 Security Considerations에 추가한다.
10. concurrent run lock과 crash reconciliation을 State & Side Effects에 추가한다.

---

## 7. Required Phase 2 Revisions

### B-03 — Artifact Contracts & Schemas

다음 contract를 추가한다.

- `ConsensusFinding`
- `ConsensusRound`
- `ConsensusLedger`
- `ScopeManifest`
- `UserDecisionReport`
- `SnapshotIdentity`

### B-04 — Role Contract Rendering

- 전체 과거 대화 대신 unresolved scope package를 주입한다.
- resolved finding을 prompt에서 제외한다.
- reviewer가 related scope를 확장하려면 evidence와 relation을 작성하게 한다.

### B-05 — Pure State Machine

- plan/code consensus limit 기본값을 각각 5로 변경한다.
- limit 도달 시 `USER_DECISION_REQUIRED`로 전이한다.
- `merge`, `reject`, `revise`를 서로 다른 signal로 처리한다.
- `timeout_alive`를 성공 signal 목록에서 제외한다.

### B-06 — Worker Provisioning & Dispatch

- lifecycle payload의 `taskId`, `dispatchId`를 검증한다.
- stale handle과 stale dispatch를 구분한다.
- decision gate를 자동 추측으로 처리하지 않는다.

### B-07 — Scope & Safety Guards

- cumulative `HEAD` diff가 아니라 step delta를 검사한다.
- `affected_files` path를 정규화한다.
- unresolved finding의 dependency closure를 계산한다.
- resolved finding이 다음 scope package에 포함되지 않는지 검증한다.

### B-08 — Coordinator Loop & State Persistence

- consensus ledger를 갱신한다.
- round 유효성 조건을 검사한다.
- 5회 후 user decision report와 gate를 생성한다.
- state와 artifact를 atomic하게 기록한다.
- 사용자 결정이 내려질 때까지 `BLOCKED`를 유지한다.

### B-10 — Test Suite & E2E Validation

최소 다음 validation을 추가한다.

| ID | 검증 |
|---|---|
| `V-CONS-01` | 5회 불일치 후 자동 승인되지 않음 |
| `V-CONS-02` | 5회 불일치 후 `USER_DECISION_REQUIRED` 도달 |
| `V-CONS-03` | resolved finding이 다음 round prompt에서 제외됨 |
| `V-CONS-04` | unresolved finding의 dependency closure만 포함됨 |
| `V-CONS-05` | source 변경 시 이전 dual approval이 stale 처리됨 |
| `V-CONS-06` | user decision report가 모든 unresolved finding과 option을 포함함 |
| `V-CONS-07` | `reject`가 `DONE`으로 전이되지 않음 |
| `V-CONS-08` | `SKIPPED` test가 자동 success로 전이되지 않음 |
| `V-CONS-09` | 다른 dispatch의 `worker_done`이 현재 step을 완료시키지 않음 |
| `V-CONS-10` | operational retry가 consensus round를 소비하지 않음 |

---

## 8. Validation and Risks

### 8.1 Validation Performed

| 항목 | 상태 | 결과 |
|---|---|---|
| Phase 1 문서 검토 | `PASS` | 구조는 적절하지만 blocking revision 필요 |
| Phase 2 문서 검토 | `PASS` | 10개 block 확인, `B-09.Dependencies` 누락 |
| 현재 Orca CLI/skill 계약 확인 | `PASS` | Orca ADE `1.4.159`과 주요 command 일치 |
| Source implementation | `NOT RUN` | 문서 검토 범위 |
| Unit/E2E tests | `NOT RUN` | 구현 전 단계 |

### 8.2 Remaining Risks

- Claude와 Codex CLI의 실제 permission profile 조합은 E2E 검증 전까지 `NOT RUN`이다.
- untracked file을 포함한 snapshot digest 방식은 Phase 3에서 정확한 canonicalization이 필요하다.
- user decision이 architecture나 persistent data 계약을 변경하면 Phase 1 재승인이 필요하다.
- token 절감은 prompt scope 축소로 달성하되 correctness evidence를 생략해서는 안 된다.

---

## 9. Approval Status

- [ ] Phase 1 System Design approved
- [x] Phase 1 revision requested
- [ ] Phase 2 Macro Blocking approved
- [x] Phase 2 revision requested
- [ ] Permission granted to proceed to Phase 3

**Required next action:** Claude가 이 문서의 `PLAN-001`~`PLAN-007`,
`PROCESS-001`~`PROCESS-002`, Revised Consensus Protocol을 반영해 Phase 1을 수정한다.
수정된 Phase 1은 사용자 explicit approval을 받은 뒤 Phase 2에 다시 반영해야 한다.
