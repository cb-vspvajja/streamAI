# Couchbase StreamAI v2.1.3

StreamAI is a presenter-ready television-streaming demonstration of Couchbase as the operational and AI data plane for a grounded, personalised agent.

Version 2 adds a complete **Showcase Studio** on top of the existing catalogue, Agent Memory, Agent Catalog, MCP, Agent Tracer, Search/Vector, entitlement and governed-action implementation.

## v2.1.3 showcase/customer experience modes

- The header includes a persistent switch between **Couchbase Showcase** and
  **Customer App** views. Existing installations start in Showcase mode.
- Customer App view retains Home, Films, TV Series, Continue Watching, My List,
  My Profile, catalogue Search, playback actions and the StreamAI Assistant.
- It hides AI Value, Agent Inspector, Showcase Studio, AI Data Plane status,
  memory/trace inspectors, retrieval diagnostics, technical rank badges and
  LLM/token metadata.
- The switch affects presentation only; it does not change catalogue, profile,
  entitlement, grounding or recommendation execution.

Configure the initial mode in `.env`:

```dotenv
# Presenter-friendly default; the viewer can switch between both experiences.
UI_EXPERIENCE_MODE=showcase
UI_EXPERIENCE_SWITCH_ENABLED=true
SHOWCASE_ENABLED=true
```

For a locked customer-facing deployment:

```dotenv
UI_EXPERIENCE_MODE=customer
UI_EXPERIENCE_SWITCH_ENABLED=false
SHOWCASE_ENABLED=false
```

The UI setting is not an authorization boundary. A production deployment
should also protect operator endpoints with its normal identity, role and API
gateway controls.

## v2.1.2 conversational recall tracing hotfix

- Fixes `What did I ask previously?` and `What did you just say?`, which could
  fail after Agent Memory recall succeeded because trace summarisation accessed
  catalogue-only state.
- Keeps previous-turn recall deterministic and LLM-free.
- Records bounded prior-turn evidence in the trace without mixing it with
  catalogue result titles.
- The complete regression suite contains 238 passing tests.

## v2.1.1 fast deterministic assistant hotfix

- Generic recommendations and structured catalogue requests are planned
  deterministically; they no longer wait for the planner LLM or a semantic
  plan-cache probe.
- Planner-model calls are reserved for requests the deterministic router cannot
  safely express, with a two-second time budget and honest attempted/failed
  telemetry.
- Exact plans are written through KV immediately. Eligible semantic
  vectorisation is promoted in the background after catalogue execution.
- Generic profile recommendations use FTS without generating an embedding.
  Free-form concepts still use Hybrid/Vector retrieval.
- Interactive Search has a 1.5-second budget and a 20-second circuit breaker,
  so an unavailable or warming index falls back to SQL++ once rather than
  delaying every turn.
- Search hits are hydrated with one KV multi-get and viewer interaction/history
  signals are read with one combined Query request.
- Ordinary catalogue browsing now applies disliked cross-genre exclusions.
  `Show me sci-fi movies` therefore excludes Sci-Fi/Horror titles when Horror
  is stored as a genre to avoid.
- Allowed purchase/rental titles can be recommended, while region, parental,
  exclusive-preference and availability denials remain excluded.
- A wider exact-profile SQL++ pool prevents valid catalogue matches from being
  missed when Search returns too few candidates.
- The complete regression suite contains 236 passing tests.

## v2.1.0 governed reasoning plans and semantic plan cache

- Uses the configured LLM as a constrained language planner, never as a source
  of catalogue facts. Its JSON output is validated against an allowlisted plan
  schema before execution.
- Stores validated plan templates in
  `recommendations.plan_cache`, separately from user/session-scoped Agent
  Memory.
- Reuses exact plans through KV and semantically equivalent plans through the
  `streaming-plan-cache` Vector Search index.
- Rebinds viewer, current date, relative periods, content type, title terms and
  other slots on every request. Catalogue records, profile state and
  entitlement are always read fresh.
- Rejects contextual/action plans and incompatible semantic candidates rather
  than allowing a high vector score to override hard constraints.
