#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOYMENT_TARGET=local "$ROOT_DIR/scripts/06-start-all.sh"
