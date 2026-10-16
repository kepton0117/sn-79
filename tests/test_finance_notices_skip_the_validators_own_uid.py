# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Finance notices are never sent to the validator's own uid.

A notice without an agentId goes to every axon in the extended metagraph, and the
validator's own axon is published at its external IP with nothing serving it, so a
request to it would reach whatever listens on that port, which rejects it as a
signature mismatch. Same rule as query.py's _queryable_axons: skip own uid and own
hotkey.
"""
import asyncio
from types import SimpleNamespace

from taos.im.validator.forward import notify

OWN_HOTKEY = "5Ebq3nnVj2YAyznQsFDJJSmyoYGV7sx5boXqXv9go4rNrT3G"


def _axon(hotkey, ip="203.0.113.1"):
    return SimpleNamespace(hotkey=hotkey, ip=ip)


def _validator(axons, own_uid=0):
    sent = []

    async def dendrite(axons, synapse, timeout):
        sent.append([a.hotkey for a in axons])
        return []

    v = SimpleNamespace(
        uid=own_uid,
        wallet=SimpleNamespace(hotkey=SimpleNamespace(ss58_address=OWN_HOTKEY)),
        get_extended_metagraph=lambda: SimpleNamespace(axons=axons),
        dendrite=dendrite,
    )
    return v, sent


def _notice(agent_id):
    return SimpleNamespace(event=SimpleNamespace(agentId=agent_id))


def _metagraph():
    return [_axon(OWN_HOTKEY), _axon("miner_1"), _axon("miner_2"), _axon("bench_3", ip="0.0.0.0")]


def test_a_broadcast_notice_skips_the_validators_own_uid():
    v, sent = _validator(_metagraph())
    asyncio.run(notify(v, [_notice(None)]))
    assert sent and OWN_HOTKEY not in sent[0]
    assert {"miner_1", "miner_2"} <= set(sent[0])


def test_a_notice_addressed_to_the_validators_own_uid_is_not_sent():
    v, sent = _validator(_metagraph())
    asyncio.run(notify(v, [_notice(0)]))
    assert all(OWN_HOTKEY not in hk for hk in sent)
    assert all(hk for hk in sent)


def test_a_notice_addressed_to_a_miner_still_reaches_it():
    v, sent = _validator(_metagraph())
    asyncio.run(notify(v, [_notice(2)]))
    assert sent == [["miner_2"]]


def test_the_own_hotkey_is_skipped_under_another_uid():
    axons = [_axon("miner_0"), _axon(OWN_HOTKEY), _axon("miner_2")]
    v, sent = _validator(axons, own_uid=5)
    asyncio.run(notify(v, [_notice(None)]))
    assert OWN_HOTKEY not in sent[0]


def test_an_out_of_range_agent_id_broadcasts_without_the_own_uid():
    v, sent = _validator(_metagraph())
    asyncio.run(notify(v, [_notice(99)]))
    assert sent and OWN_HOTKEY not in sent[0] and "miner_1" in sent[0]
