# Orca Loop 실행 규칙 및 주의사항

**Date:** 2026-08-04 (2026-08-11 개정)
**Applies to:** `C:\Users\mhj\Desktop\mhj_workspace\orca_harness`를 사용하는 후속 개발 세션
**Source:** `doc/codex-mhj_26_08_04_1_orca_loop_failure_session_log.md`

---

## 0. 2026-08-11 개정 — 아래 규칙 중 코드로 대체된 항목

`docs/claude-mhj_26_08_11_04_phase4-implementation-report.md`의 구현으로 다음 규칙은 **더 이상 사람이 지킬 필요가 없다.** 나머지 절은 유효하다.

| 기존 규칙 | 현재 |
| --- | --- |
| §3 "model 별칭을 임의로 쓰지 않는다" | 카탈로그 정규화가 흡수한다(`sonnet5`→`sonnet`, `terra`→`gpt-5.6-terra`, `mid`→`medium`). 정확한 값만 허용하려면 `--strict-agent-runtime` |
| §3 "resume으로 configuration을 바꾸지 않는다" | 여전히 유효. 단 **동일 값의 다른 표기**는 이제 drift로 오판하지 않는다 |
| §2 permission report 수동 탐색 | `start`가 자동 탐색·검증한다. 조건은 동일하며 약화되지 않았다 |
| §4 runner terminal 수동 생성 | `start`가 생성한다. `--no-create-terminals`로만 수동 지정 |
| §9.1 "stderr가 비어 있어도…" | `logs/step-*.stdout.log`, `*.stderr.log`, `*.runner.json`에 항상 남는다 |
| §9.5 "IN_PROGRESS라도 process가 없으면…" | `run_loop.py status --run-id <id>`가 판정과 재시작 명령을 함께 출력한다 |
| §11 "FAILED run을 resume하지 않는다" | 코드가 거부한다 |
| §13 "claude provider로 implementer를 쓰면 BLOCKED" | **해소.** 2026-08-11 spike가 `V-PERM-06`(claude implementer 쓰기)을 확보했다 |
| permission report를 Orca 버전에 고정 | **환경 지문 기준으로 전환.** `orca_version`은 정보성이고, 실제 판정은 `claude`/`codex` CLI의 major.minor, `readonly.py`+`profiles.py` 다이제스트, platform으로 한다 |

**재spike가 필요한 시점** (Orca 업데이트는 더 이상 해당하지 않는다):

- `claude` 또는 `codex` CLI의 **minor 버전**이 올라갔을 때 (patch는 무관)
- `orca_loop/readonly.py` 또는 `orca_loop/profiles.py`를 수정했을 때
- OS(platform)가 바뀌었을 때

`py -3 run_loop.py doctor`가 해당 사유를 `permission_reports[].problem`에 그대로 출력한다.

새 절차:

```text
py -3 run_loop.py doctor            # 환경·카탈로그·permission report·stale lock 진단
py -3 run_loop.py start  ... --dry-run
py -3 run_loop.py start  ...
py -3 run_loop.py status --run-id <id>
py -3 run_loop.py resume --run-id <id> [--accept-worktree-drift]
```

재시작 시 주의:

- 죽은 coordinator가 남긴 lock과 terminal은 자동 회수·재생성된다.
- in-flight step은 폐기 후 재실행되며, 입출력·로그는 `steps/<id>/ABANDONED`와 함께 보존된다.
- `IMPLEMENT`/`FIX` 중단 후 worktree가 변경됐으면 exit 3으로 멈춘다. `reports/99-failure.md`와 diff를 먼저 사용자에게 설명하고, 승인 후에만 `--accept-worktree-drift`를 붙인다. **승인 없이 파일을 삭제·복원하지 않는다.**

---

## 1. 목적

이 문서는 다음 세션에서 Orca Loop를 새로 시작하거나 중단된 run을 처리할 때 따라야 할 운영 규칙을 정의한다. 모델 식별자 오류, worker 역할 오인, gate JSON 손상, provenance 불일치, FIX 단계 scope 충돌을 반복하지 않는 것이 목적이다.

