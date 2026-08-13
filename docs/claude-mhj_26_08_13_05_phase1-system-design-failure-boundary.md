# Task Report: Orca Loop Failure Boundary

**Current Phase:** 1. System Design
**Status:** Waiting for Explicit User Approval

**Baseline:** `claude-mhj_26_08_13_04_failure-mode-remediation-plan.md` (`R-1`~`R-4`)
**대상 커밋:** `329aaec`

---

## 1. Context & Objective

- **Problem:** 루프가 멈추는 방식이 세 갈래로 흩어져 있다. 25개 예외 클래스가
  최상위 핸들러를 빠져나가고(`F-1`), 잡힌 예외도 durable 상태를 바꾸지 않으며(`F-2`),
  `operational_retry_limit`은 `ContractViolationError`에서만 도달 가능하다(`F-4`).
  그 결과 "왜 멈췄는가"를 코드가 아니라 사람이 판정해야 한다.
- **Goal:** 정지 사유를 durable 증거로 남기고, 실패를 재개 가능/불가능으로 분류하며,
  일시적 오류를 재시도 경로에 연결한다.
- **Scope:** `run_loop.py`, `orca_loop/session.py`(재사용), `orca_loop/config.py`,
  `tests/*`.
- **Out of Scope:** `R-7` 타임아웃 회계(별도 판단), 새 production dependency,
  worker 역할·consensus 정책 변경, `R-5`/`R-6`(완료됨).

---

## 2. Current-State Analysis

### 2.1 종료 경로

| 위치 | 조건 | durable 효과 |
| --- | --- | --- |
| `run_loop.py:1595` | 총 타임아웃 | `FAILED` 커밋 ✅ |
| `run_loop.py:1698` | 전이 상한 | `FAILED` 커밋 ✅ |
| `run_loop.py:2310` | `ConfigurationError` 등 4종 | 없음 |
| `run_loop.py:2325` | `ResumeBlockedError` | 실패 보고서만 |
| `run_loop.py:2336` | `OrcaLoopError` 등 6종 | 실패 보고서만 |
| `run_loop.py:2354` | `KeyboardInterrupt` | 실패 보고서만 |
| **없음** | 나머지 25개 클래스 | **traceback, 증거 없음** |

`_report_failure()`는 `render_failure_report()`만 호출하고 그것은 마크다운을 쓴다
(`reporting.py:380`). durable state는 어느 경로에서도 갱신되지 않는다.

### 2.2 컨트롤러 수명

```python
def run_coordinator(preflight, client) -> CoordinatorState:
    controller, pool = _resume(...) if resume else _initialize(...)
    return _run_loop(controller, pool, preflight, client)
```

`GenerationController`는 `run_coordinator` 안에서만 존재한다. `main()`에는 없다.
따라서 **도메인 상태 전이는 `main()`에서 수행할 수 없다.** 이것이 경계를 두 층으로
나눠야 하는 구조적 이유다.

### 2.3 복구 경로 자체의 취약성 (설계 제약)

```python
if state.generation != current_generation + 1:
    raise GenerationMismatchError(...)      # generation.py:243
```

`commit_generation`은 generation 연속성을 강제한다. 즉 **`FAILED` 커밋 시도가
`GenerationMismatchError`로 다시 실패할 수 있다.** 스텝 커밋 도중 터진 예외라면
특히 그렇다.

→ **증거 기록이 상태 전이보다 반드시 먼저 일어나야 한다.**

### 2.4 재사용 가능한 선례

`orca_loop/session.py:44`:

```python
def append_event(control_dir: Path, kind: str, detail: Mapping[str, object]) -> None:
    """Append one resume event. Never raises: this is evidence, not control."""
```

`control/resume-events.jsonl`에 append하며 **예외를 던지지 않는다.** 실패 경로에서
필요한 의미론과 정확히 일치한다.

> **베이스라인 대비 변경:** `R-1`은 원래 `control/stop.json` 신규 파일을 제안했으나,
> 본 설계는 `append_event` 재사용을 권장한다. 신규 스키마·파서·마이그레이션이
> 불필요하고, "증거는 제어를 막지 않는다"는 계약이 이미 검증돼 있다.
> `Open Question OQ-3` 참조.

---

## 3. Goals / Non-Goals

