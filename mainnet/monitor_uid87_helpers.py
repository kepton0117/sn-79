#!/usr/bin/env python3
"""Monitor HelpAgent orders supporting TopAgent UID 87."""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TARGET_UID = int(os.environ.get("MONITOR_TARGET_UID", "87"))
INTERVAL_SEC = float(os.environ.get("MONITOR_INTERVAL_SEC", "15"))
DETAIL_BOOKS = tuple(
    int(x)
    for x in os.environ.get("MONITOR_DETAIL_BOOKS", "0").split(",")
    if x.strip().isdigit()
)

TOPAGENT_JSON = REPO / f"agents/data/topagent.{TARGET_UID}.json"
WAITING_JSON = REPO / f"agents/data/{TARGET_UID}/waiting_orders.json"
STATE_JSON = REPO / f"agents/data/{TARGET_UID}/helper_monitor_state.json"
PM2_LOG_DIR = Path.home() / ".pm2/logs"

HELPERS: list[tuple[int, str, int, Path]] = [
    (228, "sn984", 9124, PM2_LOG_DIR / "miner-sn79-gold-9124-sn984-out.log"),
    (90, "sn980", 9125, PM2_LOG_DIR / "miner-sn79-gold-9125-sn980-out.log"),
    (163, "sn979", 9126, PM2_LOG_DIR / "miner-sn79-gold-9126-sn979-out.log"),
    (105, "sn977", 9127, PM2_LOG_DIR / "miner-sn79-gold-9127-sn977-out.log"),
    (60, "sn983", 9128, PM2_LOG_DIR / "miner-sn79-gold-9128-sn983-out.log"),
    (46, "sn981", 9129, PM2_LOG_DIR / "miner-sn79-gold-9129-sn981-out.log"),
    (80, "sn986", 9131, PM2_LOG_DIR / "miner-sn79-gold-9131-sn986-out.log"),
]
TARGET_LOG = PM2_LOG_DIR / "miner-sn79-gold-9133-sn987-out.log"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
HELP_RE = re.compile(r"HelpAgent book (\d+): (.+)")
SENT_RE = re.compile(r"(BUY|SELL)\s+([\d.]+)@([\d.]+)\s+ON BOOK (\d+)")
TRADE_RE = re.compile(r"BOOK (\d+) : (BUY|SELL) TRADE")
RESPONDED_RE = re.compile(
    r"Responded \(decomp=([0-9.]+)s handle=([0-9.]+)s compress=([0-9.]+)s total=([0-9.]+)s\)"
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


def help_kind(msg: str) -> str:
    if msg.startswith("IOC SELL"):
        return "ioc_sell"
    if msg.startswith("IOC BUY"):
        return "ioc_buy"
    if msg.startswith("charge MARKET BUY"):
        return "charge_buy"
    if msg.startswith("charge MARKET SELL"):
        return "charge_sell"
    if "cold warm" in msg:
        return "cold_warm"
    if msg.startswith("skip"):
        return "skip"
    return "other"


def tail_lines(path: Path, n: int = 400) -> list[str]:
    if not path.is_file():
        return []
    try:
        return [strip_ansi(l) for l in path.read_text(errors="replace").splitlines()[-n:]]
    except OSError:
        return []


def parse_helper_log(uid: int, path: Path) -> dict:
    lines = tail_lines(path, 500)
    helps: list[tuple[int, str, str]] = []
    sends: list[tuple[str, float, float, int]] = []
    trades = 0
    handles: list[float] = []
    for line in lines:
        m = RESPONDED_RE.search(line)
        if m:
            handles.append(float(m.group(2)))
        m = HELP_RE.search(line)
        if m:
            helps.append((int(m.group(1)), help_kind(m.group(2)), m.group(2)))
        m = SENT_RE.search(line)
        if m:
            sends.append((m.group(1), float(m.group(2)), float(m.group(3)), int(m.group(4))))
        if TRADE_RE.search(line):
            trades += 1
    kinds = Counter(k for _, k, _ in helps)
    recent_helps = [(b, k, msg) for b, k, msg in helps if b in DETAIL_BOOKS][-4:]
    recent_sends = [s for s in sends if s[3] in DETAIL_BOOKS][-3:]
    return {
        "uid": uid,
        "help_total": len(helps),
        "kinds": dict(kinds),
        "recent_helps": recent_helps,
        "recent_sends": recent_sends,
        "trades": trades,
        "handle_med": statistics.median(handles) if handles else None,
        "handle_last": handles[-1] if handles else None,
    }


def fmt_target_config(cfg: dict | None) -> list[str]:
    lines = ["--- target TopAgent config ---"]
    if not cfg:
        lines.append("  (topagent JSON missing)")
        return lines
    opens = cfg.get("open_books") or []
    closes = cfg.get("close_books") or []
    lines.append(f"  open_books={len(opens)} close_books={len(closes)}")
    for ob in opens[:5]:
        if isinstance(ob, dict):
            lines.append(
                f"  OPEN book {ob.get('book')}: dir={ob.get('open_direction','buy')} "
                f"price={ob.get('open_price')} max={ob.get('max_quantity')}"
            )
    return lines


def fmt_target_acceptance(waiting: dict | None) -> list[str]:
    lines = ["--- target UID 87 resting (help fills these) ---"]
    if not waiting:
        lines.append("  (waiting_orders.json missing)")
        return lines
    totals = waiting.get("totals") or {}
    lines.append(
        f"  sim_ts={waiting.get('sim_ts')} confirmed={totals.get('open', 0)} "
        f"waiting={totals.get('waiting', 0)} sent_this_tick={totals.get('sent_this_tick', 0)}"
    )
    books = waiting.get("books") or {}
    for bid in DETAIL_BOOKS:
        b = books.get(str(bid)) or books.get(bid)
        if not b:
            continue
        conf = b.get("open_orders") or []
        pend = b.get("waiting") or []
        buy = sum(float(o.get("quantity") or 0) for o in conf if str(o.get("direction", "")).upper() == "BUY")
        sell = sum(float(o.get("quantity") or 0) for o in conf if str(o.get("direction", "")).upper() == "SELL")
        lines.append(
            f"  book {bid}: conf BUY={buy:.4f} SELL={sell:.4f} open_n={len(conf)} wait_n={len(pend)}"
        )
        for o in conf[:2]:
            lines.append(
                f"    resting {o.get('direction')} {o.get('quantity')}@{o.get('price')} "
                f"#{o.get('id')}"
            )
    if len(lines) == 2:
        lines.append("  (no detail-book state)")
    return lines


def fmt_helpers(stats: list[dict]) -> list[str]:
    lines = ["--- helper help orders (last ~500 log lines each) ---"]
    agg: Counter = Counter()
    for st in stats:
        uid = st["uid"]
        kinds = st.get("kinds") or {}
        for k, v in kinds.items():
            agg[k] += v
        med = st.get("handle_med")
        med_s = f"{med:.3f}s" if med is not None else "-"
        kind_str = " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
        lines.append(
            f"  UID {uid}: help={st['help_total']} trades={st['trades']} "
            f"handle_med={med_s} [{kind_str or 'idle'}]"
        )
        for book_id, kind, msg in st.get("recent_helps") or []:
            lines.append(f"    book {book_id} {kind}: {msg[:100]}")
        for side, qty, px, book_id in st.get("recent_sends") or []:
            lines.append(f"    sent {side} {qty}@{px} book {book_id}")
    lines.append(
        "  aggregate: "
        + " ".join(f"{k}={v}" for k, v in sorted(agg.items()))
        if agg
        else "  aggregate: (no help activity in tail window)"
    )
    return lines


def fmt_target_recent() -> list[str]:
    lines = ["--- target UID 87 recent (detail books) ---"]
    got = 0
    for line in tail_lines(TARGET_LOG, 120):
        m = re.search(r"BOOK (\d+)\s*:", line)
        if not m or int(m.group(1)) not in DETAIL_BOOKS:
            continue
        if any(x in line for x in ("PLACED", "TRADE", "CANCELLED")):
            lines.append(f"  {line.strip()[-140:]}")
            got += 1
    if got == 0:
        lines.append("  (no recent detail-book events)")
    return lines


def tick() -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cfg = load_json(TOPAGENT_JSON)
    waiting = load_json(WAITING_JSON)
    helper_stats = [parse_helper_log(uid, path) for uid, _, _, path in HELPERS]

    parts = [
        "",
        "=" * 72,
        f"UID {TARGET_UID} helper-order monitor @ {ts}",
        "=" * 72,
    ]
    parts.extend(fmt_target_config(cfg))
    parts.extend(fmt_target_acceptance(waiting))
    parts.extend(fmt_helpers(helper_stats))
    parts.extend(fmt_target_recent())
    print("\n".join(parts), flush=True)

    save_json(
        STATE_JSON,
        {
            "wall_ts": time.time(),
            "target_uid": TARGET_UID,
            "helpers": [
                {
                    "uid": s["uid"],
                    "help_total": s["help_total"],
                    "kinds": s["kinds"],
                    "handle_med": s["handle_med"],
                }
                for s in helper_stats
            ],
        },
    )


def main() -> int:
    once = "--once" in sys.argv
    print(
        f"Monitoring help orders for UID {TARGET_UID} "
        f"helpers={[u for u, _, _, _ in HELPERS]} detail_books={list(DETAIL_BOOKS)} "
        f"every {INTERVAL_SEC}s",
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
