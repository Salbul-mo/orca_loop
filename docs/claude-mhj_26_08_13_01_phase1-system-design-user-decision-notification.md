# Task Report: Orca Loop User Decision Immediate Notification

**Current Phase:** 1. System Design
**Status:** Waiting for Explicit User Approval

---

## 1. Context & Objective

- **Problem:** 결정 게이트가 열려 run이 `USER_DECISION_REQUIRED`/`HUMAN_GATE`로 멈춰도
  사용자에게 능동적으로 전달되는 신호가 없다. 현재 유일한 외부 통지는
  `_record_worktree_metadata()`의 `orca worktree set --workspace-status --comment`이며,
  사용자가 Orca 보드를 직접 확인해야만 알 수 있는 pull 방식이다.
- **Goal:** 게이트가 열리는 순간 사용자에게 능동적으로(push) 도달하는 알림 경로를
  추가하고, 각 경로의 전달 결과를 durable 증거로 남긴다.
- **Scope:** `orca_loop/models.py`, `orca_loop/escalation.py`, `orca_loop/config.py`,
  `run_loop.py`, `tests/*`.
- **Out of Scope:** 외부 메신저(Slack/이메일/SMS), 새 production dependency,
  worker 역할·consensus 정책 변경, 미해결 게이트의 주기적 재알림(reminder 반복),
  non-Windows OS 네이티브 알림 구현, Orca 앱 자체 수정.

---

## 2. Current-State Analysis

### 2.1 확인된 사실 (검증 완료)

| 항목 | 사실 | 근거 |
| --- | --- | --- |
| 통지 코드 | 알림 관련 코드 없음. 유일한 외부 통지는 worktree metadata 1회 | `run_loop.py:1258-1294` |
| Orca CLI | `notify`/`alert` 계열 명령 **없음** | `orca --help`, `orca agent-context --json` 전수 검색 |
| Orca 주의환기 수단 | `terminal switch`, `file open`, `terminal create --focus` | `orca agent-context --json` |
| coordinator 핸들 | 런타임에 확보됨 | `models.py:1028 coordinator_handle`, `ORCA_TERMINAL_HANDLE` |
| worktree selector | state에 존재 | `models.py:1027 worktree_selector` |
| Windows 토스트 | WinRT `ToastNotificationManager` **실발송 성공** | 본 세션에서 실제 발송 검증 |
| BurntToast | 미설치 (불필요) | `Get-Module -ListAvailable` |
| bounded subprocess 선례 | 존재 | `orca_client.py:128-160`, `testrunner.py:144` |
| **live v1 데이터** | **실제 PENDING notice + v1 delivery 파일 존재** | `runs/codex-mhj_26_08_13_04_project-guide-luna/control/` |

### 2.2 현재 durable 스키마 (schema_version=1)

```json
// user-decision-notice-delivery.json  (실제 디스크 내용)
{"attempted_at":"2026-08-13T07:39:53.947899+00:00","error":null,
 "request_id":"notice-c45f93eaef1a23c72bdc02b8","schema_version":1,"status":"DELIVERED"}
```

단일 채널 전제이며 어떤 채널로 전달됐는지 표현할 수 없다.

### 2.3 현재 구조의 결함

1. **즉시성 없음** — pull 방식 단일 경로.
2. **채널 표현 불가** — `UserDecisionNoticeDelivery`가 flat 단일 레코드.
3. **재알림 폭주 위험** — `_resume_gate()`가 resume마다
   `_publish_user_decision_notice()`를 무조건 호출한다(`run_loop.py:1470`).
   현재는 comment 재기록이라 무해하지만, 토스트/포커스 이동을 붙이면
   resume마다 알림이 반복되고 UI 포커스를 빼앗는다. **채널 추가의 전제 조건으로
   "request_id당 채널별 1회" 멱등성이 필요하다.**

---

## 3. Goals

- **G-1** 게이트 개시 시점에 사용자에게 push 알림 도달.
- **G-2** Orca 활성/비활성 두 상황 모두 커버.
- **G-3** 채널별 전달 성공·실패·생략을 durable 증거로 기록.
- **G-4** 알림 실패가 run을 절대 중단시키지 않는다(best-effort).
- **G-5** request_id당 채널별 정확히 1회 발송(멱등).
- **G-6** 기존 v1 durable 레코드를 무손실 읽기 마이그레이션.

## 4. Non-Goals

- **NG-1** 알림 전달 보장(at-least-once/exactly-once 네트워크 의미론). durable
  authority는 여전히 `user-decision-request.json`이다.
- **NG-2** 미응답 게이트의 주기적 재촉 알림.
- **NG-3** 알림에서 직접 게이트를 해소하는 상호작용(토스트 버튼 등).
- **NG-4** macOS/Linux 네이티브 알림.

---

## 5. Proposed Architecture

