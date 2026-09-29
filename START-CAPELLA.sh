#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/capella-common.sh"
trap 'echo "Startup stopped. Correct the error above and rerun bash START-CAPELLA.sh. Existing data is retained. Container logs: bash CAPELLA-LOGS.sh" >&2' ERR

case "$(uname -m)" in
  arm64|aarch64) ;;
  *) echo "This download includes your ARM64 Agent Memory image. Use it on Apple Silicon/ARM64." >&2; exit 1 ;;
esac
for key in CB_CONN_STRING CB_USERNAME CB_PASSWORD MCP_CB_USERNAME MCP_CB_PASSWORD CHAT_BASE_URL EMBEDDING_BASE_URL CAPELLA_MODEL_API_KEY; do
  value="${!key}"
  if [[ -z "$value" || "$value" == *CHANGE_ME* || "$value" == *YOUR_* || "$value" == *YOUR-* ]]; then
    echo "Fill in $key in capella.env before starting." >&2
    exit 1
  fi
done
[[ "$CB_CONN_STRING" == couchbases://* ]] || { echo "Use the couchbases:// SDK connection string from Capella." >&2; exit 1; }
for url in "$CHAT_BASE_URL" "$EMBEDDING_BASE_URL"; do
  [[ "$url" == https://* && "$url" != */chat/completions && "$url" != */embeddings ]] || {
    echo "Use HTTPS model endpoint roots, without /chat/completions or /embeddings." >&2; exit 1;
  }
done
if [[ "$DATA_PROCESSING_MODE" == capella_workflow && ( -z "$DATA_PROCESSING_WORKFLOW_ID" || "$DATA_PROCESSING_WORKFLOW_ID" == *CHANGE_ME* ) ]]; then
  echo "Set the real DATA_PROCESSING_WORKFLOW_ID, or use DATA_PROCESSING_MODE=python_loader." >&2; exit 1
fi
[[ -s "$ROOT/certs/capella-ca.pem" ]] || {
  echo "Save your Capella cluster's downloaded root CA certificate as certs/capella-ca.pem." >&2; exit 1;
}
chmod 600 "$CAPELLA_CONFIG"
chmod 644 "$ROOT/certs/capella-ca.pem"
docker info >/dev/null 2>&1 || { echo "Start Docker Desktop and wait until its engine is ready." >&2; exit 1; }
compose config --quiet

printf '\n[1/7] Preparing the Docker setup tools...\n'
compose build tools usage
# Drain the old meter before replacing it; it remains running if transfer fails.
compose stop ui memory mcp
compose run --rm --no-deps tools python tools/bootstrap_usage.py
compose up -d --no-deps usage
compose run --rm --no-deps tools python tools/wait_usage_gateway.py
printf '\n[2/7] Checking the database and model endpoints...\n'
compose run --rm --no-deps tools python scripts/12-validate-environment.py
compose run --rm --no-deps tools python tools/check_capella_models.py
printf '\n[3/7] Preparing schema, Search indexes and the initial catalogue...\n'
compose run --rm --no-deps tools python tools/capella_setup.py prepare
printf '\n[4/7] Building Agent Memory, MCP and the catalog publisher...\n'
if ! docker image inspect streamai-capella-memory-base:1.0.0-rc2 >/dev/null 2>&1; then
  docker load -i "$ROOT/vendor/agentmemory-server-arm64-1.0.0-rc2.tar"
  docker tag agentmemory-server:arm64 streamai-capella-memory-base:1.0.0-rc2
fi
compose build memory mcp publisher
printf '\n[5/7] Starting Agent Memory and MCP...\n'
compose up -d --no-deps memory mcp
compose run --rm --no-deps tools python tools/capella_setup.py wait-memory
printf '\n[6/7] Publishing Agent Catalog and building the UI...\n'
compose run --rm --no-deps publisher
for catalog_file in tools.json prompts.json streamai-publish.json; do
  [[ -s "$ROOT/ui/.agent-catalog/$catalog_file" ]] || { echo "Publisher did not create $catalog_file." >&2; exit 1; }
done
compose build ui
compose up -d --no-deps ui
printf '\n[7/7] Checking the running application...\n'
compose run --rm --no-deps tools python tools/capella_setup.py verify
printf '\nStreamAI is ready: http://localhost:%s\n' "$CAPELLA_UI_PORT"
