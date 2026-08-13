# Task Report: Orca Loop Communication Hardening

**Current Phase:** 3. Micro Blocking
**Status:** Waiting for Explicit User Approval

---

## 1. Context & Objective

- **Problem:** 승인된 10개 Macro Block을 구현하려면 durable schema, exact Orca argv,
  message envelope, worker settlement, resume, gate, operational validation의 변경 단위를
  함수와 테스트 수준으로 고정해야 한다.
- **Goal:** 각 변경을 하나의 책임과 독립 검증 기준을 갖는 Micro Block으로 분해한다.
- **Scope:** `run_loop.py`, `worker_runner.py`, `permission_spike.py`, 기존
  `orca_loop/*.py`, 기존 `tests/*.py`, Codex/Claude `orca-loop` skill.
- **Out of Scope:** 새 production dependency, worker 역할 변경, consensus 정책 변경,
  target application 변경, 실제 provider token 사용, Git push.

---

## 2. Shared Type Contracts

다음 타입은 Phase 4에서 `orca_loop/models.py`에 추가하거나 기존 타입을 확장한다.
새 Python source module은 만들지 않는다.

| Type | Exact fields and invariants |
| --- | --- |
| `TerminalIdentity` | `handle: str`, `incarnation_id: str`, `worktree_id: str`, `worktree_path: str`; 모든 값 nonempty, `worktree_path`는 resolved target과 동일 |
| `OrchestrationBinding` | `run_id: str`, `coordinator: TerminalIdentity`; new run에서는 모두 nonempty |
| `MutationKind` | `RUN_CREATE`, `RUN_USE`, `TASK_CREATE`, `DISPATCH`, `SEND`, `GATE_CREATE`, `WORKER_STOP`, `WORKER_ABANDON`, `WORKER_RELEASE` |
| `MutationPhase` | `INTENT`, `APPLIED`, `COMMITTED` |
| `MutationRecord` | `request_id: str`, `kind: MutationKind`, `phase: MutationPhase`, `run_id: str`, `generation: int`, `step_id: Optional[str]`, `canonical_argv: tuple[str, ...]`, `response_json: Optional[str]`, `external_id: Optional[str]`; request ID와 argv는 phase 전환 동안 불변 |
| `MessageEnvelope` | `message_id: str`, `message_type: str`, `from_handle: str`, `run_id: str`, `task_id: Optional[str]`, `dispatch_id: Optional[str]`, `payload_json: str`; trusted top-level ID와 payload ID가 함께 있으면 동일해야 함 |
| `InboxClassification` | `ACCEPTED`, `DEFERRED`, `DUPLICATE`, `QUARANTINED`, `CONFLICTING` |
| `DeliveryReceipt` | `delivery_id: str`, `messages: tuple[MessageEnvelope, ...]`, `classifications: tuple[InboxClassification, ...]`, `acked: bool`; tuple 길이는 같고 ACK 전 durable write 완료 |
| `SettlementState` | `WAITING`, `ARTIFACT_READY`, `VALIDATED_SUCCEEDED`, `VALIDATED_FAILED`, `SIGNALLED` |
| `SettlementRecord` | `task_id: str`, `dispatch_id: str`, `artifact_path: Optional[str]`, `artifact_digest: Optional[str]`, `state: SettlementState`, `outcome: Optional[str]`, `reason: Optional[str]`; outcome은 `succeeded`, `failed`, 또는 `None` |
| `ResumeOutcome` | `ADOPT_WAIT`, `RECOVER_SETTLED`, `STOP_AND_RETRY`, `ABANDON_AND_BLOCK`, `NO_ACTIVE_STEP` |
| `RunHealth` | `status: str`, `resumable: bool`, `blockers: tuple[str, ...]`, `warnings: tuple[str, ...]`; blockers가 있으면 `status != PASS` |

Durable schema는 `CoordinatorState.schema_version=2`와
`RunManifest.schema_version=2`를 사용한다. Legacy version 1은 read-time migration으로
`orchestration_run_id=None`을 부여하되, active legacy run의 resume은 새 Run을 추측해
생성하지 않고 `BLOCKED`한다.

---

## 3. Micro Blocks

### M-B01-01 — Persisted orchestration identity schema

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-01` |
| **Parent Block** | `B-01` |
| **Name** | Harness Run과 Orca Run identity 분리 |
| **Rationale** | 기존 `run_id`는 filesystem run ID이며 Orca namespace ID가 아니다. |
| **Objective** | state와 manifest가 `orchestration_run_id: Optional[str]` 및 coordinator terminal identity를 round-trip한다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/generation.py`, `orca_loop/runspec.py`, `run_loop.py`, `tests/test_resume.py`, `tests/test_runspec.py` |
| **Preconditions** | Phase 2 `B-01` 승인, 기존 schema version 1 fixture 보존 |
| **Input Type** | decoded `dict[str, Any]` with `schema_version: int` and existing state/manifest fields |
| **Input Validation** | version은 1 또는 2; version 2의 Run ID와 terminal identity는 exact field set 및 nonempty string; unknown version 거부 |
| **Output Type** | `CoordinatorState`, `RunManifest` with schema version 2 and `Optional[str]` Orca Run ID |
| **Output Validation** | new run은 nonempty Orca Run ID; migrated legacy record만 `None`; state/manifest harness run IDs 동일 |
| **Exceptions** | malformed state는 `AtomicWriteError`; malformed manifest는 `ManifestError`; legacy active resume는 이후 `ResumeBlockedError` |
| **Side Effects** | next successful commit에서 version 2 state/manifest atomic write |
| **Detailed Pseudocode** | 1. raw JSON 획득; 2. root/type 검증; 3. schema version 읽기; 4. version 1이면 missing fields를 `None`으로 migration; 5. version 2 exact fields 검증; 6. external call 없음; 7. typed dataclass 구성; 8. cross-ID invariant 검증; 9. commit 시 version 2 serialize; 10. typed value 반환 또는 exception |
| **Tests** | `T-B01-01` version 2 round-trip; `T-B01-02` version 1 migration; `T-B01-03` unknown version; `T-B01-04` state/manifest Run mismatch |
| **Rollback** | version 2 writer를 비활성화하되 reader migration은 유지하여 이미 생성된 run을 읽을 수 있게 한다. |

