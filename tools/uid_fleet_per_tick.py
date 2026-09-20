#!/usr/bin/env python3
"""Merge TopAgent pm2 log + HelpAgent logs into one row per validator simulation tick."""

from __future__ import annotations

import argparse
import glob
import re
from datetime import datetime
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
WALL = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)")
SIMT = re.compile(
    r"SIMULATION TIME : (\d{2}:\d{2}:\d{2})\.\d+ \(T=(\d+)\)"
)
BOOK_ANY = re.compile(
    r"^BOOK (\d+) : (.+?) AT (\d{2}:\d{2}:\d{2})\.\d+ \(T=(\d+)\)"
)
BOOK_ANY_NO_AT = re.compile(r"^BOOK (\d+) : (.+)$")
INSTR_LINE = re.compile(
    r"^(BUY|SELL)\s+([\d.]+)@([\d.]+)\s+ON BOOK (\d+)|"
    r"^CANCEL ORDER #(\d+) ON BOOK (\d+)"
)
HELP = re.compile(
    r"HelpAgent book (\d+): (.+)"
)
HELP_IOC = re.compile(
    r"HelpAgent book (\d+): IOC (BUY|SELL) qty=([\d.]+)@([\d.]+) \((.+)\)"
)

LOG_DIR = Path.home() / ".pm2/logs"

FLEETS: dict[str, dict] = {
    "83": {
        "target_id": "83",
        "target_log": LOG_DIR / "miner-sn79-gold-9103-sn1000-out.log",
        "helpers": {
            "120": [LOG_DIR / "miner-sn79-gold-9099-sn1005-out.log"],
            "135": [LOG_DIR / "miner-sn79-gold-9101-sn1004-out.log"],
            "227": [LOG_DIR / "miner-sn79-gold-9098-sn1002-out.log"],
            "230": [LOG_DIR / "miner-sn79-gold-9102-sn1003-out.log"],
        },
    },
    "87": {
        "target_id": "87",
        "target_log": LOG_DIR / "miner-sn79-gold-9133-sn987-out.log",
        "helper_globs": {
            "228": "miner-sn79-gold-9124-sn984-out*.log",
            "90": "miner-sn79-gold-9125-sn980-out*.log",
            "163": "miner-sn79-gold-9126-sn979-out*.log",
            "105": "miner-sn79-gold-9127-sn977-out*.log",
            "60": "miner-sn79-gold-9128-sn983-out*.log",
            "46": "miner-sn79-gold-9129-sn981-out*.log",
            "80": "miner-sn79-gold-9131-sn986-out*.log",
        },
    },
}

DEFAULT_TARGET_LOG = FLEETS["87"]["target_log"]
DEFAULT_HELPER_GLOBS = FLEETS["87"]["helper_globs"]


def strip(s: str) -> str:
    return ANSI.sub("", s).strip()


def wall_of(raw: str) -> datetime | None:
    m = WALL.search(strip(raw))
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")


def expand_logs(patterns: list[str], log_dir: Path) -> list[Path]:
    out: list[Path] = []
    for pat in patterns:
        if "/" in pat:
            out.extend(Path(p) for p in glob.glob(pat))
        else:
            out.extend(Path(p) for p in glob.glob(str(log_dir / pat)))
    return sorted({p.resolve() for p in out if p.is_file()})


