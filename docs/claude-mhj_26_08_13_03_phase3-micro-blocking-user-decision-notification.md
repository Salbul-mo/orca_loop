# Task Report: Orca Loop User Decision Immediate Notification

**Current Phase:** 3. Micro Blocking
**Status:** Waiting for Explicit User Approval

**Baseline:** Phase 1 `claude-mhj_26_08_13_01`, Phase 2 `claude-mhj_26_08_13_02` (both approved)

---

## 1. Context & Objective

- **Problem:** 승인된 8개 Macro Block을 함수·타입·테스트 수준의 확정 계약으로 고정해야 한다.
- **Goal:** 각 변경을 단일 책임·독립 검증 가능한 19개 Micro Block으로 분해한다.
- **Scope:** Phase 2 Scope 승계.
- **Out of Scope:** Phase 1/2 Out of Scope 전부 승계.

---

## 2. Shared Type Contracts

Phase 4에서 사용할 확정 타입이다. 새 Python source module은 `orca_loop/notify.py`
하나만 만든다.

### 2.1 `orca_loop/models.py`

```python
class NoticeChannel(StrEnum):
    ORCA_BOARD = "ORCA_BOARD"
    ORCA_FILE_OPEN = "ORCA_FILE_OPEN"
    ORCA_TERMINAL_FOCUS = "ORCA_TERMINAL_FOCUS"
    OS_TOAST = "OS_TOAST"


class UserDecisionNoticeDeliveryStatus(StrEnum):
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"          # 신규


@dataclass(frozen=True)
class NoticeChannelDelivery:
    channel: NoticeChannel
    status: UserDecisionNoticeDeliveryStatus
    attempted_at: str            # ISO-8601, tzinfo 필수
    detail: str | None           # DELIVERED는 None, 그 외는 nonempty


@dataclass(frozen=True)
class UserDecisionNoticeDelivery:
    schema_version: int                            # 항상 2로 write
    request_id: str                                # nonempty
    attempted_at: str                              # ISO-8601, tzinfo 필수
    channels: tuple[NoticeChannelDelivery, ...]    # channel 값 유일
```

`LoopConfig`에 기본값 필드 1개 추가(기존 생성자 호출 호환 유지):

```python
notice_channels: tuple[NoticeChannel, ...] = (
    NoticeChannel.ORCA_BOARD,
    NoticeChannel.ORCA_FILE_OPEN,
    NoticeChannel.ORCA_TERMINAL_FOCUS,
    NoticeChannel.OS_TOAST,
)
```

### 2.2 `orca_loop/notify.py` (신규)

```python
class NoticeDeliveryError(RuntimeError):
    """Raised only when durable delivery evidence cannot be persisted."""


@dataclass(frozen=True)
class ChannelOutcome:
    status: UserDecisionNoticeDeliveryStatus
    detail: str | None


class ToastEmitter(Protocol):
    def emit(self, *, xml: str) -> ChannelOutcome: ...


@dataclass(frozen=True)
class NoticeTarget:
    """Everything a channel may need, resolved once by the caller."""
    control_dir: Path
    worktree_selector: str        # nonempty
    coordinator_handle: str       # may be empty -> ORCA_TERMINAL_FOCUS skips
    workspace_status: str         # e.g. "in-review"
    comment: str                  # bounded single line


class NoticeAnnouncer:
    def __init__(
        self,
        client: OrcaClient,
        *,
        channels: tuple[NoticeChannel, ...],
        toast_emitter: ToastEmitter | None = None,
    ) -> None: ...

    def announce(
        self,
        notice: UserDecisionNotice,
        target: NoticeTarget,
        *,
        force: frozenset[NoticeChannel] = frozenset(),
    ) -> UserDecisionNoticeDelivery: ...
```

`force`는 게이트 해소 시 `ORCA_BOARD`만 멱등 게이트를 우회해 상태를 갱신하기 위한
파라미터다. 다른 용도로 쓰지 않는다.

### 2.3 상수

| 상수 | 값 | 위치 |
| --- | --- | --- |
| `USER_DECISION_NOTICE_DELIVERY_SCHEMA_VERSION` | `2` | `escalation.py` |
| `ORCA_CHANNEL_TIMEOUT_MS` | `30_000` | `notify.py` |
| `TOAST_TIMEOUT_MS` | `10_000` | `notify.py` |
| `TOAST_TITLE_LIMIT` | `120` | `notify.py` |
| `TOAST_BODY_LIMIT` | `300` | `notify.py` |
| `CHANNEL_DETAIL_LIMIT` | `2000` | `notify.py` |
| `TOAST_XML_ENV` | `"ORCA_LOOP_TOAST_XML"` | `notify.py` |

기존 `USER_DECISION_NOTICE_SCHEMA_VERSION = 1`은 **변경하지 않는다**
(`user-decision-request.json` 전용).

---

## 3. Micro Blocks

