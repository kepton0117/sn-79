#!/usr/bin/env python3
"""Monitor TopAgent UID 87 on one book: config, acceptance, FIFO, L2, helpers.

Set MONITOR_BOOK_ID (default 0).
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_UID = int(os.environ.get("MONITOR_TARGET_UID", os.environ.get("MONITOR_UID", "87")))
BOOK_ID = int(os.environ.get("MONITOR_BOOK_ID", "0"))
INTERVAL_SEC = float(os.environ.get("MONITOR_INTERVAL_SEC", "15"))

TOPAGENT_JSON = REPO / f"agents/data/topagent.{TARGET_UID}.json"
WAITING_JSON = REPO / f"agents/data/{TARGET_UID}/waiting_orders.json"
L2_JSON = REPO / f"agents/data/{TARGET_UID}/l2_depth.json"
FIFO_GLOB = REPO / f"agents/data/{TARGET_UID}/fifo_state_*.json"
STATE_JSON = REPO / f"agents/data/{TARGET_UID}/book{BOOK_ID}_monitor_state.json"
_DEFAULT_LOGS = {
    87: Path.home() / ".pm2/logs/miner-sn79-gold-9133-sn987-out.log",
    179: Path.home() / ".pm2/logs/miner-sn79-gold-9134-sn988-out.log",
    49: Path.home() / ".pm2/logs/miner-sn79-gold-9132-sn982-out.log",
}
TARGET_LOG = Path(
    os.environ.get(
        "MONITOR_TARGET_LOG",
        str(_DEFAULT_LOGS.get(TARGET_UID, _DEFAULT_LOGS[87])),
    )
)

HELPERS: list[tuple[int, str, Path]] = [
    (228, "sn984", Path.home() / ".pm2/logs/miner-sn79-gold-9124-sn984-out.log"),
    (90, "sn980", Path.home() / ".pm2/logs/miner-sn79-gold-9125-sn980-out.log"),
    (163, "sn979", Path.home() / ".pm2/logs/miner-sn79-gold-9126-sn979-out.log"),
    (105, "sn977", Path.home() / ".pm2/logs/miner-sn79-gold-9127-sn977-out.log"),
    (60, "sn983", Path.home() / ".pm2/logs/miner-sn79-gold-9128-sn983-out.log"),
    (46, "sn981", Path.home() / ".pm2/logs/miner-sn79-gold-9129-sn981-out.log"),
    (80, "sn986", Path.home() / ".pm2/logs/miner-sn79-gold-9131-sn986-out.log"),
]

HELPER_WAITING = REPO / "agents/data"
SENT_RE = re.compile(r"(BUY|SELL)\s+([\d.]+)@([\d.]+)\s+ON BOOK (\d+)")
PLACED_RE = re.compile(
    rf"BOOK {BOOK_ID}\s*:\s*PLACED\s+(BUY|SELL)\s+(\w+)\s+ORDER\s+#(\d+)\s+FOR\s+([\d.]+)@([\d.]+)"
)
TRADE_RE = re.compile(
    rf"BOOK {BOOK_ID}\s*:\s*(BUY|SELL)\s+TRADE\s+#(\d+)\s*:.*?FOR\s+([\d.]+)@([\d.]+)"
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
RESPONDED_RE = re.compile(
    r"Responded \(decomp=([0-9.]+)s handle=([0-9.]+)s compress=([0-9.]+)s total=([0-9.]+)s\)"
)
CLOSE_ENGINE_RE = re.compile(
    r"close engine inv=(\d+) batch=(\d+) orders=(\d+) build=([\d.]+)s engine=([\d.]+)s cap=(\d+)"
)
BOOK0_RE = re.compile(rf"BOOK {BOOK_ID}\s*:")
HELP_RE = re.compile(rf"HelpAgent book {BOOK_ID}: (.+)")


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


def tail_lines(path: Path, n: int = 300) -> list[str]:
    if not path.is_file():
        return []
    try:
        return [strip_ansi(l) for l in path.read_text(errors="replace").splitlines()[-n:]]
    except OSError:
        return []


def book_lines(path: Path, n: int = 400) -> list[str]:
    return [l for l in tail_lines(path, n) if BOOK0_RE.search(l)]


def fmt_config(cfg: dict | None) -> list[str]:
    lines = [f"--- topagent.{TARGET_UID}.json (book {BOOK_ID}) ---"]
    if not cfg:
        lines.append("  (missing)")
        return lines
    role = None
    for item in cfg.get("open_books") or []:
        if int(item.get("book", -1)) == BOOK_ID:
            static = " static" if item.get("direction_static") else ""
            role = (
                f"OPEN dir={item.get('open_direction','buy')}{static} "
                f"price={item.get('open_price')} max={item.get('max_quantity')}"
            )
    for item in cfg.get("close_books") or []:
        if int(item.get("book", -1)) == BOOK_ID:
            role = f"CLOSE target={item.get('target_price')} qty={cfg.get('close_quantity')}"
    lines.append(f"  role: {role or 'not configured'}")
    lines.append(
        f"  close_engine cap books={cfg.get('close_max_books_per_tick','default')} "
        f"orders={cfg.get('close_max_orders_per_tick','default')}"
    )
    return lines


def fmt_acceptance(data: dict | None, prev: dict | None) -> list[str]:
    lines = [f"--- book {BOOK_ID} orders (UID {TARGET_UID} acceptance) ---"]
    if not data:
        lines.append("  (waiting_orders.json missing)")
        return lines
    totals = data.get("totals") or {}
    books = data.get("books") or {}
    entry = books.get(str(BOOK_ID)) or books.get(BOOK_ID) or {}
    open_orders = entry.get("open_orders") or []
    waiting = entry.get("waiting") or []
    lines.append(
        f"  sim_ts={data.get('sim_ts')} total_confirmed={totals.get('open', 0)} "
        f"total_waiting={totals.get('waiting', 0)} sent={totals.get('sent_this_tick', 0)}"
    )
    lines.append(
        f"  book {BOOK_ID}: open_n={len(open_orders)} wait_n={len(waiting)}"
    )
    for o in open_orders:
        lines.append(
            f"    confirmed {o.get('direction')} {o.get('quantity')}@{o.get('price')} #{o.get('id')}"
        )
    for w in waiting[:5]:
        lines.append(
            f"    pending {w.get('status')} {w.get('direction')} "
            f"{w.get('quantity')}@{w.get('price')}"
        )
    if prev and prev.get("open_n") is not None:
        lines.append(
            f"  delta: open {prev['open_n']}→{len(open_orders)} "
            f"wait {prev.get('wait_n', 0)}→{len(waiting)}"
        )
    if not open_orders and not waiting:
        lines.append(f"  (flat — no resting or pending on book {BOOK_ID})")
    sent = data.get("sent") or []
    book_sent = [
        o
        for o in sent
        if int(o.get("book_id", o.get("book", -1))) == BOOK_ID
    ]
    if book_sent:
        lines.append(f"  sent_this_tick on book {BOOK_ID}: {len(book_sent)} order(s)")
        for o in book_sent[:3]:
            lines.append(
                f"    {o.get('direction')} {o.get('quantity')}@{o.get('price')} "
                f"{o.get('timeInForce', o.get('type', ''))}"
            )
    else:
        fok_sent = [
            int(o.get("book_id", -1))
            for o in sent
            if str(o.get("timeInForce", "")).upper() == "FOK"
        ]
        if fok_sent:
            lines.append(
                f"  sent_this_tick: book {BOOK_ID} NOT in batch "
                f"(FOK sent for books {fok_sent[:8]})"
            )
    return lines


def fmt_l2(data: dict | None) -> list[str]:
    lines = [f"--- book {BOOK_ID} L2 (top of book) ---"]
    if not data:
        lines.append("  (l2_depth.json missing)")
        return lines
    book = (data.get("books") or {}).get(str(BOOK_ID)) or {}
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if bids:
        lines.append(f"  best bid: {bids[0].get('qty')}@{bids[0].get('price')}")
    if asks:
        lines.append(f"  best ask: {asks[0].get('qty')}@{asks[0].get('price')}")
    if bids and asks:
        spread = float(asks[0]["price"]) - float(bids[0]["price"])
        lines.append(f"  spread: {spread:.4f}")
    if not bids and not asks:
        lines.append("  (empty book snapshot)")
    return lines


def fmt_fifo() -> list[str]:
    lines = [f"--- book {BOOK_ID} FIFO ---"]
    paths = sorted(FIFO_GLOB.parent.glob(FIFO_GLOB.name), key=lambda p: p.stat().st_mtime)
    if not paths:
        lines.append("  (no fifo_state file)")
        return lines
    fp = paths[-1]
    data = load_json(fp)
    if not data:
        lines.append("  (fifo unreadable)")
        return lines
    book = (data.get("books") or {}).get(str(BOOK_ID)) or {}
    longs = book.get("longs") or []
    shorts = book.get("shorts") or []
    net = sum(float(x[1]) for x in longs) - sum(float(x[1]) for x in shorts)
    cum = (data.get("cumulative_realized") or {}).get(str(BOOK_ID))
    hist = data.get("history") or {}
    last_close_ts = max((int(k) for k in hist.keys()), default=0) if hist else 0
    lines.append(f"  file={fp.name}")
    lines.append(f"  net_qty={net:+.4f} longs={len(longs)} shorts={len(shorts)}")
    if longs:
        lines.append(f"  head long: {longs[0]}")
    if shorts:
        lines.append(f"  head short: {shorts[0]}")
    if cum is not None:
        lines.append(f"  cumulative_realized={float(cum):.4f}")
    if last_close_ts:
        lines.append(f"  last_history_ts={last_close_ts}")
    return lines


def fmt_fok_close(cfg: dict | None, l2: dict | None) -> list[str]:
    """Fee-aware close / FOK eligibility for this book (close-only inventory)."""
    lines = [f"--- book {BOOK_ID} FOK close eligibility ---"]
    if not cfg or not l2:
        lines.append("  (need topagent json + l2_depth)")
        return lines
    target = None
    for item in cfg.get("close_books") or []:
        if int(item.get("book", -1)) == BOOK_ID:
            target = float(item.get("target_price", 0))
    if target is None:
        lines.append("  not in close_books")
        return lines
    paths = sorted(FIFO_GLOB.parent.glob(FIFO_GLOB.name), key=lambda p: p.stat().st_mtime)
    if not paths:
        lines.append("  (no fifo_state)")
        return lines
    sys.path.insert(0, str(REPO / "agents"))
    try:
        from fifo_state_recorder import ValidatorStylePnl
        from strategy_engine import (
            fee_aware_long_close_price,
            fee_aware_short_close_price,
            expected_long_close_profit,
            expected_short_close_profit,
            process_topagent_close,
        )
    except ImportError as exc:
        lines.append(f"  import error: {exc}")
        return lines
    fifo = ValidatorStylePnl.load(paths[-1], 128)
    net = float(fifo.net_qty(BOOK_ID))
    if abs(net) < 0.25 - 1e-12:
        lines.append(f"  net_qty={net:.4f} (below min — no close)")
        return lines
    slot = (l2.get("books") or {}).get(str(BOOK_ID)) or {}
    bids, asks = slot.get("bids") or [], slot.get("asks") or []
    bb = float(bids[0]["price"]) if bids else 0.0
    ba = float(asks[0]["price"]) if asks else 0.0
    entry = float(fifo.avg_entry_price(BOOK_ID) or 0.0)
    if entry <= 0.0:
        entry = float(
            fifo.fifo_long_head_price(BOOK_ID) or fifo.fifo_short_head_price(BOOK_ID) or 0.0
        )
    mf = float(slot.get("fees_maker_rate") or slot.get("fees_last_maker_rate") or 0.0001)
    tf = float(slot.get("fees_taker_rate") or slot.get("fees_last_taker_rate") or 0.0001)
    waiting = load_json(WAITING_JSON) or {}
    wo_slot = (waiting.get("books") or {}).get(str(BOOK_ID)) or {}
    open_orders = list(wo_slot.get("open_orders") or [])
    pending = list(wo_slot.get("waiting") or [])
    side = 1 if net > 0.0 else 0
    close_dir = "SELL" if net > 0.0 else "BUY"
    has_pending = any(str(o.get("direction", "")).upper() == close_dir for o in pending)

    if net > 0.0:
        close_px = fee_aware_long_close_price(entry, target, mf)
        exp = expected_long_close_profit(entry, bb, mf, tf) if bb > 0 else 0.0
        lines.append(
            f"  LONG net={net:.4f} entry={entry:.2f} close_px={close_px:.2f} bid={bb}"
        )
    else:
        close_px = fee_aware_short_close_price(entry, target, mf)
        exp = expected_short_close_profit(entry, ba, mf, tf) if ba > 0 else 0.0
        lines.append(
            f"  SHORT net={net:.4f} entry={entry:.2f} close_px={close_px:.2f} ask={ba}"
        )
    fok_ok = (
        (net > 0 and bb >= close_px - 1e-12 and exp > target + 1e-12)
        or (net < 0 and ba <= close_px + 1e-12 and ba > 0 and exp > target + 1e-12)
    )
    lines.append(f"  exp_profit={exp:.4f} target={target} FOK_eligible={fok_ok}")

    book = {
        "id": BOOK_ID,
        "target_price": target,
        "best_bid": bb,
        "best_ask": ba,
        "maker_fee_rate": mf,
        "taker_fee_rate": tf,
        "free_base": float(slot.get("free_base") or 999),
        "free_quote": float(slot.get("free_quote") or 999999),
        "fifo_long_total_qty": float(fifo.fifo_long_total_qty(BOOK_ID)),
        "fifo_short_total_qty": float(fifo.fifo_short_total_qty(BOOK_ID)),
        "fifo_long_lots": fifo.fifo_long_lots_with_fees(BOOK_ID),
        "fifo_short_lots": fifo.fifo_short_lots_with_fees(BOOK_ID),
        "fifo_long_head_price": float(fifo.fifo_long_head_price(BOOK_ID)),
        "fifo_short_head_price": float(fifo.fifo_short_head_price(BOOK_ID)),
        "net_qty": net,
        "entry_price": entry,
        "open_orders": open_orders,
        "close_pending_side": side if has_pending else None,
        "fok_cooldown": False,
        "block_new_limit": has_pending or len(pending) >= 5,
    }
    r = process_topagent_close(
        {
            "price_decimals": 2,
            "volume_decimals": 4,
            "exchange_min_quantity": 0.25,
            "close_quantity": float(cfg.get("close_quantity", 0.25)),
            "close_dust_threshold": float(cfg.get("close_dust_threshold", 0.51)),
            "expiry_period": int(cfg.get("expiry_period", 200_000_000_000)),
            "max_maker_fee_rate": float(cfg.get("max_maker_fee_rate", 0.005)),
            "close_max_orders_per_tick": int(cfg.get("close_max_orders_per_tick", 150)),
            "books": [book],
        }
    )
    log = (r.get("logs") or [{}])[0]
    lines.append(f"  engine_decision={log.get('decision')} (no latch/cooldown)")
    if has_pending:
        lines.append("  note: waiting_orders has close-side pending → live may block")
    # rank in full batch
    close_specs = {
        int(x["book"]): float(x.get("target_price", 0))
        for x in cfg.get("close_books") or []
        if isinstance(x, dict)
    }
    l2_books = l2.get("books") or {}
    wo_books = waiting.get("books") or {}
    all_books = []
    for bid in sorted(close_specs):
        n = float(fifo.net_qty(bid))
        if abs(n) < 0.25 - 1e-12:
            continue
        sl = l2_books.get(str(bid)) or {}
        bbs, aks = sl.get("bids") or [], sl.get("asks") or []
        bbb = float(bbs[0]["price"]) if bbs else 0.0
        bba = float(aks[0]["price"]) if aks else 0.0
        ent = float(fifo.avg_entry_price(bid) or 0.0)
        if ent <= 0:
            ent = float(fifo.fifo_long_head_price(bid) or fifo.fifo_short_head_price(bid) or 0.0)
        wsl = wo_books.get(str(bid)) or {}
        sd = 1 if n > 0 else 0
        cdir = "SELL" if n > 0 else "BUY"
        wtg = list(wsl.get("waiting") or [])
        hp = any(str(o.get("direction", "")).upper() == cdir for o in wtg)
        all_books.append(
            {
                "id": bid,
                "target_price": close_specs[bid],
                "best_bid": bbb,
                "best_ask": bba,
                "maker_fee_rate": float(sl.get("fees_maker_rate") or 0.0001),
                "taker_fee_rate": float(sl.get("fees_taker_rate") or 0.0001),
                "free_base": float(sl.get("free_base") or 999),
                "free_quote": float(sl.get("free_quote") or 999999),
                "fifo_long_total_qty": float(fifo.fifo_long_total_qty(bid)),
                "fifo_short_total_qty": float(fifo.fifo_short_total_qty(bid)),
                "fifo_long_lots": fifo.fifo_long_lots_with_fees(bid),
                "fifo_short_lots": fifo.fifo_short_lots_with_fees(bid),
                "fifo_long_head_price": float(fifo.fifo_long_head_price(bid)),
                "fifo_short_head_price": float(fifo.fifo_short_head_price(bid)),
                "net_qty": n,
                "entry_price": ent,
                "open_orders": list(wsl.get("open_orders") or []),
                "close_pending_side": sd if hp else None,
                "fok_cooldown": False,
                "block_new_limit": hp or len(wtg) >= 5,
            }
        )
    full = process_topagent_close(
        {
            "price_decimals": 2,
            "volume_decimals": 4,
            "exchange_min_quantity": 0.25,
            "close_quantity": float(cfg.get("close_quantity", 0.25)),
            "close_dust_threshold": float(cfg.get("close_dust_threshold", 0.51)),
            "expiry_period": int(cfg.get("expiry_period", 200_000_000_000)),
            "max_maker_fee_rate": float(cfg.get("max_maker_fee_rate", 0.005)),
            "close_max_orders_per_tick": int(cfg.get("close_max_orders_per_tick", 150)),
            "books": all_books,
        }
    )
    limits = [
        int(o["book_id"])
        for o in full.get("orders") or []
        if str(o.get("type", "limit")).lower() == "limit"
    ]
    if BOOK_ID in limits:
        lines.append(
            f"  full_batch_rank={limits.index(BOOK_ID) + 1}/{len(limits)} limit slots"
        )
    else:
        log_full = next(
            (x for x in full.get("logs") or [] if int(x.get("book_id", -1)) == BOOK_ID),
            {},
        )
        lines.append(
            f"  full_batch: NOT in output — decision={log_full.get('decision')}"
        )
    fok_before = sum(
        1
        for x in full.get("logs") or []
        if int(x.get("book_id", -1)) < BOOK_ID
        and str(x.get("decision", "")).startswith("fok_")
        and x.get("orders_sent")
    )
    lines.append(f"  FOK slots ahead (lower book id): {fok_before}")
    return lines


def fmt_close_engine() -> list[str]:
    lines = [f"--- book {BOOK_ID} close engine + latency ---"]
    log_lines = tail_lines(TARGET_LOG, 200)
    handles = []
    engines = []
    for line in log_lines:
        m = RESPONDED_RE.search(line)
        if m:
            handles.append(float(m.group(2)))
        m = CLOSE_ENGINE_RE.search(line)
        if m:
            engines.append(tuple(m.groups()))
    if handles:
        hs = sorted(handles[-50:])
        lines.append(
            f"  handle: last={handles[-1]:.3f}s med={statistics.median(hs):.3f}s "
            f"max={hs[-1]:.3f}s (last {len(hs)} ticks)"
        )
    if engines:
        e = engines[-1]
        lines.append(
            f"  close engine: inv={e[0]} batch={e[1]} orders={e[2]} "
            f"build={e[3]}s engine={e[4]}s cap={e[5]}"
        )
    else:
        lines.append("  close engine: (no recent log lines)")
    return lines


def _helper_waiting_path(uid: int) -> Path:
    return HELPER_WAITING / str(uid) / "waiting_orders.json"


def fmt_helper_orders() -> list[str]:
    """HelpAgent resting/pending orders and recent sends on this book."""
    lines = [f"--- HelpAgent orders on book {BOOK_ID} (target UID {TARGET_UID}) ---"]
    any_activity = False
    for uid, hotkey, log_path in HELPERS:
        sub: list[str] = []
        waiting_data = load_json(_helper_waiting_path(uid))
        if waiting_data:
            books = waiting_data.get("books") or {}
            entry = books.get(str(BOOK_ID)) or books.get(BOOK_ID) or {}
            open_orders = entry.get("open_orders") or []
            pending = entry.get("waiting") or []
            sent_tick = (waiting_data.get("totals") or {}).get("sent_this_tick", 0)
            if open_orders or pending or sent_tick:
                sub.append(
                    f"    waiting_orders: open_n={len(open_orders)} wait_n={len(pending)} "
                    f"sent_this_tick={sent_tick} sim_ts={waiting_data.get('sim_ts')}"
                )
            for o in open_orders:
                sub.append(
                    f"    resting {o.get('direction')} {o.get('quantity')}@{o.get('price')} "
                    f"#{o.get('id')} status={o.get('status','?')}"
                )
            for w in pending[:3]:
                sub.append(
                    f"    pending {w.get('status')} {w.get('direction')} "
                    f"{w.get('quantity')}@{w.get('price')}"
                )

        log_lines = tail_lines(log_path, 800)
        sends = [
            (m.group(1), float(m.group(2)), float(m.group(3)), int(m.group(4)))
            for line in log_lines
            if (m := SENT_RE.search(line)) and int(m.group(4)) == BOOK_ID
        ]
        helps = [m.group(1) for line in log_lines if (m := HELP_RE.search(line))]
        placed = []
        trades = []
        for line in log_lines:
            if (m := PLACED_RE.search(line)):
                placed.append(m)
            if (m := TRADE_RE.search(line)):
                trades.append(m)

        if sends:
            s = sends[-1]
            sub.append(f"    last sent: {s[0]} {s[1]}@{s[2]}")
            for s in sends[-3:]:
                sub.append(f"    sent {s[0]} {s[1]}@{s[2]}")
        for msg in helps[-3:]:
            sub.append(f"    help: {msg[:110]}")
        for m in placed[-2:]:
            sub.append(
                f"    placed {m.group(1)} {m.group(2)} #{m.group(3)} "
                f"{m.group(4)}@{m.group(5)}"
            )
        for m in trades[-2:]:
            sub.append(
                f"    trade #{m.group(2)} {m.group(1)} {m.group(3)}@{m.group(4)}"
            )

        if sub:
            any_activity = True
            lines.append(f"  UID {uid} ({hotkey}):")
            lines.extend(sub)
        else:
            lines.append(f"  UID {uid} ({hotkey}): idle on book {BOOK_ID}")

    if not any_activity:
        lines.append("  (no helper resting/sent/help lines for this book in tail window)")
    return lines


def fmt_helpers() -> list[str]:
    lines = [f"--- helpers on book {BOOK_ID} (summary) ---"]
    total = 0
    for uid, _hotkey, path in HELPERS:
        helps = [HELP_RE.search(l) for l in tail_lines(path, 200)]
        helps = [m.group(1) for m in helps if m]
        total += len(helps)
        if helps:
            lines.append(f"  UID {uid}: {len(helps)} actions — last: {helps[-1][:85]}")
    if total == 0:
        lines.append(f"  (no recent help activity on book {BOOK_ID})")
    else:
        lines.append(f"  total help actions (tail): {total}")
    return lines


def fmt_recent_events() -> list[str]:
    lines = [f"--- recent UID {TARGET_UID} book {BOOK_ID} events ---"]
    events = book_lines(TARGET_LOG, 500)[-8:]
    for e in events:
        lines.append(f"  {e.strip()[-130:]}")
    if not events:
        lines.append("  (none in log tail)")
    return lines


def tick() -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prev = load_json(STATE_JSON)
    cfg = load_json(TOPAGENT_JSON)
    waiting = load_json(WAITING_JSON)
    l2 = load_json(L2_JSON)

    books = (waiting or {}).get("books") or {}
    entry = books.get(str(BOOK_ID)) or books.get(BOOK_ID) or {}

    parts = [
        "",
        "=" * 72,
        f"UID {TARGET_UID} book {BOOK_ID} monitor @ {ts}",
        "=" * 72,
    ]
    parts.extend(fmt_config(cfg))
    parts.extend(fmt_acceptance(waiting, prev))
    parts.extend(fmt_l2(l2))
    parts.extend(fmt_fifo())
    parts.extend(fmt_fok_close(cfg, l2))
    parts.extend(fmt_close_engine())
    parts.extend(fmt_helper_orders())
    parts.extend(fmt_helpers())
    parts.extend(fmt_recent_events())
    print("\n".join(parts), flush=True)

    snap = {
        "wall_ts": time.time(),
        "open_n": len(entry.get("open_orders") or []),
        "wait_n": len(entry.get("waiting") or []),
        "sim_ts": (waiting or {}).get("sim_ts"),
    }
    save_json(STATE_JSON, snap)
    return snap


def main() -> int:
    once = "--once" in sys.argv
    print(
        f"Monitoring UID {TARGET_UID} book {BOOK_ID} every {INTERVAL_SEC}s",
        flush=True,
    )
    while True:
        tick()
        if once:
            break
        time.sleep(INTERVAL_SEC)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
