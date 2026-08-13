# Task Report: Orca Loop Failure Boundary

**Current Phase:** 2. Macro Blocking
**Status:** Waiting for Explicit User Approval

**Baseline:** `claude-mhj_26_08_13_05_phase1-system-design-failure-boundary.md` (approved)

---

## 1. Context & Objective

- **Problem:** 승인된 3층 경계 설계를 독립 검증 가능한 구현 경계로 분할해야 한다.
- **Goal:** 8개 Macro Block으로 분해하고 각 블록에 자체 완료 판정 기준을 부여한다.
- **Scope:** Phase 1 Scope + §2의 범위 정정 2건.
- **Out of Scope:** Phase 1 Out of Scope 승계 (`R-7`, 신규 dependency, worker 역할 변경).

---

## 2. 승인된 Scope 대비 정정 요청 2건

Phase 1은 Scope를 `run_loop.py`, `session.py`(재사용), `config.py`, `tests/*`로
적었다. 블록을 확정하며 두 곳이 추가로 필요함을 확인했다. **아키텍처를 바꾸지 않고
배치만 조정**하지만, 승인된 산출물과의 차이이므로 명시한다.

| 정정 | 내용 | 근거 |
| --- | --- | --- |
| `S-1` | `orca_loop/failure.py` 신규 모듈 | 분류기는 20여 개 예외 타입을 import해야 한다. `run_loop.py`는 이미 2,370줄이고, 분류기를 CLI 전체를 끌어오지 않고 테스트하려면 별도 모듈이 낫다 |
| `S-2` | `orca_loop/coordinator.py` 수정 | `operational_retry_result(error: ContractViolationError)`가 `error.reason`을 읽는다(`coordinator.py:826`). `RETRYABLE` 확장을 위해 시그니처를 `reason: str`로 넓혀야 한다 |

`S-1`을 거부하시면 분류기를 `run_loop.py`에 배치한다. `S-2`는 대안이 없다 —
넓히지 않으면 `B-05`가 불가능하다.

---

## 3. Core Deliverables — Macro Blocks

### B-01 — 정지 분류

| Field | Definition |
| --- | --- |
| **Block ID** | `B-01` |
| **Name** | `StopClass` and `classify_stop()` |
| **Rationale** | 분류는 순수 판정이며 I/O·상태와 무관하다. 다른 모든 블록이 이것에 의존하므로 단독으로 전수 검증한다. 오분류 비용이 비대칭(`R-A`)이라 이 블록의 정확성이 과제 전체의 안전성을 결정한다 |
| **Objective** | 36개 예외 클래스(신규 25 + 기존 11)가 각각 승인된 분류를 받고, 미등록 예외가 `INTERRUPTED`로 떨어진다 |
| **Scope** | `orca_loop/failure.py`(신규): `StopClass`, `TERMINAL_EXCEPTIONS`, `classify_stop()` |
| **Exclusions** | 증거 기록, 상태 전이, 재시도 실행, 호출 지점 판별 |
| **Dependencies** | 없음 |
| **Input** | `BaseException` |
| **Output** | `StopClass` — `TERMINAL` / `INTERRUPTED` / `RETRYABLE` |
| **Side Effects** | 없음 |
| **Failure Modes** | 없음. 어떤 입력에도 값을 반환한다. 미등록 타입은 `INTERRUPTED` |
| **Validation** | `V-B01-01` 분류표 전수 / `V-B01-02` 미등록 예외 기본값 / `V-B01-03` 서브클래스가 부모 분류를 상속 / `V-B01-04` `TERMINAL` 목록이 명시 집합과 정확히 일치 |
| **High-Level Pseudocode** | 1. `RETRYABLE_EXCEPTIONS` 멤버십 검사(가장 좁은 집합 우선) → 2. `TERMINAL_EXCEPTIONS` `isinstance` 검사 → 3. 그 외 `INTERRUPTED` 반환 |

**주의:** `isinstance` 기반이므로 상속 관계가 분류를 결정한다.
`ScopeViolationError ⊂ InputStagingError`처럼 둘 다 `TERMINAL`인 경우는 무해하지만,
부모와 자식의 분류가 다르면 좁은 쪽을 먼저 검사해야 한다. `V-B01-03`이 이를 고정한다.

