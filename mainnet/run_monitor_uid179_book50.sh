#!/usr/bin/env bash
# PM2 wrapper: monitor TopAgent UID 179 book 50 (FOK close audit).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export MONITOR_TARGET_UID=179
export MONITOR_BOOK_ID=50
export MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-15}"
exec python3 "$REPO_ROOT/mainnet/monitor_uid87_book0.py"
