#!/usr/bin/env python3
"""Monitor UID 49 TopAgent: acceptance backlog, latency, config, helpers."""
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
UID = int(os.environ.get("MONITOR_UID", "49"))
INTERVAL_SEC = float(os.environ.get("MONITOR_INTERVAL_SEC", "30"))
DETAIL_BOOKS = tuple(
    int(x)
    for x in os.environ.get("MONITOR_DETAIL_BOOKS", "0,1,3,4,5").split(",")
    if x.strip().isdigit()
)

TOPAGENT_JSON = REPO / f"agents/data/topagent.{UID}.json"
WAITING_JSON = REPO / f"agents/data/{UID}/waiting_orders.json"
STATE_JSON = REPO / f"agents/data/{UID}/monitor_state.json"
OPEN_FILL_JSON = REPO / f"agents/data/{UID}/open_fill_monitor.json"
PM2_LOG_DIR = Path.home() / ".pm2/logs"

PASSIVE_FILL_RE = re.compile(
    r"BOOK (\d+) : (?:BUY|SELL) TRADE #\d+ : YOUR PASSIVE ORDER #\d+ \(AGENT \d+\)"
)
HELP_IOC_RE = re.compile(
    r"HelpAgent book (\d+): IOC (BUY|SELL) qty=([\d.]+)@([\d.]+) "
    r"\(target_(BUY|SELL)_remaining=([^)]+)"
)
HELP_SKIP_RE = re.compile(
    r"HelpAgent book (\d+): skip help \(([^)]+)\)"
)

TARGET_LOG = PM2_LOG_DIR / "miner-sn79-gold-9132-sn982-out.log"
HELPER_LOGS = sorted(PM2_LOG_DIR.glob("miner-sn79-gold-912*-out.log")) + sorted(
    PM2_LOG_DIR.glob("miner-sn79-gold-9130-sn985-out.log")
) + sorted(PM2_LOG_DIR.glob("miner-sn79-gold-9131-sn986-out.log"))

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WALL_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
DECOMP_RE = re.compile(r"Decompressed \(([0-9.]+)s\)")
RESPONDED_RE = re.compile(
    r"Responded \(decomp=([0-9.]+)s handle=([0-9.]+)s compress=([0-9.]+)s total=([0-9.]+)s\)"
)
PLACED_RE = re.compile(r"BOOK (\d+) : PLACED")
SIM_TS_RE = re.compile(r"SIMULATION TIME : ([0-9:.]+)")
BOOK_ACTION_RE = re.compile(
    r"(HelpAgent book (\d+):|(?:BUY|SELL)\s+[\d.]+\@[\d.]+\s+ON BOOK (\d+)|BOOK (\d+)\s*:)",
    re.I,
)


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


def sum_side_qty(orders: list, book_id: int, direction: str) -> float:
    total = 0.0
    for o in orders or []:
        try:
            if int(o.get("book_id", -1)) != book_id:
                continue
        except (TypeError, ValueError):
            continue
        if str(o.get("direction", "")).upper() != direction.upper():
            continue
        try:
            total += float(o.get("quantity") or 0.0)
        except (TypeError, ValueError):
            pass
    return total


def fmt_config(data: dict | None) -> list[str]:
    lines = ["--- topagent config ---"]
    if not data:
        lines.append("  (missing or invalid)")
        return lines
    open_books = data.get("open_books") or []
    close_books = data.get("close_books") or []
    lines.append(f"  open_books={len(open_books)} close_books={len(close_books)}")
    lines.append(
        f"  close_qty={data.get('close_quantity')} dust={data.get('close_dust_threshold')} "
        f"open_qty={data.get('open_quantity')} depth={data.get('use_depth_qty')}"
    )
    for book_id in DETAIL_BOOKS:
        for item in open_books:
            if int(item.get("book", -1)) == book_id:
                lines.append(
                    f"  OPEN  book {book_id}: dir={item.get('open_direction')} "
                    f"price={item.get('open_price')} max={item.get('max_quantity')}"
                )
                break
        else:
            for item in close_books:
                if int(item.get("book", -1)) == book_id:
                    lines.append(
                        f"  CLOSE book {book_id}: target={item.get('target_price')}"
                    )
                    break
    return lines


