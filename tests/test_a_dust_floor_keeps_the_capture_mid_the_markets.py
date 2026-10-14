# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The dust floor on the capture mid. A print takes part in the capture mid only if its size clears a tenth of the
book's typical background-sided print: the median size of the trailing 256 background-sided prints at or above the
book's minimum order size, fixed per print on arrival. Everything else about the mid is unchanged: unweighted over
the prints that take part, the same 2W+1 index window, batch and live paths agreeing. A print below the floor shapes
nothing, a print of real size still shapes the mid, and a print below the minimum order size is a residual and does
not vote for the floor."""
import copy
import pickle
from collections import defaultdict

from taos.im.validator.debeta import (
    CAPTURE_REACH_WINDOWS, DUST_FLOOR_FRAC, accumulate_book_capture, centered_mid, centered_mid_floored,
    flush_capture_state, running_floors,
)

W = 15
BOOK = 7
MAKER = 1
BG = -2
BG2 = -3


def _mk(price, q=10.0, s=0, ma=MAKER, ta=BG):
    return {"p": price, "q": q, "s": s, "Ma": ma, "Ta": ta}


def _stream(n=90):
    """The background market prints around 100 and a maker is filled by it 0.4 inside on alternating sides, so the
    maker earns a real spread against a mid the background defines."""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append(_mk(100.0 + 0.01 * ((i * 7) % 5 - 2), q=10.0 + (i % 4), ma=BG2, ta=BG))
        elif (i // 2) % 2 == 0:
            out.append(_mk(99.6, q=10.0 + (i % 4), s=1))   # s == 1: the maker buys
        else:
            out.append(_mk(100.4, q=10.0 + (i % 4), s=0))  # s == 0: the maker sells
    return out


def _with_dust(trades, at, price=105.0, q=0.001, ma=7, ta=8):
    out = list(trades)
    for k, i in enumerate(sorted(at)):
        out.insert(i + k, _mk(price, q=q, s=k % 2, ma=ma, ta=ta))
    return out


MIN_SIZE = 0.25   # the book's minimum order size: a print below it is a residual and cannot vote the reference down


def _run(batches, state=None):
    buy, sell = defaultdict(dict), defaultdict(dict)
    for k, batch in enumerate(batches):
        accumulate_book_capture(buy, sell, BOOK, batch, W, ts=k * 1_000_000_000, mid_state=state, min_size=MIN_SIZE)
    if state is not None:
        flush_capture_state(state, buy, sell, W, ts=len(batches) * 1_000_000_000, force=True)
    return buy, sell


def _credit(buy, sell, uid=MAKER):
    return buy[uid].get(BOOK, 0.0), sell[uid].get(BOOK, 0.0)


def _floored(prices, trades):
    sizes = [float(t["q"]) for t in trades]
    bgs = [t["Ma"] < 0 or t["Ta"] < 0 for t in trades]
    return centered_mid_floored(prices, sizes, running_floors(sizes, bgs, MIN_SIZE), W, bgs)


def test_dust_between_miners_moves_no_capture_on_the_batch_path():
    clean = _stream()
    dusty = _with_dust(clean, at=(30, 33, 36, 50, 53, 56))
    b0, s0 = _credit(*_run([clean]))
    b1, s1 = _credit(*_run([dusty]))
    assert b0 > 50 and s0 > 50, "the fixture earns a real spread"
    assert abs(b1 - b0) <= 0.03 * b0 and abs(s1 - s0) <= 0.03 * s0, (b0, b1, s0, s1)
    # the control: without the floor the same prints move the mid at the real prints far more
    prices = [float(t["p"]) for t in dusty]
    real = [p < 104.0 for p in prices]
    plain = centered_mid(prices, W)
    floored = _floored(prices, dusty)
    clean_mid = centered_mid([float(t["p"]) for t in clean], W)
    k = iter(clean_mid)
    moved = sum(abs(m - next(k)) for m, r in zip(plain, real) if r)
    k = iter(clean_mid)
    kept = sum(abs(m - next(k)) for m, r in zip(floored, real) if r)
    assert moved > 10 * kept, (moved, kept)


def test_the_live_path_agrees_with_the_batch_path_with_dust_present():
    dusty = _with_dust(_stream(), at=(30, 33, 36, 50, 53, 56))
    whole = _run([dusty])
    ones = _run([[t] for t in dusty], state={})
    fours = _run([dusty[i:i + 4] for i in range(0, len(dusty), 4)], state={})
    for a, b in ((whole, ones), (whole, fours)):
        assert abs(a[0][MAKER][BOOK] - b[0][MAKER][BOOK]) < 1e-6 and abs(a[1][MAKER][BOOK] - b[1][MAKER][BOOK]) < 1e-6


def test_a_real_size_print_between_miners_still_shapes_the_mid():
    """The floor is about size, not about who traded: a 10-lot print between two miners at 105 moves the mid (unless
    the market-vote dial is on: test_the_market_vote_dial_closes_minimum_size_shaping)."""
    clean = _stream()
    big = _with_dust(clean, at=(40,), q=10.0)
    b0, s0 = _credit(*_run([clean]))
    b1, s1 = _credit(*_run([big]))
    prices = [float(t["p"]) for t in big]
    assert abs(centered_mid(prices, W)[40] - _floored(prices, big)[40]) < 1e-9
    # a mid pushed up pays the maker's buys more and its sells less, so one side is the measure, not the total
    assert b1 - b0 > 0.05 * b0 and s0 - s1 > 0.05 * s0, "a real-size print must still move capture"


def test_a_flood_of_dust_cannot_lower_the_floor():
    """Only prints at or above the book's minimum order size vote the reference, and a 0.001-lot print is a residual
    below it, so forty of them, between miners or against the market, leave the floor where the real prints put it."""
    clean = _stream(60)
    flood = _with_dust(clean, at=tuple(range(20, 40)), q=0.001)                      # 20 between miners
    flood = _with_dust(flood, at=tuple(range(20, 60)), q=0.001, ta=BG)                # 20 more against the market, tiny
    b0, s0 = _credit(*_run([clean]))
    b1, s1 = _credit(*_run([flood]))
    assert abs(b1 - b0) <= 0.03 * b0 and abs(s1 - s0) <= 0.03 * s0, (b0, b1, s0, s1)


def test_a_stretch_of_dust_only_books_nothing():
    """A fill whose whole reach holds no other print that clears the floor has no reference and books no capture
    (within the reach the window steps past dust to the nearest real prints: the crowded-window test)."""
    victim = 9
    side = CAPTURE_REACH_WINDOWS * W + 5
    dust = [_mk(105.0 + 0.01 * (i % 3), q=0.001, s=i % 2, ma=7, ta=8) for i in range(2 * side)]
    seq = _stream(6) + dust[:side] + [_mk(105.0, q=10.0, s=1, ma=victim, ta=BG)] + dust[side:]
    buy, sell = _run([seq])
    assert buy[victim].get(BOOK, 0.0) == 0.0 and sell[victim].get(BOOK, 0.0) == 0.0, "no reference, no capture"
    live_buy, live_sell = _run([[t] for t in seq], state={})
    assert live_buy[victim].get(BOOK, 0.0) == 0.0 and live_sell[victim].get(BOOK, 0.0) == 0.0


def test_a_window_persisted_before_the_floor_continues_and_its_carried_prints_are_not_dust():
    """A validator restored from a 0.6.3 state carries live windows without sizes, floors or a reference. The
    carried prints are padded as never dust (size inf, floor 0), because their sizes were never kept, the reference
    fills from the prints that follow, and the window keeps earning close to what a window started afresh at the same
    prints does. The one bounded transient of the upgrade: a fill carried without its own floor reads its window at
    floor 0, so dust printed inside its window after the upgrade still votes for it, as 0.6.3 let it, until the
    carried fills leave (at most 2W+1 prints per book). The transient is about a tenth of the credit with dust
    three prints after the upgrade, and zero once the carried fills are gone."""
    dusty = _with_dust(_stream(), at=(50, 53, 56))
    half = len(dusty) // 2
    old = {}
    buy, sell = defaultdict(dict), defaultdict(dict)
    accumulate_book_capture(buy, sell, BOOK, dusty[:half], W, ts=0, mid_state=old, min_size=MIN_SIZE)
    for k in ("sizes", "floors", "ref_sorted", "ref_fifo", "min_size"):   # what 0.6.3 never wrote
        old[BOOK].pop(k, None)
    accumulate_book_capture(buy, sell, BOOK, dusty[half:], W, ts=1_000_000_000, mid_state=old, min_size=MIN_SIZE)
    flush_capture_state(old, buy, sell, W, ts=2_000_000_000, force=True)
    st = old[BOOK]
    assert len(st["sizes"]) == len(st["floors"]) == len(st["prices"]), "the lists stay parallel after the upgrade"
    assert buy[MAKER][BOOK] > 0 and sell[MAKER][BOOK] > 0
    # the same stream started afresh: the transient above is the whole difference, bounded, and vanishes with the
    # carried fills; a clean stream after the upgrade earns the same on both paths
    fresh_b, fresh_s = _credit(*_run([dusty], state={}))
    assert abs(buy[MAKER][BOOK] - fresh_b) <= 0.15 * fresh_b and abs(sell[MAKER][BOOK] - fresh_s) <= 0.15 * fresh_s, (buy[MAKER][BOOK], fresh_b)
    clean = _stream()
    old2, b2, s2 = {}, defaultdict(dict), defaultdict(dict)
    accumulate_book_capture(b2, s2, BOOK, clean[:half], W, ts=0, mid_state=old2, min_size=MIN_SIZE)
    for k in ("sizes", "floors", "ref_sorted", "ref_fifo", "min_size"):
        old2[BOOK].pop(k, None)
    accumulate_book_capture(b2, s2, BOOK, clean[half:], W, ts=1_000_000_000, mid_state=old2, min_size=MIN_SIZE)
    flush_capture_state(old2, b2, s2, W, ts=2_000_000_000, force=True)
    cb, cs = _credit(*_run([clean], state={}))
    assert abs(b2[MAKER][BOOK] - cb) < 1e-6 and abs(s2[MAKER][BOOK] - cs) < 1e-6, "no dust, no transient: identical"


def test_the_window_state_round_trips_with_the_floor_inputs():
    dusty = _with_dust(_stream(), at=(30, 33, 36, 50, 53, 56))
    half = len(dusty) // 2
    state = {}
    buy, sell = defaultdict(dict), defaultdict(dict)
    accumulate_book_capture(buy, sell, BOOK, dusty[:half], W, ts=0, mid_state=state)
    restored = pickle.loads(pickle.dumps(copy.deepcopy(state)))
    buy2, sell2 = copy.deepcopy(buy), copy.deepcopy(sell)
    accumulate_book_capture(buy, sell, BOOK, dusty[half:], W, ts=1_000_000_000, mid_state=state)
    accumulate_book_capture(buy2, sell2, BOOK, dusty[half:], W, ts=1_000_000_000, mid_state=restored)
    flush_capture_state(state, buy, sell, W, ts=2_000_000_000, force=True)
    flush_capture_state(restored, buy2, sell2, W, ts=2_000_000_000, force=True)
    assert buy[MAKER][BOOK] == buy2[MAKER][BOOK] and sell[MAKER][BOOK] == sell2[MAKER][BOOK]
    assert set(state[BOOK]) >= {"prices", "sizes", "floors", "ref_sorted", "ref_fifo"}, "the window carries sizes, each print's floor and the reference"
    assert all(isinstance(state[BOOK][k], list) for k in ("prices", "sizes", "floors", "ref_sorted", "ref_fifo")), "plain lists: the state snapshots and persists as data"
    assert DUST_FLOOR_FRAC == 0.1
