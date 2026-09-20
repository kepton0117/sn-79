# SPDX-License-Identifier: MIT
# MM engine with a single gate owner per decision (market → balance → side → FIFO → safety).

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Literal


PreferredSide = Literal["BUY", "SELL", "BOTH", "NONE"]
PositionMode = Literal["long_heavy", "short_heavy", "balanced"]


def _v(d, key, default):
    return d[key] if key in d else default


def round_decimals(value, decimals):
    if decimals <= 0:
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    scale = Decimal(10) ** decimals
    scaled = Decimal(str(value)) * scale
    rounded = scaled.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(rounded / scale)


def round_decimals_up(value, decimals):
    if decimals <= 0:
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_UP))
    scale = Decimal(10) ** decimals
    scaled = Decimal(str(value)) * scale
    rounded = scaled.quantize(Decimal("1"), rounding=ROUND_UP)
    return float(rounded / scale)


def round_decimals_down(value, decimals):
    if decimals <= 0:
        return float(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_DOWN))
    scale = Decimal(10) ** decimals
    scaled = Decimal(str(value)) * scale
    rounded = scaled.quantize(Decimal("1"), rounding=ROUND_DOWN)
    return float(rounded / scale)


def clamp(x, lo, hi):
    return max(lo, min(x, hi))


def _read_fifo_lots_with_fees(
    book: dict,
    key: str,
    *,
    open_fee_rate_fallback: float = 0.0,
) -> list[tuple[float, float, float]]:
    """Parse FIFO lots as (qty, price, open_fee); estimate fee when missing."""
    raw = _v(book, key, [])
    lots: list[tuple[float, float, float]] = []
    if not isinstance(raw, list):
        return lots
    for item in raw:
        fee = None
        if isinstance(item, (list, tuple)) and len(item) >= 4:
            qty, price = float(item[1]), float(item[2])
            fee = float(item[3])
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            qty, price = float(item[0]), float(item[1])
            fee = float(item[2])
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            qty, price = float(item[0]), float(item[1])
        elif isinstance(item, dict):
            qty = float(item.get("qty", item.get("quantity", 0.0)))
            price = float(item.get("price", 0.0))
            if "fee" in item:
                fee = float(item["fee"])
        else:
            continue
        if qty <= 0.0 or price <= 0.0:
            continue
        if fee is None or (
            fee == 0.0 and open_fee_rate_fallback > 0.0
        ):
            # Seeded/bootstrap lots store fee=0; estimate from validator rate.
            fee = price * qty * open_fee_rate_fallback
        lots.append((qty, price, max(fee, 0.0)))
    return lots


def _read_fifo_lots(book: dict, key: str) -> list[tuple[float, float]]:
    raw = _v(book, key, [])
    lots: list[tuple[float, float]] = []
    if not isinstance(raw, list):
        return lots
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 4:
            qty, price = float(item[1]), float(item[2])
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            qty, price = float(item[0]), float(item[1])
        elif isinstance(item, dict):
            qty = float(item.get("qty", item.get("quantity", 0.0)))
            price = float(item.get("price", 0.0))
        else:
            continue
        if qty > 0.0 and price > 0.0:
            lots.append((qty, price))
    return lots


def _fifo_long_close_entry(book: dict, close_qty: float) -> float:
    """Entry price for a micro long close: FIFO lots first, then head price."""
    if close_qty <= 0.0:
        return 0.0
    entry = worst_long_close_entry(_read_fifo_lots(book, "fifo_long_lots"), close_qty)
    if entry > 0.0:
        return entry
    long_qty = float(_v(book, "fifo_long_total_qty", 0.0))
    head = float(_v(book, "fifo_long_head_price", 0.0))
    if head > 0.0 and long_qty + 1e-9 >= close_qty:
        return head
    return 0.0


def _fifo_short_close_entry(book: dict, close_qty: float) -> float:
    """Entry price for a micro short cover: FIFO lots first, then head price."""
    if close_qty <= 0.0:
        return 0.0
    entry = worst_short_close_entry(
        _read_fifo_lots(book, "fifo_short_lots"), close_qty
    )
    if entry > 0.0:
        return entry
    short_qty = float(_v(book, "fifo_short_total_qty", 0.0))
    head = float(_v(book, "fifo_short_head_price", 0.0))
    if head > 0.0 and short_qty + 1e-9 >= close_qty:
        return head
    return 0.0


def _lot_qty_price(item) -> tuple[float, float]:
    """Extract (qty, price) from a FIFO lot row (2-, 3-, or 4-tuple)."""
    if not isinstance(item, (list, tuple)) or len(item) < 2:
        return 0.0, 0.0
    if len(item) >= 4:
        return float(item[1]), float(item[2])
    return float(item[0]), float(item[1])


def _lots_as_fee_lots(
    lots: list,
    *,
    fee_rate: float,
    head_price: float = 0.0,
    head_qty: float = 0.0,
) -> list[tuple[float, float, float]]:
    """Normalize FIFO lots to (qty, price, open_fee) for validator-style pricing."""
    out: list[tuple[float, float, float]] = []
    for item in lots or []:
        fee = None
        if isinstance(item, (list, tuple)) and len(item) >= 4:
            qty, price = float(item[1]), float(item[2])
            fee = float(item[3])
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            qty, price = float(item[0]), float(item[1])
            fee = float(item[2])
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            qty, price = float(item[0]), float(item[1])
        else:
            continue
        if qty <= 0.0 or price <= 0.0:
            continue
        if fee is None:
            fee = price * qty * fee_rate
        out.append((qty, price, max(fee, 0.0)))
    if not out and head_price > 0.0 and head_qty > 0.0:
        out.append((head_qty, head_price, head_price * head_qty * fee_rate))
    return out


def worst_long_close_entry(
    lots: list, close_qty: float
) -> float:
    """Highest long entry among FIFO lots consumed by a planned sell size."""
    if close_qty <= 0.0 or not lots:
        return 0.0
    remaining = close_qty
    worst = 0.0
    for item in lots:
        if remaining <= 0.0:
            break
        qty, price = _lot_qty_price(item)
        take = min(qty, remaining)
        if take > 0.0:
            worst = max(worst, price)
            remaining -= take
    return worst


def worst_short_close_entry(
    lots: list, close_qty: float
) -> float:
    """Lowest short entry among FIFO lots consumed by a planned buy size."""
    if close_qty <= 0.0 or not lots:
        return 0.0
    remaining = close_qty
    worst = 0.0
    found = False
    for item in lots:
        if remaining <= 0.0:
            break
        qty, price = _lot_qty_price(item)
        take = min(qty, remaining)
        if take > 0.0:
            if not found:
                worst = price
                found = True
            else:
                worst = min(worst, price)
            remaining -= take
    return worst if found else 0.0


def maker_fee_rate_from_lots(
    lots: list[tuple[float, float, float]],
) -> float:
    """Implied maker fee rate from FIFO open fees: sum(fee) / sum(price*qty)."""
    notional = 0.0
    fees = 0.0
    for qty, price, fee in lots:
        if qty <= 0.0 or price <= 0.0:
            continue
        notional += qty * price
        fees += float(fee)
    if notional <= 0.0 or fees <= 0.0:
        return 0.0
    return fees / notional


def close_maker_fee_rate_for_pricing(
    account_maker_fee_rate: float,
    min_close_maker_fee_rate: float = 0.0,
    *extra_rates: float,
) -> float:
    """Maker close pricing rate: prefer real fill rates only.

    ``extra_rates`` are observed real rates (validator fill / prior-tick /
    lot-implied). When any extra > 0, return max(extras) and ignore account.
    When all extras are 0 (pre-first-fill), keep prior logic:
    max(account if > 0, min_close_maker_fee_rate, extras).
    """
    extras = [float(x or 0.0) for x in extra_rates]
    real_rate = max(extras) if extras else 0.0
    if real_rate > 0.0:
        return real_rate
    acct = float(account_maker_fee_rate or 0.0)
    if acct <= 0.0:
        acct = 0.0
    rate = max(acct, float(min_close_maker_fee_rate or 0.0))
    for extra in extras:
        rate = max(rate, extra)
    return rate

def fee_aware_long_close_price(
    entry: float, target_edge: float, maker_fee_rate: float
) -> float:
    """Min sell price for net profit >= target_edge after open+close maker fees."""
    if entry <= 0.0:
        return 0.0
    denom = 1.0 - maker_fee_rate
    if denom <= 0.0:
        denom = 1.0
    return (entry * (1.0 + maker_fee_rate) + target_edge) / denom


def fee_aware_long_close_total_pnl(
    entry: float,
    qty: float,
    target_net_pnl: float,
    maker_fee_rate: float,
) -> float:
    """Sell price for target net realized PnL (QUOTE) on qty after maker fees."""
    if entry <= 0.0 or qty <= 0.0:
        return 0.0
    return fee_aware_long_close_price(
        entry, target_net_pnl / qty, maker_fee_rate
    )


def fee_aware_long_close_price_fok(
    entry: float,
    target_edge: float,
    open_maker_fee_rate: float,
    close_taker_fee_rate: float,
) -> float:
    """Sell price for target net edge: maker open (post-only), taker FOK close."""
    if entry <= 0.0:
        return 0.0
    denom = 1.0 - close_taker_fee_rate
    if denom <= 0.0:
        denom = 1.0
    return (entry * (1.0 + open_maker_fee_rate) + target_edge) / denom


def fee_aware_long_close_total_pnl_fok(
    entry: float,
    qty: float,
    target_net_pnl: float,
    open_maker_fee_rate: float,
    close_taker_fee_rate: float,
) -> float:
    """Sell price for target net PnL with maker open and taker FOK close fees."""
    if entry <= 0.0 or qty <= 0.0:
        return 0.0
    return fee_aware_long_close_price_fok(
        entry,
        target_net_pnl / qty,
        open_maker_fee_rate,
        close_taker_fee_rate,
    )


def fee_aware_short_close_price(
    entry: float, target_edge: float, maker_fee_rate: float
) -> float:
    """Max buy price to cover a short for net profit >= target_edge after fees."""
    if entry <= 0.0:
        return 0.0
    denom = 1.0 + maker_fee_rate
    if denom <= 0.0:
        denom = 1.0
    return (entry * (1.0 - maker_fee_rate) - target_edge) / denom


def fifo_accurate_long_close_sell_floor(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_net_pnl: float,
    close_fee_rate: float,
    price_decimals: int,
    tick: float,
) -> float:
    """Min sell price with validator FIFO fees; safe after decimal rounding."""
    from fifo_pnl import predict_long_close_realized, solve_long_close_sell_price

    if close_qty <= 1e-12 or not lots:
        return 0.0
    raw = solve_long_close_sell_price(
        lots, close_qty, target_net_pnl, close_fee_rate
    )
    price = round_decimals_up(raw, price_decimals)
    for _ in range(10000):
        pnl = predict_long_close_realized(
            lots, close_qty, price, close_fee_rate
        )
        if pnl + 1e-12 >= target_net_pnl:
            return price
        price = round_decimals_up(price + tick, price_decimals)
    return price


def fifo_accurate_short_close_buy_ceiling(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_net_pnl: float,
    close_fee_rate: float,
    price_decimals: int,
    tick: float,
) -> float:
    """Max buy price with validator FIFO fees; safe after decimal rounding."""
    from fifo_pnl import predict_short_close_realized, solve_short_close_buy_price

    if close_qty <= 1e-12 or not lots:
        return 0.0
    raw = solve_short_close_buy_price(
        lots, close_qty, target_net_pnl, close_fee_rate
    )
    price = round_decimals_down(raw, price_decimals)
    for _ in range(10000):
        pnl = predict_short_close_realized(
            lots, close_qty, price, close_fee_rate
        )
        if pnl + 1e-12 >= target_net_pnl:
            return price
        if price <= tick:
            break
        price = round_decimals_down(price - tick, price_decimals)
    return price


def _close_target_net_pnl(target_edge: float, close_qty: float) -> float:
    """Convert per-unit realize edge (price-space) to total QUOTE PnL target."""
    return target_edge * max(close_qty, 0.0)


def _long_close_entry_floor(
    ctx: "BookCtx",
    close_qty: float,
    target_edge: float,
    *,
    price_decimals: int | None = None,
) -> float:
    planned = min(max(close_qty, 0.0), ctx.fifo_long_qty)
    decimals = price_decimals if price_decimals is not None else ctx.price_decimals
    lots = _lots_as_fee_lots(
        ctx.fifo_long_lots,
        fee_rate=ctx.maker_fee_rate,
        head_price=ctx.fifo_long_head,
        head_qty=ctx.fifo_long_head_qty if ctx.fifo_long_head_qty > 0 else planned,
    )
    if lots and planned > 0.0:
        return fifo_accurate_long_close_sell_floor(
            lots,
            planned,
            _close_target_net_pnl(target_edge, planned),
            ctx.maker_fee_rate,
            decimals,
            ctx.tick,
        )
    entry = worst_long_close_entry(ctx.fifo_long_lots, planned)
    if entry <= 0.0:
        entry = ctx.fifo_long_head
    return fee_aware_long_close_price(entry, target_edge, ctx.maker_fee_rate)


def _short_close_entry_ceiling(
    ctx: "BookCtx",
    close_qty: float,
    target_edge: float,
    *,
    price_decimals: int | None = None,
) -> float:
    planned = min(max(close_qty, 0.0), ctx.fifo_short_qty)
    decimals = price_decimals if price_decimals is not None else ctx.price_decimals
    lots = _lots_as_fee_lots(
        ctx.fifo_short_lots,
        fee_rate=ctx.maker_fee_rate,
        head_price=ctx.fifo_short_head,
        head_qty=ctx.fifo_short_head_qty if ctx.fifo_short_head_qty > 0 else planned,
    )
    if lots and planned > 0.0:
        return fifo_accurate_short_close_buy_ceiling(
            lots,
            planned,
            _close_target_net_pnl(target_edge, planned),
            ctx.maker_fee_rate,
            decimals,
            ctx.tick,
        )
    entry = worst_short_close_entry(ctx.fifo_short_lots, planned)
    if entry <= 0.0:
        entry = ctx.fifo_short_head
    return fee_aware_short_close_price(entry, target_edge, ctx.maker_fee_rate)


def level_volume_sum(levels, depth):
    total = 0.0
    n = 0
    for level in levels:
        if n >= depth:
            break
        total += float(_v(level, "quantity", _v(level, "qty", 0.0)))
        n += 1
    return total


def depth_threshold_price(
    levels,
    qty_threshold: float,
    max_levels: int,
) -> tuple[float | None, float | None, float]:
    """Walk levels from touch; return (depth_qty_price, last_level_price, cum).

    ``depth_qty_price`` is the first level where cum qty > ``qty_threshold``.
    If never reached, ``depth_qty_price`` is None and caller should use last.
    ``last_level_price`` is the last level within ``max_levels`` (worst L21).
    """
    cum = 0.0
    last_px: float | None = None
    hit_px: float | None = None
    n = 0
    thresh = float(qty_threshold)
    for level in levels or []:
        if n >= int(max_levels):
            break
        try:
            px = float(_v(level, "price", 0.0))
            qty = float(_v(level, "quantity", _v(level, "qty", 0.0)))
        except (TypeError, ValueError):
            n += 1
            continue
        if px <= 0.0:
            n += 1
            continue
        last_px = px
        cum += max(0.0, qty)
        if hit_px is None and cum > thresh:
            hit_px = px
        n += 1
    return hit_px, last_px, cum


def touch_qty(levels):
    if not levels or not isinstance(levels[0], dict):
        return 0.0
    return _v(levels[0], "quantity", 0.0)


def add_limit_order(
    orders,
    book_id,
    direction,
    quantity,
    price,
    expiry_period,
    exchange_min_quantity,
    *,
    post_only=True,
    time_in_force="GTT",
):
    if quantity < exchange_min_quantity or price <= 0.0:
        return
    orders.append(
        {
            "type": "limit",
            "book_id": book_id,
            "direction": direction,
            "quantity": quantity,
            "price": price,
            "postOnly": post_only,
            "timeInForce": time_in_force,
            "expiry_period": expiry_period,
        }
    )


def finalize_order_quantity(
    quantity,
    max_quantity,
    exchange_min_quantity,
    volume_decimals,
):
    if quantity < exchange_min_quantity:
        return 0.0
    quantity = min(quantity, max_quantity)
    quantity = round_decimals(quantity, volume_decimals)
    if quantity < exchange_min_quantity:
        return 0.0
    return quantity


def net_near_exchange_min(
    abs_net: float,
    exchange_min_quantity: float,
    *,
    flat_qty: float,
    volume_decimals: int,
) -> bool:
    """True when |net| is effectively one venue min lot (float / rounding residue)."""
    min_lot = float(exchange_min_quantity)
    if min_lot <= 0.0:
        return False
    qty_step = pow(10.0, -volume_decimals) if volume_decimals > 0 else 1e-6
    tol = max(float(flat_qty), qty_step, min_lot * 0.004)
    return abs_net + tol + 1e-12 >= min_lot


def add_cancel_order(orders, book_id, order):
    if "id" not in order:
        return
    orders.append(
        {
            "type": "cancel",
            "book_id": book_id,
            "order_id": order["id"],
        }
    )


def read_position(book):
    if "position" in book:
        if isinstance(book["position"], (int, float)):
            return float(book["position"])
        if isinstance(book["position"], dict):
            return _v(book["position"], "base", 0.0)
    if "account" in book and isinstance(book["account"], dict):
        return _v(book["account"], "base", 0.0)
    if "accounts" in book and isinstance(book["accounts"], dict):
        return _v(book["accounts"], "base", 0.0)
    return 0.0


def read_free_base(book):
    if "free_base" in book and isinstance(book["free_base"], (int, float)):
        return float(book["free_base"])
    return read_position(book)


def read_free_quote(book):
    if "free_quote" in book and isinstance(book["free_quote"], (int, float)):
        return float(book["free_quote"])
    return 0.0


def read_open_orders(book):
    if "open_orders" in book and isinstance(book["open_orders"], list):
        return book["open_orders"]
    if "orders" in book and isinstance(book["orders"], list):
        return book["orders"]
    if "active_orders" in book and isinstance(book["active_orders"], list):
        return book["active_orders"]
    return []


def classify_regime_state(
    trend_score,
    pressure_score,
    reversal_score,
    pressure_threshold,
    reversal_threshold,
    trend_flat_threshold,
):
    flat = abs(trend_score) <= trend_flat_threshold
    up = trend_score > trend_flat_threshold
    down = trend_score < -trend_flat_threshold
    bid_pressure = pressure_score > pressure_threshold
    ask_pressure = pressure_score < -pressure_threshold
    rev_up = reversal_score > reversal_threshold
    rev_down = reversal_score < -reversal_threshold

    if up and rev_up and bid_pressure:
        return "up_accelerating"
    if up and ask_pressure and rev_down:
        return "up_weakening"
    if up and bid_pressure:
        return "up_confirmed"
    if down and rev_down and ask_pressure:
        return "down_accelerating"
    if down and bid_pressure and rev_up:
        return "down_weakening"
    if down and ask_pressure:
        return "down_confirmed"
    if flat:
        return "flat"
    if up:
        return "up_confirmed" if bid_pressure else "up_weakening"
    if down:
        return "down_confirmed" if ask_pressure else "down_weakening"
    return "flat"


def regime_direction(regime_state):
    if regime_state in ("up_accelerating", "up_confirmed", "up_weakening"):
        return 1
    if regime_state in ("down_accelerating", "down_confirmed", "down_weakening"):
        return -1
    return 0


