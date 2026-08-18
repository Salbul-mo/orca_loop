# Task Report: `blocking_reason` 을 합의 입력으로 승격

**Current Phase:** 3. Micro Blocking
**Status:** Waiting for Approval

Phase 1: `claude-mhj_26_08_18_01_phase1-system-design-blocking-reason-consensus.md`
Phase 2: `claude-mhj_26_08_18_02_phase2-macro-blocking-blocking-reason-consensus.md`

---

## 1. Context and Objective

Phase 2 의 6개 블록을 편집 단위(파일 · 삽입 지점 · 정확한 코드)로 확정한다.

---

## 2. Deliverables — 편집 단위

### 2.1 `M-1` · `orca_loop/contracts.py::_parse_finding` — 증거 의무

**삽입 지점:** `required_fix`/`required_change` XOR 검사 직후, `line_value` 파싱 직전
(현재 `contracts.py:869-873` 과 `874` 사이).

**현재 코드**

```python
    if (required_fix is None) == (required_change is None):
        raise ContractViolationError(
            f"{context} must have exactly one required action"
        )
    line_value = raw["line"]
```

**변경 후**

```python
    if (required_fix is None) == (required_change is None):
        raise ContractViolationError(
            f"{context} must have exactly one required action"
        )
    grounds = _strings(
        raw["acceptance_criteria_ids"],
        f"{context}.acceptance_criteria_ids",
    ) + _strings(raw["evidence_refs"], f"{context}.evidence_refs")
    if not grounds:
        raise ContractViolationError(
            f"{context} must cite acceptance_criteria_ids or evidence_refs"
        )
    line_value = raw["line"]
```

**주의 사항**

- `_strings` 를 두 번 호출하게 되지만(아래 `Finding(...)` 생성부에서 다시 호출),
  `_strings` 는 부작용 없는 순수 검증·변환이므로 중복 호출이 안전하다. 결과를
  변수에 담아 재사용하면 생성부의 인자 배치를 흐트러뜨리므로 **최소 개입**을 택한다.
- `_strings` 가 먼저 타입을 검증하므로, 값이 문자열 배열이 아니면 이 검사 이전에
  기존 오류가 발생한다. 순서 역전 없음.
- 예외 메시지는 `{context}` 접두어(`review.findings[0]`)를 유지해 어느 finding 이
  문제인지 워커에게 그대로 전달된다.

---

### 2.2 `M-2` · `orca_loop/ledger.py` — import 추가

**현재** (`ledger.py:8-29`) `from .models import (...)` 블록에 `BlockingReason` 없음.

**변경:** 알파벳 순서상 `ConsensusKind` 앞에 삽입한다.

```python
from .models import (
    BlockingReason,
    ConsensusKind,
    ConsensusLedger,
    ...
```

---

### 2.3 `M-3` · `orca_loop/ledger.py::commit_round` — `B5` 지속 escalation

**삽입 지점:** 기존 signature 기반 `E-05` 블록(`ledger.py:590-607`) **직후**,
`E-02` 블록(`608`) 직전.

**추가 코드**

```python
        if (
            record.finding.blocking_reason is BlockingReason.B5
            and len(history) >= 2
        ):
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E05,
                    reason=(
                        "reviewer reported insufficient basis to decide "
                        "in two valid rounds"
                    ),
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=(
                        f"E-05:B5:{record.finding.finding_id}"
                    ),
                )
            )
```

**설계 근거 재확인**

