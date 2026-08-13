# Task Report: Orca Loop Failure Boundary

**Current Phase:** 3. Micro Blocking
**Status:** Waiting for Explicit User Approval

**Baseline:** Phase 1 `claude-mhj_26_08_13_05`, Phase 2 `claude-mhj_26_08_13_06` (both approved,
including scope corrections `S-1` and `S-2`)

---

## 1. Context & Objective

- **Problem:** 승인된 8개 Macro Block을 함수·타입·테스트 수준의 확정 계약으로 고정한다.
- **Goal:** 12개 Micro Block으로 분해한다.
- **Scope:** Phase 2 Scope 승계 (`orca_loop/failure.py` 신규, `orca_loop/coordinator.py`
  시그니처 확장 포함).
- **Out of Scope:** Phase 1/2 Out of Scope 승계.

---

## 2. Shared Type Contracts

### 2.1 `orca_loop/failure.py` (신규)

```python
class StopClass(StrEnum):
    TERMINAL = "TERMINAL"
    INTERRUPTED = "INTERRUPTED"
    RETRYABLE = "RETRYABLE"


STOP_EVENT_KIND = "stopped"
FORCE_FAIL_EVENT_KIND = "force_failed"
STOP_REASON_LIMIT = 2_000


def classify_stop(exc: BaseException) -> StopClass: ...


def record_stop_event(
    control_dir: Path,
    *,
    exc: BaseException,
    classification: StopClass,
    generation: int,
    state: LoopState | None,
    state_committed: bool,
) -> None: ...


@dataclass(frozen=True)
class StopEvent:
    classification: StopClass
    exception: str
    reason: str
    generation: int
    state: str | None
    resumable: bool
    state_committed: bool
    recorded_at: str


def read_latest_stop_event(control_dir: Path) -> StopEvent | None: ...
```

### 2.2 분류 집합 (확정)

```python
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ContractViolationError,      # ProvenanceError 포함
    OrcaTimeoutError,
)

TERMINAL_EXCEPTIONS: tuple[type[BaseException], ...] = (
    InputStagingError,           # Scope/PathBoundary/TransportProvenance 포함
    GuardScopeViolationError,
    GuardPathBoundaryError,
    LedgerIntegrityError,        # InvalidRoundError 포함
    TemplateContractError,
    WorkspaceError,              # PathBoundaryError 포함
    LaunchProfileError,
    GenerationMismatchError,
    TestPolicyError,
    DispatchProvenanceError,
    GateProtocolError,
)
```

**검사 순서가 계약이다:** `RETRYABLE` → `TERMINAL` → 기본값 `INTERRUPTED`.

상속 관계상 순서가 결과를 바꾸는 쌍:

| 좁은 쪽 | 넓은 쪽 | 결과 |
| --- | --- | --- |
| `OrcaTimeoutError` (`RETRYABLE`) | `OrcaCommandError` (`INTERRUPTED`) | 먼저 검사되어 `RETRYABLE` |
| `GenerationMismatchError` (`TERMINAL`) | `GenerationError` (`INTERRUPTED`) | 명시 목록이라 `TERMINAL` |
| `DispatchProvenanceError` (`TERMINAL`) | `DispatcherError` (`INTERRUPTED`) | 명시 목록이라 `TERMINAL` |
| `AtomicWriteError` | `GenerationError` | 둘 다 `INTERRUPTED` |

`TERMINAL`/`RETRYABLE` 어느 쪽도 서로의 서브클래스가 아니므로 두 집합 간 충돌은
없다. `V-B01-04`가 이 불변식을 고정한다.

### 2.3 정지 이벤트 wire 형식

`append_event(control_dir, "stopped", detail)`의 `detail`:

| 필드 | 타입 | 불변식 |
| --- | --- | --- |
| `classification` | `str` | `StopClass` 값 |
| `exception` | `str` | `type(exc).__name__`, nonempty |
| `reason` | `str` | `str(exc)`를 공백 정규화 후 2000자 절단 |
| `generation` | `int` | `>= 0` |
| `state` | `str \| None` | `LoopState` 값 |
| `resumable` | `bool` | `classification is not TERMINAL` |
| `state_committed` | `bool` | `FAILED` 커밋 성공 여부 |

`recorded_at`, `kind`는 `append_event`가 부여한다(`session.py:49-54`).

---

## 3. Micro Blocks

### M-B01-01 — 모듈 골격과 `StopClass`

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-01` |
| **Parent Block** | `B-01` |
| **Name** | `orca_loop/failure.py` 신설 |
| **Rationale** | 분류기가 20여 개 예외 타입을 import해야 하며, `run_loop.py`(2,370줄)를 거치지 않고 테스트 가능해야 한다 |
| **Objective** | 모듈이 생성되고 `StopClass`와 상수가 정의되며 순환 import가 없다 |
| **Target Files** | `orca_loop/failure.py`(신규) |
| **Preconditions** | 없음 |
| **Input Type** | 없음(선언) |
| **Input Validation** | 없음 |
| **Output Type** | `StopClass`, `STOP_EVENT_KIND`, `FORCE_FAIL_EVENT_KIND`, `STOP_REASON_LIMIT` |
| **Output Validation** | `StopClass` 멤버 3개. `import orca_loop.failure` 성공 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `from __future__ import annotations` → 2. 표준 라이브러리 import → 3. 예외 클래스를 `contracts`, `dispatcher`, `generation`, `guards`, `ledger`, `orca_client`, `profiles`, `roles`, `testrunner`, `transport`, `workspace`, `escalation`에서 import → 4. `models`에서 `LoopState` import → 5. `session`에서 `append_event` import → 6. `StopClass` 선언 → 7. 상수 선언 |
| **Tests** | `T-B01-01` import 성공 및 순환 import 부재 / `T-B01-02` `StopClass` 값 문자열 |
| **Rollback** | 모듈 삭제 |

**순환 import 주의:** `failure.py`는 `run_loop.py`를 import하지 않는다. `session.py`가
`dispatcher`를 import하므로(`session.py:18`) `failure.py` → `session` → `dispatcher`
방향만 존재하며 역방향은 없다.

---

### M-B01-02 — 분류 집합 상수

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-02` |
| **Parent Block** | `B-01` |
| **Name** | `RETRYABLE_EXCEPTIONS`, `TERMINAL_EXCEPTIONS` |
| **Rationale** | 분류표를 함수 본문이 아니라 데이터로 두어야 전수 테스트가 표를 직접 대조할 수 있다 |
| **Objective** | 두 tuple이 §2.2와 정확히 일치하고 상호 배타적이다 |
| **Target Files** | `orca_loop/failure.py` |
| **Preconditions** | `M-B01-01` |
| **Input Type** | 없음(상수) |
| **Input Validation** | 없음 |
| **Output Type** | `tuple[type[BaseException], ...]` × 2 |
| **Output Validation** | 두 집합 간 `issubclass` 관계가 없다. 각 원소가 `BaseException` 서브클래스다 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `RETRYABLE_EXCEPTIONS` 선언(2종) → 2. `TERMINAL_EXCEPTIONS` 선언(11종) → 3. 각 항목 옆에 포함되는 서브클래스를 주석으로 명시 |
| **Tests** | `T-B01-03` 두 집합 간 상속 관계 부재 / `T-B01-04` 모든 원소가 예외 클래스 |
| **Rollback** | 상수 제거 |

---

