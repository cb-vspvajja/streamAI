# Rebuild StreamAI: Capella AIDP reference implementation

Copy everything below into your coding agent, and attach the UI reference kit. If you have the application ZIP, attach that too: it is the authoritative implementation reference. This prompt describes the v1.1.0 functional contract; it does not guarantee byte-for-byte reproduction or access to proprietary vendor distributions.

---

You are building StreamAI, a working media-streaming recommendation and assistant demo for a Couchbase solutions engineer. The audience includes customers, business leaders and technical teams. Deliver runnable source code, a Docker startup path, documentation and tests. Work through implementation and validation; do not stop after scaffolding or a design proposal.

## 1. Product purpose and priorities

Demonstrate how live operational data, conversational memory, semantic search and governed AI actions work together. Use language models where interpretation or extraction adds value. Use deterministic code for exact business rules, catalogue existence, permissions, counts and simple actions.

The experience must be attractive, explainable and grounded in a real catalogue. Never recommend invented titles or imply a purchase/playback succeeded without a stored action receipt. Playback is a demo interaction, not licensed video streaming. Synthetic availability/entitlement records must be labelled as demo policies.

Accurate measurement is a requirement. Do not inflate baselines, hide background inference, report missing usage as zero, clamp negative savings, or imply that token reduction automatically lowers Capella invoices. A polished demo must remain honest.

The source app is a single-instance demo with one active viewer context per application process. Do not silently claim production tenant isolation, multi-user concurrency or Netflix-scale recommendation quality. Identify the work needed for those capabilities.

## 2. Files and visual fidelity

Use the attached StreamAI UI reference kit in this order:
1. README and UI-UX-SPEC.md for the visual and interaction contract.
2. tokens.json for exact colours, typography, borders and spacing.
3. screenshots for each populated screen.
4. prototype/index.html and its CSS for a working visual reference.
5. frontend-reference for the application’s original HTML/CSS/JavaScript.

Retain the dark StreamAI visual identity and existing information hierarchy. The upper-left logo is the text “StreamAI”, with AI accented red. Do not add a Couchbase logo to the application. The presentation deck may use Couchbase branding.

Use the existing CSS if the source is available. Reuse its component classes rather than approximate them. Maintain the cinematic hero, horizontal title carousels, right-side assistant drawer, profile and memory dialogs, title details, viewing guide, usage section, and agent inspector. Keep the showcase/customer-view switch. Operational detail belongs in showcase mode; normal browsing remains simple.

The kit’s fixture figures and fictional titles illustrate layout only. Never seed these numbers into live measured metrics or represent the screenshots as actual model savings.

## 3. Architecture and runtime

Use Python FastAPI for the local backend and plain HTML/CSS/JavaScript for the frontend unless an explicit migration is requested. Use the Couchbase Python SDK, the official Agent Memory SDK/server, the Couchbase MCP Server and native Agent Catalog/Tracer APIs.

Docker Compose runs:
- ui: FastAPI, static frontend, orchestration, deterministic planner/policy, SDK clients.
- memory: native Couchbase Agent Memory API. Persists users, sessions, original blocks and embeddings in Capella.
- mcp: official Couchbase MCP server with least-privilege read-only credentials for its database access.
- usage: custom demo inference proxy and measurement service. This is application instrumentation, not a Couchbase product.
- tools: one-shot connectivity checks, schema/index setup and catalogue ingestion.
- publisher: one-shot Agent Catalog prompt/tool publication.

Capella supplies operational data, KV, SQL++ Query, Search including vectors, Model Service, native AI state storage and optional AI Functions. A managed Data Processing workflow is optional. The default catalogue loader is Python, so do not depict a managed workflow as active without evidence.

All application persistence must use Couchbase, including viewer state, cached plans, usage events, reporting settings and experiment receipts. No SQLite, Redis, local database fallback or silently authoritative in-memory usage ledger. Temporary files, Docker images and logs are not application databases.

The current distribution is macOS Apple Silicon friendly and includes the approved arm64 Agent Memory rc2 image archive and MCP source distribution. Obtain these through legitimate official distributions or supplied files. Do not fabricate vendor code or download credentials. If reproducing on another CPU architecture, obtain a matching server image and validate it explicitly.