def compute_sell_urgency(
    trend_score,
    pressure_score,
    reversal_score,
    pressure_threshold,
    reversal_threshold,
    trend_flat_threshold,
):
    up_weakening = (
        trend_score > 0.0
        and pressure_score < -pressure_threshold
        and reversal_score < -reversal_threshold
    )
    down_confirmed = trend_score < 0.0 and pressure_score < -pressure_threshold
    flat_ask_pressure = (
        abs(trend_score) <= trend_flat_threshold
        and pressure_score < -pressure_threshold
    )
    up_confirmed = (
        trend_score > 0.0
        and pressure_score > pressure_threshold
        and reversal_score >= -reversal_threshold
    )
    if up_weakening:
        return 0.90
    if down_confirmed:
        return 1.00
    if flat_ask_pressure:
        return 0.80
    if up_confirmed:
        return 0.20
    return 1.00


def compute_buy_urgency(
    trend_score,
    pressure_score,
    reversal_score,
    pressure_threshold,
    reversal_threshold,
    trend_flat_threshold,
):
    down_weakening = (
        trend_score < 0.0
        and pressure_score > pressure_threshold
        and reversal_score > reversal_threshold
    )
    up_confirmed = (
        trend_score > 0.0
        and pressure_score > pressure_threshold
        and reversal_score >= -reversal_threshold
    )
    flat_bid_pressure = (
        abs(trend_score) <= trend_flat_threshold
        and pressure_score > pressure_threshold
    )
    down_confirmed = trend_score < 0.0 and pressure_score < -pressure_threshold
    if down_weakening:
        return 0.90
    if up_confirmed:
        return 1.00
    if flat_bid_pressure:
        return 0.80
    if down_confirmed:
        return 0.20
    return 0.60


def urgency_sell_touch_ticks(sell_urgency, touch_back_ticks, tick):
    if sell_urgency >= 0.80:
        return 0.0
    if sell_urgency <= 0.25:
        return max(touch_back_ticks, 1.0) * tick
    return touch_back_ticks * tick


def _risk_exit_sell_signal(trend_score, pressure_score, regime_dir, pressure_threshold, trend_flat_threshold):
    if pressure_score >= -pressure_threshold:
        return False
    return regime_dir < 0 or trend_score < -trend_flat_threshold


def _risk_exit_buy_signal(trend_score, pressure_score, regime_dir, pressure_threshold, trend_flat_threshold):
    if pressure_score <= pressure_threshold:
        return False
    return regime_dir > 0 or trend_score > trend_flat_threshold


def _price_inside_spread(price: float, best_bid: float, best_ask: float) -> bool:
    """True when best_bid < price < best_ask (strict open interval)."""
    return (
        best_bid > 0.0
        and best_ask > best_bid
        and (best_bid + 1e-12) < float(price) < (best_ask - 1e-12)
    )


def _strong_bullish(trend_score, pressure_score, trend_thresh, pressure_thresh) -> bool:
    return float(trend_score) > float(trend_thresh) and float(pressure_score) > float(
        pressure_thresh
    )


def _strong_bearish(trend_score, pressure_score, trend_thresh, pressure_thresh) -> bool:
    return float(trend_score) < -float(trend_thresh) and float(pressure_score) < -float(
        pressure_thresh
    )


@dataclass
class Allow:
    buy: bool = True
    sell: bool = True
    reasons: list[str] = field(default_factory=list)

    def block(self, side: Literal["buy", "sell", "both"], reason: str) -> None:
        if side in ("buy", "both") and self.buy:
            self.buy = False
            self.reasons.append(f"{reason}:buy")
        if side in ("sell", "both") and self.sell:
            self.sell = False
            self.reasons.append(f"{reason}:sell")


@dataclass
class BookCtx:
    book_id: int
    tick: float
    mid: float
    best_bid: float
    best_ask: float
    spread_ticks: float
    position_delta: float
    inv_ratio: float
    inv_abs: float
    free_base: float
    free_quote: float
    maker_fee_rate: float
    has_long_fifo: bool
    has_short_fifo: bool
    fifo_long_head: float
    fifo_short_head: float
    fifo_long_qty: float
    fifo_short_qty: float
    fifo_bootstrapped: bool
    regime_state: str
    regime_dir: int
    pressure_score: float
    trend_score: float
    activity_mode: float
    directional_gate: float
    realization_gate: float
    inv_skew_threshold: float
    position_mode: PositionMode
    preferred_side: PreferredSide = "BOTH"
    buy_price: float = 0.0
    sell_price: float = 0.0
    buy_qty: float = 0.0
    sell_qty: float = 0.0
    risk_exit_sell: bool = False
    risk_exit_buy: bool = False
    min_profitable_sell: float = 0.0
    max_profitable_buy: float = 0.0
    strict_profit_only: bool = False
    fifo_long_head_qty: float = 0.0
    fifo_short_head_qty: float = 0.0
    price_decimals: int = 2
    fifo_long_lots: list = field(default_factory=list)
    fifo_short_lots: list = field(default_factory=list)


def resolve_position_mode(
    has_long_fifo: bool,
    has_short_fifo: bool,
    fifo_long_qty: float,
    fifo_short_qty: float,
    fifo_threshold: float,
    inv_ratio: float,
    inv_skew_threshold: float,
) -> PositionMode:
    if has_long_fifo or has_short_fifo:
        if fifo_long_qty > fifo_short_qty + fifo_threshold:
            return "long_heavy"
        if fifo_short_qty > fifo_long_qty + fifo_threshold:
            return "short_heavy"
        return "balanced"
    if inv_ratio > inv_skew_threshold:
        return "long_heavy"
    if inv_ratio < -inv_skew_threshold:
        return "short_heavy"
    return "balanced"


def choose_preferred_side(ctx: BookCtx) -> PreferredSide:
    """Regime/tuner output → desired side only. Does not check FIFO legality."""
    if ctx.activity_mode <= 0.0:
        if ctx.inv_ratio > 0.01:
            return "SELL"
        if ctx.inv_ratio < -0.01:
            return "BUY"
        return "BOTH"

    if ctx.directional_gate > 0.5:
        if ctx.regime_dir > 0:
            return "BUY"
        if ctx.regime_dir < 0:
            return "SELL"

    if (
        ctx.position_mode == "balanced"
        and not ctx.has_long_fifo
        and not ctx.has_short_fifo
        and ctx.regime_state == "flat"
        and abs(ctx.pressure_score) <= 1e-9
    ):
        return "BOTH"

    if (
        ctx.position_mode == "balanced"
        and not ctx.has_long_fifo
        and not ctx.has_short_fifo
    ):
        return "BUY"

    return "BOTH"


def apply_balance_gate(
    allow: Allow,
    ctx: BookCtx,
    max_inventory: float,
    exchange_min_quantity: float,
) -> None:
    if ctx.position_delta >= max_inventory:
        allow.block("buy", "balance_max_long")
    if ctx.position_delta <= -max_inventory:
        allow.block("sell", "balance_max_short")
    if ctx.free_base < exchange_min_quantity:
        allow.block("sell", "balance_no_base")
    if ctx.free_quote < ctx.buy_price * ctx.buy_qty:
        allow.block("buy", "balance_no_quote")


def apply_side_gate(allow: Allow, ctx: BookCtx) -> None:
    """Apply preferred side + position mode (FIFO primary, inventory backup)."""
    side = ctx.preferred_side
    if side == "NONE":
        allow.block("both", "regime_none")
    elif side == "BUY":
        allow.block("sell", "regime_prefer_buy")
    elif side == "SELL":
        allow.block("buy", "regime_prefer_sell")

    if ctx.position_mode == "long_heavy":
        if not ctx.has_short_fifo:
            allow.block("buy", "position_long_heavy")
    elif ctx.position_mode == "short_heavy":
        allow.block("sell", "position_short_heavy")


def apply_inventory_reduce_gate(allow: Allow, ctx: BookCtx) -> None:
    """Flat FIFO but inventory skewed → only reducing side (replaces open_only gate)."""
    if ctx.has_long_fifo or ctx.has_short_fifo:
        return
    if ctx.inv_abs <= ctx.inv_skew_threshold:
        return
    if ctx.inv_ratio > 0.0:
        allow.block("buy", "inventory_skewed_long")
    elif ctx.inv_ratio < 0.0:
        allow.block("sell", "inventory_skewed_short")


def apply_fifo_profit_gate(
    allow: Allow,
    ctx: BookCtx,
    *,
    min_realize_edge: float,
    max_realize_dist_ticks: float,
    risk_exit_gate: float,
    risk_exit_size_frac: float,
    touch_back_ticks: float,
    sell_urgency: float,
    buy_urgency: float,
    price_decimals: int,
    rebalance_active: bool,
    pressure_threshold: float,
    trend_flat_threshold: float,
) -> None:
    if ctx.realization_gate <= 0.5 and not ctx.strict_profit_only:
        ctx.buy_qty *= buy_urgency
        ctx.sell_qty *= sell_urgency
        return

    tick = ctx.tick
    if ctx.strict_profit_only:
        risk_on = False
        min_realize_edge += tick * 2.0
    else:
        risk_on = risk_exit_gate > 0.5
    risk_sell = risk_on and _risk_exit_sell_signal(
        ctx.trend_score,
        ctx.pressure_score,
        ctx.regime_dir,
        pressure_threshold,
        trend_flat_threshold,
    )
    risk_buy = risk_on and _risk_exit_buy_signal(
        ctx.trend_score,
        ctx.pressure_score,
        ctx.regime_dir,
        pressure_threshold,
        trend_flat_threshold,
    )

    long_close = ctx.has_long_fifo and (
        ctx.position_mode == "long_heavy"
        or ctx.inv_ratio > 0.01
        or ctx.strict_profit_only
    )
    if long_close:
        touch_sell = round_decimals(
            ctx.best_ask
            + urgency_sell_touch_ticks(sell_urgency, touch_back_ticks, tick),
            price_decimals,
        )
        ctx.sell_qty *= sell_urgency
        ctx.min_profitable_sell = _long_close_entry_floor(
            ctx, ctx.sell_qty, min_realize_edge
        )

        force_risk = risk_sell and touch_sell < ctx.min_profitable_sell
        force_cut = False
        if max_realize_dist_ticks > 0.0:
            max_touch = ctx.best_ask + max_realize_dist_ticks * tick
            if ctx.min_profitable_sell > max_touch:
                force_cut = True

        if force_risk or force_cut:
            ctx.sell_price = touch_sell
            ctx.risk_exit_sell = True
            if force_cut:
                ctx.sell_qty = max(
                    ctx.sell_qty * risk_exit_size_frac,
                    ctx.sell_qty * 0.01,
                )
        else:
            ctx.sell_price = max(ctx.min_profitable_sell, touch_sell)
            if ctx.sell_price < ctx.min_profitable_sell:
                allow.block("sell", "fifo_unprofitable_sell")
            else:
                allow.sell = True

    elif ctx.has_long_fifo and risk_sell:
        ctx.sell_qty *= sell_urgency
        ctx.min_profitable_sell = _long_close_entry_floor(
            ctx, ctx.sell_qty, min_realize_edge
        )
        ctx.sell_price = round_decimals(
            ctx.best_ask
            + urgency_sell_touch_ticks(sell_urgency, touch_back_ticks, tick),
            price_decimals,
        )
        ctx.risk_exit_sell = True

    short_close = ctx.has_short_fifo and (
        ctx.position_mode == "short_heavy"
        or ctx.inv_ratio < -0.01
        or ctx.strict_profit_only
    )
    if short_close:
        buy_back = touch_back_ticks * tick
        if buy_urgency <= 0.25:
            buy_back *= 3.0
        elif buy_urgency < 0.80:
            buy_back *= 2.0
        touch_buy = round_decimals(ctx.best_bid - buy_back, price_decimals)
        ctx.buy_qty *= buy_urgency
        ctx.max_profitable_buy = _short_close_entry_ceiling(
            ctx, ctx.buy_qty, min_realize_edge
        )

        force_risk = risk_buy and touch_buy > ctx.max_profitable_buy
        force_cut = False
        if max_realize_dist_ticks > 0.0:
            min_touch = ctx.best_bid - max_realize_dist_ticks * tick
            if ctx.max_profitable_buy < min_touch:
                force_cut = True

        if force_risk or force_cut:
            ctx.buy_price = touch_buy
            ctx.risk_exit_buy = True
            if force_cut:
                ctx.buy_qty = max(
                    ctx.buy_qty * risk_exit_size_frac,
                    ctx.buy_qty * 0.01,
                )
        else:
            ctx.buy_price = min(ctx.max_profitable_buy, touch_buy)
            if ctx.buy_price > ctx.max_profitable_buy:
                allow.block("buy", "fifo_unprofitable_buy")
            else:
                allow.buy = True

    elif ctx.has_short_fifo and risk_buy:
        ctx.buy_qty *= buy_urgency
        ctx.max_profitable_buy = _short_close_entry_ceiling(
            ctx, ctx.buy_qty, min_realize_edge
        )
        buy_back = touch_back_ticks * tick
        ctx.buy_price = round_decimals(ctx.best_bid - buy_back, price_decimals)
        ctx.risk_exit_buy = True

    if (
        ctx.has_long_fifo
        and ctx.mid < ctx.fifo_long_head + min_realize_edge
        and ctx.position_mode != "short_heavy"
        and not ctx.has_short_fifo
    ):
        allow.block("buy", "fifo_underwater_long")

    if (
        rebalance_active
        and not ctx.strict_profit_only
        and ctx.realization_gate > 0.5
    ):
        if (
            ctx.position_mode == "short_heavy"
            and ctx.has_short_fifo
            and not ctx.risk_exit_buy
        ):
            touch_buy = round_decimals(
                ctx.best_bid - touch_back_ticks * tick, price_decimals
            )
            if touch_buy <= ctx.max_profitable_buy or ctx.max_profitable_buy <= 0.0:
                ctx.max_profitable_buy = _short_close_entry_ceiling(
                    ctx, ctx.buy_qty, min_realize_edge
                )
                ctx.buy_price = touch_buy
                allow.buy = True
            elif ctx.max_profitable_buy > 0.0:
                ctx.buy_price = round_decimals(ctx.max_profitable_buy, price_decimals)
                if ctx.buy_price < ctx.best_bid - tick:
                    allow.block("buy", "fifo_rebalance_wait")
        if (
            ctx.position_mode == "long_heavy"
            and ctx.has_long_fifo
            and not ctx.risk_exit_sell
        ):
            touch_sell = round_decimals(
                ctx.best_ask + touch_back_ticks * tick, price_decimals
            )
            ctx.min_profitable_sell = _long_close_entry_floor(
                ctx, ctx.sell_qty, min_realize_edge
            )
            if touch_sell >= ctx.min_profitable_sell:
                ctx.sell_price = touch_sell
                allow.sell = True
            else:
                ctx.sell_price = round_decimals(
                    ctx.min_profitable_sell, price_decimals
                )
                if ctx.sell_price > ctx.best_ask + tick:
                    allow.block("sell", "fifo_rebalance_wait")

    if ctx.strict_profit_only and ctx.has_long_fifo and not ctx.has_short_fifo:
        allow.block("buy", "strict_close_only")
    if ctx.strict_profit_only and ctx.has_short_fifo and not ctx.has_long_fifo:
        allow.block("sell", "strict_close_only")

    if ctx.realization_gate > 0.5 and not (ctx.risk_exit_sell or ctx.risk_exit_buy):
        if not ctx.has_long_fifo and not ctx.has_short_fifo:
            ctx.buy_qty *= buy_urgency
            ctx.sell_qty *= sell_urgency


def apply_fifo_close_price_floor(
    allow: Allow,
    ctx: BookCtx,
    *,
    min_realize_edge: float,
    price_decimals: int,
) -> None:
    """Hard clamp: never post a closing quote below FIFO break-even (+ edge)."""
    if ctx.risk_exit_sell or ctx.risk_exit_buy:
        return

    from fifo_pnl import predict_long_close_realized, predict_short_close_realized

    tick = ctx.tick
    if ctx.has_long_fifo and allow.sell:
        floor = _long_close_entry_floor(
            ctx, ctx.sell_qty, min_realize_edge, price_decimals=price_decimals
        )
        ctx.min_profitable_sell = floor
        if ctx.sell_price < floor:
            ctx.sell_price = round_decimals_up(floor, price_decimals)
        lots = _lots_as_fee_lots(
            ctx.fifo_long_lots,
            fee_rate=ctx.maker_fee_rate,
            head_price=ctx.fifo_long_head,
            head_qty=ctx.fifo_long_head_qty or ctx.sell_qty,
        )
        planned = min(max(ctx.sell_qty, 0.0), ctx.fifo_long_qty)
        if lots and planned > 0.0:
            pnl = predict_long_close_realized(
                lots, planned, ctx.sell_price, ctx.maker_fee_rate
            )
            if pnl + 1e-12 < _close_target_net_pnl(min_realize_edge, planned):
                allow.block("sell", "fifo_unprofitable_sell")
        if ctx.sell_price > ctx.best_ask + tick:
            allow.block("sell", "fifo_unprofitable_sell")

    if ctx.has_short_fifo and allow.buy:
        ceiling = _short_close_entry_ceiling(
            ctx, ctx.buy_qty, min_realize_edge, price_decimals=price_decimals
        )
        ctx.max_profitable_buy = ceiling
        if ctx.buy_price > ceiling:
            ctx.buy_price = round_decimals_down(ceiling, price_decimals)
        lots = _lots_as_fee_lots(
            ctx.fifo_short_lots,
            fee_rate=ctx.maker_fee_rate,
            head_price=ctx.fifo_short_head,
            head_qty=ctx.fifo_short_head_qty or ctx.buy_qty,
        )
        planned = min(max(ctx.buy_qty, 0.0), ctx.fifo_short_qty)
        if lots and planned > 0.0:
            pnl = predict_short_close_realized(
                lots, planned, ctx.buy_price, ctx.maker_fee_rate
            )
            if pnl + 1e-12 < _close_target_net_pnl(min_realize_edge, planned):
                allow.block("buy", "fifo_unprofitable_buy")
        if ctx.buy_price < ctx.best_bid - tick:
            allow.block("buy", "fifo_unprofitable_buy")


def cancel_unprofitable_close_orders(
    orders: list,
    book_id: int,
    open_orders: list,
    ctx: BookCtx,
    *,
    min_realize_edge: float,
    skip_ids: set,
) -> int:
    """Cancel resting quotes that would realize a FIFO loss if filled."""
    from fifo_pnl import predict_long_close_realized, predict_short_close_realized

    cancelled = 0
    for order in open_orders:
        order_id = _v(order, "id", None)
        if order_id is None or order_id in skip_ids:
            continue
        order_price = _v(order, "price", 0.0)
        if order_price <= 0.0:
            continue
        direction = str(_v(order, "direction", "")).upper()
        order_qty = float(_v(order, "quantity", 0.0) or 0.0)
        if ctx.has_long_fifo and direction == "SELL":
            planned = min(
                max(order_qty, ctx.sell_qty),
                ctx.fifo_long_qty,
            )
            lots = _lots_as_fee_lots(
                ctx.fifo_long_lots,
                fee_rate=ctx.maker_fee_rate,
                head_price=ctx.fifo_long_head,
                head_qty=ctx.fifo_long_head_qty or planned,
            )
            if lots and planned > 0.0:
                pnl = predict_long_close_realized(
                    lots, planned, order_price, ctx.maker_fee_rate
                )
                if pnl + 1e-12 < _close_target_net_pnl(min_realize_edge, planned):
                    add_cancel_order(orders, book_id, order)
                    skip_ids.add(order_id)
                    cancelled += 1
            else:
                floor = _long_close_entry_floor(
                    ctx, planned, min_realize_edge, price_decimals=ctx.price_decimals
                )
                if order_price < floor - 1e-12:
                    add_cancel_order(orders, book_id, order)
                    skip_ids.add(order_id)
                    cancelled += 1
        elif ctx.has_short_fifo and direction == "BUY":
            planned = min(
                max(order_qty, ctx.buy_qty),
                ctx.fifo_short_qty,
            )
            lots = _lots_as_fee_lots(
                ctx.fifo_short_lots,
                fee_rate=ctx.maker_fee_rate,
                head_price=ctx.fifo_short_head,
                head_qty=ctx.fifo_short_head_qty or planned,
            )
            if lots and planned > 0.0:
                pnl = predict_short_close_realized(
                    lots, planned, order_price, ctx.maker_fee_rate
                )
                if pnl + 1e-12 < _close_target_net_pnl(min_realize_edge, planned):
                    add_cancel_order(orders, book_id, order)
                    skip_ids.add(order_id)
                    cancelled += 1
            else:
                ceiling = _short_close_entry_ceiling(
                    ctx, planned, min_realize_edge, price_decimals=ctx.price_decimals
                )
                if order_price > ceiling + 1e-12:
                    add_cancel_order(orders, book_id, order)
                    skip_ids.add(order_id)
                    cancelled += 1
    return cancelled


