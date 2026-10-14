# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""A miner's reply is read under a bound on every size the validator spends memory on.

The validator decodes a reply's base64 text, decompresses it and decodes the msgpack before it knows how many
instructions the reply carries. Each step is bounded: the text length before decoding, the decompressed size
while decompressing (whatever size a zstd frame declares), the instruction count while the msgpack is read and
before any model is built, and the books part of the container, which a stock miner sends empty. Only the
reply's response is decoded; its other fields are skipped unread.
"""
import base64
import inspect
import tracemalloc
import zlib
from pathlib import Path

import lz4.frame
import msgspec
import pytest
import zstandard as zstd

from taos.im.protocol import MarketSimulationStateUpdate
from taos.im.protocol.models import OrderDirection
from taos.im.protocol.response import FinanceAgentResponse
from taos.im.utils import compress as cmod

DEV = Path(__file__).resolve().parents[1]
ENGINES = ("lz4", "zlib", "zstd")
VERSION = 63
MAX_BYTES = 1 << 20
MAX_INSTRUCTIONS = 1_000


def _raw_compress(raw: bytes, engine: str) -> bytes:
    if engine == "zlib":
        return zlib.compress(raw, 9)
    if engine == "lz4":
        return lz4.frame.compress(raw)
    return zstd.ZstdCompressor(level=3).compress(raw)


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def _container(payload: dict, engine: str, books=None) -> dict:
    return {
        "books": _b64(_raw_compress(msgspec.msgpack.encode(books), engine)),
        "payload": _b64(_raw_compress(msgspec.msgpack.encode(payload), engine)),
    }


def _stock_reply(engine: str, n_books: int = 3) -> MarketSimulationStateUpdate:
    """What a stock miner sends back: the request with its inputs cleared and its response compressed."""
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine=engine)
    syn.response = FinanceAgentResponse(agent_id=7)
    for b in range(n_books):
        syn.response.limit_order(book_id=b, direction=OrderDirection.BUY, quantity=1.5, price=299.5)
        syn.response.market_order(book_id=b, direction=OrderDirection.SELL, quantity=2.0)
    return syn.clear_inputs().compress()


@pytest.mark.parametrize("engine", ENGINES)
def test_a_stock_miner_reply_is_read(engine):
    reply = _stock_reply(engine)
    assert isinstance(reply.compressed, dict), "the stock miner sends the two-part container"
    refused = reply.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS)
    assert refused is None, refused
    assert reply.compressed is None
    assert reply.response.agent_id == 7
    assert [i.type for i in reply.response.instructions] == ["PLACE_ORDER_LIMIT", "PLACE_ORDER_MARKET"] * 3


@pytest.mark.parametrize("engine", ENGINES)
def test_a_reply_that_decompresses_past_the_bound_is_refused_without_a_large_allocation(engine):
    """64 MB of padding compresses to a few kB in every engine; zstd also declares the 64 MB in its frame."""
    padded = {"response": {"agent_id": 7, "instructions": []}, "pad": b"\0" * (64 << 20)}
    payload = _container(padded, engine)
    assert len(payload["payload"]) < 400_000
    tracemalloc.start()
    try:
        with pytest.raises(cmod.ReplyRefused, match="decompress"):
            cmod.decode_reply(payload, engine=engine, version=VERSION, max_bytes=MAX_BYTES,
                              max_instructions=MAX_INSTRUCTIONS)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * MAX_BYTES, f"{engine}: peak {peak / 1e6:.1f} MB for a reply bounded at {MAX_BYTES / 1e6:.1f} MB"


@pytest.mark.parametrize("engine", ENGINES)
def test_an_instruction_list_over_the_cap_is_refused_before_any_model_is_built(engine, monkeypatch):
    """The count is read off the msgpack array header; the response is never decoded into objects."""
    built = []
    real = cmod._decode_response
    monkeypatch.setattr(cmod, "_decode_response", lambda *a, **k: (built.append(1), real(*a, **k))[1])
    one = {"y": "PLACE_ORDER_MARKET", "agentId": 7, "bookId": 0, "direction": 0, "quantity": 1.0}
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine=engine)
    syn.compressed = _container({"response": {"agent_id": 7, "instructions": [one] * (MAX_INSTRUCTIONS + 1)}}, engine)
    refused = syn.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS)
    assert refused and "instructions" in refused, refused
    assert syn.response is None and not built


def test_the_books_part_of_a_reply_must_be_empty():
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine="lz4")
    syn.compressed = _container({"response": {"agent_id": 7, "instructions": []}}, "lz4",
                                books={0: {"bids": [[1.0, 1.0]] * 50}})
    refused = syn.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS)
    assert refused and "books" in refused, refused


def test_text_over_the_bound_is_refused_before_it_is_decoded(monkeypatch):
    def no_decode(*a, **k):
        raise AssertionError("base64 decoded before its length was checked")
    monkeypatch.setattr(cmod.pybase64, "b64decode", no_decode)
    with pytest.raises(cmod.ReplyRefused, match="encoded"):
        cmod.decode_reply({"books": None, "payload": "A" * (4 * MAX_BYTES)}, engine="lz4", version=VERSION,
                          max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS)


def test_an_unknown_engine_is_refused():
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine="brotli")
    syn.compressed = _container({"response": {"agent_id": 7, "instructions": []}}, "lz4")
    refused = syn.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS)
    assert refused and "engine" in refused, refused


def test_only_the_response_is_decoded():
    """Notices, accounts and config in a reply are skipped: the validator never uses them from a miner."""
    junk = {str(i): [{"y": "x"}] * 50 for i in range(200)}
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine="zstd")
    syn.compressed = _container({"response": {"agent_id": 7, "instructions": []}, "notices": junk,
                                 "accounts": junk, "config": "x" * 1000}, "zstd")
    assert syn.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS) is None
    assert syn.response.agent_id == 7
    assert syn.notices is None and syn.accounts is None and syn.config is None and syn.books is None


def test_a_reply_with_no_response_is_read_as_none():
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine="lz4")
    syn.compressed = _container({"response": None}, "lz4")
    assert syn.decompress_reply(version=VERSION, max_bytes=MAX_BYTES, max_instructions=MAX_INSTRUCTIONS) is None
    assert syn.response is None and syn.compressed is None


def test_the_reply_bound_scales_with_the_instruction_cap():
    instr, size = cmod.reply_bounds(book_count=128, max_instructions_per_book=100)
    assert instr == 128 * (100 + cmod.REPLY_OVERSUBMIT_PER_BOOK)
    assert instr * 260 < size < 16 << 20, "room for every instruction at its largest encoding, and no more"


def _over_limit_reply(engine, per_book, n_books=128):
    syn = MarketSimulationStateUpdate(timestamp=1, version=VERSION, compression_engine=engine)
    syn.response = FinanceAgentResponse(agent_id=7)
    for b in range(n_books):
        for _ in range(per_book):
            syn.response.market_order(book_id=b, direction=OrderDirection.SELL, quantity=2.0)
    return syn.clear_inputs().compress()


@pytest.mark.parametrize("engine", ENGINES)
def test_a_reply_over_the_per_book_limit_is_read_whole_for_the_query_loop_to_trim(engine):
    """0.6.3 read an over-limit reply and kept the first max_instructions_per_book per book; the bound keeps that. The
    example OrderOptionAgent sends about 26 per book (order options demonstrated per book), over the default 5."""
    instr, size = cmod.reply_bounds(book_count=128, max_instructions_per_book=5)
    syn = _over_limit_reply(engine, 26)
    assert syn.decompress_reply(version=VERSION, max_bytes=size, max_instructions=instr) is None
    assert len(syn.response.instructions) == 26 * 128


@pytest.mark.parametrize("engine", ENGINES)
def test_a_reply_past_the_oversubmit_allowance_is_refused(engine):
    instr, size = cmod.reply_bounds(book_count=128, max_instructions_per_book=5)
    syn = _over_limit_reply(engine, 5 + cmod.REPLY_OVERSUBMIT_PER_BOOK + 1)
    refused = syn.decompress_reply(version=VERSION, max_bytes=size, max_instructions=instr)
    assert refused and syn.response is None, refused


def test_the_query_loop_reads_replies_through_the_bound():
    src = (DEV / "taos" / "im" / "validator" / "query.py").read_text()
    body = src[src.index("def validate_responses"):]
    body = body[:body.index("\n    def ", 10)]
    assert "decompress_reply(" in body and "reply_bounds(" in body
    assert "synapse.decompress()" not in body
    assert "decompress_reply" in inspect.getsource(MarketSimulationStateUpdate)