def fmt_acceptance(data: dict | None, prev: dict | None) -> list[str]:
    lines = ["--- acceptance backlog ---"]
    if not data:
        lines.append("  (waiting_orders missing)")
        return lines

    totals = data.get("totals") or {}
    confirmed = data.get("confirmed") or []
    waiting = data.get("waiting") or []
    sent = data.get("sent") or []
    now = time.time()

    n_conf = len(confirmed)
    n_wait = len(waiting)
    wst = Counter(w.get("status") for w in waiting if isinstance(w, dict))
    lines.append(
        f"  sim_ts={data.get('sim_ts')} sim_id={data.get('sim_id')} "
        f"books_tracked={data.get('book_count')}"
    )
    lines.append(
        f"  confirmed={n_conf} waiting={n_wait} "
        f"(accept={wst.get('waiting_accept', 0)} cancel={wst.get('waiting_cancel', 0)}) "
        f"sent_this_tick={totals.get('sent_this_tick', len(sent))}"
    )

    if prev:
        pc = int(prev.get("confirmed", 0))
        pw = int(prev.get("waiting", 0))
        dt = max(now - float(prev.get("wall_ts") or now), 0.001)
        lines.append(
            f"  delta {dt:.0f}s: confirmed {pc}→{n_conf} ({n_conf - pc:+d}) "
            f"waiting {pw}→{n_wait} ({n_wait - pw:+d})"
        )
        if n_conf - pc <= 0 and wst.get("waiting_accept", 0) > 50:
            lines.append("  ⚠ no new confirmations — backlog not draining")

    wall_ages = [
        now - float(w["wall_ts"])
        for w in waiting
        if isinstance(w, dict) and w.get("wall_ts")
    ]
    if wall_ages:
        wall_ages.sort()
        lines.append(
            f"  pending wall age (sec): p50={wall_ages[len(wall_ages) // 2]:.1f} "
            f"p90={wall_ages[int(len(wall_ages) * 0.9)]:.1f} max={wall_ages[-1]:.1f}"
        )

    cur_sim = int(data.get("sim_ts") or 0)
    sim_ages = []
    for w in waiting:
        if not isinstance(w, dict):
            continue
        wsim = w.get("sim_ts")
        if wsim is None or not cur_sim:
            continue
        try:
            sim_ages.append((cur_sim - int(wsim)) / 1_000_000_000)
        except (TypeError, ValueError):
            pass
    if sim_ages:
        sim_ages.sort()
        lines.append(
            f"  pending sim age (sec):  p50={sim_ages[len(sim_ages) // 2]:.1f} "
            f"p90={sim_ages[int(len(sim_ages) * 0.9)]:.1f} max={sim_ages[-1]:.1f}"
        )

    dup = Counter(
        int(w["book_id"])
        for w in waiting
        if isinstance(w, dict) and w.get("status") == "waiting_accept"
    )
    if dup:
        top = dup.most_common(5)
        lines.append(f"  top duplicate pending/book: {top}")

    for book_id in DETAIL_BOOKS:
        books = data.get("books") or {}
        entry = books.get(str(book_id)) or books.get(book_id) or {}
        open_orders = entry.get("open_orders") or []
        book_waiting = entry.get("waiting") or []
        buy_open = sum_side_qty(open_orders, book_id, "BUY")
        sell_open = sum_side_qty(open_orders, book_id, "SELL")
        buy_wait = sum_side_qty(book_waiting, book_id, "BUY")
        sell_wait = sum_side_qty(book_waiting, book_id, "SELL")
        lines.append(
            f"  book {book_id}: conf BUY={buy_open:.4f} SELL={sell_open:.4f} | "
            f"pend BUY={buy_wait:.4f} SELL={sell_wait:.4f} "
            f"(open_n={len(open_orders)} wait_n={len(book_waiting)})"
        )
        for o in open_orders[:2]:
            lines.append(
                f"    resting {o.get('direction')} {o.get('quantity')}@{o.get('price')}"
            )
    return lines


