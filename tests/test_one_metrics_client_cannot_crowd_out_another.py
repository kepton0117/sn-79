# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The metrics server bounds the requests each client address has in flight, and nothing else: an address at its
bound gets 503 at once, and every other address is served as usual. Our own monitoring pulls seven family paths,
possibly in parallel, from one address, so no set of other clients may push it out.

Driven against the metrics app the reporting service builds. The agent family is held in flight until the test
releases it, standing in for a slow pull; every other family answers at once. The client address is the TCP peer,
which the ASGI transport sets per client here."""
import asyncio
import threading
import time

import httpx
from prometheus_client import CollectorRegistry

from taos.im.validator import report
from taos.im.validator.exposition import REGISTRY_ORDER
from taos.im.validator.http_surface import METRICS_PER_CLIENT_CONCURRENCY

N = METRICS_PER_CLIENT_CONCURRENCY


class _HeldExposition:
    """Serves every family at once except agent, which waits for the release."""

    def __init__(self):
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.held = 0

    def main_registries(self):
        return REGISTRY_ORDER

    def serve(self, name=None):
        if name == "agent":
            with self._lock:
                self.held += 1
            self.release.wait(timeout=30)
        return b"# family\n"


def _service(monkeypatch):
    monkeypatch.setattr(report, "SNAPSHOT_ENABLED", True)
    svc = report.ReportingService.__new__(report.ReportingService)
    svc._first_publish_done = True
    svc._exposition_start = time.monotonic()
    svc.registries = {name: CollectorRegistry() for name in REGISTRY_ORDER}
    svc.exposition = _HeldExposition()
    return svc


def _client(app, address):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(address, 40000)), base_url="http://v")


async def _until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the held requests never reached the app"
        await asyncio.sleep(0.01)


def test_an_address_at_its_bound_does_not_crowd_out_another(monkeypatch):
    svc = _service(monkeypatch)
    exp = svc.exposition
    app = svc._metrics_app()

    async def scenario():
        crowd = [_client(app, "198.51.100.7"), _client(app, "198.51.100.8")]
        ours = _client(app, "203.0.113.5")
        held = [asyncio.create_task(c.get("/metrics/agent")) for c in crowd for _ in range(N)]
        try:
            await _until(lambda: exp.held == len(held))
            for c in crowd:
                assert (await c.get("/metrics/validator")).status_code == 503
            for path in ("/metrics/validator", "/metrics/simulation", "/metrics/books", "/metrics/gentrx",
                         "/metrics/miner", "/metrics/trades"):
                assert (await ours.get(path)).status_code == 200, path
        finally:
            exp.release.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200] * len(held)

    asyncio.run(scenario())


def test_the_request_past_the_bound_is_refused_and_the_bound_frees_as_requests_finish(monkeypatch):
    svc = _service(monkeypatch)
    exp = svc.exposition
    app = svc._metrics_app()

    async def scenario():
        one = _client(app, "198.51.100.7")
        held = [asyncio.create_task(one.get("/metrics/agent")) for _ in range(N - 1)]
        try:
            await _until(lambda: exp.held == N - 1)
            assert (await one.get("/metrics/validator")).status_code == 200, "the last request within the bound"
            held.append(asyncio.create_task(one.get("/metrics/agent")))
            await _until(lambda: exp.held == N)
            refused = await one.get("/metrics/validator")
            assert refused.status_code == 503, "one past the bound is refused at once"
        finally:
            exp.release.set()
        assert [r.status_code for r in await asyncio.gather(*held)] == [200] * N
        again = await asyncio.gather(*(one.get("/metrics/validator") for _ in range(N)))
        assert [r.status_code for r in again] == [200] * N, "the bound frees as the held requests finish"

    asyncio.run(scenario())
