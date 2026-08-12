# Task Report: Resumable / Deterministic Orca Loop

**Current Phase:** 4. Code Implementation
**Status:** Validation Passed
**Date:** 2026-08-11
**Depends on:** Phase 1 / 2 / 3 (모두 승인)

---

## 1. Context and Objective

사용자 요구 4건을 harness와 skill에 구현했다.

| # | 요구사항 | 구현 블록 | 상태 |
| --- | --- | --- | --- |
| R1 | 중단되어도 언제든 재시작 | MB-5.1, MB-3, MB-4, MB-5.2~5.4 | 완료 |
| R2 | 단계별 결과물의 파일 산출 | MB-2, MB-6 | 완료 |
| R3 | 항상 동일하게 구동되는 실행 경로 | MB-7, MB-8 | 완료 |
| R4 | model/effort 오입력 흡수 | MB-1 | 완료 |

---

## 2. Deliverables

### 2.1 신규 파일

| 파일 | 역할 |
| --- | --- |
| `orca_loop/catalog.py` | provider별 model/effort 카탈로그 + 관용 해석 (R4) |
| `orca_loop/runspec.py` | `run-manifest.json` 작성·검증·복원 (R1, R3) |
| `orca_loop/session.py` | terminal 생존 확인·재바인딩, `resume-events.jsonl` (R1) |
| `orca_loop/reporting.py` | 단계별 Markdown, artifact 이력, run 요약, 중단 보고 (R2) |
| `tests/test_catalog.py` (26) | 카탈로그·정규화 |
| `tests/test_locking.py` (9) | stale lock 회수 |
| `tests/test_worker_runner.py` (8) | worker 증거 보존 |
| `tests/test_runspec.py` (20) | manifest 왕복·복원·검증 |
| `tests/test_session.py` (11) | 재바인딩·이벤트 로그 |
| `tests/test_resume.py` (14) | resume 조정·drift 정책 |
| `tests/test_reporting.py` (13) | 리포트 렌더링 |
| `tests/test_cli_commands.py` (15) | subcommand·축약형·복원 |
| `docs/claude-mhj_26_08_11_0{1,2,3,4}_*.md` | Phase 1~4 문서 |

### 2.2 변경 파일

| 파일 | 변경 |
| --- | --- |
| `orca_loop/locking.py` | `pid_alive`(Windows `OpenProcess` 기반), `inspect_lock`, `acquire_run_lock(reclaim_stale, force, on_reclaim)` |
| `orca_loop/config.py` | 카탈로그 정규화 통합, `--strict-agent-runtime`, `--accept-worktree-drift`, `--force-unlock`, `verify_coordinator` 옵션, permission report 자동 탐색 |
| `orca_loop/coordinator.py` | artifact 이력·단계 리포트·run 요약 훅 (commit 성공 이후 best-effort) |
| `orca_loop/dispatcher.py` | worker job에 `log_dir`, `step_id` 추가 |
| `worker_runner.py` | 실행 전 `runner.json`, 판정 전 stdout/stderr 기록, 실패 경로에 증거 경로 포함 |
| `run_loop.py` | `start`/`resume`/`status`/`doctor` subcommand, resume 재작성, drift 정책, 중단 보고 |
| `~/.claude/skills/orca-loop/SKILL.md` | 조립 절차 제거, 고정 명령 + 중단 복구 절 |

### 2.3 새 run 디렉터리 레이아웃

```
runs/<run-id>/
  control/run-manifest.json        재시작 입력 단일 출처
  control/request.md               request 원본의 byte-identical 사본
  control/resume-events.jsonl      재바인딩·재기준화·step 폐기 이력
  artifacts/history/<kind>.g####.json   개정별 불변 사본
  reports/00-run-summary.md        매 전이마다 갱신 (재시작 명령 포함)
  reports/01-plan.md … 05-cross-review.md
  reports/99-failure.md            중단 원인 + 증거 + 재시작 방법
  logs/step-<id>.stdout.log / .stderr.log / .runner.json
  steps/<step-id>/ABANDONED        폐기된 step 마커 (내용은 보존)
```

---