---

### B-02 — 정지 증거 기록

| Field | Definition |
| --- | --- |
| **Block ID** | `B-02` |
| **Name** | `record_stop_event()` |
| **Rationale** | Phase 1 §2.3이 확인했듯 `FAILED` 커밋 자체가 `GenerationMismatchError`로 실패할 수 있다. 증거는 상태 전이보다 먼저, 그리고 절대 실패를 전파하지 않아야 한다(`G-6`) |
| **Objective** | 정지 사건이 `control/resume-events.jsonl`에 남고, 기록 실패가 원래 예외를 가리지 않는다 |
| **Scope** | `orca_loop/failure.py`: `record_stop_event()`. `session.append_event()` 재사용 |
| **Exclusions** | 이벤트 읽기(`B-06`), 상태 전이(`B-03`) |
| **Dependencies** | `B-01` |
| **Input** | `control_dir: Path`, `exc: BaseException`, `classification: StopClass`, `generation: int`, `state: LoopState \| None`, `state_committed: bool` |
| **Output** | `None` |
| **Side Effects** | `control/resume-events.jsonl` 1줄 append |
| **Failure Modes** | 없음. `append_event`가 모든 I/O 오류를 삼킨다(`session.py:45`) |
| **Validation** | `V-B02-01` 이벤트 필드 정확성 / `V-B02-02` `reason` 2000자 절단 / `V-B02-03` `append_event` 실패 시에도 예외 없음 |
| **High-Level Pseudocode** | 1. `reason = str(exc)[:2000]` 정규화 → 2. `detail` 구성(`classification`, `exception`, `reason`, `generation`, `state`, `resumable`, `state_committed`) → 3. `append_event(control_dir, "stopped", detail)` |

---

### B-03 — Layer 1 분류 경계

| Field | Definition |
| --- | --- |
| **Block ID** | `B-03` |
| **Name** | `run_coordinator` 예외 경계 |
| **Rationale** | `GenerationController`가 존재하는 유일한 지점이다(`run_loop.py` `run_coordinator`). 도메인 상태 전이는 여기서만 가능하다 |
| **Objective** | `TERMINAL`은 증거 + `FAILED` 커밋 후 정상 반환하고, `INTERRUPTED`는 증거만 남기고 전파한다 |
| **Scope** | `run_loop.py` `run_coordinator()` |
| **Exclusions** | 분류 로직(`B-01`), 기록 구현(`B-02`), 재시도(`B-05`) |
| **Dependencies** | `B-01`, `B-02` |
| **Input** | `PreflightResult`, `OrcaClient` |
| **Output** | `CoordinatorState` (`TERMINAL` 및 정상 종료) 또는 예외 전파(`INTERRUPTED`) |
| **Side Effects** | 정지 이벤트 append. `TERMINAL`일 때만 `state.<n>.json` 커밋 |
| **Failure Modes** | `FAILED` 커밋이 `GenerationError`로 실패 → 포착 → `state_committed=false`로 기록 → 원래 예외 전파. 분류 중 2차 예외 → `INTERRUPTED` 취급 후 원래 예외 전파 |
| **Validation** | `V-B03-01` `TERMINAL` → `FAILED` + 이벤트 / `V-B03-02` `INTERRUPTED` → 상태 보존 + 이벤트 + 전파 / `V-B03-03` 커밋 실패해도 이벤트 존재·원래 예외 전파 / `V-B03-04` 증거 기록이 상태 전이보다 먼저 |
| **High-Level Pseudocode** | 1. `controller, pool = _resume(...) or _initialize(...)` — **이 구간은 경계 밖**(컨트롤러가 아직 없음) → 2. `try: return _run_loop(...)` → 3. `except BaseException as exc:` → 4. `kind = classify_stop(exc)` → 5. `committed = False` → 6. `TERMINAL`이면 `try: controller.commit(state_value=FAILED, status=FAILED, signal=ABORT); committed = True except GenerationError: pass` → 7. `record_stop_event(..., state_committed=committed)` → 8. `TERMINAL`이면 `return controller.state`, 아니면 `raise` |

