"""The gateway's own settings (vogon step 2, 2026-10-10).

Nothing under the gateway tree reads the application's shared `Settings`;
the parity of the two objects' defaults is the application's test
(data-plane/tests/test_gateway_settings_parity.py).
"""
from __future__ import annotations

import ast
import pathlib

GW_ROOT = pathlib.Path(__file__).resolve().parents[1]
GW_APP = GW_ROOT / "vogon"


def test_a_set_front_config_path_is_used_as_given():
    from vogon.gateway.settings import GatewaySettings  # noqa: PLC0415 — a fresh settings object per test
    g = GatewaySettings(_env_file=None, front_config_path="conf/front.yaml")
    assert g.resolved_front_config_path().endswith("/conf/front.yaml")


def test_an_unset_front_config_path_resolves_to_nothing():
    """The gateway ships no words of its own: unset means the model tier is
    off, never a file in some other tree."""
    from vogon.gateway.settings import GatewaySettings  # noqa: PLC0415 — a fresh settings object per test
    g = GatewaySettings(_env_file=None, front_config_path="")
    assert g.resolved_front_config_path() == ""


def test_the_neutral_template_loads_with_beats_only():
    """examples/front.yaml: the beat pools pass the guard, and with no prompt
    or criteria the model tier stays off."""
    from vogon.gateway.transport.front_source import FrontConfig  # noqa: PLC0415
    cfg = FrontConfig.load(str(GW_ROOT / "examples" / "front.yaml"))
    assert cfg is not None
    assert cfg.beats.get("hold") and cfg.beats.get("wait")
    assert not cfg.prompt and not cfg.can_read_moves


def test_nothing_in_the_gateway_tree_reads_the_shared_settings():
    offenders = []
    for path in sorted(GW_APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = getattr(node, "module", None) if isinstance(node, ast.ImportFrom) else None
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if mod == "app.config" or any(n == "app.config" for n in names):
                offenders.append(str(path.relative_to(GW_APP.parent)))
    assert not offenders, offenders