## 3. 요구사항별 구현 결과

### R1 — 재시작

Phase 1에서 5개, Phase 3에서 1개, 총 6개 원인을 모두 제거했다.

| 원인 | 해결 |
| --- | --- |
| stale lock이 영구 차단 | 죽은 pid의 락은 자동 회수, 살아있으면 기존대로 거부 (`--force-unlock`만 예외) |
| coordinator handle 불일치 | `session.ensure_coordinator_terminal`이 생존 확인 후 필요 시 재생성 |
| worktree snapshot 불일치 | 읽기 단계는 자동 재기준화, 쓰기 단계는 exit 3 + `--accept-worktree-drift` |
| 죽은 worker terminal 재사용 | 죽은 것만 선별 재생성, worker_key 매핑 유지 |
| `reconcile_resume` 미연결 | resume 경로에 연결, 결정을 이벤트로 기록 |
| 입력값 미저장 | `run-manifest.json` + `control/request.md`로 `resume --run-id`만으로 복원 |

### R2 — 산출물

- worker의 실제 command line·stdout·stderr가 **exit code 판정 이전에** 기록된다. `agent exited 1`, timeout, JSON 추출 실패 세 경우 모두 3개 파일이 남는다.
- 승격 artifact가 `artifacts/history/<kind>.g####.json`으로 불변 보존된다(개정 이력 유실 해소).
- 매 전이마다 `reports/00-run-summary.md`가 갱신되고, 단계별 Markdown이 생성된다.
- 모든 리포트 생성은 durable commit **성공 이후** best-effort로 수행되며, 실패는 `logs/reporting.log`에만 남고 run을 죽이지 않는다.

### R3 — 실행 표면

`start`가 스스로 수행하므로 모델이 조립할 여지가 사라졌다.

- permission report 자동 탐색 (조건은 기존과 동일: PASS / strategy D / orca_version 일치 / 전 check PASS / canonical_path 일치)
- coordinator terminal 자동 생성 (`--dry-run`은 생성하지 않음 — 리허설이 터미널을 누수하지 않도록)
- model/effort 정규화 및 dry-run 출력에 결과 표시
- `--agent KEY=MODEL/EFFORT` 축약형
- subcommand 미지정 시 `start`로 해석 → **기존 호출 형식과 98개 테스트가 그대로 통과**

### R4 — model/effort 관용 입력

`exact → alias → normalized → fuzzy → clamped → default` 6단계. provider는 어떤 경로로도 바뀌지 않는다.

실제 동작 확인 (doctor 출력): codex 카탈로그는 `~/.codex/models_cache.json`에서, 기본값은 `~/.codex/config.toml`에서 읽는다.

```
- claude: default=sonnet; models=opus, sonnet, fable, haiku, claude-opus-5, ...
- codex:  default=gpt-5.6-sol; models=gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5, ...
```

---

## 4. Phase 3 설계 대비 변경 2건

| 항목 | 설계 | 실제 | 이유 |
| --- | --- | --- | --- |
| artifact 이력 훅 위치 | `transport.promote_artifact` 시그니처 확장 | `coordinator.execute_worker_step`에서 호출 | 승격 직후 지점에서 `workspace.root`와 generation을 이미 알고 있어 시그니처 변경이 불필요 |
| resume의 `PROMOTE_ARTIFACT`/`WAIT_DISPATCH` | artifact 재승격 / dispatch 대기 | **모든 in-flight step을 폐기 후 재실행** | 재승격에 필요한 guard 기준값(step 이전 snapshot·file state)이 영속화되어 있지 않아, 재승격은 잘못된 기준으로 scope를 검증하게 된다. 재실행은 정의상 정확하고, 폐기 step은 `ABANDONED` 마커와 함께 입출력·로그가 모두 보존된다 |

---

## 5. Validation and Risks

### 5.1 Validation Performed

