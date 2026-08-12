# Task Report: Resumable / Deterministic Orca Loop

**Current Phase:** 2. Macro Blocking
**Status:** Waiting for Approval
**Date:** 2026-08-11
**Depends on:** `claude-mhj_26_08_11_01_phase1-system-design-resumable-orca-loop.md` (승인 완료)

---

## 1. Context and Objective

### 1.1 승인된 설계 결정 (Phase 1)

| ID | 결정 |
| --- | --- |
| A-1 | model/effort 미매칭 시 **자동 대체 + 경고**, `--strict-agent-runtime`으로 하드 실패 선택 |
| A-2 | IMPLEMENT/FIX 단계 worktree drift는 **기본 차단(exit 3)**, `--accept-worktree-drift` 명시 시에만 재기준화 |
| A-3 | 읽기 전용 단계(PLAN/REVIEW/EVALUATE/TEST_GATE)의 drift는 기록 후 자동 재기준화 |
| A-4 | provider는 정규화로 절대 변경하지 않음 (permission 계약 보존) |
| A-5 | subcommand 미지정 시 `start`로 해석하여 기존 호출/테스트 보존 |

### 1.2 Phase 2 목표

Phase 1 설계를 **독립 검증 가능한 9개 작업 블록**으로 분해하고, 블록 간 의존성·인터페이스 변경·검증 기준을 확정한다.

---

## 2. Deliverables — Macro Block 정의

### 의존성 그래프

```
MB-1 (catalog)  ──┐
MB-2 (evidence) ──┼─→ MB-3 (manifest) ─→ MB-4 (session) ─→ MB-5 (resume)
                  │            │                                  │
                  └────────────┴──────→ MB-6 (reports) ────────────┤
                                                                   ▼
                                                            MB-7 (CLI surface)
                                                                   ▼
                                                            MB-8 (SKILL.md)
                                                                   ▼
                                                            MB-9 (regression/docs)
```

MB-1과 MB-2는 상호 독립이며 가장 먼저 착수한다(리스크 최저, 효과 즉시 체감).

---

### MB-1 — Agent 카탈로그 및 model/effort 정규화

**요구사항:** R4
**목적:** 잘못된 model/effort가 worker 기동 시점이 아니라 **preflight 시점에** 흡수·정규화되도록 한다.

| 항목 | 내용 |
| --- | --- |
| 신규 | `orca_loop/catalog.py` |
| 변경 | `orca_loop/config.py` (`resolve_agent_runtime`), `orca_loop/contracts.py` (runtime snapshot 스키마 v3) |
| 신규(선택) | `agent-catalog.json` (harness 루트, 사용자 편집 가능 override) |
| 테스트 | `tests/test_catalog.py` (신규), `tests/test_cli.py` 보강 |

**산출물 인터페이스**

```python
@dataclass(frozen=True)
class ResolvedAgentValue:
    requested: str | None
    value: str | None
    method: Literal["exact","alias","normalized","fuzzy","default","inherit"]
    candidates: tuple[str, ...]
    warning: str | None

def load_catalog(harness_root: Path, home: Path | None = None) -> AgentCatalog
def resolve_agent_options(catalog, provider, model, effort, *, strict: bool) -> tuple[ResolvedAgentValue, ResolvedAgentValue]
```

**카탈로그 출처 우선순위:** `<harness>/agent-catalog.json` → `~/.codex/models_cache.json`(codex) → 내장 정적 목록. 네트워크 접근 없음.

**검증 기준**
- `sonnet5`→`sonnet`(alias), `GPT 5.6 Terra`→`gpt-5.6-terra`(normalized), `gpt-5.6-terr`→`gpt-5.6-terra`(fuzzy), `없는모델`→provider 기본값+warning
- codex effort는 **해석된 모델이 지원하는 목록** 안에서만 결정 (`gpt-5.5`는 `max` 미지원 → `xhigh`로 하향)
- `--strict-agent-runtime` 시 `default` 판정은 ConfigurationError
- provider 값은 어떤 경로로도 변경되지 않음 (A-4 회귀 테스트)