**설계 판단:** 6단계와 7단계의 순서가 Phase 1 §2.3의 "증거 먼저"와 어긋나 보이지만,
`record_stop_event`는 `state_committed` 값을 담아야 하므로 커밋 시도 뒤에 호출된다.
`G-6`는 여전히 성립한다 — 커밋이 어떻게 실패하든 이벤트는 반드시 기록된다.
`V-B03-03`이 이를 고정한다.

---

### B-04 — Layer 2 포괄 경계

| Field | Definition |
| --- | --- |
| **Block ID** | `B-04` |
| **Name** | `main()` catch-all |
| **Rationale** | 컨트롤러 생성 전(preflight, `_initialize`, `_resume`) 실패와 `B-03`이 전파한 `INTERRUPTED`를 모두 받아야 한다. 25개 예외가 traceback으로 빠지는 `F-1`이 여기서 닫힌다 |
| **Objective** | 어떤 예외도 traceback 없이 JSON 오류와 종료 코드로 귀결되고, 가능하면 증거가 남는다 |
| **Scope** | `run_loop.py` `main()` |
| **Exclusions** | 상태 전이(컨트롤러 없음) |
| **Dependencies** | `B-01`, `B-02`, `B-03` |
| **Input** | 전파된 예외, `arguments: RunArguments \| None` |
| **Output** | `int` 종료 코드 |
| **Side Effects** | 정지 이벤트(`arguments`와 run 디렉터리가 있을 때만), 실패 보고서, stderr JSON |
| **Failure Modes** | `arguments`가 `None`이면 증거를 남기지 않고 보고만 한다(기존 `_report_failure` 가드와 동일) |
| **Validation** | `V-B04-01` 미분류 예외가 `EXIT_RUNTIME_FAILURE`로 귀결 / `V-B04-02` preflight 실패는 기존 `EXIT_PREFLIGHT` 유지 / `V-B04-03` `arguments is None`에서 안전 / `V-B04-04` `KeyboardInterrupt` 동작 보존 |
| **High-Level Pseudocode** | 기존 `except` 체인 뒤에 `except BaseException as exc:` 추가 → 1. `arguments`가 있고 run 디렉터리가 존재하면 `record_stop_event` → 2. `_report_failure(arguments, str(exc))` → 3. stderr JSON 출력 → 4. `EXIT_RUNTIME_FAILURE` 반환. 기존 4개 핸들러의 종료 코드는 **변경하지 않는다** |

---

### B-05 — Layer 0 재시도 확장

| Field | Definition |
| --- | --- |
| **Block ID** | `B-05` |
| **Name** | `RETRYABLE` 라우팅과 뮤테이션 제외 |
| **Rationale** | `F-4`. 재시도가 가장 유효한 일시적 오류가 재시도 경로에 연결돼 있지 않다. 동시에 Phase 1 §4.5의 뮤테이션 제외 규칙을 지켜야 한다 |
| **Objective** | `RETRYABLE` 예외가 `operational_retry_result`로 가고 한도에서 에스컬레이션되며, 뮤테이션 경로의 타임아웃은 재시도되지 않는다 |
| **Scope** | `run_loop.py` `_run_loop()` except 절, `orca_loop/coordinator.py` `operational_retry_result()` 시그니처(`S-2`) |
| **Exclusions** | 분류표 정의(`B-01`), 경계 처리(`B-03`) |
| **Dependencies** | `B-01` |
| **Input** | 루프 본문에서 발생한 예외, `LoopCounters`, `operational_retry_limit` |
| **Output** | `StepExecutionResult`(재시도) 또는 예외 전파 |
| **Side Effects** | 재시도 시 generation 커밋 1회 |
| **Failure Modes** | 한도 도달 → `USER_DECISION_REQUIRED` 또는 `ABORT`(기존 `operational_retry_result` 로직 보존) |
| **Validation** | `V-B05-01` `RETRYABLE`이 재시도로 감 / `V-B05-02` 한도에서 에스컬레이션 / `V-B05-03` `TERMINAL`/`INTERRUPTED`는 전파 / `V-B05-04` 뮤테이션 경로 `OrcaTimeoutError` 미재시도 / `V-B05-05` `ContractViolationError` 기존 동작 보존 |
| **High-Level Pseudocode** | 1. `operational_retry_result`의 `error` 파라미터를 `reason: str`로 교체(에스컬레이션 마커 검사는 `reason` 기준으로 보존) → 2. `_run_loop`의 `except ContractViolationError`를 `except Exception as exc:`로 확장 → 3. `classify_stop(exc) is not RETRYABLE`이면 `raise` → 4. 뮤테이션 경로 발생이면 `raise` → 5. 아니면 `operational_retry_result(reason=...)` 후 커밋 |