def parse_target_blocks(paths: list[Path], book_filter: int | None) -> tuple[
    dict[int, dict], list[tuple[datetime, int]]
]:
    """tick -> {sim, book_events[], instructions[]} and wall->tick map."""
    ticks: dict[int, dict] = {}
    wall_to_tick: list[tuple[datetime, int]] = []
    cur_tick: int | None = None
    cur_sim = "?"
    cur_wall: datetime | None = None
    in_instr = False
    instr_ready = False

    def flush_instr(line: str) -> None:
        nonlocal cur_tick
        if cur_tick is None:
            return
        m = INSTR_LINE.match(line)
        if not m:
            return
        if m.group(1):
            side, qty, px, book = m.group(1), m.group(2), m.group(3), int(m.group(4))
            if book_filter is not None and book != book_filter:
                return
            ticks[cur_tick]["instructions"].append(
                f"send {side} {qty}@{px} book {book}"
            )
        else:
            oid, book = m.group(5), int(m.group(6))
            if book_filter is not None and book != book_filter:
                return
            ticks[cur_tick]["instructions"].append(f"cancel #{oid} book {book}")

    for path in paths:
        with open(path, errors="ignore") as fh:
            for raw in fh:
                line = strip(raw)
                w = wall_of(raw)
                if w:
                    cur_wall = w

                m = SIMT.search(line)
                if m:
                    cur_sim = m.group(1)
                    cur_tick = int(m.group(2)) // 10**9
                    in_instr = False
                    instr_ready = False
                    ticks.setdefault(
                        cur_tick,
                        {"sim": cur_sim, "book_events": [], "instructions": []},
                    )
                    ticks[cur_tick]["sim"] = cur_sim
                    if cur_wall:
                        wall_to_tick.append((cur_wall, cur_tick))
                    continue

                if "SIMULATION INSTRUCTIONS" in line:
                    in_instr = True
                    instr_ready = False
                    continue
                if "NO SIMULATION INSTRUCTIONS" in line:
                    in_instr = False
                    instr_ready = False
                    continue
                if in_instr and line.startswith("---"):
                    if not instr_ready:
                        instr_ready = True
                        continue
                    in_instr = False
                    instr_ready = False
                    continue
                if in_instr and instr_ready:
                    if not line or line.startswith("Responded"):
                        in_instr = False
                        instr_ready = False
                        continue
                    flush_instr(line)
                    continue

                m = BOOK_ANY.match(line)
                if m:
                    book_id = int(m.group(1))
                    if book_filter is not None and book_id != book_filter:
                        continue
                    if cur_tick is not None:
                        ticks[cur_tick]["book_events"].append(
                            f"book {book_id}: {m.group(2)}"
                        )
                    continue
                m = BOOK_ANY_NO_AT.match(line)
                if m and " AT " not in line:
                    book_id = int(m.group(1))
                    if book_filter is not None and book_id != book_filter:
                        continue
                    if cur_tick is not None:
                        ticks[cur_tick]["book_events"].append(
                            f"book {book_id}: {m.group(2)}"
                        )

    wall_to_tick.sort()
    return ticks, wall_to_tick


def tick_for_wall(
    w: datetime, wall_to_tick: list[tuple[datetime, int]]
) -> int | None:
    """Map wall clock to the validator tick active on [wall_i, wall_{i+1})."""
    if not wall_to_tick:
        return None
    first_w = wall_to_tick[0][0]
    last_w = wall_to_tick[-1][0]
    if w < first_w or w > last_w + __import__("datetime").timedelta(seconds=30):
        return None
    lo, hi, idx = 0, len(wall_to_tick) - 1, 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if wall_to_tick[mid][0] <= w:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if idx + 1 < len(wall_to_tick) and w >= wall_to_tick[idx + 1][0]:
        idx += 1
    return wall_to_tick[idx][1]


def fleet_helpers(fleet_key: str) -> dict[str, list[Path]]:
    spec = FLEETS[fleet_key]
    if "helpers" in spec:
        return {
            uid: [p for p in paths if Path(p).is_file()]
            for uid, paths in spec["helpers"].items()
        }
    return {
        uid: expand_logs([pat], LOG_DIR)
        for uid, pat in spec["helper_globs"].items()
    }


