#!/usr/bin/env bash
set -euo pipefail

NETWORK_NAME="${NETWORK_NAME:-agent-memory-network}"
OLLAMA_CONTAINER="${OLLAMA_CONTAINER:-ollama}"
LLM_MODEL="${OLLAMA_LLM_MODEL:-${LLM_MODEL:-llama3.2:1b}}"
EMBEDDING_MODEL="${OLLAMA_EMBED_MODEL:-${EMBEDDING_MODEL:-nomic-embed-text}}"

model_present() {
  local model="$1"
  local names
  names="$(docker exec "$OLLAMA_CONTAINER" ollama list | awk 'NR>1 {print $1}')"
  if grep -Fxq "$model" <<<"$names"; then
    return 0
  fi
  if [[ "$model" != *:* ]] && grep -Fxq "${model}:latest" <<<"$names"; then
    return 0
  fi
  return 1
}

docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 || docker network create "$NETWORK_NAME" >/dev/null

if docker container inspect "$OLLAMA_CONTAINER" >/dev/null 2>&1; then
  docker start "$OLLAMA_CONTAINER" >/dev/null || true
  docker network connect "$NETWORK_NAME" "$OLLAMA_CONTAINER" 2>/dev/null || true
else
  docker run -d \
    --name "$OLLAMA_CONTAINER" \
    --network "$NETWORK_NAME" \
    -p 11434:11434 \
    -v ollama-models:/root/.ollama \
    --restart unless-stopped \
    ollama/ollama >/dev/null
fi

echo "Waiting for Ollama..."
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS http://localhost:11434/api/tags >/dev/null

if model_present "$LLM_MODEL"; then
  echo "LLM already present: $LLM_MODEL"
else
  echo "Pulling LLM: $LLM_MODEL"
  docker exec "$OLLAMA_CONTAINER" ollama pull "$LLM_MODEL"
fi

if model_present "$EMBEDDING_MODEL"; then
  echo "Embedding model already present: $EMBEDDING_MODEL"
else
  echo "Pulling embedding model: $EMBEDDING_MODEL"
  docker exec "$OLLAMA_CONTAINER" ollama pull "$EMBEDDING_MODEL"
fi

echo
docker exec "$OLLAMA_CONTAINER" ollama list
