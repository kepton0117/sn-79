# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The exchange validator reads a miner's reply through the same bounded path as the simulation validator.

validate_responses serves both engines, so every state synapse it parses reads a reply under the bounds of
utils.compress.decode_reply: the exchange synapse as well as the simulation one.
"""
import pytest

from taos.im.protocol import MarketSimulationStateUpdate
from taos.im.protocol.exchange import ExchangeStateUpdate
from taos.im.protocol.exchange.response import ExchangeAgentResponse
from taos.im.protocol.models import OrderDirection

ENGINES = ("lz4", "zlib", "zstd")
VERSION = 63
DELEGATE = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


def _stock_exchange_reply(engine: str) -> ExchangeStateUpdate:
    """What a stock exchange miner sends back: the request with its inputs cleared and its response compressed."""
    syn = ExchangeStateUpdate(timestamp=1, block=1, version=VERSION, compression_engine=engine)
    syn.response = ExchangeAgentResponse(agent_id=7)
    for b in range(2):
        syn.response.limit_order(book_id=b, delegate=DELEGATE, direction=OrderDirection.BUY, quantity=1.5, price=0.01)
        syn.response.market_order(book_id=b, delegate=DELEGATE, direction=OrderDirection.SELL, quantity=2.0,
                                  max_slippage=0.05)
    return syn.clear_inputs().compress()


@pytest.mark.parametrize("cls", [MarketSimulationStateUpdate, ExchangeStateUpdate])
def test_every_state_synapse_the_query_loop_parses_reads_a_reply_under_the_bound(cls):
    assert callable(getattr(cls, "decompress_reply", None)), f"{cls.__name__} has no bounded reply reader"


@pytest.mark.parametrize("engine", ENGINES)
def test_a_stock_exchange_miner_reply_is_read(engine):
    reply = _stock_exchange_reply(engine)
    assert isinstance(reply.compressed, dict), "the stock miner sends the two-part container"
    refused = reply.decompress_reply(version=VERSION, max_bytes=1 << 20, max_instructions=1_000)
    assert refused is None, refused
    assert reply.compressed is None
    assert reply.response.agent_id == 7 and len(reply.response.instructions) == 4
    assert reply.pools is None and reply.books is None and reply.notices is None


def test_an_exchange_reply_over_the_instruction_cap_is_refused():
    reply = _stock_exchange_reply("lz4")
    refused = reply.decompress_reply(version=VERSION, max_bytes=1 << 20, max_instructions=3)
    assert refused and "instructions" in refused, refused
    assert reply.response is None
