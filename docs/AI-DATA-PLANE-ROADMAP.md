# StreamAI AI Data Plane Roadmap

## v2.1.3 — showcase/customer experience modes

- Preserve the full Couchbase showcase while adding a clean customer-facing
  streaming experience.
- Make the UI mode switch persistent, configurable and lockable per deployment.
- Keep grounded execution identical while removing operator panels and
  technical response metadata from customer presentation.

## v2.1.2 — conversational recall tracing

- Keep previous-question and previous-answer recall deterministic and LLM-free.
- Summarise Agent Memory tool output without assuming catalogue result fields.
- Preserve bounded prior-turn evidence in Agent Tracer.

## v2.1.1 — latency-bounded governed planning

- Deterministic requests bypass planner and semantic-cache model work.
- Semantic plan vectorisation is promoted after response-critical execution.
- Generic profile recommendations avoid embeddings; semantic concepts retain
  Hybrid/Vector retrieval.
- Search failure opens a short circuit and uses immediate SQL++ fallback.
- Multi-get hydration and one combined viewer-signal query reduce round trips.
- Cross-genre exclusions and allowed non-included offers are handled
  consistently.

## v2.1.0 — governed planning and semantic plan reuse

- Constrained LLM planning with application-side schema validation.
- Exact KV and semantic Vector Search plan cache.
- Safe slot rebinding and current-data execution.
- Separation of shared plan cache from viewer/session Agent Memory.
- Planner/cache telemetry, trace evidence and presenter workflow.

## v2.0.10 — natural analytics grouping

- Recognize `each`, `every` and `…-wise` as grouped-aggregation language.
- Keep all variants on the same allowlisted Agent Catalog/MCP analytics tool.

## v2.0.9 — grounded conversational context and analytics

- Resolve conversational references to exact prior-turn IDs or retained Agent Memory blocks.
- Enforce set intersections for follow-up filters so context cannot silently widen into an unrelated Search.
- Generate one multi-seed vector from exact My List or prior-result embeddings.
- Query governed catalogue aggregates through an allowlisted Agent Catalog/MCP analytics tool.
- Trace memory source, hard candidate boundaries, selected IDs and read-only analytics execution.

## v2.0.8 — multi-title governed actions

- Resolve bounded ordinal ranges from prior grounded result IDs.
- Apply identity, entitlement, viewer policy, mutation and receipt rules per title.
- Expose partial outcomes as deterministic trace evidence.

## v2.0.7 — database-boundary enforcement

- Ground common highest-rated questions in the catalogue even when the user does not explicitly say `in the catalogue`.
- Prevent model-pretraining knowledge from entering a viewer response without current-turn database evidence.
- Expose blocked model calls as deterministic, auditable trace decisions.

## v2.0.6 — exclusive preference policy enforcement

- Enforce explicit `only` preferences across discovery, entitlement and governed writes.
- Reconcile legacy likes and My List entries against the exclusive allow-list on upgrade.
- Expose the same policy outcome in search traces, entitlement responses and viewer-facing explanations.

## v2.0.5 — MCP catalogue statistics hotfix

- Register and execute the governed `get_catalogue_statistics` tool through the warm MCP runtime.

## v2.0.4 — complete showcase

Delivered the local-first and Capella-ready showcase across operational data, memory, tools/prompts, MCP, tracing, lexical/vector retrieval, policy, actions, explainability, resilience and evaluation.

## Next production increments

- Replace demo authentication with OIDC and enterprise identity mapping.
- Add event-driven recommendation invalidation using Capella Eventing or an external event bus.
- Add Capella Analytics/Columnar cohort and experiment analysis.
- Add real prompt/tool promotion workflows with approval and rollback.
- Add calibrated offline recommendation evaluation datasets.
- Add Couchbase Lite offline profile/history and local Search for edge clients.
- Add Kubernetes deployment manifests, autoscaling and production observability.

## Highest-value demo increments

Build these as one production-proof loop instead of isolated feature screens:

1. **Fresh content to grounded answer** — ingest a new title, preprocess and vectorize it, then prove it is searchable without an application reindex release.
2. **Policy and action assurance** — replay golden allowed and denied journeys, including the exclusive Family/Horror case, and report a zero policy-violation rate.
3. **Observable economics** — show p50/p95 latency, memory/cache hit rate, LLM calls avoided, tokens saved and estimated cost per successful journey.
4. **Controlled agent change** — promote a prompt or tool version, expose its trace/evaluation delta, then roll it back.
5. **Closed-loop improvement** — analyze cohort outcomes, change ranking or policy configuration and demonstrate the measured effect without moving operational data into another serving stack.

The strongest executive narrative is: one governed data plane takes a title from ingestion through retrieval, memory, policy, action, trace, evaluation and rollback, while the application remains responsive and the system of record stays authoritative.