- Adds one governed catalogue-plan executor for title fields, release periods,
  ratings, genres, people, language, country, runtime and personalized
  retrieval.
- Correctly plans `show me 5 top rated movies`, `films released last year`,
  `what new movies will I like`, `movies that are sequals`, and `movies with
  War in the name`.
- Adds separate telemetry for planner and grounded-response LLM avoidance,
  exact and semantic plan-cache hits, validated plan reuses and planner tokens
  saved.
- The complete regression suite contains 227 passing tests.

## v2.0.10 natural grouped-analytics phrasing

- Corrects `How many movies are in each genre?`, which previously fell through to title Search.
- Treats `in each`, `in every`, `for each`, `for every`, `each … has` and `genre-wise` as aggregate grouping language.
- Produces the same validated, read-only analytics plan as `by genre` and `per genre`.
- Uses the SQL++-safe internal alias `metricValue`; `VALUE` is reserved and is never emitted as an alias.
- Adds regression coverage proving that these variants cannot route to catalogue recommendations.

## v2.0.9 grounded conversational context and catalogue analytics

- Recalls the previous user question or assistant answer from active or retained Agent Memory without asking the LLM to reconstruct it.
- Reads current My List state and recent additions from exact profile IDs and completed action receipts.
- Uses up to five exact My List or previous-result title embeddings as one normalised multi-seed vector query.
- Treats `these`, `those`, `the ones`, `it`, ordinals and `this list` as references to prior exact IDs, never as new Search text.
- Intersects availability, plan, watched, liked, My List, content-type and rating follow-ups with the previous grounded results; it cannot silently broaden the list.
- Supports collective governed actions such as `add them` and `add the ones missing in my list`, with exact-ID re-reads and the existing five-title safety bound.
- Adds the read-only `query_catalogue_analytics` Agent Catalog/MCP tool for allowlisted counts, average ratings and average runtimes by genre, year, language, country or content type.
- Routes natural profile recall such as `what do I like?` to the authoritative viewer profile.
- Adds a conversational regression matrix; the complete suite contains 217 passing tests.

## v2.0.8 multi-title ordinal actions

- Resolves `Add first 2 to my list`, `add first two to my list`, `the first three movies` and `both of them` against the immediately preceding grounded result list.
- Re-reads every selected catalogue record by exact title ID before any action.
- Applies entitlement and exclusive-preference policy independently to each selected title.
- Executes sequential viewer-profile writes, creates one action receipt per title and records per-title outcomes in Agent Tracer.
- Returns an explicit partial-success response when one title succeeds and another is denied or fails.
- Fails closed when the previous result list is too short, and limits one batch to five titles.
- Keeps playback single-title only.

## v2.0.7 database-boundary enforcement

- Grounds `Which is the highest rated movie?` and equivalent wording in the catalogue's stored rating data even when the user omits `in the catalogue`.
- Removes the unmatched-question LLM fallback from the viewer response path. A request without current-turn database evidence receives a deterministic boundary response instead of model-pretraining facts.
- Blocks external review scores such as Rotten Tomatoes, IMDb and Metacritic unless the source and value exist in the current catalogue record.
- Marks the decision in the trace as `database_boundary`, `modelCallBlocked=true` and `llmInvoked=false`.
- Passes the published Agent Catalog system prompt into any explicitly grounded internal model composition path.
- Adds regression tests proving that an empty evidence set cannot call the model.
- Repairs the truncated Showcase Studio stylesheet from the prior archive.

## v2.0.6 exclusive-preference policy enforcement

- Treats `I only like children movies` as an enforceable Family-only content policy, not merely a ranking hint.
- Filters conflicting titles from assistant discovery without silently applying the normal one-turn disliked-genre override.
- Applies the same deterministic decision in the direct SDK service, warm MCP adapter and published Agent Catalog entitlement tool.
- Rejects Like, My List and Play actions outside the exclusive policy before writing interaction, profile, history or action-receipt documents.
- Returns a clear viewer-facing conflict explanation and exposes the policy decision in the trace.
- Adds regression coverage for Horror search, Like and Play while confirming Family content remains available.