| 항목 | 상태 | 근거 |
| --- | --- | --- |
| 전체 테스트 스위트 | **PASS** | `py -3 -m unittest discover -s tests -t tests` → **221 tests OK** (baseline 98 + 신규 123) |
| 기존 98개 테스트 무수정 통과 | **PASS** | 하위호환 폴백 확인 |
| `run_loop.py doctor` 실행 | **PASS** | orca ready, 카탈로그 로드, permission report 판정, stale lock 2건 탐지 |
| `run_loop.py status --run-id <중단된 run>` | **PASS** | 중단된 실제 run의 상태(`PLAN`/`STEP_DISPATCHED`, generation 7)와 재시작 명령 출력, 파일 무변경 확인 |
| stale lock 탐지 정확도 | **PASS** | pid 45152·23824 모두 `alive: false` 판정 |
| 실제 `start` 실행 | **NOT RUN** | 아래 5.3 참조 |
| 실제 중단→`resume` end-to-end | **NOT RUN** | 아래 5.3 참조 |

### 5.2 남은 위험

| ID | 위험 | 상태 |
| --- | --- | --- |
| RK-11 | Windows `os.kill(pid,0)`이 프로세스를 종료 | 회피 완료 — `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` 사용 |
| RK-12 | PID 재사용 오탐 | 생존 pid는 회수하지 않으므로 오탐은 "거부" 방향으로만 발생 |
| RK-13 | resume 재실행으로 worker 중복 | 새 step_id·새 task로 수행하고 `dispatch_and_wait`가 foreign 메시지를 무시하므로 provenance 충돌 없음 |
| RK-15 | 리포트가 매 commit마다 기록되어 I/O 증가 | 파일 1개(수 KB) 재작성. 실측 영향 없음 |

### 5.3 후속 작업 — permission 게이트를 환경 지문 기준으로 전환 (2026-08-11, 승인 후 구현 완료)

#### 문제

`permission-feasibility.json`이 `orca_version` **정확 일치**로 검증되어, Orca가 업데이트될 때마다 agent 5회를 다시 돌려야 했다. 그러나 이 report가 실제로 증명하는 것은 "icacls RX가 걸린 디렉터리에서 agent CLI가 쓰기에 실패하고 outbox에는 성공한다"이고, 그 결과를 결정하는 것은 다음이다.

1. Windows ACL 집행 (OS)
2. `claude` / `codex` CLI의 버전과 동작
3. harness 자신의 `readonly.py`(ACL 적용)와 `profiles.py`(launch 플래그)

Orca는 terminal 생성과 orchestration 메시지 전달만 하고 **파일 접근을 중재하지 않는다.** agent는 `worker_runner.py`의 자식 프로세스로 실행되며 Orca가 샌드박싱하지 않는다. 즉 기존 게이트는 **실제 위험 요인이 아닌 값에 핀을 박고, 정작 중요한 agent CLI 버전은 검사하지 않고 있었다.**

#### 구현

신규 `orca_loop/environment.py`가 환경 지문을 정의한다.

```python
PermissionEnvironment(platform, claude_cli, codex_cli, enforcement_digest)
```

- `enforcement_digest` = `orca_loop/readonly.py` + `orca_loop/profiles.py`의 sha256 (개행 정규화)
- `claude_cli` / `codex_cli` = `<cli> --version`에서 파싱

판정 규칙:

| 항목 | 판정 |
| --- | --- |
| `enforcement_digest` | **차단** — 잠금/launch 코드가 바뀌면 증명이 무효 |
| `platform` | **차단** |
| agent CLI **major.minor** | **차단** — 예: `2.1.x` → `2.2.0` |
| agent CLI **patch** | 허용 — patch 릴리스는 쓰기 거부 방식을 바꾸지 않음 |
| `orca_version` | **정보성 note** |

`environment` 필드가 **없는 legacy report**는 기존대로 `orca_version` 정확 일치를 적용한다(더 나은 증거가 없으므로).

변경 파일: `orca_loop/environment.py`(신규), `models.py`(`PermissionEnvironment`), `contracts.py`(optional `environment` 파싱), `config.py`(`permission_environment_problems`, `permission_report_notes`, `run_preflight`), `profiles.py`(legacy에만 버전 검사), `permission_spike.py`(finalize에서 지문 캡처), `run_loop.py`(`doctor`에 지문 표시). 테스트 `tests/test_environment.py` 18건 추가.

