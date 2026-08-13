# Task Report: Orca Loop User Decision Immediate Notification

**Current Phase:** 2. Macro Blocking
**Status:** Waiting for Explicit User Approval

**Baseline:** `claude-mhj_26_08_13_01_phase1-system-design-user-decision-notification.md` (approved)

---

## 1. Context & Objective

- **Problem:** 승인된 4채널 알림 설계를 독립 검증 가능한 구현 경계로 분할해야 한다.
- **Goal:** 각 블록이 하나의 책임과 자체 완료 판정 기준을 갖도록 8개 Macro Block으로 분해한다.
- **Scope:** `orca_loop/models.py`, `orca_loop/escalation.py`, `orca_loop/notify.py`(신규),
  `orca_loop/config.py`, `run_loop.py`, `tests/fakes.py`, `tests/test_escalation.py`,
  `tests/test_cli.py`, `tests/test_cli_commands.py`, `tests/test_notify.py`(신규).
- **Out of Scope:** Phase 1의 Out of Scope 전부 승계.

---

## 2. Phase 2에서 새로 확정된 사실

Phase 1의 미결 항목을 실호출로 해소했다.

| ID | 항목 | 결과 |
| --- | --- | --- |
| `R-5` 해소 | `orca file open`이 gitignore된 `runs/` 경로를 여는가 | **가능**. `{"relativePath":"runs/.../user-decision.md","opened":true}` |
| 신규 `F-1` | `file open` 정확한 flag 집합 | `help, json, pairing-code, environment, path, worktree` |
| 신규 `F-2` | `terminal switch` 정확한 flag 집합 | `help, json, pairing-code, environment, terminal` |
| 신규 `F-3` | `tests/fakes.py`의 `VALID_FLAGS`가 미등록 명령 argv를 `AssertionError`로 거부 | 신규 채널은 등록하지 않으면 **테스트가 즉시 실패**한다 |
| 신규 `F-4` | `assert_supported_argv()`는 non-`--` 토큰으로 prefix를 뽑으므로 `file open <path>`의 위치 인자를 허용 | `("file","open")` prefix 매칭 정상 |

`F-3`은 `fakes.py` 주석이 명시하듯 과거 `gate-create --run` 회귀가 green suite를
통과한 원인이었다. 신규 채널의 flag 등록은 선택이 아니라 필수 작업이다.

---

## 3. Core Deliverables — Macro Blocks

### B-01 — 채널별 durable delivery 스키마와 v1 마이그레이션

| Field | Definition |
| --- | --- |
| **Block ID** | `B-01` |
| **Name** | `UserDecisionNoticeDelivery` schema v2 and read-time migration |
| **Rationale** | 멱등 판정과 채널별 증거의 **전제 조건**. 이 블록 없이는 어떤 채널도 중복 발송을 막을 수 없다. 또한 live v1 파일이 디스크에 실재하므로 마이그레이션이 선행되어야 한다. |
| **Objective** | v2 레코드가 round-trip하고, 실제 디스크의 v1 파일이 `ORCA_BOARD` 단일 채널로 무손실 승격된다. |
| **Scope** | `orca_loop/models.py`(`NoticeChannel`, `NoticeChannelDelivery`, `UserDecisionNoticeDelivery` 재정의, `SKIPPED` 추가), `orca_loop/escalation.py`(parse/write) |
| **Exclusions** | 채널 실행 로직, `user-decision-request.json` 스키마(v1 유지), config, status 출력 |
| **Dependencies** | 없음 |
| **Input** | `Path` (control dir) → `dict[str, object]` decoded JSON, `schema_version ∈ {1, 2}` |
| **Output** | `UserDecisionNoticeDelivery | None`; 쓰기는 항상 v2 |
| **Side Effects** | `control/user-decision-notice-delivery.json` atomic write |
| **Failure Modes** | 파손 JSON·미지원 version·불변식 위반 → `DecisionReportError`; atomic write 실패 → `DecisionReportError` 전파 |
| **Validation** | `V-B01-01` v2 round-trip / `V-B01-02` 실디스크 v1 fixture 마이그레이션 / `V-B01-03` unknown version 거부 / `V-B01-04` 채널 중복·`detail` 불변식 |
| **High-Level Pseudocode** | 1. 파일 읽기·root 타입 검증 → 2. `schema_version` 분기 → 3. v1이면 flat 레코드를 `ORCA_BOARD` 채널 1건으로 승격(`error`→`detail`, DELIVERED는 `detail=None` 정규화) → 4. v2면 exact field 검증 후 채널 배열 파싱 → 5. 채널 유일성·상태별 `detail` 불변식 검증 → 6. typed dataclass 반환 |