## 2. 실행 전 필수 확인

새 run을 만들기 전에 다음 조건을 모두 확인한다.

1. 사용자가 **전체 task prompt**를 제공했는지 확인한다. 파일을 prompt로 지정했다면 전체 byte를 request copy로 사용한다.
2. 사용자가 네 worker의 **model과 effort를 각각 명시적으로 지정**했는지 확인한다.
3. target repository, branch, current HEAD, `git status --short`를 확인한다.
4. tracked/untracked 변경이 하나라도 있으면 먼저 diff를 읽는다. 사용자 승인 없이 `restore`, `reset`, `clean`, 삭제 또는 덮어쓰기를 하지 않는다.
5. 부분 구현을 이어갈 경우 clean checkpoint를 먼저 만든다. 기존 신규 파일이 남은 dirty worktree에서 새 run을 시작하면 plan의 `add`와 후속 FIX의 `modify` 판정이 충돌할 수 있다.
6. permission feasibility report가 현재 Orca runtime과 호환되고 `PASS`인지 확인한다.
7. test policy를 harness parser로 검증하고 digest를 기록한다.
8. baseline test를 실제로 실행한다. 실행하지 않았다면 `NOT RUN`으로 기록한다.
9. prompt source와 request copy의 SHA-256이 동일한지 확인한다.
10. 실제 launch 전에 동일 인자로 dry-run을 수행하고 resolved runtime을 확인한다.

## 3. Worker 역할과 model 규칙

역할은 이름으로 추측하지 않고 `orca_loop/profiles.py`의 고정 매핑을 따른다.

| 단계 | worker key |
| --- | --- |
| Planner | `claude_planner` |
| Plan Reviewer | `codex_review` |
| Implementer / Fixer | `codex_implementer` |
| Code Reviewer | `claude_code_review` |
| Cross Confirmer | `codex_review` |

주의사항:

- `claude_code_review`는 plan reviewer가 아니다.
- 이 환경에서 검증된 model 식별자는 Claude의 `sonnet`, Codex의 `gpt-5.6-terra`이다.
- `sonnet5`, `terra` 같은 별칭을 임의로 사용하지 않는다. 이전 run에서 worker exit code 1을 발생시켰다.
- user가 model 또는 effort 변경을 요청하면 **resume으로 configuration을 바꾸지 않는다**. resume은 최초 run과 동일 configuration만 사용한다. 설정을 바꾸려면 새 run을 만든다.
- 현재 사용자가 마지막으로 승인한 조합은 다음과 같다.
  - `claude_planner=sonnet`, `effort=medium`
  - `claude_code_review=sonnet`, `effort=medium`
  - `codex_implementer=gpt-5.6-terra`, `effort=medium`
  - `codex_review=gpt-5.6-terra`, `effort=medium`
- 후속 세션에서도 위 값을 자동 가정하지 말고 사용자에게 다시 명시적으로 확인받는다.

## 4. Runner와 orchestration 규칙

1. run마다 target worktree에 전용 runner terminal을 새로 만든다.
2. 해당 runner terminal 안에서 orchestration Run을 생성한다.
3. orchestration Run의 `coordinator_handle`과 harness의 `--coordinator-handle`이 같은 terminal인지 확인한다.
4. gate 조회와 gate resolve는 반드시 동일 runner terminal에서 수행한다.
5. step 및 total timeout은 dry-run, 실제 launch, resume에서 모두 동일하게 유지한다.

권장 timeout:

```text
--step-timeout-ms 3600000
--total-timeout-ms 14400000
```

60초 terminal read/wait timeout은 monitoring 주기일 뿐 worker 실패가 아니다. worker의 절대 제한은 `--step-timeout-ms`다.

## 5. Prompt 및 artifact 계약

### 5.1 PlanDocument

