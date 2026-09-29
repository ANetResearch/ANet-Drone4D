"""agent-runtime 进程（AgentRuntimeApp）在测试线程中运行：LocalBus（与 api、sim 同 namespace）+ LocalRing。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from awr.contracts import LAYOUT_ID
from awr.runtime.bus import LocalBus
from awr.runtime.statering import LOSSY, LocalRing


class StubCtx:
    def __init__(self, settings: Any, persist: Path) -> None:
        self.run_id = settings.run_id
        self.world_id = settings.world_id if hasattr(settings, "world_id") else settings.world
        self.run_dir = Path(settings.run_dir)
        self.persist_dir = persist
        self._secret = settings.secret
        self.restart_count = 0
        self.stops: list[Any] = []

    def read_secret(self) -> bytes:
        return self._secret

    def heartbeat_writer(self) -> Any:
        class _Hb:
            def beat(self) -> None:
                return None

            def close(self) -> None:
                return None

        return _Hb()

    def add_stop_callback(self, cb: Any) -> None:
        self.stops.append(cb)


class AppThread:
    def __init__(self, settings: Any, persist: Path) -> None:
        self.settings = settings
        self.ctx = StubCtx(settings, persist)
        self.loop = asyncio.new_event_loop()
        self.app: Any = None
        self.thread = threading.Thread(target=self._run, name="agent-runtime", daemon=True)
        self.done = threading.Event()
        self.code: int | None = None

    def _run(self) -> None:
        from awr.agent.runtime.app import AgentRuntimeApp

        asyncio.set_event_loop(self.loop)
        bus = LocalBus.open("agent-runtime", namespace=self.settings.namespace, loop=self.loop)
        ring = LocalRing.attach(Path(self.settings.ring_path), expect_layout_id=LAYOUT_ID)
        ring.register(LOSSY, "agent-runtime")
        self.app = AgentRuntimeApp(self.ctx, bus=bus, ring=ring)
        try:
            self.code = self.loop.run_until_complete(self.app.run())
            rest = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
            for t in rest:
                t.cancel()
            if rest:
                self.loop.run_until_complete(asyncio.wait(rest, timeout=5))
        finally:
            self.done.set()

    def start(self, timeout: float = 20.0) -> AppThread:
        self.thread.start()
        end = time.monotonic() + timeout
        while (self.app is None or self.app.state != "READY") and time.monotonic() < end and not self.done.is_set():
            time.sleep(0.02)
        if self.app is None or self.app.state != "READY":
            raise RuntimeError("agent-runtime 未就绪")
        return self

    def call(self, fn: Any) -> Any:
        box: dict[str, Any] = {}
        ev = threading.Event()

        def run() -> None:
            try:
                box["v"] = fn()
            except BaseException as ex:
                box["e"] = ex
            ev.set()

        self.loop.call_soon_threadsafe(run)
        ev.wait(10)
        if "e" in box:
            raise box["e"]
        return box.get("v")

    def stop(self) -> None:
        if self.app is not None and not self.done.is_set():
            self.loop.call_soon_threadsafe(self.app.request_stop)
            self.done.wait(15)
        self.thread.join(5)
