# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
HelpAgent: helper miner that IOC-fills a target miner's resting open orders.

Reads the target's TopAgent JSON for which books to help.

Price sources, in order:
  1. Target waiting_orders.json (178 already posted; leftover dual).
  2. Same-tick mid bracket when the target JSON uses mid mode
     (use_depth_qty=false / open_price=0): BUY@mid − bracket_ticks/2 and
     SELL@mid + bracket_ticks/2 from this tick's L2 — same math as TopAgent,
     so 178 GTT dual and 187 IOC dual can land in one matching cycle.
  3. Legacy single-side lift at JSON open_price if that is > 0.

Same tick, in order:
  1. IOC SELL into every BUY level (needs base)
  2. IOC BUY into every SELL level (needs quote)

Each IOC is sized off this miner's free wallet rather than the target's
recorded size: SELL takes base_free - 1, BUY takes quote_free / price - 1.
The target's waiting_orders row is only a price source and a "the dual exists"
gate, because it can lag the exchange by a tick; an IOC that overshoots the
resting dual just cancels the unmatched tail.

Optional cold-book kappa warm: one MARKET BUY + SELL pair per tick, repeated
cold_warm_rounds times (default 3 ticks) on configured cold_books.

Market recharge (when IOC help fails for low balance) is capped per book by
recharge_times (default 3). Counters persist in this miner's waiting_orders.json.
Recharge is the only quantity knob left; help size itself is uncapped by config.

Hot-reload: edit agents/data/helpagent.<uid>.json and/or the target topagent JSON.

  live_config=1 live_config_path=agents/data/helpagent.203.json
  help_uid=80 target_config_path=agents/data/topagent.80.json
