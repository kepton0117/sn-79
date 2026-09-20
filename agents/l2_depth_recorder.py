# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Persist engine L2 depth (21 levels) for BookManageBot display.

Validator Prometheus book_gauges often only expose ~5 levels. Miners already
receive full L21 via ``Book.from_json`` / ``from_ypy`` (depth=21). This module
snapshots ``state.books`` on each respond tick so BMB can show the full ladder.

Writes (atomic replace)::

    <agents>/data/<uid>/l2_depth.json
    <agents>/l2_depth.<uid>.json          # next to live configs
    <output_dir>/l2_depth.json            # when agent has output_dir
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DEPTH = 21
DEFAULT_MIN_INTERVAL_SEC = 1.0

_lock = threading.Lock()
_last_write_wall: dict[int, float] = {}
_logged_start = False


def _g(obj: Any, *names: str, default=None):
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                pass
    return default


def _level_price_qty(lvl: Any) -> tuple[float | None, float | None]:
    if lvl is None:
        return None, None
    if isinstance(lvl, dict):
        px = lvl.get("p", lvl.get("price"))
        qty = lvl.get("q", lvl.get("qty", lvl.get("quantity", lvl.get("volume"))))
        try:
            px_f = float(px) if px is not None else None
        except (TypeError, ValueError):
            px_f = None
        try:
            qty_f = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            qty_f = None
        return px_f, qty_f
    raw = getattr(lvl, "_raw", None)
    if isinstance(raw, dict):
        try:
            px = float(raw["p"]) if raw.get("p") is not None else None
        except (TypeError, ValueError, KeyError):
            px = None
        try:
            qty = float(raw["q"]) if raw.get("q") is not None else None
        except (TypeError, ValueError, KeyError):
            qty = None
        return px, qty
    if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
        try:
            return float(lvl[0]), float(lvl[1])
        except (TypeError, ValueError):
            return None, None
    px = _g(lvl, "price", "p")
    qty = _g(lvl, "quantity", "qty", "q", "volume", "v")
    try:
        px_f = float(px) if px is not None else None
    except (TypeError, ValueError):
        px_f = None
    try:
        qty_f = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty_f = None
    return px_f, qty_f


