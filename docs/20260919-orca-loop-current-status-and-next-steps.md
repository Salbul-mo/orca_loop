# Orca Loop 현재 상태 및 남은 작업 정리

작성일: 2026-09-19

## 1. 문서 목적

이 문서는 현재 `orca_loop` 리팩터링의 실제 구현 상태를 한 번에 파악하고, 다음 작업을 새 세션에서도 바로 이어갈 수 있도록 정리한 상태 문서다.

핵심 목표는 다음과 같다.

- Orca loop의 불필요한 고정 state-machine 절차를 줄인다.
- 반복적인 Permission Feasibility Spike를 제거한다.
- 권한은 정적 `PermissionProfile` 정책으로 결정적으로 검증한다.
- Master Agent를 일반 worker와 분리된 control/routing 계층으로 도입한다.
- Master가 검증된 조건에서만 중간 단계를 생략하거나 접을 수 있게 한다.
- 기존 consensus, evidence, test, destructive approval, escalation 의미는 유지한다.
- 최종 merge/reject/revise 권한은 항상 사람에게 남긴다.

현재 큰 흐름은 **Permission Spike 제거 + 정적 permission policy + Master routing + 단계적 state-machine 완화**까지 진행된 상태다.

---

## 2. 현재 저장소 상태

작업 대상 저장소:

```text
Windows:
C:\Users\mhj21\Desktop\workspace\orca_loop

Relay cwd:
orca_loop
```

현재 worktree는 의도적으로 dirty 상태다.

다음 명령은 사용하지 않는다.

```text
git reset
git restore
git clean
git stash
git commit
git push
```

또한:

- dependency 설치 금지
- unrelated user change 덮어쓰기 금지
- broad refactor 금지
- phase-by-phase로 진행
- 안전 조건이 만족되지 않으면 기존 fixed path를 fallback으로 유지

현재 주요 수정 파일은 최소 다음과 같다.

```text
run_loop.py
tests/test_cli.py
docs/orca-loop-master-permission-refactor-plan.md
```

이번 상태 문서:

```text
docs/20260919-orca-loop-current-status-and-next-steps.md
```

---

## 3. 최종 Permission Profile 정책

worker permission profile은 두 개만 유지한다.

```text
READ_ONLY
WORKSPACE_WRITE
```

Role → PermissionProfile 정책:

```text
planner         -> READ_ONLY
plan_reviewer   -> READ_ONLY
implementer     -> WORKSPACE_WRITE
code_reviewer   -> READ_ONLY
cross_confirmer -> READ_ONLY
```

핵심 규칙:

- Coordinator가 role/profile compatibility를 검증한다.
- `build_launch_profile()`은 검증된 permission profile을 입력으로 사용한다.
- `build_launch_profile()`이 Role만 보고 write scope를 추론하지 않는다.
- `profiles.py`는 authorization source가 아니다.
- permission policy digest를 저장한다.
- resume 시 permission-policy drift를 거부한다.

---

## 4. 제거 완료된 Permission Feasibility 개념

다음 개념은 제거된 상태이며 다시 도입하지 않는다.

```text
PermissionFeasibilityReport
PermissionCheck
PermissionStrategy
AgentAccessMode
ProviderCapability
permission_report_digest
--permission-report
parse_permission_report
V-PERM
recurring Permission Feasibility Spike runtime behavior
```

Permission Spike를 위한 AI 호출이나 worker session을 다시 추가하지 않는다.

---

## 5. Master Agent 현재 구조

Master는 일반 worker가 아니다.

### 5.1 MasterAction

```text
dispatch
test
finish
escalate
abort
```

### 5.2 MasterDecision

필드:

```text
action
role
permission_profile
reason
```

wire alias:

```text
permissionProfile
```

검증:

- `dispatch`는 role/profile이 모두 필요하다.
- non-dispatch action은 role/profile이 모두 null이어야 한다.
- reason은 빈 문자열이면 안 된다.

