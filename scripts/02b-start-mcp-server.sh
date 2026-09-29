#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ "${MCP_ENABLED:-true}" == "true" ]] || { echo "MCP Server disabled."; exit 0; }
NETWORK_NAME="${NETWORK_NAME:-agent-memory-network}"
CONTAINER="${MCP_CONTAINER:-couchbase-mcp-server}"
IMAGE="${MCP_IMAGE:-couchbase-stream-ai-mcp-server:1.0.0}"
HOST_PORT="${MCP_HOST_PORT:-8000}"
TARGET="${DEPLOYMENT_TARGET:-local}"
BUILD_LOCAL="${MCP_BUILD_LOCAL_IMAGE:-true}"

if [[ -n "${MCP_CB_CONNECTION_STRING:-}" ]]; then
  MCP_CONN="$MCP_CB_CONNECTION_STRING"
elif [[ "$TARGET" == "local" ]]; then
  MCP_CONN="couchbase://host.docker.internal"
else
  MCP_CONN="${CB_CONN_STRING:?CB_CONN_STRING is required}"
fi
MCP_USER="${MCP_CB_USERNAME:-streamai_mcp}"
MCP_PASS="${MCP_CB_PASSWORD:-streamaiMcp123}"

# The schema-inspection MCP tool calls Couchbase's management endpoint to list
# scopes and collections. In local mode, fail early with a precise RBAC message
# instead of allowing the later MCP proof to return an opaque 403.
if [[ "$TARGET" == "local" && "${MCP_PREFLIGHT_SCHEMA_PERMISSION:-true}" == "true" ]]; then
  management_url="${MCP_MANAGEMENT_URL:-http://localhost:8091}"
  response_file="$(mktemp)"
  trap 'rm -f "$response_file"' EXIT
  status="$(curl -sS -u "$MCP_USER:$MCP_PASS" -o "$response_file" -w '%{http_code}' \
    "$management_url/pools/default/buckets/${CONTENT_BUCKET:-streaming}/scopes" || true)"
  if [[ "$status" == "403" ]]; then
    echo "ERROR: MCP user '$MCP_USER' cannot list scopes and collections in bucket '${CONTENT_BUCKET:-streaming}'." >&2
    echo "Grant the read-only admin role (ro_admin) to the dedicated MCP user, then rerun this script." >&2
    cat "$response_file" >&2 || true
    exit 1
  elif [[ "$status" == "401" ]]; then
    echo "ERROR: MCP credentials were rejected for user '$MCP_USER'. Check MCP_CB_USERNAME and MCP_CB_PASSWORD." >&2
    exit 1
  elif [[ "$status" != "200" ]]; then
    echo "ERROR: MCP schema-permission preflight returned HTTP $status from $management_url." >&2
    cat "$response_file" >&2 || true
    exit 1
  fi
  rm -f "$response_file"
  trap - EXIT
  echo "MCP RBAC preflight passed for user: $MCP_USER"
fi

docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null
if [[ "$BUILD_LOCAL" == "true" ]]; then
  if [[ "${FORCE_MCP_REBUILD:-false}" == "true" ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker build -t "$IMAGE" "$ROOT_DIR/mcp-server"
  fi
else
  docker pull "$IMAGE" >/dev/null
fi

docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

args=(
  -d
  --name "$CONTAINER"
  --network "$NETWORK_NAME"
  --add-host=host.docker.internal:host-gateway
  -p "${HOST_PORT}:8000"
  -e "CB_CONNECTION_STRING=$MCP_CONN"
  -e "CB_USERNAME=$MCP_USER"
  -e "CB_PASSWORD=$MCP_PASS"
  -e "CB_MCP_TRANSPORT=http"
  -e "CB_MCP_HOST=0.0.0.0"
  -e "CB_MCP_PORT=8000"
  -e "CB_MCP_READ_ONLY_MODE=${MCP_READ_ONLY:-true}"
  -e "CB_MCP_DISABLED_TOOLS=${MCP_DISABLED_TOOLS:-}"
  -e "CB_MCP_CONFIRMATION_REQUIRED_TOOLS=${MCP_CONFIRMATION_REQUIRED_TOOLS:-}"
  -e "CB_MCP_LOG_LEVEL=${MCP_LOG_LEVEL:-info}"
  -e "CB_MCP_LOG_SINKS=${MCP_LOG_SINKS:-stderr}"
)

# Optional MCP OAuth resource-server configuration for non-local deployments.
[[ -n "${MCP_OAUTH_JWKS_URI:-}" ]] && args+=( -e "CB_MCP_OAUTH_JWT_JWKS_URI=$MCP_OAUTH_JWKS_URI" )
[[ -n "${MCP_OAUTH_ISSUER:-}" ]] && args+=( -e "CB_MCP_OAUTH_JWT_ISSUER=$MCP_OAUTH_ISSUER" )
[[ -n "${MCP_OAUTH_AUDIENCE:-}" ]] && args+=( -e "CB_MCP_OAUTH_JWT_AUDIENCE=$MCP_OAUTH_AUDIENCE" )
[[ -n "${MCP_OAUTH_MCP_BASE_URL:-}" ]] && args+=( -e "CB_MCP_OAUTH_MCP_BASE_URL=$MCP_OAUTH_MCP_BASE_URL" )

args+=( --restart unless-stopped "$IMAGE" )
docker run "${args[@]}" >/dev/null

for attempt in $(seq 1 60); do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://localhost:${HOST_PORT}/mcp" || true)"
  # An MCP endpoint can return 400/405/406 to a bare GET while still being ready.
  if [[ "$code" != "000" && "$code" != "500" && "$code" != "502" && "$code" != "503" ]]; then
    version="$(docker exec "$CONTAINER" couchbase-mcp-server --version 2>/dev/null || true)"
    echo "Couchbase MCP Server reachable: http://localhost:${HOST_PORT}/mcp (HTTP $code) ${version}"
    exit 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    docker logs --tail 200 "$CONTAINER" >&2 || true
    exit 1
  fi
  sleep 2
done

docker logs --tail 200 "$CONTAINER" >&2 || true
exit 1
