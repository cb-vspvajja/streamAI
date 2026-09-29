#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ "$(uname -s)" == "Darwin" ]] || { echo "Fast Mac mode is only for macOS." >&2; exit 1; }
command -v ollama >/dev/null 2>&1 || {
  echo "Native Ollama is required. Install from https://ollama.com/download/mac then rerun." >&2
  exit 1
}
PORT="${OLLAMA_NATIVE_PORT:-11435}"
MODEL="${OLLAMA_LLM_MODEL:-llama3.2:1b}"
LOG="${OLLAMA_NATIVE_LOG_FILE:-/tmp/couchbase-streamai-native-ollama.log}"
if ! curl -fsS "http://localhost:${PORT}/api/tags" >/dev/null 2>&1; then
  echo "Starting native Ollama on port $PORT..."
  OLLAMA_HOST="127.0.0.1:${PORT}" nohup ollama serve >"$LOG" 2>&1 &
  for _ in $(seq 1 45); do
    curl -fsS "http://localhost:${PORT}/api/tags" >/dev/null 2>&1 && break
    sleep 1
  done
fi
curl -fsS "http://localhost:${PORT}/api/tags" >/dev/null || { tail -n 80 "$LOG"; exit 1; }
if ! curl -fsS "http://localhost:${PORT}/api/tags" | grep -q "${MODEL%%:*}"; then
  OLLAMA_HOST="127.0.0.1:${PORT}" ollama pull "$MODEL"
else
  echo "Native model already present: $MODEL"
fi
OLLAMA_MODE=native OLLAMA_NATIVE_PORT="$PORT" "$ROOT_DIR/scripts/05-start-ui.sh"
