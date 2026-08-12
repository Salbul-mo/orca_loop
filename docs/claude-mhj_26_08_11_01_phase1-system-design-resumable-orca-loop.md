# Task Report: Resumable / Deterministic Orca Loop

**Current Phase:** 1. System Design
**Status:** Waiting for Approval
**Date:** 2026-08-11
**Harness:** `C:\Users\mhj\Desktop\mhj_workspace\orca_harness`

---

## 1. Context and Objective

### 1.1 Goal

사용자가 요청한 네 가지 개선을 harness와 skill에 반영한다.

| # | 요구사항 |
| --- | --- |
| R1 | 중간에 중단되어도 언제든 재시작할 수 있는 유연한 구조 |
| R2 | 단계별 결과물이 파일로 산출되어 참조 가능 |
| R3 | 호출 방식이 매번 달라지지 않는, 항상 동일하게 구동되는 실행 경로 |
| R4 | model / effort 오입력을 각 agent의 실제 사용 가능 목록에서 가장 유력한 값으로 흡수 |

### 1.2 Scope

- **In scope:** `orca_loop/` 모듈, `run_loop.py`, `worker_runner.py`, `prompts/`(필요 시), `~/.claude/skills/orca-loop/SKILL.md`
- **Out of scope (변경하지 않음):**
  - permission feasibility gate 및 `V-PERM-*` 검증 규칙 (보안 계약)
  - `machine.py` 상태 전이표, `ledger.py` consensus 규칙, `guards.py` scope 검증
  - `contracts.py`의 artifact 스키마(PlanDocument / ReviewArtifact / ImplementationArtifact)
  - Orca CLI 자체, orchestration gate 프로토콜

---

## 2. 현재 구조 요약

```
run_loop.py                     coordinator entrypoint (단일 프로세스, 단일 실행)
  orca_loop/config.py           argparse + preflight + agent runtime 해석
  orca_loop/coordinator.py      GenerationController, step 실행, 전이 커밋
  orca_loop/dispatcher.py       worker terminal 생성 / task-create / dispatch / wait
  orca_loop/generation.py       control/state.N.json + ledger.N.json + commit.json (원자적)
  orca_loop/profiles.py         role → 실제 agent command line (model/effort 주입 지점)
  orca_loop/transport.py        step out/ → artifacts/<kind>.json 승격
  worker_runner.py              worker terminal 안에서 agent를 실제로 실행하는 wrapper
runs/<run-id>/
  control/   state.N.json, ledger.N.json, commit.json, agent-runtime.json
  artifacts/ plan.json, plan_review.json, implementation.json, code_review.json, cross_review.json
  steps/<step-id>/ in/(contract.md, request.md, ...)  out/(<artifact>.json)
  review/    frozen.diff, scope-manifest.json, repository-N/ (read-only mirror)
  logs/      (생성만 되고 사용되지 않음)
```

generation 단위 durable commit 자체는 이미 견고하다. 문제는 **재진입 경로**와 **증거 보존**, 그리고 **호출 표면**에 있다.

---

## 3. 근본 원인 분석

### 3.1 R1 — 재시작이 안 되는 이유 (5개 독립 원인)

| ID | 원인 | 근거 |
| --- | --- | --- |
| C1-1 | resume이 **동일한 coordinator terminal handle**을 요구. Orca 재시작/터미널 종료 시 그 handle은 존재하지 않음 | `run_loop.py:240-243`, preflight의 `terminal show` `config.py:982-990` |
| C1-2 | resume이 **worktree snapshot digest 일치**를 요구. IMPLEMENT 도중 중단되면 파일이 일부 기록되어 있어 항상 불일치 | `run_loop.py:244-248` |
| C1-3 | resume이 **crash 이전 worker terminal handle 4개를 그대로 재사용**. 죽은 터미널이면 dispatch 실패 | `run_loop.py:263-265` |
| C1-4 | `reconcile_resume()`이 구현되어 있으나 **production 경로에서 호출되지 않음**. in-flight step(STEP_DISPATCHED 등)이 조정 없이 재실행됨 | `coordinator.py:742`, 참조처는 `tests/test_coordinator.py`뿐 |
| C1-5 | resume에 request / permission report / test policy / timeout / model·effort 8값을 **다시 정확히 입력**해야 하고, 하나라도 다르면 `agent runtime configuration drift on resume`로 차단. 그런데 이 값들은 `agent-runtime.json`(model/effort)을 빼면 **어디에도 저장되지 않음** | `config.py:614-627`, `run_loop.py:227-266` |