### M-B01-02 — Attested coordinator binding and explicit Run propagation

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B01-02` |
| **Parent Block** | `B-01` |
| **Name** | Capability preflight, `run-create/run-use`, exact `--run` |
| **Rationale** | foreign `--from`과 ambient Run 의존을 동시에 제거해야 한다. |
| **Objective** | 실제 start는 current `ORCA_TERMINAL_HANDLE`만 coordinator로 사용하고 모든 orchestration command에 persisted Run ID를 전달한다. |
| **Target Files** | `orca_loop/config.py`, `orca_loop/session.py`, `orca_loop/dispatcher.py`, `orca_loop/escalation.py`, `run_loop.py`, `tests/test_cli.py`, `tests/test_dispatcher.py`, `tests/test_escalation.py` |
| **Preconditions** | `M-B01-01`; runtime capabilities에 `orchestration.contract.v1`, `agent-session.host-authority.v1` 존재 |
| **Input Type** | `PreflightResult`, `Mapping[str, str]` environment, `Optional[str]` persisted Orca Run ID, `OrcaClient` |
| **Input Validation** | actual start의 environment handle nonempty; explicit coordinator와 current handle 동일; dry-run만 dummy identity 허용; required capabilities 포함 |
| **Output Type** | `OrchestrationBinding` |
| **Output Validation** | `run-current`의 Run ID와 expected ID 동일; coordinator handle/incarnation/worktree 동일; orchestration argv에 exact `--run` 존재 |
| **Exceptions** | missing capability/identity는 `PreflightError`; receipt mismatch는 `OrcaProtocolError`; legacy `None` resume는 `ResumeBlockedError` |
| **Side Effects** | new start에서 Run 1개 생성 또는 resume에서 기존 Run bind; binding state commit |
| **Detailed Pseudocode** | 1. status/environment 획득; 2. capability와 handle 검증; 3. persisted binding 읽기; 4. new/resume 분기; 5. `run-create` 또는 `run-use` 호출; 6. `run-current` 확인; 7. `OrchestrationBinding` 구성; 8. terminal/worktree invariant 검증; 9. state/manifest atomic commit; 10. 모든 Task/Dispatch/check/Gate 함수에 binding 전달 |
| **Tests** | `T-B01-05` no ambient Run start; `T-B01-06` foreign ambient isolation; `T-B01-07` current handle mismatch; `T-B01-08` missing capability; `T-B01-09` exact `--run` on all commands |
| **Rollback** | actual start를 `BLOCKED`로 유지하고 implicit/foreign identity 방식으로 되돌리지 않는다. |

### M-B02-01 — Durable mutation record store

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B02-01` |
| **Parent Block** | `B-02` |
| **Name** | Mutation intent와 receipt atomic persistence |
| **Rationale** | stable request ID가 memory에만 있으면 crash 후 exact recovery가 불가능하다. |
| **Objective** | operation별 `MutationRecord`를 `INTENT -> APPLIED -> COMMITTED`로 단조 전이시킨다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/generation.py`, `tests/test_resume.py` |
| **Preconditions** | `M-B01-01`; control directory boundary 검증 완료 |
| **Input Type** | `MutationRecord`, `Path control_dir` |
| **Input Validation** | UUID-format request ID; generation nonnegative; canonical argv nonempty; prior record와 immutable fields 동일; phase 역행 금지 |
| **Output Type** | persisted `MutationRecord` |
| **Output Validation** | reread value와 written value 동일; 동일 request ID는 record 1개; APPLIED는 response와 external ID invariant 충족 |
| **Exceptions** | invalid transition은 `GenerationMismatchError`; filesystem 오류는 `AtomicWriteError` |
| **Side Effects** | `control/orchestration-operations.json` atomic replace |
| **Detailed Pseudocode** | 1. control path 획득; 2. path boundary 검증; 3. 기존 records 읽기; 4. request ID lookup; 5. 신규 또는 legal transition 계산; 6. external call 없음; 7. canonical record 집합 구성; 8. phase/invariant 재검증; 9. atomic write와 reread; 10. persisted record 반환 |
| **Tests** | `T-B02-01` new intent; `T-B02-02` monotonic transition; `T-B02-03` conflicting argv; `T-B02-04` corrupt store; `T-B02-05` atomic write failure |
| **Rollback** | operation store를 보존한 채 새 mutation을 중단한다. Store 삭제나 request ID 재생성은 하지 않는다. |

### M-B02-02 — Idempotent Orca mutation executor

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B02-02` |
| **Parent Block** | `B-02` |
| **Name** | Exact `--retry-request` execution wrapper |
| **Rationale** | 각 caller가 별도로 retry를 구현하면 argv drift와 duplicate effect가 생긴다. |
| **Objective** | Run/Task/Dispatch/Message/Gate/worker mutation이 하나의 executor를 통해 durable exact retry를 사용한다. |
| **Target Files** | `orca_loop/orca_client.py`, `orca_loop/dispatcher.py`, `orca_loop/escalation.py`, `run_loop.py`, `worker_runner.py`, `tests/test_orca_client.py`, `tests/test_dispatcher.py`, `tests/test_escalation.py` |
| **Preconditions** | `M-B02-01`; operation별 canonical argv 정의 |
| **Input Type** | `MutationKind`, `tuple[str, ...] argv`, `int timeout_ms`, `str run_id`, `int generation`, `Optional[str] step_id` |
| **Input Validation** | argv에 `--retry-request` 중복 금지; timeout 범위 준수; same intent replay 시 byte-equivalent argv |
| **Output Type** | `OrcaResponse` plus committed `MutationRecord` |
| **Output Validation** | parsed response `ok=true`; expected external ID nonempty; record phase COMMITTED |
| **Exceptions** | known no-effect failure는 `OrcaCommandError`; unknown effect는 INTENT 보존 후 `OrcaTimeoutError`; malformed receipt는 `OrcaProtocolError` |
| **Side Effects** | Orca mutation, operation store phase commits |
| **Detailed Pseudocode** | 1. operation inputs 획득; 2. argv/timeout 검증; 3. unresolved record lookup; 4. 없으면 UUID 생성 후 INTENT write; 5. exact `--retry-request` argv 호출; 6. timeout이면 record 보존 후 raise; 7. response와 external ID 추출; 8. expected contract 검증; 9. APPLIED와 domain commit 후 COMMITTED write; 10. response 반환 |
| **Tests** | `T-B02-06` response-loss replay; `T-B02-07` same request exact argv; `T-B02-08` mismatch rejection; `T-B02-09` each mutation kind uses executor; `T-B02-10` duplicate external object prevention |
| **Rollback** | executor를 fail closed 상태로 두고 unresolved INTENT를 유지한다. Direct non-idempotent retry로 우회하지 않는다. |