### M-B01-01 — 채널 enum과 채널 delivery 레코드 타입

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-01` |
| **Parent Block** | `B-01` |
| **Name** | `NoticeChannel`, `SKIPPED`, `NoticeChannelDelivery` |
| **Rationale** | 이후 모든 블록이 참조하는 최소 타입. 단독으로 도입해 변경면을 분리한다. |
| **Objective** | 세 타입이 정의되고 기존 import가 깨지지 않는다. |
| **Target Files** | `orca_loop/models.py` |
| **Preconditions** | 없음 |
| **Input Type** | 없음(타입 선언) |
| **Input Validation** | 없음 |
| **Output Type** | `NoticeChannel`, `UserDecisionNoticeDeliveryStatus`(SKIPPED 포함), `NoticeChannelDelivery` |
| **Output Validation** | `NoticeChannel` 멤버 4개; `UserDecisionNoticeDeliveryStatus` 멤버 3개 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `GateKind` 인접 위치에 `NoticeChannel` 선언 → 2. 기존 `UserDecisionNoticeDeliveryStatus`에 `SKIPPED` 추가 → 3. `UserDecisionNotice` 인접에 `NoticeChannelDelivery` frozen dataclass 선언 → 4. 필드 순서는 `channel, status, attempted_at, detail` |
| **Tests** | `T-B01-01` enum 멤버 값 문자열 정확성 / `T-B01-02` `NoticeChannelDelivery` frozen 여부 |
| **Rollback** | 타입 선언 제거(소비자 없으면 무영향) |

---

### M-B01-02 — delivery 레코드 v2 재정의

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-02` |
| **Parent Block** | `B-01` |
| **Name** | `UserDecisionNoticeDelivery` 채널 배열화 |
| **Rationale** | flat `status`/`error`로는 채널별 멱등 판정이 불가능하다. |
| **Objective** | 타입이 `channels` 배열을 보유하고 `status`/`error` 필드가 제거된다. |
| **Target Files** | `orca_loop/models.py` |
| **Preconditions** | `M-B01-01` |
| **Input Type** | 없음(타입 선언) |
| **Input Validation** | 없음 |
| **Output Type** | `UserDecisionNoticeDelivery(schema_version:int, request_id:str, attempted_at:str, channels:tuple[NoticeChannelDelivery, ...])` |
| **Output Validation** | 파싱·구성 시점 불변식은 `M-B01-03`/`M-B01-04`가 강제 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. 기존 dataclass의 `status`, `error` 필드 제거 → 2. `channels: tuple[NoticeChannelDelivery, ...]` 추가 → 3. docstring을 "per-channel best-effort delivery evidence"로 갱신 |
| **Tests** | `T-B01-03` 필드 집합 정확성(`dataclasses.fields`) |
| **Rollback** | 이전 정의 복원. 단 `M-B01-03` 이후에는 단독 롤백 불가 |

---

### M-B01-03 — v2 파서와 v1 읽기 마이그레이션

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-03` |
| **Parent Block** | `B-01` |
| **Name** | `_parse_notice_delivery()` v1/v2 분기 |
| **Rationale** | live v1 파일이 디스크에 실재한다(`runs/codex-mhj_26_08_13_04_project-guide-luna/control/`). 마이그레이션 없이는 기존 run의 `status`가 blocker를 뿜는다. |
| **Objective** | v1과 v2 파일 모두 동일한 `UserDecisionNoticeDelivery`로 읽힌다. |
| **Target Files** | `orca_loop/escalation.py` |
| **Preconditions** | `M-B01-02` |
| **Input Type** | `Path` → `dict[str, object]` |
| **Input Validation** | root는 dict; `schema_version ∈ {1, 2}`; v1 exact fields `{schema_version, request_id, status, attempted_at, error}`; v2 exact fields `{schema_version, request_id, attempted_at, channels}`; 채널 항목 exact fields `{channel, status, attempted_at, detail}`; 모든 timestamp는 `datetime.fromisoformat` 파싱 가능하며 `tzinfo is not None`; `channel` 값 중복 금지; `DELIVERED`는 `detail is None`, `FAILED`/`SKIPPED`는 nonempty `detail` |
| **Output Type** | `UserDecisionNoticeDelivery` |
| **Output Validation** | `schema_version == 2`; `request_id` nonempty; `channels` 길이 ≥ 0 |
| **Exceptions** | `DecisionReportError` — 파손 JSON, 미지원 version, field 불일치, 불변식 위반. 원인은 `from exc`로 보존 |
| **Side Effects** | 없음(읽기 전용, 파일 재기록 안 함) |
| **Detailed Pseudocode** | 1. `_strict_notice_object(path)`로 dict 획득 → 2. `schema_version` 읽기, `{1,2}` 아니면 `DecisionReportError` → 3. **v1 분기:** exact fields 검증 → `status` enum 변환 → `attempted_at` timestamp 검증 → `detail = None if status is DELIVERED else (error or "legacy delivery failure")` → `channels = (NoticeChannelDelivery(ORCA_BOARD, status, attempted_at, detail),)` → 4. **v2 분기:** exact fields 검증 → `channels` 가 list인지 확인 → 각 항목 exact fields·enum·timestamp·detail 불변식 검증 → 채널 중복 검사 → 5. 두 분기 모두 `schema_version=2`로 dataclass 구성 후 반환 |
| **Tests** | `T-B01-04` 실디스크 v1 내용 fixture 마이그레이션 / `T-B01-05` v2 round-trip / `T-B01-06` version 3 거부 / `T-B01-07` 채널 중복 거부 / `T-B01-08` `DELIVERED` + `detail` 동시 존재 거부 / `T-B01-09` naive timestamp 거부 |
| **Rollback** | v1 분기만 남기고 v2 분기 제거 |

---

### M-B01-04 — v2 writer

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-04` |
| **Parent Block** | `B-01` |
| **Name** | `write_user_decision_notice_delivery()` 채널 배열 서명 |
| **Rationale** | 쓰기는 항상 v2 단일 경로여야 스키마가 갈라지지 않는다. |
| **Objective** | 채널 배열을 받아 v2 레코드를 atomic write하고 그 값을 반환한다. |
| **Target Files** | `orca_loop/escalation.py` |
| **Preconditions** | `M-B01-03` |
| **Input Type** | `control_dir: Path`, `request_id: str`, `channels: Sequence[NoticeChannelDelivery]` |
| **Input Validation** | `request_id` nonempty; 채널 중복 금지; 각 채널의 `detail` 불변식 검증(파서와 동일 규칙 재사용) |
| **Output Type** | `UserDecisionNoticeDelivery` |
| **Output Validation** | `schema_version == 2`; 직렬화 결과가 `_parse_notice_delivery()`로 다시 읽힌다 |
| **Exceptions** | `DecisionReportError` — 입력 불변식 위반, `AtomicWriteError` 변환 |
| **Side Effects** | `control/user-decision-notice-delivery.json` atomic write (`write_atomic_bytes`) |
| **Detailed Pseudocode** | 1. `request_id` 검증 → 2. 채널 중복·`detail` 불변식 검증 → 3. `attempted_at = _utc_now()` → 4. dataclass 구성 → 5. `serialize_json(value) + "\n"` 을 `write_atomic_bytes`로 기록, `AtomicWriteError` → `DecisionReportError` 변환 → 6. 구성한 값 반환 |
| **Tests** | `T-B01-10` write→read round-trip / `T-B01-11` 중복 채널 입력 거부 / `T-B01-12` `AtomicWriteError` 변환 |
| **Rollback** | 이전 서명 복원(호출부 동반 복원 필요) |

