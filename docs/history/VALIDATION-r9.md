> **Historical archive — not the current runbook.** This describes an earlier package. The current version uses Couchbase as the only application database; follow [README](../../README.md) and [ARCHITECTURE](../../ARCHITECTURE.md).

# Validation — 27 September 2026

Built from the supplied original v2.1.3 ZIP. Both migration patches are incorporated. The original ZIP and your working application directory were not edited to create this package.

- 8 transport/configuration tests passed.
- 13 bootstrap tests passed: fresh loading, data preservation on restart, incompatible vectors, scoped Search creation/updates, index UUID/replica preservation and permission failures. Search tests use real SDK 4.6.2 index types with fake management operations.
- 69 Python files parsed; shell scripts and new profiles passed Bash syntax checks.
- Docker Compose v2.23 parsed all five services. Checked endpoint normalization, model settings, read-only CA mount, credential separation and canonical serialization of fixture passwords containing dollar signs/quotes.
- The Memory NVIDIA change was checked offline against both source-package copies in the supplied rc2 image, covering passage/query calls, startup probes, dimension detection and unchanged request shape for other models.

The local Docker daemon was unavailable to this execution environment, so images were not built or started here. No live Capella, model or workflow call was made. The startup command performs real checks using the values you supply.

These are targeted package checks, not a claim that the entire application was exercised against Capella. The original regression tests remain included.

Additional launcher checks used a fake Docker command: seven stages execute in order from a path containing spaces; setup failure stops before startup; a missing certificate stops before build/provisioning. These do not execute containers.

Revision r2 fixes the Agent Memory Dockerfile to invoke Python using Docker's exec form. The supplied minimal image has no /bin/sh, so a shell-form RUN fails before Python can start. The user's startup log confirmed successful live database access, query/passage embeddings (2048 dimensions), streamed chat, schema/index creation and initial data loading before this build failure. These observations do not verify the later Memory/MCP/UI startup stages.

Revision r2 checks: validated JSON exec-form RUN instructions; traced the supplied image Python symlinks to its executable; tested the macOS in-place repair command and a repeat invocation. Docker daemon access remains unavailable here, so the revised image build and remaining live startup stages require a rerun on the user machine.

Revision r3 fixes Agent Catalog TLS configuration. The user's next startup log confirms successful Memory/MCP/publisher image builds and Agent Memory readiness, followed by a missing AGENT_CATALOG_CONN_ROOT_CERTIFICATE error at publishing. The runtime now exports this variable, and Compose passes it alongside a read-only CA mount to the publisher, UI and setup tools. The publisher checks the certificate path before indexing. The certificate requirement is documented at https://docs.couchbase.com/ai/build/integrate-agent-with-catalog.html.

Revision r3 checks: Docker Compose v2.23 rendered the intended TLS connection, certificate setting and matching read-only file mount for all three services. Memory configuration and UI credential separation were preserved. Publisher checks rejected a missing setting, missing file, empty file and directory; a readable fixture file reached the dependency stage. Bash syntax checks passed. These checks used fixture settings, not real credentials or TLS handshakes. Publishing, UI startup and application behaviour still require live verification after applying r3.

Revision r4 fixes the plan-cache statistics SQL++ index. A live read-only diagnostic reproduced QueryIndexNotFoundException for the query used by /api/planner/cache. The existing version/expiry index and vector Search index did not satisfy its type predicate. Both automatic provisioning and the reference schema now create ix_plan_cache_stats on (type, semanticEligible, hitCount). Verification now identifies this endpoint and directs the user to the UI logs when it returns an HTTP error.

Revision r4 live validation (27 September 2026): after Docker access was granted, the missing statistics index was added to the configured Capella demo and confirmed online. The previously failing query succeeded, and EXPLAIN selected ix_plan_cache_stats. The complete startup verifier then passed all eight required services and the plan-cache Search check. Browser-facing /health, /api/planner/cache and / returned HTTP 200, with healthy status and searchHealthy=true. AI Functions and managed Data Processing remain disabled in this configuration. These results verify startup and health endpoints; they do not claim a full test of every conversational or governed-action flow.

