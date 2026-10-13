# ORCA planning request — platform-poc AI manager bridge

Role expectation:
- The user explicitly wants ChatGPT to act as Master.
- This run is PLANNING ONLY. Do not implement product code.
- Inspect the actual platform-poc repository and the source plan:
  docs/local/26_10_12_04_ai-manager-bridge-integrated-work-plan.ko.md
- Produce a concrete implementation plan for the monitoring/control app (platform-poc), suitable for handing to implementers.
- After plan review/consensus, finish. Do not proceed to product implementation.

Primary scope:
- platform-poc only.
- AI server changes are not to be implemented here; define their required API contract as an integration deliverable.
- Preserve current product behavior outside this integration scope.
- Avoid opportunistic refactors and new dependencies unless strictly necessary.

Architectural invariant — MUST NOT be weakened:
1. The primary integration boundary is Monitoring/Control App <-> AI Runtime API.
2. sidecar is ONLY a credential/authentication transparent relay.
3. sidecar may:
   - maintain AI identity/login/session credentials,
   - validate/authorize incoming bearer credentials,
   - inject required credentials for outbound relay,
   - transparently relay request/response bytes and minimal bridge health/metrics.
4. sidecar MUST NOT own or interpret:
   - camera/scenario/ROI business semantics,
   - Start/Stop business state,
   - Event business schema/policy,
   - retry/reconciliation policy,
   - command queues or operation tracking,
   - domain response mapping,
   - inference lifecycle state,
   - application-specific API evolution.
5. A future AI business API addition/change must normally require ZERO sidecar business-code changes.
6. If a proposed design adds a new middle API/service layer or business logic to sidecar, reject it unless there is a narrowly justified credential/transport reason.

Required planning packages:
- B1 certificate-signing API retirement / manager-ca AI trust removal.
- B2 dedicated AI HTTP client with certificate+hostname verification disabled, endpoint/token consistency, clip source binding, complete AI-CA dependency removal while preserving REC/Media trust behavior.
- B3 per-AI audience token schema, issuance, introspection, lifecycle, API rejection behavior.
- B4 remove AI WSS wiring while preserving REC behavior.
- B5 Prometheus deployment and per-AI scrape configuration generation.
- B6 status/statistics query layer and UI behavior.
- B7 deployment/config/docs cleanup.
- B8 tests, DB integration tests, fake-AI integration, Prometheus integration, rollback/compatibility evidence.
- Explicit dependency graph and safe implementation order. Note especially B3 -> B2 target/token use -> B5 credentials/scrape dependencies; identify what can run in parallel.
- Baseline/test gates and acceptance criteria per package.

Required integration deliverable:
Create a detailed AI-server-facing specification as part of the PLAN (not implementation) so the AI team can implement with minimal interpretation.
It must define at least:
- architectural responsibility split: platform-poc vs AI Runtime vs sidecar/gateway,
- HTTPS/TLS assumptions,
- bearer/audience token rules,
- login and introspection contract,
- Start API,
- Stop API,
- Event ingest contract,
- clip/thumbnail retrieval rules,
- metrics endpoints and Prometheus exposition contract,
- request/response examples and fixtures,
- status/error semantics,
- timeout and retry ownership,
- compatibility across W1/W2/W3,
- forbidden behaviors/anti-patterns,
- contract change procedure.

Error diagnostics requirement:
Design explicit diagnostics for development/integration.
- API error handling must expose enough structured information to identify failure cause quickly.
- Define stable machine error code, human-readable message, request/correlation ID, failure layer/origin, retryability, relevant safe context such as serverId/cameraId/upstream status where applicable.
- Logs should include diagnostic detail such as exception class, upstream status, endpoint identity, operation, requestId/correlationId and elapsed time when useful.
- Do NOT expose secrets, bearer tokens, passwords, raw credentials, private-key material, internal filesystem paths, or stack traces in external API responses.
- Distinguish developer/integration diagnostics from production/public response safety.
- Specify redaction and logging rules.
- For transparent proxy/sidecar failures preserve upstream response whenever possible; only bridge-originated failures use bridge error codes/headers.
- Keep existing externally visible contract compatibility unless the source plan explicitly authorizes a change.

Output:
- A detailed, file-oriented, dependency-aware implementation plan.
- A dedicated section/document plan for the AI-server API/integration specification.
- A detailed error-code/diagnostic matrix.
- Explicit non-goals to prevent overengineering.
- Risks, rollback, test matrix, acceptance criteria.
- Identify any decision that truly requires human approval; otherwise resolve engineering choices autonomously.
