#!/usr/bin/env bash
# Deploy HelpAgents for TopAgent UID 178: helpers 147 + 187 (sharded, same-tick IOC).
#
# Shard split (helpagent.*.json help_shard_index / help_shard_total):
#   UID 147 → slice 0 of 2
#   UID 187 → slice 1 of 2
#
# Run on the host that owns each chain UID's hotkey. On this GOLD box, only 187
# (sn1004 / 9150) is typical; UID 147 must run HelpAgent on its own axon host.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGET=178
COMMON="live_config=1 target_config_path=$REPO_ROOT/agents/data/topagent.${TARGET}.json help_uid=${TARGET} max_maker_fee_rate=0.005 charge_qty=100 max_help_qty=100 fifo_bootstrap=0 lazy_load=1"

echo "=== HelpAgent UID 187 (local GOLD sn1004:9150) ==="
HOTKEY_NAME="${HOTKEY_187:-sn1004}" \
AXON_PORT="${PORT_187:-9150}" \
AGENT_PARAMS="${COMMON} live_config_path=$REPO_ROOT/agents/data/helpagent.187.json data_dir=$REPO_ROOT/agents/data/187" \
"$REPO_ROOT/mainnet/run_help187.sh"

if [[ "${DEPLOY_147:-0}" == "1" ]]; then
  echo "=== HelpAgent UID 147 (set HOTKEY_147 and PORT_147 for this host) ==="
  : "${HOTKEY_147:?Set HOTKEY_147 to the wallet hotkey registered as UID 147}"
  : "${PORT_147:?Set PORT_147 to axon port for UID 147}"
  mkdir -p "$REPO_ROOT/agents/data/147"
  HOTKEY_NAME="$HOTKEY_147" \
  AXON_PORT="$PORT_147" \
  ENDPOINT="${ENDPOINT:-wss://entrypoint-finney.opentensor.ai:443}" \
  AGENT_PARAMS="${COMMON} live_config_path=$REPO_ROOT/agents/data/helpagent.147.json data_dir=$REPO_ROOT/agents/data/147" \
  "$REPO_ROOT/mainnet/run_helpagent.sh"
else
  echo ""
  echo "UID 147 not started here (DEPLOY_147=0)."
  echo "On the UID 147 machine, copy helpagent.147.json and run HelpAgent with:"
  echo "  help_uid=178 help_shard_index=0 help_shard_total=2"
  echo "  live_config_path=.../helpagent.147.json target_config_path=.../topagent.178.json"
fi

echo ""
echo "Target TopAgent: topagent.${TARGET}.json · helpers: help_agent_uids [147, 187]"
pm2 list | grep -E '9150|147|178' || true
