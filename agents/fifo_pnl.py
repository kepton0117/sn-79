# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Validator-style FIFO realized PnL tracker for miner agents.

Matches subnet dashboard accounting:
- FIFO lot matching with fee-on-close (see validator._match_trade_fifo)
- Rolling window = kappa lookback (default 3 sim-hours)

Cash-book alignment (taosim ClearingManager):
- On BUY fills with a *positive* fee, the exchange converts the QUOTE fee to
  BASE via ``ceil(fee/price, baseDecimals=4)`` and credits ``qty − haircut``.
  EVENT_TRADE.quantity still reports the full match. When
  ``buy_base_fee_haircut`` is enabled (default), this tracker records the
  wallet credit so FIFO net matches ``bb.t − int_base`` (and free_base).
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict, deque
from pathlib import Path

KAPPA_LOOKBACK_NS = 10_800_000_000_000  # default scoring.kappa.lookback

# taosim baseIncrementDecimals (ClearingManager / Balances roundUpBase).
BASE_VOLUME_DECIMALS = 4


def ceil_base_qty(qty: float, decimals: int = BASE_VOLUME_DECIMALS) -> float:
    """Match ``util::roundUp(x, baseIncrementDecimals)``."""
    if qty <= 0.0:
        return 0.0
    scale = 10 ** int(decimals)
    return math.ceil(qty * scale - 1e-15) / scale


def buy_base_fee_haircut(
    qty: float,
    price: float,
    fee_quote: float,
    *,
    decimals: int = BASE_VOLUME_DECIMALS,
) -> tuple[float, float, float]:
    """Apply taosim resting/aggressing BUY positive-fee BASE haircut.

    Returns ``(qty_credited, haircut_base, fee_for_fifo)``.

    When ``fee_quote > 0``: haircut = ceil(fee/price, 4dp), credit qty−haircut,
    and store economic open fee = haircut×price (QUOTE equivalent of BASE taken).
    When ``fee_quote ≤ 0`` (rebate / free): full qty, fee unchanged (QUOTE rebate).
    """
    q = float(qty)
    px = float(price)
    fee = float(fee_quote)
    if q <= 0.0 or px <= 0.0:
        return 0.0, 0.0, fee
    if fee <= 0.0:
        return q, 0.0, fee
    haircut = ceil_base_qty(fee / px, decimals=decimals)
    if haircut <= 0.0:
        return q, 0.0, fee
    if haircut >= q:
        return 0.0, q, q * px
    return q - haircut, haircut, haircut * px


