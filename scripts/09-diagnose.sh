#!/usr/bin/env bash
set -euo pipefail
printf 'Containers:\n'; docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|couchbase|agentmemory|ollama|stream-ai' || true
printf '\nUI health:\n'; curl -sS http://localhost:8088/health || true
printf '\nAgent Memory health:\n'; curl -sS http://localhost:8080/health || true
printf '\nRecent UI logs:\n'; docker logs --tail 120 couchbase-stream-ai-ui 2>&1 || true
printf '\nRecent Agent Memory logs:\n'; docker logs --tail 120 agentmemory-server 2>&1 || true
