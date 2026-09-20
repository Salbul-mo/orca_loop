# Orca Loop 경량화 및 Master Agent 권한 제어 도입 계획서

## 1. 목적

현재 Orca Loop는 실행 전 `permission_spike.py`를 통해 실제 에이전트 세션을 생성하고, 역할별 read/write 가능 여부를 검증한 뒤 `permission-feasibility.json`을 생성해야 한다.

이 구조는 초기 아키텍처 검증 단계에서는 의미가 있었지만 현재 운영 구조에서는 다음 비용을 만든다.

- 매 실행 전 permission 검증을 위한 별도 에이전트 호출 필요
- 실제 작업과 무관한 AI 토큰 소비
- fixture repository 및 permission step 생성
- permission report 생성 및 digest 검증
- `run_id`마다 permission report를 다시 준비해야 하는 운영 부담
- 이미 확정된 Strategy D를 반복적으로 다시 검증
- 실제 작업보다 준비 단계가 복잡해지는 문제

이번 리팩터링에서는 `permission_spike.py`와 `PermissionFeasibilityReport` 기반 구조를 제거하고, **Master Agent가 필요한 worker와 권한 수준을 요청하고 Coordinator가 이를 정책적으로 검증·집행하는 구조**로 전환한다.

---

## 2. 핵심 설계 원칙

### 2.1 Master는 권한을 직접 부여하지 않는다

Master Agent는 다음 실행에 필요한 권한을 요청한다.

```json
{
  "action": "dispatch",
  "role": "implementer",
  "permissionProfile": "workspace_write",
  "reason": "대상 소스 코드 수정이 필요함"
}
```

실제 권한 부여 여부는 Coordinator가 결정한다.

```text
Master
  ↓
Permission Request
  ↓
Coordinator
  ↓
Permission Policy Validation
  ↓
Launch Profile 생성
  ↓
Worker 실행
```

Master가 임의로 높은 권한을 요청하더라도 Coordinator 정책을 통과하지 못하면 실행하지 않는다.

### 2.2 권한은 명시적인 Permission Profile로 제한한다

초기에는 다음 권한 프로필만 지원한다.

```text
CONTROL_ONLY
READ_ONLY
WORKSPACE_WRITE
TEST_EXECUTE
```

| Permission Profile | 용도 |
|---|---|
| `CONTROL_ONLY` | Master의 판단 및 routing |
| `READ_ONLY` | Planner, Reviewer |
| `WORKSPACE_WRITE` | Implementer, Fixer |
| `TEST_EXECUTE` | Coordinator Test Gate |

`FULL_ACCESS`, `UNRESTRICTED`와 같은 광범위한 권한 프로필은 만들지 않는다.

---

## 3. 목표 아키텍처

현재 구조:

```text
Permission Spike
   ↓
Permission Report
   ↓
Preflight
   ↓
Planner
   ↓
Plan Reviewer
   ↓
Implementer
   ↓
Test
   ↓
Code Reviewer
   ↓
Cross Confirm
```

변경 후:

```text
                  Master Agent
                       │
             작업 분석 / Routing
                       │
                       ▼
                  Coordinator
                       │
            Permission Policy
                       │
       ┌───────────────┼──────────────┐
       ▼               ▼              ▼
   READ_ONLY     WORKSPACE_WRITE   TEST_EXECUTE
       │               │              │
       ▼               ▼              ▼
 Planner/Reviewer   Implementer      Test Gate
```

Permission Spike는 전체 실행 흐름에서 제거한다.

---

## 4. Master Agent 역할

Master Agent는 기존 Planner나 Reviewer와 별개의 Control Plane 역할을 담당한다.

Master의 책임:

- 사용자 요청 분석
- 작업 규모 판단
- 작업 위험도 판단
- 다음 worker 결정
- worker에 필요한 Permission Profile 요청
- 계획 단계 필요 여부 결정
- review 필요 여부 결정
- test 필요 여부 결정
- worker 결과를 기반으로 다음 행동 결정
- 종료 가능 여부 판단
- 사용자 판단이 필요한 경우 escalation 요청

Master가 직접 수행하지 않는 작업:

- source file 수정
- destructive operation 실행
- test command 직접 실행
- permission policy 변경
- Coordinator 상태 직접 변경

---

## 5. Master Decision 계약

새로운 `MasterDecision` 계약을 도입한다.

```python
class MasterAction(StrEnum):
    DISPATCH = "dispatch"
    TEST = "test"
    FINISH = "finish"
    ESCALATE = "escalate"
    ABORT = "abort"
```

예상 모델:

```python
@dataclass(frozen=True)
class MasterDecision:
    action: MasterAction
    role: Role | None
    permission_profile: PermissionProfile | None
    reason: str
```

향후 필요하면 다음 필드를 추가한다.

```text
allowed_paths
required_checks
review_required
test_required
risk_level
```

1차 구현에서는 계약을 최소화한다.

---

## 6. Permission Policy

Coordinator가 권한 허용 여부를 결정한다.

초기 정적 정책:

```python
ROLE_ALLOWED_PERMISSIONS = {
    Role.PLANNER: {
        PermissionProfile.READ_ONLY,
    },
    Role.PLAN_REVIEWER: {
        PermissionProfile.READ_ONLY,
    },
    Role.IMPLEMENTER: {
        PermissionProfile.WORKSPACE_WRITE,
    },
    Role.CODE_REVIEWER: {
        PermissionProfile.READ_ONLY,
    },
    Role.CROSS_CONFIRMER: {
        PermissionProfile.READ_ONLY,
    },
}
```

Master에는 기본적으로 `CONTROL_ONLY`를 부여하며, repository 관찰이 필요한 설계라면 `READ_ONLY`까지 허용할 수 있다.

---

## 7. Permission Spike 제거 범위

### 7.1 `permission_spike.py`

최종적으로 삭제한다.

현재 포함된 다음 기능도 제거 대상이다.

- permission fixture 생성
- role별 permission output directory 생성
- fixture용 Git repository 초기화
- worker result 기록
- `V-PERM-01~06`
- permission report 생성
- permission report digest 계산

