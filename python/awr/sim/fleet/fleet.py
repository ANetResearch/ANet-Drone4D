"""FleetSim 门面与 FleetConfig（M08 §7.1.3、§6.3.5；M08-FR-010、FR-013、FR-038、FR-040）。

`FleetSim` 持有 FleetState、ProfileTable、PathBuffer 与 Pipeline：`add(spec)` 初始化 slot（出生在地面、IDLE、按航向）、
`remove(slot)`、`step(ctx, n)` 推进 n 个主时钟 tick（4 ms）。`build_pipeline()` 装配 M08 自身的 stage 与插件登记的 stage
（`build_default`，M08 §6.4.1）：numba 模式为融合 stage `l1`，oracle 模式拆成 `refgen`…`integrate` 6 个 numpy stage。
内核选择（M08-FR-040；ADR-038）：`FleetConfig.kernel`（环境变量 `AWR_KERNEL` 覆盖）为 numba 且 numba 可用时用融合核，否则
退回 oracle（`kernel_fallback` 记原因，机群上限钳到 300 架）。M09 登记了 `fsm` stage 或 `safety` 状态块时不装配兜底 `fsm_min`。
状态块：插件登记的块优先；未登记的 safety、battery、mission 使用 M08 的兜底定义（`state.FALLBACK_BLOCKS`）。

D2（M08-FR-201、FR-202、FR-210 至 FR-212）：时钟档由 `FleetConfig.clock_profile`（空串为进程当前档位，`AWR_CLOCK_PROFILE`）给出；
`step` 按档位的 tick 长度推进 `t_sim_ns`，pipeline 按档调度表解析（L1 every 1 @ 100 Hz 等）。spawn 时写构型列 `frame_kind`，
固定翼的 slot 置 `L1 | FW`（VTOL 以 MC 模式出生）；`set_fidelity` 入队、由 ingest 在步边界执行（`kinematic_write.FidelityOps`）。
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
from ..runtime import clock_profile as CP
from . import kernels_l1 as KL
from .kinematic_write import FidelityOps
from .path import PathBuffer
from .pipeline import Pipeline, StageCtx
from .profiles import ProfileTable
from .px4lite import quat_from_yaw
from .setpoint import p_stop as _p_stop
from .stages import budgets as B
from .stages import registry as R
from .stages.clock import st_clock
from .stages.cmd_watch import st_cmd_watch
from .stages.contact import CollideStage, ContactCfg, ContactStage
from .stages.fsm_min import FsmMin
from .stages.ingest import IngestStage, SetpointMailbox
from .stages.kinematic import KinematicStage
from .stages.l1 import L1Oracle, L1Stage
from .stages.tap import TapStage
from .state import CAPACITY, FALLBACK_BLOCKS, FRAME_FIXED_WING, CtrlMode, FleetState

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
    tick_budget_us: int = 2300  # 慢任务按"本 tick 预算 − 已用"填充的目标（ADR-070：2800 → 2300 µs，为墙钟抖动留出单步 p99 余量）
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
    clock_profile: str = ""  # 时钟档（M08-FR-201）：空串按 AWR_CLOCK_PROFILE（缺省 d1_250）；写入 meta.json 与输入日志头

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, **over) -> FleetConfig:
        e = os.environ if env is None else env
        c = cls(**over)
        k = (e.get("AWR_KERNEL") or "").strip().lower()
        if k in ("numba", "numpy"):
            c = replace(c, kernel=k)
        cp = (e.get("AWR_CLOCK_PROFILE") or "").strip()
        if cp and not c.clock_profile:
            c = replace(c, clock_profile=CP.get(cp).name)
        if "capacity" not in over:
            # 容量：AWR_FLEET_CAPACITY，否则按时钟档（sandbox100 为 64，ClockProfile.fleet_capacity；D2-AC-08），否则 1024
            cap_env = (e.get("AWR_FLEET_CAPACITY") or "").strip()
            if cap_env.isdigit() and int(cap_env) > 0:
                c = replace(c, capacity=int(cap_env))
            else:
                fc = c.profile().fleet_capacity
                if fc > 0:
                    c = replace(c, capacity=int(fc))
        return c

    def profile(self) -> CP.ClockProfile:
        """显式档位，否则按 `AWR_CLOCK_PROFILE`（缺省 d1_250）；不取进程当前档位（同一进程内先后构造的测试台互不影响）。"""
        return CP.get(self.clock_profile) if self.clock_profile else CP.from_env()


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
                 paths: PathBuffer | None = None, profile: CP.ClockProfile | str | None = None) -> None:
        self.cfg = cfg or FleetConfig()
        self.clock_profile = CP.get(profile) if profile is not None else self.cfg.profile()
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
        self.fidelity_ops = FidelityOps()  # VTOL 保真度切换（M08-FR-212）：M21 经 set_fidelity 入队，ingest 执行

    def set_fidelity(self, slots: Any, *, add: int = 0, remove: int = 0) -> None:
        """VTOL 保真度切换（M08-FR-212）：只入队，下一步边界由 ingest 执行（写输入日志）。"""
        self.fidelity_ops.set_fidelity(slots, add=add, remove=remove)

    def frame_kind_view(self) -> np.ndarray:
        v = self.S.frame_kind.view()
        v.flags.writeable = False
        return v

    @property
    def max_vehicles(self) -> int:
        return NUMPY_MAX_VEHICLES if self.kernel == "numpy" else min(1000, self.cfg.capacity)

    # ------------------------------------------------------------ pipeline（build_default，M08 §6.4.1）
    def build_pipeline(self, *, engine: Any = None, supervisor: Any = None, ring: Any = None,
                       lease_owner: Callable[[], np.ndarray] | None = None, roster_version: Callable[[], int] = lambda: 0,
                       lifecycle: Callable[[FleetState, StageCtx], None] | None = None,
                       wall_ns: Callable[[], int] | None = None, timer: Callable[[], int] | None = None,
                       extra: tuple = ()) -> Pipeline:
        """装配 M08 的 stage 与插件登记的 stage（M08 §6.4.1），按时钟档解析调度（M08-FR-202）。`extra` 为组合根追加的 M08
        stage（`tap_targets` 等，StageSpec 列表）。"""
        c = self.cfg
        prof = self.clock_profile
        cc = ContactCfg(c.wall_step_m, c.pen_m, c.impact_vz_mps, c.land_detect_s, c.land_detect_land_s)
        self.tap = TapStage(ring, lease_owner=lease_owner or (lambda: np.zeros(self.S.capacity, np.uint8)),
                            roster_version=roster_version, wall_ns=wall_ns, every=c.tap_every, idle_opt=prof.tap_idle_opt)
        mk = R.make_stage
        L1_FID = R.Fidelity.L1 | R.Fidelity.L2
        fops = self.fidelity_ops
        T_ref = self

        def pre(S: FleetState, ctx: StageCtx) -> None:
            if fops.pending:
                fops.apply(S, T_ref.T, ctx.tick, ctx.t_ns * 1e-9)

        builtin = [
            *mk("clock", 1, 0, 0, st_clock, owner="M08", builtin=True),
            *mk("ingest", 1, 0, 10, IngestStage(engine=engine, supervisor=supervisor, lifecycle=lifecycle,
                                                mailbox=self.mailbox, watchdog_s=c.stream_watchdog_s, pre=pre),
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
        # PT 每次调用取 ctx.profiles.PT（ProfileTable 加入存档版本或会话克隆时写时复制重建矩阵，M08-FR-208）
        contact = ContactStage(self.world, cc, PT=None, kernel=self.kernel, capacity=self.S.capacity)
        builtin += [
            *mk("kinematic", c.l1_every, 0, 85, self.kinematic, owner="M08", fidelity=R.Fidelity.L0, builtin=True),
            *mk("contact", c.l1_every, 0, 90, contact, owner="M08", fidelity=L1_FID, builtin=True),
            # 机间碰撞 25 Hz，在奇数 tick ≡ 9 mod 10（ADR-070；此前在 contact 内、tick ≡ 6 mod 10）
            *mk("collide", 10, 9, 91, CollideStage(contact), owner="M08", fidelity=L1_FID, builtin=True),
            *mk("cmd_watch", 5, 4, 128, st_cmd_watch, owner="M08", builtin=True),
            *mk("tap", c.tap_every, 0, 140, self.tap, owner="M08", builtin=True),
        ]
        builtin += list(extra)
        names = {B.base_name(s.name) for s in self.reg.stages}
        self.uses_fallback_fsm = "fsm" not in names and "safety" not in self.reg.blocks
        if self.uses_fallback_fsm:
            builtin += mk("fsm_min", c.l1_every, 0, 95, FsmMin(), owner="M08", builtin=True)
        self.pipeline = Pipeline.build(builtin, self.reg, timer=timer, profile=prof)
        # 按档解析后的 every 回写到按 every 计步长的 M08 stage 对象（L1 物理与控制、oracle 6 段、tap 节流）
        sp = self.pipeline.spec("l1") or self.pipeline.spec("refgen")
        if sp is not None:
            self.l1.every = sp.every
            self.l1.oracle.every = sp.every
        sp = self.pipeline.spec("tap")
        if sp is not None:
            self.tap.every = sp.every
        return self.pipeline

    def step(self, ctx: StageCtx, n_ticks: int = 1) -> None:
        """推进 n 个主时钟 tick；ctx.tick 为已执行的 tick 数，本函数先 +1 再执行（t_sim_ns = tick × tick_ns，按时钟档）。"""
        assert self.pipeline is not None, "build_pipeline() 未调用"
        if ctx.profiles is None:
            ctx.profiles = self.T
        if ctx.paths is None:
            ctx.paths = self.PB
        prof = self.clock_profile
        if ctx.profile is not prof:
            ctx.profile = prof
            ctx.dt_tick = prof.dt_tick
        tick_ns = prof.tick_ns
        for k in range(n_ticks):
            ctx.tick += 1
            ctx.t_ns = ctx.tick * tick_ns
            ctx.batch_remaining = n_ticks - 1 - k
            self.pipeline.run_tick(self.S, ctx)

    # ------------------------------------------------------------ 增删
    def add(self, spec: EntitySpec, *, slot: int, agent_no: int, entity_id: str, t_s: float = 0.0,
            fidelity: R.Fidelity = R.Fidelity.L1) -> int:
        S = self.S
        if S.active[slot]:
            raise ValueError(f"slot {slot} 已在用")
        pid = self.T.index(spec.profile_id)
        fk = int(self.T.frame_kind[pid]) if pid < self.T.frame_kind.size else 0
        if fk == FRAME_FIXED_WING and fidelity == R.Fidelity.L1:
            fidelity = R.Fidelity.L1 | R.Fidelity.FW  # 固定翼：运动学由 M21 fw_kin 驱动（M08-FR-210）
        z = spec.home_enu_m[2]
        home_enu = np.array([spec.home_enu_m[0], spec.home_enu_m[1], 0.0 if z is None else z], np.float64)
        home = enu_to_ned(home_enu)
        yaw = float(yaw_ned_from_enu(spec.yaw_rad))
        S.active[slot] = True
        S.fidelity[slot] = int(fidelity)
        S.frame_kind[slot] = fk
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
        S.frame_kind[slot] = 0
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
    def checkpoint_arrays(self, copy: bool = True) -> dict[str, np.ndarray]:
        out = self.S.checkpoint_arrays(copy)
        for k, a in self.PB.checkpoint_arrays(copy).items():
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
