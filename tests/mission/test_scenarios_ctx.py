"""`rest/scenarios.py` 的请求级上下文（M10-FR-213；M10-AC-210；AWR-04 §4.7、§14.3）。

- 全部路由经 `Depends(ctx_for)` 取运行上下文，不再直接读 `request.app.state.awr`（M11-AC-201 的静态规则）；
- show-api 中 `ctx_for` 恒返回唯一上下文：D1 行为见 `test_missions_rest.py`（R10、R11、R23–R26、R65 与任务控制）；
- sandbox-api 不挂载 scenarios 路由：`/api/sandbox/v1/sessions/{sid}/…` 与全站都不出现 `/api/scenarios`、`/api/missions`。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awr.api.rest import scenarios as SC

SRC = Path(SC.__file__).read_text(encoding="utf-8")


def test_routes_use_ctx_for_not_app_state() -> None:
    tree = ast.parse(SRC)
    body = SRC.split('"""', 2)[2]                                       # 去掉模块说明
    assert "app.state" not in body and "app_ctx(" not in body
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "ctx_for" in names
    from awr.api import deps

    assert SC.ctx_for is deps.ctx_for
    routed = [r for r in SC.router.routes if str(getattr(r, "path", "")).startswith(("/api/missions", "/api/scenario/"))
              or getattr(r, "path", "") == "/api/scenario"]
    assert len(routed) >= 7
    for r in routed:
        calls = {getattr(d.call, "__name__", "") for d in r.dependant.dependencies}
        assert "ctx_for" in calls, r.path


def test_sandbox_api_does_not_mount_scenarios(tmp_path) -> None:
    try:
        from awr.api.sandbox_main import DENY_MODULES, create_sandbox_app
    except ImportError:  # pragma: no cover - sandbox-api（M11）未交付
        pytest.skip("sandbox-api not available")
    assert "scenarios" in DENY_MODULES
    app = create_sandbox_app(state_dir=tmp_path, token_key=b"k" * 32, bus_kind="local")
    paths = [str(getattr(r, "path", "")) for r in app.routes]
    assert paths and not any("/scenario" in p or "/missions" in p for p in paths), paths