### 7.2 CLI

현재 필수 인자인 다음 옵션을 제거한다.

```text
--permission-report
```

`RunArguments`의 다음 필드도 제거한다.

```python
permission_report_path: Path
```

### 7.3 `PreflightResult`

다음 필드를 제거한다.

```python
permission_report: PermissionFeasibilityReport
```

### 7.4 `run_preflight()`

다음 permission report 검증을 제거한다.

- report path 존재 확인
- `parse_permission_report()`
- `status == PASS`
- `strategy == READONLY_REPOSITORY`
- Orca version과 report version 비교
- canonical path 검증
- 각 `V-PERM-*` check PASS 검증

Preflight에는 다음 검증을 유지한다.

```text
Python runtime
Target worktree
Request file
Git HEAD
Dirty worktree validation
Test policy
Orca runtime status
Orca version
Coordinator terminal
```

---

## 8. Launch Profile 구조 변경

현재:

```python
build_launch_profile(
    role,
    worktree,
    step_input,
    step_output,
    permission_report,
    ...
)
```

변경 후:

```python
build_launch_profile(
    role,
    permission_profile,
    worktree,
    step_input,
    step_output,
    ...
)
```

Launch Profile은 다음 정보를 기준으로 실제 command를 결정한다.

```text
Role
+
Provider
+
PermissionProfile
```

---

## 9. Permission Report Digest 제거

현재 Coordinator state에는 permission report provenance를 위한 다음 값이 존재한다.

```text
permission_report_digest
```

이를 다음 값으로 대체하는 것을 권장한다.

```text
permission_policy_digest
```

정적 Permission Policy를 canonical JSON으로 직렬화한 뒤 digest를 계산한다.

이렇게 하면 resume 시에도 해당 run이 어떤 권한 정책으로 시작됐는지 검증할 수 있다.

기존 구조:

```text
실제 Agent Permission Spike 결과의 digest
```

변경 구조:

```text
Coordinator가 집행하는 deterministic permission policy의 digest
```

---

## 10. 단계별 구현 계획

### Phase 1 — Permission Profile 도입

목표: Permission Spike를 제거하기 전에 새로운 권한 모델을 추가한다.

구현:

- `PermissionProfile` enum 추가
- Role별 allowed permission 정의
- Coordinator permission validation 함수 추가
- 단위 테스트 작성

완료 기준:

```text
Planner + READ_ONLY            → PASS
Reviewer + READ_ONLY           → PASS
Implementer + WORKSPACE_WRITE  → PASS

Reviewer + WORKSPACE_WRITE     → DENY
Planner + WORKSPACE_WRITE      → DENY
```

기존 실행 흐름은 이 단계에서는 유지한다.

### Phase 2 — Launch Profile 전환

목표: `PermissionFeasibilityReport` 대신 `PermissionProfile`로 worker command를 생성한다.

주요 수정 대상:

```text
orca_loop/profiles.py
orca_loop/coordinator.py
run_loop.py
관련 테스트
```

변경:

```text
permission_report
        ↓
permission_profile
```

완료 기준:

- Planner read-only 실행
- Reviewer read-only 실행
- Implementer write 실행
- 기존 provider/model/effort 설정 유지

### Phase 3 — Permission Report 의존성 제거

목표: 실제 loop에서 `permission-feasibility.json` 없이 실행 가능하게 만든다.

**상태: 완료 (2026-09-19)**

현재 production/test 코드에는 다음 항목의 참조가 남아 있지 않다.

```text
RunArguments.permission_report_path
PreflightResult.permission_report
--permission-report
parse_permission_report()
PermissionFeasibilityReport
```

실행 권한 provenance는 `permission_policy_digest`로 대체되었고,
resume 시 현재 정적 Permission Policy와 committed digest가 다르면 실행을 거부한다.

제거 대상:

```text
RunArguments.permission_report_path
PreflightResult.permission_report
--permission-report
parse_permission_report 사용
permission report preflight validation
```

완료 기준:

```text
python run_loop.py ...
```

실행 시 permission report가 필요하지 않아야 한다.

### Phase 4 — Permission Spike 삭제

목표: Permission Spike 시스템을 코드베이스에서 완전히 제거한다.

**상태: 완료 (2026-09-19)**

production/test 코드 기준으로 Permission Spike 전용 구현은 제거되었다.

```text
permission_spike.py                 없음
permission feasibility 전용 테스트 없음
PermissionFeasibilityReport        없음
PermissionCheck                    없음
PermissionStrategy                 없음
AgentAccessMode                    없음
ProviderCapability                 없음
V-PERM-* runtime/test dependency   없음
```

`phase1-system-design.md`, `phase2-macro-blocking.md`, 과거 Phase 3/4 구현 문서에
남아 있는 `V-PERM-*` 및 Permission Spike 설명은 당시 설계/검증 기록이므로
역사적 문서로 보존한다. 현행 구현 계약으로 해석하지 않는다.

삭제:

```text
permission_spike.py
permission feasibility 전용 테스트
permission spike fixture logic
obsolete docs references
```

정리 후보:

```text
PermissionFeasibilityReport
PermissionCheck
PermissionStrategy
관련 parser/serializer
```

단, 실제 삭제 전에 전체 참조를 검색하여 production dependency가 없는지 확인한다.

---

## 11. Master Agent 도입 순서

Permission 구조 전환이 완료된 뒤 Master를 추가한다.

Master 도입과 Permission Spike 제거를 한 번에 묶지 않는 이유:

- permission regression과 routing regression을 분리할 수 있음
- 테스트 실패 원인 추적이 쉬움
- intermediate state에서도 기존 loop 실행 가능

### Phase 5 — Master Contract

**상태: 완료 (2026-09-19)**

Master routing에는 연결하지 않은 상태에서 계약 계층만 추가하고 검증했다.

```text
MasterAction
MasterDecision
MasterDecision parser
MasterDecision validator
prompts/master.md
```

검증 결과:

