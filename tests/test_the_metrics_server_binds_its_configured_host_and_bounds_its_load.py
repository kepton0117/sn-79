# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The metrics server binds `--prometheus.host` (every interface by default, so the existing scrapers keep working),
bounds the requests each client address has in flight (no global cap, so other clients cannot crowd out our own
scraper; the bound itself is driven in test_one_metrics_client_cannot_crowd_out_another.py), writes no line per
request, and its combined /metrics carries the board families only: the two bulk families (agent, trades) keep their
own paths.

The reporting service is a separate process that parses its own arguments, so the validator forwards the host to it,
and the child's parser accepts it."""
import argparse
import ast
import os
import time

import pytest
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, Gauge

import taos.common.config  # noqa: F401  (the validator's config import, which turns on bittensor's argument parse)
from taos.common.utils.prometheus import prometheus
from taos.im.validator import exposition
from taos.im.validator.http_surface import (
    METRICS_HOST_DEFAULT,
    METRICS_PER_CLIENT_CONCURRENCY,
    metrics_server_options,
    per_client_limit_from_env,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config(argv):
    bt = pytest.importorskip("bittensor")
    parser = argparse.ArgumentParser()
    prometheus.add_args(parser)
    return bt.Config(parser, args=argv)


def test_the_default_bind_is_every_interface():
    assert METRICS_HOST_DEFAULT == "0.0.0.0"
    config = _config([])
    assert config.prometheus.host == "0.0.0.0"
    assert metrics_server_options(config)["host"] == "0.0.0.0"


def test_a_host_can_bind_one_interface():
    opts = metrics_server_options(_config(["--prometheus.host", "203.0.113.7", "--prometheus.port", "9091"]))
    assert opts["host"] == "203.0.113.7"
    assert opts["port"] == 9091


def test_the_server_writes_no_access_log_and_has_no_global_connection_cap():
    opts = metrics_server_options(_config([]))
    assert opts["access_log"] is False
    assert opts["log_level"] == "warning"
    assert "limit_concurrency" not in opts, "a global cap lets other clients crowd out our own scraper"


def test_the_per_client_bound_defaults_to_eight_and_reads_a_positive_integer():
    if "MVTRX_METRICS_PER_CLIENT_CONCURRENCY" not in os.environ:
        assert METRICS_PER_CLIENT_CONCURRENCY == 8
    assert per_client_limit_from_env("12") == 12
    for raw in (None, "", "eight", "0", "-3"):
        assert per_client_limit_from_env(raw) == 8, raw


def test_the_reporting_child_accepts_the_host():
    bt = pytest.importorskip("bittensor")
    from taos.im.validator.report import build_parser

    assert bt.Config(build_parser(), args=[]).prometheus.host == "0.0.0.0"
    assert bt.Config(build_parser(), args=["--prometheus.host", "203.0.113.7"]).prometheus.host == "203.0.113.7"


def test_the_validator_forwards_the_host_to_the_reporting_child():
    src = open(os.path.join(REPO, "taos/im/neurons/validator.py"), encoding="utf-8").read()
    assert "'--prometheus.host'" in src


def test_the_metrics_server_start_takes_the_shared_options():
    tree = ast.parse(open(os.path.join(REPO, "taos/im/validator/report.py"), encoding="utf-8").read())
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "run" and getattr(n.func.value, "id", None) == "uvicorn"]
    assert len(runs) == 1
    spread = [k.value for k in runs[0].keywords if k.arg is None]
    assert [k.arg for k in runs[0].keywords if k.arg is not None] == []
    assert len(spread) == 1 and getattr(spread[0].func, "id", None) == "metrics_server_options"


def test_the_shipped_combined_path_carries_the_board_families():
    assert exposition.main_mode_from(None) == "board"
    assert exposition.main_mode_from("") == "board"
    assert exposition.main_mode_from("all") == "all"
    assert exposition.main_mode_from(" BOARD ") == "board"


def _service_with_one_gauge_per_family(main_mode):
    from taos.im.validator.report import ReportingService

    svc = ReportingService.__new__(ReportingService)
    svc._first_publish_done = True
    svc._exposition_start = time.monotonic()
    svc.registries = {name: CollectorRegistry() for name in exposition.REGISTRY_ORDER}
    for name, registry in svc.registries.items():
        Gauge(f"family_{name}_probe", "probe", registry=registry).set(1)
    svc.exposition = exposition.Exposition(svc.registries, main_mode=main_mode)
    return svc


@pytest.mark.parametrize("published", [False, True])
def test_the_combined_path_leaves_the_bulk_families_to_their_own_paths(published):
    svc = _service_with_one_gauge_per_family("board")
    if published:
        svc.exposition.publish(step=1, sim_timestamp=1, sim_id="x")
    client = TestClient(svc._metrics_app())
    combined = client.get("/metrics").text
    for name in exposition.BOARD_REGISTRIES:
        assert f"family_{name}_probe" in combined, name
    for name in ("agent", "trades"):
        assert f"family_{name}_probe" not in combined, name
        assert f"family_{name}_probe" in client.get(f"/metrics/{name}").text, name


@pytest.mark.parametrize("published", [False, True])
def test_all_keeps_every_family_on_the_combined_path(published):
    svc = _service_with_one_gauge_per_family("all")
    if published:
        svc.exposition.publish(step=1, sim_timestamp=1, sim_id="x")
    combined = TestClient(svc._metrics_app()).get("/metrics").text
    for name in exposition.REGISTRY_ORDER:
        assert f"family_{name}_probe" in combined, name