class _FifoBook:
    """Per-book FIFO position queues: (timestamp, qty, price, fee)."""

    __slots__ = ("longs", "shorts", "bootstrapped_long", "bootstrapped_short")
   
    def __init__(self) -> None:
        self.longs: deque[tuple[int, float, float, float]] = deque()
        self.shorts: deque[tuple[int, float, float, float]] = deque()
        self.bootstrapped_long = False
        self.bootstrapped_short = False

    def match(
        self, is_buy: bool, qty: float, price: float, fee: float, ts: int
    ) -> tuple[float, float]:
        """Return (realized_pnl, roundtrip_qty). Mirrors validator._match_trade_fifo."""
        if qty <= 0.0 or price <= 0.0:
            return 0.0, 0.0

        if is_buy:
            if not self.shorts:
                self.longs.append((ts, qty, price, fee))
                return 0.0, 0.0
            queue = self.shorts
            opposite = self.longs
        else:
            if not self.longs:
                self.shorts.append((ts, qty, price, fee))
                return 0.0, 0.0
            queue = self.longs
            opposite = self.shorts

        realized = 0.0
        rt_qty = 0.0
        remaining = qty
        qty_inv = 1.0 / qty

        while remaining > 0.0 and queue:
            old_ts, old_qty, old_price, old_fee = queue[0]
            if is_buy:
                price_pnl = (old_price - price) * old_qty
            else:
                price_pnl = (price - old_price) * old_qty

            if old_qty <= remaining:
                close_fee = fee * old_qty * qty_inv
                if is_buy:
                    realized += (old_price - price) * old_qty - old_fee - close_fee
                else:
                    realized += (price - old_price) * old_qty - old_fee - close_fee
                rt_qty += old_qty
                remaining -= old_qty
                queue.popleft()
            else:
                # Partial close of this lot — prorate BOTH open and close fees
                # (validator match_trade_fifo: close_fee = fee * remaining * qty_inv).
                old_qty_inv = 1.0 / old_qty
                open_fee = old_fee * remaining * old_qty_inv
                close_fee = fee * remaining * qty_inv
                if is_buy:
                    realized += (old_price - price) * remaining - open_fee - close_fee
                else:
                    realized += (price - old_price) * remaining - open_fee - close_fee
                rt_qty += remaining
                queue[0] = (
                    old_ts,
                    old_qty - remaining,
                    old_price,
                    old_fee - open_fee,
                )
                remaining = 0.0

        if remaining > 0.0:
            open_fee = fee * remaining * qty_inv
            opposite.append((ts, remaining, price, open_fee))

        return realized, rt_qty

    def net_qty(self) -> float:
        long_qty = sum(q for _, q, _, _ in self.longs)
        short_qty = sum(q for _, q, _, _ in self.shorts)
        return long_qty - short_qty

    def avg_entry_price(self) -> float:
        nq = self.net_qty()
        if abs(nq) < 1e-12:
            return 0.0
        if nq > 0.0:
            total = sum(q for _, q, _, _ in self.longs)
            if total <= 0.0:
                return 0.0
            return sum(q * price for _, q, price, _ in self.longs) / total
        total = sum(q for _, q, _, _ in self.shorts)
        if total <= 0.0:
            return 0.0
        return sum(q * price for _, q, price, _ in self.shorts) / total

    def mark_to_market(self, mid: float) -> tuple[float, float]:
        nq = self.net_qty()
        entry = self.avg_entry_price()
        if abs(nq) < 1e-12 or entry <= 0.0 or mid <= 0.0:
            return 0.0, 0.0
        if nq > 0.0:
            unrealized = (mid - entry) * nq
            bps = (mid - entry) / entry * 1e4
        else:
            unrealized = (entry - mid) * (-nq)
            bps = (entry - mid) / entry * 1e4
        return unrealized, bps


