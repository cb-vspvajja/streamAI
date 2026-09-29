# AI viewing guides — Capella integration (1.1.0)

## Demonstrate an application feature

1. Keep `AI_FUNCTIONS_ENABLED=true` in `capella.env`. The scoped usage comparison works with it enabled.
2. Open a film or series card. Its original synopsis remains visible above **AI viewing guide**.
3. Click **Generate viewing guide**. Capella evaluates classification, summary and sentiment against that title's catalogue document. The app displays the generated outputs and saves them in the same document under `aiEnrichment`.
4. Open **Saved execution details** to see the execution ID, Capella Query request ID, source and generation time. This is evidence inside the application; no browser developer tools are needed.
5. Click **Reuse saved guide**, or reopen the title. No new guide-generation query is needed. The explicit reuse button records a reuse action; simply opening the title is a read and does not inflate the reuse counter.
6. Click **Ask the assistant about this title**, or type `Tell me about The Scandal` after enriching that title. The assistant reads the saved summary and mood classification from the catalogue and labels their origin in its reply. It does not regenerate that guide. Other work during a chat, including background Agent Memory inference, can still consume tokens.
7. Inspect **Measured Agent Memory savings**. Run Compare memory policies after two to four short chat turns. AI Functions do not provide that baseline; generate guides separately from the trial. The separate catalogue-AI line records observed viewing-guide activity, with unreported cloud token usage left unknown.

The inspector's **AI viewing guide** button opens this title experience. It prefers the title you selected, then the latest grounded recommendation, then the home hero. It no longer fires an invisible proof request.

## Functions and business meaning

| Function | Input | Application use |
| --- | --- | --- |
| `default:ai_classification` | Title and overview; suggested mood labels | Mood classification displayed in the viewing guide and reused in title-information chat replies. |
| `default:ai_summary` | Catalogue overview | Short synopsis displayed on the title page and reused by the assistant. |
| `default:ai_sentiment` | Catalogue overview | Synopsis tone displayed in the viewing guide. It is not audience-review sentiment or a personal preference. |

This release integrates AI Functions into title discovery and assistant title-information replies. It does not use their outputs to change carousel rankings, entitlements, age ratings or parental controls. Those remain governed by authoritative data. Outputs are labelled AI-generated and are not guarantees of suitability or accuracy.

## Configure Capella once

Enable Classification, Summarization and Sentiment Analysis on the operational cluster and associate them with the intended generative model (your Mistral deployment). The application's cluster credential needs Query Curl Access for advanced credentials, plus access to read/update catalogue titles and write the telemetry collection. Keep its existing application permissions. Enable `AI_FUNCTIONS_ENABLED=true` and apply the configuration with `bash START-CAPELLA.sh`.

[Official AI Functions documentation](https://docs.couchbase.com/ai/build/ai-functions.html)
[Cluster access roles](https://docs.couchbase.com/cloud/clusters/manage-database-users.html)

## Durable storage and reuse

- Guide: `streaming.catalogue.titles`, document for the actual film/series, field `aiEnrichment`.
- Execution audit: `streaming.telemetry.ai_metrics`, key `ai-function-event::<execution/action ID>`, type `streamai_ai_function_event`.
- Chat reuse: reported directly to the primary Couchbase usage ledger; the normal chat trace includes the labelled response and `catalogue_ai_guide_reused` response mode.
- This feature does not store catalogue data in the Agent Memory bucket. Generation itself is not a native Agent Tracer span; its durable audit and viewing-guide evidence are separate.

The saved guide contains text outputs, source fingerprint, recipe, generation time, execution ID and Query request ID when returned. The integration does not report an exact deployed model identifier: the model is whichever is associated with these functions in Capella. Regenerate after changing that association if desired.

Unchanged sources and recipes reuse the guide. A changed title or synopsis invalidates it for display/chat reuse. Catalogue reloads preserve the record, allowing the app to detect staleness. Explicit regeneration replaces the guide only on a validated success; compare-and-swap protects concurrent catalogue edits. Initial audit persistence must succeed before inference starts. A failed or partial response never becomes a successful new guide. The previous saved guide remains if regeneration fails.

One successful generation returns three function results. This is not proof of exactly three upstream model HTTP requests or of fresh GPU generation: model-side retries and caches are not observable here. Displayed duration is application-side generation duration, not GPU-only latency.

## Usage and savings

Enabling AI Functions does not itself imply model tokens were consumed. Executing functions can consume model/query resources. Their calls originate inside Capella and bypass the local gateway. The function responses used here do not return a token-usage report. Consequently their model-request count, tokens and cost remain unknown, never zero. Successful guide reuse issues no new AI Functions query; it does not prove the rest of a chat used zero tokens.

The three cards compare two executed native Memory ingestion policies on identical recent conversation content. They include embeddings and routed Memory background work during each phase. The ordinary deployment view separately counts all routed sources. AI Functions are outside the inference meter; overlapping generation invalidates a trial. Dollars are illustrative token-cost equivalents, not Capella billed savings. Missing evidence suppresses the result, and negative differences remain visible. See SAVINGS-DEMO.md and METRICS-AUDIT.md.

A genuine overall token-saving comparison requires comparable baseline workload measurements and complete model usage, including the cloud function path. A financial saving additionally requires relevant billed costs/capacity, not a multiplication of tokens by illustrative rates. See `METRICS-AUDIT.md`.