```text
targeted Master contract/policy tests
8 passed, 4 subtests passed

contracts + coordinator regression
31 passed, 18 subtests passed

contracts + permission/resume regression
60 passed, 26 subtests passed
```

현재 `run_loop.py` routing에는 Master를 연결하지 않는다. Runtime/routing 변경은
Phase 6 이후 단계에서 별도로 수행한다.

추가:

```text
MasterAction
MasterDecision
MasterDecision parser
MasterDecision validator
prompts/master.md
```

Master는 이 단계에서는 실제 routing에 연결하지 않고 계약 테스트부터 작성한다.

### Phase 6 — Master Runtime

Master 전용 agent runtime을 추가한다.

**상태: 완료 (2026-09-19)**

기존 4-worker runtime schema와 snapshot을 변경하지 않고 Master runtime을 별도
계약/lifecycle로 추가했다.

```text
MasterRuntimeOptions
MasterRuntimeConfig
MasterRuntimeSnapshot
master-runtime.json
--master-config PATH
master_runtime.py adapter boundary
```

Master runtime config는 독립 schema v1을 사용하며, resume 시 별도 immutable
snapshot의 configuration digest drift를 검증한다. Master 설정이 없는 기존 run은
기존 worker runtime 동작을 그대로 유지한다.

`master_runtime.py`는 정적 `prompts/master.md`와 canonical JSON decision context를
provider invocation boundary에 전달하고, 반환값을 `parse_master_decision()`으로
검증한다. 이 단계에서는 실제 loop routing을 연결하지 않는다.

검증 결과:

```text
Master runtime contract/snapshot targeted tests
2 passed

Master runtime adapter tests
4 passed

Master config lifecycle targeted tests
2 passed

Master runtime 관련 regression
69 passed, 26 subtests passed
```

기존 4-worker runtime schema와 직접 결합하지 않는 것을 우선한다.

예시:

```text
master:
  provider: claude
  model: ...
  effort: ...
```

기존 4-worker agent runtime snapshot과 backward compatibility를 유지한다.

### Phase 7 — Master 최초 Routing

**상태: 완료 (2026-09-19)**

Master가 설정된 신규 run에서 최초 `INIT` routing만 Master 판단에 연결했다.
이 단계에서는 worker 완료 후 routing, test 결과 routing, 종료 판단은 연결하지
않는다.

현재 동작:

```text
Master runtime 없음
INIT + OK
→ PLAN

Master runtime 있음
INIT
→ Master 1회 호출
→ Coordinator validate_master_decision()
→ planner/read_only       → PLAN
→ implementer/workspace_write → IMPLEMENT
```

초기 routing에서는 `dispatch` action만 허용하며 `planner`와 `implementer`만
선택할 수 있다. `code_reviewer` 등 다른 worker role은 Role/Profile 조합이
일반 permission policy상 유효하더라도 초기 routing 단계에서는 거부한다.
`test`, `finish`, `escalate`, `abort`도 INIT 단계에서는 임의 state로 매핑하지
않고 명시적으로 거부한다.

Master decision context는 Coordinator가 읽은 deterministic 입력만 전달한다.

```text
stage = initial_routing
runId
currentState = INIT
request = request.md UTF-8 원문
allowedDispatches = planner/read_only, implementer/workspace_write
```

Master는 대상 repository를 직접 작업하지 않는다. 실제 provider invocation은
worker `LaunchProfile`을 재사용하지 않고 별도 command builder를 사용한다.

```text
Claude
- noninteractive print mode
- restricted
- tools disabled
- permission-mode plan

Codex
- exec
- sandbox read-only
- approval never
- skip-git-repo-check
- ephemeral
```

두 provider 모두 임시 빈 working directory에서 실행하며 worker용 권한 우회
옵션인 `bypassPermissions`와
`--dangerously-bypass-approvals-and-sandbox`를 사용하지 않는다. timeout/process
tree 종료 방식은 기존 worker runner 관례와 동일한 방향으로 구현했다.

검증 결과:

```text
Master runtime adapter/provider command tests
6 passed

INIT routing targeted tests
6 passed, 18 deselected

Master/permission/CLI 관련 regression
76 passed, 4 warnings, 26 subtests passed
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test는 이 단계에서 실행하지 않았다.

### Phase 8 — Worker 완료 후 Master 판단

**상태: 완료 (2026-09-19)**

검증된 worker artifact가 `ARTIFACT_VERIFIED`까지 통과하고
`ARTIFACT_OK`가 생성된 뒤, Master runtime이 설정된 경우에만 Master를 한 번
호출하도록 연결했다. timeout, worker escalation, decision gate, Coordinator
escalation 등 비정상 signal은 Master로 우회하지 않고 기존 Coordinator 전이를
그대로 유지한다.

이번 단계에서는 고정 state machine을 완화하지 않는다. Master는 현재 state에서
기존 machine이 허용하는 다음 worker/test 단계 또는 escalation만 선택할 수 있다.

```text
PLAN / PLAN_REVISE
→ dispatch plan_reviewer/read_only
→ 또는 escalate

IMPLEMENT / FIX
→ test
→ 또는 escalate

CODE_REVIEW
→ dispatch cross_confirmer/read_only
→ 또는 escalate
```

다음 Coordinator 내부 평가 전이는 아직 Master routing 대상이 아니다.

```text
PLAN_REVIEW → PLAN_CONSENSUS_EVALUATE
CROSS_CONFIRM → CONSENSUS_EVALUATE
```

`finish`도 이 단계에서는 허용하지 않으며 종료 판단 단계에서 별도로 연결한다.
따라서 Master가 `PLAN → IMPLEMENT`처럼 기존 검토 경계를 건너뛰거나,
`IMPLEMENT → finish`처럼 test gate를 생략하는 결정을 반환하면 Coordinator가
거부한다.

Master decision context에는 Coordinator가 이미 검증한 정보만 전달한다.

```text
stage = worker_completion
runId
currentState
completedRole
request
artifact = verified artifact의 wire value
resultSignal = ARTIFACT_OK
allowedDecisions
```

`prompts/master.md`에도 `allowedDispatches`와 `allowedDecisions`가 제공된 경우 해당
목록 밖의 transition을 만들지 않도록 stage-bound 규칙을 추가했다.

검증 결과:

```text
Worker-completion Master targeted tests
4 passed, 24 deselected