---

### M-B02-01 — notify 모듈 골격과 채널 결과 타입

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B02-01` |
| **Parent Block** | `B-02` |
| **Name** | `orca_loop/notify.py` 신설 |
| **Rationale** | 전송 책임을 `escalation.py`(961줄, 게이트 프로토콜 소유)에서 분리한다. |
| **Objective** | 모듈이 생성되고 `ChannelOutcome`, `NoticeTarget`, `ToastEmitter`, `NoticeDeliveryError`가 정의된다. |
| **Target Files** | `orca_loop/notify.py`(신규) |
| **Preconditions** | `M-B01-01` |
| **Input Type** | 없음(타입 선언) |
| **Input Validation** | 없음 |
| **Output Type** | 위 4개 심볼 |
| **Output Validation** | 순환 import 없음(`notify` → `models`, `orca_client`, `escalation` 단방향) |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `from __future__ import annotations` → 2. 표준 라이브러리 import → 3. `models`/`orca_client`/`escalation` import → 4. 상수 선언(2.3절) → 5. `NoticeDeliveryError`, `ChannelOutcome`, `NoticeTarget`, `ToastEmitter` 선언 |
| **Tests** | `T-B02-01` `import orca_loop.notify` 성공 및 순환 import 부재 |
| **Rollback** | 모듈 삭제 |

---

### M-B02-02 — `NoticeAnnouncer.announce()` 멱등 게이트와 병합

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B02-02` |
| **Parent Block** | `B-02` |
| **Name** | 채널 순회·멱등 판정·결과 병합·단일 write |
| **Rationale** | `_resume_gate()`가 resume마다 발행을 호출하므로(`run_loop.py:1470`) 멱등 게이트가 없으면 토스트·포커스가 반복된다. **채널 추가의 전제 조건이다.** |
| **Objective** | 이미 `DELIVERED`인 채널은 재실행되지 않고, 기존 결과가 보존된 채로 병합되며, write는 정확히 1회 발생한다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B01-04`, `M-B02-01` |
| **Input Type** | `notice: UserDecisionNotice`, `target: NoticeTarget`, `force: frozenset[NoticeChannel]` |
| **Input Validation** | `notice.request_id` nonempty; `target.worktree_selector` nonempty |
| **Output Type** | `UserDecisionNoticeDelivery` |
| **Output Validation** | 반환 레코드의 채널 집합 ⊇ (기존 채널 ∪ 이번 시도 채널); 채널 중복 없음 |
| **Exceptions** | `NoticeDeliveryError` — durable write 실패 시에만(`DecisionReportError`를 변환). 채널 실행 예외는 전파하지 않음 |
| **Side Effects** | 채널 부작용은 위임; delivery 파일 write 1회 |
| **Detailed Pseudocode** | 1. `existing = read_user_decision_notice_delivery(target.control_dir)` → 2. `existing`의 `request_id`가 다르면 기존 결과를 버리고 빈 상태에서 시작(다른 게이트의 증거를 승계하지 않는다) → 3. `merged: dict[NoticeChannel, NoticeChannelDelivery]` 를 기존 채널로 초기화 → 4. `delivered = {ch for ch, rec in merged.items() if rec.status is DELIVERED}` → 5. `self.channels` 순회: `ch in delivered and ch not in force` → 건너뜀(기존 레코드 유지) → 6. 아니면 `outcome = self._dispatch(ch, notice, target)`; `_dispatch`는 내부에서 예외를 포착해 `ChannelOutcome`만 반환 → 7. `merged[ch] = NoticeChannelDelivery(ch, outcome.status, _utc_now(), outcome.detail)` → 8. 순회 종료 후 `write_user_decision_notice_delivery(control_dir, request_id, tuple(merged.values()))`; `DecisionReportError` → `NoticeDeliveryError` 변환 → 9. 결과 반환 |
| **Tests** | `T-B02-02` 2회 호출 시 2번째 채널 실행 0회 / `T-B02-03` `force={ORCA_BOARD}`면 board만 재실행 / `T-B02-04` 채널 1개 실패가 나머지를 막지 않음 / `T-B02-05` 전 채널 실패해도 예외 없음 / `T-B02-06` write 호출 정확히 1회 / `T-B02-07` request_id 불일치 시 기존 증거 미승계 |
| **Rollback** | `announce()`가 항상 전 채널을 실행하도록 멱등 게이트만 제거 |

---

### M-B03-01 — `ORCA_BOARD` 채널

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-01` |
| **Parent Block** | `B-03` |
| **Name** | worktree metadata 채널 이관 |
| **Rationale** | 기존 동작을 채널 모델로 옮기되 argv와 timeout을 보존해야 회귀가 없다. |
| **Objective** | 기존 `_record_worktree_metadata()`와 동일한 argv·timeout으로 호출하고 `ChannelOutcome`을 반환한다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B02-01` |
| **Input Type** | `notice: UserDecisionNotice`, `target: NoticeTarget` |
| **Input Validation** | `target.workspace_status` nonempty; `target.comment` nonempty |
| **Output Type** | `ChannelOutcome` |
| **Output Validation** | argv == `("worktree","set","--worktree",S,"--workspace-status",W,"--comment",C)` |
| **Exceptions** | 없음(내부 포착) |
| **Side Effects** | Orca 보드 metadata 갱신 |
| **Detailed Pseudocode** | 1. argv 구성 → 2. `client.call(argv, timeout_ms=ORCA_CHANNEL_TIMEOUT_MS)` → 3. `OrcaCommandError` 포착 → `ChannelOutcome(FAILED, str(exc)[:CHANNEL_DETAIL_LIMIT])` → 4. 성공 시 `ChannelOutcome(DELIVERED, None)` |
| **Tests** | `T-B03-01` argv 정확성 / `T-B03-02` timeout 30_000 / `T-B03-03` `OrcaCommandError` → FAILED |
| **Rollback** | 채널 제거 후 `run_loop`의 기존 함수 복원 |

---

### M-B03-02 — `ORCA_FILE_OPEN` 채널

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-02` |
| **Parent Block** | `B-03` |
| **Name** | 결정 리포트를 Orca 에디터로 열기 |
| **Rationale** | 사용자에게 위치가 아니라 **내용**을 전달하는 유일한 채널이다. Phase 2에서 gitignore된 `runs/` 경로도 열림을 실호출로 확인했다. |
| **Objective** | report가 존재하면 열고, 없으면 `SKIPPED`를 반환한다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B02-01`, `M-B03-04`(fake flag 등록) |
| **Input Type** | `notice: UserDecisionNotice`, `target: NoticeTarget` |
| **Input Validation** | `notice.report_path` nonempty; `Path(notice.report_path).is_file()` |
| **Output Type** | `ChannelOutcome` |
| **Output Validation** | argv == `("file","open", report_path, "--worktree", S)` — 경로는 **위치 인자** |
| **Exceptions** | 없음(내부 포착) |
| **Side Effects** | Orca 에디터 탭 열림 |
| **Detailed Pseudocode** | 1. `path = Path(notice.report_path)` → 2. `not path.is_file()` → `ChannelOutcome(SKIPPED, "decision report is not present")` → 3. argv 구성(경로는 `str(path)`) → 4. `client.call(argv, timeout_ms=ORCA_CHANNEL_TIMEOUT_MS)` → 5. `OrcaCommandError` → `FAILED` → 6. 성공 → `DELIVERED` |
| **Tests** | `T-B03-04` argv 정확성(위치 인자) / `T-B03-05` 파일 부재 시 SKIPPED 및 client 미호출 / `T-B03-06` `OrcaCommandError` → FAILED |
| **Rollback** | 기본 채널 목록에서 제거 |

---

### M-B03-03 — `ORCA_TERMINAL_FOCUS` 채널

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-03` |
| **Parent Block** | `B-03` |
| **Name** | coordinator 터미널 탭 전면화 |
| **Rationale** | 사용자를 결정이 필요한 **위치**로 이동시킨다. |
| **Objective** | handle이 있으면 전환하고, 없으면 `SKIPPED`를 반환한다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B02-01`, `M-B03-04` |
| **Input Type** | `notice: UserDecisionNotice`, `target: NoticeTarget` |
| **Input Validation** | `target.coordinator_handle` nonempty |
| **Output Type** | `ChannelOutcome` |
| **Output Validation** | argv == `("terminal","switch","--terminal", H)` |
| **Exceptions** | 없음(내부 포착) |
| **Side Effects** | Orca UI 포커스 이동 — **사용자 작업을 가로챌 수 있음**(`R-1`) |
| **Detailed Pseudocode** | 1. `not target.coordinator_handle` → `ChannelOutcome(SKIPPED, "coordinator handle is unavailable")` → 2. argv 구성 → 3. `client.call(..., timeout_ms=ORCA_CHANNEL_TIMEOUT_MS)` → 4. `OrcaCommandError` → `FAILED` → 5. 성공 → `DELIVERED` |
| **Tests** | `T-B03-07` argv 정확성 / `T-B03-08` handle 빈 값 SKIPPED 및 client 미호출 / `T-B03-09` `OrcaCommandError` → FAILED |
| **Rollback** | 기본 채널 목록에서 제거(`OQ-2` 재검토 시 경로) |

---

### M-B03-04 — fake argv 계약 등록

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-04` |
| **Parent Block** | `B-03` |
| **Name** | `VALID_FLAGS`에 신규 명령 2건 추가 |
| **Rationale** | `assert_supported_argv()`가 미등록 명령을 `AssertionError`로 거부한다. 등록 없이는 `M-B03-02`/`M-B03-03` 테스트가 즉시 실패한다. 반대로 잘못 등록하면 실제 CLI가 거부할 argv가 green으로 통과한다(과거 `gate-create --run` 회귀 원인). |
| **Objective** | 실제 `orca agent-context --json`이 보고한 flag 집합과 정확히 일치하는 항목이 추가된다. |
| **Target Files** | `tests/fakes.py` |
| **Preconditions** | 없음 |
| **Input Type** | 없음(테이블 상수) |
| **Input Validation** | 없음 |
| **Output Type** | `VALID_FLAGS` 항목 2건 |
| **Output Validation** | `("file","open") -> {"environment","json","pairing-code","path","worktree"}`; `("terminal","switch") -> {"environment","json","pairing-code","terminal"}` |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `("file","open")` 항목을 `frozenset`으로 추가 → 2. `("terminal","switch")` 항목 추가 → 3. 기존 항목 스타일대로 `help`는 제외(기존 항목 모두 미포함) |
| **Tests** | `T-B03-10` 신규 채널 argv가 `assert_supported_argv()`를 통과 / `T-B03-11` 미지 flag(`--focus` 등) 사용 시 여전히 거부 |
| **Rollback** | 항목 제거 |

