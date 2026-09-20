# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
TopAgent: per-book open brackets with locked open prices.

  open_books[]  — same-tick two-sided GTT.
                  use_depth_qty=1 + open_price=0 → lock P from L21 depth, then
                  BUY@P + SELL@P+1 (or SELL@P + BUY@P-1).
                  use_depth_qty=0 → first tick from L2 mid sets BUY@mid − bracket/2 and
                  SELL@mid + bracket/2, then locks open_price (buy leg) + close_price
                  (sell leg) in live JSON; later ticks keep those prices.
                  Each side tops up until cumulative own fill qty reaches max_quantity
                  (BUY and SELL capped independently). When both sides reach max,
                  the book is removed from open_books and appended to stop_books
                  (no further duals until removed from stop_books). No flatten.
  stop_books[] — books retired from open_books; the open engine does not run here.
                  Exit is the open-book ±1 rest, so there is no close engine.
                  restore_stop_books=1 (default) re-opens one per tick when the
                  BookManageBot alpha feed says |alpha| < baseline and the touch is
                  wide (>= restore_min_spread) and thin (< restore_max_touch_qty),
                  using restore_bracket_ticks / restore_quantity.
  market_close_books[] — market close close_quantity each tick (long → SELL, short → BUY);
                  uses market_close_max_slippage.
  general_books[] — two-sided maker for making_raw: BUY mid − general_ticks,
                  SELL mid + general_ticks (L2 mid). Clip max 0.5. If |fifo net| > 0.25
                  the adding side is not posted (keeps |α| small vs skill-book mean).
                  Skipped on books also in open_books. Flatten still wins.
  flatten_books[] — Topstrategy-style flatten via strategy_engine.process_topagent_flatten:
                  cancel all resting orders, then market-close validator net (free_base − int_base);
                  uses flatten_max_slippage (default 0.02). Blocks open on listed books.

  use_depth_qty + open_price=0 + close_price=0 → first tick snapshots depth + bracket close leg,
  persists both prices (buy leg = open_price, sell leg = close_price). Reset either to 0 in
  JSON to re-snapshot. Manual: set both prices in open_books[].

  pending_guard=1 (default) → do not re-send a limit on (book, side) while a prior send is still
  waiting for validator acceptance (shared with waiting_orders_recorder).
  max_pending_per_book=5 (default) → skip new sends on a book when it already has 5+ waiting orders
  (aligns with validator max_instructions_per_book).

Hot-reload: edit agents/data/topagent.<uid>.json (or live_config_path) while running.

  live_config=1 live_config_path=agents/data/topagent.203.json
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import types
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

# importlib spec_from_file_location does not register the module until after exec;
# @dataclass needs sys.modules[__name__] during class body evaluation.
if __name__ not in sys.modules:
    sys.modules[__name__] = types.ModuleType(__name__)

import bittensor as bt

from taos.common.agents import launch
from taos.im.agents import GenTRXAgent
from taos.im.protocol.events import TradeEvent
from taos.im.protocol.models import (
    OrderCurrency,
    OrderDirection,
    STP,
    TimeInForce,
    LoanSettlementOption,
)


def _round_px(price: float, decimals: int) -> float:
    """Round to `decimals`, keeping the double at or above that decimal value.

    The exchange stores ROUND_DOWN of the exact binary value, so a double that
    lands just under its own decimal rests a full tick low: 437.2 is really
    437.19999... and comes back as 437.19. The bracket then reads as stale
    every tick and is cancelled and re-placed forever without ever filling.
    Nudging up one ULP costs nothing after truncation and keeps the order on
    the tick we asked for.
    """
    out = round(price, decimals)
    target = Decimal(repr(out))
    if Decimal(out) < target:
        out = math.nextafter(out, math.inf)
    return out


def _tick(decimals: int) -> float:
    return 10.0 ** (-decimals)


# Min sim-time before retry after a close send/cancel (matches Topstrategy micro close).
CLOSE_PENDING_COOLDOWN_NS = 3_000_000_000
SIM_MINUTE_NS = 60_000_000_000
DEFAULT_REGIME_PATH = Path(__file__).resolve().parent / "data" / "trade_price" / "regime.json"


def _level_px_qty(level) -> tuple[float | None, float | None]:
    if level is None:
        return None, None
    if isinstance(level, dict):
        px = level.get("p", level.get("price"))
        qty = level.get("q", level.get("qty", level.get("quantity", level.get("volume"))))
    else:
        px = getattr(level, "price", None) or getattr(level, "p", None)
        qty = getattr(level, "quantity", None) or getattr(level, "qty", None) or getattr(level, "q", None)
    try:
        px_f = float(px) if px is not None else None
    except (TypeError, ValueError):
        px_f = None
    try:
        qty_f = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty_f = None
    return px_f, qty_f


def depth_threshold_price(
    levels,
    qty_threshold: float,
    max_levels: int,
) -> tuple[float | None, float | None, float]:
    """First level (from touch) where cum qty > threshold; else deepest L21 price."""
    cum = 0.0
    last_px: float | None = None
    hit_px: float | None = None
    thresh = float(qty_threshold)
    n = 0
    for level in levels or []:
        if n >= int(max_levels):
            break
        px, qty = _level_px_qty(level)
        if px is None or px <= 0.0 or qty is None:
            n += 1
            continue
        last_px = px
        cum += max(0.0, qty)
        if hit_px is None and cum > thresh:
            hit_px = px
        n += 1
    return hit_px, last_px, cum


def depth_bid_open_price(book, qty_threshold: float, max_levels: int = 21) -> float | None:
    """BUY open price: first bid level where cumulative qty > threshold."""
    levels = getattr(book, "bids", None)
    if levels is None and isinstance(book, dict):
        levels = book.get("bids", book.get("b"))
    hit_px, last_px, cum = depth_threshold_price(levels, qty_threshold, max_levels)
    if hit_px is not None:
        return hit_px
    if last_px is not None and cum > 0.0:
        return last_px
    return None


