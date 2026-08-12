# Task Report: Resumable / Deterministic Orca Loop

**Current Phase:** 3. Micro Blocking
**Status:** Waiting for Approval
**Date:** 2026-08-11
**Depends on:** Phase 1 (승인), Phase 2 (승인)

---

## 0. Phase 2 이후 추가 발견 및 설계 조정

### 0.1 신규 발견 — Stale run lock이 재시작을 영구 차단 (R1 원인 #6)

`locking.py:32-87`의 `acquire_run_lock()`은 `O_CREAT|O_EXCL`로 락 파일을 만들고, `run_loop.main()`의 `finally`에서만 해제한다. **coordinator 프로세스가 비정상 종료되면 락 파일이 영구히 남는다.** 그리고 `main()`은 `start`와 `resume` 모두에서 락을 획득하므로, **재시작 자체가 불가능해진다.**

현재 harness에 실제로 stale lock 2개가 남아 있음을 확인했다.

| 락 파일 | run_id | pid | 프로세스 상태 |
| --- | --- | --- | --- |
| `2b931545….lock` | `claude-mhj_26_08_04_1_slides32-34-camera-mgmt` | 45152 | **NOT RUNNING** (2026-08-04) |
| `codex-mhj_26_08_03_5_stale-run4.lock` | `codex-mhj_26_08_03_4_slide10-map-view` | 23824 | **NOT RUNNING** (2026-08-03) |

즉 지금 이 시점에 `platform-poc` worktree에 대해 새 run이든 resume이든 시도하면 `worktree already has a coordinator lock`으로 즉시 실패한다. 이 항목을 **MB-5의 최우선 micro task(MB-5.1)** 로 편입한다.

### 0.2 설계 조정 — `contracts.py` 스키마 변경 철회

Phase 2 MB-1은 `agent-runtime.json`을 schema v3로 올려 `requested`/`method`를 기록하려 했다. 그러나 `parse_agent_runtime_snapshot()`(`contracts.py:526-570`)이 `_exact()`로 **정확히 5개 키만** 허용하고, `configuration_digest`가 `_agent_runtime_value()` 결과로 계산되므로, 필드를 추가하면 digest 의미와 resume drift 비교가 함께 흔들린다.

**조정:** `contracts.py`와 `agent-runtime.json`은 **전혀 건드리지 않는다.** 정규화 provenance는 전부 신규 `run-manifest.json`(MB-3)과 dry-run 출력에 기록한다.

효과: `configuration_digest` 의미 불변 → resume drift 비교 로직 불변 → Phase 2 RK-2 소멸. MB-1의 변경 파일이 `catalog.py`(신규) + `config.py` 2개로 축소된다.

---

## 1. MB-1 — Agent 카탈로그 및 정규화

### 1.1 신규 파일 `orca_loop/catalog.py`

```python
CLAUDE_EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORT_LADDER  = ("low", "medium", "high", "xhigh", "max", "ultra")

@dataclass(frozen=True)
class ModelEntry:
    canonical: str                  # CLI에 그대로 전달되는 값
    aliases: tuple[str, ...]
    efforts: tuple[str, ...]        # 이 모델이 지원하는 effort (사다리 순서 유지)
    default_effort: str | None

@dataclass(frozen=True)
class AgentCatalog:
    models: Mapping[AgentProvider, tuple[ModelEntry, ...]]
    default_model: Mapping[AgentProvider, str]
    sources: tuple[str, ...]        # 로드 출처 기록 (진단용)

@dataclass(frozen=True)
class ResolvedValue:
    requested: str | None
    value: str | None
    method: str                     # exact|alias|normalized|fuzzy|clamped|default|inherit
    warning: str | None

class CatalogError(RuntimeError): ...

def load_catalog(harness_root: Path, *, home: Path | None = None) -> AgentCatalog
def resolve_model(catalog, provider, requested, *, strict: bool) -> ResolvedValue
def resolve_effort(catalog, provider, model_canonical, requested, *, strict: bool) -> ResolvedValue
def format_resolution_lines(items) -> tuple[str, ...]      # 요약 출력용
```

### 1.2 카탈로그 로드 (MB-1.1)

우선순위대로 병합하되, **앞선 출처가 이긴다.**

1. `<harness_root>/agent-catalog.json` — 존재 시. 파싱 실패는 `CatalogError`가 아니라 **경고 후 무시**(카탈로그 오류가 run을 막지 않게)
2. `~/.codex/models_cache.json` — codex 전용. `models[].slug`, `models[].supported_reasoning_levels[].effort`, `default_reasoning_level` 사용. `visibility == "hide"` 항목도 **포함**(사용자가 명시하면 쓸 수 있어야 함)하되 fuzzy 후보 순위에서는 뒤로
3. 내장 정적 목록 — 아래