### 5.3 Master runtime

Master는 normal worker pool과 독립이다.

- Master WorkerKey 없음
- normal runtime agent entry 없음
- 별도 runtime/config/snapshot schema v1
- runtime path:

```text
control/master-runtime.json
```

CLI:

```text
--master-config
```

provider/model default를 임의로 만들지 않는다.

### 5.4 Master 권한

Master는 control-only다.

- `CONTROL_ONLY` worker permission profile을 만들지 않는다.
- Master는 repository에 쓰지 않는다.
- Master에게 workspace-write 또는 dangerous sandbox를 주지 않는다.

Claude Master invocation은 제한 모드만 사용한다.

개념적 형태:

```text
claude -p ... --restricted --tools "" --permission-mode plan --output-format text
```

Codex Master도 read-only다.

개념적 형태:

```text
codex --ask-for-approval never exec ... --sandbox read-only --skip-git-repo-check --ephemeral -
```

---

## 6. 사람의 최종 권한

이 규칙은 현재 설계의 최상위 안전 경계다.

```text
HUMAN_GATE owns merge / reject / revise
```

즉:

- Master `finish`는 `READY_FOR_MERGE`를 의미하지 않는다.
- Master가 직접 merge-ready 상태로 이동하지 않는다.
- 모든 shortcut은 최종적으로 사람의 판단 경계를 유지해야 한다.
- 과거 문서의 `READY`, `READY_FOR_MERGE` target sketch는 현재 규칙보다 우선하지 않는다.

---

## 7. 완료된 주요 단계

## 7.1 Permission Spike 제거

완료.

기존 반복 permission feasibility workflow를 제거하고 deterministic static permission policy로 대체했다.

## 7.2 PermissionProfile 정책

완료.

- worker profile 두 개로 축소
- Coordinator boundary validation
- launch profile integration
- policy digest persist
- resume drift detection

## 7.3 Master runtime / contract

완료.

- MasterDecision 계약
- Master runtime/config snapshot
- provider invocation restriction
- Master routing foundation
- human final authority 유지

---

## 8. 단계적 state-machine 완화 진행 상황

전체 item 19는 아직 진행 중이다.

현재까지 Phase 10~15가 구현됐다.

---

## 8.1 Phase 10 — final Master routing

완료.

`CONSENSUS_EVALUATE + UNRESOLVED_ZERO` 이후 Master가:

```text
finish
escalate
```

를 선택할 수 있다.

`finish`는 기존 final transition을 유지하며:

```text
HUMAN_GATE
```

로 이동한다.

직접 `READY_FOR_MERGE`로 이동하지 않는다.

---

## 8.2 Phase 11 — safe plan이 plan review를 생략 가능

완료.

`PLAN` 또는 `PLAN_REVISE` 직후 low-risk 조건이면 Master가 implementer를 직접 dispatch할 수 있다.

대표 조건:

```text
unresolved count = 0
data/API/schema change 없음
delete/rename 없음
```

조건 미충족 시 기존 plan review 경로를 유지한다.

---

## 8.3 Phase 12 — safe passing test가 human gate로 바로 finish 가능

완료.

조건:

- 실제 test PASS
- test gate PASS
- low-risk plan

Master가 `finish`를 선택할 수 있고 결과는:

```text
HUMAN_GATE
```

이다.

`NOT_RUN` 등은 finish shortcut 대상이 아니다.

---

## 8.4 Phase 13 — clean code review가 cross-confirm을 생략 가능

완료.

clean low-risk `CODE_REVIEW`에서 다음 조건이 모두 만족되면 Master가 `finish`를 선택할 수 있다.

대표 조건:

```text
ARTIFACT_OK
test gate PASS
artifact kind = CODE_REVIEW
role = code_reviewer
verdict = APPROVE
reviewed finding 없음
finding decision 없음
new finding 없음
non-blocking suggestion 없음
escalation 없음
current plan 존재
unresolved count = 0
data/API/schema change 없음
delete/rename 없음
```

