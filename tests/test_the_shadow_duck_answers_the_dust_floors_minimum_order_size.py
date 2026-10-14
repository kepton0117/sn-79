# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The dust floor (0.6.4) reads each book's minimum order size off the simulation configuration, through
config_for_book: a print below it is a residual and does not vote the floor's reference. The scoring shadow re-scores
from a simulation DUCK that carried only volumeDecimals and miner_wealth, so inside the child _min_order_size took its
0.0 fallback on every book while main read the real minimum: on a two-class layout the two sides built different
floors from the same prints, and the divergence would have surfaced as a parity mismatch, read as a shadow fault.
The duck now answers config_for_book with the book's minimum order size, for every book the layout names, and the
shadow and main read the same bar."""
from types import SimpleNamespace

from taos.im.validator.scoring_shadow import _simulation_duck
from taos.im.validator.trade import _min_order_size


class _Sim:
    """A two-class layout: books 0 to 95 at one minimum, 96 to 127 at another, as the testnet configuration."""
    volumeDecimals = 4
    miner_wealth = 1_000_000.0
    book_count = 128

    @property
    def book_ids(self):
        return list(range(128))

    def config_for_book(self, book_id):
        return SimpleNamespace(min_order_size=0.5 if int(book_id) < 96 else 2.5364, volumeDecimals=4)


def test_the_duck_answers_the_minimum_order_size_main_reads_on_every_book():
    sim = _Sim()
    duck = _simulation_duck(sim)
    assert duck.volumeDecimals == 4 and duck.miner_wealth == 1_000_000.0, "what the duck carried before is unchanged"
    for b in (0, 17, 95, 96, 127):
        assert _min_order_size(SimpleNamespace(simulation=duck), b) == _min_order_size(SimpleNamespace(simulation=sim), b), b
    assert _min_order_size(SimpleNamespace(simulation=duck), 96) == 2.5364


def test_a_single_market_layout_without_a_minimum_answers_zero_on_both_sides():
    class _Single:
        volumeDecimals = 4
        miner_wealth = 1.0
        book_count = 4

        def config_for_book(self, book_id):
            return SimpleNamespace(min_order_size=0.0)
    sim = _Single()
    duck = _simulation_duck(sim)
    for b in range(4):
        assert _min_order_size(SimpleNamespace(simulation=duck), b) == _min_order_size(SimpleNamespace(simulation=sim), b) == 0.0


def test_the_duck_still_ships_as_plain_data():
    # the INIT frame to the scoring child is pickled; a lookup held as a lambda on a namespace would not survive it
    import pickle
    duck = pickle.loads(pickle.dumps(_simulation_duck(_Sim()), protocol=5))
    assert duck.config_for_book(127).min_order_size == 2.5364 and duck.config_for_book(5).min_order_size == 0.5
    assert duck.volumeDecimals == 4


def test_a_layout_that_cannot_say_leaves_the_duck_answering_zero_like_main():
    sim = SimpleNamespace(volumeDecimals=4, miner_wealth=1.0)   # no config_for_book at all
    duck = _simulation_duck(sim)
    assert _min_order_size(SimpleNamespace(simulation=duck), 3) == _min_order_size(SimpleNamespace(simulation=sim), 3) == 0.0
