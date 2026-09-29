# StreamAI 1.1.0 — Couchbase Capella demo

StreamAI demonstrates how **live operational data, conversational memory, semantic search and governed AI actions work together** in a streaming-media application. A viewer can discover titles, ask catalogue questions, maintain preferences and a watchlist, and see evidence of the data and tools behind an answer.

This is a **demo application** connected to Capella, with selective Memory processing and measured policy comparisons. Version **1.1.0** builds on the Couchbase-only version 1 baseline. It remains a single-instance presentation application, not a production multi-user service. Vendor versions and existing records are preserved.

**Start here:** [Quick start](#quick-start) · [Upgrade an existing installation](#upgrade-an-existing-installation) · [Demo walkthrough](#demo-walkthrough) · [Troubleshooting](#troubleshooting)

**Further reading:** [Detailed architecture](ARCHITECTURE.md) · [AI viewing guides](AI-FUNCTIONS-GUIDE.md) · [Metrics definitions and audit](METRICS-AUDIT.md) · [Release validation](VALIDATION.md) · [Changes](CHANGELOG.md) · [Savings demo](SAVINGS-DEMO.md) · [Rebuild prompt](REBUILD-PROMPT.md)

## What runs where

| Location | Components | Purpose |
| --- | --- | --- |
| Browser | StreamAI interface | Catalogue, chat, profile, memory inspector, usage and presenter tools |
| Local Docker | `ui` | FastAPI backend, agent orchestration, deterministic policies and static interface |
| Local Docker | `memory` | Couchbase Agent Memory API and asynchronous memory enrichment |
| Local Docker | `mcp` | Couchbase MCP Server for approved database reads |
| Local Docker | `usage` | Demo-specific inference proxy; persists and reads its entire usage ledger in Capella |
| Local Docker, during setup | `tools`, `publisher` | Provision/load/check the demo and publish native Agent Catalog tools/prompts |
| Capella Operational | Data, Query, Index, Search | Catalogue, profiles, history, policies, plans, memory, traces, usage events/settings and vector retrieval |
| Capella AI Data Plane | Model Service | Chat/planning and embedding inference |
| Capella, optional | AI Functions; Data Processing workflow | Persisted viewing guides; managed catalogue vectorization |

Agent Memory runs in Docker and persists its data in Capella. Agent Catalog and Agent Tracer use native SDKs and Capella collections. The local usage service is custom demo instrumentation, not a Couchbase product component. **Couchbase is the only application database**, including usage history, reporting-window boundaries and trial receipts/settings. This package has no SQLite ledger, local database volume or database fallback. Local containers still execute application logic; logs, temporary files and model/publish caches are not application databases.

The configured models are:

- LLM: `mistralai/mistral-7b-instruct-v0.3`.
- Embeddings: `nvidia/llama-3.2-nv-embedqa-1b-v2`, **2048 dimensions**, with query/passage input modes.
- Agent Catalog publisher: local `all-MiniLM-L12-v2` for its own catalogue of tools/prompts. These embeddings are separate from media and memory embeddings.

Ollama is not started or used by the Capella launcher. Legacy local-development profiles are retained for reference.

## Prerequisites

1. **Apple Silicon Mac / ARM64 host and Docker Compose v2.** The bundled Agent Memory server image is ARM64. The launcher rejects other architectures; this ZIP is not an AMD64 release. Start Docker Desktop before launching. No host Git, Xcode, Python or Node installation is needed to run the demo.
2. **Capella Operational cluster with Data, Query, Index and Search**, and buckets named `streaming` and `agent_memory`. The current Agent Memory quickstart specifies Server **8.0.2 or later**; confirm availability for your cluster. Setup creates scopes, collections and indexes inside the existing buckets. [Agent Memory prerequisites](https://docs.couchbase.com/ai/build/agent-memory/get-started-agent-mem.html)
3. **Network access.** Allow the host's outbound public IP in Capella, including the VPN's egress IP when relevant. Docker needs DNS/TLS access to the cluster and model endpoints, plus access to image/package registries and the publisher's first model download. [Capella allowed IP addresses](https://docs.couchbase.com/cloud/clusters/allow-ip-address.html)
4. **Database credentials.** The simplest demo setup uses one account with read/write access to both buckets and the permissions needed to create scopes, collections, SQL++ and Search indexes. Use a separate read-only account for MCP on `streaming`. Database credentials, model inference credentials and Capella organization/project roles are different things. [Cluster access credentials](https://docs.couchbase.com/cloud/clusters/manage-database-users.html)
5. **Both deployed models**, their HTTPS inference endpoint roots, and an inference key valid for them. A Capella Management API key is not interchangeable with an inference key.
6. **The cluster's root CA**, downloaded from its Connect area and saved as `certs/capella-ca.pem`.

For AI viewing guides, also enable **Classification, Summarization and Sentiment Analysis** on this cluster and associate the intended generative model. Advanced application credentials need **Query Curl Access** as well as ordinary data access. AI Functions have additional cluster/plan requirements; check the [official AI Functions prerequisites](https://docs.couchbase.com/ai/build/ai-functions.html).

## Quick start

### 1. Extract and configure

Extract `streamai-capella-v1.1.0.zip`. In the extracted `streamai-capella-v1.1.0` folder, edit **`capella.env`** and replace all eight `CHANGE_ME` values:

| Variable | Value to supply |
| --- | --- |
| `CB_CONN_STRING` | `couchbases://` SDK hostname from Capella |
| `CB_USERNAME`, `CB_PASSWORD` | Application/setup database credential |
| `MCP_CB_USERNAME`, `MCP_CB_PASSWORD` | Separate read-only database credential |
| `CHAT_BASE_URL` | HTTPS chat inference endpoint root |
| `EMBEDDING_BASE_URL` | HTTPS embedding inference endpoint root |
| `CAPELLA_MODEL_API_KEY` | Inference key accepted by both endpoints |

Use single quotes around passwords and keys so spaces and `$` remain literal. These files are sourced by Bash; they are not arbitrary dotenv syntax. Use only trusted configuration. Do not include `/chat/completions` or `/embeddings` in an endpoint root; the launcher removes a trailing `/v1` automatically.

For your existing Capella setup with AI Functions deployed and the Python catalogue loader, use:

```bash
AI_FUNCTIONS_ENABLED=true
DATA_PROCESSING_MODE=python_loader
DATA_PROCESSING_WORKFLOW_ID=
```

The distributed default keeps AI Functions off so a new user can start before provisioning them. Merely setting the flag does not deploy a cloud service.

### 2. Choose catalogue content

For a quick new installation, leave `LOAD_SAMPLE_CATALOGUE=true`. An empty target receives six fictional sample titles.

For TMDB content on the first load:

```bash
LOAD_SAMPLE_CATALOGUE=false
TMDB_READ_ACCESS_TOKEN='your-TMDB-read-access-token'
```

TMDB supplies media metadata and artwork. Subscription offers and regional availability added by this demo are synthetic business data, **not real TMDB streaming rights**. “Play” simulates a viewing interaction; this package does not deliver licensed video streams.

Changing these flags after a catalogue is loaded does not replace it. See [Catalogue maintenance](#catalogue-maintenance).

### 3. Add the certificate and launch

Save the public CA as `certs/capella-ca.pem`, start Docker Desktop, open Terminal in the extracted folder and run:

```bash
bash START-CAPELLA.sh
```

Wait for **“StreamAI is ready”**, then open [StreamAI](http://localhost:8089).

The first build downloads dependencies and the Agent Catalog embedding model. Subsequent starts reuse images and caches. There are no host-side Git patches or Xcode commands to run.

### 4. Create or restore a viewer

Use **Create viewer** to choose a login ID and demo PIN, or sign in with an existing viewer. Operational persona/profile seeding and UI authentication registration are separate: seeding catalogue data does not establish a universal login PIN.

The demo stores salted PIN hashes and resumable state in Capella. It uses one active viewer per backend instance; separate browser tabs are not isolated production user sessions.

## What the launcher does

| Stage | Action | Success evidence |
| --- | --- | --- |
| Before stages | Loads configuration, checks ARM64, placeholders, HTTPS/TLS, CA and Docker | Invalid configuration stops before setup |
| 1 | Builds tools/gateway; stops old model producers; prepares the telemetry collection/index; verifies/adopts old meter history; starts the Couchbase-backed gateway | Couchbase usage storage available before inference probes |
| 2 | Tests cluster access and real chat/embedding requests | Endpoint, streaming and 2048-dimension checks pass |
| 3 | Creates application schema/indexes; loads an empty catalogue; waits for vectors/Search | Catalogue and both Search indexes ready |
| 4 | Imports the bundled Memory image when necessary; builds Memory, MCP and publisher | Compatible local images available |
| 5 | Starts Memory and MCP | Memory API healthy |
| 6 | Publishes native tools/prompts; builds and starts UI | Published catalogue artifacts and application readiness |
| 7 | Checks services and plan-cache Search/statistics | Required checks pass; enabled optional services are checked |

Repeat startup retains existing catalogue and history. It does not drop buckets or clear Docker volumes. An incompatible embedding model/dimension stops setup instead of silently mixing vectors. Health checks establish connectivity and configuration; they do not measure recommendation quality or prove an unused AI Function executed.

See [Architecture: startup](ARCHITECTURE.md#startup-from-command-to-ready-application) for the detailed sequence.

## Demo walkthrough

Use a catalogue title that exists in your installation; examples depend on whether you loaded sample or TMDB data.

| Step | Action | What to show |
| --- | --- | --- |
| 1. Live facts | Inspect My Profile, watch history and a title's availability | Operational data and business rules ground the experience |
| 2. Explicit preference | Save a genre preference, then use **Sync / retry Agent Memory** if needed | Dedicated profile-memory session; stored versus ready facts |
| 3. Search | Search for a mood or theme; compare lexical/vector/hybrid evidence in presenter tools | Semantic retrieval finds candidates; policies still filter them |
| 4. Governed plan | Ask a structured catalogue question, then repeat it | Validated plan and, when eligible, cache-hit evidence; current data is read again |
| 5. Continuity | Follow up about a result, or start a new session and inspect recalled facts | Conversation references and durable profile memory; new sessions may require retrieval/enrichment to settle |
| 6. Action | Ask to add a resolved title to My List | Tool trace, policy decision, operational write and action receipt |
| 7. AI Functions | Open a title and choose **Generate viewing guide** | Summary, mood classification, synopsis tone and saved execution details |
| 8. Reuse | Choose **Reuse saved guide**, then **Ask the assistant about this title** | Same saved guide and execution ID reused; labelled assistant response |
| 9. Memory efficiency | Complete two to four short turns, then choose **Compare memory policies** | Two executed policies, matching originals, measured tokens and illustrative cost |

Customer mode hides presenter-oriented technical panels. Showcase mode exposes inspector, traces, Search Lab, governance, evaluation and usage. Presenter/persona/reset controls can modify demo state; use them deliberately.

Not every chat calls the LLM. Structured questions, profile/history responses, actions and cached plans can use deterministic paths. Embeddings and asynchronous memory processing can still consume model resources after a deterministic answer.

### What the carousels actually use

| Carousel | Data used in this implementation |
| --- | --- |
| Top Picks | Operational profile, interactions and history; preference/policy filtering and ranking |
| Because You Watched / Started | Most recent title with viewing progress; stored title vector for neighbours, recommendation exclusions and policies; genre fallback on retrieval failure |
| Trending Now | Catalogue popularity/rating, denied-title filtering and explicit disliked-title suppression; not the full negative genre/theme preference policy |
| Continue Watching, Recently Watched, My List | Operational watch history and profile lists |

**None of these carousels calls Agent Memory directly**, including Top Picks. Preferences mirrored into Agent Memory also remain in the operational profile. Do not attribute carousel changes to a semantic-memory lookup that did not happen. AI viewing guides are used in title details and title-information chat, not carousel ranking.

## Where the data lives

Default names are shown below. Native SDK collection names describe the supplied/tested package.

| Data | Location |
| --- | --- |
| Media catalogue and embeddings | `streaming.catalogue.titles` |
| Saved AI viewing guide | The title document's `aiEnrichment` field |
| Profiles, watch history, interactions, login/resumable state | `streaming.viewers.profiles`, `watch_history`, `interactions`, `app_state` |
| Validated plans and operational traces | `streaming.recommendations.plan_cache`, `traces` |
| Entitlements, action receipts and ingestion records | `streaming.operations.entitlements`, `action_receipts`, `ingestion_jobs` |
| Native Agent Catalog | `streaming.agent_catalog.tools`, `prompts`, `metadata` |
| Native Agent Tracer | `streaming.agent_activity.logs` |
| Agent Memory | `agent_memory.agentmemory.users`, `sessions`, `memory` |
| Primary usage ledger, window/settings and AI Function execution audit | `streaming.telemetry.ai_metrics` |

Catalogue, Agent Catalog and traces do not belong in the Agent Memory bucket simply because they support an agent. [Architecture: persistence](ARCHITECTURE.md#persistence-and-ownership) explains ownership and the other provisioned collections.

Short-term conversation blocks use a one-hour application TTL by default. Long-term facts request TTL 0, subject to bucket/collection maxTTL. Explicit seeded profile preferences are mirrored on login/profile sync into a session beginning `streamai-profile-`, with annotation `source=streamai_operational_profile_sync`. Seeded watch history is not copied by that mirror. Accepted memory writes become searchable only after enrichment is ready.

## Understanding usage and savings

The headline cards compare **two executed Agent Memory policies for the same recent conversation**. Previous policy summarizes each saved short turn/fact batch; selective policy retains originals and embeddings without those optional summaries. Normal chat uses selective processing by default. Longer turns still request summaries.

After two to four short exchanges, choose **Compare memory policies**. This performs real model work in isolated sessions, verifies matching ready originals, records usage in Couchbase and removes its temporary user. The cards use recorded LLM requests, provider tokens and an illustrative token-cost equivalent including embeddings. They do not assume a number of calls per chat.

Both phases include Memory background work and retries. Incomplete usage, failed verification or detected competing app activity suppresses savings; negative results stay negative. The receipt also shows outside-phase overhead and full experiment usage. Running the test consumes both phases' resources.

This measures Memory ingestion efficiency, not a no-AIDP architecture, whole-application economics or equal-quality retrieval. Preserving originals does not prove equivalent summary-derived search quality. Dollar values use editable illustrative rates, not Capella invoices. [Capella billing](https://docs.couchbase.com/cloud/billing/billing.html) depends on provisioned resources and clock hours.

All routed deployment usage remains available separately. AI Functions and managed workflows bypass the inference gateway; their unreported tokens/cost stay unknown. AI Functions are not required for a Memory trial. Overlapping viewing-guide generation invalidates the trial.

See [SAVINGS-DEMO.md](SAVINGS-DEMO.md) for the presenter path and [METRICS-AUDIT.md](METRICS-AUDIT.md) for definitions and exclusions.

## Configuration and optional services

The launcher reads `capella.env`, or the file selected by `CAPELLA_CONFIG`, then sources `config/capella-runtime.sh`. Compose receives explicit environment variables and uses `/dev/null` as its dotenv file. An unrelated `.env` file does not configure this launcher.

```bash
CAPELLA_CONFIG="$PWD/my-capella.env" bash START-CAPELLA.sh
```

Use the same `CAPELLA_CONFIG` when stopping, reading logs or using the Compose helper.

| Setting | Effective default / behavior |
| --- | --- |
| `CONTENT_BUCKET`, `AGENTMEMORY_BUCKET` | `streaming`, `agent_memory`; advanced overrides supported by the launcher, but review static SQL and published tool assumptions before renaming |
| `CAPELLA_UI_PORT`, `CAPELLA_MEMORY_PORT`, `CAPELLA_MCP_PORT` | `8089`, `8081`, `8001`; bound to loopback |
| `MEMORY_SUMMARY_POLICY` | `selective`; omit optional summaries for short pairs; `always` restores conversation summaries |
| `AI_FUNCTIONS_ENABLED` | `false`; set true after deployment/association |
| `DATA_PROCESSING_MODE` | `python_loader`; optional `capella_workflow` |
| `LOAD_SAMPLE_CATALOGUE` | `true`; affects initial load, not an existing catalogue |
| `AGENTMEMORY_HEALTH_REFRESH_INTERVAL_SECONDS`, `AGENTMEMORY_HEALTH_CACHE_TTL_SECONDS` | `300`, `600` seconds; model probes are cached separately from frequent HTTP liveness checks |
| `CAPELLA_WAIT_SECONDS` | `300`; waits for services/indexing |
| `CHAT_API_KEY`, `EMBEDDING_API_KEY` | Optional separate inference keys; default to the common key |
| `CB_PROVISION_USERNAME`, `CB_PROVISION_PASSWORD` | Optional setup/publisher identity; default to application identity |
| `CAPELLA_*_URL`, project/cluster IDs | Optional presenter links into the appropriate Capella pages |

Many other settings, including model names, vector dimensions, required native components and planner defaults, are **fixed assignments** in `config/capella-runtime.sh`. Adding the same variable to `capella.env` will not override a later fixed assignment. Other settings exist in `ui/app/config.py` but are not necessarily passed through Compose. For advanced tuning, check the runtime script, Compose environment and application Settings together. Treat model/dimension changes as a data migration.

For managed Data Processing, provision a workflow that reads `streaming.catalogue.titles.embeddingText`, writes `embedding`, and uses the NVIDIA passage model with 2048 dimensions. Set `DATA_PROCESSING_MODE=capella_workflow` and a real `DATA_PROCESSING_WORKFLOW_ID`. The app writes source documents and waits for vectors; it does not create or invoke a workflow-control API. The ID is configuration metadata, not proof of a workflow run. [Structured-data workflow setup](https://docs.couchbase.com/ai/build/vectorization-service/vectorize-structured-data-capella.html)

`.env.capella-full-aidp.example` selects both optional services. Use it only after deploying both; AI Functions alone does not require changing from `python_loader`. Model Service semantic cache is separate from the application's semantic plan cache; the shipped runtime requests `MODEL_SERVICE_CACHE_MODE=none`.

## Operating the demo

```bash
bash CAPELLA-LOGS.sh
bash CAPELLA-LOGS.sh ui
bash CAPELLA-LOGS.sh memory
bash CAPELLA-LOGS.sh usage
bash STOP-CAPELLA.sh
```

Local ports are UI 8089, Memory API 8081 and MCP 8001. The usage gateway listens on port 8090 only inside Docker. Memory's internal Prometheus port 9090 is not published by this Compose file.

Useful read-only endpoints: `/health`, `/api/readiness`, `/api/status`, `/api/planner/cache`, `/api/agent/traces`, `/api/metrics`, `/api/memories`. `/api/status` includes viewer context; do not share its full output publicly.

Stopping retains Capella data and named Docker volumes. Stopping local containers does not stop cloud models or a Capella cluster. Back up Capella data, including `telemetry.ai_metrics`, when the records matter. Do not delete volumes to start a new measurement window.

### Catalogue maintenance

To intentionally run the loader again using the selected configuration, from the application folder:

```bash
bash -c 'source ./scripts/capella-common.sh; compose run --rm --no-deps tools python tools/load_catalogue.py'
```

The usage service must be running. This can call TMDB and embedding inference, update existing title documents and write ingestion records. It does not clear all old catalogue documents. Existing guides are preserved; changed title/overview text makes them stale. Rebuilding embeddings for a different model is a deliberate migration, not a flag-only operation. Retain a backup for valuable data.

### Upgrade an existing installation

1. Extract the **full** version 1.1.0 ZIP into a new folder.
2. Copy your working `capella.env` and `certs/capella-ca.pem` into that folder. Retain any deliberate runtime/Compose customizations after reviewing them against this release. Do not overwrite working credentials with placeholders.
3. If you used a custom config path, continue using `CAPELLA_CONFIG` or copy its values into the new `capella.env`. Keep the previous usage container running for the first upgrade, so its reporting settings and verified history can transfer. Leave `USAGE_LEDGER_ID` at its default `streamai`.
4. Run `bash START-CAPELLA.sh` from the new folder and refresh the browser after success.

The Compose project stays `streamai-capella`, so its containers are rebuilt from the new folder while named volumes and Capella data remain. Do not run two copies simultaneously under that same project name.

Version 1.1.0 changes app-versioned plan keys and published metadata, so new plans warm naturally. It performs no bucket reset or normal viewer-memory deletion. Keep the previous folder/configuration for recovery. Rebuild UI, Memory and usage together; the trial needs matching correlation support.

### How usage history transfers

The launcher stops UI, Memory and MCP before replacing the meter. It creates the telemetry collection and `ix_usage_ledger_time` index, waits for the previous meter to finish writing its audit copies to Capella, then compares current-window request counts, reported input/output tokens and completed chats. Only a matching, fully flushed snapshot is adopted. Its reporting boundary and operator-configured assumptions are saved in a Couchbase metadata document. A pending backlog, unfinished call, timeout or mismatch stops the upgrade while leaving the previous meter available for investigation. Allow up to `USAGE_UPGRADE_WAIT_SECONDS` (default 120 seconds), fix its connection and rerun the launcher if needed.

On a fresh install, or when the old meter is already stopped/unavailable and no Couchbase settings exist, a **new reporting window** starts. Earlier audit copies stay in Capella but are excluded from the new ledger until verified; the app does not guess old settings or reconstruct missing tokens. For automatic history adoption, perform the upgrade while the previous usage service is still running. The old Docker volume is neither mounted nor read by the new application, and is left untouched as a recovery artifact. Do not delete it until any required historical recovery is complete.

The dashboard now queries Capella directly. Each model attempt is recorded with durable KV writes before forwarding and after completion; SQL++ reads use request-plus consistency. A failed initial write blocks forwarding; a failed final write leaves incomplete usage visible and suppresses affected token/cost estimates. There is no offline database fallback. One gateway is supported per ledger namespace. Normal gateway recreation preserves records, assumptions and window boundaries without a local volume. The ledger sets no application expiry; bucket/collection maxTTL still applies. Use maxTTL 0 for retained history and back up the collection.

To inspect the primary ledger in Capella, open `streaming.telemetry.ai_metrics`. New event keys start `measured-usage::streamai::`; settings are under `measured-usage-state::streamai`. Adopted events can retain older keys beginning `measured-usage::`. The `type` field separates these records from AI Function audits and older metric projections. A custom `CONTENT_BUCKET` changes the bucket portion of these paths.

## Troubleshooting

| Symptom | Diagnosis and next action |
| --- | --- |
| Xcode license or missing patch script | Use this complete ZIP and `START-CAPELLA.sh`. No host Git patch is required. Git used by the publisher runs inside Docker. |
| Connection timeout | Check allowed egress IP/VPN, `couchbases://` hostname, Docker DNS and TLS access. |
| Permissions error during setup | Check schema/index privileges, access to both buckets, and provisioning credentials. MCP needs its own read-only identity. |
| Model 401/404 | Check inference key, region/deployment and endpoint root; do not append a completion path. |
| Certificate failure | Download the CA for this cluster into the expected file; do not disable TLS verification. |
| Vector incompatibility | Existing vectors must use the configured model and dimension. Use a separate demo target or migrate deliberately. |
| Still loading samples after switching TMDB flag | Existing data is retained. Run a deliberate loader update or use an empty target. |
| AI Functions projection/5010 error | Inspect the nested reason in UI logs. Check all three functions, model association, Query Curl Access, model availability and text input. A healthy flag alone is not invocation proof. |
| Guide unavailable/stale | Generate after configuration is ready. Changed source text invalidates an older guide; a failed regeneration preserves the earlier record. |
| Tracer session without messages | Verify rebuilt version, published native catalog, typed-content trace integration and tracer error in status. SDK logs are separate from operational traces. |
| Profile preferences absent in Memory | Sign in again or use My Profile → Sync / retry Agent Memory. Check stored/ready status and the dedicated profile session. |
| Short-term memory absent later | Check TTL; UI transcript/state is a different store. Accepted does not mean enriched. |
| Usage “Unavailable” | Check gateway logs, Capella access to `telemetry.ai_metrics`, the usage SQL++ index, completed chats and incomplete token/activity reports. The meter needs KV read/write and Query Select; setup needs schema/index permissions. Enabling AI Functions is not required. |
| Savings unavailable or negative | Run the measured trial after two short turns. Inspect the receipt; incomplete data suppresses results and background work can outweigh savings. See SAVINGS-DEMO.md. |
| Plan-cache query/index failure | Rerun setup and check both SQL++ indexes and scoped plan-cache Search. They serve different operations. |
| New UI/code not visible | Rebuild with the launcher from the intended folder, then refresh the browser. |

## Development, validation and limits

See [VALIDATION.md](VALIDATION.md) for commands, results and the exact tested scope. Core code is in `ui/app`; UI assets in `ui/static`; metering in `usage-gateway`; bootstrap and ingestion in `tools`; deployment defaults in `config` and `compose.capella.yaml`.

The supplied MCP distribution is `couchbase-mcp-server==1.0.0.post1`; Agent Catalog requirements are `agentc>=1.1.0,<1.2`, tested with 1.1.2. The Memory SDK wheel is 1.0.0; the supplied server image is 1.0.0-rc2 with a narrowly scoped NVIDIA compatibility layer. **The app's stable version does not change a dependency's release status.** Requirements still include ranges; uncached future builds need revalidation.

The legacy local launcher supports `AGENT_MEMORY_IMAGE_TAR`, `AGENT_MEMORY_IMAGE` and automatic image discovery. The current Capella launcher instead imports the included vendor tar directly. Use [docs/ORIGINAL-README.md](docs/ORIGINAL-README.md) only for that earlier local deployment; it is not the current Capella runbook. Historical migration notes are under `docs/history`.

This release does not establish production concurrency, security certification, recommendation success rates or SLA/throughput claims. Demo authentication, one active viewer, in-process caches/tasks and synthetic entitlement data require redesign before production deployment. Semantic similarity and heuristic recommendation scores are not calibrated probabilities. See [Architecture: production boundaries](ARCHITECTURE.md#production-boundaries-and-scaling).