def _side_levels(levels: Any, *, max_levels: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if levels is None:
        return out
    # Book.bids/asks are lists; L2Snapshot may be price→LevelInfo dicts.
    # Never materialize the full side — books can hold far more than max_levels.
    try:
        if isinstance(levels, dict):
            it = levels.values()
        else:
            it = levels
        for i, lvl in enumerate(it):
            if i >= max_levels:
                break
            px, qty = _level_price_qty(lvl)
            if px is None and qty is None:
                continue
            out.append({"level": i, "price": px, "qty": qty})
    except TypeError:
        return out
    return out


def _book_side_levels(book: Any, *, side: str, max_levels: int) -> list[dict[str, Any]]:
    raw_book = getattr(book, "_raw", None)
    if isinstance(raw_book, dict):
        key = "b" if side == "bid" else "a"
        wire = raw_book.get(key, [])
        if isinstance(wire, list):
            return _side_levels(wire, max_levels=max_levels)
    if side == "bid":
        return _side_levels(_g(book, "bids", "b"), max_levels=max_levels)
    return _side_levels(_g(book, "asks", "a"), max_levels=max_levels)


def _agents_dir_from_live(live: Any) -> Path | None:
    if not live:
        return None
    try:
        p = Path(live).resolve()
        if p.parent.name == "data" and re.search(
            r"top(?:strategy|agent)\.\d+\.(?:live\.)?json$", p.name
        ):
            return p.parent.parent
        return p.parent
    except Exception:
        return None


def _uid_from_agent(agent: Any) -> int | None:
    for attr in ("uid", "_uid", "agent_id"):
        raw = getattr(agent, attr, None)
        if raw is None:
            continue
        try:
            return int(raw)
        except (TypeError, ValueError):
            continue
    live = getattr(agent, "live_config_path", None)
    if live:
        m = re.search(
            r"top(?:strategy|agent)\.(\d+)\.(?:live\.)?json$", str(live)
        )
        if m:
            return int(m.group(1))
    return None


def _write_paths(agent: Any, uid: int) -> list[Path]:
    paths: list[Path] = []
    live = getattr(agent, "live_config_path", None)
    agents_dir = _agents_dir_from_live(live)
    if agents_dir is None:
        agents_dir = Path(__file__).resolve().parent

    paths.append(agents_dir / "data" / str(uid) / "l2_depth.json")
    paths.append(agents_dir / f"l2_depth.{uid}.json")

    for attr in ("output_dir", "data_dir", "_data_dir", "fifo_data_dir"):
        raw = getattr(agent, attr, None)
        if not raw:
            continue
        try:
            root = Path(raw)
            if root.name == "data" and root.parent.name == "agents":
                paths.append(root / str(uid) / "l2_depth.json")
                continue
            paths.append(root / str(uid) / "l2_depth.json")
            nested = root / str(uid)
            if nested.is_dir() or str(raw).rstrip("/").endswith(f"/{uid}"):
                paths.append(nested / "l2_depth.json")
        except Exception:
            continue

    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _account_balances(agent: Any, book_id: int) -> dict[str, float | None]:
    """Balances from agent account for one book (axon state every tick)."""
    out: dict[str, float | None] = {
        "free_base": None,
        "free_quote": None,
        "int_base": None,
        "base_total": None,
        "base_reserved": None,
        "quote_total": None,
        "base_loan": None,
        "base_collateral": None,
    }
    accounts = getattr(agent, "accounts", None)
    if not isinstance(accounts, dict):
        return out
    account = accounts.get(book_id)
    if account is None:
        account = accounts.get(str(book_id))
    if account is None:
        return out
    bb = _g(account, "base_balance", "bb")
    qb = _g(account, "quote_balance", "qb")

    def _f(obj: Any, *names: str) -> float | None:
        raw = _g(obj, *names)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    out["free_base"] = _f(bb, "free", "f")
    out["base_total"] = _f(bb, "total", "t")
    out["base_reserved"] = _f(bb, "reserved", "r")
    out["int_base"] = _f(bb, "initial", "i")
    out["free_quote"] = _f(qb, "free", "f")
    out["quote_total"] = _f(qb, "total", "t")
    out["base_loan"] = _f(account, "base_loan", "bl")
    out["base_collateral"] = _f(account, "base_collateral", "bc")
    return out


def _merge_all_account_balances(agent: Any, books: dict[str, Any]) -> None:
    """Ensure every account book has axon balances, even without L2 depth."""
    accounts = getattr(agent, "accounts", None)
    if not isinstance(accounts, dict):
        return
    for book_id in accounts.keys():
        try:
            bid = int(book_id)
        except (TypeError, ValueError):
            continue
        key = str(bid)
        bals = _account_balances(agent, bid)
        if bals.get("int_base") is None and bals.get("free_base") is None:
            continue
        row = books.get(key)
        if not isinstance(row, dict):
            row = {"book_id": bid, "bids": [], "asks": [], "bid_levels": 0, "ask_levels": 0}
            books[key] = row
        for field, val in bals.items():
            if val is not None:
                row[field] = val
        row["balances_source"] = "axon"


def snapshot_from_state(
    state: Any,
    *,
    uid: int | None = None,
    max_levels: int = DEFAULT_DEPTH,
    agent: Any = None,
) -> dict[str, Any]:
    books_raw = _g(state, "books") or {}
    books: dict[str, Any] = {}
    max_bid = 0
    max_ask = 0
    try:
        items = books_raw.items() if hasattr(books_raw, "items") else []
    except Exception:
        items = []
    for book_id, book in items:
        try:
            bid = int(book_id if not hasattr(book, "id") else _g(book, "id", "i", default=book_id))
        except (TypeError, ValueError):
            continue
        bids = _book_side_levels(book, side="bid", max_levels=max_levels)
        asks = _book_side_levels(book, side="ask", max_levels=max_levels)
        if not bids and not asks:
            # Still keep a row if we later attach balances from accounts.
            if agent is None:
                continue
        max_bid = max(max_bid, len(bids))
        max_ask = max(max_ask, len(asks))
        best_bid = bids[0]["price"] if bids else None
        best_ask = asks[0]["price"] if asks else None
        mid = None
        if best_bid is not None and best_ask is not None:
            mid = (float(best_bid) + float(best_ask)) / 2.0
        row: dict[str, Any] = {
            "book_id": bid,
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "bid_levels": len(bids),
            "ask_levels": len(asks),
        }
        if agent is not None:
            bals = _account_balances(agent, bid)
            row.update({k: v for k, v in bals.items() if v is not None})
            if bals.get("int_base") is not None:
                row["balances_source"] = "axon"
        if bids or asks or row.get("int_base") is not None:
            books[str(bid)] = row

    if agent is not None:
        _merge_all_account_balances(agent, books)

    sim_id = None
    try:
        cfg = _g(state, "config")
        sim_id = _g(cfg, "simulation_id", "simulationId")
    except Exception:
        sim_id = None
    ts = _g(state, "timestamp", "t")
    try:
        ts_i = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts_i = None

    n_int = sum(
        1
        for b in books.values()
        if isinstance(b, dict) and b.get("int_base") is not None
    )
    return {
        "available": bool(books),
        "source": "axon_engine",
        "uid": uid,
        "sim_id": sim_id,
        "sim_ts": ts_i,
        "max_levels": int(max_levels),
        "book_count": len(books),
        "int_base_books": n_int,
        "max_bid_levels": max_bid,
        "max_ask_levels": max_ask,
        "wall_ts": time.time(),
        "books": books,
    }


def record_l2_from_state(
    agent: Any,
    state: Any,
    *,
    max_levels: int = DEFAULT_DEPTH,
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
) -> dict[str, Any] | None:
    """Throttle-write L21 snapshot. Returns payload when written, else None."""
    global _logged_start
    uid = _uid_from_agent(agent)
    if uid is None:
        return None

    now = time.time()
    with _lock:
        last = _last_write_wall.get(uid, 0.0)
        if now - last < float(min_interval_sec):
            return None
        _last_write_wall[uid] = now

    payload = snapshot_from_state(
        state, uid=uid, max_levels=max_levels, agent=agent
    )
    if not payload.get("available"):
        return None

    for path in _write_paths(agent, uid):
        try:
            _atomic_write(path, payload)
        except Exception:
            continue

    if not _logged_start:
        _logged_start = True
        try:
            print(
                f"L2_DEPTH_RECORDER uid={uid} "
                f"max_bid={payload.get('max_bid_levels')} "
                f"max_ask={payload.get('max_ask_levels')} "
                f"books={payload.get('book_count')}"
            )
        except Exception:
            pass
    return payload


def patch_topstrategy(
    *,
    max_levels: int = DEFAULT_DEPTH,
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
) -> None:
    """Wrap ``Topstrategy.respond`` to dump L21 depth for BMB."""
    import Topstrategy as mod

    cls = mod.Topstrategy
    if getattr(cls, "_l2_depth_recorder_patched", False):
        return
    orig = cls.respond

    def _respond_with_l2(self, state):
        try:
            record_l2_from_state(
                self,
                state,
                max_levels=max_levels,
                min_interval_sec=min_interval_sec,
            )
        except Exception:
            pass
        return orig(self, state)

    cls.respond = _respond_with_l2
    cls._l2_depth_recorder_patched = True
    # Intentionally do not register LIVE_TUNABLE_PARAMS — snapshot requires attrs.
