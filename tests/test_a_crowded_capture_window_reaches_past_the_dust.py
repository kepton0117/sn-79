# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""A fill's capture mid reads the W nearest prints on each side that clear its dust floor, reaching past prints below
the floor up to CAPTURE_REACH_WINDOWS x W prints a side. Prints below the floor therefore cannot crowd a fill's real
neighbours out of its window: surrounding a fill with them leaves its mid, and its captured spread, where the market
put them. Without dust the window is the plain 2W+1 one, and the batch and live paths agree whatever the batching."""
from collections import defaultdict

from taos.im.validator.debeta import (
    CAPTURE_REACH_WINDOWS, accumulate_book_capture, centered_mid_floored, flush_capture_state, running_floors,
)

W = 15
REACH = CAPTURE_REACH_WINDOWS * W
BOOK = 7
VICTIM = 9
BG = -2
BG2 = -3
MIN_SIZE = 0.25


def _mk(price, q=10.0, s=0, ma=BG2, ta=BG):
    return {"p": price, "q": q, "s": s, "Ma": ma, "Ta": ta}


def _market(n, start=0):
    """Background prints around 100 with some texture, so every window's mean is distinct."""
    return [_mk(100.0 + 0.01 * (((start + i) * 7) % 5 - 2), q=10.0 + ((start + i) % 4)) for i in range(n)]


def _dust(n, price=104.0):
    return [_mk(price + 0.01 * (i % 3), q=0.001, s=i % 2, ma=7, ta=8) for i in range(n)]


def _victim_fill():
    return _mk(99.6, q=10.0, s=1, ma=VICTIM, ta=BG)   # the victim buys 0.4 under the market's mid


def _run(seq, batches=None):
    buy, sell = defaultdict(dict), defaultdict(dict)
    if batches is None:
        accumulate_book_capture(buy, sell, BOOK, seq, W, ts=0, min_size=MIN_SIZE)
        return buy, sell
    state = {}
    for k in range(0, len(seq), batches):
        accumulate_book_capture(buy, sell, BOOK, seq[k:k + batches], W, ts=k, mid_state=state, min_size=MIN_SIZE)
    flush_capture_state(state, buy, sell, W, ts=len(seq), force=True)
    return buy, sell


def _victim_credit(seq, batches=None):
    buy, _ = _run(seq, batches)
    return buy[VICTIM].get(BOOK, 0.0)


def test_dust_crowding_a_fill_leaves_its_captured_spread_where_the_market_put_it():
    clean = _market(300) + [_victim_fill()] + _market(300, start=300)
    crowded = _market(300) + _dust(W) + [_victim_fill()] + _dust(W) + _market(300, start=300)
    base = _victim_credit(clean)
    assert base > 3.0, "the fixture fill earns a real spread"
    assert abs(_victim_credit(crowded) - base) < 1e-9, (base, _victim_credit(crowded))


def test_the_crowded_mid_is_the_mean_of_the_nearest_prints_that_clear_the_floor():
    left, right = _market(40), _market(40, start=40)
    seq = left + _dust(2 * W) + [_victim_fill()] + _dust(3 * W) + right
    sizes = [float(t["q"]) for t in seq]
    bgs = [t["Ma"] < 0 or t["Ta"] < 0 for t in seq]
    floors = running_floors(sizes, bgs, MIN_SIZE)
    i = len(left) + 2 * W
    expected = (sum(t["p"] for t in left[-W:]) + seq[i]["p"] + sum(t["p"] for t in right[:W])) / (2 * W + 1)
    assert abs(centered_mid_floored([t["p"] for t in seq], sizes, floors, W, bgs)[i] - expected) < 1e-9


def test_without_dust_the_window_is_the_plain_one():
    seq = _market(120)
    sizes = [float(t["q"]) for t in seq]
    floors = running_floors(sizes, [True] * len(seq), MIN_SIZE)
    mids = centered_mid_floored([t["p"] for t in seq], sizes, floors, W, [True] * len(seq))
    for i in (0, 7, 15, 60, 104, 119):
        lo, hi = max(0, i - W), min(len(seq), i + W + 1)
        assert abs(mids[i] - sum(t["p"] for t in seq[lo:hi]) / (hi - lo)) < 1e-12


def test_the_live_path_agrees_with_the_batch_path_under_crowding():
    seq = (_market(200) + _dust(2 * W) + [_victim_fill()] + _dust(W) + _market(40, start=200)
           + _dust(3 * W) + [_victim_fill()] + _dust(4 * W) + _market(200, start=240))
    whole = _victim_credit(seq)
    assert whole > 6.0
    for b in (1, 4, 37):
        assert abs(_victim_credit(seq, batches=b) - whole) < 1e-9, (b, whole, _victim_credit(seq, batches=b))


def test_past_the_reach_a_fill_with_no_qualifying_neighbour_books_nothing():
    seq = _market(300) + _dust(REACH + 5) + [_victim_fill()] + _dust(REACH + 5) + _market(300, start=300)
    assert _victim_credit(seq) == 0.0
    assert _victim_credit(seq, batches=3) == 0.0


def test_the_live_window_stays_bounded_by_the_reach():
    seq = []
    for k in range(6):
        seq += _market(50, start=50 * k) + _dust(2 * W) + [_victim_fill()] + _dust(2 * W)
    buy, sell = defaultdict(dict), defaultdict(dict)
    state = {}
    for k in range(0, len(seq), 5):
        accumulate_book_capture(buy, sell, BOOK, seq[k:k + 5], W, ts=k, mid_state=state, min_size=MIN_SIZE)
        assert len(state[BOOK]["prices"]) <= 2 * REACH + 1
