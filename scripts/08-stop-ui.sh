#!/usr/bin/env bash
set -euo pipefail
docker rm -f "${UI_CONTAINER:-couchbase-stream-ai-ui}" >/dev/null 2>&1 || true
echo "StreamAI UI stopped. Couchbase, Agent Memory, Ollama and all data were preserved."
