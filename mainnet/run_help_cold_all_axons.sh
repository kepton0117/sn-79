#!/usr/bin/env bash
# Deploy HelpAgent with cold_all_books × cold_warm_rounds on every GOLD SN79 axon.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COLD_PARAMS="cold_all_books=1 cold_warm_qty=0.25 cold_warm_rounds=3 cold_max_slippage=1 cold_warm_once=1 cold_warm_max_books_per_tick=128"

# chain_uid:hotkey:port:help_uid:target_topagent_uid
AXONS=(
  "228:sn984:9124:87:87"
  "90:sn980:9125:87:87"
  "163:sn979:9126:87:87"
  "105:sn977:9127:87:87"
  "60:sn983:9128:87:87"
  "46:sn981:9129:87:87"
  "203:sn985:9130:87:87"
  "80:sn986:9131:87:87"
  "49:sn982:9132:49:49"
  "87:sn987:9133:87:87"
  "179:sn988:9134:179:179"
  "108:sn989:9135:108:108"
)

echo "Updating helpagent JSON + deploying cold warm (3 rounds, all books) on ${#AXONS[@]} axons..."

for entry in "${AXONS[@]}"; do
  IFS=: read -r uid hotkey port help_uid target_uid <<< "$entry"
  cfg="$REPO_ROOT/agents/data/helpagent.${uid}.json"
  python3 - <<PY
import json
from pathlib import Path
uid = int("${uid}")
help_uid = int("${help_uid}")
target_uid = int("${target_uid}")
p = Path("${cfg}")
data = json.loads(p.read_text()) if p.is_file() else {}
data["help_uid"] = help_uid
data["target_config_path"] = f"agents/data/topagent.{target_uid}.json"
data["max_maker_fee_rate"] = 0.005
data["cold_all_books"] = 1
data["cold_warm_qty"] = 0.25
data["cold_warm_rounds"] = 3
data["cold_max_slippage"] = 1
data["cold_warm_once"] = 1
data["cold_warm_max_books_per_tick"] = 128
data.pop("cold_warmed_books", None)
p.write_text(json.dumps(data, indent=2) + "\n")
print(f"Updated helpagent.{uid}.json help_uid={help_uid}")
PY
  echo "--- UID $uid hotkey=$hotkey port=$port ---"
  HOTKEY_NAME="$hotkey" \
  AXON_PORT="$port" \
  AGENT_PARAMS="live_config=1 live_config_path=agents/data/helpagent.${uid}.json help_uid=${help_uid} target_config_path=agents/data/topagent.${target_uid}.json max_maker_fee_rate=0.005 ${COLD_PARAMS}" \
  "$REPO_ROOT/mainnet/run_helpagent.sh" 2>&1 | tail -5
done

echo ""
echo "Done. All axons on HelpAgent cold_all_books × 3."
pm2 list | grep miner-sn79 || true