## 4. Configuration and launch

Provide capella.env.example and a placeholder capella.env, plus START-CAPELLA.sh, STOP-CAPELLA.sh and CAPELLA-LOGS.sh. Preserve the full AIDP example profile as a compatible entry point. Startup must work in paths containing spaces and must not require host Git, Xcode, Python or accepting Apple SDK licences. Use Docker for build and setup dependencies.

Expose:
- TLS Couchbase connection string, application credentials, separate MCP credentials and optional provisioning credentials.
- Content bucket name, native Memory bucket configuration and native Agent Catalog/Tracer storage configuration.
- Model endpoint roots, inference API key and CA path.
- TMDB token, catalogue sizes/sample-data flag, ingestion method and optional AI Functions controls.
- MEMORY_SUMMARY_POLICY=selective by default.
- Health refresh/cache intervals appropriate for a demo; health probes are real measured work.
- Explicit provider/routing variables. Display the configured Capella models, not stale Ollama copy.

Default model contract:
- LLM: mistralai/mistral-7b-instruct-v0.3.
- Embeddings: nvidia/llama-3.2-nv-embedqa-1b-v2.
- Vector dimension: 2048.
- NVIDIA requests distinguish query versus passage and use supported float encoding.
- Mistral health checks use supported max_tokens=1, temperature=0. Do not rely on an ignored max_completion_tokens parameter.
- Route inference through the meter before setup or ingestion begins.
- During measured trials force X-cb-cache=none; cache hits must not masquerade as newly measured inference reduction.

Never commit real credentials, certificates with private material, viewer data or populated usage history to the downloadable source.

Launcher order:
1. Validate configuration, TLS/CA, architecture and Docker.
2. Bootstrap the Couchbase usage collection/index using authorized setup credentials.
3. Start a Couchbase-backed usage gateway; fail closed if measurement cannot persist.
4. Run metered connectivity/model checks.
5. Provision content schemas, scoped Search indexes and vector mappings.
6. Load/resume only an empty or interrupted catalogue; preserve populated catalogues and saved AI guides.
7. Build/start compatible native Memory and MCP services.
8. Publish native Agent Catalog definitions and build/start the UI.
9. Verify readiness, enabled integrations and usable vector coverage.
Keep launch rerunnable and preserve data on restart. No destructive reset in routine upgrades.

## 5. Data model and ownership

Follow the supplied schema and configurability. Key storage responsibilities:
- streaming.catalogue.titles: TMDB or clearly labelled sample title documents, source metadata, embeddings, availability demo fields and optional aiEnrichment.
- streaming.viewers scopes/collections: operational profile, preferences, history, interactions, entitlements and resumable app state.
- streaming.recommendations: generated/versioned recommendation artifacts and bounded plan cache.
- streaming.operations: ingestion/bootstrap/action records as defined by the application.
- streaming.telemetry.ai_metrics: durable model-attempt events, control settings and trial receipts.
- Native Agent Memory collections: users, sessions and memory blocks, managed through the official SDK/API.
- Native Agent Catalog and Tracer collections: tool/prompt definitions and typed activity records managed by their native integration.

Do not put the content catalogue into the Memory bucket merely because the app uses memory. Do not equate the custom usage ledger with Agent Tracer. Document actual bucket/scope/collection configuration and provide inspection queries.

Use distinct document types/key namespaces, CAS where concurrent mutation matters, bounded TTLs for short-term blocks and policy-aware cache invalidation. Preserve memory/session ownership and viewer identity across queued work.

## 6. Catalogue, search and recommendations

Load TMDB movies and television metadata with provenance. Provide a clearly labelled sample catalogue only when requested/configured. Store title ID, content type, synopsis, genre, people, language, year, runtime, rating and artwork references. Retain TMDB attribution. Demo availability and subscription rules must have provenance.

Implement:
- Field-aware full-text Search.
- Vector query retrieval using 2048-dimensional NVIDIA vectors.
- Hybrid retrieval with explicit effective-mode evidence.
- Structured KV/SQL++ shortcuts for exact facts, counts and known filters.
- Visible fallback if Search/embedding is unavailable.
- Deterministic exclusion/eligibility filtering after candidate retrieval.