Master/permission/CLI 관련 regression
80 passed, 4 warnings, 26 subtests passed
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test는 이 단계에서도 실행하지 않았다.

### Phase 9 — Test 결과 후 Master 판단

**상태: 완료 (2026-09-19)**

`TEST_GATE`에서 실제 test policy를 실행한 뒤 생성되는 기존
`PASS` / `NOT_RUN` / `FAIL` 결과에 대해, Master runtime이 설정된 경우
Master가 다음 bounded action을 선택하도록 연결했다.

이번 단계 역시 고정 state machine을 완화하지 않는다.

```text
PASS / NOT_RUN
→ dispatch code_reviewer/read_only
→ 또는 escalate

FAIL
→ dispatch implementer/workspace_write
→ 또는 escalate
```

따라서 다음과 같은 transition은 아직 허용하지 않는다.

```text
PASS → finish
FAIL → planner
FAIL → 임의 worker
```

`POLICY_VIOLATION`은 Master 판단으로 우회하지 않는다. 기존 Coordinator 정책대로
`USER_DECISION_REQUIRED`로 진행하여 test execution policy 위반을 Master가
덮어쓰지 못하게 유지한다.

Master decision context에는 현재 Coordinator가 보유하는 test 결과 계약 범위만
전달한다.

```text
stage = test_result
runId
currentState = TEST_GATE
request
plan
testStatus
resultSignal
testFixAttempts
allowedDecisions
```

현재 `execute_test_gate()` 계약은 test command의 상세 stdout/stderr를
`StepExecutionResult`에 보존하지 않고 `TestGateStatus`와 signal을 반환한다.
따라서 이번 단계에서는 별도의 test-result persistence 계약을 추가하지 않고
기존 검증 결과만 Master 입력으로 사용한다.

검증 결과:

```text
Test-result Master targeted tests
4 passed, 28 deselected

Master/permission/CLI 관련 regression
84 passed, 4 warnings, 26 subtests passed
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

### Phase 10 — 종료 직전 Master 판단

**상태: 완료 (2026-09-19)**

코드 consensus 평가가 정상적으로 완료되어 `CONSENSUS_EVALUATE`에서
`UNRESOLVED_ZERO`가 생성된 경우에만, Master runtime이 설정되어 있으면
Master를 한 번 호출하도록 연결했다.

이번 단계에서도 고정 state machine과 최종 human safety boundary는 완화하지
않는다. 허용되는 Master decision은 다음 두 가지뿐이다.

```text
finish
→ 기존 UNRESOLVED_ZERO 결과 유지
→ 기존 machine이 HUMAN_GATE로 전이
→ 최종 merge/reject/revise 결정은 human gate가 계속 담당

escalate
→ USER_DECISION_REQUIRED
```

따라서 `finish`는 `READY_FOR_MERGE`를 직접 의미하지 않는다. Master는 검증된
evidence가 최종 human disposition 단계로 진행하기에 충분하다고 판단할 수 있을
뿐이며, 기존 `HUMAN_GATE + MERGE/REJECT/REVISE_*` 권한을 대체하지 않는다.

다음 동작은 이번 단계에서 허용하지 않는다.

```text
finish → READY_FOR_MERGE 직접 전이
dispatch arbitrary worker
abort
UNRESOLVED_REMAIN에서 Master final routing
Coordinator escalation을 Master final routing으로 우회
```

Master decision context에는 현재 Coordinator가 검증한 final evidence 범위만
전달한다.

```text
stage = final_decision
runId
currentState = CONSENSUS_EVALUATE
request
plan
ledger
testStatus
resultSignal = UNRESOLVED_ZERO
allowedDecisions = [finish, escalate]
```

`finish`가 반환되면 원래 `StepExecutionResult`를 그대로 유지하여 기존
`commit_step_transition()`이 `CONSENSUS_EVALUATE + UNRESOLVED_ZERO → HUMAN_GATE`
전이를 수행한다. 즉 Master는 여전히 상태 전이 authority가 아니다.

검증 결과:

```text
Final-decision Master targeted tests
4 passed, 32 deselected

Master/permission/CLI 관련 regression
88 passed, 4 warnings, 26 subtests passed
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

다음 구현 단계는 item 19의 고정 state machine 단계적 완화이며, 이번 Phase 10에는
포함하지 않았다.

---

## 12. Master 호출 지점

초기에는 Master를 모든 transition마다 호출하지 않는다.

### 12.1 Run 시작

Master가 최초 작업 경로를 결정한다.

```text
간단한 수정
→ IMPLEMENTER

설계 변경
→ PLANNER

복잡한 변경
→ PLANNER + REVIEW
```

### 12.2 Worker 완료

```text
worker result
   ↓
Master
   ├─ another worker
   ├─ test
   ├─ review
   ├─ finish
   └─ escalate
```

### 12.3 Test 실패

```text
implementation 문제
→ implementer

설계 문제
→ planner

환경 문제
→ escalate
```

### 12.4 종료 직전

Master가 evidence가 충분한지 확인한다.

단, 종료 결정 역시 Coordinator 정책을 통과해야 한다.

---

## 13. 기존 고정 State Machine의 단계적 완화

초기에는 현재 state machine을 유지한다.

### Phase 11 — 1차 완화: 안전한 plan의 plan review 생략

**상태: 부분 완료 (2026-09-19)**

item 19의 첫 완화로, `PLAN` / `PLAN_REVISE` worker가 검증된 `PlanDocument`를
반환한 뒤 Master가 다음 worker를 선택할 때 제한적으로 `implementer`를 직접
선택할 수 있게 했다.

다만 plan review 생략은 모든 plan에 허용하지 않는다. Coordinator가 다음 조건을
모두 확인한 경우에만 `implementer/workspace_write`를 `allowedDecisions`에 추가한다.

