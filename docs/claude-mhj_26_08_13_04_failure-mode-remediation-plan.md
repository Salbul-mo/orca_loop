# Orca Loop 실패 모드 탐색 결과 및 개선 계획

**작성:** 2026-08-13
**상태:** `R-5`, `R-6` 구현 착수 승인됨. 나머지는 결정 대기
**대상 커밋 기준:** `a4367ae`

---

## 1. 설계 원칙

**"루프가 왜 멈췄는가"와 "run의 도메인 상태"를 분리한다.**

현재는 두 개념이 뒤섞여 있다. 정지 사유는 마크다운 보고서에만 남고, 도메인 상태는
갱신되지 않으며, 프로세스 생존 여부는 락에서 따로 추론해야 한다. 세 신호가 각자
놀기 때문에 `orca_loop_execution_rules.md` §9.5 같은 사람 규칙이 코드의 공백을
메우고 있다.

### 1.1 폐기된 초기 제안

탐색 초기에 "최상위에 `except Exception`을 두고 `FAILED`를 커밋한다"를 제안했으나
**이는 틀린 처방이며 채택하지 않는다.**

근거: `ResumeBlockedError`가 발생하는 조건은 코드상 4개뿐이다.

| 위치 | 조건 |
| --- | --- |
| `run_loop.py:497` | worktree 드리프트 (write 상태에서) |
| `run_loop.py:617` | 현재 Orca 터미널이 죽어 있음 |
| `run_loop.py:652` | Orca Run 바인딩이 없는 legacy run |
| `run_loop.py:758` | 워커가 펜싱되지 않음 |

**`FAILED` 상태를 막는 코드는 없다.** §11의 "FAILED run을 resume하지 않는다"는
문서 규칙일 뿐이다. 따라서 모든 예외를 `FAILED`로 커밋하면 Orca CLI 일시 오류
하나가 *문서상 재개 금지 + 코드상 재개 가능*인 모호한 run을 만든다. 회복력이
올라가는 것이 아니라 내려간다.

---

## 2. 위험 카탈로그

### F-1 예외 분류 누락

`main()`의 최상위 핸들러(`run_loop.py:2310-2353`)가 잡는 예외는 11종이다. 실제로는
**25개 클래스가 그물을 빠져나간다.**

```
contracts.py    ContractViolationError, ProvenanceError
escalation.py   DecisionReportError
generation.py   GenerationError, GenerationMismatchError
guards.py       GuardPathBoundaryError, GuardScopeViolationError
ledger.py       LedgerIntegrityError, InvalidRoundError
notify.py       NoticeDeliveryError
profiles.py     LaunchProfileError
readonly.py     ReadOnlyMirrorError
roles.py        TemplateContractError
snapshot.py     SnapshotError, SnapshotChangedError, GitCommandError,
                SnapshotPathBoundaryError
testrunner.py   TestExecutionError, TestPolicyError
transport.py    InputStagingError, ScopeViolationError,
                TransportPathBoundaryError, TransportProvenanceError
workspace.py    PathBoundaryError, WorkspaceError
```

중간 계층에도 없다. `coordinator.py`는 `KeyError`/`JSONDecodeError`만 잡고,
`_execute_worker` 경로(`run_loop.py:1204~1688`)에는 `GateProtocolError` 하나뿐이다.
`ContractViolationError`만 루프 안에서 잡힌다(`:1688`).

**결과:** 스코프 위반, 스냅샷 드리프트, 템플릿 파손 시 stack trace로 프로세스가
죽고 실패 보고서조차 남지 않는다.

### F-2 잡힌 예외도 상태를 전이하지 않음

`_report_failure()`는 `render_failure_report()`를 호출할 뿐이고 그것은 마크다운
파일만 쓴다(`reporting.py:380`). durable state는 건드리지 않는다. `ABORT`를
커밋하는 경로는 `_run_loop` 내부의 총 타임아웃·전이 상한 두 곳뿐이다.

### F-3 예외 메시지 문자열 매칭

```python
except GateProtocolError as exc:
    if "exactly one resolved gate" in str(exc):   # run_loop.py:1472
        return False
    raise
```

`wait_gate_resolution`은 `len(matches) != 1`일 때 단일 메시지를 던진다
(`escalation.py`). 즉 **0건(정상 대기)과 2건 이상(실제 프로토콜 위반)이 같은
예외로 합쳐지고, 호출부는 둘 다 "대기 중"으로 삼킨다.** 단순 취약성이 아니라
잠재 결함이다.

### F-4 `operational_retry_limit`이 사실상 미연결