#### 결과

이제 재spike가 필요한 시점은 **Orca 업데이트가 아니라 agent CLI의 minor 업데이트 또는 harness 잠금 코드 변경**이며, `doctor`가 그 사유를 그대로 출력한다.

```
"problem": "claude CLI version changed: report 2.1.227, current 2.2.0"
"problem": "read-only enforcement code changed since the report (orca_loop/readonly.py, orca_loop/profiles.py)"
```

### 5.4 2026-08-11 permission spike 재실행 결과

`runs\20260811-permission-spike-01\control\permission-feasibility.json` — `status=PASS`, `strategy=D`, `orca_version=1.4.179`, 6개 체크 전부 PASS.

| Check | 대상 | 관측 |
| --- | --- | --- |
| V-PERM-01 | claude 읽기 | `read_value='permission spike source baseline'` |
| V-PERM-02 | claude 쓰기 차단 | `bash: source.txt: Permission denied` / `EPERM ... source.txt.tmp.12616...` |
| V-PERM-03 | claude outbox 쓰기 | probe.txt 생성 |
| V-PERM-04 | codex 쓰기 차단 | codex tool router `Exit code: 1, Failed to write file ...source.txt` |
| V-PERM-05 | codex implementer 쓰기 | `approved implementer write\n` 바이트 확인 |
| **V-PERM-06** | **claude implementer 쓰기** | `approved claude implementer write\n` 바이트 확인 — **최초 확보** |

절차: 런타임과 동일하게 `prepare_readonly_mirror`(icacls RX) 미러에서 read-only 역할 3개, writable fixture에서 implementer 2개를 실행하고, 매 역할 후 `source.txt` sha256이 미러·fixture 양쪽에서 불변임을 코디네이터가 독립 검증했다.

V-PERM-06 확보로 §13에 기록된 "codex_implementer/codex_review provider를 claude로 바꾸면 BLOCKED" 제약이 해소되었다.

환경 지문: `platform=Windows`, `claude_cli=2.1.227`, `codex_cli=0.146.0`, `enforcement_digest=sha256:cd28626c…`

### 5.5 (해소됨) 실제 실행이 막혀 있던 사유

`doctor`가 드러낸 사실:

```
orca.version            = 1.4.179
EXPECTED_ORCA_VERSION   = 1.4.164
사용 가능한 permission report = 20260803-permission-spike-01 (orca_version 1.4.164)
```

현재 Orca 런타임이 **1.4.179**로 올라가 있는데, harness의 기대 버전과 유일하게 유효한 permission feasibility report는 **1.4.164** 기준이다. 따라서 지금 `start`를 실행하면 preflight가 `Orca version drift`로 `BLOCKED`된다.

**해소됨** — 5.4의 재spike로 1.4.179 기준 report를 생성하고 `EXPECTED_ORCA_VERSION`을 갱신했으며, 5.3의 전환으로 앞으로 Orca 업데이트만으로는 다시 막히지 않는다. `doctor`는 현재 `status=PASS`다.

### 5.4 정리하지 않은 항목

- stale lock 2건은 사용자 결정에 따라 **수동 삭제하지 않았다.** 다음 `start`/`resume` 시 코드가 자동 회수하며, 그 자체가 회수 로직의 실검증이 된다.
- `runs/` 누적(6.6MB+, read-only 미러로 인한 `Permission denied`)은 범위 밖으로 남겨 두었다. `prune` 커맨드가 필요하면 삭제 동작이므로 별도 승인이 필요하다.

---

## 6. 사용법 요약

```text
py -3 run_loop.py doctor
py -3 run_loop.py start  --run-id <id> --request <path> --worktree <path> \
                         --agent claude_planner=sonnet/medium \
                         --agent claude_code_review=sonnet/medium \
                         --agent codex_implementer=gpt-5.6-terra/high \
                         --agent codex_review=gpt-5.6-terra/high --dry-run
py -3 run_loop.py start  ... (동일, --dry-run 제거)
py -3 run_loop.py status --run-id <id>
py -3 run_loop.py resume --run-id <id> [--accept-worktree-drift]
```