- planner는 Markdown이나 설명 없이 전체 `PlanDocument` JSON object 하나만 반환해야 한다.
- root field를 누락하거나 acceptance criterion 일부만 반환하면 안 된다.
- revision은 직전 valid plan의 root field를 보존하고 `plan_version`을 정확히 1 증가시킨다.
- `request_digest`, `test_policy_digest`, `snapshot_digest`, consensus round는 현재 staged input 값을 그대로 사용한다.
- provenance는 과거 run, 과거 gate 또는 사용자 설명에서 추측하지 않는다.

### 5.2 Delivered finding 규칙

plan revision에서는 **현재 revision `contract.md`의 `Delivered finding IDs`만** 사용한다.

- 값이 `[]`이면 `reviewed_finding_ids=[]`, `finding_decisions=[]`이어야 한다.
- 과거 review의 finding ID 또는 `HumanDecision.affected_finding_ids`를 자동 계승하지 않는다.
- 값이 non-empty이면 해당 ID 집합과 `reviewed_finding_ids`가 정확히 일치해야 하며 중복·누락·추가 ID가 없어야 한다.

### 5.3 ReviewArtifact

반환 전에 다음 wire value를 대조한다.

- `severity`: `P0`, `P1`, `P2`
- `blocking_reason`: `B1`~`B5`
- `impact_class`: `none`, `architecture`, `requirement_interpretation`, `db_schema`, `external_api`, `security_auth`
- `required_fix`와 `required_change`: 정확히 하나만 nonempty
- `reopens`: string 또는 `null`
- `line`: 양의 integer 또는 `null`

### 5.4 ImplementationArtifact

- `changed_files`에 실제 변경 파일을 빠짐없이 기록한다.
- coordinator가 실행하지 않은 test를 `PASS`로 기록하지 않는다.
- 승인된 plan scope 밖의 파일을 수정하지 않는다.

## 6. User decision gate 처리

gate가 생성되면 임의 승인·종료·resume하지 않는다.

1. 동일 runner에서 `orca orchestration gate-list --json`으로 pending gate를 확인한다.
2. `user-decision.md`, review artifact, state history를 읽는다.
3. 사용자에게 다음 내용을 먼저 설명한다.
   - 실제 결정 대상
   - 근거와 영향 범위
   - 각 option의 의미
   - 권장 option과 이유
4. 사용자의 명시적 답변을 받은 뒤에만 resolve한다.
5. resolve 결과의 `status=resolved`와 resolution 원문을 확인한다.
6. 이후 최초 run과 동일한 model, effort, timeout, request, policy로 resume한다.

option 의미를 단계에 맞게 설명한다.

- plan 단계의 `merge`: Git merge가 아니라 plan 승인 및 구현 단계 진입이다.
- `revise_design`: plan revision으로 돌아간다.
- `revise_code`: 구현 결과 수정으로 돌아간다.
- `reject`: run을 종료한다.

## 7. HumanDecision JSON 전달 규칙

`revise_design`과 `revise_code`는 다음 전체 JSON을 요구한다.

```json
{
  "decision": "revise_design",
  "decision_note": "User-approved bounded revision description.",
  "affected_acceptance_criteria": [],
  "affected_finding_ids": [],
  "report_digest": "sha256:<current-report-digest>"
}
```

PowerShell 변수의 JSON 문자열을 native executable 인자로 직접 넘기지 않는다. 내부 double quote가 제거되어 `malformed JSON`이 발생할 수 있다.

안전한 전달 방법:

1. JSON을 UTF-8로 serialize한다.
2. 필요하면 Base64로 encode한다.
3. runner terminal에서 Python을 실행한다.
4. Python `subprocess.run([...])`의 argv element로 `--resolution` 값을 전달한다.
5. gate-list 결과에서 resolution의 escaped double quote가 보존됐는지 확인한다.

`merge`와 `reject`처럼 단순 option은 plain text resolution으로 전달할 수 있다.

## 8. Monitoring 및 상태 판정

monitoring은 cursor 기반으로 수행한다.