---

### B-02 — 알림 오케스트레이션 계층과 멱등 게이트

| Field | Definition |
| --- | --- |
| **Block ID** | `B-02` |
| **Name** | `orca_loop/notify.py` — `NoticeAnnouncer` |
| **Rationale** | 채널 선택·멱등 판정·결과 병합은 채널 구현과 독립된 정책이다. `escalation.py`(961줄, 게이트 프로토콜 소유)에 전송 책임을 섞지 않는다. |
| **Objective** | 채널 목록을 받아 순서대로 시도하고, 이미 `DELIVERED`인 채널은 재실행하지 않으며, 결과를 기존 레코드와 병합해 1회 write한다. |
| **Scope** | `orca_loop/notify.py`(신규): `NoticeAnnouncer`, `ChannelOutcome`, `ToastEmitter` 프로토콜 정의 |
| **Exclusions** | 개별 채널의 실제 명령 실행(B-03/B-04), config 파싱(B-05), run_loop 호출부(B-06) |
| **Input** | `UserDecisionNotice`, `tuple[NoticeChannel, ...]`, `OrcaClient`, `Path`(control dir), `str`(worktree_selector), `str`(coordinator_handle) |
| **Output** | `UserDecisionNoticeDelivery` (병합 후 최종 상태) |
| **Side Effects** | 채널 부작용은 위임. 자체적으로는 delivery 파일 write 1회 |
| **Failure Modes** | 채널 예외는 포착 → `FAILED` 기록 → 다음 채널 계속. 전 채널 실패해도 정상 반환. delivery write 실패만 전파 |
| **Validation** | `V-B02-01` 멱등(2회 호출 시 2번째는 채널 실행 0회) / `V-B02-02` 채널 1개 실패가 나머지를 막지 않음 / `V-B02-03` 전 채널 실패해도 예외 없음 / `V-B02-04` 병합 시 기존 채널 결과 보존 |
| **High-Level Pseudocode** | 1. 기존 delivery 로드(B-01) → 2. `delivered = {c.channel for c in existing if c.status is DELIVERED}` → 3. 요청 채널 순회: `delivered`면 건너뜀(레코드 보존), 아니면 dispatch → 4. 각 결과를 `NoticeChannelDelivery`로 수집 → 5. 기존+신규 병합(채널 키 기준 최신 우선) → 6. `write_user_decision_notice_delivery()` 1회 → 7. 최종 레코드 반환 |

---

### B-03 — Orca 인앱 채널 (L2) 과 fake argv 계약 등록

| Field | Definition |
| --- | --- |
| **Block ID** | `B-03` |
| **Name** | `ORCA_BOARD`, `ORCA_FILE_OPEN`, `ORCA_TERMINAL_FOCUS` 채널 |
| **Rationale** | 세 채널 모두 `OrcaClient.call()` 단일 경로를 쓰므로 하나의 검증 경계로 묶는다. `F-3`에 따라 fake의 flag 등록이 동반되지 않으면 테스트가 실패하므로 같은 블록에 포함한다. |
| **Objective** | 세 채널이 각각 정확한 argv를 bounded 호출로 발행하고, 적용 불가 조건에서 `SKIPPED`를 반환한다. |
| **Scope** | `orca_loop/notify.py`(채널 3종 구현), `tests/fakes.py`(`VALID_FLAGS`에 `("file","open")`, `("terminal","switch")` 추가) |
| **Exclusions** | 토스트(B-04), 멱등 정책(B-02) |
| **Input** | `UserDecisionNotice`, `OrcaClient`, `worktree_selector: str`, `coordinator_handle: str` |
| **Output** | `ChannelOutcome(status, detail)` |
| **Side Effects** | Orca 보드 metadata 갱신 / 에디터 탭 열림 / 터미널 탭 포커스 이동 |
| **Failure Modes** | `OrcaCommandError`·`OrcaTimeoutError` → `FAILED(detail=str(exc)[:2000])`; `coordinator_handle` 빈 값 → `SKIPPED`; report 파일 부재 → `SKIPPED` |
| **Validation** | `V-B03-01` 각 채널 argv 정확성(`--worktree`/`--terminal`/`path` 위치인자) / `V-B03-02` fake `VALID_FLAGS` 등록으로 argv 거부 없음 / `V-B03-03` timeout 30_000ms 고정 / `V-B03-04` precondition 미충족 시 `SKIPPED` |
| **High-Level Pseudocode** | `ORCA_BOARD`: 기존 `_record_worktree_metadata` 로직 이관 → `worktree set --worktree S --workspace-status in-review --comment <bounded>`. `ORCA_FILE_OPEN`: report 존재 확인 → `file open <report_path> --worktree S`. `ORCA_TERMINAL_FOCUS`: handle 존재 확인 → `terminal switch --terminal H`. 각각 예외 포착 후 `ChannelOutcome` 반환 |

