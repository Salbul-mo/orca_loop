# Task Report: `blocking_reason` 을 합의 입력으로 승격

**Current Phase:** 1. System Design
**Status:** Approved (2026-08-18)

---

## 1. Context and Objective

### 1.1 Goal

`blocking_reason` (`B1`~`B5`) 은 현재 리포트 출력에만 쓰인다. 리뷰어 프롬프트가
`B1`~`B5` 를 일관된 의미로 정의하게 된 이상, 이 코드를 합의(consensus) 판정의
입력으로 승격해 **현재 escalation 이 놓치는 구멍을 닫는다.**

### 1.2 Scope

`orca_loop/ledger.py` 의 `commit_round()` escalation 도출부. 필요 시
`orca_loop/coordinator.py` 의 `E-03` 도출부. 프롬프트 3종 문서 갱신, 테스트 추가.

**Exclusions:** enum 확장 없음 (`EscalationCode` 는 `E-01`~`E-08` 유지).
상태 전이표(`machine.py`) 변경 없음. 워커 출력 스키마 필드 추가/삭제 없음.

---

## 2. Deliverables — 검증된 현황과 설계

### 2.1 현황 (코드 확인 완료)

| 사실 | 근거 |
|---|---|
| `Finding.blocking_reason` 은 boundary dataclass 필드로 존재 | `orca_loop/models.py:594` |
| 파싱 시 enum 값 검증만 수행 | `orca_loop/contracts.py:844`, `885-889` |
| **유일한 읽기 지점이 리포트 헤더 문자열** | `orca_loop/reporting.py:174` |
| 합의 escalation 도출은 `impact_class` 만 읽음 | `orca_loop/ledger.py:608`, `618`, `631` |
| `ImpactClass.DB_SCHEMA`, `ImpactClass.EXTERNAL_API` 는 **전 소스에서 한 번도 참조되지 않음** | `grep -rn "ImpactClass\." orca_loop/ run_loop.py worker_runner.py` → `608/618/631` 3곳뿐 |

### 2.2 문제 1 — `B5` 는 결함 주장이 아닌데 결함처럼 처리된다

`B1`~`B4` 는 "산출물이 틀렸다"는 주장이다. revise/fix round 가 고칠 수 있다.
`B5` 는 "주어진 근거로는 판정 자체가 불가능하다"는 주장이다. 산출물을 고쳐도
**근거가 보강되지 않으면 구조적으로 해소되지 않는다.**

현재 루프는 `B5` finding 을 `B1` 과 완전히 동일하게 취급한다. 미해결로 계수하고
`PLAN_REVISE`/`FIX` round 를 소비시킨다.

`E-05`(동일 문제 2회 반복)가 이를 잡아줄 것 같지만 **잡지 못한다.**

```
orca_loop/ledger.py:486,497
    signature_changed = previous.signature != current_signature
    return signature_changed or scope_shrunk or status_advanced or side_improved
```

`signature` 의 입력은 `root_cause` 다 (`finding_signature()`, `ledger.py:78`).
`B5` 를 낸 리뷰어는 다음 round 에서 "여전히 판정 불가, 이번엔 X 때문에" 로
**자연스럽게 표현을 바꾼다.** → `signature_changed=True` → `material_progress=True`
→ `E-05` 영구 억제. 루프는 round 상한(5)을 전부 소진한 뒤에야 `U-01` 로 넘어간다.

### 2.3 문제 2 — `B4` 와 `impact_class` 가 서로를 교차 검증하지 않는다

`E-04`(보안·인증 이견)의 유일한 발화 조건:

```
orca_loop/ledger.py:630-633
    record.finding.impact_class is ImpactClass.SECURITY_AUTH
    and _conflicting_sides(record, next_round)
```

리뷰어가 `blocking_reason="B4"`(보안·무결성·호환성 위험)로 정확히 신고하면서
`impact_class="none"` 으로 잘못 분류하면 — 양측이 상충해도 escalation 경로가
**침묵으로 사라진다.** 프롬프트가 경고하는 바로 그 상황이다:

> choosing the wrong class silently removes the coordinator's ability to escalate.

두 축은 독립적으로 같은 우려를 진술한다. 하나만 요구하면 단일 오분류가
escalation 을 무력화한다.

### 2.4 문제 3 — 프롬프트가 약속한 `E-03` 이 구현되어 있지 않다

