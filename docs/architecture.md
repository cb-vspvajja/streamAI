# Couchbase StreamAI v2.1.3 Architecture

## Governed planning and plan reuse

The assistant separates language reasoning from operational facts:

1. Agent Memory supplies viewer/session context and exact previous result IDs.
2. The planner checks the versioned KV plan cache.
3. Deterministic profile, genre, rating, date and title-field requests execute
   immediately without an embedding or planner-model call.
4. Free-form requests get a bounded semantic cache probe; a candidate is reused
   only after guard compatibility and slot rebinding.
5. When no deterministic plan exists, the configured LLM returns constrained
   JSON within a bounded time budget, which is validated
   against the application plan schema.
6. `query_catalogue_plan` executes against current catalogue, viewer and
   entitlement data through Query, FTS, Vector and KV.
7. The viewer response may name only title IDs returned by that execution.

Exact plan documents are written synchronously through KV. Eligible query
embeddings are added in the background after execution, so plan-cache
population cannot delay the viewer response.

Agent Memory does not store shared plan templates. It remains scoped to durable
viewer facts and conversation state, while the plan cache has independent TTL,
planner/tool-schema versions and reuse telemetry.

## Logical architecture

```text
Browser / Presenter
        |
        v
StreamAI FastAPI + static UI
        |
        +-- deterministic intent and policy router
        +-- governed write service
        +-- Showcase Studio
        |
        +------------------------+----------------------+------------------+
        |                        |                      |                  |
        v                        v                      v                  v
Agent Catalog              Couchbase MCP         Search/Vector      Model provider
prompt/tool lookup          read-only transport   direct hot path     Ollama/Capella/
+ Agent Tracer spans        KV / SQL++ / schema   FTS + KNN           compatible API
        |                        |                      |                  |
        +------------------------+----------------------+------------------+
                                 |
                                 v
                         Couchbase operational data

streaming.catalogue.titles
streaming.viewers.profiles
streaming.viewers.watch_history
streaming.viewers.interactions
streaming.operations.entitlements
streaming.operations.action_receipts
streaming.recommendations.traces
streaming.telemetry.ai_metrics
streaming.telemetry.showcase_state
streaming.telemetry.experiments
streaming.telemetry.evaluations
streaming.telemetry.profile_snapshots
```

## Agent Memory

Agent Memory stores short-term conversation state and durable facts. The structured viewer profile remains the low-latency operational source for deterministic preference, policy and recommendation decisions. The UI combines both views in the Memory Inspector.

Explicit conversation-recall questions first read the active transcript. If a new session has no local turns, the same tool performs one bounded Agent Memory list operation and selects the newest retained conversation block. The LLM is never asked to reconstruct a prior question or answer.

## Conversational evidence resolver

Follow-ups are resolved against typed evidence rather than being appended to a Search query:

| Reference | Authoritative evidence | Operation |
|---|---|---|
| `what did I ask before?` | Active transcript, then Agent Memory | Return the latest prior user turn |
| `what did you add recently?` | Completed action receipts | Hydrate exact title IDs in event-time order |
| `similar to ones in My List` | Current profile watchlist IDs | Mean-normalised embedding centroid, then one KNN query |
| `the ones I can watch` | Previous result IDs + entitlement | Strict set intersection |
| `add the ones missing` | Previous result IDs − current watchlist IDs | Bounded exact-ID batch write |
| `who directed the second one?` | Previous result ID at ordinal 2 | Exact KV read, then catalogue-field answer |

The resolver never converts `these`, `it`, `the ones` or `this list` into lexical catalogue terms. Missing context produces a deterministic no-context response.

## Agent Catalog and MCP

Agent Catalog provides versioned tools and prompts. It does not execute tools. StreamAI resolves the catalogued business tool and normally executes its read-only data operation through the Couchbase MCP Server. Validated writes remain inside StreamAI so title identity, entitlement and policy cannot be bypassed.

`query_catalogue_analytics` exposes a non-SQL input schema. It accepts only three measures (`count`, `average_rating`, `average_runtime`), five dimensions (`genre`, `release_year`, `original_language`, `country`, `content_type`), bounded filters and a maximum of 50 groups. The MCP adapter maps that validated plan to parameterised, scoped, read-only SQL++; natural-language text cannot supply SQL or identifiers.

## Database boundary

Viewer responses follow a fail-closed evidence contract:

1. Deterministic intent routing identifies catalogue, viewer, entitlement or action requests.
2. Governed reads return current-turn evidence and exact title IDs.
3. Deterministic renderers answer known factual and policy questions.
4. If no current-turn evidence can answer the request, StreamAI returns a database-boundary response and blocks the model call.

The viewer-facing model path cannot answer from model-pretraining knowledge. External ratings, reviews and other third-party facts are available only when they are explicitly stored in a returned catalogue record. Traces expose `modelCallBlocked`, `llmInvoked` and the `database_boundary` route.

## Multi-title governed actions

Bounded references such as `first two` are resolved only against `resultTitleIds` from the immediately preceding grounded catalogue request. The runtime accepts at most five titles in one batch and does not allow multi-title playback.

For each selected ID, StreamAI:

1. Re-reads the exact catalogue record.
2. Applies entitlement and exclusive-preference policy.
3. Performs the validated mutation sequentially to avoid viewer-profile update races.
4. Writes an independent action receipt.
5. Adds the title-level outcome to the agent trace.

A batch may partially succeed. The response and trace identify completed, denied, unresolved and failed titles without rolling an allowed action back or concealing a denial.

## Search routes

- FTS: field-aware lexical and structured retrieval.
- Vector: KNN over title embeddings.
- Hybrid: regular Search query and KNN in one request.
- SQL++ fallback: correctness-preserving structured fallback when Search is unavailable or warming.

Known structured concepts such as exact genres can bypass embedding generation.

## Showcase Studio

### Presenter state

The presenter workflow is defined in application code and scenario state is stored in `telemetry.showcase_state`.

### Search experiments

Search Lab executes live retrieval and stores summary metrics in `telemetry.experiments`.

### Profile time travel

Profile changes write snapshots before and after governed interactions or preference updates. These are stored in `telemetry.profile_snapshots` and are not used as the authoritative profile.

### Evaluation

Evaluation runs deterministic parser checks and live catalogue/Search health checks. Results are stored in `telemetry.evaluations`.

### Fault injection

Faults are application-level flags. They do not stop Couchbase, Agent Memory, MCP or model containers. This makes the demo repeatable and safe.

## Capella profile

The same application can use:

- Capella Operational for Data, Query, Index and Search.
- Agent Memory, Agent Catalog, MCP and Agent Tracer.
- AI Data Plane Model Service for managed LLM/embedding endpoints and caching.
- AI Functions through SQL++.
- Managed Data Processing workflows for ingestion and vectorisation.

AI Functions and managed workflows require Capella-side configuration and remain disabled in the local profile.