def parse_helpers(
    helper_logs: dict[str, list[Path]],
    wall_to_tick: list[tuple[datetime, int]],
    book_filter: int | None,
    wall_lo: datetime | None,
    wall_hi: datetime | None,
) -> dict[int, dict[str, list[str]]]:
    out: dict[int, dict[str, list[str]]] = {}
    for uid, paths in helper_logs.items():
        for path in paths:
            with open(path, errors="ignore") as fh:
                for raw in fh:
                    line = strip(raw)
                    m = HELP.search(line)
                    if not m:
                        continue
                    book_id = int(m.group(1))
                    if book_filter is not None and book_id != book_filter:
                        continue
                    w = wall_of(raw)
                    if w is None:
                        continue
                    if wall_lo and w < wall_lo:
                        continue
                    if wall_hi and w > wall_hi:
                        continue
                    tick = tick_for_wall(w, wall_to_tick)
                    if tick is None:
                        continue
                    msg = m.group(2)
                    im = HELP_IOC.search(line)
                    if im:
                        msg = (
                            f"IOC {im.group(2)} {float(im.group(3)):>9.4f}@"
                            f"{im.group(4)} book {book_id} ({im.group(5)})"
                        )
                    else:
                        msg = f"book {book_id}: {msg}"
                    out.setdefault(tick, {}).setdefault(uid, []).append(msg)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fleet",
        choices=sorted(FLEETS.keys()),
        default=None,
        help="Preset target + helper logs (83 or 87)",
    )
    ap.add_argument("--target-log", type=Path, default=None)
    ap.add_argument("--target-id", default=None)
    ap.add_argument("--book", type=int, default=None, help="Only show this book id")
    ap.add_argument("--last", type=int, default=0, help="Only print last N ticks with activity")
    ap.add_argument("--from-tick", type=int, default=None)
    ap.add_argument("--to-tick", type=int, default=None)
    ap.add_argument(
        "--target-only",
        action="store_true",
        help="Do not merge default target log (use with --log-glob for one rotated file)",
    )
    ap.add_argument(
        "--log-glob",
        default="",
        help="Extra target log globs (comma-separated), e.g. rotated pm2 files",
    )
    args = ap.parse_args()

    if args.fleet:
        spec = FLEETS[args.fleet]
        target_log = args.target_log or spec["target_log"]
        target_id = args.target_id or spec["target_id"]
        helper_logs = fleet_helpers(args.fleet)
    else:
        target_log = args.target_log or DEFAULT_TARGET_LOG
        target_id = args.target_id or "87"
        helper_logs = {
            uid: expand_logs([pat], LOG_DIR)
            for uid, pat in DEFAULT_HELPER_GLOBS.items()
        }
    helper_uids = sorted(helper_logs.keys(), key=int)

    log_dir = LOG_DIR
    target_paths: list[Path] = []
    if not args.target_only:
        target_paths.append(target_log)
    if args.log_glob:
        for g in args.log_glob.split(","):
            target_paths.extend(expand_logs([g.strip()], log_dir))
    target_paths = sorted({p.resolve() for p in target_paths if p.is_file()})
    if not target_paths:
        raise SystemExit(f"no target logs found (tried {target_log})")

    ticks, wall_to_tick = parse_target_blocks(target_paths, args.book)
    if not ticks:
        raise SystemExit("no simulation ticks in target log(s)")

    wall_lo = wall_to_tick[0][0] if wall_to_tick else None
    wall_hi = wall_to_tick[-1][0] if wall_to_tick else None
    helpers = parse_helpers(
        helper_logs, wall_to_tick, args.book, wall_lo, wall_hi
    )
    active = sorted(ticks.keys())
    if args.from_tick is not None:
        active = [t for t in active if t >= args.from_tick]
    if args.to_tick is not None:
        active = [t for t in active if t <= args.to_tick]
    if args.last and args.last > 0:
        # Last N ticks that have anything to show
        interesting = [
            t
            for t in active
            if ticks[t]["book_events"]
            or ticks[t]["instructions"]
            or helpers.get(t)
        ]
        if interesting:
            active = interesting[-args.last :]

    if not active:
        raise SystemExit("no ticks in the selected range")
    bf = f" book {args.book}" if args.book is not None else ""
    print(
        f"=== UID {target_id}{bf}: ticks {active[0]}..{active[-1]} "
        f"({len(active)} shown) ==="
    )
    print(f"target logs: {', '.join(p.name for p in target_paths)}\n")

    for tick in active:
        block = ticks[tick]
        hel = helpers.get(tick, {})
        if (
            not block["book_events"]
            and not block["instructions"]
            and not hel
        ):
            continue
        print(f"--- tick {tick}  sim {block['sim']} ---")
        for e in block["book_events"]:
            print(f"    {target_id}  exchange  {e}")
        for e in block["instructions"]:
            print(f"    {target_id}  intent    {e}")
        for uid in helper_uids:
            for h in hel.get(uid, []):
                print(f"    {uid}  help      {h}")
        print()


if __name__ == "__main__":
    main()