**뮤테이션 경로 판별:** `_execute_worker` 호출 구간에서 발생한 예외는 재시도하지
않는다. `_run_loop`의 `if state in WORKER_STATES` 분기를 별도 `try`로 감싸
`RETRYABLE` 승격을 적용하지 않는 방식으로 구현한다. 예외 타입이 아니라 **발생
위치**가 판별 기준이라는 Phase 1 §4.5 규칙을 그대로 따른다.

---

### B-06 — `status` verdict

| Field | Definition |
| --- | --- |
| **Block ID** | `B-06` |
| **Name** | 정지 이벤트 읽기와 verdict 판정 |
| **Rationale** | `G-4`. `IN_PROGRESS` + 락 없음 조합에서 현재 `status`가 아무것도 말하지 않는 것이 `F-2`의 핵심 공백이다 |
| **Objective** | `status --json`이 `stop`과 `verdict`를 노출하고, 과거 generation의 정지 기록은 무시된다 |
| **Scope** | `orca_loop/failure.py`: `read_latest_stop_event()`. `run_loop.py` `_status_report()` |
| **Exclusions** | 이벤트 기록(`B-02`) |
| **Dependencies** | `B-02` |
| **Input** | `control_dir: Path`, `CoordinatorState`, `LockInfo \| None` |
| **Output** | `dict` — `stop` 키(선택), `verdict` 키(필수, 5종) |
| **Side Effects** | 없음(읽기 전용) |
| **Failure Modes** | 파손 JSONL 줄은 건너뛴다. 이벤트 파일 부재는 `stop` 키 생략 |
| **Validation** | `V-B06-01` verdict 5종 / `V-B06-02` staleness(generation 전진 후 무시) / `V-B06-03` `IN_PROGRESS` + 락 없음 → `STOPPED_RESUMABLE` / `V-B06-04` 파손 줄 내성 / `V-B06-05` 기존 status 키 무변경 |
| **High-Level Pseudocode** | 1. JSONL을 역순으로 읽어 `kind == "stopped"` 첫 줄 획득 → 2. `event.generation != state.generation`이면 `None` 취급 → 3. verdict 판정표(Phase 1 §6.2) 적용 → 4. `stop`/`verdict` 키 설정 |

---

### B-07 — `force-fail` 서브커맨드

| Field | Definition |
| --- | --- |
| **Block ID** | `B-07` |
| **Name** | 운영자 명시 종료 |
| **Rationale** | `B-03`이 `INTERRUPTED`에서 상태를 보존하므로, 운영자가 의도적으로 run을 끝낼 수단이 필요하다. 현재는 없다(`OQ-4` 승인분) |
| **Objective** | 살아 있는 코디네이터가 없는 run을 `FAILED`로 전이하고 근거를 남긴다 |
| **Scope** | `run_loop.py`: `SUBCOMMANDS`, `_print_subcommand_help`, `main()` 분기, 신규 `_force_fail()` |
| **Exclusions** | 자동 종료, 강제 락 해제 |
| **Dependencies** | `B-06`(락 판정 재사용) |
| **Input** | `--run-id: str`, `--reason: str` |
| **Output** | `int` 종료 코드 |
| **Side Effects** | `FAILED` 커밋, `kind="force_failed"` 이벤트 |
| **Failure Modes** | 락이 살아 있으면 거부(`EXIT_PREFLIGHT`). 이미 terminal 상태면 거부. 상태 로드 실패는 `EXIT_PREFLIGHT` |
| **Validation** | `V-B07-01` 살아 있는 락 거부 / `V-B07-02` `FAILED` 커밋 + 이벤트 / `V-B07-03` 이미 terminal이면 거부 / `V-B07-04` help 문자열 존재 |
| **High-Level Pseudocode** | 1. 인자 파싱 → 2. 상태·원장 로드 → 3. `inspect_lock`으로 생존 확인, 살아 있으면 거부 → 4. terminal 상태면 거부 → 5. `GenerationController` 구성 후 `FAILED` 커밋 → 6. `force_failed` 이벤트 append → 7. JSON 출력 |