실제 관측: 마지막 run `claude-mhj_26_08_04_1_slides32-34-camera-mgmt`는 generation 7 `state=PLAN / step_stage=STEP_DISPATCHED`에서 멈춤. 이 상태는 위 5개 중 최소 3개(C1-1, C1-3, C1-4)에 동시에 걸린다.

### 3.2 R2 — 단계별 결과물이 유실되는 이유

| ID | 원인 | 근거 |
| --- | --- | --- |
| C2-1 | worker의 **stdout/stderr가 어디에도 저장되지 않음**. `agent exited 1`이면 stderr 마지막 4096자만 escalation 메시지로 흘러가고 소멸 | `worker_runner.py:281-284`, `main()` 예외 경로 `321-352` |
| C2-2 | 승격 artifact가 **같은 파일명으로 덮어씌워짐**. plan revision 5회를 돌아도 `plan.json` 하나만 남고 v1~v4는 소멸 | `transport.py:267` |
| C2-3 | `runs/<id>/logs/`가 생성만 되고 **한 번도 기록되지 않음** | `workspace.py:124`, 코드베이스 전체에 write 없음 |
| C2-4 | **사람이 읽을 수 있는 산출물이 없음**. plan/review 결과는 내부 계약 JSON뿐이고, 실패 시 "어디까지 됐는지" 요약이 없음 | 설계상 부재 |
| C2-5 | `runs/`가 `.gitignore` 대상이며, review mirror가 read-only로 남아 정리도 어려움(현재 `runs/` 6.6MB, `Permission denied` 다수) | `.gitignore`, `readonly.py` |

관측: 실패한 두 run의 `steps/*/out/`은 **완전히 비어 있음**. 즉 agent가 무엇을 출력했는지 사후 확인이 불가능하다.

### 3.3 R3 — 호출이 매번 달라지는 이유

현재 SKILL.md는 실행 전에 다음을 **모델이 직접 조립**하게 한다.

1. `runs\*\control\permission-feasibility.json` 중 조건을 만족하는 최신본 탐색
2. `orca terminal create`로 runner terminal 생성 후 handle 추출
3. `--agent-model` / `--agent-effort` 8개를 포함한 13개 이상 플래그 조립
4. `--dry-run` 1회 실행
5. **동일 문자열을 PowerShell quoting을 유지한 채** `orca terminal send`로 재전송

한 번의 실행에 자유도가 높은 조립 단계가 5곳 있고, 이 중 어느 하나만 달라져도 결과가 달라진다. `orca_loop_execution_rules.md` §13이 정확히 이 실패(모델 별칭·provider override 조합 오류)를 기록하고 있다. 즉 **문서로 규율하는 대신 코드로 고정해야 하는 문제**다.

### 3.4 R4 — 잘못된 model/effort가 늦게 터지는 이유

- `--agent-model` / `--agent-effort` 값은 **공백·제어문자만 거부**하고 나머지는 무검증 통과 (`config.py:134-170`, `contracts.py:270-283`).
- 그 문자열이 그대로 실제 CLI 인자가 된다 (`profiles.py:107-151`).
- 결과적으로 preflight·permission·worker provisioning을 **모두 통과한 뒤** agent 프로세스가 exit 1로 죽고, C2-1 때문에 원인 증거도 남지 않는다.

이 환경에서 실제 카탈로그는 조회 가능하다.

| provider | 출처 | 값 |
| --- | --- | --- |
| codex | `~/.codex/models_cache.json` (검증함) | `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini` 등 + 모델별 `supported_reasoning_levels` |
| claude | `claude --help` (검증함) | effort = `low\|medium\|high\|xhigh\|max`, model = alias(`fable`,`opus`,`sonnet`,`haiku`) 또는 full name(`claude-opus-5` 등) |

---

## 4. 목표 아키텍처

### 4.1 신규/변경 모듈