## v2.0.5 MCP catalogue-count hotfix

- Fixes `How many movies are in the catalogue?` and related movie, series and genre-count questions when MCP and Agent Catalog are required.
- Registers `get_catalogue_statistics` in the live MCP business-tool adapter.
- Runs the exact count as one read-only, scoped SQL++ aggregate through the existing warm MCP session.
- Adds regression coverage for filtered and unfiltered counts.

## v2.0.4 conversational and retrieval corrections

- `I only like children movies` is a deterministic exclusive preference update, not a catalogue browse.
- `children`, `kids` and `child-friendly` normalise to the authoritative `Family` genre.
- Exclusive positive content statements replace previous positive genres/themes while preserving negative and safety exclusions.
- Descriptive structured requests such as `atmospheric Science Fiction` use Hybrid Search first, then safely broaden to the exact genre inventory when optional literal terms are too narrow.
- Empty recommendation responses distinguish catalogue inventory from viewer-region, subscription and preference filtering.
- Viewer-facing assistant text is scrubbed of database/vendor implementation names; technical product names remain in presenter and inspector surfaces.

## What v2 demonstrates

| Capability | Demonstration |
|---|---|
| Data / KV | Viewer profile, title, entitlement, action receipt, experiment and evaluation documents |
| Query + Index | Watch history, session traces, profile snapshots and telemetry |
| Search / FTS | Exact titles, genres, people, keywords and structured filters |
| Vector Search | Mood, theme and conceptual retrieval over catalogue embeddings |
| Hybrid Search | A regular Search query and KNN query in one Couchbase Search request |
| Semantic plan cache | Exact KV and Vector Search reuse of validated reasoning plans |
| Agent Memory | Durable facts and conversation context reused across sessions |
| Agent Catalog | Git-versioned, published prompts and tools |
| MCP Server | Read-only Couchbase access through Streamable HTTP |
| Agent Tracer | Full session replay, tool inputs/results, policy and component path |
| Governed actions | Likes, dislikes, My List, progress and completion using exact title IDs |
| Entitlement policy | Region, tier, offer type, parental rating and viewer safety preferences |
| Showcase Studio | Guided presenter path, personas, Search Lab, evidence, governance, resilience and evaluation |
| Capella profile | Optional Model Service, AI Functions, managed Data Processing and native UI deep links |

## New Showcase Studio

Open **Showcase Studio** from the top navigation.

### Presenter

A guided start-to-finish demonstration with:

- Exact prompts and expected outcomes.
- Couchbase component badges for each step.
- One-click prompt execution.
- Executive, Family and Guest personas.
- A complete demo reset that retains the authenticated viewer but clears interaction, trace and metrics state.

### Search Lab

Runs the same query through FTS, Vector and Hybrid modes against Couchbase and compares:

- Requested and effective mode.
- Search latency.
- Candidate and result counts.
- Result-set overlap.
- Exact structured shortcuts.
- Fallbacks caused by a controlled fault.

Every comparison is persisted in:

```text
streaming.telemetry.experiments
```

### Recommendation Evidence Scorecard

Open any title card and click **Evidence scorecard**. The scorecard shows deterministic contributions from:

- Profile genre affinity.
- Theme and synopsis affinity.
- People affinity.
- Search or semantic relevance.
- Catalogue popularity and quality.
- Negative-preference violations.
- Entitlement decision.

The score is an explanation score for the demonstration, not a calibrated probability.

Each Search result also includes **How the search selected this title**:

- FTS identifies genuine field/term evidence, including synonym expansion such as `motorcycle` → `biker`.
- Vector shows the raw Couchbase Search score, relative-to-top score, a strong/moderate/weak band and direct catalogue corroboration when present.
- A Vector result with no visible term match is labelled as a nearest embedding neighbour rather than given invented field attribution.
- Hybrid distinguishes lexical-plus-vector evidence from a vector-only candidate.

