# Changelog

## 1.1.0 — 28 September 2026

- Selective optional summaries preserve short-turn originals and embeddings while avoiding redundant generative extraction.
- Execute previous/selective policies on identical recent chat content, with native model-call correlation and ready-content verification.
- Replace hypothetical headline savings with observed differences and a token-cost equivalent, including embeddings/background work.
- Persist receipts in Couchbase; suppress incomplete comparisons and preserve negative results.
- Add savings instructions, reconstruction prompt and matching UI references. Preserve Couchbase-only application persistence.


## 1.0.0 — 28 September 2026

Stable Capella demo baseline, based on the validated 2.1.3 Capella r9 package. The application version restarts at 1.0.0; database data and vendor component versions are preserved.

- Replaced the local usage database with Couchbase as the only application database. Usage events, reporting-window state, assumptions and control audit records persist directly in Capella with durable KV writes and request-plus reads. Removed the usage data volume and mirror worker.
- Added telemetry-first provisioning and verified adoption of history from an existing running meter.
- Added storage failure, restart, CAS, namespace, upgrade and regression tests.
- Comprehensive README, startup-to-request architecture, storage map, configuration and operating guide.
- Persisted AI viewing guides with classification, summary and synopsis tone; visible execution details and assistant reuse.
- Measured routed model usage including background Agent Memory work, with separate hypothetical savings and explicit cloud-service exclusions.
- Idempotent profile-preference mirror into long-term Agent Memory, native trace messages and governed plan reuse.
- StreamAI-only header wordmark.
- Stop command shuts down the usage gateway after the app, Memory and MCP.
- Application version aligned across runtime, image tags, prompt metadata and browser assets.
- Reduced Memory inference-probe frequency to 300 seconds (configurable, 600-second cache TTL) and corrected Mistral health probes to use a supported one-output-token limit. All probes remain metered.
- Legacy profile and wording regression checks updated to the effective Capella configuration.

This download is package revision `couchbase-ledger-r1`, with application version retained at 1.0.0. The existing agent features and comparison formulas are preserved; metering persistence, startup and related documentation now use Couchbase exclusively. Refer to VALIDATION.md for the tested scope and limitations.

## Historical releases

The following entries refer to the original release series before the version reset.


## 2.1.3

- Added a persistent Showcase/Customer experience switch in the application
  header; the existing complete Couchbase showcase remains the default.
- Customer App view keeps the streaming catalogue, profile, My List, playback
  and assistant while hiding AI Value, Agent Inspector, Showcase Studio, data
  plane status, memory/trace inspectors and search diagnostics.
- Removed technical ranking badges, retrieval traces and LLM/token metadata
  from the customer-facing presentation without changing grounded execution.
- Added `UI_EXPERIENCE_MODE` and `UI_EXPERIENCE_SWITCH_ENABLED` deployment
  controls. `SHOWCASE_ENABLED=false` now avoids starting Showcase Studio.
- Added bounded `/api/ui-config` startup configuration with safe fallback when
  an invalid mode is supplied or Showcase Studio is disabled.
- Added experience-mode regression coverage; complete suite: 244 passing tests.

## 2.1.2

- Fixed conversational recall tracing for `What did I ask previously?` and
  `What did you just say?`.
- Prevented the trace summarizer from reading catalogue-result state when a
  tool returned an Agent Memory record instead of title results.
- Kept recalled user and assistant messages as bounded trace evidence while
  keeping catalogue title summaries scoped to actual result lists.
- Added regressions for memory-only trace output and catalogue-result trace
  output; complete suite: 238 passing tests.

## 2.1.1

- Removed planner-model and semantic-cache waits from generic recommendations
  and deterministic structured catalogue requests.
- Added bounded planner and semantic-probe time budgets plus accurate
  attempted/failed model telemetry.
- Moved semantic plan-cache vectorisation to background promotion after fresh
  catalogue execution.
- Removed embeddings from generic profile recommendations while preserving
  Hybrid/Vector retrieval for free-form semantic concepts.
- Added an interactive Search deadline and circuit breaker with immediate
  SQL++ fallback after a Search failure.
