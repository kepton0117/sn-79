# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Persist validator-style FIFO state for BookManageBot local qty / κ columns.

Writes (atomic replace)::

    <output_dir>/fifo_state_<validator>_<sim_id>.json

BookManageBot resolves the same path via ``{data_dir}/{uid}/fifo_state_*.json``.
"""

from __future__ import annotations

import csv
import logging
import os
from pathlib import Path
from typing import Any

from fifo_pnl import ValidatorStylePnl

try:
    from taos.im.utils import timestamp_from_duration
except ImportError:
    timestamp_from_duration = None  # type: ignore

log = logging.getLogger(__name__)


def _sim_id(state: Any) -> str:
    cfg = getattr(state, "config", None)
    sid = getattr(cfg, "simulation_id", None) if cfg is not None else None
    return str(sid or "unknown")


def _validator_hotkey(state: Any, fallback: str | None = None) -> str:
    dend = getattr(state, "dendrite", None)
    hk = getattr(dend, "hotkey", None) if dend is not None else None
    if hk:
        return str(hk)
    return str(fallback or "unknown")


def fifo_state_path(output_dir: Path | str, validator: str, sim_id: str) -> Path:
    """Path BookManageBot expects: ``{uid_dir}/fifo_state_{validator}_{sim}.json``."""
    val = str(validator).replace(":", "_").replace("/", "_")[:120]
    sim = str(sim_id).replace(":", "_").replace("/", "_")[:80]
    return Path(output_dir) / f"fifo_state_{val}_{sim}.json"


def _book_count(agent: Any, state: Any) -> int:
    sim_cfg = getattr(agent, "simulation_config", None)
    if sim_cfg is not None:
        bc = getattr(sim_cfg, "book_count", None)
        if bc is not None:
            try:
                return int(bc)
            except (TypeError, ValueError):
                pass
    cfg = getattr(state, "config", None)
    bc = getattr(cfg, "book_count", None) if cfg is not None else None
    if bc is not None:
        try:
            return int(bc)
        except (TypeError, ValueError):
            pass
    return 128


def _trades_csv_path(agent: Any, state: Any) -> Path | None:
    try:
        sim_dir = Path(agent.simulation_output_dir(state))
    except Exception:
        return None
    path = sim_dir / "trades.csv"
    return path if path.is_file() else None


def replay_fifo_from_trades_csv(
    fifo: ValidatorStylePnl,
    path: Path,
    uid: int,
    *,
    end_ts: int | None = None,
) -> int:
    """Backfill FIFO from session ``trades.csv`` when no persisted state exists."""
    if not path.is_file():
        return 0
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                ts_raw = (row.get("timestamp") or "").strip()
                if not ts_raw:
                    continue
                try:
                    if timestamp_from_duration is not None:
                        ts = int(timestamp_from_duration(ts_raw))
                    else:
                        ts = int(float(ts_raw))
                except (TypeError, ValueError):
                    continue
                rows.append((ts, row))
    except OSError as exc:
        log.warning("fifo replay read failed %s: %s", path, exc)
        return 0

    if not rows:
        return 0
    rows.sort(key=lambda item: (item[0], int(float(item[1].get("tradeId") or 0))))

    processed = 0
    for ts, row in rows:
        try:
            book_id = int(float(row["bookId"]))
            trade_id = int(float(row["tradeId"]))
            taker_id = int(float(row["takerAgentId"]))
            maker_id = int(float(row["makerAgentId"]))
            side = int(float(row["side"]))
            price = float(row["price"])
            qty = float(row["quantity"])
            taker_fee = float(row.get("takerFee") or 0)
            maker_fee = float(row.get("makerFee") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if fifo.record_trade(
            trade_id,
            book_id,
            ts,
            taker_id,
            maker_id,
            side,
            price,
            qty,
            taker_fee,
            maker_fee,
            int(uid),
        ):
            processed += 1
    if end_ts and end_ts > 0:
        try:
            fifo.prune(int(end_ts))
        except Exception:
            pass
    return processed


def validator_net_qty(agent: Any, book_id: int) -> float | None:
    """Cash-book open BASE: (bb.t − loan + collateral) − int_base."""
    accounts = getattr(agent, "accounts", None)
    if not isinstance(accounts, dict):
        return None
    acct = accounts.get(book_id)
    if acct is None:
        acct = accounts.get(str(book_id))
    if acct is None:
        return None
    try:
        bb = acct.base_balance
        net = (
            float(bb.total)
            - float(getattr(acct, "base_loan", 0) or 0)
            + float(getattr(acct, "base_collateral", 0) or 0)
        )
        initial = getattr(bb, "initial", None)
        if initial is not None and float(initial) > 0.0:
            net -= float(initial)
        return net
    except (AttributeError, TypeError, ValueError):
        return None


def _level_price(level: Any) -> float | None:
    if level is None:
        return None
    if isinstance(level, dict):
        raw = level.get("p", level.get("price"))
    else:
        raw = getattr(level, "price", None) or getattr(level, "p", None)
    try:
        px = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return px if px and px > 0.0 else None


def _bootstrap_entry_price(
    agent: Any, book_id: int, state: Any, *, is_long: bool
) -> float:
    open_books = getattr(agent, "open_books", None) or {}
    spec = open_books.get(book_id)
    if spec is not None:
        try:
            px = float(getattr(spec, "open_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0.0:
            return px

    book = (getattr(state, "books", None) or {}).get(book_id)
    if book is None:
        return 0.0
    levels = getattr(book, "asks" if is_long else "bids", None) or []
    if not levels:
        return 0.0
    px = _level_price(levels[0])
    return float(px) if px is not None else 0.0


def maybe_bootstrap_fifo_from_accounts(
    agent: Any, fifo: ValidatorStylePnl, state: Any
) -> int:
    """Seed FIFO lots from validator wallet inventory when tape qty diverges.

    Covers missed passive maker fills (SETTLEMENT NOT READY) so local fifo_net
    matches validator inventory for close logic and BookManageBot local qty.
    """
    if not bool(int(getattr(agent, "fifo_bootstrap", 1))):
        return 0

    sim_cfg = getattr(agent, "simulation_config", None)
    min_qty = float(getattr(sim_cfg, "min_order_size", 0.25) or 0.25)
    ts = int(getattr(state, "timestamp", 0) or 0)
    if ts <= 0:
        ts = int(getattr(agent, "_last_state_ts", 0) or 0)
    if ts <= 0:
        return 0

    book_ids: set[int] = set()
    for bid in getattr(state, "books", None) or {}:
        try:
            book_ids.add(int(bid))
        except (TypeError, ValueError):
            continue
    accounts = getattr(agent, "accounts", None)
    if isinstance(accounts, dict):
        for bid in accounts.keys():
            try:
                book_ids.add(int(bid))
            except (TypeError, ValueError):
                continue

    boot_count = 0
    for book_id in sorted(book_ids):
        book = (getattr(state, "books", None) or {}).get(book_id)
        if book is not None:
            bids = getattr(book, "bids", None)
            asks = getattr(book, "asks", None)
            if not bids or not asks:
                continue

        validator_net = validator_net_qty(agent, book_id)
        if validator_net is None:
            continue

        fifo_net = float(fifo.net_qty(book_id))
        gap = validator_net - fifo_net
        if abs(gap) < min_qty * 0.51:
            continue

        boot_long, boot_short = fifo.fifo_bootstrapped(book_id)

        if gap > 0.0:
            entry = _bootstrap_entry_price(agent, book_id, state, is_long=True)
            if entry <= 0.0:
                continue
            if boot_long and fifo.resync_bootstrap_long(book_id, ts, validator_net, entry):
                log.info(
                    "fifo bootstrap resync book=%d long %.4f@%.4f (validator=%+.4f fifo=%+.4f)",
                    book_id,
                    validator_net,
                    entry,
                    validator_net,
                    fifo_net,
                )
                boot_count += 1
            elif fifo.fifo_long_head_price(book_id) <= 0.0 and fifo.fifo_short_head_price(
                book_id
            ) <= 0.0:
                if fifo.seed_long(book_id, ts, validator_net, entry):
                    log.info(
                        "fifo bootstrap book=%d long %.4f@%.4f (validator=%+.4f fifo=%+.4f)",
                        book_id,
                        validator_net,
                        entry,
                        validator_net,
                        fifo_net,
                    )
                    boot_count += 1
        elif gap < 0.0:
            entry = _bootstrap_entry_price(agent, book_id, state, is_long=False)
            if entry <= 0.0:
                continue
            short_qty = abs(validator_net)
            if boot_short and fifo.resync_bootstrap_short(book_id, ts, short_qty, entry):
                log.info(
                    "fifo bootstrap resync book=%d short %.4f@%.4f (validator=%+.4f fifo=%+.4f)",
                    book_id,
                    short_qty,
                    entry,
                    validator_net,
                    fifo_net,
                )
                boot_count += 1
            elif fifo.fifo_short_head_price(book_id) <= 0.0 and fifo.fifo_long_head_price(
                book_id
            ) <= 0.0:
                if fifo.seed_short(book_id, ts, short_qty, entry):
                    log.info(
                        "fifo bootstrap book=%d short %.4f@%.4f (validator=%+.4f fifo=%+.4f)",
                        book_id,
                        short_qty,
                        entry,
                        validator_net,
                        fifo_net,
                    )
                    boot_count += 1

    return boot_count


def ensure_fifo(agent: Any, state: Any, *, validator: str | None = None) -> ValidatorStylePnl:
    """Load or create in-memory FIFO for the current validator/sim session."""
    val = validator or _validator_hotkey(state)
    sim = _sim_id(state)
    key = f"{val}:{sim}"
    cache = getattr(agent, "_fifos", None)
    if not isinstance(cache, dict):
        cache = {}
        agent._fifos = cache

    if key in cache:
        agent._fifo = cache[key]
        agent._fifo_sim_key = key
        return cache[key]

    book_count = _book_count(agent, state)
    out_dir = Path(getattr(agent, "output_dir", ".") or ".")
    path = fifo_state_path(out_dir, val, sim)
    haircut = bool(int(getattr(agent, "fifo_buy_base_fee_haircut", 1) or 0))
    if path.is_file():
        fifo = ValidatorStylePnl.load(path, book_count)
        fifo.buy_base_fee_haircut = haircut
    else:
        fifo = ValidatorStylePnl(book_count, buy_base_fee_haircut=haircut)

    if len(fifo._seen_trade_keys) == 0 or not getattr(
        agent, f"_fifo_replay_done_{key}", False
    ):
        trades_path = _trades_csv_path(agent, state)
        if trades_path is not None:
            end_ts = int(getattr(state, "timestamp", 0) or 0)
            n = replay_fifo_from_trades_csv(
                fifo, trades_path, int(getattr(agent, "uid", -1)), end_ts=end_ts
            )
            setattr(agent, f"_fifo_replay_done_{key}", True)
            if n > 0:
                log.info("fifo replay %s trades=%d path=%s", key, n, trades_path)

    cache[key] = fifo
    agent._fifo = fifo
    agent._fifo_sim_key = key
    agent._fifo_state_path = path
    return fifo


def record_trade_event(
    agent: Any,
    event: Any,
    *,
    state: Any | None = None,
    validator: str | None = None,
    state_timestamp: int | None = None,
) -> bool:
    """Feed one EVENT_TRADE into FIFO (idempotent by book+trade id)."""
    if event is None or getattr(event, "bookId", None) is None:
        return False
    ctx = state or getattr(agent, "_fifo_ctx_state", None)
    if ctx is None:
        return False

    val = validator or _validator_hotkey(ctx)
    fifo = ensure_fifo(agent, ctx, validator=val)
    ts = state_timestamp
    if ts is None or ts <= 0:
        ts = int(getattr(event, "timestamp", 0) or 0)
    if ts <= 0:
        ts = int(getattr(agent, "_last_state_ts", 0) or 0)
    if ts <= 0:
        return False

    uid = int(getattr(agent, "uid", -1))
    try:
        return bool(
            fifo.record_trade(
                int(event.tradeId),
                int(event.bookId),
                int(ts),
                int(event.takerAgentId),
                int(event.makerAgentId),
                int(event.side),
                float(event.price),
                float(event.quantity),
                float(getattr(event, "takerFee", 0) or 0),
                float(getattr(event, "makerFee", 0) or 0),
                uid,
            )
        )
    except (TypeError, ValueError):
        return False


def persist_fifo_state(
    agent: Any,
    *,
    state: Any | None = None,
    force: bool = False,
) -> None:
    """Save FIFO snapshot to ``fifo_state_*.json`` under ``output_dir``."""
    if not bool(getattr(agent, "fifo_persist", True)):
        return
    fifo = getattr(agent, "_fifo", None)
    if fifo is None:
        return
    ctx = state or getattr(agent, "_fifo_ctx_state", None)
    path = getattr(agent, "_fifo_state_path", None)
    if path is None and ctx is not None:
        val = _validator_hotkey(ctx)
        sim = _sim_id(ctx)
        path = fifo_state_path(getattr(agent, "output_dir", "."), val, sim)
        agent._fifo_state_path = path

    interval = max(1, int(getattr(agent, "fifo_persist_interval_ticks", 1) or 1))
    tick = int(getattr(agent, "_fifo_persist_tick", 0) or 0) + 1
    agent._fifo_persist_tick = tick
    if not force and tick % interval != 0:
        return

    if ctx is not None:
        end_ts = int(getattr(ctx, "timestamp", 0) or 0)
        if end_ts > 0:
            try:
                fifo.prune(end_ts)
            except Exception:
                pass

    if path is None:
        return
    try:
        fifo.save(Path(path))
    except OSError as exc:
        log.warning("fifo save failed %s: %s", path, exc)
