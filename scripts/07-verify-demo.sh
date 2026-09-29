#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
UI_BASE_URL="${UI_BASE_URL:-http://localhost:8088}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

printf '\nStreamAI v1.0.0 native AI Data Plane verification\n'
printf 'Application: %s\n' "$UI_BASE_URL"

curl -fsS "$UI_BASE_URL/health" >/dev/null
UI_CONFIG_JSON="$(curl -fsS "$UI_BASE_URL/api/ui-config")"
STATUS_JSON="$(curl -fsS "$UI_BASE_URL/api/status")"

UI_CONFIG_JSON="$UI_CONFIG_JSON" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["UI_CONFIG_JSON"])
config = payload.get("data") or {}
mode = config.get("defaultExperience")
available = config.get("availableExperiences") or []
if mode not in {"showcase", "customer"} or mode not in available:
    raise SystemExit(f"Invalid UI experience configuration: {config}")
print(f"UI experience: {mode} (switch enabled: {bool(config.get('switchEnabled'))})")
PY

STATUS_JSON="$STATUS_JSON" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["STATUS_JSON"])
if not payload.get("ready"):
    raise SystemExit(f"StreamAI is not ready: {payload.get('error') or payload}")

services = payload.get("services") or {}
required = [
    "data_kv",
    "query_index",
    "search_fts_vector",
    "agent_memory",
    "agent_catalog",
    "mcp_server",
    "agent_tracer",
    "model_runtime",
]
failed = []
print("\nRequired runtime components:")
for name in required:
    item = services.get(name) or {}
    healthy = bool(item.get("healthy"))
    print(f"  {'OK' if healthy else 'FAIL':4} {name:24} {item.get('detail') or item.get('error') or ''}")
    if not healthy:
        failed.append(name)

mcp = services.get("mcp_server") or {}
print(f"\nMCP package: {mcp.get('package') or 'unknown'}")
print(f"MCP transport: {mcp.get('transport') or 'unknown'}")
print(f"MCP discovered tools: {mcp.get('toolCount', 0)}")
print(f"MCP read only: {mcp.get('readOnly')}")

catalog = services.get("agent_catalog") or {}
print(f"Agent Catalog snapshot: {catalog.get('catalogId') or 'latest published catalog'}")

print("\nConditional Capella AI services:")
for name in ("ai_functions", "data_processing"):
    item = services.get(name) or {}
    state = "OK" if item.get("healthy") else "FAIL"
    enabled = "enabled" if item.get("enabled") else "fallback/disabled"
    print(f"  {state:4} {name:24} {enabled} - {item.get('detail') or item.get('error') or ''}")

active = payload.get("data_plane", {}).get("active") or []
print("\nActive data-plane capabilities:")
for value in active:
    print(f"  - {value}")

if failed:
    print("\nRequired component failures: " + ", ".join(failed), file=sys.stderr)
    raise SystemExit(1)
PY

printf '\nAssistant plan cache:\n'
curl -fsS "$UI_BASE_URL/api/planner/cache" | python3 -c '
import json,sys
d=(json.load(sys.stdin).get("data") or {})
print("  collection:", d.get("collection", "unknown"))
print("  vector index:", d.get("searchIndex", "unknown"))
print("  vector healthy:", d.get("searchHealthy"))
print("  validated plans:", d.get("plans", 0))
print("  recorded cache hits:", d.get("cacheHits", 0))
'

if [[ "${VERIFY_MCP_SCHEMA_PROOF:-false}" == "true" ]]; then
  printf '\nRunning Agent Catalog -> MCP schema proof...\n'
  curl -fsS -X POST "$UI_BASE_URL/api/agent/mcp/schema" | python3 -c '
import json,sys
p=json.load(sys.stdin)
d=(p.get("data") or {}).get("schema") or {}
print("  keyspaces returned:", d.get("count", "unknown"))
print("  MCP primitive:", d.get("mcpPrimitive", "unknown"))
'
fi

printf '\nVerification succeeded. Open %s\n' "$UI_BASE_URL"