### M-B03-01 — Strict message envelope and durable inbox

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-01` |
| **Parent Block** | `B-03` |
| **Name** | Trusted envelope parser와 receipt store |
| **Rationale** | 현재 `Completion`은 sender/message/Run identity를 소실한다. |
| **Objective** | 모든 Delivery row를 exact `MessageEnvelope`로 파싱하고 ACK 전 durable receipt에 기록한다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/dispatcher.py`, `orca_loop/generation.py`, `tests/test_dispatcher.py` |
| **Preconditions** | `M-B01-02`, `M-B02-01` |
| **Input Type** | Orca check result `dict[str, Any]` containing `deliveryId: str` and `messages: list[dict[str, Any]]` |
| **Input Validation** | Delivery ID nonempty; 모든 row object; message ID/type/from/Run nonempty; top-level/payload task와 dispatch ID가 모두 있으면 exact match; payload JSON object only |
| **Output Type** | `DeliveryReceipt` with `acked=False` |
| **Output Validation** | input row count와 envelopes/classifications 길이 동일; raw digest와 reread digest 동일 |
| **Exceptions** | malformed Delivery는 `DispatchProvenanceError`; write failure는 `AtomicWriteError`; row를 조용히 skip하지 않음 |
| **Side Effects** | `control/inbox.jsonl` 또는 equivalent bounded durable receipt append |
| **Detailed Pseudocode** | 1. check result 획득; 2. Delivery/root 검증; 3. prior receipt 조회; 4. 각 raw row 읽기; 5. envelope/payload 변환; 6. external call 없음; 7. envelope tuple 구성; 8. row-count와 ID invariant 검증; 9. unacked receipt atomic append; 10. receipt 반환 |
| **Tests** | `T-B03-01` exact current shape; `T-B03-02` nested shape; `T-B03-03` non-dict row rejection; `T-B03-04` envelope/payload conflict; `T-B03-05` sender/Run required |
| **Rollback** | raw Delivery를 ACK하지 않은 상태로 보존하고 parser 수정 후 replay한다. |

### M-B03-02 — Inbox classification, promotion, and ACK pump

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B03-02` |
| **Parent Block** | `B-03` |
| **Name** | Full-batch processing and idempotent ACK |
| **Rationale** | production callback 없는 foreign message와 multiple signal을 안전하게 처리해야 한다. |
| **Objective** | whole Delivery를 classify/promote한 뒤에만 ACK하고 replay 시 domain transition을 중복 수행하지 않는다. |
| **Target Files** | `orca_loop/dispatcher.py`, `orca_loop/coordinator.py`, `tests/test_dispatcher.py`, `tests/test_coordinator.py` |
| **Preconditions** | `M-B03-01`; expected active/recoverable Dispatch set 제공 |
| **Input Type** | `DeliveryReceipt`, `tuple[DispatchHandle, ...] expected_dispatches` |
| **Input Validation** | expected Dispatch IDs unique; assigned sender handle 일치; worker_done/escalation/status message별 required fields 충족 |
| **Output Type** | `tuple[Completion, ...]` plus receipt with final classifications and `acked=True` |
| **Output Validation** | every row classified; identical terminal signal만 DUPLICATE; conflicting signals는 CONFLICTING; DEFERRED/QUARANTINED durable |
| **Exceptions** | ambiguity는 `DispatchProvenanceError`; ACK failure는 unacked receipt 유지; protocol spoof는 quarantine 후 typed error |
| **Side Effects** | domain event commit, receipt classification update, exact Run Delivery ACK |
| **Detailed Pseudocode** | 1. oldest unacked receipt 획득; 2. expected Dispatch 검증; 3. processed message IDs 읽기; 4. 각 envelope를 sender/task/dispatch/type로 분류; 5. accepted event를 idempotent domain event로 변환; 6. 필요한 release/settlement external call 수행; 7. final classifications 구성; 8. every-row invariant 검증; 9. receipt write 후 ACK하고 acked write; 10. accepted completions 반환 |
| **Tests** | `T-B03-06` mixed batch; `T-B03-07` foreign-only; `T-B03-08` duplicate same digest; `T-B03-09` escalation/done permutations; `T-B03-10` crash before/after ACK; `T-B03-11` ACK replay |
| **Rollback** | ACK pump를 중단하되 durable receipts를 유지한다. 이미 ACK된 receipt는 local replay만 수행한다. |

### M-B04-01 — Bounded and sanitized worker subprocess

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-01` |
| **Parent Block** | `B-04` |
| **Name** | Child environment isolation과 bounded termination |
| **Rationale** | agent child가 wrapper의 Orca authority와 unrelated secrets를 상속하면 조기 lifecycle signal과 credential exposure가 가능하다. |
| **Objective** | provider 실행에 필요한 allowlisted environment만 전달하고 launch, communicate, terminate, drain을 모두 bounded하게 수행한다. |
| **Target Files** | `worker_runner.py`, `orca_loop/orca_client.py`, `orca_loop/testrunner.py`, `tests/test_worker_runner.py`, `tests/test_orca_client.py`, `tests/test_testrunner.py` |
| **Preconditions** | 기존 provider command와 Windows 필수 environment key 목록 확인 |
| **Input Type** | `Sequence[str] command`, `Path cwd`, `Mapping[str, str] parent_env`, `int timeout_ms`, `bytes stdin` |
| **Input Validation** | command nonempty; cwd directory; timeout positive/bounded; allowlist key exact; `ORCA_TERMINAL_HANDLE`, `ORCA_PANE_KEY`, dispatch capability와 task-local secrets 제외 |
| **Output Type** | `ProcessResult(return_code: int, stdout: bytes, stderr: bytes, timed_out: bool)` |
| **Output Validation** | process reaped; output bounded/evidence persisted; elapsed time bounded by run+termination grace |
| **Exceptions** | access denial은 `PermissionObservationError`; timeout/kill failure는 `WorkerRunnerError` 또는 `OrcaTimeoutError` with cause |
| **Side Effects** | subprocess 생성/종료, evidence log write; unrelated environment mutation 없음 |
| **Detailed Pseudocode** | 1. command/env/cwd 획득; 2. input 검증; 3. parent env에서 allowlist 구성; 4. lifecycle/secret keys 제거; 5. Popen external call; 6. bounded communicate; 7. timeout이면 bounded tree kill과 direct kill fallback; 8. process reaped/output invariant 확인; 9. evidence atomic write; 10. result 반환 또는 typed exception |
| **Tests** | `T-B04-01` lifecycle key absence; `T-B04-02` required Windows keys retained; `T-B04-03` taskkill hang/failure fallback; `T-B04-04` bounded drain; `T-B04-05` no secret in runner record |
| **Rollback** | wrapper launch를 중단하고 기존 unsafe environment inheritance로 되돌리지 않는다. |