1. 마지막 `nextCursor`를 보존하고 그 이후 출력만 읽는다.
2. `claude -p`와 `codex exec`는 완료 전 stdout이 비어 있을 수 있다.
3. 무출력만으로 정지나 실패를 판단하지 않는다.
4. 다음을 함께 확인한다.
   - runner terminal cursor와 최종 line
   - `run_loop.py` process 존재 여부
   - active worker process와 실제 command line
   - 최신 `control/state.<generation>.json`
   - 최신 step output artifact와 timestamp
5. state가 `IN_PROGRESS`여도 runner process가 없고 terminal에 error가 있으면 실제 run은 종료된 것이다.

상태 보고는 다음 기준을 사용한다.

- `PASS`: command가 성공하고 결과를 검증함
- `FAIL`: command 또는 gate/artifact validation이 실패함
- `BLOCKED`: credential, environment, permission, user decision 때문에 실행 불가
- `NOT RUN`: 실행하지 않음

## 9. 실패 후 복구 규칙

### 9.1 `agent exited 1`

- 해당 step binding에서 실제 provider, model, effort command line을 확인한다.
- model 별칭 오류인지 먼저 확인한다.
- stderr가 비어 있어도 artifact 미생성과 exit code를 근거로 실패를 기록한다.
- configuration 변경이 필요하면 기존 run을 resume하지 않고 새 run을 만든다.

### 9.2 `malformed JSON`

- gate resolution의 quote 손상을 확인한다.
- user의 기존 승인을 보존한 채 Python argv 방식으로 동일 gate를 교정한다.
- 교정된 JSON과 current report digest가 일치하는지 확인한 뒤 resume한다.

### 9.3 Finding provenance 불일치

- 현재 step의 `contract.md`에서 `Delivered finding IDs`를 다시 읽는다.
- plan output의 `reviewed_finding_ids`와 `finding_decisions`를 비교한다.
- 과거 gate finding을 자동 이월하지 않는다.
- state가 `FAILED`가 되면 resume하지 않는다.

### 9.4 FIX 단계 `add`/`modify` scope violation

대표 오류:

```text
scope_violation:<path>:modified file is not in approved plan scope
```

원인:

- IMPLEMENT에서 plan operation `add`로 생성한 파일이 FIX 단계에서는 이미 존재한다.
- validator가 이를 `modified`로 분류하면서 기존 plan의 `add`와 충돌할 수 있다.

복구:

1. 변경 파일을 삭제하거나 restore하지 않는다.
2. implementation/fix artifact와 diff를 보존한다.
3. user에게 현재 변경, test 상태, checkpoint 필요성을 설명한다.
4. 새 run이 필요하면 사용자 승인 하에 clean checkpoint를 만든다.
5. 새 planner가 기존 파일을 `modify`로 계획하도록 현재 repository 상태를 정확히 읽게 한다.
6. harness validator를 수정할 수 있다면 FIX scope 검증이 original snapshot 대비 operation을 사용하도록 별도 수정한다.

### 9.5 `FAILED`와 잔류 `IN_PROGRESS`

- state가 `FAILED`인 run은 resume하지 않는다.
- state가 `IN_PROGRESS`라도 runner process가 종료됐으면 정상 실행으로 간주하지 않는다.
- terminal error, process table, state history를 함께 근거로 새 run 또는 직접 복구를 결정한다.

## 10. Test 실행 규칙

1. test policy의 exact allowlist command만 coordinator가 실행한다.
2. 같은 worktree/build directory에서 `gradlew test`와 `gradlew build`를 병렬 실행하지 않는다.
3. Gradle command는 다음 순서로 실행한다.

```text
targeted tests
gradlew test
gradlew build
mysqlIntegrationTest
browser manual verification
```

4. 병렬 Gradle 실행으로 `build/test-results/test/binary/output.bin` 점유 오류가 발생하면 코드 실패로 분류하지 않는다. process 종료 후 순차 재실행한다.
5. `TEST_DATASOURCE_*`가 없으면 `mysqlIntegrationTest`는 `BLOCKED`로 기록한다.
6. `SPRING_DATASOURCE_*`와 실행 가능한 DB가 없으면 browser verification과 screenshot은 `BLOCKED`로 기록한다.
7. 실행하지 않은 browser 검증이나 integration test를 `PASS`로 기록하지 않는다.

