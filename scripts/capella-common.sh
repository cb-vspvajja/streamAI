#!/usr/bin/env bash
set -euo pipefail
CAPELLA_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAPELLA_CONFIG="${CAPELLA_CONFIG:-$CAPELLA_ROOT/capella.env}"
[[ -f "$CAPELLA_CONFIG" ]] || { echo "Missing capella.env. Restore it from capella.env.example." >&2; exit 1; }
bash -n "$CAPELLA_CONFIG"
set -a
source "$CAPELLA_CONFIG"
set +a
# Empty defaults allow the stop/log commands to work before setup is complete.
: "${CB_CONN_STRING:=}" "${CB_USERNAME:=}" "${CB_PASSWORD:=}"
: "${MCP_CB_USERNAME:=}" "${MCP_CB_PASSWORD:=}"
: "${CHAT_BASE_URL:=}" "${EMBEDDING_BASE_URL:=}" "${CAPELLA_MODEL_API_KEY:=}"
export CB_CONN_STRING CB_USERNAME CB_PASSWORD MCP_CB_USERNAME MCP_CB_PASSWORD
export CHAT_BASE_URL EMBEDDING_BASE_URL CAPELLA_MODEL_API_KEY
CHAT_BASE_URL="${CHAT_BASE_URL%/}"
CHAT_BASE_URL="${CHAT_BASE_URL%/v1}"
EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL%/}"
EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL%/v1}"
source "$CAPELLA_ROOT/config/capella-runtime.sh"
command -v docker >/dev/null || { echo "Install and start Docker Desktop first." >&2; exit 1; }
docker compose version >/dev/null || { echo "Docker Compose v2 is required (included in Docker Desktop)." >&2; exit 1; }
compose() {
  docker compose --profile setup --project-name streamai-capella --project-directory "$CAPELLA_ROOT" \
    --env-file /dev/null -f "$CAPELLA_ROOT/compose.capella.yaml" "$@"
}