---

### B-04 — OS 토스트 채널 (L3) 과 주입 안전 페이로드

| Field | Definition |
| --- | --- |
| **Block ID** | `B-04` |
| **Name** | `OS_TOAST` — WinRT toast via bounded PowerShell |
| **Rationale** | 유일하게 Orca 외부로 나가는 경로이자 유일한 신뢰 경계다. `run_id`가 사용자 지정 이름에서 파생되므로 명령 주입·XML 주입 방어가 이 블록의 핵심 책임이다. |
| **Objective** | Windows에서 토스트가 실제 표시되고, 어떤 notice 문자열도 argv나 스크립트 본문에 삽입되지 않는다. |
| **Scope** | `orca_loop/notify.py`(`OS_TOAST` 채널, `PowerShellToastEmitter`, XML 빌더) |
| **Exclusions** | non-Windows 네이티브 알림, 토스트 버튼 상호작용 |
| **Input** | `UserDecisionNotice`, `ToastEmitter` |
| **Output** | `ChannelOutcome` |
| **Side Effects** | `powershell.exe` 단발 subprocess(10s bound), OS 알림 센터 항목 + 알람음 |
| **Failure Modes** | 비Windows/PowerShell 부재 → `SKIPPED("platform not supported")`; 실행 실패·타임아웃 → `FAILED`; 프로세스 미종료 → `_terminate_tree()` 선례로 강제 종료 |
| **Validation** | `V-B04-01` argv에 notice 파생 문자열 **미포함**(주입 run 이름으로 검증) / `V-B04-02` XML escape(`<`, `&`, `"`, 개행) / `V-B04-03` 길이 상한(title 120, body 300) / `V-B04-04` 비Windows `SKIPPED` / `V-B04-05` timeout `FAILED` |
| **High-Level Pseudocode** | 1. `platform.system() != "Windows"` → `SKIPPED` → 2. `shutil.which("powershell")` 없으면 `SKIPPED` → 3. title/body 구성 후 개행·제어문자 정규화 및 절단 → 4. `xml.sax.saxutils.escape` 적용해 toast XML 문자열 생성 → 5. `env = os.environ.copy(); env["ORCA_LOOP_TOAST_XML"] = xml` → 6. **고정 스크립트 문자열**로 `powershell -NoProfile -NonInteractive -Command <FIXED>` 실행(`shell=False`, 10s) → 7. 종료코드 0이면 `DELIVERED`, 아니면 `FAILED(stderr tail)` |

고정 스크립트는 `$env:ORCA_LOOP_TOAST_XML`만 참조하며 notice 값을 문자열 결합하지 않는다.

---

### B-05 — 설정과 CLI 표면

| Field | Definition |
| --- | --- |
| **Block ID** | `B-05` |
| **Name** | `LoopConfig.notice_channels` and `--notice-channels` |
| **Rationale** | `R-1`(포커스 탈취) 완화 수단이자 `OQ-2` 재검토 여지를 코드가 아닌 설정으로 흡수한다. |
| **Objective** | 채널 집합을 CLI로 선택할 수 있고, 미지정 시 4채널 전체가 활성화되며, 기존 `LoopConfig` 생성자 호출이 그대로 동작한다. |
| **Scope** | `orca_loop/models.py`(`LoopConfig`에 기본값 필드 추가), `orca_loop/config.py`(파서·검증) |
| **Exclusions** | 채널 실행, 알림 정책 |
| **Input** | `str | None` (예: `"board,file-open,terminal-focus,os-toast"`, `"none"`) |
| **Output** | `tuple[NoticeChannel, ...]` (중복 제거, 선언 순서 보존) |
| **Side Effects** | 없음 |
| **Failure Modes** | 미지 채널명·빈 토큰 → `ConfigurationError`(유효 목록 포함) |
| **Validation** | `V-B05-01` 기본값 4채널 / `V-B05-02` 부분 선택 / `V-B05-03` `none` → 빈 tuple / `V-B05-04` 미지 이름 거부 / `V-B05-05` 기존 `LoopConfig(...)` 호출 무변경 통과 |
| **High-Level Pseudocode** | 1. flag 미지정 → 기본 4채널 반환 → 2. `"none"` → `()` → 3. 콤마 분리·strip → 4. 별칭 맵(`board`→`ORCA_BOARD` 등)으로 변환, 미지 이름은 `ConfigurationError` → 5. 순서 보존 중복 제거 후 tuple 반환 |

