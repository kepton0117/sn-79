"""TopAgent market_close_books — market slice closes."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from TopAgent import TopAgent, parse_market_close_books
from taos.im.protocol.models import OrderCurrency, OrderDirection


class _Fees:
    maker_fee_rate = 0.005
    taker_fee_rate = 0.005


class _Acct:
    def __init__(self, *, base_free=10.0, quote_free=10000.0, orders=None):
        self.base_balance = SimpleNamespace(free=base_free)
        self.quote_balance = SimpleNamespace(free=quote_free)
        self.fees = _Fees()
        self.orders = orders or []


def test_parse_market_close_books():
    assert parse_market_close_books([73, 42]) == {73, 42}
    assert parse_market_close_books([{"book": 73}, {"book": 125}]) == {73, 125}


def test_market_close_long_sends_sell():
    agent = TopAgent.__new__(TopAgent)
    agent.close_quantity = 0.25
    agent.close_dust_threshold = 0.51
    agent.market_close_max_slippage = 0.01
    agent.max_maker_fee_rate = 1.0
    agent.simulation_config = SimpleNamespace(min_order_size=0.25)
    agent.accounts = {73: _Acct(base_free=5.0)}
    agent._fok_close_last_ts = {}
    agent._last_state_ts = 5_000_000_000
    agent._fifo_net_qty = lambda book_id, state=None: 2.0

    class _Resp:
        def __init__(self):
            self.calls = []

        def market_order(self, **kwargs):
            self.calls.append(kwargs)

        def cancel_order(self, *args, **kwargs):
            pass

    resp = _Resp()
    agent._cancel_all_resting_close = lambda *a, **k: None
    agent._maybe_market_close(resp, 73, pd=2, vd=2, state=SimpleNamespace(books={73: {}}))

    assert len(resp.calls) == 1
    c = resp.calls[0]
    assert c["book_id"] == 73
    assert c["direction"] == OrderDirection.SELL
    assert c["quantity"] == 0.25
    assert c["currency"] == OrderCurrency.BASE


def test_market_close_respects_cooldown():
    agent = TopAgent.__new__(TopAgent)
    agent.close_quantity = 0.25
    agent.close_dust_threshold = 0.51
    agent.market_close_max_slippage = 0.01
    agent.max_maker_fee_rate = 1.0
    agent.simulation_config = SimpleNamespace(min_order_size=0.25)
    agent.accounts = {73: _Acct(base_free=5.0)}
    agent._fok_close_last_ts = {73: 4_000_000_000}
    agent._last_state_ts = 5_000_000_000
    agent._fifo_net_qty = lambda book_id, state=None: 2.0

    class _Resp:
        def market_order(self, **kwargs):
            raise AssertionError("should not send on cooldown")

    agent._cancel_all_resting_close = lambda *a, **k: None
    agent._maybe_market_close(_Resp(), 73, pd=2, vd=2, state=SimpleNamespace(books={73: {}}))


def test_market_close_short_sends_buy():
    agent = TopAgent.__new__(TopAgent)
    agent.close_quantity = 0.25
    agent.close_dust_threshold = 0.51
    agent.market_close_max_slippage = 0.01
    agent.max_maker_fee_rate = 1.0
    agent.simulation_config = SimpleNamespace(min_order_size=0.25)
    agent.accounts = {73: _Acct(base_free=0.0, quote_free=5000.0)}
    agent._fok_close_last_ts = {}
    agent._last_state_ts = 5_000_000_000
    agent._fifo_net_qty = lambda book_id, state=None: -2.0
    agent._best_bid_ask = lambda book: (300.0, 301.0)

    class _Resp:
        def __init__(self):
            self.calls = []

        def market_order(self, **kwargs):
            self.calls.append(kwargs)

    resp = _Resp()
    agent._cancel_all_resting_close = lambda *a, **k: None
    state = SimpleNamespace(books={73: SimpleNamespace(bids=[], asks=[])})
    agent._maybe_market_close(resp, 73, pd=2, vd=2, state=state)

    assert len(resp.calls) == 1
    assert resp.calls[0]["direction"] == OrderDirection.BUY
    assert resp.calls[0]["quantity"] == 0.25
