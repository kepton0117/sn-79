#!/usr/bin/env bash
# Deploy HelpAgent on all GOLD SN79 miners except UID 179 (TopAgent target).
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

echo "Deploying ${#HELPERS[@]} HelpAgents for help_uid=179 (TopAgent stays on sn988:9134)..."

for entry in "${HELPERS[@]}"; do
  IFS=: read -r uid hotkey port <<< "$entry"
  echo "--- UID $uid hotkey=$hotkey port=$port ---"
  HOTKEY_NAME="$hotkey" \
  AXON_PORT="$port" \
  AGENT_PARAMS="live_config=1 live_config_path=agents/data/helpagent.${uid}.json help_uid=179 target_config_path=agents/data/topagent.179.json max_maker_fee_rate=0.005" \
  "$REPO_ROOT/mainnet/run_helpagent.sh" 2>&1 | tail -5
done

echo ""
echo "Done. Target TopAgent: miner-sn79-gold-9134-sn988 (UID 179)"
pm2 list | grep miner-sn79 || true
