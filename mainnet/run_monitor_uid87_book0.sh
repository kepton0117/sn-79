#!/usr/bin/env bash
# PM2 wrapper: monitor TopAgent UID 87 book 0.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-15}"
export MONITOR_BOOK_ID=0
exec python3 "$REPO_ROOT/mainnet/monitor_uid87_book0.py"