재시도 카운터를 올리는 유일한 입구가 `ContractViolationError`이다
(`run_loop.py:1688-1696`). 가장 재시도가 유효한 일시적 외부 오류
(`OrcaCommandError`)는 재시도 없이 run을 끝낸다.

### F-5 타임아웃 상호작용

기본값 `step=900_000`(15분), `total=7_200_000`(2시간)
(`config.py:64-66`). 총 타임아웃 검사가 반복 시작 지점에만 있어(`:1594`) 실제
벽시계 상한은 `total + step`이다. 또한 느린 스텝 8개로 2시간이 소진되는데,
계획 5라운드 + 코드 5라운드 구조의 스텝 수는 쉽게 20개를 넘는다. §11이 resume에서
타임아웃 변경을 금지하므로 회복 경로는 새 run뿐이다.

### F-6 락 PID 재사용

`inspect_lock`의 생존 판정이 `pid_alive(pid)`뿐이다(`locking.py:120`). 락 파일에
`started_at_ns`가 기록되지만(`:186`) 판정에 쓰이지 않는다. PID 재사용 시 죽은 락이
영구히 살아있어 보여 `--force-unlock` 없이는 resume이 불가능하고, 반대로
`--force-unlock` 오용은 코디네이터 2중 실행을 만든다.

### F-7 알림 durable 쓰기 실패

`NoticeDeliveryError`(`notify.py:196,198,222`)도 F-1 목록에 포함된다. 채널 실패는
전부 삼켜지지만 durable 증거 쓰기 실패는 의도적으로 전파되며, 그 예외가 최상위에서
잡히지 않는다. 기존 `ensure_user_decision_notice`의 `DecisionReportError`도 같은
위치에 이미 노출돼 있었다.

### 심각도

| ID | 빈도 | 회복 |
| --- | --- | --- |
| `F-1` | 높음 | 수동 |
| `F-2` | `F-1`/`F-4` 발생 시 항상 | 수동 판정 필요 |
| `F-4` | 중 | 새 run |
| `F-5` | 중 | 새 run만 가능 |
| `F-3` | 낮음 (변경 시 즉시) | — |
| `F-6` | 낮음 | `--force-unlock` |
| `F-7` | 낮음 | 수동 |

---

## 3. 개선 항목

### Wave 1 — 관측 가능성 (선행 필수)

#### `R-1` durable stop record + `status` 판정

| Field | 내용 |
| --- | --- |
| 해결 | `F-2` |
| 변경 | `control/stop.json` 신규 — `{reason, classification, at, resumable, generation}`. `status`가 이것과 락 생존을 결합해 단일 판정 출력 |
| 근거 | `status`는 현재 `lock.run_id != run_id and lock.alive`일 때만 blocker를 낸다(`run_loop.py:2088`). **`IN_PROGRESS` + 락 없음**(프로세스 사망)은 아무것도 보고하지 않는다 |
| 왜 먼저 | 나머지 수정의 검증이 "실제로 무엇이 터졌는가"에 의존한다. 이 변경만은 루프 동작을 바꾸지 않는다 |
| 위험 | 낮음 |
| 검증 | 프로세스 강제 종료 후 `status`가 interrupted를 단정하는지 |

### Wave 2 — 실패 경계

#### `R-2` 최상위 예외 경계와 분류

| Field | 내용 |
| --- | --- |
| 해결 | `F-1`, `F-7` |
| 분류 A (terminal) | 재실행해도 반복되는 계약·무결성 위반 → 도메인 상태 `FAILED` 커밋 |
| 분류 B (interrupted) | 외부·일시·환경 → 상태 보존, `stop.json`에 `resumable=true` |
| 의존 | `R-1` |
| 위험 | **중.** 분류 오류가 재개 가능한 run을 죽이거나 그 반대 |
| 검증 | 25개 클래스 분류 테이블 테스트 + fault injection |

#### `R-3` `--force-fail` 명시 종료

`R-2`가 상태를 보존하는 만큼 운영자가 의도적으로 run을 종료할 수단이 필요하다.
현재는 없다.

### Wave 3 — 재시도 분류

#### `R-4` 일시 오류를 재시도 경로에 연결

| Field | 내용 |
| --- | --- |
| 해결 | `F-4` |
| 변경 | 분류 B 중 in-process 재시도가 안전한 것을 `operational_retry_result`로 유도 |
| **주의** | 뮤테이션은 재시도 대상이 아니다. `execute_mutation`이 이미 request ID 저널로 replay를 보장하므로(`orca_client.py:212`) 이중 재시도는 금물 |
| 의존 | `R-2`의 분류표 |
| 위험 | 중. 한도 도달 시 `USER_DECISION_REQUIRED`로 에스컬레이션되므로 무한 루프는 아님 |