### Goals

- **G-1** 모든 종료 경로가 durable 증거를 남긴다.
- **G-2** 실패가 `terminal`(재실행해도 반복) / `interrupted`(재개 가능)로 분류된다.
- **G-3** `terminal`만 도메인 상태를 `FAILED`로 전이한다.
- **G-4** `status`가 사람 판정 없이 "중단됨 / 진행 중 / 재개 가능"을 단정한다.
- **G-5** 일시적 오류가 `operational_retry_limit`에 연결된다.
- **G-6** 증거 기록 실패가 원래 예외를 가리지 않는다.

### Non-Goals

- **NG-1** 자동 재개. 분류는 판정일 뿐 재개를 실행하지 않는다.
- **NG-2** 예외 없는 루프. 분류 불가 예외는 보수적으로 `interrupted`로 남긴다.
- **NG-3** 타임아웃 의미 변경(`R-7`).

---

## 4. Proposed Architecture

### 4.1 2층 경계

```
main()
 └─ [Layer 2] 포괄 경계 — controller 없음
      · 증거 기록(가능하면) + 실패 보고서 + 종료 코드
      · 상태 전이 없음
      └─ run_coordinator()
           └─ [Layer 1] 분류 경계 — controller 있음
                · 분류 → 증거 기록 → (terminal이면) FAILED 커밋
                └─ _run_loop()
                     └─ [Layer 0] 재시도 경계 (기존 확장)
                          · retryable → operational_retry_result
```

| 층 | 위치 | 가진 것 | 할 수 있는 것 |
| --- | --- | --- | --- |
| 0 | `_run_loop` except | controller, ledger | 재시도, 에스컬레이션 |
| 1 | `run_coordinator` | controller | 증거 + 상태 전이 |
| 2 | `main` | arguments만 | 증거 + 보고서 + 종료 코드 |

Layer 0은 이미 존재한다(`ContractViolationError`만). Layer 1은 신규. Layer 2는
기존 핸들러 확장.

### 4.2 분류

```python
class StopClass(StrEnum):
    TERMINAL = "TERMINAL"          # 재실행해도 반복되는 계약·무결성 위반
    INTERRUPTED = "INTERRUPTED"    # 외부·일시·환경
    RETRYABLE = "RETRYABLE"        # INTERRUPTED 중 in-process 재시도가 안전한 것
```

`RETRYABLE ⊂ INTERRUPTED`. Layer 0은 `RETRYABLE`만 소비하고, 나머지는 Layer 1로
올라간다.

### 4.3 분류표 (25개 + 기존 11개)

| 예외 | 분류 | 근거 |
| --- | --- | --- |
| `ContractViolationError`, `ProvenanceError` | `RETRYABLE` | 워커 산출물 스키마 위반. 재프롬프트로 회복 가능 (기존 동작 보존) |
| `TransportProvenanceError` | `TERMINAL` | task/dispatch ID 불일치. 재시도로 바뀌지 않음 |
| `ScopeViolationError`, `TransportPathBoundaryError` | `TERMINAL` | 경계 위반. 반복됨 |
| `InputStagingError` | `TERMINAL` | 스테이징 계약 위반 |
| `GuardScopeViolationError`, `GuardPathBoundaryError` | `TERMINAL` | 가드 위반 |
| `LedgerIntegrityError`, `InvalidRoundError` | `TERMINAL` | 원장 무결성 |
| `TemplateContractError` | `TERMINAL` | 템플릿 계약. 코드 수정 필요 |
| `PathBoundaryError`, `WorkspaceError` | `TERMINAL` | 경로 경계 |
| `LaunchProfileError` | `TERMINAL` | 권한 보고서 불일치 |
| `GenerationMismatchError` | `TERMINAL` | 세대 불연속. 재개해도 반복 |
| `TestPolicyError` | `TERMINAL` | 정책 계약 위반 |
| `DecisionReportError` | `INTERRUPTED` | 대부분 파일시스템 I/O |
| `NoticeDeliveryError` | `INTERRUPTED` | 알림 증거 쓰기 실패 |
| `GenerationError`(기타) | `INTERRUPTED` | I/O 계열 |
| `AtomicWriteError` | `INTERRUPTED` | 디스크 I/O |
| `SnapshotChangedError` | `INTERRUPTED` | 사람이 worktree를 만짐. `--accept-worktree-drift`로 재개 |
| `SnapshotError`, `GitCommandError`, `SnapshotPathBoundaryError` | `INTERRUPTED` | 외부 git |
| `TestExecutionError` | `INTERRUPTED` | 외부 프로세스 |
| `ReadOnlyMirrorError` | `INTERRUPTED` | 파일시스템 |
| `OrcaTimeoutError` | `RETRYABLE` | 일시적. **단, 뮤테이션은 §4.5 참조** |
| `OrcaCommandError`, `OrcaProtocolError` | `INTERRUPTED` | Orca 런타임 |
| `DispatchTimeoutError`, `WorkerLostError` | `INTERRUPTED` | 워커 상실. 펜싱 후 재개 |
| `WorkerProvisionError`, `StepBindingError` | `INTERRUPTED` | 프로비저닝 |
| `DispatchProvenanceError` | `TERMINAL` | provenance 불일치 |
| `GateProtocolError` | `TERMINAL` | 게이트 프로토콜 위반 (`R-5` 이후 모호성만 남음) |
| `RunLockError` | `INTERRUPTED` | 락 경합 |
| `OSError` | `INTERRUPTED` | 환경 |
| **미분류 예외** | `INTERRUPTED` | **보수적 기본값** (`NG-2`) |

