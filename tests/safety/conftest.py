"""tests/safety 公共夹具（M09 §10.3）。

- 把本目录加入 sys.path 以导入 `safelib`（pytest 以 importlib 模式运行）；
- 收集阶段导入 `awr.sim.safety` 后立即 `uninstall()`：导入即登记到全局登记表是 sim-core 组合根的约定，但同一 pytest 进程中
  M08、M11 的用例以全局登记表构造 SimCore，不应被 M09 改变；M09 的用例一律在隔离登记表中 `install()`（safelib.Harness）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import awr.sim.safety as _M09  # noqa: E402

_M09.uninstall()


@pytest.fixture(scope="session")
def tiny_world(tmp_path_factory):
    """M04 合成小世界（`fake_world_query`：L 形凹禁飞区、矩形限制区、100 m 塔、两栋 30 m 楼）。"""
    from awr.world.geometry.fake import fake_world_query

    return fake_world_query(tmp_path_factory.mktemp("m09world"))


@pytest.fixture(autouse=True)
def _isolate_command_extension_points():
    """运动提供者与细校验执行者是进程级表（不随 isolated_registry 切换）。整棵 tests/ 一起收集时 M10 在收集阶段自动
    install()，其细校验执行者只认自己的 CommandEngine，本目录 Harness 的"交细校验"调用因此永不终结（INT-1）。"""
    from awr.sim.core import command as CMD

    provs, fine = CMD.motion_providers(), list(CMD._FINE)
    CMD.reset_motion_providers()
    CMD._FINE[:] = []
    try:
        yield
    finally:
        CMD.reset_motion_providers()
        for p in {id(p): p for p in provs.values()}.values():
            CMD.register_motion_provider(p)
        CMD._FINE[:] = fine