### M-B04-02 — Strict artifact extraction and claimed provenance

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-02` |
| **Parent Block** | `B-04` |
| **Name** | Exactly-one artifact와 provenance conflict rejection |
| **Rationale** | largest-object heuristic와 silent ID overwrite가 agent 사용 오류를 감춘다. |
| **Objective** | provider output에서 exactly one JSON artifact만 수락하고 present provenance가 expected value와 다르면 실패한다. |
| **Target Files** | `worker_runner.py`, `prompts/planner.md`, `prompts/plan_reviewer.md`, `prompts/implementer.md`, `prompts/code_reviewer.md`, `prompts/cross_confirmer.md`, `tests/test_dispatcher.py`, `tests/test_worker_runner.py` |
| **Preconditions** | `M-B04-01`; role별 parser contract 유지 |
| **Input Type** | `str stdout`, `ExpectedProvenance`, `Role` |
| **Input Validation** | provider envelope는 알려진 shape; candidate JSON object 정확히 1개; size 1..`MAX_ARTIFACT_BYTES`; task/dispatch field는 둘 다 absent 또는 둘 다 present |
| **Output Type** | canonical artifact `str` |
| **Output Validation** | strict JSON object; claimed IDs exact match; wrapper가 conflict를 덮어쓰지 않음; digest deterministic |
| **Exceptions** | zero/multiple candidate, partial provenance, mismatch는 `WorkerRunnerError`; downstream schema error는 `ContractViolationError` |
| **Side Effects** | validated artifact atomic write; prompt wording 변경 |
| **Detailed Pseudocode** | 1. stdout와 expected provenance 획득; 2. size/encoding 검증; 3. provider event rows 읽기; 4. candidate 목록 추출; 5. exactly-one 조건 적용; 6. external call 없음; 7. canonical JSON 구성; 8. present provenance exact match 검증; 9. output atomic write; 10. canonical string 반환 |
| **Tests** | `T-B04-06` Claude/Codex exact envelope; `T-B04-07` multiple object rejection; `T-B04-08` trailing message rejection; `T-B04-09` stale ID rejection; `T-B04-10` prompt uses wrapper-supplied provenance, not unused preamble |
| **Rollback** | ambiguous output을 실패로 유지하며 heuristic acceptance를 복원하지 않는다. |

### M-B04-03 — Artifact-ready handshake and exactly-once settlement

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B04-03` |
| **Parent Block** | `B-04` |
| **Name** | Validation-before-worker_done protocol |
| **Rationale** | `worker_done succeeded`는 즉시 external Task를 settle하므로 coordinator validation보다 먼저 보내면 안 된다. |
| **Objective** | wrapper가 artifact-ready status를 보낸 뒤 coordinator validation 결과를 durable binding으로 받아 정확히 한 번 final worker_done을 전송한다. |
| **Target Files** | `worker_runner.py`, `orca_loop/dispatcher.py`, `orca_loop/coordinator.py`, `orca_loop/models.py`, `tests/test_dispatcher.py`, `tests/test_coordinator.py`, `tests/test_worker_runner.py` |
| **Preconditions** | `M-B03-02`, `M-B04-02`; step `binding.json` coordinator-owned |
| **Input Type** | `SettlementRecord`, validated artifact path/digest, `DispatchHandle`, `InputManifest`, repository guard context |
| **Input Validation** | task/dispatch/path/digest exact; legal state transition only; final outcome exactly `succeeded` or `failed` |
| **Output Type** | `SettlementRecord(state=SIGNALLED)` and one accepted worker_done Completion |
| **Output Validation** | success signal only after schema/provenance/scope/guard validation; failure path uses `outcome=failed`; signal count exactly one |
| **Exceptions** | invalid handshake는 `StepBindingError`; validation error는 failed settlement reason으로 보존; send uncertainty는 B-02 exact retry |
| **Side Effects** | artifact-ready status message, `binding.json` settlement update, final worker_done, Orca Task/Dispatch settlement |
| **Detailed Pseudocode** | 1. wrapper artifact와 job binding 획득; 2. identity/digest 검증; 3. binding의 current settlement 읽기; 4. ARTIFACT_READY record/write; 5. status message external call; 6. coordinator inbox가 ready를 받고 artifact/schema/scope/guard 검증; 7. succeeded/failed settlement 구성; 8. settlement invariant 검증; 9. coordinator binding write 후 wrapper exact-retry worker_done send; 10. SIGNALLED write와 completion 반환 |
| **Tests** | `T-B04-11` success ordering; `T-B04-12` malformed artifact failed outcome; `T-B04-13` scope violation failed outcome; `T-B04-14` send response loss; `T-B04-15` duplicate ready/done; `T-B04-16` wrapper crash after validation |
| **Rollback** | active settlement을 fail closed하고 worker를 fence한다. Validation-before-settlement 순서는 유지한다. |

### M-B05-01 — Terminal identity and authoritative worker inspection

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-01` |
| **Parent Block** | `B-05` |
| **Name** | Terminal incarnation 및 Dispatch status verification |
| **Rationale** | `terminal show` exit 0만으로는 wrong worktree, orphan, disconnect, reincarnation을 구분할 수 없다. |
| **Objective** | recorded terminal을 exact metadata로 검증하고 active Dispatch는 `worker-show`/`dispatch-show`로 확인한다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/session.py`, `run_loop.py`, `tests/test_session.py`, `tests/test_resume.py` |
| **Preconditions** | `M-B01-02`; persisted `TerminalIdentity` 사용 |
| **Input Type** | `TerminalIdentity`, terminal show `dict[str, Any]`, active `DispatchHandle` |
| **Input Validation** | returned handle/incarnation/worktree ID/path exact; `connected is True`; `orphaned is False`; worker response task/dispatch exact |
| **Output Type** | verified `TerminalIdentity` and typed worker state string |
| **Output Validation** | wrong identity never reused; typed stale/not-found만 replacement eligible; timeout/transport failure propagates |
| **Exceptions** | identity mismatch는 `WorkerProvisionError`; transient command error는 `OrcaCommandError`; malformed shape는 `OrcaProtocolError` |
| **Side Effects** | read-only terminal/worker/dispatch queries; replacement 생성 없음 |
| **Detailed Pseudocode** | 1. recorded identity 획득; 2. field invariant 검증; 3. current state/manifest 읽기; 4. terminal show 호출; 5. metadata 비교; 6. active이면 worker-show/dispatch-show 호출; 7. typed inspection result 구성; 8. cross-ID 검증; 9. read-only evidence append; 10. result 반환 또는 typed error |
| **Tests** | `T-B05-01` correct terminal; `T-B05-02` wrong path/handle; `T-B05-03` reincarnation; `T-B05-04` disconnected/orphaned; `T-B05-05` transient error no replacement |
| **Rollback** | replacement를 만들지 않고 resume을 `BLOCKED`한다. |