| 질문 | 답 |
|---|---|
| 왜 `history` 이고 `record.unresolved_signature_history` 가 아닌가 | `history` 는 이번 round observation 을 포함한 값이다(`ledger.py:580`). 이번 커밋이 두 번째 미해결 round 라면 즉시 발화해야 한다 |
| 왜 `material_progress` 를 보지 않는가 | 바로 그것이 우회 경로다. `B5` 는 표현이 바뀌어도 근거가 보강되지 않으면 해소되지 않는다 |
| 왜 `_conflicting_sides` 를 보지 않는가 | `B5` 는 이견이 아니라 판정 불가다. 양측이 나란히 판정 불가여도 루프는 진행할 수 없다 |
| 기존 `E-05` 와 동시 발화 가능한가 | 가능하다. signature 가 동일하고 진전이 없는 `B5` finding 은 두 키 모두 발화한다. `_dedupe_escalations` 는 키가 다르므로 둘 다 남긴다. reason 두 줄이 사용자에게 보고되며, 이는 정확한 서술이다 |

---

### 2.4 `M-4` · `orca_loop/ledger.py::commit_round` — `E-04` 자격 확대

**변경 지점:** `ledger.py:629-633` 조건식.

**현재**

```python
        if (
            record.finding.impact_class is ImpactClass.SECURITY_AUTH
            and _conflicting_sides(record, next_round)
        ):
```

**변경 후**

```python
        if (
            (
                record.finding.impact_class is ImpactClass.SECURITY_AUTH
                or record.finding.blocking_reason is BlockingReason.B4
            )
            and _conflicting_sides(record, next_round)
        ):
```

본문(`EscalationTrigger` 생성)은 **한 글자도 바꾸지 않는다.** dedup key
`E-04:{finding_id}` 가 그대로이므로 두 축이 모두 걸려도 1회만 발화한다.

---

### 2.5 `M-5` · `orca_loop/ledger.py::commit_round` — 계약 변경 `E-03`

**삽입 지점:** `E-04` 블록 직후, `records.append(current_record)` 직전
(현재 `ledger.py:641` 과 `642` 사이).

**추가 코드**

```python
        if record.finding.impact_class in {
            ImpactClass.DB_SCHEMA,
            ImpactClass.EXTERNAL_API,
        }:
            escalations.append(
                EscalationTrigger(
                    code=EscalationCode.E03,
                    reason=(
                        "data, API, or schema contract change requires "
                        "user approval"
                    ),
                    evidence_refs=record.finding.evidence_refs,
                    deduplication_key=f"E-03:{record.finding.finding_id}",
                )
            )
```

**reason 문자열**은 `coordinator.py:724` 의 기존 `E-03` 문구와 동일하게 맞춘다.
사용자 리포트에서 두 경로가 같은 사건으로 읽히는 것이 옳다.

**`in {...}` 형태를 쓰는 이유:** `ImpactClass` 는 `StrEnum` 이며 집합 멤버십은
`is` 비교 두 번과 동치다. 세 번째 계약 클래스가 추가될 때 확장 지점이 하나다.

---

### 2.6 `M-6` · `tests/test_ledger.py::finding()` 파라미터화

**현재** (`tests/test_ledger.py:34-56`)

```python
def finding(
    finding_id: str = "F-1",
    *,
    depends_on: tuple[str, ...] = (),
) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=Severity.P1,
        blocking_reason=BlockingReason.B1,
        impact_class=ImpactClass.NONE,
        ...
        root_cause="The state transition is missing.",
```

**변경 후**

```python
def finding(
    finding_id: str = "F-1",
    *,
    depends_on: tuple[str, ...] = (),
    blocking_reason: BlockingReason = BlockingReason.B1,
    impact_class: ImpactClass = ImpactClass.NONE,
    root_cause: str = "The state transition is missing.",
) -> Finding:
    return Finding(
        finding_id=finding_id,
        severity=Severity.P1,
        blocking_reason=blocking_reason,
        impact_class=impact_class,
        ...
        root_cause=root_cause,
```

기본값이 현재 리터럴과 동일하므로 **기존 호출부 수정 없음.**

---

### 2.7 `M-7` · `tests/test_ledger.py` — 신규 테스트

기존 `E-05` 테스트(`test_ledger.py:~190-230`)가 쓰는 2-round 커밋 패턴을 재사용한다.

