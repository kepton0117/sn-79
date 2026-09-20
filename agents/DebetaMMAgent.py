# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""
Simple de-beta-oriented market maker: each tick, per book, cancel stale quotes and
place two maker limits at mid - n*tick and mid + n*tick.

Defaults: quantity=1, max_books=128. Intended as a minimal baseline for testing
spread capture (making leg) across the full book set when validators enable de-beta.
"""
import bittensor as bt
from taos.common.agents import launch
from taos.im.agents import GenTRXAgent
from taos.im.protocol.models import OrderDirection, STP, TimeInForce, LoanSettlementOption


class DebetaMMAgent(GenTRXAgent):
    """Symmetric mid ± n*tick quotes on up to 128 books."""

    def initialize(self):
        if not hasattr(self.config, "gtx_training_enabled"):
            self.config.gtx_training_enabled = False
        if not hasattr(self.config, "gtx_collect_data"):
            self.config.gtx_collect_data = False
        super().initialize()
        self.quantity = float(getattr(self.config, "quantity", 1.0))
        self.n_ticks = int(getattr(self.config, "n_ticks", 1))
        self.max_books = int(getattr(self.config, "max_books", 128))
        self.expiry_period = int(self.config.expiry_period)
        self.max_fee_rate = float(getattr(self.config, "max_fee_rate", 0.005))
        self.cancel_before_quote = bool(getattr(self.config, "cancel_before_quote", True))
        self.spread_fallback = float(getattr(self.config, "spread_fallback", 0.001))

    def _tick_size(self, price_decimals: int) -> float:
        return 10.0 ** (-price_decimals)

    def _quote_prices(self, bid: float, ask: float, tick: float, price_decimals: int):
        if bid <= 0 or ask <= 0:
            return None, None
        if bid == ask:
            mid = bid
            half = max(tick, mid * self.spread_fallback)
            buy_px = round(mid - half, price_decimals)
            sell_px = round(mid + half, price_decimals)
        else:
            mid = 0.5 * (bid + ask)
            offset = self.n_ticks * tick
            buy_px = round(mid - offset, price_decimals)
            sell_px = round(mid + offset, price_decimals)
            buy_px = min(buy_px, round(ask - tick, price_decimals))
            sell_px = max(sell_px, round(bid + tick, price_decimals))
        if buy_px <= 0 or buy_px >= sell_px:
            return None, None
        return buy_px, sell_px

    def _cancel_resting(self, response, book_id: int) -> None:
        orders = getattr(self.accounts.get(book_id), "orders", None) or []
        if not orders:
            return
        ids = [o.id for o in orders]
        if len(ids) == 1:
            response.cancel_order(book_id, ids[0])
        else:
            response.cancel_orders(book_id, ids)

    def respond_simulation(self, state):
        price_decimals = getattr(self.simulation_config, "priceDecimals", 8)
        volume_decimals = getattr(self.simulation_config, "volumeDecimals", 8)
        tick = self._tick_size(price_decimals)
        qty = round(self.quantity, volume_decimals)
        if qty <= 0:
            return self.make_response()

        response = self.make_response()
        books = state.books or {}
        book_ids = sorted(books.keys())[: self.max_books]

        for book_id in book_ids:
            if book_id not in self.accounts:
                continue
            book = books[book_id]
            bids = book.bids if hasattr(book, "bids") else []
            asks = book.asks if hasattr(book, "asks") else []
            if bids and asks:
                bid, ask = bids[0].price, asks[0].price
            elif bids:
                bid = ask = bids[0].price
            elif asks:
                bid = ask = asks[0].price
            else:
                continue

            acct = self.accounts[book_id]
            if acct.fees.maker_fee_rate > self.max_fee_rate:
                continue

            buy_px, sell_px = self._quote_prices(bid, ask, tick, price_decimals)
            if buy_px is None:
                continue

            if self.cancel_before_quote and acct.orders:
                self._cancel_resting(response, book_id)

            if acct.quote_balance.free >= qty * buy_px:
                response.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.BUY,
                    quantity=qty,
                    price=buy_px,
                    stp=STP.CANCEL_BOTH,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=self.expiry_period,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )
            if acct.base_balance.free >= qty:
                response.limit_order(
                    book_id=book_id,
                    direction=OrderDirection.SELL,
                    quantity=qty,
                    price=sell_px,
                    stp=STP.CANCEL_BOTH,
                    timeInForce=TimeInForce.GTT,
                    expiryPeriod=self.expiry_period,
                    leverage=0.0,
                    settlement_option=LoanSettlementOption.NONE,
                )

        return response


if __name__ == "__main__":
    """
    Mainnet deploy (scores from trades.csv after you have fills):

      ./run_miner.sh -n DebetaMMAgent -m "quantity=1 max_books=128 n_ticks=10 expiry_period=200000000000 max_fee_rate=0.005"

      # or mainnet (matches other GOLD miners):
      ./mainnet/run_debeta_mm.sh

      python scripts/debeta_score_mainnet.py --uid YOUR_UID

    BookManageBot Books tab also shows d-buy/d-sell/d-mtm/d-α when trades.csv exists.
    Validator de-beta emissions apply only when validators enable --scoring.debeta.enabled;
    until then dashboard shows legacy κ + PnL — local script still shows de-beta preview.
    """
    launch(DebetaMMAgent)