---

### M-B04-01 — 주입 안전 토스트 페이로드 빌더

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-01` |
| **Parent Block** | `B-04` |
| **Name** | `_build_toast_xml()` |
| **Rationale** | `run_id`는 사용자 지정 run 이름에서 파생되므로 신뢰 경계다. XML 주입과 제어문자 삽입을 여기서 차단한다. |
| **Objective** | 어떤 notice 문자열이 들어와도 well-formed XML이 생성되고 길이 상한이 지켜진다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B02-01` |
| **Input Type** | `notice: UserDecisionNotice` |
| **Input Validation** | 없음(모든 입력을 비신뢰로 취급) |
| **Output Type** | `str` — toast XML |
| **Output Validation** | `xml.etree.ElementTree.fromstring()`로 파싱 가능; title ≤ 120, body ≤ 300 |
| **Exceptions** | 없음 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `title = f"Orca Loop: USER DECISION REQUIRED ({notice.gate_kind.value})"` → 2. `body = f"run={notice.run_id} | gate={notice.gate_id} | options={','.join(notice.allowed_options)} | report={Path(notice.report_path).name}"` → 3. 각 문자열에 `" ".join(value.split())` 적용(개행·탭·제어문자 정규화) → 4. `title[:TOAST_TITLE_LIMIT]`, `body[:TOAST_BODY_LIMIT]` 절단 → 5. `xml.sax.saxutils.escape` 적용 → 6. `<toast scenario="reminder"><visual><binding template="ToastGeneric"><text>{title}</text><text>{body}</text></binding></visual><audio src="ms-winsoundevent:Notification.Looping.Alarm2"/></toast>` 조립 → 7. 반환 |
| **Tests** | `T-B04-01` `<`,`&`,`"`,`'` 포함 run_id 이스케이프 후 파싱 가능 / `T-B04-02` 개행·탭 정규화 / `T-B04-03` 길이 상한 / `T-B04-04` 정상 입력 XML 구조 |
| **Rollback** | 없음(순수 함수) |