| 테스트 | 검증 ID | 내용 |
|---|---|---|
| `test_b5_finding_escalates_after_two_rounds` | `V-R8a-01`, `-02` | round 1 커밋 후 `E-05:B5:` 키 없음 → round 2 커밋 후 존재 |
| `test_b5_escalation_survives_root_cause_rewording` | `V-R8a-03` | round 2 에서 `root_cause` 를 바꿔 signature 를 변경 → 기존 signature 기반 `E-05` 는 발화하지 않고 `E-05:B5:` 만 발화 |
| `test_b1_finding_does_not_trigger_b5_escalation` | `V-R8a-04` | `B1` finding 2 round → `E-05:B5:` 키 없음 |
| `test_b4_finding_escalates_e04_without_security_impact_class` | `V-R8b-01` | `B4` + `impact_class=none` + 양측 상충 → `E-04` |
| `test_b4_finding_without_conflict_does_not_escalate` | `V-R8b-02` | `B4` + 양측 동일 결정 → `E-04` 없음 |
| `test_security_auth_escalation_is_unchanged` | `V-R8b-03` | 기존 경로 회귀 확인 (`B1` + `security_auth` + 상충 → `E-04` 1회) |
| `test_contract_impact_class_escalates_e03` | `V-R8c-01` | `db_schema`, `external_api` 각각 → `E-03`, 상충 없이도 발화. key 는 `E-03:{id}` |

**`V-R8a-03` 구현 방법:** `apply_review_artifact` 는 동일 `finding_id` 에 내용이
다른 finding 이 오면 `LedgerIntegrityError` 를 던진다(`ledger.py:226-228`). 따라서
round 2 의 `root_cause` 변경은 artifact 재적용이 아니라 **원장 레코드를 직접
`dataclasses.replace` 로 갱신**해 signature 만 달라진 상태를 만든다. 기존
`test_ledger.py` 가 `FindingRecord` 를 직접 import 하고 있으므로 추가 import 없음.

**`_conflicting_sides` 를 만드는 방법:** 같은 round 에 `Side.CLAUDE=APPROVE`,
`Side.CODEX=CHANGE_REQUIRED` 결정을 넣는다. 단 `_status_from_decisions` 가
`CHANGE_REQUIRED` 를 우선하므로 레코드는 미해결로 남아 escalation 루프에 진입한다.

---

### 2.8 `M-8` · `tests/test_contracts.py` — 증거 의무 테스트

`review_value()` 의 `findings` 는 현재 `[]` 다. 테스트 전용 finding 딕셔너리를
모듈 수준 헬퍼로 추가한다.

```python
def finding_value(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "id": "F-1",
        "severity": "P1",
        "blocking_reason": "B1",
        "impact_class": "none",
        "file": "src/example.py",
        "line": 1,
        "root_cause": "The transition is missing.",
        "description": "The transition is not implemented.",
        "required_fix": "Implement the transition.",
        "required_change": None,
        "acceptance_criteria_ids": ["AC-1"],
        "affected_files": ["src/example.py"],
        "test_ids": ["T-1"],
        "depends_on": [],
        "evidence": ["src/example.py:1"],
        "reopens": None,
    }
    value.update(overrides)
    return value
```

| 테스트 | 검증 ID | 내용 |
|---|---|---|
| `test_finding_requires_acceptance_criteria_or_evidence` | `V-R8d-01`~`03` | 둘 다 `[]` → `ContractViolationError`, 메시지에 `acceptance_criteria_ids or evidence_refs` 포함. 각각 하나만 있으면 파싱 성공 |

**verdict 정합:** `findings` 가 비지 않으면 `approval_obligation` 검사가
`APPROVE` 를 거부한다. `review_value()` 의 기본 verdict 는 `REVISE`/
`CHANGES_REQUESTED` 이므로 그대로 쓸 수 있다.

