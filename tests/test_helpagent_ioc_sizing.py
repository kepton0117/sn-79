"""HelpAgent._help_ioc sizes from the free wallet, not the target's size."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import HelpAgent as help_mod


class _Response:
    def __init__(self):
        self.orders = []

    def limit_order(self, **kw):
        self.orders.append(kw)


def _acct(base_free: float, quote_free: float):
    return SimpleNamespace(
        base_balance=SimpleNamespace(free=base_free),
        quote_balance=SimpleNamespace(free=quote_free),
    )


def _agent():
    return help_mod.HelpAgent.__new__(help_mod.HelpAgent)


def _send(agent, response, acct, *, target_side, px, remaining=100.0):
    return help_mod.HelpAgent._help_ioc(
        agent,
        response,
        acct,
        45,
        target_side=target_side,
        px=px,
        remaining=remaining,
        vd=4,
        min_sz=0.25,
    )


def test_sell_uses_all_free_base_less_one():
    response = _Response()
    assert _send(
        _agent(), response, _acct(49.9638, 0.0), target_side="BUY", px=437.19
    )
    order = response.orders[0]
    assert order["quantity"] == 48.9638
    assert order["price"] == 437.19
    assert order["direction"] == help_mod.OrderDirection.SELL


def test_buy_sizes_off_the_order_price_not_mid():
    response = _Response()
    assert _send(
        _agent(), response, _acct(0.0, 43820.0), target_side="SELL", px=438.20
    )
    order = response.orders[0]
    # 43820 / 438.20 = 100 base affordable, minus the one-unit reserve.
    assert order["quantity"] == 99.0
    assert order["price"] == 438.20
    assert order["direction"] == help_mod.OrderDirection.BUY
    assert order["quantity"] * order["price"] <= 43820.0


def test_size_ignores_the_targets_recorded_remaining():
    """A stale/undersized waiting_orders row must not shrink the help order."""
    response = _Response()
    assert _send(
        _agent(),
        response,
        _acct(500.0, 0.0),
        target_side="BUY",
        px=437.19,
        remaining=1.0,
    )
    assert response.orders[0]["quantity"] == 499.0


def test_no_order_when_wallet_is_below_the_reserve():
    response = _Response()
    assert not _send(
        _agent(), response, _acct(0.9, 0.0), target_side="BUY", px=437.19
    )
    assert response.orders == []


def test_no_order_when_target_level_is_dust():
    """remaining still gates: a level below min size means no dual to hit."""
    response = _Response()
    assert not _send(
        _agent(),
        response,
        _acct(500.0, 0.0),
        target_side="BUY",
        px=437.19,
        remaining=0.1,
    )
    assert response.orders == []


def test_ioc_flags_are_preserved():
    response = _Response()
    _send(_agent(), response, _acct(49.9638, 0.0), target_side="BUY", px=437.19)
    order = response.orders[0]
    assert order["timeInForce"] == help_mod.TimeInForce.IOC
    assert order["stp"] == help_mod.STP.CANCEL_BOTH
    assert order["leverage"] == 0.0
