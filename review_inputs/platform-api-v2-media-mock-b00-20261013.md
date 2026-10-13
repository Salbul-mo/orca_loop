# ORCA implementation request — platform-poc B-00 Media API v2 mock contract

Repository target:
C:\Users\mhj\Desktop\mhj_workspace\company\platform-poc

Current branch:
feat/api-v2-media-mock-b00

Authoritative design:
docs/reference/26_10_12_api_v2_1단계_01_system_design.md
docs/reference/26_10_13_api_v2_1단계_02_고객회신불필요_macro_blocking.md
docs/reference/26_10_13_api_v2_1단계_03_고객회신불필요_micro_blocking.md

Objective:
Complete B-00 only. Make mock/media-server a reliable Phase 1 API v2 test oracle before any Java B-01 work.

Execute the micro blocks in this order:
1. M-B00-02 — rename the streaming authentication contract from mediaToken/mediaTokenIssuedAtWallMs to streamingToken/streamingTokenIssuedAtWallMs everywhere in the v2 mock control responses, generated stream URLs, ZLM/WHEP/WebRTC authentication, logs/comments/README, and smoke assertions. Do not rename playbackId/downloadToken or recording-related rec names.
2. M-B00-03 — implement streaming.server.settings revision semantics:
   - persist a current settings revision in mock state;
   - settings.get returns the current revision;
   - settings.put requires a positive integer revision;
   - newer revision applies and becomes current;
   - equal revision is idempotent and must not produce a second logical mutation;
   - stale revision returns success with applied:false and the current revision, without modifying settings;
   - channel enable/disable must not increment or otherwise change settings revision;
   - validate all rejectable input before mutating settings so an invalid request cannot partially apply.
3. M-B00-04 — make camera-groups.list empty-group visibility configurable by Mock configuration/environment without changing the default behavior unexpectedly. Both modes must be testable:
   - empty groups visible;
   - empty groups hidden.
   Existing non-empty groups and camera counts must remain correct.
4. M-B00-05 — extend smoke.js so the v2 smoke gate explicitly verifies:
   - existing action coverage remains complete;
   - streamingToken is present and mediaToken is absent;
   - valid streamingToken authorizes WHEP/WebRTC and invalid token is rejected;
   - settings.get exposes revision;
   - newer settings revision applies;
   - equal revision is idempotent;
   - stale revision returns applied:false with current revision and does not mutate settings;
   - streaming.channels.setEnabled leaves settings revision unchanged;
   - empty-group visible and hidden behaviors are both exercised deterministically;
   - existing HLS/export/WHEP/WebRTC checks remain passing.

Constraints:
- Scope is mock/media-server and documentation directly needed for B-00 only.
- Do not modify Java integration, run-local-rec.sh, DB schema, AI APIs, playback-sessions/exports naming, recplay-sessions/recexports, ONVIF feature implementation, externalAccess, multi-Media assignment, or hierarchical group path contracts.
- Keep internal REC/recording names when they refer to recording functionality.
- Do not perform broad refactors or dependency changes.
- Do not install dependencies.
- Do not commit, push, reset, clean, restore, stash, or merge.
- Preserve existing API v2 behavior not explicitly changed here.
- Work only from code evidence; do not invent customer-dependent behavior.
- The test command available in the test policy starts the standalone mock, runs the existing smoke suite, and stops only that test process.

Completion evidence:
- implementation artifact lists exact files changed;
- ORCA test gate executes the allowlisted standalone smoke wrapper and passes;
- code review has no blocking findings;
- final worktree diff is limited to B-00 scope.
