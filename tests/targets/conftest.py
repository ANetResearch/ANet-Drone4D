"""tests/targets 公共夹具（M18）：把本目录与 tests/sim、tests/contracts、tools/fake 加入 sys.path（pytest 以 importlib 模式运行）。

插件 `awr.sim.targets.plugin` 在导入时向当前登记表登记；为不影响其他目录的用例，本目录的用例只在隔离登记表（`tlib.Bench`、
`R.isolated_registry()`）中导入与装配插件。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[0] / "sim"), str(_HERE.parents[0] / "contracts"), str(_HERE.parents[1] / "tools" / "fake")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_command_extension_points():
    """同 tests/sim：进程级运动提供者表随用例隔离（整棵 tests/ 一起收集时 M10 会登记 goto 等提供者）。"""
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