def apply_final_safety_gate(
    allow: Allow,
    ctx: BookCtx,
    exchange_min_quantity: float,
    max_quantity: float,
    volume_decimals: int,
) -> None:
    ctx.buy_qty = finalize_order_quantity(
        ctx.buy_qty, max_quantity, exchange_min_quantity, volume_decimals
    )
    ctx.sell_qty = finalize_order_quantity(
        ctx.sell_qty, max_quantity, exchange_min_quantity, volume_decimals
    )
    if ctx.buy_qty < exchange_min_quantity:
        allow.block("buy", "safety_min_qty")
    if ctx.sell_qty < exchange_min_quantity:
        allow.block("sell", "safety_min_qty")
    if ctx.free_base < ctx.sell_qty:
        allow.block("sell", "safety_base")
    if ctx.free_quote < ctx.buy_qty * ctx.buy_price:
        allow.block("buy", "safety_quote")
    if ctx.buy_price >= ctx.sell_price and allow.buy and allow.sell:
        allow.block("buy", "safety_crossed_quotes")


def _reason_flags(reasons: list[str]) -> dict:
    joined = " ".join(reasons)
    return {
        "blocked_buy_regime": "regime" in joined and ":buy" in joined,
        "blocked_sell_regime": "regime" in joined and ":sell" in joined,
        "blocked_buy_fifo": "position_long_heavy" in joined,
        "blocked_sell_fifo": "position_short_heavy" in joined,
        "blocked_sell_realization": "fifo_unprofitable_sell" in joined
        or "fifo_rebalance_wait:sell" in joined,
        "blocked_buy_realization": "fifo_unprofitable_buy" in joined
        or "fifo_rebalance_wait:buy" in joined,
        "blocked_buy_underwater_long": "fifo_underwater_long" in joined,
        "blocked_inventory_skew": "inventory_skewed" in joined,
    }


def apply_phase1_roundtrip_gate(
    output: dict,
    book: dict,
    book_id: int,
    input: dict,
    exchange_min_quantity: float,
    log: dict,
) -> tuple[bool, bool]:
    """Phase-1 per-book roundtrip cap. Returns (skip_book, strict_profit_only)."""
    scoring_phase = int(_v(input, "scoring_phase", 1))
    if scoring_phase != 1:
        return False, bool(
            _v(book, "strict_profit_only", _v(input, "strict_profit_only", False))
        )
    if not bool(int(_v(input, "phase1_roundtrip_limit_enabled", 0))):
        return False, bool(
            _v(book, "strict_profit_only", _v(input, "strict_profit_only", False))
        )
    max_rt = int(_v(input, "phase1_max_roundtrips", 0))
    if max_rt <= 0:
        return False, bool(
            _v(book, "strict_profit_only", _v(input, "strict_profit_only", False))
        )

    rt_count = int(_v(book, "phase1_roundtrip_count", 0))
    long_qty = float(_v(book, "fifo_long_total_qty", 0.0))
    short_qty = float(_v(book, "fifo_short_total_qty", 0.0))
    flat_qty = max(exchange_min_quantity * 0.1, 1e-6)
    flat = abs(long_qty - short_qty) <= flat_qty
    log["phase1_roundtrip_count"] = rt_count
    log["phase1_max_roundtrips"] = max_rt

    base_strict = bool(
        _v(book, "strict_profit_only", _v(input, "strict_profit_only", False))
    )
    if rt_count >= max_rt and flat:
        open_orders = read_open_orders(book)
        cancelled = 0
        for order in open_orders:
            if _v(order, "id", None) is not None:
                add_cancel_order(output["orders"], book_id, order)
                cancelled += 1
        log["cancelled"] = cancelled
        log["decision"] = "phase1_roundtrip_done"
        log["action"] = "phase1_wait"
        return True, base_strict

    if rt_count >= max_rt or not flat:
        return False, True
    return False, base_strict


def _decision_from_allow(allow: Allow, ctx: BookCtx) -> str:
    if allow.buy and allow.sell:
        return "quote_both"
    if allow.buy:
        return "quote_buy_only"
    if allow.sell:
        return "quote_sell_only"
    if any("fifo_unprofitable" in r or "fifo_rebalance_wait" in r for r in allow.reasons):
        return "wait_profitable_close"
    if any("regime" in r for r in allow.reasons):
        return "skip_adverse_direction"
    if any("inventory_skewed" in r for r in allow.reasons):
        return "skip_inventory_skew"
    return "quote_none"


def process_topstrategy(input):
    try:
        base_quantity = _v(input, "quantity", 1.0)
        exchange_min_quantity = _v(input, "exchange_min_quantity", 0.25)
        min_quantity = max(
            _v(input, "min_quantity", base_quantity * 0.25),
            exchange_min_quantity,
        )
        max_quantity = _v(input, "max_quantity", base_quantity * 1.5)
        price_decimals = _v(input, "price_decimals", 2)
        volume_decimals = _v(input, "volume_decimals", 4)
        book_depth = _v(input, "book_depth", _v(input, "imbalance_depth", 5))
        max_inventory = _v(input, "max_inventory", 100.0)
        default_neutral_position = _v(input, "neutral_position", 0.0)
        quote_width_ticks = _v(input, "quote_width_ticks", 2.0)
        inventory_skew = _v(input, "inventory_skew", 4.0)
        min_spread_ticks = _v(input, "min_spread_ticks", 1.0)
        touch_back_ticks = _v(input, "touch_back_ticks", 0.0)
        activity_mode = _v(input, "activity_mode", 1.0)
        inventory_reduce_buy_ratio = _v(input, "inventory_reduce_buy_ratio", 0.25)
        inventory_reduce_sell_ratio = _v(
            input, "inventory_reduce_sell_ratio", 0.25
        )
        imbalance_skew_ticks = _v(input, "imbalance_skew_ticks", 0.0)
        momentum_skew_ticks = _v(input, "momentum_skew_ticks", 0.0)
        directional_gate = _v(input, "directional_gate", 1.0)
        cancel_fair_move_ticks = _v(input, "cancel_fair_move_ticks", 2.0)
        realization_gate = _v(input, "realization_gate", 1.0)
        min_realize_edge_ticks = _v(input, "min_realize_edge_ticks", 1.0)
        max_realize_dist_ticks = _v(input, "max_realize_dist_ticks", 3.0)
        risk_exit_size_frac = _v(
            input,
            "risk_exit_size_frac",
            _v(input, "loss_cut_size_frac", 0.10),
        )
        risk_exit_gate = _v(
            input,
            "risk_exit_gate",
            max(
                _v(input, "bearish_loss_sell_gate", 0.0),
                _v(input, "bullish_loss_buy_gate", 0.0),
            ),
        )
        rebalance_gate = _v(input, "rebalance_gate", 0.0)
        reversal_threshold = _v(input, "reversal_threshold", 0.25)
        trend_flat_threshold = _v(input, "trend_flat_threshold", 0.50)
        open_only_inv_ratio = _v(input, "open_only_inv_ratio", 0.15)
        fifo_qty_threshold = _v(
            input, "fifo_qty_threshold", base_quantity * 0.5
        )
        pressure_imbalance_threshold = _v(
            input, "pressure_imbalance_threshold", 0.15
        )
        expiry_period = _v(input, "expiry_period", 10000000000)

        output: dict = {"orders": [], "logs": []}
        if "books" not in input or not isinstance(input["books"], list):
            return output

        tick = pow(10.0, -price_decimals)
        rebalance_active = rebalance_gate > 0.5

        for book in input["books"]:
            log: dict = {}
            book_id = _v(book, "id", 0)
            log["book_id"] = book_id

            book_quantity = _v(book, "quantity", base_quantity)
            book_activity_mode = _v(book, "activity_mode", activity_mode)
            book_directional_gate = _v(book, "directional_gate", directional_gate)
            book_regime = _v(book, "book_regime", "")
            book_realization_gate = _v(book, "realization_gate", realization_gate)
            inv_skew_threshold = _v(
                book, "open_only_inv_ratio", open_only_inv_ratio
            )
            book_max_realize_dist = _v(
                book, "max_realize_dist_ticks", max_realize_dist_ticks
            )
            book_risk_exit_gate = _v(book, "risk_exit_gate", risk_exit_gate)

            if book_regime:
                log["book_regime"] = book_regime
            log["book_quantity"] = book_quantity
            log["book_activity_mode"] = book_activity_mode
            log["book_realization_gate"] = book_realization_gate
            log["book_directional_gate"] = book_directional_gate
            log["inv_skew_threshold"] = inv_skew_threshold

            best_bid = _v(book, "best_bid", 0.0)
            best_ask = _v(book, "best_ask", 0.0)
            log["best_bid"] = best_bid
            log["best_ask"] = best_ask

            # 1. Market validity gate
            if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
                log["action"] = "skip_bad_market"
                output["logs"].append(log)
                continue

            mid = _v(book, "midquote", (best_bid + best_ask) * 0.5)
            spread_ticks = (best_ask - best_bid) / tick
            log["mid"] = mid
            log["spread_ticks"] = spread_ticks

            if spread_ticks < min_spread_ticks:
                log["action"] = "skip_tight_spread"
                output["logs"].append(log)
                continue

            bids = _v(book, "bids", [])
            asks = _v(book, "asks", [])
            if level_volume_sum(bids, book_depth) + level_volume_sum(asks, book_depth) <= 0.0:
                log["action"] = "skip_no_depth"
                output["logs"].append(log)
                continue

            maker_fee_rate = _v(book, "maker_fee_rate", 0.0)

            if bool(_v(book, "at_volume_cap", False)):
                log["action"] = "skip_volume_cap"
                log["decision"] = "observe_volume_cap"
                output["logs"].append(log)
                continue

            skip_book, book_strict_profit_only = apply_phase1_roundtrip_gate(
                output,
                book,
                book_id,
                input,
                exchange_min_quantity,
                log,
            )
            if skip_book:
                output["logs"].append(log)
                continue

            position = read_position(book)
            book_neutral = _v(book, "neutral_position", default_neutral_position)
            position_delta = position - book_neutral
            inv_ratio = clamp(position_delta / max(max_inventory, 1e-9), -1.0, 1.0)
            inv_abs = abs(inv_ratio)
            free_base = read_free_base(book)
            free_quote = read_free_quote(book)

            trend_score = _v(book, "trend_score", 0.0)
            trend_score_prev = _v(book, "trend_score_prev", trend_score)
            pressure_score = _v(book, "pressure_score", 0.0)
            reversal_score = _v(
                book, "reversal_score", trend_score - trend_score_prev
            )
            regime_state = classify_regime_state(
                trend_score,
                pressure_score,
                reversal_score,
                pressure_imbalance_threshold,
                reversal_threshold,
                trend_flat_threshold,
            )
            regime_dir = regime_direction(regime_state)

            fifo_long_head = _v(book, "fifo_long_head_price", 0.0)
            fifo_short_head = _v(book, "fifo_short_head_price", 0.0)
            fifo_long_qty = _v(
                book,
                "fifo_long_total_qty",
                _v(book, "fifo_long_head_qty", 0.0),
            )
            fifo_short_qty = _v(
                book,
                "fifo_short_total_qty",
                _v(book, "fifo_short_head_qty", 0.0),
            )
            fifo_long_head_qty = _v(book, "fifo_long_head_qty", 0.0)
            fifo_short_head_qty = _v(book, "fifo_short_head_qty", 0.0)
            fifo_long_lots = _read_fifo_lots_with_fees(
                book, "fifo_long_lots", open_fee_rate_fallback=maker_fee_rate
            )
            fifo_short_lots = _read_fifo_lots_with_fees(
                book, "fifo_short_lots", open_fee_rate_fallback=maker_fee_rate
            )
            has_long_fifo = fifo_long_head > 0.0
            has_short_fifo = fifo_short_head > 0.0
            fifo_bootstrapped = bool(
                _v(book, "fifo_long_bootstrapped", False)
                or _v(book, "fifo_short_bootstrapped", False)
            )

            kappa_bootstrap = _v(book, "kappa_bootstrap", False)
            if kappa_bootstrap and "realization_gate" not in book:
                book_realization_gate = min(realization_gate, 0.25)

            fifo_threshold = max(fifo_qty_threshold, book_quantity * 0.5)
            position_mode = resolve_position_mode(
                has_long_fifo,
                has_short_fifo,
                fifo_long_qty,
                fifo_short_qty,
                fifo_threshold,
                inv_ratio,
                inv_skew_threshold,
            )

            book_imbalance = pressure_score
            trend_slope_ticks = _v(
                book, "trend_slope_ticks", _v(book, "mid_delta_ticks", 0.0)
            )
            inv_sign = 1.0 if inv_ratio >= 0.0 else -1.0
            fair = (
                mid
                - inv_sign * inv_abs * inv_abs * inventory_skew * tick
                + book_imbalance * imbalance_skew_ticks * tick
                + trend_slope_ticks * momentum_skew_ticks * tick
            )

            half_width = quote_width_ticks + 0.20 * max(0.0, spread_ticks - 1.0)
            # One maker fee per quote side; FIFO closes use fee_aware_* on entry.
            fee_quote_skew = maker_fee_rate * mid
            buy_price = round_decimals(
                min(
                    fair - half_width * tick - fee_quote_skew,
                    best_bid - touch_back_ticks * tick,
                ),
                price_decimals,
            )
            sell_price = round_decimals(
                max(
                    fair + half_width * tick + fee_quote_skew,
                    best_ask + touch_back_ticks * tick,
                ),
                price_decimals,
            )
            if buy_price <= 0.0 or sell_price <= 0.0 or buy_price >= sell_price:
                log["action"] = "skip_invalid_quotes"
                output["logs"].append(log)
                continue

            buy_qty = book_quantity
            sell_qty = book_quantity
            book_max_quantity = float(max_quantity)
            if inv_ratio > 0.01:
                buy_qty *= max(0.0, 1.0 - inventory_reduce_buy_ratio * inv_abs)
            if inv_ratio < -0.01:
                sell_qty *= max(0.0, 1.0 - inventory_reduce_sell_ratio * inv_abs)

            sell_urgency = compute_sell_urgency(
                trend_score,
                pressure_score,
                reversal_score,
                pressure_imbalance_threshold,
                reversal_threshold,
                trend_flat_threshold,
            )
            buy_urgency = compute_buy_urgency(
                trend_score,
                pressure_score,
                reversal_score,
                pressure_imbalance_threshold,
                reversal_threshold,
                trend_flat_threshold,
            )

            ctx = BookCtx(
                book_id=book_id,
                tick=tick,
                mid=mid,
                best_bid=best_bid,
                best_ask=best_ask,
                spread_ticks=spread_ticks,
                position_delta=position_delta,
                inv_ratio=inv_ratio,
                inv_abs=inv_abs,
                free_base=free_base,
                free_quote=free_quote,
                maker_fee_rate=maker_fee_rate,
                has_long_fifo=has_long_fifo,
                has_short_fifo=has_short_fifo,
                fifo_long_head=fifo_long_head,
                fifo_short_head=fifo_short_head,
                fifo_long_qty=fifo_long_qty,
                fifo_short_qty=fifo_short_qty,
                fifo_long_head_qty=fifo_long_head_qty,
                fifo_short_head_qty=fifo_short_head_qty,
                price_decimals=price_decimals,
                fifo_long_lots=fifo_long_lots,
                fifo_short_lots=fifo_short_lots,
                fifo_bootstrapped=fifo_bootstrapped,
                regime_state=regime_state,
                regime_dir=regime_dir,
                pressure_score=pressure_score,
                trend_score=trend_score,
                activity_mode=book_activity_mode,
                directional_gate=book_directional_gate,
                realization_gate=book_realization_gate,
                inv_skew_threshold=inv_skew_threshold,
                position_mode=position_mode,
                buy_price=buy_price,
                sell_price=sell_price,
                buy_qty=buy_qty,
                sell_qty=sell_qty,
                strict_profit_only=book_strict_profit_only,
            )
            ctx.preferred_side = choose_preferred_side(ctx)

            open_orders = read_open_orders(book)
            cancelled_ids: set = set()
            cancelled = 0
            for order in open_orders:
                order_id = _v(order, "id", None)
                order_price = _v(order, "price", 0.0)
                if order_price <= 0.0:
                    continue
                if abs(fair - order_price) / max(tick, 1e-12) >= cancel_fair_move_ticks:
                    add_cancel_order(output["orders"], book_id, order)
                    if order_id is not None:
                        cancelled_ids.add(order_id)
                    cancelled += 1

            target_edge = min_realize_edge_ticks * tick
            if fifo_bootstrapped:
                target_edge += tick
            if book_realization_gate > 0.5 and inv_abs > 0.35:
                target_edge += tick * (inv_abs - 0.35) * 2.0
            if (
                book_realization_gate > 0.5
                and book_max_realize_dist <= 0.0
                and book_risk_exit_gate <= 0.5
                and position_mode in ("long_heavy", "short_heavy")
            ):
                target_edge += tick * 0.75
            min_realize_edge = target_edge

            allow = Allow()
            apply_balance_gate(allow, ctx, max_inventory, exchange_min_quantity)
            apply_side_gate(allow, ctx)
            apply_inventory_reduce_gate(allow, ctx)
            apply_fifo_profit_gate(
                allow,
                ctx,
                min_realize_edge=min_realize_edge,
                max_realize_dist_ticks=book_max_realize_dist,
                risk_exit_gate=book_risk_exit_gate,
                risk_exit_size_frac=risk_exit_size_frac,
                touch_back_ticks=touch_back_ticks,
                sell_urgency=sell_urgency,
                buy_urgency=buy_urgency,
                price_decimals=price_decimals,
                rebalance_active=rebalance_active and not book_strict_profit_only,
                pressure_threshold=pressure_imbalance_threshold,
                trend_flat_threshold=trend_flat_threshold,
            )
            apply_fifo_close_price_floor(
                allow,
                ctx,
                min_realize_edge=min_realize_edge,
                price_decimals=price_decimals,
            )
            cancelled += cancel_unprofitable_close_orders(
                output["orders"],
                book_id,
                open_orders,
                ctx,
                min_realize_edge=min_realize_edge,
                skip_ids=cancelled_ids,
            )
            apply_final_safety_gate(
                allow, ctx, exchange_min_quantity, book_max_quantity, volume_decimals
            )

            take_sell = False
            take_buy = False

            sent = 0
            if take_buy:
                add_limit_order(
                    output["orders"],
                    book_id,
                    "BUY",
                    ctx.buy_qty,
                    ctx.buy_price,
                    expiry_period,
                    exchange_min_quantity,
                    post_only=False,
                    time_in_force="FOK",
                )
                sent += 1
            elif allow.buy:
                add_limit_order(
                    output["orders"],
                    book_id,
                    "BUY",
                    ctx.buy_qty,
                    ctx.buy_price,
                    expiry_period,
                    exchange_min_quantity,
                )
                sent += 1
            if take_sell:
                add_limit_order(
                    output["orders"],
                    book_id,
                    "SELL",
                    ctx.sell_qty,
                    ctx.sell_price,
                    expiry_period,
                    exchange_min_quantity,
                    post_only=False,
                    time_in_force="FOK",
                )
                sent += 1
            elif allow.sell:
                add_limit_order(
                    output["orders"],
                    book_id,
                    "SELL",
                    ctx.sell_qty,
                    ctx.sell_price,
                    expiry_period,
                    exchange_min_quantity,
                )
                sent += 1

            flags = _reason_flags(allow.reasons)
            log.update(
                {
                    "inventory_ratio": inv_ratio,
                    "position_mode": position_mode,
                    "fifo_mode": position_mode,
                    "preferred_side": ctx.preferred_side,
                    "regime": regime_state,
                    "regime_direction": regime_dir,
                    "fair": fair,
                    "fee_quote_skew": fee_quote_skew,
                    "cancelled": cancelled,
                    "gate_reasons": allow.reasons,
                    "allow_buy": allow.buy or take_buy,
                    "allow_sell": allow.sell or take_sell,
                    "buy_price": ctx.buy_price,
                    "sell_price": ctx.sell_price,
                    "buy_quantity": ctx.buy_qty,
                    "sell_quantity": ctx.sell_qty,
                    "orders_sent": sent,
                    "risk_exit_sell": ctx.risk_exit_sell,
                    "risk_exit_buy": ctx.risk_exit_buy,
                    "loss_cut_sell": ctx.risk_exit_sell and book_max_realize_dist > 0.0,
                    "loss_cut_buy": ctx.risk_exit_buy and book_max_realize_dist > 0.0,
                    "bearish_loss_sell": ctx.risk_exit_sell,
                    "bullish_loss_buy": ctx.risk_exit_buy,
                    "min_profitable_sell": ctx.min_profitable_sell,
                    "max_profitable_buy": ctx.max_profitable_buy,
                    "min_realize_edge": min_realize_edge,
                    "decision": _decision_from_allow(allow, ctx),
                    "action": "cancel_and_quote" if sent > 0 else "cancel_or_wait",
                }
            )
            log.update(flags)
            output["logs"].append(log)

        return output

    except Exception as ex:
        return {"error": str(ex), "orders": [], "logs": []}