---

### B-08 — 회귀와 fault injection 검증

| Field | Definition |
| --- | --- |
| **Block ID** | `B-08` |
| **Name** | 검증 매트릭스 |
| **Rationale** | 이 과제는 예외 경로가 산출물이므로, 정상 경로 테스트로는 아무것도 보증하지 못한다 |
| **Objective** | `V-B01-01`~`V-B07-04`가 구현되고, 전체 스위트가 baseline(332 passed) 이상 통과한다 |
| **Scope** | `tests/test_failure.py`(신규), `tests/test_cli_commands.py`, `tests/test_cli.py` |
| **Exclusions** | 실제 Orca 런타임 대상 통합 테스트 |
| **Dependencies** | `B-01`~`B-07` |
| **Input** | 구현된 블록 |
| **Output** | 테스트 결과와 구현 보고서 |
| **Side Effects** | 임시 디렉터리 |
| **Failure Modes** | 신규 실패는 수정. 무관한 기존 실패는 분리 기록 |
| **Validation** | `V-B08-01` `pytest -q` 전체 PASS / `V-B08-02` 분류표 36종 전수 / `V-B08-03` live v1 run에 대한 `status` 회귀 |
| **High-Level Pseudocode** | 1. 분류 파라미터화 테스트 → 2. `mock`으로 각 층 예외 주입 → 3. `FakeOrcaClient` 재사용 → 4. 전체 스위트 실행 |

---

## 4. Dependency Order

```
B-01 (분류)
  ├─→ B-02 (증거) ──→ B-03 (Layer 1) ──→ B-04 (Layer 2)
  │                     │
  │                     └─→ B-06 (status) ──→ B-07 (force-fail)
  └─→ B-05 (재시도)
                                              B-08 (검증)
```

임계 경로: `B-01 → B-02 → B-03 → B-04`.
`B-05`는 `B-01` 완료 후 병행 가능하다.

---

## 5. Implementation Boundary Rules

1. `CoordinatorState` 스키마와 durable 파일 형식을 **변경하지 않는다**. 정지 증거는
   기존 `resume-events.jsonl`에 append한다.
2. 기존 4개 최상위 핸들러의 **종료 코드를 변경하지 않는다**. `B-04`는 뒤에 추가만 한다.
3. `status`의 기존 키를 제거·변경하지 않는다. `stop`, `verdict`는 추가다.
4. `TERMINAL` 분류는 명시 목록만. 미등록은 항상 `INTERRUPTED`(`R-A`).
5. 뮤테이션 경로에서는 in-process 재시도를 하지 않는다(Phase 1 §4.5).
6. 신규 production dependency를 도입하지 않는다.
7. `FAILED` run의 resume을 코드로 막지 않는다(`OQ-1` 승인분).

---

## 6. Validation and Risks

- **Validation:** 블록 확정 전 `operational_retry_result` 시그니처
  (`coordinator.py:821-828`), `GenerationController.commit` 파라미터
  (`coordinator.py:299-320`), `_split_command`/`SUBCOMMANDS` 구조
  (`run_loop.py:1782-1793`), `append_event` 계약(`session.py:44-60`),
  `commit_generation`의 generation 강제(`generation.py:243`)를 직접 확인했다.
- **Risks:**
  - `B-01`이 과제 전체의 안전성을 결정한다. `V-B01-01` 전수 테스트가 PASS하기
    전에는 `B-03`의 `TERMINAL` 분기를 활성화하지 않는다.
  - `B-05`의 `except Exception` 확장은 루프의 기존 흐름을 바꾼다.
    `V-B05-05`(기존 `ContractViolationError` 동작 보존)를 회귀 기준으로 삼는다.
- **Open Questions:** `S-1` 승인 여부. 나머지는 Phase 1에서 확정되었다.

---

## 7. Approval

- [ ] Macro Blocking approved
- [ ] Revision requested

**Next phase after explicit approval:** Phase 3 — Micro Blocking
