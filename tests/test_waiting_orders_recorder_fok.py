"""waiting_orders_recorder IOC/FOK timeInForce normalization."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import waiting_orders_recorder as wor
from taos.im.protocol.models import TimeInForce


def test_normalize_tif_enum_repr():
    assert wor._normalize_tif_name(TimeInForce.FOK) == "FOK"
    assert wor._normalize_tif_name("TimeInForce.FOK") == "FOK"
    assert wor._normalize_tif_name("TimeInForce.IOC") == "IOC"
    assert wor._normalize_tif_name(3) == "FOK"
    assert wor._normalize_tif_name("GTT") == "GTT"


def test_fok_not_added_to_pending():
    wor._pending_by_uid.clear()
    pending: list[dict] = []
    sent = [
        {
            "type": "limit",
            "book_id": 42,
            "direction": "SELL",
            "price": 295.0,
            "quantity": 0.25,
            "timeInForce": "TimeInForce.FOK",
        }
    ]
    out = wor._add_sent_to_pending(pending, sent, now=2.0, sim_ts=100)
    assert out == []


def test_add_sent_skips_fok_when_prior_gtt_pending():
    pending = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 42,
            "direction": "SELL",
            "price": 295.0,
            "quantity": 0.25,
            "timeInForce": "GTT",
            "wall_ts": 1.0,
            "status": "waiting_accept",
        }
    ]
    sent = [
        {
            "type": "limit",
            "book_id": 42,
            "direction": "SELL",
            "price": 295.0,
            "quantity": 0.25,
            "timeInForce": "TimeInForce.FOK",
        }
    ]
    out = wor._add_sent_to_pending(pending, sent, now=2.0, sim_ts=100)
    assert len(out) == 1
    assert out[0]["timeInForce"] == "GTT"


def test_reconcile_drops_fok_pending():
    pending = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 42,
            "direction": "SELL",
            "price": 295.0,
            "quantity": 0.25,
            "timeInForce": "TimeInForce.FOK",
            "wall_ts": 1.0,
            "status": "waiting_accept",
        }
    ]
    kept = wor._reconcile_pending(pending, confirmed=[], now=2.0, max_age_sec=90.0)
    assert kept == []


def test_mark_pending_skips_fok():
    wor._pending_by_uid.clear()

    class _Agent:
        uid = 49

    agent = _Agent()
    wor.mark_pending_limit(
        agent, 42, side=1, price=295.0, quantity=0.25, time_in_force="FOK"
    )
    with wor._lock:
        assert wor._pending_by_uid.get(49) is None or wor._pending_by_uid.get(49) == []


def test_book_pending_cap_blocks_sixth():
    pending = [
        {
            "kind": "new",
            "type": "limit",
            "book_id": 42,
            "direction": "SELL",
            "price": 295.0 + i,
            "quantity": 0.25,
            "timeInForce": "GTT",
            "wall_ts": 1.0,
            "status": "waiting_accept",
        }
        for i in range(5)
    ]
    sent = [
        {
            "type": "limit",
            "book_id": 42,
            "direction": "BUY",
            "price": 290.0,
            "quantity": 0.25,
            "timeInForce": "GTT",
        }
    ]
    out = wor._add_sent_to_pending(
        pending, sent, now=2.0, sim_ts=100, max_pending_per_book=5
    )
    assert len(out) == 5


def test_cancel_dedupe_on_resend():
    pending = [
        {
            "kind": "cancel",
            "type": "cancel",
            "book_id": 42,
            "order_id": 548666,
            "wall_ts": 1.0,
            "status": "waiting_cancel",
        }
    ]
    sent = [{"type": "cancel", "book_id": 42, "order_id": 548666}]
    out = wor._add_sent_to_pending(pending, sent, now=2.0, sim_ts=100)
    cancels = [p for p in out if p.get("kind") == "cancel"]
    assert len(cancels) == 1
    assert cancels[0]["wall_ts"] == 2.0


def test_stale_cancel_dropped_on_reconcile():
    pending = [
        {
            "kind": "cancel",
            "type": "cancel",
            "book_id": 42,
            "order_id": 548666,
            "wall_ts": 1.0,
            "status": "waiting_cancel",
        }
    ]
    confirmed = [{"book_id": 42, "id": 548666, "direction": "SELL", "price": 295.0, "quantity": 0.25}]
    kept = wor._reconcile_pending(
        pending,
        confirmed,
        now=1.0 + wor.CANCEL_PENDING_MAX_AGE_SEC + 1.0,
        max_age_sec=90.0,
    )
    assert kept == []
