"""M10 进程内测试台：隔离登记表中 `install()` M10 插件，SimCore 以注入的假墙钟单步驱动（LocalBus、LocalRing），
世界为 M04 合成小世界或已构建的城市；plan-pool 为 inline（结果在下一个步边界生效）。"""

from __future__ import annotations

import contextlib
import secrets
from pathlib import Path
from typing import Any

import numpy as np

from awr.contracts import LAYOUT_ID
from awr.runtime.bus import LocalBus
from awr.runtime.statering import LocalRing
from awr.sim.core import metrics as MET
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.config import SimConfig
from awr.sim.runtime.main import SimCore

ROOT = Path(__file__).resolve().parents[2]
OP = {"principal_id": "p-op", "role": "operator", "entry": "api", "seat": True}


class Sim:
    def __init__(self, world: Any, *, n: int = 1, profile_id: str = "x500", spawn_xy=(-150.0, -100.0),
                 spacing: float = 20.0, world_id: str | None = None) -> None:
        import awr.sim.mission as M10
        from awr.sim.core import command as CMD

        self._stack = contextlib.ExitStack()
        # 运动提供者与细校验执行者是进程级表：结束时恢复，避免本测试台的运行时残留给同进程中其他用例
        provs, fine = CMD.motion_providers(), list(CMD._FINE)

        def _restore() -> None:
            CMD.reset_motion_providers()
            for p in {id(p): p for p in provs.values()}.values():
                CMD.register_motion_provider(p)
            CMD._FINE[:] = fine

        self._stack.callback(_restore)
        self.reg = self._stack.enter_context(R.isolated_registry())
        self._stack.enter_context(MET.isolated_metrics())
        self.rt = M10.install()
        self.W = [1_000_000_000]
        run = "rt" + secrets.token_hex(4)
        self.path = f"/m10t/{run}/state.sim-core"
        self.ring, _ = LocalRing.open_or_create(self.path, capacity=1024, slots=32, layout_id=LAYOUT_ID, id_base=0,
                                                id_count=1024)
        self.bus = LocalBus.open("sim-core", namespace=f"awr/test/{run}")
        cfg = SimConfig(run_id=run, world_id=world_id or getattr(world, "world_id", "tiny"), n_vehicles=n,
                        load_world=False, autoplay=True, spawn_xy=spawn_xy, profile_id=profile_id,
                        spawn_spacing_m=spacing)
        self.core = SimCore(cfg, self.bus, self.ring, secret=None, wall_ns=lambda: self.W[0], reg=self.reg,
                            world=world)
        self.events: list[tuple[float, str, dict]] = []
        orig = self.core.events.emit

        def emit(kind: str, **kw: Any) -> int:
            data = {k: v for k, v in kw.items() if k not in ("t_sim_ns", "fields")} | dict(kw.get("fields") or {})
            self.events.append((kw.get("t_sim_ns", 0) * 1e-9, kind, data))
            return orig(kind, **kw)

        self.core.events.emit = emit
        self.core.start()
        self.results: dict[str, tuple[str, int, Any]] = {}
        self.core.engine.subscribe_results("", lambda c: self.results.__setitem__(
            c.cid, (c.status, int(c.code or 0), dict(c.effect or {}).get("metrics"))))
        self.core._lease_op({"v": 1, "cid": "seat", "op": "seat_claim", "principal": OP})
        self._n = 0

    @property
    def ids(self) -> list[str]:
        return [e.id for e in self.core.roster.by_slot.values()]

    def slot(self, vid: str) -> int:
        return self.core.roster.resolve(vid).slot

    def pos(self, vid: str) -> np.ndarray:
        return self.core.fleet.S.enu.pos[self.slot(vid)].copy()

    def advance(self, seconds: float) -> None:
        """推进仿真时间；时钟暂停（例如剧本结束 on_complete = pause）时按墙钟空转后返回。"""
        end = self.core.clock.t_ns + int(seconds * 1e9)
        idle = 0
        while self.core.clock.t_ns < end and idle < 50:
            t0 = self.core.clock.t_ns
            self.W[0] += 5 * TICK_NS * max(1, int(self.core.clock.rate))
            self.core.iterate()
            idle = idle + 1 if self.core.clock.t_ns == t0 else 0

    def until(self, pred, timeout_s: float, step_s: float = 0.5) -> bool:
        end = self.core.clock.t_ns + int(timeout_s * 1e9)
        while self.core.clock.t_ns < end:
            if pred():
                return True
            self.advance(step_s)
        return bool(pred())

    def cmd(self, op: str, args: dict, uav: str, cid: str | None = None) -> dict:
        self._n += 1
        cid = cid or f"t{self._n:04d}"
        return self.core.engine.handle({"v": 1, "cid": cid, "op": op, "uav": uav, "args": args, "principal": OP,
                                        "lease": None, "t_wall_ns": 0, "epoch_seen": 1, "batch_id": None})

    def kinds(self, prefix: str) -> list[tuple[float, str, dict]]:
        return [e for e in self.events if e[1].startswith(prefix)]

    def close(self) -> None:
        try:
            self.core.stop()
        finally:
            self.rt.close()
            self.bus.close()
            LocalRing.remove(self.path)
            self._stack.close()
