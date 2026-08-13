# Task Report: Orca Loop Communication Hardening

**Current Phase:** 2. Macro Blocking
**Status:** Waiting for Explicit User Approval

---

## 1. Context & Objective

- **Problem:** 현재 harness는 Orca `Run` ownership, terminal attestation, Delivery ACK,
  worker settlement, resume, gate authorization이 하나의 검증된 lifecycle로 연결되지
  않는다. 전체 unit test는 통과하지만 최신 Orca `1.4.180`의 실제 통신 계약을 충분히
  검증하지 않는다.
- **Goal:** 승인된 Phase 1 설계를 구현 가능한 대형 작업 블록으로 분해하고, 각 블록의
  입력·출력·side effect·failure mode·validation 경계를 확정한다.
- **Scope:** `run_loop.py`, `worker_runner.py`, `permission_spike.py`, `orca_loop/`,
  `prompts/`, `tests/`, Codex/Claude `orca-loop` skill.
- **Out of Scope:** worker 수와 역할 변경, provider 교체, consensus 정책 변경, target
  application 수정, dependency 추가, 실제 token-consuming Orca Loop 실행, Git push.

---

## 2. Core Deliverables

### B-01 — Orca protocol capability 및 coordinator ownership

| Field | Definition |
| --- | --- |
| **Block ID** | `B-01` |
| **Name** | Explicit Run and attested coordinator foundation |
| **Rationale** | 모든 Task, Dispatch, Message, Gate가 동일한 신뢰 주체와 Run namespace에 속해야 후속 통신 보강이 의미를 갖는다. |
| **Objective** | 새 run은 attested coordinator terminal에서 정확히 하나의 Orca Run을 생성하고, resume은 동일 Run을 재바인딩하며, 모든 orchestration 호출이 명시적 Run에 귀속된다. |
| **Scope** | Orca capability preflight, coordinator identity validation, `run-create`, `run-use`, `run-current`, explicit `--run`, harness run ID와 Orca Run ID 분리 |
| **Exclusions** | worker message 처리, resume attempt 결정, gate resolution 정책 |
| **Dependencies** | 없음 |
| **Input** | harness `run_id: str`, attested terminal environment, Orca status/capabilities, persisted `orchestration_run_id: str | None` |
| **Output** | verified coordinator binding과 nonempty `orchestration_run_id` |
| **Side Effects** | 새 run에서 Orca Run 1개 생성, manifest/state 갱신 |
| **Failure Modes** | `run_required`, `consumer_fenced`, missing capability, foreign ambient Run, malformed receipt. Side effect 전에는 `BLOCKED`, 불확실한 mutation 결과는 B-02 recovery로 전달한다. |
| **Validation** | `V-B01-01` no ambient Run start, `V-B01-02` foreign ambient Run isolation, `V-B01-03` foreign `--from` rejection, `V-B01-04` every command exact `--run`, `V-B01-05` version-only drift remains NOTE while missing capability is BLOCKED |

**High-Level Pseudocode**

```text
inspect current terminal attestation and required capabilities
if new run:
    create Run from current attested coordinator
    validate returned coordinator and Run IDs
else:
    bind persisted Run from current attested coordinator
    validate current binding
persist harness_run_id and orchestration_run_id separately
pass exact Run ID to every orchestration operation
```

### B-02 — Durable communication state and idempotent mutation journal

| Field | Definition |
| --- | --- |
| **Block ID** | `B-02` |
| **Name** | Durable orchestration mutation protocol |
| **Rationale** | External mutation 적용과 local commit 사이의 crash window가 Task, Dispatch, Gate, Message 중복을 만든다. |
| **Objective** | 모든 orchestration mutation이 durable intent, stable request ID, exact retry 및 applied receipt를 갖는다. |
| **Scope** | state/manifest schema migration, mutation journal, `--retry-request`, Task/Dispatch/Gate/send intent와 receipt |
| **Exclusions** | 메시지 내용 분류, worker artifact schema |
| **Dependencies** | `B-01` |
| **Input** | operation kind, canonical argv/parameters, harness generation, stable request ID |
| **Output** | `INTENT -> APPLIED -> COMMITTED` operation record와 external object ID |
| **Side Effects** | `runs/<run-id>/control`에 atomic journal/state write, Orca mutation |
| **Failure Modes** | effect applied but response lost, response received but local commit failed, mismatched retry argv, corrupt journal. Exact replay만 허용하고 ambiguity는 fail closed한다. |
| **Validation** | `V-B02-01` crash before mutation, `V-B02-02` effect-after-response-loss, `V-B02-03` crash before binding commit, `V-B02-04` mismatched request replay rejection, `V-B02-05` legacy state migration |

