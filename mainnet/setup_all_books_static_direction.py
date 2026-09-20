#!/usr/bin/env python3
"""Generate 128-book open_books and one-time static directions for TopAgent UIDs."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TREND_SCRIPT = REPO / "mainnet" / "trend_direction_uid87.py"

TRADE_SOURCES = [108, 87, 179, 46, 60, 80, 90, 105, 163, 228, 203]
DEFAULT_TARGET_UIDS = (87, 179)


def base_config() -> dict:
    return {
        "open_books": [
            {
                "book": b,
                "use_depth_qty": True,
                "open_price": 0,
                "max_quantity": 200.0,
            }
            for b in range(128)
        ],
        "close_books": [],
        "close_quantity": 0.25,
        "open_quantity": 200,
        "open_depth_qty": 300,
        "open_depth_levels": 21,
        "use_depth_qty": True,
        "expiry_period": 200000000000,
        "max_maker_fee_rate": 0.005,
        "live_config": 1,
        "close_max_books_per_tick": 150,
        "close_max_orders_per_tick": 150,
        "direction_trend_enabled": False,
        "direction_trend_lookback_minutes": 10,
        "direction_trend_flat_threshold_pct": 0.001,
        "direction_trend_min_trades": 2,
        "direction_trend_trade_sources": TRADE_SOURCES,
    }


def write_config(uid: int) -> Path:
    path = REPO / f"agents/data/topagent.{uid}.json"
    path.write_text(json.dumps(base_config(), indent=2) + "\n")
    return path


def init_directions(uid: int) -> int:
    books = ",".join(str(b) for b in range(128))
    cmd = [
        sys.executable,
        str(TREND_SCRIPT),
        "--uid",
        str(uid),
        "--init-books",
        books,
    ]
    print(f"Running: {' '.join(cmd[:6])} ... (128 books)")
    return subprocess.call(cmd, cwd=str(REPO))


def summarize(uid: int) -> None:
    path = REPO / f"agents/data/topagent.{uid}.json"
    cfg = json.loads(path.read_text())
    buys = sells = static = 0
    for item in cfg.get("open_books") or []:
        if item.get("direction_static"):
            static += 1
        d = str(item.get("open_direction", "buy")).lower()
        if d == "sell":
            sells += 1
        else:
            buys += 1
    print(f"UID {uid}: {len(cfg.get('open_books', []))} open books, "
          f"buy={buys} sell={sells} static={static}")


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uids",
        type=str,
        default="",
        help="Comma-separated TopAgent UIDs (default: 87,179)",
    )
    args = parser.parse_args()
    if args.uids.strip():
        target_uids = tuple(int(x.strip()) for x in args.uids.split(",") if x.strip())
    else:
        target_uids = DEFAULT_TARGET_UIDS

    for uid in target_uids:
        path = write_config(uid)
        print(f"Wrote {path} (128 open books, close caps 150/150)")
    for uid in target_uids:
        rc = init_directions(uid)
        if rc != 0:
            return rc
        summarize(uid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