class ValidatorStylePnl:
    """Tracks FIFO realized PnL like the validator dashboard.

    With ``buy_base_fee_haircut=True`` (default), BUY inventory follows the
    exchange cash book (ClearingManager BASE fee roundUp), not the raw tape qty.
    """

    def __init__(
        self,
        book_count: int,
        lookback_ns: int = KAPPA_LOOKBACK_NS,
        *,
        buy_base_fee_haircut: bool = True,
    ) -> None:
        self._lookback_ns = lookback_ns
        self.buy_base_fee_haircut = bool(buy_base_fee_haircut)
        self._books: dict[int, _FifoBook] = {i: _FifoBook() for i in range(book_count)}
        self._history: dict[int, dict[int, float]] = defaultdict(dict)
        self._cumulative_realized: dict[int, float] = defaultdict(float)
        self._history_ok = False
        # Trade IDs are unique per book, not globally — use composite keys.
        self._seen_trade_keys: set[tuple[int, int]] = set()
        self._buy_haircut_base_total = 0.0
        self._buy_haircut_fills = 0

    @property
    def book_count(self) -> int:
        return len(self._books)

    def reset(self, book_count: int | None = None) -> None:
        if book_count is not None:
            self._books = {i: _FifoBook() for i in range(book_count)}
        else:
            for book in self._books.values():
                book.longs.clear()
                book.shorts.clear()
                book.bootstrapped_long = False
                book.bootstrapped_short = False
        self._history.clear()
        self._cumulative_realized.clear()
        self._seen_trade_keys.clear()
        self._buy_haircut_base_total = 0.0
        self._buy_haircut_fills = 0
        self._history_ok = False

    def clear_book(self, book_id: int) -> None:
        """Wipe one book's FIFO lots, history, and cumulative (other books intact)."""
        bid = int(book_id)
        book = self._books.get(bid)
        if book is None:
            book = _FifoBook()
            self._books[bid] = book
        book.longs.clear()
        book.shorts.clear()
        book.bootstrapped_long = False
        book.bootstrapped_short = False
        self._cumulative_realized.pop(bid, None)
        self._normalize_history()
        empty_ts: list[int] = []
        for ts, books in list(self._history.items()):
            if not isinstance(books, dict):
                continue
            if bid in books:
                books.pop(bid, None)
            if not books:
                empty_ts.append(int(ts))
        for ts in empty_ts:
            self._history.pop(ts, None)
        if self._seen_trade_keys:
            self._seen_trade_keys = {
                key for key in self._seen_trade_keys if int(key[0]) != bid
            }

    def _normalize_history(self) -> None:
        if self._history_ok:
            return
        if not isinstance(self._history, dict):
            self._history = defaultdict(dict)
            self._history_ok = True
            return
        if not isinstance(self._history, defaultdict):
            self._history = defaultdict(dict, dict(self._history))
        for ts, books in list(self._history.items()):
            if not isinstance(books, dict):
                self._history[ts] = {}
        self._repair_inverted_history()
        self._history_ok = True

    def _repair_inverted_history(self) -> None:
        """Fix legacy snapshots that stored history[book_id][timestamp] instead of [ts][book]."""
        if not self._history:
            return
        outer_keys = [int(k) for k in self._history.keys()]
        if not outer_keys:
            return
        sample = self._history[outer_keys[0]]
        if not isinstance(sample, dict) or not sample:
            return
        inner_keys = [int(k) for k in sample.keys()]
        if not inner_keys:
            return
        max_outer = max(outer_keys)
        max_inner = max(inner_keys)
        # Correct layout: outer keys are sim timestamps (very large ns).
        if max_outer > 1_000_000_000_000 or max_inner < 10_000:
            return
        if max_outer >= 10_000:
            return
        fixed: dict[int, dict[int, float]] = defaultdict(dict)
        for book_id, ts_map in list(self._history.items()):
            if not isinstance(ts_map, dict):
                continue
            for ts, pnl in ts_map.items():
                ts_i = int(ts)
                book_i = int(book_id)
                bucket = fixed[ts_i]
                bucket[book_i] = bucket.get(book_i, 0.0) + float(pnl)
        self._history = defaultdict(dict, dict(fixed))

    def _history_bucket(self, timestamp: int) -> dict[int, float]:
        self._normalize_history()
        bucket = self._history.get(timestamp)
        if not isinstance(bucket, dict):
            bucket = {}
            self._history[timestamp] = bucket
        return bucket

    def history_book_pnl(self, timestamp: int, book_id: int) -> float:
        bucket = self._history.get(timestamp)
        if not isinstance(bucket, dict):
            return 0.0
        return float(bucket.get(book_id, 0.0))

    def record_trade(
        self,
        trade_id: int,
        book_id: int,
        timestamp: int,
        taker_agent_id: int,
        maker_agent_id: int,
        taker_side: int,
        price: float,
        qty: float,
        taker_fee: float,
        maker_fee: float,
        uid: int,
    ) -> bool:
        """Record one fill. Returns True if newly processed."""
        trade_key = (book_id, trade_id)
        if trade_key in self._seen_trade_keys:
            return False
        if taker_agent_id == maker_agent_id:
            return False

        is_taker = taker_agent_id == uid
        is_maker = maker_agent_id == uid
        if not is_taker and not is_maker:
            return False

        self._seen_trade_keys.add(trade_key)
        is_buy = (is_taker and taker_side == 0) or (is_maker and taker_side == 1)
        fee = maker_fee if is_maker else taker_fee
        qty_eff = float(qty)
        fee_eff = float(fee)

        # taosim ClearingManager: positive fee on BUY → BASE haircut via
        # ceil(fee/price, 4dp); tape qty stays full. Align FIFO with wallet.
        if self.buy_base_fee_haircut and is_buy:
            qty_eff, haircut, fee_eff = buy_base_fee_haircut(qty_eff, price, fee_eff)
            if haircut > 0.0:
                self._buy_haircut_base_total += haircut
                self._buy_haircut_fills += 1

        if qty_eff <= 0.0:
            return True

        if book_id not in self._books:
            self._books[book_id] = _FifoBook()

        book = self._books[book_id]
        book.bootstrapped_long = False
        book.bootstrapped_short = False
        pnl, _ = book.match(is_buy, qty_eff, price, fee_eff, timestamp)
        if pnl != 0.0:
            books_at_ts = self._history_bucket(timestamp)
            books_at_ts[book_id] = books_at_ts.get(book_id, 0.0) + pnl
            self._cumulative_realized[book_id] += pnl
        return True

    def _book(self, book_id: int) -> _FifoBook:
        if book_id not in self._books:
            self._books[book_id] = _FifoBook()
        return self._books[book_id]

    def net_qty(self, book_id: int) -> float:
        return self._book(book_id).net_qty()

    def avg_entry_price(self, book_id: int) -> float:
        return self._book(book_id).avg_entry_price()

    def mark_to_market(self, book_id: int, mid: float) -> tuple[float, float]:
        return self._book(book_id).mark_to_market(mid)

    def cumulative_realized_pnl(self, book_id: int) -> float:
        return float(self._cumulative_realized.get(book_id, 0.0))

    def total_cumulative_realized_pnl(self) -> float:
        return float(sum(self._cumulative_realized.values()))

    def has_open_position(self, book_id: int, flat_threshold: float) -> bool:
        return abs(self.net_qty(book_id)) >= flat_threshold

    def prune(self, now_ts: int) -> None:
        threshold = now_ts - self._lookback_ns
        pruned = {
            ts: books
            for ts, books in self._history.items()
            if ts >= threshold
        }
        self._history = defaultdict(dict, pruned)

    def window_book_stats(
        self, now_ts: int, lookback_ns: int | None = None
    ) -> tuple[dict[int, float], dict[int, int]]:
        """Single-pass rolling-window PnL totals and observation counts per book."""
        window = self._lookback_ns if lookback_ns is None else lookback_ns
        threshold = now_ts - window
        pnl_totals: dict[int, float] = defaultdict(float)
        obs_counts: dict[int, int] = defaultdict(int)
        for ts, books in self._history.items():
            if ts < threshold:
                continue
            if not isinstance(books, dict):
                continue
            for book_id, pnl in books.items():
                pnl_totals[book_id] += pnl
                if pnl != 0.0:
                    obs_counts[book_id] += 1
        return dict(pnl_totals), dict(obs_counts)

    def realized_pnl_by_book(self, now_ts: int) -> dict[int, float]:
        pnl_by_book, _ = self.window_book_stats(now_ts)
        return pnl_by_book

    def observation_count_by_book(self, now_ts: int) -> dict[int, int]:
        _, obs_by_book = self.window_book_stats(now_ts)
        return obs_by_book

    def total_realized_pnl(self, now_ts: int) -> float:
        return sum(self.realized_pnl_by_book(now_ts).values())

    def book_realized_pnl(self, book_id: int, now_ts: int) -> float:
        return self.realized_pnl_by_book(now_ts).get(book_id, 0.0)

    def book_observation_count(self, book_id: int, now_ts: int) -> int:
        """Non-zero FIFO close timestamps for a book in the lookback window."""
        return self.observation_count_by_book(now_ts).get(book_id, 0)

    def history_snapshot(self) -> dict[int, dict[int, float]]:
        """Deep copy of realized PnL history for kappa preview."""
        return {ts: dict(books) for ts, books in self._history.items()}

    def windowed_history(
        self, now_ts: int
    ) -> dict[int, dict[int, float]]:
        """History entries within the kappa lookback window."""
        threshold = now_ts - self._lookback_ns
        return {
            ts: dict(books)
            for ts, books in self._history.items()
            if ts >= threshold
        }

    @property
    def close_count(self) -> int:
        """Count of FIFO close events (non-zero realized PnL timestamps per book)."""
        return sum(
            1
            for books in self._history.values()
            if isinstance(books, dict)
            for pnl in books.values()
            if pnl != 0.0
        )

    @property
    def realized_pnl_total(self) -> float:
        return self.total_cumulative_realized_pnl()

    def fifo_long_head_price(self, book_id: int) -> float:
        book = self._book(book_id)
        return book.longs[0][2] if book.longs else 0.0

    def fifo_short_head_price(self, book_id: int) -> float:
        book = self._book(book_id)
        return book.shorts[0][2] if book.shorts else 0.0

    def fifo_long_head_qty(self, book_id: int) -> float:
        book = self._book(book_id)
        return book.longs[0][1] if book.longs else 0.0

    def fifo_long_lots(self, book_id: int) -> list[tuple[float, float]]:
        """Oldest-first (qty, price) slices for long FIFO closes."""
        book = self._book(book_id)
        return [(qty, price) for _, qty, price, _ in book.longs]

    def fifo_long_lots_with_fees(
        self, book_id: int
    ) -> list[tuple[float, float, float]]:
        """Oldest-first (qty, price, open_fee) slices for long FIFO closes."""
        book = self._book(book_id)
        return [(qty, price, fee) for _, qty, price, fee in book.longs]

    def fifo_short_lots(self, book_id: int) -> list[tuple[float, float]]:
        """Oldest-first (qty, price) slices for short FIFO closes."""
        book = self._book(book_id)
        return [(qty, price) for _, qty, price, _ in book.shorts]

    def fifo_short_lots_with_fees(
        self, book_id: int
    ) -> list[tuple[float, float, float]]:
        """Oldest-first (qty, price, open_fee) slices for short FIFO closes."""
        book = self._book(book_id)
        return [(qty, price, fee) for _, qty, price, fee in book.shorts]

    def fifo_short_head_qty(self, book_id: int) -> float:
        book = self._book(book_id)
        return book.shorts[0][1] if book.shorts else 0.0

    def fifo_long_total_qty(self, book_id: int) -> float:
        return sum(qty for _, qty, _, _ in self._book(book_id).longs)

    def fifo_short_total_qty(self, book_id: int) -> float:
        return sum(qty for _, qty, _, _ in self._book(book_id).shorts)

    def fifo_bootstrapped(self, book_id: int) -> tuple[bool, bool]:
        book = self._book(book_id)
        return book.bootstrapped_long, book.bootstrapped_short

    def seed_long(self, book_id: int, timestamp: int, qty: float, price: float) -> bool:
        if qty <= 0.0 or price <= 0.0:
            return False
        book = self._book(book_id)
        if book.longs:
            return False
        book.longs.append((timestamp, qty, price, 0.0))
        book.bootstrapped_long = True
        return True

    def seed_short(self, book_id: int, timestamp: int, qty: float, price: float) -> bool:
        if qty <= 0.0 or price <= 0.0:
            return False
        book = self._book(book_id)
        if book.shorts:
            return False
        book.shorts.append((timestamp, qty, price, 0.0))
        book.bootstrapped_short = True
        return True

    def resync_bootstrap_long(
        self, book_id: int, timestamp: int, qty: float, price: float
    ) -> bool:
        """Replace a single bootstrapped long lot (validator inventory resync)."""
        if qty <= 0.0 or price <= 0.0:
            return False
        book = self._book(book_id)
        if not book.bootstrapped_long or len(book.longs) != 1:
            return False
        book.longs[0] = (timestamp, qty, price, 0.0)
        return True

    def resync_bootstrap_short(
        self, book_id: int, timestamp: int, qty: float, price: float
    ) -> bool:
        """Replace a single bootstrapped short lot (validator inventory resync)."""
        if qty <= 0.0 or price <= 0.0:
            return False
        book = self._book(book_id)
        if not book.bootstrapped_short or len(book.shorts) != 1:
            return False
        book.shorts[0] = (timestamp, qty, price, 0.0)
        return True

    def _book_to_dict(self, book: _FifoBook) -> dict:
        return {
            "longs": [list(lot) for lot in book.longs],
            "shorts": [list(lot) for lot in book.shorts],
            "bootstrapped_long": book.bootstrapped_long,
            "bootstrapped_short": book.bootstrapped_short,
        }

    @staticmethod
    def _lot_from_list(lot: list | dict | tuple) -> tuple[int, float, float, float]:
        if isinstance(lot, dict):
            # Legacy JgsAgent FifoTracker lot: {price, qty, fee} — no timestamp.
            if "0" in lot or 0 in lot:
                return (
                    int(lot.get("0", lot.get(0, 0))),
                    float(lot.get("1", lot.get(1, 0))),
                    float(lot.get("2", lot.get(2, 0))),
                    float(lot.get("3", lot.get(3, 0))),
                )
            return (
                int(lot.get("timestamp", lot.get("ts", 0))),
                float(lot.get("qty", lot.get("quantity", 0))),
                float(lot.get("price", 0)),
                float(lot.get("fee", 0)),
            )
        if isinstance(lot, (list, tuple)) and len(lot) >= 4:
            return (int(lot[0]), float(lot[1]), float(lot[2]), float(lot[3]))
        raise ValueError(f"invalid FIFO lot: {lot!r}")

    def _lots_from_side(self, lots: list) -> deque[tuple[int, float, float, float]]:
        parsed: deque[tuple[int, float, float, float]] = deque()
        for lot in lots:
            try:
                parsed.append(self._lot_from_list(lot))
            except (KeyError, TypeError, ValueError):
                continue
        return parsed

    def _book_from_dict(self, data: dict) -> _FifoBook:
        book = _FifoBook()
        book.longs = self._lots_from_side(data.get("longs", []))
        book.shorts = self._lots_from_side(data.get("shorts", []))
        book.bootstrapped_long = bool(data.get("bootstrapped_long", False))
        book.bootstrapped_short = bool(data.get("bootstrapped_short", False))
        return book

    def to_dict(self) -> dict:
        return {
            "lookback_ns": self._lookback_ns,
            "buy_base_fee_haircut": bool(self.buy_base_fee_haircut),
            "buy_haircut_base_total": float(self._buy_haircut_base_total),
            "buy_haircut_fills": int(self._buy_haircut_fills),
            "seen_trade_keys": [list(k) for k in self._seen_trade_keys],
            "cumulative_realized": {str(k): v for k, v in self._cumulative_realized.items()},
            "history": {str(ts): {str(b): p for b, p in books.items()} for ts, books in self._history.items()},
            "books": {str(bid): self._book_to_dict(book) for bid, book in self._books.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, book_count: int) -> ValidatorStylePnl:
        haircut = data.get("buy_base_fee_haircut", True)
        fifo = cls(
            book_count,
            lookback_ns=int(data.get("lookback_ns", KAPPA_LOOKBACK_NS)),
            buy_base_fee_haircut=bool(haircut),
        )
        fifo._buy_haircut_base_total = float(data.get("buy_haircut_base_total", 0.0) or 0.0)
        fifo._buy_haircut_fills = int(data.get("buy_haircut_fills", 0) or 0)
        fifo._seen_trade_keys = {
            (int(k[0]), int(k[1])) for k in data.get("seen_trade_keys", [])
        }
        fifo._cumulative_realized = defaultdict(
            float, {int(k): float(v) for k, v in data.get("cumulative_realized", {}).items()}
        )
        fifo._history = defaultdict(dict)
        for ts_str, books in data.get("history", {}).items():
            ts = int(ts_str)
            if isinstance(books, dict):
                fifo._history[ts] = {int(b): float(p) for b, p in books.items()}
            else:
                fifo._history[ts] = {}
        for bid_str, book_data in data.get("books", {}).items():
            fifo._books[int(bid_str)] = fifo._book_from_dict(book_data)
        fifo._normalize_history()
        fifo._history_ok = True
        return fifo

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), separators=(",", ":")))
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path, book_count: int) -> ValidatorStylePnl:
        if not path.is_file():
            return cls(book_count)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return cls(book_count)
        if not isinstance(data, dict):
            return cls(book_count)
        # JgsAgent FifoTracker snapshot — incompatible; start fresh for ValidatorStylePnl.
        if "seen_trade_ids" in data and "seen_trade_keys" not in data:
            return cls(book_count)
        try:
            return cls.from_dict(data, book_count)
        except (KeyError, TypeError, ValueError):
            return cls(book_count)