**High-Level Pseudocode**

```text
derive stable request ID from run/generation/operation
atomically persist mutation INTENT and canonical parameters
execute mutation with --retry-request
persist external receipt and object identity
commit domain state
mark operation COMMITTED
on restart, replay only the exact unresolved request
```

### B-03 — Durable inbox, message provenance, and ACK discipline

| Field | Definition |
| --- | --- |
| **Block ID** | `B-03` |
| **Name** | Lossless coordinator inbox |
| **Rationale** | Orca `check`는 최대 50개 메시지의 full FIFO Delivery를 반환하므로 step별 즉시 ACK 구조는 foreign 또는 conflicting message를 잃는다. |
| **Objective** | Delivery 전체를 durable하게 수신·검증·분류한 뒤에만 ACK하고, crash/replay/duplicate에서도 정확히 한 번 domain event로 승격한다. |
| **Scope** | raw envelope journal, Delivery ID/message ID, sender/Run/task/dispatch 검증, quarantine, duplicate/conflict policy, ACK ordering |
| **Exclusions** | worker process launch, gate decision semantics |
| **Dependencies** | `B-01`, `B-02` |
| **Input** | Orca Delivery envelope와 expected active/recoverable Dispatch set |
| **Output** | classified inbox records: accepted, deferred, duplicate, quarantined, conflicting |
| **Side Effects** | durable inbox append, Orca Delivery ACK, bounded audit log |
| **Failure Modes** | malformed payload, envelope/payload ID mismatch, foreign sender, multiple terminal signals, ACK failure, crash before/after ACK. 처리되지 않은 row가 있으면 ACK하지 않는다. |
| **Validation** | `V-B03-01` mixed active/foreign batch, `V-B03-02` foreign-only batch, `V-B03-03` duplicate identical completion, `V-B03-04` escalation/worker_done conflict permutations, `V-B03-05` ACK crash boundaries, `V-B03-06` sender spoof rejection |

**High-Level Pseudocode**

```text
check oldest Delivery for exact Run
atomically record raw Delivery and every message
validate trusted envelope identity and payload consistency
classify every row without discarding foreign rows
promote accepted events idempotently
persist classification and required follow-up decisions
ACK only when the complete Delivery is durable and handled
```

### B-04 — Worker execution, isolation, and exactly-once settlement

| Field | Definition |
| --- | --- |
| **Block ID** | `B-04` |
| **Name** | Wrapper-owned worker lifecycle |
| **Rationale** | Child agent가 lifecycle identity를 상속하고 artifact 검증 전에 success settlement가 발생하면 agent와 wrapper가 같은 Dispatch 권한을 경쟁한다. |
| **Objective** | Child agent는 artifact 생성만 수행하고 wrapper/coordinator만 lifecycle signal과 final outcome을 소유한다. 성공·실패 모두 정확히 한 번 settlement된다. |
| **Scope** | sanitized child environment, prompt/preamble authority 정리, typed message flags, artifact-ready/validation/final settlement, bounded process termination |
| **Exclusions** | agent provider/model 정책, artifact business schema 변경 |
| **Dependencies** | `B-01`, `B-02`, `B-03` |
| **Input** | validated Job contract, agent process output, artifact path/digest, assigned worker identity |
| **Output** | validated artifact plus one terminal `worker_done` outcome, or fenced failure record |
| **Side Effects** | child process 실행/종료, artifact/evidence atomic write, Orca lifecycle message |
| **Failure Modes** | direct agent lifecycle attempt, timeout, process kill failure, output ambiguity, artifact validation failure, send response loss. Secret 및 Orca lifecycle identity는 child에게 전달하지 않는다. |
| **Validation** | `V-B04-01` child `ORCA_*` non-exposure, `V-B04-02` successful exactly-once settlement, `V-B04-03` failed outcome settlement, `V-B04-04` malformed artifact cannot remain succeeded, `V-B04-05` bounded Windows termination, `V-B04-06` preamble/ID mismatch rejection |

**High-Level Pseudocode**

```text
construct minimal child environment without lifecycle authority
run agent with bounded timeout and evidence capture
extract exactly one artifact candidate
validate claimed provenance instead of silently overwriting conflicts
persist artifact-ready receipt
coordinator validates schema, digest, scope, and repository guard
send one final succeeded or failed outcome through the wrapper authority
```

