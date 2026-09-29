> **Historical archive — not the current runbook.** This describes an earlier package. The current version uses Couchbase as the only application database; follow [README](../../README.md) and [ARCHITECTURE](../../ARCHITECTURE.md).

# StreamAI for Capella — ready-to-configure package

Your original v2.1.3 application, with the Capella changes already applied. UI, Agent Memory and MCP run in Docker on your Apple Silicon Mac. Application data and persistent memory live in Capella.

- Chat: `mistralai/mistral-7b-instruct-v0.3`
- Embeddings: `nvidia/llama-3.2-nv-embedqa-1b-v2`, **2048 dimensions**

**No patches, Git, Xcode or Python installation are needed on your Mac.** Setup runs inside Docker. Your supplied Agent Memory ARM64 image and MCP package are included.

## Three steps

1. **Edit `capella.env`.** Fill in its eight `CHANGE_ME` values: cluster SDK address, database username/password, read-only MCP username/password, two model endpoint roots and one inference key valid for both models. Keep passwords/keys in single quotes. The model endpoints may be identical; a trailing `/v1` is normalized automatically.
2. **Save the cluster's public root CA certificate as `certs/capella-ca.pem`.** Download it from the target Capella cluster's Connect area. Startup mounts this certificate read-only for Agent Memory, the Agent Catalog publisher and the UI, and sets their certificate variables. Start Docker Desktop.
3. In Terminal, enter this extracted folder and run:

   ```bash
   bash START-CAPELLA.sh
   ```

Open **http://localhost:8089** after the success message. First startup downloads/builds dependencies and the Agent Catalog embedding model; later runs reuse build caches.

## Capella prerequisites