```python
STATIC_CLAUDE = (
    ModelEntry("opus",   ("opus5","opus-5"),        CLAUDE_EFFORT_LADDER, "high"),
    ModelEntry("sonnet", ("sonnet5","sonnet-5"),    CLAUDE_EFFORT_LADDER, "medium"),
    ModelEntry("haiku",  ("haiku45","haiku-4-5"),   CLAUDE_EFFORT_LADDER, "low"),
    ModelEntry("fable",  ("fable5","fable-5"),      CLAUDE_EFFORT_LADDER, "high"),
    ModelEntry("claude-opus-5",   (), CLAUDE_EFFORT_LADDER, "high"),
    ModelEntry("claude-sonnet-5", (), CLAUDE_EFFORT_LADDER, "medium"),
    ModelEntry("claude-fable-5",  (), CLAUDE_EFFORT_LADDER, "high"),
    ModelEntry("claude-haiku-4-5-20251001", (), CLAUDE_EFFORT_LADDER, "low"),
)
STATIC_CODEX = (  # models_cache.json 부재 시 폴백
    ModelEntry("gpt-5.6-terra", ("terra",), CODEX_EFFORT_LADDER, "medium"),
    ModelEntry("gpt-5.6-sol",   ("sol",),   CODEX_EFFORT_LADDER, "low"),
    ModelEntry("gpt-5.6-luna",  ("luna",),  CODEX_EFFORT_LADDER[:5], "medium"),
    ModelEntry("gpt-5.5",  (), CODEX_EFFORT_LADDER[:4], "medium"),
    ModelEntry("gpt-5.4",  (), CODEX_EFFORT_LADDER[:4], "medium"),
)
DEFAULT_MODEL = {CLAUDE: "sonnet", CODEX: "gpt-5.6-terra"}
```

full name(`claude-sonnet-5` 등)을 **별도 canonical entry**로 둔다. 사용자가 정확한 full name을 주면 그대로 통과시키고, alias로 축약 변환하지 않는다(모델 고정 의도 보존).

codex slug에서 alias 자동 파생: 마지막 하이픈 세그먼트(`gpt-5.6-terra`→`terra`). 충돌 시(둘 이상이 같은 alias) 해당 alias를 폐기한다.

### 1.3 정규화 알고리즘 (MB-1.2)

```python
def _norm(value): return re.sub(r"[^a-z0-9]", "", value.casefold())

def resolve_model(catalog, provider, requested, *, strict):
    if requested is None:                       return ResolvedValue(None, None, "inherit", None)
    entries = catalog.models[provider]
    if requested in canonicals:                 return (..., "exact")
    if requested in alias_index:                return (canonical, "alias")
    if _norm(requested) in norm_index:          return (canonical, "normalized")
    match = difflib.get_close_matches(_norm(requested), norm_keys, n=1, cutoff=0.6)
    if match:
        if strict: raise ConfigurationError(...)
        return (canonical, "fuzzy", warning=f"{requested!r} -> {canonical!r} (fuzzy)")
    if strict: raise ConfigurationError(f"unknown model {requested!r}; candidates: {...}")
    return (catalog.default_model[provider], "default",
            warning=f"{requested!r} not in catalog; using {default!r}")
```

effort는 **해석된 모델이 지원하는 목록** 안에서만 결정한다.

```python
EFFORT_ALIASES = {"mid":"medium","med":"medium","normal":"medium","default":"medium",
                  "hi":"high","xhi":"xhigh","x-high":"xhigh","extra-high":"xhigh",
                  "minimal":"low","min":"low","lowest":"low","maximum":"max","highest":"max"}

def resolve_effort(catalog, provider, model_canonical, requested, *, strict):
    supported = entry.efforts
    if requested is None: return ("inherit")
    v = exact / alias / normalized / fuzzy  →  ladder 값 하나로 축약
    if v in supported: return (v, method)
    # 사다리에는 있지만 이 모델이 미지원 → 지원 값 중 v 이하 최대값으로 하향
    lowered = highest supported level <= v   (없으면 supported[0])
    if strict: raise ConfigurationError(...)
    return (lowered, "clamped", warning=f"{model} does not support {v!r}; using {lowered!r}")
```

**불변식:** provider는 어떤 경로로도 바뀌지 않는다. 반환값은 항상 카탈로그 canonical 또는 `None`(inherit).

### 1.4 `config.py` 통합 (MB-1.3)

