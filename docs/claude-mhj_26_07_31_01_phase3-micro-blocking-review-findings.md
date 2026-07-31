# Review Findings: Phase 3 Micro Blocking (Claude 검수)

**검토 대상:** `docs/codex-mhj_26_07_31_03_phase3-micro-blocking.md` Revision 2
**대조 baseline:** `docs/phase1-system-design.md` Revision 6, `docs/phase2-macro-blocking.md` Revision 6
**검토자:** Claude (`claude-mhj`)
**검토일:** 2026-07-31
**검토 방식:** 3개 문서 전문 정독 + 문서 간 계약 전수 대조 + grep 기반 정적 검사
**판정:** **Revision 요청** — §2 재작성 전 승인 비권장

---

## 1. 요약

프로세스·구조 측면(37개 Micro Block, 16 필수 필드, Phase 4 구현 순서, `C3-01`~`C3-07` 보정)은
정확하게 작성되어 있으며 실측 검사로 확인했다. `C3-01`(step_id 선발급으로 dispatch race 제거),
`C3-02`(ledger propagation), `C3-03`(exact allowlist + sanitized env), `C3-05`(permission first gate)는
실제 결함을 정확히 겨냥한 개선이다.

그러나 **§2 "Shared Boundary Exact Type Contracts"가 Phase 1 §8 스키마의 부분집합**이다.
이 문서를 그대로 승인해 구현하면 다음이 코드에서 사라진다.

- 원칙 3 승인 의무의 강제 근거 (`blocking_reason` `B1`~`B5`)
- 원칙 4 escalation 8종 중 `E-01`~`E-04`, `E-07`, `E-08`의 입력 데이터
- `REV2-002` per-finding dual approval의 완전성 검사 (`reviewed_finding_ids`)
- `REV2-003` round 단일 계수 (state가 round를 보관하지 않는다는 불변식)
- `E-05` 반복 판정의 per-finding signature history

§2는 "모든 Micro Block에서 동일한 의미로 사용한다"고 선언한 단일 기준이므로, 여기서 누락된
필드는 Phase 4 구현자에게 전달되지 않는다. 수정 범위는 §2 재작성 1건과 Micro Block 4개 추가로
한정된다.

---

## 2. 실측 확인된 정상 항목

| 검사 | 상태 | 근거 |
|---|---|---|
| Micro Block 수 / ID 중복 | **PASS** | `### M-Bxx-xx` 헤더 37건, `Rollback` 필드 37건 → 16필드 누락 0 |
| Parent coverage | **PASS** | `B-00`~`B-15` 전부 1개 이상 매핑 |
| Phase 4 구현 순서의 topological 유효성 | **PASS** | 각 블록 `Preconditions` 기준 전수 대조, 순서 위반 0 |
| Trailing whitespace | **PASS** | Phase 1·2·3 합계 0 (다른 리뷰 문서에는 6건 존재) |
| Target file traceability | **PASS** | Phase 2의 모든 대상 모듈이 1개 이상 Micro Block에 매핑 |
| 5/5 round 상한, 조기 합의, `E-05` 2회차 규칙 | **PASS** | Phase 1 `Q-1`, Phase 2 `B-14` 기본값과 일치 |
| `NOT_RUN` 보존 (`Q-2`) | **PASS** | `M-B12-03`, `M-B13-01`, §6.1 reviewer test status 행이 일관 |

순서 검증에서 확인한 주요 edge: `M-B09-02←M-B04-01`(4단계<8단계), `M-B10-02←M-B12-01`(13<14),
`M-B15-01←M-B08-02`(7<18), `M-B05-02←M-B07-03`(6<10), `M-B11-02←M-B09-02`(8<11) 전부 유효.

---

## 3. P0 — 타입 계약이 자기 pseudocode 및 상위 문서와 모순

> 아래 식별자는 Phase 3 문서 전체 grep 결과 **0회 출현**을 확인했다:
> `escalation_signals`, `agrees_with_reviewer`, `blocking_reason`, `impact_class`,
> `addressed_findings`, `plan_change_required`, `test_failure_attribution`,
> `operational_retr*`, `artifact_ok`, `unresolved_zero`, `SignatureObservation`,
> `test_ids`, `opened_round`.
> `reviewed_finding_ids`는 663행 pseudocode에서 **1회만** 출현하며 타입 선언에는 없다.

### `P3M-001` — `ReviewArtifact`가 Phase 1 §8.6 스키마의 부분집합

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `ReviewArtifact` (320~330행) |
| **모순 대상** | Phase 1 §8.6, §6.3 `[2]`·`[3]`; Phase 2 `B-03` `parse_review()`; Phase 3 §4 `M-B03-02` 663행 |

누락 필드:

| 필드 | 요구 출처 | 없으면 불가능해지는 것 |
|---|---|---|
| `reviewed_finding_ids` | Phase 1 §8.6, `REV2-002` | `M-B03-02`의 "validate reviewed_finding_ids equals delivered unresolved IDs"가 존재하지 않는 필드를 검증. `V-B03-07`, `V-R3-05` 구현 불가 |
| `escalation_signals` | Phase 1 §6.3 `[3]`, §8.6 | 워커 신고형 `E-01`·`E-02`·`E-04`·`E-07`·`E-08` 전달 경로 소실. Phase 2 `B-07` `detect_escalations(ledger, art)`가 `art.escalation_signals`를 읽음 |
| `agrees_with_reviewer` | Phase 1 §8.6 말미, Phase 2 `B-05`·`B-07` | `cross_confirmer` 계약 자체가 성립 불가 |
| `run_id`/`task_id`/`dispatch_id` | Phase 1 §8.6 | artifact 본문 provenance 검증(`F-6` 대조) 불가. `ExpectedProvenance`만으로는 본문 위조를 잡지 못함 |
| `consensus_round` | Phase 1 §8.6 | 유효 round 판정(§9.4 조건 1) 입력 소실 |

**권고:** Phase 1 §8.6 JSON 필드를 1:1로 사상. `cross_review`는 `agrees_with_reviewer`를 갖는
별도 dataclass 또는 `ReviewArtifact`의 optional 필드로 선언한다.

---

### `P3M-002` — artifact용 `Finding` 타입 부재, 원장 타입으로 대체

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `FindingRecord` (272~280행), `ReviewArtifact.findings` |
| **모순 대상** | Phase 1 §8.6 finding 스키마, §9.5, §6.3 `[1]`; Phase 2 `B-03`·`B-07` |

`ReviewArtifact.findings: tuple[FindingRecord, ...]`로 선언되어 있으나 `FindingRecord`는
원장 레코드 타입(`status`, `max_status_reached`, `decisions`)이며 artifact finding이 아니다.
그 결과 다음 필드가 문서 전체에서 사라졌다.

| 필드 | 없으면 불가능해지는 것 |
|---|---|
| `blocking_reason` (`B1`~`B5`) | **원칙 3의 핵심 강제 수단.** "근거를 대지 못하는 지적은 정의상 non_blocking"이 검증 불가. Phase 2 `B-03`의 `bad_blocking_reason` reason, `V-B03-02` 소실 |
| `impact_class` | `E-01`(architecture), `E-02`(requirement_interpretation), `E-03`(db_schema/external_api), `E-04`(security_auth) **자동 탐지 전부 불가**. Phase 2 `B-07` `detect_escalations()`가 `rec.impact_class`로 분기 |
| `acceptance_criteria_ids` | `unresolved_signature` 계산 입력(Phase 1 §9.5). `material_progress (b)` 판정 불가 |
| `required_fix` / `required_change` | 동일. signature 4개 입력 중 2개가 없음 |
| `test_ids` | `unresolved_scope_package()`의 `test_ids` union 불가 (`P3M-013` 참조) |
| `severity`, `file`, `line`, `description`, `evidence`, `reopens` | 사용자 결정 문서(§17.2 9항 "관련 source/test/evidence"), `E-06` 재개봉 탐지 |

**권고:** `Finding`(artifact 경계)과 `FindingRecord`(원장 상태)를 분리 선언하고,
`FindingRecord`가 `Finding`을 포함하거나 필요한 필드를 승계하도록 한다.

---

### `P3M-003` — `FindingDecision`에 `snapshot_digest`·`round` 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `FindingDecision` (265~270행) |
| **모순 대상** | Phase 1 §8.6 `finding_decisions[]`, §9.5 RESOLVED 조건; Phase 2 `B-07` `_resolve_status()`, `compute_material_progress()` |

Phase 1 §9.5의 `RESOLVED` 조건은 "양측 `APPROVE` **AND 두 decision의 `snapshot_digest`가 동일**
AND evidence 존재"이며, Phase 2 `_resolve_status()`는
`c.snapshot_digest == x.snapshot_digest == snapshot_digest`를 비교한다.
`compute_material_progress()`의 `(d)` 조건은 `rec.decisions[s].improved_since(prev.round)`로
decision의 `round`를 요구한다. 두 필드 모두 없다.

결과: stale snapshot에 대한 승인이 `RESOLVED`로 통과할 수 있고(`V-R3-03` 위반),
`material_progress (d)`가 계산 불가하다.

**권고:** `snapshot_digest: str`, `round: int` 추가.

---

### `P3M-004` — `ConsensusLedger`의 signature가 finding 단위가 아니라 원장 단위

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `ConsensusLedger.unresolved_signature: str` (282~287행) |
| **모순 대상** | Phase 1 §8.8, §9.5; Phase 2 `B-07` `commit_round()`; Phase 3 §4 `M-B07-02` pseudocode |