---

### M-B04-02 — bounded PowerShell 토스트 emitter

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-02` |
| **Parent Block** | `B-04` |
| **Name** | `PowerShellToastEmitter` |
| **Rationale** | 유일하게 Orca 외부로 나가는 프로세스 실행. 명령 주입 차단과 시간 경계가 핵심 책임이다. |
| **Objective** | notice 파생 문자열이 argv에 전혀 포함되지 않은 채로 토스트가 발송되고, 10초 내에 반드시 종료된다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B04-01` |
| **Input Type** | `xml: str` |
| **Input Validation** | `xml` nonempty |
| **Output Type** | `ChannelOutcome` |
| **Output Validation** | argv는 상수 tuple과 정확히 일치 — 가변 부분 없음 |
| **Exceptions** | 없음(내부 포착) |
| **Side Effects** | `powershell.exe` 단발 subprocess, OS 알림 센터 항목, 알람음 |
| **Detailed Pseudocode** | 1. `platform.system() != "Windows"` → `ChannelOutcome(SKIPPED, "platform does not support OS toast")` → 2. `executable = shutil.which("powershell")`; `None` → `SKIPPED("powershell is not on PATH")` → 3. `env = dict(os.environ); env[TOAST_XML_ENV] = xml` → 4. `argv = (executable, "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT)` — `_TOAST_SCRIPT`는 **모듈 상수**이며 `$env:ORCA_LOOP_TOAST_XML`만 참조한다 → 5. `subprocess.run(argv, shell=False, env=env, capture_output=True, timeout=TOAST_TIMEOUT_MS/1000, check=False)` → 6. `TimeoutExpired` → `FAILED("toast emission timed out")` / `OSError` → `FAILED(str(exc))` → 7. `returncode != 0` → `FAILED(stderr tail[:CHANNEL_DETAIL_LIMIT])` → 8. `DELIVERED` |
| **Tests** | `T-B04-05` argv에 run_id/gate_id 문자열 미포함(주입 run 이름으로 검증) / `T-B04-06` XML이 env로만 전달됨 / `T-B04-07` 비Windows SKIPPED / `T-B04-08` PowerShell 부재 SKIPPED / `T-B04-09` `TimeoutExpired` → FAILED / `T-B04-10` `returncode != 0` → FAILED |
| **Rollback** | 기본 채널 목록에서 `OS_TOAST` 제거 |