```text
verified PlanDocument
result signal = ARTIFACT_OK
ledger unresolved finding count = 0
data/API/schema change = 없음
delete/rename affected file = 없음
```

위 조건을 만족하지 않으면 기존 경계를 유지한다.

```text
PLAN / PLAN_REVISE
→ plan_reviewer/read_only
→ 또는 escalate
```

조건을 만족하는 경우에만 다음 선택지가 추가된다.

```text
PLAN / PLAN_REVISE
→ plan_reviewer/read_only
→ implementer/workspace_write
→ 또는 escalate
```

Master가 direct implementation을 선택하면 Coordinator가 검증된 artifact의 ledger,
plan version, snapshot provenance가 이미 `ARTIFACT_VERIFIED`에 기록된 상태에서
`TRANSITION_COMMITTED + IMPLEMENT`를 즉시 기록한다. 이후 `_execute_worker()`는
고정 `PLAN -> PLAN_REVIEW` transition을 다시 적용하지 않는다.

이 완화는 기존 `PLAN_CONSENSUS_EVALUATE`에서 수행하던 사용자 승인 필요 조건을
우회하지 않도록 제한된다. 특히 다음 plan은 review 생략 대상이 아니다.

```text
unresolved finding이 있는 plan
data/API/schema contract 변경 plan
delete/rename destructive plan
```

아직 완화하지 않은 경계:

```text
IMPLEMENT / FIX → TEST_GATE
TEST_GATE → CODE_REVIEW
CODE_REVIEW → CROSS_CONFIRM
CROSS_CONFIRM → CONSENSUS_EVALUATE
CONSENSUS_EVALUATE → HUMAN_GATE
```

따라서 item 19 전체를 완료한 것은 아니다.

검증 결과:

```text
Plan-review relaxation targeted tests
6 passed, 32 deselected

Master/permission/CLI 관련 regression
90 passed, 4 warnings, 26 subtests passed
exit code = 0
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

### Phase 12 — 2차 완화: 저위험 PASS의 code-review chain 생략

**상태: 부분 완료 (2026-09-19)**

item 19의 두 번째 완화로, `TEST_GATE`가 실제 `PASS`로 끝난 저위험 변경에 한해
Master가 `code_reviewer` 대신 `finish`를 선택할 수 있게 했다.

`finish`는 `READY_FOR_MERGE`로 직접 이동하지 않는다. Coordinator가 검증한 뒤
기존 최종 사람 판단 경계인 `HUMAN_GATE`로만 이동한다.

shortcut은 다음 조건을 모두 만족해야 열린다.

```text
result signal = PASS
test_gate_status = PASS
ledger unresolved finding count = 0
plan data/API/schema change = 없음
plan affected_files에 delete/rename 없음
```

위 조건을 만족하면 `allowedDecisions`는 다음 선택을 포함한다.

```text
code_reviewer/read_only
finish
escalate
```

Master가 `finish`를 선택하면 Coordinator가 `TRANSITION_COMMITTED + HUMAN_GATE`를
기록한다. `test_fix_attempts`는 기존 `TEST_GATE + PASS` machine semantics와 동일하게
0으로 리셋하고 `operational_retries`는 보존한다.

다음 경우에는 shortcut이 열리지 않고 기존 `CODE_REVIEW` 경계를 유지한다.

```text
NOT_RUN
PASS signal과 test_gate_status 불일치
unresolved finding 존재
data/API/schema 변경 plan
delete/rename destructive plan
```

즉 Phase 12는 저위험 변경에서만 아래 경로를 허용한다.

```text
IMPLEMENT / FIX
→ TEST_GATE(PASS)
→ Master finish
→ HUMAN_GATE
```

`HUMAN_GATE`의 merge/reject/revise authority는 그대로 사람에게 남는다.

아직 완화하지 않은 일반/고위험 review 경계:

```text
TEST_GATE → CODE_REVIEW
CODE_REVIEW → CROSS_CONFIRM
CROSS_CONFIRM → CONSENSUS_EVALUATE
CONSENSUS_EVALUATE → HUMAN_GATE
```

따라서 item 19 전체는 여전히 진행 중이다.

검증 결과:

```text
Test-result relaxation targeted tests
5 passed, 35 deselected

Master/permission/CLI 관련 regression
92 passed, 4 warnings, 26 subtests passed
exit code = 0
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

### Phase 13 — 3차 완화: clean code review의 cross-confirm 생략

**상태: 부분 완료 (2026-09-19)**

item 19의 세 번째 완화로, 이미 실제 테스트가 `PASS`했고 code review 자체도
완전히 clean한 저위험 변경에 한해 Master가 `cross_confirmer` 대신 `finish`를
선택할 수 있게 했다.

`finish`는 `READY_FOR_MERGE`로 직접 이동하지 않는다. Coordinator가 검증한 뒤
기존 최종 사람 판단 경계인 `HUMAN_GATE`로만 이동한다.

shortcut은 다음 조건을 모두 만족해야 열린다.

```text
worker result signal = ARTIFACT_OK
test_gate_status = PASS
artifact kind = CODE_REVIEW
artifact role = code_reviewer
verdict = APPROVE
reviewed_finding_ids = empty
finding_decisions = empty
findings = empty
non_blocking_suggestions = empty
artifact escalation_signals = empty
result escalations = empty
current plan artifact 존재
ledger unresolved finding count = 0
plan data/API/schema change = 없음
plan affected_files에 delete/rename 없음
```

위 조건을 만족하면 `allowedDecisions`는 다음 선택을 포함한다.

```text
cross_confirmer/read_only
finish
escalate
```

Master가 `finish`를 선택하면 Coordinator가
`TRANSITION_COMMITTED + HUMAN_GATE`를 기록한다. 기존 code-review 경계의 counter
semantics를 변경하지 않으며, 이전 `PASS` test gate 상태도 그대로 보존한다.

다음 경우에는 shortcut이 열리지 않고 기존 `CODE_REVIEW -> CROSS_CONFIRM` 경계를
유지한다.