| 모듈 | 종류 | 책임 |
| --- | --- | --- |
| `orca_loop/catalog.py` | 신규 | provider별 model/effort 카탈로그 로딩 + 정규화 해석 (R4) |
| `orca_loop/runspec.py` | 신규 | `control/run-manifest.json` 작성·검증·로드 (R1, R3) |
| `orca_loop/session.py` | 신규 | coordinator/worker terminal 생존 확인 및 재바인딩 (R1) |
| `orca_loop/reporting.py` | 신규 | 단계별 Markdown 리포트, artifact 이력, run 요약 (R2) |
| `orca_loop/config.py` | 변경 | subcommand 파서, 카탈로그 연동, manifest 기반 resume 입력 복원 |
| `orca_loop/coordinator.py` | 변경 | `reconcile_resume()`를 실제 resume 경로에 연결 |
| `orca_loop/transport.py` | 변경 | artifact 승격 시 이력본 동시 기록 |
| `run_loop.py` | 변경 | `start` / `resume` / `status` / `doctor` subcommand |
| `worker_runner.py` | 변경 | 성공·실패 무관하게 stdout/stderr/command line 항상 파일로 보존 |
| `SKILL.md` | 재작성 | 조립 단계 제거, 고정 2-command 실행 |

### 4.2 run 디렉터리 레이아웃 (추가분만 표시)

```
runs/<run-id>/
  control/
    run-manifest.json          # NEW: 재시작에 필요한 전체 입력의 단일 출처
    agent-runtime.json         # 유지 (정규화 결과 + 원래 요청값 기록)
    resume-events.json         # NEW: 재시작/재바인딩/드리프트 이력 append-only
  artifacts/
    plan.json ...              # 유지 (현재본)
    history/
      plan.g0007.json          # NEW: 세대별 불변 사본
  reports/                     # NEW: 사람이 읽는 산출물
    00-run-summary.md          # 매 전이마다 갱신
    01-plan.md
    02-plan-review.md
    03-implementation.md
    04-code-review.md
    05-cross-review.md
    99-failure.md              # 실패/중단 시 원인 + 재시작 명령
  logs/                        # NEW: 실제 사용
    step-<step-id>.stdout.log
    step-<step-id>.stderr.log
    step-<step-id>.runner.json # 실제 실행된 command line, exit code, 소요시간
```

`reports/`와 `logs/`는 `runs/`가 `.gitignore` 대상이므로, `--export-dir`가 주어지면 종료 시 지정 경로로 복사한다(기본 미동작).

---

## 5. 핵심 설계 결정

### D-1. 재시작 입력의 단일 출처: `run-manifest.json`

`start` 시 다음을 확정 기록한다.

```jsonc
{
  "schema_version": 1,
  "run_id": "...",
  "created_at": "2026-08-11T...Z",
  "harness_root": "...",
  "worktree_path": "...",
  "request": { "path": "...", "digest": "sha256:...", "copy": "control/request.md" },
  "permission_report": { "path": "...", "digest": "sha256:..." },
  "test_policy": { "path": null, "digest": "sha256:..." },
  "limits": { "step_timeout_ms": 3600000, "total_timeout_ms": 14400000, ... },
  "agent_runtime": {
    "claude_planner": {
      "provider": "claude",
      "requested": { "model": "sonnet5", "effort": "mid" },
      "resolved":  { "model": "sonnet",  "effort": "medium" },
      "resolution_method": { "model": "alias", "effort": "fuzzy" }
    }, ...
  },
  "orca_version": "1.4.164",
  "terminals": { "coordinator": "term_...", "workers": { "claude_planner": "term_...", ... } }
}
```

→ `resume`은 **`--run-id`만** 필요하다. 나머지는 manifest에서 복원하고 digest로 재검증한다.
→ request 원본은 `control/request.md`로 **복사 보존**하므로, 원본 파일이 사라져도 재시작 가능하다.

### D-2. Terminal 재바인딩 (C1-1, C1-3 해소)

resume 시 `session.ensure_*`가 `terminal show`로 생존을 확인하고, 죽은 것만 `terminal create`로 재생성한다. worker_key ↔ terminal 매핑은 유지하고, 재바인딩 사실을 `resume-events.json`과 state history에 남긴다. **재바인딩은 실패가 아니라 정상 복구 경로**로 취급한다.

### D-3. Worktree drift 처리 (C1-2 해소, 안전성 유지)

| 중단 시점의 state | drift 발견 시 기본 동작 |
| --- | --- |
| PLAN / PLAN_REVIEW / CODE_REVIEW / CROSS_CONFIRM / *_EVALUATE / TEST_GATE (읽기 전용 단계) | drift 내용을 `resume-events.json`에 기록하고 **자동 re-baseline 후 진행** |
| IMPLEMENT / FIX (쓰기 단계) | drift 리포트를 `reports/99-failure.md`에 기록하고 **exit 3(사용자 결정 필요)로 정지**. `--accept-worktree-drift` 명시 시에만 re-baseline 후 진행 |

