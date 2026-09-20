"""Prices must survive the exchange's ROUND_DOWN truncation.

The exchange stores the truncation of a float's exact binary value, so a
double sitting just under its decimal (437.2 is 437.19999...) used to rest a
full tick low and the bracket was cancelled and re-placed every tick forever.
"""

from __future__ import annotations

import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import TopAgent as top


def _as_exchange_stores(price: float, decimals: int) -> Decimal:
    """Mirror of the exchange: ROUND_DOWN on the exact binary value."""
    return Decimal(price).quantize(
        Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN
    )


# Every price the live bracket used, including the ones that churned.
@pytest.mark.parametrize(
    "price", [437.2, 438.2, 298.88, 298.87, 347.92, 348.42, 370.16, 371.16]
)
def test_rounded_price_survives_exchange_truncation(price):
    out = top._round_px(price, 2)
    assert _as_exchange_stores(out, 2) == Decimal(repr(price))


def test_the_price_that_churned_book_45():
    """437.2 rested at 437.19 before the fix."""
    assert _as_exchange_stores(437.2, 2) == Decimal("437.19")
    assert _as_exchange_stores(top._round_px(437.2, 2), 2) == Decimal("437.20")


def test_nudge_is_below_half_a_tick():
    """The correction must be invisible to any tick-level comparison."""
    for price in (437.2, 438.2, 298.88):
        assert abs(top._round_px(price, 2) - price) < top._tick(2) * 0.51


def test_prices_already_above_are_untouched():
    assert top._round_px(347.92, 2) == 347.92
    assert top._round_px(370.16, 2) == 370.16


def test_rounding_decisions_are_unchanged():
    assert top._round_px(437.196, 2) == pytest.approx(437.20)
    assert top._round_px(437.194, 2) == pytest.approx(437.19)
    assert top._round_px(0.0, 2) == 0.0


def test_higher_precision_books():
    for decimals in (0, 1, 3, 5, 8):
        out = top._round_px(437.2, decimals)
        assert _as_exchange_stores(out, decimals) == Decimal(repr(round(437.2, decimals)))
