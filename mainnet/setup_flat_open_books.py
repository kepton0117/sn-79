#!/usr/bin/env python3
"""Add open_books for flat inventory books; trim them from close_books."""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))

from fifo_state_recorder import ValidatorStylePnl  # noqa: E402

TREND_SCRIPT = REPO / "mainnet" / "trend_direction_uid87.py"
DEFAULT_FLAT_THRESHOLD = 0.51


def load_book_kappa(uid: int, kappa_path: Path | None) -> dict[int, float]:
    if kappa_path is None:
        kappa_path = REPO / "agents" / "data" / f"validator_book_kappa.{uid}.json"
    if not kappa_path.is_file():
        return {}
    try:
        data = json.loads(kappa_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    raw = data.get("books") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[int, float] = {}
    for key, val in raw.items():
        try:
            out[int(key)] = float(val)
        except (TypeError, ValueError):
            continue
    return out


def open_book_entry(book_id: int) -> dict:
    return {
        "book": book_id,
        "use_depth_qty": True,
        "open_price": 0,
        "max_quantity": 200.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument(
        "--flat-threshold",
        type=float,
        default=DEFAULT_FLAT_THRESHOLD,
        help="|net_qty| below this counts as flat (default: 0.51)",
    )
    parser.add_argument(
        "--skip-init",
        action="store_true",
        help="Do not run trend_direction --init-books",
    )
    parser.add_argument(
        "--kappa-min",
        type=float,
        default=None,
        help="Require validator kappa > this value (e.g. 0 for kappa>0)",
    )
    parser.add_argument(
        "--kappa-file",
        type=str,
        default="",
        help="Path to validator_book_kappa JSON (default: agents/data/validator_book_kappa.<uid>.json)",
    )
    args = parser.parse_args()

    uid = int(args.uid)
    cfg_path = REPO / f"agents/data/topagent.{uid}.json"
    cfg = json.loads(cfg_path.read_text())

    fifo_paths = sorted(
        glob.glob(str(REPO / f"agents/data/{uid}/fifo_state_*.json")),
        key=lambda p: Path(p).stat().st_mtime,
    )
    if not fifo_paths:
        print(f"error: no fifo_state for UID {uid}", file=sys.stderr)
        return 1
    fifo = ValidatorStylePnl.load(Path(fifo_paths[-1]), 128)

    kappa_by_book: dict[int, float] = {}
    if args.kappa_min is not None:
        kappa_path = Path(args.kappa_file) if args.kappa_file else None
        kappa_by_book = load_book_kappa(uid, kappa_path)

    flatten_books = {
        int(b)
        for b in (cfg.get("flatten_books") or [])
    }

    flat_books: list[int] = []
    inventory_books: list[int] = []
    kappa_skipped = 0
    for book_id in range(128):
        net = float(fifo.net_qty(book_id))
        if abs(net) < float(args.flat_threshold) - 1e-12:
            if book_id in flatten_books:
                continue
            if args.kappa_min is not None:
                kappa = kappa_by_book.get(book_id)
                if kappa is None or kappa <= float(args.kappa_min) + 1e-12:
                    kappa_skipped += 1
                    continue
            flat_books.append(book_id)
        elif abs(net) >= 0.25 - 1e-12:
            inventory_books.append(book_id)

    existing_open = {
        int(x["book"])
        for x in cfg.get("open_books") or []
        if isinstance(x, dict) and "book" in x
    }
    new_open = sorted(set(flat_books) | existing_open)
    cfg["open_books"] = [open_book_entry(b) for b in new_open]

    close_specs = {
        int(x["book"]): float(x.get("target_price", 0.25))
        for x in cfg.get("close_books") or []
        if isinstance(x, dict) and "book" in x
    }
    if not close_specs:
        close_specs = {b: 0.25 for b in range(128)}
    keep_close = sorted(b for b in close_specs if b not in flat_books)
    cfg["close_books"] = [
        {"book": b, "target_price": close_specs.get(b, 0.25)} for b in keep_close
    ]

    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
    print(
        f"UID {uid}: open_books={len(cfg['open_books'])} "
        f"(+{len(new_open) - len(existing_open)} flat) "
        f"close_books={len(cfg['close_books'])} "
        f"(inventory~{len(inventory_books)}) fifo={Path(fifo_paths[-1]).name}"
        + (
            f" kappa_min={args.kappa_min} skipped={kappa_skipped}"
            if args.kappa_min is not None
            else ""
        )
    )

    if args.skip_init:
        return 0

    books_arg = ",".join(str(b) for b in new_open)
    cmd = [sys.executable, str(TREND_SCRIPT), "--uid", str(uid), "--init-books", books_arg]
    print(f"Running init-books for {len(new_open)} books...")
    return subprocess.call(cmd, cwd=str(REPO))


if __name__ == "__main__":
    raise SystemExit(main())