`E-05`는 **finding 단위** 규칙이다(Phase 1 §9.5: "각 유효 round 종료 시 다음 값으로
`unresolved_signature`를 만든다", §8.8: `unresolved_signature_history[]`).
Phase 3는 이를 원장 전체의 단일 문자열로 축약했다.

동시에 `M-B07-02` pseudocode는 "append observation / if last two observations have same
signature and no progress append E-05"를 요구하는데, observation을 담을
**`SignatureObservation` 타입이 §2에 없고** `FindingRecord`에 history 필드도 없다.

추가 누락(Phase 1 §8.8 대비): `informational`, `reopened`, `generation`, `run_id`,
`opened_round`, `resolved_round`, `resolved_snapshot_digest`, `resolution`, `impact_class`.
`FindingStatus.INFORMATIONAL` enum은 존재하지만 이를 보관할 컨테이너가 없다.
또한 `ReviewArtifact.non_blocking_suggestions: tuple[str, ...]`는 평문 문자열인 반면
Phase 2 `B-07`은 `ledger.informational.add(s.id)`로 ID를 요구한다.

**권고:** `SignatureObservation` 선언 + `FindingRecord.unresolved_signature_history` 추가,
원장 단일 `unresolved_signature` 필드 삭제, `informational`/`reopened` 복원,
`non_blocking_suggestions`를 `id`를 갖는 타입으로 변경.

---

### `P3M-005` — `ImplementationArtifact`가 `E-07`·`E-08` 입력을 갖지 않음

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `ImplementationArtifact` (332~340행) |
| **모순 대상** | Phase 1 §8.7, §9.8; Phase 2 `B-07` `apply_implementation()` |

| 누락 필드 | 결과 |
|---|---|
| `status ∈ {IMPLEMENTED, HALTED_FOR_ESCALATION}` | `E-08` 신고 시 "구현 중단" 상태를 표현할 수 없음 |
| `addressed_findings[]` | finding을 `VERIFY_REQUIRED`로 승격하는 유일한 경로 소실. `apply_implementation()` 구현 불가 |
| `plan_change_required` | `E-08` 트리거 소실 |
| `test_failure_attribution` | `E-07` 트리거 소실 (Phase 2 `B-09`의 `attribution_hint`도 Phase 3 `TestGateResult`에서 함께 삭제되어 이중 소실) |
| `escalation_signals` | 워커 신고 경로 소실 |
| provenance 3종, `consensus_round` | `P3M-001`과 동일 |

역방향 문제: Phase 3는 `test_gate_status: TestGateStatus`를 **구현자 산출물에** 넣었다.
`TEST_GATE`는 Phase 1 §9.1·§9.3에서 **coordinator 단계**이며, 구현자가 자기 테스트 결과를
신고하게 하면 `REV2-001`("검토자가 실행되지 않은 테스트를 `PASS`로 가정하지 않을 것")의
전제가 깨진다. `test_commands`를 구현 artifact에 두는 것도 승인된 `plan.md`의 Test Contract가
유일 출처라는 §8.1과 충돌한다.

**권고:** Phase 1 §8.7 필드로 교체하고 `test_gate_status`·`test_commands`를 제거.

---

### `P3M-006` — `SignalKind`가 Phase 2 전이표의 signal 집합과 불일치

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `SignalKind` (148~159행) |
| **모순 대상** | Phase 2 `B-08` 전이 테이블(706~730행), `transition()` pseudocode; Phase 3 `M-B08-01` |

Phase 2 전이표가 사용하는 signal: `ok`, `artifact_ok`, `unresolved_zero`, `unresolved_remain`,
`PASS`/`FAIL`/`NOT_RUN`/`POLICY_VIOLATION`, `merge`/`reject`/`revise_code`/`revise_design`,
`escalate`, `abort`.

Phase 3 `SignalKind`: `APPROVE`, `REVISE`, `CHANGES_REQUESTED`, `TEST_PASS`, `TEST_FAIL`,
`TEST_NOT_RUN`, `ESCALATE`, `MERGE`, `REJECT`, `REVISE_CODE`, `REVISE_DESIGN`.

문제 두 가지:

1. **누락:** `ok`(INIT→PLAN), `artifact_ok`(모든 워커 step의 주 신호), `unresolved_zero`/
   `unresolved_remain`(EVALUATE 결과), `abort`(→`FAILED`), `POLICY_VIOLATION`, operational
   retry 신호. `M-B08-01` pseudocode의 "if signal is abort return FAILED"는 존재하지 않는
   enum 값을 참조한다.
2. **혼입:** verdict(`APPROVE`/`REVISE`/`CHANGES_REQUESTED`)를 전이 signal로 넣었다.
   Phase 2 전이표는 `CODE_REVIEW → CROSS_CONFIRM`이 **"verdict 무관"** 임을 명시하고
   `V-B08-07`로 검증한다. verdict를 signal로 두면 이 불변식이 타입 수준에서 깨진다.

**권고:** `SignalKind`를 Phase 2 전이표 signal 집합으로 교체하고, verdict는
`ReviewVerdict`로만 유지한다.

---

### `P3M-007` — `LoopCounters`가 round를 보관해 `REV2-003` 위반

| 항목 | 내용 |
|---|---|
| **심각도** | P0 |
| **위치** | Phase 3 §2 `LoopCounters` (289~293행) |
| **모순 대상** | Phase 1 §8.9("`plan_round`/`code_round`는 **여기 없다.** 원장이 유일한 권위"), §9.4; Phase 2 `B-08` `V-B08-06`; Phase 1 `V-R3-07` |

`LoopCounters`는 `TransitionResult.counters_after`로 상태머신이 운반하는 값이며 `state.json`에
직렬화된다. 여기에 `plan_consensus_rounds`/`code_consensus_rounds`를 두면
`ConsensusLedger.plan_rounds`/`code_rounds`와 **이중 소스**가 되어 `REV2-003`이 해결하려던
문제가 그대로 재발한다. `M-B08-01` pseudocode의 "apply only non-ledger counters / read ledger
rounds for limit checks without mutation"은 올바른 의도이지만 타입이 이를 배반한다.

동시에 `operational_retries` 카운터가 누락되어 있다(Phase 1 §8.9, §9.4 상한 1).

**권고:** `LoopCounters`에서 round 2개 삭제, `operational_retries: int` 추가.
round는 `LedgerView`로만 읽는다.

---

## 4. P1 — 상위 요구가 어떤 Micro Block에도 배정되지 않음

### `P3M-008` — operational retry 경로 전체 부재

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 1 §10.3, §10.2 `U-06`, §10.1 `A-01`, `V-CONS-10`, `R-1`; Phase 2 `B-12` `_operational_retry_left()`·`_redispatch_with_reminder()` |

Phase 2 `B-12`가 명시한 "계약 위반 → 스키마·의무 재고지 후 재디스패치(round 미소비) → 반복 시
`escalate`" 경로가 Phase 3의 어떤 Micro Block에도 없다. `M-B12-02` pseudocode는
promote 실패 분기 자체를 담지 않는다. `LoopCounters`에 `operational_retries`도 없다
(`P3M-007`).

결과: `A-01`(retry 소진 후 `FAILED`), `U-06`(승인 의무 위반 반복 → `USER_DECISION_REQUIRED`),
`V-CONS-10`(retry가 round 미소비)이 구현·검증 불가.

**권고:** `M-B12-05 — Operational Retry and Contract Reminder Redispatch` 신설.

---

### `P3M-009` — `HUMAN_GATE` state 실행 분기의 주인 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 2 `B-12` `_execute_state()` (`if state is HUMAN_GATE`) |

`M-B12-02`는 워커 step 전용("prepare step workspace … dispatch … promote"),
`M-B12-03`은 "evaluate 또는 TEST_GATE"만 담당한다고 명시했다. `M-B13-02`는 gate lifecycle을
다루지만 coordinator가 `state == HUMAN_GATE`에서 이를 호출하는 분기는 어디에도 배정되지 않았다.

**권고:** `M-B12-03`의 Objective를 "non-worker states = EVALUATE / TEST_GATE / HUMAN_GATE"로
확장하거나 별도 Micro Block 신설.

---

### `P3M-010` — `M-B12-02`에 non-artifact completion 분기 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 2 `B-12` (`STEP_TIMEOUT`/`ESCALATION`/`DECISION_GATE` 3분기), `REV2-005`, Phase 1 §10.2 `U-03`·`U-09` |

Phase 2 `B-12`가 명시한 세 분기가 `M-B12-02` pseudocode에서 사라졌다
("dispatch, durable dispatch binding, wait completion / promote and guard artifact").
`CompletionKind` enum에는 네 값이 모두 있으나 소비 지점이 없다.

결과: native escalation이 `USER_DECISION_REQUIRED`로 라우팅되는 `REV2-005` 경로,
워커 `decision_gate` 처리(`U-03`), step timeout `abort` 경로가 코드 계약에서 소실.
`M-B10-03`의 `T-B10-07`(native escalation)은 dispatcher 반환값까지만 검증한다.

**권고:** `M-B12-02` pseudocode에 4분기 복원 + Output Validation에 명시.

---

### `P3M-011` — 파괴적 작업 승인 gate의 주인 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 1 §8.2("승인 계획에 명시된 삭제·rename도 실행 전 사용자의 파괴적 작업 승인을 요구"), §10.1 `A-04`; Phase 2 `B-11` `V-B11-07` |

`DestructiveApproval{approved_paths, decision_digest}` 타입은 선언되어 있고
`M-B04-02`·`M-B11-01`이 이를 **소비**하지만, 이 값을 **생성**하는 Micro Block이 없다.
`GateKind`는 `FINAL | ESCALATION` 2종뿐이라 파괴적 작업 gate를 표현할 수단도 없다.

결과: 계획에 명시된 `delete`/`rename`이 승인 없이 통과하거나(무검증), 항상 거부되어
정상 작업이 진행 불가한 상태 중 하나가 된다.

**권고:** `GateKind`에 `DESTRUCTIVE` 추가 + `M-B13-03 — Destructive Operation Approval Gate` 신설.

---

### `P3M-012` — `RoleContext`가 템플릿 placeholder를 채울 수 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **위치** | Phase 3 §2 `RoleContext` (356~365행) |
| **모순 대상** | Phase 2 `B-05` `RoleContext` 및 `render()` values dict; Phase 3 `M-B05-01`·`M-B05-02` |

누락: `worktree_path`, `step_dir`, `coordinator_handle`, `plan_version`.

`M-B05-01`은 템플릿이 "require output only under STEP_OUTPUT_DIR", "worker_done instruction"을
포함하도록 요구하고, `M-B05-02`의 Output Validation은 "no unresolved placeholder"다.
그러나 `STEP_DIR`/`OUT_DIR`/`IN_DIR`/`DIFF_PATH`/`COORDINATOR_HANDLE`/`PLAN_VERSION`/
`WORKTREE_PATH`를 공급할 필드가 `RoleContext`에 없으므로 두 요구는 동시에 만족될 수 없다.
특히 `COORDINATOR_HANDLE`은 `F-1`(핸들 자동 해석 금지) 대응의 핵심이다.

**권고:** Phase 2 `B-05` `RoleContext` 필드 전부 복원.

---

### `P3M-013` — `ScopePackage`가 자기 pseudocode보다 좁음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **위치** | Phase 3 §2 `ScopePackage` (350~354행) |
| **모순 대상** | Phase 1 §9.6; Phase 2 `B-07` `unresolved_scope_package()`; Phase 3 `M-B07-03` pseudocode |

선언된 필드는 `finding_ids`, `acceptance_criteria_ids`, `affected_files` 3개인데,
같은 문서 `M-B07-03` pseudocode는 "union exact acceptance criteria, affected files, **test IDs**"와
"include only **last-round conflict excerpts**"를 수행한다고 기술한다. `test_ids`와
`disagreement_excerpts`를 담을 필드가 없다.

Phase 1 §9.6은 "관련 targeted test 결과"와 "직전 round의 양측 상충 문장"을 포함 대상으로 명시한다.

**권고:** `test_ids`, `disagreement_excerpts` 추가.

---

### `P3M-014` — 대안 계획 탐지의 책임 위치가 불일치

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 1 §10.1 `A-11`, §6.1; Phase 2 `B-03` `parse_review()`의 `alternative_plan_detected`; Phase 3 `C3-04`, `M-B05-02` |

`C3-04`는 판정 기준을 구조화 section 검사로 정당하게 구체화했다. 그러나 이를
`roles.py`(`M-B05-02`)에 배치했는데, 해당 블록의 Input Type은
`RoleContext`, `ScopePackage`, `Path template_path`뿐이고 **reviewer artifact가 없다.**
`M-B05-02` pseudocode의 "for reviewer output reject only schema-disallowed sections"는
입력으로 받지 않은 객체를 검사한다. 동시에 `M-B03-02`(parser)에는 대안 계획 검사가 없다.

`roles.py`는 워커 **입력**을 렌더링하는 모듈이고 reviewer **출력** 검증은 `contracts.py`
경계다(Phase 2 `B-05` Exclusions: "verdict 판정, escalation 판정").

**권고:** 검사 로직을 `M-B03-02`(contracts)로 이동. `M-B05-02`는 순수 렌더러로 유지하고
`AlternativePlanViolation`을 예외 목록에서 제거.

---

### `P3M-015` — test policy 파일의 출처가 정의되지 않음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 1 §8.1 CLI 인자 목록, §6.0; Phase 3 `M-B09-01`·`M-B14-01` |

`M-B09-01` pseudocode는 "load policy from **coordinator CLI path** before workers start"라고
하지만, Phase 1 §8.1의 CLI 인자 목록에 해당 플래그가 없고 `M-B14-01`의 argv 검증 항목에도
없다(`M-B14-01`은 "load exact test policy"라고만 기술). `TestExecutionPolicy.policy_digest`의
계산 규칙도 미정의다.

Phase 1 §8.1은 "테스트 명령은 CLI 인자가 아니다"라고 명시하므로, **정책 파일 경로**를
CLI 인자로 추가하는 것은 §8.1 문구 갱신을 동반해야 한다(명령 자체가 아니라 allowlist 경로이므로
충돌은 아니지만 문구가 오해를 만든다).

**권고:** `--test-policy <path>` 인자를 Phase 1 §8.1·Phase 2 `B-14`·`M-B14-01`에 동시 추가하고
`policy_digest` 계산 규칙을 명시. 정책 파일 부재 시 `NOT_RUN`(`P3-R3`)은 이미 정의됨.

---

### `P3M-016` — `Q-5`(snapshot digest canonicalization)가 확정되지 않음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **모순 대상** | Phase 1 §15 `Q-5`("Phase 3에서 확정"), §8.4 digest 공식; Phase 3 `M-B04-01` |

Phase 1 §8.4는 문자열 연결 기반 공식을 이미 명시했고, Phase 3 `M-B04-01`은
"hash tagged length-prefixed components"라는 한 줄로 **다른 알고리즘**을 지시한다.
두 문서의 digest 값은 서로 다른 결과를 낳는다. Phase 3가 확정 책임을 위임받았으므로
바이트 수준 정규 공식(태그 문자열, 구분자, 길이 인코딩, 정렬 기준)을 명시해야 한다.

추가로 `M-B04-01` Output Validation의 "text CRLF→LF, binary raw bytes"는 Phase 1
`normalize()`(전부 CRLF→LF)와 다르며, text/binary 판별 규칙이 정의되지 않았다.

**권고:** 정규 공식 전문을 `M-B04-01`에 기재하고 Phase 1 §8.4를 동일 공식으로 갱신.
`Q-5` 상태를 "확정"으로 변경.

---

### `P3M-017` — `PermissionFeasibilityReport`에 digest·저장 경로 없음

| 항목 | 내용 |
|---|---|
| **심각도** | P1 |
| **위치** | Phase 3 §2 `PermissionFeasibilityReport` (211~216행), `M-B00-01`, `M-B02-02` |

`M-B02-02`의 Input Validation은 "permission report **digest**와 `PASS` strategy"를 요구하지만
타입에는 `status`, `strategy`, `checks`, `evidence`만 있다. `M-B00-01`의 Target Files는
"runtime `permission-feasibility.json`"이라고만 하고 경로를 정의하지 않으며,
`M-B02-02`가 이를 어떻게 로드하는지도 미정의다. Orca `appVersion` 바인딩도 없어
버전이 달라진 뒤에도 과거 spike 결과를 재사용할 수 있다.

**권고:** `report_digest: str`, `orca_version: str` 추가 + canonical 경로 확정
(예: `runs/<run_id>/control/permission-feasibility.json` 또는 harness 루트 고정 경로).

---

## 5. P2 — 문서 정합 및 검증 주장

### `P3M-018` — §6.1.1의 "3-worker 표현 0" 주장이 사실과 다름

Phase 3 §6.1.1은 `Cross-document stale contract | PASS | old 'always PASS', 3-worker,
dispatch-path, reject-all-deletion 표현 0`이라고 보고한다. 그러나
**`phase1-system-design.md` 700행**에 다음이 그대로 남아 있다.

```json
"worker_handles":{"claude":"term_...","codex_impl":"term_...","codex_review":"term_..."},
```

Phase 1 Revision 6 §5.3은 워커 터미널이 **4개**(planner / code_reviewer 분리)라고 명시하므로
이 예시는 stale이다. 검사 패턴이 좁았거나 보고가 부정확하다.

동반 문제 — **`WorkerKey` 값 drift:**

| 문서 | 값 |
|---|---|
| Phase 3 §2 `WorkerKey.CODEX_IMPLEMENTER` | `"codex_implementer"` |
| Phase 2 `B-10` 워커 매핑(858행) | `codex_impl` |
| Phase 1 §8.9(700행) | `codex_impl` |

**권고:** Phase 1 §8.9 예시를 4키로 갱신, `WorkerKey` 값 하나로 통일,
§6.1.1 해당 행을 정정하고 실제 검사 패턴을 evidence에 기재.

---

### `P3M-019` — §3 의존 그림과 `Preconditions`가 실제로 불일치

`M-B15-01`의 `test_plan_traceability.py`가 `Preconditions`로 DAG를 재생성한다는 설계는 옳다.
그러나 §3 그림과 실제 `Preconditions` edge set이 다음과 같이 다르다.

| 항목 | §3 그림 | 실제 `Preconditions` |
|---|---|---|
| `M-B06-02` | `M-B06-01`만 | `M-B03-02`, `M-B06-01` |
| `M-B08-01` | `M-B07-03` → `M-B08-01` | `M-B03-01`, `M-B07-02` (`M-B07-03` 아님) |
| `M-B09-02` | `M-B09-01`만 | `M-B09-01`, `M-B04-01` |
| `M-B15-01` | `M-B14-02` → `M-B15-01` | `M-B14-02` 없음 (`M-B03-02`, `M-B04-02`, `M-B07-03`, `M-B08-02`, `M-B09-02`, `M-B11-02`) |
| `M-B15-02` | `M-B15-01` → `M-B15-02` | `M-B15-01` 없음 (`M-B12-04`, `M-B13-02`, `M-B14-02`) |
| `M-B10-03` | — | `M-B10-01`이 `Preconditions`에 M-* ID로 없음("terminal tui-idle"로만 표현) |

§3은 스스로를 "별도 수기 edge 목록을 진실 원천으로 두지 않는다"고 선언하는데,
§6.1은 `Preconditions DAG | 문서에서 재생성한 edge set과 **canonical graph**/order exact match`를
요구한다. "canonical graph"의 정의가 없어 테스트 기준이 성립하지 않는다.

**참고:** Phase 4 **순서** 자체는 `Preconditions` 기준으로 위반 0임을 전수 확인했다.
문제는 그래프 비교 기준의 정의뿐이다.

**권고:** §6.1을 Phase 2 §2.1과 동일하게 "acyclic + undefined ID 0 + Phase 4 order가 유효한
topological order"로 한정하거나, §3을 실제 edge set으로 정정하고 canonical로 승격한다.
`M-B10-01 → M-B10-03` edge를 `Preconditions`에 M-* ID로 명시할 것.

---

### `P3M-020` — Phase 2에 없는 신규 모듈이 변경 목록에 선언되지 않음

Phase 3가 새로 도입한 모듈: `orca_loop/models.py`, `profiles.py`, `bootstrap.py`,
`workspace.py`, `generation.py`, `locking.py`.

이 중 `generation.py`(원자적 커밋)와 `locking.py`(run lock)는 부작용을 가진 핵심 모듈인데
Phase 2 `B-01`·`B-02`·`B-12`·`B-14`의 Scope에는 없다. §1.1 `C3-01`~`C3-07` 확정 목록에도
모듈 분리가 언급되지 않았다.

**권고:** §1.1에 `C3-08 — 모듈 분해` 항목을 추가하거나 Phase 2의 해당 블록 Scope를 갱신.

---

### `P3M-021` — Phase 2 `B-09` 환경 정책과 상충(미반영)

Phase 2 `B-09` 정책 표: `환경 | 부모 환경 상속. secret 주입하지 않음`
Phase 3 `C3-03` / `M-B09-02`: `construct env from fixed OS minimum plus policy allowed keys`
(sanitized env, `allowed_env_keys`)

Phase 3가 보안상 옳지만, Phase 2 Revision 6가 baseline으로 함께 승인되면 두 문서가
모순 상태로 확정된다. 또한 Phase 2 `TestGateResult.attribution_hint`/`infer_attribution()`이
Phase 3 `TestGateResult(status, command_results, before_snapshot, after_snapshot)`에서
삭제되어 `E-07` 입력이 `P3M-005`와 이중으로 소실된다.

**권고:** Phase 2 `B-09` 정책 표를 sanitized env로 갱신, `attribution` 필드 복원.

---

### `P3M-022` — §2가 선언하지 않은 boundary 타입 다수

§2는 "아래 type은 모든 Micro Block에서 동일한 의미로 사용한다"고 선언하지만, 다음은 필드
타입·예외로만 등장하고 정의가 없다.

```text
LedgerView, EscalationTrigger, LedgerUpdate, RoundEvidence, ConsensusKind,
ExpectedProvenance, Violation, TestPolicyViolation, PolicyValidation, Completion,
WorkerHandle, WorkerPool, PreparedTask, SignatureObservation, Finding, TestContract,
CoordinatorState, ResumeAction, ScopeManifest, E2EConfig, GuardReport, InputManifest,
DigestEntry, StagedInput, PromotedArtifact, RunWorkspace, StepWorkspace, BootstrapReport,
LaunchProfile, OrcaResponse, TestGateResult, FrozenReview, RenderedContract,
DecisionReport, GateBinding, ResumeDecision, CommitManifest
```

일부는 Output Type 셀에 인라인 시그니처가 있어 실질적으로 정의되지만
(`GuardReport(ok, violations)` 등), `LedgerView`·`EscalationTrigger`·`RoundEvidence`·
`ConsensusKind`·`Completion`·`WorkerHandle`·`SignatureObservation`·`Finding`·
`ExpectedProvenance`는 어디에도 없다. §6.1.1의 "11 named type definition 누락 0"은
검사 범위가 좁다.

**권고:** 최소한 위 9개를 §2에 추가하고, 인라인 정의 타입은 §2 참조 표로 색인.

---

### `P3M-023` — 잔여 소소 항목

| 항목 | 내용 |
|---|---|
| a | `M-B08-02` "max 256 transition" vs Phase 2 `B-15` `MAX_STEPS := 128` — 값 불일치(기능상 무해하나 문서 drift) |
| b | `M-B02-01` "run [resolved_executable, *argv, `--json`]" — 무조건 append. Phase 2 `B-02`는 `--json` 중복 방지 조건부. argv에 이미 있으면 중복 플래그 |
| c | `ORCA_DEV_REPO_ROOT` 해석 우선순위가 Phase 1 §12(`ORCA_CLI_COMMAND` → `orca` → `orca-ide`)에 없는 신규 도입인데 §1.1에 미선언 |
| d | `M-B07-02` Output Validation이 "plan/code 각각 `<=5`"로 상한을 하드코딩. `LoopConfig`는 `1..5` 가변이므로 원장은 주입된 limit을 사용해야 함 |
| e | `ReviewVerdict`가 단일 enum이라 plan(`APPROVE\|REVISE`)/code(`APPROVE\|CHANGES_REQUESTED`)별 허용값 제약을 타입으로 표현하지 못함. `M-B03-02`가 런타임 검증하도록 명시 필요 |
| f | `PlanDocument.acceptance_criteria_ids: tuple[str, ...]` — Phase 1 §8.2 섹션 9는 "안정적 `AC-*` ID **+ 검증 방법**"을 요구하는데 검증 방법이 소실 |
| g | `WorkerDonePayload`는 snake_case이나 wire format은 camelCase(`taskId`/`dispatchId`/`reportPath`/`artifactDigest`, Phase 1 §8.5). §2 말미의 "dataclass field 이름·enum 값을 대조하는 contract test"는 현재 규칙 그대로면 실패한다. JSON↔dataclass 매핑 규칙 명시 필요 |
| h | `E-03` 특례(`plan.md §6 != "없음"` → `IMPLEMENT` 진입 전 발화, 사용자 승인 후 같은 run 내 재발화 금지)가 어떤 Micro Block에도 배정되지 않음. `PlanDocument.data_api_schema_changes` 필드는 존재 |

---

## 6. 수정 체크리스트

**A. §2 재작성 (P0 7건 — 승인 전 필수)**

- [ ] `Finding`(artifact) / `FindingRecord`(ledger) 분리, `SignatureObservation` 추가
- [ ] `ReviewArtifact`에 `reviewed_finding_ids`·`escalation_signals`·`agrees_with_reviewer`·provenance·`consensus_round` 복원
- [ ] `FindingDecision`에 `snapshot_digest`·`round` 추가
- [ ] `ConsensusLedger`에 per-finding history·`informational`·`reopened`·`generation` 복원, 원장 단일 `unresolved_signature` 삭제
- [ ] `ImplementationArtifact`를 Phase 1 §8.7 필드로 교체, `test_gate_status`/`test_commands` 제거
- [ ] `SignalKind`를 Phase 2 전이표 signal 집합으로 교체
- [ ] `LoopCounters`에서 round 삭제, `operational_retries` 추가
- [ ] `RoleContext`에 `worktree_path`·`step_dir`·`coordinator_handle`·`plan_version` 복원
- [ ] `ScopePackage`에 `test_ids`·`disagreement_excerpts` 추가
- [ ] `PermissionFeasibilityReport`에 `report_digest`·`orca_version` 추가
- [ ] 미정의 boundary 타입 9종 선언
- [ ] JSON(camelCase) ↔ dataclass(snake_case) 매핑 규칙 명시

**B. Micro Block 추가/수정 (P1)**

- [ ] `M-B12-05` operational retry 신설
- [ ] `M-B12-03`에 `HUMAN_GATE` 실행 분기 포함
- [ ] `M-B12-02` pseudocode에 `STEP_TIMEOUT`/`ESCALATION`/`DECISION_GATE` 4분기 복원
- [ ] `M-B13-03` destructive approval gate 신설 + `GateKind.DESTRUCTIVE`
- [ ] 대안 계획 탐지를 `M-B05-02` → `M-B03-02`로 이동
- [ ] `--test-policy` 인자 정의(Phase 1 §8.1 동반 수정) + `policy_digest` 규칙
- [ ] `M-B04-01`에 snapshot digest 정규 공식 전문 기재 → `Q-5` 확정
- [ ] `E-03` 특례 담당 블록 지정

**C. 문서 정합 (P2)**

- [ ] Phase 1 §8.9 `worker_handles` 3키 → 4키 갱신
- [ ] `WorkerKey` 값 통일 (`codex_impl` 또는 `codex_implementer`)
- [ ] §6.1 DAG 비교 기준을 Phase 2 §2.1과 동일하게 한정하거나 §3을 canonical edge set으로 정정
- [ ] §6.1.1 "3-worker 표현 0" 행 정정 + 실제 검사 패턴 evidence 기재
- [ ] Phase 2 `B-09` 환경 정책을 sanitized env로 갱신, `attribution` 복원
- [ ] 신규 모듈 6종을 §1.1 또는 Phase 2 Scope에 선언
- [ ] `P3M-023` a~h 처리

---

## 7. Validation and Risks

### 7.1 Validation Performed

| 항목 | 상태 | 근거 |
|---|---|---|
| 3개 문서 전문 정독 및 계약 전수 대조 | **PASS** | Phase 1 1344행, Phase 2 1435행, Phase 3 1679행 |
| Micro Block 수·필수 필드 grep 검사 | **PASS** | 헤더 37 / `Rollback` 37 일치 |
| Parent coverage 대조 | **PASS** | `B-00`~`B-15` 전부 매핑 |
| Phase 4 순서 topological 검증 | **PASS** | `Preconditions` 기준 위반 0 |
| Trailing whitespace 검사 | **PASS** | Phase 1·2·3 합계 0 |
| 누락 식별자 grep 검증 | **PASS** | P0 항목 13개 식별자 0회 출현 확인 |
| 코드 구현 / 단위 테스트 / E2E | **NOT RUN** | Phase 4 |
| 권한 프로파일 실동작 | **NOT RUN** | `M-B00-01` |

### 7.2 검토의 한계

- 본 검수는 **문서 정합성**만 판정했다. Orca CLI 실제 응답 shape, 에이전트 권한 플래그 동작,
  Windows process tree 종료는 검증 대상이 아니며 각각 `P3-R2`·`P3-R1`·`P3-R4`로 남아 있다.
- `P3M-019`의 edge 대조는 수기 전수 확인이며, `test_plan_traceability.py` 구현 시 자동화된다.

### 7.3 Risks

| ID | 리스크 |
|---|---|
| `CR-1` | §2를 그대로 승인하면 Phase 4 구현자가 누락 필드를 임의 보충하게 되어 문서-코드 계약이 처음부터 분기한다 |
| `CR-2` | `P3M-007`(round 이중 소스)은 구현 후 증상이 "가끔 round가 2씩 오른다" 형태로만 드러나 발견이 늦다 |
| `CR-3` | `P3M-002`(`impact_class` 부재)는 `E-01`~`E-04`가 **조용히 발화하지 않는** 형태로 나타난다. 테스트를 별도로 작성하지 않으면 통과한 것처럼 보인다 |

---

## 8. Approval

- [ ] Phase 3 Micro Blocking Revision 2 승인
- [x] **Revision 요청** — §2 재작성 및 Micro Block 4건 추가 후 재검토
- [ ] Phase 4 착수 권한

Phase 1 Revision 6와 Phase 2 Revision 6도 `P3M-018`·`P3M-021`의 정정이 필요하므로,
세 문서를 함께 갱신한 뒤 일괄 승인하는 것을 권고한다.
