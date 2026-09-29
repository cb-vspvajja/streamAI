# Couchbase StreamAI v2.1.3 Presenter Guide

## Prepare

```bash
cd couchbase-stream-ai-v2.1.3
./scripts/06-start-all.sh
./scripts/07-verify-demo.sh
```

Open `http://localhost:8088`, sign in and open **Showcase Studio**.

The header switch defaults to **Showcase**. Toggle it to **Customer** to show
the released-product view: streaming rows, search, profile and assistant remain,
while AI Value, inspectors, Showcase Studio and technical diagnostics disappear.
Toggle back to continue the technical demonstration.

## Recommended 15-minute flow

1. Reset to the Executive persona.
2. Use Presenter step 1 to verify the data plane.
3. Say `I only like children movies` and verify the durable profile shows `Family` in exclusive mode.
4. Start a new session and ask what the viewer likes to prove memory is durable across sessions.
5. Ask `Show me horror movies`; verify that no titles are returned and the response explains the policy conflict.
6. Try to like or play a known Horror title; verify that the governed action is denied before any profile, My List or history write.
7. Ask for Family movies and play one; verify the same governed path succeeds for in-policy content.
8. Ask `What should I watch next?`, then `Add first 2 to my list`; verify both exact titles appear in My List.
9. Ask `What movies did you add to my list recently?`, then `Show me movies similar to ones in my list`.
10. Ask `Show me the ones I can watch from this list`; verify every returned ID is a subset of the previous results.
11. Ask `How many movies are in the catalogue by genre?`; show the read-only analytics tool and grouped SQL++ result in the trace.
12. Open Agent Tracer and show exact title IDs, policy intersections, action receipts, memory source and MCP analytics evidence.
13. Open Search Lab and compare `atmospheric political thriller`.
14. Open one result and show the Evidence Scorecard.
15. Enable embedding timeout and repeat a semantic search to show FTS recovery.
16. Run the Evaluation Dashboard.
17. Close on AI Value metrics and the Capella v2 panel.

## Multi-title action proof

1. Ask `What should I watch next?`.
2. Note the first two grounded title cards.
3. Ask `Add first 2 to my list`.
4. Verify the response names exactly those two titles.
5. Open My List and confirm both records.
6. Open the trace and show exact-ID resolution, policy, write and receipt evidence for each title.

The action is bounded to the previous result list and a maximum of five titles. If one title is blocked, the response and trace show a partial outcome. Playback remains single-title only.

## Conversational-context proof

1. Ask `Suggest some good movies`.
2. Ask `Which one is the highest rated?`; verify the title comes only from the previous cards.
3. Ask `Who directed the second one?`; verify the trace uses the second prior title ID, not a Search for the words “second one.”
4. Ask `Add them to my list`; verify the five-title limit is enforced.
5. Ask `What did I ask before?`; show the active-transcript memory source.
6. Start a new session and repeat the recall question; show the Agent Memory block ID when retained short-term memory is available.

The key point is that conversational language selects an evidence set and an operation. It does not become unbounded retrieval text.

## Catalogue-analytics proof

Try:

- `How many movies are in the catalogue by genre?`
- `What is the average rating of movies by release year?`
- `Show the title count by original language.`
- `What is the average runtime of series by genre?`

Each request produces a validated tool plan. The measure, dimension, filters, maximum row count and read-only flag are visible in the trace; the assistant formats returned rows but cannot invent the values.

## Database-boundary proof

1. Ask `Which is the highest rated movie?`.
2. Verify that the response uses the stored catalogue rating and the trace reports `catalogue_top_rated_grounded`.
3. Ask `Which movie has the highest Rotten Tomatoes score?`.
4. Verify that the assistant states that the current catalogue has no verified external review score.
5. Open the trace and verify `modelCallBlocked=true` and `llmInvoked=false`.

This contrast demonstrates that the AI data plane supplies the evidence boundary: the assistant can use authoritative application data but cannot silently substitute facts learned during model training.

## Policy proof to emphasize

- The language model interprets `children movies`; the profile service stores the canonical `Family` preference.
- `only` creates a deterministic allow-list, not a ranking hint.
- Search, direct entitlement checks, likes, My List and playback all use the same rule.
- A denial is observable in the trace and happens before state is written.
- The existing title, preference and entitlement records remain the source of truth; no policy decision depends on an ungrounded model answer.

## Talk track

> The model interprets language. Couchbase remains responsible for customer identity, durable context, catalogue facts, governed tools, retrieval, policy, actions and observability.

## Resilience cautions

- Clear all simulations after demonstrating a failure.
- Search outage deliberately causes SQL++ fallback and may change ranking.
- Agent Memory outage does not delete memory; it only rejects the simulated persistence call.
- Model outage leaves deterministic catalogue, profile, policy and database-boundary routes available.

## Capella accuracy

In local mode:

- Do show Agent Memory, Agent Catalog, MCP, Agent Tracer, Data/KV, Query, FTS and Vector.
- Do not claim that native AI Functions or managed Data Processing executed locally.
- Use the Capella tab to explain how the same adapters are configured in the managed profile.
## Semantic plan-cache demonstration

1. Ask `Show me films about democratic collapse`.
2. Point out that the first unseen phrasing produces and validates a structured
   plan while the results come from fresh catalogue data.
3. After the background cache promotion completes, ask
   `Find movies about a collapsing democracy`.
4. The response metadata should show `plan semantic cache`, a similarity
   percentage and `LLM avoided`.
5. Explain that Vector Search matched the language and the validated plan was
   reused, while Search/Query still executed against current catalogue data.
6. Contrast this with Agent Memory: preferences and previous-result IDs are
   viewer/session context; reusable reasoning plans are shared, versioned
   operational cache documents.

For the latency contrast, ask `Show me films released last year`: this is a
fully deterministic temporal plan and deliberately skips both the planner
model and semantic-cache embedding.
