"""固定翼与 VTOL 的 sim-core 进程内夹具：隔离登记表 + M21 插件 + 可控风场（M07 env stage 的替身）+ CoreHarness（sandbox100）。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from catlib import install
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R


class FwRig:
    def __init__(self, profile_id: str = "awr_f1", *, wind_enu: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 clock: str = "sandbox100", n: int = 1) -> None:
        self._reg_ctx = R.isolated_registry()
        reg = self._reg_ctx.__enter__()
        self.wind = np.array(wind_enu, np.float64)

        def env(S: Any, ctx: Any) -> None:
            idx = S.active_idx().astype(np.int64)
            if idx.size:
                S.set_wind_from_enu(np.ascontiguousarray(np.tile(self.wind, (idx.size, 1))), idx)

        R.register_stage("env", 5, 0, 20, owner="M07")(env)
        self.cat, self.rt = install()
        try:
            self.h = CoreHarness(n=n, reg=reg, profile_id=profile_id, fleet_cfg={"clock_profile": clock}, spacing=40.0)
        except Exception:
            self._reg_ctx.__exit__(None, None, None)
            raise
        self.s = self.h.slot()
        self.vid = self.h.ids()[0]

    def close(self) -> None:
        try:
            self.h.close()
        finally:
            self._reg_ctx.__exit__(None, None, None)

    def __enter__(self) -> FwRig:
        return self

    def __exit__(self, *a: Any) -> None:
        self.close()

    # ---- helpers
    @property
    def st(self) -> Any:
        return self.rt.slots[self.s]

    def cmd(self, op: str, args: dict | None = None, cid: str | None = None) -> tuple[dict, Any]:
        adm = self.h.cmd(op, args or {}, cid=cid)
        return adm, self.h.core.engine.idem.get(adm["cid"]).call if adm.get("status") == "accepted" else None

    def run_until(self, pred, timeout_s: float, dt: float = 0.5) -> bool:
        t_end = self.h.t + timeout_s
        while self.h.t < t_end:
            if pred():
                return True
            self.h.advance(dt)
        return bool(pred())

    def takeoff(self, alt: float | None = None) -> Any:
        adm, c = self.cmd("takeoff", {} if alt is None else {"alt_m": alt}, cid="tk")
        assert adm["status"] == "accepted", adm
        assert self.run_until(lambda: c.final, 120.0), c
        assert c.status == "succeeded", (c.status, c.code)
        return c

    def pos(self) -> np.ndarray:
        return self.h.S.enu.pos[self.s].copy()

    def vel(self) -> np.ndarray:
        return self.h.S.enu.vel[self.s].copy()

    def fs(self) -> int:
        return int(self.h.S.blocks["safety"]["fs"][self.s])

    def power_w(self) -> float:
        from awr.sim.core import power as PW

        return float(PW.power_by_frame(self.h.S, np.array([self.s]), self.h.core.ctx)[0])


def radius(p: np.ndarray, c: tuple[float, float]) -> float:
    return math.hypot(float(p[0]) - c[0], float(p[1]) - c[1])