즉 "무조건 차단"에서 "읽기 단계는 자동, 쓰기 단계는 명시 승인"으로 바꾼다. 승인 없는 파일 삭제/복원은 여전히 하지 않는다.

### D-4. `reconcile_resume()` 실연결 (C1-4 해소)

`step_stage`별 조치를 확정한다.

| step_stage | 조치 |
| --- | --- |
| STEP_PENDING / STEP_PREPARED | 해당 step 폐기, 신규 step id로 재실행 |
| TASK_CREATED | dispatch 존재 시 대기, 없으면 재dispatch |
| STEP_DISPATCHED | task/dispatch 생존 확인 → 생존이면 대기, 소멸이면 step 폐기 후 재실행(현재는 사용자 결정 필요로 막힘 → 완화) |
| WORKER_DONE_RECEIVED | `out/` artifact 존재 시 승격 재시도, 없으면 step 폐기 후 재실행 |
| ARTIFACT_VERIFIED | 전이만 재적용 |
| TRANSITION_COMMITTED | 다음 step부터 정상 진행 |

폐기된 step 디렉터리는 삭제하지 않고 `steps/<id>/ABANDONED` 마커만 남긴다(증거 보존).

### D-5. Worker 증거 항상 보존 (C2-1 해소)

`worker_runner.run_job()`을 재구성한다.

1. agent 프로세스 실행 **직전** 에 `logs/step-<id>.runner.json`에 실제 command line(model/effort 포함)을 기록
2. 프로세스 종료 후 exit code와 무관하게 stdout/stderr를 `logs/step-<id>.stdout.log` / `.stderr.log`로 기록
3. 그 다음에야 exit code 판정 / artifact 추출을 수행
4. 실패 시 escalation payload에 로그 경로를 포함

→ `agent exited 1`이 나도 **무엇을 실행했고 무엇을 출력했는지 100% 남는다.** 이것이 R2에서 체감 효과가 가장 큰 변경이다.

### D-6. 단계별 리포트 생성 (C2-2 ~ C2-4 해소)

- `transport.promote_artifact()`가 `artifacts/history/<kind>.g<generation>.json`을 함께 기록(불변).
- `reporting.render_stage_report()`가 승격 직후 해당 단계 Markdown을 생성.
- `reporting.render_run_summary()`가 **모든 전이 커밋마다** `reports/00-run-summary.md`를 갱신. 내용: 현재 state/generation, 단계별 상태표, unresolved finding 수, 마지막 오류, **그대로 복사해 쓸 수 있는 resume 명령줄**.

### D-7. 고정 실행 표면 (R3 해소)

`run_loop.py`에 subcommand를 도입하되, **subcommand 없이 기존 플래그만 준 경우 `start`로 해석**하여 기존 98개 테스트와 현행 호출을 보존한다.

```text
py -3 run_loop.py start   --run-id <id> --request <path> --worktree <path>
                          --agent sonnet/medium,sonnet/medium,gpt-5.6-terra/high,gpt-5.6-terra/high
                          [--dry-run] [--create-terminals] [--test-policy <path>]
py -3 run_loop.py resume  --run-id <id> [--accept-worktree-drift]
py -3 run_loop.py status  --run-id <id>
py -3 run_loop.py doctor
```

`start`가 스스로 수행하는 것(= 모델이 조립하지 않는 것):

- permission feasibility report 자동 탐색 및 검증 (조건 불충족 시 BLOCKED)
- coordinator terminal 생성 및 handle 확정 (`--create-terminals`, 기본 on)
- model/effort 정규화 및 요약 출력
- manifest 기록 → 그대로 실행

`--agent` 축약형은 worker 순서 고정(planner, code_review, implementer, review)이며, 기존 `--agent-model` / `--agent-effort` 형식도 계속 허용한다.

### D-8. Model/Effort 정규화 (R4 해소)

`catalog.resolve(provider, requested_model, requested_effort)`의 결정 순서:

1. **exact** — 카탈로그 slug와 완전 일치
2. **alias** — 내장 별칭 표 (`sonnet5→sonnet`, `terra→gpt-5.6-terra`, `mid→medium`, `xhi→xhigh` 등)
3. **normalized** — casefold + 공백/구분자 제거 후 비교 (`GPT 5.6 Terra→gpt-5.6-terra`)
4. **fuzzy** — `difflib.get_close_matches(cutoff=0.6)` 최상위 후보
5. **default** — 미매칭 시 provider 기본값 채택 + **경고 출력**

