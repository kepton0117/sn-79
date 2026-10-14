# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""A miner that reads the simulation state lazily gets each book's maker-taker ratio from `book.MTR`, as an eager
reader does. The lazy book never fills the model's fields, so the accessor reads the wire dict, and a book whose wire
dict carries no ratio reads None."""
from taos.im.protocol.lazy import LazyBooks
from taos.im.protocol.models import Book

RAW = {"i": 0, "r": 0.4, "b": [{"p": 300.0, "q": 1.5, "o": None}], "a": [{"p": 300.5, "q": 2.0, "o": None}], "e": []}


def test_a_lazy_book_reads_its_ratio_from_the_wire():
    book = LazyBooks({0: RAW})[0]
    assert book.MTR == 0.4
    assert book.parse().MTR == 0.4
    assert Book.model_validate(RAW).MTR == book.MTR, "the eager and the lazy reader agree"


def test_a_lazy_book_without_a_ratio_reads_none():
    raw = {k: v for k, v in RAW.items() if k != "r"}
    assert LazyBooks({0: raw})[0].MTR is None


def test_a_miner_reading_lazily_sees_every_books_ratio():
    from taos import __spec_version__
    from taos.im.protocol import MarketSimulationStateUpdate
    from taos.im.utils.compress import compress

    books = {0: dict(RAW), 1: dict(RAW, i=1, r=0.25)}
    synapse = MarketSimulationStateUpdate.parse_dict({"timestamp": 1, "books": books})
    object.__setattr__(synapse, "accounts", {5: {}})
    object.__setattr__(synapse, "notices", {5: []})
    object.__setattr__(synapse, "config", None)
    synapse.version = __spec_version__
    wire = synapse.compress(level=1, engine="lz4",
                            compressed_books=compress(books, level=1, engine="lz4", version=__spec_version__))
    received = MarketSimulationStateUpdate(**wire.model_dump())
    received.decompress(lazy=True)
    assert {b: received.books[b].MTR for b in (0, 1)} == {0: 0.4, 1: 0.25}