- `resolve_agent_runtime()`의 `resolved = tuple(AgentRuntimeOptions(...))` 생성 직후, `build_agent_runtime_config()` **직전**에 정규화를 삽입
- `AgentRuntimeResolution`에 `resolutions: tuple[tuple[WorkerKey, ResolvedValue, ResolvedValue], ...]` 필드 추가
- `print_agent_runtime_summary()`를 `requested → resolved (method)` 형식으로 확장. 경고는 `[WARN]` 접두로 stderr
- `--strict-agent-runtime` 플래그 추가 (기본 off = A-1)
- `--configure-agents` 대화 입력에도 동일 정규화 적용

**resume 시 동작:** 기존 코드는 persisted snapshot과 입력값의 `configuration_digest`를 비교한다. 정규화는 **비교 이전에** 수행되므로, `sonnet5`로 시작한 run을 `sonnet`으로 resume해도 drift로 오판하지 않는다(오히려 개선).

### 1.5 테스트 (MB-1.4) — `tests/test_catalog.py`

| ID | 케이스 | 기대 |
| --- | --- | --- |
| T-C1 | `sonnet` / claude | exact, `sonnet` |
| T-C2 | `sonnet5` | alias, `sonnet` |
| T-C3 | `GPT 5.6 Terra` | normalized, `gpt-5.6-terra` |
| T-C4 | `gpt-5.6-terr` | fuzzy, `gpt-5.6-terra` |
| T-C5 | `없는모델` | default + warning |
| T-C6 | `없는모델` + strict | ConfigurationError |
| T-C7 | `gpt-5.5` + effort `max` | clamped → `xhigh` |
| T-C8 | effort `mid` | alias → `medium` |
| T-C9 | `models_cache.json` 부재 | 정적 폴백으로 동작 |
| T-C10 | `agent-catalog.json` 손상 | 경고 후 무시, run 계속 |
| T-C11 | 모든 경로에서 provider 불변 | 회귀 |

---

## 2. MB-2 — Worker 실행 증거 상시 보존

### 2.1 job 스키마 확장 (MB-2.1)

`dispatcher.dispatch_and_wait()`의 job dict에 2개 키 추가:

```python
"log_dir": str((step.root.parents[1] / "logs").resolve()),   # runs/<run-id>/logs
"step_id": step.step_id,
```

`worker_runner._load_job()`의 `required` 집합에 동일하게 추가. **두 파일을 같은 커밋에서 변경**한다(Phase 2 RK-6).

### 2.2 `run_job()` 재구성 (MB-2.2)

```python
def run_job(job):
    log_dir  = Path(job["log_dir"]);  log_dir.mkdir(parents=True, exist_ok=True)
    base     = log_dir / f"step-{job['step_id']}"
    record   = {"schema_version":1, "step_id":..., "task_id":..., "dispatch_id":...,
                "command": list(job["profile_command"]), "agent_cwd":...,
                "started_at": iso_utc_now(), "status":"RUNNING",
                "stdout_log": str(base)+".stdout.log", "stderr_log": str(base)+".stderr.log"}
    _write_atomic(base.with_suffix(".runner.json"), canonical(record))   # ← 실행 전

    process = Popen(...)
    try:
        out, err = process.communicate(prompt, timeout=...)
        timed_out = False
    except TimeoutExpired:
        _terminate_tree(process); out, err = process.communicate(); timed_out = True

    _write_atomic(base+".stdout.log", out or b"")          # ← exit code 판정 전, 항상
    _write_atomic(base+".stderr.log", err or b"")
    record.update(exit_code=process.returncode, timed_out=timed_out,
                  finished_at=..., duration_ms=...)

    try:
        if timed_out:            raise WorkerRunnerError(f"agent timed out after {timeout_ms} ms")
        if returncode != 0:      raise WorkerRunnerError(f"agent exited {rc}: {tail(err)}")
        artifact = extract_agent_artifact(stdout)  # 기존 폴백 로직 유지
        ...
    except WorkerRunnerError as exc:
        record.update(status="FAILED", error=str(exc))
        _write_atomic(base+".runner.json", canonical(record))
        raise
    record.update(status="PASS", artifact_digest=digest, report_path=...)
    _write_atomic(base+".runner.json", canonical(record))
    return {...}
```

`main()`의 escalation payload에 `stdoutLog` / `stderrLog` / `runnerRecord` 경로를 추가한다. 로그 기록 자체가 실패해도(디스크 오류 등) **원래 오류를 덮지 않는다**(로그 기록은 try/except로 감싸고 원 예외 우선).

### 2.3 테스트 (MB-2.3) — `tests/test_worker_runner.py`

