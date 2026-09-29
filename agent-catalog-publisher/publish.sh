#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${AGENT_CATALOG_SOURCE_DIR:-/workspace/ui/agent_catalog}"
OUTPUT_DIR="${AGENT_CATALOG_CATALOG:-/workspace/ui/.agent-catalog}"
ACTIVITY_DIR="${AGENT_CATALOG_ACTIVITY:-/workspace/ui/.agent-activity}"
PROJECT_DIR=/tmp/streamai-agent-catalog-project
LOCAL_CATALOG_DIR="$PROJECT_DIR/.agent-catalog"
LOCAL_ACTIVITY_DIR="$PROJECT_DIR/.agent-activity"

if [[ "${AGENT_CATALOG_CONN_STRING:-}" == couchbases://* ]]; then
  : "${AGENT_CATALOG_CONN_ROOT_CERTIFICATE:?Set AGENT_CATALOG_CONN_ROOT_CERTIFICATE to the mounted Capella CA certificate path.}"
  [[ -f "$AGENT_CATALOG_CONN_ROOT_CERTIFICATE" && -r "$AGENT_CATALOG_CONN_ROOT_CERTIFICATE" && -s "$AGENT_CATALOG_CONN_ROOT_CERTIFICATE" ]] || {
    echo "Agent Catalog cannot read its Capella CA certificate: $AGENT_CATALOG_CONN_ROOT_CERTIFICATE. Check the certificate file and Docker mount." >&2
    exit 1
  }
fi

python - <<'PY'
try:
    import packaging.version  # noqa: F401
    import sentence_transformers  # noqa: F401
    import agentc  # noqa: F401
    import agentc_cli  # noqa: F401
except Exception as exc:
    raise SystemExit(
        "Agent Catalog publisher dependency check failed. "
        "Rebuild the v1.0.0 image with FORCE_AGENT_CATALOG_REBUILD=true. "
        f"Cause: {type(exc).__name__}: {exc}"
    )
PY

[[ -d "$SOURCE_DIR" ]] || { echo "Missing Agent Catalog source: $SOURCE_DIR" >&2; exit 1; }

# Validate prompt source files before invoking the heavier semantic indexer. The
# current Agent Catalog parser expects each .prompt file to be one YAML document
# with the prompt body stored under the mandatory `content` field.
SOURCE_DIR="$SOURCE_DIR" python - <<'PROMPT_VALIDATE'
import os
from pathlib import Path
import yaml

root = Path(os.environ["SOURCE_DIR"])
paths = sorted(root.rglob("*.prompt"))
for path in paths:
    try:
        with path.open("r", encoding="utf-8") as fp:
            document = yaml.safe_load(fp)
    except Exception as exc:
        raise SystemExit(f"Invalid Agent Catalog prompt YAML: {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemExit(f"Agent Catalog prompt must be one YAML object: {path}")
    missing = [key for key in ("record_kind", "name", "description", "content") if not document.get(key)]
    if missing:
        raise SystemExit(f"Agent Catalog prompt is missing {', '.join(missing)}: {path}")
    if document.get("record_kind") != "prompt":
        raise SystemExit(f"Agent Catalog prompt has record_kind={document.get('record_kind')!r}: {path}")
    if not isinstance(document.get("content"), (str, dict)):
        raise SystemExit(f"Agent Catalog prompt content must be a string or YAML object: {path}")
print(f"Validated {len(paths)} Agent Catalog prompt file(s).")
PROMPT_VALIDATE
rm -rf "$PROJECT_DIR"
mkdir -p "$PROJECT_DIR" "$OUTPUT_DIR" "$ACTIVITY_DIR" "$LOCAL_CATALOG_DIR" "$LOCAL_ACTIVITY_DIR"
cp -R "$SOURCE_DIR" "$PROJECT_DIR/agent_catalog"
cd "$PROJECT_DIR"

git init -q
git config user.email "streamai-demo@couchbase.local"
git config user.name "Couchbase StreamAI Demo"
git add agent_catalog
git commit -q -m "StreamAI Agent Catalog ${APP_VERSION:-1.0.0}"

# agentc 1.1.x writes and reads its local index relative to the Git project.
# Keep that native layout for index/publish, then export the resulting JSON
# files to the bind-mounted UI directory used by the runtime image.
export AGENT_CATALOG_CATALOG=".agent-catalog"
export AGENT_CATALOG_ACTIVITY=".agent-activity"
export AGENT_CATALOG_OUTPUT_DIR="$OUTPUT_DIR"
export AGENT_CATALOG_OUTPUT_ACTIVITY_DIR="$ACTIVITY_DIR"
export AGENT_CATALOG_INTERACTIVE=False

# agentc init establishes the local project metadata. Some package revisions
# initialise implicitly during index, so an already-initialised result is safe.
agentc init >/tmp/agentc-init.log 2>&1 || true
rm -f \
  "$OUTPUT_DIR/tools.json" "$OUTPUT_DIR/prompts.json" \
  "$OUTPUT_DIR/tool-catalog.json" "$OUTPUT_DIR/prompt-catalog.json"
agentc index "$PROJECT_DIR/agent_catalog"
agentc publish --bucket "${AGENT_CATALOG_BUCKET:?AGENT_CATALOG_BUCKET is required}"

# agentc 1.1.x writes its native runtime indexes as tools.json and prompts.json.
# Earlier preview builds used tool-catalog.json and prompt-catalog.json. Export
# the native names consumed by agentc.Catalog(), while also writing legacy
# aliases so an existing v1.5.x operational script can inspect the snapshot.
export_catalog_index() {
  local native_name="$1"
  local legacy_name="$2"
  local source_file=""

  for candidate in \
    "$LOCAL_CATALOG_DIR/$native_name" \
    "$LOCAL_CATALOG_DIR/$legacy_name"; do
    if [[ -s "$candidate" ]]; then
      source_file="$candidate"
      break
    fi
  done

  if [[ -z "$source_file" ]]; then
    source_file="$(find "$PROJECT_DIR" -type f \
      \( -name "$native_name" -o -name "$legacy_name" \) \
      -print -quit)"
  fi

  [[ -n "$source_file" && -s "$source_file" ]] || {
    echo "Agent Catalog publish succeeded but local index is missing: $native_name" >&2
    echo "Expected agentc native file '$native_name' (or legacy '$legacy_name')." >&2
    find "$PROJECT_DIR/.agent-catalog" -maxdepth 2 -type f -print >&2 || true
    exit 1
  }

  cp "$source_file" "$OUTPUT_DIR/$native_name"
  cp "$source_file" "$OUTPUT_DIR/$legacy_name"
}

export_catalog_index tools.json tool-catalog.json
export_catalog_index prompts.json prompt-catalog.json
if [[ -d "$LOCAL_ACTIVITY_DIR" ]]; then
  cp -R "$LOCAL_ACTIVITY_DIR"/. "$ACTIVITY_DIR"/ 2>/dev/null || true
fi

python - <<'PY'
import json, os, subprocess, time
out = os.environ["AGENT_CATALOG_OUTPUT_DIR"]
commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
with open(os.path.join(out, "streamai-publish.json"), "w", encoding="utf-8") as fp:
    json.dump({
        "appVersion": os.getenv("APP_VERSION", "1.0.0"),
        "gitCommit": commit,
        "bucket": os.environ.get("AGENT_CATALOG_BUCKET"),
        "publishedAt": time.time(),
        "source": "Couchbase Agent Catalog CLI",
        "catalogId": commit,
        "nativeIndexFiles": ["tools.json", "prompts.json"],
        "compatibilityAliases": ["tool-catalog.json", "prompt-catalog.json"],
    }, fp, indent=2)
PY

echo "Native Couchbase Agent Catalog published to bucket: ${AGENT_CATALOG_BUCKET}"