`ConfigurationError`, `PreflightError`, `RunWorkspaceExistsError`,
`ManifestError`, `ResumeBlockedError`는 run 시작 전 또는 재개 판정이므로 현행
`EXIT_PREFLIGHT`/`EXIT_USER_REQUIRED` 동작을 유지하며 분류 대상이 아니다.

### 4.4 미분류 기본값이 `INTERRUPTED`인 이유

`TERMINAL`은 `FAILED`를 커밋한다. 오분류 비용이 비대칭이다.

- `INTERRUPTED`를 `TERMINAL`로 잘못 보면 → **재개 가능한 run을 죽인다** (회복 불가)
- `TERMINAL`을 `INTERRUPTED`로 잘못 보면 → 사람이 한 번 더 재개를 시도한다 (회복 가능)

따라서 확신이 없으면 `INTERRUPTED`다.

### 4.5 뮤테이션 재시도 금지

`execute_mutation`은 INTENT를 디스크에 쓰고 동일 `--retry-request` argv로 replay한다
(`orca_client.py:212-247`). in-process 재시도가 이 저널을 우회하지는 않지만,
**타임아웃된 뮤테이션은 효과가 불명이므로** 재시도 대상에서 제외한다.

규칙: `OrcaTimeoutError`는 **읽기 계열 호출에서만** `RETRYABLE`이다. 뮤테이션
경로에서 발생하면 `INTERRUPTED`로 승격한다. 판별은 예외 타입이 아니라 호출 지점이
결정하므로, Layer 0은 `_execute_worker` 바깥에서 발생한 것만 재시도한다.

---

## 5. End-to-End Data Flow

```
예외 발생
   |
[Layer 0] _run_loop except
   | classify(exc) is RETRYABLE and 뮤테이션 경로 밖?
   |   yes -> operational_retry_result -> commit -> 루프 계속
   |          (한도 도달 시 USER_DECISION_REQUIRED)
   |   no  -> 전파
   v
[Layer 1] run_coordinator except
   | 1. classify(exc)
   | 2. append_event(control_dir, "stopped", {...})     <- 절대 raise 안 함
   | 3. TERMINAL이면:
   |       try: controller.commit(state=FAILED, signal=ABORT)
   |       except GenerationError: 증거에 commit_failed 추가 후 계속
   |    return controller.state                          <- 정상 반환
   | 4. INTERRUPTED이면: 전파
   v
[Layer 2] main except
   | 1. append_event 시도 (arguments가 있으면)
   | 2. _report_failure(...)
   | 3. JSON 오류 출력 + EXIT_RUNTIME_FAILURE
   v
status --json
   | 최신 "stopped" 이벤트 + state.generation + 락 생존
   -> verdict: RUNNING | STOPPED_RESUMABLE | STOPPED_TERMINAL | COMPLETED
```

---

## 6. Input / Output Contracts

### 6.1 정지 이벤트

