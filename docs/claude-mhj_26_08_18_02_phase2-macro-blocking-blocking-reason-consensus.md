# Task Report: `blocking_reason` 을 합의 입력으로 승격

**Current Phase:** 2. Macro Blocking
**Status:** Waiting for Approval

Phase 1: `claude-mhj_26_08_18_01_phase1-system-design-blocking-reason-consensus.md`

---

## 1. Context and Objective

Phase 1 에서 확정한 4개 규칙을 구현 블록으로 분해한다.

| 규칙 | 내용 |
|---|---|
| `R-8a` | `B5` 가 2개 유효 round 를 살아남으면 `E-05` |
| `R-8b` | `B4` 를 `E-04` 의 두 번째 자격 조건으로 추가 (양측 상충 요건 유지) |
| `R-8c` | `impact_class ∈ {db_schema, external_api}` → `E-03` |
| `R-8d` | blocking finding 은 `acceptance_criteria_ids` 또는 `evidence_refs` 중 하나 이상 nonempty |

---

## 2. Deliverables — Macro Block 정의

### `B-R8-1` 증거 의무 계약 검사

| 항목 | 내용 |
|---|---|
| **Rationale** | 원칙 3의 강제 수단. 근거 없는 finding 이 합의 원장에 들어오면 `blocking_reason` 을 합의 입력으로 쓸 수 없다. `phase1-system-design.md:433`, `phase2-macro-blocking.md:326` 이 규정했으나 미구현 |
| **Objective** | `Finding` 파싱 시 `acceptance_criteria_ids` 와 `evidence_refs` 가 **둘 다 비어 있으면** 거부 |
| **Scope** | `orca_loop/contracts.py::_parse_finding` (유일 호출처는 `contracts.py:1076`) |
| **Exclusions** | `non_blocking_suggestions` 는 대상 아님 (`Finding` 이 아니라 별도 타입). `FindingDecision.evidence_refs` 는 대상 아님 |
| **Dependencies** | 없음 |
| **Input** | `raw["acceptance_criteria_ids"]`, `raw["evidence_refs"]` (이미 `_strings` 로 파싱됨) |
| **Output** | 변경 없음 (검증만 추가) |
| **Side Effects** | 없음 |
| **Failure Modes** | `ContractViolationError` — `RETRYABLE` 로 분류되어(`failure.py`) 워커 재요청 경로를 탄다 |
| **Validation** | `V-R8d-01` 둘 다 비면 거부 · `V-R8d-02` `acceptance_criteria_ids` 만 있으면 통과 · `V-R8d-03` `evidence_refs` 만 있으면 통과 · `V-R8d-04` 기존 계약 테스트 회귀 없음 |

**검사 위치를 `_parse_finding` 내부로 두는 이유:** 호출처가 하나뿐이며, `Finding`
객체가 성립하는 유일한 관문이다. `parse_review_artifact` 상위에 두면 `_tuple` 이
만들어낸 context 문자열(`review.findings[i]`)을 잃는다.

---

### `B-R8-2` `B5` 지속 escalation (`R-8a`)

| 항목 | 내용 |
|---|---|
| **Rationale** | `E-05` 의 signature 기반 근사치가 `B5` 를 구조적으로 놓친다 (Phase 1 §2.2) |
| **Objective** | `blocking_reason == B5` 이고 미해결 관측 이력이 2회 이상이면 `E-05` 발화 |
| **Scope** | `orca_loop/ledger.py::commit_round` — 기존 `E-05` 블록(`596-607`) **직후** |
| **Exclusions** | 기존 signature 기반 `E-05` 블록은 **수정하지 않는다** |
| **Dependencies** | 없음 |
| **Input** | `record.finding.blocking_reason`, `history` (지역 변수, 이번 round observation 포함) |
| **Output** | `escalations` 리스트에 `EscalationTrigger` 추가 |
| **Side Effects** | 없음 (순수 함수) |
| **Failure Modes** | 없음 — 조건 불일치 시 아무것도 하지 않는다 |
| **Validation** | `V-R8a-01`~`04` (Phase 1 §4.1) |

**dedup key:** `E-05:B5:{finding_id}`. signature 를 키에서 제외하므로 표현을
바꿔도 finding 당 1회만 발화한다. 기존 키 `E-05:{id}:{signature}` 와 접두어가
달라 충돌하지 않는다.

**임계값:** `len(history) >= 2`. `history` 는 `record.unresolved_signature_history`
에 이번 round observation 을 더한 값이므로 (`ledger.py:578`) "미해결 상태로
커밋된 유효 round 수"다. `E-01` 이 쓰는 술어와 동일하다.

