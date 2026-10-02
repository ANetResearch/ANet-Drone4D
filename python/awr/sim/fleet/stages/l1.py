"""stage `l1`（order 030，every 2，125 Hz）：refgen → pos_ctrl → att_ctrl → motor → aero → integrate（M08 §6.4.1、§6.5）。

- numba 模式：一个融合 stage `l1`，调用 `kernels_l1.tick_l1`（M08-FR-038）；
- oracle 模式（`AWR_KERNEL=numpy` 或 numba 不可用）：拆成 6 个 numpy stage `refgen`（030）、`pos_ctrl`（040）、`att_ctrl`（050）、
  `motor`（060）、`aero`（070）、`integrate`（080），顺序与数学相同（M08-FR-039）；`L1Oracle.run_all()` 一次执行全部 6 段
  （对拍用）。

只处理 fidelity 含 L1 或 L2 的 slot；TRAJ 槽位的参考由运动提供者的跟踪器（order 027）写入。RTL 阶段取 safety 块的 `sub`
（FlightState 为 RTL 时，否则视为 CLIMB）；Velocity 槽位的方向自由距离 `d_free` 取 M04 `free_distance(p, dir, 200 m)` 与 M09
`d_free_fence_m` 的较小者（仅对活动 Velocity 会话计算）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from awr.contracts.enums import FlightState

from .. import aero as AE
from .. import kernels_l1 as K
from .. import params_px4 as P
from .. import px4lite, setpoint
from .registry import Fidelity

if TYPE_CHECKING:
    from ..path import PathBuffer
    from ..pipeline import StageCtx
    from ..profiles import ProfileTable
    from ..state import FleetState

__all__ = ["L1Oracle", "L1Stage", "l1_indices", "prepare_inputs"]

_L12 = int(Fidelity.L1 | Fidelity.L2)


def l1_indices(S: FleetState) -> np.ndarray:
    return np.flatnonzero(S.active & ((S.fidelity & _L12) != 0)).astype(np.int32)


def rtl_phase_of(S: FleetState) -> np.ndarray:
    sb = S.blocks.get("safety")
    if sb is None:
        return np.zeros(S.capacity, np.uint8)
    return np.where(sb["fs"] == int(FlightState.RTL), sb["sub"], 0).astype(np.uint8)


def prepare_inputs(S: FleetState, idx: np.ndarray, world: Any) -> None:
    """Velocity 槽位的方向自由距离（M08-FR-029）。"""
    vi = idx[S.ctrl_mode[idx] == K.M_VELOCITY]
    if vi.size == 0:
        return
    vc = S.vel_cmd[vi].copy()
    body = S.vel_frame[vi] == 1
    if body.any():
        psi = px4lite.yaw_from_quat(S.q[vi])
        n_ = vc[:, 0] * np.cos(psi) + vc[:, 1] * np.sin(psi)
        e_ = vc[:, 0] * np.sin(psi) - vc[:, 1] * np.cos(psi)
        vc = np.where(body[:, None], np.stack([n_, e_, -vc[:, 2]], 1), vc)
    nrm = np.linalg.norm(vc, axis=1)
    d = np.full(vi.size, np.inf)
    mv = nrm > 1e-6
    if world is not None and mv.any():
        o_enu = np.stack([S.p[vi, 1], S.p[vi, 0], -S.p[vi, 2]], 1)[mv]
        dirs = vc[mv] / nrm[mv, None]
        d_enu = np.stack([dirs[:, 1], dirs[:, 0], -dirs[:, 2]], 1)
        try:
            d[mv] = np.asarray(world.free_distance(o_enu, d_enu, P.VEL_FREE_MAX_M), np.float64)
        except Exception:
            d[mv] = np.inf
    sb = S.blocks.get("safety")
    if sb is not None and "d_free_fence_m" in sb:
        fence = sb["d_free_fence_m"][vi].astype(np.float64)
        fence = np.where(np.isnan(fence) | (fence <= 0), np.inf, fence)
        d = np.minimum(d, fence)
    S.d_free[vi] = d


class L1Oracle:
    """numpy oracle 的 6 个 stage（共享参数表与 PathBuffer；类别在 refgen 阶段按初始运动模式判定并存入 `S.l1_cls`）。"""

    def __init__(self, T: ProfileTable, PB: PathBuffer | None, *, every: int = 2, time_stretch: bool = True,
                 stop_motion: bool = True, faults: bool = True, w_fail: float = 4.0, world: Any = None) -> None:
        self.T = T
        self.PB = PB
        self.every = every
        self.time_stretch = time_stretch
        self.stop_motion = stop_motion
        self.faults = faults
        self.w_fail = w_fail
        self.world = world

    # 类别掩码
    def _cls(self, S: FleetState) -> tuple[np.ndarray, np.ndarray]:
        idx = l1_indices(S)
        c = S.l1_cls[idx]
        run = idx[(c == setpoint.CLS_ACTIVE) | (c == setpoint.CLS_FALL)]
        return run, S.l1_cls[run] == setpoint.CLS_FALL

    def refgen(self, S: FleetState, ctx: StageCtx) -> None:
        idx = l1_indices(S)
        S.l1_cls[:] = setpoint.CLS_NONE
        if idx.size == 0:
            return
        cls = setpoint.classify(S, idx)
        S.l1_cls[idx] = cls
        setpoint.park(S, idx[cls == setpoint.CLS_PARKED])
        act = idx[cls == setpoint.CLS_ACTIVE]
        prepare_inputs(S, act, self.world)
        setpoint.refgen(S, self.T.LT, self.PB, act, ctx.dt_tick * self.every, ctx.t_ns * 1e-9,
                        time_stretch=self.time_stretch, rtl_phase=rtl_phase_of(S))

    def pos_ctrl(self, S: FleetState, ctx: StageCtx) -> None:
        run, fall = self._cls(S)
        px4lite.pos_ctrl(S, self.T.PT, self.T.LT, run, fall, ctx.dt_tick * self.every)

    def att_ctrl(self, S: FleetState, ctx: StageCtx) -> None:
        run, fall = self._cls(S)
        px4lite.att_ctrl_l1(S, self.T.LT, run, fall, ctx.dt_tick * self.every, faults=self.faults, w_fail=self.w_fail)

    def motor(self, S: FleetState, ctx: StageCtx) -> None:
        run, fall = self._cls(S)
        px4lite.motor(S, self.T.PT, run, fall, ctx.dt_tick * self.every)

    def aero(self, S: FleetState, ctx: StageCtx) -> None:
        run, _ = self._cls(S)
        AE.aero(S, self.T.PT, run, faults=self.faults)

    def integrate(self, S: FleetState, ctx: StageCtx) -> None:
        run, _ = self._cls(S)
        AE.integrate(S, self.T.PT, run, ctx.dt_tick * self.every)
        S.touch()

    def run_all(self, S: FleetState, ctx: StageCtx) -> None:
        for f in (self.refgen, self.pos_ctrl, self.att_ctrl, self.motor, self.aero, self.integrate):
            f(S, ctx)

    def stages(self) -> list[tuple[str, int, Any]]:
        return [("refgen", 30, self.refgen), ("pos_ctrl", 40, self.pos_ctrl), ("att_ctrl", 50, self.att_ctrl),
                ("motor", 60, self.motor), ("aero", 70, self.aero), ("integrate", 80, self.integrate)]


class L1Stage:
    """融合 stage：numba 核（`kernel = "numba"`）或依次执行 oracle 的 6 段（`kernel = "numpy"`）。"""

    def __init__(self, T: ProfileTable, PB: PathBuffer | None = None, *, every: int = 2, time_stretch: bool = True,
                 stop_motion: bool = True, faults: bool = True, w_fail: float = 4.0, kernel: str = "numba",
                 world: Any = None) -> None:
        self.T = T
        self.PB = PB
        self.every = every
        self.flags = float((1 if time_stretch else 0) | (2 if stop_motion else 0))
        self.faults = 1.0 if faults else 0.0
        self.w_fail = float(w_fail)
        self.kernel = kernel if (kernel != "numba" or K.HAVE_NUMBA) else "numpy"
        self.world = world
        self.oracle = L1Oracle(T, PB, every=every, time_stretch=time_stretch, stop_motion=stop_motion, faults=faults,
                               w_fail=w_fail, world=world)

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        if self.kernel != "numba":
            self.oracle.run_all(S, ctx)
            return
        idx = l1_indices(S)
        if idx.size == 0:
            return
        prepare_inputs(S, idx, self.world)
        rp = rtl_phase_of(S)
        K.run_l1(S, self.T.PT, self.T.LT, self.PB, idx, ctx.dt_tick * self.every, ctx.t_ns * 1e-9, self.flags,
                 self.w_fail, self.faults, rp)
        S.touch()
        # 同一 tick 内 contact 复用（本 tick 在 l1 与 contact 之间不改变 active、fidelity 与 M09 RTL 子阶段；FX-SIM1）
        S.l1_tick_cache = (S.tick, idx, rp)