## 11. 금지 사항

- 사용자 설명 없이 decision gate를 자동 resolve하지 않는다.
- `FAILED` run을 resume하지 않는다.
- resume에서 model, effort, timeout, request 또는 policy를 바꾸지 않는다.
- worker 이름만 보고 역할을 추측하지 않는다.
- PowerShell native argument로 structured JSON을 직접 전달하지 않는다.
- 과거 finding ID를 현재 revision에 자동 이월하지 않는다.
- dirty worktree를 확인하지 않고 새 run을 시작하지 않는다.
- scope violation을 해결하려고 승인 없이 신규 파일을 삭제하거나 기존 변경을 restore하지 않는다.
- 같은 build directory를 사용하는 Gradle test/build를 병렬 실행하지 않는다.
- runner state 하나만 보고 실행 생존 여부를 판단하지 않는다.

## 12. 새 run 체크리스트

다음 항목을 모두 충족한 뒤 실제 launch한다.

```text
[ ] 전체 prompt 확보
[ ] 네 worker model/effort 사용자 명시 확인
[ ] target repository와 branch 확인
[ ] git status와 diff 확인
[ ] clean worktree 또는 사용자 승인 checkpoint 확인
[ ] permission feasibility PASS 확인
[ ] test policy parse와 digest 확인
[ ] baseline test 결과 기록
[ ] prompt/request SHA-256 동일 확인
[ ] 전용 runner terminal 생성
[ ] orchestration Run 생성 및 coordinator handle 일치 확인
[ ] step/total timeout 명시
[ ] dry-run PASS 및 resolved runtime 확인
[ ] 실제 launch
[ ] cursor, process, state, artifact를 함께 monitoring
[ ] gate 발생 시 사용자에게 결정 내용을 먼저 설명
[ ] test는 동일 build directory에서 순차 실행
```

## 13. 2026-08-05 실패 기록 — codex_implementer/codex_review provider를 claude로 바꿔 dry-run BLOCKED

### 13.1 무엇을 시도했는가

`claude-mhj_26_08_05_1_slide35-map-layout` run 준비 중, 사용자가 Gate 2에서 "모든 모델을 sonnet/medium으로, provider도 claude로 바꿔서"를 요청했다. 그 결과 다음 조합으로 launch 인자를 구성했다.

```text
--agent-model claude_planner=sonnet          --agent-effort claude_planner=medium
--agent-model claude_code_review=sonnet      --agent-effort claude_code_review=medium
--agent-provider codex_implementer=claude    --agent-model codex_implementer=sonnet   --agent-effort codex_implementer=medium
--agent-provider codex_review=claude         --agent-model codex_review=sonnet        --agent-effort codex_review=medium
```

즉 `codex_implementer`·`codex_review`의 **provider까지 기본값(codex)에서 claude로 override**했다.

### 13.2 실제 결과

`--dry-run`이 `exit code 2`, `status=BLOCKED`로 즉시 종료됐다.

```json
{"error": "permission report does not prove claude writable capability required by codex_implementer; pass V-PERM-06 before worker provisioning", "status": "BLOCKED"}
```

### 13.3 근본 원인

가장 최신 permission-feasibility report(`runs\20260803-permission-spike-01\control\permission-feasibility.json`, `status=PASS`, `strategy=D`, `orca_version=1.4.164`, 현재 runtime과 일치)를 점검한 결과, 이 report는 `V-PERM-01`~`V-PERM-05` 5개 check만 포함한다.

| check | 검증 대상 | 사용된 provider/model |
| --- | --- | --- |
| `V-PERM-01`~`V-PERM-03` | `claude_planner`, `claude_code_review`의 쓰기 차단(원본 파일 보호) | `claude -p --model claude-sonnet-5 --effort low` |
| `V-PERM-04` | `codex_review`의 쓰기 차단 | `codex exec --model gpt-5.6-luna --config model_reasoning_effort=low` |
| `V-PERM-05` | **approved target에 대한 implementer 쓰기 성공** | `codex exec --model gpt-5.6-luna ...` |