- Replaced serial Search-hit KV hydration with one multi-get and combined
  viewer interaction/history hydration into one Query.
- Applied stored exclusions to ordinary cross-genre browsing and preserved the
  explicit one-turn disliked-genre override.
- Kept allowed purchase/rental recommendations while filtering real policy and
  availability denials.
- Added a wider exact-profile fallback pool for sparse Search candidate sets.
- Added nine latency/policy regressions; complete suite: 236 passing tests.

## 2.1.0

- Added a governed structured planner that uses the configured LLM for
  language interpretation while keeping catalogue facts and execution inside
  Couchbase.
- Added `recommendations.plan_cache` for reusable, versioned plan templates.
- Added exact KV plan-cache lookup and semantic reuse through the
  `streaming-plan-cache` Vector Search index.
- Added conservative cache validation, slot rebinding, relative-date
  resolution, schema-version invalidation and contextual/action exclusions.
- Added the `query_catalogue_plan` governed tool and fresh execution for title
  fields, release periods, rating order, structured filters, semantic concepts
  and personalized recommendations.
- Corrected top-N ratings, last-year releases, new personalized movies, sequel
  spelling, and title-contains versus genre routing.
- Added planner/cache metrics and UI trace labels for exact/semantic reuse,
  similarity, planner calls and tokens saved.
- Added local and Capella configuration for the planner, cache collection,
  semantic threshold, TTL and plan-cache Search index.
- Added ten planner/cache regression tests; the complete suite now contains
  227 passing tests.

## 2.0.10

### Natural grouped-analytics phrasing

- Routes `How many movies are in each genre?` to `query_catalogue_analytics` with `count`, `genre` and `movie` filters.
- Supports `each`, `every`, `for each`, `for every`, `in each`, `in every` and `genre-wise` grouping variants.
- Prevents these aggregate questions from falling through to title Search.
- Replaces the reserved SQL++ alias `value` with `metricValue` in the direct SDK, MCP adapter and published Agent Catalog tool.
- Complete suite: 217 passing tests.

## 2.0.9

### Grounded conversational context

- Added deterministic routing for prior questions/answers, current My List, recent My List receipts, prior-result repetition and contextual filters.
- Pronouns, ordinals and collective references now resolve to exact IDs from the immediately preceding grounded result set.
- Added strict previous-result intersections for playable, available, watched, unseen, liked, My List, content-type and highest-rated follow-ups.
- Added exact-ID collective actions, including `add them` and `add the ones missing in my list`, with the existing five-title bound.
- Added multi-seed similarity over up to five exact My List or previous-result embeddings.
- Added cross-session fallback recall from retained Agent Memory conversation blocks.
- Routed generic preference questions such as `what do I like?` to the structured viewer profile.

### Catalogue analytics

- Added the read-only `query_catalogue_analytics` Agent Catalog business tool and warm MCP adapter.
- Supports allowlisted count, average-rating and average-runtime measures grouped by genre, release year, original language, country or content type.
- Uses parameterised, scoped SQL++ only; user text cannot become SQL syntax or arbitrary identifiers.
- Corrected `How many movies are in the catalogue by genre?` so it returns a grouped aggregate instead of title recommendations.

### Verification

- Added ten focused contextual/analytics regressions and expanded related coverage.
- Complete suite: 217 passing tests.

## 2.0.8

### Multi-title ordinal actions

- Resolves numeric and worded prefix selections from the immediately preceding grounded result IDs.
- Supports bounded batches for Likes, My List, My List removal and Not Interested actions.
- Re-reads, policy-checks, writes, receipts and traces each exact title independently.
- Reports all-success, partial-success, policy-denied and failed outcomes without using the LLM.
- Rejects insufficient result sets and requests above five titles without searching the ordinal phrase as a catalogue title.
- Keeps playback restricted to one resolved title.
- Adds seven focused regressions; the complete suite now contains 207 passing tests.

## 2.0.7

### Database-boundary enforcement