def parse_log_ticks(log_path: Path, *, tail_blocks: int = 400) -> dict:
    out = {
        "decompress_samples": [],
        "respond_samples": [],
        "wall_intervals": [],
        "tick_rows": [],
        "settlement_not_ready": 0,
        "settlement_ready": 0,
        "failed_cancel": 0,
        "failed_other": 0,
        "handle_errors": 0,
        "placed_recent": 0,
        "last_wall": None,
        "last_decompress": None,
        "last_sim": None,
        "last_handle": None,
        "last_total": None,
    }
    if not log_path.is_file():
        return out

    text = strip_ansi(log_path.read_text(errors="replace"))
    out["settlement_not_ready"] = text.count("SETTLEMENT NOT READY")
    out["settlement_ready"] = text.count("Settlement ready: proxy AUTHORISED")
    out["failed_cancel"] = text.count("FAILED TO CANCEL ORDER")
    out["failed_other"] = text.count("FAILED TO PLACE")
    out["handle_errors"] = text.count("Agent handle error")

    for m in DECOMP_RE.finditer(text):
        out["decompress_samples"].append(float(m.group(1)))

    for m in RESPONDED_RE.finditer(text):
        out["respond_samples"].append(
            tuple(float(m.group(i)) for i in range(1, 5))
        )
    if out["respond_samples"]:
        last = out["respond_samples"][-1]
        out["last_decompress"] = last[0]
        out["last_handle"] = last[1]
        out["last_total"] = last[3]

    tail_text = text[-800_000:]
    out["placed_recent"] = len(PLACED_RE.findall(tail_text))

    blocks = text.split("--------------------------------------------------")[-tail_blocks:]
    prev_wall: float | None = None
    pending_decompress: float | None = None
    pending_wall_str: str | None = None
    for blk in blocks:
        wt_m = WALL_TS_RE.search(blk)
        dc_m = DECOMP_RE.search(blk)
        sim_m = SIM_TS_RE.search(blk)

        if wt_m and dc_m:
            pending_wall_str = wt_m.group(1)
            pending_decompress = float(dc_m.group(1))
            try:
                wall = datetime.strptime(
                    pending_wall_str, "%Y-%m-%d %H:%M:%S.%f"
                ).timestamp()
                if prev_wall is not None:
                    out["wall_intervals"].append(wall - prev_wall)
                prev_wall = wall
                out["last_wall"] = pending_wall_str
                out["last_decompress"] = pending_decompress
            except ValueError:
                pass
            out["decompress_samples"].append(pending_decompress)

        if sim_m:
            sim = sim_m.group(1).strip()
            out["last_sim"] = sim
            new = sum(1 for ln in blk.splitlines() if " ON BOOK " in ln)
            can = sum(1 for ln in blk.splitlines() if "CANCEL ORDER" in ln)
            fail = sum(1 for ln in blk.splitlines() if "FAILED" in ln)
            # Orders often appear in the block before decompress; also count prior block
            out["tick_rows"].append(
                {
                    "sim": sim,
                    "new": new,
                    "cancel": can,
                    "fail": fail,
                    "decompress": pending_decompress,
                    "wall": pending_wall_str,
                }
            )
            pending_decompress = None
            pending_wall_str = None

    # Orders frequently logged just before the decompress separator — merge backward
    merged: list[dict] = []
    raw_blocks = text.split("--------------------------------------------------")[-tail_blocks - 1 :]
    for i, blk in enumerate(raw_blocks):
        sim_m = SIM_TS_RE.search(blk)
        if not sim_m:
            continue
        prev_blk = raw_blocks[i - 1] if i > 0 else ""
        new = sum(1 for ln in blk.splitlines() if " ON BOOK " in ln)
        new += sum(1 for ln in prev_blk.splitlines() if " ON BOOK " in ln)
        can = sum(1 for ln in blk.splitlines() if "CANCEL ORDER" in ln)
        can += sum(1 for ln in prev_blk.splitlines() if "CANCEL ORDER" in ln)
        fail = sum(1 for ln in blk.splitlines() if "FAILED" in ln)
        dc_m = DECOMP_RE.search(prev_blk)
        wt_m = WALL_TS_RE.search(prev_blk)
        merged.append(
            {
                "sim": sim_m.group(1).strip(),
                "new": new,
                "cancel": can,
                "fail": fail,
                "decompress": float(dc_m.group(1)) if dc_m else None,
                "wall": wt_m.group(1) if wt_m else None,
            }
        )
    if merged:
        out["tick_rows"] = merged[-80:]

    return out


