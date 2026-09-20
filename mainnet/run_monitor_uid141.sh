#!/usr/bin/env bash
# PM2 wrapper: monitor newly registered Topstrategy UID 141.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-15}"
exec python3 "$REPO_ROOT/mainnet/monitor_uid141.py"