`prompts/plan_reviewer.md:96` 이 리뷰어에게 약속하는 표:

| `impact_class` | What the coordinator may raise |
| --- | --- |
| `db_schema`, `external_api` | `E-03` user approval for the contract change |

`phase1-system-design.md:1065` 도 동일하게 규정한다:

> `E-03` … coordinator가 `impact_class ∈ {db_schema, external_api}` **또는**
> `plan.md §6 != "없음"` 을 탐지

실제 구현(`coordinator.py:708-745`)은 `plan.data_api_schema_changes` 와
destructive operation 만 본다. **finding 의 `impact_class` 는 보지 않는다.**
`db_schema`/`external_api` 는 파싱만 되고 아무 일도 일으키지 않는 dead enum 이다.

계획서 §6 이 "없음"인데 리뷰어가 "이 구현은 스키마를 바꾼다"를 `db_schema` 로
신고하는 경우 — 즉 **계획이 놓친 계약 변경** — 이 정확히 사라지는 경로다.

---

## 3. 설계

### 3.1 규칙 R-8a — `B5` 지속 시 `E-05` (신규 발화 조건)

```
if record.finding.blocking_reason is BlockingReason.B5 and len(history) >= 2:
    escalate(E-05, key=f"E-05:B5:{finding_id}")
```

- **코드:** `E-05` 재사용. 문서상 `E-05` 의 취지는 "동일 문제 2회 반복 →
  예산과 무관하게 즉시 사용자 전달"이며(`phase1-system-design.md:1067`),
  signature 일치 검사는 그 취지의 *근사치*일 뿐이다. `B5` 는 그 근사치가
  구조적으로 놓치는 부분집합이므로 같은 코드가 맞다. **enum 확장 불필요.**
- **임계값 2:** `len(history) >= 2` 는 "2개의 유효 round 를 살아남았다" 이며
  `E-01` 이 이미 쓰는 술어와 동일하다. round 1 커밋 후 revise 1회를 정당한
  보강 기회로 인정하고, 두 번째에도 동일하면 넘긴다. 즉시(`>=1`) escalation 은
  계획 검토의 `B5`(문서 불완전 → planner 가 실제로 고칠 수 있음)를 과잉 차단한다.
- **dedup key:** `E-05:B5:{finding_id}` — signature 를 키에서 뺀다. 표현을
  바꿔도 finding 당 정확히 1회만 발화한다. 기존 `E-05:{id}:{signature}` 와 충돌 없음.

### 3.2 규칙 R-8b — `B4` 를 `E-04` 의 두 번째 트리거로 추가

```
(impact_class is SECURITY_AUTH or blocking_reason is B4)
    and _conflicting_sides(record, next_round)
```

- **상충 요건은 유지한다.** 양측이 합의한 `B4` finding 은 그냥 고치면 되는
  finding 이지 escalation 대상이 아니다. 넓히는 것은 *어떤 finding 이 자격이
  있는가* 뿐이다.
- dedup key 는 기존 `E-04:{finding_id}` 를 그대로 쓴다. 두 축이 모두 걸려도 1회.
- **비대칭 비용:** 놓친 보안 escalation > 불필요한 게이트 1회. `failure.py` 의
  분류 기본값과 같은 원칙이다.

### 3.3 규칙 R-8c — `impact_class ∈ {db_schema, external_api}` → `E-03`

```
if record.finding.impact_class in {DB_SCHEMA, EXTERNAL_API}:
    escalate(E-03, key=f"E-03:{finding_id}")
```

- 위치는 `ledger.commit_round()`. `coordinator.py` 의 기존 `E-03`(계획서 §6,
  destructive)은 **그대로 둔다.** 두 경로는 서로 다른 입력(계획 문서 vs finding)을
  보며 dedup key 접두어가 달라 충돌하지 않는다.
- `_conflicting_sides` 요건 **없음.** 계약 변경은 이견이 아니라 승인 대상이다.
  `E-02` 가 `requirement_interpretation` 을 무조건 발화시키는 것과 같은 형태다.
- 이 규칙은 `blocking_reason` 이 아니라 `impact_class` 를 쓴다. 요청 범위 밖이나,
  같은 함수·같은 테스트 파일을 건드리고 **프롬프트의 거짓 약속을 참으로 만든다.**

### 3.4 프롬프트 갱신

