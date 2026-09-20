#!/usr/bin/env bash
# Deploy Topstrategy UID 108 on SN79 mainnet (sn993 / port 9098).
# Agent code lives in 08_05; FIFO stays in 08_05 mainnet/data/108.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
if [[ "${SKIP_PIP:-0}" != "1" ]]; then
  pip install -e . -q
fi

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
  SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  GIT_HTTP_PROXY GIT_HTTPS_PROXY || true
export NO_PROXY='*'
export no_proxy='*'
export BT_NO_PARSE_CLI_ARGS=false

ENDPOINT="${ENDPOINT:-wss://lite.sub.latent.to:443}"
WALLET_PATH="${WALLET_PATH:-/root/.bittensor/wallets/}"
WALLET_NAME="${WALLET_NAME:-GOLD}"
HOTKEY_NAME="${HOTKEY_NAME:-sn993}"
NETUID="${NETUID:-79}"
AXON_PORT="${AXON_PORT:-9098}"
EXTERNAL_IP="${EXTERNAL_IP:-176.9.86.107}"
AGENT_PATH="${AGENT_PATH:-/root/MVTRX_08_05/sn-79/agents}"
AGENT_NAME="${AGENT_NAME:-Topstrategy}"
LIVE_CONFIG="${LIVE_CONFIG:-$REPO_ROOT/agents/data/topstrategy.108.json}"
DATA_DIR="${DATA_DIR:-/root/MVTRX_08_05/sn-79/mainnet/data/108}"
AGENT_PARAMS="${AGENT_PARAMS:-live_config=1 live_config_path=$LIVE_CONFIG data_dir=$DATA_DIR expiry_period=10000000000000 max_maker_fee_rate=0.005 lazy_load=1 micro_rest_minutes=0}"

PM2_NAME="miner-sn79-${WALLET_NAME,,}-${AXON_PORT}-${HOTKEY_NAME}"

mkdir -p "$DATA_DIR"
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
echo "Started $PM2_NAME (Topstrategy UID 108 hotkey=$HOTKEY_NAME port=$AXON_PORT)"
pm2 logs "$PM2_NAME" --lines 40 --nostream