### 5.1 채널 모델

알림을 **4개의 독립 채널**로 일반화한다. 기존 board 갱신도 채널 하나로 흡수한다.

| 채널 | 계층 | 수단 | 커버 상황 |
| --- | --- | --- | --- |
| `ORCA_BOARD` | L1 | `orca worktree set --workspace-status --comment` | 항상(기존 동작 보존) |
| `ORCA_FILE_OPEN` | L2 | `orca file open <report> --worktree <selector>` | Orca 사용 중 — 내용까지 전달 |
| `ORCA_TERMINAL_FOCUS` | L2 | `orca terminal switch --terminal <coordinator_handle>` | Orca 사용 중 — 위치까지 전달 |
| `OS_TOAST` | L3 | WinRT toast (PowerShell 경유), `scenario="reminder"` | Orca 비활성/자리 비움 |

### 5.2 컴포넌트 책임

```
run_loop.py
  _publish_user_decision_notice()      게이트 개시 시 알림 오케스트레이션
  _close_user_decision_notice()        해소 시 board 상태만 갱신
        |
        v
orca_loop/notify.py  (신규 모듈)
  NoticeAnnouncer                      채널 선택 + 멱등 판정 + 실행 + 기록
    ├─ _announce_orca_board()          OrcaClient 경유
    ├─ _announce_orca_file_open()      OrcaClient 경유
    ├─ _announce_orca_terminal_focus() OrcaClient 경유
    └─ _announce_os_toast()            ToastEmitter 경유 (주입 가능한 seam)
        |
        v
orca_loop/escalation.py
  read/write_user_decision_notice_delivery()   durable 채널별 증거 (schema v2)
```

**설계 원칙:** `escalation.py`는 durable 스키마의 소유자로 유지하고,
채널 실행 로직은 신규 `notify.py`로 분리한다. `escalation.py`가 이미 961줄이며
게이트 프로토콜 책임을 지고 있어 알림 전송 책임을 섞지 않는다.

> **주의:** Phase 3 문서(`codex-mhj_26_08_13_02`)는 "새 Python source module을 만들지
> 않는다"는 제약을 두었으나, 그것은 별개 과업(communication hardening)의 범위 제약이다.
> 본 과업에서 `notify.py` 신설이 부적절하다고 판단되시면 `escalation.py` 내부 배치로
> 변경하겠다 — **Open Question OQ-3** 참조.

### 5.3 채널 선택 규칙

```
enabled_channels = config.notice_channels        # 기본값: 전체 4개
for channel in enabled_channels:
    if already_delivered(request_id, channel):   # 멱등 게이트
        continue -> SKIPPED(reason=already-delivered)
    if not channel.applicable(runtime):          # 예: OS_TOAST on non-Windows
        record SKIPPED(reason=not-applicable)
        continue
    execute bounded; record DELIVERED | FAILED
```

---

## 6. Data Flow (End-to-End)

```
게이트 개시 (_ensure_gate / _resume_gate)
   |
   1. ensure_user_decision_notice()      -> user-decision-request.json (PENDING)   [권위]
   |
   2. read_user_decision_notice_delivery() -> 기존 채널별 결과 로드 (v1이면 마이그레이션)
   |
   3. NoticeAnnouncer.announce(notice)
   |     ├─ ORCA_BOARD          orca worktree set  (30s)
   |     ├─ ORCA_FILE_OPEN      orca file open     (30s)
   |     ├─ ORCA_TERMINAL_FOCUS orca terminal switch (30s)
   |     └─ OS_TOAST            powershell -Command <고정 스크립트> (10s)
   |           payload는 환경변수로만 전달 (argv 문자열 결합 금지)
   |
   4. write_user_decision_notice_delivery() -> 채널별 결과 병합 후 atomic write
   |
   5. run은 기존대로 wait_gate_resolution()으로 블로킹

게이트 해소 (_close_user_decision_notice)
   |
   1. resolve_user_decision_notice()  -> RESOLVED
   2. ORCA_BOARD 채널만 재실행 (completed/in-progress 상태 반영)
      -> 나머지 채널은 해소 시 발송하지 않는다
```

---

## 7. Input / Output Contracts

### 7.1 신규 타입 (`orca_loop/models.py`)

```python
class NoticeChannel(StrEnum):
    ORCA_BOARD = "ORCA_BOARD"
    ORCA_FILE_OPEN = "ORCA_FILE_OPEN"
    ORCA_TERMINAL_FOCUS = "ORCA_TERMINAL_FOCUS"
    OS_TOAST = "OS_TOAST"


class UserDecisionNoticeDeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"        # 신규 값


@dataclass(frozen=True)
class NoticeChannelDelivery:
    channel: NoticeChannel
    status: UserDecisionNoticeDeliveryStatus
    attempted_at: str          # ISO-8601, tz 필수
    detail: str | None         # FAILED는 오류, SKIPPED는 사유, DELIVERED는 None


@dataclass(frozen=True)
class UserDecisionNoticeDelivery:
    schema_version: int                            # 2
    request_id: str
    attempted_at: str
    channels: tuple[NoticeChannelDelivery, ...]    # 채널당 최대 1개, 채널명 유일
```

