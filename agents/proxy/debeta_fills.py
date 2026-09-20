# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Collect sim trade fills in the local proxy and replay de-beta making + skill offline."""
from __future__ import annotations

import atexit
import json
from collections import defaultdict
from pathlib import Path

from taos.im.validator.debeta import (
    accumulate_book_capture,
    accumulate_book_mtm,
    book_alphas_by_book,
    debeta_scores,
    median_abs_floor,
    prune_hist_1level,
    prune_hist_2level,
)
from taos.im.validator.trade import CAPTURE_W

DEFAULT_MAKING_WINDOW_NS = 86400_000_000_000   # 24 sim-hours
DEFAULT_SKILL_WINDOW_NS = 10800_000_000_000     # 3 sim-hours


def _trade_from_event(ev, book_id: int, batch_ts: int) -> dict | None:
    if isinstance(ev, dict):
        if ev.get("y") != "t":
            return None
        return {
            "book": book_id,
            "p": float(ev["p"]),
            "q": float(ev["q"]),
            "s": int(ev["s"]),
            "Ma": ev.get("Ma", ev.get("makerAgentId", -1)),
            "Ta": ev.get("Ta", ev.get("takerAgentId", -1)),
            "ts": batch_ts,
            "i": int(ev.get("i", ev.get("tradeId", 0))),
        }
    if getattr(ev, "y", None) != "t" and getattr(ev, "type", None) != "t":
        return None
    return {
        "book": book_id,
        "p": float(ev.p),
        "q": float(ev.q),
        "s": int(ev.s),
        "Ma": ev.Ma if ev.Ma is not None else -1,
        "Ta": ev.Ta if ev.Ta is not None else -1,
        "ts": batch_ts,
        "i": int(ev.i),
    }


def extract_trades_from_state(state) -> list[dict]:
    """Pull new book 't' events from one state update (same source as validator trade.py)."""
    books = getattr(state, "books", None)
    if not books:
        return []
    batch_ts = int(getattr(state, "timestamp", 0) or 0)
    out = []
    for book_id, book in books.items():
        bid = int(book_id)
        if isinstance(book, dict):
            events = book.get("e") or []
        else:
            events = getattr(book, "events", None) or getattr(book, "e", None) or []
        for ev in events:
            row = _trade_from_event(ev, bid, batch_ts)
            if row is not None:
                out.append(row)
    return out


def _group_batches(trades: list[dict]) -> dict[tuple[int, int], list]:
    batches: dict[tuple[int, int], list] = defaultdict(list)
    for t in trades:
        batches[(int(t["ts"]), int(t["book"]))].append({
            "p": t["p"], "q": t["q"], "s": t["s"],
            "Ma": t["Ma"], "Ta": t["Ta"],
        })
    return batches


def replay_debeta(
    trades: list[dict],
    *,
    making_window_ns: int | None = None,
    skill_window_ns: int | None = None,
    mark_mode: str = "last",
    mark_window: int = 0,
):
    """Replay fills through making + skill accumulators (validator debeta.py)."""
    buy: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    sell: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    mtm: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    invsum: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    invn: dict[int, int] = {}
    inv: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    p_first: dict[int, float] = {}
    p_last: dict[int, float | None] = {}
    drift: dict[int, float] = {}

    buy_hist = {} if making_window_ns else None
    sell_hist = {} if making_window_ns else None
    mtm_hist = {} if skill_window_ns else None
    invsum_hist = {} if skill_window_ns else None
    invn_hist = {} if skill_window_ns else None
    drift_hist = {} if skill_window_ns else None
    mark_state = {} if mark_mode != "last" and mark_window > 0 else None

    batches = _group_batches(trades)
    for (ts, book_id) in sorted(batches.keys()):
        batch = batches[(ts, book_id)]
        accumulate_book_capture(
            buy, sell, book_id, batch, CAPTURE_W,
            buy_hist=buy_hist, sell_hist=sell_hist, ts=ts,
        )
        accumulate_book_mtm(
            mtm, invsum, invn, inv, p_first, p_last, book_id, batch,
            mtm_hist=mtm_hist, invsum_hist=invsum_hist, invn_hist=invn_hist,
            drift=drift, drift_hist=drift_hist, ts=ts,
            mark_state=mark_state, mark_mode=mark_mode, mark_window=mark_window,
        )
        if making_window_ns and buy_hist is not None:
            threshold = ts - making_window_ns
            prune_hist_2level(buy_hist, buy, threshold)
            prune_hist_2level(sell_hist, sell, threshold)
        if skill_window_ns:
            threshold = ts - skill_window_ns
            prune_hist_2level(mtm_hist, mtm, threshold)
            prune_hist_2level(invsum_hist, invsum, threshold)
            prune_hist_1level(invn_hist, invn, threshold)
            prune_hist_1level(drift_hist, drift, threshold)

    return {
        "buy": buy,
        "sell": sell,
        "mtm": mtm,
        "invsum": invsum,
        "invn": invn,
        "inv": inv,
        "p_first": p_first,
        "p_last": p_last,
        "drift": drift,
    }