### M-B05-02 — Resume decision, fencing, retry, and resource accounting

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B05-02` |
| **Parent Block** | `B-05` |
| **Name** | Authoritative resume state machine |
| **Rationale** | live old worker와 replacement의 동시 write를 방지하고 settled terminal을 account해야 한다. |
| **Objective** | worker state와 durable inbox/settlement에 따라 deterministic `ResumeOutcome`을 결정하고 replacement 전 fencing을 증명한다. |
| **Target Files** | `orca_loop/coordinator.py`, `orca_loop/session.py`, `run_loop.py`, `tests/test_resume.py`, `tests/test_coordinator.py` |
| **Preconditions** | `M-B03-02`, `M-B04-03`, `M-B05-01` |
| **Input Type** | `CoordinatorState`, `ConsensusLedger`, `Optional[ActiveStep]`, typed worker state, unacked receipts, `SettlementRecord` |
| **Input Validation** | state/ledger generation 동일; active Task/Dispatch complete; retry는 prior Dispatch ID 필요; outcome_unknown에서 unfenced replacement 금지 |
| **Output Type** | `ResumeOutcome` and optional replacement `DispatchHandle` |
| **Output Validation** | ready는 adopt; settled는 recover; failed/stopped는 `retry-of`; unknown은 stop success 후 retry 또는 abandon+BLOCKED; terminal은 release/reuse decision 기록 |
| **Exceptions** | irreconcilable state는 `ResumeAmbiguityError`; stop uncertainty는 `ResumeBlockedError`; mutation failure는 `OrcaCommandError` |
| **Side Effects** | Run bind, inbox replay, worker stop/abandon/release, retry dispatch, recovery state/event commit |
| **Detailed Pseudocode** | 1. committed state와 receipts 획득; 2. provenance 검증; 3. active worker inspection 읽기; 4. state별 outcome 계산; 5. ready면 wait, settled면 recover, failed/stopped면 retry 준비; 6. unknown이면 stop/abandon external call; 7. outcome과 next binding 구성; 8. no-concurrent-editor invariant 검증; 9. decision commit 후 replacement/release 실행; 10. outcome 반환 또는 block |
| **Tests** | `T-B05-06` ready adoption; `T-B05-07` settled recovery; `T-B05-08` failed retry-of; `T-B05-09` unknown stop failure; `T-B05-10` late completion quarantine; `T-B05-11` release before next wait |
| **Rollback** | 자동 retry를 비활성화하고 run을 `USER_DECISION_REQUIRED`로 유지한다. |

### M-B06-01 — Durable gate authorization binding

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-01` |
| **Parent Block** | `B-06` |
| **Name** | Gate options, context, and Run-scoped recovery |
| **Rationale** | 생성 시 option과 blocked context를 저장해야 recovered gate도 동일한 권한 계약을 갖는다. |
| **Objective** | `GateBinding`이 exact Run/task/gate/report/options/blocked scope를 round-trip하고 recovery에서 모두 대조된다. |
| **Target Files** | `orca_loop/models.py`, `orca_loop/escalation.py`, `orca_loop/generation.py`, `run_loop.py`, `tests/test_escalation.py`, `tests/test_resume.py` |
| **Preconditions** | `M-B01-02`, `M-B02-02` |
| **Input Type** | `str orchestration_run_id`, `str task_id`, `DecisionReport`, `GateKind`, `tuple[HumanDecisionKind, ...] allowed_decisions`, `LoopState blocked_state` |
| **Input Validation** | IDs nonempty; allowed decisions nonempty/unique; report digest valid; options consistent with gate kind; affected scope canonical |
| **Output Type** | extended `GateBinding` |
| **Output Validation** | create/recovered gate Run/task/report/options exact; binding committed before wait |
| **Exceptions** | mismatch/duplicate gate는 `GateProtocolError`; mutation uncertainty는 B-02 recovery |
| **Side Effects** | exact Run gate-create/list call, binding state commit |
| **Detailed Pseudocode** | 1. gate context 획득; 2. IDs/options 검증; 3. existing binding/operation 읽기; 4. 없으면 authorization intent commit; 5. exact Run gate-create external call; 6. 있으면 exact Run/task gate-list recovery; 7. binding 구성; 8. returned fields/options 검증; 9. state atomic commit; 10. binding 반환 |
| **Tests** | `T-B06-01` binding round-trip; `T-B06-02` exact Run/task; `T-B06-03` recovered option mismatch; `T-B06-04` duplicate gates; `T-B06-05` response-loss recovery |
| **Rollback** | unresolved gate를 유지하고 새 gate 생성/decision routing을 중단한다. |

### M-B06-02 — Decision membership and scope authorization

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B06-02` |
| **Parent Block** | `B-06` |
| **Name** | HumanDecision authorization before routing |
| **Rationale** | strict JSON parsing만으로 unlisted decision이나 broadened scope를 막을 수 없다. |
| **Objective** | parsed resolution을 `GateBinding` authorization contract와 비교한 뒤에만 state transition한다. |
| **Target Files** | `orca_loop/contracts.py`, `orca_loop/escalation.py`, `run_loop.py`, `tests/test_escalation.py`, `tests/test_coordinator.py` |
| **Preconditions** | `M-B06-01` |
| **Input Type** | `GateBinding`, `HumanDecision`, current `CoordinatorState` |
| **Input Validation** | decision membership; report digest exact; affected finding/acceptance IDs는 authorized set subset; current blocked state exact |
| **Output Type** | authorized `TransitionSignal` |
| **Output Validation** | invalid decision에서 gate/context 불변; valid decision만 clear gate/context; E-03 obligation은 merge approval 전 유지 |
| **Exceptions** | authorization failure는 `GateProtocolError`; invalid state routing은 `OrcaLoopError` |
| **Side Effects** | authorized transition commit; invalid path는 none |
| **Detailed Pseudocode** | 1. binding/resolution/state 획득; 2. strict schema 검증; 3. current gate/context 읽기; 4. decision/options membership 비교; 5. digest/scope/subset 비교; 6. external call 없음; 7. TransitionSignal 구성; 8. obligation invariant 검증; 9. valid transition과 clear flags atomic commit; 10. signal 반환 또는 exception |
| **Tests** | `T-B06-06` E-03 revise_code rejection; `T-B06-07` destructive option enforcement; `T-B06-08` stale digest; `T-B06-09` broadened scope; `T-B06-10` valid merge/reject/revision routes |
| **Rollback** | gate를 pending 상태로 유지하고 authorization을 완화하지 않는다. |

### M-B07-01 — Manifest identity and control-boundary validation

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B07-01` |
| **Parent Block** | `B-07` |
| **Name** | Requested run과 persisted manifest cross-check |
| **Rationale** | copied manifest가 다른 run/worktree/terminal을 실행하는 것을 차단해야 한다. |
| **Objective** | resume/status가 requested run ID, harness root, control directory, request copy, worktree identity를 모두 검증한다. |
| **Target Files** | `orca_loop/runspec.py`, `run_loop.py`, `tests/test_runspec.py`, `tests/test_cli_commands.py` |
| **Preconditions** | `M-B01-01` |
| **Input Type** | `str requested_run_id`, `Path harness_root`, `Path control_dir`, `RunManifest` |
| **Input Validation** | run ID regex; manifest run ID exact; harness/control/request-copy resolved boundary; worktree Git top-level; digests exact |
| **Output Type** | verified `RunManifest` or `tuple[str, ...] problems` for read-only status |
| **Output Validation** | resume에는 zero problems; status는 모든 problems 표시; foreign lock/worktree를 사용하지 않음 |
| **Exceptions** | resume mismatch는 `ManifestError`; malformed manifest는 status에서 caught `BLOCKED` envelope |
| **Side Effects** | read-only verification; successful migration 시 manifest atomic rewrite only |
| **Detailed Pseudocode** | 1. requested paths 획득; 2. run ID/path 검증; 3. manifest read; 4. persisted identities 읽기; 5. resolved path/digest 비교; 6. external call 없음; 7. problems 또는 verified manifest 구성; 8. boundary invariant 검증; 9. optional migration write; 10. return 또는 exception |
| **Tests** | `T-B07-01` copied manifest; `T-B07-02` run ID mismatch; `T-B07-03` harness/control escape; `T-B07-04` request-copy escape; `T-B07-05` malformed status handling |
| **Rollback** | resume을 `BLOCKED`하고 persisted files를 변경하지 않는다. |