---

### B-06 — run_loop 통합과 발행·해소 오케스트레이션

| Field | Definition |
| --- | --- |
| **Block ID** | `B-06` |
| **Name** | `_publish_user_decision_notice` / `_close_user_decision_notice` rewiring |
| **Rationale** | `_resume_gate()`가 resume마다 발행을 호출하므로(`run_loop.py:1470`) 통합 지점에서 멱등 계약이 실제로 성립하는지 확인해야 한다. 해소 시에는 board만 갱신해야 토스트·포커스가 재발하지 않는다. |
| **Objective** | 게이트 개시 시 설정된 전 채널이 1회 발행되고, resume 반복에도 재발송이 없으며, 해소 시 `ORCA_BOARD`만 실행된다. |
| **Scope** | `run_loop.py`(`_record_worktree_metadata` 제거·이관, `_publish_user_decision_notice`, `_close_user_decision_notice`) |
| **Exclusions** | status 출력(B-07), 채널 구현(B-03/B-04) |
| **Input** | `GenerationController`, `OrcaClient`, `report_path: Path`, `GateBinding` |
| **Output** | `UserDecisionNotice` (발행) / `None` (해소) |
| **Side Effects** | B-02~B-04에 위임 |
| **Failure Modes** | 알림 실패는 run에 영향 없음. `gate_binding is None`에서 발행 시도 → 기존 `OrcaLoopError` 유지 |
| **Validation** | `V-B06-01` 발행 1회에 전 채널 실행 / `V-B06-02` resume 3회에도 채널 실행 총 1회 / `V-B06-03` 해소 시 `ORCA_BOARD`만 / `V-B06-04` 전 채널 실패해도 `wait_gate_resolution` 정상 진입 / `V-B06-05` 기존 `test_cli.py` 통과 |
| **High-Level Pseudocode** | 발행: 1. `ensure_user_decision_notice()` → 2. `NoticeAnnouncer(config.notice_channels).announce(...)` → 3. notice 반환. 해소: 1. `resolve_user_decision_notice()` → `None`이면 종료 → 2. 상태에 따라 `completed`/`in-progress` 결정 → 3. `ORCA_BOARD` 채널만 강제 재실행(멱등 게이트 우회, 상태 반영이 목적) |

---

### B-07 — status 출력 계약 갱신

| Field | Definition |
| --- | --- |
| **Block ID** | `B-07` |
| **Name** | `_status_report()` per-channel delivery exposure |
| **Rationale** | `OQ-1`에서 승인된 외부 계약 변경. 기존 flat `status`/`error` 키가 채널 배열로 바뀐다. |
| **Objective** | `status --json`이 채널별 결과를 노출하고, 불일치는 기존과 동일하게 blocker로 승격된다. |
| **Scope** | `run_loop.py` `_status_report()` |
| **Exclusions** | notice 자체 검증 로직(기존 유지) |
| **Input** | `Path`(control dir), `CoordinatorState` |
| **Output** | `dict[str, object]` — `user_decision_notice_delivery: {request_id, attempted_at, channels: [{channel, status, attempted_at, detail}]}` |
| **Side Effects** | 없음(읽기 전용) |
| **Failure Modes** | 파손 delivery → `notice_problems` + blocker(기존 동작 보존) |
| **Validation** | `V-B07-01` 채널 배열 노출 / `V-B07-02` request_id 불일치 blocker 유지 / `V-B07-03` v1 파일 입력 시 마이그레이션 결과 노출 / `V-B07-04` `test_cli_commands.py` 갱신 |
| **High-Level Pseudocode** | 1. `read_user_decision_notice_delivery()` → 2. `None`이면 키 생략 → 3. notice와 `request_id` 대조, 불일치 시 problem 추가 → 4. 채널 배열 직렬화 → 5. `value["user_decision_notice_delivery"]` 설정 |

