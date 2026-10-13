"""tests/catalog 公共夹具（M21）：把本目录与 tests/sim 加入 sys.path（`catlib`、`simlib`），隔离进程级的运动提供者表。

运动提供者（`register_motion_provider`）是进程级表，不随 `isolated_registry()` 切换：每个用例开始时清空、结束后恢复（同
tests/sim/conftest.py 的做法），M21 的固定翼提供者在用例内由 `catlib.install()` 装配。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent / "sim")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _isolate_command_extension_points():
    from awr.sim.core import command as CMD

    provs = CMD.motion_providers()
    fw = {k: CMD.fw_provider(k) for k in (1, 2)}
    fine = list(CMD._FINE)
    CMD.reset_motion_providers()
    CMD._FINE[:] = []
    try:
        yield
    finally:
        CMD.reset_motion_providers()
        for p in {id(p): p for p in provs.values()}.values():
            CMD.register_motion_provider(p)
        for p in {id(p): p for p in fw.values() if p is not None}.values():
            CMD.register_motion_provider(p)
        CMD._FINE[:] = fine