Revision r4 offline checks: the changed Python files parse successfully, and a simulated HTTP 500 produces the endpoint-specific diagnostic while still failing verification. Small-update extraction is checked against prior packages with fixture configuration, certificate and local data files preserved.

Revision r5 fixes native Agent Tracer message logging. The live agentc 1.1.2 SDK reads content.kind directly; the previous gateway supplied dictionaries. The existing session had one begin event, and its operational trace recorded AttributeError: 'dict' object has no attribute 'kind'. The same operational trace incorrectly reported nativeAgentTracerLogged=true despite that failure. The gateway now validates payloads into the public Content models, uses tool_result for results, logs supported error events, keeps extra metadata, assigns unique tool-call IDs and closes a failed start. Native session IDs and logging status in the operational projection now reflect the actual trace.

Revision r5 tests: six regression tests using the real agentc Span and Content types passed without database/network access. They cover typed user/assistant/tool events, metadata, redaction, call/result correlation, start failure, finish failure, tool failure, trace-error handling and disabled tracing. A related 28-test run reported 24 passed and four failed. The same four failures were reproduced against the unchanged r4 package: they assert legacy README/profile text that the Capella package replaced with runtime defaults. They are pre-existing packaging-test mismatches, not r5 tracing regressions; the complete original test suite is not claimed to pass.

Revision r5 live validation (27 September 2026): the three application files were backed up, updated in the user's working copy, and the UI image rebuilt and restarted. Installed agentc remains 1.1.2. A labelled diagnostic used the real read-only Couchbase MCP schema tool in session streamai::sai::session-1-ce116c. SQL++ with request-plus consistency confirmed stored user, assistant, tool-call, tool-result, key-value and end events in streaming.agent_activity.logs. The corresponding operational trace recorded nativeAgentTracerLogged=true with the matching session ID. The historical lone begin event was retained; missing historical messages were not fabricated or replayed.

Memory diagnostics for this viewer showed zero chat entries, zero short-/long-term submissions, no pending persistence and no persistence error. The agent_memory.agentmemory scope contains users and sessions (one document each) and memory (zero blocks). This confirms the existing empty-memory observation corresponds to no conversational memory submissions. It does not constitute an end-to-end memory-write/summary/embedding test. MCP schema proof deliberately does not write memory.

Revision r5 post-restart checks: /health, /api/status and /api/planner/cache returned HTTP 200. The status endpoint reported all eight required services healthy and no startup error. Both ZIPs passed CRC checks; the full package retains placeholder credentials, and the small update excludes environment files and certificates.

## Revision r6 — measured usage audit

18 gateway tests pass against real FastAPI/HTTP client plumbing with controlled upstream responses: source accounting, batch embeddings, missing usage, retries, errors, SSE framing/cumulative reports/truncation, Memory HTTP acceptance, concurrent writes, reporting windows, mirror revision races, restart recovery, cached token metadata and invalid activity values. No external model calls occur in these tests.

58 selected application tests pass using the installed Couchbase and Agent Catalog SDKs: metrics API contract, tracing, transport/bootstrap, governed planner cache, latency policy and showcase usability. Obsolete tests that asserted hypothetical savings now assert that those estimates are not exposed as measured values. This is not a claim that the complete original test suite passes.

JavaScript renderer checks pass for unavailable data, rejection of old aggregates, background source counts, partial token sums, coverage arithmetic and unknown token values. Python and JavaScript syntax checks pass. Compose renders all four inference source routes and the Memory API proxy correctly; the usage service has no published host port. The r6 gateway Docker image builds successfully. Live installation was not applied because write access to the existing demo folder was not granted. At the final isolated-check attempt, the previous UI container was no longer present. The new gateway has therefore not been verified against the current Capella deployment; startup performs real model and service checks. No live background-accounting result is claimed.

## Revision r7 — explicit comparison and profile-memory reconciliation

