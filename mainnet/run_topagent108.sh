#!/usr/bin/env bash
# Deploy TopAgent UID 108 on SN79 mainnet (sn989 / port 9135).
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
HOTKEY_NAME="${HOTKEY_NAME:-sn989}"
NETUID="${NETUID:-79}"
AXON_PORT="${AXON_PORT:-9135}"
EXTERNAL_IP="${EXTERNAL_IP:-176.9.86.107}"
AGENT_PATH="${AGENT_PATH:-$REPO_ROOT/agents}"
AGENT_NAME="${AGENT_NAME:-TopAgent}"
AGENT_PARAMS="${AGENT_PARAMS:-live_config=1 live_config_path=agents/data/topagent.108.json expiry_period=200000000000 max_maker_fee_rate=0.005 fifo_bootstrap=0 lazy_load=1}"

PM2_NAME="miner-sn79-${WALLET_NAME,,}-${AXON_PORT}-${HOTKEY_NAME}"

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
echo "Started $PM2_NAME (TopAgent UID 108 hotkey=$HOTKEY_NAME port=$AXON_PORT lazy_load=1)"
pm2 logs "$PM2_NAME" --lines 25 --nostream