`append_event(control_dir, "stopped", detail)`의 `detail`:

```python
{
    "classification": "TERMINAL" | "INTERRUPTED",
    "exception": str,          # 예외 클래스명
    "reason": str,             # str(exc), 2000자 상한
    "generation": int,         # 발생 시점 state.generation
    "state": str,              # LoopState 값
    "resumable": bool,         # INTERRUPTED == True
    "state_committed": bool,   # FAILED 커밋 성공 여부
}
```

`recorded_at`과 `kind`는 `append_event`가 붙인다.

### 6.2 `status` 판정

```python
{
    "stop": {                       # 최신 stopped 이벤트가 현 generation일 때만
        "classification": str,
        "exception": str,
        "reason": str,
        "resumable": bool,
        "recorded_at": str,
    },
    "verdict": "RUNNING" | "STOPPED_RESUMABLE" | "STOPPED_TERMINAL"
             | "BLOCKED_ON_USER" | "COMPLETED",
}
```

**staleness 규칙:** 정지 이벤트는 `event.generation == state.generation`일 때만
현재 상태로 인정한다. 재개가 generation을 전진시키면 과거 정지 기록은 자동으로
무효가 된다. 별도 삭제가 필요 없다.

**verdict 판정:**

| 조건 | verdict |
| --- | --- |
| `state ∈ {READY_FOR_MERGE, REJECTED}` | `COMPLETED` |
| `state ∈ {HUMAN_GATE, USER_DECISION_REQUIRED}` | `BLOCKED_ON_USER` |
| `state is FAILED` | `STOPPED_TERMINAL` |
| 현 generation 정지 이벤트 존재 + `resumable` | `STOPPED_RESUMABLE` |
| 락이 살아 있음 | `RUNNING` |
| 그 외 (`IN_PROGRESS` + 락 없음) | `STOPPED_RESUMABLE` |

마지막 행이 `F-2`의 핵심 공백을 메운다. 현재는 이 조합에서 `status`가 아무것도
말하지 않는다.

### 6.3 `--force-fail` (`R-3`)

```
run_loop.py force-fail --run-id <id> --reason <text>
```

`IN_PROGRESS` run을 운영자 의도로 종료한다. 락이 살아 있으면 거부한다.
`FAILED` 커밋 + `kind="force_failed"` 이벤트.

---

## 7. State and Side Effects

| 대상 | 변경 |
| --- | --- |
| `control/resume-events.jsonl` | `stopped`, `force_failed` 이벤트 append |
| `state.<n>.json` | `TERMINAL`일 때만 `FAILED` 커밋 |
| `runs/<id>/*.md` | 기존 실패 보고서 유지 |
| `CoordinatorState` 스키마 | **변경 없음** |
| 종료 코드 | **변경 없음** |

durable 스키마 신규 파일 없음. `CoordinatorState` 무변경.

---

## 8. Error and Exception Strategy

| 상황 | 처리 |
| --- | --- |
| 증거 기록 실패 | `append_event`가 삼킨다. 원래 예외를 가리지 않는다 (`G-6`) |
| `FAILED` 커밋이 `GenerationError` | 포착 → `state_committed=false` 기록 → 전파하지 않음 |
| Layer 1에서 분류 중 2차 예외 | 보수적으로 `INTERRUPTED` 취급 후 원래 예외 전파 |
| `KeyboardInterrupt` | `INTERRUPTED`. 사람이 멈춘 것은 재개 가능 |
| Layer 0 재시도 한도 초과 | 기존대로 `USER_DECISION_REQUIRED` |

**원칙:** 증거는 제어를 막지 않는다. 복구 시도가 실패해도 원래 실패 정보를 잃지
않는다.

---

## 9. Security Considerations

| 위협 | 대응 |
| --- | --- |
| 예외 메시지에 경로·토큰 노출 | `reason` 2000자 상한. 기존 `str(exc)[:2000]` 관행과 동일 |
| 이벤트 로그 무한 증가 | append-only. 스텝당 최대 1건이므로 실질 상한은 전이 상한(128) |
| `--force-fail` 오용 | 살아 있는 락에 대해 거부. `--run-id` 명시 필수 |

새 외부 표면 없음. 네트워크·프로세스 실행 추가 없음.

---

## 10. Compatibility Considerations