### M-B01-03 — `classify_stop()`

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-03` |
| **Parent Block** | `B-01` |
| **Name** | 분류 함수 |
| **Rationale** | 오분류 비용이 비대칭(`R-A`)이므로 판정 규칙을 한 곳에 고정한다 |
| **Objective** | 36개 예외가 승인된 분류를 받고, 미등록 예외가 `INTERRUPTED`로 떨어진다 |
| **Target Files** | `orca_loop/failure.py` |
| **Preconditions** | `M-B01-02` |
| **Input Type** | `BaseException` |
| **Input Validation** | 없음. 어떤 객체에도 값을 반환한다 |
| **Output Type** | `StopClass` |
| **Output Validation** | 항상 3개 멤버 중 하나 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `isinstance(exc, RETRYABLE_EXCEPTIONS)` → `StopClass.RETRYABLE` → 2. `isinstance(exc, TERMINAL_EXCEPTIONS)` → `StopClass.TERMINAL` → 3. `StopClass.INTERRUPTED` 반환 |
| **Tests** | `T-B01-05` 36종 파라미터화 전수 / `T-B01-06` 미등록 예외(`RuntimeError`, `ValueError`) → `INTERRUPTED` / `T-B01-07` `KeyboardInterrupt` → `INTERRUPTED` / `T-B01-08` `OrcaTimeoutError`가 `OrcaCommandError`보다 우선 / `T-B01-09` `GenerationMismatchError`와 `GenerationError` 분리 |
| **Rollback** | 함수 제거 |

---

### M-B02-01 — `record_stop_event()`

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B02-01` |
| **Parent Block** | `B-02` |
| **Name** | 정지 증거 기록 |
| **Rationale** | `FAILED` 커밋이 실패할 수 있으므로(`generation.py:243`) 증거는 독립적으로 반드시 남아야 한다(`G-6`) |
| **Objective** | 정지 사건이 JSONL에 1줄 append되고, 기록 실패가 예외를 던지지 않는다 |
| **Target Files** | `orca_loop/failure.py` |
| **Preconditions** | `M-B01-03` |
| **Input Type** | `control_dir: Path`, `exc: BaseException`, `classification: StopClass`, `generation: int`, `state: LoopState \| None`, `state_committed: bool` |
| **Input Validation** | 없음. 모든 입력을 그대로 직렬화 가능한 형태로 변환한다 |
| **Output Type** | `None` |
| **Output Validation** | §2.3 필드 집합과 정확히 일치하는 줄이 append된다 |
| **Exceptions** | **없음.** `append_event`가 모든 I/O 오류를 삼킨다(`session.py:58-60`) |
| **Side Effects** | `control/resume-events.jsonl` 1줄 append |
| **Detailed Pseudocode** | 1. `reason = " ".join(str(exc).split())[:STOP_REASON_LIMIT]` → 2. `detail = {"classification": classification.value, "exception": type(exc).__name__, "reason": reason, "generation": generation, "state": None if state is None else state.value, "resumable": classification is not StopClass.TERMINAL, "state_committed": state_committed}` → 3. `append_event(control_dir, STOP_EVENT_KIND, detail)` |
| **Tests** | `T-B02-01` 필드 정확성 / `T-B02-02` `reason` 절단·개행 정규화 / `T-B02-03` `append_event` 예외 주입 시에도 반환 / `T-B02-04` `resumable`이 분류에서 파생 |
| **Rollback** | 함수 제거 |

---