**리스크:** `agent-runtime.json` 스키마 확장이 기존 run의 resume 파싱을 깨뜨릴 수 있음 → v1/v2 파싱 경로 유지, 신규 필드는 optional.

---

### MB-2 — Worker 실행 증거 상시 보존

**요구사항:** R2
**목적:** exit code와 무관하게 "무엇을 실행했고 무엇을 출력했는지"가 항상 디스크에 남게 한다.

| 항목 | 내용 |
| --- | --- |
| 변경 | `worker_runner.py` (`run_job`, `_load_job`, `main`) |
| 변경 | `orca_loop/dispatcher.py` (`dispatch_and_wait`의 job dict) |
| 테스트 | `tests/test_worker_runner.py` (신규), `tests/test_dispatcher.py` 보강 |

**실행 순서 재정의**

1. job 로드 직후 `logs/step-<step_id>.runner.json` 기록 (실제 argv, cwd, model/effort, 시작 시각)
2. agent 프로세스 실행
3. **exit code 판정 이전에** stdout/stderr를 `logs/step-<step_id>.stdout.log` / `.stderr.log`로 기록
4. exit code / artifact 추출 판정
5. 실패 시 `runner.json`에 exit code·error·로그 경로를 갱신하고, escalation payload에도 로그 경로 포함

**인터페이스 변경(내부):** worker job 스키마에 `log_dir`, `step_id` 추가. `_load_job`의 `set(value) != required` 엄격 검증 때문에 **dispatcher와 runner를 동시에 변경해야 한다.**

**검증 기준**
- agent exit 1 → `stdout.log`/`stderr.log`/`runner.json` 3개 모두 존재, `runner.json.exit_code == 1`
- timeout → 부분 stdout 보존
- JSON 추출 실패 → 원문 stdout 보존
- 성공 경로의 기존 동작(artifact 기록 + worker_done 송신) 불변

---

### MB-3 — Run manifest (재시작 입력 단일 출처)

**요구사항:** R1, R3
**목적:** `resume --run-id` 하나로 모든 입력을 복원 가능하게 한다.

| 항목 | 내용 |
| --- | --- |
| 신규 | `orca_loop/runspec.py` |
| 변경 | `run_loop.py` (`_initialize`, `_resume`), `orca_loop/config.py` (preflight 입력 복원) |
| 테스트 | `tests/test_runspec.py` (신규) |

**산출물:** `runs/<id>/control/run-manifest.json` (schema_version 1) + `runs/<id>/control/request.md` (원본 복사본)

기록 항목: run_id, created_at, harness_root, worktree_path, request(path/digest/copy), permission_report(path/digest), test_policy(path/digest), limits(timeout·round·retry 전체), agent_runtime(worker별 provider/requested/resolved/method), orca_version, terminals(coordinator + worker 4).

**검증 기준**
- `start` 후 manifest만으로 동일 `RunArguments` 재구성 가능
- request 원본 파일을 삭제해도 `control/request.md`로 resume 성공
- digest 불일치 시 명확한 BLOCKED 메시지 (조용한 진행 금지)
- manifest 없는 legacy run은 기존 플래그 경로로 fallback (회귀 방지)

---

### MB-4 — Terminal 세션 재바인딩

**요구사항:** R1
**목적:** 죽은 terminal이 재시작을 영구 차단하지 않게 한다.

| 항목 | 내용 |
| --- | --- |
| 신규 | `orca_loop/session.py` |
| 변경 | `run_loop.py` (`_resume`), `orca_loop/config.py` (preflight의 `terminal show` 강제 완화) |
| 테스트 | `tests/test_session.py` (신규), `tests/test_orca_client.py` 보강 |

**동작**
- `ensure_coordinator_terminal(client, worktree, run_id, recorded_handle)` → 생존 확인, 사망 시 재생성
- `ensure_worker_pool(client, worktree, recorded_workers, coordinator_handle)` → **죽은 것만** 재생성, worker_key ↔ handle 매핑 유지
- 재바인딩 사실을 `control/resume-events.json`(append-only)과 state history에 기록