| ID | 케이스 | 기대 |
| --- | --- | --- |
| T-W1 | exit 1 | 3개 파일 존재, `status=FAILED`, `exit_code=1`, stderr 원문 보존 |
| T-W2 | timeout | `timed_out=true`, 부분 stdout 보존 |
| T-W3 | stdout에 JSON 없음 | 원문 stdout 보존, `status=FAILED` |
| T-W4 | 정상 | `status=PASS`, artifact/worker_done 기존 동작 불변 |
| T-W5 | job에 신규 키 누락 | 명확한 스키마 오류 |
| T-W6 | `log_dir` 쓰기 실패 | 원래 agent 오류가 그대로 전파 |

---

## 3. MB-3 — Run manifest

### 3.1 신규 파일 `orca_loop/runspec.py`

```python
MANIFEST_NAME = "run-manifest.json"
MANIFEST_SCHEMA_VERSION = 1

@dataclass(frozen=True)
class FileRef:      path: str; digest: str | None
@dataclass(frozen=True)
class AgentRecord:  provider: str; requested_model: str|None; model: str|None
                    requested_effort: str|None; effort: str|None
                    model_method: str; effort_method: str
@dataclass(frozen=True)
class RunManifest:
    schema_version: int; run_id: str; created_at: str; harness_root: str
    worktree_path: str; request: FileRef; request_copy: str
    permission_report: FileRef; test_policy: FileRef | None
    limits: Mapping[str, int]; agents: Mapping[str, AgentRecord]
    orca_version: str; coordinator_handle: str
    workers: Mapping[str, str]                       # worker_key -> terminal handle

def build_manifest(preflight, resolutions, pool=None) -> RunManifest
def write_manifest(control_dir, manifest) -> Path        # write_atomic_bytes 사용
def read_manifest(control_dir) -> RunManifest | None     # 없으면 None (legacy)
def update_terminals(control_dir, coordinator, workers) -> RunManifest
def manifest_to_arguments(manifest, harness_root) -> RunArguments   # resume 입력 복원
def verify_inputs(manifest) -> tuple[str, ...]           # digest 검증, 문제 목록 반환
```

### 3.2 기록·복원 시점 (MB-3.1)

| 시점 | 동작 |
| --- | --- |
| `_initialize()` 워크스페이스 생성 직후 | request 원본을 `control/request.md`로 복사(원자적) |
| worker pool 확정 직후 | `write_manifest()` — terminals 포함 |
| `_resume()` 진입 | `read_manifest()` → 있으면 `manifest_to_arguments()`로 입력 복원, 없으면 legacy 경로 |
| terminal 재바인딩 후 (MB-4) | `update_terminals()` |

### 3.3 digest 검증 규칙 (MB-3.2)

- request: `control/request.md` 사본의 digest가 manifest와 일치해야 함. 원본 경로가 사라졌거나 내용이 달라졌으면 **사본을 사용**하고 이벤트에 기록(원본은 참고용)
- permission report: 경로가 바뀌어도 digest가 같으면 수용하고 경로 갱신. digest가 다르면 **BLOCKED**(보안 계약)
- test policy: 동일 규칙. 정책 자체가 사라졌으면 BLOCKED (조용히 무정책으로 진행 금지)

### 3.4 테스트 (MB-3.3) — `tests/test_runspec.py`

T-M1 왕복(build→write→read) 동일성 / T-M2 request 원본 삭제 후 복원 성공 / T-M3 permission digest 불일치 → BLOCKED / T-M4 manifest 부재 → legacy 폴백 / T-M5 스키마 버전 불일치 → 명확한 오류 / T-M6 경로에 비ASCII·공백 포함

---

## 4. MB-4 — Terminal 세션 재바인딩

### 4.1 신규 파일 `orca_loop/session.py`

```python
EVENTS_NAME = "resume-events.jsonl"      # append-only JSON Lines

def terminal_alive(client, handle) -> bool:
    try:    client.call(("terminal","show","--terminal",handle), timeout_ms=10_000); return True
    except OrcaCommandError: return False

def ensure_coordinator_terminal(client, *, worktree, run_id, recorded) -> tuple[str, bool]
def ensure_worker_pool(client, *, worktree_selector, recorded, coordinator_handle) -> tuple[WorkerPool, tuple[WorkerKey,...]]
def append_event(control_dir, kind: str, detail: Mapping[str, object]) -> None
```

`ensure_worker_pool` 의사코드:

```python
alive, rebound = [], []
for key in WorkerKey:                       # 결정적 순서
    handle = recorded.get(key)
    if handle and terminal_alive(client, handle):
        alive.append(existing_handle_object(key, handle)); continue
    created = create_terminal(client, worktree_selector, title=f"ORCA LOOP {key.value}")
    if created.terminal_handle == coordinator_handle: raise WorkerProvisionError(...)
    alive.append(created); rebound.append(key)
assert len({h.terminal_handle for h in alive}) == 4
return WorkerPool(tuple(alive)), tuple(rebound)
```

