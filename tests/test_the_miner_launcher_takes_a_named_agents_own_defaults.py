# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""A miner started with `-n <agent>` and no `-m` gets that agent's own parameters.

run_miner.sh's default parameters belong to its default agent. Naming another agent with `-n` kept them, so
`run_miner.sh -n RandomMakerAgent` passed SimpleRegressorAgent's parameters to an agent that reads none of them
and lacks the ones it requires (an agent with a missing required parameter raises in initialize and the miner
exits). The per-agent map in bin/agent_params.sh is the one the acceptance launchers already share; the launcher
resolves through it whenever an agent is named and its parameters are not."""
import subprocess
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
MAP = DEV / "bin" / "agent_params.sh"


def _resolve(agent, named, given, current):
    script = f'. "{MAP}"; resolve_agent_params "$1" "$2" "$3" "$4"'
    r = subprocess.run(["bash", "-c", script, "_", agent, named, given, current],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _map(agent):
    r = subprocess.run(["bash", "-c", f'. "{MAP}"; agent_params "$1"', "_", agent],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def test_a_named_agent_without_params_gets_its_own_defaults():
    got = _resolve("RandomMakerAgent", "1", "0", "quantity=1.0 model=PassiveAggressiveRegressor")
    assert got == _map("RandomMakerAgent") and "min_leverage=" in got


def test_explicit_params_always_win():
    assert _resolve("RandomMakerAgent", "1", "1", "min_quantity=2.0") == "min_quantity=2.0"


def test_the_default_agent_keeps_the_launchers_default_params():
    assert _resolve("SimpleRegressorAgent", "0", "0", "quantity=1.0") == "quantity=1.0"


def test_an_agent_the_map_does_not_know_keeps_what_it_had():
    assert _resolve("MyOwnAgent", "1", "0", "foo=1") == "foo=1"


def test_the_launcher_resolves_through_the_map():
    text = (DEV / "run_miner.sh").read_text()
    assert 'bin/agent_params.sh' in text and 'resolve_agent_params "$AGENT_NAME"' in text, (
        "run_miner.sh must source bin/agent_params.sh and resolve the parameters of a named agent through it")