**검증 기준**
- coordinator 사망 → 재생성 후 resume 성공, manifest의 terminals 갱신
- worker 4개 중 2개 사망 → 2개만 재생성, 나머지 handle 불변
- 재생성된 handle이 coordinator handle과 같으면 기존 `WorkerProvisionError` 유지

**리스크:** 재바인딩이 진행 중인 dispatch를 고아로 만들 수 있음 → MB-5의 reconcile이 dispatch 생존을 먼저 판정한 뒤에만 재바인딩 수행.

---

### MB-5 — Resume 조정 및 drift 정책

**요구사항:** R1
**목적:** 중단 지점에 따라 결정적으로 재개하고, 위험한 재개만 차단한다.

| 항목 | 내용 |
| --- | --- |
| 변경 | `run_loop.py` (`_resume`, `_run_loop` 진입), `orca_loop/coordinator.py` (`reconcile_resume` 연결) |
| 테스트 | `tests/test_resume.py` (신규), `tests/test_coordinator.py` 보강 |

**step_stage별 조치 (Phase 1 D-4 확정표 적용)**

| step_stage | 조치 |
| --- | --- |
| STEP_PENDING / STEP_PREPARED | step 폐기 → 신규 step id로 재실행 |
| TASK_CREATED | dispatch 존재 시 대기, 없으면 재dispatch |
| STEP_DISPATCHED | task/dispatch 생존 확인 → 생존이면 대기, 소멸이면 폐기 후 재실행 |
| WORKER_DONE_RECEIVED | `out/` artifact 존재 시 승격 재시도, 없으면 폐기 후 재실행 |
| ARTIFACT_VERIFIED | 전이만 재적용 |
| TRANSITION_COMMITTED | 다음 step부터 정상 진행 |

폐기 step은 **삭제하지 않고** `steps/<id>/ABANDONED` 마커 파일만 추가한다.

**drift 정책 (A-2, A-3)**
- 읽기 전용 단계: `resume-events.json` 기록 후 자동 재기준화
- IMPLEMENT/FIX: `reports/99-failure.md`에 drift 상세 기록 후 **exit 3**, `--accept-worktree-drift` 시에만 재기준화
- 어떤 경로에서도 사용자 파일 삭제/복원/reset 없음

**검증 기준**
- 6개 step_stage × (터미널 생존/사망) 조합의 결정 테이블 테스트
- IMPLEMENT drift + 플래그 없음 → exit 3 및 failure 리포트 생성
- IMPLEMENT drift + `--accept-worktree-drift` → 재기준화 후 진행, 이벤트 기록
- FAILED / REJECTED 상태 run은 resume 거부 (기존 운영 규칙 §11 유지)

---

### MB-6 — 단계별 리포트 및 artifact 이력

**요구사항:** R2
**목적:** 각 단계 결과를 사람이 읽을 수 있는 파일로 남기고, 개정 이력을 보존한다.

| 항목 | 내용 |
| --- | --- |
| 신규 | `orca_loop/reporting.py` |
| 변경 | `orca_loop/transport.py` (`promote_artifact`), `orca_loop/coordinator.py` (`GenerationController.commit`), `run_loop.py` (실패 경로) |
| 테스트 | `tests/test_reporting.py` (신규), `tests/test_contracts.py`/`test_dispatcher.py` 회귀 |

**산출물**
- `artifacts/history/<kind>.g<generation>.json` — 불변 세대 사본
- `reports/01-plan.md` ~ `05-cross-review.md` — 승격 직후 생성 (본문 한국어, 식별자 영문)
- `reports/00-run-summary.md` — **매 전이 커밋마다 갱신**. 현재 state/generation, 단계별 상태표, unresolved finding 수, 마지막 오류, **복사해서 바로 쓸 수 있는 resume 명령줄** 포함
- `reports/99-failure.md` — 실패/중단 시 원인·증거 경로·다음 조치

