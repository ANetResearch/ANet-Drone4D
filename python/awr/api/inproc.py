"""`--inproc` 单进程模式：LocalBus + LocalRing，sim-core 主循环在同一进程的后台线程（AWR-17 §9.8；M11-FR-102）。

只用于单元测试、集成测试与 ≤ 50 架的降级演示，不参与性能验收。awr.api 不静态依赖 awr.sim（AWR-03 §4.2 依赖规则），
sim-core 入口以 `importlib` 按名称加载（`awr.sim.runtime.main.run`）。

用法：`python -m awr.api.inproc [--world shenzhen] [--port 8000] [--n 1] [--no-autoplay]`；测试用 `InprocSim` 与
`inproc_settings()` 直接组装（见 tests/rt/test_skeleton_chain.py）。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import secrets
import shutil
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from awr.contracts import LAYOUT_ID
from awr.runtime.bus import LocalBus
from awr.runtime.statering import LocalRing

from .settings import ApiSettings

__all__ = ["InprocSim", "inproc_settings", "main"]


def inproc_settings(world_id: str = "shenzhen", *, run_dir: Path | None = None, **over: Any) -> ApiSettings:
    run_id = "inproc-" + secrets.token_hex(3)
    base = Path("/dev/shm") if Path("/dev/shm").is_dir() else None
    rd = run_dir or Path(tempfile.mkdtemp(prefix="awr-inproc-", dir=base))
    s = ApiSettings(run_id=run_id, world_id=world_id, run_dir=rd, persist_dir=rd, bus_kind="local", profile="dev")
    return replace(s, **over) if over else s


class InprocSim:
    """在后台线程运行 sim-core（LocalRing 与 LocalBus 按 run 目录与 namespace 与 api 汇合）。"""

    def __init__(self, settings: ApiSettings, *, n: int = 1, autoplay: bool = True, spawn_xy: tuple[float, float] | None = None,
                 load_world: bool = True, plugins: tuple[str, ...] = ()) -> None:
        self.settings = settings
        rt = importlib.import_module("awr.sim.runtime.main")
        cfg_mod = importlib.import_module("awr.sim.runtime.config")
        self._run = rt.run
        self.cfg = cfg_mod.SimConfig(world_id=settings.world_id, worlds_dir=settings.worlds_dir, run_id=settings.run_id,
                                     n_vehicles=n, autoplay=autoplay, spawn_xy=spawn_xy, load_world=load_world,
                                     plugins=tuple(plugins))
        self.ring, reused = LocalRing.open_or_create(settings.ring_path, capacity=1024, slots=32, layout_id=LAYOUT_ID,
                                                     id_base=0, id_count=1024)
        self.reused = reused
        self.bus = LocalBus.open("sim-core", namespace=settings.namespace)
        self.stop_ev = threading.Event()
        self.ready = threading.Event()
        self.core: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._main, name="sim-core", daemon=True)

    def _main(self) -> None:
        try:
            self.core = self._run(self.cfg, self.bus, self.ring, stop=self.stop_ev, secret=self.settings.secret,
                                  reused=self.reused, ready=self.ready)
        except BaseException as e:
            self.error = e
            self.ready.set()

    def start(self, timeout: float = 30.0) -> InprocSim:
        self.thread.start()
        if not self.ready.wait(timeout):
            raise TimeoutError("sim-core 未在时限内就绪")
        if self.error is not None:
            raise RuntimeError("sim-core 启动失败") from self.error
        return self

    def stop(self, *, remove: bool = True) -> None:
        self.stop_ev.set()
        self.thread.join(5.0)
        with contextlib.suppress(Exception):
            self.bus.close()
        if remove:
            with contextlib.suppress(Exception):
                LocalRing.remove(self.settings.ring_path)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from .main import create_app

    ap = argparse.ArgumentParser(prog="python -m awr.api.inproc", description="AWR 单进程模式（LocalBus + LocalRing）")
    ap.add_argument("--world", default="shenzhen")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--no-autoplay", action="store_true")
    ap.add_argument("--log-level", default="warning")
    a = ap.parse_args(argv)
    s = inproc_settings(a.world)
    sim = InprocSim(s, n=a.n, autoplay=not a.no_autoplay).start()
    print(f"READY inproc run={s.run_id} world={s.world_id} api http://{a.host}:{a.port}/world/{s.world_id}", flush=True)
    print(f"  admin    {s.admin_password}", flush=True)
    try:
        uvicorn.run(create_app(s, ring_cls=LocalRing), host=a.host, port=a.port, ws="websockets",
                    ws_per_message_deflate=False, ws_max_size=262144, log_level=a.log_level)
    finally:
        sim.stop()
        shutil.rmtree(s.run_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