Home rows: Top Picks, Because You Watched/Started, Trending Now, Continue Watching, Recently Watched, My List. Explain their actual inputs. The source app’s home rows read operational profile/history; they do not directly call Agent Memory on every carousel load. Because You Watched may reuse the stored seed-title vector. Do not claim Trending applies every personal rule if it only applies entitlement and disliked-title filters.

Like, dislike, watch/progress, watchlist and explicit preferences must persist and invalidate relevant caches. Never use a vector similarity score as a probability, an age rating or a reliable parser for negation.

## 7. Assistant and governed actions

Support catalogue discovery, personalized requests, title questions, explicit preferences, region changes/questions, history/count/analytics questions, recommendation explanations, follow-up references such as “the second one”, and approved watchlist/like/play demo actions.

Use a bounded typed plan: allowlisted intent, filters, sort, result count, optional analytics fields. Never execute arbitrary model-produced SQL/code. Resolve references to known title IDs. Check current availability, region, subscription and parental controls before an action. Produce a durable receipt or visible error.

Planner order:
- Exact validated plan cache.
- Eligible semantic template reuse with compatibility guard, slot rebinding and schema/version validation.
- Deterministic plan when possible.
- Bounded LLM fallback only when needed, with output validation and timeout.
- Explicit safe fallback/unknown response on failure.

Reusing a deterministic plan is not proof of an LLM call avoided. A cached template still executes fresh data/policy reads. Do not cache final permission decisions or reuse stale personalized answers.

Stream progress and a grounded answer to the browser. Capture user message, plan/tool activity, assistant response, session identifier, native trace linkage and errors. The Agent Tracer session must contain meaningful child messages/spans; an empty session is not sufficient evidence.

## 8. Memory behaviour and real optimization

Store the original conversation through native Agent Memory and keep immediate working context in Couchbase app state for responsiveness. Capture immutable viewer/session targets before background submission. Rehydrate durable facts across sessions.

Mirror structured profile preferences into a dedicated deterministic native Memory session. Reconcile idempotently: submit only missing facts, retry failed blocks, remove obsolete blocks owned by the mirror only after replacements are accepted. Preserve unrelated memories. Show accepted, stored, pending and ready states separately.

Default selective policy:
- Conversation content of at most 4,000 combined characters: context_required=False.
- Longer conversations: context_required=True so summarization remains available.
- Already explicit structured facts: context_required=False.
- Embeddings and original native Memory blocks remain enabled in both cases.
- MEMORY_SUMMARY_POLICY=always restores conversation summarization when required.
- Avoid double-writing the same profile change as both a conversational fact and profile mirror if mirror submission succeeds.
- If mirroring fails, preserve the fallback fact write and expose the error.

Do not confuse async_processing=False with disabling inference. context_required=False skips optional LLM extraction in the validated native server. Embedding inference still occurs and must be counted. Verify SDK/server support; fail visibly if the supplied native version does not support these semantics. Do not silently pass unsupported flags.

Semantic memory search can remain an explicit operation; do not add an embedding to every simple chat just to showcase a service. Explain where summaries enrich retrieval and where original content suffices.

## 9. AI Functions as a visible product feature

Use deployed classification, summarization and sentiment functions to create an AI viewing guide from a title’s current catalogue text. Validate the actual Capella function signatures against current official docs and deployed configuration.

Save a successful guide as aiEnrichment on the title document with input/source fingerprint, recipe/version, generation timestamp, execution ID, query request ID and provenance. Use CAS to protect concurrent catalogue edits. A failure or partial result must not overwrite a valid previous guide.

Expose Generate viewing guide, Reuse saved guide, Saved execution details and Ask the assistant about this title. The assistant can reuse a valid guide and label its origin. Source text changes invalidate it. Show guide content and persistent evidence in the UI; browser traffic alone is insufficient.

Do not claim three returned function values equal exactly three upstream model requests. AI Functions may bypass the custom model gateway and may not return token usage. Show their activity separately with unknown tokens/cost, never zero.

## 10. Measurement and paired savings comparison

