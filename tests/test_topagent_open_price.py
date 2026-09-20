"""TopAgent open price resolution (fixed, depth, mid)."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from TopAgent import OpenBookSpec, TopAgent, mid_open_price
from taos.im.protocol.models import OrderDirection


def test_mid_open_price_buy_inside_spread():
    book = SimpleNamespace(
        bids=[SimpleNamespace(price=100.0, quantity=10.0)],
        asks=[SimpleNamespace(price=102.0, quantity=10.0)],
    )
    px = mid_open_price(book, OrderDirection.BUY, pd=2)
    assert px == 101.0


def test_mid_open_price_sell_inside_spread():
    book = SimpleNamespace(
        bids=[SimpleNamespace(price=100.0, quantity=10.0)],
        asks=[SimpleNamespace(price=102.0, quantity=10.0)],
    )
    px = mid_open_price(book, OrderDirection.SELL, pd=2)
    assert px == 101.0


def test_resolve_open_price_mid_when_no_depth_and_zero_price():
    agent = TopAgent.__new__(TopAgent)
    agent.open_depth_levels = 21
    agent.open_depth_qty = 200.0
    spec = OpenBookSpec(
        book=7,
        open_price=0.0,
        max_quantity=50.0,
        use_depth_qty=False,
        open_direction=OrderDirection.BUY,
    )
    book = SimpleNamespace(
        bids=[SimpleNamespace(price=200.0, quantity=5.0)],
        asks=[SimpleNamespace(price=204.0, quantity=5.0)],
    )
    state = SimpleNamespace(books={7: book})
    px, from_depth, from_mid = TopAgent._resolve_open_price(agent, 7, spec, state, pd=2)
    assert px == 202.0
    assert from_depth is False
    assert from_mid is True


def test_resolve_open_price_fixed_when_price_set():
    agent = TopAgent.__new__(TopAgent)
    spec = OpenBookSpec(
        book=1,
        open_price=457.63,
        max_quantity=50.0,
        use_depth_qty=False,
    )
    px, from_depth, from_mid = TopAgent._resolve_open_price(
        agent, 1, spec, SimpleNamespace(books={}), pd=2
    )
    # Sub-tick: _round_px keeps the double at or above its decimal so the
    # exchange's ROUND_DOWN does not drop it a tick.
    assert px == pytest.approx(457.63)
    assert from_depth is False
    assert from_mid is False