def _micro_fifo_long_closeable(
    long_qty: float, close_qty: float, flat_qty: float
) -> bool:
    """True when FIFO has enough long lots to close without opening a short."""
    if close_qty <= 0.0:
        return False
    return long_qty + flat_qty + 1e-9 >= close_qty


def _micro_fifo_short_closeable(
    short_qty: float, close_qty: float, flat_qty: float
) -> bool:
    """True when FIFO has enough short lots to cover without opening a long."""
    if close_qty <= 0.0:
        return False
    return short_qty + flat_qty + 1e-9 >= close_qty


def _micro_edge_sell_price_total(
    entry: float,
    qty: float,
    target_net_pnl: float,
    open_maker_fee_rate: float,
    close_taker_fee_rate: float,
    price_decimals: int,
    *,
    close_fok: bool = False,
) -> float:
    if close_fok:
        target = fee_aware_long_close_total_pnl_fok(
            entry,
            qty,
            target_net_pnl,
            open_maker_fee_rate,
            close_taker_fee_rate,
        )
    else:
        target = fee_aware_long_close_total_pnl(
            entry,
            qty,
            target_net_pnl,
            open_maker_fee_rate,
        )
    if target_net_pnl >= 0.0:
        return round_decimals_up(target, price_decimals)
    return round_decimals_down(target, price_decimals)


def _micro_close_order_flags(close_fok: bool) -> tuple[str, bool]:
    if close_fok:
        return "FOK", False
    return "GTC", True


def take_pnl_meets_target(
    pred_pnl: float,
    target_net_pnl: float,
    max_net_pnl: float | None = None,
) -> bool:
    """True when a taker fill's predicted PnL meets a positive profit target.

    Requires target > 0 so a zero/negative target cannot force eager FOK takes.
    When max_net_pnl > 0, also reject windfalls above the cap (e.g. target 0.03
    but touch would realize 0.05–0.06).
    """
    target = float(target_net_pnl)
    if target <= 0.0:
        return False
    pred = float(pred_pnl)
    if pred + 1e-12 < target:
        return False
    if max_net_pnl is not None and float(max_net_pnl) > 0.0:
        if pred > float(max_net_pnl) + 1e-12:
            return False
    return True


def _drop_new_book_limits(
    orders: list,
    book_id: int,
    direction: str,
) -> int:
    """Remove same-tick limit orders on book/side (keeps cancels). Returns dropped count."""
    want = str(direction).upper()
    kept: list = []
    dropped = 0
    for order in orders:
        if int(_v(order, "book_id", -1)) != int(book_id):
            kept.append(order)
            continue
        if str(_v(order, "type", "")).lower() == "cancel":
            kept.append(order)
            continue
        if str(_v(order, "direction", "")).upper() == want:
            dropped += 1
            continue
        kept.append(order)
    if dropped:
        orders[:] = kept
    return dropped


def _fifo_lots_for_take_predict(
    book: dict,
    *,
    side: Literal["long", "short"],
    qty: float,
    maker_fee_rate: float,
    entry: float,
) -> list[tuple[float, float, float]]:
    key = "fifo_long_lots" if side == "long" else "fifo_short_lots"
    lots = _read_fifo_lots_with_fees(
        book, key, open_fee_rate_fallback=maker_fee_rate
    )
    if lots or entry <= 0.0 or qty <= 0.0:
        return lots
    return [(qty, entry, entry * qty * maker_fee_rate)]


def _micro_order_price_stale(
    order_price: float,
    target_price: float,
    tick: float,
    price_decimals: int,
    *,
    tolerance_ticks: float = 1.0,
) -> bool:
    """True when a resting open/close order price differs from target beyond tolerance."""
    if target_price <= 0.0:
        return False
    order_r = round_decimals(order_price, price_decimals)
    target_r = round_decimals(target_price, price_decimals)
    return abs(order_r - target_r) > tick * tolerance_ticks + 1e-12


def _resting_long_close_fee_unprofitable(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    order_price: float,
    close_fee_rate: float,
    target_net_pnl: float,
) -> bool:
    """True when a resting long close would miss target under current close fee rate."""
    if (
        not lots
        or close_qty <= 0.0
        or order_price <= 0.0
        or float(target_net_pnl) <= 0.0
    ):
        return False
    from fifo_pnl import predict_long_close_realized

    pred = predict_long_close_realized(
        lots, close_qty, order_price, close_fee_rate
    )
    return float(pred) + 1e-12 < float(target_net_pnl)


def _resting_short_close_fee_unprofitable(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    order_price: float,
    close_fee_rate: float,
    target_net_pnl: float,
) -> bool:
    """True when a resting short cover would miss target under current close fee rate."""
    if (
        not lots
        or close_qty <= 0.0
        or order_price <= 0.0
        or float(target_net_pnl) <= 0.0
    ):
        return False
    from fifo_pnl import predict_short_close_realized

    pred = predict_short_close_realized(
        lots, close_qty, order_price, close_fee_rate
    )
    return float(pred) + 1e-12 < float(target_net_pnl)


def micro_fifo_long_close_sell_price(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_net_pnl: float,
    close_fee_rate: float,
    price_decimals: int,
    *,
    tick: float | None = None,
    clamp_profit: bool = True,
    close_price_offset: float = 0.0,
) -> tuple[float, dict]:
    """FIFO-accurate micro close sell price with optional tick-down if still profitable."""
    from fifo_pnl import predict_long_close_realized, solve_long_close_sell_price

    if tick is None:
        tick = pow(10.0, -price_decimals)
    trace: dict = {
        "pricing": "fifo_accurate",
        "close_qty": close_qty,
        "target_net_pnl": target_net_pnl,
        "close_fee_rate": close_fee_rate,
        "fifo_lots": [
            {"qty": q, "price": round(p, 8), "open_fee": round(f, 8)}
            for q, p, f in lots
        ],
    }
    if close_qty <= 0.0 or not lots:
        trace["error"] = "missing_lots"
        return 0.0, trace

    raw = solve_long_close_sell_price(
        lots, close_qty, target_net_pnl, close_fee_rate
    )
    trace["raw_sell_price"] = round(raw, 8)
    if target_net_pnl >= 0.0:
        price = round_decimals_up(raw, price_decimals)
    else:
        price = round_decimals_down(raw, price_decimals)

    tick_adjustments = 0
    if clamp_profit and target_net_pnl <= 0.0:
        while price > 0.0:
            predicted = predict_long_close_realized(
                lots, close_qty, price, close_fee_rate
            )
            if predicted <= 0.0:
                break
            price = round_decimals_down(price - tick, price_decimals)
            tick_adjustments += 1
            if tick_adjustments > 10000:
                break

    trace["tick_down_adjustments"] = tick_adjustments
    trace["rounded_sell_price"] = price
    if close_price_offset > 0.0:
        trace["close_price_offset"] = close_price_offset
        price = round_decimals_down(price - close_price_offset, price_decimals)
        trace["offset_sell_price"] = price
    trace["predicted_pnl"] = round(
        predict_long_close_realized(lots, close_qty, price, close_fee_rate), 8
    )
    remaining = close_qty
    cost = 0.0
    open_fees = 0.0
    for lot_qty, lot_price, lot_fee in lots:
        if remaining <= 1e-12:
            break
        take = min(lot_qty, remaining)
        cost += take * lot_price
        open_fees += lot_fee * take / lot_qty if lot_qty > 0.0 else 0.0
        remaining -= take
    trace["fifo_cost_basis"] = round(cost, 8)
    trace["fifo_open_fees"] = round(open_fees, 8)
    return price, trace


def micro_fifo_short_close_buy_price(
    lots: list[tuple[float, float, float]],
    close_qty: float,
    target_net_pnl: float,
    close_fee_rate: float,
    price_decimals: int,
    *,
    tick: float | None = None,
) -> tuple[float, dict]:
    """FIFO-accurate max buy price to cover a short for target net PnL."""
    from fifo_pnl import predict_short_close_realized, solve_short_close_buy_price

    if tick is None:
        tick = pow(10.0, -price_decimals)
    trace: dict = {
        "pricing": "fifo_accurate_short",
        "close_qty": close_qty,
        "target_net_pnl": target_net_pnl,
        "close_fee_rate": close_fee_rate,
        "fifo_lots": [
            {"qty": q, "price": round(p, 8), "open_fee": round(f, 8)}
            for q, p, f in lots
        ],
    }
    if close_qty <= 0.0 or not lots:
        trace["error"] = "missing_lots"
        return 0.0, trace

    raw = solve_short_close_buy_price(
        lots, close_qty, target_net_pnl, close_fee_rate
    )
    trace["raw_buy_price"] = round(raw, 8)
    if target_net_pnl >= 0.0:
        price = round_decimals_down(raw, price_decimals)
    else:
        price = round_decimals_up(raw, price_decimals)

    for _ in range(10000):
        predicted = predict_short_close_realized(
            lots, close_qty, price, close_fee_rate
        )
        if predicted + 1e-12 >= target_net_pnl:
            break
        if price <= tick:
            break
        price = round_decimals_down(price - tick, price_decimals)

    trace["rounded_buy_price"] = price
    trace["predicted_pnl"] = round(
        predict_short_close_realized(lots, close_qty, price, close_fee_rate), 8
    )
    return price, trace


def micro_sell_price_trace(
    entry: float,
    qty: float,
    target_net_pnl: float,
    open_maker_fee_rate: float,
    price_decimals: int,
    *,
    close_fok: bool = False,
    close_taker_fee_rate: float | None = None,
    micro_entry: float = 0.0,
    close_entry_source: str = "",
    best_bid: float = 0.0,
    best_ask: float = 0.0,
    fifo_lots: list[tuple[float, float, float]] | None = None,
    tick: float | None = None,
    clamp_profit: bool = True,
    close_price_offset: float = 0.0,
) -> dict:
    """Breakdown of micro await_close sell pricing for logging / diagnostics."""
    taker_fee = (
        open_maker_fee_rate
        if close_taker_fee_rate is None
        else close_taker_fee_rate
    )
    per_unit = target_net_pnl / qty if qty > 0.0 else 0.0
    close_fee_rate = taker_fee if close_fok else open_maker_fee_rate
    if fifo_lots:
        _, fifo_trace = micro_fifo_long_close_sell_price(
            fifo_lots,
            qty,
            target_net_pnl,
            close_fee_rate,
            price_decimals,
            tick=tick,
            clamp_profit=clamp_profit,
            close_price_offset=close_price_offset,
        )
        return {
            **fifo_trace,
            "close_mode": "fok" if close_fok else "maker",
            "close_entry": round(entry, 8),
            "close_entry_source": close_entry_source,
            "micro_entry": round(micro_entry, 8) if micro_entry > 0.0 else None,
            "target_per_unit": round(per_unit, 8),
            "maker_fee_rate": open_maker_fee_rate,
            "taker_fee_rate": taker_fee if close_fok else open_maker_fee_rate,
            "price_decimals": price_decimals,
            "best_bid": best_bid,
            "best_ask": best_ask,
        }
    if close_fok:
        raw = fee_aware_long_close_total_pnl_fok(
            entry, qty, target_net_pnl, open_maker_fee_rate, taker_fee
        )
    else:
        raw = fee_aware_long_close_total_pnl(
            entry, qty, target_net_pnl, open_maker_fee_rate
        )
    rounded = _micro_edge_sell_price_total(
        entry,
        qty,
        target_net_pnl,
        open_maker_fee_rate,
        taker_fee,
        price_decimals,
        close_fok=close_fok,
    )
    offset_price = rounded
    if close_price_offset > 0.0:
        offset_price = round_decimals_down(rounded - close_price_offset, price_decimals)
    trace = {
        "pricing": "single_entry",
        "close_mode": "fok" if close_fok else "maker",
        "close_entry": round(entry, 8),
        "close_entry_source": close_entry_source,
        "micro_entry": round(micro_entry, 8) if micro_entry > 0.0 else None,
        "sell_qty": qty,
        "target_net_pnl": target_net_pnl,
        "target_per_unit": round(per_unit, 8),
        "maker_fee_rate": open_maker_fee_rate,
        "taker_fee_rate": taker_fee if close_fok else open_maker_fee_rate,
        "raw_sell_price": round(raw, 8),
        "rounded_sell_price": rounded,
        "price_decimals": price_decimals,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }
    if close_price_offset > 0.0:
        trace["close_price_offset"] = close_price_offset
        trace["offset_sell_price"] = offset_price
    return trace


