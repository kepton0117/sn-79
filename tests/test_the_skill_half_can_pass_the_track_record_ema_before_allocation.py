# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""scoring.debeta.skill_share_ema: the skill half's share can pass the same track-record EMA as the making half.

Under the proportional_both pool the making term and share are smoothed by apply_track_record_ema BEFORE
allocation, so a later bad window costs standing gradually. With the dial off the skill half is allocated from the
round's own share; with it on, the skill half's share is smoothed on the same schedule before allocation. The
default is off."""
import inspect

import torch

from taos.im.validator import reward
from taos.im.validator.reward import pool_pay_vector, smoothed_skill_share

S = 1_000_000_000
UIDS = [0, 1, 2, 3]
HL = 3600 * S


def _detail(skilled):
    """Two uids with positive skill on enough books; `skilled` names who holds the net alpha this round."""
    out = {}
    for u in UIDS:
        on = u in skilled
        out[u] = {"skill_raw": 1.0 if on else 0.0, "skill_net_alpha": 10.0 if on else 0.0,
                  "skill_books": 30 if on else 0, "skill_p11_factor": 1.0, "present": True}
    return out


def _round(dcfg, detail, ema, ts, n_snap, last):
    return smoothed_skill_share(dcfg, detail, UIDS, None, ema, [], ts, HL, n_snap, last, set(UIDS))


def test_the_dial_off_returns_none_so_the_round_share_pays_as_before():
    assert _round({"skill_share_ema": 0, "skill_min_books": 20}, _detail({0}), {}, 1 * S, {}, None) is None


def test_with_the_dial_on_a_window_without_skill_costs_standing_gradually():
    dcfg = {"skill_share_ema": 1, "skill_min_books": 20}
    ema, n = {}, {}
    first = _round(dcfg, _detail({0, 1}), ema, 1 * S, dict(n), None)
    for u in (0, 1):
        n[u] = n.get(u, 0) + 1
    assert first[0] > 0 and first[1] > 0
    # uid 1's skill vanishes in the next window: the raw share would drop it to zero at once
    second = _round(dcfg, _detail({0}), ema, 2 * S + HL, dict(n), 1 * S)
    assert second[1] > 0.0, "standing earned over earlier windows is lost gradually, not in one window"
    assert second[1] < first[1], "and it is lost"
    assert "skill" in ema and ema["skill"], "the smoothed share carries under pool_ema['skill']"


def test_pool_pay_vector_pays_the_smoothed_skill_share_it_is_given():
    detail = _detail({0})
    common = dict(pool_mode="proportional_both", pool=0.5, detail=detail, all_uids=UIDS,
                  ladder_scores={u: 1.0 for u in UIDS}, trading_scores={u: 1.0 for u in UIDS},
                  making_share={u: 0.25 for u in UIDS}, config={"rewarding": {"seed": 1, "pareto": {"shape": 1.0, "scale": 1.0}}},
                  skill_min_books=20)
    raw = pool_pay_vector(**common)
    smoothed = pool_pay_vector(**common, skill_share={0: 0.6, 1: 0.4, 2: 0.0, 3: 0.0})
    assert isinstance(raw, torch.Tensor) and isinstance(smoothed, torch.Tensor)
    assert float(smoothed[1]) > float(raw[1]), "uid 1 is paid from the smoothed share it was given"


def test_main_and_the_scoring_child_use_the_one_helper():
    from taos.im.validator import scoring_shadow
    assert "smoothed_skill_share(" in inspect.getsource(reward.get_rewards)
    assert "smoothed_skill_share(" in inspect.getsource(scoring_shadow.shadow_score)


def test_the_dial_ships_off():
    from taos.im import config as im_config
    src = inspect.getsource(im_config.add_im_validator_args)
    i = src.index('"--scoring.debeta.skill_share_ema"')
    assert "default=0" in src[i:i + 900]
