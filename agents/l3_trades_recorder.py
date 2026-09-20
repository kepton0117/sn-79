# SPDX-FileCopyrightText: 2026
# SPDX-License-Identifier: MIT
"""Append full-book L3 prints (`book.events` y=='t') for validator-matched de-beta.

Miner `trades.csv` is this uid's ET notices only. Validator M-α / making walk every
book print. This recorder writes the public tape the axon already receives.
"""

from __future__ import annotations

import csv
import json
import logging
import queue
import threading
from pathlib import Path
from typing import Any, Iterable

_probe_done = False

try:
    from taos.im.utils import duration_from_timestamp
except ImportError:
    duration_from_timestamp = None  # type: ignore

log = logging.getLogger(__name__)

L3_FILENAME = "l3_trades.csv"
MARKS_FILENAME = "l3_marks.csv"

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
MARKS_HEADER = ["timestamp", "bookId", "tradeId", "price"]

_writers_lock = threading.Lock()
_writers: dict[str, "_PathWriter"] = {}
_seen: dict[str, set[tuple[int, int]]] = {}
_SEEN_CAP = 250_000


class _PathWriter:
    def __init__(self, path: Path, header: list[str] | None = None) -> None:
        self.path = path
        self._header = list(header or CSV_HEADER)
        self._q: queue.SimpleQueue[list | None] = queue.SimpleQueue()
        self._header_done = path.is_file() and path.stat().st_size > 0
        self._thread = threading.Thread(
            target=self._run,
            name=f"l3-trades-{path.name}",
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
                        writer.writerow(self._header)
                        self._header_done = True
                    writer.writerow(row)
            except OSError as exc:
                log.debug("l3_trades.csv append skipped %s: %s", self.path, exc)


def _writer_for(path: Path, header: list[str] | None = None) -> _PathWriter:
    key = str(path.resolve())
    with _writers_lock:
        writer = _writers.get(key)
        if writer is None:
            writer = _PathWriter(path, header=header)
            _writers[key] = writer
        return writer


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            val = getattr(obj, name)
            if val is not None:
                return val
    return default


def _as_int(val: Any) -> int | None:
    try:
        if val is None or val == "":
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def _as_float(val: Any) -> float | None:
    try:
        if val is None or val == "":
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _is_trade(ev: Any) -> bool:
    y = _g(ev, "y", "type")
    return y in ("t", "T") or str(y).lower() == "t"


def _iter_book_events(book: Any) -> Iterable[Any]:
    # Prefer LazyBook._raw['e'] so we never trigger TradeInfo.model_validate
    # (a malformed print would abort the whole dump).
    raw = getattr(book, "_raw", None)
    if isinstance(raw, dict):
        events = raw.get("e")
        if events:
            return events
    events = _g(book, "e", default=None)
    if events is None and not isinstance(book, dict):
        events = getattr(book, "events", None)
    if events is None:
        return []
    try:
        return list(events)
    except TypeError:
        return []


def extract_l3_trades(state: Any) -> list[list]:
    """Public book prints from this state update. Empty if books were omitted."""
    if duration_from_timestamp is None:
        return []
    books = _g(state, "books", default=None) or {}
    try:
        items = books.items()
    except Exception:
        return []
    rows: list[list] = []
    for book_id, book in items:
        bid = _as_int(_g(book, "id", "i", default=book_id) if book is not None else book_id)
        if bid is None:
            try:
                bid = int(book_id)
            except (TypeError, ValueError):
                continue
        for ev in _iter_book_events(book):
            if not _is_trade(ev):
                continue
            tid = _as_int(_g(ev, "i", "id", "tradeId"))
            px = _as_float(_g(ev, "p", "price"))
            qty = _as_float(_g(ev, "q", "quantity"))
            if tid is None or px is None or qty is None or qty <= 0:
                continue
            ts_raw = _g(ev, "t", "timestamp")
            try:
                ts = duration_from_timestamp(int(ts_raw or 0))
            except (TypeError, ValueError):
                continue
            side = _as_int(_g(ev, "s", "side"))
            if side is None:
                side = 0
            rows.append(
                [
                    ts,
                    bid,
                    tid,
                    "",
                    _g(ev, "Ta", "taker_agent_id", "takerAgentId", default=""),
                    _g(ev, "Ti", "taker_id", "takerOrderId", default=""),
                    _g(ev, "Tf", "taker_fee", "takerFee", default=""),
                    _g(ev, "Ma", "maker_agent_id", "makerAgentId", default=""),
                    _g(ev, "Mi", "maker_id", "makerOrderId", default=""),
                    _g(ev, "Mf", "maker_fee", "makerFee", default=""),
                    side,
                    px,
                    qty,
                ]
            )
    return rows


def _maybe_probe(agent: Any, state: Any, path: Path, n_rows: int) -> None:
    global _probe_done
    books = _g(state, "books", default=None) or {}
    try:
        n_books_now = len(books)
    except Exception:
        n_books_now = 0
    if _probe_done or n_books_now <= 0:
        return
    _probe_done = True
    books = _g(state, "books", default=None) or {}
    n_books = 0
    n_with_e = 0
    n_ev = 0
    n_t = 0
    sample = None
    try:
        items = list(books.items())[:8]
        n_books = len(books)
        for book_id, book in items:
            evs = list(_iter_book_events(book))
            n_ev += len(evs)
            ts = [e for e in evs if _is_trade(e)]
            n_t += len(ts)
            if evs:
                n_with_e += 1
            if sample is None and evs:
                e0 = evs[0]
                sample = {
                    "book": str(book_id),
                    "type": type(book).__name__,
                    "ev0_type": type(e0).__name__,
                    "ev0": {k: _g(e0, k) for k in ("y", "type", "i", "p", "q", "s", "Ma", "Ta")},
                    "has_raw": hasattr(book, "_raw"),
                    "raw_e_len": len((getattr(book, "_raw", {}) or {}).get("e") or [])
                    if hasattr(book, "_raw")
                    else None,
                }
    except Exception as exc:
        sample = {"error": str(exc)}
    try:
        probe = path.parent / "l3_probe.json"
        probe.write_text(
            json.dumps(
                {
                    "l3_path": str(path),
                    "n_rows_this_tick": n_rows,
                    "n_books": n_books,
                    "n_books_sampled_with_e": n_with_e,
                    "n_events_sampled": n_ev,
                    "n_trades_sampled": n_t,
                    "sample": sample,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _l3_paths(agent: Any, state: Any) -> list[Path]:
    """Session dir BMB already reads, plus a stable uid-level copy."""
    paths: list[Path] = []
    try:
        sim_dir = Path(agent.simulation_output_dir(state))
        if "unidentified-" not in sim_dir.name:
            paths.append(sim_dir / L3_FILENAME)
    except Exception:
        pass
    out = Path(getattr(agent, "output_dir", "") or "")
    if out.is_dir():
        existing = sorted(out.glob("*/*/trades.csv"), key=lambda p: p.stat().st_mtime)
        if existing:
            paths.append(existing[-1].parent / L3_FILENAME)
        paths.append(out / L3_FILENAME)
    uid = getattr(agent, "uid", None)
    if uid is not None:
        live = Path(__file__).resolve().parent / "data" / str(uid) / L3_FILENAME
        paths.append(live)
    # unique preserve order
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        key = str(p.resolve()) if p.parent.exists() or True else str(p)
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def record_l3_from_state(agent: Any, state: Any) -> int:
    """Queue new L3 prints for this tick. Returns the number enqueued."""
    paths = _l3_paths(agent, state)
    if not paths:
        return 0
    path = paths[0]
    rows = extract_l3_trades(state)
    _maybe_probe(agent, state, path, len(rows))
    if not rows:
        return 0
    with _writers_lock:
        seen = _seen.setdefault("global", set())
    writers = [_writer_for(p) for p in paths]
    mark_writers = [
        _writer_for(p.with_name(MARKS_FILENAME), header=MARKS_HEADER) for p in paths
    ]
    n = 0
    for row in rows:
        try:
            pair = (int(row[1]), int(row[2]))
        except (TypeError, ValueError):
            continue
        if pair in seen:
            continue
        seen.add(pair)
        if len(seen) > _SEEN_CAP:
            extra = list(seen)[: _SEEN_CAP // 2]
            for old in extra:
                seen.discard(old)
        mark_row = [row[0], row[1], row[2], row[11]]
        for writer in writers:
            writer.enqueue(row)
        for writer in mark_writers:
            writer.enqueue(mark_row)
        n += 1
    return n