성공 시:

```text
CODE_REVIEW
→ Master finish
→ HUMAN_GATE
```

human final boundary는 유지된다.

---

## 8.5 Phase 14 — clean cross-confirm에서 CONSENSUS_EVALUATE durable state 접기 [REVERTED]

2026-09-20 과설계 검토 후 철회했다. 실제 worker/provider 호출은 줄지 않고 deterministic state만 접으면서 preview/fallback 복잡도만 증가했기 때문이다.

기존 경로:

```text
CROSS_CONFIRM
→ CONSENSUS_EVALUATE
→ HUMAN_GATE
```

Phase 14 shortcut:

```text
CROSS_CONFIRM(clean agreement)
→ existing execute_evaluate(CODE) preview
→ Master finish
→ HUMAN_GATE
```

중요한 점은 consensus evaluation을 제거하지 않았다는 것이다.

preview에서 그대로 유지되는 의미:

```text
code round evidence validation
both artifacts valid
snapshot digest validation
code consensus round limit
code_round increment
unresolved/escalation calculation
```

preview가 실제 `UNRESOLVED_ZERO`이고 escalation이 없을 때만 shortcut을 연다.

그 외에는 Master를 호출하지 않고 기존:

```text
CROSS_CONFIRM -> CONSENSUS_EVALUATE
```

를 유지한다.

---

## 8.6 Phase 15 — clean plan review에서 PLAN_CONSENSUS_EVALUATE durable state 접기 [REVERTED]

2026-09-20 과설계 검토 후 철회했다. plan consensus evaluate는 로컬 deterministic 단계라 별도 Master preview로 접는 이득보다 코드·테스트 복잡도가 더 컸다.

기존 경로:

```text
PLAN_REVIEW
→ PLAN_CONSENSUS_EVALUATE
→ IMPLEMENT
```

Phase 15 shortcut:

```text
PLAN_REVIEW(clean APPROVE)
→ existing execute_evaluate(PLAN) preview
→ Master dispatch implementer
→ IMPLEMENT
```

### 8.6.1 shortcut eligibility

review artifact는 다음을 만족해야 한다.

```text
worker signal = ARTIFACT_OK
artifact kind = PLAN_REVIEW
role = plan_reviewer
verdict = APPROVE
reviewed_finding_ids = empty
finding_decisions = empty
findings = empty
non_blocking_suggestions = empty
artifact escalation_signals = empty
result escalations = empty
current plan exists
```

그 다음 기존 `execute_evaluate()`를 `PLAN_CONSENSUS_EVALUATE` 상태로 preview 실행한다.

### 8.6.2 preview에서 그대로 유지하는 기존 의미

```text
plan.json + plan_review.json evidence
both_artifacts_valid
reviewed plan version validation
plan consensus round limit
plan_round increment
unresolved finding calculation
data/API/schema E-03
delete/rename destructive E-03
destructive approval semantics
```

### 8.6.3 Master에게 허용되는 선택

preview가 실제 `UNRESOLVED_ZERO`이고 escalation이 없을 때만:

```text
dispatch implementer / workspace_write
escalate
```

만 허용한다.

허용하지 않는 것:

```text
finish
planner jump
arbitrary role dispatch
```

### 8.6.4 성공 시 durable commit

Master가 implementer dispatch를 선택하면 preview ledger를 사용해:

```text
step_stage = TRANSITION_COMMITTED
state = IMPLEMENT
signal = UNRESOLVED_ZERO
plan_round = previous + 1
```

을 기록한다.

따라서 `PLAN_CONSENSUS_EVALUATE`가 원래 소유하던 plan round commit 의미를 잃지 않는다.

### 8.6.5 fallback 조건

다음 경우 shortcut을 사용하지 않는다.