def predict_long_close_realized(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    sell_price: float,
    close_fee_rate: float,
) -> float:
    """Predict realized PnL for a long close sell (validator FIFO fee split)."""
    if close_qty <= 1e-12 or sell_price <= 0.0 or not lots:
        return 0.0
    total_close_fee = sell_price * close_qty * close_fee_rate
    remaining = close_qty
    realized = 0.0
    for lot_qty, lot_price, lot_open_fee in lots:
        if remaining <= 1e-12:
            break
        if lot_qty <= 1e-12 or lot_price <= 0.0:
            continue
        if lot_qty <= remaining + 1e-12:
            close_fee = total_close_fee * lot_qty / close_qty
            realized += (
                (sell_price - lot_price) * lot_qty - lot_open_fee - close_fee
            )
            remaining -= lot_qty
        else:
            open_fee = lot_open_fee * remaining / lot_qty
            close_fee = total_close_fee * remaining / close_qty
            realized += (
                (sell_price - lot_price) * remaining - open_fee - close_fee
            )
            remaining = 0.0
    return realized


def solve_long_close_sell_price(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_pnl: float,
    close_fee_rate: float,
    *,
    max_iter: int = 80,
) -> float:
    """Sell price that realizes target_pnl (QUOTE) on close_qty (monotone in price)."""
    if close_qty <= 1e-12 or not lots:
        return 0.0

    max_entry = max(price for _, price, _ in lots if price > 0.0)
    lo = max(1e-6, max_entry * 0.01)
    hi = max(max_entry * 1.5, lo + 1.0)

    p_hi = predict_long_close_realized(lots, close_qty, hi, close_fee_rate)
    while p_hi < target_pnl and hi < max_entry * 100.0 + 1000.0:
        hi *= 2.0
        p_hi = predict_long_close_realized(lots, close_qty, hi, close_fee_rate)

    p_lo = predict_long_close_realized(lots, close_qty, lo, close_fee_rate)
    if p_lo > target_pnl:
        lo = max(1e-6, lo * 0.1)
        p_lo = predict_long_close_realized(lots, close_qty, lo, close_fee_rate)

    for _ in range(max_iter):
        mid = (lo + hi) * 0.5
        p_mid = predict_long_close_realized(lots, close_qty, mid, close_fee_rate)
        if abs(p_mid - target_pnl) <= 1e-10:
            return mid
        if p_mid < target_pnl:
            lo = mid
        else:
            hi = mid
    return (lo + hi) * 0.5


