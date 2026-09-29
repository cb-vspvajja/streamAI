#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE. Run: cp .env.example .env" >&2; exit 1; }
# Preserve explicit command-line overrides before loading .env. Sourcing an
# environment file normally replaces exported values with the same name.
EXTERNAL_CB_LOADER_CONN_STRING="${CB_LOADER_CONN_STRING-}"
EXTERNAL_CB_LOADER_USERNAME="${CB_LOADER_USERNAME-}"
EXTERNAL_CB_LOADER_PASSWORD="${CB_LOADER_PASSWORD-}"
EXTERNAL_EMBEDDING_LOADER_BASE_URL="${EMBEDDING_LOADER_BASE_URL-}"
EXTERNAL_EMBEDDING_BASE_URL="${EMBEDDING_BASE_URL-}"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

[[ -n "$EXTERNAL_CB_LOADER_CONN_STRING" ]] && export CB_LOADER_CONN_STRING="$EXTERNAL_CB_LOADER_CONN_STRING"
[[ -n "$EXTERNAL_CB_LOADER_USERNAME" ]] && export CB_LOADER_USERNAME="$EXTERNAL_CB_LOADER_USERNAME"
[[ -n "$EXTERNAL_CB_LOADER_PASSWORD" ]] && export CB_LOADER_PASSWORD="$EXTERNAL_CB_LOADER_PASSWORD"
if [[ -n "$EXTERNAL_EMBEDDING_LOADER_BASE_URL" ]]; then
  export EMBEDDING_LOADER_BASE_URL="$EXTERNAL_EMBEDDING_LOADER_BASE_URL"
elif [[ -n "$EXTERNAL_EMBEDDING_BASE_URL" ]]; then
  # Backward-compatible host override for the command documented during the
  # v1.5.0 startup investigation.
  export EMBEDDING_LOADER_BASE_URL="$EXTERNAL_EMBEDDING_BASE_URL"
fi

# The catalogue loader runs on the host, whereas the UI, Agent Memory, MCP and
# Agent Catalog run in Docker. Keep separate endpoints so container DNS names
# never leak into the host-side Python process.
if [[ "${DEPLOYMENT_TARGET:-local}" == "local" ]]; then
  export CB_LOADER_CONN_STRING="${CB_LOADER_CONN_STRING:-couchbase://localhost}"
  export CB_LOADER_USERNAME="${CB_LOADER_USERNAME:-${CB_ADMIN_USERNAME:-Administrator}}"
  export CB_LOADER_PASSWORD="${CB_LOADER_PASSWORD:-${CB_ADMIN_PASSWORD:-password}}"
  export CB_SEARCH_HOST_URL="${CB_SEARCH_HOST_URL:-http://localhost:8094}"
  export EMBEDDING_LOADER_BASE_URL="${EMBEDDING_LOADER_BASE_URL:-${OLLAMA_EMBED_HOST_URL:-http://localhost:11434}}"
  export CB_SEARCH_ADMIN_USERNAME="${CB_SEARCH_ADMIN_USERNAME:-${CB_ADMIN_USERNAME:-Administrator}}"
  export CB_SEARCH_ADMIN_PASSWORD="${CB_SEARCH_ADMIN_PASSWORD:-${CB_ADMIN_PASSWORD:-password}}"
fi

LOADER_EMBED_URL="${EMBEDDING_LOADER_BASE_URL:-${EMBEDDING_BASE_URL:-${OLLAMA_EMBED_HOST_URL:-http://localhost:11434}}}"
OLLAMA_TAGS_URL="${LOADER_EMBED_URL%/v1}/api/tags"
USES_OLLAMA=false
case "$LOADER_EMBED_URL" in
  *ollama*|*localhost:11434*|*127.0.0.1:11434*) USES_OLLAMA=true ;;
esac

if [[ "$USES_OLLAMA" == "true" ]]; then
  if ! curl -fsS "$OLLAMA_TAGS_URL" >/dev/null 2>&1; then
    echo "Ollama embedding endpoint is not reachable at $LOADER_EMBED_URL." >&2
    echo "Run ./scripts/01-start-ollama.sh or configure EMBEDDING_LOADER_BASE_URL." >&2
    exit 1
  fi

  if docker container inspect ollama >/dev/null 2>&1; then
    EMBED_MODEL="${OLLAMA_EMBED_MODEL:-${EMBEDDING_MODEL:-nomic-embed-text}}"
    MODEL_NAMES="$(docker exec ollama ollama list | awk 'NR>1 {print $1}')"
    if grep -Fxq "$EMBED_MODEL" <<<"$MODEL_NAMES" || \
       { [[ "$EMBED_MODEL" != *:* ]] && grep -Fxq "${EMBED_MODEL}:latest" <<<"$MODEL_NAMES"; }; then
      echo "Embedding model already present: $EMBED_MODEL"
    else
      echo "Pulling embedding model: $EMBED_MODEL"
      docker exec ollama ollama pull "$EMBED_MODEL" >/dev/null
    fi
  fi
else
  echo "Using configured embedding endpoint: $LOADER_EMBED_URL"
fi

VENV="$ROOT_DIR/.venv-loader"
if [[ ! -x "$VENV/bin/python" ]]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV/bin/python" -m pip install -r "$ROOT_DIR/tools/requirements.txt" >/dev/null

"$VENV/bin/python" "$ROOT_DIR/tools/load_catalogue.py"