### M-B07-02 — Side-effect ordering and provisioning transaction

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B07-02` |
| **Parent Block** | `B-07` |
| **Name** | Parse/preflight/lock before terminal and Run mutation |
| **Rationale** | invalid args, dirty worktree, lock loser, partial worker provisioning이 resource를 남기면 안 된다. |
| **Objective** | actual start는 structural parse, preflight, identity verification, worktree lock, durable intent 순으로 완료한 뒤에만 Run/terminal mutation을 수행한다. |
| **Target Files** | `run_loop.py`, `orca_loop/config.py`, `orca_loop/dispatcher.py`, `orca_loop/session.py`, `tests/test_cli.py`, `tests/test_dispatcher.py` |
| **Preconditions** | `M-B01-02`, `M-B02-02`, `M-B07-01` |
| **Input Type** | `Sequence[str] argv`, `PreflightResult`, `RunLock`, `Mapping[WorkerKey, TerminalIdentity]` |
| **Input Validation** | full parser success; Git clean; current coordinator attested; lock ownership exact; required four worker keys |
| **Output Type** | initialized `GenerationController` and `WorkerPool` |
| **Output Validation** | any pre-mutation failure creates zero resources; Nth provisioning failure has durable record and typed report; pool handles unique/correct worktree |
| **Exceptions** | config/preflight는 `BLOCKED`; `WorkerProvisionError`는 caught `FAIL`; uncertain terminal mutation은 durable operation record 유지 |
| **Side Effects** | lock acquisition, Run create, worker terminal create/show, state/manifest writes in ordered transaction |
| **Detailed Pseudocode** | 1. argv 획득; 2. structural parse; 3. read-only preflight와 Git validation; 4. coordinator identity와 lock 획득; 5. run mutation intent commit; 6. Run/worker external mutations 순차 실행; 7. pool/controller 구성; 8. uniqueness/worktree invariant 검증; 9. manifest/state/provision receipts commit; 10. initialized values 반환 또는 typed failure report |
| **Tests** | `T-B07-06` invalid config zero terminals; `T-B07-07` dirty worktree zero terminals; `T-B07-08` lock contention zero terminals; `T-B07-09` Nth terminal failure; `T-B07-10` caught WorkerProvisionError |
| **Rollback** | owned and proven disposable resource만 release 대상으로 기록하고 자동 broad cleanup은 하지 않는다. |

### M-B07-03 — Truthful CLI help, status, and health reporting

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B07-03` |
| **Parent Block** | `B-07` |
| **Name** | Subcommand help와 resumability truth model |
| **Rationale** | help가 required argument error로 끝나고 status가 blockers를 PASS로 감추면 operator가 잘못 행동한다. |
| **Objective** | `doctor/start/resume/status --help`가 exit 0이며 status가 manifest/input/lock/state/Run blockers를 반영한다. |
| **Target Files** | `run_loop.py`, `orca_loop/reporting.py`, `tests/test_cli_commands.py`, `tests/test_reporting.py` |
| **Preconditions** | `M-B07-01`, `M-B07-02` |
| **Input Type** | CLI argv and optional committed state/manifest/lock/Run inspection results |
| **Input Validation** | help는 side-effect-free; status selector valid; lock only when verified worktree exists; lock run ID exact |
| **Output Type** | exit code `int` and JSON `RunHealth` envelope for status |
| **Output Validation** | blockers imply `resumable=False`; terminal READY may be non-resumable; malformed manifest caught; no `Path("")` lock lookup |
| **Exceptions** | inspection error는 structured `BLOCKED`; unexpected internal error는 `FAIL`; help는 exception 없음 |
| **Side Effects** | help/status none; report render only for actual run failure |
| **Detailed Pseudocode** | 1. argv 획득; 2. subcommand/help 우선 parse; 3. help면 render/exit 0; 4. status면 state/manifest/input/lock/Run read; 5. read-only external inspection 수행; 6. blockers/warnings 계산; 7. RunHealth 구성; 8. status/resumable invariant 검증; 9. JSON 출력; 10. exact exit code 반환 |
| **Tests** | `T-B07-11` every help exit 0; `T-B07-12` missing/malformed manifest; `T-B07-13` input drift; `T-B07-14` foreign/missing lock; `T-B07-15` terminal states resumability |
| **Rollback** | status를 conservative `BLOCKED`로 유지한다. False PASS로 되돌리지 않는다. |

### M-B08-01 — Permission worker status propagation

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-01` |
| **Parent Block** | `B-08` |
| **Name** | Evidence와 worker status 결합 |
| **Rationale** | truthy evidence만으로 FAIL worker가 PASS check를 만들 수 있다. |
| **Objective** | 관련 worker가 모두 PASS이고 evidence predicate도 true일 때만 `PermissionCheck.PASS`를 생성한다. |
| **Target Files** | `permission_spike.py`, `tests/test_permission_feasibility.py` |
| **Preconditions** | 기존 `ValidationStatus` four-state contract 유지 |
| **Input Type** | role별 worker result exact dict: `role: str`, `status: str`, `read_value: Optional[str]`, four booleans/lists as existing schema |
| **Input Validation** | role exact; status valid enum; evidence/runtime IDs string lists; boolean fields exact bool |
| **Output Type** | `PermissionFeasibilityReport` |
| **Output Validation** | worker FAIL은 related check FAIL; BLOCKED 우선순위 보존; NOT_RUN은 PASS 불가; report status는 checks의 deterministic aggregate |
| **Exceptions** | schema mismatch는 `PermissionSpikeError`; missing file은 existing BLOCKED behavior 유지 |
| **Side Effects** | canonical permission report atomic write |
| **Detailed Pseudocode** | 1. worker results 획득; 2. schema/status 검증; 3. relevant role sets 읽기; 4. 각 check evidence predicate 계산; 5. external call 없음; 6. role status와 predicate 결합; 7. PermissionCheck tuple 구성; 8. four-state invariant 검증; 9. report/digest atomic write; 10. report 반환 |
| **Tests** | `T-B08-01` all PASS; `T-B08-02` FAIL with truthy evidence; `T-B08-03` BLOCKED; `T-B08-04` NOT_RUN; `T-B08-05` multi-worker precedence |
| **Rollback** | report 생성을 `BLOCKED`하고 false PASS 로직을 복원하지 않는다. |

### M-B08-02 — Filesystem-aware test mutation guard

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-02` |
| **Parent Block** | `B-08` |
| **Name** | Ignored output 포함 test side-effect detection |
| **Rationale** | Git snapshot은 ignored files와 write-restore mutation을 보지 못한다. |
| **Objective** | test policy boundary에서 `.git`을 제외한 filesystem changes를 탐지하고 allowed output만 허용한다. |
| **Target Files** | `orca_loop/snapshot.py`, `orca_loop/testrunner.py`, `tests/test_snapshot.py`, `tests/test_testrunner.py` |
| **Preconditions** | existing Git snapshot/digest behavior 유지; no external dependency |
| **Input Type** | `Path worktree`, `tuple[str, ...] allowed_output_paths`, before/after filesystem metadata and content digests |
| **Input Validation** | worktree Git top-level; allowed paths normalized relative paths; symlink/path escape 거부; `.git` always excluded |
| **Output Type** | `tuple[TestPolicyViolation, ...]` and `TestGateResult` |
| **Output Validation** | ignored disallowed creation detected; allowed outputs accepted; tracked/untracked behavior preserved; transient write observation capability가 없으면 그 한계를 명시 |
| **Exceptions** | unreadable file/path race는 policy violation 또는 typed `SnapshotError`; silent ignore 금지 |
| **Side Effects** | before/after filesystem scan, approved test command execution |
| **Detailed Pseudocode** | 1. worktree/policy 획득; 2. paths/boundary 검증; 3. before Git+filesystem state 읽기; 4. command validation; 5. sanitized external test call; 6. after state 읽기; 7. created/modified/deleted entries 계산; 8. allowed-output invariant 검증; 9. violations/result 구성; 10. TestGateResult 반환 |
| **Tests** | `T-B08-06` ignored file creation; `T-B08-07` allowed ignored output; `T-B08-08` symlink escape; `T-B08-09` deletion/modification; `T-B08-10` existing snapshot regression |
| **Rollback** | automated test gate를 `POLICY_VIOLATION` 또는 `NOT RUN`으로 보수 처리한다. |

