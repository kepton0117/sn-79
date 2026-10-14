# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""Neither of the validator's HTTP servers serves a route list: no /openapi.json, /docs or /redoc on the API or on the
metrics server. Both apps come from one constructor, and the metrics app is driven here as the reporting service
builds it, so the test reads the shipped routes rather than a copy."""
import ast
import os
import time

from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry

from taos.im.validator.exposition import REGISTRY_ORDER, Exposition
from taos.im.validator.http_surface import make_app

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC_PATHS = ("/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect")


def _metrics_service():
    from taos.im.validator.report import ReportingService

    svc = ReportingService.__new__(ReportingService)
    svc._first_publish_done = True
    svc._exposition_start = time.monotonic()
    svc.registries = {name: CollectorRegistry() for name in REGISTRY_ORDER}
    svc.exposition = Exposition(svc.registries)
    return svc


def test_the_shared_constructor_serves_no_route_list():
    app = make_app()

    @app.get("/sltp/levels")
    async def levels():
        return {"levels": []}

    client = TestClient(app)
    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path
    assert client.get("/sltp/levels").status_code == 200


def test_the_metrics_server_serves_its_families_and_no_route_list():
    client = TestClient(_metrics_service()._metrics_app())
    for path in DOC_PATHS:
        assert client.get(path).status_code == 404, path
    for path in ("/metrics", "/metrics/validator", "/metrics/agent"):
        assert client.get(path).status_code == 200, path


def _calls_named(path, name):
    tree = ast.parse(open(path, encoding="utf-8").read())
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]


def test_both_servers_build_their_app_with_the_shared_constructor():
    for rel in ("taos/im/neurons/validator.py", "taos/im/validator/report.py"):
        path = os.path.join(REPO, rel)
        assert not _calls_named(path, "FastAPI"), f"{rel} builds a FastAPI app directly"
        assert len(_calls_named(path, "make_app")) == 1, f"{rel} builds its app with make_app()"
