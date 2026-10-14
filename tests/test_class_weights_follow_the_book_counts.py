"""Asset-class weights from the layout's book counts (0.6.4): with
``scoring.debeta.class_weights = books`` each class weighs its share of the layout's books, 96 and 32 books giving 0.75
and 0.25, so the weight follows the layout instead of a hand-set dial. The setting resolves against the class map the
scorer already reads, so main and the scoring child read the same weights."""
from taos.im.config import add_im_validator_args
from taos.im.validator import reward


def _cmap(n0=96, n1=32):
    return {**{b: 0 for b in range(n0)}, **{b: 1 for b in range(n0, n0 + n1)}}


def test_books_weighs_each_class_by_its_share_of_the_books():
    assert reward.class_weights_for_layout("books", _cmap()) == [0.75, 0.25]
    assert reward.class_weights_for_layout(" Books ", _cmap(100, 28)) == [100 / 128, 28 / 128]


def test_books_on_a_single_market_or_without_a_map_is_the_flat_rule():
    assert reward.class_weights_for_layout("books", {b: 0 for b in range(128)}) == []
    assert reward.class_weights_for_layout("books", {}) == []


def test_explicit_weights_keep_their_meaning():
    assert reward.class_weights_for_layout("0.95,0.05", _cmap()) == [0.95, 0.05]


def test_the_published_config_carries_books_rather_than_failing_to_parse_it():
    assert reward.class_weights_published("books") == "books"
    assert reward.class_weights_published("0.95,0.05") == [0.95, 0.05]


def test_the_shipped_default_is_books():
    import argparse
    p = argparse.ArgumentParser(add_help=False)
    add_im_validator_args(None, p)
    assert p.get_default("scoring.debeta.class_weights") == "books"