### B-05 — Resume reconciliation and terminal resource accounting

| Field | Definition |
| --- | --- |
| **Block ID** | `B-05` |
| **Name** | Authoritative restart and worker recovery |
| **Rationale** | Local binding만 보고 active step을 abandon하면 live worker와 replacement가 동일 worktree를 동시에 수정할 수 있다. |
| **Objective** | Resume이 durable local state와 authoritative Orca state를 대조하여 adopt/wait, recover, stop, abandon, retry 중 하나를 결정하고 old worker를 명확히 fence한다. |
| **Scope** | `worker-show`, `dispatch-show`, late message replay, `worker-stop`, `worker-abandon`, `retry-of`, `worker-retain/release/reuse`, terminal incarnation validation |
| **Exclusions** | Git drift 승인 정책 자체, user-requested destructive cleanup |
| **Dependencies** | `B-01`~`B-04` |
| **Input** | durable operation/inbox state, active Task/Dispatch IDs, Orca worker state, terminal metadata, artifact digest |
| **Output** | deterministic `ADOPT`, `RECOVER`, `STOP_AND_RETRY`, `ABANDON_AND_BLOCK`, `SETTLED` decision |
| **Side Effects** | Run rebind, worker fence/stop/retain/release/reuse, recovery event commit |
| **Failure Modes** | `ready`, `failed`, `stopped`, `outcome_unknown`, disconnected/orphaned terminal, transient show failure, late completion. 불확실한 process는 새 editor와 병행하지 않는다. |
| **Validation** | `V-B05-01` live worker adoption, `V-B05-02` settled completion recovery, `V-B05-03` outcome-unknown block/fence, `V-B05-04` retry-of identity, `V-B05-05` late stale completion quarantine, `V-B05-06` terminal release/reuse accounting |

**High-Level Pseudocode**

```text
bind persisted Run to attested coordinator
replay durable inbox and unresolved mutation intents
query authoritative Dispatch and worker state
if settled: recover exact result and account terminal
if ready: resume waiting without replacement
if failed or stopped: retry with explicit retry-of
if outcome unknown: stop or abandon, then block unless fencing is proven
persist decision before launching any replacement
```

### B-06 — Gate authorization and decision provenance

| Field | Definition |
| --- | --- |
| **Block ID** | `B-06` |
| **Name** | Authorized gate resolution |
| **Rationale** | Gate options가 UI text로만 존재하면 unlisted decision이 approval obligation을 우회할 수 있다. |
| **Objective** | Gate 생성 시 허용 decision과 blocked context를 저장하고, resolution을 state transition 전에 exact하게 검증한다. |
| **Scope** | `GateBinding` 확장, allowed decision membership, report digest, finding/acceptance scope, recovered gate validation, Run scoping |
| **Exclusions** | 새로운 decision 종류, 자동 승인 |
| **Dependencies** | `B-01`, `B-02`, `B-03` |
| **Input** | gate kind, allowed decisions, blocked state, report digest, finding/acceptance scope, raw resolution |
| **Output** | authorized `HumanDecision` 또는 typed `GateProtocolError` |
| **Side Effects** | Gate create/list/resolve 조회, approved decision state commit |
| **Failure Modes** | unlisted decision, stale digest, foreign Run/task/gate, broadened affected IDs, malformed strict JSON. 실패 시 gate와 blocked context를 유지한다. |
| **Validation** | `V-B06-01` E-03 rejects `revise_code`, `V-B06-02` destructive gate option enforcement, `V-B06-03` stale/foreign gate rejection, `V-B06-04` affected scope subset, `V-B06-05` recovered option equality |

**High-Level Pseudocode**

```text
persist gate authorization contract before gate creation
create or recover gate inside exact Run and Task
read resolved decision
validate gate identity, report digest, allowed decision, and affected scope
route only an authorized decision
clear gate context only after successful durable transition
```

### B-07 — Manifest, status, startup, and terminal consistency