`V-PERM-05`의 evidence를 직접 확인하면 `codex exec`로 구현자 쓰기 권한을 검증했다는 것이 명시되어 있다 — 즉 이 report가 증명하는 것은 **"codex provider로 구동되는 codex_implementer"의 쓰기 가능성**이지, "claude provider로 구동되는 codex_implementer"가 아니다.

`codex_implementer`/`codex_review`의 provider를 `claude`로 override하면, harness는 이 override된 조합(= claude provider가 실제로 codex_implementer 역할의 쓰기 권한을 가지는지)을 증명하는 **별도 check(`V-PERM-06`)** 를 요구한다. 이 run 준비 시점에 존재하는 모든 permission-feasibility report(`20260731-permission-spike-01/02/03`, `20260803-permission-spike-01` 4건 전부)를 `V-PERM-06` 키워드로 재확인했으나 어디에도 없었다.

```text
20260803-permission-spike-01/control/permission-feasibility.json   → V-PERM-06 없음
20260731-permission-spike-03/control/permission-feasibility.json   → V-PERM-06 없음
20260731-permission-spike-02/control/permission-feasibility.json   → V-PERM-06 없음
20260731-permission-spike-01/control/permission-feasibility.json   → V-PERM-06 없음
```

### 13.4 이 문서의 §3과의 정합성

이 실패는 사실 §3(`Worker 역할과 model 규칙`)에 이미 기록되어 있던 제약과 정확히 일치한다.

> "이 환경에서 검증된 model 식별자는 Claude의 `sonnet`, Codex의 `gpt-5.6-terra`이다."
> "현재 사용자가 마지막으로 승인한 조합: `codex_implementer=gpt-5.6-terra`, `codex_review=gpt-5.6-terra`"

즉 `codex_implementer`/`codex_review`는 **codex provider 유지, model은 `gpt-5.6-terra`** 조합만 이 harness 인스턴스에서 permission 검증을 통과한 상태다. `provider=claude`로의 전환은 이 문서가 예상하지 못한 새 조합이 아니라, **§3이 "자동 가정하지 말라"고 명시한 바로 그 조합 변경 요청**이었고, permission report가 그 변경을 커버하지 못해 harness가 정상적으로 차단한 것이다.

### 13.5 복구 옵션 (미결정 — 사용자 확인 필요)

1. **`codex_implementer`/`codex_review`는 provider를 codex로 되돌리고 model만 `gpt-5.6-terra`를 쓴다.** `claude_planner`/`claude_code_review`만 `sonnet`/`medium`으로 유지. 기존 검증된 조합이므로 즉시 진행 가능.
2. **새 permission-feasibility spike를 먼저 실행해 `V-PERM-06`(claude provider로 구동되는 codex_implementer/codex_review의 쓰기 권한)을 검증한다.** 이후에만 이번 조합으로 launch 가능. 별도 준비 작업 필요.
3. 사용자가 다른 model/provider 조합을 다시 지정한다.

### 13.6 이번 실패에서 추가로 확인한 사실

- `dry-run`은 이 BLOCKED 판정을 **worker를 하나도 기동하지 않고** 즉시 반환했다 — permission report 대조가 worker provisioning보다 먼저 수행된다는 뜻이다. 따라서 이 실패는 `agent exited 1`(§9.1)이나 model 별칭 오류가 아니라 **permission report와 요청된 provider/model 조합의 불일치**로 명확히 분류해야 한다.
- request 파일(`claude-mhj_26_08_05_1_request.md`)과 runner terminal(`term_bf27e094-da22-4b54-b9ef-ca3066e4df86`)은 이미 생성된 상태이며, 이번 실패로 무효화되지 않는다. 동일 run-id로 provider/model만 바꿔 dry-run을 재시도할 수 있다.
