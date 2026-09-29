#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

echo "Starting existing Couchbase, Ollama and Agent Memory containers..."
for container in couchbase ollama agentmemory-server; do
  docker start "$container" >/dev/null 2>&1 || true
done

if [[ "$(uname -s)" == "Darwin" ]] && command -v ollama >/dev/null 2>&1; then
  FORCE_UI_REBUILD=false "$ROOT_DIR/scripts/10-fast-mac-mode.sh"
else
  FORCE_UI_REBUILD=false OLLAMA_MODE=docker "$ROOT_DIR/scripts/05-start-ui.sh"
fi