### M-B08-03 — Shared bounded command and doctor error boundary

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B08-03` |
| **Parent Block** | `B-08` |
| **Name** | Orca/test command timeout hardening과 doctor defect 수정 |
| **Rationale** | `taskkill`과 post-kill drain이 무한 대기할 수 있고 `_doctor_report`가 undefined `arguments`를 참조한다. |
| **Objective** | external command termination이 bounded하고 doctor가 local inspection failure를 structured BLOCKED로 반환한다. |
| **Target Files** | `orca_loop/orca_client.py`, `orca_loop/testrunner.py`, `worker_runner.py`, `run_loop.py`, `tests/test_orca_client.py`, `tests/test_testrunner.py`, `tests/test_cli_commands.py` |
| **Preconditions** | `M-B04-01` worker-specific process behavior PASS |
| **Input Type** | `subprocess.Popen[bytes]`, `int terminate_timeout_ms`, optional caught `OrcaCommandError`/`PreflightError` |
| **Input Validation** | timeout positive and bounded; process object valid; doctor has no run arguments or permission-marker side effect |
| **Output Type** | bounded termination result or doctor `dict[str, Any]` |
| **Output Validation** | no unbounded `communicate()` after timeout; doctor return type always dict; no undefined variable path |
| **Exceptions** | termination failure preserves original timeout cause; doctor converts expected errors to `status=BLOCKED` and does not raise |
| **Side Effects** | process kill/drain; doctor read-only status/catalog/environment inspection |
| **Detailed Pseudocode** | 1. process/error input 획득; 2. timeout/type 검증; 3. process poll/state 읽기; 4. kill strategy 선택; 5. bounded taskkill/direct kill/drain external calls; 6. failure evidence 수집; 7. result 또는 doctor envelope 구성; 8. return-type/time invariant 검증; 9. read-only diagnostic output; 10. return 또는 original typed exception |
| **Tests** | `T-B08-11` taskkill timeout; `T-B08-12` direct kill fallback; `T-B08-13` pipe remains open; `T-B08-14` doctor Orca failure; `T-B08-15` doctor always dict |
| **Rollback** | affected command을 `BLOCKED`/`FAIL`로 종료하고 unbounded wait를 복원하지 않는다. |

### M-B09-01 — Codex and Claude skill workflow rewrite

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B09-01` |
| **Parent Block** | `B-09` |
| **Name** | Single harness-owned lifecycle contract |
| **Rationale** | 두 skill의 manual/automatic Run ownership 지시가 상충한다. |
| **Objective** | 두 skill이 동일한 `doctor -> intake -> runner terminal -> dry-run -> confirmation -> start -> status/resume` 흐름을 지시하고 lifecycle internals는 harness에 위임한다. |
| **Target Files** | `C:\Users\mhj\.codex\skills\orca-loop\SKILL.md`, `C:\Users\mhj\.claude\skills\orca-loop\SKILL.md` |
| **Preconditions** | `M-B01-02`~`M-B08-03` public behavior와 help 검증 완료 |
| **Input Type** | final CLI help text, exit/status contract, worker catalog/role mapping, evidence path contract |
| **Input Validation** | command examples 실제 parser와 일치; four workers exact; task prompt verbatim; model/effort confirmation; no manual Run/mailbox/Gate mutation 지시 |
| **Output Type** | two valid UTF-8 `SKILL.md` files with YAML frontmatter `name` and `description` only |
| **Output Validation** | body imperative/concise, under 500 lines, ownership rules 동일, PASS/FAIL/BLOCKED/NOT RUN reporting explicit |
| **Exceptions** | code/skill contract mismatch면 Phase 4 중단; skill validation error는 수정 후 재검증 |
| **Side Effects** | existing Codex/Claude skill files 수정 |
| **Detailed Pseudocode** | 1. final CLI/evidence contracts 획득; 2. current skills parse; 3. stale/conflicting instructions 식별; 4. shared workflow outline 구성; 5. external mutation 없음; 6. platform-specific intake/launch text 작성; 7. two SKILL documents 구성; 8. frontmatter/command/ownership invariant 검증; 9. apply patch; 10. updated paths 반환 |
| **Tests** | `T-B09-01` frontmatter exact; `T-B09-02` four worker mapping; `T-B09-03` command examples; `T-B09-04` no manual lifecycle ownership; `T-B09-05` line count |
| **Rollback** | pre-edit content를 diff로 보존하여 inverse patch하고 unrelated skill은 건드리지 않는다. |