---

### B-08 — 회귀 및 통합 검증

| Field | Definition |
| --- | --- |
| **Block ID** | `B-08` |
| **Name** | Regression matrix and end-to-end verification |
| **Rationale** | 알림은 부작용 중심 기능이라 단위 테스트만으로는 실제 도달을 보증하지 못한다. |
| **Objective** | 전체 스위트가 baseline(299 passed) 이상으로 통과하고, Windows 실환경에서 토스트·파일 열기가 실제 동작함을 확인한다. |
| **Scope** | `tests/test_notify.py`(신규), `tests/test_escalation.py`, `tests/test_cli.py`, `tests/test_cli_commands.py` 갱신 |
| **Exclusions** | Orca 앱 UI 자동화 검증, CI 파이프라인 구성 |
| **Input** | 구현된 B-01~B-07 |
| **Output** | 테스트 결과 및 구현 보고서 |
| **Side Effects** | 테스트 실행 중 실제 토스트 **발생 안 함**(`ToastEmitter` 주입 seam) |
| **Failure Modes** | 신규 실패 → 원인 수정. 무관한 기존 실패 → 보고서에 분리 기록 |
| **Validation** | `V-B08-01` `pytest -q` 전체 PASS / `V-B08-02` 실제 v1 run 디렉터리로 `status` 실행 회귀 없음 / `V-B08-03` 수동 1회 실환경 토스트 확인 |
| **High-Level Pseudocode** | 1. 블록별 타깃 테스트 실행 → 2. 전체 스위트 실행 → 3. `status` 명령을 live v1 run에 대해 실행 → 4. 수동 실환경 확인 → 5. 구현 보고서 작성 |

---

## 4. Dependency Order

```
B-01  (스키마 · 마이그레이션)
  |
B-02  (오케스트레이션 · 멱등)
  |
  +---> B-03  (Orca 인앱 채널 + fake flags)
  |
  +---> B-04  (OS 토스트)
          |
B-05  (설정 · CLI)          <- B-01의 NoticeChannel에만 의존, B-02~04와 병행 가능
  |
B-06  (run_loop 통합)        <- B-02, B-03, B-04, B-05 전부 필요
  |
B-07  (status 출력)
  |
B-08  (회귀 · 실환경 검증)
```

임계 경로: `B-01 → B-02 → B-03/B-04 → B-06 → B-07 → B-08`.
`B-05`는 `B-01` 완료 후 언제든 착수 가능하다.

---

## 5. Implementation Boundary Rules

1. `user-decision-request.json` 스키마는 **변경하지 않는다**(v1 유지). 이번 변경은
   delivery 레코드에 한정한다.
2. `CoordinatorState`와 state machine은 변경하지 않는다.
3. 신규 production dependency를 도입하지 않는다.
4. 알림 실패는 절대 run을 중단시키지 않는다. durable 기록 실패만 전파한다.
5. 기존 `worktree set` 호출의 argv와 timeout(30_000ms)을 보존한다.
6. 소스 파일에는 `claude-mhj_` 접두를 적용하지 않는다(`OQ-4` 확정: import 경로 보존).

---

## 6. Validation and Risks

- **Validation:** Phase 2 작성 전 `orca file open` 실호출, `orca agent-context --json`의
  flag 집합, `tests/fakes.py`의 argv 거부 로직을 직접 확인해 블록 경계에 반영했다.
- **Risks:**
  - `B-04`가 가장 큰 신규 위험면이다. argv 주입 테스트(`V-B04-01`)가 PASS하기 전에는
    토스트 채널을 기본 활성 목록에 넣지 않는다.
  - `B-07`의 출력 형태 변경은 되돌리기 쉬우나 외부 소비자가 있으면 영향이 있다.
    현재 저장소 내 소비자는 `tests/test_cli_commands.py`뿐임을 확인했다.
- **Open Questions:** 없음. `OQ-1`~`OQ-4`는 Phase 1 승인으로 기본 제안대로 확정되었다.

---

## 7. Approval

- [ ] Macro Blocking approved
- [ ] Revision requested

**Next phase after explicit approval:** Phase 3 — Micro Blocking