def _handle_micro_book(
    output: dict,
    *,
    book: dict,
    book_id: int,
    log: dict,
    best_bid: float,
    best_ask: float,
    tick: float,
    price_decimals: int,
    volume_decimals: int,
    exchange_min_quantity: float,
    max_quantity: float,
    expiry_period: int,
    elapsed_min: float,
    micro_edge_quote: float,
    micro_edge_quantity: float,
    maker_fee_rate: float,
    free_base: float,
    free_quote: float,
    flat_qty: float,
    micro_close_fok: bool = False,
    close_price_offset: float = 0.0,
    lose_to_stop_pnl: float = -0.01,
    fok_max_pnl: float = 0.0,
    fok_min_pnl: float = -0.0025,
    micro_open_sell: bool = False,
    micro_open_quantity: float = 0.0,
    take_on_target: bool = False,
    take_max_pnl: float = 0.0,
    micro_open_at_mid: bool = False,
    net_close_threshold: float = 0.5,
    micro_trend_open: bool = False,
    trend_flat_threshold: float = 0.50,
    min_close_maker_fee_rate: float = 0.0,
    max_net_qty: float = 0.25,
    flat_dual_quote: bool = True,
    pressure_threshold: float = 0.12,
    micro_trend_require_pressure: bool = True,
    dual_trend_threshold: float = 0.0,
    dual_pressure_threshold: float = 0.0,
    lock_open_price: bool = False,
    open_only: bool = False,
    close_only: bool = False,
    is_only_buy: bool = False,
    open_flat_only: bool = False,
    micro_depth_open: bool = False,
    micro_depth_levels: int = 21,
    micro_depth_qty: float = 100.0,
    micro_depth_imb_eps: float = 0.0,
) -> None:
    """Micro loop: BUY or SELL open, then close at target net PnL.

    When take_on_target is set, FOK at touch if taker-fee predicted PnL >= target
    and (if take_max_pnl > 0) <= take_max_pnl.
    When micro_open_at_mid is set, open limits use mid (post-only clamped).
    When lock_open_price is set, resting opens keep their first price (no mid
    rejoin); cancel only if post-only/invalid, wrong side, or trend hold.
    When open_only is set (phase0 during micro_rest), skip/cancel closes and
    dual exits; inventory may still scale in.
    When close_only is set (phase0 after micro_rest), skip/cancel opens; only
    peel/close remaining inventory.
    When is_only_buy is set, send BUY opens only (no SELL, no peel/close).
    When open_flat_only is set, new opens (incl. scale-in / dust repair) only
    when |net| <= flat_qty; inventory peels until flat before next warehouse open.
    Open qty is capped by free_base (SELL) / free_quote/price (BUY).
    When abs(net_qty) < net_close_threshold, close qty = abs(net) (full small pos).
    When abs(net_qty) >= max_net_qty: no opens.
    When abs(net_qty) > max_net_qty: close all at profitable target.
    When micro_depth_open and flat: L21 size gate replaces trend —
    ask_cum > bid_cum → BUY (join bids); bid_cum > ask_cum → SELL (join asks);
    else hold. Open prices = first level where cum qty > micro_depth_qty on the
    join side (BUY walks bids; SELL walks asks); fallback last L21 if unmet.
    Depth opens are post-only (may sit deeper than BBO), not mid-spread only.
    Else when micro_trend_open and flat: strong bullish→BUY, strong bearish→SELL;
    otherwise hold. Strong = |trend| > threshold, and when
    micro_trend_require_pressure also |pressure| > pressure_threshold same sign.
    When flat_dual_quote and flat seek_open: same tick places open at mid and
    speculative close priced for target edge (assumed entry = open price).
    Dual legs require a *stricter* bull/bear gate (dual_trend/pressure thresholds,
    at least as high as the open gate) and prices strictly inside (best_bid, best_ask).
    Maker closes price with real fill fee rate only; before first fill
    (real=0) fall back to account / min_close_maker_fee_rate. Cancel resting
    closes that fall below target under that rate.
    """
    # Real fill rate when known; else account/min_close pre-first-fill fallback.
    validator_maker_fee_rate = float(
        _v(
            book,
            "validator_maker_fee_rate",
            _v(book, "last_maker_fee_rate", 0.0),
        )
    )
    pricing_maker_fee_rate = close_maker_fee_rate_for_pricing(
        maker_fee_rate,
        min_close_maker_fee_rate,
        validator_maker_fee_rate,
    )
    micro_state = str(_v(book, "phase0_micro_state", "seek_open"))
    micro_entry = float(_v(book, "phase0_micro_entry_price", 0.0))
    long_qty = float(_v(book, "fifo_long_total_qty", 0.0))
    short_qty = float(_v(book, "fifo_short_total_qty", 0.0))
    # Flat only when |net| <= flat_qty (default 0.0005). Do NOT treat
    # sub-min inventory (e.g. 0.05 / 0.229) as flat — that caused side flips.
    flat_eps = max(float(flat_qty), 0.0)
    net_qty = float(long_qty) - float(short_qty)
    abs_net = abs(net_qty)
    min_lot = float(exchange_min_quantity)
    near_min_lot = net_near_exchange_min(
        abs_net, min_lot, flat_qty=flat_eps, volume_decimals=volume_decimals
    )
    is_flat = abs_net <= flat_eps + 1e-12
    has_long = net_qty > flat_eps + 1e-12
    has_short = net_qty < -(flat_eps + 1e-12)
    trend_score = float(_v(book, "trend_score", 0.0))
    pressure_score = float(_v(book, "pressure_score", 0.0))
    trend_flat_hold = False
    # Dual intent from trend/config (before inventory overrides open_sell).
    # Used to detect speculative close filling before the open leg.
    trend_thresh = max(0.0, float(trend_flat_threshold))
    press_thresh = max(0.0, float(pressure_threshold))
    require_pressure = bool(micro_trend_require_pressure)
    strong_bull = (
        _strong_bullish(trend_score, pressure_score, trend_thresh, press_thresh)
        if require_pressure
        else (trend_score > trend_thresh)
    )
    strong_bear = (
        _strong_bearish(trend_score, pressure_score, trend_thresh, press_thresh)
        if require_pressure
        else (trend_score < -trend_thresh)
    )
    # Dual needs a stronger regime than a single-leg open (thresholds floored at open gate).
    dual_trend_thresh = max(trend_thresh, max(0.0, float(dual_trend_threshold or 0.0)))
    dual_press_thresh = max(press_thresh, max(0.0, float(dual_pressure_threshold or 0.0)))
    dual_strong_bull = (
        _strong_bullish(trend_score, pressure_score, dual_trend_thresh, dual_press_thresh)
        if require_pressure
        else (trend_score > dual_trend_thresh)
    )
    dual_strong_bear = (
        _strong_bearish(trend_score, pressure_score, dual_trend_thresh, dual_press_thresh)
        if require_pressure
        else (trend_score < -dual_trend_thresh)
    )
    intended_open_sell = bool(micro_open_sell)
    intended_dual_side = False
    depth_n = max(1, int(micro_depth_levels or 21))
    depth_qty_th = float(micro_depth_qty if micro_depth_qty is not None else 100.0)
    depth_eps = max(0.0, float(micro_depth_imb_eps or 0.0))
    depth_bid_cum = 0.0
    depth_ask_cum = 0.0
    depth_thin_sell: bool | None = None  # True→SELL, False→BUY, None→hold/na
    if micro_depth_open:
        depth_bid_cum = float(
            level_volume_sum(book.get("bids") if isinstance(book, dict) else [], depth_n)
        )
        depth_ask_cum = float(
            level_volume_sum(book.get("asks") if isinstance(book, dict) else [], depth_n)
        )
        depth_tot = depth_bid_cum + depth_ask_cum
        log["micro_depth_levels"] = depth_n
        log["micro_depth_qty"] = depth_qty_th
        log["micro_depth_bid_cum"] = round(depth_bid_cum, 8)
        log["micro_depth_ask_cum"] = round(depth_ask_cum, 8)
        if depth_tot > 0.0:
            depth_imb = (depth_bid_cum - depth_ask_cum) / depth_tot
            log["micro_depth_imb"] = round(depth_imb, 8)
            # ask_cum > bid_cum → BUY (join bid book); bid_cum > ask_cum → SELL.
            if depth_imb < -depth_eps:
                depth_thin_sell = False  # more asks → BUY
            elif depth_imb > depth_eps:
                depth_thin_sell = True  # more bids → SELL
            else:
                depth_thin_sell = None
        if depth_thin_sell is not None:
            intended_open_sell = bool(depth_thin_sell)
    elif micro_trend_open:
        if strong_bull:
            intended_open_sell = False
        elif strong_bear:
            intended_open_sell = True
    # Dual intent only in the stricter bull/bear band (not every flat open).
    if flat_dual_quote and (dual_strong_bull or dual_strong_bear):
        intended_dual_side = True
        if dual_strong_bull:
            intended_open_sell = False
        elif dual_strong_bear:
            intended_open_sell = True
    # Adverse dual: close filled first → inventory opposite of intended open.
    adverse_dual_close = bool(
        flat_dual_quote
        and intended_dual_side
        and (not is_flat)
        and (
            ((not intended_open_sell) and has_short and not has_long)
            or (intended_open_sell and has_long and not has_short)
        )
    )
    # Inventory-aware open side:
    #   short → keep SELL farm; long → keep BUY farm;
    #   flat → depth thin-side (if enabled) else strong trend bias else config;
    #   flat + not decisive → hold (no open).
    if has_short and not has_long:
        open_sell = True
        open_side_source = "inv_short"
    elif has_long and not has_short:
        open_sell = False
        open_side_source = "inv_long"
    else:
        open_sell = bool(micro_open_sell)
        open_side_source = "config"
        if micro_depth_open:
            if depth_thin_sell is False:
                open_sell = False
                open_side_source = "depth_ask_heavy"  # join bids
            elif depth_thin_sell is True:
                open_sell = True
                open_side_source = "depth_bid_heavy"  # join asks
            else:
                open_side_source = "depth_balanced"
                trend_flat_hold = True
        elif micro_trend_open:
            if strong_bull:
                open_sell = False  # strong bullish → long
                open_side_source = "trend_strong_bull"
            elif strong_bear:
                open_sell = True  # strong bearish → short
                open_side_source = "trend_strong_bear"
            else:
                open_side_source = "trend_flat"
                trend_flat_hold = True

    if is_only_buy:
        open_sell = False
        open_side_source = "is_only_buy"

    # Warehouse: scale-in open until open_target; peel close_qty whenever inventory
    # is trade-sized (scale-in and peel can run together).
    open_target = float(micro_open_quantity)
    if open_target <= 0.0:
        open_target = float(micro_edge_quantity)
    open_target = max(open_target, float(exchange_min_quantity))
    qty_cap = max(float(max_quantity), open_target, float(micro_edge_quantity))

    # |net| >= max_net → no opens; |net| > max_net → flatten all at profitable price.
    max_net = float(max_net_qty) if max_net_qty else 0.0
    at_max_net = max_net > 0.0 and abs_net + 1e-12 >= max_net
    over_max_net = max_net > 0.0 and abs_net > max_net + 1e-12
    if over_max_net or adverse_dual_close:
        qty_cap = max(qty_cap, abs_net)

    # Close size: if over max_net, close all; elif |net| < threshold, flatten
    # that small net; else peel edge qty.
    close_raw = float(micro_edge_quantity)
    net_thresh = float(net_close_threshold)
    if over_max_net or adverse_dual_close:
        # Adverse close-first or oversized: flatten full residual net.
        close_raw = abs_net
    elif net_thresh > 0.0 and abs_net + 1e-9 < net_thresh:
        if near_min_lot:
            close_raw = min_lot
        elif abs_net + 1e-9 >= min_lot:
            close_raw = abs_net
    if has_long and not has_short:
        close_raw = min(close_raw, long_qty)
    elif has_short and not has_long:
        close_raw = min(close_raw, short_qty)
    if near_min_lot and close_raw + 1e-9 < min_lot:
        close_raw = min_lot

    close_qty = finalize_order_quantity(
        close_raw,
        qty_cap,
        exchange_min_quantity,
        volume_decimals,
    )
    open_qty = close_qty
    scale_in = False
    # When flat, ignore sub-flat lot dust for open sizing (open full target).
    if is_flat:
        side_qty_for_open = 0.0
    elif open_sell:
        side_qty_for_open = max(0.0, short_qty)
    else:
        side_qty_for_open = max(0.0, long_qty)
    remaining_open = max(0.0, open_target - side_qty_for_open)
    if remaining_open + 1e-9 >= exchange_min_quantity:
        open_qty = finalize_order_quantity(
            remaining_open,
            qty_cap,
            exchange_min_quantity,
            volume_decimals,
        )
        if open_sell:
            scale_in = (not has_long) and (open_qty + 1e-9 >= exchange_min_quantity)
        else:
            scale_in = (not has_short) and (open_qty + 1e-9 >= exchange_min_quantity)
    else:
        open_qty = 0.0
    if trend_flat_hold or at_max_net or adverse_dual_close:
        open_qty = 0.0
        scale_in = False
    # Balance cap applied after open prices are known (see below).

    # Peel whenever inventory is trade-sized; also scale-in when below warehouse.
    close_side = ""
    also_scale_in = False
    if has_long and has_short:
        close_side = "both"
    elif has_long:
        close_side = "long"
        if scale_in and not at_max_net and not adverse_dual_close:
            also_scale_in = True
            micro_state = "seek_open"
        else:
            micro_state = "await_close"
    elif has_short:
        close_side = "short"
        if scale_in and not at_max_net and not adverse_dual_close:
            also_scale_in = True
            micro_state = "seek_open"
        else:
            micro_state = "await_close"
    elif scale_in and not at_max_net:
        micro_state = "seek_open"
    elif micro_state == "await_close":
        # Latched state but truly flat (abs(net) < flat_qty) — farm again.
        micro_state = "seek_open"

    # Dust repair (venue rejects qty < exchange_min):
    # flat < abs(net) < exchange_min (not ~min lot) → open exchange_min same side,
    # then once abs(net) >= exchange_min, net_close_threshold closes full net.
    dust_repair = (
        (not is_flat)
        and (not open_flat_only)
        and (not at_max_net)
        and (not adverse_dual_close)
        and (not near_min_lot)
        and abs_net + 1e-9 < min_lot
    )
    if dust_repair:
        open_qty = finalize_order_quantity(
            float(exchange_min_quantity),
            max(float(qty_cap), float(exchange_min_quantity)),
            exchange_min_quantity,
            volume_decimals,
        )
        scale_in = open_qty + 1e-9 >= float(exchange_min_quantity)
        close_qty = 0.0
        close_side = ""
        also_scale_in = False
        micro_state = "seek_open"

    # Rest-window phase0: warehouse opens only. Cancel peels / dual closes.
    if open_only:
        close_qty = 0.0
        close_side = ""
        also_scale_in = False
        micro_state = "seek_open"
        log["phase0_edge_open_only"] = True

    # BUY warehouse opens only — no peel/close, no SELL legs.
    if is_only_buy:
        close_qty = 0.0
        close_side = ""
        also_scale_in = False
        micro_state = "seek_open"
        log["is_only_buy"] = 1

    # Flat-only opens: peel until flat; no scale-in / dust repair while holding.
    flat_only_hold = bool(open_flat_only and not is_flat)
    if flat_only_hold:
        open_qty = 0.0
        scale_in = False
        also_scale_in = False
        if has_long or has_short:
            micro_state = "await_close"

    # After rest: phase0 peels only — no new opens / dual / scale-in.
    if close_only:
        open_qty = 0.0
        scale_in = False
        also_scale_in = False
        if has_long and has_short:
            close_side = "both"
            micro_state = "await_close"
        elif has_long:
            close_side = "long"
            micro_state = "await_close"
        elif has_short:
            close_side = "short"
            micro_state = "await_close"
        else:
            close_qty = 0.0
            close_side = ""
            micro_state = "seek_open"
        log["phase0_edge_close_only"] = True

    micro_qty = close_qty  # close / peel size
    # Default open: join touch (bid buy / ask sell). Optional: mid or L21 depth_qty.
    buy_price = round_decimals(best_bid, price_decimals)
    sell_open_price = round_decimals(best_ask, price_decimals)
    mid_px = 0.0
    if best_bid > 0.0 and best_ask > best_bid:
        mid_px = round_decimals((best_bid + best_ask) / 2.0, price_decimals)
    if micro_depth_open:
        bids_lv = book.get("bids") if isinstance(book, dict) else []
        asks_lv = book.get("asks") if isinstance(book, dict) else []
        d100_ask, last_ask, _ = depth_threshold_price(asks_lv, depth_qty_th, depth_n)
        d100_bid, last_bid, _ = depth_threshold_price(bids_lv, depth_qty_th, depth_n)
        # BUY joins bids: first cum > depth_qty (fallback last L21 bid).
        worst_buy = float(last_bid) if last_bid and last_bid > 0.0 else float(best_bid)
        depth_buy = float(d100_bid) if d100_bid and d100_bid > 0.0 else worst_buy
        if depth_buy > 0.0:
            buy_price = round_decimals(depth_buy, price_decimals)
        # SELL joins asks: first cum > depth_qty (fallback last L21 ask).
        worst_sell = float(last_ask) if last_ask and last_ask > 0.0 else float(best_ask)
        depth_sell = float(d100_ask) if d100_ask and d100_ask > 0.0 else worst_sell
        if depth_sell > 0.0:
            sell_open_price = round_decimals(depth_sell, price_decimals)
        log["micro_depth_buy_d100"] = round(float(d100_bid or 0.0), 8)
        log["micro_depth_buy_worst"] = round(float(worst_buy or 0.0), 8)
        log["micro_depth_sell_d100"] = round(float(d100_ask or 0.0), 8)
        log["micro_depth_sell_worst"] = round(float(worst_sell or 0.0), 8)
        # Post-only only (depth may sit deeper than BBO — do not pull to touch).
        if buy_price >= best_ask - 1e-12:
            buy_price = round_decimals(best_ask - tick, price_decimals)
            if buy_price <= 0.0 or buy_price >= best_ask - 1e-12:
                buy_price = round_decimals(best_bid, price_decimals)
        if sell_open_price <= best_bid + 1e-12:
            sell_open_price = round_decimals(best_bid + tick, price_decimals)
            if sell_open_price <= best_bid + 1e-12:
                sell_open_price = round_decimals(best_ask, price_decimals)
    elif micro_open_at_mid and mid_px > 0.0:
        buy_price = mid_px
        sell_open_price = mid_px
        # Keep post-only: buy must stay below ask; sell above bid.
        if buy_price >= best_ask - 1e-12:
            buy_price = round_decimals(best_ask - tick, price_decimals)
            if buy_price <= 0.0 or buy_price >= best_ask - 1e-12:
                buy_price = round_decimals(best_bid, price_decimals)
        if sell_open_price <= best_bid + 1e-12:
            sell_open_price = round_decimals(best_bid + tick, price_decimals)
            if sell_open_price <= best_bid + 1e-12:
                sell_open_price = round_decimals(best_ask, price_decimals)

    # Locked open price: reuse first post until fill / trend-hold / clear.
    # Do not chase mid. Only drop the lock when it would take (cross).
    locked_open_px = float(_v(book, "phase0_micro_locked_open_price", 0.0) or 0.0)
    if (not lock_open_price) or trend_flat_hold or (not is_flat and not also_scale_in):
        locked_open_px = 0.0
    if lock_open_price and locked_open_px > 0.0:
        locked_r = round_decimals(locked_open_px, price_decimals)
        if open_sell:
            if locked_r > best_bid + 1e-12:
                sell_open_price = locked_r
            else:
                # Locked sell would take — keep lock but skip posting this tick.
                log["phase0_micro_lock_blocked_cross"] = True
                open_qty = 0.0
                scale_in = False
                also_scale_in = False
        else:
            if locked_r < best_ask - 1e-12:
                buy_price = locked_r
            else:
                log["phase0_micro_lock_blocked_cross"] = True
                open_qty = 0.0
                scale_in = False
                also_scale_in = False

    # Cap open size by free balances: SELL uses free_base, BUY uses free_quote/price.
    if scale_in and open_qty + 1e-9 >= exchange_min_quantity and not dust_repair:
        if open_sell:
            bal_raw = max(0.0, float(free_base))
        else:
            ref_px = float(buy_price) if buy_price > 0.0 else 0.0
            bal_raw = (float(free_quote) / ref_px) if ref_px > 0.0 else 0.0
        bal_capped = min(float(open_qty), bal_raw)
        open_qty = finalize_order_quantity(
            bal_capped,
            qty_cap,
            exchange_min_quantity,
            volume_decimals,
        )
        if open_qty + 1e-9 < exchange_min_quantity:
            open_qty = 0.0
            scale_in = False
            also_scale_in = False
            if close_side in ("long", "short") and not is_flat:
                micro_state = "await_close"
        log["phase0_micro_open_bal_cap"] = round(bal_raw, 8)
    sell_price = 0.0
    buy_close_price = 0.0
    sell_qty = micro_qty
    buy_close_qty = micro_qty
    close_entry = 0.0
    close_entry_source = ""
    micro_sell_trace: dict | None = None
    micro_buy_trace: dict | None = None
    close_fee_lots: list[tuple[float, float, float]] = []
    # Flat + *stronger* bull/bear: post open and speculative close in the same tick.
    flat_dual = (
        bool(flat_dual_quote)
        and (not open_only)
        and (not close_only)
        and (not is_only_buy)
        and is_flat
        and (not trend_flat_hold)
        and (dual_strong_bull or dual_strong_bear)
        and (not at_max_net)
        and scale_in
        and open_qty + 1e-9 >= float(exchange_min_quantity)
        and micro_edge_quote > 0.0
    )
    if flat_dual:
        open_px = float(sell_open_price if open_sell else buy_price)
        if not _price_inside_spread(open_px, best_bid, best_ask):
            # Open leg outside (best_bid, best_ask): skip dual and open.
            flat_dual = False
            open_qty = 0.0
            scale_in = False
            log["phase0_micro_skip_open_outside_spread"] = True
        elif open_sell:
            assumed_entry = float(sell_open_price)
            if assumed_entry > 0.0:
                per_unit = micro_edge_quote / open_qty
                raw_cover = fee_aware_short_close_price(
                    assumed_entry, per_unit, pricing_maker_fee_rate
                )
                buy_close_price = round_decimals_down(raw_cover, price_decimals)
                # Dual cover must sit inside (best_bid, best_ask) and below open.
                if (
                    not _price_inside_spread(buy_close_price, best_bid, best_ask)
                    or buy_close_price >= assumed_entry - 1e-12
                ):
                    buy_close_price = 0.0
                    log["phase0_micro_skip_dual_outside_spread"] = True
                else:
                    buy_close_qty = open_qty
                    close_entry = assumed_entry
                    close_entry_source = "flat_dual_assumed"
                    micro_buy_trace = {
                        "pricing": "flat_dual_short",
                        "close_entry": assumed_entry,
                        "close_entry_source": close_entry_source,
                        "buy_close_price": buy_close_price,
                        "buy_close_qty": buy_close_qty,
                        "target_net_pnl": micro_edge_quote,
                        "pricing_maker_fee_rate": pricing_maker_fee_rate,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                    }
        else:
            assumed_entry = float(buy_price)
            if assumed_entry > 0.0:
                per_unit = micro_edge_quote / open_qty
                raw_sell = fee_aware_long_close_price(
                    assumed_entry, per_unit, pricing_maker_fee_rate
                )
                sell_price = round_decimals_up(raw_sell, price_decimals)
                # Dual exit must sit inside (best_bid, best_ask) and above open.
                if (
                    not _price_inside_spread(sell_price, best_bid, best_ask)
                    or sell_price <= assumed_entry + 1e-12
                ):
                    sell_price = 0.0
                    log["phase0_micro_skip_dual_outside_spread"] = True
                else:
                    sell_qty = open_qty
                    close_entry = assumed_entry
                    close_entry_source = "flat_dual_assumed"
                    micro_sell_trace = {
                        "pricing": "flat_dual_long",
                        "close_entry": assumed_entry,
                        "close_entry_source": close_entry_source,
                        "sell_price": sell_price,
                        "sell_qty": sell_qty,
                        "target_net_pnl": micro_edge_quote,
                        "pricing_maker_fee_rate": pricing_maker_fee_rate,
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                    }
        if (open_sell and buy_close_price <= 0.0) or (
            (not open_sell) and sell_price <= 0.0
        ):
            flat_dual = False

    # Flat (non-dual) open: mid/trend must be inside spread; depth opens are post-only.
    if is_flat and open_qty + 1e-9 >= float(exchange_min_quantity) and not trend_flat_hold:
        open_px = float(sell_open_price if open_sell else buy_price)
        if micro_depth_open:
            postable = (
                open_px > best_bid + 1e-12
                if open_sell
                else open_px < best_ask - 1e-12
            )
            if not postable:
                open_qty = 0.0
                scale_in = False
                flat_dual = False
                log["phase0_micro_skip_open_outside_spread"] = True
        elif not _price_inside_spread(open_px, best_bid, best_ask):
            open_qty = 0.0
            scale_in = False
            flat_dual = False
            log["phase0_micro_skip_open_outside_spread"] = True

    log["phase1_micro_open_quantity"] = open_target
    log["phase1_micro_open_sell"] = int(open_sell)
    log["phase1_micro_open_at_mid"] = int(micro_open_at_mid)
    log["micro_lock_open_price"] = int(lock_open_price)
    log["micro_open_side_source"] = open_side_source
    log["micro_open_flat_only"] = int(open_flat_only)
    log["micro_open_flat_only_hold"] = int(flat_only_hold)
    log["micro_flat_dual_quote"] = int(flat_dual)
    log["trend_score"] = round(trend_score, 6)
    log["pressure_score"] = round(pressure_score, 6)
    log["micro_strong_bull"] = int(strong_bull)
    log["micro_strong_bear"] = int(strong_bear)
    log["micro_dual_strong_bull"] = int(dual_strong_bull)
    log["micro_dual_strong_bear"] = int(dual_strong_bear)
    log["micro_dual_trend_threshold"] = round(dual_trend_thresh, 6)
    log["micro_dual_pressure_threshold"] = round(dual_press_thresh, 6)
    log["account_maker_fee_rate"] = round(maker_fee_rate, 8)
    log["validator_maker_fee_rate"] = round(validator_maker_fee_rate, 8)
    log["pricing_maker_fee_rate"] = round(pricing_maker_fee_rate, 8)
    if mid_px > 0.0:
        log["mid"] = mid_px
    log["phase1_micro_scale_in"] = int(scale_in)
    log["phase1_micro_also_scale_in"] = int(also_scale_in)
    log["phase1_micro_dust_repair"] = int(dust_repair)
    log["micro_near_min_lot"] = int(near_min_lot)
    log["fifo_short_total_qty"] = short_qty
    log["fifo_long_total_qty"] = long_qty
    log["net_qty"] = net_qty
    log["close_qty"] = close_qty
    log["micro_net_close_threshold"] = net_thresh
    log["micro_max_net_qty"] = max_net
    log["at_max_net"] = int(at_max_net)
    log["over_max_net"] = int(over_max_net)
    log["adverse_dual_close"] = int(adverse_dual_close)
    log["intended_open_sell"] = int(intended_open_sell)

    if close_side == "long":
        # Peel close_qty (or full net when over_max_net).
        sell_qty = micro_qty
        fifo_lots = _read_fifo_lots_with_fees(
            book,
            "fifo_long_lots",
            open_fee_rate_fallback=pricing_maker_fee_rate,
        )
        lot_fee_rate = maker_fee_rate_from_lots(fifo_lots)
        if lot_fee_rate > pricing_maker_fee_rate:
            pricing_maker_fee_rate = lot_fee_rate
            log["pricing_maker_fee_rate"] = round(pricing_maker_fee_rate, 8)
            log["lot_implied_maker_fee_rate"] = round(lot_fee_rate, 8)
        close_entry = _fifo_long_close_entry(book, sell_qty)
        if close_entry > 0.0:
            close_entry_source = "fifo_lots"
        elif micro_entry > 0.0:
            close_entry = micro_entry
            close_entry_source = "micro_entry"
        if not fifo_lots and close_entry > 0.0:
            fifo_lots = [
                (
                    sell_qty,
                    close_entry,
                    close_entry * sell_qty * pricing_maker_fee_rate,
                )
            ]
        if fifo_lots:
            close_fee_lots = fifo_lots
            taker_fee_rate = float(_v(book, "taker_fee_rate", maker_fee_rate))
            close_fee_rate = (
                taker_fee_rate if micro_close_fok else pricing_maker_fee_rate
            )
            sell_price, fifo_trace = micro_fifo_long_close_sell_price(
                fifo_lots,
                sell_qty,
                micro_edge_quote,
                close_fee_rate,
                price_decimals,
                tick=tick,
                clamp_profit=True,
                close_price_offset=close_price_offset,
            )
            micro_sell_trace = micro_sell_price_trace(
                close_entry,
                sell_qty,
                micro_edge_quote,
                pricing_maker_fee_rate,
                price_decimals,
                close_fok=micro_close_fok,
                close_taker_fee_rate=taker_fee_rate,
                micro_entry=micro_entry,
                close_entry_source=close_entry_source,
                best_bid=best_bid,
                best_ask=best_ask,
                fifo_lots=fifo_lots,
                tick=tick,
                clamp_profit=True,
                close_price_offset=close_price_offset,
            )
            micro_sell_trace.setdefault(
                "predicted_pnl", fifo_trace.get("predicted_pnl")
            )
            micro_sell_trace["pricing_maker_fee_rate"] = pricing_maker_fee_rate
            micro_sell_trace["account_maker_fee_rate"] = maker_fee_rate

    elif close_side == "short":
        # Always peel close_qty (e.g. 0.25), never cover the full warehouse at once.
        buy_close_qty = micro_qty
        fifo_lots = _read_fifo_lots_with_fees(
            book,
            "fifo_short_lots",
            open_fee_rate_fallback=pricing_maker_fee_rate,
        )
        lot_fee_rate = maker_fee_rate_from_lots(fifo_lots)
        if lot_fee_rate > pricing_maker_fee_rate:
            pricing_maker_fee_rate = lot_fee_rate
            log["pricing_maker_fee_rate"] = round(pricing_maker_fee_rate, 8)
            log["lot_implied_maker_fee_rate"] = round(lot_fee_rate, 8)
        close_entry = _fifo_short_close_entry(book, buy_close_qty)
        if close_entry > 0.0:
            close_entry_source = "fifo_lots"
        elif micro_entry > 0.0:
            close_entry = micro_entry
            close_entry_source = "micro_entry"
        if not fifo_lots and close_entry > 0.0:
            fifo_lots = [
                (
                    buy_close_qty,
                    close_entry,
                    close_entry * buy_close_qty * pricing_maker_fee_rate,
                )
            ]
        if fifo_lots:
            close_fee_lots = fifo_lots
            buy_close_price, fifo_trace = micro_fifo_short_close_buy_price(
                fifo_lots,
                buy_close_qty,
                micro_edge_quote,
                pricing_maker_fee_rate,
                price_decimals,
                tick=tick,
            )
            # Keep the target-PnL ceiling only. Do NOT join a crashed best_bid
            # via min(bid, ceiling) — that turns a +0.1 wait into a windfall fill
            # (e.g. buy 346 after short 408). If ceiling >= ask, the crossed-cover
            # guard waits until the book recovers for a post-only ~target fill.
            micro_buy_trace = {
                "pricing": "fifo_accurate_short",
                "close_entry": close_entry,
                "close_entry_source": close_entry_source,
                "buy_close_price": buy_close_price,
                "buy_close_qty": buy_close_qty,
                "target_net_pnl": micro_edge_quote,
                "predicted_pnl": fifo_trace.get("predicted_pnl"),
                "pricing_maker_fee_rate": pricing_maker_fee_rate,
                "account_maker_fee_rate": maker_fee_rate,
                "best_bid": best_bid,
                "best_ask": best_ask,
            }

    open_orders = read_open_orders(book)
    cancelled_ids: set = set()
    cancelled = 0

    if close_side == "both":
        for order in open_orders:
            order_id = _v(order, "id", None)
            if order_id is None:
                continue
            add_cancel_order(output["orders"], book_id, order)
            cancelled_ids.add(order_id)
            cancelled += 1
        log["phase0_micro_state"] = micro_state
        log["phase0_edge_mode"] = "micro_quote"
        log["phase0_target_edge"] = micro_edge_quote
        log["fifo_long_total_qty"] = long_qty
        log["fifo_short_total_qty"] = short_qty
        log["decision"] = "phase0_micro_skip_inventory"
        log["cancelled"] = cancelled
        log["orders_sent"] = 0
        log["action"] = "phase0_micro_wait"
        output["logs"].append(log)
        return

    for order in open_orders:
        order_id = _v(order, "id", None)
        if order_id is None:
            continue
        direction = str(_v(order, "direction", "")).upper()
        order_price = float(_v(order, "price", 0.0))
        should_cancel = False
        fee_cancel = False
        if is_only_buy:
            if direction == "SELL":
                should_cancel = True
            elif direction == "BUY" and has_short and not has_long:
                should_cancel = True
        # Dual close filled first: cancel ALL resting (esp. the open leg),
        # then reprice a fresh await_close on residual net.
        if adverse_dual_close:
            should_cancel = True
        elif also_scale_in and close_side == "long":
            # Dual: BUY scale-in + SELL peel.
            if direction == "BUY":
                if lock_open_price:
                    if order_price >= best_ask - 1e-12:
                        should_cancel = True
                elif _micro_order_price_stale(
                    order_price, buy_price, tick, price_decimals
                ):
                    should_cancel = True
            elif direction == "SELL" and sell_price > 0.0:
                if _micro_order_price_stale(
                    order_price, sell_price, tick, price_decimals
                ):
                    should_cancel = True
                elif _resting_long_close_fee_unprofitable(
                    close_fee_lots,
                    sell_qty,
                    order_price,
                    pricing_maker_fee_rate,
                    micro_edge_quote,
                ):
                    should_cancel = True
                    fee_cancel = True
        elif also_scale_in and close_side == "short":
            # Dual: SELL scale-in + BUY peel.
            if direction == "SELL":
                if lock_open_price:
                    if order_price <= best_bid + 1e-12:
                        should_cancel = True
                elif _micro_order_price_stale(
                    order_price, sell_open_price, tick, price_decimals
                ):
                    should_cancel = True
            elif direction == "BUY" and buy_close_price > 0.0:
                if _micro_order_price_stale(
                    order_price, buy_close_price, tick, price_decimals
                ):
                    should_cancel = True
                elif _resting_short_close_fee_unprofitable(
                    close_fee_lots,
                    buy_close_qty,
                    order_price,
                    pricing_maker_fee_rate,
                    micro_edge_quote,
                ):
                    should_cancel = True
                    fee_cancel = True
        elif micro_state == "seek_open":
            if trend_flat_hold:
                # Flat + weak trend: cancel any resting opens and stay dark.
                should_cancel = True
            elif open_sell:
                if direction == "BUY":
                    if flat_dual and buy_close_price > 0.0:
                        # Speculative cover for flat dual short open.
                        if _micro_order_price_stale(
                            order_price, buy_close_price, tick, price_decimals
                        ):
                            should_cancel = True
                        elif not _price_inside_spread(
                            order_price, best_bid, best_ask
                        ):
                            should_cancel = True
                    else:
                        should_cancel = True
                elif direction == "SELL":
                    if lock_open_price or micro_depth_open:
                        # Keep first/depth open price until fill; cancel only if would take.
                        if order_price <= best_bid + 1e-12:
                            should_cancel = True
                    elif _micro_order_price_stale(
                        order_price, sell_open_price, tick, price_decimals
                    ):
                        should_cancel = True
                    elif is_flat and not _price_inside_spread(
                        order_price, best_bid, best_ask
                    ):
                        should_cancel = True
            else:
                if direction == "SELL":
                    if flat_dual and sell_price > 0.0:
                        # Speculative long close for flat dual buy open.
                        if _micro_order_price_stale(
                            order_price, sell_price, tick, price_decimals
                        ):
                            should_cancel = True
                        elif not _price_inside_spread(
                            order_price, best_bid, best_ask
                        ):
                            should_cancel = True
                    else:
                        should_cancel = True
                elif direction == "BUY":
                    if lock_open_price or micro_depth_open:
                        # Keep first/depth open price until fill; cancel only if would take.
                        if order_price >= best_ask - 1e-12:
                            should_cancel = True
                    elif _micro_order_price_stale(
                        order_price, buy_price, tick, price_decimals
                    ):
                        should_cancel = True
                    elif is_flat and not _price_inside_spread(
                        order_price, best_bid, best_ask
                    ):
                        should_cancel = True
        elif close_side == "long":
            if direction == "BUY":
                should_cancel = True
            elif direction == "SELL" and sell_price > 0.0:
                if _micro_order_price_stale(
                    order_price, sell_price, tick, price_decimals
                ):
                    should_cancel = True
                elif _resting_long_close_fee_unprofitable(
                    close_fee_lots,
                    sell_qty,
                    order_price,
                    pricing_maker_fee_rate,
                    micro_edge_quote,
                ):
                    should_cancel = True
                    fee_cancel = True
        elif close_side == "short":
            if direction == "SELL":
                should_cancel = True
            elif direction == "BUY" and buy_close_price > 0.0:
                if _micro_order_price_stale(
                    order_price, buy_close_price, tick, price_decimals
                ):
                    should_cancel = True
                elif _resting_short_close_fee_unprofitable(
                    close_fee_lots,
                    buy_close_qty,
                    order_price,
                    pricing_maker_fee_rate,
                    micro_edge_quote,
                ):
                    should_cancel = True
                    fee_cancel = True
        if should_cancel:
            add_cancel_order(output["orders"], book_id, order)
            cancelled_ids.add(order_id)
            cancelled += 1
            if fee_cancel:
                log["phase0_micro_cancel_fee_unprofitable"] = True

    sent = 0
    log["phase0_micro_state"] = micro_state
    log["phase0_micro_entry_price"] = micro_entry if micro_entry > 0.0 else None
    if close_entry > 0.0:
        log["phase0_close_entry"] = close_entry
    log["phase0_edge_mode"] = "micro_quote"
    log["phase0_target_edge"] = micro_edge_quote
    log["phase0_micro_open_sell"] = int(open_sell)
    log["phase0_micro_close_side"] = close_side or None
    if micro_sell_trace is not None:
        log["micro_sell_trace"] = micro_sell_trace
    if micro_buy_trace is not None:
        log["micro_buy_trace"] = micro_buy_trace

    def _persist_locked_open(px: float) -> None:
        if not lock_open_price or px <= 0.0:
            return
        px_r = round_decimals(float(px), price_decimals)
        log["phase0_micro_locked_open_price"] = px_r
        if isinstance(book, dict):
            book["phase0_micro_locked_open_price"] = px_r

    def _clear_locked_open() -> None:
        log["phase0_micro_locked_open_price"] = 0.0
        if isinstance(book, dict):
            book["phase0_micro_locked_open_price"] = 0.0

    if lock_open_price and (trend_flat_hold or (not is_flat and not also_scale_in)):
        _clear_locked_open()
    elif lock_open_price and locked_open_px > 0.0:
        _persist_locked_open(locked_open_px)

    if micro_state == "seek_open":
        open_attempts = int(_v(book, "phase0_micro_open_attempts", 0))
        max_open_attempts = max(
            1, int(_v(book, "phase0_micro_max_open_attempts", 1))
        )
        open_pending = bool(_v(book, "phase0_micro_open_pending", False))
        log["phase0_micro_open_attempts"] = open_attempts
        log["phase0_micro_max_open_attempts"] = max_open_attempts
        # Gate opens on open_qty (not close/peel size) so dust repair can fire.
        if trend_flat_hold:
            log["decision"] = "phase0_micro_trend_flat_hold"
            _clear_locked_open()
        elif log.get("phase0_micro_lock_blocked_cross"):
            log["decision"] = "phase0_micro_lock_wait_cross"
            if locked_open_px > 0.0:
                _persist_locked_open(locked_open_px)
        elif flat_only_hold:
            log["decision"] = "phase0_micro_flat_only_hold"
        elif log.get("phase0_micro_skip_open_outside_spread"):
            log["decision"] = "phase0_micro_skip_open_outside_spread"
        elif open_qty + 1e-9 < exchange_min_quantity:
            log["decision"] = "phase0_micro_skip_qty"
        elif open_sell:
            # Depth/locked opens may sit deeper than BBO; only require post-only.
            sell_postable = (
                sell_open_price > best_bid + 1e-12
                if (lock_open_price or micro_depth_open)
                else _price_inside_spread(sell_open_price, best_bid, best_ask)
            )
            if not sell_postable:
                log["decision"] = "phase0_micro_skip_open_outside_spread"
            elif sell_open_price <= best_bid:
                log["decision"] = "phase0_micro_skip_crossed_sell"
            elif free_base < open_qty:
                log["decision"] = "phase0_micro_skip_no_base"
            else:
                has_sell = any(
                    str(_v(o, "direction", "")).upper() == "SELL"
                    and _v(o, "id", None) not in cancelled_ids
                    for o in open_orders
                )
                has_dual_cover = any(
                    str(_v(o, "direction", "")).upper() == "BUY"
                    and _v(o, "id", None) not in cancelled_ids
                    for o in open_orders
                )
                log["sell_price"] = sell_open_price
                log["sell_quantity"] = open_qty
                log["sell_tif"] = "GTC"
                if flat_dual and buy_close_price > 0.0:
                    log["buy_close_price"] = buy_close_price
                    log["buy_close_quantity"] = buy_close_qty
                if has_sell:
                    log["decision"] = "phase0_micro_wait_open"
                    if open_pending:
                        log["phase0_micro_open_pending"] = True
                    if lock_open_price:
                        for o in open_orders:
                            if _v(o, "id", None) in cancelled_ids:
                                continue
                            if str(_v(o, "direction", "")).upper() != "SELL":
                                continue
                            opx = float(_v(o, "price", 0.0) or 0.0)
                            if opx > 0.0:
                                _persist_locked_open(opx)
                                break
                    # Keep speculative cover alive while open rests.
                    if (
                        flat_dual
                        and buy_close_price > 0.0
                        and buy_close_price < sell_open_price - 1e-12
                        and _price_inside_spread(buy_close_price, best_bid, best_ask)
                        and not has_dual_cover
                    ):
                        add_limit_order(
                            output["orders"],
                            book_id,
                            "BUY",
                            buy_close_qty,
                            buy_close_price,
                            expiry_period,
                            exchange_min_quantity,
                            post_only=True,
                            time_in_force="GTC",
                        )
                        sent += 1
                        log["phase0_micro_flat_dual_close"] = True
                elif open_attempts >= max_open_attempts:
                    log["decision"] = "phase0_micro_skip_open_cap"
                elif open_pending and cancelled == 0:
                    log["decision"] = "phase0_micro_wait_open"
                    log["phase0_micro_open_pending"] = True
                else:
                    # Short open / scale-in: may sell while already short.
                    add_limit_order(
                        output["orders"],
                        book_id,
                        "SELL",
                        open_qty,
                        sell_open_price,
                        expiry_period,
                        exchange_min_quantity,
                        post_only=True,
                        time_in_force="GTC",
                    )
                    sent += 1
                    if lock_open_price:
                        _persist_locked_open(sell_open_price)
                    if (
                        flat_dual
                        and buy_close_price > 0.0
                        and buy_close_price < sell_open_price - 1e-12
                        and _price_inside_spread(buy_close_price, best_bid, best_ask)
                        and not has_dual_cover
                    ):
                        add_limit_order(
                            output["orders"],
                            book_id,
                            "BUY",
                            buy_close_qty,
                            buy_close_price,
                            expiry_period,
                            exchange_min_quantity,
                            post_only=True,
                            time_in_force="GTC",
                        )
                        sent += 1
                        log["phase0_micro_flat_dual_close"] = True
                    log["decision"] = (
                        "phase0_micro_reopen_sell"
                        if open_attempts > 0 or short_qty + 1e-9 >= exchange_min_quantity
                        else (
                            "phase0_micro_open_sell_dual"
                            if flat_dual and log.get("phase0_micro_flat_dual_close")
                            else "phase0_micro_open_sell"
                        )
                    )
        else:
            buy_postable = (
                buy_price < best_ask - 1e-12
                if (lock_open_price or micro_depth_open)
                else _price_inside_spread(buy_price, best_bid, best_ask)
            )
            if not buy_postable:
                log["decision"] = "phase0_micro_skip_open_outside_spread"
            elif buy_price >= best_ask:
                log["decision"] = "phase0_micro_skip_crossed_buy"
            elif free_quote < open_qty * buy_price:
                log["decision"] = "phase0_micro_skip_no_quote"
            else:
                has_buy = any(
                    str(_v(o, "direction", "")).upper() == "BUY"
                    and _v(o, "id", None) not in cancelled_ids
                    for o in open_orders
                )
                has_dual_exit = any(
                    str(_v(o, "direction", "")).upper() == "SELL"
                    and _v(o, "id", None) not in cancelled_ids
                    for o in open_orders
                )
                log["buy_price"] = buy_price
                log["buy_quantity"] = open_qty
                log["buy_tif"] = "GTC"
                if flat_dual and sell_price > 0.0:
                    log["sell_close_price"] = sell_price
                    log["sell_close_quantity"] = sell_qty
                if has_buy:
                    log["decision"] = "phase0_micro_wait_open"
                    if open_pending:
                        log["phase0_micro_open_pending"] = True
                    if lock_open_price:
                        for o in open_orders:
                            if _v(o, "id", None) in cancelled_ids:
                                continue
                            if str(_v(o, "direction", "")).upper() != "BUY":
                                continue
                            opx = float(_v(o, "price", 0.0) or 0.0)
                            if opx > 0.0:
                                _persist_locked_open(opx)
                                break
                    if (
                        flat_dual
                        and sell_price > 0.0
                        and sell_price > buy_price + 1e-12
                        and _price_inside_spread(sell_price, best_bid, best_ask)
                        and not has_dual_exit
                    ):
                        add_limit_order(
                            output["orders"],
                            book_id,
                            "SELL",
                            sell_qty,
                            sell_price,
                            expiry_period,
                            exchange_min_quantity,
                            post_only=True,
                            time_in_force="GTC",
                        )
                        sent += 1
                        log["phase0_micro_flat_dual_close"] = True
                elif open_attempts >= max_open_attempts:
                    log["decision"] = "phase0_micro_skip_open_cap"
                elif open_pending and cancelled == 0:
                    log["decision"] = "phase0_micro_wait_open"
                    log["phase0_micro_open_pending"] = True
                else:
                    # Long open / scale-in: may buy while already long.
                    add_limit_order(
                        output["orders"],
                        book_id,
                        "BUY",
                        open_qty,
                        buy_price,
                        expiry_period,
                        exchange_min_quantity,
                        post_only=True,
                        time_in_force="GTC",
                    )
                    sent += 1
                    if lock_open_price:
                        _persist_locked_open(buy_price)
                    if (
                        flat_dual
                        and sell_price > 0.0
                        and sell_price > buy_price + 1e-12
                        and _price_inside_spread(sell_price, best_bid, best_ask)
                        and not has_dual_exit
                    ):
                        add_limit_order(
                            output["orders"],
                            book_id,
                            "SELL",
                            sell_qty,
                            sell_price,
                            expiry_period,
                            exchange_min_quantity,
                            post_only=True,
                            time_in_force="GTC",
                        )
                        sent += 1
                        log["phase0_micro_flat_dual_close"] = True
                    log["decision"] = (
                        "phase0_micro_reopen_buy"
                        if open_attempts > 0 or long_qty + 1e-9 >= exchange_min_quantity
                        else (
                            "phase0_micro_open_buy_dual"
                            if flat_dual and log.get("phase0_micro_flat_dual_close")
                            else "phase0_micro_open_buy"
                        )
                    )
    if close_side == "long":
        open_decision = log.get("decision")
        if sell_qty < exchange_min_quantity:
            log["decision"] = "phase0_micro_skip_qty"
        elif sell_price <= 0.0:
            log["decision"] = "phase0_micro_skip_bad_close"
        elif not _micro_fifo_long_closeable(long_qty, sell_qty, flat_qty):
            log["decision"] = "phase0_micro_skip_fifo_desync"
            log["fifo_long_total_qty"] = long_qty
            log["fifo_short_total_qty"] = short_qty
            if close_entry_source:
                log["close_entry_source"] = close_entry_source
        elif free_base < sell_qty:
            log["decision"] = "phase0_micro_skip_no_base"
        else:
            has_sell = any(
                str(_v(o, "direction", "")).upper() == "SELL"
                and _v(o, "id", None) not in cancelled_ids
                for o in open_orders
            )
            close_pending = bool(_v(book, "phase0_micro_close_pending", False))
            close_tif, close_post_only = _micro_close_order_flags(
                micro_close_fok
            )
            force_fok_cross = False
            force_fok_target = False
            if not micro_close_fok and sell_price > 0.0 and best_bid > 0.0:
                from fifo_pnl import predict_long_close_realized

                # Match UID10: use account fee rates as-is (rebates allowed).
                taker_fee_rate = float(_v(book, "taker_fee_rate", maker_fee_rate))
                fifo_lots_close = _fifo_lots_for_take_predict(
                    book,
                    side="long",
                    qty=sell_qty,
                    maker_fee_rate=pricing_maker_fee_rate,
                    entry=close_entry,
                )
                bid_px = round_decimals(best_bid, price_decimals)
                pred_at_bid = (
                    predict_long_close_realized(
                        fifo_lots_close,
                        sell_qty,
                        bid_px,
                        taker_fee_rate,
                    )
                    if fifo_lots_close
                    else 0.0
                )
                log["phase0_micro_pred_pnl_at_bid"] = round(pred_at_bid, 8)
                if take_on_target and take_pnl_meets_target(
                    pred_at_bid, micro_edge_quote, take_max_pnl
                ):
                    force_fok_target = True
                    force_fok_cross = True
                    close_tif, close_post_only = "FOK", False
                    sell_price = bid_px
                    log["phase0_micro_take_on_target"] = True
                    # Drop same-tick scale-in BUY to avoid STP vs FOK sell.
                    dropped = _drop_new_book_limits(
                        output["orders"], book_id, "BUY"
                    )
                    if dropped:
                        sent = max(0, sent - dropped)
                        log["phase0_micro_take_drop_scale_in"] = dropped
                elif (
                    take_on_target
                    and take_max_pnl > 0.0
                    and pred_at_bid + 1e-12 >= micro_edge_quote
                    and pred_at_bid > take_max_pnl + 1e-12
                ):
                    log["phase0_micro_skip_take_above_max"] = True
                    log["phase0_micro_take_max_pnl"] = take_max_pnl
                elif sell_price <= best_bid + 1e-12:
                    # Crossed maker close → FOK@bid only in small-lose band.
                    fok_hi = float(fok_max_pnl)
                    fok_lo = float(fok_min_pnl)
                    if fok_lo > fok_hi:
                        fok_lo, fok_hi = fok_hi, fok_lo
                    if pred_at_bid > fok_hi + 1e-12:
                        log["phase0_micro_skip_fok_would_win"] = True
                    elif pred_at_bid < fok_lo - 1e-12:
                        log["phase0_micro_skip_fok_too_lossy"] = True
                    else:
                        force_fok_cross = True
                        close_tif, close_post_only = "FOK", False
                        sell_price = bid_px
                        log["phase0_micro_close_crossed_to_fok"] = True
            if force_fok_cross and has_sell:
                for order in open_orders:
                    order_id = _v(order, "id", None)
                    if order_id is None or order_id in cancelled_ids:
                        continue
                    if str(_v(order, "direction", "")).upper() != "SELL":
                        continue
                    add_cancel_order(output["orders"], book_id, order)
                    cancelled_ids.add(order_id)
                    cancelled += 1
                has_sell = False
            log["sell_price"] = sell_price
            log["sell_quantity"] = sell_qty
            log["sell_tif"] = close_tif
            log["sell_post_only"] = close_post_only
            if has_sell:
                log["decision"] = "phase0_micro_wait_close"
                if close_pending:
                    log["phase0_micro_close_pending"] = True
            elif cancelled > 0 and not force_fok_cross and not adverse_dual_close:
                log["decision"] = "phase0_micro_wait_close"
                log["phase0_micro_close_reprice"] = True
            elif close_pending and not force_fok_cross and not adverse_dual_close:
                log["decision"] = "phase0_micro_wait_close"
                log["phase0_micro_close_pending"] = True
            elif (
                not micro_close_fok
                and not force_fok_cross
                and sell_price > 0.0
                and best_bid > 0.0
                and sell_price <= best_bid + 1e-12
            ):
                log["decision"] = "phase0_micro_wait_close"
                log["phase0_micro_skip_crossed_close"] = True
            else:
                add_limit_order(
                    output["orders"],
                    book_id,
                    "SELL",
                    sell_qty,
                    sell_price,
                    expiry_period,
                    exchange_min_quantity,
                    post_only=close_post_only,
                    time_in_force=close_tif,
                )
                sent += 1
                log["decision"] = (
                    "phase0_micro_adverse_dual_reprice_sell"
                    if adverse_dual_close
                    else "phase0_micro_close_sell"
                )
                if force_fok_cross or adverse_dual_close:
                    log["phase0_micro_close_reprice"] = True
        if also_scale_in and open_decision and not log.get("phase0_micro_take_on_target"):
            log["decision"] = f"{open_decision}+phase0_micro_close_sell"
            log["phase1_micro_scale_and_peel"] = 1
    if close_side == "short":
        open_decision = log.get("decision")
        # Maker cover by default; FOK@ask only when take_on_target PnL gate hits.
        if buy_close_qty < exchange_min_quantity:
            log["decision"] = "phase0_micro_skip_qty"
        elif buy_close_price <= 0.0:
            log["decision"] = "phase0_micro_skip_bad_close"
        elif not _micro_fifo_short_closeable(short_qty, buy_close_qty, flat_qty):
            log["decision"] = "phase0_micro_skip_fifo_desync"
            log["fifo_long_total_qty"] = long_qty
            log["fifo_short_total_qty"] = short_qty
            if close_entry_source:
                log["close_entry_source"] = close_entry_source
        else:
            from fifo_pnl import predict_short_close_realized

            # Match UID10: use account fee rates as-is (rebates allowed).
            taker_fee_rate = float(_v(book, "taker_fee_rate", maker_fee_rate))
            ask_px = round_decimals(best_ask, price_decimals)
            fifo_lots_cover = _fifo_lots_for_take_predict(
                book,
                side="short",
                qty=buy_close_qty,
                maker_fee_rate=pricing_maker_fee_rate,
                entry=close_entry,
            )
            pred_at_ask = (
                predict_short_close_realized(
                    fifo_lots_cover,
                    buy_close_qty,
                    ask_px,
                    taker_fee_rate,
                )
                if fifo_lots_cover and ask_px > 0.0
                else 0.0
            )
            log["phase0_micro_pred_pnl_at_ask"] = round(pred_at_ask, 8)
            force_fok_target = bool(
                take_on_target
                and ask_px > 0.0
                and take_pnl_meets_target(
                    pred_at_ask, micro_edge_quote, take_max_pnl
                )
            )
            if force_fok_target:
                log["phase0_micro_take_on_target"] = True
                # Drop same-tick scale-in SELL to avoid STP vs FOK buy.
                dropped = _drop_new_book_limits(
                    output["orders"], book_id, "SELL"
                )
                if dropped:
                    sent = max(0, sent - dropped)
                    log["phase0_micro_take_drop_scale_in"] = dropped
            elif (
                take_on_target
                and take_max_pnl > 0.0
                and ask_px > 0.0
                and pred_at_ask + 1e-12 >= micro_edge_quote
                and pred_at_ask > take_max_pnl + 1e-12
            ):
                log["phase0_micro_skip_take_above_max"] = True
                log["phase0_micro_take_max_pnl"] = take_max_pnl
            cover_price = ask_px if force_fok_target else buy_close_price
            cover_tif, cover_post_only = (
                ("FOK", False) if force_fok_target else ("GTC", True)
            )
            if free_quote < buy_close_qty * cover_price:
                log["decision"] = "phase0_micro_skip_no_quote"
            elif (
                not force_fok_target
                and buy_close_price >= best_ask - 1e-12
            ):
                # Ceiling at/through ask would cross — wait for better price.
                log["decision"] = "phase0_micro_wait_close"
                log["phase0_micro_skip_crossed_cover"] = True
                log["buy_price"] = buy_close_price
                log["buy_quantity"] = buy_close_qty
            else:
                has_buy = any(
                    str(_v(o, "direction", "")).upper() == "BUY"
                    and _v(o, "id", None) not in cancelled_ids
                    for o in open_orders
                )
                close_pending = bool(
                    _v(book, "phase0_micro_close_pending", False)
                )
                if force_fok_target and has_buy:
                    for order in open_orders:
                        order_id = _v(order, "id", None)
                        if order_id is None or order_id in cancelled_ids:
                            continue
                        if str(_v(order, "direction", "")).upper() != "BUY":
                            continue
                        add_cancel_order(output["orders"], book_id, order)
                        cancelled_ids.add(order_id)
                        cancelled += 1
                    has_buy = False
                log["buy_price"] = cover_price
                log["buy_quantity"] = buy_close_qty
                log["buy_tif"] = cover_tif
                log["buy_post_only"] = cover_post_only
                if has_buy:
                    log["decision"] = "phase0_micro_wait_close"
                    if close_pending:
                        log["phase0_micro_close_pending"] = True
                elif cancelled > 0 and not force_fok_target and not adverse_dual_close:
                    log["decision"] = "phase0_micro_wait_close"
                    log["phase0_micro_close_reprice"] = True
                elif close_pending and not force_fok_target and not adverse_dual_close:
                    log["decision"] = "phase0_micro_wait_close"
                    log["phase0_micro_close_pending"] = True
                else:
                    add_limit_order(
                        output["orders"],
                        book_id,
                        "BUY",
                        buy_close_qty,
                        cover_price,
                        expiry_period,
                        exchange_min_quantity,
                        post_only=cover_post_only,
                        time_in_force=cover_tif,
                    )
                    sent += 1
                    log["decision"] = (
                        "phase0_micro_adverse_dual_reprice_buy"
                        if adverse_dual_close
                        else "phase0_micro_close_buy"
                    )
                    if force_fok_target or adverse_dual_close:
                        log["phase0_micro_close_reprice"] = True
        if also_scale_in and open_decision and not log.get("phase0_micro_take_on_target"):
            log["decision"] = f"{open_decision}+phase0_micro_close_buy"
            log["phase1_micro_scale_and_peel"] = 1
    if not log.get("decision"):
        log["decision"] = "phase0_micro_wait"

    log["cancelled"] = cancelled
    log["orders_sent"] = sent
    if trend_flat_hold and sent == 0:
        log["action"] = "phase0_micro_trend_flat_hold"
    else:
        log["action"] = "phase0_micro_quote" if sent > 0 else "phase0_micro_wait"
    output["logs"].append(log)