def _latency_line(name: str, samples: list[float]) -> str:
    recent = samples[-100:]
    if not recent:
        return ""
    p90_idx = min(int(len(recent) * 0.9), len(recent) - 1)
    return (
        f"  {name} (last {len(recent)}): "
        f"p50={statistics.median(recent):.3f}s "
        f"p90={sorted(recent)[p90_idx]:.3f}s "
        f"max={max(recent):.3f}s"
    )


def fmt_latency(log_stats: dict) -> list[str]:
    lines = ["--- latency trace ---"]
    responds = log_stats.get("respond_samples") or []
    if responds:
        recent = responds[-100:]
        decomp = [r[0] for r in recent]
        handle = [r[1] for r in recent]
        total = [r[3] for r in recent]
        for name, samples in (
            ("decompress", decomp),
            ("handle", handle),
            ("total", total),
        ):
            line = _latency_line(name, samples)
            if line:
                lines.append(line)
        lines.append(
            f"  last respond: handle={log_stats.get('last_handle')}s "
            f"total={log_stats.get('last_total')}s"
        )
        placed = int(log_stats.get("placed_recent") or 0)
        lines.append(f"  PLACED events (log tail): {placed}")
    else:
        dcs = log_stats.get("decompress_samples") or []
        if dcs:
            line = _latency_line("decompress", dcs[-200:])
            if line:
                lines.append(line)
    intervals = log_stats.get("wall_intervals") or []
    dcs = log_stats.get("decompress_samples") or []
    if intervals:
        recent = intervals[-100:]
        med = statistics.median(recent)
        lines.append(
            f"  wall tick interval: p50={med:.2f}s p90={sorted(recent)[int(len(recent) * 0.9)]:.2f}s "
            f"max={max(recent):.2f}s"
        )
        lines.append(
            f"  sim vs wall lag: ~{med:.1f}x (sim advances 1s per tick, wall ~{med:.1f}s)"
        )
        if dcs:
            recent_dc = dcs[-len(recent) :]
            if len(recent_dc) == len(recent):
                implied = [intervals[-len(recent) + i] - recent_dc[i] for i in range(len(recent))]
                implied = [x for x in implied if x > 0]
                if implied:
                    lines.append(
                        f"  implied handle+compress+network: p50={statistics.median(implied):.2f}s"
                    )

    lines.append(
        f"  last tick: wall={log_stats.get('last_wall')} "
        f"decompress={log_stats.get('last_decompress')}s sim={log_stats.get('last_sim')}"
    )
    rows = log_stats.get("tick_rows") or []
    if rows:
        last = rows[-1]
        if last.get("decompress") is not None:
            lines.append(
                f"  last busy tick decompress={last['decompress']:.3f}s "
                f"orders={last.get('new', 0)} cancels={last.get('cancel', 0)}"
            )
    lines.append(
        "  validator delays (defaults): base 10ms–1s sim + 5–25ms per 2nd instr/book; "
        "max 5 instr/book/response"
    )
    return lines


