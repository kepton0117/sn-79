# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The miner's book rebuild shares the levels an event left alone, and survives a dead worker.

A full copy of every level per event, inside a joblib worker, grows with events times levels, enough for a worker to
die under memory pressure, and its EOFError would take the whole update down. Each event's snapshot is a new top-level
dict whose untouched side and untouched levels are the previous snapshot's own objects, and only the level the event
moved is a new object: the history reads exactly the same (the consumer validates each snapshot into its own model
and never mutates these dicts) at a fraction of the memory. And a batch whose worker died is rebuilt in the calling
process rather than taking the update down with it."""
import concurrent.futures
import copy
import logging
import random

import taos.im.utils.history as hmod
from taos.im.utils.history import batch_history, history


def _reference_history(snapshot, events, volume_decimals):
    """The rebuild as it was: mutate one book and deep-copy it after every event."""
    out = {snapshot['timestamp']: copy.deepcopy(snapshot)}
    trades = {}
    leverage_by_id = {}
    for event in sorted(events, key=lambda x: x['t']):
        if event['y'] == 'o':
            lev = float(event.get('l') or 0.0)
            if lev > 0.0 and event.get('i') is not None:
                leverage_by_id[event['i']] = lev
            side = snapshot['bids'] if event['s'] == 0 else snapshot['asks']
            if event['p'] not in side:
                side[event['p']] = {'p': event['p'], 'q': 0.0, 'o': None}
            side[event['p']]['q'] = round(side[event['p']]['q'] + event['q'] * (1.0 + lev), volume_decimals)
        elif event['y'] == 't':
            trades[event['t']] = event
            side = snapshot['asks'] if event['s'] == 0 else snapshot['bids']
            if event['p'] in side:
                side[event['p']]['q'] = round(side[event['p']]['q'] - event['q'], volume_decimals)
                if side[event['p']]['q'] == 0.0:
                    del side[event['p']]
        elif event['y'] == 'c':
            s = event.get('s')
            on_ask = int(s) == 1 if s is not None else (bool(snapshot['asks']) and event['p'] >= min(snapshot['asks']))
            c_lev = event.get('l')
            removed = event['q'] * (1.0 + (float(c_lev) if c_lev is not None else leverage_by_id.get(event.get('i'), 0.0)))
            side = snapshot['asks'] if on_ask else snapshot['bids']
            if event['p'] in side:
                side[event['p']]['q'] = round(side[event['p']]['q'] - removed, volume_decimals)
                if side[event['p']]['q'] == 0.0:
                    del side[event['p']]
        snapshot['timestamp'] = event['t']
        out[event['t']] = copy.deepcopy(snapshot)
    return out, trades


def _snapshot(levels=40):
    bids = {round(100.0 - 0.1 * i, 2): {'p': round(100.0 - 0.1 * i, 2), 'q': 5.0 + i, 'o': None} for i in range(1, levels + 1)}
    asks = {round(100.0 + 0.1 * i, 2): {'p': round(100.0 + 0.1 * i, 2), 'q': 5.0 + i, 'o': None} for i in range(1, levels + 1)}
    return {'timestamp': 0, 'bids': bids, 'asks': asks, 'extra': 'kept'}


def _stream(seed, n=300):
    rng = random.Random(seed)
    out = []
    for k in range(1, n + 1):
        kind = rng.choice('ootc')
        p = round(100.0 + rng.choice([-1, 1]) * 0.1 * rng.randint(1, 45), 2)
        if kind == 'o':
            lev = rng.choice([0.0, 0.0, 0.0, 2.0])
            out.append({'y': 'o', 't': k, 'p': p, 'q': float(rng.randint(1, 4)), 's': 0 if p < 100 else 1, 'i': k, 'l': lev or None})
        elif kind == 't':
            out.append({'y': 't', 't': k, 'p': p, 'q': float(rng.randint(1, 8)), 's': 1 if p < 100 else 0})
        else:
            c = {'y': 'c', 't': k, 'p': p, 'q': float(rng.randint(1, 8)), 'i': rng.randint(1, k)}
            if rng.random() < 0.5:
                c['s'] = 0 if p < 100 else 1
            if rng.random() < 0.3:
                c['l'] = 2.0
            out.append(c)
    rng.shuffle(out)
    return out


def test_the_rebuild_reads_exactly_as_the_deep_copy_did():
    for seed in range(4):
        events = _stream(seed)
        got = history(copy.deepcopy(_snapshot()), copy.deepcopy(events), 4)
        want = _reference_history(copy.deepcopy(_snapshot()), copy.deepcopy(events), 4)
        assert got == want, seed


def test_untouched_levels_are_shared_between_consecutive_snapshots_and_the_moved_one_is_new():
    snap = _snapshot(levels=30)
    events = [{'y': 'o', 't': 1, 'p': 99.9, 'q': 2.0, 's': 0, 'i': 1},
              {'y': 't', 't': 2, 'p': 100.1, 'q': 1.0, 's': 0},
              {'y': 'c', 't': 3, 'p': 99.9, 'q': 2.0, 's': 0, 'i': 1}]
    hist, _ = history(snap, events, 4)
    s0, s1, s2 = hist[0], hist[1], hist[2]
    assert s1 is not s0 and s1['bids'] is not s0['bids'], "a new book and a new side for the side the event moved"
    assert s1['asks'] is s0['asks'], "the untouched side is the same object"
    assert s1['bids'][99.9] is not s0['bids'][99.9] and s1['bids'][99.9]['q'] == s0['bids'][99.9]['q'] + 2.0
    assert all(s1['bids'][p] is s0['bids'][p] for p in s0['bids'] if p != 99.9), "every other level is shared"
    assert s0['bids'][99.9]['q'] == 5.0 + 1, "the earlier snapshot is untouched"
    assert s2['asks'][100.1] is not s1['asks'][100.1] and s2['asks'][100.1]['q'] == s1['asks'][100.1]['q'] - 1.0
    distinct_levels = {id(l) for s in hist.values() for side in ('bids', 'asks') for l in s[side].values()}
    assert len(distinct_levels) <= 60 + len(events), "one new level object per event, not a copy of the book"


def test_a_batch_whose_worker_died_is_rebuilt_in_the_calling_process(monkeypatch):
    from loky.process_executor import TerminatedWorkerError
    snaps = {b: _snapshot(levels=5) for b in range(4)}
    events = {b: _stream(b, n=20) for b in range(4)}
    batches = [[0, 1], [2, 3]]
    calls = []

    class _Pool:
        def submit(self, fn, snapshots, evs, vd):
            calls.append(sorted(snapshots))
            f = concurrent.futures.Future()
            if 2 in snapshots:
                f.set_exception(TerminatedWorkerError("worker killed (memory)"))
            else:
                f.set_result(fn(snapshots, evs, vd))
            return f

    warned = []
    monkeypatch.setattr(hmod, "get_reusable_executor", lambda max_workers: _Pool())
    monkeypatch.setattr(hmod, "logger", logging.getLogger("test.book_rebuild"))
    hmod.logger.warning = lambda msg, *a, **k: warned.append(msg)
    out = batch_history(snaps, events, batches, 4)
    assert sorted(out) == [0, 1, 2, 3], "every book comes back"
    for b in range(4):
        want = _reference_history(copy.deepcopy(_snapshot(levels=5)), copy.deepcopy(events[b]), 4)
        assert out[b] == want
    assert calls == [[0, 1], [2, 3]]
    assert len(warned) == 1 and "rebuilt in-process" in warned[0] and "[2, 3]" in warned[0], warned
