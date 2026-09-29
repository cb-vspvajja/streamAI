#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$ROOT/scripts/capella-common.sh"
compose stop ui memory mcp
compose stop usage
echo "Capella demo containers stopped. Data remains in Capella."
