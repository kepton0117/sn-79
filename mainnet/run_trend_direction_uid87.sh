#!/usr/bin/env bash
# PM2 wrapper: trend-based open_direction for TopAgent UID 87.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export TREND_TARGET_UID="${TREND_TARGET_UID:-87}"
export TREND_LOOKBACK_MIN="${TREND_LOOKBACK_MIN:-10}"
export TREND_FLAT_PCT="${TREND_FLAT_PCT:-0.001}"
export TREND_MIN_TRADES="${TREND_MIN_TRADES:-2}"
export TREND_INTERVAL_SEC="${TREND_INTERVAL_SEC:-15}"
exec python3 "$REPO_ROOT/mainnet/trend_direction_uid87.py"
