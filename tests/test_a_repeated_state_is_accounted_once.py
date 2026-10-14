# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""A simulation state handled twice is accounted once.

The simulation engine reads the state from shared memory. On a stale queue frame it reads whatever the memory holds
(engines/simulation.py, "SHM /state size mismatch ... reading actual"), so main can handle the same state twice.
In the simulation nothing dedupes a fill, so accounting every state handled would count the repeated state's fills
twice. The scoring child drops a repeat at or below its INIT base and applies a later one, so after a restart, when
the repeat is the INIT's base state, main would count it twice and the child once, and the two would diverge on
debeta_cap until a re-INIT.

Both sides call update_trade_volumes, so it now skips an exact repeat of the last accounted timestamp in the
simulation. Exchange mode is unchanged: it dedupes fills by trade id, and two exchange states may share a time."""
from types import SimpleNamespace

import msgpack
import test_scoring_shadow as tss
import test_shadow_snapshot_completeness as tsc
from taos.im.validator.scoring_shadow import (
    apply_state_bytes,
    compute_parity_components,
    compute_parity_digest,
)
from taos.im.validator.trade import update_trade_volumes

_S = tss._S


def _raw(st):
    return msgpack.packb({"timestamp": st.timestamp, "books": st.books, "accounts": {}, "notices": st.notices},
                         use_bin_type=True)


def test_a_state_handled_twice_is_accounted_once():
    once, twice = tss._fresh_main(), tss._fresh_main()
    for st in tsc._debeta_rounds(1, 4):
        update_trade_volumes(once, st)
        update_trade_volumes(twice, st)
        update_trade_volumes(twice, st)
    assert compute_parity_components(twice) == compute_parity_components(once)


def test_a_repeat_of_the_init_base_leaves_main_and_the_child_in_parity():
    """Main handles the base state, INITs, then handles the same state again; the child drops the repeat (its
    `ts <= base_ts` rule) and both then apply the same rounds."""
    rounds = tsc._debeta_rounds(1, 10)
    main = tss._fresh_main()
    for st in rounds[:5]:
        update_trade_volumes(main, st)
    child = tss._snapshot_to_shadow(main)
    update_trade_volumes(main, rounds[4])
    for st in rounds[5:]:
        update_trade_volumes(main, st)
        apply_state_bytes(child, _raw(st))
        assert compute_parity_digest(child) == compute_parity_digest(main), f"diverged at ts={st.timestamp}"


def test_a_repeat_after_the_base_is_skipped_by_both_sides():
    rounds = tsc._debeta_rounds(1, 10)
    main = tss._fresh_main()
    for st in rounds[:3]:
        update_trade_volumes(main, st)
    child = tss._snapshot_to_shadow(main)
    for i, st in enumerate(rounds[3:]):
        for _ in range(2 if i == 1 else 1):
            update_trade_volumes(main, st)
            apply_state_bytes(child, _raw(st))
    assert compute_parity_digest(child) == compute_parity_digest(main)
    ref = tss._fresh_main()
    for st in rounds:
        update_trade_volumes(ref, st)
    assert compute_parity_components(main) == compute_parity_components(ref)


def test_the_exchange_still_accounts_two_states_that_share_a_time():
    main = tss._fresh_main()
    main.engine = SimpleNamespace(mode="exchange")
    update_trade_volumes(main, tsc._fill_state(3 * _S, [(1, 2, 0, 1.0, 100.0, 0)]))
    update_trade_volumes(main, tsc._fill_state(3 * _S, [(1, 2, 0, 2.0, 101.0, 0)]))
    assert [t.p for t in main.recent_trades[0]] == [100.0, 101.0], main.recent_trades[0]


def test_the_child_starts_from_mains_last_accounted_time():
    """The INIT carries main's last accounted time, so a replica handed the base state again skips it as main does."""
    import inspect

    from taos.im.validator.scoring_shadow import ScoringShadow

    assert '"_last_accounted_ts"' in inspect.getsource(ScoringShadow.send_init)
    main = tss._fresh_main()
    for st in tsc._debeta_rounds(1, 3):
        update_trade_volumes(main, st)
    child = tss._snapshot_to_shadow(main)
    assert child._last_accounted_ts == 3 * _S
    update_trade_volumes(child, tsc._debeta_rounds(3, 1)[0])
    assert compute_parity_digest(child) == compute_parity_digest(main)
