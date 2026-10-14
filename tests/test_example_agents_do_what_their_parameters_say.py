# SPDX-FileCopyrightText: 2026 Rayleigh Research <to@rayleigh.re>
# SPDX-License-Identifier: MIT
"""The example agents and the miner launcher's defaults do what their parameters say.

The examples are templates rather than competitive strategies, but a miner copies them, so an order must carry
the size the agent computed, a dataclass default must not be one object shared by every instance, and the
launcher's default parameters must be ones the default agent reads, in the units it reads them."""
import ast
import re
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
AGENTS = DEV / "agents"
ORDER_CALLS = {"limit_order", "market_order"}
IMMUTABLE_BUILTINS = ("int", "float", "str", "bool", "bytes", "tuple", "frozenset")


def _order_quantity_exprs(path: Path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ORDER_CALLS:
            for kw in node.keywords:
                if kw.arg == "quantity":
                    yield node.lineno, kw.value


def _adds_leverage(expr) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            for side in (node.left, node.right):
                if isinstance(side, ast.Name) and side.id == "leverage":
                    return True
    return False


def test_no_example_order_adds_the_leverage_to_its_quantity():
    """Leverage scales an order on the engine side; adding it to the quantity sends a different size than the
    agent computed (RandomMakerAgent sold `quantity * 1 + leverage`, the quantity plus the leverage)."""
    offenders = [f"{p.name}:{line}" for p in sorted(AGENTS.glob("*.py"))
                 for line, expr in _order_quantity_exprs(p) if _adds_leverage(expr)]
    assert not offenders, f"orders whose quantity adds the leverage: {offenders}"


def _mutable_dataclass_defaults(path: Path):
    tree = ast.parse(path.read_text())
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        is_dc = any((isinstance(d, ast.Name) and d.id == "dataclass")
                    or (isinstance(d, ast.Call) and getattr(d.func, "id", None) == "dataclass")
                    for d in cls.decorator_list)
        if not is_dc:
            continue
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.value, ast.Call):
                fn = stmt.value.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name not in ("field",) + IMMUTABLE_BUILTINS:
                    yield f"{path.name}:{stmt.lineno} {cls.name}.{getattr(stmt.target, 'id', '?')} = {name}()"


def test_no_example_dataclass_shares_one_mutable_default():
    """A dataclass field defaulting to an instance shares that one object across every instance (and Python
    3.11+ refuses an unhashable one at import); default_factory gives each instance its own."""
    offenders = [o for p in sorted(AGENTS.glob("*.py")) for o in _mutable_dataclass_defaults(p)]
    assert not offenders, f"dataclass fields defaulting to a shared instance: {offenders}"


def _launcher_default(name: str) -> str:
    m = re.search(rf'^{name}="?([^"\n]*)"?', (DEV / "run_miner.sh").read_text(), re.M)
    assert m, f"run_miner.sh sets no default {name}"
    return m.group(1).strip()


def test_the_launchers_default_parameters_are_ones_its_default_agent_reads():
    agent = _launcher_default("AGENT_NAME")
    params = dict(kv.split("=", 1) for kv in _launcher_default("AGENT_PARAMS").split())
    src = (AGENTS / f"{agent}.py").read_text()
    read = set(re.findall(r"self\.config\.([a-z_]+)", src))
    ignored = sorted(k for k in params if k not in read)
    assert not ignored, f"run_miner.sh passes {ignored} to {agent}, which never reads them"


def test_the_launchers_default_expiry_is_in_simulation_nanoseconds():
    """expiry_period is simulation nanoseconds: 200 expires a GTT order almost at once, where the shared
    per-agent defaults (bin/agent_params.sh) use 120 s."""
    params = dict(kv.split("=", 1) for kv in _launcher_default("AGENT_PARAMS").split())
    if "expiry_period" in params:
        assert float(params["expiry_period"]) >= 1_000_000_000, params["expiry_period"]
