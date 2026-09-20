#!/usr/bin/env bash
# Deploy CppBridgeAgent + engine7.cpp on SN79 mainnet (same pattern as UID 60 fleet).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
pip install -e . -q

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
  SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  GIT_HTTP_PROXY GIT_HTTPS_PROXY || true
export NO_PROXY='*'
export no_proxy='*'
export BT_NO_PARSE_CLI_ARGS=false

ENDPOINT="${ENDPOINT:-wss://lite.sub.latent.to:443}"
WALLET_PATH="${WALLET_PATH:-/root/.bittensor/wallets/}"
WALLET_NAME="${WALLET_NAME:-GOLD}"
HOTKEY_NAME="${HOTKEY_NAME:-sn985}"
NETUID="${NETUID:-79}"
AXON_PORT="${AXON_PORT:-9130}"
EXTERNAL_IP="${EXTERNAL_IP:-176.9.86.107}"
AGENT_PATH="${AGENT_PATH:-/root/subnetlist/sn-79/agents/cpp_engine}"
AGENT_NAME="${AGENT_NAME:-CppBridgeAgent}"
ENGINE_BIN="${ENGINE_BIN:-/root/subnetlist/MVTRX_06_02/sn-79/agents/cpp_miner/build/engine7_engine}"
AGENT_PARAMS="${AGENT_PARAMS:-lazy_load=1 auto_build=0 engine=${ENGINE_BIN} quantity=0.25 signal_threshold=0.08 strong_threshold=0.35 depth_levels=5 min_spread_ticks=1 expiry_period=120000000000 solution_log=0}"

PM2_NAME="miner-sn79-${WALLET_NAME,,}-${AXON_PORT}-${HOTKEY_NAME}"

if [ ! -x "$ENGINE_BIN" ]; then
  echo "Missing engine7 binary: $ENGINE_BIN" >&2
  exit 1
fi

read -ra _agent_params <<< "$AGENT_PARAMS"

pm2 delete "$PM2_NAME" 2>/dev/null || true
pm2 start "$REPO_ROOT/mainnet/run_miner_noproxy.sh" \
  --name="$PM2_NAME" \
  --interpreter bash \
  --cwd "$REPO_ROOT/taos/im/neurons" \
  -- \
  "$REPO_ROOT/taos/im/neurons/miner.py" \
  --netuid "$NETUID" \
  --subtensor.chain_endpoint "$ENDPOINT" \
  --wallet.path "$WALLET_PATH" \
  --wallet.name "$WALLET_NAME" \
  --wallet.hotkey "$HOTKEY_NAME" \
  --axon.port "$AXON_PORT" \
  --axon.external_ip "$EXTERNAL_IP" \
  --axon.external_port "$AXON_PORT" \
  --logging.info \
  --agent.path "$AGENT_PATH" \
  --agent.name "$AGENT_NAME" \
  --agent.params "${_agent_params[@]}"
pm2 save || true
echo "Started $PM2_NAME (CppBridgeAgent engine7 hotkey=$HOTKEY_NAME port=$AXON_PORT)"
pm2 logs "$PM2_NAME" --lines 25 --nostream
