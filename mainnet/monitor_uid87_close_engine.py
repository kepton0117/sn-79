#!/usr/bin/env python3
"""Monitor TopAgent UID 87 close engine: throughput, sends, offline replay delta."""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_UID = 87
INTERVAL_SEC = float(os.environ.get("MONITOR_INTERVAL_SEC", "20"))
WINDOW_TICKS = int(os.environ.get("MONITOR_WINDOW_TICKS", "60"))

TOPAGENT_JSON = REPO / f"agents/data/topagent.{TARGET_UID}.json"
WAITING_JSON = REPO / f"agents/data/{TARGET_UID}/waiting_orders.json"
L2_JSON = REPO / f"agents/data/{TARGET_UID}/l2_depth.json"
FIFO_GLOB = REPO / f"agents/data/{TARGET_UID}/fifo_state_*.json"
STATE_JSON = REPO / f"agents/data/{TARGET_UID}/close_engine_monitor.json"
TARGET_LOG = Path.home() / ".pm2/logs/miner-sn79-gold-9133-sn987-out.log"
ERROR_LOG = Path.home() / ".pm2/logs/miner-sn79-gold-9133-sn987-error.log"

CLOSE_ENGINE_RE = re.compile(
    r"close engine inv=(\d+) batch=(\d+) orders=(\d+) build=([\d.]+)s engine=([\d.]+)s cap=(\d+)"
)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def load_json(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def tail_close_engine(path: Path, n: int = 400) -> list[tuple]:
    if not path.is_file():
        return []
    raw = path.read_text(errors="ignore").splitlines()[-n:]
    out: list[tuple] = []
    for line in raw:
        m = CLOSE_ENGINE_RE.search(strip_ansi(line))
        if m:
            out.append(tuple(m.groups()))
    return out


def offline_replay_orders() -> int | None:
    sys.path.insert(0, str(REPO / "agents"))
    try:
        from strategy_engine import process_topagent_close
        from fifo_state_recorder import ValidatorStylePnl
    except ImportError:
        return None

    cfg = load_json(TOPAGENT_JSON)
    l2 = load_json(L2_JSON)
    if not cfg or not l2:
        return None
    wo = load_json(WAITING_JSON) or {}
    fifo_files = sorted(FIFO_GLOB.parent.glob(FIFO_GLOB.name), key=lambda p: p.stat().st_mtime)
    if not fifo_files:
        return None
    fifo = ValidatorStylePnl.load(fifo_files[-1], 128)
    close_specs = {
        int(x["book"]): float(x.get("target_price", 0))
        for x in cfg.get("close_books") or []
        if isinstance(x, dict)
    }
    l2_books = l2.get("books") or {}
    wo_books = wo.get("books") or {}
    books: list[dict] = []
    for bid in sorted(close_specs):
        net = float(fifo.net_qty(bid))
        if abs(net) < 0.25 - 1e-12:
            continue
        slot = l2_books.get(str(bid)) or l2_books.get(bid) or {}
        bids, asks = slot.get("bids") or [], slot.get("asks") or []
        bb = float(bids[0]["price"]) if bids else 0.0
        ba = float(asks[0]["price"]) if asks else 0.0
        entry = float(fifo.avg_entry_price(bid) or 0.0)
        if entry <= 0:
            entry = float(fifo.fifo_long_head_price(bid) or fifo.fifo_short_head_price(bid) or 0.0)
        wo_slot = wo_books.get(str(bid)) or wo_books.get(bid) or {}
        side = 1 if net > 0 else 0
        close_dir = "SELL" if net > 0 else "BUY"
        waiting = list(wo_slot.get("waiting") or [])
        has_pending = any(str(o.get("direction", "")).upper() == close_dir for o in waiting)
        books.append(
            {
                "id": bid,
                "target_price": close_specs[bid],
                "best_bid": bb,
                "best_ask": ba,
                "maker_fee_rate": float(slot.get("fees_maker_rate") or 0.0001),
                "taker_fee_rate": float(slot.get("fees_taker_rate") or 0.0001),
                "free_base": float(slot.get("free_base") or 999),
                "free_quote": float(slot.get("free_quote") or 999999),
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
                "block_new_limit": has_pending or len(waiting) >= 5,
            }
        )
    cap = int(cfg.get("close_max_orders_per_tick", 150))
    result = process_topagent_close(
        {
            "price_decimals": 2,
            "volume_decimals": 4,
            "exchange_min_quantity": 0.25,
            "close_quantity": float(cfg.get("close_quantity", 0.25)),
            "close_dust_threshold": float(cfg.get("close_dust_threshold", 0.51)),
            "expiry_period": int(cfg.get("expiry_period", 200_000_000_000)),
            "max_maker_fee_rate": float(cfg.get("max_maker_fee_rate", 0.005)),
            "close_max_orders_per_tick": cap,
            "books": books,
        }
    )
    return len(result.get("orders") or [])


AUDIT_JSON = REPO / f"agents/data/{TARGET_UID}/close_engine_fok_audit.json"


def fok_eligibility_audit() -> tuple[list[str], dict]:
    """Classify all inventory books + engine decisions."""
    sys.path.insert(0, str(REPO / "agents"))
    try:
        from strategy_engine import (
            process_topagent_close,
            fee_aware_long_close_price,
            fee_aware_short_close_price,
            expected_long_close_profit,
            expected_short_close_profit,
        )
        from fifo_state_recorder import ValidatorStylePnl
    except ImportError as exc:
        return [f"  fok_audit: import error {exc}"], {}

    cfg = load_json(TOPAGENT_JSON)
    l2 = load_json(L2_JSON)
    wo = load_json(WAITING_JSON) or {}
    if not cfg or not l2:
        return ["  fok_audit: missing config or l2"], {}

    fifo_files = sorted(FIFO_GLOB.parent.glob(FIFO_GLOB.name), key=lambda p: p.stat().st_mtime)
    if not fifo_files:
        return ["  fok_audit: no fifo_state"], {}
    fifo = ValidatorStylePnl.load(fifo_files[-1], 128)
    close_specs = {
        int(x["book"]): float(x.get("target_price", 0))
        for x in cfg.get("close_books") or []
        if isinstance(x, dict)
    }
    l2_books = l2.get("books") or {}
    wo_books = wo.get("books") or {}
    cats: dict[str, list[int]] = {
        "fok_ready": [],
        "gtc_only": [],
        "crossed_no_fok": [],
        "skip_bad_close_px": [],
        "no_touch": [],
    }
    books: list[dict] = []

    for bid in sorted(close_specs):
        net = float(fifo.net_qty(bid))
        if abs(net) < 0.25 - 1e-12:
            continue
        target = close_specs[bid]
        slot = l2_books.get(str(bid)) or l2_books.get(bid) or {}
        bids, asks = slot.get("bids") or [], slot.get("asks") or []
        bb = float(bids[0]["price"]) if bids else 0.0
        ba = float(asks[0]["price"]) if asks else 0.0
        entry = float(fifo.avg_entry_price(bid) or 0.0)
        if entry <= 0:
            entry = float(
                fifo.fifo_long_head_price(bid) or fifo.fifo_short_head_price(bid) or 0.0
            )
        wo_slot = wo_books.get(str(bid)) or wo_books.get(bid) or {}
        side = 1 if net > 0 else 0
        close_dir = "SELL" if net > 0 else "BUY"
        waiting = list(wo_slot.get("waiting") or [])
        has_pending = any(str(o.get("direction", "")).upper() == close_dir for o in waiting)
        mf = float(slot.get("fees_maker_rate") or 0.0001)
        tf = float(slot.get("fees_taker_rate") or 0.0001)

        if net > 0:
            cp = fee_aware_long_close_price(entry, target, mf)
            if cp <= 0 or cp <= entry + 1e-12:
                cats["skip_bad_close_px"].append(bid)
            elif bb <= 0:
                cats["no_touch"].append(bid)
            elif bb >= cp - 1e-12:
                exp = expected_long_close_profit(entry, bb, mf, tf)
                cats["fok_ready" if exp > target + 1e-12 else "crossed_no_fok"].append(bid)
            else:
                cats["gtc_only"].append(bid)
        else:
            cp = fee_aware_short_close_price(entry, target, mf)
            if cp <= 0 or cp >= entry - 1e-12:
                cats["skip_bad_close_px"].append(bid)
            elif ba <= 0:
                cats["no_touch"].append(bid)
            elif ba <= cp + 1e-12:
                exp = expected_short_close_profit(entry, ba, mf, tf)
                cats["fok_ready" if exp > target + 1e-12 else "crossed_no_fok"].append(bid)
            else:
                cats["gtc_only"].append(bid)

        books.append(
            {
                "id": bid,
                "target_price": target,
                "best_bid": bb,
                "best_ask": ba,
                "maker_fee_rate": mf,
                "taker_fee_rate": tf,
                "free_base": float(slot.get("free_base") or 999),
                "free_quote": float(slot.get("free_quote") or 999999),
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
                "block_new_limit": has_pending or len(waiting) >= 5,
            }
        )

    cap = int(cfg.get("close_max_orders_per_tick", 150))
    result = process_topagent_close(
        {
            "price_decimals": 2,
            "volume_decimals": 4,
            "exchange_min_quantity": 0.25,
            "close_quantity": float(cfg.get("close_quantity", 0.25)),
            "close_dust_threshold": float(cfg.get("close_dust_threshold", 0.51)),
            "expiry_period": int(cfg.get("expiry_period", 200_000_000_000)),
            "max_maker_fee_rate": float(cfg.get("max_maker_fee_rate", 0.005)),
            "close_max_orders_per_tick": cap,
            "books": books,
        }
    )
    by_dec: dict[str, list[int]] = {}
    for x in result.get("logs") or []:
        by_dec.setdefault(str(x.get("decision")), []).append(int(x["book_id"]))

    sent_fok = sorted(
        x["book_id"]
        for x in wo.get("sent") or []
        if x.get("type") == "limit" and "FOK" in str(x.get("timeInForce", ""))
    )
    fok_engine = sorted(
        int(x["book_id"])
        for x in result.get("logs") or []
        if str(x.get("decision", "")).startswith("fok_") and x.get("orders_sent")
    )
    inv_n = len(books)
    lines = [
        f"  fok_audit: inv={inv_n} fok_ready={len(cats['fok_ready'])} "
        f"gtc_only={len(cats['gtc_only'])} crossed={len(cats['crossed_no_fok'])} "
        f"bad_px={len(cats['skip_bad_close_px'])}",
        f"  engine: fok={len(fok_engine)} resting={len(by_dec.get('resting_wait', []))} "
        f"wait_pending={by_dec.get('wait_pending', [])} "
        f"limits={sum(1 for o in result.get('orders') or [] if str(o.get('type','limit')).lower()=='limit')}",
        f"  live FOK sent: {len(sent_fok)} books",
    ]
    audit = {
        "sim_ts": l2.get("sim_ts"),
        "inventory_books": inv_n,
        "eligibility": {k: len(v) for k, v in cats.items()},
        "eligibility_books": cats,
        "engine_decisions": {k: len(v) for k, v in by_dec.items()},
        "wait_pending_books": by_dec.get("wait_pending", []),
        "fok_engine_books": fok_engine,
        "fok_live_sent": sent_fok,
    }
    return lines, audit


def fmt_waiting() -> list[str]:
    wo = load_json(WAITING_JSON)
    if not wo:
        return ["  waiting_orders: (missing)"]
    sent = wo.get("sent") or []
    limits = [x for x in sent if x.get("type") == "limit"]
    fok = [x for x in limits if "FOK" in str(x.get("timeInForce", ""))]
    gtc = [x for x in limits if "GTC" in str(x.get("timeInForce", ""))]
    cancels = len(sent) - len(limits)
    totals = wo.get("totals") or {}
    lines = [
        f"  sent: {len(limits)} limits ({len(fok)} FOK, {len(gtc)} GTC, {cancels} cancels) "
        f"open={totals.get('open')} waiting={totals.get('waiting')} sim_ts={wo.get('sim_ts')}",
    ]
    if fok:
        lines.append(f"  FOK books: {sorted(x['book_id'] for x in fok)}")
    return lines


def recent_errors() -> list[str]:
    if not ERROR_LOG.is_file():
        return []
    hits = []
    for line in ERROR_LOG.read_text(errors="ignore").splitlines()[-80:]:
        plain = strip_ansi(line)
        if "close engine" in plain.lower() or "TopAgent close" in plain:
            hits.append(plain[-160:])
    return hits[-3:]


def main() -> int:
    history: deque[int] = deque(maxlen=WINDOW_TICKS)
    prev_log_size = TARGET_LOG.stat().st_size if TARGET_LOG.is_file() else 0
    print(
        f"[close-engine-monitor] UID {TARGET_UID} interval={INTERVAL_SEC}s "
        f"window={WINDOW_TICKS} ticks",
        flush=True,
    )
    while True:
        try:
            now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            engines = tail_close_engine(TARGET_LOG)
            if engines:
                history.extend(int(e[2]) for e in engines[-5:])

            latest = engines[-1] if engines else None
            inv = batch = orders = cap = None
            build_s = engine_s = None
            if latest:
                inv, batch, orders, build_s, engine_s, cap = latest
                orders_i = int(orders)
            else:
                orders_i = None

            wo_lines = fmt_waiting()
            fok_lines, fok_audit = fok_eligibility_audit()
            offline = offline_replay_orders()
            errs = recent_errors()

            lines = [f"=== UID {TARGET_UID} close engine @ {now} ==="]
            if latest:
                cap_bind = orders_i is not None and cap and orders_i >= int(cap) - 1
                lines.append(
                    f"  live: inv={inv} batch={batch} orders={orders} "
                    f"build={build_s}s engine={engine_s}s cap={cap}"
                    + (" CAP_BINDING" if cap_bind else "")
                )
            else:
                lines.append("  live: (no close engine log lines)")

            if history:
                hs = list(history)
                lines.append(
                    f"  window({len(hs)}): min={min(hs)} max={max(hs)} "
                    f"avg={statistics.mean(hs):.1f} last={hs[-1]}"
                )
            if offline is not None and orders_i is not None:
                lines.append(
                    f"  offline_replay: {offline} orders (delta={offline - orders_i:+d})"
                )
            lines.extend(wo_lines)
            lines.extend(fok_lines)
            if errs:
                lines.append("  recent errors:")
                lines.extend(f"    {e}" for e in errs)

            cfg = load_json(TOPAGENT_JSON) or {}
            fok_cd = cfg.get("fok_close_cooldown_ns", 0)
            lines.append(
                f"  config: close_max_orders={cfg.get('close_max_orders_per_tick')} "
                f"fok_cooldown_ns={fok_cd}"
            )

            block = "\n".join(lines)
            print(block, flush=True)
            save_json(
                STATE_JSON,
                {
                    "updated_at": now,
                    "latest": {
                        "inv": inv,
                        "batch": batch,
                        "orders": orders_i,
                        "cap": cap,
                        "build_s": build_s,
                        "engine_s": engine_s,
                    },
                    "window": {
                        "n": len(history),
                        "min": min(history) if history else None,
                        "max": max(history) if history else None,
                        "avg": round(statistics.mean(history), 2) if history else None,
                    },
                    "offline_replay_orders": offline,
                    "fok_close_cooldown_ns": fok_cd,
                    "fok_audit": fok_audit,
                },
            )
            if fok_audit:
                save_json(AUDIT_JSON, fok_audit)
        except Exception as exc:
            print(f"[close-engine-monitor] error: {exc}", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())