def parse_micro_edge_books(raw) -> frozenset[int]:
    """Parse book id list from config (JSON list, set, or comma-separated string)."""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        parts = raw
    else:
        return frozenset()
    out: set[int] = set()
    for item in parts:
        try:
            bid = int(item)
        except (TypeError, ValueError):
            continue
        if bid >= 0:
            out.add(bid)
    return frozenset(out)


def process_micro_roundtrip(input: dict) -> dict:
    """Micro roundtrip: post-only BUY at best_bid, FIFO-accurate close for target PnL.

    Only books listed in phase0_micro_edge_books are traded; all others are skipped.
    """
    output: dict = {"orders": [], "logs": []}
    books = input.get("books")
    if not isinstance(books, list):
        return output

    books = sorted(
        books,
        key=lambda b: (
            0 if _topagent_book_fok_eligible(b) else 1,
            int(_v(b, "id", 0)),
        ),
    )

    price_decimals = int(_v(input, "price_decimals", 2))
    volume_decimals = int(_v(input, "volume_decimals", 4))
    tick = pow(10.0, -price_decimals)
    elapsed_min = float(_v(input, "scoring_elapsed_min", 0.0))
    # Strict hold: no opens/closes; cancel resting quotes until scoring clock clears rest.
    rest_minutes = float(_v(input, "micro_rest_minutes", 0.0) or 0.0)
    rest_hold = rest_minutes > 0.0 and elapsed_min + 1e-12 < rest_minutes
    exchange_min_quantity = float(_v(input, "exchange_min_quantity", 0.25))
    max_quantity = float(_v(input, "max_quantity", exchange_min_quantity))
    min_spread_ticks = float(_v(input, "min_spread_ticks", 0.5))
    expiry_period = int(_v(input, "expiry_period", 10_000_000_000))
    # Prefer agent flat_qty_threshold (e.g. 0.0005). Fallback keeps prior behavior.
    flat_qty = float(_v(input, "flat_qty_threshold", 0.0) or 0.0)
    if flat_qty <= 0.0:
        flat_qty = max(exchange_min_quantity * 0.5, 1e-6)
    micro_edge_books = parse_micro_edge_books(
        _v(input, "phase0_micro_edge_books", None)
    )
    phase0_edge_books = parse_micro_edge_books(
        _v(input, "phase0_edge_books", None)
    )
    micro_edge_quote = float(_v(input, "phase0_micro_edge_quote", 0.0))
    # Distinct from phase1 quote (engine overwrites phase0_micro_edge_quote).
    phase0_target_quote = float(_v(input, "phase0_target_profit", 0.0) or 0.0)
    if phase0_target_quote <= 0.0:
        phase0_target_quote = micro_edge_quote
    micro_edge_quantity = float(
        _v(input, "phase0_micro_edge_quantity", exchange_min_quantity)
    )
    micro_close_fok = bool(int(_v(input, "phase0_micro_close_fok", 0)))
    close_price_offset = max(
        0.0, float(_v(input, "phase0_micro_close_price_offset", 0.0))
    )
    lose_to_stop_pnl = float(
        _v(input, "phase0_micro_lose_to_stop_pnl", -0.01)
    )
    fok_max_pnl = float(_v(input, "phase0_micro_fok_max_pnl", 0.0))
    fok_min_pnl = float(_v(input, "phase0_micro_fok_min_pnl", -0.0025))
    micro_max_open_attempts = max(
        1, int(_v(input, "phase0_micro_max_open_attempts", 1))
    )
    micro_open_sell = bool(int(_v(input, "phase1_micro_open_sell", 0)))
    micro_open_quantity = float(
        _v(input, "phase1_micro_open_quantity", micro_edge_quantity)
    )
    take_on_target = bool(int(_v(input, "micro_take_on_target", 0)))
    # Cap opportunistic FOK takes (0 = no cap). Keeps peels near edge_quote.
    take_max_pnl = float(
        _v(
            input,
            "phase1_micro_take_max_quote",
            _v(input, "micro_take_max_pnl", 0.0),
        )
    )
    micro_open_at_mid_default = bool(
        int(_v(input, "phase1_micro_open_at_mid", 0))
    )
    net_close_threshold = float(_v(input, "micro_net_close_threshold", 0.5))
    # Cap inventory: |net| > max → no opens, close all at profitable target.
    max_net_qty = float(
        _v(
            input,
            "micro_max_net_qty",
            _v(input, "phase1_micro_max_net_qty", micro_open_quantity or 0.25),
        )
    )
    # Flat seek_open: place open + speculative close in the same tick.
    flat_dual_quote = bool(
        int(_v(input, "micro_flat_dual_quote", 1))
    )
    micro_trend_open = bool(int(_v(input, "micro_trend_open", 0)))
    trend_flat_threshold = float(
        _v(input, "micro_trend_flat_threshold", _v(input, "trend_flat_threshold", 0.50))
    )
    pressure_threshold = float(
        _v(
            input,
            "micro_pressure_threshold",
            _v(input, "pressure_imbalance_threshold", 0.12),
        )
    )
    micro_trend_require_pressure = bool(
        int(_v(input, "micro_trend_require_pressure", 1))
    )
    # Dual-only gate (stricter than open). 0 → fall back to open thresholds in handler.
    dual_trend_threshold = float(
        _v(input, "micro_dual_trend_threshold", 0.0)
    )
    dual_pressure_threshold = float(
        _v(input, "micro_dual_pressure_threshold", 0.0)
    )
    # Floor for maker-close pricing / resting-close fee checks (0 = never assume rebate).
    min_close_maker_fee_rate = float(
        _v(
            input,
            "min_close_maker_fee_rate",
            _v(input, "phase0_min_close_maker_fee_rate", 0.0),
        )
    )
    lock_open_price = bool(int(_v(input, "micro_lock_open_price", 0)))
    open_flat_only = bool(int(_v(input, "micro_open_flat_only", 0)))
    is_only_buy = bool(int(_v(input, "is_only_buy", 0)))
    # L21 thin-side open + depth_qty pricing (replaces trend gate when on).
    micro_depth_open = bool(int(_v(input, "micro_depth_open", 0)))
    micro_depth_levels = max(1, int(_v(input, "micro_depth_levels", 21) or 21))
    micro_depth_qty = float(_v(input, "micro_depth_qty", 100.0) or 100.0)
    micro_depth_imb_eps = float(_v(input, "micro_depth_imb_eps", 0.0) or 0.0)
    # Allow warehouse opens larger than global max_quantity (e.g. open 1.0).
    max_quantity = max(max_quantity, micro_open_quantity, micro_edge_quantity, max_net_qty)

    for book in books:
        log: dict = {"book_id": _v(book, "id", 0)}
        book_id = int(log["book_id"])
        best_bid = float(_v(book, "best_bid", 0.0))
        best_ask = float(_v(book, "best_ask", 0.0))
        log["best_bid"] = best_bid
        log["best_ask"] = best_ask

        if book_id not in micro_edge_books and book_id not in phase0_edge_books:
            log["action"] = "phase0_micro_skip"
            log["decision"] = "phase0_micro_skip_unlisted"
            output["logs"].append(log)
            continue

        if rest_hold and book_id not in phase0_edge_books:
            cancelled = 0
            for order in read_open_orders(book):
                add_cancel_order(output["orders"], book_id, order)
                cancelled += 1
            log["action"] = "phase0_micro_rest_hold"
            log["decision"] = "phase0_micro_rest_hold"
            log["scoring_elapsed_min"] = round(elapsed_min, 2)
            log["micro_rest_minutes"] = rest_minutes
            log["cancelled"] = cancelled
            log["orders_sent"] = 0
            output["logs"].append(log)
            continue

        is_phase0_edge = book_id in phase0_edge_books
        book_quote = phase0_target_quote if is_phase0_edge else micro_edge_quote
        # phase0: open-only during rest; close-only after rest.
        book_open_only = bool(rest_hold and is_phase0_edge)
        book_close_only = bool((not rest_hold) and is_phase0_edge)
        book_take_on_target = bool(take_on_target) or book_close_only
        if is_phase0_edge:
            log["phase0_edge"] = True
            log["phase0_target_profit"] = book_quote
            log["scoring_elapsed_min"] = round(elapsed_min, 2)
            log["micro_rest_minutes"] = rest_minutes
            log["rest_hold"] = bool(rest_hold)

        if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
            log["action"] = "skip_bad_market"
            log["decision"] = "phase0_skip_bad_market"
            output["logs"].append(log)
            continue

        spread_ticks = (best_ask - best_bid) / tick
        if spread_ticks < min_spread_ticks:
            log["action"] = "skip_tight_spread"
            log["decision"] = "phase0_skip_tight_spread"
            output["logs"].append(log)
            continue

        maker_fee_rate = float(_v(book, "maker_fee_rate", 0.0))

        if bool(_v(book, "at_volume_cap", False)):
            log["action"] = "skip_volume_cap"
            log["decision"] = "phase0_skip_volume_cap"
            output["logs"].append(log)
            continue

        free_base = read_free_base(book)
        free_quote = read_free_quote(book)
        if isinstance(book, dict):
            book.setdefault(
                "phase0_micro_max_open_attempts", micro_max_open_attempts
            )

        _handle_micro_book(
            output,
            book=book,
            book_id=book_id,
            log=log,
            best_bid=best_bid,
            best_ask=best_ask,
            tick=tick,
            price_decimals=price_decimals,
            volume_decimals=volume_decimals,
            exchange_min_quantity=exchange_min_quantity,
            max_quantity=max_quantity,
            expiry_period=expiry_period,
            elapsed_min=elapsed_min,
            micro_edge_quote=book_quote,
            micro_edge_quantity=micro_edge_quantity,
            maker_fee_rate=maker_fee_rate,
            free_base=free_base,
            free_quote=free_quote,
            flat_qty=flat_qty,
            micro_close_fok=micro_close_fok,
            close_price_offset=close_price_offset,
            lose_to_stop_pnl=lose_to_stop_pnl,
            fok_max_pnl=fok_max_pnl,
            fok_min_pnl=fok_min_pnl,
            micro_open_sell=bool(
                int(_v(book, "phase1_micro_open_sell", int(micro_open_sell)))
            ),
            micro_open_quantity=micro_open_quantity,
            take_on_target=book_take_on_target,
            take_max_pnl=take_max_pnl,
            micro_open_at_mid=bool(
                int(
                    _v(
                        book,
                        "phase1_micro_open_at_mid",
                        int(micro_open_at_mid_default),
                    )
                )
            ),
            net_close_threshold=net_close_threshold,
            micro_trend_open=micro_trend_open,
            trend_flat_threshold=trend_flat_threshold,
            min_close_maker_fee_rate=min_close_maker_fee_rate,
            max_net_qty=max_net_qty,
            flat_dual_quote=flat_dual_quote,
            pressure_threshold=pressure_threshold,
            micro_trend_require_pressure=micro_trend_require_pressure,
            dual_trend_threshold=dual_trend_threshold,
            dual_pressure_threshold=dual_pressure_threshold,
            lock_open_price=lock_open_price,
            open_only=book_open_only,
            close_only=book_close_only,
            is_only_buy=bool(int(_v(book, "is_only_buy", int(is_only_buy)))),
            open_flat_only=open_flat_only,
            micro_depth_open=micro_depth_open,
            micro_depth_levels=micro_depth_levels,
            micro_depth_qty=micro_depth_qty,
            micro_depth_imb_eps=micro_depth_imb_eps,
        )

    return output


