# SPDX-License-Identifier: MIT
"""
Portfolio flatten: cancel open orders, then market-close exchange net inventory.

Primary target (cash book / validator qty):
    net_qty = free_base - int_base
            ≈ (bb.f) - (bb.i)
    fallback: position - neutral_position
            = (bb.t - loan + collateral) - bb.i

If |net| <= flat_qty: treat as flat (cancel leftover opens).
If 0 < |net| < exchange_min: open +min market, next tick close min+|net|.
  long dust:  BUY min, then SELL (min + qty)
  short dust: SELL min, then BUY (min + qty)
If |net| >= exchange_min: cancel all open orders, then market-close.
Flatten mode never rests makers (``flatten_use_maker=0``).

``portfolio_flatten_use_fifo=1`` is legacy only (tape qty); default is cash book.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy_engine import (
    add_cancel_order,
    finalize_order_quantity,
    read_open_orders,
    read_position,
    round_decimals,
)


@dataclass
class FlattenConfig:
    portfolio_flatten: int = 0
    portfolio_flatten_use_fifo: int = 0
    portfolio_flatten_max_books: int = 0
    portfolio_flatten_books: tuple[int, ...] = ()
    exchange_min_quantity: float = 0.25
    volume_decimals: int = 4
    flat_qty: float = 0.0
    flatten_use_maker: int = 0


def _default_flat_qty(exchange_min_quantity: float) -> float:
    # Tiny dust only; sub-min residuals use open-min then close min+qty.
    _ = exchange_min_quantity
    return 0.0005


def _zero_inventory_dust(
    long_qty: float, short_qty: float, flat_qty: float
) -> tuple[float, float]:
    """Treat qty at or below flat_qty as flat (zero inventory)."""
    if flat_qty <= 0.0:
        flat_qty = 0.0
    if long_qty <= flat_qty:
        long_qty = 0.0
    if short_qty <= flat_qty:
        short_qty = 0.0
    return long_qty, short_qty


def _market_order(book_id: int, direction: str, quantity: float) -> dict:
    return {
        "type": "market",
        "book_id": book_id,
        "direction": direction,
        "quantity": quantity,
        "max_slippage": 0.50,
    }


def _limit_maker_order(
    book_id: int, direction: str, quantity: float, price: float
) -> dict:
    return {
        "type": "limit",
        "book_id": book_id,
        "direction": direction,
        "quantity": quantity,
        "price": price,
        "postOnly": False,
        "timeInForce": "GTC",
    }


def _book_tick(book: dict) -> float:
    raw = book.get("tick") or book.get("tick_size")
    try:
        tick = float(raw or 0.0)
    except (TypeError, ValueError):
        tick = 0.0
    if tick > 0.0:
        return tick
    return 0.01


def _maker_close_price(
    direction: str, best_bid: float, best_ask: float, tick: float
) -> float:
    if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid or tick <= 0.0:
        return 0.0
    if str(direction).upper() == "SELL":
        px = round_decimals(best_bid + tick, 2)
        if px >= best_ask:
            px = round_decimals(best_ask, 2)
        if px <= best_bid:
            return 0.0
        return px
    px = round_decimals(best_ask - tick, 2)
    if px <= best_bid:
        px = round_decimals(best_bid, 2)
    if px >= best_ask:
        return 0.0
    return px


def _order_dir(order: dict) -> str:
    return str(order.get("direction") or order.get("side") or "").upper()


def _order_px(order: dict) -> float:
    try:
        return float(order.get("price") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _maker_still_good(
    order: dict,
    direction: str,
    best_bid: float,
    best_ask: float,
    min_qty: float,
) -> bool:
    if _order_dir(order) != str(direction).upper():
        return False
    try:
        qty = float(order.get("quantity") or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    if qty + 1e-12 < min_qty:
        return False
    px = _order_px(order)
    if px <= 0.0:
        return False
    if str(direction).upper() == "SELL":
        return px > best_bid and px <= best_ask + 1e-12
    return px < best_ask and px >= best_bid - 1e-12


def _visible_opposite_qty(book: dict, direction: str) -> float:
    """Sum of L2 qty on the side a market flatten would hit."""
    key = "bids" if str(direction).upper() == "SELL" else "asks"
    total = 0.0
    for lvl in book.get(key) or []:
        if isinstance(lvl, dict):
            total += float(lvl.get("quantity") or 0.0)
        elif isinstance(lvl, (list, tuple)) and len(lvl) > 1:
            total += float(lvl[1])
    return total


def _flatten_tick_qty(target: float, min_qty: float, visible: float) -> float:
    cap = _FLATTEN_MAX_QTY_PER_TICK
    if visible >= min_qty:
        cap = min(cap, visible)
    return min(target, cap)


_FLATTEN_MAX_QTY_PER_TICK = 5.0
_FLATTEN_MAKER_MAX_QTY = 50.0


def exchange_net_qty(book: dict) -> float:
    """Validator/cash open BASE: prefer free_base − int_base."""
    int_base = book.get("int_base")
    if int_base is None or int_base == "":
        int_base = book.get("base_balance_initial")
    if int_base is None or int_base == "":
        int_base = book.get("neutral_position")
    free = book.get("free_base")
    try:
        int_f = float(int_base) if int_base is not None and int_base != "" else None
    except (TypeError, ValueError):
        int_f = None
    try:
        free_f = float(free) if free is not None and free != "" else None
    except (TypeError, ValueError):
        free_f = None
    if int_f is not None and free_f is not None:
        return free_f - int_f
    position = read_position(book)
    neutral = float(book.get("neutral_position", 0.0) or 0.0)
    if int_f is not None:
        return float(position) - int_f
    return float(position) - neutral


def _long_short_qty(book: dict, cfg: FlattenConfig) -> tuple[float, float, float]:
    if cfg.portfolio_flatten_use_fifo:
        long_qty = float(
            book.get("fifo_long_total_qty")
            or book.get("fifo_long_head_qty")
            or 0.0
        )
        short_qty = float(
            book.get("fifo_short_total_qty")
            or book.get("fifo_short_head_qty")
            or 0.0
        )
        long_qty = max(0.0, long_qty)
        short_qty = max(0.0, short_qty)
        return long_qty, short_qty, long_qty - short_qty

    net = exchange_net_qty(book)
    if net > 0.0:
        return net, 0.0, net
    if net < 0.0:
        return 0.0, -net, net
    return 0.0, 0.0, 0.0


def flatten_book_orders(
    book: dict,
    cfg: FlattenConfig,
) -> tuple[list[dict], list[dict]]:
    """Flatten one book: cancel all opens, then market-close inventory."""
    orders: list[dict] = []
    logs: list[dict] = []
    book_id = int(book["id"])
    best_bid = float(book.get("best_bid", 0.0))
    best_ask = float(book.get("best_ask", 0.0))
    free_base = float(book.get("free_base", 0.0))
    free_quote = float(book.get("free_quote", 0.0))
    tick = _book_tick(book)
    use_maker = bool(cfg.flatten_use_maker)

    open_orders = read_open_orders(book)
    n_pre_cancel = 0
    for order in open_orders:
        add_cancel_order(orders, book_id, order)
        n_pre_cancel += 1
    if n_pre_cancel:
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "cancel_orders",
                "count": n_pre_cancel,
            }
        )
    open_orders = []

    flat_qty = cfg.flat_qty if cfg.flat_qty > 0.0 else _default_flat_qty(
        cfg.exchange_min_quantity
    )
    min_qty = round_decimals(cfg.exchange_min_quantity, cfg.volume_decimals)
    long_qty, short_qty, net_qty = _long_short_qty(book, cfg)
    long_qty, short_qty = _zero_inventory_dust(long_qty, short_qty, flat_qty)
    if long_qty <= 0.0 and short_qty <= 0.0:
        net_qty = 0.0
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "flat",
                "net_qty": net_qty,
                "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
            }
        )
        return orders, logs

    def _cancel_all() -> None:
        n_cancel = 0
        for order in open_orders:
            add_cancel_order(orders, book_id, order)
            n_cancel += 1
        if n_cancel:
            logs.append(
                {
                    "book_id": book_id,
                    "flatten_action": "cancel_orders",
                    "count": n_cancel,
                }
            )

    def _keep_or_place_maker(direction: str, qty: float, action: str) -> bool:
        price = _maker_close_price(direction, best_bid, best_ask, tick)
        if price <= 0.0:
            return False
        keep = None
        n_cancel = 0
        for order in open_orders:
            if keep is None and _maker_still_good(
                order, direction, best_bid, best_ask, min_qty
            ):
                keep = order
            else:
                add_cancel_order(orders, book_id, order)
                n_cancel += 1
        if n_cancel:
            logs.append(
                {
                    "book_id": book_id,
                    "flatten_action": "cancel_orders",
                    "count": n_cancel,
                }
            )
        if keep:
            logs.append(
                {
                    "book_id": book_id,
                    "flatten_action": "flatten_maker_rest",
                    "direction": direction,
                    "quantity": float(keep.get("quantity") or qty),
                    "price": _order_px(keep),
                    "net_qty": net_qty,
                    "target_qty": qty,
                }
            )
            return True
        orders.append(_limit_maker_order(book_id, direction, qty, price))
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": action,
                "quantity": qty,
                "price": price,
                "net_qty": net_qty,
                "target_qty": qty,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
            }
        )
        return True

    if flat_qty < long_qty < min_qty:
        cost = min_qty * best_ask
        if min_qty > 0.0 and best_ask > 0.0 and free_quote >= cost:
            _cancel_all()
            orders.append(_market_order(book_id, "BUY", min_qty))
            logs.append(
                {
                    "book_id": book_id,
                    "flatten_action": "buy_open_min",
                    "quantity": min_qty,
                    "net_qty": net_qty,
                    "target_qty": long_qty,
                    "close_qty": round_decimals(min_qty + long_qty, cfg.volume_decimals),
                    "free_quote": free_quote,
                    "free_base": free_base,
                    "int_base": book.get("int_base", book.get("neutral_position")),
                    "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
                }
            )
            return orders, logs
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "skip_buy_open_min_no_quote",
                "net_qty": net_qty,
                "target_qty": long_qty,
                "min_qty": min_qty,
                "free_quote": free_quote,
            }
        )
        return orders, logs

    if long_qty >= min_qty:
        sell_qty = round_decimals(
            min(long_qty, free_base, _FLATTEN_MAKER_MAX_QTY),
            cfg.volume_decimals,
        )
        if sell_qty >= min_qty and use_maker:
            if _keep_or_place_maker("SELL", sell_qty, "sell_net_maker"):
                return orders, logs
        if sell_qty >= min_qty:
            _cancel_all()
            mkt_qty = round_decimals(
                min(
                    sell_qty,
                    _flatten_tick_qty(
                        long_qty, min_qty, _visible_opposite_qty(book, "SELL")
                    ),
                ),
                cfg.volume_decimals,
            )
            if mkt_qty >= min_qty:
                orders.append(_market_order(book_id, "SELL", mkt_qty))
                logs.append(
                    {
                        "book_id": book_id,
                        "flatten_action": "sell_net",
                        "quantity": mkt_qty,
                        "net_qty": net_qty,
                        "target_qty": long_qty,
                        "free_base": free_base,
                        "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
                    }
                )
                return orders, logs
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "skip_sell_no_base",
                "net_qty": net_qty,
                "target_qty": long_qty,
                "free_base": free_base,
            }
        )
        return orders, logs

    if flat_qty < short_qty < min_qty:
        sell_open = round_decimals(min(min_qty, free_base), cfg.volume_decimals)
        if sell_open >= min_qty:
            _cancel_all()
            orders.append(_market_order(book_id, "SELL", sell_open))
            logs.append(
                {
                    "book_id": book_id,
                    "flatten_action": "sell_open_min",
                    "quantity": sell_open,
                    "net_qty": net_qty,
                    "target_qty": short_qty,
                    "close_qty": round_decimals(min_qty + short_qty, cfg.volume_decimals),
                    "free_base": free_base,
                    "int_base": book.get("int_base", book.get("neutral_position")),
                    "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
                }
            )
            return orders, logs
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "skip_sell_open_min_no_base",
                "net_qty": net_qty,
                "target_qty": short_qty,
                "min_qty": min_qty,
                "free_base": free_base,
            }
        )
        return orders, logs

    if short_qty >= min_qty:
        buy_qty = round_decimals(
            min(short_qty, _FLATTEN_MAKER_MAX_QTY),
            cfg.volume_decimals,
        )
        cost = buy_qty * (best_ask if best_ask > 0.0 else best_bid)
        if buy_qty >= min_qty and use_maker and free_quote >= cost:
            if _keep_or_place_maker("BUY", buy_qty, "buy_net_maker"):
                return orders, logs
        if buy_qty >= min_qty and best_ask > 0.0:
            _cancel_all()
            mkt_qty = finalize_order_quantity(
                _flatten_tick_qty(
                    short_qty, min_qty, _visible_opposite_qty(book, "BUY")
                ),
                _flatten_tick_qty(
                    short_qty, min_qty, _visible_opposite_qty(book, "BUY")
                ),
                cfg.exchange_min_quantity,
                cfg.volume_decimals,
            )
            mkt_cost = mkt_qty * best_ask
            if mkt_qty >= min_qty and free_quote >= mkt_cost:
                orders.append(_market_order(book_id, "BUY", mkt_qty))
                logs.append(
                    {
                        "book_id": book_id,
                        "flatten_action": "buy_net",
                        "quantity": mkt_qty,
                        "net_qty": net_qty,
                        "target_qty": short_qty,
                        "free_quote": free_quote,
                        "mode": "fifo" if cfg.portfolio_flatten_use_fifo else "exchange_net",
                    }
                )
                return orders, logs
        logs.append(
            {
                "book_id": book_id,
                "flatten_action": "skip_buy_no_quote",
                "net_qty": net_qty,
                "target_qty": short_qty,
                "free_quote": free_quote,
            }
        )
        return orders, logs

    logs.append(
        {
            "book_id": book_id,
            "flatten_action": "skip_below_min",
            "net_qty": net_qty,
            "min_qty": min_qty,
        }
    )
    return orders, logs


def flatten_portfolio_orders(
    books: list[dict],
    cfg: FlattenConfig,
) -> tuple[list[dict], list[dict]]:
    """Flatten all books (or up to ``portfolio_flatten_max_books``)."""
    if not cfg.portfolio_flatten and not cfg.portfolio_flatten_books:
        return [], []

    work = sorted(books, key=lambda b: int(b["id"]))
    if cfg.portfolio_flatten_books:
        allowed = set(cfg.portfolio_flatten_books)
        work = [b for b in work if int(b["id"]) in allowed]
    max_books = int(cfg.portfolio_flatten_max_books)
    if max_books > 0:
        work = work[:max_books]

    all_orders: list[dict] = []
    all_logs: list[dict] = []
    for book in work:
        orders, logs = flatten_book_orders(book, cfg)
        all_orders.extend(orders)
        all_logs.extend(logs)
    return all_orders, all_logs


def flatten_books_marked_flat(logs: list[dict]) -> list[int]:
    """Book ids reported flat by ``flatten_book_orders`` (no inventory, no orders)."""
    return sorted(
        int(log["book_id"])
        for log in logs
        if log.get("flatten_action") == "flat" and "book_id" in log
    )
