#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

"$ROOT_DIR/scripts/12-validate-environment.py"
if [[ "${START_AGENT_MEMORY:-true}" == "true" ]] && ! curl -fsS "${AGENT_MEMORY_HOST_HEALTH_URL:-http://localhost:8080/health}" >/dev/null 2>&1; then
  "$ROOT_DIR/scripts/02-start-agent-memory.sh"
fi
if [[ "${START_MCP_SERVER:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/02b-start-mcp-server.sh"
fi
if [[ "${AGENT_CATALOG_ENABLED:-true}" == "true" && "${PUBLISH_AGENT_CATALOG_ON_START:-true}" == "true" ]]; then
  "$ROOT_DIR/scripts/04d-publish-agent-catalog.sh"
fi
"$ROOT_DIR/scripts/05-start-ui.sh"
