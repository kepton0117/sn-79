"""TopAgent book recruiting: reach target_book_count using thin-touch books."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from TopAgent import OpenBookSpec, TopAgent


class _Book:
    def __init__(self, bid_px, bid_qty, ask_px, ask_qty):
        self._raw = {
            "b": [{"p": bid_px, "q": bid_qty}],
            "a": [{"p": ask_px, "q": ask_qty}],
        }


class _State:
    def __init__(self, books):
        self.books = books


def _agent(*, open_books=(), stop=(), enabled=True):
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 120
    agent.open_books = {
        b: OpenBookSpec(book=b, open_price=0.0, max_quantity=100.0)
        for b in open_books
    }
    agent.stop_books = set(stop)
    agent.flatten_books = set()
    agent.market_close_books = set()
    agent.general_books = {}
    agent._open_book_side_fills = {}
    agent._open_fill_trade_keys = set()
    agent._open_fills_epoch = {}
    agent._open_skip_logged = set()
    agent._open_started_ts = {}
    agent._fifo_ctx_state = None
    agent._last_state_ts = 1_000
    agent.live_config = False
    agent.live_config_path = Path("/tmp/does-not-exist.json")

    agent.acquire_open_books = enabled
    agent.target_book_count = 4
    agent.acquire_bracket_ticks = 100
    agent.acquire_quantity = 100.0
    agent.acquire_max_touch_qty = 10.0
    return agent


def _thin(n=5.0):
    return _Book(100.0, n, 100.02, n)


def _thick():
    return _Book(100.0, 50.0, 100.02, 50.0)


def test_acquires_a_thin_book_with_the_configured_bracket():
    agent = _agent()
    assert agent._maybe_acquire_open_books(_State({7: _thin()})) == 1

    spec = agent.open_books[7]
    assert spec.bracket_ticks == 100
    assert spec.max_quantity == 100.0
    assert spec.open_price == 0.0


def test_thick_touch_is_not_acquired():
    agent = _agent()
    assert agent._maybe_acquire_open_books(_State({7: _thick()})) == 0
    assert agent.open_books == {}


def test_stop_books_count_toward_the_target():
    """A retired book keeps its alpha, so it still fills one of the four slots."""
    agent = _agent(stop=(1, 2, 3, 4))
    assert agent._maybe_acquire_open_books(_State({7: _thin()})) == 0
    assert agent.open_books == {}


def test_open_and_stop_are_counted_together():
    agent = _agent(open_books=(1, 2), stop=(3,))
    # 3 engaged, so exactly one more slot.
    assert agent._maybe_acquire_open_books(_State({7: _thin(), 8: _thin()})) == 1
    assert agent._maybe_acquire_open_books(_State({7: _thin(), 8: _thin()})) == 0
    assert len(agent.open_books) == 3


def test_a_book_is_never_claimed_twice():
    agent = _agent()
    state = _State({7: _thin()})
    assert agent._maybe_acquire_open_books(state) == 1
    assert agent._maybe_acquire_open_books(state) == 0
    assert list(agent.open_books) == [7]


def test_thinnest_book_is_preferred():
    agent = _agent()
    state = _State({7: _thin(9.0), 8: _thin(2.0), 9: _thin(6.0)})
    assert agent._maybe_acquire_open_books(state) == 1
    assert list(agent.open_books) == [8]


def test_one_book_per_tick_until_target():
    agent = _agent()
    state = _State({n: _thin(float(n)) for n in (1, 2, 3, 4, 5, 6)})
    for expected in (1, 2, 3, 4):
        assert agent._maybe_acquire_open_books(state) == 1
        assert len(agent.open_books) == expected
    assert agent._maybe_acquire_open_books(state) == 0
    assert len(agent.open_books) == 4


def test_flatten_and_market_close_books_are_not_candidates():
    agent = _agent()
    agent.flatten_books = {7}
    agent.market_close_books = {8}
    assert agent._maybe_acquire_open_books(_State({7: _thin(), 8: _thin()})) == 0
    assert agent.open_books == {}


def test_disabled_by_default_flag_is_a_no_op():
    agent = _agent(enabled=False)
    assert agent._maybe_acquire_open_books(_State({7: _thin()})) == 0
    assert agent.open_books == {}
