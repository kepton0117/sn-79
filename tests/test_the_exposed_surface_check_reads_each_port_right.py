# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The exposed-surface check a deploy runs from outside a validator host: each validator API port refuses or drops
the connection, the metrics port answers its validator family, and no server answers /openapi.json, /docs or /redoc.
Its verdict is driven here through stand-in probes, so no socket is opened."""
import importlib.util
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "runbooks", "deploy", "exposed_surface_check.py")


@pytest.fixture(scope="module")
def check():
    if not os.path.exists(SCRIPT):
        pytest.skip("the deploy runbooks are not in this tree")
    spec = importlib.util.spec_from_file_location("exposed_surface_check", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _probes(open_ports=(), statuses=None):
    statuses = statuses or {}

    def tcp(host, port, timeout):
        return port in open_ports

    def http(host, port, path, timeout):
        return statuses.get((port, path))

    return tcp, http


def _healthy_metrics(port=9091):
    s = {(port, "/metrics/validator"): 200}
    s.update({(port, p): 404 for p in ("/openapi.json", "/docs", "/redoc")})
    return s


def test_a_host_as_intended_passes(check):
    tcp, http = _probes(open_ports={9091}, statuses=_healthy_metrics())
    results = check.run_checks("validator.example", [8000, 8001], 9091, tcp=tcp, http=http)
    assert results and all(ok for ok, _line in results), results
    assert check.exit_code(results) == 0


def test_a_reachable_api_port_fails(check):
    statuses = _healthy_metrics()
    statuses[(8000, "/openapi.json")] = 404
    tcp, http = _probes(open_ports={8000, 9091}, statuses=statuses)
    results = check.run_checks("validator.example", [8000, 8001], 9091, tcp=tcp, http=http)
    failed = [line for ok, line in results if not ok]
    assert any("8000" in line for line in failed), results
    assert check.exit_code(results) == 1


@pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
def test_a_route_list_on_the_metrics_port_fails(check, path):
    statuses = _healthy_metrics()
    statuses[(9091, path)] = 200
    tcp, http = _probes(open_ports={9091}, statuses=statuses)
    results = check.run_checks("validator.example", [8000], 9091, tcp=tcp, http=http)
    assert any(path in line for ok, line in results if not ok), results
    assert check.exit_code(results) == 1


def test_a_silent_metrics_port_fails(check):
    tcp, http = _probes(open_ports=set(), statuses={})
    results = check.run_checks("validator.example", [8000], 9091, tcp=tcp, http=http)
    assert any("9091" in line for ok, line in results if not ok), results
    assert check.exit_code(results) == 1
