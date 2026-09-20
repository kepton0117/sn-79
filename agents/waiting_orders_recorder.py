# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Persist waiting (sent-but-not-yet-accepted) orders for BookManageBot.

Each respond tick:
  1. Reconcile prior pending against validator-confirmed ``account.orders``
  2. Add this tick's newly sent limits/markets/cancels to pending
  3. Dump confirmed + waiting (+ last sent) for BMB

Writes (atomic replace)::

    <agents>/data/<uid>/waiting_orders.json
    <agents>/waiting_orders.<uid>.json
    <output_dir>/waiting_orders.json

HelpAgent payloads also include ``recharge_times`` (default 3) and per-book
``recharge_used`` counts for market recharge caps.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_MAX_AGE_SEC = 90.0
DEFAULT_MIN_INTERVAL_SEC = 0.5
DEFAULT_MAX_PENDING_PER_BOOK = 5
CANCEL_PENDING_MAX_AGE_SEC = 15.0
DEFAULT_RECHARGE_TIMES = 3

_lock = threading.Lock()
# uid -> list of pending order dicts
_pending_by_uid: dict[int, list[dict[str, Any]]] = {}
_last_write_wall: dict[int, float] = {}
_logged_start = False
# uid -> {"times": int, "used": {book_id: count}} (HelpAgent market recharge cap)
_recharge_state_by_uid: dict[int, dict[str, Any]] = {}


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

    paths.append(agents_dir / "data" / str(uid) / "waiting_orders.json")
    paths.append(agents_dir / f"waiting_orders.{uid}.json")

    for attr in ("output_dir", "data_dir", "_data_dir", "fifo_data_dir"):
        raw = getattr(agent, attr, None)
        if not raw:
            continue
        try:
            root = Path(raw)
            # Skip shared agents/data — both miners would clobber waiting_orders.json.
            if root.name == "data" and root.parent.name == "agents":
                paths.append(root / str(uid) / "waiting_orders.json")
                continue
            paths.append(root / str(uid) / "waiting_orders.json")
            nested = root / str(uid)
            if nested.is_dir() or str(raw).rstrip("/").endswith(f"/{uid}"):
                paths.append(nested / "waiting_orders.json")
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


def _side_name(side: Any) -> str:
    if side is None:
        return ""
    if isinstance(side, str):
        s = side.strip().upper()
        if s in ("BUY", "BID", "0"):
            return "BUY"
        if s in ("SELL", "ASK", "1"):
            return "SELL"
        return s
    try:
        # OrderDirection enum or int: 0=BUY, 1=SELL (taosim)
        v = int(side)
        return "BUY" if v == 0 else "SELL" if v == 1 else str(v)
    except (TypeError, ValueError):
        name = getattr(side, "name", None)
        if isinstance(name, str):
            return name.upper()
        return str(side).upper()