def predict_short_close_realized(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    buy_price: float,
    close_fee_rate: float,
) -> float:
    """Predict realized PnL for a short cover buy (validator FIFO fee split)."""
    if close_qty <= 1e-12 or buy_price <= 0.0 or not lots:
        return 0.0
    total_close_fee = buy_price * close_qty * close_fee_rate
    remaining = close_qty
    realized = 0.0
    for lot_qty, lot_price, lot_open_fee in lots:
        if remaining <= 1e-12:
            break
        if lot_qty <= 1e-12 or lot_price <= 0.0:
            continue
        if lot_qty <= remaining + 1e-12:
            close_fee = total_close_fee * lot_qty / close_qty
            realized += (
                (lot_price - buy_price) * lot_qty - lot_open_fee - close_fee
            )
            remaining -= lot_qty
        else:
            open_fee = lot_open_fee * remaining / lot_qty
            close_fee = total_close_fee * remaining / close_qty
            realized += (
                (lot_price - buy_price) * remaining - open_fee - close_fee
            )
            remaining = 0.0
    return realized


def solve_short_close_buy_price(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_pnl: float,
    close_fee_rate: float,
    *,
    max_iter: int = 80,
) -> float:
    """Max buy price that realizes target_pnl (QUOTE) on close_qty (monotone in price)."""
    if close_qty <= 1e-12 or not lots:
        return 0.0

    min_entry = min(price for _, price, _ in lots if price > 0.0)
    lo = max(1e-6, min_entry * 0.01)
    hi = max(min_entry * 1.5, lo + 1.0)

    p_lo = predict_short_close_realized(lots, close_qty, lo, close_fee_rate)
    while p_lo < target_pnl and lo > 1e-9:
        lo *= 0.5
        p_lo = predict_short_close_realized(lots, close_qty, lo, close_fee_rate)

    p_hi = predict_short_close_realized(lots, close_qty, hi, close_fee_rate)
    while p_hi > target_pnl and hi < min_entry * 100.0 + 1000.0:
        hi *= 2.0
        p_hi = predict_short_close_realized(lots, close_qty, hi, close_fee_rate)

    for _ in range(max_iter):
        mid = (lo + hi) * 0.5
        p_mid = predict_short_close_realized(lots, close_qty, mid, close_fee_rate)
        if abs(p_mid - target_pnl) <= 1e-10:
            return mid
        if p_mid > target_pnl:
            lo = mid
        else:
            hi = mid
    return (lo + hi) * 0.5