def fmt_health(log_stats: dict) -> list[str]:
    lines = ["--- health / other causes ---"]
    snr = log_stats.get("settlement_not_ready", 0)
    sr = log_stats.get("settlement_ready", 0)
    if snr > 0 and sr == 0:
        lines.append(
            f"  ⚠ SETTLEMENT NOT READY ({snr} log hits) — passive fills may not settle locally"
        )
    elif sr > 0:
        lines.append("  settlement: ready (proxy authorised + funded)")
    else:
        lines.append("  settlement: unknown (no log lines yet)")

    fc = log_stats.get("failed_cancel", 0)
    if fc:
        lines.append(
            f"  ⚠ failed cancels: {fc} — often canceling orders never accepted on book"
        )
    fp = log_stats.get("failed_other", 0)
    if fp:
        lines.append(f"  failed place: {fp}")
    he = log_stats.get("handle_errors", 0)
    if he:
        lines.append(f"  ⚠ agent handle errors: {he}")

    lines.append(
        "  duplicate resend: TopAgent only sees confirmed acct.orders; "
        "unaccepted orders get re-sent each tick"
    )
    return lines


def fmt_recent_ticks(log_stats: dict, n: int = 8) -> list[str]:
    lines = ["--- recent ticks (sim | new | cancel | fail | decomp) ---"]
    rows = log_stats.get("tick_rows") or []
    active = [r for r in rows if r["new"] + r["cancel"] + r["fail"] > 0]
    show = active[-n:] if active else rows[-n:]
    if not show:
        lines.append("  (no ticks parsed)")
        return lines
    for r in show:
        dc = r.get("decompress")
        dc_s = f"{dc:.3f}s" if dc is not None else "-"
        lines.append(
            f"  {r['sim']} | new={r['new']} cancel={r['cancel']} fail={r['fail']} decomp={dc_s}"
        )
    if not active and rows:
        lines.append("  (idle — no orders/cancels in recent ticks)")
    return lines