```text
clean PLAN_REVIEW APPROVE가 아님
suggestion 존재
finding/decision 존재
artifact/result escalation 존재
current plan 없음
plan_review.json evidence 없음
preview != UNRESOLVED_ZERO
API/schema/data 변경 E-03
delete/rename destructive E-03
plan version mismatch
plan round limit 초과
```

fallback:

```text
PLAN_REVIEW
→ PLAN_CONSENSUS_EVALUATE
→ 기존 경로
```

### 8.6.6 InvalidRoundError 처리

Phase 15에서 추가로 보완한 부분이다.

`execute_evaluate()` 내부의 `commit_round()`은 다음 상황에서 `InvalidRoundError`를 발생시킨다.

```text
plan round version mismatch
plan consensus round limit exceeded
```

shortcut preview는 기존 durable evaluate보다 일찍 실행되므로, preview 때문에 기존 경로보다 먼저 runtime failure가 발생해서는 안 된다.

따라서 Phase 15 PLAN preview에서는 **`InvalidRoundError`만 구체적으로 catch**하여 preview unavailable로 처리하고 기존 durable state로 fallback한다.

```text
InvalidRoundError
→ no Master shortcut
→ PLAN_CONSENSUS_EVALUATE
```

broad `Exception` catch는 추가하지 않았다.

---

## 9. 현재 worker-completion Master routing 구조

현재 Master가 worker completion 이후 개입할 수 있는 주요 state:

```text
PLAN
PLAN_REVISE
PLAN_REVIEW
IMPLEMENT
FIX
CODE_REVIEW
CROSS_CONFIRM
```

### PLAN / PLAN_REVISE

기본:

```text
dispatch plan_reviewer
escalate
```

safe plan이면 추가:

```text
dispatch implementer
```

### PLAN_REVIEW

Master 추가 호출 없이 기존 deterministic 경로를 유지한다.

```text
PLAN_REVIEW
→ PLAN_CONSENSUS_EVALUATE
```

### IMPLEMENT / FIX

```text
test
escalate
```

### CODE_REVIEW

기본:

```text
dispatch cross_confirmer
escalate
```

clean low-risk이면 추가:

```text
finish -> HUMAN_GATE
```

### CROSS_CONFIRM

Master 추가 호출 없이 기존 deterministic 경로를 유지한다.

```text
CROSS_CONFIRM
→ CONSENSUS_EVALUATE
→ HUMAN_GATE
```

---

## 10. 현재 검증 결과

### 10.1 과설계 제거 후 focused suite

실행:

```text
python -m pytest tests/test_cli.py -q -k worker_completion_master
```

최종 결과:

```text
13 passed, 29 deselected, 9 subtests passed
EXIT=0
```

Phase 11~13의 실제 worker-call 절감 정책과 test-result routing을 검증한다.

### 10.2 과설계 제거 후 related regression

실행:

```text
python -m pytest \
  tests/test_master_runtime.py \
  tests/test_contracts.py \
  tests/test_coordinator.py \
  tests/test_escalation.py \
  tests/test_orca_client.py \
  tests/test_cli.py \
  -q
```

최종 결과:

```text
94 passed, 4 warnings, 35 subtests passed
EXIT=0
```

4개 warning은 기존 pytest collection warning이다.

```text
TestExecutionPolicy __init__
TestGateStatus __new__
TestContract __init__ (coordinator)
TestContract __init__ (escalation)
```

신규 회귀로 보지 않는다.

---

## 11. 아직 실행하지 않은 검증

다음은 아직 수행하지 않았다.

```text
full repository test suite — PASS: 125 passed, 8 warnings, 35 subtests passed
actual Claude Master subprocess smoke test
actual Codex Master subprocess smoke test
```

전체 repository suite는 PASS다. 실제 Claude/Codex provider subprocess smoke만 아직 PASS라고 주장하지 않는다.

---

## 12. 현재 state-machine 방향