| Field | Definition |
| --- | --- |
| **Block ID** | `B-07` |
| **Name** | Operational metadata and side-effect ordering |
| **Rationale** | Manifest identity mismatch, weak terminal liveness, premature provisioning, inaccurate status가 recovery와 operator 판단을 왜곡한다. |
| **Objective** | Side effect 전 identity/preflight/lock을 확정하고, manifest/state/terminal/status가 같은 run과 worktree를 가리키도록 한다. |
| **Scope** | manifest schema/invariants, requested run ID validation, control boundary, terminal metadata/incarnation, preflight-lock-provision order, typed provisioning errors, CLI help/status/doctor |
| **Exclusions** | orchestration message semantics, target repository cleanup |
| **Dependencies** | `B-01`, `B-02`, `B-05` |
| **Input** | requested run ID, manifest/state, resolved paths, terminal show metadata, lock state |
| **Output** | verified run context와 truthful health/resumable report |
| **Side Effects** | manifest/state migration, terminals only after validation/lock, failure report |
| **Failure Modes** | copied manifest, request-copy path escape, missing/malformed manifest, wrong worktree terminal, orphaned/disconnected terminal, transient lookup error, partial provisioning. 실패를 `BLOCKED` 또는 `FAIL`로 명확히 분류한다. |
| **Validation** | `V-B07-01` copied manifest rejection, `V-B07-02` control boundary enforcement, `V-B07-03` wrong/disconnected terminal rejection, `V-B07-04` transient error does not create duplicate terminal, `V-B07-05` preflight/lock failure creates zero terminals, `V-B07-06` truthful status/resumable, `V-B07-07` subcommand help exits 0 |

**High-Level Pseudocode**

```text
parse command structure without side effects
validate run ID, paths, manifest identity, inputs, capabilities, and Git state
acquire exact worktree lock
validate or provision attested terminals with durable ownership records
execute coordinator
derive status and resumability from all validated blockers
```

### B-08 — Permission, test, parser, and process integrity

| Field | Definition |
| --- | --- |
| **Block ID** | `B-08` |
| **Name** | Execution trust-boundary hardening |
| **Rationale** | Permission report와 test guard가 false PASS를 허용하거나 child environment/parser/timeout이 비결정적이면 통신 수정 후에도 결과를 신뢰할 수 없다. |
| **Objective** | Worker status와 evidence가 일치할 때만 permission PASS를 만들고, test side effect와 agent output을 보수적으로 검증하며 모든 subprocess 경로를 bounded하게 만든다. |
| **Scope** | permission worker status propagation, `.gitignore` 포함 filesystem guard, output parser ambiguity rejection, sanitized environment, bounded subprocess helpers, `_doctor_report` exception defect |
| **Exclusions** | permission strategy 변경, 외부 sandbox dependency 추가, historic report 자동 삭제 |
| **Dependencies** | `B-04`; 나머지 항목은 병렬 구현 가능 |
| **Input** | worker validation status/evidence, before/after filesystem state, provider output stream, process timeout policy |
| **Output** | truthful permission/test status, exactly one artifact candidate, bounded process result |
| **Side Effects** | validation report/evidence write, test command 및 agent subprocess 실행 |
| **Failure Modes** | worker FAIL with truthy evidence, ignored output creation, write-restore mutation, multiple JSON candidates, kill/send hang, secret exposure. 모호성은 PASS로 변환하지 않는다. |
| **Validation** | `V-B08-01` FAIL/BLOCKED/NOT_RUN propagation, `V-B08-02` ignored output detection, `V-B08-03` multiple artifact rejection, `V-B08-04` minimal child env, `V-B08-05` bounded kill/drain/send, `V-B08-06` doctor failure path |

**High-Level Pseudocode**

```text
require worker PASS before evaluating positive permission evidence
capture filesystem state across the approved boundary
run command with sanitized environment and bounded process control
reject disallowed or ambiguous mutations and output candidates
emit exact PASS/FAIL/BLOCKED/NOT_RUN evidence
```

### B-09 — Orca Loop skill synchronization

| Field | Definition |
| --- | --- |
| **Block ID** | `B-09` |
| **Name** | Codex and Claude skill contract alignment |
| **Rationale** | 현재 Codex skill은 manual Run setup을, Claude skill은 harness-owned automation을 설명하여 동일 harness에 상충하는 조작을 지시한다. |
| **Objective** | 코드가 제공하는 단일 CLI/lifecycle 계약을 두 skill에 반영하고 skill이 harness-owned mailbox와 Run state를 직접 조작하지 않게 한다. |
| **Scope** | Codex `SKILL.md`, `agents/openai.yaml`, Claude `SKILL.md`, trigger/intake/dry-run/start/status/resume/monitor/reporting contract |
| **Exclusions** | 새 skill 생성, README/CHANGELOG 추가, unrelated skill 수정 |
| **Dependencies** | `B-01`~`B-08`의 최종 public behavior |
| **Input** | validated CLI help, final state/exit contract, runtime evidence paths, model catalog behavior |
| **Output** | concise, non-conflicting, version-compatible Codex/Claude skill instructions |
| **Side Effects** | 사용자 skill directory의 기존 파일 수정 |
| **Failure Modes** | stale CLI syntax, duplicated responsibilities, manual mailbox consumption, wrong worker-role mapping, obsolete permission version rule. Code/skill 불일치는 validation failure로 처리한다. |
| **Validation** | `V-B09-01` `quick_validate.py`, `V-B09-02` `agents/openai.yaml` consistency, `V-B09-03` command examples against `--help`, `V-B09-04` trigger/intake simulation, `V-B09-05` no manual Run/mailbox ownership conflict |