카탈로그 출처 우선순위: `agent-catalog.json`(harness 루트, 사용자 편집 가능) → `~/.codex/models_cache.json`(codex) → 내장 정적 목록. 네트워크 접근 없음, 순수 함수, 단위 테스트 가능.

effort는 **해석된 model이 지원하는 목록** 안에서만 해석한다(codex는 모델별로 목록이 다름). 미지원 effort는 가장 가까운 지원 값으로 낮춘다.

정규화 결과는 dry-run 출력과 manifest에 모두 남기며, provider는 **절대 자동 변경하지 않는다** (permission 계약 우회 방지).

---

## 6. 변경하지 않는 안전 계약

- permission feasibility 검증, `V-PERM-*` 요구, provider별 access mode 확인 → 그대로 유지
- 정규화는 model/effort 문자열만 다루고 provider를 바꾸지 않으므로 `§13` 유형의 BLOCKED은 **여전히 정상적으로 차단**된다
- `guards.py` scope 검증, destructive gate, human gate 프로토콜 → 그대로 유지
- 사용자 승인 없는 파일 삭제/복원/reset → 없음

---

## 7. Validation and Risks

### 7.1 Validation Performed (현 시점)

| 항목 | 상태 | 비고 |
| --- | --- | --- |
| 기존 테스트 스위트 baseline | **PASS** | `py -3 -m unittest discover -s tests -t tests` → 98 tests OK (19.6s) |
| codex 모델 카탈로그 존재 확인 | **PASS** | `~/.codex/models_cache.json`, 8개 모델 + 모델별 effort 목록 |
| claude effort/model 옵션 확인 | **PASS** | `claude --help` → effort `low\|medium\|high\|xhigh\|max` |
| 실패 run의 산출물 유실 확인 | **PASS** | 최근 2개 run의 `steps/*/out/` 완전히 비어 있음 |
| `reconcile_resume` 미사용 확인 | **PASS** | production 참조 0건 |

### 7.2 Risks

| ID | 위험 | 완화 |
| --- | --- | --- |
| RK-1 | resume 완화가 잘못된 상태에서 재개를 허용할 가능성 | 쓰기 단계 drift는 기본 차단 유지(D-3), 모든 완화 판단을 `resume-events.json`에 기록 |
| RK-2 | `configuration_digest` 계산 변경으로 기존 run의 resume이 깨질 가능성 | 정규화 **후** 값으로 digest 계산, manifest 없는 legacy run은 기존 경로로 fallback |
| RK-3 | subcommand 도입이 기존 호출/테스트를 깨뜨릴 가능성 | subcommand 미지정 시 `start`로 해석, 기존 플래그 전부 유지 |
| RK-4 | 카탈로그 fallback이 사용자가 의도하지 않은 모델을 조용히 선택 | 기본은 경고 출력 + manifest 기록, `--strict-agent-runtime`로 하드 실패 선택 가능 |
| RK-5 | 리포트/로그 생성이 read-only 검토 미러 규칙과 충돌 | 기록 대상은 `runs/<id>/logs`·`reports`뿐이며 worktree에는 쓰지 않음 |

### 7.3 관측된 부수 문제 (본 작업 범위 외, 별도 승인 필요)

- `runs/` 누적 6.6MB 이상, `review/repository-N/` 미러가 read-only로 남아 삭제/검색 시 `Permission denied` 다발. 정리 커맨드(`run_loop.py prune`)가 필요해 보이나 삭제 동작이므로 별도 승인 대상으로 분리한다.

---

## 8. 미결 결정사항 (사용자 확인 요청)

| ID | 질문 | 제안 |
| --- | --- | --- |
| Q1 | model/effort 미매칭 시 기본 동작 | **자동 대체 + 경고**(요구 R4에 부합), `--strict-agent-runtime` opt-in |
| Q2 | `reports/*.md` 서술 언어 | **한국어 본문 + 영어 식별자** (AGENTS.md 규칙과 일치) |
| Q3 | 쓰기 단계(IMPLEMENT/FIX) drift resume | **기본 차단, 플래그로만 허용** (안전 우선) |
| Q4 | 결과물 외부 export | 기본 off, `--export-dir` 지정 시에만 복사 |

---

## 9. Approval Status

- [ ] Phase 1 System Design 승인
- [ ] 수정 요청
- [ ] Phase 2 (Macro Blocking) 진행 허가