def expected_long_close_profit(
    entry: float,
    exit_px: float,
    maker_fee: float,
    taker_fee: float,
) -> float:
    """Net per-unit profit closing a long at exit_px as taker (open assumed maker)."""
    if entry <= 0.0 or exit_px <= 0.0:
        return 0.0
    return exit_px * (1.0 - taker_fee) - entry * (1.0 + maker_fee)


def expected_short_close_profit(
    entry: float,
    exit_px: float,
    maker_fee: float,
    taker_fee: float,
) -> float:
    """Net per-unit profit covering a short at exit_px as taker (open assumed maker)."""
    if entry <= 0.0 or exit_px <= 0.0:
        return 0.0
    return entry * (1.0 - maker_fee) - exit_px * (1.0 + taker_fee)


def _topagent_resolve_close_qty(
    net_qty: float,
    held: float,
    close_quantity: float,
    close_dust_threshold: float,
    volume_decimals: int,
) -> float:
    cap = abs(net_qty)
    if cap < close_dust_threshold - 1e-12:
        qty = min(cap, held)
    else:
        qty = min(close_quantity, cap, held)
    return round_decimals(qty, volume_decimals)


def _topagent_close_entry(book: dict, net_qty: float) -> float:
    entry = float(_v(book, "entry_price", 0.0) or 0.0)
    if entry > 0.0:
        return entry
    if net_qty > 0.0:
        return _fifo_long_close_entry(book, abs(net_qty))
    if net_qty < 0.0:
        return _fifo_short_close_entry(book, abs(net_qty))
    return 0.0