---

### `B-R8-3` `B4` → `E-04` 자격 확대 (`R-8b`)

| 항목 | 내용 |
|---|---|
| **Rationale** | 두 축(`blocking_reason`, `impact_class`)이 같은 우려를 독립적으로 진술한다. 하나만 요구하면 단일 오분류가 escalation 을 무력화한다 (Phase 1 §2.3) |
| **Objective** | `E-04` 발화 자격을 `impact_class == security_auth` **또는** `blocking_reason == B4` 로 확대 |
| **Scope** | `orca_loop/ledger.py::commit_round` — 기존 `E-04` 블록(`629-641`)의 조건식만 수정 |
| **Exclusions** | `_conflicting_sides(record, next_round)` 요건은 **유지**. dedup key `E-04:{finding_id}` **유지** |
| **Dependencies** | 없음 |
| **Input** | `record.finding.impact_class`, `record.finding.blocking_reason` |
| **Output** | 기존과 동일한 `EscalationTrigger` |
| **Side Effects** | 없음 |
| **Failure Modes** | 없음 |
| **Validation** | `V-R8b-01`~`03` (Phase 1 §4.1) |

**reason 문자열:** 기존 `security or authentication policy disagreement` 를
유지한다. `E-04` 의 문서상 정의(`phase1-system-design.md:1066`)와 일치하며,
어느 축으로 걸렸는지는 finding 자체에서 확인 가능하다.

---

### `B-R8-4` 계약 변경 finding → `E-03` (`R-8c`)

| 항목 | 내용 |
|---|---|
| **Rationale** | `phase1-system-design.md:1065` 와 `prompts/plan_reviewer.md:96` 이 규정·약속했으나 미구현. `db_schema`/`external_api` 는 현재 dead enum (Phase 1 §2.4) |
| **Objective** | 미해결 finding 의 `impact_class ∈ {db_schema, external_api}` 이면 `E-03` 발화 |
| **Scope** | `orca_loop/ledger.py::commit_round` — `E-04` 블록 직후 |
| **Exclusions** | `coordinator.py:708-745` 의 기존 `E-03`(계획서 §6, destructive)은 **수정하지 않는다** |
| **Dependencies** | 없음 |
| **Input** | `record.finding.impact_class` |
| **Output** | `escalations` 리스트에 `EscalationTrigger` 추가 |
| **Side Effects** | 없음 |
| **Failure Modes** | 없음 |
| **Validation** | `V-R8c-01`~`02` (Phase 1 §4.1) |

**상충 요건 없음:** 계약 변경은 이견이 아니라 승인 대상이다. `E-02` 가
`requirement_interpretation` 을 무조건 발화시키는 것과 동일한 형태다.

**dedup key:** `E-03:{finding_id}`. `coordinator.py` 의 키는
`E-03:{plan_version}:{digest}` 와 `E-03:destructive:{plan_version}:{digest}` 이므로
세 형태가 모두 구별된다.

---

### `B-R8-5` 프롬프트 갱신

| 항목 | 내용 |
|---|---|
| **Rationale** | 리뷰어가 코드 선택의 결과를 모르면 합의 입력으로서 신뢰도가 떨어진다. `code_reviewer.md` 의 `db_schema` 행은 현재 사실과 다르다 |
| **Objective** | 3개 프롬프트의 `blocking_reason` 절과 escalation 표를 실제 동작과 일치시킨다 |
| **Scope** | `prompts/code_reviewer.md`, `prompts/cross_confirmer.md`, `prompts/plan_reviewer.md` |
| **Exclusions** | `planner.md`, `implementer.md` 는 finding 을 생산하지 않으므로 대상 아님. 출력 필드 목록·JSON 스키마 부분은 변경 없음 |
| **Dependencies** | `B-R8-1`~`B-R8-4` (문서가 구현을 따라간다) |
| **Input/Output** | 마크다운 텍스트 |
| **Side Effects** | 없음 |
| **Failure Modes** | 없음 |
| **Validation** | `V-R8p-01` 3개 파일 모두 `B4`·`B5` 결과 문장 존재 · `V-R8p-02` `code_reviewer` 의 `db_schema` 행이 `E-03` 로 정정됨 · `V-R8p-03` 증거 의무가 권고가 아닌 거부 사유로 진술됨 |

**추가할 내용 (3개 파일 공통):**

1. `B4` 정의 뒤 — 양측 상충 시 `impact_class` 와 무관하게 `E-04` 로 사용자에게 간다
2. `B5` 정의 뒤 — 두 번째 유효 round 까지 미해결이면 `E-05` 로 사용자에게 간다.
   판정 불가를 회피 수단으로 쓰지 못하게 하는 강제 장치다