**불변식**
- `channels`의 `channel` 값은 중복 불가.
- `DELIVERED`는 `detail is None`, `FAILED`/`SKIPPED`는 `detail` 필수 nonempty.
- `attempted_at`(레코드)은 채널 중 최신 `attempted_at`과 같거나 이후.

### 7.2 스키마 마이그레이션 (v1 → v2)

읽기 시점 마이그레이션. 쓰기는 항상 v2.

```
v1 {schema_version:1, request_id, status, attempted_at, error}
      |
      v
v2 {schema_version:2, request_id, attempted_at,
    channels:[{channel:"ORCA_BOARD", status, attempted_at, detail:error}]}
```

v1의 단일 레코드는 의미상 board 갱신이므로 `ORCA_BOARD` 채널로 승격한다.
`status=DELIVERED`이면 `detail=None`으로 정규화한다.

### 7.3 설정 (`LoopConfig`)

`models.py:1072 LoopConfig`에 **기본값을 가진 필드**를 추가해 기존 생성자 호환을
유지한다.

```python
notice_channels: tuple[NoticeChannel, ...] = (
    NoticeChannel.ORCA_BOARD,
    NoticeChannel.ORCA_FILE_OPEN,
    NoticeChannel.ORCA_TERMINAL_FOCUS,
    NoticeChannel.OS_TOAST,
)
```

CLI: `--notice-channels board,file-open,terminal-focus,os-toast`
(미지정 시 전체, `--notice-channels none`으로 전면 비활성화)

### 7.4 OS_TOAST 페이로드 계약

| 필드 | 출처 | 상한 |
| --- | --- | --- |
| title | 고정 문자열 + `gate_kind` | 120자 |
| body | `run={run_id} gate={gate_id} options={...} report={basename}` | 300자 |

XML escape 후 환경변수 `ORCA_LOOP_TOAST_XML`로 전달한다.

---

## 8. State and Side Effects

| 대상 | 변경 | 비고 |
| --- | --- | --- |
| `control/user-decision-notice-delivery.json` | schema v2로 atomic write | 기존 파일명·경로 유지 |
| `control/user-decision-request.json` | **변경 없음** | 권위 레코드, 스키마 v1 유지 |
| `CoordinatorState` | **변경 없음** | 알림은 state machine에 영향 없음 |
| Orca 보드 | workspace-status/comment | 기존 동작 |
| Orca UI | 에디터 탭 열림, 터미널 탭 포커스 이동 | **신규 사용자 가시 부작용** |
| OS | 토스트 알림 + 알람음 | **신규 사용자 가시 부작용** |
| 프로세스 | `powershell.exe` 단발 subprocess (10s bound) | Windows 한정 |

---

## 9. Error and Exception Strategy

| 상황 | 처리 |
| --- | --- |
| 채널 실행 실패(`OrcaCommandError`, `OrcaTimeoutError`) | 포착 → `FAILED` 기록 → **다음 채널 계속** |
| PowerShell 부재/비Windows | `SKIPPED(detail="platform not supported")` |
| `coordinator_handle` 미확보 | `ORCA_TERMINAL_FOCUS` → `SKIPPED` |
| report 파일 부재 | `ORCA_FILE_OPEN` → `SKIPPED` |
| delivery 파일 파손 | `DecisionReportError` → status 명령이 blocker로 노출(기존 동작 유지) |
| delivery atomic write 실패 | `DecisionReportError` 전파 — durable 증거 실패는 숨기지 않는다 |
| 채널 전부 실패 | run 계속. 게이트 대기는 정상 진행 |

**원칙:** 채널 전송 실패는 삼키고 기록한다. durable 기록 자체의 실패는 전파한다.

---

## 10. Security Considerations

| 위협 | 대응 |
| --- | --- |
| **PowerShell 명령 주입** — `run_id`는 사용자 지정 run 이름에서 파생 | argv 문자열 결합 **금지**. 고정 스크립트 + 환경변수 전달. `shell=False` |
| **XML 주입** — notice 필드가 toast XML에 삽입 | Python에서 `xml.sax.saxutils.escape` 후 삽입, 길이 상한 적용 |
| 제어문자/개행 주입 | 기존 `_notice_comment()`와 동일하게 `" ".join(value.split())` 정규화 |
| 경로 탈출 — `file open` 대상 | `report_path`는 harness 생성 경로. `workspace.py`의 `_within()` 경계 검사 재사용 |
| 민감정보 노출 | 토스트에 요청 원문·diff 포함 금지. ID와 옵션명만 |
| 무한 프로세스 | 10s bounded, `_terminate_tree()` 선례 적용 |