```text
test status가 PASS가 아님
CHANGES_REQUESTED verdict
reviewed finding 존재
finding decision 존재
blocking finding 존재
non-blocking suggestion 존재
artifact/result escalation 존재
unresolved ledger finding 존재
data/API/schema 변경 plan
delete/rename destructive plan
current plan artifact 부재
```

즉 Phase 13은 아래 경로만 조건부로 허용한다.

```text
CODE_REVIEW(clean, low-risk, tested)
→ Master finish
→ HUMAN_GATE
```

`HUMAN_GATE`의 merge/reject/revise authority는 그대로 사람에게 남는다.
따라서 item 19 전체는 여전히 진행 중이다.

검증 결과:

```text
Worker-completion relaxation targeted tests
8 passed, 34 deselected, 9 subtests passed
exit code = 0

Master/permission/CLI 관련 regression
94 passed, 4 warnings, 35 subtests passed
exit code = 0
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

### Phase 14 — 4차 완화: clean cross-confirm의 consensus-evaluate 상태 접기 [REVERTED]

**상태: 부분 완료 (2026-09-19)**

item 19의 네 번째 완화로, 이미 실제 테스트가 `PASS`했고 cross confirmer까지
완전히 clean하게 동의한 저위험 변경에 한해 별도의 durable
`CONSENSUS_EVALUATE` 상태를 거치지 않고 Master가 최종 사람 판단으로 넘길 수 있게 했다.

중요하게도 `CONSENSUS_EVALUATE`가 담당하던 검증 자체를 삭제한 것은 아니다.
Coordinator는 `CROSS_CONFIRM` 완료 직후 기존 `execute_evaluate()`를 preview로 실행해
다음을 그대로 수행한다.

```text
code round evidence 검증
both_artifacts_valid 검증
snapshot digest 일치 검증
code consensus round limit 검증
code_round + 1 commit 결과 계산
unresolved finding / escalation 계산
```

이 preview 결과가 실제 `UNRESOLVED_ZERO`이고 escalation이 없을 때만 shortcut이 열린다.
preview는 immutable ledger에서 새 ledger를 계산하므로 부적격 경로에서는 결과를 버리고
기존 `CROSS_CONFIRM -> CONSENSUS_EVALUATE` 상태 전이를 그대로 사용한다.

shortcut 조건은 다음과 같다.

```text
worker result signal = ARTIFACT_OK
test_gate_status = PASS
artifact kind = CROSS_REVIEW
artifact role = cross_confirmer
verdict = APPROVE
agrees_with_reviewer = true
reviewed_finding_ids = empty
finding_decisions = empty
findings = empty
non_blocking_suggestions = empty
artifact escalation_signals = empty
result escalations = empty
current plan artifact 존재
ledger unresolved finding count = 0
plan data/API/schema change = 없음
plan affected_files에 delete/rename 없음
code-round preview = UNRESOLVED_ZERO
preview escalations = empty
```

위 조건을 만족한 경우에만 Master에게 다음 선택을 제공한다.

```text
finish
escalate
```

Master가 `finish`를 선택하면 Coordinator는 preview에서 계산된 ledger를 사용해
다음을 durable하게 기록한다.

```text
step_stage = TRANSITION_COMMITTED
state = HUMAN_GATE
signal = UNRESOLVED_ZERO
code_round = previous code_round + 1
test_gate_status = PASS 유지
```

따라서 이 shortcut은 기존 정상 경로의 핵심 consensus commit semantics를 보존한다.

```text
기존:
CROSS_CONFIRM
→ CONSENSUS_EVALUATE
→ Master final decision
→ HUMAN_GATE

Phase 14 shortcut:
CROSS_CONFIRM(clean agreement)
→ code consensus evaluate/round commit을 같은 routing 단계에서 수행
→ Master finish
→ HUMAN_GATE
```

다음 경우에는 Master shortcut을 호출하지 않고 기존
`CROSS_CONFIRM -> CONSENSUS_EVALUATE` 경로를 그대로 유지한다.

```text
test status가 PASS가 아님
cross confirmer가 reviewer와 disagree
clean APPROVE가 아님
reviewed finding / decision / finding 존재
non-blocking suggestion 존재
artifact/result escalation 존재
unresolved ledger finding 존재
data/API/schema 변경 plan
delete/rename destructive plan
current plan artifact 부재
round evidence가 유효하지 않음
preview 결과가 UNRESOLVED_ZERO가 아님
preview escalation 존재
```

`READY_FOR_MERGE`로 직접 이동하는 경로는 추가하지 않았다.
최종 merge/reject/revise 권한은 계속 `HUMAN_GATE`의 사람에게만 있다.
따라서 item 19 전체는 여전히 진행 중이다.

검증 결과:

```text
Worker-completion relaxation targeted tests
10 passed, 34 deselected, 13 subtests passed
exit code = 0

Master/permission/CLI 관련 regression
96 passed, 4 warnings, 39 subtests passed
exit code = 0
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

### Phase 15 — 5차 완화: clean plan review의 plan-consensus-evaluate 상태 접기 [REVERTED]

**상태: 부분 완료 (2026-09-19)**

item 19의 다섯 번째 완화로, 이미 `PLAN_REVIEW`까지 수행했고 plan reviewer가
완전히 clean한 `APPROVE`를 반환한 경우 별도의 durable
`PLAN_CONSENSUS_EVALUATE` 상태를 반드시 거치지 않아도 되게 했다.

이 변경도 plan consensus 검증 자체를 삭제하지 않는다. Coordinator는
`PLAN_REVIEW` 완료 직후 기존 `execute_evaluate()`를
`PLAN_CONSENSUS_EVALUATE` 상태로 preview 실행해 다음 기존 의미를 그대로 재사용한다.

```text
plan round evidence 검증
plan.json + plan_review.json 유효성 검증
reviewed plan version 일치 검증
plan consensus round limit 검증
plan_round + 1 commit 결과 계산
unresolved finding 계산
data/API/schema 변경 E-03 계산
delete/rename destructive approval 계산
```

preview 결과가 실제 `UNRESOLVED_ZERO`이고 escalation이 없을 때만 Master routing을 연다.
그 경우 Master에게 허용되는 선택은 다음 둘뿐이다.

