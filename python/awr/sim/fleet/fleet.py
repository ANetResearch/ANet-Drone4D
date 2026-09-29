"""FleetSim 门面与 FleetConfig（M08 §7.1.3、§6.3.5；M08-FR-010、FR-013、FR-038、FR-040）。

`FleetSim` 持有 FleetState、ProfileTable、PathBuffer 与 Pipeline：`add(spec)` 初始化 slot（出生在地面、IDLE、按航向）、
`remove(slot)`、`step(ctx, n)` 推进 n 个主时钟 tick（4 ms）。`build_pipeline()` 装配 M08 自身的 stage 与插件登记的 stage
（`build_default`，M08 §6.4.1）：numba 模式为融合 stage `l1`，oracle 模式拆成 `refgen`…`integrate` 6 个 numpy stage。
内核选择（M08-FR-040；ADR-038）：`FleetConfig.kernel`（环境变量 `AWR_KERNEL` 覆盖）为 numba 且 numba 可用时用融合核，否则
退回 oracle（`kernel_fallback` 记原因，机群上限钳到 300 架）。M09 登记了 `fsm` stage 或 `safety` 状态块时不装配兜底 `fsm_min`。
状态块：插件登记的块优先；未登记的 safety、battery、mission 使用 M08 的兜底定义（`state.FALLBACK_BLOCKS`）。
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np

from awr.world.georef.frames import enu_to_ned, yaw_ned_from_enu

from ..backends.base import EntitySpec
from . import kernels_l1 as KL
from .path import PathBuffer
from .pipeline import TICK_NS, Pipeline, StageCtx
from .profiles import ProfileTable
from .px4lite import quat_from_yaw
from .setpoint import p_stop as _p_stop
from .stages import budgets as B
from .stages import registry as R
from .stages.clock import st_clock
from .stages.cmd_watch import st_cmd_watch
from .stages.contact import ContactCfg, ContactStage
from .stages.fsm_min import FsmMin
from .stages.ingest import IngestStage, SetpointMailbox
from .stages.kinematic import KinematicStage
from .stages.l1 import L1Oracle, L1Stage
from .stages.tap import TapStage
from .state import CAPACITY, FALLBACK_BLOCKS, CtrlMode, FleetState

__all__ = ["NUMPY_MAX_VEHICLES", "FleetConfig", "FleetSim", "resolve_kernel"]

NUMPY_MAX_VEHICLES = 300


@dataclass(frozen=True)
class FleetConfig:
    tick_hz: int = 250
    l1_every: int = 2
    env_every: int = 5
    tap_every: int = 2
    aero_override: str | None = None
    turbulence: str = "box"
    stop_motion: bool = True
    time_stretch: bool = True
    stream_watchdog_s: float = 0.25
    kernel: str = "numba"
    capacity: int = CAPACITY
    path_capacity: int = 524288
    max_batch_base: int = 5
    tick_budget_us: int = 2800
    slow_budget_us: int = 1000
    boot_s: float = 0.2
    ready_s: float = 0.5
    wall_step_m: float = 1.0
    pen_m: float = 0.3
    impact_vz_mps: float = 3.0
    land_detect_s: float = 1.0
    land_detect_land_s: float = 0.5
    faults: bool = True
    omega_fail_rad_s: float = 4.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **over) -> FleetConfig:
        e = os.environ if env is None else env
        c = cls(**over)
        k = (e.get("AWR_KERNEL") or "").strip().lower()
        if k in ("numba", "numpy"):
            c = replace(c, kernel=k)
        return c


def resolve_kernel(requested: str) -> tuple[str, str | None]:
    """返回 (实际内核, 退回原因)；numba 不可用时退回 numpy（ADR-038）。"""
    if requested == "numpy":
        return "numpy", "AWR_KERNEL=numpy"
    if not KL.HAVE_NUMBA:
        return "numpy", f"numba import failed: {KL.NUMBA_ERROR}"
    return "numba", None


class FleetSim:
    def __init__(self, cfg: FleetConfig | None = None, env: Any = None, world: Any = None, events: Any = None,
                 profiles: ProfileTable | None = None, seed: int = 0, *, reg: R.Registry | None = None,
                 paths: PathBuffer | None = None) -> None:
        self.cfg = cfg or FleetConfig()
        self.env = env
        self.world = world
        self.events = events
        T = profiles or ProfileTable()
        if self.cfg.aero_override in ("linear", "composite"):
            for pid in list(T.ids):
                T = T.with_aero(pid, self.cfg.aero_override)
        self.T = T
        self.seed = int(seed)
        self.reg = reg if reg is not None else R.registry()
        self.kernel, self.kernel_fallback = resolve_kernel(self.cfg.kernel)
        blocks = dict(self.reg.blocks)
        for name, spec in FALLBACK_BLOCKS.items():
            blocks.setdefault(name, spec)
        self.S = FleetState(self.cfg.capacity, blocks)
        self.PB = paths if paths is not None else PathBuffer(self.cfg.path_capacity)
        self.mailbox = SetpointMailbox(self.cfg.capacity)
        self.pipeline: Pipeline | None = None
        self.tap: TapStage | None = None
        self.l1: L1Stage | None = None
        self.oracle: L1Oracle | None = None
        self.kinematic = KinematicStage()
        self.uses_fallback_fsm = False

    @property
    def max_vehicles(self) -> int:
        return NUMPY_MAX_VEHICLES if self.kernel == "numpy" else min(1000, self.cfg.capacity)

    # ------------------------------------------------------------ pipeline（build_default，M08 §6.4.1）
    def build_pipeline(self, *, engine: Any = None, supervisor: Any = None, ring: Any = None,
                       lease_owner: Callable[[], np.ndarray] | None = None, roster_version: Callable[[], int] = lambda: 0,
                       lifecycle: Callable[[FleetState, StageCtx], None] | None = None,
                       wall_ns: Callable[[], int] | None = None, timer: Callable[[], int] | None = None) -> Pipeline:
        c = self.cfg
        cc = ContactCfg(c.wall_step_m, c.pen_m, c.impact_vz_mps, c.land_detect_s, c.land_detect_land_s)
        self.tap = TapStage(ring, lease_owner=lease_owner or (lambda: np.zeros(self.S.capacity, np.uint8)),
                            roster_version=roster_version, wall_ns=wall_ns, every=c.tap_every)
        mk = R.make_stage
        L1_FID = R.Fidelity.L1 | R.Fidelity.L2
        builtin = [
            *mk("clock", 1, 0, 0, st_clock, owner="M08", builtin=True),
            *mk("ingest", 1, 0, 10, IngestStage(engine=engine, supervisor=supervisor, lifecycle=lifecycle,
                                                mailbox=self.mailbox, watchdog_s=c.stream_watchdog_s),
                owner="M08", builtin=True),
        ]
        self.l1 = L1Stage(self.T, self.PB, every=c.l1_every, time_stretch=c.time_stretch, stop_motion=c.stop_motion,
                          faults=c.faults, w_fail=c.omega_fail_rad_s, kernel=self.kernel, world=self.world)
        if self.kernel == "numba":
            builtin += mk("l1", c.l1_every, 0, 30, self.l1, owner="M08", fidelity=L1_FID, builtin=True)
        else:
            self.oracle = self.l1.oracle
            for name, order, fn in self.oracle.stages():
                builtin += mk(name, c.l1_every, 0, order, fn, owner="M08", fidelity=L1_FID, builtin=True)
        builtin += [
            *mk("kinematic", c.l1_every, 0, 85, self.kinematic, owner="M08", fidelity=R.Fidelity.L0, builtin=True),
            *mk("contact", c.l1_every, 0, 90, ContactStage(self.world, cc, PT=self.T.PT, kernel=self.kernel,
                                                           capacity=self.S.capacity),
                owner="M08", fidelity=L1_FID, builtin=True),
            *mk("cmd_watch", 5, 4, 128, st_cmd_watch, owner="M08", builtin=True),
            *mk("tap", c.tap_every, 0, 140, self.tap, owner="M08", builtin=True),
        ]
        names = {B.base_name(s.name) for s in self.reg.stages}
        self.uses_fallback_fsm = "fsm" not in names and "safety" not in self.reg.blocks
        if self.uses_fallback_fsm:
            builtin += mk("fsm_min", c.l1_every, 0, 95, FsmMin(), owner="M08", builtin=True)
        self.pipeline = Pipeline.build(builtin, self.reg, timer=timer)
        return self.pipeline

    def step(self, ctx: StageCtx, n_ticks: int = 1) -> None:
        """推进 n 个主时钟 tick；ctx.tick 为已执行的 tick 数，本函数先 +1 再执行（t_sim_ns = tick × 4 ms）。"""
        assert self.pipeline is not None, "build_pipeline() 未调用"
        if ctx.profiles is None:
            ctx.profiles = self.T
        if ctx.paths is None:
            ctx.paths = self.PB
        for k in range(n_ticks):
            ctx.tick += 1
            ctx.t_ns = ctx.tick * TICK_NS
            ctx.batch_remaining = n_ticks - 1 - k
            self.pipeline.run_tick(self.S, ctx)

    # ------------------------------------------------------------ 增删
    def add(self, spec: EntitySpec, *, slot: int, agent_no: int, entity_id: str, t_s: float = 0.0,
            fidelity: R.Fidelity = R.Fidelity.L1) -> int:
        S = self.S
        if S.active[slot]:
            raise ValueError(f"slot {slot} 已在用")
        pid = self.T.index(spec.profile_id)
        z = spec.home_enu_m[2]
        home_enu = np.array([spec.home_enu_m[0], spec.home_enu_m[1], 0.0 if z is None else z], np.float64)
        home = enu_to_ned(home_enu)
        yaw = float(yaw_ned_from_enu(spec.yaw_rad))
        S.active[slot] = True
        S.fidelity[slot] = int(fidelity)
        S.agent_no[slot] = agent_no
        S.ids[slot] = entity_id
        S.profile_id[slot] = pid
        S.limits_id[slot] = self.T.limits_index(spec.limits_profile, spec.profile_id)
        S.home[slot] = home
        for arr in (S.p, S.p_prev, S.tr_x, S.target, S.pos_ref, S.pos_sp):
            arr[slot] = home
        for arr in (S.v, S.a_meas, S.omega, S.tr_v, S.tr_a, S.vel_int, S.thr_sp, S.wind, S.vel_cmd, S.force, S.axis_anchor):
            arr[slot] = 0.0
        S.q[slot] = quat_from_yaw(np.array([yaw]))[0]
        S.q_sp[slot] = S.q[slot]
        S.yaw_sp[slot] = yaw
        S.thrust[slot] = 0.0
        S.thr_cap[slot] = 1.0
        S.ctrl_mode[slot] = CtrlMode.KINEMATIC if fidelity == R.Fidelity.L0 else CtrlMode.IDLE
        S.ctrl_phase[slot] = 0
        S.mode_t[slot] = t_s
        S.mode_evt[slot] = 0
        S.land_xy[slot] = home[:2]
        S.ground_z[slot] = home[2]
        S.agl[slot] = 0.0
        S.landed[slot] = True
        S.in_contact[slot] = True
        S.in_air[slot] = False
        S.crash_sub[slot] = 0
        S.contact_t[slot] = np.nan
        S.td_t[slot] = np.nan
        S.speed_cmd[slot] = np.nan
        S.v_rtl[slot] = np.nan
        S.stopping[slot] = False
        S.rho[slot] = 1.225
        S.thrust_scale[slot] = 1.0
        S.motor_ok[slot] = 0xFF
        S.est_age_s[slot] = 0.0
        S.vel_sess[slot] = False
        S.axis_lock[slot] = 0
        S.path_len[slot] = 0
        S.path_tau[slot] = 0.0
        S.orb[slot] = 0.0
        S.desc_v[slot] = 0.5
        S.vel_vmax[slot] = np.inf
        S.d_free[slot] = np.inf
        S.hold_alt[slot] = True
        for blk in S.blocks.values():
            for arr in blk.values():
                arr[slot] = 0
        if "battery" in S.blocks:
            S.blocks["battery"]["battery_pct"][slot] = 255
            S.blocks["battery"]["soc"][slot] = spec.initial_soc
        if "mission" in S.blocks:
            S.blocks["mission"]["mission_item"][slot] = 0xFFFF
        if "safety" in S.blocks and "d_free_fence_m" in S.blocks["safety"]:
            S.blocks["safety"]["d_free_fence_m"][slot] = np.inf
        self.mailbox.clear(slot)
        S.touch()
        return slot

    def remove(self, slot: int) -> None:
        S = self.S
        if S.path_len[slot] > 0:
            self.PB.release(int(S.path_off[slot]))
            S.path_len[slot] = 0
        S.active[slot] = False
        S.ids[slot] = None
        S.ctrl_mode[slot] = CtrlMode.IDLE
        S.vel_sess[slot] = False
        self.kinematic.drop(slot)
        S.touch()

    # ------------------------------------------------------------ 视图与助手（M07、M09、M10、M13）
    def pos_enu_view(self) -> np.ndarray:
        return self.S.enu.pos

    def vel_enu_view(self) -> np.ndarray:
        return self.S.enu.vel

    def set_wind_from_enu(self, w_enu: np.ndarray) -> None:
        self.S.set_wind_from_enu(w_enu)

    def p_stop(self, slots: np.ndarray) -> np.ndarray:
        """刹停点（World ENU，m；供 M09 围栏折线 [p, p_stop, goal]，M08-FR-022）。"""
        ps = _p_stop(self.S, self.T.LT, np.asarray(slots, np.int64))
        return np.stack([ps[:, 1], ps[:, 0], -ps[:, 2]], 1)

    # ------------------------------------------------------------ checkpoint（FR-083，ext）
    def checkpoint_arrays(self) -> dict[str, np.ndarray]:
        out = self.S.checkpoint_arrays()
        for k, a in self.PB.checkpoint_arrays().items():
            out[f"pb.{k}"] = a
        return out

    def checkpoint_meta(self) -> dict:
        S = self.S
        return {"ids": list(S.ids), "tick": int(S.tick), "t_ns": int(S.t_ns), "pb": self.PB.checkpoint_meta(),
                "kernel": self.kernel, "kinematic": self.kinematic.checkpoint_meta()}

    def restore(self, arrays: dict[str, np.ndarray], meta: dict) -> None:
        S = self.S
        S.restore_arrays({k: v for k, v in arrays.items() if not k.startswith("pb.")})
        self.PB.restore({k[3:]: v for k, v in arrays.items() if k.startswith("pb.")}, meta.get("pb", {}))
        S.ids[:] = list(meta.get("ids", [None] * S.capacity))
        S.tick = int(meta.get("tick", S.tick))
        S.t_ns = int(meta.get("t_ns", S.t_ns))
        self.kinematic.restore_meta(meta.get("kinematic") or {})
        S.touch()


_ = math