def accumulate_making(trades: list[dict], *, making_window_ns: int | None = None):
    state = replay_debeta(trades, making_window_ns=making_window_ns, skill_window_ns=None)
    return state["buy"], state["sell"]


def export_debeta_report(
    state: dict,
    *,
    w_make: float = 0.30,
    floor_scale: float = 0.5,
):
    buy, sell = state["buy"], state["sell"]
    mtm, invsum, invn, inv, drift = (
        state["mtm"], state["invsum"], state["invn"], state["inv"], state["drift"],
    )
    alphas_by_book = book_alphas_by_book(mtm, invsum, invn, drift)
    alphas_list = {
        uid: [alphas_by_book[uid][b] for b in sorted(alphas_by_book[uid])]
        for uid in alphas_by_book
    }
    uids = sorted(
        set(buy.keys()) | set(sell.keys()) | set(mtm.keys()) | set(invsum.keys())
    )
    floor = median_abs_floor(alphas_list, scale=floor_scale) if floor_scale > 0 else 0.0
    # 0.6.1: debeta_components removed; scores + detail replace it.
    detail: dict = {}
    scores = debeta_scores(
        {u: dict(buy[u]) for u in uids},
        {u: dict(sell[u]) for u in uids},
        alphas_list,
        floor=floor,
        w_make=w_make,
        detail=detail,
        making_floor_scale=0.0,
        skill_rank_scope="positives",
        making_rank_scope="positives",
    )
    parts = {
        "making_raw": {},
        "making_magnitude": {},
        "making_rank": {},
        "skill_raw": {},
        "skill": {},
        "skill_rank": {},
        "combined": {},
    }
    for u in set(scores) | set(detail):
        row = detail.get(u) or {}
        making_raw = float(row.get("making_raw", 0.0))
        skill_raw = float(row.get("skill_raw", 0.0))
        parts["making_raw"][u] = making_raw
        parts["making_magnitude"][u] = making_raw
        parts["making_rank"][u] = float(row.get("making_rank", 0.0))
        parts["skill_raw"][u] = skill_raw
        parts["skill"][u] = skill_raw
        parts["skill_rank"][u] = float(row.get("skill_rank", 0.0))
        parts["combined"][u] = float(scores.get(u, 0.0))

    book_meta = {}
    for b in sorted(set(invn.keys()) | set(drift.keys()) | set(state["p_first"].keys())):
        book_meta[str(b)] = {
            "invn": int(invn.get(b, 0)),
            "drift": float(drift.get(b, 0.0)),
            "p_first": state["p_first"].get(b),
            "p_last": state["p_last"].get(b),
        }

    payload = {
        "meta": {
            "capture_w": CAPTURE_W,
            "w_make": w_make,
            "floor_scale": floor_scale,
            "floor": floor,
        },
        "books": book_meta,
        "uids": {},
    }

    for u in uids:
        books = sorted(
            set(buy.get(u, {}))
            | set(sell.get(u, {}))
            | set(mtm.get(u, {}))
            | set(invsum.get(u, {}))
        )
        book_rows = {}
        for b in books:
            n = invn.get(b, 0)
            isum = float(invsum.get(u, {}).get(b, 0.0))
            mean_inv = isum / n if n > 0 else 0.0
            book_rows[str(b)] = {
                "buy_cap": float(buy.get(u, {}).get(b, 0.0)),
                "sell_cap": float(sell.get(u, {}).get(b, 0.0)),
                "mtm": float(mtm.get(u, {}).get(b, 0.0)),
                "invsum": isum,
                "mean_inv": mean_inv,
                "alpha": float(alphas_by_book.get(u, {}).get(b, 0.0)),
                "inv_end": float(inv.get(b, {}).get(u, 0.0)),
            }
        payload["uids"][str(u)] = {
            "books": book_rows,
            "totals": {
                "buy_cap": sum(buy[u].values()),
                "sell_cap": sum(sell[u].values()),
                "making_raw": parts["making_raw"].get(u, 0.0),
                "making_magnitude": parts["making_magnitude"].get(u, 0.0),
                "making_rank": parts["making_rank"].get(u, 0.0),
                "skill_raw": parts["skill_raw"].get(u, 0.0),
                "skill": parts["skill"].get(u, 0.0),
                "skill_rank": parts["skill_rank"].get(u, 0.0),
                "combined": parts["combined"].get(u, 0.0),
                "qualifying_books": sum(
                    1 for a in alphas_list.get(u, []) if abs(a) >= floor
                ),
            },
        }
    return payload