def _confirmed_orders(agent: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    accounts = getattr(agent, "accounts", None)
    if not isinstance(accounts, dict):
        return out
    for book_id, account in accounts.items():
        try:
            bid = int(book_id)
        except (TypeError, ValueError):
            continue
        if account is None:
            continue
        orders = _g(account, "orders") or []
        try:
            items = list(orders)
        except TypeError:
            continue
        for order in items:
            try:
                oid = int(_g(order, "id", "order_id", "i"))
            except (TypeError, ValueError):
                oid = None
            side = _g(order, "side", "direction", "s")
            try:
                # Prefer explicit direction string if present
                direction = _g(order, "direction")
                if direction is None:
                    direction = _side_name(side)
                else:
                    direction = _side_name(direction)
            except Exception:
                direction = _side_name(side)
            try:
                price = float(_g(order, "price", "p"))
            except (TypeError, ValueError):
                price = None
            try:
                qty = float(_g(order, "quantity", "qty", "q", "volume"))
            except (TypeError, ValueError):
                qty = None
            out.append(
                {
                    "book_id": bid,
                    "id": oid,
                    "direction": direction,
                    "price": price,
                    "quantity": qty,
                    "status": "confirmed",
                }
            )
    return out


def _fp_new(order: dict[str, Any]) -> tuple:
    """Fingerprint for a new limit/market (no id yet)."""
    try:
        bid = int(order.get("book_id"))
    except (TypeError, ValueError):
        bid = -1
    direction = _side_name(order.get("direction"))
    otype = str(order.get("type") or "limit").lower()
    try:
        price = round(float(order.get("price") or 0.0), 6)
    except (TypeError, ValueError):
        price = 0.0
    try:
        qty = round(float(order.get("quantity") or order.get("qty") or 0.0), 6)
    except (TypeError, ValueError):
        qty = 0.0
    return (bid, otype, direction, price, qty)


def _fp_confirmed(order: dict[str, Any]) -> tuple:
    return (
        int(order.get("book_id", -1)),
        "limit",
        _side_name(order.get("direction")),
        round(float(order.get("price") or 0.0), 6),
        round(float(order.get("quantity") or 0.0), 6),
    )


def _normalize_tif_name(tif: Any) -> str:
    """Normalize TimeInForce to IOC/FOK/GTT/... (handles enum repr and ints)."""
    if tif is None:
        return ""
    if isinstance(tif, int):
        return {2: "IOC", 3: "FOK"}.get(tif, str(tif))
    name = getattr(tif, "name", None)
    if name is not None:
        return str(name).upper()
    s = str(tif).strip()
    if not s:
        return ""
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    return s.upper()


def _is_ioc_or_fok(tif: Any) -> bool:
    return _normalize_tif_name(tif) in ("IOC", "FOK")


def _normalize_sent(raw: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not raw:
        return out
    try:
        items = list(raw)
    except TypeError:
        return out
    for o in items:
        if not isinstance(o, dict):
            continue
        row = dict(o)
        try:
            row["book_id"] = int(row.get("book_id"))
        except (TypeError, ValueError):
            continue
        row["type"] = str(row.get("type") or "limit").lower()
        if row["type"] == "cancel":
            try:
                row["order_id"] = int(row.get("order_id") or row.get("id"))
            except (TypeError, ValueError):
                continue
        else:
            row["direction"] = _side_name(row.get("direction"))
            try:
                row["price"] = float(row.get("price"))
            except (TypeError, ValueError):
                row["price"] = None
            try:
                row["quantity"] = float(row.get("quantity") or row.get("qty") or 0.0)
            except (TypeError, ValueError):
                row["quantity"] = None
        out.append(row)
    return out


def sent_orders_from_response(response: Any) -> list[dict[str, Any]]:
    """Extract limit/market/cancel orders from a FinanceAgentResponse."""
    out: list[dict[str, Any]] = []
    for instr in getattr(response, "instructions", None) or []:
        typ = str(getattr(instr, "type", "") or "")
        if typ == "PLACE_ORDER_LIMIT":
            try:
                out.append(
                    {
                        "type": "limit",
                        "book_id": int(instr.bookId),
                        "direction": instr.direction,
                        "price": float(instr.price),
                        "quantity": float(instr.quantity),
                        "postOnly": bool(getattr(instr, "postOnly", False)),
                        "timeInForce": _normalize_tif_name(
                            getattr(instr, "timeInForce", "")
                        ),
                    }
                )
            except (TypeError, ValueError, AttributeError):
                continue
        elif typ == "PLACE_ORDER_MARKET":
            try:
                out.append(
                    {
                        "type": "market",
                        "book_id": int(instr.bookId),
                        "direction": instr.direction,
                        "quantity": float(instr.quantity),
                    }
                )
            except (TypeError, ValueError, AttributeError):
                continue
        elif typ == "CANCEL_ORDERS":
            try:
                bid = int(instr.bookId)
            except (TypeError, ValueError, AttributeError):
                continue
            for c in getattr(instr, "cancellations", None) or []:
                try:
                    out.append(
                        {
                            "type": "cancel",
                            "book_id": bid,
                            "order_id": int(c.orderId),
                        }
                    )
                except (TypeError, ValueError, AttributeError):
                    continue
    return out


def _reconcile_pending(
    pending: list[dict[str, Any]],
    confirmed: list[dict[str, Any]],
    *,
    now: float,
    max_age_sec: float,
) -> list[dict[str, Any]]:
    """Drop pending that accepted / cancelled / expired."""
    conf_ids: set[tuple[int, int]] = set()
    conf_fps: list[tuple] = []
    for c in confirmed:
        bid = int(c.get("book_id", -1))
        oid = c.get("id")
        if oid is not None:
            try:
                conf_ids.add((bid, int(oid)))
            except (TypeError, ValueError):
                pass
        conf_fps.append(_fp_confirmed(c))

    # Multiset consume of fingerprints for new-order acceptance.
    remaining_fps = list(conf_fps)
    kept: list[dict[str, Any]] = []
    for p in pending:
        wall = float(p.get("wall_ts") or 0.0)
        if max_age_sec > 0 and wall > 0 and (now - wall) > float(max_age_sec):
            continue
        kind = str(p.get("kind") or p.get("type") or "").lower()
        otype = str(p.get("type") or "").lower()
        # Drop stale market/IOC/FOK rows (never become resting limits).
        if otype == "market" or _is_ioc_or_fok(p.get("timeInForce")):
            continue
        bid = int(p.get("book_id", -1))
        if kind == "cancel":
            try:
                oid = int(p.get("order_id"))
            except (TypeError, ValueError):
                continue
            if (
                CANCEL_PENDING_MAX_AGE_SEC > 0
                and wall > 0
                and (now - wall) > float(CANCEL_PENDING_MAX_AGE_SEC)
            ):
                continue
            # Cancel accepted when order no longer rests on the book.
            if (bid, oid) not in conf_ids:
                continue
            kept.append(p)
            continue
        # New limit: accepted when a matching confirmed fingerprint appears.
        fp = _fp_new(p)
        try:
            idx = remaining_fps.index(fp)
        except ValueError:
            kept.append(p)
            continue
        remaining_fps.pop(idx)
        # Matched → accepted, drop from waiting.
    return kept


def _drop_pending_new_on_side(
    pending: list[dict[str, Any]], book_id: int, direction: Any
) -> list[dict[str, Any]]:
    """Keep at most one unaccepted limit per (book, side)."""
    want = _side_name(direction)
    kept: list[dict[str, Any]] = []
    for p in pending:
        kind = str(p.get("kind") or p.get("type") or "").lower()
        if kind != "cancel" and int(p.get("book_id", -1)) == int(book_id):
            if _side_name(p.get("direction")) == want:
                continue
        kept.append(p)
    return kept


def _pending_count_for_book(pending: list[dict[str, Any]], book_id: int) -> int:
    """Count in-flight new limits and cancels on one book."""
    bid = int(book_id)
    n = 0
    for p in pending:
        if int(p.get("book_id", -1)) != bid:
            continue
        status = str(p.get("status") or "")
        if status in ("waiting_accept", "waiting_cancel"):
            n += 1
    return n


def _pending_accept_count_for_book(pending: list[dict[str, Any]], book_id: int) -> int:
    """Count unaccepted new limits only (excludes cancel backlog)."""
    bid = int(book_id)
    n = 0
    for p in pending:
        if int(p.get("book_id", -1)) != bid:
            continue
        if str(p.get("status") or "") == "waiting_accept":
            n += 1
    return n


def _drop_pending_cancel(pending: list[dict[str, Any]], book_id: int, order_id: int) -> list[dict[str, Any]]:
    """Remove prior in-flight cancels for one order id."""
    bid = int(book_id)
    oid = int(order_id)
    kept: list[dict[str, Any]] = []
    for p in pending:
        kind = str(p.get("kind") or p.get("type") or "").lower()
        if kind == "cancel" and int(p.get("book_id", -1)) == bid:
            try:
                if int(p.get("order_id")) == oid:
                    continue
            except (TypeError, ValueError):
                pass
        kept.append(p)
    return kept


def _recharge_times_for_agent(agent: Any) -> int | None:
    """None when this agent does not track recharge (e.g. TopAgent)."""
    if not hasattr(agent, "recharge_times"):
        return None
    raw = getattr(agent, "recharge_times", DEFAULT_RECHARGE_TIMES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RECHARGE_TIMES


def _parse_recharge_used(raw: Any) -> dict[int, int]:
    out: dict[int, int] = {}
    if raw is None:
        return out
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = max(0, int(v))
            except (TypeError, ValueError):
                continue
        return out
    try:
        out[0] = max(0, int(raw))
    except (TypeError, ValueError):
        pass
    return out


def hydrate_recharge_from_payload(data: dict[str, Any], uid: int) -> None:
    if not isinstance(data, dict):
        return
    if "recharge_times" not in data and "recharge_used" not in data:
        return
    try:
        times = max(0, int(data.get("recharge_times", DEFAULT_RECHARGE_TIMES)))
    except (TypeError, ValueError):
        times = DEFAULT_RECHARGE_TIMES
    used = _parse_recharge_used(data.get("recharge_used"))
    with _lock:
        _recharge_state_by_uid[uid] = {"times": times, "used": used}


def hydrate_recharge_from_disk(agent: Any) -> None:
    """Load recharge_used from the newest waiting_orders dump for this uid."""
    uid = _uid_from_agent(agent)
    if uid is None or _recharge_times_for_agent(agent) is None:
        return
    best_mtime = -1.0
    best: dict[str, Any] | None = None
    for path in _write_paths(agent, uid):
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if mtime >= best_mtime and isinstance(data, dict):
            best = data
            best_mtime = mtime
    if best is not None:
        hydrate_recharge_from_payload(best, uid)


def recharge_allow(agent: Any, book_id: int) -> bool:
    """True when another market recharge is allowed on this book."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return True
    times = _recharge_times_for_agent(agent)
    if times is None:
        return True
    bid = int(book_id)
    with _lock:
        st = _recharge_state_by_uid.setdefault(
            uid, {"times": times, "used": {}}
        )
        st["times"] = times
        used = int((st.get("used") or {}).get(bid, 0))
    return used < times


def note_recharge(agent: Any, book_id: int) -> None:
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    times = _recharge_times_for_agent(agent)
    if times is None:
        return
    bid = int(book_id)
    with _lock:
        st = _recharge_state_by_uid.setdefault(
            uid, {"times": times, "used": {}}
        )
        st["times"] = times
        used_map = st.setdefault("used", {})
        used_map[bid] = int(used_map.get(bid, 0)) + 1


def clear_recharge_books(agent: Any, book_ids: set[int] | list[int]) -> None:
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    with _lock:
        st = _recharge_state_by_uid.get(uid)
        if not st:
            return
        used_map = st.get("used") or {}
        for bid in book_ids:
            used_map.pop(int(bid), None)


def recharge_payload_fields(agent: Any, uid: int) -> dict[str, Any]:
    times = _recharge_times_for_agent(agent)
    if times is None:
        return {}
    with _lock:
        st = _recharge_state_by_uid.setdefault(
            uid, {"times": times, "used": {}}
        )
        st["times"] = times
        used = {str(k): int(v) for k, v in (st.get("used") or {}).items()}
    return {"recharge_times": times, "recharge_used": used}


def _max_pending_per_book(agent: Any) -> int:
    raw = getattr(agent, "max_pending_per_book", DEFAULT_MAX_PENDING_PER_BOOK)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PENDING_PER_BOOK


def _pending_new_fps(pending: list[dict[str, Any]]) -> set[tuple]:
    fps: set[tuple] = set()
    for p in pending:
        kind = str(p.get("kind") or p.get("type") or "").lower()
        if kind == "cancel":
            continue
        fps.add(_fp_new(p))
    return fps


def _add_sent_to_pending(
    pending: list[dict[str, Any]],
    sent: list[dict[str, Any]],
    *,
    now: float,
    sim_ts: int | None,
    max_pending_per_book: int = 0,
) -> list[dict[str, Any]]:
    out = list(pending)
    existing_new = _pending_new_fps(out)
    cap = max(0, int(max_pending_per_book))
    for o in sent:
        otype = str(o.get("type") or "limit").lower()
        book_id = int(o["book_id"])
        if otype == "cancel":
            out = _drop_pending_cancel(out, book_id, int(o["order_id"]))
            row = {
                "kind": "cancel",
                "type": "cancel",
                "book_id": book_id,
                "order_id": int(o["order_id"]),
                "wall_ts": now,
                "sim_ts": sim_ts,
                "status": "waiting_cancel",
            }
            out.append(row)
            continue
        if cap > 0 and _pending_accept_count_for_book(out, book_id) >= cap:
            continue
        elif otype == "market":
            # Market never rests in account.orders. Still recorded in payload["sent"].
            continue
        else:
            tif = _normalize_tif_name(o.get("timeInForce") or o.get("tif"))
            if _is_ioc_or_fok(tif):
                # FOK/IOC fill or kill immediately — never waiting_accept backlog.
                continue
            out = _drop_pending_new_on_side(out, book_id, o.get("direction"))
            existing_new = _pending_new_fps(out)
            if cap > 0 and _pending_accept_count_for_book(out, book_id) >= cap:
                continue
            row = {
                "kind": "new",
                "type": otype,
                "book_id": book_id,
                "direction": _side_name(o.get("direction")),
                "price": o.get("price"),
                "quantity": o.get("quantity"),
                "postOnly": o.get("postOnly"),
                "timeInForce": tif,
                "wall_ts": now,
                "sim_ts": sim_ts,
                "status": "waiting_accept",
            }
            if _fp_new(row) in existing_new:
                continue
            existing_new.add(_fp_new(row))
            out.append(row)
    return out


def reconcile_pending_before_tick(
    agent: Any, *, max_age_sec: float = DEFAULT_MAX_AGE_SEC
) -> None:
    """Drop accepted/expired pending rows before TopAgent builds a new response."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    now = time.time()
    confirmed = _confirmed_orders(agent)
    with _lock:
        prior = list(_pending_by_uid.get(uid) or [])
        _pending_by_uid[uid] = _reconcile_pending(
            prior, confirmed, now=now, max_age_sec=max_age_sec
        )


def pending_count_for_book(agent: Any, book_id: int) -> int:
    """In-flight new limits + cancels on one book (for validator cap alignment)."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return 0
    with _lock:
        pending = list(_pending_by_uid.get(uid) or [])
    return _pending_count_for_book(pending, int(book_id))


def has_pending_book_at_limit(agent: Any, book_id: int, *, limit: int) -> bool:
    cap = max(0, int(limit))
    if cap <= 0:
        return False
    uid = _uid_from_agent(agent)
    if uid is None:
        return False
    with _lock:
        pending = list(_pending_by_uid.get(uid) or [])
    return _pending_accept_count_for_book(pending, int(book_id)) >= cap


def has_pending_new_limit(agent: Any, book_id: int, side: int) -> bool:
    """True when a GTT/GTC limit was sent but not yet resting on the book."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return False
    want = "BUY" if int(side) == 0 else "SELL"
    with _lock:
        pending = list(_pending_by_uid.get(uid) or [])
    for p in pending:
        kind = str(p.get("kind") or p.get("type") or "").lower()
        if kind == "cancel":
            continue
        if int(p.get("book_id", -1)) != int(book_id):
            continue
        if _side_name(p.get("direction")) != want:
            continue
        return True
    return False


def clear_all_pending(agent: Any) -> None:
    """Drop all in-memory pending rows for this uid (e.g. cancel-all mode)."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    with _lock:
        _pending_by_uid[uid] = []


def clear_pending_new_limits(agent: Any, book_id: int, side: int) -> None:
    """Clear unaccepted limits on one side (e.g. before cancel/replace)."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    want = "BUY" if int(side) == 0 else "SELL"
    with _lock:
        pending = list(_pending_by_uid.get(uid) or [])
        kept: list[dict[str, Any]] = []
        for p in pending:
            kind = str(p.get("kind") or p.get("type") or "").lower()
            if kind != "cancel" and int(p.get("book_id", -1)) == int(book_id):
                if _side_name(p.get("direction")) == want:
                    continue
            kept.append(p)
        _pending_by_uid[uid] = kept


def mark_pending_limit(
    agent: Any,
    book_id: int,
    side: int,
    *,
    price: float,
    quantity: float,
    sim_ts: int | None = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    time_in_force: str = "GTT",
) -> None:
    """Record one in-flight GTT/GTC limit immediately after TopAgent sends it."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    tif = _normalize_tif_name(time_in_force)
    if _is_ioc_or_fok(tif):
        return
    now = time.time()
    direction = "BUY" if int(side) == 0 else "SELL"
    row = {
        "kind": "new",
        "type": "limit",
        "book_id": int(book_id),
        "direction": direction,
        "price": price,
        "quantity": quantity,
        "timeInForce": tif,
        "wall_ts": now,
        "sim_ts": sim_ts,
        "status": "waiting_accept",
    }
    confirmed = _confirmed_orders(agent)
    cap = _max_pending_per_book(agent)
    with _lock:
        prior = list(_pending_by_uid.get(uid) or [])
        pending = _reconcile_pending(
            prior, confirmed, now=now, max_age_sec=max_age_sec
        )
        if cap > 0 and _pending_accept_count_for_book(pending, int(book_id)) >= cap:
            return
        pending = _drop_pending_new_on_side(pending, int(book_id), direction)
        pending.append(row)
        _pending_by_uid[uid] = pending


def register_sent_limits(
    agent: Any,
    response: Any,
    *,
    sim_ts: int | None = None,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
) -> None:
    """Synchronously record this tick's sent limits (before async dump)."""
    uid = _uid_from_agent(agent)
    if uid is None:
        return
    sent = _normalize_sent(sent_orders_from_response(response))
    now = time.time()
    confirmed = _confirmed_orders(agent)
    with _lock:
        prior = list(_pending_by_uid.get(uid) or [])
        pending = _reconcile_pending(
            prior, confirmed, now=now, max_age_sec=max_age_sec
        )
        pending = _add_sent_to_pending(
            pending,
            sent,
            now=now,
            sim_ts=sim_ts,
            max_pending_per_book=_max_pending_per_book(agent),
        )
        _pending_by_uid[uid] = pending


def build_payload(
    agent: Any,
    state: Any,
    sent_orders: list[dict[str, Any]] | None,
    *,
    uid: int,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
) -> dict[str, Any]:
    now = time.time()
    confirmed = _confirmed_orders(agent)
    sent = _normalize_sent(sent_orders)

    ts = _g(state, "timestamp", "t")
    try:
        sim_ts = int(ts) if ts is not None else None
    except (TypeError, ValueError):
        sim_ts = None
    sim_id = None
    try:
        cfg = _g(state, "config")
        sim_id = _g(cfg, "simulation_id", "simulationId")
    except Exception:
        sim_id = None

    with _lock:
        prior = list(_pending_by_uid.get(uid) or [])
        pending = _reconcile_pending(
            prior, confirmed, now=now, max_age_sec=max_age_sec
        )
        pending = _add_sent_to_pending(
            pending,
            sent,
            now=now,
            sim_ts=sim_ts,
            max_pending_per_book=_max_pending_per_book(agent),
        )
        _pending_by_uid[uid] = pending

    books: dict[str, Any] = {}
    for c in confirmed:
        bid = str(int(c["book_id"]))
        slot = books.setdefault(
            bid,
            {
                "book_id": int(c["book_id"]),
                "open_orders": [],
                "waiting": [],
                "open_count": 0,
                "waiting_count": 0,
            },
        )
        slot["open_orders"].append(c)
        slot["open_count"] = len(slot["open_orders"])

    for w in pending:
        bid = str(int(w["book_id"]))
        slot = books.setdefault(
            bid,
            {
                "book_id": int(w["book_id"]),
                "open_orders": [],
                "waiting": [],
                "open_count": 0,
                "waiting_count": 0,
            },
        )
        slot["waiting"].append(w)
        slot["waiting_count"] = len(slot["waiting"])

    n_open = sum(int(b.get("open_count") or 0) for b in books.values())
    n_wait = len(pending)
    n_wait_new = sum(1 for w in pending if str(w.get("kind")) == "new")
    n_wait_cancel = sum(1 for w in pending if str(w.get("kind")) == "cancel")

    payload: dict[str, Any] = {
        "available": True,
        "source": "axon_engine",
        "uid": uid,
        "sim_id": sim_id,
        "sim_ts": sim_ts,
        "wall_ts": now,
        "totals": {
            "open": n_open,
            "waiting": n_wait,
            "waiting_new": n_wait_new,
            "waiting_cancel": n_wait_cancel,
            "sent_this_tick": len(sent),
        },
        "sent": sent,
        "waiting": pending,
        "confirmed": confirmed,
        "books": books,
        "book_count": len(books),
    }
    payload.update(recharge_payload_fields(agent, uid))
    return payload


def record_waiting_orders(
    agent: Any,
    state: Any,
    sent_orders: list[dict[str, Any]] | None = None,
    *,
    max_age_sec: float = DEFAULT_MAX_AGE_SEC,
    min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
) -> dict[str, Any] | None:
    """Update pending + write dump. Returns payload when written."""
    global _logged_start
    uid = _uid_from_agent(agent)
    if uid is None:
        return None

    now = time.time()
    with _lock:
        last = _last_write_wall.get(uid, 0.0)
        if now - last < float(min_interval_sec):
            # Still reconcile/add so pending stays accurate; skip disk write.
            skip_write = True
        else:
            skip_write = False
            _last_write_wall[uid] = now

    payload = build_payload(
        agent, state, sent_orders, uid=uid, max_age_sec=max_age_sec
    )

    if not skip_write:
        for path in _write_paths(agent, uid):
            try:
                _atomic_write(path, payload)
            except Exception:
                continue

    if not _logged_start:
        _logged_start = True
        try:
            t = payload.get("totals") or {}
            print(
                f"WAITING_ORDERS_RECORDER uid={uid} "
                f"open={t.get('open')} waiting={t.get('waiting')} "
                f"sent={t.get('sent_this_tick')}"
            )
        except Exception:
            pass
    return payload
