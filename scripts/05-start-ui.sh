#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a; source "$ENV_FILE"; set +a
NETWORK_NAME="${NETWORK_NAME:-agent-memory-network}"; UI_IMAGE="${UI_IMAGE:-couchbase-stream-ai-ui:1.0.0}"; UI_CONTAINER="${UI_CONTAINER:-couchbase-stream-ai-ui}"; UI_PORT="${UI_PORT:-8088}"

# Resolve the chat endpoint from the execution mode rather than relying on a
# host-specific value in .env. The UI runs in Docker in both cases.
CHAT_RUNTIME_BASE_URL="${CHAT_BASE_URL:-}"
if [[ "${CHAT_PROVIDER:-ollama}" == "ollama" ]]; then
  case "${OLLAMA_MODE:-auto}" in
    configured) : "${CHAT_BASE_URL:?CHAT_BASE_URL is required for configured mode}" ;;
    native) CHAT_RUNTIME_BASE_URL="http://host.docker.internal:${OLLAMA_NATIVE_PORT:-11435}" ;;
    docker) CHAT_RUNTIME_BASE_URL="http://ollama:11434" ;;
    auto)
      if [[ "$(uname -s)" == "Darwin" ]] && command -v ollama >/dev/null 2>&1; then
        CHAT_RUNTIME_BASE_URL="http://host.docker.internal:${OLLAMA_NATIVE_PORT:-11435}"
      else
        CHAT_RUNTIME_BASE_URL="http://ollama:11434"
      fi
      ;;
    *) echo "Unsupported OLLAMA_MODE=${OLLAMA_MODE}" >&2; exit 1 ;;
  esac
fi
docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null
if [[ "${AGENT_CATALOG_ENABLED:-true}" == "true" && "${AGENT_CATALOG_REQUIRED:-true}" == "true" ]]; then
  [[ -s "$ROOT_DIR/ui/.agent-catalog/tools.json" ]] || { echo "Agent Catalog tool index is missing (tools.json). Run scripts/04d-publish-agent-catalog.sh" >&2; exit 1; }
  [[ -s "$ROOT_DIR/ui/.agent-catalog/prompts.json" ]] || { echo "Agent Catalog prompt index is missing (prompts.json)." >&2; exit 1; }
fi
if [[ "${FORCE_UI_REBUILD:-false}" == true ]] || ! docker image inspect "$UI_IMAGE" >/dev/null 2>&1; then docker build -t "$UI_IMAGE" "$ROOT_DIR/ui"; fi
docker rm -f "$UI_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$UI_CONTAINER" --network "$NETWORK_NAME" --add-host=host.docker.internal:host-gateway -p "$UI_PORT:8088" --env-file "$ENV_FILE" \
  -e APP_VERSION=1.0.0 \
  -e CB_CONN_STRING="${CB_CONN_STRING:-couchbase://host.docker.internal}" \
  -e CB_USERNAME="${CB_USERNAME:-agentMemory}" \
  -e CB_PASSWORD="${CB_PASSWORD:-password}" \
  -e CHAT_API_KEY="${CHAT_API_KEY-}" \
  -e CHAT_HEADERS_JSON="${CHAT_HEADERS_JSON-}" \
  -e EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL:-http://ollama:11434}" \
  -e EMBEDDING_MODEL="${EMBEDDING_MODEL:-nomic-embed-text}" \
  -e EMBEDDING_API_KEY="${EMBEDDING_API_KEY-}" \
  -e EMBEDDING_HEADERS_JSON="${EMBEDDING_HEADERS_JSON-}" \
  -e AGENT_MEMORY_URL="${AGENT_MEMORY_URL:-http://agentmemory-server:8080}" \
  -e MCP_URL="${MCP_URL:-http://couchbase-mcp-server:8000/mcp}" \
  -e MCP_BEARER_TOKEN="${MCP_BEARER_TOKEN-}" \
  -e CHAT_BASE_URL="$CHAT_RUNTIME_BASE_URL" \
  -e AGENT_CATALOG_CONN_STRING="${AGENT_CATALOG_CONN_STRING:-${CB_CONN_STRING:-couchbase://host.docker.internal}}" \
  -e AGENT_CATALOG_USERNAME="${AGENT_CATALOG_USERNAME:-${CB_USERNAME:-streamai}}" \
  -e AGENT_CATALOG_PASSWORD="${AGENT_CATALOG_PASSWORD:-${CB_PASSWORD:-streamai123}}" \
  -e AGENT_CATALOG_BUCKET="${AGENT_CATALOG_BUCKET:-${CONTENT_BUCKET:-streaming}}" \
  -e AGENT_CATALOG_CATALOG=/app/.agent-catalog \
  -e AGENT_CATALOG_ACTIVITY=/app/.agent-activity \
  -v streamai-agent-activity:/app/.agent-activity \
  --restart unless-stopped "$UI_IMAGE" >/dev/null
for attempt in $(seq 1 "${UI_STARTUP_ATTEMPTS:-90}"); do
  if curl -fsS "http://localhost:${UI_PORT}/api/readiness" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("ready") else 1)' >/dev/null 2>&1; then
    echo "StreamAI ready: http://localhost:${UI_PORT}"
    exit 0
  fi
  sleep 2
done
docker logs --tail 200 "$UI_CONTAINER" >&2; exit 1
