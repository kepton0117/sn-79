"""Merge the 227 tick log and the three helper logs into one per-tick view of book 78."""

import re
import sys
from datetime import datetime

BOOK = sys.argv[1] if len(sys.argv) > 1 else "78"
TOP_LOG = "/root/MVTRX_08_29/sn-79/mainnet/runs/20260919_194155_uid227/pm2/out.log"
HELPERS = {
    "120": "/root/.pm2/logs/miner-sn79-gold-9099-sn1005-out.log",
    "135": "/root/.pm2/logs/miner-sn79-gold-9101-sn1004-out.log",
    "230": "/root/.pm2/logs/miner-sn79-gold-9102-sn1003-out.log",
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")
WALL = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
SIMT = re.compile(r"SIMULATION TIME : (\d{2}:\d{2}:\d{2})\.\d+ \(T=(\d+)\)")
BOOKLINE = re.compile(rf"^BOOK {BOOK} : (.+?) AT (\d{{2}}:\d{{2}}:\d{{2}})\.\d+ \(T=(\d+)\)")
IOC = re.compile(
    rf"HelpAgent book {BOOK}: IOC (BUY|SELL) qty=([\d.]+)@([\d.]+) \((\w+)=([\d.]+) (\w+)=([\d.]+)\)"
)


def wall_of(line):
    m = WALL.search(ANSI.sub("", line))
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f") if m else None


# Pass 1: wall <-> sim tick map, and every BOOK <n> event keyed by sim tick.
ticks = {}          # tick -> sim clock label
wall_to_tick = []   # (wall, tick) sorted
events = {}         # tick -> [event strings]
last_wall = None

for raw in open(TOP_LOG, errors="ignore"):
    line = ANSI.sub("", raw).strip()
    w = wall_of(raw)
    if w:
        last_wall = w
    m = SIMT.search(line)
    if m:
        tick = int(m.group(2)) // 10**9
        ticks[tick] = m.group(1)
        if last_wall:
            wall_to_tick.append((last_wall, tick))
        continue
    m = BOOKLINE.match(line)
    if m:
        tick = int(m.group(3)) // 10**9
        ticks.setdefault(tick, m.group(2))
        events.setdefault(tick, []).append(m.group(1))

wall_to_tick.sort()
if not events:
    print(f"no book {BOOK} events in the 227 log")
    raise SystemExit


def tick_for_wall(w):
    """Nearest tick whose wall stamp is at or before w."""
    lo, hi, best = 0, len(wall_to_tick) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if wall_to_tick[mid][0] <= w:
            best = wall_to_tick[mid][1]
            lo = mid + 1
        else:
            hi = mid - 1
    return best


# Pass 2: helper IOCs bucketed into the tick that was live when they were sent.
helper_ioc = {}  # tick -> uid -> [str]
for uid, path in HELPERS.items():
    try:
        fh = open(path, errors="ignore")
    except OSError:
        continue
    for raw in fh:
        m = IOC.search(ANSI.sub("", raw))
        if not m:
            continue
        w = wall_of(raw)
        if w is None:
            continue
        tick = tick_for_wall(w)
        if tick is None:
            continue
        side, qty, px, _rk, rem, wk, wv = m.groups()
        helper_ioc.setdefault(tick, {}).setdefault(uid, []).append(
            f"IOC {side:<4} {float(qty):>9.4f}@{px}  (sees target_rem={rem}, {wk}={wv})"
        )

lo, hi = min(events), max(events)
print(f"=== book {BOOK}: ticks {lo}..{hi}  ({ticks.get(lo)} -> {ticks.get(hi)} sim) ===\n")
for tick in range(lo, hi + 1):
    evs = events.get(tick, [])
    ioc = helper_ioc.get(tick, {})
    if not evs and not ioc:
        continue
    print(f"--- tick {tick}  sim {ticks.get(tick, '?')} ---")
    for e in evs:
        print(f"    227  {e}")
    for uid in ("120", "135", "230"):
        for s in ioc.get(uid, []):
            print(f"    {uid}  {s}")
    print()