### 4.2 preflight 완화 (MB-4.1)

`config.run_preflight()` 말미의 `terminal show --terminal <coordinator_handle>`(`config.py:982-990`)를 **`verify_coordinator: bool = True` 파라미터로 분기**한다. `resume` 경로는 `False`로 호출하고, 생존 확인·재생성은 `session.ensure_coordinator_terminal()`이 담당한다. `start` 경로의 동작은 불변.

### 4.3 테스트 (MB-4.2) — `tests/test_session.py`

T-S1 전부 생존 → 재생성 0건 / T-S2 coordinator만 사망 → coordinator만 재생성 / T-S3 worker 2개 사망 → 2개만 재생성, 나머지 handle 불변 / T-S4 재생성 handle == coordinator → `WorkerProvisionError` / T-S5 이벤트가 JSONL로 append(기존 줄 보존)

---

## 5. MB-5 — Stale lock, Resume 조정, Drift 정책

### 5.1 Stale lock 회수 (MB-5.1) — 최우선

`locking.py`에 추가:

```python
def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0)은 Windows에서 TerminateProcess를 호출하므로 절대 사용 금지
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle: return False
        ctypes.windll.kernel32.CloseHandle(handle); return True
    try: os.kill(pid, 0)
    except ProcessLookupError: return False
    except PermissionError:    return True
    return True

@dataclass(frozen=True)
class StaleLockInfo: path: Path; run_id: str; pid: int; alive: bool; age_seconds: float

def inspect_lock(harness_root, worktree) -> StaleLockInfo | None
def acquire_run_lock(harness_root, worktree, run_id, *, reclaim_stale: bool = False,
                     force: bool = False) -> RunLock
```

회수 규칙:

| 조건 | 동작 |
| --- | --- |
| 락 없음 | 정상 획득 |
| 락 존재 + pid **사망** + `reclaim_stale=True` | 락 파일 삭제 후 재획득, `resume-events.jsonl`에 `lock_reclaimed` 기록 |
| 락 존재 + pid **생존** | 기존과 동일하게 `RunLockError` (동시 실행 방지). `force=True`일 때만 회수 |
| 락 파일 손상(JSON 파싱 불가) | pid 판정 불가 → `force=True`에서만 회수 |

`reclaim_stale`은 `resume`에서 **기본 True**, `start`에서는 기본 True(동일 worktree의 죽은 run이 새 run을 막을 이유가 없음). `force`는 `--force-unlock` 플래그로만.

PID 재사용 위험: pid가 살아 있으면 회수하지 않으므로 오탐은 "재시작이 거부되는" 안전한 방향으로만 발생한다.

**즉시 효과:** 현재 남아 있는 stale lock 2건이 자동 회수되어 `platform-poc` run이 다시 가능해진다.

### 5.2 `reconcile_resume()` 실연결 (MB-5.2)

`run_loop._resume()`을 다음으로 대체한다.

```python
def _resume(preflight, client):
    workspace, _ = create_run_workspace(..., "resume", resume=True)
    manifest = runspec.read_manifest(workspace.control_dir)          # MB-3
    state, ledger, _ = load_committed(workspace.control_dir)
    if state.run_id != arguments.run_id: raise OrcaLoopError(...)
    if state.state in {FAILED, REJECTED}:                            # 운영규칙 §11 유지
        raise OrcaLoopError("failed or rejected run cannot be resumed")

    coordinator, rebound_c = session.ensure_coordinator_terminal(...)  # MB-4
    pool, rebound_w        = session.ensure_worker_pool(...)
    if rebound_c or rebound_w:
        session.append_event(control_dir, "terminal_rebound", {...})
        runspec.update_terminals(control_dir, coordinator, pool)

    drift = _resolve_drift(state, worktree, allow=arguments.accept_worktree_drift)  # 5.3
    controller = GenerationController(workspace, state, ledger)
    if rebound_c or drift.rebaselined:
        controller.commit(stage=state.step_stage, active=state.active,
                          reason="resume rebound/rebaselined",
                          snapshot_digest=drift.new_digest or None)

    decision = reconcile_resume(state, ledger,
                                task_exists=_task_exists(client, state.active),
                                dispatch_exists=_dispatch_exists(client, state.active),
                                output_exists=_step_output_exists(workspace, state.active))
    _apply_resume_decision(controller, decision)     # 아래 표
    return controller, pool
```

`_apply_resume_decision` 매핑:

| `ResumeAction` | 동작 |
| --- | --- |
| `CREATE_TASK` | 아무것도 하지 않음 — `_run_loop()`이 현재 state에서 새 step을 만든다 |
| `DISPATCH_TASK` / `WAIT_DISPATCH` | 현행 구현은 step 재실행이 더 단순하고 안전하므로, **step 폐기 후 재실행**으로 통일. 폐기 시 `steps/<id>/ABANDONED` 마커 기록 |
| `PROMOTE_ARTIFACT` | `out/`에 artifact가 있으면 승격 재시도, 실패 시 폐기 후 재실행 |
| `APPLY_TRANSITION` | 전이만 재적용 |
| `USER_DECISION_REQUIRED` | 기존 gate 경로 진입 (exit 3) |

> 설계 근거: Orca task/dispatch는 재생성 비용이 낮고, 이미 죽은 dispatch를 기다리는 것이 가장 흔한 정지 원인이었다(마지막 run이 정확히 `STEP_DISPATCHED`에서 멈춤). "대기"보다 "재실행"을 기본으로 삼는다. 폐기된 step은 삭제하지 않으므로 증거는 보존된다.

`ABANDONED` 마커 내용: 폐기 사유, 원래 task_id/dispatch_id, 폐기 시각, 후속 step_id.

### 5.3 Drift 정책 (MB-5.3)

```python
@dataclass(frozen=True)
class DriftDecision: drifted: bool; rebaselined: bool; new_digest: str|None; report: str|None

READ_ONLY_STATES = {PLAN, PLAN_REVISE, PLAN_REVIEW, CODE_REVIEW, CROSS_CONFIRM,
                    PLAN_CONSENSUS_EVALUATE, CONSENSUS_EVALUATE, TEST_GATE}

def _resolve_drift(state, worktree, *, allow):
    current = capture_snapshot(worktree)
    if current.snapshot_digest == state.snapshot_digest: return DriftDecision(False, False, None, None)
    report = render_drift_report(state, current)          # base_head, tracked/staged/untracked 차이
    if state.state in READ_ONLY_STATES:                   # A-3
        append_event(..., "drift_rebaselined", {...});  return DriftDecision(True, True, current.snapshot_digest, report)
    if allow:                                             # A-2, --accept-worktree-drift
        append_event(..., "drift_accepted", {...});     return DriftDecision(True, True, current.snapshot_digest, report)
    write reports/99-failure.md(report)
    raise ResumeBlockedError(...)                         # → exit 3
```

`ResumeBlockedError`는 `run_loop.main()`에서 `EXIT_USER_REQUIRED`(3)로 매핑한다. **파일 삭제/복원/reset은 어떤 경로에서도 하지 않는다.**

### 5.4 테스트 (MB-5.4) — `tests/test_resume.py`, `tests/test_locking.py` 보강

| ID | 케이스 | 기대 |
| --- | --- | --- |
| T-L1 | 죽은 pid 락 + resume | 자동 회수, 이벤트 기록 |
| T-L2 | 살아있는 pid 락 | `RunLockError` 유지 |
| T-L3 | 손상된 락 파일 | `--force-unlock` 없이는 거부 |
| T-R1~R6 | step_stage 6종 × (task/dispatch 생존·사망) | 결정 테이블 일치 |
| T-R7 | 읽기 단계 drift | 자동 재기준화 + 이벤트 |
| T-R8 | IMPLEMENT drift, 플래그 없음 | exit 3 + `99-failure.md` 생성 |
| T-R9 | IMPLEMENT drift + `--accept-worktree-drift` | 재기준화 후 진행 |
| T-R10 | FAILED/REJECTED run | resume 거부 |
| T-R11 | 폐기 step | `ABANDONED` 존재, 원본 `in`/`out` 보존 |

---

## 6. MB-6 — 단계별 리포트 및 artifact 이력

### 6.1 신규 파일 `orca_loop/reporting.py`

```python
STAGE_ORDER = (("plan","01-plan"), ("plan_review","02-plan-review"),
               ("implementation","03-implementation"), ("code_review","04-code-review"),
               ("cross_review","05-cross-review"))

def record_artifact_history(artifact_dir, kind, generation, raw: bytes) -> Path
def render_stage_report(run_root, kind, artifact_obj, generation) -> Path
def render_run_summary(run_root, state, ledger, manifest) -> Path
def render_failure_report(run_root, *, reason, evidence_paths, resume_command) -> Path
def resume_command_line(manifest) -> str
```

모든 함수는 **예외를 밖으로 던지지 않는다**(내부 try/except → 실패 시 `logs/reporting.log`에 기록하고 `None` 반환). 리포트 실패가 run을 죽이지 않게 한다(Phase 2 RK-7).

