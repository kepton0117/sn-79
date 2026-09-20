#!/usr/bin/env bash
# Restart every GOLD SN79 axon in _topagent_fleet_targets.txt on TopAgent.py (+ optional extras).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGETS="$REPO_ROOT/mainnet/_topagent_fleet_targets.txt"
# Not in _topagent_fleet_targets.txt (141 shares hotkey sn1003 with fleet UID 232 — pick one)
EXTRA_UIDS="${EXTRA_UIDS:-178 187}"

ensure_config() {
  local uid="$1"
  local cfg="$REPO_ROOT/agents/data/topagent.${uid}.json"
  mkdir -p "$REPO_ROOT/agents/data/${uid}"
  if [[ -f "$cfg" ]]; then
    return 0
  fi
  python3 - <<PY
import json
from pathlib import Path
uid = int("${uid}")
p = Path("${cfg}")
live = Path("${REPO_ROOT}/agents/topagent.live.json")
if live.is_file():
    data = json.loads(live.read_text())
else:
    data = {}
data.setdefault("live_config", 1)
data.setdefault("open_books", [])
data.setdefault("close_books", [])
data.setdefault("stop_books", [])
p.write_text(json.dumps(data, indent=2) + "\n")
print(f"created {p.name} from template")
PY
}

deploy_uid() {
  local uid="$1"
  local script="$REPO_ROOT/mainnet/run_topagent${uid}.sh"
  if [[ ! -f "$script" ]]; then
    echo "SKIP uid=$uid (no run_topagent${uid}.sh)"
    return 0
  fi
  ensure_config "$uid"
  echo ""
  echo "========== TopAgent UID $uid =========="
  if ! bash "$script"; then
    echo "FAILED uid=$uid" >&2
    return 1
  fi
  return 0
}

failed=0
while read -r uid _port _hk; do
  [[ -z "${uid:-}" || "$uid" =~ ^# ]] && continue
  deploy_uid "$uid" || failed=$((failed + 1))
done < "$TARGETS"

for uid in $EXTRA_UIDS; do
  deploy_uid "$uid" || failed=$((failed + 1))
done

echo ""
echo "Fleet TopAgent deploy done. failures=$failed"
pm2 list | grep miner-sn79-gold || true
exit "$failed"