### M-B03-01 — Layer 1 경계

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-01` |
| **Parent Block** | `B-03` |
| **Name** | `run_coordinator` 분류 경계 |
| **Rationale** | `GenerationController`가 존재하는 유일한 지점이며, 도메인 상태 전이는 여기서만 가능하다 |
| **Objective** | `TERMINAL`은 `FAILED` 커밋 후 정상 반환하고 `INTERRUPTED`는 증거만 남기고 전파한다. 두 경우 모두 이벤트가 존재한다 |
| **Target Files** | `run_loop.py` `run_coordinator()` |
| **Preconditions** | `M-B02-01` |
| **Input Type** | `preflight: PreflightResult`, `client: OrcaClient` |
| **Input Validation** | 없음 |
| **Output Type** | `CoordinatorState` 또는 예외 전파 |
| **Output Validation** | `TERMINAL` 반환 시 `state.state is LoopState.FAILED` 또는 `state_committed=false`가 기록됨 |
| **Exceptions** | `INTERRUPTED`는 원래 예외를 그대로 전파한다(`raise` 단독, 컨텍스트 보존) |
| **Side Effects** | 정지 이벤트 append. `TERMINAL`일 때만 generation 커밋 |
| **Detailed Pseudocode** | 1. `controller, pool = _resume(...) or _initialize(...)` — **경계 밖** → 2. `try: return _run_loop(controller, pool, preflight, client)` → 3. `except BaseException as exc:` → 4. `kind = classify_stop(exc)` → 5. `committed = False` → 6. `if kind is StopClass.TERMINAL:` `try: controller.commit(stage=TRANSITION_COMMITTED, active=None, reason=f"stopped: {type(exc).__name__}", signal=SignalKind.ABORT, state_value=LoopState.FAILED, status=RunStatus.FAILED); committed = True` `except (GenerationError, OSError): pass` → 7. `record_stop_event(controller.workspace.control_dir, exc=exc, classification=kind, generation=controller.state.generation, state=controller.state.state, state_committed=committed)` → 8. `if kind is StopClass.TERMINAL: return controller.state` → 9. `raise` |
| **Tests** | `T-B03-01` `TERMINAL` → `FAILED` + 이벤트 + 정상 반환 / `T-B03-02` `INTERRUPTED` → 상태 보존 + 이벤트 + 전파 / `T-B03-03` 커밋이 `GenerationMismatchError`여도 이벤트에 `state_committed=false` / `T-B03-04` `KeyboardInterrupt` 전파 / `T-B03-05` 정상 경로 무변경 |
| **Rollback** | `try/except` 제거 |

**`BaseException` 사용 근거:** `KeyboardInterrupt`와 `SystemExit`도 증거를 남겨야
한다. 둘 다 `INTERRUPTED`로 분류되어 그대로 전파되므로 중단 의미는 보존된다.

---

### M-B04-01 — Layer 2 포괄 경계

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-01` |
| **Parent Block** | `B-04` |
| **Name** | `main()` catch-all |
| **Rationale** | 컨트롤러 생성 전 실패와 `M-B03-01`이 전파한 `INTERRUPTED`를 모두 받는다. `F-1`이 여기서 닫힌다 |
| **Objective** | 어떤 예외도 traceback 없이 JSON 오류와 종료 코드로 귀결된다 |
| **Target Files** | `run_loop.py` `main()` |
| **Preconditions** | `M-B03-01` |
| **Input Type** | 전파된 예외, `arguments: RunArguments \| None` |
| **Input Validation** | `arguments is None`이거나 run 디렉터리가 없으면 증거를 생략한다 |
| **Output Type** | `int` |
| **Output Validation** | `EXIT_RUNTIME_FAILURE`. 기존 4개 핸들러의 종료 코드는 불변 |
| **Exceptions** | 없음. 최종 경계다 |
| **Side Effects** | 정지 이벤트(가능할 때), 실패 보고서, stderr JSON |
| **Detailed Pseudocode** | 기존 `except KeyboardInterrupt` **뒤에** 추가. 1. `except BaseException as exc:` → 2. `if arguments is not None:` control dir 경로 계산 후 `is_dir()`이면 `record_stop_event(..., classification=classify_stop(exc), generation=-1, state=None, state_committed=False)` → 3. `_report_failure(arguments, str(exc))` → 4. stderr에 `{"status": "FAIL", "error": str(exc)}` 출력 → 5. `return EXIT_RUNTIME_FAILURE` |
| **Tests** | `T-B04-01` 미분류 예외 → `EXIT_RUNTIME_FAILURE` + JSON / `T-B04-02` preflight 실패는 `EXIT_PREFLIGHT` 유지 / `T-B04-03` `arguments is None`에서 안전 / `T-B04-04` `finally`의 락 해제 유지 |
| **Rollback** | 핸들러 제거 |

