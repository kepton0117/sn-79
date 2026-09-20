"""Background trades.csv writer."""

from __future__ import annotations

import csv
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from trades_csv_recorder import CSV_HEADER, enqueue_trade_csv_row


def test_enqueue_writes_csv_in_background(tmp_path):
    agent = SimpleNamespace(
        fifo_log_trades=True,
        simulation_output_dir=lambda _state: str(tmp_path),
    )
    state = SimpleNamespace()
    event = SimpleNamespace(
        timestamp=1_000_000_000,
        bookId=73,
        tradeId=42,
        clientOrderId=0,
        takerAgentId=49,
        takerOrderId=100,
        takerFee=0.1,
        makerAgentId=2,
        makerOrderId=99,
        makerFee=-0.05,
        side=1,
        price=357.5,
        quantity=0.25,
    )

    assert enqueue_trade_csv_row(agent, event, state) is True

    path = tmp_path / "trades.csv"
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if path.is_file() and path.stat().st_size > 0:
            break
        time.sleep(0.05)
    assert path.is_file()

    rows = list(csv.reader(path.open()))
    assert rows[0] == CSV_HEADER
    assert rows[1][1] == "73"
    assert rows[1][2] == "42"
    assert rows[1][11] == "357.5"
    assert rows[1][12] == "0.25"


def test_enqueue_respects_fifo_log_trades_off(tmp_path):
    agent = SimpleNamespace(
        fifo_log_trades=False,
        simulation_output_dir=lambda _state: str(tmp_path),
    )
    event = SimpleNamespace(timestamp=1, bookId=0, tradeId=1)
    assert enqueue_trade_csv_row(agent, event, SimpleNamespace()) is False
    assert not (tmp_path / "trades.csv").exists()
