#!/usr/bin/env bash
# Restart all GOLD SN79 axons with CppBridgeAgent + engine7.cpp
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ENGINE_DIR="/root/subnetlist/sn-79/agents/cpp_engine"
ENGINE_BIN="${ENGINE_BIN:-${ENGINE_DIR}/taos_engine7}"

if [ ! -x "$ENGINE_BIN" ]; then
  echo "Building engine7..."
  sh "${ENGINE_DIR}/build.sh" v7
fi

# hotkey:port (all GOLD SN79 axons on this host)
AXONS=(
  "sn984:9124"
  "sn980:9125"
  "sn979:9126"
  "sn977:9127"
  "sn983:9128"
  "sn981:9129"
  "sn985:9130"
  "sn986:9131"
  "sn982:9132"
  "sn987:9133"
  "sn988:9134"
  "sn989:9135"
)

echo "Deploying CppBridgeAgent engine7 on ${#AXONS[@]} axons..."
echo "ENGINE_BIN=$ENGINE_BIN"

for entry in "${AXONS[@]}"; do
  IFS=: read -r hotkey port <<< "$entry"
  echo ""
  echo "=== hotkey=$hotkey port=$port ==="
  HOTKEY_NAME="$hotkey" \
  AXON_PORT="$port" \
  AGENT_PATH="$ENGINE_DIR" \
  ENGINE_BIN="$ENGINE_BIN" \
  AGENT_PARAMS="lazy_load=1 auto_build=0 engine=${ENGINE_BIN} quantity=0.25 signal_threshold=0.08 strong_threshold=0.35 depth_levels=5 min_spread_ticks=1 expiry_period=120000000000 solution_log=0" \
  "$REPO_ROOT/mainnet/run_engine7.sh" 2>&1 | tail -8
done

echo ""
echo "Done. All axons restarted with engine7."
pm2 list | grep miner-sn79 || true