**`generation=-1` 의미:** Layer 2에는 컨트롤러가 없어 실제 generation을 알 수 없다.
`-1`은 어떤 `state.generation`과도 일치하지 않으므로 `M-B06-01`의 staleness 규칙에
의해 자동으로 "현재 아님"이 된다. 즉 Layer 2 이벤트는 증거로만 남고 verdict를
바꾸지 않는다.

---

### M-B05-01 — `operational_retry_result` 시그니처 확장

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-01` |
| **Parent Block** | `B-05` |
| **Name** | `error` → `reason` 파라미터 (`S-2`) |
| **Rationale** | 현재 `error.reason`을 읽으므로(`coordinator.py:826`) `ContractViolationError` 외 타입을 넘길 수 없다 |
| **Objective** | 함수가 `reason: str`을 받고 에스컬레이션 마커 판정이 동일하게 동작한다 |
| **Target Files** | `orca_loop/coordinator.py`, `run_loop.py`(호출부) |
| **Preconditions** | 없음 |
| **Input Type** | `ledger: ConsensusLedger`, `counters: LoopCounters`, `limit: int`, `reason: str`, `finding_ids: tuple[str, ...]` |
| **Input Validation** | `reason`은 nonempty |
| **Output Type** | `StepExecutionResult` |
| **Output Validation** | 기존과 동일한 `SignalKind` 선택 로직 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `error: ContractViolationError` 파라미터를 `reason: str`로 교체 → 2. 본문의 `error.reason`을 `reason`으로 치환 → 3. `TransitionSignal(signal, reason, finding_ids)` 구성 → 4. 호출부(`run_loop.py:1689`)를 `reason=exc.reason`으로 갱신 |
| **Tests** | `T-B05-01` 마커 문자열 3종에서 `ESCALATE` / `T-B05-02` 한도 미만에서 `OPERATIONAL_RETRY` / `T-B05-03` 한도 도달·마커 없음에서 `ABORT` |
| **Rollback** | 시그니처 복원 |

---

### M-B05-02 — `_run_loop` 재시도 라우팅

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-02` |
| **Parent Block** | `B-05` |
| **Name** | `RETRYABLE` 라우팅과 뮤테이션 경로 제외 |
| **Rationale** | `F-4`. 동시에 Phase 1 §4.5의 "발생 위치가 판별 기준" 규칙을 지켜야 한다 |
| **Objective** | `RETRYABLE`이 재시도로 가고, 워커 구간의 비계약 예외는 재시도되지 않으며, 기존 `ContractViolationError` 동작이 보존된다 |
| **Target Files** | `run_loop.py` `_run_loop()` |
| **Preconditions** | `M-B01-03`, `M-B05-01` |
| **Input Type** | 루프 본문 예외, `LoopCounters`, `config.operational_retry_limit` |
| **Input Validation** | 없음 |
| **Output Type** | `StepExecutionResult` 커밋 또는 예외 전파 |
| **Output Validation** | 재시도 시 `transitions`가 1 증가 |
| **Exceptions** | `RETRYABLE`이 아니거나 워커 구간 비계약 예외는 전파 |
| **Side Effects** | 재시도 시 generation 커밋 1회 |
| **Detailed Pseudocode** | 1. 루프 본문 진입 전 `in_worker = state in WORKER_STATES` 계산 → 2. `except Exception as exc:` (기존 `except ContractViolationError` 대체) → 3. `if classify_stop(exc) is not StopClass.RETRYABLE: raise` → 4. `if in_worker and not isinstance(exc, ContractViolationError): raise` → 5. `reason = exc.reason if isinstance(exc, ContractViolationError) else " ".join(str(exc).split())[:STOP_REASON_LIMIT]` → 6. `retry = operational_retry_result(ledger=..., counters=..., limit=..., reason=reason, finding_ids=...)` → 7. `commit_step_transition(controller, retry, config)` → 8. `transitions += 1` |
| **Tests** | `T-B05-04` 비워커 구간 `OrcaTimeoutError` 재시도 / `T-B05-05` 워커 구간 `OrcaTimeoutError` 전파 / `T-B05-06` 워커 구간 `ContractViolationError`는 기존대로 재시도 / `T-B05-07` `TERMINAL` 전파 / `T-B05-08` 한도 도달 시 `USER_DECISION_REQUIRED` |
| **Rollback** | `except ContractViolationError`로 복원 |