`_TOAST_SCRIPT` 본문(고정, 문자열 결합 금지):

```powershell
$ErrorActionPreference='Stop'
$AppId='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null
[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]>$null
$d=New-Object Windows.Data.Xml.Dom.XmlDocument
$d.LoadXml($env:ORCA_LOOP_TOAST_XML)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show(
  (New-Object Windows.UI.Notifications.ToastNotification $d))
```

---

### M-B04-03 — `OS_TOAST` 채널 배선

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-03` |
| **Parent Block** | `B-04` |
| **Name** | 채널 dispatch에 토스트 연결 및 emitter 주입 |
| **Rationale** | 테스트가 실제 토스트를 발생시키지 않도록 emitter를 주입 가능한 seam으로 유지한다. |
| **Objective** | `NoticeAnnouncer`가 `OS_TOAST`를 주입된 emitter로 위임하고, 미주입 시 `PowerShellToastEmitter` 기본값을 쓴다. |
| **Target Files** | `orca_loop/notify.py` |
| **Preconditions** | `M-B02-02`, `M-B04-01`, `M-B04-02` |
| **Input Type** | `notice: UserDecisionNotice` |
| **Input Validation** | 없음 |
| **Output Type** | `ChannelOutcome` |
| **Output Validation** | emitter가 주입되면 `PowerShellToastEmitter`가 인스턴스화되지 않는다 |
| **Exceptions** | emitter가 던지는 예외는 `_dispatch`가 포착해 `FAILED`로 변환 |
| **Side Effects** | 위임 |
| **Detailed Pseudocode** | 1. `xml = _build_toast_xml(notice)` → 2. `self._toast_emitter.emit(xml=xml)` 반환 |
| **Tests** | `T-B04-11` 주입 emitter 사용 확인 / `T-B04-12` emitter 예외 → FAILED |
| **Rollback** | 채널 dispatch에서 분기 제거 |

---

### M-B05-01 — `LoopConfig.notice_channels` 필드

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-01` |
| **Parent Block** | `B-05` |
| **Name** | 설정 필드 추가와 검증 |
| **Rationale** | `R-1` 완화 수단을 설정으로 흡수한다. 기본값을 주어 기존 생성자 호출을 보존한다. |
| **Objective** | 필드가 기본값 4채널로 추가되고 기존 `LoopConfig(...)` 호출이 무변경 통과한다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/config.py`(`validate_loop_config`) |
| **Preconditions** | `M-B01-01` |
| **Input Type** | `tuple[NoticeChannel, ...]` |
| **Input Validation** | 중복 금지 |
| **Output Type** | `LoopConfig` |
| **Output Validation** | 필드가 dataclass 마지막 위치(기본값 필드 규칙) |
| **Exceptions** | `ConfigurationError` — 중복 채널 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `LoopConfig` 끝에 기본값 필드 추가 → 2. `validate_loop_config`에 중복 검사 추가 |
| **Tests** | `T-B05-01` 기본값 4채널 / `T-B05-02` 기존 생성자 호출 통과 / `T-B05-03` 중복 거부 |
| **Rollback** | 필드 제거 |

---

### M-B05-02 — `--notice-channels` CLI 파싱

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-02` |
| **Parent Block** | `B-05` |
| **Name** | 채널 선택 flag |
| **Rationale** | 채널 비활성화가 코드 수정 없이 가능해야 한다. |
| **Objective** | 콤마 구분 별칭이 `tuple[NoticeChannel, ...]`로 변환되고 미지 이름은 거부된다. |
| **Target Files** | `orca_loop/config.py` |
| **Preconditions** | `M-B05-01` |
| **Input Type** | `str | None` |
| **Input Validation** | `None` → 기본값; `"none"` → `()`; 그 외는 콤마 분리 후 각 토큰이 별칭 맵의 키여야 함; 빈 토큰 금지 |
| **Output Type** | `tuple[NoticeChannel, ...]` |
| **Output Validation** | 중복 제거, 입력 순서 보존 |
| **Exceptions** | `ConfigurationError` — 미지 이름·빈 토큰. 메시지에 유효 목록 포함 |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. 별칭 맵 `{"board":ORCA_BOARD, "file-open":ORCA_FILE_OPEN, "terminal-focus":ORCA_TERMINAL_FOCUS, "os-toast":OS_TOAST}` 선언 → 2. `value is None` → 기본 4채널 → 3. `value.strip().lower() == "none"` → `()` → 4. 콤마 분리·strip·lower → 5. 빈 토큰 → `ConfigurationError` → 6. 맵 조회 실패 → `ConfigurationError(f"unknown notice channel: {token}; valid: {sorted(alias)}")` → 7. 순서 보존 중복 제거 → 8. tuple 반환 |
| **Tests** | `T-B05-04` 기본값 / `T-B05-05` 부분 선택 / `T-B05-06` `none` → `()` / `T-B05-07` 미지 이름 거부 / `T-B05-08` 중복 입력 정규화 |
| **Rollback** | flag 제거(필드 기본값은 유지) |

