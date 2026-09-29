#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ "${AGENT_CATALOG_ENABLED:-true}" == "true" ]] || { echo "Agent Catalog disabled."; exit 0; }
NETWORK_NAME="${NETWORK_NAME:-agent-memory-network}"
IMAGE="${AGENT_CATALOG_PUBLISHER_IMAGE:-couchbase-stream-ai-agent-catalog-publisher:1.0.0}"
TARGET="${DEPLOYMENT_TARGET:-local}"

docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null
if [[ "${FORCE_AGENT_CATALOG_REBUILD:-false}" == "true" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker build -t "$IMAGE" "$ROOT_DIR/agent-catalog-publisher"
fi

if [[ -n "${AGENT_CATALOG_CONN_STRING:-}" ]]; then
  CATALOG_CONN="$AGENT_CATALOG_CONN_STRING"
elif [[ "$TARGET" == "local" ]]; then
  CATALOG_CONN="couchbase://host.docker.internal"
else
  CATALOG_CONN="${CB_CONN_STRING:?CB_CONN_STRING is required}"
fi
if [[ "$TARGET" == "local" ]]; then
  CATALOG_USER="${AGENT_CATALOG_PUBLISH_USERNAME:-${CB_ADMIN_USERNAME:-Administrator}}"
  CATALOG_PASS="${AGENT_CATALOG_PUBLISH_PASSWORD:-${CB_ADMIN_PASSWORD:-password}}"
else
  CATALOG_USER="${AGENT_CATALOG_PUBLISH_USERNAME:-${AGENT_CATALOG_USERNAME:-${CB_USERNAME:-}}}"
  CATALOG_PASS="${AGENT_CATALOG_PUBLISH_PASSWORD:-${AGENT_CATALOG_PASSWORD:-${CB_PASSWORD:-}}}"
fi
CATALOG_BUCKET="${AGENT_CATALOG_BUCKET:-${CONTENT_BUCKET:-streaming}}"

rm -rf "$ROOT_DIR/ui/.agent-catalog" "$ROOT_DIR/ui/.agent-activity"
mkdir -p "$ROOT_DIR/ui/.agent-catalog" "$ROOT_DIR/ui/.agent-activity"

docker run --rm \
  --network "$NETWORK_NAME" \
  --add-host=host.docker.internal:host-gateway \
  -v "$ROOT_DIR:/workspace" \
  -v streamai-agent-catalog-model-cache:/root/.cache \
  -e APP_VERSION=1.0.0 \
  -e AGENT_CATALOG_SOURCE_DIR=/workspace/ui/agent_catalog \
  -e AGENT_CATALOG_CATALOG=/workspace/ui/.agent-catalog \
  -e AGENT_CATALOG_ACTIVITY=/workspace/ui/.agent-activity \
  -e AGENT_CATALOG_CONN_STRING="$CATALOG_CONN" \
  -e AGENT_CATALOG_USERNAME="$CATALOG_USER" \
  -e AGENT_CATALOG_PASSWORD="$CATALOG_PASS" \
  -e AGENT_CATALOG_BUCKET="$CATALOG_BUCKET" \
  -e AGENT_CATALOG_INTERACTIVE=False \
  -e AGENT_CATALOG_DEBUG="${AGENT_CATALOG_DEBUG:-False}" \
  -e AGENT_CATALOG_EMBEDDING_MODEL="${AGENT_CATALOG_EMBEDDING_MODEL:-all-MiniLM-L12-v2}" \
  "$IMAGE"

for catalog_file in tools.json prompts.json streamai-publish.json; do
  [[ -s "$ROOT_DIR/ui/.agent-catalog/$catalog_file" ]] || {
    echo "Agent Catalog publication did not export $catalog_file to ui/.agent-catalog." >&2
    echo "Files currently exported:" >&2
    find "$ROOT_DIR/ui/.agent-catalog" -maxdepth 2 -type f -print >&2 || true
    exit 1
  }
done
echo "Agent Catalog runtime snapshot exported to: $ROOT_DIR/ui/.agent-catalog"
