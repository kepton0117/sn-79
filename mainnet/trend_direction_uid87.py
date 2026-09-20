#!/usr/bin/env python3
"""Set TopAgent open_direction from latest N sim-minutes of trade price history.

Reads merged trades.csv from UID 87 + helper miners (same sim session), computes
per-book trend over a rolling window, and writes open_direction into
agents/data/topagent.<uid>.json. TopAgent and HelpAgent hot-reload on mtime change.

No agent code changes required.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

try:
    from taos.im.utils import timestamp_from_duration
except ImportError:
    timestamp_from_duration = None  # type: ignore

SIM_MINUTE_NS = 60_000_000_000

DEFAULT_TARGET_UID = 87
DEFAULT_HELPER_UIDS = (46, 60, 80, 90, 105, 163, 228)
DEFAULT_LOOKBACK_MIN = 10
DEFAULT_FLAT_PCT = 0.001
DEFAULT_MIN_TRADES = 2
DEFAULT_INTERVAL_SEC = 15.0


@dataclass(frozen=True)
class TradeRow:
    ts_ns: int
    book_id: int
    trade_id: int
    price: float
    quantity: float


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw) if raw.strip() else default
    except ValueError:
        return default


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


def parse_ts_ns(raw: str) -> int | None:
    if not raw or timestamp_from_duration is None:
        return None
    try:
        return int(timestamp_from_duration(raw.strip()))
    except (TypeError, ValueError):
        return None


def find_trades_csv(uid: int, sim_id: str | None) -> Path | None:
    base = REPO / "agents" / "data" / str(uid)
    if not base.is_dir():
        return None
    if sim_id:
        matches = sorted(base.glob(f"*/{sim_id}/trades.csv"), key=lambda p: p.stat().st_mtime)
        if matches:
            return matches[-1]
    candidates = sorted(base.rglob("trades.csv"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def load_trades(paths: list[Path]) -> dict[tuple[int, int], TradeRow]:
    """Dedupe by (book_id, trade_id) across all source CSV files."""
    out: dict[tuple[int, int], TradeRow] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            with path.open(newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ts_ns = parse_ts_ns(row.get("timestamp", ""))
                    if ts_ns is None:
                        continue
                    try:
                        book_id = int(row.get("bookId", row.get("book_id", -1)))
                        trade_id = int(row.get("tradeId", row.get("trade_id", -1)))
                        price = float(row.get("price", 0))
                        qty = float(row.get("quantity", 0))
                    except (TypeError, ValueError):
                        continue
                    if book_id < 0 or trade_id < 0 or price <= 0:
                        continue
                    key = (book_id, trade_id)
                    prev = out.get(key)
                    if prev is None or ts_ns >= prev.ts_ns:
                        out[key] = TradeRow(
                            ts_ns=ts_ns,
                            book_id=book_id,
                            trade_id=trade_id,
                            price=price,
                            quantity=qty,
                        )
        except OSError:
            continue
    return out


def trades_in_window(
    all_trades: dict[tuple[int, int], TradeRow],
    book_id: int,
    start_ns: int,
    end_ns: int,
) -> list[TradeRow]:
    rows = [
        t
        for t in all_trades.values()
        if t.book_id == book_id and start_ns <= t.ts_ns <= end_ns
    ]
    rows.sort(key=lambda t: t.ts_ns)
    return rows


def direction_from_trades(
    trades: list[TradeRow],
    *,
    flat_threshold_pct: float,
    min_trades: int,
    current: str | None,
) -> tuple[str | None, dict]:
    meta: dict = {
        "trade_count": len(trades),
        "open_price": None,
        "close_price": None,
        "pct_change": None,
        "vwap": None,
    }
    if len(trades) < min_trades:
        meta["reason"] = f"insufficient_trades (<{min_trades})"
        return None, meta

    open_px = trades[0].price
    close_px = trades[-1].price
    notional = sum(t.price * t.quantity for t in trades)
    volume = sum(t.quantity for t in trades)
    vwap = notional / volume if volume > 0 else close_px
    meta.update(open_price=open_px, close_price=close_px, vwap=round(vwap, 8))

    if open_px <= 0:
        meta["reason"] = "invalid_open_price"
        return None, meta

    pct = (close_px - open_px) / open_px
    meta["pct_change"] = round(pct, 8)

    th = max(0.0, flat_threshold_pct)
    if pct > th:
        meta["reason"] = "uptrend"
        return "buy", meta
    if pct < -th:
        meta["reason"] = "downtrend"
        return "sell", meta

    meta["reason"] = "flat"
    return current, meta


def _book_direction_static(item: dict) -> bool:
    """When true, the loop watcher never updates open_direction for this book."""
    if item.get("direction_static") is True:
        return True
    if item.get("direction_trend") is False:
        return True
    return False


def init_static_directions(
    target_uid: int,
    book_ids: list[int] | None,
    *,
    lookback_min: int,
    flat_threshold_pct: float,
    min_trades: int,
    dry_run: bool = False,
) -> dict:
    """Set open_direction once from trade history; mark book direction_static."""
    cfg_path = REPO / f"agents/data/topagent.{target_uid}.json"
    l2_path = REPO / f"agents/data/{target_uid}/l2_depth.json"
    cfg = load_json(cfg_path) or {}

    lookback_min = int(cfg.get("direction_trend_lookback_minutes", lookback_min))
    flat_threshold_pct = float(
        cfg.get("direction_trend_flat_threshold_pct", flat_threshold_pct)
    )
    min_trades = int(cfg.get("direction_trend_min_trades", min_trades))

    l2 = load_json(l2_path) or {}
    sim_ts = int(l2.get("sim_ts") or 0)
    sim_id = str(l2.get("sim_id") or "")
    if sim_ts <= 0:
        return {"skipped": True, "reason": "missing sim_ts in l2_depth.json"}

    lookback_ns = lookback_min * SIM_MINUTE_NS
    window_start = sim_ts - lookback_ns

    source_uids = resolve_source_uids(cfg, target_uid)
    csv_paths = [p for uid in source_uids if (p := find_trades_csv(uid, sim_id or None))]
    all_trades = load_trades(csv_paths)

    open_books = cfg.get("open_books") or []
    want = set(book_ids) if book_ids else None
    inits: list[dict] = []
    updated = False

    for item in open_books:
        if not isinstance(item, dict):
            continue
        book_id = int(item.get("book", -1))
        if book_id < 0:
            continue
        if want is not None and book_id not in want:
            continue

        window_trades = trades_in_window(all_trades, book_id, window_start, sim_ts)
        current = str(item.get("open_direction", "")).strip().lower()
        if current not in ("buy", "sell"):
            current = "buy"
        new_dir, meta = direction_from_trades(
            window_trades,
            flat_threshold_pct=flat_threshold_pct,
            min_trades=min_trades,
            current=current,
        )
        if new_dir is None:
            new_dir = current
        item["open_direction"] = new_dir
        item["direction_static"] = True
        updated = True
        inits.append(
            {
                "book": book_id,
                "direction": new_dir,
                "direction_static": True,
                "trades": meta.get("trade_count"),
                "open_price": meta.get("open_price"),
                "close_price": meta.get("close_price"),
                "pct_change": meta.get("pct_change"),
                "reason": meta.get("reason"),
            }
        )

    result = {
        "mode": "init",
        "wall_utc": datetime.now(timezone.utc).isoformat(),
        "target_uid": target_uid,
        "sim_ts": sim_ts,
        "lookback_minutes": lookback_min,
        "inits": inits,
        "updated": updated,
        "dry_run": dry_run,
    }
    if updated and not dry_run:
        save_json(cfg_path, cfg)
        result["config_path"] = str(cfg_path)
    return result


def resolve_source_uids(cfg: dict | None, target_uid: int) -> tuple[int, ...]:
    if cfg:
        raw = cfg.get("direction_trend_trade_sources")
        if isinstance(raw, list) and raw:
            try:
                uids = tuple(int(x) for x in raw)
                if target_uid not in uids:
                    uids = (target_uid,) + uids
                return uids
            except (TypeError, ValueError):
                pass
    env = os.environ.get("TREND_TRADE_UIDS", "")
    if env.strip():
        try:
            uids = tuple(int(x.strip()) for x in env.split(",") if x.strip())
            if target_uid not in uids:
                uids = (target_uid,) + uids
            return uids
        except ValueError:
            pass
    return (target_uid,) + DEFAULT_HELPER_UIDS


def update_open_directions(
    target_uid: int,
    *,
    lookback_min: int,
    flat_threshold_pct: float,
    min_trades: int,
    dry_run: bool = False,
) -> dict:
    cfg_path = REPO / f"agents/data/topagent.{target_uid}.json"
    l2_path = REPO / f"agents/data/{target_uid}/l2_depth.json"
    state_path = REPO / f"agents/data/{target_uid}/trend_direction_state.json"

    cfg = load_json(cfg_path) or {}
    if cfg.get("direction_trend_enabled") is False:
        return {"skipped": True, "reason": "direction_trend_enabled=false"}

    lookback_min = int(cfg.get("direction_trend_lookback_minutes", lookback_min))
    flat_threshold_pct = float(
        cfg.get("direction_trend_flat_threshold_pct", flat_threshold_pct)
    )
    min_trades = int(cfg.get("direction_trend_min_trades", min_trades))

    l2 = load_json(l2_path) or {}
    sim_ts = int(l2.get("sim_ts") or 0)
    sim_id = str(l2.get("sim_id") or "")
    if sim_ts <= 0:
        return {"skipped": True, "reason": "missing sim_ts in l2_depth.json"}

    lookback_ns = lookback_min * SIM_MINUTE_NS
    window_start = sim_ts - lookback_ns

    source_uids = resolve_source_uids(cfg, target_uid)
    csv_paths: list[Path] = []
    for uid in source_uids:
        p = find_trades_csv(uid, sim_id or None)
        if p is not None:
            csv_paths.append(p)

    all_trades = load_trades(csv_paths)
    open_books = cfg.get("open_books") or []
    if not open_books:
        return {"skipped": True, "reason": "no open_books in config"}

    changes: list[dict] = []
    books_meta: dict[str, dict] = {}
    updated = False

    for item in open_books:
        if not isinstance(item, dict):
            continue
        book_id = int(item.get("book", -1))
        if book_id < 0:
            continue
        if _book_direction_static(item):
            books_meta[str(book_id)] = {
                "book": book_id,
                "skipped": True,
                "reason": "direction_static",
                "current_direction": str(item.get("open_direction", "buy")).lower(),
            }
            continue
        current = str(item.get("open_direction", "buy")).strip().lower()
        if current not in ("buy", "sell"):
            current = "buy"

        window_trades = trades_in_window(all_trades, book_id, window_start, sim_ts)
        new_dir, meta = direction_from_trades(
            window_trades,
            flat_threshold_pct=flat_threshold_pct,
            min_trades=min_trades,
            current=current,
        )
        meta["book"] = book_id
        meta["current_direction"] = current
        meta["new_direction"] = new_dir
        meta["window_start_ns"] = window_start
        meta["window_end_ns"] = sim_ts
        books_meta[str(book_id)] = meta

        if new_dir is None or new_dir == current:
            continue
        item["open_direction"] = new_dir
        updated = True
        changes.append(
            {
                "book": book_id,
                "from": current,
                "to": new_dir,
                "trades": meta.get("trade_count"),
                "pct_change": meta.get("pct_change"),
                "reason": meta.get("reason"),
            }
        )

    result = {
        "wall_utc": datetime.now(timezone.utc).isoformat(),
        "target_uid": target_uid,
        "sim_id": sim_id,
        "sim_ts": sim_ts,
        "lookback_minutes": lookback_min,
        "trade_sources": list(source_uids),
        "trades_csv_files": [str(p) for p in csv_paths],
        "total_trades_loaded": len(all_trades),
        "books": books_meta,
        "changes": changes,
        "updated": updated,
        "dry_run": dry_run,
    }

    save_json(state_path, result)

    if updated and not dry_run:
        save_json(cfg_path, cfg)
        result["config_path"] = str(cfg_path)

    return result


def log_result(result: dict) -> None:
    if result.get("skipped"):
        print(f"[trend-direction] skipped: {result.get('reason')}")
        return
    changes = result.get("changes") or []
    if changes:
        for ch in changes:
            print(
                f"[trend-direction] book {ch['book']}: "
                f"{ch['from']} -> {ch['to']} "
                f"(trades={ch.get('trades')} pct={ch.get('pct_change')} "
                f"reason={ch.get('reason')})"
            )
    else:
        books = result.get("books") or {}
        summary = ", ".join(
            (
                f"b{k}={v.get('current_direction')}(static)"
                if v.get("skipped")
                else f"b{k}={v.get('current_direction')}({v.get('trade_count')}t,{v.get('reason')})"
            )
            for k, v in sorted(books.items(), key=lambda x: int(x[0]))
        )
        print(
            f"[trend-direction] no change sim_ts={result.get('sim_ts')} "
            f"lookback={result.get('lookback_minutes')}m {summary}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uid",
        type=int,
        default=_env_int("TREND_TARGET_UID", DEFAULT_TARGET_UID),
        help="TopAgent UID (default: 87)",
    )
    parser.add_argument(
        "--lookback-min",
        type=int,
        default=_env_int("TREND_LOOKBACK_MIN", DEFAULT_LOOKBACK_MIN),
        help="Sim-minute lookback window (default: 10)",
    )
    parser.add_argument(
        "--flat-pct",
        type=float,
        default=_env_float("TREND_FLAT_PCT", DEFAULT_FLAT_PCT),
        help="Min |pct change| to flip direction (default: 0.001 = 0.1%%)",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=_env_int("TREND_MIN_TRADES", DEFAULT_MIN_TRADES),
        help="Minimum trades in window before updating (default: 2)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=_env_float("TREND_INTERVAL_SEC", DEFAULT_INTERVAL_SEC),
        help="Loop interval seconds (default: 15)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one update and exit",
    )
    parser.add_argument(
        "--init-books",
        type=str,
        default="",
        help="Comma-separated book ids: set open_direction once from history "
        "and mark direction_static (then exit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute directions but do not write topagent JSON",
    )
    args = parser.parse_args()

    if timestamp_from_duration is None:
        print("error: taos.im.utils.timestamp_from_duration unavailable", file=sys.stderr)
        return 1

    def run_once() -> dict:
        return update_open_directions(
            args.uid,
            lookback_min=args.lookback_min,
            flat_threshold_pct=args.flat_pct,
            min_trades=args.min_trades,
            dry_run=args.dry_run,
        )

    if args.init_books.strip():
        try:
            book_ids = [int(x.strip()) for x in args.init_books.split(",") if x.strip()]
        except ValueError:
            print("error: --init-books requires comma-separated integers", file=sys.stderr)
            return 1
        result = init_static_directions(
            args.uid,
            book_ids,
            lookback_min=args.lookback_min,
            flat_threshold_pct=args.flat_pct,
            min_trades=args.min_trades,
            dry_run=args.dry_run,
        )
        for init in result.get("inits") or []:
            print(
                f"[trend-direction] init book {init['book']}: "
                f"direction={init['direction']} static=true "
                f"(trades={init.get('trades')} "
                f"{init.get('open_price')}->{init.get('close_price')} "
                f"pct={init.get('pct_change')} reason={init.get('reason')})"
            )
        if not result.get("inits"):
            print("[trend-direction] init: no matching open_books entries")
        return 0

    if args.once:
        log_result(run_once())
        return 0

    print(
        f"[trend-direction] watching uid={args.uid} "
        f"lookback={args.lookback_min}m interval={args.interval}s "
        f"dry_run={args.dry_run}"
    )
    while True:
        try:
            log_result(run_once())
        except Exception as exc:
            print(f"[trend-direction] error: {exc}", file=sys.stderr)
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
