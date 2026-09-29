#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || {
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"
  echo "Created $ENV_FILE. Review it and rerun; the bundled sample catalogue is used when no TMDB token is set."
  exit 1
}
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

"$ROOT_DIR/scripts/00-check-prerequisites.sh"
if [[ "${DEPLOYMENT_TARGET:-local}" == "local" ]]; then
  "$ROOT_DIR/scripts/00-start-couchbase.sh"
  [[ "${CHAT_PROVIDER:-ollama}" == "ollama" || "${EMBEDDING_BASE_URL:-}" == *ollama* ]] && "$ROOT_DIR/scripts/01-start-ollama.sh"
fi
if [[ "${START_AGENT_MEMORY:-true}" == "true" ]] && ! curl -fsS "${AGENT_MEMORY_HOST_HEALTH_URL:-http://localhost:8080/health}" >/dev/null 2>&1; then "$ROOT_DIR/scripts/02-start-agent-memory.sh"; fi
if [[ "${DEPLOYMENT_TARGET:-local}" == "local" ]]; then "$ROOT_DIR/scripts/03-setup-content-plane.sh"; else "$ROOT_DIR/scripts/12-validate-environment.py"; fi

if [[ "${START_MCP_SERVER:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/02b-start-mcp-server.sh"
fi

if [[ "${DEPLOYMENT_TARGET:-local}" == "local" ]]; then
  count_json="$(curl -fsS -u "${CB_ADMIN_USERNAME:-Administrator}:${CB_ADMIN_PASSWORD:-password}" -X POST "http://${CB_HOST:-localhost}:8093/query/service" --data-urlencode "statement=SELECT RAW COUNT(1) FROM \`${CONTENT_BUCKET:-streaming}\`.\`catalogue\`.\`titles\`")"
  count="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("results") or [0])[0])' <<<"$count_json")"
  if [[ "${FORCE_CATALOGUE_RELOAD:-false}" == true || "$count" == 0 ]]; then "$ROOT_DIR/scripts/04-load-catalogue.sh"; else echo "Catalogue already contains $count titles."; fi
  "$ROOT_DIR/scripts/04b-upgrade-search-index.sh"
  "$ROOT_DIR/scripts/04c-upgrade-governed-agent.sh"
fi

if [[ "${AGENT_CATALOG_ENABLED:-true}" == "true" && "${PUBLISH_AGENT_CATALOG_ON_START:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/04d-publish-agent-catalog.sh"
fi

if [[ "${DEPLOYMENT_TARGET:-local}" == "local" && "${CHAT_PROVIDER:-ollama}" == "ollama" && "$(uname -s)" == "Darwin" ]] && command -v ollama >/dev/null 2>&1; then
  "$ROOT_DIR/scripts/10-fast-mac-mode.sh"
else
  OLLAMA_MODE=docker "$ROOT_DIR/scripts/05-start-ui.sh"
fi
