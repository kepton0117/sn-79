#!/usr/bin/env bash
# Deploy HelpAgent on GOLD SN79 miners to support TopAgent UID 108.
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
  "80:sn986:9131"
)

echo "Deploying ${#HELPERS[@]} HelpAgents for help_uid=108 (TopAgent on sn989:9135)..."

for entry in "${HELPERS[@]}"; do
  IFS=: read -r uid hotkey port <<< "$entry"
  cfg="$REPO_ROOT/agents/data/helpagent.${uid}.json"
  python3 - <<PY
import json
from pathlib import Path
p = Path("$cfg")
data = json.loads(p.read_text()) if p.is_file() else {}
data["help_uid"] = 108
data["target_config_path"] = "agents/data/topagent.108.json"
p.write_text(json.dumps(data, indent=2) + "\n")
print(f"Updated {p.name}: help_uid=108")
PY
  echo "--- UID $uid hotkey=$hotkey port=$port ---"
  HOTKEY_NAME="$hotkey" \
  AXON_PORT="$port" \
  AGENT_PARAMS="live_config=1 live_config_path=agents/data/helpagent.${uid}.json help_uid=108 target_config_path=agents/data/topagent.108.json max_maker_fee_rate=0.005" \
  "$REPO_ROOT/mainnet/run_helpagent.sh" 2>&1 | tail -5
done

echo ""
echo "Done. Target TopAgent: miner-sn79-gold-9135-sn989 (UID 108)"
pm2 list | grep miner-sn79 || true