3. 증거 의무 — `acceptance_criteria_ids` 와 `evidence_refs` 가 **둘 다 비면
   산출물 전체가 거부된다** (기존 권고 문장을 강화)

**`plan_reviewer.md` 한정:** `db_schema`/`external_api` 행은 이미 `E-03` 로
적혀 있으므로 변경 불필요.

**`code_reviewer.md`·`cross_confirmer.md` 한정:** 해당 행을
`contract-change review at the plan level` 에서 `E-03` 로 정정.

---

### `B-R8-6` 테스트

| 항목 | 내용 |
|---|---|
| **Rationale** | 합의 로직 변경은 회귀 위험이 가장 큰 영역이다 |
| **Objective** | Phase 1 §4.1 의 검증 ID 를 모두 실행 가능한 테스트로 구현 |
| **Scope** | `tests/test_ledger.py` (`R-8a`~`R-8c`), `tests/test_contracts.py` (`R-8d`) |
| **Exclusions** | 프롬프트는 실행 가능한 계약이 아니므로 자동 테스트 대상 아님 — 수동 대조로 검증 |
| **Dependencies** | `B-R8-1`~`B-R8-4` |
| **Input** | 기존 `tests/test_ledger.py::finding()` 헬퍼 (파라미터화 필요) |
| **Output** | 테스트 케이스 |
| **Side Effects** | 없음 |
| **Failure Modes** | 기존 375 테스트 중 하나라도 실패하면 회귀 |
| **Validation** | 전체 스위트 실행 |

**헬퍼 변경:** `tests/test_ledger.py:34` 의 `finding()` 은 현재
`blocking_reason=BlockingReason.B1`, `impact_class=ImpactClass.NONE` 고정이다.
`blocking_reason`, `impact_class`, `root_cause` 를 키워드 인자로 노출한다.
**기존 호출부는 기본값이 동일하므로 수정 불필요.**

---

## 3. 구현 순서와 결합도

```
B-R8-1 (contracts)  ─┐
B-R8-2 (ledger E-05) ├─ 서로 독립. 순서 무관
B-R8-3 (ledger E-04) │
B-R8-4 (ledger E-03) ─┘
        ↓
B-R8-6 (tests)  ← 4개 블록 전부에 의존
        ↓
B-R8-5 (prompts) ← 구현이 확정된 뒤 문서화
```

`B-R8-2`, `B-R8-3`, `B-R8-4` 는 `commit_round` 의 같은
`for record in updated.findings` 루프 본문을 건드리므로 **한 번에 편집한다.**

---

## 4. Validation and Risks

### 4.1 Validation Performed (현 시점)

| 항목 | 상태 |
|---|---|
| `blocking_reason` 참조 지점 전수 조사 | **PASS** — `models.py:594`, `contracts.py:844/885`, `reporting.py:174` 3곳 확인 |
| `ImpactClass.*` 참조 지점 전수 조사 | **PASS** — `ledger.py:608/618/631` 3곳뿐, `DB_SCHEMA`/`EXTERNAL_API` 참조 0 |
| `_parse_finding` 호출처 조사 | **PASS** — `contracts.py:1076` 단일 |
| 기존 `E-05` 억제 경로 확인 | **PASS** — `ledger.py:486`, `497` 코드 확인 |
| 신규 코드 실행 | **NOT RUN** — Phase 4 대상 |

### 4.2 Risks

| ID | 항목 | 완화 |
|---|---|---|
| `RK-4` | `B-R8-1` 이 기존 산출물을 거부해 진행 중인 run 이 막힘 | `ContractViolationError` 는 `RETRYABLE` 이므로 워커 재요청으로 흡수된다. `run_loop.py` 의 재시도 라우팅이 이미 처리 |
| `RK-5` | 한 finding 이 여러 escalation 을 동시에 발화 (`E-05`+`E-04`+`E-03`) | 설계상 정상. `_dedupe_escalations` 가 키 단위로 중복만 제거하며, `coordinator` 는 reason 을 모두 이어붙여 사용자에게 보고한다 |
| `RK-6` | `tests/test_ledger.py::finding()` 시그니처 변경이 기존 테스트를 깨뜨림 | 키워드 인자에 기존 값과 동일한 기본값을 주므로 호출부 수정 불필요 |

---

## 5. Approval Status

- [ ] Current phase approved
- [ ] Revision requested
- [ ] Permission granted to proceed to the next phase