- Routes highest-rated movie, film, series, show and title questions to the deterministic catalogue-rating query even when `in the catalogue` is omitted.
- Removes the unconstrained viewer-facing model fallback for unmatched requests.
- Returns a deterministic no-evidence response when current-turn catalogue or viewer data cannot answer the request.
- Refuses external review-score claims unless the source and value are stored in the current catalogue.
- Records the boundary decision and model-call block in the agent plan, retrieval trace and usage metrics.
- Forwards the active Agent Catalog system prompt to explicitly grounded model composition calls.
- Adds focused regressions proving that empty evidence cannot invoke a provider.
- Repairs the truncated Showcase Studio stylesheet shipped in the prior archive.

## 2.0.6

### Exclusive preference enforcement

- Promotes `I only like …` from stored profile metadata to a deterministic content allow-list.
- Enforces that allow-list in Search/recommendation filtering, direct entitlement, the warm MCP adapter and the published Agent Catalog tool.
- Blocks Like, My List and Play before any write when the resolved title conflicts with the exclusive policy.
- Reconciles legacy likes and My List entries on upgrade so an earlier out-of-policy action is not left visible.
- Keeps factual watch history for audit while suppressing out-of-policy titles from the resumable Continue Watching surface.
- Preserves the existing one-turn override for ordinary disliked genres, but never applies it to an exclusive allow-list.
- Adds explicit policy evidence to traces and clear viewer-facing conflict replies.
- Adds ten focused regressions covering Horror discovery/actions, upgrade reconciliation and allowed Family content.

## 2.0.5

### MCP catalogue statistics

- Registered the published `get_catalogue_statistics` business tool in the live MCP gateway.
- Executes movie, series, genre and full-catalogue counts as a single read-only scoped SQL++ aggregate through the warm MCP session.
- Added runtime regression coverage for filtered and unfiltered catalogue counts, including the exact failing business-tool path.

## 2.0.4

### Preference routing

- Treats `I only like children movies` as an exclusive durable preference update.
- Normalises children, kids and child-friendly wording to the catalogue's `Family` genre.
- Replaces earlier positive genres/themes for explicit `only` statements while preserving negative and safety exclusions.

### Structured semantic retrieval

- Corrected descriptive genre requests such as `atmospheric science-fiction film`.
- Hybrid Search remains the primary route; when optional descriptive terms are too narrow, SQL++ safely broadens to the authoritative genre/content-type inventory.
- Empty responses now distinguish a genuinely empty catalogue category from titles excluded by region, subscription or viewer policy.
- Normalises trailing punctuation out of semantic residual terms.

### Consumer language

- Added server and browser safeguards that remove vendor/database implementation names from viewer-facing assistant messages.
- Technical product naming remains available in the AI Data Plane Inspector, Governance Studio and presenter surfaces.

## 2.0.1

### Search evidence

- Added per-result evidence narratives to Search Lab cards and title details.
- FTS results now identify the catalogue field and expanded term that matched, such as `motorcycle` → `biker` in an overview.
- Vector results now show the raw Couchbase Search score, relative position within the selected mode, confidence band and whether any visible catalogue field corroborates the query.
- Weak KNN neighbours with no direct query-term evidence are labelled explicitly instead of being presented as obvious matches.
- Hybrid results now distinguish lexical-plus-vector matches from vector-only candidates admitted by the KNN side of the combined result.

### UI fixes

- Made the title-detail dialog vertically scrollable so the complete Evidence Scorecard is reachable on laptop and smaller displays.
- Added a dedicated Search evidence section to each selected title.
- Added Search evidence to the Evidence Scorecard response and UI.

### Policy and Data fixes

- Auto-populates the entitlement simulator from the most recently opened title card.
- Added visible inline validation, loading, success and error states inside the Policy & Data dialog.
- Fixed the apparent no-op when no title was selected or the API returned an error; those messages were previously hidden behind the modal top layer.
- Shows the baseline and what-if region, plan and parental-rating decision without mutating the authoritative viewer profile.

## 1.6.1

- Corrected final-rank percentages after business sorting.
- Corrected lexical versus semantic evidence display.
- Added deterministic named and ordinal liked-title actions.