---

### M-B06-01 — 발행 경로 재배선

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-01` |
| **Parent Block** | `B-06` |
| **Name** | `_publish_user_decision_notice()` → `NoticeAnnouncer` |
| **Rationale** | 통합 지점에서 멱등 계약이 실제로 성립하는지가 이 과업의 성패다. |
| **Objective** | 발행 시 설정된 전 채널이 1회 실행되고, resume 반복에도 재실행이 없다. |
| **Target Files** | `run_loop.py` |
| **Preconditions** | `M-B02-02`, `M-B03-01~04`, `M-B04-03`, `M-B05-01` |
| **Input Type** | `controller: GenerationController`, `client: OrcaClient`, `report_path: Path`, `channels: tuple[NoticeChannel, ...]` |
| **Input Validation** | `controller.state.gate_binding is not None` |
| **Output Type** | `UserDecisionNotice` |
| **Output Validation** | 반환 notice의 `request_id`가 delivery 레코드의 `request_id`와 일치 |
| **Exceptions** | `OrcaLoopError` — gate binding 부재(기존 동작 보존). `NoticeDeliveryError`는 전파 |
| **Side Effects** | 채널 부작용 위임 |
| **Detailed Pseudocode** | 1. `binding is None` → `OrcaLoopError`(기존) → 2. `notice = ensure_user_decision_notice(...)` → 3. `target = NoticeTarget(control_dir, state.worktree_selector, state.coordinator_handle, "in-review", _notice_comment(notice))` → 4. `NoticeAnnouncer(client, channels=channels).announce(notice, target)` → 5. `notice` 반환. 기존 `_record_worktree_metadata()`는 삭제 |
| **Tests** | `T-B06-01` 발행 1회에 전 채널 실행 / `T-B06-02` resume 3회에도 채널 실행 총 1회 / `T-B06-03` 전 채널 실패해도 `wait_gate_resolution` 진입 |
| **Rollback** | 이전 `_record_worktree_metadata()` 호출 복원 |

---

### M-B06-02 — 해소 경로 board 전용화

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-02` |
| **Parent Block** | `B-06` |
| **Name** | `_close_user_decision_notice()` |
| **Rationale** | 해소 시 토스트·포커스가 다시 뜨면 알림이 소음이 된다. |
| **Objective** | 해소 시 `ORCA_BOARD`만 실행되고 상태가 `completed`/`in-progress`로 갱신된다. |
| **Target Files** | `run_loop.py` |
| **Preconditions** | `M-B06-01` |
| **Input Type** | `controller`, `client`, `binding: GateBinding` |
| **Input Validation** | 없음 |
| **Output Type** | `None` |
| **Output Validation** | 실행된 채널 집합 == `{ORCA_BOARD}` |
| **Exceptions** | `DecisionReportError` — binding 불일치(기존 동작 보존) |
| **Side Effects** | 보드 상태 갱신만 |
| **Detailed Pseudocode** | 1. `notice = resolve_user_decision_notice(control_dir, binding=binding)` → `None`이면 반환 → 2. `workspace_status = "completed" if state in {READY_FOR_MERGE, REJECTED} else "in-progress"` → 3. `target = NoticeTarget(..., workspace_status, comment="Orca Loop user decision recorded | ...")` → 4. `NoticeAnnouncer(client, channels=(ORCA_BOARD,)).announce(notice, target, force=frozenset({ORCA_BOARD}))` |
| **Tests** | `T-B06-04` `ORCA_BOARD`만 실행 / `T-B06-05` 상태 매핑 / `T-B06-06` force로 재실행됨 |
| **Rollback** | 이전 구현 복원 |

---

