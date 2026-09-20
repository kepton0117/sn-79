"""FIFO bootstrap from validator account inventory."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from fifo_pnl import ValidatorStylePnl
from fifo_state_recorder import maybe_bootstrap_fifo_from_accounts


@dataclass
class Balance:
    total: float
    initial: float


@dataclass
class Account:
    base_balance: Balance
    base_loan: float = 0.0
    base_collateral: float = 0.0


class FakeAgent:
    fifo_bootstrap = True
    simulation_config = SimpleNamespace(min_order_size=0.25)


def _agent(*, accounts: dict, open_books: dict | None = None) -> FakeAgent:
    agent = FakeAgent()
    agent.accounts = accounts
    agent.open_books = open_books or {}
    return agent


def _state(book_id: int, bid: float, ask: float) -> SimpleNamespace:
    level_bid = SimpleNamespace(price=bid)
    level_ask = SimpleNamespace(price=ask)
    book = SimpleNamespace(bids=[level_bid], asks=[level_ask])
    return SimpleNamespace(timestamp=1_000, books={book_id: book})


def test_bootstrap_short_when_fifo_flat_and_validator_short():
    book_id = 5
    agent = _agent(
        accounts={
            book_id: Account(
                base_balance=Balance(total=100.0 - 44.782, initial=100.0)
            )
        },
    )
    fifo = ValidatorStylePnl(8)
    state = _state(book_id, bid=307.0, ask=307.01)

    n = maybe_bootstrap_fifo_from_accounts(agent, fifo, state)

    assert n == 1
    assert fifo.net_qty(book_id) == -44.782
    boot_long, boot_short = fifo.fifo_bootstrapped(book_id)
    assert not boot_long
    assert boot_short
    assert fifo.fifo_short_head_price(book_id) == 307.0


def test_bootstrap_long_when_fifo_flat_and_validator_long():
    book_id = 4
    agent = _agent(
        accounts={
            book_id: Account(
                base_balance=Balance(total=100.0 + 74.8, initial=100.0)
            )
        },
    )
    fifo = ValidatorStylePnl(8)
    state = _state(book_id, bid=275.5, ask=275.57)

    n = maybe_bootstrap_fifo_from_accounts(agent, fifo, state)

    assert n == 1
    assert abs(fifo.net_qty(book_id) - 74.8) < 1e-9
    assert fifo.fifo_bootstrapped(book_id) == (True, False)


def test_no_bootstrap_when_fifo_already_matches():
    book_id = 4
    agent = _agent(
        accounts={
            book_id: Account(
                base_balance=Balance(total=100.0 + 10.0, initial=100.0)
            )
        },
    )
    fifo = ValidatorStylePnl(8)
    fifo.seed_long(book_id, 100, 10.0, 275.0)
    state = _state(book_id, bid=275.5, ask=275.57)

    n = maybe_bootstrap_fifo_from_accounts(agent, fifo, state)

    assert n == 0
    assert fifo.net_qty(book_id) == 10.0
