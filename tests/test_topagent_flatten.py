"""TopAgent flatten_books — Topstrategy-style engine flatten."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from TopAgent import TopAgent, parse_flatten_books
from flatten_function import exchange_net_qty, flatten_portfolio_orders, FlattenConfig
from strategy_engine import process_topagent_flatten
from taos.im.protocol.models import OrderCurrency, OrderDirection


class _Fees:
    maker_fee_rate = 0.005
    taker_fee_rate = 0.005


class _Bal:
    def __init__(self, *, free=0.0, total=0.0, initial=100.0):
        self.free = free
        self.total = total
        self.initial = initial


class _Acct:
    def __init__(self, *, base_free=10.0, quote_free=10000.0, base_initial=100.0, orders=None):
        self.base_balance = _Bal(free=base_free, total=base_free, initial=base_initial)
        self.quote_balance = _Bal(free=quote_free, total=quote_free, initial=10000.0)
        self.fees = _Fees()
        self.orders = orders or []
        self.base_loan = 0.0
        self.base_collateral = 0.0


def test_parse_flatten_books():
    assert parse_flatten_books([50, 76]) == {50, 76}
    assert parse_flatten_books([{"book": 50}]) == {50}


def test_exchange_net_qty_from_int_base():
    book = {"free_base": 132.5, "int_base": 100.0}
    assert exchange_net_qty(book) == 32.5


def test_flatten_engine_long_sends_sell_market():
    book = {
        "id": 50,
        "best_bid": 280.0,
        "best_ask": 281.0,
        "free_base": 132.5,
        "free_quote": 10000.0,
        "int_base": 100.0,
        "open_orders": [],
        "bids": [{"price": 280.0, "quantity": 10.0}],
        "asks": [{"price": 281.0, "quantity": 10.0}],
        "tick": 0.01,
    }
    cfg = FlattenConfig(
        portfolio_flatten_books=(50,),
        exchange_min_quantity=0.25,
        volume_decimals=2,
        flatten_use_maker=0,
    )
    orders, logs = flatten_portfolio_orders([book], cfg)
    market = [o for o in orders if o.get("type") == "market"]
    assert len(market) == 1
    assert market[0]["direction"] == "SELL"
    assert market[0]["quantity"] == 5.0
    assert any(l.get("flatten_action") == "sell_net" for l in logs)


def test_process_topagent_flatten_wrapper():
    result = process_topagent_flatten(
        {
            "flatten_books": [50],
            "exchange_min_quantity": 0.25,
            "volume_decimals": 2,
            "books": [
                {
                    "id": 50,
                    "best_bid": 280.0,
                    "best_ask": 281.0,
                    "free_base": 2.0,
                    "free_quote": 10000.0,
                    "int_base": 100.0,
                    "open_orders": [],
                    "bids": [{"price": 280.0, "quantity": 10.0}],
                    "asks": [{"price": 281.0, "quantity": 10.0}],
                }
            ],
        }
    )
    assert result["orders"]
    assert result["logs"]


def test_apply_flatten_engine_market_order():
    agent = TopAgent.__new__(TopAgent)
    agent.flatten_max_slippage = 0.02
    agent._fok_close_last_ts = {}
    agent._close_pending = {}
    agent._close_pending_sent_ts = {}
    agent.accounts = {50: _Acct(base_free=5.0, base_initial=100.0)}

    class _Resp:
        def __init__(self):
            self.calls = []

        def cancel_order(self, *args, **kwargs):
            pass

        def market_order(self, **kwargs):
            self.calls.append(kwargs)

    resp = _Resp()
    agent._clear_pending_limit = lambda *a, **k: None
    agent._clear_close_pending = lambda *a, **k: None
    agent._apply_flatten_engine_orders(
        resp,
        {
            "orders": [
                {
                    "type": "market",
                    "book_id": 50,
                    "direction": "SELL",
                    "quantity": 2.0,
                    "max_slippage": 0.50,
                }
            ],
            "logs": [],
        },
        sim_ts=0,
    )
    assert len(resp.calls) == 1
    c = resp.calls[0]
    assert c["book_id"] == 50
    assert c["direction"] == OrderDirection.SELL
    assert c["quantity"] == 2.0
    assert c["max_slippage"] == 0.02
    assert c["currency"] == OrderCurrency.BASE


def test_maybe_open_skips_flatten_books():
    agent = TopAgent.__new__(TopAgent)
    agent.close_books = {}
    agent.market_close_books = set()
    agent.flatten_books = {50}
    agent.max_maker_fee_rate = 1.0
    agent.open_quantity = 0.25
    agent.simulation_config = SimpleNamespace(min_order_size=0.25)
    agent.accounts = {50: _Acct()}
    agent._fifo_net_qty = lambda book_id, state=None: 0.0

    class _Resp:
        def limit_order(self, **kwargs):
            raise AssertionError("should not open on flatten book")

    agent._resolve_open_price = lambda *a, **k: (100.0, False)
    agent._effective_open_post_price = lambda px, *a, **k: px
    agent._has_resting_side = lambda *a, **k: False
    agent._place_gtt_limit = lambda *a, **k: None
    from taos.im.protocol.models import OrderDirection as OD

    spec = SimpleNamespace(
        open_direction=OD.BUY,
        max_quantity=10.0,
        open_price=100.0,
    )
    agent._maybe_open(_Resp(), 50, spec, pd=2, vd=2)
