#!/usr/bin/env bash
# Deploy HelpAgent UID 160 (sn995:9141) to support TopAgent UID 5 (sn1000:9146).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

HOTKEY_NAME="${HOTKEY_NAME:-sn995}" \
AXON_PORT="${AXON_PORT:-9141}" \
AGENT_PARAMS="live_config=1 live_config_path=agents/data/helpagent.160.json help_uid=5 target_config_path=agents/data/topagent.5.json max_maker_fee_rate=0.005" \
"$REPO_ROOT/mainnet/run_helpagent.sh"