```text
dispatch implementer / workspace_write
escalate
```

`finish`, planner 재호출, 임의 role dispatch는 허용하지 않는다.

Master가 implementer dispatch를 선택하면 Coordinator는 preview에서 계산한 ledger를
사용해 다음 transition을 durable하게 기록한다.

```text
step_stage = TRANSITION_COMMITTED
state = IMPLEMENT
signal = UNRESOLVED_ZERO
plan_round = previous plan_round + 1
```

따라서 기존 정상 경로가 소유하던 plan consensus round commit semantics를 유지한다.

```text
기존:
PLAN_REVIEW
→ PLAN_CONSENSUS_EVALUATE
→ IMPLEMENT

Phase 15 shortcut:
PLAN_REVIEW(clean APPROVE)
→ plan consensus evaluate/round commit을 같은 routing 단계에서 수행
→ Master dispatch implementer
→ IMPLEMENT
```

다음 경우에는 Master를 호출하지 않고 기존
`PLAN_REVIEW -> PLAN_CONSENSUS_EVALUATE` 경로를 그대로 유지한다.

```text
worker result가 ARTIFACT_OK가 아님
artifact kind/role/verdict가 clean PLAN_REVIEW APPROVE가 아님
reviewed finding / decision / finding 존재
non-blocking suggestion 존재
artifact/result escalation 존재
current plan artifact 부재
plan round evidence가 불완전함
preview 결과가 UNRESOLVED_ZERO가 아님
data/API/schema 변경으로 E-03 발생
delete/rename에 destructive approval이 없어 E-03 발생
plan version mismatch
plan consensus round limit 초과
```

특히 preview는 shortcut을 위한 선행 탐색이므로, 기존 durable evaluate 단계에서
발생해야 할 `InvalidRoundError`를 shortcut 실행 시점에 더 일찍 노출하지 않는다.
PLAN preview에서 이 특정 round 오류가 발생하면 preview를 사용할 수 없는 것으로 보고
기존 `PLAN_CONSENSUS_EVALUATE` 상태 전이로 fallback한다. broad exception catch는 추가하지 않았다.

이 Phase는 `HUMAN_GATE` 최종 권한을 변경하지 않는다. 이후 코드/test/review 경로가
어떻게 단축되더라도 merge/reject/revise의 최종 판단은 계속 사람에게 남는다.
따라서 item 19 전체는 여전히 진행 중이다.

검증 결과:

```text
Worker-completion relaxation targeted tests
13 passed, 34 deselected, 17 subtests passed
exit code = 0

Master/permission/CLI 관련 regression
99 passed, 4 warnings, 43 subtests passed
exit code = 0
```

focused test에는 다음 fallback도 포함한다.

```text
non-blocking suggestion
data/API/schema E-03
delete destructive E-03
plan_review.json 누락
plan consensus round limit 초과
```

위 regression은 관련 suite만 실행한 결과이며 전체 repository test 결과가 아니다.
실제 Claude/Codex provider subprocess smoke test도 아직 실행하지 않았다.

현재:

```text
PLAN
→ PLAN_REVIEW
→ PLAN_CONSENSUS
→ IMPLEMENT
→ TEST
→ CODE_REVIEW
→ CROSS_CONFIRM
→ CONSENSUS
→ HUMAN_GATE
```

Master가 안정화된 이후 다음 구조로 점진적으로 변경한다.

```text
MASTER
   ↓
필요한 단계만 실행
   ↓
MASTER
   ↓
다음 단계
```

### 작은 변경

```text
MASTER
→ IMPLEMENT
→ TEST
→ MASTER
→ READY
```

### 일반 변경

```text
MASTER
→ PLAN
→ IMPLEMENT
→ TEST
→ CODE_REVIEW
→ MASTER
→ READY
```

### 위험한 변경

```text
MASTER
→ PLAN
→ PLAN_REVIEW
→ IMPLEMENT
→ TEST
→ CODE_REVIEW
→ CROSS_CONFIRM
→ HUMAN_GATE
```

---

## 14. 안전 경계

Permission Spike를 제거하더라도 안전 장치는 유지한다.

### 14.1 Destructive operation

다음 작업은 Master가 자동 승인할 수 없도록 한다.

```text
파일 삭제
파일 rename
대규모 데이터 변경
위험한 외부 side effect
```

필요한 경우 기존 human escalation을 유지한다.

### 14.2 Permission escalation

Master는 허용된 permission profile 범위를 초과할 수 없다.

예:

```text
CODE_REVIEWER + WORKSPACE_WRITE
```

요청은 Coordinator가 거부한다.

### 14.3 Test policy

Master가 테스트가 불필요하다고 판단하더라도 Coordinator test policy에서 필수라면 테스트를 수행한다.

즉 Master는 정책 제안자이며 최종 safety authority가 아니다.

---

## 15. 테스트 계획

### Permission Policy 테스트

검증 항목:

- 허용된 Role/Profile 조합
- 금지된 Role/Profile 조합
- unknown profile 거부
- Master의 과도한 권한 요청 거부

### Launch Profile 테스트

Provider별 지원 조합을 검증한다.

```text
Claude READ_ONLY
Codex READ_ONLY
Codex WORKSPACE_WRITE
Claude WORKSPACE_WRITE
```

실제 지원하지 않는 조합은 명시적으로 거부한다.

### Preflight 테스트

Permission report 없이 정상 preflight가 가능해야 한다.

```text
normal preflight → PASS
```

기존 permission-report 관련 테스트는 제거하거나 Permission Policy 테스트로 대체한다.

### Resume 테스트

Permission report digest 대신 다음 값을 사용한다.

```text
permission_policy_digest
```

정책이 변경된 상태에서 resume할 경우 기본적으로 BLOCKED 처리하거나 명시적인 migration 정책을 적용한다.

---

## 16. Migration 정책

기존 run에는 다음 값이 저장되어 있을 수 있다.

```text
permission_report_digest
```

신규 run부터는 다음 값을 사용한다.