"""



from __future__ import annotations

import json
import sys
import threading
import types
from dataclasses import dataclass
from pathlib import Path

# importlib spec_from_file_location does not register the module until after exec;
# @dataclass needs sys.modules[__name__] during class body evaluation.
if __name__ not in sys.modules:
    sys.modules[__name__] = types.ModuleType(__name__)

import bittensor as bt

from taos.common.agents import launch
from taos.im.agents import GenTRXAgent
from taos.im.protocol.models import (
    OrderDirection,
    OrderCurrency,
    STP,
    TimeInForce,
    LoanSettlementOption,
)


def _round_px(price: float, decimals: int) -> float:
    return round(price, decimals)


def _tick(decimals: int) -> float:
    return 10.0 ** (-decimals)


def _g(obj, *names, default=None):
    for name in names:
        if obj is None:
            break
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                pass
    return default


def _level_px_qty(level) -> tuple[float | None, float | None]:
    if level is None:
        return None, None
    if isinstance(level, dict):
        px = level.get("p", level.get("price"))
        qty = level.get("q", level.get("qty", level.get("quantity", level.get("volume"))))
    else:
        px = getattr(level, "price", None) or getattr(level, "p", None)
        qty = (
            getattr(level, "quantity", None)
            or getattr(level, "qty", None)
            or getattr(level, "q", None)
        )
    try:
        px_f = float(px) if px is not None else None
    except (TypeError, ValueError):
        px_f = None
    try:
        qty_f = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty_f = None
    return px_f, qty_f


def _parse_optional_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _parse_bool(val, default: bool = False) -> bool:
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class OpenBookSpec:
    book: int
    open_price: float
    max_quantity: float
    open_direction: OrderDirection = OrderDirection.BUY
    use_depth_qty: bool = False
    bracket_ticks: int | None = None


def _parse_open_direction(val, default: OrderDirection = OrderDirection.BUY) -> OrderDirection:
    if val is None:
        return default
    if isinstance(val, OrderDirection):
        return val
    if isinstance(val, int):
        return OrderDirection.SELL if val == 1 else OrderDirection.BUY
    s = str(val).strip().upper()
    if s in ("SELL", "S", "SHORT", "ASK", "1"):
        return OrderDirection.SELL
    return OrderDirection.BUY


def parse_open_books(raw) -> list[OpenBookSpec]:
    out: list[OpenBookSpec] = []
    if not raw:
        return out
    for item in raw:
        if isinstance(item, int):
            out.append(OpenBookSpec(book=int(item), open_price=0.0, max_quantity=1.0))
        elif isinstance(item, dict):
            out.append(
                OpenBookSpec(
                    book=int(item["book"]),
                    open_price=float(item.get("open_price", item.get("price", 0.0))),
                    max_quantity=float(item.get("max_quantity", item.get("max_qty", 1.0))),
                    open_direction=_parse_open_direction(
                        item.get("open_direction", item.get("direction"))
                    ),
                    use_depth_qty=_parse_bool(item.get("use_depth_qty"), default=False),
                    bracket_ticks=_parse_optional_int(
                        item.get("bracket_ticks", item.get("ticks"))
                    ),
                )
            )
    return out


class HelpAgent(GenTRXAgent):
    """IOC sell helper for another miner's TopAgent open books."""

    def initialize(self):
        if not hasattr(self.config, "gtx_training_enabled"):
            self.config.gtx_training_enabled = False
        if not hasattr(self.config, "gtx_collect_data"):
            self.config.gtx_collect_data = False
        super().initialize()

        repo_root = Path(__file__).resolve().parent.parent
        agent_dir = repo_root / "agents"

        self.help_uid = int(getattr(self.config, "help_uid", 0) or 0)
        self.charge_qty = float(getattr(self.config, "charge_qty", 100.0))
        self.charge_max_slippage = float(getattr(self.config, "charge_max_slippage", 1.0))
        try:
            self.recharge_times = max(
                0, int(getattr(self.config, "recharge_times", 3) or 3)
            )
        except (TypeError, ValueError):
            self.recharge_times = 3
        self.cold_books: set[int] = set()
        self.cold_warm_qty = float(getattr(self.config, "cold_warm_qty", 0.25))
        self.cold_warm_rounds = max(1, int(getattr(self.config, "cold_warm_rounds", 3)))
        self.cold_max_slippage = float(getattr(self.config, "cold_max_slippage", 1.0))
        self.cold_warm_once = bool(int(getattr(self.config, "cold_warm_once", 1)))
        self.cold_all_books = bool(int(getattr(self.config, "cold_all_books", 0)))
        self.cold_warm_max_books_per_tick = int(
            getattr(self.config, "cold_warm_max_books_per_tick", 0) or 0
        )
        self._cold_warmed: set[int] = set()
        self._cold_warm_tick: dict[int, int] = {}
        self.cancel_all_orders = int(getattr(self.config, "cancel_all_orders", 0) or 0)
        self._agents_dir = agent_dir

        self.live_config = bool(int(getattr(self.config, "live_config", 1)))
        live_path = getattr(self.config, "live_config_path", "")
        if live_path:
            p = Path(live_path)
            self.live_config_path = p if p.is_absolute() else repo_root / p
        else:
            self.live_config_path = agent_dir / "data" / f"helpagent.{self.uid}.json"

        target_path = getattr(self.config, "target_config_path", "")
        if target_path:
            p = Path(target_path)
            self.target_config_path = p if p.is_absolute() else repo_root / p
        elif self.help_uid:
            self.target_config_path = agent_dir / "data" / f"topagent.{self.help_uid}.json"
        else:
            self.target_config_path = agent_dir / "data" / "topagent.80.json"

        self._live_config_mtime: float | None = None
        self._target_config_mtime: float | None = None
        self.help_books_filter: set[int] | None = None
        self.help_targets: dict[int, OpenBookSpec] = {}
        self.target_bracket_ticks = 2
        self._pending_same_tick: set[int] = set()
        self._saw_target_rest: set[int] = set()

        if self.live_config_path.is_file():
            self._load_live_json(self.live_config_path.read_text(), source="init")
        self._load_target_json(source="init")

        try:
            from waiting_orders_recorder import hydrate_recharge_from_disk

            hydrate_recharge_from_disk(self)
        except Exception:
            pass

        bt.logging.info(
            f"HelpAgent uid={self.uid} help_uid={self.help_uid} "
            f"targets={sorted(self.help_targets)} "
            f"cold={'all' if self.cold_all_books else sorted(self.cold_books)} "
            f"live={self.live_config_path} target_cfg={self.target_config_path}"
        )

    def _load_live_json(self, text: str, *, source: str) -> None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            bt.logging.warning(f"HelpAgent live JSON parse failed ({source}): {exc}")
            return

        if "help_uid" in data:
            try:
                self.help_uid = int(data["help_uid"])
            except (TypeError, ValueError):
                pass

        if "target_config_path" in data and data["target_config_path"]:
            repo_root = Path(__file__).resolve().parent.parent
            p = Path(str(data["target_config_path"]))
            self.target_config_path = p if p.is_absolute() else repo_root / p

        if "charge_qty" in data:
            try:
                self.charge_qty = float(data["charge_qty"])
            except (TypeError, ValueError):
                pass

        if "charge_max_slippage" in data:
            try:
                self.charge_max_slippage = float(data["charge_max_slippage"])
            except (TypeError, ValueError):
                pass

        if "recharge_times" in data:
            try:
                self.recharge_times = max(0, int(data["recharge_times"]))
            except (TypeError, ValueError):
                pass

        raw_filter = data.get("help_books")
        if raw_filter is None:
            self.help_books_filter = None
        elif isinstance(raw_filter, list):
            self.help_books_filter = {int(b) for b in raw_filter}
        else:
            self.help_books_filter = {int(raw_filter)}

        raw_cold = data.get("cold_books")
        if raw_cold is None:
            pass
        elif isinstance(raw_cold, list):
            self.cold_books = {int(b) for b in raw_cold}
        else:
            self.cold_books = {int(raw_cold)}

        if "cold_warm_qty" in data:
            try:
                self.cold_warm_qty = float(data["cold_warm_qty"])
            except (TypeError, ValueError):
                pass

        if "cold_warm_rounds" in data:
            try:
                self.cold_warm_rounds = max(1, int(data["cold_warm_rounds"]))
            except (TypeError, ValueError):
                pass

        if "cold_max_slippage" in data:
            try:
                self.cold_max_slippage = float(data["cold_max_slippage"])
            except (TypeError, ValueError):
                pass

        if "cold_warm_once" in data:
            self.cold_warm_once = bool(int(data["cold_warm_once"]))

        if "cold_all_books" in data:
            self.cold_all_books = bool(int(data["cold_all_books"]))

        if "cold_warm_max_books_per_tick" in data:
            try:
                self.cold_warm_max_books_per_tick = max(
                    0, int(data["cold_warm_max_books_per_tick"])
                )
            except (TypeError, ValueError):
                pass

        warmed = data.get("cold_warmed_books")
        if isinstance(warmed, list):
            done = {int(b) for b in warmed}
            self._cold_warmed = done
            for bid in done:
                self._cold_warm_tick[int(bid)] = self.cold_warm_rounds

        if "cancel_all_orders" in data:
            try:
                self.cancel_all_orders = int(data["cancel_all_orders"] or 0)
            except (TypeError, ValueError):
                pass

    def _load_target_json(self, *, source: str) -> None:
        path = self.target_config_path
        if not path.is_file():
            bt.logging.warning(f"HelpAgent target config missing: {path}")
            self.help_targets = {}
            return
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            bt.logging.warning(f"HelpAgent target JSON parse failed ({source}): {exc}")
            return

        specs = parse_open_books(data.get("open_books") or [])
        targets: dict[int, OpenBookSpec] = {}
        for spec in specs:
            if self.help_books_filter is not None and spec.book not in self.help_books_filter:
                continue
            targets[spec.book] = spec

        try:
            self.target_bracket_ticks = max(1, int(data.get("bracket_ticks", 2) or 2))
        except (TypeError, ValueError):
            self.target_bracket_ticks = 2

        if "recharge_times" in data:
            try:
                self.recharge_times = max(0, int(data["recharge_times"]))
            except (TypeError, ValueError):
                pass

        prev = set(self.help_targets)
        new_books = set(targets) - prev
        removed = prev - set(targets)
        if removed or new_books:
            try:
                from waiting_orders_recorder import clear_recharge_books

                clear_recharge_books(self, removed | new_books)
            except Exception:
                pass
        if source == "init":
            self._pending_same_tick = set()
        else:
            self._pending_same_tick = (getattr(self, "_pending_same_tick", set()) | new_books) & set(targets)

        self.help_targets = targets
        self._target_config_mtime = path.stat().st_mtime
        extra = f" same_tick={sorted(self._pending_same_tick)}" if self._pending_same_tick else ""
        bt.logging.info(
            f"HelpAgent target reload ({source}): help_uid={self.help_uid} "
            f"books={sorted(targets)} bracket={self.target_bracket_ticks}{extra}"
        )

    def _book_from_state(self, state, book_id: int):
        books = state.books or {}
        return books.get(book_id) or books.get(str(book_id))

    def _spec_bracket_ticks(self, spec: OpenBookSpec) -> int:
        if spec.bracket_ticks is not None:
            return max(1, int(spec.bracket_ticks))
        return max(1, int(getattr(self, "target_bracket_ticks", 2) or 2))

    def _touch_best_bid_ask(self, book) -> tuple[float | None, float | None]:
        raw = getattr(book, "_raw", None)
        if isinstance(raw, dict):
            bids = raw.get("b") or []
            asks = raw.get("a") or []
        else:
            bids = _g(book, "bids", "b") or []
            asks = _g(book, "asks", "a") or []
        best_bid = None
        best_ask = None
        if bids:
            best_bid, _ = _level_px_qty(bids[0])
        if asks:
            best_ask, _ = _level_px_qty(asks[0])
        return best_bid, best_ask

    def _mid_bracket_prices(
        self,
        state,
        book_id: int,
        spec: OpenBookSpec,
        pd: int,
    ) -> tuple[float, float] | None:
        """Same as TopAgent: BUY@mid − bracket/2, SELL@mid + bracket/2."""
        book = self._book_from_state(state, book_id)
        if book is None:
            return None
        best_bid, best_ask = self._touch_best_bid_ask(book)
        if (
            best_bid is None
            or best_ask is None
            or best_bid <= 0.0
            or best_ask <= 0.0
        ):
            return None
        mid = (float(best_bid) + float(best_ask)) / 2.0
        tick = _tick(pd)
        half = tick * (self._spec_bracket_ticks(spec) / 2.0)
        buy_px = _round_px(mid - half, pd)
        sell_px = _round_px(mid + half, pd)
        if sell_px <= buy_px:
            buy_px = _round_px(mid - tick, pd)
            sell_px = _round_px(mid + tick, pd)
        if buy_px <= 0.0 or sell_px <= buy_px:
            return None
        return buy_px, sell_px

    def _book_in_state(self, state, book_id: int) -> bool:
        books = state.books or {}
        return book_id in books or str(book_id) in books

    def _cold_books_for_state(self, state) -> list[int]:
        if self.cold_all_books:
            books = state.books or {}
            out: list[int] = []
            for key in books:
                try:
                    out.append(int(key))
                except (TypeError, ValueError):
                    continue
            return sorted(out)
        return sorted(self.cold_books)

    def _maybe_reload_configs(self) -> None:
        if self.live_config and self.live_config_path.is_file():
            mtime = self.live_config_path.stat().st_mtime
            if self._live_config_mtime is None or mtime != self._live_config_mtime:
                self._load_live_json(
                    self.live_config_path.read_text(), source="mtime"
                )
                self._live_config_mtime = mtime
                self._load_target_json(source="live-reload")

        if self.target_config_path.is_file():
            mtime = self.target_config_path.stat().st_mtime
            if self._target_config_mtime is None or mtime != self._target_config_mtime:
                self._load_target_json(source="mtime")

    def _waiting_orders_paths(self) -> list[Path]:
        uid = self.help_uid
        if uid <= 0:
            return []
        paths = [
            self._agents_dir / "data" / str(uid) / str(uid) / str(uid) / "waiting_orders.json",
            self._agents_dir / "data" / str(uid) / str(uid) / "waiting_orders.json",
            self._agents_dir / "data" / str(uid) / "waiting_orders.json",
            self._agents_dir / f"waiting_orders.{uid}.json",
            self._agents_dir / "data" / f"waiting_orders.{uid}.json",
        ]
        out: list[Path] = []
        seen: set[str] = set()
        for p in paths:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        return out

    def _price_matches(self, order_px: float, target_px: float, pd: int) -> bool:
        try:
            opx = float(order_px)
        except (TypeError, ValueError):
            return False
        return abs(opx - target_px) <= _tick(pd) * 0.51

    def _sum_side_qty_from_orders(
        self,
        orders,
        book_id: int,
        px: float,
        pd: int,
        side: str,
    ) -> float:
        total = 0.0
        side_u = side.upper()
        for o in orders or []:
            ob = _g(o, "book_id", "bookId", default=book_id)
            try:
                if int(ob) != int(book_id):
                    continue
            except (TypeError, ValueError):
                continue
            direction = str(_g(o, "direction", default="")).upper()
            if direction != side_u:
                continue
            order_px = _g(o, "price", "p", default=0.0)
            if not self._price_matches(order_px, px, pd):
                continue
            try:
                total += float(_g(o, "quantity", "qty", "q", default=0.0) or 0.0)
            except (TypeError, ValueError):
                continue
        return total

    def _sum_buy_qty_from_orders(self, orders, book_id: int, px: float, pd: int) -> float:
        return self._sum_side_qty_from_orders(orders, book_id, px, pd, "BUY")

    def _sum_sell_qty_from_orders(self, orders, book_id: int, px: float, pd: int) -> float:
        return self._sum_side_qty_from_orders(orders, book_id, px, pd, "SELL")

    def _load_waiting_payload(self) -> dict | None:
        """Newest waiting_orders dump for help_uid, or None."""
        best: dict | None = None
        best_mtime = -1.0
        for path in self._waiting_orders_paths():
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            try:
                if int(data.get("uid", self.help_uid) or self.help_uid) != int(self.help_uid):
                    continue
            except (TypeError, ValueError):
                continue
            if mtime >= best_mtime:
                best = data
                best_mtime = mtime
        return best

    def _orders_for_book(self, data: dict, book_id: int) -> list[dict]:
        orders: list[dict] = []
        books = data.get("books") or {}
        book_entry = books.get(str(book_id))
        if book_entry is None:
            book_entry = books.get(book_id)
        if isinstance(book_entry, dict):
            for key in ("open_orders", "waiting"):
                for o in book_entry.get(key) or []:
                    if isinstance(o, dict):
                        orders.append(o)
        for key in ("confirmed", "waiting", "sent"):
            for o in data.get(key) or []:
                if not isinstance(o, dict):
                    continue
                if str(o.get("type") or "limit").lower() == "cancel":
                    continue
                try:
                    if int(o.get("book_id", o.get("bookId", -1))) != int(book_id):
                        continue
                except (TypeError, ValueError):
                    continue
                orders.append(o)
        unique: list[dict] = []
        seen: set[tuple] = set()
        for o in orders:
            oid = o.get("id", o.get("order_id"))
            try:
                key: tuple = ("id", int(oid)) if oid is not None else (
                    str(o.get("direction")),
                    float(o.get("price") or 0.0),
                    float(o.get("quantity") or 0.0),
                    str(o.get("kind") or o.get("status") or ""),
                )
            except (TypeError, ValueError):
                unique.append(o)
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(o)
        return unique

    def _target_resting_levels(self, book_id: int, pd: int) -> dict[str, dict[float, float]]:
        """BUY/SELL -> {rounded_price: qty} from the target's waiting_orders dump."""
        out: dict[str, dict[float, float]] = {"BUY": {}, "SELL": {}}
        data = self._load_waiting_payload()
        if data is None:
            return out
        for o in self._orders_for_book(data, book_id):
            side = str(_g(o, "direction", default="") or "").upper()
            if side not in ("BUY", "SELL"):
                raw_side = _g(o, "side", default=None)
                try:
                    side = "SELL" if int(raw_side) == 1 else "BUY"
                except (TypeError, ValueError):
                    continue
            if side not in out:
                continue
            try:
                px = _round_px(float(_g(o, "price", "p", default=0.0) or 0.0), pd)
                qty = float(_g(o, "quantity", "qty", "q", default=0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if px <= 0.0 or qty <= 0.0:
                continue
            bucket = out[side]
            bucket[px] = bucket.get(px, 0.0) + qty
        return out

    def _target_resting_open_qty(
        self,
        book_id: int,
        px: float,
        pd: int,
        side: str,
    ) -> float | None:
        """Sum help_uid open orders at target price from waiting_orders JSON."""
        if self._load_waiting_payload() is None:
            return None
        levels = self._target_resting_levels(book_id, pd)
        total = 0.0
        for order_px, qty in levels.get(side.upper(), {}).items():
            if self._price_matches(order_px, px, pd):
                total += qty
        return max(0.0, total)

    def _target_resting_buy_qty(self, book_id: int, px: float, pd: int) -> float | None:
        return self._target_resting_open_qty(book_id, px, pd, "BUY")

    def _best_ask(self, book) -> float | None:
        asks = _g(book, "asks", "a")
        if not asks:
            return None
        first = asks[0]
        px = _g(first, "price", "p")
        try:
            return float(px) if px is not None else None
        except (TypeError, ValueError):
            return None

    def _maybe_charge(
        self,
        response,
        book_id: int,
        acct,
        book,
        pd: int,
        vd: int,
    ) -> bool:
        """Market-order recharge when base/quote too low to help. Returns True if sent."""
        try:
            from waiting_orders_recorder import note_recharge, recharge_allow
        except ImportError:
            note_recharge = None

            def recharge_allow(_agent, _book_id) -> bool:
                return True

        if not recharge_allow(self, book_id):
            bt.logging.info(
                f"HelpAgent book {book_id}: skip charge (recharge_times="
                f"{getattr(self, 'recharge_times', 3)} exhausted)"
            )
            return False

        base_free = float(_g(acct.base_balance, "free", default=0.0) or 0.0)
        quote_free = float(_g(acct.quote_balance, "free", default=0.0) or 0.0)
        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        charge_qty = round(float(self.charge_qty), vd)
        if charge_qty < min_sz - 1e-12:
            charge_qty = round(min_sz, vd)

        slip = float(self.charge_max_slippage)
        best_ask = self._best_ask(book) or 0.0
        est_buy_cost = charge_qty * best_ask if best_ask > 0.0 else 0.0

        if base_free < min_sz - 1e-12:
            if quote_free >= min_sz and (est_buy_cost <= 0.0 or quote_free >= est_buy_cost * 0.01):
                bt.logging.info(
                    f"HelpAgent book {book_id}: charge MARKET BUY qty={charge_qty} "
                    f"(base_free={base_free:.4f} quote_free={quote_free:.2f} "
                    f"best_ask={best_ask} slip={slip})"
                )
                response.market_order(
                    book_id=book_id,
                    direction=OrderDirection.BUY,
                    quantity=charge_qty,
                    currency=OrderCurrency.BASE,
                    stp=STP.CANCEL_BOTH,
                    max_slippage=slip,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
                if note_recharge is not None:
                    note_recharge(self, book_id)
                return True
            sell_qty = round(min(charge_qty, base_free), vd)
            if sell_qty >= min_sz - 1e-12:
                bt.logging.info(
                    f"HelpAgent book {book_id}: charge MARKET SELL qty={sell_qty} "
                    f"(base_free={base_free:.4f} quote_free={quote_free:.2f} slip={slip})"
                )
                response.market_order(
                    book_id=book_id,
                    direction=OrderDirection.SELL,
                    quantity=sell_qty,
                    currency=OrderCurrency.BASE,
                    stp=STP.CANCEL_BOTH,
                    max_slippage=slip,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
                if note_recharge is not None:
                    note_recharge(self, book_id)
                return True
            if quote_free >= min_sz:
                bt.logging.info(
                    f"HelpAgent book {book_id}: charge MARKET BUY qty={charge_qty} (fallback) "
                    f"(base_free={base_free:.4f} quote_free={quote_free:.2f} slip={slip})"
                )
                response.market_order(
                    book_id=book_id,
                    direction=OrderDirection.BUY,
                    quantity=charge_qty,
                    currency=OrderCurrency.BASE,
                    stp=STP.CANCEL_BOTH,
                    max_slippage=slip,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
                if note_recharge is not None:
                    note_recharge(self, book_id)
                return True
            return False

        if est_buy_cost > 0.0 and quote_free < est_buy_cost and base_free >= min_sz:
            sell_qty = round(min(charge_qty, base_free), vd)
            if sell_qty >= min_sz - 1e-12:
                bt.logging.info(
                    f"HelpAgent book {book_id}: charge MARKET SELL qty={sell_qty} "
                    f"(low quote for buy: need~{est_buy_cost:.2f} have={quote_free:.2f})"
                )
                response.market_order(
                    book_id=book_id,
                    direction=OrderDirection.SELL,
                    quantity=sell_qty,
                    currency=OrderCurrency.BASE,
                    stp=STP.CANCEL_BOTH,
                    max_slippage=slip,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
                if note_recharge is not None:
                    note_recharge(self, book_id)
                return True
        return False

    def _account_for_book(self, book_id: int):
        acct = self.accounts.get(book_id)
        if acct is not None:
            return acct
        return self.accounts.get(str(book_id))

    def _maybe_cold_warm(
        self,
        response,
        state,
        book_id: int,
        pd: int,
        vd: int,
    ) -> bool:
        """One MARKET BUY+SELL pair per tick; cold_warm_rounds ticks per book."""
        if not self.cold_all_books and book_id not in self.cold_books:
            return False
        if self.cold_warm_once and book_id in self._cold_warmed:
            return False

        rounds = self.cold_warm_rounds
        tick = self._cold_warm_tick.get(book_id, 0)
        if tick >= rounds:
            if self.cold_warm_once:
                self._cold_warmed.add(book_id)
            return False

        acct = self._account_for_book(book_id)
        if acct is None:
            return False

        book = self._book_from_state(state, book_id)
        if book is None:
            return False

        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        qty = round(float(self.cold_warm_qty), vd)
        if qty < min_sz - 1e-12:
            qty = round(min_sz, vd)

        quote_free = float(_g(acct.quote_balance, "free", default=0.0) or 0.0)
        best_ask = self._best_ask(book) or 0.0
        need_quote = qty * best_ask if best_ask > 0.0 else qty
        if quote_free < max(min_sz, need_quote * 0.01) - 1e-12:
            bt.logging.debug(
                f"HelpAgent book {book_id}: skip cold warm tick {tick + 1}/{rounds} "
                f"(quote_free={quote_free:.2f} need~{need_quote:.2f})"
            )
            return False

        slip = float(self.cold_max_slippage)
        bt.logging.info(
            f"HelpAgent book {book_id}: cold warm tick {tick + 1}/{rounds} "
            f"BUY+SELL qty={qty} slip={slip}"
        )
        response.market_order(
            book_id=book_id,
            direction=OrderDirection.BUY,
            quantity=qty,
            currency=OrderCurrency.BASE,
            stp=STP.CANCEL_BOTH,
            max_slippage=slip,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        response.market_order(
            book_id=book_id,
            direction=OrderDirection.SELL,
            quantity=qty,
            currency=OrderCurrency.BASE,
            stp=STP.CANCEL_BOTH,
            max_slippage=slip,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )

        tick += 1
        self._cold_warm_tick[book_id] = tick
        if tick >= rounds and self.cold_warm_once:
            self._cold_warmed.add(book_id)
        return True

    def _cancel_resting(self, response, book_id: int) -> None:
        acct = self.accounts.get(book_id)
        if acct is None:
            return
        orders = getattr(acct, "orders", None) or []
        if not orders:
            return
        ids = [o.id for o in orders]
        if len(ids) == 1:
            response.cancel_order(book_id, ids[0])
        else:
            response.cancel_orders(book_id, ids)

    def _help_ioc(
        self,
        response,
        acct,
        book_id: int,
        *,
        target_side: str,
        px: float,
        remaining: float,
        vd: int,
        min_sz: float,
    ) -> bool:
        """IOC into one resting target level. SELL hits BUY, BUY hits SELL.

        Size is the whole free wallet side less one unit, not the target's
        recorded size. waiting_orders can lag the exchange by a tick, so sizing
        to it under-fills; an IOC that overshoots the dual cancels the tail.
        """
        if px <= 0.0 or remaining < min_sz - 1e-12:
            return False
        is_hit_sell = target_side.upper() == "SELL"
        help_direction = OrderDirection.BUY if is_hit_sell else OrderDirection.SELL
        base_free = float(getattr(acct.base_balance, "free", 0.0) or 0.0)
        quote_free = float(getattr(acct.quote_balance, "free", 0.0) or 0.0)
        if is_hit_sell:
            # Bought at px, so affordable base is bounded by px and not by mid.
            help_qty = (quote_free / px) - 1.0 if quote_free > 0.0 else 0.0
            wallet_note = f"quote_free={quote_free:.2f}"
        else:
            help_qty = base_free - 1.0
            wallet_note = f"base_free={base_free:.4f}"
        help_qty = round(help_qty, vd)
        if help_qty < min_sz - 1e-12:
            return False
        dir_label = "BUY" if help_direction == OrderDirection.BUY else "SELL"
        bt.logging.info(
            f"HelpAgent book {book_id}: IOC {dir_label} qty={help_qty}@{px} "
            f"(target_{target_side}_remaining={remaining} {wallet_note})"
        )
        response.limit_order(
            book_id=book_id,
            direction=help_direction,
            quantity=help_qty,
            price=px,
            stp=STP.CANCEL_BOTH,
            timeInForce=TimeInForce.IOC,
            leverage=0.0,
            settlement_option=LoanSettlementOption.NONE,
        )
        return True

    def _maybe_help(
        self,
        response,
        state,
        book_id: int,
        spec: OpenBookSpec,
        pd: int,
        vd: int,
    ) -> None:
        acct = self._account_for_book(book_id)
        if acct is None:
            return

        min_sz = float(getattr(self.simulation_config, "min_order_size", 0.25) or 0.25)
        levels = self._target_resting_levels(book_id, pd)
        buy_levels = {
            px: qty
            for px, qty in (levels.get("BUY") or {}).items()
            if qty >= min_sz - 1e-12
        }
        sell_levels = {
            px: qty
            for px, qty in (levels.get("SELL") or {}).items()
            if qty >= min_sz - 1e-12
        }

        if buy_levels or sell_levels:
            self._saw_target_rest.add(book_id)
        elif book_id in getattr(self, "_saw_target_rest", set()):
            # First dual cleared — arm same-tick help for the next 178 dual.
            self._pending_same_tick.add(book_id)
            self._saw_target_rest.discard(book_id)

        if not buy_levels and not sell_levels:
            # Same-tick path: TopAgent has not written waiting_orders yet.
            # Mid-mode books share L2 mid ± bracket_ticks/2 with 178.
            if not spec.use_depth_qty and book_id in getattr(self, "_pending_same_tick", set()):
                pair = self._mid_bracket_prices(state, book_id, spec, pd)
                if pair is not None:
                    buy_px, sell_px = pair
                    qty = max(min_sz, float(spec.max_quantity))
                    buy_levels = {buy_px: qty}
                    sell_levels = {sell_px: qty}
                    self._pending_same_tick.discard(book_id)
                    bt.logging.info(
                        f"HelpAgent book {book_id}: same-tick mid bracket "
                        f"BUY@{buy_px} SELL@{sell_px} qty={qty} "
                        f"ticks={self._spec_bracket_ticks(spec)}"
                    )
            if not buy_levels and not sell_levels:
                px = _round_px(spec.open_price, pd)
                if px <= 0.0:
                    return
                is_sell_open = spec.open_direction == OrderDirection.SELL
                target_side = "SELL" if is_sell_open else "BUY"
                remaining = self._target_resting_open_qty(book_id, px, pd, target_side)
                if remaining is not None and remaining < min_sz - 1e-12:
                    bt.logging.debug(
                        f"HelpAgent book {book_id}: skip help (target {target_side} "
                        f"remaining={remaining:.4f} < min_sz)"
                    )
                    return
                if is_sell_open:
                    sell_levels = {px: remaining if remaining is not None else spec.max_quantity}
                else:
                    buy_levels = {px: remaining if remaining is not None else spec.max_quantity}

        if not buy_levels and not sell_levels:
            return

        self._cancel_resting(response, book_id)

        sent = False
        for px in sorted(buy_levels, reverse=True):
            if self._help_ioc(
                response,
                acct,
                book_id,
                target_side="BUY",
                px=px,
                remaining=buy_levels[px],
                vd=vd,
                min_sz=min_sz,
            ):
                sent = True
        for px in sorted(sell_levels):
            if self._help_ioc(
                response,
                acct,
                book_id,
                target_side="SELL",
                px=px,
                remaining=sell_levels[px],
                vd=vd,
                min_sz=min_sz,
            ):
                sent = True

        if sent:
            return
        book = self._book_from_state(state, book_id)
        if book is not None:
            self._maybe_charge(response, book_id, acct, book, pd, vd)
        else:
            bt.logging.warning(
                f"HelpAgent book {book_id}: need charge but book missing from state "
                f"(base/quote short for dual help)"
            )

    def _persist_cancel_all_off(self) -> None:
        self.cancel_all_orders = 0
        path = getattr(self, "live_config_path", None)
        if path is None or not path.is_file():
            return
        try:
            import fcntl

            with path.open("r+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    raw = handle.read()
                    data = json.loads(raw) if raw.strip() else {}
                    if not isinstance(data, dict):
                        return
                    data["cancel_all_orders"] = 0
                    payload = json.dumps(data, indent=2) + "\n"
                    handle.seek(0)
                    handle.truncate(0)
                    handle.write(payload)
                    handle.flush()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            self._live_config_mtime = path.stat().st_mtime
        except Exception:
            pass

    def respond_simulation(self, state):
        self._maybe_reload_configs()
        pd = getattr(self.simulation_config, "priceDecimals", 8)
        vd = getattr(self.simulation_config, "volumeDecimals", 8)
        response = self.make_response()

        if int(getattr(self, "cancel_all_orders", 0) or 0):
            cancelled = 0
            accounts = getattr(self, "accounts", None) or {}
            if isinstance(accounts, dict):
                for book_id in list(accounts):
                    try:
                        bid = int(book_id)
                    except (TypeError, ValueError):
                        continue
                    before = len(getattr(response, "instructions", None) or [])
                    self._cancel_resting(response, bid)
                    after = len(getattr(response, "instructions", None) or [])
                    cancelled += max(0, after - before)
            bt.logging.info(f"CANCEL_ALL_ORDERS count={cancelled} flatten=0")
            self._persist_cancel_all_off()

        cold_sent = 0
        for book_id in self._cold_books_for_state(state):
            if self.cold_warm_max_books_per_tick > 0 and cold_sent >= self.cold_warm_max_books_per_tick:
                break
            if not self._book_in_state(state, book_id):
                bt.logging.debug(f"HelpAgent book {book_id}: skip cold (not in state.books)")
                continue
            if self._maybe_cold_warm(response, state, book_id, pd, vd):
                cold_sent += 1

        for book_id, spec in sorted(self.help_targets.items()):
            if not self._book_in_state(state, book_id):
                bt.logging.debug(f"HelpAgent book {book_id}: skip (not in state.books)")
                continue
            self._maybe_help(response, state, book_id, spec, pd, vd)

        return response


try:
    _agents_dir = Path(__file__).resolve().parent
    if str(_agents_dir) not in sys.path:
        sys.path.insert(0, str(_agents_dir))
    from l2_depth_recorder import record_l2_from_state as _record_l2_from_state
    from waiting_orders_recorder import (
        record_waiting_orders as _record_waiting_orders,
        sent_orders_from_response as _sent_orders_from_response,
    )

    _orig_handle_for_l2 = HelpAgent.handle
    _orig_respond_sim = HelpAgent.respond_simulation

    def _handle_with_l2_snapshot(self, state):
        def _dump_l2() -> None:
            try:
                uid = int(getattr(self, "uid", -1))
                raw = getattr(state, "accounts", None) or {}
                mine = raw.get(uid) if uid in raw else raw.get(str(uid))
                if isinstance(mine, dict):
                    self.accounts = mine
                _record_l2_from_state(self, state, max_levels=21, min_interval_sec=1.0)
                _record_waiting_orders(
                    self, state, [], max_age_sec=90.0, min_interval_sec=0.0
                )
            except Exception:
                pass

        threading.Thread(target=_dump_l2, daemon=True).start()
        return _orig_handle_for_l2(self, state)

    def _respond_sim_with_waiting(self, state):
        response = _orig_respond_sim(self, state)

        def _dump_waiting() -> None:
            try:
                _record_waiting_orders(
                    self,
                    state,
                    _sent_orders_from_response(response),
                    max_age_sec=90.0,
                    min_interval_sec=0.0,
                )
            except Exception:
                pass

        threading.Thread(target=_dump_waiting, daemon=True).start()
        return response

    HelpAgent.handle = _handle_with_l2_snapshot
    HelpAgent.respond_simulation = _respond_sim_with_waiting
except Exception:
    pass


if __name__ == "__main__":
    launch(HelpAgent)
