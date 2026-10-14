"""A slow or failing state save at the run's end does not skip the run-end update.

SimulationEngine.on_end() saves the state and then calls `update_repo(end=True)`. A large state saved on a
host short of memory can take longer than the save's bound; if the TimeoutError that follows skipped
update_repo, no fresh simulation would start, and at a boundary the validator would stay on an ended run.
"""
import asyncio
import os
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import taos.im.validator.engines.simulation as sim  # noqa: E402


def _engine(save):
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    calls = []
    v = SimpleNamespace()
    v.simulation = SimpleNamespace(logDir="/r/logs/20261009_175951", simulation_id="20261009_1759", book_count=2)
    v.effective_max_uids = 4
    v.main_loop = loop
    v._save_state_sync = save
    v.update_repo = lambda end=False: calls.append(("update_repo", end))
    e = sim.SimulationEngine.__new__(sim.SimulationEngine)
    e.validator = v
    e._last_start_ts = 1
    e._notify_seed_log_dir_change = lambda d: None
    return e, v, calls, loop


def test_a_save_slower_than_its_bound_still_reaches_the_update(monkeypatch):
    monkeypatch.setattr(sim, "_END_SAVE_TIMEOUT_S", 0.2)
    done = []

    async def slow_save():
        await asyncio.sleep(1.0)
        done.append(True)

    e, v, calls, loop = _engine(slow_save)
    t0 = time.monotonic()
    e.on_end()
    assert calls == [("update_repo", True)], "the run-end update was skipped after a slow save"
    assert time.monotonic() - t0 < 0.9, "on_end waited for the whole save instead of its bound"
    time.sleep(1.2)
    assert done == [True], "the save was cancelled rather than left to complete in the background"
    loop.call_soon_threadsafe(loop.stop)


def test_a_failing_save_still_reaches_the_update(monkeypatch):
    monkeypatch.setattr(sim, "_END_SAVE_TIMEOUT_S", 1.0)

    async def bad_save():
        raise OSError("disk full")

    e, v, calls, loop = _engine(bad_save)
    e.on_end()
    assert calls == [("update_repo", True)]
    loop.call_soon_threadsafe(loop.stop)


def test_a_quick_save_is_waited_for_before_the_update(monkeypatch):
    monkeypatch.setattr(sim, "_END_SAVE_TIMEOUT_S", 2.0)
    order = []

    async def quick_save():
        await asyncio.sleep(0.05)
        order.append("saved")

    e, v, calls, loop = _engine(quick_save)
    v.update_repo = lambda end=False: order.append("update_repo")
    e.on_end()
    assert order == ["saved", "update_repo"], "a save inside its bound must finish before the update"
    assert v.simulation.logDir is None and v.simulation.simulation_id is None
    loop.call_soon_threadsafe(loop.stop)
