#!/usr/bin/env bash
# Deploy HelpAgent on SN79 UID 227 (sn1002 / port 9098) targeting TopAgent UID 83.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
  SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  GIT_HTTP_PROXY GIT_HTTPS_PROXY || true
export NO_PROXY='*'
export no_proxy='*'
export BT_NO_PARSE_CLI_ARGS=false

ENDPOINT="${ENDPOINT:-wss://lite.sub.latent.to:443}"
WALLET_PATH="${WALLET_PATH:-/root/.bittensor/wallets/}"
WALLET_NAME="${WALLET_NAME:-GOLD}"
HOTKEY_NAME="${HOTKEY_NAME:-sn1002}"
NETUID="${NETUID:-79}"
AXON_PORT="${AXON_PORT:-9098}"
EXTERNAL_IP="${EXTERNAL_IP:-176.9.86.107}"
AGENT_PATH="${AGENT_PATH:-$REPO_ROOT/agents}"
AGENT_NAME="${AGENT_NAME:-HelpAgent}"
AGENT_PARAMS="${AGENT_PARAMS:-live_config=1 live_config_path=$REPO_ROOT/agents/data/helpagent.227.json help_uid=83 target_config_path=$REPO_ROOT/agents/data/topagent.83.json data_dir=$REPO_ROOT/agents/data/227 charge_qty=50 fifo_bootstrap=0 lazy_load=1}"

PM2_NAME="miner-sn79-${WALLET_NAME,,}-${AXON_PORT}-${HOTKEY_NAME}"

read -ra _agent_params <<< "$AGENT_PARAMS"

mkdir -p "$REPO_ROOT/agents/data/227"

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
echo "Started $PM2_NAME (HelpAgent chain UID 227 -> help_uid=83 port=$AXON_PORT)"
