#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT_DIR/.env}"
NETWORK_NAME="${NETWORK_NAME:-agent-memory-network}"
CONTAINER_NAME="${CONTAINER_NAME:-agentmemory-server}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: $ENV_FILE does not exist." >&2
  echo "Run: cp '$ROOT_DIR/.env.example' '$ROOT_DIR/.env'" >&2
  exit 1
fi

# Preserve explicit shell overrides, then load the deployment profile. Docker
# --env-file does not expand ${VAR} references, so the shell resolves them.
OVERRIDE_AGENT_MEMORY_IMAGE="${AGENT_MEMORY_IMAGE-}"
OVERRIDE_AGENT_MEMORY_IMAGE_TAR="${AGENT_MEMORY_IMAGE_TAR-}"
OVERRIDE_AGENT_MEMORY_AUTO_LOAD="${AGENT_MEMORY_AUTO_LOAD-}"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
[[ -n "$OVERRIDE_AGENT_MEMORY_IMAGE" ]] && AGENT_MEMORY_IMAGE="$OVERRIDE_AGENT_MEMORY_IMAGE"
[[ -n "$OVERRIDE_AGENT_MEMORY_IMAGE_TAR" ]] && AGENT_MEMORY_IMAGE_TAR="$OVERRIDE_AGENT_MEMORY_IMAGE_TAR"
[[ -n "$OVERRIDE_AGENT_MEMORY_AUTO_LOAD" ]] && AGENT_MEMORY_AUTO_LOAD="$OVERRIDE_AGENT_MEMORY_AUTO_LOAD"

case "$(uname -m)" in
  arm64|aarch64)
    DOCKER_ARCH="arm64"
    DEFAULT_IMAGE="agentmemory-server:arm64"
    TAR_ARCH_PATTERN='(arm64|aarch64)'
    ;;
  x86_64|amd64)
    DOCKER_ARCH="amd64"
    DEFAULT_IMAGE="agentmemory-server:amd64"
    TAR_ARCH_PATTERN='(amd64|x86_64)'
    ;;
  *)
    echo "ERROR: unsupported architecture: $(uname -m)" >&2
    exit 1
    ;;
esac

image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}

image_architecture() {
  docker image inspect --format '{{.Architecture}}' "$1" 2>/dev/null || true
}

find_compatible_loaded_image() {
  local candidate=""
  local candidate_arch=""
  local candidate_lower=""
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    candidate_lower="$(printf '%s' "$candidate" | tr '[:upper:]' '[:lower:]')"
    case "$candidate_lower" in
      *agentmemory*|*agent-memory*) ;;
      *) continue ;;
    esac
    candidate_arch="$(image_architecture "$candidate")"
    if [[ "$candidate_arch" == "$DOCKER_ARCH" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)
  return 1
}

