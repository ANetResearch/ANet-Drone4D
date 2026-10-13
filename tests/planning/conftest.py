"""M10 测试公共设置：本目录的辅助模块可直接 import；每个用例内 plan-pool 缺省 inline（提交即执行，结果仍在下一次
drain 生效）、不自动加载剧本。环境变量只在用例期间经 monkeypatch 设置，不泄漏到同一 pytest 进程中其他模块的用例
（例如 e2e 启动的 sim-core 子进程）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


@pytest.fixture(autouse=True)
def _m10_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_PLAN_POOL", "inline")
    monkeypatch.setenv("AWR_SCENARIO_LOAD", "0")


PLUGINS = "awr.sim.planning.kinds_builtin,plan_test_kinds"


@pytest.fixture
def plan_plugins(monkeypatch: pytest.MonkeyPatch) -> str:
    """测试插件 `plan_test_kinds`（test.* 类别）：本进程导入并登记；spawn 出的 worker 经 PYTHONPATH 与 AWR_PLAN_PLUGINS
    导入同一模块（D2 注册表，M10-FR-201）。"""
    import os

    from awr.sim.planning.jobs import load_plan_plugins

    monkeypatch.setenv("AWR_PLAN_PLUGINS", PLUGINS)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([_HERE, *[p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                                                               if p]]))
    load_plan_plugins(PLUGINS, strict=True)
    return PLUGINS