**High-Level Pseudocode**

```text
derive public workflow only from validated harness behavior
rewrite both skill bodies to the same ownership boundaries
keep SKILL.md concise and imperative
regenerate or verify Codex UI metadata
validate syntax and forward-test representative start/resume requests
```

### B-10 — Regression and live protocol validation

| Field | Definition |
| --- | --- |
| **Block ID** | `B-10` |
| **Name** | Layered verification and release evidence |
| **Rationale** | Mock-only tests가 최신 runtime contract 실패를 잡지 못했으므로 unit, fault-injection, disposable live protocol 검증을 분리해야 한다. |
| **Objective** | 각 Macro Block의 negative path를 회귀 테스트로 고정하고, token-consuming agent 없이 disposable Run에서 lifecycle grammar와 state transitions를 검증한다. |
| **Scope** | targeted unit tests, fault-injection tests, full suite, disposable Orca integration, doctor/help/dry-run, diff inspection, skill validation |
| **Exclusions** | 실제 provider token 사용, target application E2E, merge/push |
| **Dependencies** | `B-01`~`B-09` |
| **Input** | implemented code/skills, disposable fixture, active Orca runtime |
| **Output** | PASS/FAIL/BLOCKED/NOT RUN evidence matrix와 known limitations |
| **Side Effects** | disposable Orca Run/Task/Dispatch/Gate와 temporary fixture 생성; 테스트가 소유한 resource만 명시적으로 정리 |
| **Failure Modes** | unavailable runtime, consumer fencing, residual live worker, flaky timing, cleanup uncertainty. 실환경 검증 불가는 unit PASS와 분리해 `BLOCKED` 또는 `NOT RUN`으로 보고한다. |
| **Validation** | `V-B10-01` targeted tests, `V-B10-02` full 244+ suite, `V-B10-03` disposable no-agent Run/Task/Dispatch/check/ACK/gate flow, `V-B10-04` crash/replay suite, `V-B10-05` `doctor`/help/dry-run, `V-B10-06` `git diff --check` and clean scope review |

**High-Level Pseudocode**

```text
run block-specific tests after each implementation unit
run deterministic crash and response-loss injections
exercise disposable current Orca protocol without provider execution
run full regression suite and CLI diagnostics
validate both skills
report direct evidence separately from NOT RUN scope
```

### Dependency Order

```text
B-01
  -> B-02
      -> B-03
          -> B-04
              -> B-05
      -> B-06
      -> B-07
B-04 -> B-08
B-01..B-08 -> B-09
B-01..B-09 -> B-10
```

### Implementation Boundary Rules

1. 각 block은 targeted validation이 PASS한 뒤 다음 dependent block으로 이동한다.
2. Orca mutation은 `B-02` 이전에 확대하지 않는다.
3. `B-03`의 durable inbox 없이 production ACK behavior를 바꾸지 않는다.
4. `B-05` 이전에는 active worker를 자동 replacement하지 않는다.
5. `B-09`는 public CLI behavior가 확정된 뒤 마지막에 수정한다.
6. 새 production dependency는 계획에 포함하지 않으며 필요해질 경우 별도 승인을 받는다.

---

## 3. Validation and Risks

- **Validation:** Phase 1에서 확인한 source paths, Orca `1.4.180` help/guide,
  244-test baseline과 모든 confirmed finding을 각 Block 및 `V-*` 항목에 추적했다.
- **Risks:** state/manifest schema migration, crash injection의 재현성, terminal attestation
  제약, test가 생성한 disposable Orca resource의 소유권 구분이 주요 위험이다.
- **Open Questions:** 없음. Phase 3에서 exact dataclass/schema/exception/argv와 legacy
  manifest migration 규칙을 결정한다.

---

## 4. Approval or Completion Status

## Approval

- [ ] Macro Blocking approved
- [ ] Revision requested

**Next phase after explicit approval:** Phase 3 — Micro Blocking
