#!/usr/bin/env bash
# Deploy HelpAgent UID 187 (sn1004 / port 9150) targeting TopAgent UID 178.
# Finney endpoint: this hotkey was burned_register'd on finney. No pip.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
  SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  GIT_HTTP_PROXY GIT_HTTPS_PROXY || true
export NO_PROXY='*'
export no_proxy='*'
export BT_NO_PARSE_CLI_ARGS=false

ENDPOINT="${ENDPOINT:-wss://entrypoint-finney.opentensor.ai:443}"
WALLET_PATH="${WALLET_PATH:-/root/.bittensor/wallets/}"
WALLET_NAME="${WALLET_NAME:-GOLD}"
HOTKEY_NAME="${HOTKEY_NAME:-sn1004}"
NETUID="${NETUID:-79}"
AXON_PORT="${AXON_PORT:-9150}"
EXTERNAL_IP="${EXTERNAL_IP:-176.9.86.107}"
AGENT_PATH="${AGENT_PATH:-$REPO_ROOT/agents}"
AGENT_NAME="${AGENT_NAME:-HelpAgent}"
AGENT_PARAMS="${AGENT_PARAMS:-live_config=1 live_config_path=/root/MVTRX_08_29/sn-79/agents/data/helpagent.187.json help_uid=178 target_config_path=/root/MVTRX_08_29/sn-79/agents/data/topagent.178.json data_dir=/root/MVTRX_08_29/sn-79/agents/data/187 max_maker_fee_rate=0.005 charge_qty=100 max_help_qty=100 fifo_bootstrap=0 lazy_load=1}"

PM2_NAME="miner-sn79-${WALLET_NAME,,}-${AXON_PORT}-${HOTKEY_NAME}"

read -ra _agent_params <<< "$AGENT_PARAMS"

mkdir -p "$REPO_ROOT/agents/data/187"

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
echo "Started $PM2_NAME (HelpAgent UID 187 -> help_uid=178 port=$AXON_PORT)"
