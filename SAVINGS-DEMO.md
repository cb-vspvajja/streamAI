# Demonstrating measured Memory efficiency

## Upgrade

Extract streamai-capella-v1.1.0.zip into a new folder. Copy your working capella.env and public certs/capella-ca.pem into it. Keep AI_FUNCTIONS_ENABLED=true if the three functions are deployed. Start Docker Desktop and run:

    bash START-CAPELLA.sh

The launcher rebuilds UI, usage and the derived Memory image. All three must match this release for trial correlation. Normal Capella data is retained. Refresh the browser after startup. The default MEMORY_SUMMARY_POLICY=selective skips optional summaries of short turns/explicit facts while retaining originals and embeddings.

## Demo path

1. Sign in and complete two to four short assistant exchanges. Example prompts: “I like science fiction, but avoid horror”; “Show me science fiction movies”; “What are my preferences?”; “What did I ask for earlier?” Use genres/titles present in your catalogue.
2. Wait for background Memory writes/profile sync. Finish any ingestion job or competing model activity.
3. Open Measured Agent Memory savings. Optionally set illustrative USD rates before starting. Choose Compare memory policies.
4. Leave the app idle while it stores the same recent exchanges into two isolated Memory sessions. Both phases call real models and consume resources.
5. Show the three cards, then expand the receipt: phase counts, embedding costs, background requests, exact-content checks and recorded request IDs.
6. Inspect the real viewer's Agent Memory to explain that originals remain. Trial sessions are cleaned up; their accounting receipt remains in Couchbase.

## Presenter wording

“These are two measured ways of storing the same recent conversation in Agent Memory. Both retain the originals and their embeddings. Selective processing avoids a redundant generative summary for short turns and explicit preferences. The cards show the recorded request and token difference, including embeddings and Memory background work. Dollars are an illustrative token-cost equivalent, not a reduction in our Capella invoice.”

Do not describe this as a measured comparison against AWS, a no-AIDP application or equal-quality retrieval. It measures Memory ingestion efficiency. Billing impact requires workload and capacity planning.

## Troubleshooting

| State | Action |
| --- | --- |
| Run comparison | Chatting supplies the workload; explicitly run the experiment to obtain a measured baseline. |
| Too few eligible turns | Complete at least two pairs, each at most 4,000 combined characters. |
| Background writes active | Wait for pending chat/profile writes to finish, then retry. |
| Missing correlation | Rebuild Memory and usage from this ZIP. |
| Missing usage / failed requests | Inspect the receipt and endpoint/model errors; unknown is not zero. |
| Incomplete ready/content proof | Check native Memory processing; accepted does not mean ready. |
| Overlapping activity | Stop other callers, ingestion and AI Functions generation during the trial. |
| Negative difference | Included background/retry work exceeded avoided summaries. Inspect and explain it honestly. |
| Interrupted run | Let outstanding Memory work settle; restart usage to mark the run failed, then retry. |
| Cleanup failed | The receipt names an isolated streamai-cost-trial- user. Remove only that user with native Memory tools, never the normal viewer bucket. |

The test itself consumes both phases' resources. The positive difference describes a policy saving for this workload, not a refund for performing the experiment.
