#!/usr/bin/env bash
# PM2 wrapper: monitor HelpAgent orders supporting TopAgent UID 87.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-15}"
export MONITOR_TARGET_UID=87
export MONITOR_DETAIL_BOOKS="${MONITOR_DETAIL_BOOKS:-0}"
exec python3 "$REPO_ROOT/mainnet/monitor_uid87_helpers.py"
