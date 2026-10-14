# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The scoring child applies the state that triggered a run change AFTER the shift, as main does.

Two run changes are detected while main handles a state rather than from a start event: the first update of a
freshly started validator whose engine opened (or resumed) a run behind the clock the validator saved ("RUN CHANGE
NOT SEEN"), and a clock that steps back on the run in progress ("RESUME NOT SEEN"). In both, the listener tees the
raw state to the child BEFORE main's tick handling notices the change, so the child sees that state first and the
shift second, while main shifts first and then applies the state.

For example, a validator that sent the child its INIT at base_ts 1362 s receives a first state at 1201 s: the
child would drop it under its old-run guard (ts <= base_ts) and then shift, while main shifts and applies it, and
the two would differ by the realized-P&L entries of that one state and by the prune clock until a re-INIT. The child now holds a state that steps back
until the next frame tells it what main did with it: after a shift it is applied on the new time base; otherwise
it is applied (or, at or below the INIT base, dropped) exactly as before."""
import time

from taos.im.validator.scoring_shadow import ScoringShadow, compute_parity_components, parity_vectors
from taos.im.validator.trade import shift_simulation_histories, update_trade_volumes

from test_scoring_shadow import _S, _BOOKS, _UIDS, _fresh_main, _raw_state, _state


def _shift(main, old_ts, new_ts):
    shift_simulation_histories(
        main, old_ts, new_ts,
        book_count=_BOOKS, volume_decimals=4, lookback=main.config.scoring.kappa.lookback,
        volume_assessment_period=main.config.scoring.activity.trade_volume_assessment_period,
        miner_wealth=50000.0, effective_max_uids=_UIDS,
    )


def _settle(shadow, ts, main, timeout=20.0):
    shadow.record_main_digest(ts, (compute_parity_components(main), parity_vectors(main)))   # as main records it
    end = time.time() + timeout
    while time.time() < end and shadow.matches + shadow.mismatches == 0:
        time.sleep(0.1)


def _run(old_run, trigger, after):
    """INIT on old_run's last state, then a trigger state behind it, the shift main applies on noticing it, and
    the states after it up to a parity boundary. Returns the shadow's match and mismatch counts."""
    main = _fresh_main()
    for ts, trades in old_run:
        update_trade_volumes(main, _state(ts, trades))
    main._shadow_applied_ts = old_run[-1][0]

    shadow = ScoringShadow(parity_ns=5 * _S)
    shadow.start()
    try:
        shadow.send_init(main).result(timeout=60)
        # the listener tees the trigger before main's tick handling sees the clock step back
        t_ts, t_trades = trigger
        shadow.tee(_raw_state(t_ts, t_trades), t_ts)
        # main notices it, shifts from its clock onto the new run's start, tells the child, then applies it
        shadow.emit_sim_start(old_run[-1][0], 0)
        _shift(main, old_run[-1][0], 0)
        update_trade_volumes(main, _state(t_ts, t_trades))
        for ts, trades in after:
            update_trade_volumes(main, _state(ts, trades))
            shadow.tee(_raw_state(ts, trades), ts)
        _settle(shadow, after[-1][0], main)
        return shadow.matches, shadow.mismatches
    finally:
        shadow.stop()


OLD_RUN = [(ts * _S, [(1 + ts % 3, 2, ts % 2, 1.0 + ts / 10, 100.0 + ts, ts % 2)]) for ts in range(6, 13)]


def test_a_first_state_behind_the_init_base_is_applied_after_the_shift():
    """RUN CHANGE NOT SEEN: the trigger (2 s) sits below the INIT base (12 s)."""
    matches, mismatches = _run(OLD_RUN, (2 * _S, [(2, 1, 0, 1.5, 101.0, 1)]),
                               [(3 * _S, [(3, 1, 1, 2.0, 55.0, 0)]), (4 * _S, []), (5 * _S, [(1, 3, 1, 2.0, 56.0, 1)])])
    assert (matches, mismatches) == (1, 0), f"parity at 5 s after the run change: {matches} match, {mismatches} mismatch"


def test_a_state_that_steps_back_above_the_init_base_is_applied_after_the_shift():
    """RESUME NOT SEEN: INIT at 6 s, the run continues to 9 s, then the clock steps back to 7 s, behind main's
    clock but above the INIT base, and main re-opens the run on the resumed clock. The first parity boundary
    after INIT (10 s) falls after the step back, so the comparison is unambiguous."""
    old = OLD_RUN[:4]                      # 6, 7, 8, 9 s
    main = _fresh_main()
    update_trade_volumes(main, _state(*old[0]))
    main._shadow_applied_ts = old[0][0]
    shadow = ScoringShadow(parity_ns=5 * _S)
    shadow.start()
    try:
        shadow.send_init(main).result(timeout=60)
        for ts, trades in old[1:]:
            update_trade_volumes(main, _state(ts, trades))
            shadow.tee(_raw_state(ts, trades), ts)
        trig = (7 * _S, [(2, 3, 1, 1.0, 90.0, 0)])
        shadow.tee(_raw_state(*trig), trig[0])
        shadow.emit_sim_start(old[-1][0], trig[0])
        _shift(main, old[-1][0], trig[0])
        update_trade_volumes(main, _state(*trig))
        for ts, trades in ((8 * _S, [(1, 2, 0, 1.0, 99.0, 1)]), (9 * _S, []), (10 * _S, [(3, 1, 1, 1.0, 98.0, 0)])):
            update_trade_volumes(main, _state(ts, trades))
            shadow.tee(_raw_state(ts, trades), ts)
        _settle(shadow, 10 * _S, main)
        assert (shadow.matches, shadow.mismatches) == (1, 0), (shadow.matches, shadow.mismatches)
    finally:
        shadow.stop()


def test_a_state_at_the_init_base_with_no_run_change_is_still_dropped():
    """The old-run guard keeps its job: a resent state at or below the INIT base, with no shift after it, is
    already in the snapshot and must not be applied twice."""
    main = _fresh_main()
    for ts, trades in OLD_RUN:
        update_trade_volumes(main, _state(ts, trades))
    main._shadow_applied_ts = OLD_RUN[-1][0]
    shadow = ScoringShadow(parity_ns=5 * _S)
    shadow.start()
    try:
        shadow.send_init(main).result(timeout=60)
        ts, trades = OLD_RUN[-1]
        shadow.tee(_raw_state(ts, trades), ts)          # the engine resends the last state
        for nts in (13 * _S, 14 * _S, 15 * _S):
            update_trade_volumes(main, _state(nts, []))
            shadow.tee(_raw_state(nts, []), nts)
        _settle(shadow, 15 * _S, main)
        assert (shadow.matches, shadow.mismatches) == (1, 0), (shadow.matches, shadow.mismatches)
    finally:
        shadow.stop()