107 targeted Python checks pass: 19 gateway/API tests, 15 baseline-calculation tests, 15 profile-memory tests and the same 58 selected application checks used in r6. Tests cover signed differences, separate LLM/embedding tokens, background/setup/retry inclusion, all-viewer comparison scope, invalid assumptions, unknown usage suppression, persistent/audited assumptions, seeded and existing profiles, repeated sync, pagination, partial acceptance, enrichment retry, owned-block removal, viewer-switch isolation and chat/profile/seed integration hooks. The Agent Memory transport test uses the installed SDK with controlled HTTP responses; it is not a live memory-write test.

All 107 checks pass against both the existing dependency image and the freshly built r7 UI image (Agent Memory SDK 1.0.0, Agent Catalog SDK 1.1.2 and Couchbase SDK 4.6.3). Tests use mocked external services and no real Capella/model credentials. One Starlette HTTP test-client deprecation warning is present. As with r6, this is a targeted regression set, not the complete original test suite.

JavaScript syntax and renderer checks pass, including negative estimates, USD formatting, activity-reporting gaps and missing-usage suppression. The new dashboard was inspected in a browser using explicitly labelled synthetic data; no displayed fixture number is a measured demo result. Both r7 UI and gateway Docker images build successfully.

The user's running application folder was not changed, and no r7 live Capella deployment or end-to-end enrichment result is claimed. Install the fresh package, run startup verification, sign in and check the new profile memory status after background processing. Earlier live r4/r5 observations above remain historical, not evidence of r7 deployment.

## Revision r8 — compact savings panel

108 targeted Python checks pass, including the existing r7 set and a new check marking enabled cloud AI Functions and managed Data Processing outside the local meter. JavaScript syntax and renderer checks pass for positive, negative, empty and unavailable results; managed-service coverage suppression; visibility-based polling and overlapping-request prevention. An HTML structure check confirms that assumptions and detailed usage are collapsed by default while the three headline cards stay visible.

The compact panel was visually inspected in the browser with labelled synthetic data; displayed fixture amounts are not real demo usage. The r8 UI Docker image builds. No real AI Function, model, Capella or billed-usage call was made for this revision. This is not a live deployment or full-suite validation claim. Earlier r7 accounting, profile synchronization and native tracing changes remain included.


## Revision r9 — persisted viewing guides and scoped savings

73 targeted Python tests pass for AI guide generation/persistence, normalized error handling, cache/concurrent reuse, source invalidation, recipe changes, disabled generation with saved data, CAS protection, catalogue reingestion, assistant summary reuse and unchanged entitlement authority; gateway accounting, baseline arithmetic, missing usage, negative values and existing governed chat/title handling are also covered. Two JavaScript test suites pass: measured usage rendering/polling, and viewing-guide output/escaping, duplicate-click suppression, retry and title-switch protection. Python syntax validation and JavaScript syntax checks pass.

UI and usage-gateway Docker images build. The title guide and compact usage panel were inspected in the browser. Layout tests used a real Capella-generated guide and clearly labelled synthetic metrics; fixture savings are not claimed as real usage. The top-left header now shows only StreamAI, with the CB icon removed.

A real Capella validation generated and saved the guide for `tv::275102` (The Scandal), confirmed Query request ID `ecf93855-edcb-4462-8ccf-bbf9e1a76619`, and read the persisted guide back. A second application request reused the same execution ID without another AI Functions query. The assistant's title-information renderer returned that saved summary with its origin labelled. This validation did not replace the user's running UI. Its generation/reuse audit is in the catalogue/telemetry data; the validation deliberately did not send the new-schema events into the old running r8 gateway. An actual full browser chat plus asynchronous Agent Memory cycle on the upgraded stack has not been exercised in this revision.

An expanded run of the legacy `test_v150_native_ai_plane.py` file has six pre-existing failures, reproduced against r8: two tests mock an older Agent Catalog span API; four assert superseded environment/README layout. They are outside this change. No complete original-suite pass is claimed.

AI Functions token/request totals remain unknown: the function responses consumed by this integration contain no usage report. The visible scoped savings comparison remains available with AI Functions enabled, but is explicitly not an overall deployment or billed-savings figure.
