#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE. Run: cp .env.example .env" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

HOST="${CB_HOST:-localhost}"
ADMIN_USER="${CB_ADMIN_USERNAME:-Administrator}"
ADMIN_PASS="${CB_ADMIN_PASSWORD:-password}"
CONTAINER="${COUCHBASE_CONTAINER:-couchbase}"
IMAGE="${COUCHBASE_IMAGE:-couchbase/server:enterprise-8.0.2}"

if curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "http://${HOST}:8091/pools/default" >/dev/null 2>&1; then
  echo "Couchbase is already running and initialized on ${HOST}:8091."
else
  if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
    docker start "$CONTAINER" >/dev/null
  else
    echo "Starting Couchbase Server: $IMAGE"
    docker run -d \
      --name "$CONTAINER" \
      -p 8091-8097:8091-8097 \
      -p 11210-11211:11210-11211 \
      -v couchbase-streamai-data:/opt/couchbase/var \
      --restart unless-stopped \
      "$IMAGE" >/dev/null
  fi

  echo "Waiting for Couchbase Server..."
  for _ in $(seq 1 120); do
    curl -fsS "http://${HOST}:8091/ui/index.html" >/dev/null 2>&1 && break
    sleep 2
  done

  if ! curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "http://${HOST}:8091/pools/default" >/dev/null 2>&1; then
    echo "Initializing single-node cluster with Data, Index, Query and Search..."
    docker exec "$CONTAINER" couchbase-cli cluster-init \
      -c 127.0.0.1 \
      --cluster-username "$ADMIN_USER" \
      --cluster-password "$ADMIN_PASS" \
      --cluster-name "StreamAI Local" \
      --services data,index,query,fts \
      --cluster-ramsize "${CB_DATA_RAM_MB:-2048}" \
      --cluster-index-ramsize "${CB_INDEX_RAM_MB:-1024}" \
      --cluster-fts-ramsize "${CB_FTS_RAM_MB:-512}" \
      --index-storage-setting default >/dev/null
  fi
fi

base="http://${HOST}:8091"
if ! curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$base/pools/default/buckets/agent_memory" >/dev/null 2>&1; then
  echo "Creating Agent Memory bucket..."
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X POST "$base/pools/default/buckets" \
    -d "name=agent_memory" -d "bucketType=couchbase" -d "storageBackend=magma" \
    -d "ramQuotaMB=${AGENT_MEMORY_BUCKET_RAM_MB:-256}" -d "replicaNumber=0" >/dev/null
fi

# Local-only broad role keeps first-time Agent Memory bootstrap predictable.
curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X PUT \
  "$base/settings/rbac/users/local/${AGENTMEMORY_USERNAME:-agentMemory}" \
  --data-urlencode "name=Agent Memory local service" \
  --data-urlencode "password=${AGENTMEMORY_PASSWORD:-password}" \
  --data-urlencode "roles=admin" >/dev/null

echo "Couchbase local foundation is ready."