def _topagent_manage_resting_close(
    output: dict,
    book_id: int,
    open_orders: list,
    *,
    side: str,
    close_px: float,
    close_qty: float,
    tick: float,
    qty_tol: float,
    long_close: bool,
) -> str:
    """Cancel stale resting close orders; return keep | wait | none."""
    if not open_orders:
        return "none"
    keep = False
    cancelled = False
    for order in open_orders:
        direction = str(_v(order, "direction", "")).upper()
        if direction != side:
            continue
        o_px = float(_v(order, "price", 0.0) or 0.0)
        o_qty = float(_v(order, "quantity", 0.0) or 0.0)
        order_id = _v(order, "id", None)
        if order_id is None:
            continue
        if o_px <= 0.0:
            add_cancel_order(output["orders"], book_id, order)
            cancelled = True
            continue
        if long_close:
            if o_px + tick * 0.51 < close_px:
                add_cancel_order(output["orders"], book_id, order)
                cancelled = True
                continue
        elif o_px > close_px + tick * 0.51:
            add_cancel_order(output["orders"], book_id, order)
            cancelled = True
            continue
        if abs(o_qty - close_qty) > qty_tol:
            add_cancel_order(output["orders"], book_id, order)
            cancelled = True
            continue
        if abs(o_px - close_px) <= tick * 0.51:
            keep = True
            continue
        add_cancel_order(output["orders"], book_id, order)
        cancelled = True
    if keep:
        return "keep"
    if cancelled:
        return "wait"
    return "none"


def _handle_topagent_close_book(
    output: dict,
    *,
    book: dict,
    close_quantity: float,
    close_dust_threshold: float,
    price_decimals: int,
    volume_decimals: int,
    exchange_min_quantity: float,
    expiry_period: int,
) -> None:
    """TopAgent hybrid close: fee-aware GTC post-only or FOK when touch is profitable."""
    book_id = int(_v(book, "id", 0))
    log: dict = {"book_id": book_id}
    tick = pow(10.0, -price_decimals)
    min_sz = float(exchange_min_quantity)
    qty_tol = min_sz * 0.51

    net_qty = float(_v(book, "net_qty", 0.0) or 0.0)
    if abs(net_qty) < min_sz - 1e-12:
        log["decision"] = "skip_no_inventory"
        output["logs"].append(log)
        return

    maker_fee = float(_v(book, "maker_fee_rate", 0.0) or 0.0)
    taker_fee = float(_v(book, "taker_fee_rate", maker_fee) or maker_fee)
    target_price = float(_v(book, "target_price", 0.0) or 0.0)
    best_bid = float(_v(book, "best_bid", 0.0) or 0.0)
    best_ask = float(_v(book, "best_ask", 0.0) or 0.0)
    free_base = float(_v(book, "free_base", 0.0) or 0.0)
    free_quote = float(_v(book, "free_quote", 0.0) or 0.0)
    open_orders = read_open_orders(book)
    fok_cooldown = bool(_v(book, "fok_cooldown", False))
    close_pending_side = _v(book, "close_pending_side", None)
    block_new = bool(_v(book, "block_new_limit", False))

    entry = _topagent_close_entry(book, net_qty)
    if entry <= 0.0:
        log["decision"] = "skip_no_entry"
        output["logs"].append(log)
        return

    if net_qty > 0.0:
        close_side_int = 1
        close_px = round_decimals(
            fee_aware_long_close_price(entry, target_price, maker_fee),
            price_decimals,
        )
        if close_px <= 0.0 or close_px <= entry + 1e-12:
            log["decision"] = "skip_bad_close_px"
            output["logs"].append(log)
            return
        close_qty = _topagent_resolve_close_qty(
            net_qty, free_base, close_quantity, close_dust_threshold, volume_decimals
        )
        if close_qty < min_sz - 1e-12:
            log["decision"] = "skip_qty"
            output["logs"].append(log)
            return

        use_fok = False
        if best_bid > 0.0 and best_bid >= close_px - 1e-12:
            exp_profit = expected_long_close_profit(
                entry, best_bid, maker_fee, taker_fee
            )
            use_fok = exp_profit > target_price + 1e-12

        if use_fok:
            if fok_cooldown:
                log["decision"] = "fok_cooldown"
                output["logs"].append(log)
                return
            for order in open_orders:
                if str(_v(order, "direction", "")).upper() == "SELL":
                    add_cancel_order(output["orders"], book_id, order)
            add_limit_order(
                output["orders"],
                book_id,
                "SELL",
                close_qty,
                close_px,
                expiry_period,
                exchange_min_quantity,
                post_only=False,
                time_in_force="FOK",
            )
            log["decision"] = "fok_close_long"
            log["close_side"] = close_side_int
            log["orders_sent"] = 1
            output["logs"].append(log)
            return

        if best_bid > 0.0 and close_px <= best_bid + 1e-12:
            log["decision"] = "skip_crossed_maker"
            output["logs"].append(log)
            return

        mgmt = _topagent_manage_resting_close(
            output,
            book_id,
            open_orders,
            side="SELL",
            close_px=close_px,
            close_qty=close_qty,
            tick=tick,
            qty_tol=qty_tol,
            long_close=True,
        )
        if mgmt in ("keep", "wait"):
            log["decision"] = f"resting_{mgmt}"
            log["close_side"] = close_side_int
            output["logs"].append(log)
            return
        if close_pending_side == 1:
            log["decision"] = "wait_pending"
            log["close_side"] = close_side_int
            output["logs"].append(log)
            return
        if block_new:
            log["decision"] = "skip_pending_guard"
            log["close_side"] = close_side_int
            output["logs"].append(log)
            return
        add_limit_order(
            output["orders"],
            book_id,
            "SELL",
            close_qty,
            close_px,
            expiry_period,
            exchange_min_quantity,
            post_only=True,
            time_in_force="GTC",
        )
        log["decision"] = "gtc_close_long"
        log["close_side"] = close_side_int
        log["orders_sent"] = 1
        output["logs"].append(log)
        return

    close_side_int = 0
    close_px = round_decimals(
        fee_aware_short_close_price(entry, target_price, maker_fee),
        price_decimals,
    )
    if close_px <= 0.0 or close_px >= entry - 1e-12:
        log["decision"] = "skip_bad_close_px"
        output["logs"].append(log)
        return
    ref_px = best_ask if best_ask > 0.0 else 0.0
    affordable = free_quote / ref_px if ref_px > 0.0 else 0.0
    close_qty = _topagent_resolve_close_qty(
        net_qty, affordable - 1.0, close_quantity, close_dust_threshold, volume_decimals
    )
    if close_qty < min_sz - 1e-12:
        log["decision"] = "skip_qty"
        output["logs"].append(log)
        return

    use_fok = False
    if best_ask > 0.0 and best_ask <= close_px + 1e-12:
        exp_profit = expected_short_close_profit(
            entry, best_ask, maker_fee, taker_fee
        )
        use_fok = exp_profit > target_price + 1e-12

    if use_fok:
        if fok_cooldown:
            log["decision"] = "fok_cooldown"
            output["logs"].append(log)
            return
        for order in open_orders:
            if str(_v(order, "direction", "")).upper() == "BUY":
                add_cancel_order(output["orders"], book_id, order)
        add_limit_order(
            output["orders"],
            book_id,
            "BUY",
            close_qty,
            close_px,
            expiry_period,
            exchange_min_quantity,
            post_only=False,
            time_in_force="FOK",
        )
        log["decision"] = "fok_close_short"
        log["close_side"] = close_side_int
        log["orders_sent"] = 1
        output["logs"].append(log)
        return

    if best_ask > 0.0 and close_px >= best_ask - 1e-12:
        log["decision"] = "skip_crossed_maker"
        output["logs"].append(log)
        return

    mgmt = _topagent_manage_resting_close(
        output,
        book_id,
        open_orders,
        side="BUY",
        close_px=close_px,
        close_qty=close_qty,
        tick=tick,
        qty_tol=qty_tol,
        long_close=False,
    )
    if mgmt in ("keep", "wait"):
        log["decision"] = f"resting_{mgmt}"
        log["close_side"] = close_side_int
        output["logs"].append(log)
        return
    if close_pending_side == 0:
        log["decision"] = "wait_pending"
        log["close_side"] = close_side_int
        output["logs"].append(log)
        return
    if block_new:
        log["decision"] = "skip_pending_guard"
        log["close_side"] = close_side_int
        output["logs"].append(log)
        return
    add_limit_order(
        output["orders"],
        book_id,
        "BUY",
        close_qty,
        close_px,
        expiry_period,
        exchange_min_quantity,
        post_only=True,
        time_in_force="GTC",
    )
    log["decision"] = "gtc_close_short"
    log["close_side"] = close_side_int
    log["orders_sent"] = 1
    output["logs"].append(log)


def _topagent_book_fok_eligible(book: dict) -> bool:
    """True when touch is crossed and taker profit exceeds target (FOK path)."""
    target_price = float(_v(book, "target_price", 0.0) or 0.0)
    net_qty = float(_v(book, "net_qty", 0.0) or 0.0)
    entry = _topagent_close_entry(book, net_qty)
    if entry <= 0.0:
        return False
    maker_fee = float(_v(book, "maker_fee_rate", 0.0) or 0.0)
    taker_fee = float(_v(book, "taker_fee_rate", maker_fee) or maker_fee)
    if net_qty > 0.0:
        close_px = fee_aware_long_close_price(entry, target_price, maker_fee)
        best_bid = float(_v(book, "best_bid", 0.0) or 0.0)
        if close_px <= 0.0 or best_bid < close_px - 1e-12:
            return False
        return (
            expected_long_close_profit(entry, best_bid, maker_fee, taker_fee)
            > target_price + 1e-12
        )
    close_px = fee_aware_short_close_price(entry, target_price, maker_fee)
    best_ask = float(_v(book, "best_ask", 0.0) or 0.0)
    if close_px <= 0.0 or best_ask <= 0.0 or best_ask > close_px + 1e-12:
        return False
    return (
        expected_short_close_profit(entry, best_ask, maker_fee, taker_fee)
        > target_price + 1e-12
    )


def process_topagent_close(input: dict) -> dict:
    """Batch TopAgent close engine: one pass over all close books (TopStrategy-style)."""
    output: dict = {"orders": [], "logs": []}
    books = input.get("books")
    if not isinstance(books, list):
        return output

    books = sorted(
        books,
        key=lambda b: (
            0 if _topagent_book_fok_eligible(b) else 1,
            int(_v(b, "id", 0)),
        ),
    )

    price_decimals = int(_v(input, "price_decimals", 2))
    volume_decimals = int(_v(input, "volume_decimals", 4))
    exchange_min_quantity = float(_v(input, "exchange_min_quantity", 0.25))
    close_quantity = float(_v(input, "close_quantity", exchange_min_quantity))
    close_dust_threshold = float(_v(input, "close_dust_threshold", 0.51))
    expiry_period = int(_v(input, "expiry_period", 200_000_000_000))
    max_orders = max(1, int(_v(input, "close_max_orders_per_tick", 20)))
    new_limits = 0

    for book in books:
        before = len(output["orders"])
        _handle_topagent_close_book(
            output,
            book=book,
            close_quantity=close_quantity,
            close_dust_threshold=close_dust_threshold,
            price_decimals=price_decimals,
            volume_decimals=volume_decimals,
            exchange_min_quantity=exchange_min_quantity,
            expiry_period=expiry_period,
        )
        for o in list(output["orders"][before:]):
            if str(o.get("type", "limit")).lower() != "limit":
                continue
            if new_limits >= max_orders:
                output["orders"].remove(o)
            else:
                new_limits += 1
    return output


def process_topagent_flatten(input: dict) -> dict:
    """TopAgent flatten engine: cancel all + market-close validator net (Topstrategy-style)."""
    from flatten_function import FlattenConfig, flatten_portfolio_orders

    flatten_books = input.get("flatten_books") or ()
    if isinstance(flatten_books, list):
        flatten_books = tuple(int(b) for b in flatten_books)
    elif isinstance(flatten_books, (set, frozenset)):
        flatten_books = tuple(sorted(int(b) for b in flatten_books))
    else:
        flatten_books = ()

    cfg = FlattenConfig(
        portfolio_flatten=int(_v(input, "portfolio_flatten", 0)),
        portfolio_flatten_use_fifo=0,
        portfolio_flatten_max_books=int(_v(input, "flatten_max_books_per_tick", 0)),
        portfolio_flatten_books=flatten_books,
        exchange_min_quantity=float(_v(input, "exchange_min_quantity", 0.25)),
        volume_decimals=int(_v(input, "volume_decimals", 4)),
        flat_qty=float(_v(input, "flatten_flat_qty", 0.0)),
        flatten_use_maker=0,
    )
    orders, logs = flatten_portfolio_orders(input.get("books") or [], cfg)
    return {"orders": orders, "logs": logs}


# --- 08_05 / 07_13 name aliases (Topstrategy bytecode from MVTRX_07_13) ---
parse_phase0_micro_edge_books = parse_micro_edge_books
process_phase0_roundtrip = process_micro_roundtrip
