"""TopAgent pending-order guard (no duplicate sends while awaiting accept)."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import waiting_orders_recorder as wor
from TopAgent import TopAgent


class _Order:
    def __init__(self, oid: int, side: int, price: float, qty: float):
        self.id = oid
        self.side = side
        self.price = price
        self.quantity = qty


class _Acct:
    def __init__(self, orders=None):
        self.orders = orders or []


class _AgentStub:
    uid = 49
    accounts = {3: _Acct()}


def test_has_pending_new_limit_after_register():
    wor._pending_by_uid.clear()
    agent = _AgentStub()
    resp = SimpleNamespace(
        instructions=[
            SimpleNamespace(
                type="PLACE_ORDER_LIMIT",
                bookId=3,
                direction=1,
                price=365.0,
                quantity=0.25,
                postOnly=False,
                timeInForce="GTT",
            )
        ]
    )
    wor.register_sent_limits(agent, resp, sim_ts=1000)
    assert wor.has_pending_new_limit(agent, 3, side=1) is True
    assert wor.has_pending_new_limit(agent, 3, side=0) is False


def test_reconcile_drops_pending_when_confirmed():
    wor._pending_by_uid.clear()
    agent = _AgentStub()
    resp = SimpleNamespace(
        instructions=[
            SimpleNamespace(
                type="PLACE_ORDER_LIMIT",
                bookId=3,
                direction=1,
                price=365.0,
                quantity=0.25,
                postOnly=False,
                timeInForce="GTT",
            )
        ]
    )
    wor.register_sent_limits(agent, resp, sim_ts=1000)
    agent.accounts[3] = _Acct([_Order(1, side=1, price=365.0, qty=0.25)])
    wor.reconcile_pending_before_tick(agent, max_age_sec=90.0)
    assert wor.has_pending_new_limit(agent, 3, side=1) is False


def test_add_sent_dedupes_same_fingerprint():
    pending = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 3,
            "direction": "SELL",
            "price": 365.0,
            "quantity": 0.25,
            "wall_ts": time.time(),
            "sim_ts": 1000,
            "status": "waiting_accept",
        }
    ]
    sent = [
        {
            "type": "limit",
            "book_id": 3,
            "direction": "SELL",
            "price": 365.0,
            "quantity": 0.25,
            "timeInForce": "GTT",
        }
    ]
    out = wor._add_sent_to_pending(pending, sent, now=time.time(), sim_ts=1001)
    assert len(out) == 1


def test_topagent_skips_open_when_pending():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent._pending_sides_this_tick = set()
    agent._last_state_ts = 1000
    agent.accounts = {3: _Acct()}
    wor.mark_pending_limit(agent, 3, side=0, price=300.0, quantity=0.25, sim_ts=1000)
    assert agent._has_pending_limit(3, side=0) is True


def test_mark_pending_replaces_same_book_side():
    wor._pending_by_uid.clear()
    agent = _AgentStub()
    wor.mark_pending_limit(agent, 3, side=1, price=365.0, quantity=0.25, sim_ts=1000)
    wor.mark_pending_limit(agent, 3, side=1, price=366.0, quantity=0.25, sim_ts=1001)
    with wor._lock:
        pending = wor._pending_by_uid.get(49) or []
    new_on_side = [
        p
        for p in pending
        if int(p.get("book_id", -1)) == 3 and wor._side_name(p.get("direction")) == "SELL"
    ]
    assert len(new_on_side) == 1
    assert float(new_on_side[0]["price"]) == 366.0


def test_place_gtt_limit_blocks_second_send_same_tick():
    wor._pending_by_uid.clear()
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 49
    agent.pending_guard = True
    agent.pending_guard_max_age_sec = 90.0
    agent._pending_sides_this_tick = set()
    agent._last_state_ts = 1000
    agent.expiry_period = 200_000_000_000
    agent.accounts = {3: _Acct()}

    class _Resp:
        def __init__(self):
            self.instructions = []

        def limit_order(self, **kwargs):
            self.instructions.append(kwargs)

    resp = _Resp()
    ok1 = agent._place_gtt_limit(
        resp, book_id=3, direction=__import__("taos.im.protocol.models", fromlist=["OrderDirection"]).OrderDirection.SELL,
        quantity=0.25, price=365.0,
    )
    ok2 = agent._place_gtt_limit(
        resp, book_id=3, direction=__import__("taos.im.protocol.models", fromlist=["OrderDirection"]).OrderDirection.SELL,
        quantity=0.25, price=365.0,
    )
    assert ok1 is True
    assert ok2 is False
    assert len(resp.instructions) == 1
