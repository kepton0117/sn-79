#!/usr/bin/env bash
# One-shot: 128 open books + static directions from trade history for TopAgent UID 108.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
exec python3 "$REPO_ROOT/mainnet/setup_all_books_static_direction.py" --uids 108
