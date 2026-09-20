#!/usr/bin/env python3
"""Monitor TopAgent UID 141 (08_29 axon, GOLD sn1003:9098)."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

UID = 141
INTERVAL_SEC = float(os.environ.get("MONITOR_INTERVAL_SEC", "15"))
REPO = Path("/root/MVTRX_08_29/sn-79")
LIVE_JSON = REPO / "agents/data/topagent.141.json"
WAIT_JSON = REPO / "agents/data/141/waiting_orders.json"
DATA_DIR = REPO / "agents/data/141"
LOG = Path.home() / ".pm2/logs/miner-sn79-gold-9098-sn1003-out.log"
ERR = Path.home() / ".pm2/logs/miner-sn79-gold-9098-sn1003-error.log"
STATE = DATA_DIR / "monitor_state.json"
PM2_NAME = "miner-sn79-gold-9098-sn1003"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
TICK_RE = re.compile(r"TopAgent uid=141")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
SENT_RE = re.compile(r"(BUY|SELL)\s+[\d.]+@[\d.]+\s+ON BOOK")


def load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def pm2_row() -> dict:
    import subprocess

    try:
        apps = json.loads(subprocess.check_output(["pm2", "jlist"], text=True))
    except Exception as exc:
        return {"error": str(exc)}
    for a in apps:
        if a.get("name") == PM2_NAME:
            e = a.get("pm2_env") or {}
            return {
                "status": e.get("status"),
                "pid": a.get("pid"),
                "restarts": e.get("restart_time"),
                "unstable": e.get("unstable_restarts"),
            }
    return {"status": "missing"}


def tail_text(path: Path, nbytes: int = 180_000) -> str:
    if not path.is_file():
        return ""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return ANSI_RE.sub("", f.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def snapshot() -> dict:
    text = tail_text(LOG)
    ticks = list(TICK_RE.finditer(text))
    last = ticks[-1] if ticks else None
    no_sim = text.count("NO SIMULATION INSTRUCTIONS")
    placed = text.count("PLACED")
    trades = text.count("TRADE #")
    errors = text.count("ERROR")
    sent = len(re.findall(r"(BUY|SELL)\s+[\d.]+@[\d.]+\s+ON BOOK", text))
    last_ts = None
    if last:
        head = text[max(0, last.start() - 80) : last.start()]
        m = TS_RE.search(head)
        last_ts = m.group(1) if m else None
    wait = load_json(WAIT_JSON) or {}
    live = load_json(LIVE_JSON) or {}
    fifo_files = sorted(DATA_DIR.glob("**/fifo_state_*.json"), key=lambda p: p.stat().st_mtime)
    fifo = load_json(fifo_files[-1]) if fifo_files else {}
    books = fifo.get("books") if isinstance(fifo, dict) else {}
    nets = []
    if isinstance(books, dict):
        for bid, row in books.items():
            if not isinstance(row, dict):
                continue
            net = row.get("fifo_net", row.get("net_qty"))
            try:
                nets.append((int(bid), float(net)))
            except (TypeError, ValueError):
                pass
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    settle_ready = "Settlement ready: proxy" in text
    settle_missing = "SETTLEMENT NOT READY" in text
    out = {
        "ts": now,
        "pm2": pm2_row(),
        "agent": "TopAgent",
        "last_tick_wall": last_ts,
        "last_orders": sent,
        "last_phase": None,
        "last_phase_min": None,
        "last_fifo_seen": None,
        "tail_ticks": len(ticks),
        "tail_no_sim": no_sim,
        "tail_placed": placed,
        "tail_trades": trades,
        "tail_sent": sent,
        "tail_errors": errors,
        "waiting": (wait.get("totals") or {}),
        "sim_id": wait.get("sim_id"),
        "sim_ts": wait.get("sim_ts"),
        "fifo_nonzero": sum(1 for _, n in nets if abs(n) >= 0.25),
        "fifo_abs": round(sum(abs(n) for _, n in nets), 4),
        "open_books": len(live.get("open_books") or []),
        "close_books": len(live.get("close_books") or []),
        "settle_ready": settle_ready,
        "settle_missing": settle_missing,
        "err_bytes": ERR.stat().st_size if ERR.is_file() else 0,
    }
    return out


def fmt(s: dict) -> str:
    pm = s.get("pm2") or {}
    return (
        f"{s['ts']} uid={UID} agent={s.get('agent')} pm2={pm.get('status')} "
        f"rst={pm.get('restarts')} tick={s.get('last_tick_wall')} "
        f"sent={s.get('tail_sent')} placed={s.get('tail_placed')} "
        f"trades={s.get('tail_trades')} no_sim={s.get('tail_no_sim')} "
        f"err={s.get('tail_errors')} wait={s.get('waiting')} "
        f"fifo_nz={s.get('fifo_nonzero')} fifo_abs={s.get('fifo_abs')} "
        f"open={s.get('open_books')} close={s.get('close_books')} "
        f"settle={'ready' if s.get('settle_ready') else ('MISSING' if s.get('settle_missing') else '?')} "
        f"sim={s.get('sim_id')}"
    )


def main() -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    prev = None
    while True:
        try:
            s = snapshot()
            print(fmt(s), flush=True)
            STATE.write_text(json.dumps({"latest": s, "prev": prev}, indent=2) + "\n")
            prev = s
        except Exception as exc:
            print(f"{datetime.now(timezone.utc).isoformat()} monitor error: {exc}", flush=True)
        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
