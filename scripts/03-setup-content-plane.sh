#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE. Run: cp .env.example .env" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

CB_HOST="${CB_HOST:-localhost}"
ADMIN_USER="${CB_ADMIN_USERNAME:-Administrator}"
ADMIN_PASS="${CB_ADMIN_PASSWORD:-password}"
APP_USER="${CB_APP_USERNAME:-streamai}"
APP_PASS="${CB_APP_PASSWORD:-streamai123}"
BUCKET="${CONTENT_BUCKET:-streaming}"
MCP_ENABLED_FLAG="${MCP_ENABLED:-true}"
MCP_USER="${MCP_CB_USERNAME:-streamai_mcp}"
MCP_PASS="${MCP_CB_PASSWORD:-streamaiMcp123}"

base="http://${CB_HOST}:8091"
query_url="http://${CB_HOST}:8093/query/service"

echo "Waiting for Couchbase management API..."
for _ in $(seq 1 90); do
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$base/pools/default" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$base/pools/default" >/dev/null

if ! curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$base/pools/default/buckets/$BUCKET" >/dev/null 2>&1; then
  echo "Creating bucket: $BUCKET"
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X POST "$base/pools/default/buckets" \
    -d "name=$BUCKET" \
    -d "bucketType=couchbase" \
    -d "storageBackend=magma" \
    -d "ramQuotaMB=${STREAMING_BUCKET_RAM_MB:-256}" \
    -d "replicaNumber=0" \
    -d "flushEnabled=1" >/dev/null
else
  echo "Bucket already exists: $BUCKET"
fi

for _ in $(seq 1 60); do
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" "$base/pools/default/buckets/$BUCKET" >/dev/null 2>&1 && break
  sleep 2
done

create_scope() {
  local scope="$1"
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X POST \
    "$base/pools/default/buckets/$BUCKET/scopes" -d "name=$scope" >/dev/null 2>&1 || true
}
create_collection() {
  local scope="$1" collection="$2"
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X POST \
    "$base/pools/default/buckets/$BUCKET/scopes/$scope/collections" \
    -d "name=$collection" >/dev/null 2>&1 || true
}

create_scope catalogue
create_scope viewers
create_scope recommendations
create_scope operations
create_scope telemetry

create_collection catalogue titles
create_collection catalogue people
create_collection catalogue genres
create_collection viewers profiles
create_collection viewers watch_history
create_collection viewers interactions
create_collection viewers app_state
create_collection recommendations generated
create_collection recommendations traces
create_collection recommendations plan_cache
create_collection operations ingestion_jobs
create_collection operations entitlements
create_collection operations agent_catalog
create_collection operations action_receipts
create_collection telemetry ai_metrics
create_collection telemetry showcase_state
create_collection telemetry experiments
create_collection telemetry evaluations
create_collection telemetry profile_snapshots

echo "Creating/updating local StreamAI application user: $APP_USER"
app_roles="data_reader[$BUCKET],data_writer[$BUCKET],query_select[$BUCKET],query_insert[$BUCKET],query_update[$BUCKET],query_delete[$BUCKET],fts_searcher[$BUCKET]"

if [[ "$MCP_ENABLED_FLAG" == "true" && "$MCP_USER" == "$APP_USER" ]]; then
  echo "WARNING: MCP_CB_USERNAME matches the application user; adding read-only cluster metadata access for MCP schema discovery."
  echo "         Use a separate MCP_CB_USERNAME (recommended) to keep application and MCP privileges isolated."
  app_roles="$app_roles,ro_admin"
fi

curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X PUT \
  "$base/settings/rbac/users/local/$APP_USER" \
  --data-urlencode "name=Couchbase StreamAI local application" \
  --data-urlencode "password=$APP_PASS" \
  --data-urlencode "roles=$app_roles" >/dev/null

if [[ "$MCP_ENABLED_FLAG" == "true" && "$MCP_USER" != "$APP_USER" ]]; then
  echo "Creating/updating dedicated read-only MCP user: $MCP_USER"
  mcp_roles="ro_admin,data_reader[$BUCKET],query_select[$BUCKET],fts_searcher[$BUCKET]"
  curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X PUT \
    "$base/settings/rbac/users/local/$MCP_USER" \
    --data-urlencode "name=Couchbase StreamAI read-only MCP runtime" \
    --data-urlencode "password=$MCP_PASS" \
    --data-urlencode "roles=$mcp_roles" >/dev/null
