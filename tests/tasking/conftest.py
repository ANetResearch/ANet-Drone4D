"""tests/tasking 公共夹具（M20）：把本目录、tests/sim、tests/targets 与 tools/fake 加入 sys.path（pytest 以 importlib 模式运行）。

插件 `awr.sim.tasking.plugin` 导入即向当前登记表登记；本目录的 sim-core 用例只在隔离登记表或 `CoreHarness` 自带的登记表中装配。
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parents[0] / "sim"), str(_HERE.parents[0] / "contracts"), str(_HERE.parents[1] / "tools" / "fake")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

ROOT = _HERE.parents[1]


@pytest.fixture(scope="session")
def synthcity():
    from awr.world.geometry.query import open_world_query

    wd = ROOT / "worlds" / "synthcity"
    if not (wd / "world.json").exists():
        pytest.skip("synthcity world not built")
    try:
        return open_world_query(wd, allow_derive=False)
    except Exception as e:  # 派生缓存缺失
        pytest.skip(f"synthcity geometry unavailable: {e}")


@pytest.fixture(autouse=True)
def _isolate_command_extension_points():
    """同 tests/sim、tests/targets：进程级运动提供者表与细校验执行者随用例隔离。"""
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