**`finding_decisions` 정합:** `parse_review_artifact` 는 `decision_ids` 가
`reviewed_ids | {신규 finding id}` 와 정확히 일치할 것을 요구한다
(`contracts.py:1077-1083`). `finding_value()` 의 기본 `id` 를 기존 fixture 의
`"F-1"` 과 맞췄으므로 추가 결정 항목 없이 그대로 성립한다.

---

### 2.9 `M-9` · 프롬프트 3종

**공통 — `Choosing blocking_reason` 절의 `B4`·`B5` 항목 뒤에 결과 문장 추가**

```
- `B4` security, integrity, or compatibility risk: ...
  A `B4` finding escalates to the user as `E-04` as soon as the two lanes
  disagree about it, whatever `impact_class` you assigned.
- `B5` insufficient basis to decide: ...
  Never use `B5` to avoid taking a position on evidence you do have. A `B5`
  finding that is still unresolved after a second valid round goes to the user
  as `E-05`, whether or not you reworded it.
```

**공통 — 증거 의무 강화.** 현재 문장:

> Put a `file:line` reference in `evidence_refs` for every finding you raise; a
> finding the coordinator cannot trace back to the frozen diff is not actionable.

뒤에 추가:

> A finding with both `acceptance_criteria_ids` and `evidence_refs` empty is
> rejected and the whole artifact is returned to you.

**`code_reviewer.md` · `cross_confirmer.md` 한정 — escalation 표 정정**

| 현재 | 변경 후 |
|---|---|
| `db_schema`, `external_api` → contract-change review at the plan level | `db_schema`, `external_api` → `E-03` user approval for the contract change |

**`plan_reviewer.md`** 의 해당 행은 이미 `E-03` 이므로 변경 없음.

**표 각주 추가 (3개 공통):** 현재 "`E-05`(반복) 와 `E-06`(재개봉) 은 자동 도출"
문장에 `E-03`·`E-04` 의 새 경로가 포함되도록 문구를 조정한다.

---

## 3. 구현 순서

```
1. M-2  ledger import
2. M-3, M-4, M-5   ledger commit_round (한 번에 편집)
3. M-1  contracts _parse_finding
4. M-6, M-7  test_ledger
5. M-8  test_contracts
6. 전체 스위트 실행
7. M-9  프롬프트 (구현 확정 후)
8. 전체 스위트 재실행
```

---

## 4. Validation and Risks

### 4.1 Validation Plan

| 단계 | 명령 | 기대 |
|---|---|---|
| 단위 | `.venv\Scripts\python.exe -m pytest tests/test_ledger.py -q` | 신규 7건 포함 전부 통과 |
| 단위 | `.venv\Scripts\python.exe -m pytest tests/test_contracts.py -q` | 신규 1건 포함 전부 통과 |
| 회귀 | `.venv\Scripts\python.exe -m pytest -q` | 기존 375건 + 신규 8건 |
| 수동 | 프롬프트 3종 대조 | `V-R8p-01`~`03` |

### 4.2 Risks

| ID | 항목 | 판단 |
|---|---|---|
| `RK-7` | `M-1` 이 `_strings` 를 중복 호출한다 | `_strings` 는 순수 함수. 성능 영향은 finding 당 리스트 2회 순회로 무시 가능. 최소 개입을 우선 |
| `RK-8` | `V-R8a-03` 이 원장 레코드를 직접 조작해 실제 워커 경로를 재현하지 않는다 | 검증 대상은 "signature 가 달라져도 `E-05:B5:` 가 발화하는가" 이며 이는 `commit_round` 단위 계약이다. 워커 경로는 `test_dispatcher` 가 별도로 다룬다 |
| `RK-9` | `E-03` 이 코드 검토 단계에서도 발화하게 된다 | 의도된 동작이다. 계획이 놓친 계약 변경을 구현 단계에서 발견하는 것이 이 규칙의 핵심 가치다 |

---

## 5. Approval Status

- [ ] Current phase approved
- [ ] Revision requested
- [ ] Permission granted to proceed to the next phase