**4단계 근거:** 워커 구간(`_execute_worker`)은 `task-create`와 `dispatch` 뮤테이션을
발행한다. 그 구간의 타임아웃은 효과가 불명이므로 재시도하지 않는다. 반면
`ContractViolationError`는 뮤테이션 완료 후 산출물 파싱에서 발생하므로 안전하며,
기존 동작이기도 하다.

---

### M-B06-01 — `read_latest_stop_event()`

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-01` |
| **Parent Block** | `B-06` |
| **Name** | 최신 정지 이벤트 읽기 |
| **Rationale** | JSONL은 이력이므로 "현재 정지 상태"를 뽑는 규칙이 필요하다 |
| **Objective** | 마지막 `stopped` 이벤트를 반환하고 파손된 줄에 내성이 있다 |
| **Target Files** | `orca_loop/failure.py` |
| **Preconditions** | `M-B02-01` |
| **Input Type** | `control_dir: Path` |
| **Input Validation** | 파일 부재 → `None` |
| **Output Type** | `StopEvent \| None` |
| **Output Validation** | 반환 시 모든 필드가 타입에 맞는다 |
| **Exceptions** | 없음. I/O 오류와 파손 줄은 삼킨다 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `path = control_dir / EVENTS_NAME`; 없으면 `None` → 2. `try: lines = path.read_text(encoding="utf-8").splitlines() except OSError: return None` → 3. 역순 순회 → 4. `json.loads` 실패 시 건너뜀 → 5. `kind != STOP_EVENT_KIND`면 건너뜀 → 6. 필수 필드 타입 검증 실패 시 건너뜀 → 7. `StopEvent` 구성 후 반환 → 8. 없으면 `None` |
| **Tests** | `T-B06-01` 최신 이벤트 반환 / `T-B06-02` 파손 줄 건너뜀 / `T-B06-03` 다른 `kind` 무시 / `T-B06-04` 파일 부재 → `None` |
| **Rollback** | 함수 제거 |

---

### M-B06-02 — `status` verdict

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-02` |
| **Parent Block** | `B-06` |
| **Name** | `_status_report` verdict 판정 |
| **Rationale** | `G-4`. `IN_PROGRESS` + 락 없음에서 현재 `status`가 침묵하는 것이 `F-2`의 핵심 공백이다 |
| **Objective** | `stop`과 `verdict` 키가 노출되고 과거 generation 기록은 무시된다 |
| **Target Files** | `run_loop.py` `_status_report()` |
| **Preconditions** | `M-B06-01` |
| **Input Type** | `control: Path`, `state: CoordinatorState`, `lock: LockInfo \| None` |
| **Input Validation** | 없음 |
| **Output Type** | `dict[str, object]` — `stop`(선택), `verdict`(필수) |
| **Output Validation** | `verdict ∈ {RUNNING, STOPPED_RESUMABLE, STOPPED_TERMINAL, BLOCKED_ON_USER, COMPLETED}` |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `event = read_latest_stop_event(control)` → 2. `current = event if event is not None and event.generation == state.generation else None` → 3. `current`가 있으면 `value["stop"] = {...}` → 4. verdict 판정: `state ∈ {READY_FOR_MERGE, REJECTED}` → `COMPLETED`; `state ∈ {HUMAN_GATE, USER_DECISION_REQUIRED}` → `BLOCKED_ON_USER`; `state is FAILED` → `STOPPED_TERMINAL`; `current is not None and current.resumable` → `STOPPED_RESUMABLE`; `lock is not None and lock.alive and lock.run_id == run_id` → `RUNNING`; 그 외 → `STOPPED_RESUMABLE` → 5. `value["verdict"] = verdict` |
| **Tests** | `T-B06-05` verdict 5종 / `T-B06-06` staleness / `T-B06-07` `IN_PROGRESS` + 락 없음 → `STOPPED_RESUMABLE` / `T-B06-08` 기존 키 무변경 / `T-B06-09` live v1 run 회귀 |
| **Rollback** | 두 키 제거 |

