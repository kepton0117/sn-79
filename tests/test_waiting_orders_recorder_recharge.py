"""waiting_orders_recorder recharge_times / recharge_used."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

import waiting_orders_recorder as wor


class _HelpStub:
    uid = 135
    recharge_times = 3
    live_config_path = Path("/tmp/helpagent.135.json")


def setup_function():
    wor._recharge_state_by_uid.clear()


def test_recharge_allow_and_note_per_book():
    agent = _HelpStub()
    assert wor.recharge_allow(agent, 23) is True
    wor.note_recharge(agent, 23)
    wor.note_recharge(agent, 23)
    wor.note_recharge(agent, 23)
    assert wor.recharge_allow(agent, 23) is False
    assert wor.recharge_allow(agent, 99) is True


def test_build_payload_includes_recharge_fields():
    agent = _HelpStub()
    wor.note_recharge(agent, 23)
    payload = wor.build_payload(agent, state={}, sent_orders=[], uid=135)
    assert payload["recharge_times"] == 3
    assert payload["recharge_used"] == {"23": 1}


def test_hydrate_from_payload():
    data = {"recharge_times": 3, "recharge_used": {"23": 2}}
    wor.hydrate_recharge_from_payload(data, 135)
    agent = _HelpStub()
    assert wor.recharge_allow(agent, 23) is True
    wor.note_recharge(agent, 23)
    assert wor.recharge_allow(agent, 23) is False


def test_topagent_omits_recharge_fields():
    class _TopStub:
        uid = 120

    payload = wor.build_payload(_TopStub(), state={}, sent_orders=[], uid=120)
    assert "recharge_times" not in payload
    assert "recharge_used" not in payload