- **durable 스키마:** 신규 파일 없음, 기존 스키마 무변경. 마이그레이션 불필요.
- **기존 run:** 정지 이벤트가 없는 run은 `stop` 키가 없고 verdict가 락·상태로만
  결정된다. 하위 호환.
- **종료 코드:** 변경 없음. `TERMINAL`은 `FAILED` 상태를 거쳐 기존 `exit_code()`가
  `EXIT_RUNTIME_FAILURE`를 낸다.
- **`status` 출력:** `stop`, `verdict` 키 **추가**. 기존 키 제거·변경 없음.
  저장소 내 소비자는 `tests/test_cli_commands.py`뿐임을 확인했다.
- **`R-5`/`R-6`:** 독립. 충돌 없음.

---

## 11. Test and Validation Strategy

| ID | 항목 |
| --- | --- |
| `V-01` | 분류표 전수 — 25개 + 기존 11개 각각이 기대 분류를 받는지 |
| `V-02` | 미분류 예외가 `INTERRUPTED`로 떨어지는지 |
| `V-03` | `TERMINAL` → `FAILED` 커밋 + 이벤트 기록 |
| `V-04` | `INTERRUPTED` → 상태 보존 + 이벤트 기록 + 전파 |
| `V-05` | `commit_generation`이 실패해도 이벤트가 남고 원래 예외가 전파 |
| `V-06` | `append_event`가 실패해도 원래 예외가 전파 (`G-6`) |
| `V-07` | `status` verdict 5종 |
| `V-08` | 정지 이벤트 staleness — generation 전진 후 무시 |
| `V-09` | `IN_PROGRESS` + 락 없음 → `STOPPED_RESUMABLE` |
| `V-10` | `RETRYABLE`이 재시도 경로로 가고 한도에서 에스컬레이션 |
| `V-11` | 뮤테이션 경로의 `OrcaTimeoutError`는 재시도되지 않음 |
| `V-12` | `--force-fail`이 살아 있는 락을 거부 |
| `V-13` | 전체 회귀 — 현재 baseline **332 passed** |

fault injection은 기존 `FakeOrcaClient` seam과 `unittest.mock`으로 수행한다.

---

## 12. Risks

| ID | 위험 | 완화 |
| --- | --- | --- |
| `R-A` | **분류 오류로 재개 가능한 run을 죽임** | 미분류 기본값 `INTERRUPTED`, `TERMINAL`은 명시 목록만. `V-01` 전수 테스트 |
| `R-B` | Layer 1의 `FAILED` 커밋이 generation 불일치로 실패 | 증거를 먼저 기록. 커밋 실패를 삼키고 기록 (`V-05`) |
| `R-C` | 뮤테이션 재시도로 이중 효과 | §4.5 규칙. `V-11` |
| `R-D` | `status` 출력 확장이 기존 소비자에 영향 | 키 추가만. 저장소 내 소비자 1개 확인 |
| `R-E` | 정지 이벤트가 과거 기록과 혼동 | generation stamp (`V-08`) |

---

## 13. Open Questions

| ID | 질문 | 기본 제안 |
| --- | --- | --- |
| `OQ-1` | `FAILED` run의 resume을 **코드로** 막을 것인가? | **막지 않음.** §11은 문서 규칙으로 유지. 분류 오류의 비용을 낮게 유지하는 쪽이 `R-A`에 유리하다. 대신 `status` verdict가 `STOPPED_TERMINAL`을 단정하고, resume 시 경고를 출력한다 |
| `OQ-2` | `ContractViolationError`를 `RETRYABLE`로 유지? | **유지.** 현행 동작이며 워커 재프롬프트로 실제 회복된다 |
| `OQ-3` | 정지 증거를 `append_event` 재사용 vs `stop.json` 신규 | **`append_event` 재사용.** 신규 스키마·파서·마이그레이션 불필요, "never raises" 계약이 검증됨. 베이스라인 계획 대비 변경 |
| `OQ-4` | `--force-fail`을 이번 범위에 포함? | **포함.** Layer 1이 상태를 보존하는 만큼 의도적 종료 수단이 함께 필요하다 |

---

## 14. Approval

- [ ] System Design approved
- [ ] Revision requested

**Next phase after explicit approval:** Phase 2 — Macro Blocking
