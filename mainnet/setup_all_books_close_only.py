#!/usr/bin/env python3
"""Set TopAgent to close-only on all 128 books (no open_books)."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_UIDS = (87, 179)
TARGET_PRICE = 0.25


def close_only_config(existing: dict | None = None) -> dict:
    base = dict(existing or {})
    base["open_books"] = []
    base["close_books"] = [
        {"book": b, "target_price": TARGET_PRICE} for b in range(128)
    ]
    base.setdefault("close_quantity", 0.25)
    base.setdefault("open_quantity", 200)
    base.setdefault("open_depth_qty", 300)
    base.setdefault("open_depth_levels", 21)
    base.setdefault("use_depth_qty", True)
    base.setdefault("expiry_period", 200_000_000_000)
    base.setdefault("max_maker_fee_rate", 0.005)
    base.setdefault("live_config", 1)
    base["close_max_books_per_tick"] = 150
    base["close_max_orders_per_tick"] = 150
    base["direction_trend_enabled"] = False
    return base


def main() -> int:
    for uid in TARGET_UIDS:
        path = REPO / f"agents/data/topagent.{uid}.json"
        existing = None
        if path.is_file():
            existing = json.loads(path.read_text())
        cfg = close_only_config(existing)
        path.write_text(json.dumps(cfg, indent=2) + "\n")
        print(
            f"Wrote {path}: open_books={len(cfg['open_books'])} "
            f"close_books={len(cfg['close_books'])} "
            f"target_price={TARGET_PRICE}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