```text
permission_policy_digest
```

선택지는 두 가지다.

### 선택 A — 기존 run 호환 유지

```text
기존 run → 기존 permission-report 기반 resume
신규 run → permission-policy 기반 실행
```

장점은 호환성이지만 코드 복잡성이 증가한다.

### 선택 B — 신규 schema로 절단

```text
기존 run → 새 버전에서 resume 지원하지 않음
신규 run → 새로운 permission policy만 사용
```

현재 개발 단계에서 기존 run resume 보장이 중요하지 않다면 선택 B가 훨씬 단순하다.

구현 전 어느 정책을 사용할지 확정한다.

---

## 17. 최종 삭제 대상

최종 상태에서 제거 후보:

```text
permission_spike.py

PermissionFeasibilityReport
PermissionCheck
PermissionStrategy

parse_permission_report()
permission report serializer/parser 관련 함수

--permission-report

RunArguments.permission_report_path
PreflightResult.permission_report

permission_report_digest

V-PERM-01
V-PERM-02
V-PERM-03
V-PERM-04
V-PERM-05
V-PERM-06
```

실제 삭제 전 전체 reference search를 수행한다.

---

## 18. 신규 구성요소

추가되는 주요 요소:

```text
PermissionProfile
PermissionPolicy
permission_policy_digest

MasterAction
MasterDecision
MasterDecision validator

prompts/master.md

Master runtime adapter
Master routing logic
```

---

## 19. 권장 구현 순서

```text
1. PermissionProfile 추가
2. Role → PermissionProfile 정책 추가
3. Coordinator permission validation 추가
4. LaunchProfile을 permission profile 기반으로 변경
5. 관련 테스트 통과
6. --permission-report 제거
7. Preflight permission report 의존 제거
8. CoordinatorState permission report provenance 제거/교체
9. permission_spike.py 삭제
10. obsolete permission 타입/테스트/문서 제거
11. 전체 regression test
12. MasterDecision 계약 추가
13. master.md 추가
14. Master runtime 추가
15. Master 최초 routing 연결
16. worker 완료 후 Master 판단 연결
17. test 결과 후 Master 판단 연결
18. 종료 판단 연결
19. 고정 state machine 단계적 완화
```

핵심은 **Permission Spike 제거와 Master routing 도입을 별도 단계로 검증하는 것**이다.

---

## 20. 완료 상태

최종 목표:

```text
User Request
      ↓
   Master
      ↓
작업 난이도/위험도 판단
      ↓
다음 Worker + 필요 권한 요청
      ↓
 Coordinator
      ↓
정적 Permission Policy 검증
      ↓
필요한 Worker만 실행
      ↓
필요한 Test/Review만 실행
      ↓
   Master
      ↓
완료 / 추가 작업 / Escalation
```

기존 다음 구성은 제거한다.

```text
Permission Spike
Permission Fixture
Permission Worker Sessions
Permission Feasibility Report
Permission Report Digest
--permission-report
```

## 21. 기대 효과

- permission 검증을 위한 AI 토큰 소비 제거
- 매 run마다 수행하던 사전 permission 작업 제거
- 실행 준비 시간 감소
- worker 생성 및 dispatch 횟수 감소
- Permission 정책 책임 경계 명확화
- Master가 작업 규모에 따라 필요한 worker만 선택 가능
- 작은 작업의 전체 loop 비용 감소
- Coordinator의 deterministic safety boundary 유지
- 향후 경량/표준/엄격 실행 경로 확장 가능

핵심 원칙:

> **Master는 작업을 지휘하고 필요한 권한을 요청한다. Coordinator는 허용된 권한만 집행한다. Permission Spike를 통한 반복 실측은 제거한다.**

---

## 2026-09-20 과설계 정정

Phase 14~18에서 시도한 deterministic durable-state collapse는 현재 구현에서 철회됐다.

이유:

```text
실제 LLM/worker 호출 감소 없음
로컬 transition 자체 비용은 작음
resume/debug boundary를 잃는 대신 preview/fallback 분기가 증가
virtual source-state와 inline routing이 코드 복잡도를 증가
전용 테스트 표면이 과도하게 확대
```

현재 유지하는 최적화 기준은 Phase 11~13처럼 실제 agent 작업을 줄이는 경우다.

```text
safe plan -> plan reviewer 생략 가능
safe verified PASS -> review chain 생략 가능
clean code review -> cross confirmer 생략 가능
```

과설계 제거 후 검증:

```text
focused Master routing: 13 passed, 29 deselected, 9 subtests passed
related regression: 94 passed, 4 warnings, 35 subtests passed
full repository: 125 passed, 8 warnings, 35 subtests passed
```

향후에는 durable state 개수를 줄이는 것 자체를 목표로 하지 않는다. 실제 worker/provider 호출 또는 고비용 외부 작업을 줄이는 경우에만 추가 shortcut을 검토한다.

---

## 2026-09-20 과설계 정정

Phase 14~18에서 시도한 deterministic durable-state collapse는 현재 구현에서 철회됐다.

이유:

```text
실제 LLM/worker 호출 감소 없음
로컬 transition 자체 비용은 작음
resume/debug boundary를 잃는 대신 preview/fallback 분기가 증가
virtual source-state와 inline routing이 코드 복잡도를 증가
전용 테스트 표면이 과도하게 확대
```

현재 유지하는 최적화 기준은 Phase 11~13처럼 실제 agent 작업을 줄이는 경우다.

```text
safe plan -> plan reviewer 생략 가능
safe verified PASS -> review chain 생략 가능
clean code review -> cross confirmer 생략 가능
```

과설계 제거 후 검증:

```text
focused Master routing: 13 passed, 29 deselected, 9 subtests passed
related regression: 94 passed, 4 warnings, 35 subtests passed
full repository: 125 passed, 8 warnings, 35 subtests passed
```

향후에는 durable state 개수를 줄이는 것 자체를 목표로 하지 않는다. 실제 worker/provider 호출 또는 고비용 외부 작업을 줄이는 경우에만 추가 shortcut을 검토한다.
