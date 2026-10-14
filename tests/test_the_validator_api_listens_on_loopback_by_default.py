# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The validator API answers the simulator and the SL/TP service on this host, so it listens on loopback unless a
deployment names another address with `--api.host`.

Both places the validator starts that server (one per mechanism) take their options from the same function, so the
default cannot drift between them, and neither writes a line per request to the log."""
import argparse
import ast
import os

import pytest

from taos.im.config import add_im_validator_args
from taos.im.validator.http_surface import API_HOST_DEFAULT, api_server_options

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VALIDATOR = os.path.join(REPO, "taos", "im", "neurons", "validator.py")


def _config(argv):
    bt = pytest.importorskip("bittensor")
    parser = argparse.ArgumentParser()
    add_im_validator_args(None, parser)
    return bt.Config(parser, args=argv)


def test_the_default_bind_is_loopback():
    assert API_HOST_DEFAULT == "127.0.0.1"
    config = _config([])
    assert config.api.host == "127.0.0.1"
    opts = api_server_options(config)
    assert opts["host"] == "127.0.0.1"
    assert opts["port"] == 8000


def test_a_deployment_can_name_another_address_and_port():
    opts = api_server_options(_config(["--api.host", "10.0.0.7", "--port", "8001"]))
    assert opts["host"] == "10.0.0.7"
    assert opts["port"] == 8001


def test_the_server_writes_no_access_log_and_logs_warnings_only():
    opts = api_server_options(_config([]))
    assert opts["access_log"] is False
    assert opts["log_level"] == "warning"


def test_a_config_without_the_option_still_binds_loopback():
    from types import SimpleNamespace

    assert api_server_options(SimpleNamespace(port=8000))["host"] == "127.0.0.1"


def _uvicorn_runs(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run":
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "uvicorn":
                calls.append(node)
    return calls


def test_both_api_server_starts_take_the_shared_options():
    calls = _uvicorn_runs(VALIDATOR)
    assert len(calls) == 2, "the validator starts the API in two places, one per mechanism"
    for call in calls:
        assert not call.keywords or all(k.arg is None for k in call.keywords), (
            f"line {call.lineno}: uvicorn.run takes only **api_server_options(...), no literal host or port"
        )
        spread = [k.value for k in call.keywords if k.arg is None]
        assert len(spread) == 1 and isinstance(spread[0], ast.Call), f"line {call.lineno}"
        assert getattr(spread[0].func, "id", None) == "api_server_options", f"line {call.lineno}"
