"""TopAgent stop→open restore: a wide, thin touch, and alpha under baseline when known."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from TopAgent import SIM_MINUTE_NS, TopAgent


class _Book:
    def __init__(self, bid_px, bid_qty, ask_px, ask_qty):
        self._raw = {
            "b": [{"p": bid_px, "q": bid_qty}],
            "a": [{"p": ask_px, "q": ask_qty}],
        }


class _State:
    def __init__(self, books):
        self.books = books


def _agent(tmp_path, *, baseline=273.5, alphas=None, written_at=None, stop=(23,)):
    agent = TopAgent.__new__(TopAgent)
    agent.uid = 120
    agent._open_fills_epoch = {}
    agent._open_fill_trade_keys = set()
    agent._restore_block_logged = {}
    agent._fifo_ctx_state = None
    agent.stop_books = set(stop)
    agent.open_books = {}
    agent.flatten_books = set()
    agent.market_close_books = set()
    agent._open_book_side_fills = {}
    agent._open_skip_logged = set()
    agent._open_started_ts = {}
    agent._restore_last_ts = {}
    agent._last_state_ts = 10 * SIM_MINUTE_NS
    agent.live_config = False
    agent.live_config_path = tmp_path / "topagent.120.json"

    agent.restore_stop_books = True
    agent.restore_bracket_ticks = 50
    agent.restore_quantity = 100.0
    agent.restore_min_spread = 0.5
    agent.restore_max_touch_qty = 20.0
    agent.restore_cooldown_minutes = 30.0
    agent.alpha_baseline_max_age_sec = 900.0

    path = tmp_path / "debeta_baseline.120.json"
    path.write_text(
        json.dumps(
            {
                "uid": 120,
                "baseline": baseline,
                "written_at": written_at if written_at is not None else time.time(),
                "alphas": alphas if alphas is not None else {"23": 212.0},
            }
        )
    )
    agent.alpha_baseline_path = path
    agent.alpha_baseline_public_path = tmp_path / "debeta_mean_alpha.json"
    agent._alpha_baseline_cache = {}
    agent._alpha_baseline_mtime = {}
    return agent


def _write_public(tmp_path, *, baseline=273.5, alphas_by_uid=None, written_at=None):
    """The field-wide file BMB publishes for every uid, not just configured ones."""
    path = tmp_path / "debeta_mean_alpha.json"
    path.write_text(
        json.dumps(
            {
                "written_at": written_at if written_at is not None else time.time(),
                "baseline": baseline,
                "alphas_by_uid": alphas_by_uid or {"120": {"23": 212.0}},
            }
        )
    )
    return path


def test_restores_book_under_baseline_on_wide_thin_touch(tmp_path):
    agent = _agent(tmp_path)
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books
    spec = agent.open_books[23]
    assert spec.bracket_ticks == 50
    assert spec.max_quantity == 100.0
    assert spec.open_price == 0.0


def test_scored_book_is_left_alone(tmp_path):
    agent = _agent(tmp_path, alphas={"23": 400.0})
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_tight_spread_blocks_restore(tmp_path):
    agent = _agent(tmp_path)
    state = _State({23: _Book(100.0, 5.0, 100.2, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_thick_touch_blocks_restore(tmp_path):
    agent = _agent(tmp_path)
    state = _State({23: _Book(100.0, 5.0, 100.8, 40.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_stale_baseline_falls_back_to_spread_and_touch(tmp_path):
    """BMB going quiet must not silently disable the rule."""
    agent = _agent(tmp_path, written_at=time.time() - 5_000.0)
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books


def test_missing_baseline_file_falls_back_to_spread_and_touch(tmp_path):
    """BMB only publishes for the uids it is configured for."""
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"

    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books


def test_book_without_published_alpha_falls_back_to_spread_and_touch(tmp_path):
    agent = _agent(tmp_path, alphas={"41": 10.0})
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books


def test_public_file_supplies_the_baseline_when_no_per_uid_file(tmp_path):
    """BMB names only some uids; the field file covers the rest."""
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"
    _write_public(tmp_path)

    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books


def test_public_file_alpha_gate_still_skips_a_scored_book(tmp_path):
    """The fallback must carry the alpha gate, not just the baseline number."""
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"
    _write_public(tmp_path, alphas_by_uid={"120": {"23": 400.0}})

    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_public_file_for_another_uid_leaves_us_on_touch_gates(tmp_path):
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"
    _write_public(tmp_path, alphas_by_uid={"999": {"23": 400.0}})

    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert 23 not in agent.stop_books


def test_stale_public_file_is_ignored(tmp_path):
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"
    _write_public(
        tmp_path,
        alphas_by_uid={"120": {"23": 400.0}},
        written_at=time.time() - 5_000.0,
    )

    # Stale means no opinion, so the scored book is no longer held back.
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1


def test_per_uid_file_wins_over_the_public_one(tmp_path):
    agent = _agent(tmp_path, alphas={"23": 400.0})
    _write_public(tmp_path, alphas_by_uid={"120": {"23": 1.0}})

    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_no_baseline_still_blocks_a_tight_spread(tmp_path):
    """The fallback drops the alpha gate, not the touch gates."""
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"

    state = _State({23: _Book(100.0, 5.0, 100.2, 4.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_no_baseline_still_blocks_a_thick_touch(tmp_path):
    agent = _agent(tmp_path)
    agent.alpha_baseline_path = tmp_path / "absent.json"

    state = _State({23: _Book(100.0, 5.0, 100.8, 40.0)})
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_one_book_per_tick_then_cooldown(tmp_path):
    agent = _agent(
        tmp_path,
        alphas={"23": 212.0, "41": 100.0},
        stop=(23, 41),
    )
    state = _State(
        {
            23: _Book(100.0, 5.0, 100.8, 4.0),
            41: _Book(200.0, 5.0, 200.9, 4.0),
        }
    )

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert agent.open_books.keys() == {23}

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1
    assert agent.open_books.keys() == {23, 41}

    # Both re-opened; a re-stopped book waits out the cooldown.
    agent.open_books.pop(23)
    agent.stop_books.add(23)
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0

    agent._last_state_ts += int(31 * SIM_MINUTE_NS)
    assert agent._maybe_restore_stop_books(None, 0.25, state) == 1


def test_flatten_book_is_never_restored(tmp_path):
    agent = _agent(tmp_path)
    agent.flatten_books = {23}
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def test_disabled_flag_is_a_no_op(tmp_path):
    agent = _agent(tmp_path)
    agent.restore_stop_books = False
    state = _State({23: _Book(100.0, 5.0, 100.8, 4.0)})

    assert agent._maybe_restore_stop_books(None, 0.25, state) == 0
    assert agent.stop_books == {23}


def _write_trades_csv(path, uid, book, rows):
    """rows: (trade_id, side, qty) with uid as the maker."""
    header = (
        "timestamp,bookId,tradeId,clientOrderId,takerAgentId,takerOrderId,"
        "takerFee,makerAgentId,makerOrderId,makerFee,side,price,quantity"
    )
    lines = [header]
    for trade_id, side, qty in rows:
        lines.append(
            f"04:00:00.0,{book},{trade_id},,999,1,0.0,{uid},2,0.0,{side},100.0,{qty}"
        )
    path.write_text("\n".join(lines) + "\n")


def test_restored_book_does_not_retire_on_its_previous_fills(tmp_path):
    """Regression: the CSV replay used to re-count the book's old life at once."""
    agent = _agent(tmp_path)
    sim_dir = tmp_path / "sim"
    sim_dir.mkdir()
    # 300 on each side from the book's first life — well over max_quantity=100.
    _write_trades_csv(
        sim_dir / "trades.csv",
        120,
        23,
        [(1, 1, 300.0), (2, 0, 300.0)],
    )
    agent._fifo_ctx_state = object()
    agent.simulation_output_dir = lambda _state: sim_dir

    assert agent._maybe_restore_stop_books(
        None, 0.25, _State({23: _Book(100.0, 5.0, 100.8, 4.0)})
    ) == 1
    assert agent._open_fills_epoch[23] == 2

    # Replaying the tape must not carry the old fills into the fresh cap.
    agent._seed_open_book_side_fills(23)
    fills = agent._open_side_fills(23)
    assert fills.buy == 0.0
    assert fills.sell == 0.0

    # A fill after the epoch still counts.
    assert agent._apply_open_book_fill(23, 7, True, 40.0) is True
    assert agent._open_side_fills(23).buy == 40.0