### 6.2 호출 지점 (MB-6.1)

| 지점 | 호출 |
| --- | --- |
| `transport.promote_artifact()` 승격 성공 직후 | `record_artifact_history()` — `artifacts/history/<kind>.g<gen>.json` |
| `run_loop._execute_worker()` artifact 검증 통과 후 | `render_stage_report()` |
| `GenerationController.commit()` **커밋 성공 이후** | `render_run_summary()` (best-effort) |
| `run_loop.main()` 예외/비정상 종료 경로 | `render_failure_report()` |

`promote_artifact`는 `generation` 인자가 없으므로 시그니처에 `generation: int | None = None`을 **선택 인자로 추가**한다(기존 호출부 무변경 가능).

### 6.3 `00-run-summary.md` 내용 규격 (MB-6.2)

```markdown
# Orca Loop Run Summary — <run-id>
갱신: <ISO8601>  |  상태: <status> / <state>  |  generation: <N>

## 단계별 진행
| 단계 | 상태 | artifact | 갱신 |
| plan | 완료 | artifacts/plan.json (v3) | g0012 |
| plan_review | 진행 중 | — | — |
...

## Consensus
plan_round=2/5, code_round=0/5, unresolved findings=3

## 최근 전이 (최대 10)
g0012 PLAN artifact verified (ok)

## 마지막 오류
<있으면 원인 + 증거 파일 경로>

## 재시작 방법
py -3 <harness>\run_loop.py resume --run-id <run-id>
```

### 6.4 테스트 (MB-6.3) — `tests/test_reporting.py`

T-P1 plan 5회 개정 → history 5개 + 현재본 유지 / T-P2 전이마다 요약 갱신 / T-P3 요약의 resume 명령줄이 실제 파서를 통과 / T-P4 리포트 생성 실패가 commit을 실패시키지 않음 / T-P5 실패 리포트에 증거 경로(logs 3종) 포함 / T-P6 비ASCII 본문 UTF-8 기록

---

## 7. MB-7 — 고정 실행 표면

### 7.1 파서 재구성 (MB-7.1)

```python
SUBCOMMANDS = ("start", "resume", "status", "doctor")

def parse_run_arguments(argv, *, harness_root):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-") or argv[0] not in SUBCOMMANDS:
        argv = ["start", *argv]          # ← A-5 하위 호환
    ...
```

`start` 파서는 **기존 플래그 전체를 그대로 유지**하고 아래를 추가한다.

| 플래그 | 의미 |
| --- | --- |
| `--agent K=MODEL/EFFORT` (반복) | `--agent-model`+`--agent-effort` 축약. 두 형식 혼용 시 명시 플래그 우선 |
| `--strict-agent-runtime` | 정규화 fallback 금지 (MB-1) |
| `--no-create-terminals` | coordinator terminal 자동 생성 비활성(수동 `--coordinator-handle` 사용) |
| `--accept-worktree-drift` | resume 전용, start에서는 무시 |
| `--force-unlock` | stale lock 강제 회수 |
| `--export-dir PATH` | 종료 시 `reports/`·`artifacts/` 복사 |

`--coordinator-handle`은 **required에서 optional로** 변경한다. 미지정 + `--no-create-terminals` 미지정이면 harness가 생성한다.

### 7.2 permission report 자동 탐색 (MB-7.2)

```python
def discover_permission_report(harness_root, orca_version) -> Path:
    candidates = sorted(harness_root.glob("runs/*/control/permission-feasibility.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        report = parse_permission_report(path.read_text("utf-8"))   # 실패 시 skip
        if (report.status is PASS and report.strategy is READONLY_REPOSITORY
                and report.orca_version == orca_version
                and all(c.status is PASS for c in report.checks)
                and Path(report.canonical_path).resolve() == path.resolve()):
            return path
    raise PreflightError("no valid permission feasibility report; ...")
```

**검증 조건은 SKILL.md가 요구하던 것과 동일하다.** 사람이 아니라 코드가 수행할 뿐이며, 조건을 약화하지 않는다. `--permission-report` 명시 시에는 탐색하지 않는다.

### 7.3 `status` / `doctor` (MB-7.3)

- `status --run-id X` — **읽기 전용**. manifest + committed state + 단계별 artifact 존재 여부 + stale lock 여부 + 재시작 명령줄을 JSON과 사람이 읽는 형식으로 출력. 파일을 만들거나 고치지 않는다.
- `doctor` — orca `status --json` 도달성, orca 버전 대조, `claude`/`codex` 실행 파일 존재, 카탈로그 로드 출처, permission report 후보 목록, stale lock 목록을 진단만 한다.