find_local_agent_memory_tar() {
  local explicit="${AGENT_MEMORY_IMAGE_TAR:-}"
  local candidate=""
  if [[ -n "$explicit" ]]; then
    if [[ -f "$explicit" ]]; then
      printf '%s\n' "$explicit"
      return 0
    fi
    echo "ERROR: AGENT_MEMORY_IMAGE_TAR does not exist: $explicit" >&2
    return 1
  fi

  # Look only in the project directory and its parent. This avoids GNU-only
  # find flags and remains compatible with the Bash/macOS toolchain.
  for candidate in "$ROOT_DIR"/*.tar "$(dirname "$ROOT_DIR")"/*.tar; do
    [[ -f "$candidate" ]] || continue
    if printf '%s\n' "$(basename "$candidate")" | grep -Eiq "agent[-_]?memory|agentmemory" \
      && printf '%s\n' "$(basename "$candidate")" | grep -Eiq "$TAR_ARCH_PATTERN"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

resolve_agent_memory_image() {
  local requested="${AGENT_MEMORY_IMAGE:-}"
  local compatible=""
  local image_tar=""

  if [[ -n "$requested" ]] && image_exists "$requested"; then
    printf '%s\n' "$requested"
    return 0
  fi

  if image_exists "$DEFAULT_IMAGE"; then
    printf '%s\n' "$DEFAULT_IMAGE"
    return 0
  fi

  compatible="$(find_compatible_loaded_image || true)"
  if [[ -n "$compatible" ]]; then
    echo "Found compatible Agent Memory image under tag: $compatible" >&2
    echo "Creating stable local tag: $DEFAULT_IMAGE" >&2
    docker tag "$compatible" "$DEFAULT_IMAGE"
    printf '%s\n' "$DEFAULT_IMAGE"
    return 0
  fi

  if [[ "${AGENT_MEMORY_AUTO_LOAD:-true}" == "true" ]]; then
    image_tar="$(find_local_agent_memory_tar || true)"
    if [[ -n "$image_tar" ]]; then
      echo "Loading Couchbase Agent Memory image from: $image_tar" >&2
      docker load -i "$image_tar" >&2

      if [[ -n "$requested" ]] && image_exists "$requested"; then
        printf '%s\n' "$requested"
        return 0
      fi
      if image_exists "$DEFAULT_IMAGE"; then
        printf '%s\n' "$DEFAULT_IMAGE"
        return 0
      fi
      compatible="$(find_compatible_loaded_image || true)"
      if [[ -n "$compatible" ]]; then
        echo "Loaded compatible image as: $compatible" >&2
        echo "Creating stable local tag: $DEFAULT_IMAGE" >&2
        docker tag "$compatible" "$DEFAULT_IMAGE"
        printf '%s\n' "$DEFAULT_IMAGE"
        return 0
      fi
    fi
  fi

  echo "ERROR: no compatible Couchbase Agent Memory Docker image is available for $DOCKER_ARCH." >&2
  if [[ -n "$requested" ]]; then
    echo "Requested image: $requested" >&2
  fi
  echo "Expected stable tag: $DEFAULT_IMAGE" >&2
  echo "Current Docker context: $(docker context show 2>/dev/null || echo unknown)" >&2
  echo >&2
  echo "Load the Couchbase-provided image tar, for example:" >&2
  echo "  docker load -i /path/to/agentmemory-server-${DOCKER_ARCH}-1.0.0.tar" >&2
  echo >&2
  echo "If it loads with another repository/tag, either rerun this script (auto-detection will retag it)," >&2
  echo "or set one of these in .env:" >&2
  echo "  AGENT_MEMORY_IMAGE=<loaded-repository:tag>" >&2
  echo "  AGENT_MEMORY_IMAGE_TAR=/absolute/path/to/agentmemory-server-${DOCKER_ARCH}.tar" >&2
  echo >&2
  echo "Relevant loaded images:" >&2
  docker image ls --format '  {{.Repository}}:{{.Tag}}  {{.ID}}' 2>/dev/null | grep -Ei 'agent.?memory|agentmemory' >&2 || true
  exit 1
}

IMAGE_NAME="$(resolve_agent_memory_image)"
echo "Using Agent Memory image: $IMAGE_NAME"

docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null

# Recreate the container every time. A restart does not reload an edited .env file.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# Resolve the critical settings after sourcing .env. This avoids relying on
# Docker --env-file variable interpolation, which Docker does not perform.
TARGET="${DEPLOYMENT_TARGET:-local}"
if [[ -z "${AGENTMEMORY_CONN_STRING:-}" ]]; then
  if [[ "$TARGET" == "local" ]]; then
    AGENTMEMORY_CONN_STRING="couchbase://host.docker.internal"
  else
    AGENTMEMORY_CONN_STRING="${CB_CONN_STRING:?CB_CONN_STRING or AGENTMEMORY_CONN_STRING is required}"
  fi
fi
AGENTMEMORY_USERNAME="${AGENTMEMORY_USERNAME:-agentMemory}"
AGENTMEMORY_PASSWORD="${AGENTMEMORY_PASSWORD:-password}"
AGENTMEMORY_BUCKET="${AGENTMEMORY_BUCKET:-agent_memory}"
AGENTMEMORY_EMBEDDING_MODEL="${AGENTMEMORY_EMBEDDING_MODEL:-${EMBEDDING_MODEL:-nomic-embed-text}}"
AGENTMEMORY_LLM_MODEL="${AGENTMEMORY_LLM_MODEL:-${CHAT_MODEL:-llama3.2:1b}}"
AGENTMEMORY_EMBEDDING_URL="${AGENTMEMORY_EMBEDDING_URL:-http://ollama:11434/v1}"
AGENTMEMORY_LLM_URL="${AGENTMEMORY_LLM_URL:-http://ollama:11434/v1}"
AGENTMEMORY_OPENAI_API_KEY="${AGENTMEMORY_OPENAI_API_KEY:-${OPENAI_API_KEY:-${CHAT_API_KEY:-ollama}}}"

docker run -d \
  --name "$CONTAINER_NAME" \
  --network "$NETWORK_NAME" \
  --env-file "$ENV_FILE" \
  -e "AGENTMEMORY_CONN_STRING=$AGENTMEMORY_CONN_STRING" \
  -e "AGENTMEMORY_USERNAME=$AGENTMEMORY_USERNAME" \
  -e "AGENTMEMORY_PASSWORD=$AGENTMEMORY_PASSWORD" \
  -e "AGENTMEMORY_BUCKET=$AGENTMEMORY_BUCKET" \
  -e "AGENTMEMORY_EMBEDDING_MODEL=$AGENTMEMORY_EMBEDDING_MODEL" \
  -e "AGENTMEMORY_EMBEDDING_URL=$AGENTMEMORY_EMBEDDING_URL" \
  -e "AGENTMEMORY_LLM_MODEL=$AGENTMEMORY_LLM_MODEL" \
  -e "AGENTMEMORY_LLM_URL=$AGENTMEMORY_LLM_URL" \
  -e "OPENAI_API_KEY=$AGENTMEMORY_OPENAI_API_KEY" \
  -p 8080:8080 \
  -p 9090:9090 \
  -v agentmemory-logs:/app/logs \
  --restart unless-stopped \
  "$IMAGE_NAME" >/dev/null

echo "Agent Memory container created from: $ENV_FILE"
echo "Verifying critical values loaded into the container:"
docker exec "$CONTAINER_NAME" /opt/venv/bin/python -c '
import os
for key in (
    "AGENTMEMORY_CONN_STRING",
    "AGENTMEMORY_USERNAME",
    "AGENTMEMORY_BUCKET",
    "AGENTMEMORY_EMBEDDING_MODEL",
    "AGENTMEMORY_EMBEDDING_URL",
    "AGENTMEMORY_LLM_MODEL",
    "AGENTMEMORY_LLM_URL",
):
    print(f"  {key}={os.environ.get(key)!r}")
'

echo "Waiting for Agent Memory health endpoint..."
for attempt in $(seq 1 120); do
  if response="$(curl -fsS http://localhost:8080/health 2>/dev/null)"; then
    echo "$response"
    exit 0
  fi
  if (( attempt % 10 == 0 )); then
    echo "  still starting (attempt $attempt/120)"
  fi
  sleep 2
done

echo "ERROR: Agent Memory did not become healthy." >&2
docker logs --tail 200 "$CONTAINER_NAME" >&2
exit 1
