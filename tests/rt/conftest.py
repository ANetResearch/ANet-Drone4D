"""tests/rt 公共夹具（M11 网关）：进程内栈（LocalBus + LocalRing + 后台 sim-core 线程 + uvicorn 线程）。

辅助客户端在 rtc.py（--import-mode=importlib 下经 sys.path 垫片导入）。进程内栈按模块共享，避免重复加载世界与预热。
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rtc

WORLD = rtc.WORLD


class Stack:
    def __init__(self, *, n: int = 1, hello_timeout_s: float = 1.0) -> None:
        import uvicorn

        from awr.api.inproc import InprocSim, inproc_settings
        from awr.api.main import create_app
        from awr.runtime.statering import LocalRing

        self.settings = replace(inproc_settings(WORLD), hello_timeout_s=hello_timeout_s)
        self.sim = InprocSim(self.settings, n=n).start()
        self.app = create_app(self.settings, ring_cls=LocalRing)
        self.port = rtc.free_port()
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, ws="websockets", log_level="warning",
                             lifespan="on", ws_per_message_deflate=False, ws_max_size=262144)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, name="uvicorn", daemon=True)
        self.thread.start()
        end = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > end or not self.thread.is_alive():
                raise RuntimeError("uvicorn 未启动")
            time.sleep(0.02)
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_url = f"ws://127.0.0.1:{self.port}/api/rt"
        self.origin = f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(10)
        self.sim.stop()
        shutil.rmtree(self.settings.run_dir, ignore_errors=True)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items) -> None:
    """用 `stack`（加载已构建的深圳）的用例补 needs_data 标记，使托管 CI 的 `-m "not needs_data"` 不收集它们（AWR-18 §8.2，SHOW-CI）。"""
    here = Path(__file__).resolve().parent
    for item in items:
        if "stack" in getattr(item, "fixturenames", ()) and here in Path(str(item.path)).resolve().parents:
            item.add_marker(pytest.mark.needs_data)


@pytest.fixture(scope="module")
def stack():
    if not rtc.world_ready():
        pytest.skip(f"worlds/{WORLD} 未构建（make worlds）")
    s = Stack()
    yield s
    s.close()


@pytest.fixture(scope="module")
def gws():
    """FakeSim（3 架）+ 真实 Gateway 应用 + uvicorn 线程；按模块共享（不加载世界，不依赖 awr.sim）。"""
    import fakesim

    s = fakesim.GwStack(n=3)
    yield s
    s.close()


# ------------------------------------------------------------------------------------------------ 进程级扩展点隔离（INT-1）
# 进程内栈的 InprocSim 不装配插件（plugins=()），但整棵 tests/ 一起收集时 tests/mission 在收集阶段 import awr.sim.mission，
# M10 自动 install() 把 goto 等运动提供者与细校验执行者登记进进程级表：本目录的 goto 随之交给没有 stage 驱动的 M10 提供者，
# 以截止 202 结束（test_chain_inproc 单独运行通过、全量失败）。每个用例开始时清空、结束后恢复（同 tests/sim/conftest.py）。
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