현재 일반화된 흐름은 여전히 다음 구조를 기준으로 한다.

```text
PLAN
→ PLAN_REVIEW
→ PLAN_CONSENSUS_EVALUATE
→ IMPLEMENT
→ TEST
→ CODE_REVIEW
→ CROSS_CONFIRM
→ CONSENSUS_EVALUATE
→ HUMAN_GATE
```

Phase 11~13에서 유지하는 shortcut은 **실제 worker 호출을 줄이는 경우만**이다.

```text
safe PLAN -> IMPLEMENT
safe verified TEST PASS -> HUMAN_GATE
clean CODE_REVIEW -> HUMAN_GATE
```

반면 `PLAN_CONSENSUS_EVALUATE`, `TEST_GATE`, `CONSENSUS_EVALUATE` 같은 deterministic durable state는 유지한다. 이들은 비용이 작고 resume/evidence/debug boundary로 유용하다.

---

## 13. 유지해야 하는 안전 원칙

다음 원칙은 이후 Phase에서도 유지한다.

### 13.1 Additive / conditional relaxation

shortcut은 기존 경로 위에 조건부로 추가한다.

기존 path를 제거하고 shortcut만 남기는 방식은 사용하지 않는다.

### 13.2 Canonical evaluator 재사용

consensus/safety 의미를 새 helper에서 복제하지 않는다.

가능한 경우 기존:

```text
execute_evaluate()
commit_round()
commit_step_transition()
```

등을 재사용한다.

### 13.3 Evidence semantics 유지

state를 접더라도 그 state가 소유하던 의미는 보존한다.

예:

```text
round increment
artifact evidence
snapshot digest
plan version
round limit
unresolved calculation
escalation calculation
```

### 13.4 Destructive boundary 유지

다음은 자동 shortcut으로 우회하지 않는다.

```text
delete
rename
large data change
unsafe external side effect
```

기존 destructive approval / human escalation 의미를 유지한다.

### 13.5 Failed test reroute 임의 추가 금지

실패한 test를 Master가 임의로 planner로 보내는 등의 새 routing은 별도 설계 없이 추가하지 않는다.

### 13.6 Human final authority 유지

어떤 Phase에서도:

```text
READY_FOR_MERGE
merge
reject
revise
```

최종 disposition을 Master가 직접 결정하게 만들지 않는다.

---

## 14. 남은 핵심 작업

전체 implementation item 19:

```text
고정 state machine 단계적 완화
```

는 아직 완료 상태가 아니다.

Phase 번호를 더 늘리는 방식의 state-collapse 작업은 중단한다.

### 14.1 다음 Phase 후보 선정

새 shortcut은 durable state 자체가 아니라 실제 worker/provider 호출을 최소 1회 줄일 수 있을 때만 검토한다.

검토 기준:

```text
실제 worker/provider 호출 감소가 있는가?
외부/고비용 작업 감소가 있는가?
correctness/recovery 개선이 있는가?
기존 evidence/escalation/human boundary를 보존하는가?
추가 분기와 테스트 비용이 절감 효과보다 작은가?
```

### 14.2 가능한 향후 방향

아직 구현되지 않은 방향의 예시는 다음과 같다.

```text
실제 Claude/Codex Master subprocess smoke
Master 호출 자체가 불필요한 경로 식별
실행 한 건당 Master/worker dispatch 수 계측
실제 worker를 하나 이상 줄일 수 있는 경로만 추가 최적화
```

하지만 위 항목은 아직 설계/구현 완료 상태가 아니며 다음 Phase에서 하나씩 결정해야 한다.

### 14.3 전체 repository regression

과설계 제거 후 전체 repository suite까지 검증했다.

```text
125 passed, 8 warnings, 35 subtests passed
```

큰 routing 변경을 다시 할 때만 전체 suite를 재실행한다.

### 14.4 provider smoke test