`prompts/code_reviewer.md`, `prompts/cross_confirmer.md`, `prompts/plan_reviewer.md`
의 escalation 표에 새 결과를 명시한다. 리뷰어가 결과를 모르는 채 코드를 고르면
합의 입력으로서의 신뢰도가 떨어진다.

- `B5` 항목에 "두 번째 round 까지 미해결이면 `E-05` 로 사용자에게 넘어간다" 추가
- `B4` 항목에 "양측 상충 시 `E-04`" 추가
- `code_reviewer` 의 `db_schema`/`external_api` 행을 `E-03` 으로 정정
  (현재 "contract-change review at the plan level" 로 모호하게 적혀 있음)

### 3.5 규칙 R-8d — 증거 의무 계약 검사

`phase2-macro-blocking.md:326` 이 규정하고 **구현되지 않은** 검사:

```
if not (f.acceptance_criteria_ids or f.evidence): raise CV("bad_blocking_reason")
```

`phase1-system-design.md:433`: "blocking finding 은 `acceptance_criteria_ids`
또는 구체적 결함 증거를 반드시 참조한다." 원칙 3(승인 의무)의 강제 수단이다.

이것은 합의 로직이 아니라 **계약 계층**이며, `blocking_reason` 을 신뢰할 수 있게
만드는 전제다. 적용하면 근거 없는 finding 이 `ContractViolationError` 로 거부되고
`RETRYABLE` 경로를 타 워커에게 재요청된다. 프롬프트는 이미 모든 finding 에
`file:line` evidence 를 요구하므로 규약을 지키는 워커는 영향받지 않는다.

**리스크:** 기존에 통과하던 산출물을 거부하게 되는 계약 강화다.

---

## 4. Validation and Risks

### 4.1 예정 검증

| ID | 내용 |
|---|---|
| `V-R8a-01` | `B5` finding 이 round 1 커밋 후 escalation 없음 |
| `V-R8a-02` | round 2 커밋 시 `E-05` 발화, key 가 `E-05:B5:{id}` |
| `V-R8a-03` | `root_cause` 를 바꿔 signature 를 변경해도 여전히 발화 (기존 `E-05` 가 억제되는 조건에서) |
| `V-R8a-04` | `B1` finding 은 2 round 지속해도 이 경로로 발화하지 않음 |
| `V-R8b-01` | `B4` + `impact_class=none` + 양측 상충 → `E-04` |
| `V-R8b-02` | `B4` + 양측 합의 → escalation 없음 |
| `V-R8b-03` | `security_auth` 기존 경로 회귀 없음 |
| `V-R8c-01` | `db_schema` finding → `E-03`, 상충 여부 무관 |
| `V-R8c-02` | `coordinator` 의 계획서 §6 경로와 dedup key 가 충돌하지 않음 |
| 전체 | 기존 375 테스트 회귀 없음 |

### 4.2 Risks & Open Questions

| ID | 항목 | 판단 |
|---|---|---|
| `RK-1` | `B5` 남용 리뷰어가 루프를 2 round 만에 정지시킬 수 있다 | 프롬프트가 이미 "Never use `B5` to avoid taking a position" 로 금지. 정지 결과는 `USER_DECISION_REQUIRED`(재개 가능)이지 실패가 아니다 |
| `RK-2` | `E-04` 확대로 게이트 빈도 증가 | 상충 요건을 유지하므로 증가폭은 "B4 인데 impact_class 를 틀리게 단 finding 이 양측 상충한 경우"로 한정 |
| `RK-3` | `E-03` 신설로 계획 단계 게이트 증가 | 설계 문서가 원래 규정한 동작이며 프롬프트가 이미 약속한 동작이다 |
| `OQ-1` | R-8c(`impact_class` → `E-03`)를 이번 작업에 포함할 것인가 | **확정 — 포함 (사용자 승인 2026-08-18)** |
| `OQ-2` | §3.5 증거 의무 계약 검사를 이번 작업에 포함할 것인가 | **확정 — 포함 (사용자 승인 2026-08-18)** |

---

## 5. Approval Status

- [x] Current phase approved
- [ ] Revision requested
- [x] Permission granted to proceed to the next phase

`OQ-1`, `OQ-2` 모두 **포함**으로 확정. 최종 범위는 R-8a, R-8b, R-8c, R-8d 4건.