### 7.4 테스트 (MB-7.4) — `tests/test_cli.py` 보강

T-X1 기존 98 테스트 **무수정 통과**(최우선) / T-X2 subcommand 없는 호출 = `start` / T-X3 `--agent` 축약 파싱 / T-X4 permission 자동 탐색이 조건 미달본을 거부 / T-X5 dry-run 출력에 정규화 결과 포함 / T-X6 `status`가 파일을 변경하지 않음 / T-X7 `resume --run-id`만으로 인자 복원

---

## 8. MB-8 — SKILL.md 재작성

절 구성: 고정 계약 / 도구 사용 / Gate 1 (작업 프롬프트, verbatim 유지) / Gate 2 (model·effort — 카탈로그 제시 + 자유 입력 허용 + **dry-run 정규화 결과 확인**) / 실행 (`start --dry-run` → `start` 2개 명령) / 모니터링 / **중단 시 복구** (`status` → `resume`) / 산출물 위치 / 종료 코드 해석.

삭제 대상: permission report 수동 탐색, terminal 수동 생성, 13개+ 플래그 조립, 동일 문자열 재전송 지시.

검증: 문서 내 자유 조립 단계 0개.

---

## 9. MB-9 — 회귀 및 문서

- 전체 테스트: baseline 98 + 신규 약 45개
- 실제 검증: `doctor` → `start --dry-run` → `start` → **worker 단계에서 강제 종료** → `status` → `resume` 완주
- `orca_loop_execution_rules.md` §3(모델 별칭 금지 → 정규화가 흡수), §9.1(증거 경로 추가), §11·§12(재시작 절차) 갱신
- Phase 4 구현 보고서 작성

---

## 10. 구현 순서 및 규모 추정

| 순서 | 블록 | 신규 | 변경 | 신규 테스트 |
| --- | --- | --- | --- | --- |
| 1 | MB-5.1 (stale lock) | — | `locking.py` | 3 |
| 2 | MB-1 | `catalog.py` | `config.py` | 11 |
| 3 | MB-2 | — | `worker_runner.py`, `dispatcher.py` | 6 |
| 4 | MB-3 | `runspec.py` | `run_loop.py`, `config.py` | 6 |
| 5 | MB-4 | `session.py` | `run_loop.py`, `config.py` | 5 |
| 6 | MB-5.2~5.4 | — | `run_loop.py`, `coordinator.py` | 11 |
| 7 | MB-6 | `reporting.py` | `transport.py`, `coordinator.py`, `run_loop.py` | 6 |
| 8 | MB-7 | — | `config.py`, `run_loop.py` | 7 |
| 9 | MB-8 / MB-9 | — | `SKILL.md`, 운영 문서 | — |

MB-5.1을 맨 앞으로 올렸다 — 현재 stale lock 때문에 어떤 실제 검증도 불가능하기 때문이다.

---

## 11. Validation and Risks

### 11.1 Validation Performed (현 시점)

| 항목 | 상태 | 근거 |
| --- | --- | --- |
| baseline 테스트 | **PASS** | 98 tests OK |
| stale lock 존재 및 pid 사망 확인 | **PASS** | 락 2건, PID 45152·23824 모두 NOT RUNNING |
| `agent-runtime.json` 스키마 확장 위험 확인 | **PASS** | `_exact` 5키 고정 → 설계 조정으로 회피 |
| codex 카탈로그 필드 확인 | **PASS** | slug / supported_reasoning_levels / default_reasoning_level |
| worker job 스키마 엄격성 확인 | **PASS** | `set(value) != required` |
| 코드 구현 | **NOT RUN** | Phase 4 대상 |

### 11.2 잔여 위험

| ID | 위험 | 완화 |
| --- | --- | --- |
| RK-11 | Windows에서 `os.kill(pid,0)`이 프로세스를 종료시킴 | `OpenProcess` 기반 판정으로 대체(설계에 명시), 유닉스 경로와 분리 |
| RK-12 | PID 재사용으로 살아있는 락을 오탐 | 생존 pid는 회수하지 않음 → 오탐이 "거부" 방향으로만 발생 |
| RK-13 | resume이 dispatch 대기 대신 재실행을 택해 worker 중복 실행 | 폐기 step에 `ABANDONED` 기록, 재실행은 새 step_id·새 task로 수행하여 provenance 충돌 없음 |
| RK-14 | MB-7 파서 재구성 회귀 | 하위호환 폴백을 **먼저** 구현하고 98 테스트 통과 확인 후 신규 기능 추가 |

---

## 12. Approval Status

- [ ] Phase 3 Micro Blocking 승인
- [ ] 수정 요청
- [ ] Phase 4 (Code Implementation) 진행 허가