실제 Master runtime provider invocation은 아직 subprocess smoke를 하지 않았다.

향후 별도 단계에서:

```text
Claude restricted/read-only Master invocation
Codex read-only Master invocation
```

을 실제 환경에서 smoke test할 수 있다.

이때도 repository write 권한은 주지 않는다.

### 14.5 documentation cleanup

state-machine relaxation이 충분히 안정화된 뒤:

- 과거 target sketch의 `READY` / `READY_FOR_MERGE` 표현 정리
- obsolete 설명 제거
- 최종 state diagram 갱신
- Phase별 임시 설명을 현재 구조 중심으로 통합

을 수행할 수 있다.

현재는 기록 보존을 위해 과거 Phase 내용을 삭제하지 않는다.

---

## 15. 다음 세션에서 바로 확인할 것

새 세션에서 이어갈 때 broad discovery를 다시 하지 않는다.

우선 확인할 파일:

```text
run_loop.py
tests/test_cli.py
docs/orca-loop-master-permission-refactor-plan.md
docs/20260919-orca-loop-current-status-and-next-steps.md
```

특히 `run_loop.py`에서 다음 symbol을 기준으로 이어간다.

```text
_worker_completion_master_context
_plan_review_can_be_skipped
_code_review_can_finish
_validate_worker_completion_master_decision
_route_worker_completion
_test_result_can_finish
_route_test_result
```

preview/inline state-collapse helper는 더 이상 추가하지 않는다.

다음 Phase에서는 먼저 현재 state-machine transition과 해당 state가 가진 실제 evidence/safety 의미를 읽고, 그 후에만 shortcut 설계를 한다.

---

## 16. 작업 규칙 요약

다음 세션에서도 그대로 유지한다.

```text
1. 실제 로컬 코드로 작업한다.
2. dirty worktree를 보존한다.
3. reset/restore/clean/stash/commit/push 금지.
4. dependency 설치 금지.
5. unrelated refactor 금지.
6. phase-by-phase로 진행한다.
7. 기존 path는 fallback으로 유지한다.
8. canonical evaluator를 재사용한다.
9. consensus/evidence/round semantics를 보존한다.
10. destructive approval을 우회하지 않는다.
11. Master에게 arbitrary jump를 허용하지 않는다.
12. Master에게 repository write 권한을 주지 않는다.
13. Permission Feasibility Spike를 재도입하지 않는다.
14. HUMAN_GATE의 merge/reject/revise 권한을 유지한다.
15. 테스트 evidence 없이 PASS를 주장하지 않는다.
```

---

## 17. 현재 결론

현재 Orca loop는 초기의 고정된 전체 절차에서 상당 부분 경량화되었다.

이미 완료된 핵심 변화:

```text
Permission Feasibility Spike 제거
정적 PermissionProfile 정책
Master runtime/control layer
safe plan direct implementation
safe verified PASS direct human disposition
clean code review -> HUMAN_GATE shortcut
```

Phase 14~18에서 시도한 deterministic durable-state collapse는 과설계로 판단해 철회했다.

현재 기준은 명확하다.

> **state 수를 줄이는 것이 아니라 실제 Master/worker 호출 수를 줄일 때만 경량화한다.**

`PLAN_CONSENSUS_EVALUATE`, `TEST_GATE`, `CONSENSUS_EVALUATE`는 비용이 작은 deterministic/resume boundary이므로 유지한다.

과설계 제거 후 생산 코드에서는 HEAD 대비 약 225줄의 state-collapse 로직을 제거했고, 전용 테스트도 약 540줄 제거했다. 전체 repository test는 `125 passed, 8 warnings, 35 subtests passed`로 PASS했다.

현재 안정 기준점은 **Phase 13까지의 실제 worker 절감 정책**이다.

다음 우선순위는 Phase 번호를 늘리는 것이 아니라 **실제 provider smoke 또는 Master/worker 호출 수 계측**이다.