**락 판정 위치:** 기존 `_status_report`는 lock을 manifest의 worktree 경로가 있을
때만 조회한다(`run_loop.py:2078`). verdict 판정은 그 이후에 배치하여 동일한 `lock`
값을 재사용한다.

---

### M-B07-01 — `force-fail` 서브커맨드

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B07-01` |
| **Parent Block** | `B-07` |
| **Name** | 운영자 명시 종료 |
| **Rationale** | Layer 1이 `INTERRUPTED`에서 상태를 보존하므로 의도적 종료 수단이 필요하다(`OQ-4`) |
| **Objective** | 살아 있는 코디네이터가 없는 run을 `FAILED`로 전이하고 근거를 남긴다 |
| **Target Files** | `run_loop.py`: `SUBCOMMANDS`, `_print_subcommand_help`, `main()`, 신규 `_force_fail()` |
| **Preconditions** | `M-B06-01` |
| **Input Type** | `harness_root: Path`, `run_id: str`, `reason: str` |
| **Input Validation** | `run_id` nonempty, `reason` nonempty. control 디렉터리 존재 |
| **Output Type** | `int` 종료 코드 |
| **Output Validation** | 성공 시 `state.state is LoopState.FAILED` |
| **Exceptions** | 없음. 모든 거부를 JSON + `EXIT_PREFLIGHT`로 보고 |
| **Side Effects** | generation 커밋 1회, `force_failed` 이벤트 append |
| **Detailed Pseudocode** | 1. `argparse`로 `--run-id`, `--reason` 파싱 → 2. control 디렉터리 확인, 없으면 거부 → 3. `state, ledger, _ = load_committed(control)` → 4. `state.state in TERMINAL_STATES`면 거부 → 5. `read_manifest`로 worktree 경로 획득 후 `inspect_lock`; `lock.alive and lock.run_id == run_id`면 거부 → 6. `create_run_workspace(harness_root, run_id, step_id, resume=True)`로 workspace 구성 → 7. `GenerationController(workspace, state, ledger).commit(stage=TRANSITION_COMMITTED, active=None, reason=f"force-fail: {reason}", signal=SignalKind.ABORT, state_value=LoopState.FAILED, status=RunStatus.FAILED)` → 8. `append_event(control, FORCE_FAIL_EVENT_KIND, {"reason": reason, "generation": ...})` → 9. JSON 출력 후 `EXIT_READY` |
| **Tests** | `T-B07-01` 살아 있는 락 거부 / `T-B07-02` `FAILED` 커밋 + 이벤트 / `T-B07-03` 이미 terminal이면 거부 / `T-B07-04` 미지 run 거부 / `T-B07-05` help 문자열 |
| **Rollback** | 서브커맨드 제거 |

**`step_id` 확보:** `create_run_workspace`가 `step_id`를 요구한다. `state.active`가
`None`일 수 있으므로 `state.active.step_id if state.active else "g0000-force-fail"`을
사용한다. 이 step 디렉터리는 생성만 되고 사용되지 않는다.

---

### M-B08-01 — 테스트 매트릭스

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-01` |
| **Parent Block** | `B-08` |
| **Name** | `tests/test_failure.py` 신설 및 기존 테스트 확장 |
| **Rationale** | 예외 경로가 산출물이므로 정상 경로 테스트는 아무것도 보증하지 않는다 |
| **Objective** | `T-B01-01`~`T-B07-05`가 구현되고 통과한다 |
| **Target Files** | `tests/test_failure.py`(신규), `tests/test_cli_commands.py`, `tests/test_cli.py`, `tests/test_coordinator.py` |
| **Preconditions** | `M-B01-01`~`M-B07-01` |
| **Input Type** | 없음 |
| **Input Validation** | 없음 |
| **Output Type** | 테스트 모듈 |
| **Output Validation** | 실제 Orca 호출·프로세스 실행 없음 |
| **Exceptions** | 없음 |
| **Side Effects** | 임시 디렉터리 |
| **Detailed Pseudocode** | 1. 36종 분류표를 `subTest` 파라미터화 → 2. `mock.patch`로 `_run_loop`에 예외 주입 → 3. `FakeOrcaClient` 재사용 → 4. JSONL fixture로 verdict 검증 |
| **Tests** | 자기 자신 |
| **Rollback** | 신규 파일 삭제 |

