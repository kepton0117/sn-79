# SPDX-FileCopyrightText: 2025 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Validator-style kappa-3 preview helpers used by Topstrategy."""

from __future__ import annotations

from typing import Any

import numpy as np

try:
    from taos.im.utils.kappa import kappa_3
    from taos.im.utils import normalize
except ImportError:  # pragma: no cover
    kappa_3 = None  # type: ignore
    normalize = None  # type: ignore

PUBLISH_NS = 1_000_000_000
KAPPA_LOOKBACK_NS = 10_800_000_000_000
KAPPA_MIN_LOOKBACK_NS = 5_400_000_000_000
KAPPA_TAU = 0.0
KAPPA_NORM_MIN = -2.5
KAPPA_NORM_MAX = 2.5
KAPPA_MIN_OBS = 3


def book_volume_cap(
    miner_wealth: float, capital_turnover_cap: float, book_count: int
) -> float:
    """Per-book quote turnover cap (validator activity capital share).

    Validator formula is ``capital_turnover_cap * miner_wealth`` per book.
    ``miner_wealth`` in state.config is already per-book capital, so do not
    divide by ``book_count``. The unused ``book_count`` argument is kept for
    call-site compatibility.
    """
    del book_count
    return float(miner_wealth) * float(capital_turnover_cap)


def dense_padded_history(
    event_hist: dict[int, dict[int, float]],
    ts: int,
    *,
    pad_ticks: int,
    publish_ns: int = PUBLISH_NS,
) -> dict[int, dict[int, float]]:
    """Expand sparse FIFO event history onto a fixed publish grid ending at ``ts``.

    Leading empty ticks are materialised (validator seeds empty columns) so young
    miners can still satisfy min_lookback once ``pad_ticks`` is large enough.
    """
    publish_ns = max(1, int(publish_ns))
    pad_ticks = max(1, int(pad_ticks))
    end = (int(ts) // publish_ns) * publish_ns
    start = end - (pad_ticks - 1) * publish_ns

    raw = {
        int(t): {int(b): float(p) for b, p in (books or {}).items()}
        for t, books in (event_hist or {}).items()
    }

    dense: dict[int, dict[int, float]] = {}
    t = start
    while t <= end:
        dense[t] = dict(raw.get(t, {}))
        t += publish_ns
    return dense


def preview_kappa(
    uid: int,
    history: dict[int, dict[int, float]],
    *,
    book_count: int = 128,
    lookback_ns: int = 0,
    min_lookback_ns: int = KAPPA_MIN_LOOKBACK_NS,
    min_realized_observations: int = KAPPA_MIN_OBS,
    tau: float = KAPPA_TAU,
    norm_min: float = KAPPA_NORM_MIN,
    norm_max: float = KAPPA_NORM_MAX,
) -> dict[str, Any] | None:
    """Run validator ``kappa_3`` on an already-densified history.

    Signature matches Topstrategy call sites (uid first, then history).
    When ``lookback_ns`` is 0 the history is used as-is (already window-capped).
    """
    if kappa_3 is None:
        return None
    if not history:
        return None

    raw = kappa_3(
        int(uid),
        {int(t): {int(b): float(p) for b, p in books.items()} for t, books in history.items()},
        float(tau),
        int(lookback_ns),
        float(norm_min),
        float(norm_max),
        int(min_lookback_ns),
        int(min_realized_observations),
        grace_period=0,
        deregistered_uids=[],
        book_count=int(book_count),
        cache=None,
    )
    return raw


def book_preview_kappa(
    kappa_result: dict[str, Any] | None, book_id: int
) -> float | None:
    """Normalized [0,1] kappa for one book, or None if unscored."""
    if not kappa_result or not isinstance(kappa_result.get("books"), dict):
        return None
    books = kappa_result["books"]
    raw = books.get(book_id)
    if raw is None:
        raw = books.get(str(book_id))
    if raw is None:
        return None
    if normalize is not None:
        try:
            return float(normalize(KAPPA_NORM_MIN, KAPPA_NORM_MAX, float(raw)))
        except Exception:
            pass
    # Fallback clamp matching Topstrategy's (k + 2.5) / 5 mapping.
    return max(0.0, min(1.0, (float(raw) + 2.5) * 0.2))
