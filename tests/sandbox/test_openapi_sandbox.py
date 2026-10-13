"""沙盒 OpenAPI 与挂载约定（M17-FR-038、FR-053、AC-007）：设计表（`tools/contracts/sandbox_api.py`）中 M17 的全部路由出现在实时
OpenAPI 中；每个 M17 操作带 `x-awr-reasons` 与 curl、SDK 两种 `x-codeSamples`；`rest/sandbox.py` 不导出 `router`，show-api 不挂载。"""

from __future__ import annotations

import sys
from pathlib import Path

import awr.api.rest.sandbox as rs
from awr.api.sandbox_main import PREFIX, live_openapi

ROOT = Path(__file__).resolve().parents[2]


def _design() -> list:
    p = str(ROOT / "tools" / "contracts")
    if p not in sys.path:
        sys.path.insert(0, p)
    import sandbox_api  # type: ignore[import-not-found]

    return [e for e in sandbox_api.ENDPOINTS if e.owner.startswith("rest/sandbox.py")]


def test_m17_routes_present_with_extensions() -> None:
    spec = live_openapi()
    design = _design()
    assert len(design) == 26  # D2.1：SB-69 至 SB-72、SB-76、SB-77（契约 1.2.0，D2-INT）
    for e in design:
        ops = spec["paths"].get(PREFIX + e.path)
        assert ops is not None and e.method in ops, (e.sb, e.method, e.path)
        op = ops[e.method]
        assert op.get("x-awr-reasons"), e.sb
        langs = {s["lang"] for s in op.get("x-codeSamples") or []}
        assert {"Shell", "Python"} <= langs, e.sb
        if "/sessions/{sid}" in e.path and not e.path.endswith("end-beacon"):
            assert op.get("security") == [{"bearerAuth": []}], e.sb
    assert spec["openapi"] == "3.1.0" and spec["info"]["title"] == "ANet Drone4D Sandbox API"


def test_not_mounted_on_show_api() -> None:
    assert not hasattr(rs, "router")
    from awr.api.main import discover_routers

    names = [n for n, _r in discover_routers("router")]
    assert not any(n.endswith("sandbox") for n in names)