fi

run_query() {
  local statement="$1"
  local result
  result="$(curl -fsS -u "$ADMIN_USER:$ADMIN_PASS" -X POST "$query_url" \
    --data-urlencode "statement=$statement")"
  if echo "$result" | grep -q '"status":"errors"'; then
    echo "$result" >&2
    return 1
  fi
}

B="\`$BUCKET\`"
echo "Creating SQL++ indexes..."
run_query "CREATE PRIMARY INDEX IF NOT EXISTS ON $B.\`catalogue\`.\`titles\`"
run_query "CREATE INDEX IF NOT EXISTS ix_titles_popularity ON $B.\`catalogue\`.\`titles\`(popularity DESC, voteAverage DESC)"
run_query "CREATE INDEX IF NOT EXISTS ix_titles_type_year ON $B.\`catalogue\`.\`titles\`(contentType, releaseYear DESC)"
run_query "CREATE INDEX IF NOT EXISTS ix_titles_type_genre ON $B.\`catalogue\`.\`titles\`(contentType, DISTINCT ARRAY LOWER(g) FOR g IN genres END)"
run_query "CREATE INDEX IF NOT EXISTS ix_titles_analytics ON $B.\`catalogue\`.\`titles\`(contentType, releaseYear, originalLanguage, voteAverage, runtimeMinutes, episodeRuntimeMinutes)"
run_query "CREATE PRIMARY INDEX IF NOT EXISTS ON $B.\`viewers\`.\`profiles\`"
run_query "CREATE INDEX IF NOT EXISTS ix_watch_viewer_time ON $B.\`viewers\`.\`watch_history\`(viewerId, lastWatchedAt DESC, progressPct, titleId)"
run_query "CREATE INDEX IF NOT EXISTS ix_interactions_viewer_time ON $B.\`viewers\`.\`interactions\`(viewerId, timestamp DESC, action, titleId)"
run_query "CREATE INDEX IF NOT EXISTS ix_traces_viewer_time ON $B.\`recommendations\`.\`traces\`(viewerId, timestamp DESC, type)"
run_query "CREATE INDEX IF NOT EXISTS ix_traces_viewer_session ON $B.\`recommendations\`.\`traces\`(viewerId, sessionId, startedAt ASC, type)"
run_query "CREATE INDEX IF NOT EXISTS ix_plan_cache_version_expiry ON $B.\`recommendations\`.\`plan_cache\`(plannerVersion, toolSchemaVersion, expiresAt, semanticEligible)"
run_query "CREATE INDEX IF NOT EXISTS ix_entitlements_viewer ON $B.\`operations\`.\`entitlements\`(viewerId, regionCode, subscriptionTier)"
run_query "CREATE INDEX IF NOT EXISTS ix_agent_catalog_type_name ON $B.\`operations\`.\`agent_catalog\`(type, name, version)"
run_query "CREATE INDEX IF NOT EXISTS ix_action_receipts_viewer_time ON $B.\`operations\`.\`action_receipts\`(viewerId, timestamp DESC, action, titleId)"
run_query "CREATE PRIMARY INDEX IF NOT EXISTS ON $B.\`telemetry\`.\`ai_metrics\`"
run_query "CREATE INDEX IF NOT EXISTS ix_ai_metrics_viewer_time ON $B.\`telemetry\`.\`ai_metrics\`(viewerId, updatedAt DESC)"
run_query "CREATE PRIMARY INDEX IF NOT EXISTS ON $B.\`telemetry\`.\`showcase_state\`"
run_query "CREATE INDEX IF NOT EXISTS ix_experiments_viewer_time ON $B.\`telemetry\`.\`experiments\`(viewerId, timestamp DESC, type)"
run_query "CREATE INDEX IF NOT EXISTS ix_evaluations_viewer_time ON $B.\`telemetry\`.\`evaluations\`(viewerId, timestamp DESC, type)"
run_query "CREATE INDEX IF NOT EXISTS ix_profile_snapshots_viewer_time ON $B.\`telemetry\`.\`profile_snapshots\`(viewerId, timestamp DESC, recommendationVersion)"

echo
printf 'Content plane ready.\n  Bucket: %s\n  App user: %s\n  Scopes: catalogue, viewers, recommendations, operations, telemetry\n' "$BUCKET" "$APP_USER"
