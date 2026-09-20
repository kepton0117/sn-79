"""A stuck re-quote loop must announce itself instead of running silently."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import TopAgent as top


class _Agent:
    _note_requote = top.TopAgent._note_requote


def _warnings(monkeypatch):
    seen = []
    monkeypatch.setattr(top.bt.logging, "warning", lambda msg: seen.append(msg))
    return seen


def test_repeated_identical_requote_warns(monkeypatch):
    seen = _warnings(monkeypatch)
    agent = _Agent()
    for _ in range(5):
        agent._note_requote(45, 0, 437.20, 437.19)
    assert len(seen) == 1
    assert "book 45 BUY" in seen[0]
    assert "5x in a row" in seen[0]


def test_quiet_below_the_threshold(monkeypatch):
    seen = _warnings(monkeypatch)
    agent = _Agent()
    for _ in range(4):
        agent._note_requote(45, 0, 437.20, 437.19)
    assert seen == []


def test_a_moving_price_is_normal_requoting(monkeypatch):
    """Chasing a drifting mid is expected and must not warn."""
    seen = _warnings(monkeypatch)
    agent = _Agent()
    for i in range(20):
        agent._note_requote(45, 0, 437.20 + i * 0.01, 437.19 + i * 0.01)
    assert seen == []


def test_streak_is_tracked_per_book_and_side(monkeypatch):
    seen = _warnings(monkeypatch)
    agent = _Agent()
    for _ in range(4):
        agent._note_requote(45, 0, 437.20, 437.19)
        agent._note_requote(45, 1, 438.20, 438.19)
        agent._note_requote(17, 1, 298.88, 298.87)
    assert seen == []
    agent._note_requote(45, 1, 438.20, 438.19)
    assert len(seen) == 1
    assert "book 45 SELL" in seen[0]
