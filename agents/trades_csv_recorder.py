# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Background trades.csv writer for TopAgent (keeps sync I/O out of handle)."""

from __future__ import annotations

import csv
import logging
import queue
import threading
from pathlib import Path
from typing import Any

try:
    from taos.im.utils import duration_from_timestamp
except ImportError:
    duration_from_timestamp = None  # type: ignore

log = logging.getLogger(__name__)

CSV_HEADER = [
    "timestamp",
    "bookId",
    "tradeId",
    "clientOrderId",
    "takerAgentId",
    "takerOrderId",
    "takerFee",
    "makerAgentId",
    "makerOrderId",
    "makerFee",
    "side",
    "price",
    "quantity",
]

_writers_lock = threading.Lock()
_writers: dict[str, "_PathWriter"] = {}


class _PathWriter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._q: queue.SimpleQueue[list | None] = queue.SimpleQueue()
        self._header_done = path.is_file() and path.stat().st_size > 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"trades-csv-{path.name}",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, row: list) -> None:
        self._q.put(row)

    def _run(self) -> None:
        while True:
            row = self._q.get()
            if row is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                write_header = not self._header_done
                with self.path.open("a", newline="") as f:
                    writer = csv.writer(f)
                    if write_header:
                        writer.writerow(CSV_HEADER)
                        self._header_done = True
                    writer.writerow(row)
            except OSError as exc:
                log.debug("trades.csv append skipped %s: %s", self.path, exc)


def _writer_for(path: Path) -> _PathWriter:
    key = str(path.resolve())
    with _writers_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = _PathWriter(path)
            _writers[key] = writer
        return writer


def _trade_row(event: Any) -> list | None:
    if duration_from_timestamp is None:
        return None
    try:
        ts = duration_from_timestamp(int(getattr(event, "timestamp", 0) or 0))
    except (TypeError, ValueError):
        return None
    return [
        ts,
        getattr(event, "bookId", ""),
        getattr(event, "tradeId", ""),
        getattr(event, "clientOrderId", ""),
        getattr(event, "takerAgentId", ""),
        getattr(event, "takerOrderId", ""),
        getattr(event, "takerFee", ""),
        getattr(event, "makerAgentId", ""),
        getattr(event, "makerOrderId", ""),
        getattr(event, "makerFee", ""),
        getattr(event, "side", ""),
        getattr(event, "price", ""),
        getattr(event, "quantity", ""),
    ]


def enqueue_trade_csv_row(agent: Any, event: Any, state: Any) -> bool:
    """Queue one trade row for background append; returns False when skipped."""
    if not bool(getattr(agent, "fifo_log_trades", True)):
        return False
    try:
        path = Path(agent.simulation_output_dir(state)) / "trades.csv"
    except Exception:
        return False
    row = _trade_row(event)
    if row is None:
        return False
    _writer_for(path).enqueue(row)
    return True