### M-B07-01 — status 채널 배열 노출

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B07-01` |
| **Parent Block** | `B-07` |
| **Name** | `_status_report()` delivery 직렬화 |
| **Rationale** | `OQ-1` 승인 사항. 채널별 결과가 보이지 않으면 운영자가 어떤 경로가 실패했는지 알 수 없다. |
| **Objective** | `status --json`이 채널 배열을 노출하고 기존 blocker 판정이 보존된다. |
| **Target Files** | `run_loop.py` |
| **Preconditions** | `M-B01-03` |
| **Input Type** | `control: Path`, `state: CoordinatorState`, `notice: UserDecisionNotice | None` |
| **Input Validation** | 없음 |
| **Output Type** | `dict[str, object]` — `{"request_id": str, "attempted_at": str, "channels": [{"channel": str, "status": str, "attempted_at": str, "detail": str | None}]}` |
| **Output Validation** | `channels` 순서는 `NoticeChannel` 선언 순서로 정렬 |
| **Exceptions** | `DecisionReportError` 포착 → `notice_problems` 추가(기존 동작 보존) |
| **Side Effects** | 없음 |
| **Detailed Pseudocode** | 1. `read_user_decision_notice_delivery(control)` → 2. `None`이면 키 생략 → 3. `notice is None or delivery.request_id != notice.request_id` → problem 추가(기존 규칙 유지) → 4. 채널을 enum 선언 순서로 정렬해 직렬화 → 5. `value["user_decision_notice_delivery"] = {...}` |
| **Tests** | `T-B07-01` 채널 배열 노출 / `T-B07-02` request_id 불일치 blocker 유지 / `T-B07-03` v1 파일 입력 시 `ORCA_BOARD` 1건으로 노출 / `T-B07-04` 파손 파일 blocker |
| **Rollback** | 이전 flat 직렬화 복원 |

---

### M-B08-01 — 테스트 매트릭스 구축

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-01` |
| **Parent Block** | `B-08` |
| **Name** | `tests/test_notify.py` 신설 및 기존 테스트 갱신 |
| **Rationale** | 부작용 중심 기능이라 seam 기반 검증이 필수다. |
| **Objective** | `T-B01-*` ~ `T-B07-*` 전 항목이 구현되고 통과한다. |
| **Target Files** | `tests/test_notify.py`(신규), `tests/test_escalation.py`, `tests/test_cli.py`, `tests/test_cli_commands.py`, `tests/fakes.py` |
| **Preconditions** | `M-B01-01` ~ `M-B07-01` |
| **Input Type** | 없음 |
| **Input Validation** | 없음 |
| **Output Type** | 테스트 모듈 |
| **Output Validation** | 실제 토스트 미발생, 실제 Orca 호출 미발생 |
| **Exceptions** | 없음 |
| **Side Effects** | 임시 디렉터리 생성 |
| **Detailed Pseudocode** | 1. `RecordingToastEmitter`(호출 인자 기록) 구현 → 2. `FakeOrcaClient` 재사용 → 3. 실디스크 v1 JSON 내용을 fixture 상수로 고정 → 4. 블록별 테스트 작성 |
| **Tests** | 자기 자신 |
| **Rollback** | 신규 파일 삭제 |

---

### M-B08-02 — 전체 회귀와 실환경 확인

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-02` |
| **Parent Block** | `B-08` |
| **Name** | 회귀 실행 및 구현 보고서 |
| **Rationale** | 단위 테스트는 실제 도달을 보증하지 못한다. |
| **Objective** | 전체 스위트가 baseline(299 passed) 이상 통과하고, 실환경 토스트·파일 열기가 1회 확인된다. |
| **Target Files** | 없음(실행) |
| **Preconditions** | `M-B08-01` |
| **Input Type** | 없음 |
| **Input Validation** | 없음 |
| **Output Type** | 테스트 결과 및 Phase 4 보고서 |
| **Output Validation** | 신규 실패 0 |
| **Exceptions** | 없음 |
| **Side Effects** | 수동 확인 시 실제 토스트 1회 발생 |
| **Detailed Pseudocode** | 1. `.venv\Scripts\python.exe -m pytest -q` → 2. live v1 run에 대해 `status --json` 실행해 회귀 확인 → 3. 실환경 토스트 1회 수동 확인 → 4. 보고서 작성 |
| **Tests** | 해당 없음 |
| **Rollback** | 해당 없음 |

---

## 4. Implementation Order

```
M-B01-01 -> M-B01-02 -> M-B01-03 -> M-B01-04
                                       |
M-B02-01 -------------------------------+-> M-B02-02
                                              |
M-B03-04 -> M-B03-01 -> M-B03-02 -> M-B03-03 -+
                                              |
M-B04-01 -> M-B04-02 -> M-B04-03 -------------+
                                              |
M-B05-01 -> M-B05-02 -------------------------+
                                              |
                                        M-B06-01 -> M-B06-02
                                              |
                                        M-B07-01
                                              |
                                  M-B08-01 -> M-B08-02
```

`M-B03-04`(fake flag 등록)를 채널 구현보다 **먼저** 수행한다. 등록 전에는 신규 채널
테스트가 argv 거부로 실패하므로 순서가 뒤집히면 원인 진단이 흐려진다.

---

## 5. Validation and Risks

- **Validation:** 본 Phase 작성 전 `write_atomic_bytes`(`generation.py:93`),
  `serialize_json`(`contracts.py:1702`), `AtomicWriteError`(`generation.py:34`),
  `assert_supported_argv`(`fakes.py:78`), argparse 구조(`config.py:859-954`),
  `LoopConfig` 필드 순서(`models.py:1072`)를 직접 확인해 계약에 반영했다.
- **Risks:**
  - `M-B01-02`가 기존 타입의 필드를 제거하므로 `M-B01-03`/`M-B01-04`/`M-B07-01`이
    완료되기 전까지 중간 상태에서 스위트가 red다. 이 4개는 하나의 연속 작업으로
    수행한다.
  - `M-B04-02`의 `V-B04-01`(argv 주입) 테스트가 PASS하기 전에는 `OS_TOAST`를
    기본 채널 목록에 포함하지 않는다.
- **Open Questions:** 없음.

---

## 6. Approval

- [ ] Micro Blocking approved
- [ ] Revision requested
- [ ] Permission granted to begin implementation

**Next phase after explicit approval:** Phase 4 — Code Implementation
