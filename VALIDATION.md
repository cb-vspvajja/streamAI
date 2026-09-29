# StreamAI 1.1.0 — validation record

Validated offline on 28 September 2026. This is a demo release, not a production certification.

## Verified for this package

| Check | Result |
| --- | --- |
| Python regression and new Memory-policy tests | 385 passed; one existing FastAPI/Starlette TestClient deprecation warning |
| JavaScript usage and AI-guide renderer suites | Both passed |
| Python syntax | 93 source files parsed |
| Shell syntax | 26 scripts passed Bash syntax checks |
| Docker builds | UI 1.1.0, usage 1.1.0 and derived Memory 1.1.0-nvidia built successfully |
| Compose configuration | Validated with the supplied placeholder configuration |
| Native SDK interface | Verified deployed package signatures for create_user, create_session, add_memory, context_required, async_processing and list_memories |
| Native Memory image | Strict compatibility patch applied and compiled in both source copies |
| Presentation outputs | Revised 10-slide deck and 2-slide supplement rendered, visually reviewed and checked for slide count/package integrity |
| UI reference prototype | Six populated desktop views and one narrow assistant view captured and inspected; fixture JavaScript syntax checked |

The test run uses actual installed Couchbase packages before collection to prevent legacy test-stub import contamination. Providers/storage are mocked or isolated in tests; no cloud calls are needed for this suite.

New tests cover selective/long-turn policy, eligible original-turn selection, exact content and ready-state checks, real arithmetic including embeddings, missing/failed/late or unrelated usage suppression, negative results, background and outside-phase overhead, durable receipts, gateway restart recovery, native correlation isolation, private-header stripping and cache bypass. Runner tests verify both policies receive identical originals and clean up only their owned temporary user, including rejection and cleanup-failure paths.

## What has not been validated live

- The two-policy trial has not been executed against the user's live Capella deployment. No actual cloud saving is claimed in this package.
- The running user containers/configuration/data were not updated. New local images were built separately; installation requires the supplied launcher.
- A fresh empty-cluster full setup was not repeated. Existing startup/native-component tests remain in the suite.
- Summary-derived retrieval quality equivalence, production concurrency/authentication, load/scalability, managed workflows and invoice savings were not tested.
- UI screenshots use clearly labelled fictional data. Their numbers are layout fixtures, not benchmark evidence.

Both policy phases incur real inference when the presenter runs Compare memory policies. Success requires complete correlated provider usage and matching ready originals. Failure or incomplete evidence stays unavailable; negative results are retained. Dollar figures are illustrative token-cost equivalents, not Capella bills.

## Packaging and dependencies

PACKAGE-MANIFEST.json records the app version, accounting boundary, vendor archive hashes and checks. The complete ZIP retains the supplied ARM64 Memory server archive and official MCP distribution. Vendor versions remain distinct from application version 1.1.0. Some dependency ranges are retained, so future uncached builds need validation.

The packaged configuration uses placeholders. The archive excludes credentials, cluster certificates, local databases, caches and compiled Python files. ZIP integrity, source bytes, unique paths, vendor hashes and version checks are performed before delivery. SHA-256 sidecars identify the final downloads.

See SAVINGS-DEMO.md for upgrade and live demonstration steps, and METRICS-AUDIT.md for measurement limits.