def _load_fifo_net() -> dict[int, float]:
    sys.path.insert(0, str(REPO / "agents"))
    try:
        from fifo_state_recorder import ValidatorStylePnl
    except ImportError:
        return {}
    fifo_dir = REPO / f"agents/data/{UID}"
    files = sorted(fifo_dir.glob("fifo_state_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {}
    try:
        fifo = ValidatorStylePnl.load(files[-1], 128)
        return {b: float(fifo.net_qty(b)) for b in range(128)}
    except Exception:
        return {}


def _open_book_issue(
    book_id: int,
    spec: dict,
    *,
    in_close: bool,
    resting: float,
    net: float,
) -> str:
    direction = str(spec.get("open_direction", "buy")).lower()
    px = float(spec.get("open_price") or 0)
    max_q = float(spec.get("max_quantity") or 200)
    if in_close:
        return "in_close_books"
    if px <= 0:
        return "no_open_price"
    if direction == "sell":
        room = max_q + net
    else:
        room = max_q - net
    if room <= 1e-9:
        return "at_room_cap"
    if resting >= 0.26:
        return "resting_ok"
    if resting >= 0.01:
        return "low_resting_quote_limited"
    return "no_resting_quote_or_base_limited"


def _recent_passive_fills(log_path: Path, book_ids: set[int], tail: int = 3000) -> dict[int, int]:
    if not log_path.is_file():
        return {}
    lines = strip_ansi(log_path.read_text(errors="replace")).splitlines()[-tail:]
    counts: dict[int, int] = {}
    for line in lines:
        m = PASSIVE_FILL_RE.search(line)
        if not m:
            continue
        bid = int(m.group(1))
        if bid in book_ids:
            counts[bid] = counts.get(bid, 0) + 1
    return counts


def _recent_helper_ioc(log_paths: list[Path], book_ids: set[int], tail: int = 400) -> dict[int, list[str]]:
    out: dict[int, list[str]] = {}
    for path in log_paths:
        if not path.is_file():
            continue
        for line in strip_ansi(path.read_text(errors="replace")).splitlines()[-tail:]:
            m = HELP_IOC_RE.search(line)
            if not m:
                continue
            bid = int(m.group(1))
            if bid not in book_ids:
                continue
            note = (
                f"IOC {m.group(2)} {m.group(3)}@{m.group(4)} "
                f"rem={m.group(6)}"
            )
            out.setdefault(bid, []).append(note)
    return out


def fmt_open_fills(config: dict | None, waiting: dict | None) -> tuple[list[str], dict]:
    lines = ["--- open book fill health ---"]
    audit: dict = {}
    if not config:
        lines.append("  (no topagent config)")
        return lines, audit

    open_books = config.get("open_books") or []
    close_set = {
        int(x["book"]) for x in (config.get("close_books") or []) if "book" in x
    }
    net_by_book = _load_fifo_net()
    books_wo = (waiting or {}).get("books") or {}
    open_ids = {int(x["book"]) for x in open_books if "book" in x}

    fills = _recent_passive_fills(TARGET_LOG, open_ids)
    helper_ioc = _recent_helper_ioc(HELPER_LOGS, open_ids)

    rows = []
    for item in open_books:
        bid = int(item["book"])
        direction = str(item.get("open_direction", "buy")).upper()
        px = float(item.get("open_price") or 0)
        max_q = float(item.get("max_quantity") or 200)
        net = net_by_book.get(bid, 0.0)
        entry = books_wo.get(str(bid)) or books_wo.get(bid) or {}
        resting = sum_side_qty(entry.get("open_orders") or [], bid, direction)
        issue = _open_book_issue(
            bid, item, in_close=(bid in close_set), resting=resting, net=net
        )
        rows.append(
            {
                "book": bid,
                "direction": direction,
                "open_price": px,
                "max_quantity": max_q,
                "net_qty": round(net, 4),
                "resting": round(resting, 4),
                "issue": issue,
                "passive_fills_recent": fills.get(bid, 0),
                "helper_ioc_recent": len(helper_ioc.get(bid, [])),
            }
        )

    by_issue = Counter(r["issue"] for r in rows)
    lines.append(
        f"  open_books={len(rows)} resting_ok={by_issue.get('resting_ok', 0)} "
        f"low_resting={by_issue.get('low_resting_quote_limited', 0)} "
        f"no_resting={by_issue.get('no_resting_quote_or_base_limited', 0)} "
        f"in_close={by_issue.get('in_close_books', 0)} at_cap={by_issue.get('at_room_cap', 0)}"
    )
    lines.append(
        "  typical no-fill cause: TopAgent posts only ~0.25/tick when per-book quote "
        "is low (available_qty = quote_free/price - 1); helpers fill that then wait"
    )

    problem = [r for r in rows if r["issue"] != "resting_ok"]
    problem.sort(key=lambda r: (r["issue"], -abs(r["net_qty"])))
    lines.append("  problem books (issue | dir | resting | net | px | fills | helper_ioc):")
    for r in problem[:12]:
        lines.append(
            f"    book {r['book']:3d}: {r['issue']} {r['direction']} "
            f"rest={r['resting']:.2f} net={r['net_qty']:+.2f} px={r['open_price']:.2f} "
            f"fills={r['passive_fills_recent']} ioc={r['helper_ioc_recent']}"
        )
    if len(problem) > 12:
        lines.append(f"    ... +{len(problem) - 12} more")

    ok = [r for r in rows if r["issue"] == "resting_ok"]
    if ok:
        lines.append("  resting_ok (sample):")
        for r in ok[:6]:
            lines.append(
                f"    book {r['book']:3d}: {r['direction']} rest={r['resting']:.1f} "
                f"net={r['net_qty']:+.2f} fills={r['passive_fills_recent']}"
            )

    audit = {
        "open_books": len(rows),
        "by_issue": dict(by_issue),
        "books": rows,
        "sim_ts": (waiting or {}).get("sim_ts"),
    }
    return lines, audit


def fmt_fifo() -> list[str]:
    lines = ["--- FIFO state ---"]
    fifo_dir = REPO / f"agents/data/{UID}"
    files = sorted(fifo_dir.glob("fifo_state*.json"))
    if not files:
        lines.append("  (no fifo_state file)")
        return lines
    fp = files[-1]
    data = load_json(fp)
    if not data:
        lines.append(f"  (invalid {fp.name})")
        return lines
    books = data.get("books", data)
    pos = []
    if isinstance(books, dict):
        for bid, st in books.items():
            if not isinstance(st, dict):
                continue
            net = float(st.get("net_qty", st.get("net", 0)) or 0)
            if abs(net) > 0.01:
                pos.append((int(bid), net))
    lines.append(f"  file={fp.name} books_with_position={len(pos)}")
    for bid, net in sorted(pos, key=lambda x: abs(x[1]), reverse=True)[:8]:
        lines.append(f"    book {bid}: net={net:+.4f}")
    if len(pos) > 8:
        lines.append(f"    ... +{len(pos) - 8} more")
    return lines


def tail_book_lines(path: Path, book_ids: tuple[int, ...], n: int = 120) -> list[str]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(errors="replace").splitlines()[-n:]
    except OSError:
        return []
    out: list[str] = []
    for line in raw:
        clean = strip_ansi(line)
        m = BOOK_ACTION_RE.search(clean)
        if not m:
            continue
        bid = m.group(2) or m.group(3) or m.group(4)
        if bid is None:
            continue
        try:
            if int(bid) not in book_ids:
                continue
        except ValueError:
            continue
        out.append(clean.strip())
    return out[-5:]


def fmt_logs() -> list[str]:
    lines = [f"--- recent UID {UID} (detail books) ---"]
    for ln in tail_book_lines(TARGET_LOG, DETAIL_BOOKS):
        lines.append(f"  {ln}")
    if len(lines) == 1:
        lines.append("  (no recent lines)")

    lines.append("--- recent helpers (detail books) ---")
    seen = 0
    for log in HELPER_LOGS:
        name = log.name.replace("-out.log", "")
        for ln in tail_book_lines(log, DETAIL_BOOKS, n=150):
            uid_m = re.search(r"sn(\d+)", name)
            tag = uid_m.group(1) if uid_m else name[-4:]
            lines.append(f"  [{tag}] {ln}")
            seen += 1
    if seen == 0:
        lines.append("  (no recent help lines)")
    return lines


def tick() -> dict:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    prev = load_json(STATE_JSON)
    waiting = load_json(WAITING_JSON)
    config = load_json(TOPAGENT_JSON)
    log_stats = parse_log_ticks(TARGET_LOG)

    parts = [f"\n{'=' * 72}", f"UID {UID} monitor @ {ts}", "=" * 72]
    parts.extend(fmt_config(config))
    open_lines, open_audit = fmt_open_fills(config, waiting)
    parts.extend(open_lines)
    parts.extend(fmt_acceptance(waiting, prev))
    parts.extend(fmt_latency(log_stats))
    parts.extend(fmt_health(log_stats))
    parts.extend(fmt_recent_ticks(log_stats))
    parts.extend(fmt_fifo())
    parts.extend(fmt_logs())
    print("\n".join(parts), flush=True)

    snap = {
        "wall_ts": time.time(),
        "confirmed": len((waiting or {}).get("confirmed") or []),
        "waiting": len((waiting or {}).get("waiting") or []),
        "sim_ts": (waiting or {}).get("sim_ts"),
    }
    save_json(STATE_JSON, snap)
    if open_audit:
        open_audit["updated_at"] = ts
        save_json(OPEN_FILL_JSON, open_audit)
    return snap


def main() -> int:
    once = "--once" in sys.argv
    print(
        f"Monitoring UID {UID} detail_books={list(DETAIL_BOOKS)} every {INTERVAL_SEC}s",
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
