#!/usr/bin/env python3
"""Diagnose TopAgent close engine: why only N orders/tick despite high cap."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from strategy_engine import process_topagent_close  # noqa: E402

UID = 87
DATA = REPO / "agents" / "data" / str(UID)


def load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def top_of_book(slot: dict) -> tuple[float, float]:
    bids = slot.get("bids") or []
    asks = slot.get("asks") or []
    bb = float(bids[0]["price"]) if bids else 0.0
    ba = float(asks[0]["price"]) if asks else 0.0
    return bb, ba


def main() -> int:
    cfg = load_json(REPO / f"agents/data/topagent.{UID}.json")
    l2 = load_json(DATA / "l2_depth.json")
    wo = load_json(DATA / "waiting_orders.json") if (DATA / "waiting_orders.json").is_file() else {}
    fifo_files = sorted(DATA.glob("fifo_state_*.json"), key=lambda p: p.stat().st_mtime)
    if not fifo_files:
        print("no fifo_state")
        return 1

    from fifo_state_recorder import ValidatorStylePnl  # type: ignore

    fifo = ValidatorStylePnl.load(fifo_files[-1], 128)
    l2_books = l2.get("books") or {}
    wo_books = wo.get("books") or {}
    close_specs = {int(x["book"]): float(x.get("target_price", 0)) for x in cfg.get("close_books") or [] if isinstance(x, dict)}

    books_payload: list[dict] = []
    inv_books: list[int] = []
    for bid in sorted(close_specs):
        net = float(fifo.net_qty(bid))
        if abs(net) < 0.25 - 1e-12:
            continue
        inv_books.append(bid)
        slot = l2_books.get(str(bid)) or l2_books.get(bid) or {}
        bb, ba = top_of_book(slot)
        entry = float(fifo.avg_entry_price(bid) or 0.0)
        if entry <= 0:
            entry = float(fifo.fifo_long_head_price(bid) or fifo.fifo_short_head_price(bid) or 0.0)
        wo_slot = wo_books.get(str(bid)) or wo_books.get(bid) or {}
        wait_n = len(wo_slot.get("waiting") or [])
        open_n = len(wo_slot.get("open_orders") or [])
        block_new = wait_n >= 5  # max_pending_per_book default
        side = 1 if net > 0 else 0
        has_pending = any(
            str(o.get("direction", "")).upper() == ("SELL" if net > 0 else "BUY")
            for o in (wo_slot.get("waiting") or [])
        )
        books_payload.append(
            {
                "id": bid,
                "target_price": close_specs[bid],
                "best_bid": bb,
                "best_ask": ba,
                "maker_fee_rate": float(slot.get("fees_maker_rate") or slot.get("fees_last_maker_rate") or 0.0001),
                "taker_fee_rate": float(slot.get("fees_taker_rate") or slot.get("fees_last_taker_rate") or 0.0001),
                "free_base": float(slot.get("free_base") or 0.0),
                "free_quote": float(slot.get("free_quote") or 0.0),
                "fifo_long_total_qty": float(fifo.fifo_long_total_qty(bid)),
                "fifo_short_total_qty": float(fifo.fifo_short_total_qty(bid)),
                "fifo_long_lots": fifo.fifo_long_lots_with_fees(bid),
                "fifo_short_lots": fifo.fifo_short_lots_with_fees(bid),
                "fifo_long_head_price": float(fifo.fifo_long_head_price(bid)),
                "fifo_short_head_price": float(fifo.fifo_short_head_price(bid)),
                "net_qty": net,
                "entry_price": entry,
                "open_orders": list(wo_slot.get("open_orders") or []),
                "close_pending_side": side if has_pending else None,
                "fok_cooldown": False,
                "block_new_limit": block_new or has_pending,
            }
        )

    cap = int(cfg.get("close_max_orders_per_tick", 150))
    payload = {
        "price_decimals": 2,
        "volume_decimals": 4,
        "exchange_min_quantity": 0.25,
        "close_quantity": float(cfg.get("close_quantity", 0.25)),
        "close_dust_threshold": float(cfg.get("close_dust_threshold", 0.51)),
        "expiry_period": int(cfg.get("expiry_period", 200_000_000_000)),
        "max_maker_fee_rate": float(cfg.get("max_maker_fee_rate", 0.005)),
        "close_max_orders_per_tick": cap,
        "books": books_payload,
    }

    result = process_topagent_close(payload)
    decisions = Counter(str(x.get("decision") or "?") for x in result.get("logs") or [])
    would_send = len(result.get("orders") or [])
    wants_send = sum(1 for x in result.get("logs") or [] if x.get("orders_sent"))

    print(f"UID {UID} close engine diagnostic (offline replay)")
    print(f"  inventory books: {len(inv_books)}")
    print(f"  close_max_orders_per_tick config: {cap}")
    print(f"  books wanting order (orders_sent in log): {wants_send}")
    print(f"  orders after engine cap trim: {would_send}")
    print(f"  cap binding? {wants_send > would_send} (trimmed {max(0, wants_send - would_send)})")
    print()
    print("Decision counts:")
    for k, v in decisions.most_common():
        print(f"  {k:28s} {v}")
    print()
    fok = [x for x in result.get("logs") or [] if str(x.get("decision", "")).startswith("fok_")]
    gtc = [x for x in result.get("logs") or [] if str(x.get("decision", "")).startswith("gtc_")]
    print(f"  FOK decisions: {len(fok)}  GTC decisions: {len(gtc)}")
    blocked = [x for x in result.get("logs") or [] if "skip" in str(x.get("decision")) or "wait" in str(x.get("decision")) or "cooldown" in str(x.get("decision"))]
    print(f"  skip/wait/cooldown: {len(blocked)}")
    if would_send < wants_send:
        sent_ids = {int(o["book_id"]) for o in result.get("orders") or []}
        trimmed = [x for x in fok + gtc if int(x.get("book_id", -1)) not in sent_ids]
        print(f"\n  Trimmed by cap (first {cap} book ids in iteration order):")
        for x in trimmed[:15]:
            print(f"    book {x.get('book_id')} {x.get('decision')}")
        if len(trimmed) > 15:
            print(f"    ... +{len(trimmed) - 15} more")
    book41 = next((x for x in result.get("logs") or [] if int(x.get("book_id", -1)) == 41), None)
    if book41:
        print(f"\n  book 41: {book41}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
