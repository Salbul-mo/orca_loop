# Task Report: Orca Multi-Agent Staged Development Harness

**Current Phase:** 4. Code Implementation
**Status:** Implemented / Unit and Fake Integration Validation Passed / Live Full E2E NOT RUN

---

### 1. Context and Objective

- **Goal:** Claude plan creation and revision, Codex plan review and
  implementation, Claude code review, Codex cross-confirmation, deterministic
  consensus evaluation, test gate, and user decision gate를 하나의 Orca
  coordinator loop로 구현한다.
- **Scope:** 승인된 Phase 1 Revision 7, Phase 2 Revision 7, Phase 3 Revision 3.
- **Consensus limits:** plan `5`, code `5`.
- **Early stop:** 동일한 unresolved signature가 material progress 없이 두
  유효 round에서 반복되면 `E-05`로 즉시 사용자에게 escalation한다.
- **Excluded operations:** automatic commit, merge, push, reset, source cleanup,
  and runtime-global orchestration reset.

### 2. Deliverables

#### Runtime

- `run_loop.py`
  - typed CLI and read-only preflight
  - exclusive per-worktree lock
  - new run and resume entry points
  - exact exit code mapping
  - worker/evaluate/test/human-gate state execution
- `worker_runner.py`
  - one-shot Claude/Codex process execution
  - stdout artifact extraction
  - digest-bound outbox write
  - provenance-bound `worker_done` or escalation message
- `permission_spike.py`
  - strategy selection evidence and canonical feasibility report

#### Coordinator package

- `models.py`, `contracts.py`: frozen typed contracts, strict JSON parsing,
  provenance and approval-obligation checks.
- `snapshot.py`, `guards.py`, `readonly.py`: canonical Git snapshot, frozen
  review, source scope guard, Strategy D read-only repository mirror.
- `ledger.py`, `machine.py`: per-finding dual approval, unresolved dependency
  closure, five-round limits, second identical no-progress escalation, pure
  state transition.
- `transport.py`, `dispatcher.py`, `generation.py`: staged input digest,
  task/dispatch binding, bounded completion wait, atomic state/ledger commit.
- `testrunner.py`: exact allowlist, sanitized environment, timeout tree
  termination, `PASS|FAIL|NOT_RUN|POLICY_VIOLATION`.
- `escalation.py`: twelve-section `user-decision.md`, final/escalation/
  destructive gate binding, stale decision rejection.
- `config.py`, `locking.py`, `bootstrap.py`, `profiles.py`, `roles.py`,
  `workspace.py`, `orca_client.py`: preflight, launch policy, repository and
  run lifecycle foundations.

#### Role contracts

- `prompts/planner.md`
- `prompts/plan_reviewer.md`
- `prompts/implementer.md`
- `prompts/code_reviewer.md`
- `prompts/cross_confirmer.md`

Each template contains the exact output field contract. Planner and code
reviewer use distinct Claude sessions.

#### Required implementation consistency correction

Phase 1 required Claude planner revisions to decide every delivered finding,
but the approved Phase 3 `PlanDocument` omitted the fields that can carry those
decisions. The implementation restored:

```text
PlanDocument.reviewed_finding_ids
PlanDocument.finding_decisions
```

Without these fields, plan consensus could only contain the Codex side and the
coordinator would have to infer Claude approval, which is prohibited.

`CoordinatorState` also persists gate binding, human decision, destructive
approval, blocked source state, and pending escalation provenance so a decision
is not reconstructed from free-form text.

### 3. Validation and Risks

| Command or gate | Status | Result |
|---|---|---|
| Strategy D live permission spike | **PASS** | Canonical report `sha256:784a6d27d2dd10c1b1cf2d966a62a384e3619439cb63e028fbc5669649470d43`; `V-PERM-01..05` PASS |
| `py -3 -m compileall -q orca_loop tests permission_spike.py worker_runner.py run_loop.py` | **PASS** | All Python modules compiled |
| `py -3 -m unittest discover -s tests -v` | **PASS** | 70 tests, 0 failures, 0 errors, 0 skips |
| Phase 3 traceability | **PASS** | 39 unique Micro Blocks, no undefined dependency, no cycle, all declared layers valid |
| `orca status --json` | **PASS** | Runtime and graph ready, Orca `1.4.159` |
| `py -3 run_loop.py --help` | **PASS** | Typed CLI and fixed five-round options rendered |
| Current harness dry-run | **BLOCKED** | Current repository has no Git `HEAD`; preflight correctly stopped before Orca mutation |
| Opt-in live full loop E2E | **NOT RUN** | Requires a clean committed disposable feature worktree, explicit coordinator handle, and agent execution |
| `V-PERM-06..07` live lifecycle regression | **NOT RUN** | Unit/fake guard coverage exists; final live confirmation requires opt-in fixture execution |
| Automatic commit/merge/push | **NOT RUN** | Prohibited by design |

#### Remaining risks

- Live Claude/Codex output quality and installed CLI authentication can only be
  validated by the opt-in E2E run.
- A crash-resume decision table is implemented and unit-tested for five durable
  stages. A real crash injected into an active Orca dispatch remains part of
  the opt-in E2E gate.
- Strategy D creates immutable generation-specific review mirrors. They are
  retained as run evidence and are not automatically deleted.
- Running without `--test-policy` intentionally produces `NOT_RUN`; it does not
  imply test success.

#### Launch example

The target must be a clean Git worktree with a valid `HEAD`.

```powershell
$env:ORCA_CLI_COMMAND = 'C:\Users\mhj\AppData\Local\Programs\orca\resources\bin\orca.exe'

py -3 run_loop.py `
  --run-id feature-login-timeout-01 `
  --request 'C:\path\outside-target\request.md' `
  --worktree 'C:\path\to\clean-feature-worktree' `
  --coordinator-handle 'term_runtime-issued-handle' `
  --permission-report 'C:\Users\mhj\Desktop\mhj_workspace\orca_harness\runs\20260731-permission-spike-03\control\permission-feasibility.json' `
  --test-policy 'C:\path\outside-target\test-policy.json'
```

Resume uses the identical run ID, worktree, coordinator handle, permission
report, and policy plus `--resume`.

### 4. Approval Status

- [x] Phase 4 implementation permission was granted
- [x] Unit and fake integration validation passed
- [ ] Opt-in live full E2E passed
- [ ] User approved final use on a clean feature worktree
