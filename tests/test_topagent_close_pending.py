"""TopAgent close_pending latch (one close per active book)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import waiting_orders_recorder as wor
from TopAgent import TopAgent
from taos.im.protocol.models import OrderDirection


class _Order:
    def __init__(self, oid: int, side: int, price: float, qty: float):
        self.id = oid
        self.side = side
        self.price = price
        self.quantity = qty


class _Acct:
    def __init__(self, orders=None):
        self.orders = orders or []


def test_close_pending_blocks_second_gtc_send():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent.max_pending_per_book = 5
    agent.expiry_period = 200_000_000_000
    agent._pending_sides_this_tick = set()
    agent._close_pending = {3: 1}
    agent._close_pending_sent_ts = {3: 1000}
    agent._last_state_ts = 1000
    agent.accounts = {3: _Acct()}

    class _Resp:
        def __init__(self):
            self.instructions = []

        def limit_order(self, **kwargs):
            self.instructions.append(kwargs)

    resp = _Resp()
    ok1 = agent._place_gtc_close_limit(
        resp,
        book_id=3,
        direction=OrderDirection.SELL,
        quantity=0.25,
        price=365.0,
    )
    assert ok1 is False
    assert resp.instructions == []


def test_fok_not_blocked_by_close_pending_latch():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent.max_pending_per_book = 5
    agent._pending_sides_this_tick = set()
    agent._close_pending = {42: 1}
    agent._close_pending_sent_ts = {42: 1_000_000_000}
    agent._fok_close_last_ts = {}
    agent._last_state_ts = 5_000_000_000
    agent.accounts = {
        42: _Acct([_Order(99, side=1, price=295.0, qty=0.25)])
    }

    class _Resp:
        def __init__(self):
            self.instructions = []

        def limit_order(self, **kwargs):
            self.instructions.append(kwargs)

    resp = _Resp()
    ok = agent._place_fok_close_limit(
        resp,
        book_id=42,
        direction=OrderDirection.SELL,
        quantity=0.25,
        price=295.0,
    )
    assert ok is True
    assert len(resp.instructions) == 1


def test_fok_cooldown_blocks_resend():
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.max_pending_per_book = 5
    agent._pending_sides_this_tick = set()
    agent._close_pending = {}
    agent._close_pending_sent_ts = {}
    agent._fok_close_last_ts = {42: 1_000_000_000}
    agent.accounts = {42: _Acct()}

    assert agent._fok_close_on_cooldown(42, 2_000_000_000) is True
    assert agent._fok_close_on_cooldown(42, 5_000_000_000) is False