**검증 기준**
- plan 5회 개정 후 `artifacts/history/`에 5개 파일 존재, 현재본 `plan.json` 불변 유지
- 전이마다 요약이 갱신되고, 요약의 resume 명령줄을 그대로 실행하면 재시작 성공
- 리포트 생성 실패가 **run 자체를 실패시키지 않음** (best-effort, 오류는 로그로만)

**리스크:** `commit()`은 원자적 트랜잭션 경로 → 리포트 기록은 commit **성공 이후** best-effort로 수행하고 예외를 삼킨다.

---

### MB-7 — 고정된 실행 표면 (start / resume / status / doctor)

**요구사항:** R3
**목적:** 모델이 조립하던 5개 자유도를 harness 코드로 흡수한다.

| 항목 | 내용 |
| --- | --- |
| 변경 | `run_loop.py` (`main`), `orca_loop/config.py` (파서 재구성) |
| 신규 | permission report 자동 탐색 로직 (`config.py` 또는 `runspec.py`) |
| 테스트 | `tests/test_cli.py` 대폭 보강 |

**커맨드**

```text
py -3 run_loop.py start  --run-id <id> --request <path> --worktree <path>
                         --agent <planner> <code_review> <implementer> <review>   # "model/effort" 4개
                         [--dry-run] [--test-policy <path>] [--no-create-terminals]
                         [--strict-agent-runtime] [--export-dir <path>]
py -3 run_loop.py resume --run-id <id> [--accept-worktree-drift]
py -3 run_loop.py status --run-id <id>
py -3 run_loop.py doctor
```

`start`가 자동 수행: permission report 탐색·검증 / coordinator terminal 생성 / model·effort 정규화 및 요약 출력 / manifest 기록.

**하위 호환 (A-5):** subcommand 없이 기존 `--run-id ... --coordinator-handle ... --agent-model ...` 형태로 호출하면 `start`로 해석하고 기존 동작을 그대로 수행한다. 기존 98개 테스트는 수정 없이 통과해야 한다.

**검증 기준**
- 기존 플래그 전체 조합 회귀 통과
- `start --dry-run` 출력에 정규화 결과(requested → resolved, method)가 포함
- permission report 부재/불일치 시 기존과 동일한 BLOCKED 메시지 및 exit 2
- `status`는 **읽기 전용** (파일 생성/변경 없음)
- `doctor`는 orca status·CLI 존재·카탈로그 로드·permission report 후보를 진단만 수행

---

### MB-8 — SKILL.md 재작성

**요구사항:** R3
**목적:** 스킬을 "조립 지시서"에서 "고정 절차서"로 바꾼다.

| 항목 | 내용 |
| --- | --- |
| 변경 | `~/.claude/skills/orca-loop/SKILL.md` |

**변경 골자**
- Gate 1(작업 프롬프트) 유지 — verbatim 보존 규칙 그대로
- Gate 2(model/effort)는 **관용 입력 허용**으로 변경: 카탈로그를 제시하고 자유 입력을 받되, `--dry-run`이 출력한 정규화 결과를 사용자에게 보여주고 확인받은 뒤 launch
- 실행 절차를 **`start --dry-run` → `start`** 2개 명령으로 고정. permission report 탐색·terminal 생성·플래그 조립 지시 전면 삭제
- "중단 시" 절 신설: `status --run-id` → `resume --run-id` (동일 명령, 매번 동일)
- 단계별 산출물 위치(`reports/`, `logs/`, `artifacts/history/`) 안내 추가

**검증 기준:** 문서에 남은 자유 조립 단계 0개. 실행 명령이 run마다 run-id/경로만 달라짐.

---

### MB-9 — 회귀 검증 및 운영 문서 갱신

| 항목 | 내용 |
| --- | --- |
| 변경 | `orca_loop_execution_rules.md` (§3, §9.1, §11, §12 갱신) |
| 신규 | `docs/claude-mhj_26_08_11_04_phase4-implementation-report.md` |