def export_caps(buy, sell, *, w_make: float = 0.30, floor_scale: float = 0.0):
    """Making-only report (backward compatible)."""
    state = {"buy": buy, "sell": sell, "mtm": {}, "invsum": {}, "invn": {}, "inv": {},
             "p_first": {}, "p_last": {}, "drift": {}}
    report = export_debeta_report(state, w_make=w_make, floor_scale=floor_scale)
    for uid, row in report["uids"].items():
        for book in row["books"].values():
            book.pop("mtm", None)
            book.pop("invsum", None)
            book.pop("mean_inv", None)
            book.pop("alpha", None)
            book.pop("inv_end", None)
            book["balanced_per_book"] = max(
                0.0, 2.0 * min(book["buy_cap"], book["sell_cap"])
            )
        for key in ("skill", "skill_raw", "skill_rank", "combined", "qualifying_books"):
            row["totals"].pop(key, None)
    report["books"] = {}
    return report


def print_debeta_summary(report: dict, *, focus_uid: int = 0) -> None:
    """Human-readable de-beta score lines for console after a proxy test."""
    uid_s = str(focus_uid)
    row = (report.get("uids") or {}).get(uid_s)
    if not row:
        print(f"de-beta: no data for uid {focus_uid}")
        return
    t = row.get("totals") or {}
    print("\n=== De-beta local score (uid %s) ===" % focus_uid)
    print(
        f"  making: raw={t.get('making_raw', t.get('making_magnitude', 0)):.4f}  "
        f"rank={t.get('making_rank', 0):.4f}"
    )
    print(
        f"  skill:  raw={t.get('skill_raw', t.get('skill', 0)):.4f}  "
        f"rank={t.get('skill_rank', 0):.4f}  "
        f"q_books={t.get('qualifying_books', 0)}"
    )
    print(f"  combined={t.get('combined', 0):.4f}")
    books = row.get("books") or {}
    if not books:
        return
    print(f"  {'book':>5}  {'buy':>8}  {'sell':>8}  {'mtm':>8}  {'invΣ':>8}  {'α':>8}  {'drift':>8}")
    for b in sorted(books.keys(), key=lambda x: int(x))[:16]:
        bk = books[b]
        print(
            f"  {int(b):>5}  {bk.get('buy_cap', 0):8.3f}  {bk.get('sell_cap', 0):8.3f}  "
            f"{bk.get('mtm', 0):8.3f}  {bk.get('invsum', 0):8.1f}  "
            f"{bk.get('alpha', 0):8.3f}  {bk.get('drift', 0):8.4f}"
        )
    if len(books) > 16:
        print(f"  ... +{len(books) - 16} more books (see debeta_caps.json)")


class DebetaFillsRecorder:
    """Accumulate fills during a proxy run and export JSON on simulation end."""

    def __init__(
        self,
        fills_out: str | Path | None,
        *,
        caps_out: str | Path | None = None,
        making_window_ns: int | None = None,
        skill_window_ns: int | None = DEFAULT_SKILL_WINDOW_NS,
        mark_mode: str = "last",
        mark_window: int = 0,
        w_make: float = 0.30,
        floor_scale: float = 0.5,
        focus_uid: int = 0,
    ):
        self.fills_out = Path(fills_out).resolve() if fills_out else None
        self.caps_out = Path(caps_out).resolve() if caps_out else None
        self.making_window_ns = making_window_ns
        self.skill_window_ns = skill_window_ns
        self.mark_mode = mark_mode
        self.mark_window = mark_window
        self.w_make = w_make
        self.floor_scale = floor_scale
        self.focus_uid = int(focus_uid)
        self._trades: list[dict] = []
        self._seen: set[tuple[int, int]] = set()
        self._dirty = False
        if self.fills_out:
            atexit.register(self.flush)

    @property
    def enabled(self) -> bool:
        return self.fills_out is not None

    def reset(self) -> None:
        if self._dirty:
            self.flush()
        self._trades.clear()
        self._seen.clear()
        self._dirty = False

    def record_state(self, state) -> int:
        if not self.enabled:
            return 0
        n = 0
        for row in extract_trades_from_state(state):
            key = (row["book"], row["i"])
            if key in self._seen:
                continue
            self._seen.add(key)
            self._trades.append({k: v for k, v in row.items() if k != "i"})
            n += 1
            self._dirty = True
        return n

    def flush(self) -> None:
        if not self.enabled or not self._dirty:
            return
        self.fills_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {"trades": self._trades, "count": len(self._trades)}
        self.fills_out.write_text(json.dumps(payload, indent=2))

        summary = f"de-beta fills: wrote {len(self._trades)} trades -> {self.fills_out}"
        if self.caps_out and self._trades:
            state = replay_debeta(
                self._trades,
                making_window_ns=self.making_window_ns,
                skill_window_ns=self.skill_window_ns,
                mark_mode=self.mark_mode,
                mark_window=self.mark_window,
            )
            report = export_debeta_report(
                state, w_make=self.w_make, floor_scale=self.floor_scale,
            )
            self.caps_out.parent.mkdir(parents=True, exist_ok=True)
            self.caps_out.write_text(json.dumps(report, indent=2))
            summary += f"; report -> {self.caps_out}"
            print_debeta_summary(report, focus_uid=self.focus_uid)
        try:
            import bittensor as bt
            bt.logging.info(summary)
        except Exception:
            print(summary)
        self._dirty = False
