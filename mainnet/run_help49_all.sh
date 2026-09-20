#!/usr/bin/env bash
# Deploy HelpAgent on all GOLD SN79 miners except UID 49 (TopAgent target).
# 8 helpers × max_help_qty 25 → up to 200 base/tick toward UID 49 open books.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# uid:hotkey:port
HELPERS=(
  "228:sn984:9124"
  "90:sn980:9125"
  "163:sn979:9126"
  "105:sn977:9127"
  "60:sn983:9128"
  "46:sn981:9129"
  "203:sn985:9130"
  "80:sn986:9131"
)

echo "Deploying ${#HELPERS[@]} HelpAgents for help_uid=49 (TopAgent stays on sn982:9132)..."

for entry in "${HELPERS[@]}"; do
  IFS=: read -r uid hotkey port <<< "$entry"
  echo "--- UID $uid hotkey=$hotkey port=$port ---"
  HOTKEY_NAME="$hotkey" \
  AXON_PORT="$port" \
  AGENT_PARAMS="live_config=1 live_config_path=agents/data/helpagent.${uid}.json help_uid=49 target_config_path=agents/data/topagent.49.json max_maker_fee_rate=0.005" \
  "$REPO_ROOT/mainnet/run_helpagent.sh" 2>&1 | tail -5
done

echo ""
echo "Done. Target TopAgent: miner-sn79-gold-9132-sn982 (UID 49)"
pm2 list | grep miner-sn79 || true