Create the initial Couchbase event before forwarding any model inference. If recording fails, fail closed. Finalize with status, provider usage, model, source, duration, provider request ID and supported cache headers. Parse streaming final usage correctly. Each HTTP retry is an attempt. Failed, interrupted or unreported usage is unknown, never free. Do not store prompts, answers, vectors or secrets in the usage ledger.

Count app, Agent Memory, ingestion, setup and diagnostic inference. Count health probes. Record Memory API operations separately; API writes are not equivalent to model calls. Keep all historical audit records. “Start new reporting window” only moves a display boundary.

Replace the old assumed one-call-per-chat headline with a real paired experiment:
1. Select the last 2–4 complete short chat turns and at most six explicit context facts.
2. Snapshot content and compute a SHA-256 fingerprint.
3. Use fresh, isolated native Memory users/sessions for both policies.
4. Replay the same content under the previous summarize-every-block policy and the selective policy.
5. Complete each phase synchronously, not by guessing a sleep duration.
6. Correlate native model calls and retries with trial ID/phase through request-local context and headers. Never tag unrelated health work as trial work.
7. Include routed background model work during each phase; disclose outside-phase overhead and the entire experiment cost separately.
8. Read native blocks back. Verify exact original message/fact content, multiplicity and ready status. Require correlated embedding requests in both phases and a correlated LLM request in the previous policy.
9. Block/suppress a savings claim for incomplete usage, failures, missing correlation, late work, mismatched content, or concurrent unrelated app/AI Function generation.
10. Preserve negative deltas. Clean up only the isolated trial user created by this run. Keep the durable receipt, hashes, rates and request IDs.
11. Protect against concurrent trials and app writes during a trial. Recover explicitly after process interruption.

Cards:
- LLM calls avoided = measured previous requests − measured selective requests.
- LLM tokens saved = measured previous input/output tokens − selective input/output tokens.
- Token-cost equivalent saved = previous reference cost − selective reference cost.

Reference cost = (LLM input tokens × input USD/1M + LLM output tokens × output USD/1M + embedding input tokens × embedding USD/1M) / 1,000,000.
Illustrative defaults: USD 2.50 input, USD 10.00 output, USD 0.10 embeddings per million tokens. Label these as example rates, not vendor prices. Capture rates in each trial receipt so later edits do not rewrite history.

Scope: memory-ingestion optimization for the same retained original content. This is not a no-AIDP versus AIDP benchmark, proof of equal summary-based retrieval quality, overall deployment savings, or Capella billed savings. Capella Model Service uses capacity/time billing; invoice reduction requires a separately validated capacity or operating-hours change. No prefilled “success” metrics in production.

## 11. Verification and delivery

Test meaningful behaviours: policy selection, duplicated/missing content, permission checks, current-data reads after cache reuse, profile reconciliation, safe SDK memory writes, streaming accounting, retries, missing usage, negative deltas, trial correlation/isolation, failed cleanup, process restart, rate snapshots and preserved audit history.

Run SDK-aware tests with real packages loaded; avoid import-order contamination from legacy stubs. Test the compatible native Memory image build. Exercise the paired replay using mocked providers/storage without pretending that proves live Capella savings. A live cloud trial needs the operator’s configuration and must be reported separately.

Inspect the UI at desktop and narrow widths. Verify no clipped metric values, inaccessible dialogs, missing focus states, duplicated IDs, phantom success, stale Ollama text or Couchbase logo in the app header. Compare screenshots against the kit.

Deliver:
- A fresh full source ZIP in the v1.x series with vendor prerequisites identified/bundled where authorized.
- Placeholder configuration only and a clear upgrade path that preserves the existing capella.env, certificate and database contents.
- Comprehensive README, architecture document, metrics audit, AI Functions guide, exact savings-demo path, changelog and validation report.
- A package manifest and checksum.
- An honest list of tests run and live checks not performed.
- No claim of measured cloud savings until a successful recorded trial exists.

Use current official Couchbase docs when implementing APIs:
https://docs.couchbase.com/ai/build/agent-memory/about-agent-mem.html
https://docs.couchbase.com/ai/agent-memory-api-reference/rest-api.html
https://docs.couchbase.com/ai/build/model-service/model-service.html
https://docs.couchbase.com/cloud/billing/billing.html

Implement the application, validate the result, and package it. Preserve the visual experience and the factual limits above.

