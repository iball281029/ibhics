#!/bin/bash
# Source-tree entry point; the packaged script is also used by the node agent.
set -euo pipefail
exec bash "$(dirname "$0")/../../ibhics/storage_assets/prepare.sh" "$@"