`subprocess.run(argv, shell=False, env=...)` 형태만 사용한다.

---

## 11. Compatibility Considerations

- **하위 호환:** v1 delivery 파일이 실제 디스크에 존재(검증됨). 읽기 마이그레이션
  필수. 미구현 시 기존 run의 `status` 명령이 blocker를 뿜는다.
- **공개 인터페이스:** `UserDecisionNoticeDelivery`의 `status`/`error` 필드가
  `channels`로 대체된다. 이 타입은 `run_loop.py` `_status_report()`가
  `user_decision_notice_delivery` 키로 노출한다 → **status JSON 출력 형태가 바뀐다.**
  이는 승인이 필요한 외부 계약 변경이다(**Open Question OQ-1**).
- **플랫폼:** `OS_TOAST`는 Windows 전용. 그 외 OS는 `SKIPPED`로 정상 동작.
- **의존성:** 신규 의존성 **0**. 표준 라이브러리와 OS 기본 구성만 사용.

---

## 12. Test and Validation Strategy

| ID | 항목 | 방법 |
| --- | --- | --- |
| `V-01` | v1 → v2 마이그레이션 무손실 | 실제 디스크 v1 파일을 fixture로 고정 |
| `V-02` | 채널별 DELIVERED/FAILED/SKIPPED 계약 | 단위 테스트 |
| `V-03` | 멱등성 — 동일 request_id 재발행 시 재전송 없음 | `_resume_gate` 반복 호출 시나리오 |
| `V-04` | 채널 1개 실패가 나머지를 막지 않음 | fault injection |
| `V-05` | 알림 전면 실패에도 run 계속 | 전 채널 실패 주입 |
| `V-06` | PowerShell argv에 notice 데이터 미포함 | 주입 문자열 run 이름으로 argv 검증 |
| `V-07` | XML escape | `<`, `&`, `"` 포함 run 이름 |
| `V-08` | non-Windows에서 SKIPPED | `platform.system()` 스텁 |
| `V-09` | status 명령의 delivery 노출 형태 | 기존 `test_cli_commands.py` 확장 |
| `V-10` | 전체 회귀 | `pytest -q` — 현재 baseline 299 passed |

`ToastEmitter`를 주입 가능한 seam으로 두어 테스트가 실제 토스트를 발생시키지
않도록 한다. Orca 호출은 기존 `tests/fakes.py`의 fake client를 사용한다.

---

## 13. Risks

| ID | 위험 | 영향 | 완화 |
| --- | --- | --- | --- |
| `R-1` | 포커스 탈취가 사용자 작업을 방해 | 중 | 채널 단위 비활성화 제공. 멱등으로 반복 방지 |
| `R-2` | v1 마이그레이션 누락 시 기존 run blocker | 중 | `V-01`을 실제 디스크 fixture로 고정 |
| `R-3` | status JSON 출력 형태 변경 | 중 | `OQ-1`로 승인 대상화 |
| `R-4` | PowerShell 실행 정책이 토스트를 차단 | 하 | `-NoProfile -NonInteractive` 사용, 실패는 `FAILED` 기록 후 계속 |
| `R-5` | `orca file open`이 gitignore된 `runs/` 경로를 거부할 가능성 | 하 | Phase 2에서 실호출 검증(`V-11`). 거부 시 해당 채널 기본 비활성화 |
| `R-6` | 알림이 durable authority로 오인됨 | 하 | 모든 채널 best-effort임을 문서·코드 주석에 명시 |

---

## 14. Open Questions

| ID | 질문 | 기본 제안 |
| --- | --- | --- |
| `OQ-1` | `status` 명령의 `user_decision_notice_delivery` JSON 형태 변경을 승인하는가? | **승인 전제로 진행** — 채널 배열로 변경. 거부 시 v1 형태를 요약 필드로 병기 |
| `OQ-2` | `ORCA_TERMINAL_FOCUS`(포커스 탈취)를 기본 활성화하는가? | **기본 활성화**. 거부 시 기본 비활성 + opt-in |
| `OQ-3` | 신규 모듈 `orca_loop/notify.py` 생성을 허용하는가? | **허용 전제로 진행**. 거부 시 `escalation.py` 내부 배치 |
| `OQ-4` | 파일명 접두 규칙(`claude-mhj_YY_MM_DD_...`)을 소스 파일에도 적용하는가? | **미적용 제안** — 기존 `orca_loop/*.py`가 전부 무접두이고 import 경로가 깨진다. 문서 산출물에만 적용 |

---

## 15. Approval

- [ ] System Design approved
- [ ] Revision requested

**Next phase after explicit approval:** Phase 2 — Macro Blocking