---

### M-B08-02 — 회귀와 실환경 확인

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-02` |
| **Parent Block** | `B-08` |
| **Name** | 전체 회귀 |
| **Rationale** | `except Exception` 확장이 루프 흐름을 바꾸므로 기존 동작 보존을 실측해야 한다 |
| **Objective** | 전체 스위트가 baseline(332 passed) 이상 통과하고, live run의 `status`가 회귀 없이 verdict를 낸다 |
| **Target Files** | 없음(실행) |
| **Preconditions** | `M-B08-01` |
| **Input Type** | 없음 |
| **Input Validation** | 없음 |
| **Output Type** | 테스트 결과 및 Phase 4 보고서 |
| **Output Validation** | 신규 실패 0 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `pytest -q` → 2. live run에 `status --json` 실행해 `verdict` 확인 → 3. 보고서 작성 |
| **Tests** | 해당 없음 |
| **Rollback** | 해당 없음 |

---

## 4. Implementation Order

```
M-B01-01 -> M-B01-02 -> M-B01-03
                            |
                            +-> M-B02-01 -> M-B03-01 -> M-B04-01
                            |                   |
                            |                   +-> M-B06-01 -> M-B06-02 -> M-B07-01
                            |
                            +-> M-B05-01 -> M-B05-02
                                                        |
                                              M-B08-01 -> M-B08-02
```

`M-B01-03`의 전수 테스트(`T-B01-05`)가 PASS하기 전에는 `M-B03-01`의 `TERMINAL`
분기를 활성화하지 않는다(Phase 2 §6).

---

## 5. Validation and Risks

- **Validation:** 본 Phase 작성 전 `load_committed`(`generation.py:288`),
  `create_run_workspace`(`workspace.py:93`), `SUBCOMMANDS`(`run_loop.py:1779`),
  `_status_report`의 lock 조회 위치(`run_loop.py:2078`)와 status 마감
  (`run_loop.py:2090-2092`), `operational_retry_result` 본문
  (`coordinator.py:821-848`), `append_event`(`session.py:44`)를 직접 확인했다.
- **Risks:**
  - `M-B05-02`가 `except ContractViolationError`를 `except Exception`으로 넓힌다.
    `T-B05-06`(기존 동작 보존)을 회귀 기준으로 삼는다.
  - `M-B03-01`의 `except BaseException`은 `KeyboardInterrupt`를 포착한다.
    `T-B03-04`가 전파를 고정한다.
- **Open Questions:** 없음.

---

## 6. Approval

- [ ] Micro Blocking approved
- [ ] Revision requested
- [ ] Permission granted to begin implementation

**Next phase after explicit approval:** Phase 4 — Code Implementation
