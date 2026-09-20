# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Rest-window L1 book scoring → top-N ``phase1_edge_books`` lock.

During ``micro_rest_minutes``, accumulate strong bullish/bearish L1 scores
(trend + pressure from best bid/ask + depth). When rest ends, keep the top
``rest_l1_select_count`` books in phase1 and move all other universe books
into ``stop_edge_books``, then persist both to the miner live JSON.

Designed to be called every engine tick (safe to ``importlib.reload``).
"""

from __future__ import annotations

from typing import Any


def _f(obj: Any, name: str, default: float = 0.0) -> float:
    try:
        return float(getattr(obj, name, default) or default)
    except (TypeError, ValueError):
        return float(default)


def _i(obj: Any, name: str, default: int = 0) -> int:
    try:
        return int(getattr(obj, name, default) or 0)
    except (TypeError, ValueError):
        return int(default)


def _sim_key(agent: Any, state: Any) -> str:
    try:
        return str(agent._sim_key_from_state(state) or "")
    except Exception:
        hk = getattr(getattr(state, "dendrite", None), "hotkey", "") or ""
        sid = ""
        try:
            sid = str(agent._simulation_id(state) or "")
        except Exception:
            sid = ""
        return f"{hk}:{sid}"


def _ensure_state(agent: Any) -> None:
    if not isinstance(getattr(agent, "_rest_l1_acc", None), dict):
        agent._rest_l1_acc = {}
    if not isinstance(getattr(agent, "_rest_l1_locked", None), dict):
        agent._rest_l1_locked = {}
    if not isinstance(getattr(agent, "_rest_l1_meta", None), dict):
        agent._rest_l1_meta = {}


def _thresholds(agent: Any) -> tuple[float, float, float, bool, int, int]:
    trend_th = _f(agent, "rest_l1_trend_threshold", 0.0)
    if trend_th <= 0.0:
        trend_th = _f(agent, "micro_trend_flat_threshold", 1.5)
    press_th = _f(agent, "rest_l1_pressure_threshold", 0.0)
    if press_th <= 0.0:
        press_th = _f(
            agent,
            "micro_pressure_threshold",
            _f(agent, "pressure_imbalance_threshold", 0.2),
        )
    weight = max(0.0, _f(agent, "rest_l1_pressure_weight", 1.0))
    require = bool(_i(agent, "rest_l1_require_pressure", 1))
    min_strong = max(1, _i(agent, "rest_l1_min_strong_ticks", 3))
    count = max(1, _i(agent, "rest_l1_select_count", 80))
    return trend_th, press_th, weight, require, min_strong, count


def _score_book(
    agent: Any,
    book_id: int,
    book: Any,
    tick: float,
    trend_th: float,
    press_th: float,
    weight: float,
    require: bool,
) -> tuple[float, bool, float, float] | None:
    """Return (strength, is_strong, trend, pressure) or None if unusable."""
    try:
        if not book.bids or not book.asks:
            return None
        mid = (float(book.bids[0].price) + float(book.asks[0].price)) / 2.0
        if mid <= 0.0 or tick <= 0.0:
            return None
        pressure = float(agent._pressure_score(book) or 0.0)
        trend = agent._trend_fields(int(book_id), mid, tick, pressure)
        ts = float(trend.get("trend_score") or 0.0)
        ps = float(trend.get("pressure_score") or pressure or 0.0)
    except Exception:
        return None
    strong_bull = ts > trend_th and ps > press_th
    strong_bear = ts < -trend_th and ps < -press_th
    is_strong = bool(strong_bull or strong_bear)
    if require and not is_strong:
        return None
    strength = abs(ts) + weight * abs(ps)
    if strength <= 0.0:
        return None
    return strength, is_strong, ts, ps


def _book_universe(agent: Any, state: Any, acc: dict) -> set[int]:
    """Books eligible for phase1 vs stop split at lock time."""
    universe: set[int] = set()
    for bid in getattr(agent, "phase1_edge_books", frozenset()) or ():
        universe.add(int(bid))
    for bid in getattr(agent, "stop_edge_books", frozenset()) or ():
        universe.add(int(bid))
    for bid in acc.keys():
        universe.add(int(bid))
    books = getattr(state, "books", None) or {}
    try:
        for bid in books.keys():
            universe.add(int(bid))
    except Exception:
        pass
    if not universe:
        universe = set(range(128))
    return universe


def _lock_top_books(
    agent: Any,
    state: Any,
    sim_key: str,
    acc: dict,
    min_strong: int,
    count: int,
    *,
    ts: int = 0,
) -> list[int]:
    ranked = sorted(
        acc.items(),
        key=lambda kv: (
            float(kv[1].get("sum", 0.0)) / max(int(kv[1].get("n", 0) or 0), 1),
            int(kv[1].get("strong_n", 0) or 0),
            float(kv[1].get("sum", 0.0)),
        ),
        reverse=True,
    )
    qualified = [
        int(bid)
        for bid, slot in ranked
        if int(slot.get("strong_n", 0) or 0) >= min_strong
    ]
    picked: list[int] = []
    for bid in qualified:
        if bid not in picked:
            picked.append(bid)
        if len(picked) >= count:
            break
    # Fill with best remaining absolute strength if not enough strong books.
    if len(picked) < count:
        for bid, _slot in ranked:
            b = int(bid)
            if b not in picked:
                picked.append(b)
            if len(picked) >= count:
                break

    picked_set = {int(b) for b in picked}
    universe = _book_universe(agent, state, acc)
    # Prefer keeping exactly `count` when universe is larger; fill from
    # remaining universe ids (stable sort) if scoring under-filled.
    if len(picked) < count:
        for bid in sorted(universe):
            if bid not in picked_set:
                picked.append(bid)
                picked_set.add(bid)
            if len(picked) >= count:
                break

    others = sorted(int(b) for b in universe if int(b) not in picked_set)
    agent.phase1_edge_books = frozenset(picked)

    # Park non-selected books on stop_edge (hold + no micro until release).
    stop_added = 0
    clock = int(ts or getattr(state, "timestamp", 0) or 0)
    add_fn = getattr(agent, "_add_book_to_stop_edge", None)
    if callable(add_fn):
        for bid in others:
            try:
                add_fn(
                    bid,
                    reason="rest_l1_select",
                    ts=clock,
                    force=True,
                    persist=False,
                )
                stop_added += 1
            except Exception:
                pass
    else:
        old_stop = set(getattr(agent, "stop_edge_books", frozenset()) or ())
        agent.stop_edge_books = frozenset(old_stop | set(others))
        stop_added = len(others)

    agent._rest_l1_locked[sim_key] = True
    agent._rest_l1_meta[sim_key] = {
        "picked_n": len(picked),
        "stop_n": stop_added,
        "scored_n": len(acc),
        "qualified_n": len(qualified),
        "universe_n": len(universe),
        "top": picked[:12],
    }
    try:
        agent._persist_phase1_edge_books_to_live_json()
    except Exception:
        pass
    try:
        agent._persist_stop_edge_to_live_json()
    except Exception:
        pass
    try:
        import bittensor as bt

        bt.logging.info(
            f"REST_L1_SELECT lock sim={sim_key} phase1={len(picked)}/{count} "
            f"stop={stop_added} scored={len(acc)} qualified={len(qualified)} "
            f"top={picked[:16]}"
        )
    except Exception:
        pass
    return picked


def force_score_and_lock(agent: Any, state: Any | None = None) -> list[int] | None:
    """One-shot: score current books and lock top-N + stop remainder.

    Used by hotpatch when the axon is already past rest with an empty accumulator.
    Ignores ``rest_l1_require_pressure`` so a full universe can still be ranked.
    """
    if state is None or not getattr(state, "books", None):
        return None
    if not bool(_i(agent, "rest_l1_select_enabled", 0)):
        return None
    _ensure_state(agent)
    sim_key = _sim_key(agent, state)
    if not sim_key:
        return None
    trend_th, press_th, weight, _require, min_strong, count = _thresholds(agent)
    try:
        decimals = int(agent.simulation_config.priceDecimals)
        tick = pow(10.0, -decimals)
    except Exception:
        tick = 0.01
    acc: dict = {}
    for book_id, book in state.books.items():
        try:
            if not book.bids or not book.asks:
                continue
            mid = (float(book.bids[0].price) + float(book.asks[0].price)) / 2.0
            if mid <= 0.0 or tick <= 0.0:
                continue
            pressure = float(agent._pressure_score(book) or 0.0)
            trend = agent._trend_fields(int(book_id), mid, tick, pressure)
            ts = float(trend.get("trend_score") or 0.0)
            ps = float(trend.get("pressure_score") or pressure or 0.0)
            strength = abs(ts) + weight * abs(ps)
            if strength <= 0.0:
                strength = 1e-9
            strong_bull = ts > trend_th and ps > press_th
            strong_bear = ts < -trend_th and ps < -press_th
            acc[int(book_id)] = {
                "sum": float(strength),
                "n": 1,
                "strong_n": 1 if (strong_bull or strong_bear) else 1,
            }
        except Exception:
            continue
    if not acc:
        return None
    agent._rest_l1_acc[sim_key] = acc
    agent._rest_l1_locked[sim_key] = False
    clock = int(getattr(state, "timestamp", 0) or 0)
    return _lock_top_books(
        agent, state, sim_key, acc, min_strong=1, count=count, ts=clock
    )


def update(agent: Any, state: Any, payload: dict | None = None) -> None:
    """Accumulate L1 scores during rest; lock top-N when rest ends."""
    if not bool(_i(agent, "rest_l1_select_enabled", 0)):
        return
    rest_min = _f(agent, "micro_rest_minutes", 0.0)
    if rest_min <= 0.0:
        return
    if state is None or not getattr(state, "books", None):
        return

    _ensure_state(agent)
    sim_key = _sim_key(agent, state)
    if not sim_key:
        return

    elapsed = 0.0
    if isinstance(payload, dict):
        try:
            elapsed = float(payload.get("scoring_elapsed_min") or 0.0)
        except (TypeError, ValueError):
            elapsed = 0.0
    if elapsed <= 0.0:
        try:
            ts = int(getattr(state, "timestamp", 0) or 0)
            phase = agent._scoring_phase_params(ts, sim_key)
            elapsed = float(getattr(phase, "elapsed_min", 0.0) or 0.0)
        except Exception:
            elapsed = 0.0

    in_rest = elapsed + 1e-12 < rest_min
    locked = bool(agent._rest_l1_locked.get(sim_key))
    trend_th, press_th, weight, require, min_strong, count = _thresholds(agent)
    ts = int(getattr(state, "timestamp", 0) or 0)

    if in_rest:
        # New rest window after a prior lock (e.g. sim reuse): allow re-score.
        if locked and elapsed < 1.0:
            agent._rest_l1_locked[sim_key] = False
            agent._rest_l1_acc[sim_key] = {}
            locked = False
        if locked:
            return
        try:
            decimals = int(agent.simulation_config.priceDecimals)
            tick = pow(10.0, -decimals)
        except Exception:
            tick = 0.01
        acc = agent._rest_l1_acc.setdefault(sim_key, {})
        for book_id, book in state.books.items():
            scored = _score_book(
                agent,
                int(book_id),
                book,
                tick,
                trend_th,
                press_th,
                weight,
                require,
            )
            if scored is None:
                continue
            strength, is_strong, _ts, _ps = scored
            slot = acc.setdefault(
                int(book_id),
                {"sum": 0.0, "n": 0, "strong_n": 0},
            )
            slot["sum"] = float(slot.get("sum", 0.0)) + float(strength)
            slot["n"] = int(slot.get("n", 0)) + 1
            if is_strong:
                slot["strong_n"] = int(slot.get("strong_n", 0)) + 1
        return

    # Past rest: lock once per sim.
    if locked:
        # Hot-upgrade: prior lock left phase1 oversized or stop empty while
        # universe still has books that should be parked on stop_edge.
        try:
            count = max(1, _i(agent, "rest_l1_select_count", 80))
            phase1_n = len(getattr(agent, "phase1_edge_books", frozenset()) or ())
            stop_n = len(getattr(agent, "stop_edge_books", frozenset()) or ())
            universe_n = len(_book_universe(agent, state, agent._rest_l1_acc.get(sim_key) or {}))
            need_split = (phase1_n > count and stop_n == 0) or (
                phase1_n >= count and stop_n == 0 and universe_n > count
            )
            if need_split:
                agent._rest_l1_locked[sim_key] = False
                return force_score_and_lock(agent, state)
        except Exception:
            pass
        return
    acc = agent._rest_l1_acc.get(sim_key) or {}
    if not acc:
        # Already past rest with no accumulated scores (e.g. hotpatched late).
        return force_score_and_lock(agent, state)
    _lock_top_books(
        agent, state, sim_key, acc, min_strong, count, ts=ts
    )
