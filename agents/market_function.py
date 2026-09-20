# SPDX-License-Identifier: MIT
"""
Cold-book market round trips for activity.

Separate from the MM engine: for excess cold books, place one market BUY then
one market SELL on the next tick (two-leg round trip).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from strategy_engine import finalize_order_quantity, round_decimals


@dataclass
class MarketColdConfig:
    market_cold_pulse: int = 0
    min_observations: int = 3
    min_cold_books: int = 48
    target_trades_3h: int = 3
    market_cold_recent_min_obs: int = 1
    quantity: float = 0.25
    exchange_min_quantity: float = 0.25
    volume_decimals: int = 4
    max_taker_fee_rate: float = 0.01
    # Validator kappa gates: books with no score (None) or neutral ZERO branch (~0.5).
    kappa_cold_enabled: int = 1
    kappa_neutral_norm: float = 0.5
    kappa_neutral_tolerance: float = 0.02
    # Post-cutoff inactivity: no fills since sim_start + yet_minutes.
    market_cold_yet_pulse: int = 0
    market_cold_yet_minutes: int = 80
    market_cold_yet_min_books: int = 48
    roundtrips: int = 1


@dataclass
class MarketColdState:
    """Per-book second leg: book_id -> quantity bought awaiting market sell."""

    pending_sell: dict[int, float] = field(default_factory=dict)
    rt_left: dict[int, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.pending_sell.clear()
        self.rt_left.clear()


@dataclass(frozen=True)
class ColdBookSnapshot:
    book_id: int
    best_bid: float
    best_ask: float
    free_base: float
    free_quote: float
    obs_3h: int
    obs_10m: int
    trades_3h: int
    at_volume_cap: bool
    taker_fee_rate: float
    raw_kappa: float | None = None
    norm_kappa: float | None = None
    trades_after_cutoff: int = 0
    sim_elapsed_min: float = 0.0



def seed_cold_edge_roundtrips(
    state: MarketColdState,
    book_ids: set[int] | frozenset[int] | list[int],
    roundtrips: int,
) -> None:
    """Initialize rt_left for newly enqueued cold-edge books.

    Only seeds missing keys. Do not reset ``rt_left <= 0`` — that marks
    budget exhaustion and must stay 0 so Topstrategy can retire the book.
    """
    n = max(1, int(roundtrips))
    for bid in book_ids:
        bid_i = int(bid)
        if bid_i not in state.rt_left:
            state.rt_left[bid_i] = n


def _market_order(book_id: int, direction: str, quantity: float) -> dict:
    return {
        "type": "market",
        "book_id": book_id,
        "direction": direction,
        "quantity": quantity,
    }


def _book_tradeable(book: ColdBookSnapshot, cfg: MarketColdConfig) -> bool:
    if book.at_volume_cap:
        return False
    if book.best_bid <= 0.0 or book.best_ask <= 0.0:
        return False
    if book.taker_fee_rate > cfg.max_taker_fee_rate:
        return False
    return True


def _kappa_neutral(book: ColdBookSnapshot, cfg: MarketColdConfig) -> bool:
    """True when validator scores the book at the ZERO branch (~0.5 normalized)."""
    if book.norm_kappa is not None:
        return (
            abs(book.norm_kappa - cfg.kappa_neutral_norm)
            <= cfg.kappa_neutral_tolerance
        )
    if book.raw_kappa is not None:
        return abs(book.raw_kappa) <= cfg.kappa_neutral_tolerance
    return False


def is_cold_book(book: ColdBookSnapshot, cfg: MarketColdConfig) -> bool:
    """Cold = low FIFO obs and/or validator kappa not meaningfully scored.

    Matches validator kappa_3 gates:
    - obs_3h < target_trades_3h: fewer than min_realized_observations closes
    - raw_kappa is None: insufficient_obs or insufficient_lookback (no score)
    - norm_kappa ~ 0.5: ZERO branch (tiny/neutral realized PnL)
    """
    if not _book_tradeable(book, cfg):
        return False
    if book.obs_3h < cfg.target_trades_3h:
        return True
    if not cfg.kappa_cold_enabled:
        return False
    # No preview yet: rely on obs_3h only (see tests / early session).
    if book.raw_kappa is None and book.norm_kappa is None:
        return False
    if book.raw_kappa is None:
        return True
    if _kappa_neutral(book, cfg):
        return True
    return False


def is_cold_book_yet(book: ColdBookSnapshot, cfg: MarketColdConfig) -> bool:
    """Cold-yet = no miner fills since sim_start + market_cold_yet_minutes."""
    if book.sim_elapsed_min < float(cfg.market_cold_yet_minutes):
        return False
    if not _book_tradeable(book, cfg):
        return False
    return book.trades_after_cutoff == 0


def _cold_roundtrip_orders(
    books: list[ColdBookSnapshot],
    state: MarketColdState,
    cfg: MarketColdConfig,
    *,
    selected_ids: set[int],
    cold_count: int,
    min_reserve_books: int,
    action_tag: str,
    use_roundtrip_budget: bool = False,
) -> tuple[list[dict], list[dict]]:
    orders: list[dict] = []
    logs: list[dict] = []
    qty_base = max(cfg.quantity, cfg.exchange_min_quantity)
    qty_base = round_decimals(qty_base, cfg.volume_decimals)

    for book in books:
        book_id = book.book_id
        pending_qty = state.pending_sell.get(book_id)

        if pending_qty is not None and pending_qty > 0.0:
            sell_qty = finalize_order_quantity(
                pending_qty,
                pending_qty,
                cfg.exchange_min_quantity,
                cfg.volume_decimals,
            )
            if sell_qty > 0.0 and book.free_base >= sell_qty:
                orders.append(_market_order(book_id, "SELL", sell_qty))
                del state.pending_sell[book_id]
                cold_rt_done = 0
                rt_left_after = int(state.rt_left.get(book_id, 0) or 0)
                if use_roundtrip_budget and book_id in state.rt_left:
                    rt_left_after = max(0, rt_left_after - 1)
                    state.rt_left[book_id] = rt_left_after
                    if rt_left_after <= 0:
                        cold_rt_done = 1
                logs.append(
                    {
                        "book_id": book_id,
                        "market_cold_action": f"{action_tag}_sell",
                        "quantity": sell_qty,
                        "obs_3h": book.obs_3h,
                        "obs_10m": book.obs_10m,
                        "trades_3h": book.trades_3h,
                        "trades_after_cutoff": book.trades_after_cutoff,
                        "sim_elapsed_min": book.sim_elapsed_min,
                        "cold_rt_done": cold_rt_done,
                        "rt_left": rt_left_after,
                    }
                )
            continue

        if book_id not in selected_ids:
            continue
        if use_roundtrip_budget and int(state.rt_left.get(book_id, 0) or 0) <= 0:
            continue

        buy_qty = finalize_order_quantity(
            qty_base,
            qty_base,
            cfg.exchange_min_quantity,
            cfg.volume_decimals,
        )
        if buy_qty <= 0.0:
            continue
        cost = buy_qty * book.best_ask
        if book.free_quote < cost:
            logs.append(
                {
                    "book_id": book_id,
                    "market_cold_action": f"{action_tag}_skip_no_quote",
                    "obs_3h": book.obs_3h,
                    "obs_10m": book.obs_10m,
                    "trades_3h": book.trades_3h,
                    "trades_after_cutoff": book.trades_after_cutoff,
                    "sim_elapsed_min": book.sim_elapsed_min,
                }
            )
            continue

        orders.append(_market_order(book_id, "BUY", buy_qty))
        state.pending_sell[book_id] = buy_qty
        logs.append(
            {
                "book_id": book_id,
                "market_cold_action": f"{action_tag}_buy",
                "quantity": buy_qty,
                "obs_3h": book.obs_3h,
                "obs_10m": book.obs_10m,
                "trades_3h": book.trades_3h,
                "trades_after_cutoff": book.trades_after_cutoff,
                "sim_elapsed_min": book.sim_elapsed_min,
                "cold_books": cold_count,
                "min_cold_books": min_reserve_books,
                "rt_left": int(state.rt_left.get(book_id, 0) or 0),
            }
        )

    return orders, logs


def _select_cold_books(
    books: list[ColdBookSnapshot],
    cfg: MarketColdConfig,
    is_cold: Callable[[ColdBookSnapshot, MarketColdConfig], bool],
    sort_key: Callable[[ColdBookSnapshot], tuple],
    min_reserve_books: int,
) -> set[int]:
    cold_books = [book for book in books if is_cold(book, cfg)]
    cold_books.sort(key=sort_key)
    n_to_pulse = max(0, len(cold_books) - min_reserve_books)
    return {book.book_id for book in cold_books[:n_to_pulse]}


def cold_book_market_orders(
    books: list[ColdBookSnapshot],
    state: MarketColdState,
    cfg: MarketColdConfig,
) -> tuple[list[dict], list[dict]]:
    """
    One round trip per selected cold book: BUY (leg 1) then SELL (leg 2).

    Cold books are those where the miner's FIFO observation count in the 3h
    window is below ``target_trades_3h``. Only the excess over
    ``min_cold_books`` is pulsed, coldest books first.

    Returns (orders, logs).
    """
    if not cfg.market_cold_pulse:
        return [], []

    cold_books = [book for book in books if is_cold_book(book, cfg)]
    selected_ids = _select_cold_books(
        books,
        cfg,
        is_cold_book,
        lambda book: (
            book.norm_kappa is None,
            book.norm_kappa if book.norm_kappa is not None else 0.0,
            book.obs_3h,
            book.book_id,
        ),
        cfg.min_cold_books,
    )
    return _cold_roundtrip_orders(
        books,
        state,
        cfg,
        selected_ids=selected_ids,
        cold_count=len(cold_books),
        min_reserve_books=cfg.min_cold_books,
        action_tag="roundtrip",
    )


def cold_edge_book_market_orders(
    books: list[ColdBookSnapshot],
    state: MarketColdState,
    cfg: MarketColdConfig,
    edge_books: frozenset[int],
) -> tuple[list[dict], list[dict]]:
    """Market round trips for explicit ``edge_books`` (no auto cold detection)."""
    if not edge_books and not state.pending_sell:
        return [], []

    # Budget N full BUY→SELL round trips, then set cold_rt_done for retire.
    seed_cold_edge_roundtrips(state, edge_books, cfg.roundtrips)

    relevant = [
        book
        for book in books
        if book.book_id in edge_books or book.book_id in state.pending_sell
    ]
    tradeable_edge = {
        book.book_id
        for book in relevant
        if book.book_id in edge_books
        and _book_tradeable(book, cfg)
        and int(state.rt_left.get(book.book_id, 0) or 0) > 0
    }
    return _cold_roundtrip_orders(
        relevant,
        state,
        cfg,
        selected_ids=tradeable_edge,
        cold_count=len(tradeable_edge),
        min_reserve_books=0,
        action_tag="edge_roundtrip",
        use_roundtrip_budget=True,
    )


def cold_book_yet_market_orders(
    books: list[ColdBookSnapshot],
    state: MarketColdState,
    cfg: MarketColdConfig,
) -> tuple[list[dict], list[dict]]:
    """Round-trip pulse for books with no fills after ``market_cold_yet_minutes``."""
    if not cfg.market_cold_yet_pulse:
        return [], []

    cold_books = [book for book in books if is_cold_book_yet(book, cfg)]
    selected_ids = _select_cold_books(
        books,
        cfg,
        is_cold_book_yet,
        lambda book: (book.trades_3h, book.obs_3h, book.book_id),
        cfg.market_cold_yet_min_books,
    )
    return _cold_roundtrip_orders(
        books,
        state,
        cfg,
        selected_ids=selected_ids,
        cold_count=len(cold_books),
        min_reserve_books=cfg.market_cold_yet_min_books,
        action_tag="yet_roundtrip",
    )
