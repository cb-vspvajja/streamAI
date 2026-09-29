# Measured Agent Memory savings — v1.1.0

This release changes real work, as well as the explanation. Short conversation turns and explicit facts no longer require an additional generative summary. Originals and their embeddings remain in native Agent Memory. Longer turns still request summaries. Operational preferences are reconciled idempotently rather than copied as new facts on every turn.

## What changed

The default MEMORY_SUMMARY_POLICY is selective. A conversation pair with at most 4,000 combined characters is written with context_required=False. Longer pairs, or the optional always policy, request summarization. Explicit facts and profile mirrors use context_required=False because their source is already a concise, authoritative fact.

The native Memory server still creates embeddings and persists original content. Skipping optional summarization can alter summary-derived retrieval; identical originals do not establish identical relevance or quality. The native async_processing flag only changes completion/wait behaviour; it does not eliminate inference.

## How the comparison runs

1. The presenter explicitly chooses Compare memory policies. Chatting alone does not manufacture a savings baseline.
2. The app snapshots the latest two to four eligible complete short turns and up to six explicit facts. It does not truncate text to fit.
3. It creates an isolated native Memory user named streamai-cost-trial- followed by a random run ID, and two empty sessions. Phase order varies between runs.
4. Previous policy writes those originals with optional summarization enabled. Selective policy writes them with optional summarization disabled. Both perform real, resource-consuming model work.
5. The native Memory image propagates run/phase IDs through embedding and summary requests. The gateway records them in Couchbase and requests cache bypass for trial inference. Private correlation headers are not forwarded to the provider.
6. Synchronous processing establishes a completion boundary. The app reads blocks back and checks exact originals, multiplicity, count and ready status. A SHA-256 workload fingerprint is recorded; conversation text is not copied into the usage ledger.
7. It removes only its temporary user and records cleanup status. Normal viewer memories are untouched.

This is an executed test of two Memory processing policies, not an independently implemented non-AIDP application. It measures ingestion, not chat generation, retrieval quality, infrastructure or overall product economics.

## Headline definitions

| Card | Calculation |
| --- | --- |
| LLM calls avoided | Recorded previous-phase LLM HTTP attempts minus selective-phase attempts |
| LLM tokens saved | Previous-phase provider-reported input + output minus selective-phase input + output |
| Token-cost equivalent saved | Previous-phase reference cost minus selective-phase reference cost, including embeddings |

Reference cost = (LLM input tokens × input rate + LLM output tokens × output rate + embedding input tokens × embedding rate) / 1,000,000.

Default reference rates are USD 2.50/million LLM input, USD 10.00/million LLM output and USD 0.10/million embedding input tokens. Rates are editable and snapshotted per trial; changing them does not rewrite earlier evidence. These are illustrative rates, not Capella prices or measured invoice savings. [Capella billing](https://docs.couchbase.com/cloud/billing/billing.html) describes Model Service compute/capacity and clock-hour billing.

Negative differences remain visible as extra usage. There is no minimum saving, clamp-to-zero or invented token count. Zero selective-phase LLM work is valid only with complete original-content and embedding evidence.

## Accounting boundaries

Each phase includes its tagged model requests, retries and untagged Memory background requests during that interval. Health probes are counted. Outside-phase work is shown separately, along with total experiment usage. Running the experiment consumes both policies' resources; a positive difference is not money recovered by running the test.

The UI pauses its own mutations during a trial. Other callers/containers are not globally locked. Detected overlapping app/setup/ingestion requests, another trial or AI Functions generation invalidates the comparison. Same-service Memory background work is included and can reduce or reverse the saving.

Unknown/failed/in-flight requests, missing usage, absent correlation, incomplete readback, missing previous-policy LLM work or late tagged calls suppress savings. A gateway restart marks a running trial failed. An interrupted UI run may require a gateway restart or expiry of the stale-trial interval before retrying.

## Other metrics and persistence

The deployment view still shows every routed source in its reporting window: app, Memory, setup, ingestion and diagnostics. Each retry is another HTTP attempt. A batch embedding request counts once. Model-list and plain liveness requests are not inference. Missing provider token fields are unknown.

AI viewing-guide generation/reuse is recorded separately. AI Functions, managed workflows, external callers and local Agent Catalog publishing compute bypass the inference meter. Their unreported tokens/cost remain unknown. Enabling AI Functions does not itself invalidate a Memory trial; overlapping generation does.

Trial state, events and receipts persist in streaming.telemetry.ai_metrics. Reports read the trial's own time range; a new reporting window does not erase evidence. There is no SQLite database or local persistence fallback. Legacy hypothetical-comparison API fields remain for compatibility but do not drive headline cards.

Plan hits, supplied facts, memory blocks, guide reuse and tool counts are operational evidence, not measured counterfactual tokens or quality scores. Response duration is not a browser-to-cloud SLA or scaling benchmark.

See VALIDATION.md for offline checks. This release has not been benchmarked against the user's live Capella deployment. Follow SAVINGS-DEMO.md to generate a real receipt.

- [Agent Memory API](https://docs.couchbase.com/ai/agent-memory-api-reference/rest-api.html)
- [About Agent Memory](https://docs.couchbase.com/ai/build/agent-memory/about-agent-mem.html)