def mid_open_price(
    book,
    direction: OrderDirection,
    pd: int,
    *,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> float | None:
    """Mid price for opens when use_depth_qty is off and open_price is 0 (post-only clamped)."""
    if best_bid is None or best_ask is None:
        bids = getattr(book, "bids", None)
        asks = getattr(book, "asks", None)
        if isinstance(book, dict):
            bids = bids or book.get("bids", book.get("b"))
            asks = asks or book.get("asks", book.get("a"))
        best_bid = None
        best_ask = None
        if bids:
            px, _ = _level_px_qty(bids[0])
            best_bid = px
        if asks:
            px, _ = _level_px_qty(asks[0])
            best_ask = px
    if (
        best_bid is None
        or best_ask is None
        or best_bid <= 0.0
        or best_ask <= 0.0
        or best_ask <= best_bid + 1e-12
    ):
        return None
    mid = _round_px((float(best_bid) + float(best_ask)) * 0.5, pd)
    if mid <= 0.0:
        return None
    tick = _tick(pd)
    if direction == OrderDirection.SELL:
        if mid <= best_bid + 1e-12:
            mid = _round_px(best_bid + tick, pd)
            if mid <= best_bid + 1e-12:
                mid = _round_px(best_ask, pd)
    else:
        if mid >= best_ask - 1e-12:
            mid = _round_px(best_ask - tick, pd)
            if mid <= 0.0 or mid >= best_ask - 1e-12:
                mid = _round_px(best_bid, pd)
    return mid if mid > 0.0 else None


def depth_ask_open_price(book, qty_threshold: float, max_levels: int = 21) -> float | None:
    """SELL open price: first ask level where cumulative qty > threshold."""
    levels = getattr(book, "asks", None)
    if levels is None and isinstance(book, dict):
        levels = book.get("asks", book.get("a"))
    hit_px, last_px, cum = depth_threshold_price(levels, qty_threshold, max_levels)
    if hit_px is not None:
        return hit_px
    if last_px is not None and cum > 0.0:
        return last_px
    return None


def parse_open_direction(val, default: OrderDirection = OrderDirection.BUY) -> OrderDirection:
    if val is None:
        return default
    if isinstance(val, OrderDirection):
        return val
    if isinstance(val, int):
        return OrderDirection.SELL if val == 1 else OrderDirection.BUY
    s = str(val).strip().upper()
    if s in ("SELL", "S", "SHORT", "ASK", "1"):
        return OrderDirection.SELL
    return OrderDirection.BUY


def _parse_bool(val, default: bool = False) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default


def _parse_optional_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return max(1, int(val))
    except (TypeError, ValueError):
        return None


@dataclass
class OpenBookSpec:
    book: int
    open_price: float
    max_quantity: float
    close_price: float = 0.0
    depth_qty: float | None = None
    use_depth_qty: bool = False
    open_direction: OrderDirection = OrderDirection.BUY
    bracket_ticks: int | None = None


@dataclass
class GeneralBookSpec:
    book: int
    ticks: int = 3
    max_quantity: float = 50.0


@dataclass
class BookFillState:
    """Running average entry after opens (for close pricing)."""
    qty: float = 0.0
    cost: float = 0.0
    open_fees: float = 0.0

    @property
    def avg_entry(self) -> float:
        return self.cost / self.qty if self.qty > 1e-12 else 0.0


@dataclass
class OpenBookSideFills:
    """Session cumulative own fill qty per side (tape quantity, for open-book caps)."""
    buy: float = 0.0
    sell: float = 0.0
    seeded_from_csv: bool = False


def parse_open_books(raw, *, default_use_depth_qty: bool = False) -> list[OpenBookSpec]:
    out: list[OpenBookSpec] = []
    if not raw:
        return out
    for item in raw:
        if isinstance(item, int):
            out.append(
                OpenBookSpec(
                    book=int(item),
                    open_price=0.0,
                    max_quantity=1.0,
                    use_depth_qty=default_use_depth_qty,
                )
            )
        elif isinstance(item, dict):
            raw_dq = item.get("depth_qty", item.get("open_depth_qty"))
            depth_qty = None
            if raw_dq is not None and raw_dq != "":
                try:
                    depth_qty = float(raw_dq)
                except (TypeError, ValueError):
                    depth_qty = None
            if "use_depth_qty" in item:
                use_depth = _parse_bool(item.get("use_depth_qty"), default=False)
            else:
                # Legacy: open_price=0 implied depth when global default is on.
                raw_px = item.get("open_price", item.get("price", 0.0))
                try:
                    px0 = float(raw_px or 0.0)
                except (TypeError, ValueError):
                    px0 = 0.0
                use_depth = default_use_depth_qty and px0 <= 0.0
            out.append(
                OpenBookSpec(
                    book=int(item["book"]),
                    open_price=float(item.get("open_price", item.get("price", 0.0))),
                    close_price=float(item.get("close_price", 0.0) or 0.0),
                    max_quantity=float(item.get("max_quantity", item.get("max_qty", 1.0))),
                    depth_qty=depth_qty,
                    use_depth_qty=use_depth,
                    open_direction=parse_open_direction(
                        item.get("open_direction", item.get("direction"))
                    ),
                    bracket_ticks=_parse_optional_int(
                        item.get("bracket_ticks", item.get("ticks"))
                    ),
                )
            )
    return out


def parse_market_close_books(raw) -> set[int]:
    out: set[int] = set()
    if not raw:
        return out
    for item in raw:
        if isinstance(item, int):
            out.add(int(item))
        elif isinstance(item, dict):
            out.add(int(item["book"]))
    return out


def parse_flatten_books(raw) -> set[int]:
    return parse_market_close_books(raw)


def parse_stop_books(raw) -> set[int]:
    return parse_market_close_books(raw)


def parse_general_books(
    raw,
    *,
    default_ticks: int = 3,
    default_max_quantity: float = 0.5,
) -> list[GeneralBookSpec]:
    out: list[GeneralBookSpec] = []
    if not raw:
        return out
    ticks0 = max(1, int(default_ticks or 3))
    qty0 = float(default_max_quantity or 0.5)
    for item in raw:
        if isinstance(item, int):
            out.append(GeneralBookSpec(book=int(item), ticks=ticks0, max_quantity=qty0))
        elif isinstance(item, dict) and item.get("book") is not None:
            try:
                ticks = int(item.get("ticks", ticks0) or ticks0)
            except (TypeError, ValueError):
                ticks = ticks0
            try:
                qty = float(item.get("max_quantity", item.get("max_qty", qty0)) or qty0)
            except (TypeError, ValueError):
                qty = qty0
            out.append(
                GeneralBookSpec(
                    book=int(item["book"]),
                    ticks=max(1, ticks),
                    max_quantity=qty if qty > 0.0 else qty0,
                )
            )
    return out


def _parse_release_ts(raw) -> dict[int, int]:
    out: dict[int, int] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        try:
            bid = int(key)
            ts = int(val or 0)
        except (TypeError, ValueError):
            continue
        if bid >= 0 and ts > 0:
            out[bid] = ts
    return out


class TopAgent(GenTRXAgent):
    """Open-only / close-only book lists with live JSON reload."""

    LIVE_SCALAR_KEYS: dict[str, type] = {
        "close_quantity": float,
        "close_dust_threshold": float,
        "open_dust_stop_threshold": float,
        "open_quantity": float,
        "open_depth_qty": float,
        "open_depth_levels": int,
        "expiry_period": int,
        "market_close_max_slippage": float,
        "flatten_max_slippage": float,
        "flatten_max_books_per_tick": int,
        "flatten_flat_qty": float,
        "max_pending_per_book": int,
        "eff_last": float,
        "idle_stop_edge_minutes": float,
        "open_last": float,
        "fok_close_cooldown_ns": int,
        "fifo_persist_interval_ticks": int,
        "general_ticks": int,
        "general_quantity": float,
        "general_open_max": float,
        "bracket_ticks": int,
        "recharge_times": int,
        "restore_bracket_ticks": int,
        "restore_quantity": float,
        "restore_min_spread": float,
        "restore_max_touch_qty": float,
        "restore_cooldown_minutes": float,
        "alpha_baseline_max_age_sec": float,
        "target_book_count": int,
        "acquire_bracket_ticks": int,
        "acquire_quantity": float,
        "acquire_max_touch_qty": float,
    }
    LIVE_BOOL_SCALAR_KEYS: dict[str, bool] = {
        "use_depth_qty": False,
        "cancel_all_orders": False,
        "fifo_log_trades": True,
        "restore_stop_books": True,
        "acquire_open_books": False,
    }

    def initialize(self):
        if not hasattr(self.config, "gtx_training_enabled"):
            self.config.gtx_training_enabled = False
        if not hasattr(self.config, "gtx_collect_data"):
            self.config.gtx_collect_data = False
        super().initialize()

        self.close_quantity = float(getattr(self.config, "close_quantity", 0.25))
        self.close_dust_threshold = float(
            getattr(self.config, "close_dust_threshold", 0.51)
        )
        self.open_dust_stop_threshold = float(
            getattr(self.config, "open_dust_stop_threshold", 0.01) or 0.01
        )
        self.open_quantity = float(getattr(self.config, "open_quantity", 0.25))
        self.open_depth_qty = float(getattr(self.config, "open_depth_qty", 200.0))
        self.open_depth_levels = max(1, int(getattr(self.config, "open_depth_levels", 21)))
        self.use_depth_qty = _parse_bool(
            getattr(self.config, "use_depth_qty", None), default=False
        )
        self.expiry_period = int(getattr(self.config, "expiry_period", 200_000_000_000))
        self.market_close_max_slippage = float(
            getattr(self.config, "market_close_max_slippage", 0.01) or 0.01
        )
        self.flatten_max_slippage = float(
            getattr(self.config, "flatten_max_slippage", 0.02) or 0.02
        )
        self.flatten_max_books_per_tick = max(
            1, int(getattr(self.config, "flatten_max_books_per_tick", 15))
        )
        self.flatten_flat_qty = float(getattr(self.config, "flatten_flat_qty", 0.0) or 0.0)
        self.flatten_max_slippage = float(
            getattr(self.config, "flatten_max_slippage", 0.02) or 0.02
        )
        self.flatten_max_books_per_tick = max(
            1, int(getattr(self.config, "flatten_max_books_per_tick", 15))
        )
        self.flatten_flat_qty = float(getattr(self.config, "flatten_flat_qty", 0.0) or 0.0)

        repo_root = Path(__file__).resolve().parent.parent
        agent_dir = repo_root / "agents"
        self.live_config = bool(int(getattr(self.config, "live_config", 1)))
        live_path = getattr(self.config, "live_config_path", "")
        if live_path:
            p = Path(live_path)
            if p.is_absolute():
                self.live_config_path = p
            else:
                self.live_config_path = repo_root / p
        else:
            self.live_config_path = agent_dir / "data" / f"topagent.{self.uid}.json"
        self._live_config_template = agent_dir / "topagent.live.json"
        self._live_config_mtime: float | None = None

        self.general_ticks = max(1, int(getattr(self.config, "general_ticks", 3) or 3))
        self.general_quantity = float(getattr(self.config, "general_quantity", 0.5) or 0.5)
        self.general_open_max = float(getattr(self.config, "general_open_max", 0.25) or 0.25)
        self.bracket_ticks = max(1, int(getattr(self.config, "bracket_ticks", 2) or 2))
        self.general_ticks = max(1, int(getattr(self.config, "general_ticks", 3) or 3))
        self.general_quantity = float(getattr(self.config, "general_quantity", 0.5) or 0.5)
        self.general_open_max = float(getattr(self.config, "general_open_max", 0.25) or 0.25)
        self.bracket_ticks = max(1, int(getattr(self.config, "bracket_ticks", 2) or 2))
        self.open_books: dict[int, OpenBookSpec] = {}
        self.market_close_books: set[int] = set()
        self.flatten_books: set[int] = set()
        self.stop_books: set[int] = set()
        self.general_books: dict[int, GeneralBookSpec] = {}
        self.stop_books: set[int] = set()
        self.general_books: dict[int, GeneralBookSpec] = {}
        self._fills: dict[int, BookFillState] = {}

        self.fifo_persist = bool(int(getattr(self.config, "fifo_persist", 1)))
        self.fifo_persist_interval_ticks = max(
            1, int(getattr(self.config, "fifo_persist_interval_ticks", 1))
        )
        self.fifo_buy_base_fee_haircut = bool(
            int(getattr(self.config, "fifo_buy_base_fee_haircut", 1))
        )
        self.fifo_bootstrap = bool(int(getattr(self.config, "fifo_bootstrap", 1)))
        self.fifo_bootstrap = bool(int(getattr(self.config, "fifo_bootstrap", 1)))
        self.fifo_log_trades = bool(int(getattr(self.config, "fifo_log_trades", 1)))
        self.pending_guard = bool(int(getattr(self.config, "pending_guard", 1)))
        self.pending_guard_max_age_sec = float(
            getattr(self.config, "pending_guard_max_age_sec", 90.0) or 90.0
        )
        self.max_pending_per_book = max(
            0, int(getattr(self.config, "max_pending_per_book", 5))
        )
        self.fok_close_cooldown_ns = max(
            0, int(getattr(self.config, "fok_close_cooldown_ns", 0) or 0)
        )
        self.cancel_all_orders = _parse_bool(
            getattr(self.config, "cancel_all_orders", None), default=False
        )
        self.restore_stop_books = _parse_bool(
            getattr(self.config, "restore_stop_books", None), default=True
        )
        self.restore_bracket_ticks = max(
            1, int(getattr(self.config, "restore_bracket_ticks", 50) or 50)
        )
        self.restore_quantity = float(
            getattr(self.config, "restore_quantity", 100.0) or 100.0
        )
        self.restore_min_spread = float(
            getattr(self.config, "restore_min_spread", 0.5) or 0.5
        )
        self.restore_max_touch_qty = float(
            getattr(self.config, "restore_max_touch_qty", 20.0) or 20.0
        )
        self.restore_cooldown_minutes = float(
            getattr(self.config, "restore_cooldown_minutes", 30.0) or 0.0
        )
        self.alpha_baseline_max_age_sec = float(
            getattr(self.config, "alpha_baseline_max_age_sec", 900.0) or 0.0
        )
        # Off by default: a UID without an explicit target must not start
        # claiming books just because it happens to hold fewer than four.
        self.acquire_open_books = _parse_bool(
            getattr(self.config, "acquire_open_books", None), default=False
        )
        self.target_book_count = max(
            0, int(getattr(self.config, "target_book_count", 4) or 4)
        )
        self.acquire_bracket_ticks = max(
            1, int(getattr(self.config, "acquire_bracket_ticks", 100) or 100)
        )
        self.acquire_quantity = float(
            getattr(self.config, "acquire_quantity", 100.0) or 100.0
        )
        self.acquire_max_touch_qty = float(
            getattr(self.config, "acquire_max_touch_qty", 10.0) or 10.0
        )
        raw_baseline_path = str(
            getattr(self.config, "alpha_baseline_path", "") or ""
        ).strip()
        self.alpha_baseline_path = (
            Path(raw_baseline_path)
            if raw_baseline_path
            else Path(__file__).resolve().parent
            / "data"
            / f"debeta_baseline.{self.uid}.json"
        )
        raw_public_path = str(
            getattr(self.config, "alpha_baseline_public_path", "") or ""
        ).strip()
        self.alpha_baseline_public_path = (
            Path(raw_public_path)
            if raw_public_path
            else self.alpha_baseline_path.parent / "debeta_mean_alpha.json"
        )
        self._alpha_baseline_cache: dict[str, dict | None] = {}
        self._alpha_baseline_mtime: dict[str, float] = {}
        self._restore_last_ts: dict[int, int] = {}
        self._restore_block_logged: dict[int, str] = {}
        self._open_fills_epoch: dict[int, int] = {}
        self._open_started_ts: dict[int, int] = {}
        self._open_book_side_fills: dict[int, OpenBookSideFills] = {}
        self._open_fill_trade_keys: set[tuple[int, int]] = set()
        self._open_skip_logged: set[int] = set()
        self._requote_streak: dict[tuple[int, int], tuple[float, float, int]] = {}
        self._pending_sides_this_tick: set[tuple[int, int]] = set()
        self._close_pending: dict[int, int] = {}
        self._close_pending_sent_ts: dict[int, int] = {}
        # FOK closes fill immediately — track cooldown separately from GTC latch.
        self._fok_close_last_ts: dict[int, int] = {}
        # FOK closes fill immediately — track cooldown separately from GTC latch.
        self._fok_close_last_ts: dict[int, int] = {}
        self._fifos: dict = {}
        self._fifo = None
        self._fifo_sim_key: str | None = None
        self._fifo_state_path: Path | None = None
        self._fifo_ctx_state = None
        self._last_state_ts = 0
        self._fifo_persist_tick = 0
        self._last_persisted_open_px: dict[int, float] = {}
        self._last_persisted_close_px: dict[int, float] = {}
        self._last_persisted_close_px: dict[int, float] = {}

        if self.live_config_path.is_file():
            self._load_live_json(self.live_config_path.read_text(), source="init")
        elif self.live_config:
            self._seed_live_json()

        bt.logging.info(
            f"TopAgent uid={self.uid} open={sorted(self.open_books)} "
            f"general={sorted(self.general_books)} "
            f"market_close={sorted(self.market_close_books)} "
            f"flatten={sorted(self.flatten_books)} "
            f"stop={sorted(self.stop_books)} "
            f"live={self.live_config_path}"
        )

    def _seed_live_json(self) -> None:
        if self._live_config_template.is_file():
            self._load_live_json(
                self._live_config_template.read_text(), source="template"
            )
        payload = {
            "open_books": [
                {
                    "book": s.book,
                    "open_price": s.open_price,
                    "max_quantity": s.max_quantity,
                    "use_depth_qty": s.use_depth_qty,
                    **(
                        {"open_direction": "SELL"}
                        if s.open_direction == OrderDirection.SELL
                        else {}
                    ),
                }
                for s in self.open_books.values()
            ],
            "market_close_books": sorted(self.market_close_books),
            "flatten_books": sorted(self.flatten_books),
            "stop_books": sorted(self.stop_books),
            "flatten_max_slippage": self.flatten_max_slippage,
            "flatten_max_books_per_tick": self.flatten_max_books_per_tick,
            "flatten_books": sorted(self.flatten_books),
            "stop_books": sorted(self.stop_books),
            "general_books": [
                {
                    "book": s.book,
                    "ticks": s.ticks,
                    "max_quantity": s.max_quantity,
                }
                for s in self.general_books.values()
            ],
            "general_ticks": self.general_ticks,
            "general_quantity": self.general_quantity,
            "general_open_max": self.general_open_max,
            "bracket_ticks": self.bracket_ticks,
            "stop_books": sorted(self.stop_books),
            "general_books": [
                {
                    "book": s.book,
                    "ticks": s.ticks,
                    "max_quantity": s.max_quantity,
                }
                for s in self.general_books.values()
            ],
            "general_ticks": self.general_ticks,
            "general_quantity": self.general_quantity,
            "general_open_max": self.general_open_max,
            "bracket_ticks": self.bracket_ticks,
            "flatten_max_slippage": self.flatten_max_slippage,
            "flatten_max_books_per_tick": self.flatten_max_books_per_tick,
            "close_quantity": self.close_quantity,
            "close_dust_threshold": self.close_dust_threshold,
            "open_dust_stop_threshold": self.open_dust_stop_threshold,
            "open_quantity": self.open_quantity,
            "open_depth_qty": self.open_depth_qty,
            "open_depth_levels": self.open_depth_levels,
            "use_depth_qty": self.use_depth_qty,
            "expiry_period": self.expiry_period,
            "market_close_max_slippage": self.market_close_max_slippage,
        }
        self.live_config_path.parent.mkdir(parents=True, exist_ok=True)
        self.live_config_path.write_text(json.dumps(payload, indent=2) + "\n")
        self._live_config_mtime = self.live_config_path.stat().st_mtime

    def _load_live_json(self, text: str, *, source: str) -> list[str]:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            bt.logging.warning(f"TopAgent live JSON invalid ({source}): {exc}")
            return []
        if not isinstance(data, dict):
            return []
        return self._apply_live_overrides(data, source=source)

    def _apply_live_overrides(self, data: dict, *, source: str) -> list[str]:
        changed: list[str] = []
        for key, cast in self.LIVE_SCALAR_KEYS.items():
            if key not in data:
                continue
            try:
                new_val = cast(data[key])
            except (TypeError, ValueError):
                bt.logging.warning(f"TopAgent: skip invalid {key}={data[key]!r}")
                continue
            old_val = getattr(self, key, None)
            if new_val != old_val:
                setattr(self, key, new_val)
                changed.append(f"{key}={old_val}->{new_val}")

        if "eff_last" in data:
            try:
                self.eff_last = float(data["eff_last"])
                self.idle_stop_edge_minutes = float(self.eff_last)
            except (TypeError, ValueError):
                pass
        elif "idle_stop_edge_minutes" in data:
            try:
                self.eff_last = float(data["idle_stop_edge_minutes"])
                self.idle_stop_edge_minutes = float(self.eff_last)
            except (TypeError, ValueError):
                pass

        if "idle_stop_release_ts" in data:
            release = _parse_release_ts(data.get("idle_stop_release_ts"))
            if release != getattr(self, "_idle_stop_release_ts", {}):
                self._idle_stop_release_ts = release
                changed.append(f"idle_stop_release_ts={len(release)}")

        if "open_started_ts" in data:
            started = _parse_release_ts(data.get("open_started_ts"))
            if started != getattr(self, "_open_started_ts", {}):
                self._open_started_ts = started
                changed.append(f"open_started_ts={len(started)}")

        if "open_fills_epoch" in data:
            epoch = _parse_release_ts(data.get("open_fills_epoch"))
            if epoch != getattr(self, "_open_fills_epoch", {}):
                self._open_fills_epoch = epoch
                changed.append(f"open_fills_epoch={len(epoch)}")

        for key, default in self.LIVE_BOOL_SCALAR_KEYS.items():
            if key not in data:
                continue
            new_val = _parse_bool(data[key], default=default)
            old_val = bool(getattr(self, key, default))
            if new_val != old_val:
                setattr(self, key, new_val)
                changed.append(f"{key}={old_val}->{new_val}")

        if "stop_books" in data:
            specs = parse_stop_books(data["stop_books"])
            if specs != self.stop_books:
                self.stop_books = specs
                changed.append(f"stop_books={sorted(specs)}")

        if "open_books" in data:
            specs = {
                s.book: s
                for s in parse_open_books(
                    data["open_books"], default_use_depth_qty=self.use_depth_qty
                )
            }
            if self.stop_books:
                specs = {
                    bid: s for bid, s in specs.items() if bid not in self.stop_books
                }
            if specs != self.open_books:
                added = set(specs) - set(self.open_books)
                self.open_books = specs
                now_ts = int(getattr(self, "_last_state_ts", 0) or 0)
                for bid in added:
                    if now_ts > 0 and bid not in self._open_started_ts:
                        self._open_started_ts[bid] = now_ts
                changed.append(f"open_books={sorted(specs)}")
            if hasattr(self, "_open_book_side_fills"):
                self._open_book_side_fills = {
                    bid: st
                    for bid, st in self._open_book_side_fills.items()
                    if bid in self.open_books
                }
            if hasattr(self, "_open_fill_trade_keys"):
                open_ids = set(self.open_books)
                self._open_fill_trade_keys = {
                    key
                    for key in self._open_fill_trade_keys
                    if key[0] in open_ids
                }

        if "market_close_books" in data:
            specs = parse_market_close_books(data["market_close_books"])
            if specs != self.market_close_books:
                self.market_close_books = specs
                changed.append(f"market_close_books={sorted(specs)}")

        if "flatten_books" in data:
            specs = parse_flatten_books(data["flatten_books"])
            if specs != self.flatten_books:
                self.flatten_books = specs
                changed.append(f"flatten_books={sorted(specs)}")

        if "flatten_books" in data:
            specs = parse_flatten_books(data["flatten_books"])
            if specs != self.flatten_books:
                self.flatten_books = specs
                changed.append(f"flatten_books={sorted(specs)}")

        if "general_books" in data:
            specs = {
                s.book: s
                for s in parse_general_books(
                    data["general_books"],
                    default_ticks=self.general_ticks,
                    default_max_quantity=self.general_quantity,
                )
            }
            if specs != self.general_books:
                self.general_books = specs
                changed.append(f"general_books={sorted(specs)}")

        if "general_books" in data:
            specs = {
                s.book: s
                for s in parse_general_books(
                    data["general_books"],
                    default_ticks=self.general_ticks,
                    default_max_quantity=self.general_quantity,
                )
            }
            if specs != self.general_books:
                self.general_books = specs
                changed.append(f"general_books={sorted(specs)}")

        if changed:
            bt.logging.info(f"TopAgent live reload ({source}): {', '.join(changed)}")
        return changed

    def _maybe_reload_live_config(self) -> None:
        if not self.live_config or not self.live_config_path.is_file():
            return
        mtime = self.live_config_path.stat().st_mtime
        if self._live_config_mtime is not None and mtime == self._live_config_mtime:
            return
        self._load_live_json(self.live_config_path.read_text(), source="mtime")
        self._live_config_mtime = mtime

    def _fill_state(self, book_id: int) -> BookFillState:
        if book_id not in self._fills:
            self._fills[book_id] = BookFillState()
        return self._fills[book_id]

    def _book_in_state(self, state, book_id: int) -> bool:
        books = getattr(state, "books", None) or {}
        bid = int(book_id)
        if bid in books or str(bid) in books:
            return True
        try:
            return books.get(bid) is not None or books.get(str(bid)) is not None
        except (TypeError, AttributeError):
            return False

    def _book_from_state(self, state, book_id: int):
        books = getattr(state, "books", None) or {}
        bid = int(book_id)
        try:
            return books.get(bid) or books.get(str(bid))
        except (TypeError, AttributeError):
            return None

    def _account_for_book(self, book_id: int):
        bid = int(book_id)
        acct = self.accounts.get(bid)
        if acct is not None:
            return acct
        return self.accounts.get(str(bid))

    def _note_requote(self, book_id: int, side: int, want: float, got: float) -> None:
        """Warn when the exchange keeps resting a side away from the asked price.

        An unchanging want/got pair means we cancel and re-place the same order
        every tick, so it never lives long enough for anyone to hit it.
        """
        if not hasattr(self, "_requote_streak"):
            self._requote_streak = {}
        key = (int(book_id), int(side))
        prev = self._requote_streak.get(key)
        if prev is not None and prev[0] == want and prev[1] == got:
            count = prev[2] + 1
        else:
            count = 1
        self._requote_streak[key] = (want, got, count)
        if count == 5 or (count % 250 == 0):
            bt.logging.warning(
                f"TopAgent book {book_id} {'BUY' if int(side) == 0 else 'SELL'}: "
                f"re-quoted {count}x in a row, asked {want} but the exchange "
                f"rests {got} — the order is cancelled before it can fill"
            )

    def _log_open_skip_once(self, book_id: int, reason: str) -> None:
        bid = int(book_id)
        if bid in self._open_skip_logged:
            return
        self._open_skip_logged.add(bid)
        bt.logging.info(f"TopAgent open skip book={bid}: {reason}")

    def _has_resting_side(self, book_id: int, side: int) -> bool:
        acct = self._account_for_book(book_id)
        if not acct or not acct.orders:
            return False
        return any(o.side == side for o in acct.orders)

    def _reconcile_pending_guard(self) -> None:
        if not self.pending_guard:
            return
        try:
            from waiting_orders_recorder import reconcile_pending_before_tick

            reconcile_pending_before_tick(
                self, max_age_sec=self.pending_guard_max_age_sec
            )
        except Exception:
            pass

    def _order_side(self, direction: OrderDirection) -> int:
        return 1 if direction == OrderDirection.SELL else 0

    def _has_pending_limit(self, book_id: int, side: int) -> bool:
        if not self.pending_guard:
            return False
        if (int(book_id), int(side)) in self._pending_sides_this_tick:
            return True
        try:
            from waiting_orders_recorder import has_pending_new_limit

            return has_pending_new_limit(self, book_id, side)
        except Exception:
            return False

    def _book_at_pending_limit(self, book_id: int) -> bool:
        if not self.pending_guard:
            return False
        cap = int(getattr(self, "max_pending_per_book", 5))
        if cap <= 0:
            return False
        try:
            from waiting_orders_recorder import has_pending_book_at_limit

            return has_pending_book_at_limit(self, book_id, limit=cap)
        except Exception:
            return False

    def _close_pending_side(self, book_id: int) -> int | None:
        side = self._close_pending.get(int(book_id))
        return int(side) if side is not None else None

    def _set_close_pending(self, book_id: int, side: int, sim_ts: int | None) -> None:
        bid = int(book_id)
        self._close_pending[bid] = int(side)
        if sim_ts is not None and int(sim_ts) > 0:
            self._close_pending_sent_ts[bid] = int(sim_ts)

    def _clear_close_pending(self, book_id: int) -> None:
        bid = int(book_id)
        self._close_pending.pop(bid, None)
        self._close_pending_sent_ts.pop(bid, None)
        self._fok_close_last_ts.pop(bid, None)

    def _clear_all_close_pending(self) -> None:
        self._close_pending.clear()
        self._close_pending_sent_ts.clear()
        self._fok_close_last_ts.clear()

    def _fok_close_on_cooldown(self, book_id: int, sim_ts: int | None) -> bool:
        """True during sim-time cooldown after a FOK/market close send (never rests)."""
        if hasattr(self, "fok_close_cooldown_ns"):
            cooldown_ns = int(getattr(self, "fok_close_cooldown_ns", 0) or 0)
        else:
            cooldown_ns = CLOSE_PENDING_COOLDOWN_NS
        if cooldown_ns <= 0:
            return False
        last = int(
            getattr(self, "_fok_close_last_ts", {}).get(int(book_id), 0) or 0
        )
        ts = int(sim_ts or 0)
        return last > 0 and ts > 0 and (ts - last) < cooldown_ns

    def _record_fok_close_sent(self, book_id: int, sim_ts: int | None) -> None:
        if sim_ts is not None and int(sim_ts) > 0:
            if not hasattr(self, "_fok_close_last_ts"):
                self._fok_close_last_ts = {}
            self._fok_close_last_ts[int(book_id)] = int(sim_ts)

    def _waiting_close_send(self, book_id: int, side: int) -> bool:
        """True when a close is in-flight on this book (latch or pending limit)."""
        if self._close_pending_side(int(book_id)) == int(side):
            return True
        return self._has_pending_limit(book_id, side)

    def _mark_pending_limit(
        self,
        book_id: int,
        side: int,
        *,
        price: float,
        quantity: float,
        time_in_force: str = "GTT",
    ) -> None:
        if not self.pending_guard:
            return
        self._pending_sides_this_tick.add((int(book_id), int(side)))
        try:
            from waiting_orders_recorder import mark_pending_limit

            mark_pending_limit(
                self,
                book_id,
                side,
                price=price,
                quantity=quantity,
                sim_ts=getattr(self, "_last_state_ts", None) or None,
                max_age_sec=self.pending_guard_max_age_sec,
                time_in_force=time_in_force,
            )
        except Exception:
            pass

    def _place_gtt_limit(
        self,
        response,
        *,
        book_id: int,
        direction: OrderDirection,
        quantity: float,
        price: float,
    ) -> bool:
        """Place a GTT limit unless one is already pending on this (book, side)."""
        side = self._order_side(direction)
        if self._book_at_pending_limit(book_id):
            return False
        if self._has_pending_limit(book_id, side):
            return False
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price,
            stp=STP.CANCEL_BOTH,
            timeInForce=TimeInForce.GTT,
            expiryPeriod=self.expiry_period,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        self._mark_pending_limit(
            book_id, side, price=price, quantity=quantity
        )
        return True

    def _place_gtc_close_limit(
        self,
        response,
        *,
        book_id: int,
        direction: OrderDirection,
        quantity: float,
        price: float,
    ) -> bool:
        """GTC post-only maker close; one in-flight close per book."""
        side = self._order_side(direction)
        if self._waiting_close_send(book_id, side):
            return False
        if self._book_at_pending_limit(book_id):
            return False
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price,
            stp=STP.CANCEL_BOTH,
            postOnly=True,
            timeInForce=TimeInForce.GTC,
            expiryPeriod=self.expiry_period,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        self._mark_pending_limit(
            book_id, side, price=price, quantity=quantity, time_in_force="GTC"
        )
        return True

    def _place_fok_close_limit(
        self,
        response,
        *,
        book_id: int,
        direction: OrderDirection,
        quantity: float,
        price: float,
    ) -> bool:
        """FOK close at fee floor when touch is profitable; one in-flight close per book."""
        side = self._order_side(direction)
        if self._book_at_pending_limit(book_id):
            return False
        self._clear_pending_limit(book_id, side)
        response.limit_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            price=price,
            stp=STP.CANCEL_BOTH,
            timeInForce=TimeInForce.FOK,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        return True

    def _clear_pending_limit(self, book_id: int, side: int) -> None:
        if not self.pending_guard:
            return
        self._pending_sides_this_tick.discard((int(book_id), int(side)))
        try:
            from waiting_orders_recorder import clear_pending_new_limits

            clear_pending_new_limits(self, book_id, side)
        except Exception:
            pass

    def _register_pending_limits(self, response, sim_ts: int | None) -> None:
        if not self.pending_guard:
            return
        try:
            from waiting_orders_recorder import register_sent_limits

            register_sent_limits(
                self,
                response,
                sim_ts=sim_ts,
                max_age_sec=self.pending_guard_max_age_sec,
            )
        except Exception:
            pass

    def _fifo_net_qty(self, book_id: int, state=None) -> float:
        """Current FIFO net qty (long − short) for this book."""
        ctx = state or getattr(self, "_fifo_ctx_state", None)
        if ctx is None:
            return 0.0
        try:
            from fifo_state_recorder import ensure_fifo

            fifo = ensure_fifo(self, ctx)
            return float(fifo.net_qty(int(book_id)))
        except Exception:
            return 0.0

    def _fifo_avg_entry(self, book_id: int, state=None) -> float:
        """Validator-style FIFO average entry for close pricing."""
        ctx = state or getattr(self, "_fifo_ctx_state", None)
        if ctx is None:
            return 0.0
        try:
            from fifo_state_recorder import ensure_fifo

            fifo = ensure_fifo(self, ctx)
            if abs(float(fifo.net_qty(int(book_id)))) < 1e-12:
                return 0.0
            return float(fifo.avg_entry_price(int(book_id)))
        except Exception:
            return 0.0

    def _best_bid_ask(self, book) -> tuple[float | None, float | None]:
        bids = getattr(book, "bids", None)
        asks = getattr(book, "asks", None)
        if isinstance(book, dict):
            bids = bids or book.get("bids", book.get("b"))
            asks = asks or book.get("asks", book.get("a"))
        best_bid = None
        best_ask = None
        if bids:
            px, _ = _level_px_qty(bids[0])
            best_bid = px
        if asks:
            px, _ = _level_px_qty(asks[0])
            best_ask = px
        return best_bid, best_ask

    def _touch_best_bid_ask(self, book) -> tuple[float | None, float | None]:
        """Read BBO from LazyBook raw wire without parsing full depth."""
        raw = getattr(book, "_raw", None)
        if not isinstance(raw, dict):
            return self._best_bid_ask(book)
        bids = raw.get("b") or []
        asks = raw.get("a") or []
        best_bid = None
        best_ask = None
        if bids:
            px, _ = _level_px_qty(bids[0])
            best_bid = px
        if asks:
            px, _ = _level_px_qty(asks[0])
            best_ask = px
        return best_bid, best_ask

    def _touch_quote(
        self, book
    ) -> tuple[float | None, float | None, float | None, float | None]:
        """BBO with resting size: (bid_px, bid_qty, ask_px, ask_qty)."""
        raw = getattr(book, "_raw", None)
        if not isinstance(raw, dict):
            best_bid, best_ask = self._best_bid_ask(book)
            return best_bid, None, best_ask, None
        bids = raw.get("b") or []
        asks = raw.get("a") or []
        bid_px, bid_qty = _level_px_qty(bids[0]) if bids else (None, None)
        ask_px, ask_qty = _level_px_qty(asks[0]) if asks else (None, None)
        return bid_px, bid_qty, ask_px, ask_qty

    def _effective_open_post_price(
        self,
        px: float,
        spec: OpenBookSpec,
        state,
        book_id: int,
        pd: int,
    ) -> float:
        """Price aligned to the book before limit_order (matches engine posting)."""
        px_r = _round_px(px, pd)
        if not spec.use_depth_qty:
            return px_r
        if state is None:
            return px_r
        book = (getattr(state, "books", None) or {}).get(book_id)
        if book is None:
            return px_r
        best_bid, best_ask = self._best_bid_ask(book)
        tick = _tick(pd)
        if spec.open_direction == OrderDirection.SELL:
            if best_ask is not None and best_ask > 0.0:
                if px_r > best_ask and px_r - best_ask <= tick * 1.01:
                    return _round_px(best_ask, pd)
        else:
            if best_bid is not None and best_bid > 0.0:
                if px_r < best_bid and best_bid - px_r <= tick * 1.01:
                    return _round_px(best_bid, pd)
        return px_r

    def _resting_side_price(self, book_id: int, side: int, pd: int) -> float | None:
        acct = self._account_for_book(book_id)
        if acct is None or not acct.orders:
            return None
        for order in acct.orders:
            if order.side != side:
                continue
            try:
                px = float(order.price)
            except (TypeError, ValueError):
                continue
            if px > 0.0:
                return _round_px(px, pd)
        return None

    def _book_use_depth(self, spec: OpenBookSpec) -> bool:
        return bool(spec.use_depth_qty)

    def _spec_bracket_ticks(self, spec: OpenBookSpec) -> int:
        if spec.bracket_ticks is not None:
            return max(1, int(spec.bracket_ticks))
        return max(1, int(getattr(self, "bracket_ticks", 2) or 2))

    def _mid_bracket_prices(
        self,
        book_id: int,
        spec: OpenBookSpec,
        state,
        pd: int,
    ) -> tuple[float, float] | None:
        """(buy_px, sell_px) at mid ± bracket_ticks/2. None if no two-sided book."""
        if state is None:
            return None
        book = self._book_from_state(state, book_id)
        if book is None:
            return None
        best_bid, best_ask = self._touch_best_bid_ask(book)
        if (
            best_bid is None
            or best_ask is None
            or best_bid <= 0.0
            or best_ask <= 0.0
        ):
            return None
        mid = (float(best_bid) + float(best_ask)) / 2.0
        tick = _tick(pd)
        half = tick * (self._spec_bracket_ticks(spec) / 2.0)
        buy_px = _round_px(mid - half, pd)
        sell_px = _round_px(mid + half, pd)
        if sell_px <= buy_px:
            buy_px = _round_px(mid - tick, pd)
            sell_px = _round_px(mid + tick, pd)
        if buy_px <= 0.0 or sell_px <= buy_px:
            return None
        return buy_px, sell_px

    def _depth_open_price(
        self,
        book_id: int,
        spec: OpenBookSpec,
        state,
        pd: int,
    ) -> float | None:
        if state is None:
            return None
        book = (getattr(state, "books", None) or {}).get(book_id)
        if book is None:
            return None
        thresh = spec.depth_qty if spec.depth_qty is not None else self.open_depth_qty
        best_bid, best_ask = self._best_bid_ask(book)
        tick = _tick(pd)
        if spec.open_direction == OrderDirection.SELL:
            px = depth_ask_open_price(book, thresh, self.open_depth_levels)
            if px is None or px <= 0.0:
                return None
            px = _round_px(px, pd)
            if best_bid is not None and px <= best_bid + 1e-12:
                px = _round_px(best_bid + tick, pd)
                if px <= best_bid + 1e-12:
                    if best_ask is not None and best_ask > 0.0:
                        px = _round_px(best_ask, pd)
                    else:
                        return None
            return px

        px = depth_bid_open_price(book, thresh, self.open_depth_levels)
        if px is None or px <= 0.0:
            return None
        px = _round_px(px, pd)
        if best_ask is not None and px >= best_ask - 1e-12:
            px = _round_px(best_ask - tick, pd)
            if px <= 0.0 or (best_ask is not None and px >= best_ask - 1e-12):
                if best_bid is not None and best_bid > 0.0:
                    px = _round_px(best_bid, pd)
                else:
                    return None
        return px

    def _resolve_open_price(
        self,
        book_id: int,
        spec: OpenBookSpec,
        state,
        pd: int,
    ) -> tuple[float | None, bool, bool]:
        """Return (price, from_depth, from_mid)."""
        if self._open_bracket_prices_locked(spec):
            is_sell = spec.open_direction == OrderDirection.SELL
            if is_sell:
                return _round_px(spec.close_price, pd), False, False
            return _round_px(spec.open_price, pd), False, False
        if spec.open_price > 0.0:
            return _round_px(spec.open_price, pd), False, False
        if self._book_use_depth(spec):
            px = self._depth_open_price(book_id, spec, state, pd)
            if px is None:
                return None, False, False
            return px, True, False
        if state is None:
            return None, False, False
        book = (getattr(state, "books", None) or {}).get(book_id)
        if book is None:
            return None, False, False
        px = mid_open_price(book, spec.open_direction, pd)
        if px is None or px <= 0.0:
            return None, False, False
        return px, False, True

    def _open_bracket_prices_locked(self, spec: OpenBookSpec) -> bool:
        return (
            spec.open_price > 0.0
            and spec.close_price > 0.0
            and spec.close_price > spec.open_price + 1e-12
        )

    def _resolve_open_bracket_buy_sell(
        self,
        book_id: int,
        spec: OpenBookSpec,
        state,
        pd: int,
    ) -> tuple[float, float] | None:
        """BUY leg (open_price) and SELL leg (close_price); snapshot when not locked."""
        if self._open_bracket_prices_locked(spec):
            return (
                _round_px(spec.open_price, pd),
                _round_px(spec.close_price, pd),
            )

        is_sell = spec.open_direction == OrderDirection.SELL
        use_depth = self._book_use_depth(spec)

        if use_depth and spec.open_price > 0.0 and spec.close_price <= 0.0:
            post_px = _round_px(spec.open_price, pd)
            close_px = self._bracket_close_price(post_px, is_sell, pd, spec)
            if is_sell:
                return _round_px(close_px, pd), post_px
            return post_px, _round_px(close_px, pd)

        if use_depth:
            px, _from_depth = self._resolve_open_price(book_id, spec, state, pd)
            if px is None or px <= 0.0:
                return None
            post_px = self._effective_open_post_price(px, spec, state, book_id, pd)
            close_px = self._bracket_close_price(post_px, is_sell, pd, spec)
            if is_sell:
                return _round_px(close_px, pd), post_px
            return post_px, _round_px(close_px, pd)

        pair = self._mid_bracket_prices(book_id, spec, state, pd)
        if pair is None:
            return None
        return pair

    def _sync_bracket_prices_in_memory(
        self, book_id: int, buy_px: float, sell_px: float, pd: int
    ) -> None:
        spec = self.open_books.get(int(book_id))
        if spec is None:
            return
        buy_r = _round_px(buy_px, pd)
        sell_r = _round_px(sell_px, pd)
        if spec.open_price == buy_r and spec.close_price == sell_r:
            return
        self.open_books[int(book_id)] = replace(
            spec, open_price=buy_r, close_price=sell_r
        )

    def _lock_open_bracket_prices(
        self,
        book_id: int,
        buy_px: float,
        sell_px: float,
        pd: int,
        *,
        use_depth: bool,
    ) -> None:
        self._sync_bracket_prices_in_memory(book_id, buy_px, sell_px, pd)
        self._persist_open_bracket_prices_to_live_json(
            book_id, buy_px, sell_px, pd, use_depth=use_depth
        )

    def _live_json_open_price(self, book_id: int) -> float:
        """Read open_price for book from live JSON (0 if missing/unreadable)."""
        path = self.live_config_path
        if not path.is_file():
            return 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0.0
        for row in data.get("open_books") or []:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get("book", -1)) == int(book_id):
                    return float(row.get("open_price", row.get("price", 0.0)) or 0.0)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _live_json_close_price(self, book_id: int) -> float:
        path = self.live_config_path
        if not path.is_file():
            return 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return 0.0
        for row in data.get("open_books") or []:
            if not isinstance(row, dict):
                continue
            try:
                if int(row.get("book", -1)) == int(book_id):
                    return float(row.get("close_price", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _persist_open_bracket_prices_to_live_json(
        self,
        book_id: int,
        buy_px: float,
        sell_px: float,
        pd: int,
        *,
        use_depth: bool,
    ) -> None:
        """Write locked BUY leg (open_price) and SELL leg (close_price) to live JSON."""
        if not self.live_config or not self.live_config_path.is_file():
            return
        bid = int(book_id)
        buy_r = _round_px(buy_px, pd)
        sell_r = _round_px(sell_px, pd)
        if buy_r <= 0.0 or sell_r <= buy_r:
            return
        tick = _tick(pd)
        prev = self._last_persisted_open_px.get(bid)
        if prev is not None and abs(prev - buy_r) < tick * 0.51:
            prev_close = self._last_persisted_close_px.get(bid)
            if prev_close is not None and abs(prev_close - sell_r) < tick * 0.51:
                return
        had_buy = self._live_json_open_price(bid)
        had_sell = self._live_json_close_price(bid)

        path = self.live_config_path

        def _mutator(data: dict) -> bool:
            for row in data.get("open_books") or []:
                if not isinstance(row, dict):
                    continue
                try:
                    if int(row.get("book", -1)) != bid:
                        continue
                except (TypeError, ValueError):
                    continue
                try:
                    cur_buy = float(row.get("open_price", row.get("price", 0.0)) or 0.0)
                    cur_sell = float(row.get("close_price", 0.0) or 0.0)
                except (TypeError, ValueError):
                    cur_buy, cur_sell = 0.0, 0.0
                if (
                    cur_buy > 0.0
                    and cur_sell > 0.0
                    and abs(cur_buy - buy_r) < tick * 0.51
                    and abs(cur_sell - sell_r) < tick * 0.51
                ):
                    return False
                if use_depth and cur_buy <= 0.0:
                    row["use_depth_qty"] = True
                row["open_price"] = buy_r
                row["close_price"] = sell_r
                return True
            return False

        try:
            if fcntl is not None:
                with path.open("r+", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        raw = f.read()
                        try:
                            data = json.loads(raw) if raw.strip() else {}
                        except json.JSONDecodeError as exc:
                            bt.logging.warning(
                                f"TopAgent bracket persist book={bid}: JSON parse failed: {exc}"
                            )
                            return
                        if not isinstance(data, dict):
                            return
                        if not _mutator(data):
                            return
                        payload = json.dumps(data, indent=2) + "\n"
                        f.seek(0)
                        f.truncate(0)
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                raw = path.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw) if raw.strip() else {}
                except json.JSONDecodeError as exc:
                    bt.logging.warning(
                        f"TopAgent bracket persist book={bid}: JSON parse failed: {exc}"
                    )
                    return
                if not isinstance(data, dict):
                    return
                if not _mutator(data):
                    return
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, path)
            self._live_config_mtime = path.stat().st_mtime
            self._last_persisted_open_px[bid] = buy_r
            self._last_persisted_close_px[bid] = sell_r
            action = "locked"
            if had_buy > 0.0 and (
                abs(had_buy - buy_r) >= tick * 0.51
                or abs(had_sell - sell_r) >= tick * 0.51
            ):
                action = "corrected"
            bt.logging.info(
                f"TopAgent book {bid}: {action} open_price={buy_r} "
                f"close_price={sell_r} into {path.name}"
            )
        except OSError as exc:
            bt.logging.warning(f"TopAgent bracket persist book={bid}: {exc}")

    def _persist_cancel_all_orders_off(self) -> None:
        """One-shot cancel_all_orders: write 0 back to live JSON after cancel tick."""
        if not self.live_config or not self.live_config_path.is_file():
            return
        path = self.live_config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            if not _parse_bool(data.get("cancel_all_orders"), default=False):
                return
            data["cancel_all_orders"] = 0
            path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self._live_config_mtime = path.stat().st_mtime
            bt.logging.info(f"TopAgent cancel_all_orders: cleared flag in {path.name}")
        except (OSError, json.JSONDecodeError) as exc:
            bt.logging.warning(f"TopAgent cancel_all_orders persist: {exc}")

    def log_trade_event(self, event, state):
        """Append trades.csv on a background thread (never block handle)."""
        try:
            from trades_csv_recorder import enqueue_trade_csv_row

            if enqueue_trade_csv_row(self, event, state):
                return
        except Exception:
            pass
        super().log_trade_event(event, state)

    def update(self, state):
        self._fifo_ctx_state = state
        try:
            self._last_state_ts = int(getattr(state, "timestamp", 0) or 0)
        except (TypeError, ValueError):
            self._last_state_ts = 0
        return super().update(state)

    def _record_buy_fill(self, book_id: int, qty: float, price: float, fee: float) -> None:
        st = self._fill_state(book_id)
        st.qty += qty
        st.cost += qty * price
        st.open_fees += abs(fee)

    def _record_sell_fill(self, book_id: int, qty: float) -> None:
        st = self._fill_state(book_id)
        if st.qty <= 1e-12:
            return
        take = min(qty, st.qty)
        ratio = take / st.qty
        st.qty -= take
        st.cost -= st.cost * ratio
        st.open_fees -= st.open_fees * ratio

    def onTrade(self, event: TradeEvent, validator: str = None) -> None:
        book_id = event.bookId
        if book_id is None:
            return
        self._record_open_book_trade_fill(int(book_id), event)
        uid = self.uid
        qty = float(event.quantity)
        price = float(event.price)
        if event.takerAgentId == uid and event.side == 0:
            self._record_buy_fill(book_id, qty, price, float(event.takerFee))
        elif event.makerAgentId == uid and event.side == 1:
            self._record_buy_fill(book_id, qty, price, float(event.makerFee))
        elif event.takerAgentId == uid and event.side == 1:
            self._record_sell_fill(book_id, qty)
        elif event.makerAgentId == uid and event.side == 0:
            self._record_sell_fill(book_id, qty)

        try:
            from fifo_state_recorder import record_trade_event

            record_trade_event(
                self,
                event,
                state=getattr(self, "_fifo_ctx_state", None),
                validator=validator,
                state_timestamp=int(getattr(event, "timestamp", 0) or 0)
                or int(getattr(self, "_last_state_ts", 0) or 0),
            )
        except Exception:
            pass

    def _bracket_close_price(
        self,
        open_px: float,
        is_sell: bool,
        pd: int,
        spec: OpenBookSpec | None = None,
    ) -> float:
        """Depth mode: ±1.0. Mid mode: the other mid ± bracket_ticks/2 side."""
        if spec is not None and not spec.use_depth_qty:
            width = _tick(pd) * self._spec_bracket_ticks(spec)
            if is_sell:
                return _round_px(open_px - width, pd)
            return _round_px(open_px + width, pd)
        if is_sell:
            return _round_px(open_px - 1.0, pd)
        return _round_px(open_px + 1.0, pd)

    def _refresh_open_bracket_side(
        self,
        response,
        book_id: int,
        direction: OrderDirection,
        price: float,
        qty: float,
        pd: int,
        min_sz: float,
    ) -> bool:
        """Keep one GTT rest at price. Cancel-and-wait if the rest is stale."""
        if price <= 0.0 or qty < min_sz - 1e-12:
            return False
        side = self._order_side(direction)
        resting = self._resting_side_price(book_id, side, pd)
        if resting is not None:
            if abs(resting - price) < _tick(pd) * 0.51:
                self._requote_streak.pop((int(book_id), side), None)
                return False
            self._note_requote(book_id, side, price, resting)
            self._cancel_resting_side(response, book_id, side)
            return False
        return self._place_gtt_limit(
            response,
            book_id=book_id,
            direction=direction,
            quantity=qty,
            price=price,
        )

    def _open_side_fills(self, book_id: int) -> OpenBookSideFills:
        bid = int(book_id)
        st = self._open_book_side_fills.get(bid)
        if st is None:
            st = OpenBookSideFills()
            self._open_book_side_fills[bid] = st
        return st

    def _trade_is_own_buy(self, event: TradeEvent) -> bool | None:
        """True=BUY, False=SELL, None=not our fill."""
        uid = int(self.uid)
        if event.takerAgentId == uid and int(event.side) == 0:
            return True
        if event.makerAgentId == uid and int(event.side) == 1:
            return True
        if event.takerAgentId == uid and int(event.side) == 1:
            return False
        if event.makerAgentId == uid and int(event.side) == 0:
            return False
        return None

    def _open_fills_epoch_for(self, book_id: int) -> int:
        """Highest tradeId from this book's previous life in open_books.

        A restored book starts a fresh cap: fills from before the restore must
        not count, or the replayed history retires it again on the next tick.
        """
        epoch = getattr(self, "_open_fills_epoch", None)
        if not epoch:
            return 0
        return int(epoch.get(int(book_id), 0) or 0)

    def _apply_open_book_fill(
        self, book_id: int, trade_id: int, is_buy: bool, qty: float
    ) -> bool:
        """Count one own fill toward open-book caps (deduped by book, tradeId)."""
        bid = int(book_id)
        if int(trade_id) <= self._open_fills_epoch_for(bid):
            return False
        key = (bid, int(trade_id))
        if key in self._open_fill_trade_keys:
            return False
        self._open_fill_trade_keys.add(key)
        st = self._open_side_fills(bid)
        if is_buy:
            st.buy += qty
        else:
            st.sell += qty
        return True

    def _session_trades_csv(self) -> Path | None:
        state = getattr(self, "_fifo_ctx_state", None)
        if state is None:
            return None
        try:
            path = Path(self.simulation_output_dir(state)) / "trades.csv"
        except Exception:
            return None
        return path if path.is_file() else None

    def _max_trade_id_for_book(self, book_id: int) -> int:
        """Highest tradeId on this book in the session tape, or 0."""
        path = self._session_trades_csv()
        if path is None:
            return 0
        import csv

        bid = int(book_id)
        top = 0
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        if int(float(row["bookId"])) != bid:
                            continue
                        top = max(top, int(float(row["tradeId"])))
                    except (KeyError, TypeError, ValueError):
                        continue
        except OSError as exc:
            bt.logging.warning(f"TopAgent trade epoch read: {exc}")
            return 0
        return top

    def _seed_open_book_side_fills(self, book_id: int) -> None:
        """Replay session trades.csv so restart does not forget fill caps."""
        st = self._open_side_fills(book_id)
        if st.seeded_from_csv:
            return
        st.seeded_from_csv = True
        path = self._session_trades_csv()
        if path is None:
            return
        import csv

        uid = int(self.uid)
        bid = int(book_id)
        added = 0
        try:
            with path.open(newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        if int(float(row["bookId"])) != bid:
                            continue
                        trade_id = int(float(row["tradeId"]))
                        taker_id = int(float(row["takerAgentId"]))
                        maker_id = int(float(row["makerAgentId"]))
                        side = int(float(row["side"]))
                        qty = float(row["quantity"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if taker_id != uid and maker_id != uid:
                        continue
                    is_buy = (taker_id == uid and side == 0) or (
                        maker_id == uid and side == 1
                    )
                    if self._apply_open_book_fill(bid, trade_id, is_buy, qty):
                        added += 1
        except OSError as exc:
            bt.logging.warning(f"TopAgent open fill seed book={bid}: {exc}")
            return
        if added:
            bt.logging.info(
                f"TopAgent open fill seed book={bid} buy={st.buy:.4f} "
                f"sell={st.sell:.4f} trades={added}"
            )

    def _record_open_book_trade_fill(self, book_id: int, event: TradeEvent) -> None:
        if int(book_id) not in self.open_books:
            return
        is_buy = self._trade_is_own_buy(event)
        if is_buy is None:
            return
        try:
            trade_id = int(event.tradeId)
        except (TypeError, ValueError, AttributeError):
            return
        self._seed_open_book_side_fills(book_id)
        self._apply_open_book_fill(int(book_id), trade_id, is_buy, float(event.quantity))

    def _open_book_side_room(
        self, spec: OpenBookSpec, book_id: int, direction: OrderDirection
    ) -> float:
        self._seed_open_book_side_fills(book_id)
        st = self._open_side_fills(book_id)
        filled = st.buy if direction == OrderDirection.BUY else st.sell
        return max(0.0, float(spec.max_quantity) - filled)

    def _open_bracket_order_qty(
        self,
        acct,
        direction: OrderDirection,
        price: float,
        room: float,
        clip: float,
        vd: int,
        min_sz: float,
    ) -> float:
        if room < min_sz - 1e-12 or price <= 0.0:
            return 0.0
        if direction == OrderDirection.BUY:
            quote_free = float(acct.quote_balance.free)
            available_qty = quote_free / price if quote_free > 0.0 else 0.0
        else:
            base_free = float(acct.base_balance.free)
            available_qty = base_free if base_free > 0.0 else 0.0
        return round(min(clip, room, max(0.0, available_qty - 1.0)), vd)

    def _open_book_spec_to_json_row(self, spec: OpenBookSpec) -> dict:
        row: dict = {
            "book": spec.book,
            "open_price": spec.open_price,
            "max_quantity": spec.max_quantity,
            "use_depth_qty": spec.use_depth_qty,
        }
        if spec.close_price > 0.0:
            row["close_price"] = spec.close_price
        if spec.close_price > 0.0:
            row["close_price"] = spec.close_price
        if spec.bracket_ticks is not None:
            row["bracket_ticks"] = spec.bracket_ticks
        if spec.depth_qty is not None:
            row["depth_qty"] = spec.depth_qty
        if spec.open_direction == OrderDirection.SELL:
            row["open_direction"] = "SELL"
        return row

    def _persist_open_books_to_live_json(self) -> None:
        if not self.live_config or not self.live_config_path.is_file():
            return
        path = self.live_config_path
        rows = [
            self._open_book_spec_to_json_row(s)
            for s in sorted(self.open_books.values(), key=lambda x: x.book)
        ]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            bt.logging.warning(f"TopAgent open_books persist read: {exc}")
            return
        if not isinstance(data, dict):
            return
        data["open_books"] = rows
        data["stop_books"] = sorted(self.stop_books)
        epoch = getattr(self, "_open_fills_epoch", None)
        if epoch:
            data["open_fills_epoch"] = {str(k): int(v) for k, v in sorted(epoch.items())}
        payload = json.dumps(data, indent=2) + "\n"
        try:
            if fcntl is not None:
                with path.open("r+", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        f.seek(0)
                        f.truncate(0)
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                tmp = path.with_suffix(path.suffix + ".tmp")
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, path)
            self._live_config_mtime = path.stat().st_mtime
        except OSError as exc:
            bt.logging.warning(f"TopAgent open_books persist write: {exc}")

    def _validator_base_drift(self, acct) -> float | None:
        int_base = self._account_int_base(acct)
        if int_base is None or acct is None:
            return None
        try:
            free_base = float(acct.base_balance.free)
        except (TypeError, ValueError):
            return None
        return free_base - float(int_base)

    def _open_book_inventory_dust(
        self, book_id: int, acct, state
    ) -> float:
        """Absolute inventory drift after open session (validator or FIFO)."""
        drift = self._validator_base_drift(acct)
        if drift is not None:
            return abs(float(drift))
        return abs(self._fifo_net_qty(book_id, state))

    def _open_fill_cap_done(
        self, spec: OpenBookSpec, book_id: int, min_sz: float
    ) -> bool:
        self._seed_open_book_side_fills(book_id)
        st = self._open_side_fills(book_id)
        max_q = float(spec.max_quantity)
        tol = max(float(min_sz), 1e-9)
        return st.buy >= max_q - tol and st.sell >= max_q - tol

    def _retire_open_book(
        self,
        book_id: int,
        *,
        to_stop: bool = False,
        dust: float = 0.0,
        response=None,
    ) -> None:
        bid = int(book_id)
        spec = self.open_books.pop(bid, None)
        if spec is None:
            return
        if response is not None:
            self._cancel_all_resting_on_book(response, bid)
        st = self._open_side_fills(bid)
        self._open_book_side_fills.pop(bid, None)
        if to_stop:
            self.stop_books.add(bid)
        bt.logging.info(
            f"TopAgent open book {bid} retired: buy_filled={st.buy:.4f} "
            f"sell_filled={st.sell:.4f} max={spec.max_quantity} "
            f"dust={dust:.6f} stop={to_stop}"
        )
        self._persist_open_books_to_live_json()

    def _cancel_resting_side(self, response, book_id: int, side: int) -> bool:
        """Cancel all resting orders on one side; return True if any sent."""
        acct = self.accounts.get(book_id)
        if not acct or not acct.orders:
            return False
        ids = [o.id for o in acct.orders if o.side == side]
        if not ids:
            return False
        if len(ids) == 1:
            response.cancel_order(book_id, ids[0])
        else:
            response.cancel_orders(book_id, ids)
        self._clear_pending_limit(book_id, side)
        return True

    def _maybe_open(self, response, book_id: int, spec: OpenBookSpec, pd: int, vd: int) -> None:
        if (
            book_id in self.market_close_books
            or book_id in self.flatten_books
            or book_id in self.stop_books
        ):
            return
        acct = self._account_for_book(book_id)
        if acct is None:
            self._log_open_skip_once(
                book_id, "no account on this state update (lazy accounts)"
            )
            return
        state = getattr(self, "_fifo_ctx_state", None)
        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        self._seed_open_book_side_fills(book_id)
        if self._open_fill_cap_done(spec, book_id, min_sz):
            dust = self._open_book_inventory_dust(book_id, acct, state)
            self._retire_open_book(
                book_id, to_stop=True, dust=dust, response=response
            )
            return

        is_sell = spec.open_direction == OrderDirection.SELL
        use_depth = self._book_use_depth(spec)
        bracket = self._resolve_open_bracket_buy_sell(book_id, spec, state, pd)
        if bracket is None:
            self._log_open_skip_once(
                book_id,
                "no open bracket (missing book, one-sided BBO, or depth snapshot)",
            )
            return
        buy_px, sell_px = bracket
        if not self._open_bracket_prices_locked(spec):
            self._lock_open_bracket_prices(
                book_id, buy_px, sell_px, pd, use_depth=use_depth
            )
            spec = self.open_books[int(book_id)]
            buy_px = _round_px(spec.open_price, pd)
            sell_px = _round_px(spec.close_price, pd)
        post_px = sell_px if is_sell else buy_px
        close_px = buy_px if is_sell else sell_px
        if post_px <= 0.0 or close_px <= 0.0:
            return

        close_direction = OrderDirection.BUY if is_sell else OrderDirection.SELL

        clip = round(min(self.open_quantity, spec.max_quantity), vd)
        open_room = self._open_book_side_room(spec, book_id, spec.open_direction)
        close_room = self._open_book_side_room(spec, book_id, close_direction)
        open_qty = self._open_bracket_order_qty(
            acct, spec.open_direction, post_px, open_room, clip, vd, min_sz
        )
        close_qty = self._open_bracket_order_qty(
            acct, close_direction, close_px, close_room, clip, vd, min_sz
        )

        if open_qty >= min_sz - 1e-12:
            self._refresh_open_bracket_side(
                response, book_id, spec.open_direction, post_px, open_qty, pd, min_sz
            )

        if close_qty >= min_sz - 1e-12:
            self._refresh_open_bracket_side(
                response, book_id, close_direction, close_px, close_qty, pd, min_sz
            )

    def _resolve_close_qty(
        self,
        net_qty: float,
        held: float,
        vd: int,
        min_sz: float,
    ) -> float:
        """close_quantity per tick, or full remainder when |fifo net| below dust threshold."""
        cap = abs(net_qty)
        if cap < self.close_dust_threshold - 1e-12:
            qty = min(cap, held)
        else:
            qty = min(self.close_quantity, cap, held)
        return round(qty, vd)

    def _cancel_all_resting_close(
        self,
        response,
        acct,
        book_id: int,
        *,
        side: int,
    ) -> None:
        """Cancel every resting order on the close side and clear pending guard."""
        for o in acct.orders:
            if o.side != side:
                continue
            response.cancel_order(book_id, o.id)
        self._clear_pending_limit(book_id, side)

    def _cancel_all_resting_on_book(self, response, book_id: int) -> int:
        """Cancel every confirmed resting order on one book; return cancel count."""
        bid = int(book_id)
        acct = self.accounts.get(bid)
        if acct is None:
            acct = self.accounts.get(str(bid))
        if acct is None or not getattr(acct, "orders", None):
            return 0
        ids = [o.id for o in acct.orders]
        if not ids:
            return 0
        if len(ids) == 1:
            response.cancel_order(bid, ids[0])
        else:
            response.cancel_orders(bid, ids)
        for o in acct.orders:
            try:
                self._clear_pending_limit(bid, int(o.side))
            except Exception:
                pass
        return len(ids)

    def _cancel_all_resting_orders(self, response) -> int:
        """Cancel every confirmed resting order on all books; return cancel count."""
        accounts = getattr(self, "accounts", None)
        if not isinstance(accounts, dict):
            return 0
        total = 0
        for book_id, acct in accounts.items():
            try:
                bid = int(book_id)
            except (TypeError, ValueError):
                continue
            if acct is None or not acct.orders:
                continue
            ids = [o.id for o in acct.orders]
            if not ids:
                continue
            if len(ids) == 1:
                response.cancel_order(bid, ids[0])
            else:
                response.cancel_orders(bid, ids)
            for o in acct.orders:
                self._clear_pending_limit(bid, int(o.side))
            total += len(ids)
        return total

    def _place_market_close(
        self,
        response,
        *,
        book_id: int,
        direction: OrderDirection,
        quantity: float,
    ) -> bool:
        """Market close slice; never rests on the book."""
        if quantity <= 0.0:
            return False
        slip = float(getattr(self, "market_close_max_slippage", 0.01) or 0.01)
        response.market_order(
            book_id=book_id,
            direction=direction,
            quantity=quantity,
            currency=OrderCurrency.BASE,
            stp=STP.CANCEL_BOTH,
            max_slippage=slip,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        return True

    def _maybe_market_close(
        self, response, book_id: int, pd: int, vd: int, state=None
    ) -> None:
        """Close close_quantity via market order when book is in market_close_books."""
        general = getattr(self, "general_books", None)
        if general and book_id in general:
            return
        acct = self.accounts.get(book_id)
        if acct is None:
            return

        fifo_net = self._fifo_net_qty(book_id, getattr(self, "_fifo_ctx_state", None))
        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        if abs(fifo_net) < min_sz - 1e-12:
            self._clear_close_pending(book_id)
            return

        sim_ts = int(getattr(self, "_last_state_ts", 0) or 0) or None
        if self._fok_close_on_cooldown(book_id, sim_ts):
            return

        if fifo_net > 0.0:
            close_side = 1
            held = float(acct.base_balance.free)
            close_qty = self._resolve_close_qty(fifo_net, held, vd, min_sz)
            if close_qty < min_sz - 1e-12:
                return
            self._cancel_all_resting_close(response, acct, book_id, side=close_side)
            if self._place_market_close(
                response,
                book_id=book_id,
                direction=OrderDirection.SELL,
                quantity=close_qty,
            ):
                self._record_fok_close_sent(book_id, sim_ts)
            return

        close_side = 0
        book = (getattr(state, "books", None) or {}).get(book_id) if state else None
        _, best_ask = self._best_bid_ask(book) if book is not None else (None, None)
        quote_free = float(acct.quote_balance.free)
        ref_px = float(best_ask) if best_ask is not None and best_ask > 0.0 else 0.0
        if ref_px <= 0.0:
            return
        affordable = quote_free / ref_px if ref_px > 0.0 else 0.0
        close_qty = self._resolve_close_qty(fifo_net, affordable - 1.0, vd, min_sz)
        if close_qty < min_sz - 1e-12:
            return
        self._cancel_all_resting_close(response, acct, book_id, side=close_side)
        if self._place_market_close(
            response,
            book_id=book_id,
            direction=OrderDirection.BUY,
            quantity=close_qty,
        ):
            self._record_fok_close_sent(book_id, sim_ts)

    def _engine_open_orders(self, acct) -> list[dict]:
        out: list[dict] = []
        for o in acct.orders or []:
            out.append(
                {
                    "id": o.id,
                    "direction": "SELL" if int(o.side) == 1 else "BUY",
                    "price": float(o.price) if o.price is not None else 0.0,
                    "quantity": float(getattr(o, "quantity", 0.0) or 0.0),
                    "side": int(o.side),
                }
            )
        return out

    def _account_int_base(self, acct) -> float | None:
        bb = getattr(acct, "base_balance", None)
        if bb is None:
            return None
        ib = getattr(bb, "initial", None)
        if ib is None and isinstance(bb, dict):
            ib = bb.get("initial", bb.get("i"))
        if ib is None:
            return None
        try:
            return float(ib)
        except (TypeError, ValueError):
            return None

    def _account_position_base(self, acct) -> float:
        bb = getattr(acct, "base_balance", None)
        if bb is None:
            return 0.0
        try:
            total = float(getattr(bb, "total", 0.0) or 0.0)
        except (TypeError, ValueError):
            total = 0.0
        loan = float(getattr(acct, "base_loan", 0.0) or 0.0)
        coll = float(getattr(acct, "base_collateral", 0.0) or 0.0)
        return total - loan + coll

    def _flatten_engine_l2_levels(self, book) -> tuple[list, list]:
        bids_raw = getattr(book, "bids", None)
        asks_raw = getattr(book, "asks", None)
        if isinstance(book, dict):
            bids_raw = bids_raw or book.get("bids", book.get("b"))
            asks_raw = asks_raw or book.get("asks", book.get("a"))
        raw = getattr(book, "_raw", None)
        if isinstance(raw, dict):
            if not bids_raw:
                bids_raw = raw.get("b") or []
            if not asks_raw:
                asks_raw = raw.get("a") or []

        def _levels(side_raw) -> list:
            out: list = []
            for lvl in side_raw or []:
                px, qty = _level_px_qty(lvl)
                if px is None or qty is None or qty <= 0.0:
                    continue
                out.append({"price": px, "quantity": qty})
            return out

        return _levels(bids_raw), _levels(asks_raw)

    def _build_flatten_engine_book(
        self,
        book_id: int,
        book,
        acct,
        *,
        pd: int,
    ) -> dict | None:
        if acct is None:
            return None
        best_bid, best_ask = self._touch_best_bid_ask(book)
        bids, asks = self._flatten_engine_l2_levels(book)
        int_base = self._account_int_base(acct)
        payload: dict = {
            "id": int(book_id),
            "best_bid": float(best_bid or 0.0),
            "best_ask": float(best_ask or 0.0),
            "free_base": float(acct.base_balance.free),
            "free_quote": float(acct.quote_balance.free),
            "position": self._account_position_base(acct),
            "open_orders": self._engine_open_orders(acct),
            "bids": bids,
            "asks": asks,
            "tick": _tick(pd),
        }
        if int_base is not None:
            payload["int_base"] = int_base
            payload["neutral_position"] = int_base
            payload["base_balance_initial"] = int_base
        return payload

    def _build_flatten_engine_books(self, state, *, pd: int) -> list[dict]:
        books: list[dict] = []
        state_books = getattr(state, "books", None) or {}
        for book_id in sorted(self.flatten_books):
            book = state_books.get(book_id)
            if book is None:
                continue
            acct = self.accounts.get(book_id)
            payload = self._build_flatten_engine_book(book_id, book, acct, pd=pd)
            if payload is not None:
                books.append(payload)
        return books

    def _select_flatten_engine_books(
        self, books: list[dict], sim_ts: int | None
    ) -> list[dict]:
        cap = max(1, int(getattr(self, "flatten_max_books_per_tick", 15)))
        if len(books) <= cap:
            return books
        ordered = sorted(books, key=lambda b: int(b.get("id", 0)))
        ts = int(sim_ts or 0)
        step = max(1, len(ordered) - cap + 1)
        offset = (ts // 1_000_000_000) % step
        rotated = ordered[offset:] + ordered[:offset]
        return rotated[:cap]

    def _build_flatten_engine_input(self, state, *, pd: int, vd: int, min_sz: float, sim_ts: int | None) -> dict:
        books = self._build_flatten_engine_books(state, pd=pd)
        total = len(books)
        books = self._select_flatten_engine_books(books, sim_ts)
        return {
            "price_decimals": pd,
            "volume_decimals": vd,
            "exchange_min_quantity": min_sz,
            "flatten_books": sorted(self.flatten_books),
            "flatten_max_books_per_tick": int(self.flatten_max_books_per_tick),
            "flatten_flat_qty": float(self.flatten_flat_qty),
            "books": books,
            "_flatten_books_total": total,
        }

    def _apply_flatten_engine_orders(
        self,
        response,
        engine_result: dict,
        *,
        sim_ts: int | None,
    ) -> None:
        if engine_result.get("error"):
            bt.logging.warning(
                f"TopAgent flatten engine error: {engine_result['error']}"
            )

        slip_default = float(getattr(self, "flatten_max_slippage", 0.02) or 0.02)

        for order in engine_result.get("orders", []) or []:
            order_type = str(order.get("type", "limit")).lower()
            book_id = int(order["book_id"])
            if order_type == "cancel":
                order_id = int(order["order_id"])
                acct = self.accounts.get(book_id)
                if acct is None or not acct.orders:
                    continue
                if not any(o.id == order_id for o in acct.orders):
                    continue
                side = 0
                for o in acct.orders:
                    if o.id == order_id:
                        side = int(o.side)
                        break
                response.cancel_order(book_id, order_id)
                self._clear_pending_limit(book_id, side)
                continue

            direction_name = str(order.get("direction", "")).upper()
            if direction_name == "BUY":
                direction = OrderDirection.BUY
            elif direction_name == "SELL":
                direction = OrderDirection.SELL
            else:
                continue

            quantity = float(order["quantity"])
            if quantity <= 0.0:
                continue

            if order_type == "market":
                slip_raw = order.get("max_slippage")
                slip = float(slip_raw) if slip_raw is not None else slip_default
                if slip > slip_default + 1e-12:
                    slip = slip_default
                response.market_order(
                    book_id=book_id,
                    direction=direction,
                    quantity=quantity,
                    currency=OrderCurrency.BASE,
                    stp=STP.CANCEL_BOTH,
                    max_slippage=slip,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
                self._clear_close_pending(book_id)
                continue

            if order_type != "limit":
                continue

            price = float(order.get("price") or 0.0)
            if price <= 0.0:
                continue
            tif_name = str(order.get("timeInForce", "GTC")).upper()
            if tif_name == "FOK":
                self._place_fok_close_limit(
                    response,
                    book_id=book_id,
                    direction=direction,
                    quantity=quantity,
                    price=price,
                )
            else:
                side = 1 if direction == OrderDirection.SELL else 0
                if self._place_gtc_close_limit(
                    response,
                    book_id=book_id,
                    direction=direction,
                    quantity=quantity,
                    price=price,
                ):
                    self._set_close_pending(book_id, side, sim_ts)

    def _maybe_retire_flatten_books(self, logs: list[dict]) -> None:
        try:
            from flatten_function import flatten_books_marked_flat
        except ImportError:
            return
        flat_ids = flatten_books_marked_flat(logs or [])
        if not flat_ids:
            return
        retired = [bid for bid in flat_ids if bid in self.flatten_books]
        if not retired:
            return
        self.flatten_books = {b for b in self.flatten_books if b not in set(retired)}
        bt.logging.info(f"TopAgent flatten retire flat books={retired}")
        self._persist_flatten_books()

    def _persist_live_json_fields(self, updates: dict) -> None:
        if not self.live_config or not self.live_config_path.is_file():
            return
        path = self.live_config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            bt.logging.warning(f"TopAgent live persist read: {exc}")
            return
        if not isinstance(data, dict):
            return
        data.update(updates)
        try:
            if fcntl is not None:
                with open(path, "r+", encoding="utf-8") as fh:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                    try:
                        fh.seek(0)
                        fh.write(json.dumps(data, indent=2) + "\n")
                        fh.truncate()
                    finally:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            else:
                path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self._live_config_mtime = path.stat().st_mtime
        except OSError as exc:
            bt.logging.warning(f"TopAgent live persist write: {exc}")

    def _persist_flatten_books(self) -> None:
        self._persist_live_json_fields({"flatten_books": sorted(self.flatten_books)})

    def _engaged_book_count(self) -> int:
        """Books that count toward the skill target: open now or already retired."""
        return len(set(self.open_books) | set(self.stop_books))

    def _acquire_candidates(self, state) -> list[tuple[float, int]]:
        """(touch_qty, book_id) for untouched books thin enough to recruit.

        Kappa needs at least 4 scored books, so when we hold fewer the agent
        recruits new ones. Thin books are the workable ones: a 100 lot can walk
        the touch out to a wide bracket leg, which is what manufactures alpha.
        """
        skip = (
            set(self.open_books)
            | set(self.stop_books)
            | set(self.flatten_books)
            | set(self.market_close_books)
            | set(self.general_books)
        )
        cap = float(self.acquire_max_touch_qty)
        out: list[tuple[float, int]] = []
        for raw_id, book in (getattr(state, "books", None) or {}).items():
            try:
                book_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if book_id in skip:
                continue
            bid_px, bid_qty, ask_px, ask_qty = self._touch_quote(book)
            if not bid_px or not ask_px or bid_qty is None or ask_qty is None:
                continue
            touch_qty = max(float(bid_qty), float(ask_qty))
            if touch_qty >= cap:
                continue
            out.append((touch_qty, book_id))
        out.sort()
        return out

    def _maybe_acquire_open_books(self, state) -> int:
        """Open one new book per tick while fewer than target_book_count are engaged.

        Counts `open_books | stop_books` — a retired book keeps the alpha it
        earned, so it still counts toward the target and must not be replaced.
        """
        if not getattr(self, "acquire_open_books", False):
            return 0
        target = int(self.target_book_count)
        if self._engaged_book_count() >= target:
            return 0
        candidates = self._acquire_candidates(state)
        if not candidates:
            return 0

        touch_qty, book_id = candidates[0]
        self.open_books[book_id] = OpenBookSpec(
            book=book_id,
            open_price=0.0,
            max_quantity=float(self.acquire_quantity),
            close_price=0.0,
            use_depth_qty=False,
            bracket_ticks=int(self.acquire_bracket_ticks),
        )
        # A never-opened book has no prior fills, but the tape may still hold
        # general/market-close trades on it; start the cap from now regardless.
        self._open_fills_epoch[book_id] = self._max_trade_id_for_book(book_id)
        self._open_book_side_fills.pop(book_id, None)
        self._open_fill_trade_keys = {
            key for key in self._open_fill_trade_keys if key[0] != book_id
        }
        self._open_skip_logged.discard(book_id)
        sim_ts = int(getattr(self, "_last_state_ts", 0) or 0)
        if sim_ts > 0:
            self._open_started_ts[book_id] = sim_ts
        bt.logging.info(
            f"TopAgent acquire book {book_id}: touch_qty={touch_qty:.4f} "
            f"engaged={self._engaged_book_count()}/{target} "
            f"epoch={self._open_fills_epoch[book_id]} "
            f"bracket_ticks={self.acquire_bracket_ticks} "
            f"qty={self.acquire_quantity}"
        )
        self._persist_open_books_to_live_json()
        return 1

    def _read_baseline_file(self, path) -> dict | None:
        """Parse a BookManageBot feed file, cached until its mtime moves."""
        if path is None or not path.is_file():
            return None
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        key = str(path)
        if self._alpha_baseline_mtime.get(key) != mtime:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                bt.logging.warning(f"TopAgent alpha baseline read: {exc}")
                data = None
            self._alpha_baseline_mtime[key] = mtime
            self._alpha_baseline_cache[key] = data if isinstance(data, dict) else None

        data = self._alpha_baseline_cache.get(key)
        if not data:
            return None
        max_age = float(getattr(self, "alpha_baseline_max_age_sec", 900.0) or 900.0)
        if max_age > 0.0 and (time.time() - float(data.get("written_at") or 0.0)) > max_age:
            return None
        return data

    def _alpha_baseline_snapshot(self) -> tuple[float, dict[int, float]] | None:
        """(baseline, {book: alpha}) from the BookManageBot feed, or None.

        BookManageBot writes debeta_baseline.<uid>.json for the uids it is named
        with, and debeta_mean_alpha.json for the whole field: a miner only sees
        its own fills, but the scored baseline is a field-wide statistic. The
        public file is the fallback so an agent that BMB was never configured
        for still gets a baseline instead of the rule going dark. None means
        "no usable opinion" — missing file, no baseline yet, or a stale
        snapshot — and callers fall back to the touch gates alone.
        """
        data = self._read_baseline_file(getattr(self, "alpha_baseline_path", None))
        alphas_raw = (data or {}).get("alphas")
        if data is None or alphas_raw is None:
            public = self._read_baseline_file(
                getattr(self, "alpha_baseline_public_path", None)
            )
            if public is not None:
                data = public
                alphas_raw = (public.get("alphas_by_uid") or {}).get(str(int(self.uid)))
        if data is None:
            return None
        try:
            baseline = float(data["baseline"])
        except (KeyError, TypeError, ValueError):
            return None
        if baseline <= 0.0:
            return None
        alphas: dict[int, float] = {}
        for key, val in (alphas_raw or {}).items():
            try:
                alphas[int(key)] = float(val)
            except (TypeError, ValueError):
                continue
        return baseline, alphas

    def _restore_gate(
        self, state, book_id: int, baseline: float | None, alpha: float | None
    ) -> tuple[bool, str, str]:
        """(may_restore, code, detail) for one stopped book.

        Re-arm a book when the touch is wide enough to quote a bracket inside
        and thin enough that our size sets the print rather than queueing behind
        resting depth. When the BookManageBot feed is up, a book whose |alpha|
        already clears the field baseline is left alone — it is not the one
        holding kappa back. The feed only covers the uids BMB is configured for,
        so without it the spread and touch decide on their own rather than the
        rule going dark. The code identifies the failing gate so a rule that
        never fires can still be diagnosed without logging every tick.
        """
        scored = baseline is not None and alpha is not None
        if scored and abs(alpha) >= baseline:
            return False, "scored", f"alpha={alpha:.2f} >= baseline={baseline:.2f}"
        book = self._book_from_state(state, book_id)
        if book is None:
            return False, "no_book", "not in state"
        bid_px, bid_qty, ask_px, ask_qty = self._touch_quote(book)
        if not bid_px or not ask_px or bid_qty is None or ask_qty is None:
            return False, "no_quote", "no touch quote"
        spread = float(ask_px) - float(bid_px)
        touch_qty = max(float(bid_qty), float(ask_qty))
        alpha_note = (
            f"alpha={alpha:.2f} baseline={baseline:.2f} " if scored else "alpha=n/a "
        )
        detail = f"{alpha_note}spread={spread:.4f} touch_qty={touch_qty:.4f}"
        if spread < float(self.restore_min_spread):
            return False, "spread", f"{detail} (need spread >= {self.restore_min_spread})"
        if touch_qty >= float(self.restore_max_touch_qty):
            return False, "touch_qty", f"{detail} (need < {self.restore_max_touch_qty})"
        return True, "ok", detail

    def _log_restore_block(self, book_id: int, code: str, detail: str) -> None:
        """Log a blocked restore once per failing gate, not once per tick."""
        last = getattr(self, "_restore_block_logged", None)
        if last is None:
            last = self._restore_block_logged = {}
        if last.get(book_id) == code:
            return
        last[book_id] = code
        bt.logging.info(f"TopAgent restore book {book_id} blocked [{code}]: {detail}")

    def _maybe_restore_stop_books(self, fifo, min_sz: float, state) -> int:
        """Re-open one stopped book per tick when its touch is worth quoting into.

        Called early in respond_simulation; return value bumps fifo bootstrap count.
        One book per tick with a per-book cooldown, so a book that keeps failing
        its gate cannot churn stop → open → stop every tick. The alpha baseline
        only narrows the field when BookManageBot publishes one for this uid;
        absent it the gate runs on spread and touch depth alone.
        """
        if not getattr(self, "restore_stop_books", False) or not self.stop_books:
            return 0
        snapshot = self._alpha_baseline_snapshot()
        baseline, alphas = snapshot if snapshot is not None else (None, {})
        sim_ts = int(getattr(self, "_last_state_ts", 0) or 0)
        cooldown_ns = int(
            float(getattr(self, "restore_cooldown_minutes", 30.0) or 0.0)
            * SIM_MINUTE_NS
        )
        last_ts = getattr(self, "_restore_last_ts", None)
        if last_ts is None:
            last_ts = self._restore_last_ts = {}

        for book_id in sorted(self.stop_books):
            if book_id in self.flatten_books or book_id in self.market_close_books:
                continue
            alpha = alphas.get(book_id)
            prev = int(last_ts.get(book_id, 0) or 0)
            if cooldown_ns > 0 and prev > 0 and sim_ts > 0:
                if (sim_ts - prev) < cooldown_ns:
                    continue
            ok, code, detail = self._restore_gate(state, book_id, baseline, alpha)
            if not ok:
                self._log_restore_block(book_id, code, detail)
                continue
            self._restore_block_logged.pop(book_id, None)

            self.stop_books.discard(book_id)
            self.open_books[book_id] = OpenBookSpec(
                book=book_id,
                open_price=0.0,
                max_quantity=float(self.restore_quantity),
                close_price=0.0,
                use_depth_qty=False,
                bracket_ticks=int(self.restore_bracket_ticks),
            )
            # Fresh cap: without an epoch the replayed history retires it at once.
            self._open_fills_epoch[book_id] = self._max_trade_id_for_book(book_id)
            self._open_book_side_fills.pop(book_id, None)
            self._open_fill_trade_keys = {
                key for key in self._open_fill_trade_keys if key[0] != book_id
            }
            self._open_skip_logged.discard(book_id)
            if sim_ts > 0:
                self._open_started_ts[book_id] = sim_ts
            last_ts[book_id] = sim_ts
            bt.logging.info(
                f"TopAgent restore book {book_id} stop→open: {detail} "
                f"epoch={self._open_fills_epoch[book_id]} "
                f"bracket_ticks={self.restore_bracket_ticks} "
                f"qty={self.restore_quantity}"
            )
            self._persist_open_books_to_live_json()
            return 1

        return 0

    def _run_flatten_engine_batch(
        self,
        response,
        state,
        *,
        pd: int,
        vd: int,
        min_sz: float,
        sim_ts: int | None,
    ) -> None:
        if not self.flatten_books:
            return
        from strategy_engine import process_topagent_flatten

        payload = self._build_flatten_engine_input(
            state, pd=pd, vd=vd, min_sz=min_sz, sim_ts=sim_ts
        )
        if not payload.get("books"):
            return
        t0 = time.perf_counter()
        try:
            engine_result = process_topagent_flatten(payload)
        except Exception as exc:
            bt.logging.error(f"TopAgent flatten engine failed: {exc}")
            return
        engine_elapsed = time.perf_counter() - t0
        orders = engine_result.get("orders") or []
        books_in = len(payload.get("books") or [])
        total = int(payload.get("_flatten_books_total") or books_in)
        if books_in > 0 or orders:
            bt.logging.info(
                f"TopAgent flatten engine total={total} batch={books_in} "
                f"orders={len(orders)} engine={engine_elapsed:.3f}s"
            )
        self._apply_flatten_engine_orders(response, engine_result, sim_ts=sim_ts)
        self._maybe_retire_flatten_books(engine_result.get("logs") or [])

    def _refresh_general_side(
        self,
        response,
        book_id: int,
        direction: OrderDirection,
        price: float,
        qty: float,
        pd: int,
        min_sz: float,
    ) -> None:
        side = self._order_side(direction)
        if qty < min_sz - 1e-12:
            if self._has_resting_side(book_id, side):
                self._cancel_resting_side(response, book_id, side)
            return
        resting = self._resting_side_price(book_id, side, pd)
        if resting is not None:
            if abs(resting - price) < _tick(pd) * 0.51:
                return
            self._cancel_resting_side(response, book_id, side)
            return
        self._place_gtc_close_limit(
            response,
            book_id=book_id,
            direction=direction,
            quantity=qty,
            price=price,
        )

    def _maybe_general_make(
        self,
        response,
        spec: GeneralBookSpec,
        pd: int,
        vd: int,
        state,
    ) -> None:
        book_id = int(spec.book)
        if book_id in self.flatten_books:
            return
        book = (getattr(state, "books", None) or {}).get(book_id)
        if book is None:
            return
        acct = self.accounts.get(book_id)
        if acct is None:
            return
        best_bid, best_ask = self._touch_best_bid_ask(book)
        if (
            best_bid is None
            or best_ask is None
            or best_bid <= 0.0
            or best_ask <= 0.0
        ):
            return
        mid = (float(best_bid) + float(best_ask)) / 2.0
        tick = _tick(pd)
        offset = tick * max(1, int(spec.ticks))
        buy_px = _round_px(mid - offset, pd)
        sell_px = _round_px(mid + offset, pd)
        buy_px = min(buy_px, _round_px(best_ask - tick, pd))
        sell_px = max(sell_px, _round_px(best_bid + tick, pd))
        if buy_px <= 0.0 or sell_px <= buy_px:
            return

        fifo_net = self._fifo_net_qty(book_id, state)
        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        clip = min(float(self.general_quantity), float(spec.max_quantity), 0.5)
        if clip <= 0.0:
            clip = 0.5
        cap = clip
        buy_room = cap - fifo_net
        sell_room = cap + fifo_net
        quote_free = float(acct.quote_balance.free)
        base_free = float(acct.base_balance.free)
        buy_avail = quote_free / buy_px if quote_free > 0.0 else 0.0
        sell_avail = base_free if base_free > 0.0 else 0.0
        haircut = 1.0 if clip >= 1.0 else 0.0
        buy_qty = round(min(clip, buy_room, max(0.0, buy_avail - haircut)), vd)
        sell_qty = round(min(clip, sell_room, max(0.0, sell_avail - haircut)), vd)
        open_max = float(getattr(self, "general_open_max", 0.25) or 0.25)
        if fifo_net > open_max:
            buy_qty = 0.0
        if fifo_net < -open_max:
            sell_qty = 0.0
        self._refresh_general_side(
            response, book_id, OrderDirection.BUY, buy_px, buy_qty, pd, min_sz
        )
        self._refresh_general_side(
            response, book_id, OrderDirection.SELL, sell_px, sell_qty, pd, min_sz
        )

    def respond_simulation(self, state):
        self._maybe_reload_live_config()
        self._fifo_ctx_state = state
        sim_ts: int | None = None
        try:
            sim_ts = int(getattr(state, "timestamp", 0) or 0)
            self._last_state_ts = sim_ts
        except (TypeError, ValueError):
            pass

        self._reconcile_pending_guard()
        self._pending_sides_this_tick.clear()

        pd = getattr(self.simulation_config, "priceDecimals", 8)
        vd = getattr(self.simulation_config, "volumeDecimals", 8)
        response = self.make_response()

        if self.cancel_all_orders:
            self._clear_all_close_pending()
            try:
                from waiting_orders_recorder import clear_all_pending

                clear_all_pending(self)
            except Exception:
                pass
            n = self._cancel_all_resting_orders(response)
            if n > 0:
                bt.logging.info(f"TopAgent cancel_all_orders: sent {n} cancels")
            self.cancel_all_orders = False
            self._persist_cancel_all_orders_off()
            self._register_pending_limits(response, sim_ts)
            return response

        bootstrapped = 0
        fifo = None
        try:
            from fifo_state_recorder import ensure_fifo, maybe_bootstrap_fifo_from_accounts

            fifo = ensure_fifo(self, state)
            bootstrapped = maybe_bootstrap_fifo_from_accounts(self, fifo, state)
        except Exception:
            pass

        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        if fifo is not None:
            restored = self._maybe_restore_stop_books(fifo, min_sz, state)
            if restored:
                bootstrapped = max(bootstrapped, restored)

        self._maybe_acquire_open_books(state)

        if self.flatten_books:
            self._run_flatten_engine_batch(
                response, state, pd=pd, vd=vd, min_sz=min_sz, sim_ts=sim_ts
            )

        for book_id in sorted(self.market_close_books):
            if book_id in self.general_books:
                continue
            if not self._book_in_state(state, book_id):
                continue
            self._maybe_market_close(response, book_id, pd, vd, state)

        for book_id, spec in sorted(self.general_books.items()):
            if book_id in self.open_books:
                continue
            if not self._book_in_state(state, book_id):
                continue
            self._maybe_general_make(response, spec, pd, vd, state)

        for book_id, spec in sorted(self.open_books.items()):
            if book_id in self.stop_books:
                continue
            if not self._book_in_state(state, book_id):
                self._log_open_skip_once(
                    book_id, "book not in state.books on this tick"
                )
                continue
            self._maybe_open(response, book_id, spec, pd, vd)

        try:
            from fifo_state_recorder import persist_fifo_state

            persist_fifo_state(self, state=state, force=bootstrapped > 0)
        except Exception:
            pass

        self._register_pending_limits(response, sim_ts)

        return response


# --- L2 depth + waiting orders for BookManageBot (Grafana only ~5 levels) ---
try:
    _agents_dir = Path(__file__).resolve().parent
    if str(_agents_dir) not in sys.path:
        sys.path.insert(0, str(_agents_dir))
    from l2_depth_recorder import record_l2_from_state as _record_l2_from_state
    from l3_trades_recorder import record_l3_from_state as _record_l3_from_state
    from waiting_orders_recorder import (
        record_waiting_orders as _record_waiting_orders,
        sent_orders_from_response as _sent_orders_from_response,
    )

    _orig_handle_for_l2 = TopAgent.handle
    _orig_respond_sim_for_recorders = TopAgent.respond_simulation

    def _handle_with_l2_snapshot(self, state):
        def _dump_l2() -> None:
            try:
                _record_l2_from_state(
                    self, state, max_levels=21, min_interval_sec=1.0
                )
                try:
                    _record_l3_from_state(self, state)
                except Exception:
                    pass
                _record_waiting_orders(
                    self,
                    state,
                    [],
                    max_age_sec=90.0,
                    min_interval_sec=0.0,
                )
            except Exception:
                pass

        threading.Thread(target=_dump_l2, daemon=True).start()
        return _orig_handle_for_l2(self, state)

    def _respond_sim_with_waiting(self, state):
        response = _orig_respond_sim_for_recorders(self, state)

        def _dump_waiting() -> None:
            try:
                _record_waiting_orders(
                    self,
                    state,
                    _sent_orders_from_response(response),
                    max_age_sec=90.0,
                    min_interval_sec=0.0,
                )
            except Exception:
                pass

        threading.Thread(target=_dump_waiting, daemon=True).start()
        return response

    TopAgent.handle = _handle_with_l2_snapshot
    TopAgent.respond_simulation = _respond_sim_with_waiting
except Exception:
    pass


if __name__ == "__main__":
    """
    Deploy:

      ./run_miner.sh -n TopAgent -m \\
        "live_config=1 live_config_path=agents/data/topagent.203.json \\
         expiry_period=200000000000"

    Edit agents/data/topagent.203.json while running — reloaded each tick via mtime.
    """
    launch(TopAgent)