A KNN search returns the nearest available vectors even when the catalogue has few strong matches. A result such as *Hoppers* for `motorcycle` can therefore appear as a weak semantic neighbour; v2.0.4 exposes that limitation directly on the card.

### Agent Catalog Governance

The Governance tab shows:

- Active catalogue identifier.
- Published tools.
- Active system prompt.
- Prompt and tool source hashes.
- A policy diff.
- Optional Capella Tools Hub, Prompts Hub and Agent Tracer links.

Agent Catalog versions and retrieves tools/prompts. The application runtime remains responsible for tool execution.

### Resilience Lab

The presenter can safely inject:

- MCP unavailable.
- Embedding timeout.
- Model unavailable.
- Search unavailable.
- Agent Memory unavailable.

These switches alter StreamAI request paths but do not stop Docker containers or damage Couchbase. Fallbacks remain explicit in traces:

- Embedding timeout → FTS.
- Search outage → SQL++ catalogue fallback.
- MCP outage → governed direct SDK fallback.
- Model outage → deterministic safety response.
- Agent Memory outage → user response completes, persistence failure is captured in telemetry.

### Evaluation Dashboard

Runs representative grounding and routing checks and persists each result in:

```text
streaming.telemetry.evaluations
```

The suite covers profile-question routing, ordinal actions, entitlement-title extraction, exact-genre shortcuts, semantic intent and live catalogue/Search availability.

### Policy and Data

- Opening any title card automatically selects it for the entitlement simulator.
- The simulator displays inline validation, progress, baseline and what-if decisions inside the modal.
- Entitlement what-if simulation without mutating the authoritative profile.
- Supporting Couchbase document inspection with sensitive values redacted.
- Viewer-profile time travel using before/after snapshots stored in `streaming.telemetry.profile_snapshots`.

### Capella v2

The Capella tab distinguishes configured managed services from local adapters and exposes optional links to:

- Agent Tracer.
- Tools Hub.
- Prompts Hub.
- Model Service.
- Capella console.

Native AI Functions and managed Data Processing remain Capella-only. The local profile does not claim to execute them.

---

# Fast local start

## Requirements

- Docker Desktop or Docker Engine.
- Couchbase-provided Agent Memory image for the host architecture.
- Bash, `curl` and Python 3.
- Native Ollama is recommended on Apple Silicon; containerised Ollama is supported.
- Native package pins: `couchbase-mcp-server==1.0.0.post1` and `agentc>=1.1.0,<1.2`.

## Start

```bash
unzip couchbase-stream-ai-v2.1.3.zip
cd couchbase-stream-ai-v2.1.3
cp .env.local.example .env
./scripts/06-start-all.sh
```

Open:

```text
http://localhost:8088
```

Default demo login:

```text
Login ID: sai
PIN: 1234
```

Create the viewer in the UI on the first run when it does not already exist.

## Agent Memory image

When the image is already loaded as `agentmemory-server:latest`, v2 discovers it and creates the stable architecture tag automatically. It can also be set explicitly:

```dotenv
AGENT_MEMORY_IMAGE=agentmemory-server:latest
```

Or load a Couchbase-provided tar:

```dotenv
AGENT_MEMORY_IMAGE_TAR=/absolute/path/to/agentmemory-server-arm64.tar
AGENT_MEMORY_AUTO_LOAD=true
```

## Real TMDB catalogue

The archive does not contain a TMDB credential. Set a rotated TMDB API Read Access Token in `.env`:

```dotenv
TMDB_READ_ACCESS_TOKEN=<token>
LOAD_SAMPLE_CATALOGUE=false
TMDB_LANGUAGE=en-GB
TMDB_REGION=GB
TMDB_MOVIE_LIMIT=120
TMDB_TV_LIMIT=60
```

Then run:

```bash
FORCE_CATALOGUE_RELOAD=true ./scripts/06-start-all.sh
```

Do not commit or circulate `.env`.

## Verify

```bash
./scripts/07-verify-demo.sh
```