### M-B09-02 — Codex metadata validation and forward-test

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B09-02` |
| **Parent Block** | `B-09` |
| **Name** | `agents/openai.yaml`, `quick_validate.py`, realistic invocation validation |
| **Rationale** | SKILL.md만 맞고 UI metadata나 trigger behavior가 stale하면 실제 사용에서 다시 잘못된 workflow를 시작한다. |
| **Objective** | metadata를 skill 내용과 일치시키고 non-mutating representative prompts로 intake/start/resume behavior를 검증한다. |
| **Target Files** | `C:\Users\mhj\.codex\skills\orca-loop\agents\openai.yaml`, skill-creator validation scripts; repository file 변경 없음 |
| **Preconditions** | `M-B09-01`; `openai_yaml.md` 규칙 확인 |
| **Input Type** | updated Codex SKILL.md path, UI values `display_name: str`, `short_description: str`, `default_prompt: str` |
| **Input Validation** | quoted strings; short description 25..64 chars; default prompt에 `$orca-loop` explicit; unsupported metadata 없음 |
| **Output Type** | valid `openai.yaml` and forward-test result matrix |
| **Output Validation** | `quick_validate.py` PASS; metadata/skill semantic match; forward-tests가 files/terminals/Run을 생성하지 않고 blocking intake를 올바르게 수행 |
| **Exceptions** | validation failure는 Phase 4 incomplete; forward-test mutation risk가 생기면 NOT RUN으로 보고 |
| **Side Effects** | `openai.yaml` 수정; approved non-mutating forward-test only |
| **Detailed Pseudocode** | 1. updated skill 읽기; 2. metadata constraints 검증; 3. current yaml 읽기; 4. expected UI values 계산; 5. generator script external call; 6. quick_validate external call; 7. representative prompt/result 구성; 8. no-mutation/trigger invariant 검증; 9. validation evidence 기록; 10. result matrix 반환 |
| **Tests** | `T-B09-06` quick_validate; `T-B09-07` metadata constraints; `T-B09-08` new-run intake; `T-B09-09` status request; `T-B09-10` resume request; `T-B09-11` missing inputs block |
| **Rollback** | metadata만 inverse patch하며 validated SKILL.md를 유지할 수 있다. |

### M-B10-01 — Unit and fault-injection regression matrix

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B10-01` |
| **Parent Block** | `B-10` |
| **Name** | Communication crash-boundary regression suite |
| **Rationale** | current mock suite가 happy path만 검증하여 protocol bugs를 허용했다. |
| **Objective** | `V-B01-*`~`V-B09-*` negative paths를 deterministic unit/fault tests로 고정한다. |
| **Target Files** | existing `tests/test_*.py`; 필요 시 prefix rule을 지킨 새 test file만 추가 |
| **Preconditions** | `M-B01-01`~`M-B09-02` 구현 완료 |
| **Input Type** | fake Orca responses, injected filesystem/process failures, deterministic clocks/request IDs |
| **Input Validation** | each fixture owns temp paths; no live target mutation; fault point exact; expected side effects declared |
| **Output Type** | `unittest` result with test IDs `T-B01-*` through `T-B09-*` |
| **Output Validation** | no duplicate Task/Dispatch/Gate; no message loss; no false PASS; failure classifications exact |
| **Exceptions** | caused regression은 Phase 4에서 수정; pre-existing failure는 evidence 분리; flaky timing test 금지 |
| **Side Effects** | temporary files/process mocks only |
| **Detailed Pseudocode** | 1. block validation matrix 획득; 2. fixture isolation 검증; 3. initial durable state 구성; 4. fault point 주입; 5. subject function 호출; 6. expected exception/result capture; 7. state/external-call assertions 구성; 8. no-extra-effect invariant 검증; 9. temp fixture 정리; 10. test result 반환 |
| **Tests** | `T-B10-01` all targeted modules; `T-B10-02` crash matrix; `T-B10-03` response-loss matrix; `T-B10-04` message permutation matrix; `T-B10-05` schema migration matrix |
| **Rollback** | failing implementation block을 inverse patch하되 regression tests는 유지한다. |

### M-B10-02 — Disposable live protocol and final verification

| Field | Definition |
| --- | --- |
| **Micro Block ID** | `M-B10-02` |
| **Parent Block** | `B-10` |
| **Name** | No-agent live Orca contract and release evidence |
| **Rationale** | unit mock만으로 terminal attestation과 current CLI grammar를 증명할 수 없다. |
| **Objective** | disposable fixture/Run에서 provider token 없이 Run/Task/Dispatch/status/check/ACK/Gate/recovery contract를 검증하고 전체 regression을 완료한다. |
| **Target Files** | `tests/test_e2e_orca.py`, `run_loop.py`, skill paths, generated test-owned temporary resources |
| **Preconditions** | `M-B10-01` PASS; active Orca runtime; explicitly marked disposable fixture; current terminal attested |
| **Input Type** | `E2EConfig`, environment flags, disposable worktree/terminal selectors |
| **Input Validation** | fixture marker required; Run/task titles test prefix; no provider launch; resource ownership receipt required |
| **Output Type** | validation evidence matrix with PASS/FAIL/BLOCKED/NOT RUN per command |
| **Output Validation** | current Run exact; foreign identity rejected; full Delivery processed before ACK; gate options enforced; no residual active Dispatch; full unit suite PASS |
| **Exceptions** | runtime unavailable는 BLOCKED; cleanup ownership 불확실하면 destructive cleanup 금지 및 residual evidence 보고 |
| **Side Effects** | disposable Orca Run/Task/Dispatch/Gate/terminal records; owned resources만 release |
| **Detailed Pseudocode** | 1. runtime/fixture/config 획득; 2. disposable ownership 검증; 3. unit PASS 확인; 4. Run/task/terminal 준비; 5. no-agent protocol external calls; 6. Delivery/gate/recovery assertions; 7. evidence matrix 구성; 8. no-residual-active invariant 검증; 9. owned resource release와 full suite/doctor/help/dry-run/diff-check 실행; 10. final report 반환 |
| **Tests** | `T-B10-06` live Run bind; `T-B10-07` foreign fencing; `T-B10-08` no-agent Dispatch settlement; `T-B10-09` full Delivery ACK; `T-B10-10` gate authorization; `T-B10-11` full suite and skill validation |
| **Rollback** | owned disposable resources만 release하고 code/skill은 failing Micro Block 단위 inverse patch한다. |

---

## 4. Implementation Order

```text
M-B01-01 -> M-B01-02
M-B02-01 -> M-B02-02
M-B03-01 -> M-B03-02
M-B04-01 -> M-B04-02 -> M-B04-03
M-B05-01 -> M-B05-02
M-B06-01 -> M-B06-02
M-B07-01 -> M-B07-02 -> M-B07-03
M-B08-01 -> M-B08-02 -> M-B08-03
M-B09-01 -> M-B09-02
M-B10-01 -> M-B10-02
```

Cross-block dependency는 Phase 2 dependency graph를 그대로 따른다. 실제 Phase 4에서는
`B-01 -> B-02 -> B-03 -> B-04 -> B-05`, 이후 `B-06`, `B-07`, `B-08`,
`B-09`, `B-10` 순으로 진행한다.

---

## 5. Validation and Risks

- **Validation:** 현재 source의 exact dataclass/function/test 위치, Orca `1.4.180`
  capability와 `terminal show` field, current help grammar를 Micro Block input contract에
  반영했다.
- **Risks:** `artifact-ready` handshake가 가장 큰 변경면이다. Phase 4에서 이 handshake의
  targeted tests가 PASS하기 전에는 기존 `worker_done` path를 제거하지 않는다.
- **Open Questions:** 없음. Legacy active run은 persisted Orca Run ID가 없으므로 새 Run을
  추측하지 않고 `BLOCKED`하는 것으로 확정한다.

---

## 6. Approval or Completion Status

## Approval

- [ ] Micro Blocking approved
- [ ] Revision requested
- [ ] Permission granted to begin implementation

**Next phase after explicit approval:** Phase 4 — Code Implementation
