#!/usr/bin/env bash
set -euo pipefail

missing=0
for cmd in docker curl python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "MISSING: $cmd" >&2
    missing=1
  else
    echo "OK: $cmd -> $(command -v "$cmd")"
  fi
done

docker info >/dev/null 2>&1 || { echo "Docker Desktop is not running." >&2; missing=1; }

if [[ "$(uname -s)" == "Darwin" ]]; then
  if command -v ollama >/dev/null 2>&1; then
    echo "OK: native Ollama available for Apple Metal chat acceleration"
  else
    echo "NOTE: native Ollama is not installed. Docker mode works but chat is slower on macOS."
  fi
fi

(( missing == 0 )) || exit 1