**검증 기준**
- 전체 테스트 스위트 통과 (baseline 98 + 신규)
- `start --dry-run` 실제 실행 PASS
- 인위적 중단(worker 단계에서 프로세스 종료) 후 `resume --run-id` 실제 재시작 PASS
- 문서의 "model 별칭 금지" 규칙을 "정규화가 흡수함"으로 갱신

---

## 3. 인터페이스 변경 요약

| 대상 | 변경 | 호환성 |
| --- | --- | --- |
| `run_loop.py` CLI | subcommand 추가, `--agent`/`--strict-agent-runtime`/`--accept-worktree-drift`/`--export-dir` 추가 | **추가만**, 기존 플래그 전부 유지 |
| worker job 스키마 (내부) | `log_dir`, `step_id` 추가 | dispatcher/runner 동시 변경, 외부 노출 없음 |
| `agent-runtime.json` | schema v3 (requested/method 기록) | v1/v2 파싱 유지 |
| `run-manifest.json` | 신규 | 없으면 legacy 경로 |
| run 디렉터리 | `reports/`, `logs/`, `artifacts/history/`, `control/resume-events.json` 추가 | 기존 경로 불변 |
| artifact JSON 스키마 | **변경 없음** | — |
| permission / guard / state machine | **변경 없음** | — |

---

## 4. Validation and Risks

### 4.1 블록별 검증 방법

| 블록 | 단위 테스트 | 통합 검증 |
| --- | --- | --- |
| MB-1 | `test_catalog.py` | `start --dry-run` 정규화 출력 |
| MB-2 | `test_worker_runner.py` | 의도적 잘못된 모델로 실행 → 로그 3종 생성 확인 |
| MB-3 | `test_runspec.py` | manifest만으로 resume 인자 복원 |
| MB-4 | `test_session.py` | terminal 삭제 후 resume |
| MB-5 | `test_resume.py` | step_stage×생존 조합 결정 테이블 |
| MB-6 | `test_reporting.py` | 다회 개정 후 history/reports 확인 |
| MB-7 | `test_cli.py` | 기존 98 테스트 무수정 통과 |
| MB-8 | — | 문서 리뷰 |
| MB-9 | 전체 | 실제 중단→재시작 e2e |

### 4.2 Risks

| ID | 위험 | 완화 |
| --- | --- | --- |
| RK-6 | MB-2의 job 스키마 엄격 검증으로 dispatcher/runner 버전 불일치 시 전 worker 실패 | 두 파일을 **같은 블록에서 동시 변경**, 스키마 불일치 시 명시적 오류 메시지 |
| RK-7 | MB-6 리포트 로직이 `commit()` 트랜잭션을 오염 | commit 성공 후 best-effort 실행, 예외 삼킴 + 로그 |
| RK-8 | MB-7 파서 재구성이 기존 98 테스트를 깨뜨림 | subcommand 미지정 = `start` 폴백을 **먼저** 구현하고 테스트 통과 확인 후 신규 기능 추가 |
| RK-9 | MB-5 완화가 손상된 상태의 재개를 허용 | 쓰기 단계 drift 기본 차단(A-2), 모든 완화 판단을 append-only 이벤트로 기록 |
| RK-10 | 작업량 과대 (9블록) | MB-1·MB-2만으로도 R4와 R2 핵심이 해소되므로, 중단되어도 부분 가치가 남는 순서로 배치 |

### 4.3 Validation Performed (현 시점)

| 항목 | 상태 |
| --- | --- |
| baseline 테스트 | **PASS** (98 tests, 19.6s) |
| Phase 1 설계 승인 | **PASS** (A-1 ~ A-5 확정) |
| MB 분해의 의존성 순환 여부 | **PASS** (DAG 확인, 순환 없음) |
| 코드 구현 | **NOT RUN** (Phase 4 대상) |

---

## 5. Approval Status

- [ ] Phase 2 Macro Blocking 승인
- [ ] 수정 요청
- [ ] Phase 3 (Micro Blocking) 진행 허가