- An Operational cluster compatible with Agent Memory, currently **8.0.2 or later**, with Data, Query, Index and Search. [Official prerequisites](https://docs.couchbase.com/ai/build/agent-memory/get-started-agent-mem.html)
- Buckets **`streaming`** and **`agent_memory`**. A new, empty demo target is simplest.
- Allow your Mac's public outbound IP, including your VPN address if applicable. The SDK needs DNS and TLS service access. [Allowed IPs](https://docs.couchbase.com/cloud/clusters/allow-ip-address.html)
- For this demo, `CB_USERNAME` should have **Read/Write on both buckets**, including schema/index setup. This one identity serves the app, Memory, setup and publisher. MCP uses the separate read-only credential on `streaming`; schema inspection may need the corresponding metadata read permission. [Cluster credentials](https://docs.couchbase.com/cloud/clusters/manage-database-users.html)
- Both models deployed in Capella Model Service. Use an **inference key**, separate from database credentials and a Capella Management API key.
- Docker internet access for images, Python dependencies and the first Agent Catalog model download.

## What startup handles

The command checks the database and real model calls; creates scopes, collections and SQL++ indexes; provisions the two scoped 2048-dimension Search indexes; loads six fictional sample titles into an empty catalogue; generates embeddings and seeds demo viewers/entitlements when profiles and history are empty; builds the NVIDIA-compatible Memory image; starts Memory/MCP; publishes native Agent Catalog; builds the UI and checks readiness.

Existing data is retained on repeat startup. Existing Search replica/partition settings are preserved. If existing vectors use another model/dimension, setup stops with a migration message instead of mixing vectors. Use a fresh target for the quickest test.

For your TMDB catalogue on the first load, set `LOAD_SAMPLE_CATALOGUE=false` and fill `TMDB_READ_ACCESS_TOKEN`. Changing those values after data is loaded does not automatically replace the catalogue.

## Full-AIDP extensions

The default starts Capella data, Search, Model Service, Agent Catalog and Tracer, with local Memory and MCP. **AI Functions and managed Data Processing are opt-in**, because they require additional cloud provisioning.

For a managed workflow, configure `streaming.catalogue.titles`, source `embeddingText`, destination `embedding`, the NVIDIA model, passage mode and 2048 dimensions. Then set:

```dotenv
DATA_PROCESSING_MODE=capella_workflow
DATA_PROCESSING_WORKFLOW_ID=your-real-workflow-id
```

The loader writes source documents and setup waits for your workflow to generate vectors. Search and Eventing are required. The supplied Search indexes remain necessary. [Workflow setup](https://docs.couchbase.com/ai/build/vectorization-service/vectorize-structured-data-capella.html)

After enabling AI Functions and associating a model, set `AI_FUNCTIONS_ENABLED=true`. Check the support and cluster requirements in the [AI Functions guide](https://docs.couchbase.com/ai/build/ai-functions.html). Test an actual enrichment action after startup.

The familiar `.env.capella-full-aidp.example` is included. It selects these extensions and can be chosen with `CAPELLA_CONFIG`; the normal entry point reads `capella.env`. Setting a flag does not provision the cloud service.

## Stop and troubleshoot

```bash
bash STOP-CAPELLA.sh
bash CAPELLA-LOGS.sh
bash CAPELLA-LOGS.sh memory
```

| Symptom | Check |
| --- | --- |
| Database timeout | Allowed public IP, VPN, SDK hostname, DNS and TLS access |
| Database permission error | Cluster credential and schema/index privileges |
| Model 401/404 | Inference key, region, deployed model and endpoint root |
| Certificate error | Correct public CA saved as `certs/capella-ca.pem` |
| Plan-cache status reports no matching SQL++ index | Use r4 or later setup, which creates `ix_plan_cache_stats`; the vector Search index serves a separate query path |
| Agent Tracer has a session but no messages | Use r5 or later and rebuild the UI; earlier builds passed dictionaries to an SDK that requires typed Content objects |
| Incompatible existing vectors | Fresh target or deliberate re-embedding migration |
| Waiting for workflow vectors | Workflow is running and writes 2048 values into `embedding` |
| Port in use | Change `CAPELLA_UI_PORT`; memory/MCP ports are also configurable |
| Dependency download failure | Docker internet/proxy access, disk space and package registries |

The Compose project is `streamai-capella`. Host ports: **8089 UI, 8081 Memory, 8001 MCP**. The legacy `scripts/start-capella.sh` also delegates to the new startup command.

After startup, test semantic search, a model-planned request, memory write/recall in another session and a governed action. Health checks cannot prove plan/summary quality.

## Where traces and memory are stored

In this package's verified `agentc` 1.1.2 installation, native Agent Tracer events are in `streaming.agent_activity.logs`. Agent Catalog uses `streaming.agent_catalog.tools`, `prompts` and `metadata`. The demo also keeps its own operational trace projection in `streaming.recommendations.traces`.

Agent Memory uses the `agentmemory` scope of the `agent_memory` bucket: `users`, `sessions` and `memory`. Both short- and long-term blocks live in `memory`; `annotations.memory_scope` distinguishes them. Short-term blocks use `SHORT_TERM_TTL_SECONDS`; long-term writes request TTL 0 (subject to any bucket/collection maxTTL). Resumable UI state is also cached in `streaming.viewers.app_state`, and the operational viewer profile is in `streaming.viewers.profiles`.

The MCP proof button does not create conversational memory. Send a chat turn to create a short-term block; explicit preferences, likes/dislikes and preference edits can create long-term facts. Memory enrichment is asynchronous. Tracer's storage does not need to be moved to the memory bucket.

See `VALIDATION.md` for checks performed. The original general guide is preserved in `docs/ORIGINAL-README.md`; use this README for the Capella package.

## Scoped AI savings view (r9)

The AI Usage panel now measures routed inference from the app, Agent Memory, catalogue ingestion and setup. A private local gateway records requests and provider token reports in a persistent Docker volume and mirrors them to `streaming.telemetry.ai_metrics`. It starts before startup's model probes. Keep the real HTTPS endpoint roots in `capella.env`; Compose supplies the internal gateway URLs to the callers automatically.

The default view now shows only three headline figures: LLM calls avoided, LLM tokens saved and estimated cost saving. The visible badge identifies them as estimates against an assumed baseline. Details, source usage and editable assumptions are under **How this is calculated**, closed by default. Figures refresh every five seconds while this panel and browser tab are visible. Negative differences show extra usage; incomplete reporting remains visible.

The comparison uses the same r7 arithmetic. Its editable example assumes one LLM request per completed chat, 1,000 input and 200 output tokens; USD 2.50/10.00 per million LLM input/output tokens and USD 0.10 per million embedding input tokens. These are hypothetical assumptions, not model prices or measured savings. All routed background work is included on the observed side. Negative results show extra usage; missing token reports suppress the affected estimates. The comparison covers all viewers in the reporting window. Measured usage details remain below it.

Historical usage cannot be reconstructed. Capella Model Service uses capacity and clock-hour billing, so token counts are not a bill. Verified financial savings remain unmeasured. See `METRICS-AUDIT.md` for formulas, definitions, scope, exclusions and limitations.

Updating from any earlier package: extract the full r9 ZIP into a new folder, copy your existing `capella.env` and `certs/capella-ca.pem` into it, then run `bash START-CAPELLA.sh` there. The Compose project remains `streamai-capella`; its containers are recreated from the new folder. Existing Capella data and Docker volumes are retained. The new-window button retains raw usage history; never use Docker's volume deletion options to reset a demo if you need that history.

## Seeded preferences in Agent Memory (r7)

Previously, seeding only wrote the operational profile. Revision r7 mirrors its explicit preferences into a dedicated long-term Agent Memory session when you log in, seed/reset a persona, save the profile or change preferences in chat. The operational profile remains authoritative. Seeded like/watch history remains operational data; the mirror does not copy the catalogue or traces.

After upgrading, sign out and sign in again, or open **My Profile → Sync / retry Agent Memory**. The status shows verified stored facts, how many are ready for recall and the dedicated session ID. New writes may be accepted before they are visible as stored/ready; use the button again after enrichment to refresh the counts. Repeated sync reads existing facts before adding missing ones. It reconciles only its own mirrored records and preserves conversational memories. Background model requests are counted in AI Usage.

In Capella, inspect `agent_memory.agentmemory.memory`, filtering by the displayed session ID or annotation `source=streamai_operational_profile_sync`. Storage names assume the supplied configuration. The memory inspector now labels operational-only entries explicitly, so their presence is not mistaken for a stored block. See `START-HERE-r9.txt` for the quick upgrade steps.

## AI Functions proof and accounting

Enable Classification, Summarization and Sentiment Analysis against the Mistral LLM to use the existing manual enrichment proof. `AI-FUNCTIONS-GUIDE.md` contains exact configuration, the SQL++ query and how to verify the returned output. This proof does not yet persist enrichment or publish an Agent Tracer event.

AI Functions and managed Data Processing invoke models outside the local usage gateway. r9 keeps the scoped gateway comparison visible with these services enabled. The scope and exclusions are visible beside the three cards. It does not claim overall deployment savings. AI viewing-guide operations are audited separately; their unreported tokens/cost are unknown.

## Integrated AI viewing guides (r9)

Open a title and generate a short AI viewing guide with Capella classification, summarization and sentiment. The app displays the outputs, stores them in the title's `aiEnrichment` field, shows saved execution evidence, and reuses the summary in assistant title-information replies. Reopening/reusing an unchanged guide does not regenerate it. Changed source text invalidates the guide; parental/entitlement rules remain authoritative. See `AI-FUNCTIONS-GUIDE.md` for the presenter flow and storage details.
