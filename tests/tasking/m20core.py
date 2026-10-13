"""tests/tasking 的 sim-core 台架（M20）：隔离登记表 + M09 + M18（+ 可选 M13、M19）+ M20，CoreHarness 单步驱动（sandbox100，平地世界）。

规划作业在没有 M10 规划池时同步执行（`JobRunner` 的 inline 路径，下一 tick 生效）。契约校验用 tests/contracts 的 `ctlib`。
"""

from __future__ import annotations

from typing import Any

from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


class Core:
    def __init__(self, *, perception: bool = False, world: Any = None, clock: str = "sandbox100", seat: bool = True) -> None:
        self.cm = R.isolated_registry()
        self.reg = self.cm.__enter__()
        from awr.sim import safety

        self._safety_mod = safety
        self._safety_prev = safety.SERVICE
        if "safety" in self.reg.blocks:           # 首次导入时 M09 自动装配到了当前（隔离）登记表
            self.safety = safety.SERVICE
        else:
            self.safety = safety.install(metrics=False)
            safety.SERVICE = self.safety
        from awr.sim.targets import plugin as tp

        self.tp = tp
        self.trt = tp.install()
        self.sp = self.pp = None
        if perception:
            from awr.sim.perception import plugin as pp
            from awr.sim.sensors import plugin as sp

            self.sp, self.pp = sp, pp
            sp.install().sandbox = True
            self.prt = pp.install()
        from awr.sim.tasking import plugin as kp

        self.kp = kp
        self.rt = kp.install()
        self.h = CoreHarness(n=0, reg=self.reg, fleet_cfg={"clock_profile": clock}, world=world, seat=seat)
        self.sink: list[tuple[str, dict]] = []
        self.rt.sink = lambda k, m: self.sink.append((k, m))

    # ------------------------------------------------------------ 命令与查询
    def cmd(self, op: str, args: dict | None = None, **pk: Any) -> dict:
        return self.h.cmd(op, args or {}, uav=None, **pk)

    def ok(self, op: str, args: dict | None = None) -> dict:
        r = self.cmd(op, args)
        assert r.get("status") == "accepted", r
        return (r.get("detail") or {}).get("result") or {}

    def query(self, op: str, args: dict | None = None) -> dict:
        return self.reg.queries[op].fn({"op": op, "args": args or {}}, self.h.core.ctx)

    def advance(self, s: float) -> None:
        self.h.advance(s)

    def until(self, pred, timeout_s: float, dt: float = 1.0) -> bool:
        return self.h.until(pred, timeout_s, dt)

    @property
    def t(self) -> float:
        return self.h.t

    def nest(self, pos=(0.0, 0.0), n: int = 2, model: str = "p600_mid360", **kw: Any) -> tuple[str, list[str]]:
        r = self.ok("nest/add", {"nest": {"pos": [float(pos[0]), float(pos[1]), None], "inventory": [{"model_id": model, "count": n}], **kw}})
        self.advance(2.0)
        return r["id"], r["vehicle_ids"]

    def task(self, spec: dict, *, start: bool = True) -> str:
        tid = self.ok("task/create", spec)["tid"]
        self.until(lambda: self.rt.tasks[tid].state != "PLANNING", 10.0, 0.5)
        if start and self.rt.tasks[tid].state == "READY":
            self.ok("task/start", {"tid": tid})
        return tid

    def close(self) -> None:
        try:
            self.h.close()
        finally:
            self.kp.uninstall()
            if self.pp is not None:
                self.pp.uninstall()
                self.sp.uninstall()
            self.tp.uninstall()
            self._safety_mod.uninstall(self.safety)
            self._safety_mod.SERVICE = self._safety_prev
            self.cm.__exit__(None, None, None)

    def __enter__(self) -> Core:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