### Wave 4 — 국소 결함 (독립, 병렬 가능)

#### `R-5` 게이트 대기를 타입으로 표현

`F-3` 해결. `wait_gate_resolution`이 미해결 시 `None`을 반환하고, 2건 이상일 때만
`GateProtocolError`를 던지도록 분리한다. 호출부의 문자열 매칭을 제거한다.
**부수 효과: 다중 resolved gate가 조용히 삼켜지던 결함이 실제로 표면화된다.**

#### `R-6` 락 신원에 프로세스 시작 시각 반영

`F-6` 해결. 락의 `started_at_ns`보다 **나중에** 시작된 프로세스가 그 PID를 갖고
있으면 PID 재사용이므로 stale로 판정한다.

구현 가능성 확인 완료: `pid_alive`가 이미 Windows에서 ctypes `OpenProcess`를 쓰므로
(`locking.py:55-68`) 같은 경로에서 `GetProcessTimes`를 호출할 수 있다. 신규 의존성
불필요. 비Windows는 `None`을 반환해 현행 동작으로 보수적 폴백한다.

### Wave 5 — 시간 예산

#### `R-7` 총 타임아웃 회계

`F-5` 해결. 선택지: (a) 기본값 상향, (b) 게이트 대기 시간을 총예산에서 제외,
(c) 스텝 단위 예산으로 전환. 의미 변경이므로 승인 필요.

---

## 4. 순서

```
R-1 (관측)  →  R-2 (경계·분류)  →  R-4 (재시도)
                    └─→ R-3 (명시 종료)
R-5, R-6  (독립 — 병렬 가능)
R-7  (별도 판단)
```

---

## 5. 미결정 사항

1. **`R-2` 분류 기준** — 25개 예외의 terminal/interrupted 분류표 승인 필요.
   계획 전체의 중심이다.
2. **`FAILED`의 코드 강제** — §11을 코드로 옮길지 문서 규칙으로 둘지. 옮기면
   `R-2` 분류 실수의 비용이 커진다.
3. **`R-7` 방향** — (a)/(b)/(c).

---

## 6. 진행 상태

| 항목 | 상태 |
| --- | --- |
| `R-5` | **완료** — `escalation.py`, `run_loop.py`, `tests/test_escalation.py` |
| `R-6` | **완료** — `locking.py`, `tests/test_locking.py` |
| `R-1`, `R-2`, `R-3`, `R-4`, `R-7` | 미착수. `R-1`~`R-4`와 `R-7`은 durable 스키마 추가와 실패 의미론 변경을 포함하므로 `staged-development` 대상 |

### 6.1 `R-5` 구현 결과

`wait_gate_resolution`의 반환형을 `HumanDecision | None`으로 바꾸고, 0건은 `None`,
2건 이상은 `GateProtocolError("... found N")`로 분리했다. 호출부
(`run_loop.py:1464`)의 문자열 매칭을 제거했다.

**확인된 부수 효과:** 변경 전에는 0건과 2건 이상이 동일 메시지를 던졌고 호출부가
둘 다 "대기 중"으로 삼켰다. 이제 다중 resolved gate는 실제로 표면화된다.
회귀 테스트 `test_ambiguous_gate_identity_is_not_mistaken_for_pending`가 이를 고정한다.

### 6.2 `R-6` 구현 결과

`process_started_at_ns(pid)`를 추가하고(Windows `GetProcessTimes`, 그 외 `None`),
`_owner_alive(pid, started_at_ns)`가 생존 판정을 담당한다. 락에 기록된
`started_at_ns`보다 `PID_REUSE_TOLERANCE_NS`(2초)를 넘겨 나중에 시작된 프로세스가
그 PID를 갖고 있으면 PID 재사용으로 보고 stale 처리한다.

`inspect_lock`이 이 판정을 쓰고, `acquire_run_lock`은 `info.alive`를 경유하므로
자동으로 반영된다. 시작 시각을 알 수 없으면 `True`(현행 동작)로 폴백하므로
`acquire_run_lock` docstring의 안전 방향 — "false negative는 run을 거부할 뿐
코디네이터 2중 실행을 허용하지 않는다" — 이 보존된다.

**한계:** 비Windows에서는 `process_started_at_ns`가 `None`을 반환하므로 PID 재사용
탐지가 동작하지 않는다. Linux `/proc/<pid>/stat` 경로는 이 환경에서 검증할 수 없어
구현하지 않았다.
