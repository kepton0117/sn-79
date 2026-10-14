"""A simulation run end restarts the simulator and leaves the exchange engine running.

The restart path in update.py stops the simulator by its command line. The exchange engine runs the same
binary with -e, so matching ../build/src/cpp/taosim alone would stop it with the simulator, and a host
running both mechanisms would lose its exchange at every simulation run end. Only a simulation engine is
the simulator's to kill.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import taos.im.validator.update as upd  # noqa: E402


class _Proc:
    def __init__(self, pid, name, cmdline):
        self.info = {"pid": pid, "name": name, "cmdline": cmdline}
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def test_the_exchange_engine_is_not_a_simulator():
    assert not upd._is_simulator_cmdline("../build/src/cpp/taosim -e config/exchange_0.xml")
    assert not upd._is_simulator_cmdline("/usr/bin/bash -c ../build/src/cpp/taosim -e config/exchange_0.xml")


def test_every_simulation_engine_form_is_a_simulator():
    assert upd._is_simulator_cmdline("../build/src/cpp/taosim -c latest")
    assert upd._is_simulator_cmdline("../build/src/cpp/taosim -f config/simulation_0.xml")
    assert upd._is_simulator_cmdline("/usr/bin/bash -c ../build/src/cpp/taosim -c latest")
    assert not upd._is_simulator_cmdline("python validator.py --netuid 79")


def test_the_kill_leaves_the_exchange_running(monkeypatch):
    sim = _Proc(11, "taosim", ["../build/src/cpp/taosim", "-c", "latest"])
    xch = _Proc(22, "taosim", ["../build/src/cpp/taosim", "-e", "config/exchange_0.xml"])
    xwrap = _Proc(23, "bash", ["/usr/bin/bash", "-c", "../build/src/cpp/taosim -e config/exchange_0.xml"])
    other = _Proc(33, "python", ["python", "validator.py"])
    monkeypatch.setattr(upd.psutil, "process_iter", lambda attrs=None: iter([sim, xch, xwrap, other]))
    n = upd._kill_simulator_processes()
    assert n == 1 and sim.killed
    assert not xch.killed and not xwrap.killed, "the exchange engine was killed with the simulator"
    assert not other.killed


def test_a_running_exchange_does_not_pass_for_a_running_simulator(monkeypatch):
    """The health check's no-pm2 fallback matched the same binary, so a live exchange hid a dead simulator."""
    import time
    from types import SimpleNamespace

    alerts = []
    me = SimpleNamespace(engine=SimpleNamespace(mode="simulation"), last_state_time=time.time() - 3600,
                         pagerduty_alert=alerts.append)
    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="[]", stderr=""))
    xch = _Proc(22, "taosim", ["../build/src/cpp/taosim", "-e", "config/exchange_0.xml"])
    monkeypatch.setattr(upd.psutil, "process_iter", lambda attrs=None: iter([xch]))
    assert upd.check_simulator(me) is False
    assert alerts, "a dead simulator raised no alert while the exchange engine ran"
