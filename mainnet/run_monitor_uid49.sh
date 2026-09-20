#!/usr/bin/env bash
# PM2 wrapper: monitor UID 49 (detail book 73).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MONITOR_INTERVAL_SEC="${MONITOR_INTERVAL_SEC:-15}"
export MONITOR_DETAIL_BOOKS="${MONITOR_DETAIL_BOOKS:-42,45,73,78,125}"
exec python3 "$REPO_ROOT/mainnet/monitor_uid49_books.py"