The script checks the UI, Agent Memory, Couchbase MCP Server, Agent Catalog, Agent Tracer, Search/Vector and model runtime.

---

# Upgrade from v2.1.0

```bash
unzip couchbase-stream-ai-v2.1.3.zip
cd couchbase-stream-ai-v2.1.3
cp ../couchbase-stream-ai-v2.1.0/.env .env
./scripts/06-start-all.sh
```

The application version is part of the plan-cache key, so v2.1.0 plans cannot
silently preserve the old routing behaviour. Existing catalogue, profile,
history and Agent Memory data are reused.

For the new latency controls, optionally add these values to a copied `.env`:

```dotenv
ASSISTANT_PLANNER_TIMEOUT_SECONDS=2.0
PLAN_CACHE_SEMANTIC_PROBE_TIMEOUT_SECONDS=0.75
SEARCH_INTERACTIVE_TIMEOUT_SECONDS=1.5
SEARCH_CIRCUIT_BREAKER_SECONDS=20
```

# Upgrade from v2.0.10

```bash
unzip couchbase-stream-ai-v2.1.3.zip
cd couchbase-stream-ai-v2.1.3
cp ../couchbase-stream-ai-v2.0.10/.env .env
unset FORCE_CATALOGUE_RELOAD
./scripts/06-start-all.sh
```

Content-plane provisioning creates these additional collections and indexes:

```text
streaming.telemetry.showcase_state
streaming.telemetry.experiments
streaming.telemetry.evaluations
streaming.telemetry.profile_snapshots
streaming.recommendations.plan_cache
streaming-plan-cache (scoped Vector Search index)
```

Existing catalogue, embeddings, profiles and Agent Memory data are reused.

---

# Deployment profiles

## Local

```bash
cp .env.local.example .env
```

Uses local Couchbase Enterprise, Agent Memory, MCP, Agent Catalog and Ollama/OpenAI-compatible providers.

## Existing Couchbase Server

```bash
cp .env.server.example .env
```

Use separate provisioning, application, MCP, Agent Memory and Agent Catalog identities.

## Capella Operational

```bash
cp .env.capella.example .env
```

Set `SEARCH_TRANSPORT=sdk` and configure TLS, application credentials and Search index.

## Full Capella AI Data Plane

```bash
cp .env.capella-full-aidp.example .env
```

In addition to the operational cluster, configure the Model Service (including optional semantic cache), AI Functions, managed Data Processing workflow and optional deep links:

```dotenv
CAPELLA_AGENT_TRACER_URL=
CAPELLA_TOOLS_HUB_URL=
CAPELLA_PROMPTS_HUB_URL=
CAPELLA_MODEL_SERVICE_URL=
```

AI Functions require a paid Capella operational cluster with AI Functions enabled and associated with a model. Managed workflows must already exist in Capella.

---

# Search behaviour

## FTS

Best for exact terms, titles, people, genres and filters.

## Vector

Embeds free-form conceptual language and performs KNN retrieval against catalogue embeddings.

## Hybrid

Sends both a lexical `query` and `knn` request to Couchbase Search. Structured filters are also attached as KNN pre-filters.

## Auto shortcut

Known genres and other authoritative structured requests skip embeddings. The trace displays, for example:

```text
Requested Hybrid → effective FTS
Reason: structured_filter_only
```

The card percentage is a relative display rank assigned after final sorting and viewer exclusions. The raw Search score remains available in traces.

---

# Security boundaries

- MCP uses a dedicated read-only identity.
- Validated writes remain in the StreamAI business service.
- Presenter fault injection is application-level and does not change cluster configuration.
- Supporting-document views redact passwords, tokens, secrets, PIN material and embeddings.
- Demo PIN authentication is not a production identity solution; use OIDC in production.

# Validation

The packaged source is validated with:

```bash
pytest -q
python3 -m compileall -q ui tools scripts
bash -n scripts/*.sh agent-catalog-publisher/publish.sh
node --check ui/static/app.js
```

Docker and proprietary Agent Memory execution must be validated on the target machine because the image is not included in this archive.
