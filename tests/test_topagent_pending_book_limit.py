"""TopAgent per-book waiting order cap."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import waiting_orders_recorder as wor
from TopAgent import TopAgent
from taos.im.protocol.models import OrderDirection


class _Acct:
    def __init__(self, orders=None):
        self.orders = orders or []


class _Order:
    def __init__(self, oid: int, side: int, price: float, qty: float):
        self.id = oid
        self.side = side
        self.price = price
        self.quantity = qty


def test_place_gtt_blocked_at_book_cap():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent.max_pending_per_book = 5
    agent._pending_sides_this_tick = set()
    agent._last_state_ts = 1000
    agent.expiry_period = 200_000_000_000
    agent.accounts = {3: _Acct()}

    pending = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 3,
            "direction": "SELL",
            "price": 365.0 + i,
            "quantity": 0.25,
            "timeInForce": "GTT",
            "wall_ts": 1.0,
            "status": "waiting_accept",
        }
        for i in range(5)
    ]
    wor._pending_by_uid[49] = pending

    class _Resp:
        def __init__(self):
            self.instructions = []

        def limit_order(self, **kwargs):
            self.instructions.append(kwargs)

    resp = _Resp()
    ok = agent._place_gtt_limit(
        resp,
        book_id=3,
        direction=OrderDirection.BUY,
        quantity=0.25,
        price=360.0,
    )
    assert ok is False
    assert resp.instructions == []


def test_place_fok_blocked_at_book_cap():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent.max_pending_per_book = 5
    agent._pending_sides_this_tick = set()
    agent._close_pending = {}
    agent._close_pending_sent_ts = {}
    agent._fok_close_last_ts = {}
    agent._last_state_ts = 1000
    agent.accounts = {3: _Acct()}

    wor._pending_by_uid[49] = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 3,
            "direction": "SELL",
            "price": 365.0 + i,
            "quantity": 0.25,
            "timeInForce": "GTT",
            "wall_ts": 1.0,
            "status": "waiting_accept",
        }
        for i in range(5)
    ]

    class _Resp:
        def __init__(self):
            self.instructions = []

        def limit_order(self, **kwargs):
            self.instructions.append(kwargs)

    resp = _Resp()
    ok = agent._place_fok_close_limit(
        resp,
        book_id=3,
        direction=OrderDirection.SELL,
        quantity=0.25,
        price=365.0,
    )
    assert ok is False
    assert resp.instructions == []


def test_place_fok_not_blocked_by_stale_cancel_backlog():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent.max_pending_per_book = 5
    agent._pending_sides_this_tick = set()
    agent._fok_close_last_ts = {}
    agent._last_state_ts = 1000
    agent.accounts = {42: _Acct([_Order(1, side=1, price=295.0, qty=0.25)])}

    wor._pending_by_uid[49] = [
        {
            "kind": "cancel",
            "type": "cancel",
            "book_id": 42,
            "order_id": 100 + i,
            "wall_ts": 1.0,
            "status": "waiting_cancel",
        }
        for i in range(8)
    ]

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
