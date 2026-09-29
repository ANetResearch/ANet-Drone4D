"""tests/sim 公共夹具：把本目录加入 sys.path 以导入 `simlib`（pytest 以 importlib 模式运行）。"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)


# ------------------------------------------------------------------------------------------------ 进程级扩展点隔离（INT-1）
# 运动提供者（`register_motion_provider`）与细校验执行者（`register_fine_checker`）是进程级表，不随 isolated_registry()
# 切换。整棵 tests/ 一起收集时，tests/mission 的模块在收集阶段 import awr.sim.mission，M10 自动 install() 把 goto、
# follow_path、orbit 提供者与细校验执行者登记进全局表，本目录的 CommandEngine 用例（原生 GOTO、停滞 203、截止 202、
# RTL 剖面）随之改走 M10 路径而失败；单独运行本目录时不失败。每个用例开始时清空、结束后恢复。
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_command_extension_points():
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
