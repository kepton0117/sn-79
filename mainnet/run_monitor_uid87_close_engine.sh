#!/usr/bin/env bash
# PM2 wrapper: monitor TopAgent UID 87 close engine throughput.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-20}"
export MONITOR_INTERVAL_SEC
exec python3 "$REPO_ROOT/mainnet/monitor_uid87_close_engine.py"
