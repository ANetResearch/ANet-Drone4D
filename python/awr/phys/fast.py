"""Fast 档（`fast`；M24 §6.4；P0b；不产生正式成绩）：FleetSim（M08 L1，PX4-lite）驱动的 PhysicsTier，供开发、训练与大规模搜索。

与 KinematicTier、GzSimBackend 同一接口（`submit`、`advance`、`status`、`close`，`history` 为 PoseHistoryBuffer，`tracker.kind(uav)`
给出当前指令种类）。每个 Fast 实例持有一个 FleetSim（独立登记表，不改 D2 组合根），按 Env tick 推进 `tick_ms × main_hz / 1000` 个主时钟
tick；指令在 `apply_tick` 的 tick 起点进入 FleetSim 的运动模式（velocity 经 setpoint 信箱，goto、takeoff、land、hover、kill、orbit、
follow_path 调用 M08 动作 K01–K19），飞行权威为 PX4-lite 的位置、姿态与电机环。

D3 stage（AWR-05 §12.3 扩展点 1，只登记在本实例的登记表中）：
- `mesh_contact`（order 195，50 Hz，phase 0）：静态网格接触（§6.4.4）。门控为上次精确净空减去其后的位移（真实网格距离的下界；
  ESDF 图层落地前的等价门控，判据同 §6.4.4 "d − C ≥ r + L + MARGIN 时本周期不可能接触"），未通过门控的机体与每 5 次调用一次
  的全量刷新（10 Hz）以 MeshWorldQuery 的 Embree CPU 路径做线段求交（`raycast_batch`）与最近点（`closest_points_batch`）；
  侧向或上方穿越、或在机体下方 45° 锥外贴近到碰撞半径内即坠毁（`COLLISION_WORLD`，detector = mesh，附三角形号与语义类）；下降撞到朝上表面与锥内最近点交给 D2 `contact` 判落地。
  坠毁响应同 D2 撞墙分支：回到上次安全位置，水平速度清零、只保留下降分量，电机失能（KILLED），由 `contact` 的"已坠毁落地"分支停在地表。
- `pose_history`（order 198，hz = physics_hz，phase 0）：每个物理步读取接触处理后的状态，换算为 ENU / WORLD←FLU / FLU，
  比力 `R_wbᵀ((v_n − v_{n−1})/dt − g_w)`，写入本 tick 的样本块，tick 末整块写入 PoseHistoryBuffer（§6.4.3）。
- `d3phys` 状态块（§6.3.3）：p_safe、clear_m（净空下界，≤ 50 m）、clear_tick、in_unknown、mesh_crash、v_prev_enu、ph_init。

时钟档（§6.2、M24-FR-009）：缺省 `{d1_250, 250, 250, 250}`（`l1_every = 1`，物理 250 Hz）；`{d1_250, 250, 125, 125}`（`l1_every = 2`）
只用于不含 imu、lidar、bag 输出且 tick 为 8 ms 整数倍的运行；`sandbox100` 只用于开发运行。输出含 imu、lidar 或 bag 时要求
`physics_hz ≥ 200`（ADR-160 决策 (4)），违反时 637。

位置口径：FleetSim 的机体参考点在起落架底部（静止时贴地），本档对外（PoseHistory、指令目标）一律使用机体原点，即参考点上方
`BODY_Z_M`（与 x500_d4d 在 gz 中的静止 base_link 高度、KinematicTier 一致）。Benchmark 组合关闭改变轨迹的自主动作（AWR-05 §8.1
第 1 条）：不装配 M09 FSM，速度模式的净空限速（M08-FR-029）不取网格自由距离；风与湍流不接入（对照运行口径：湍流强度为 0）。
确定性为一级 b：同镜像、同 CPU ISA 下两次运行 PoseHistory 逐字节一致（`pose_sha256`）。
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.phys import PhysError
from awr.phys.adapter import (
    CLOCK_PROFILES,
    ClockProfile,
    ContactEvent,
    DispatchResult,
    StatusSignals,
    TickResult,
    TickTiming,
    TierCommand,
    VehicleStatus,
    derive_status,
    validate_clock,
)
from awr.phys.kinematic import GEAR_Z_M
from awr.phys.pose_history import G_W, STATE_N, PoseHistoryBuffer

__all__ = ["BODY_Z_M", "CLEAR_CAP_M", "COMMANDS", "COS_NADIR", "D3PHYS_FIELDS", "MARGIN_M", "MESH_HZ", "MIN_IMU_PHYSICS_HZ",
           "REFRESH_EVERY", "FastTier", "check_physics_hz", "clock_of", "fleet_config", "resolve_profile"]

BODY_Z_M = GEAR_Z_M                       # 机体原点在 FleetSim 参考点（起落架底部）上方的高度
MIN_IMU_PHYSICS_HZ = 200                  # SLAM 轨道与 L2 IMU 输出的物理频率下限（ADR-160 决策 (4)）
COS_NADIR = math.cos(math.radians(45.0))  # §6.4.4：机体下方 45° 锥
MARGIN_M = 0.5
CLEAR_CAP_M = 50.0
MESH_HZ = 50
REFRESH_EVERY = 5                         # mesh_contact 每 5 次调用全量刷新一次净空（50 Hz → 10 Hz）
COMMANDS = ("velocity", "goto", "follow_path", "orbit", "hover", "takeoff", "land", "kill")
MAX_PATH_POINTS = 256
_IMU_OUTPUTS = frozenset({"imu", "lidar", "bag"})
_MISS = 0xFFFFFFFF
_CRASH_CAUSE = {1: "impact", 2: "world", 3: "uav", 4: "impact"}   # crash_sub：TILT 1、WORLD 2、UAV 3、IMPACT 4
_LANDING_MODES = {8: "LAND", 11: "ELAND", 12: "DESCENT"}            # CtrlMode LAND、ELAND、DESCENT_FF

# §6.3.3 d3phys 状态块（M24 登记，FleetState 统一分配，参与 checkpoint）
D3PHYS_FIELDS: dict[str, tuple[Any, tuple[int, ...]]] = {
    "p_safe": (np.float64, (3,)), "clear_m": (np.float32, ()), "clear_tick": (np.int64, ()), "in_unknown": (np.uint8, ()),
    "mesh_crash": (np.uint8, ()), "v_prev_enu": (np.float64, (3,)), "ph_init": (np.uint8, ()),
}


# ---------------------------------------------------------------------------------------------------------------- 时钟档
def clock_of(cp: str | Mapping | ClockProfile | None) -> ClockProfile:
    """档名、四元组或 ClockProfile → ClockProfile（缺省 d1_250）。"""
    if cp is None:
        return CLOCK_PROFILES["d1_250"]
    if isinstance(cp, ClockProfile):
        return cp
    if isinstance(cp, str):
        if cp not in CLOCK_PROFILES:
            raise PhysError(637, f"clock_profile {cp!r} is not a known profile")
        return CLOCK_PROFILES[cp]
    try:
        return ClockProfile(str(cp["name"]), int(cp["main_hz"]), int(cp["physics_hz"]), int(cp["l1_hz"]))
    except (KeyError, TypeError, ValueError) as e:
        raise PhysError(637, f"clock_profile {cp!r} is not a {{name, main_hz, physics_hz, l1_hz}} quadruple") from e


def check_physics_hz(cp: ClockProfile, outputs: Sequence[str] = ()) -> None:
    """M24-FR-005：输出含 imu、lidar 或 bag（SLAM 轨道与 L2 IMU）时 `physics_hz ≥ 200`，否则 637。"""
    need = sorted(_IMU_OUTPUTS & set(outputs))
    if need and cp.physics_hz < MIN_IMU_PHYSICS_HZ:
        msg = f"outputs {need} need physics_hz >= {MIN_IMU_PHYSICS_HZ} (clock_profile physics_hz = {cp.physics_hz})"
        raise PhysError(637, msg, {"violations": [msg]})


def fleet_config(cp: ClockProfile, *, capacity: int = 8, path_capacity: int = 4096):
    """§6.4.2：只覆盖 `l1_every = main_hz / l1_hz` 与 `clock_profile`（另按机数给容量），其余取 D2 缺省（`AWR_KERNEL` 仍可覆盖内核）。"""
    from awr.sim.fleet.fleet import FleetConfig
    from awr.sim.runtime import clock_profile as CP

    try:
        prof = CP.get(cp.name)
    except Exception as e:
        raise PhysError(637, f"clock profile {cp.name!r} is not a FleetSim profile") from e
    bad = []
    if prof.tick_hz != cp.main_hz:
        bad.append(f"main_hz {cp.main_hz} != {cp.name} tick_hz {prof.tick_hz}")
    if cp.physics_hz != cp.l1_hz:
        bad.append(f"physics_hz {cp.physics_hz} != l1_hz {cp.l1_hz} (one L1 call is one physics step)")
    if cp.l1_hz <= 0 or cp.main_hz % cp.l1_hz:
        bad.append("main_hz / l1_hz must be an integer")
    if bad:
        raise PhysError(637, "; ".join(bad), {"violations": bad})
    return FleetConfig.from_env(l1_every=cp.main_hz // cp.l1_hz, clock_profile=prof.name, capacity=max(8, int(capacity)),
                                path_capacity=int(path_capacity))


def resolve_profile(vehicle_ref: str, T) -> str:
    """机型引用 → FleetSim 模板号：D2 目录中的模板原样使用；D3 覆盖层变体（`vehicles/d3/variants/<id>.yaml`）取其基机型
    （动力学参数取基机型，挂载变化不改 L1 模板）；均不存在时 630。"""
    ref = str(vehicle_ref).split("@", 1)[0]
    if ref in T.ids:
        return ref
    p = Path(T.vdir) / "d3" / "variants" / f"{ref}.yaml"
    if p.is_file():
        m = re.search(r"^base:\s*([A-Za-z0-9_\-]+)", p.read_text(encoding="utf-8"), re.M)
        if m and m.group(1) in T.ids:
            return m.group(1)
    raise PhysError(630, f"vehicle {vehicle_ref!r} has no FleetSim profile")


# ---------------------------------------------------------------------------------------------------------------- 辅助
def _ned2enu(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, np.float64)
    return np.stack([a[..., 1], a[..., 0], -a[..., 2]], axis=-1)


def _enu2ned(a) -> np.ndarray:
    a = np.asarray(a, np.float64)
    return np.stack([a[..., 1], a[..., 0], -a[..., 2]], axis=-1)


def _rotate_inv(q_xyzw: np.ndarray, v: np.ndarray) -> np.ndarray:
    """R(q)ᵀ v（q 为 WORLD←FLU，xyzw）：按共轭四元数旋转。"""
    u = -q_xyzw[:, 0:3]
    w = q_xyzw[:, 3:4]
    t = 2.0 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


class _GroundView:
    """交给 FleetSim 的世界视图：只提供 D2 `contact` 的 DSM、DTM 栅格；速度模式的方向自由距离恒为上限（不做网格净空限速）。"""

    def __init__(self, world) -> None:
        self._w = world

    def dsm_grid(self):
        return self._w.dsm_grid()

    def dtm_grid(self):
        return self._w.dtm_grid()

    def free_distance(self, origins, dirs, max_m: float) -> np.ndarray:
        return np.full(len(np.asarray(origins).reshape(-1, 3)), float(max_m))


class _Events:
    """FleetSim 事件端（`ctx.events`）：只收集本 tick 的接触事件，换算为 ContactEvent。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, int, str | None, dict]] = []

    def emit(self, name: str, *, t_sim_ns: int = 0, severity: int = 0, uav: str | None = None, fields: dict | None = None, **kw) -> None:
        if name.startswith("sim.contact."):
            self.items.append((name, int(t_sim_ns), uav, {**(fields or {}), **kw}))


class _Kinds:
    """`tracker.kind(uav)`：最近一次生效的指令种类（Env 观测 `status.nav_cmd`）。"""

    def __init__(self) -> None:
        self._k: dict[str, str] = {}

    def set(self, uav: str, kind: str) -> None:
        self._k[uav] = kind

    def kind(self, uav: str) -> str:
        return self._k.get(uav, "hover")


# ---------------------------------------------------------------------------------------------------------------- 档位
@dataclass
class FastTier:
    uavs: Sequence[str]
    spawn: Mapping[str, tuple[tuple[float, float, float], float]]      # uav → ((x, y, z), yaw)；z 为机体原点高度（空中出生）
    world: Any = None                                                   # DsmWorldQuery 或 MeshWorldQuery；None 为出生点高度的水平面
    grounded: Sequence[str] | None = None
    vehicles: Mapping[str, str] | None = None                           # uav → vehicle_ref（缺省 x500）
    clock_profile: str | Mapping | ClockProfile | None = "d1_250"
    tick_ms: int = 100
    outputs: Sequence[str] = ()
    mesh_contact: bool | None = None                                    # None：世界有网格图层时开启
    history_s: float = 2.0
    retain_s: float | None = 1.0
    name: str = "fast"
    tracker: _Kinds = field(default_factory=_Kinds)

    def __post_init__(self) -> None:
        from awr.sim.fleet.fleet import FleetSim
        from awr.sim.fleet.pipeline import StageCtx
        from awr.sim.fleet.stages import registry as R

        self.uavs = tuple(self.uavs)
        self.cp = clock_of(self.clock_profile)
        validate_clock("fast", self.cp, self.tick_ms, self.outputs)
        check_physics_hz(self.cp, self.outputs)
        cfg = fleet_config(self.cp, capacity=len(self.uavs))
        self.l1_every = cfg.l1_every
        self.main_per_tick = self.tick_ms * self.cp.main_hz // 1000
        self.n_steps = self.main_per_tick // self.l1_every
        self.step_ns = 1_000_000_000 // self.cp.physics_hz
        self.dt = self.step_ns * 1e-9
        has_mesh = self.world is not None and all(hasattr(self.world, a) for a in ("tiles", "raycast_batch", "closest_points_batch"))
        if self.mesh_contact and not has_mesh:
            raise PhysError(630, "mesh_contact needs a world with a mesh geometry layer")
        self.mesh_on = has_mesh if self.mesh_contact is None else bool(self.mesh_contact)
        reg = R.Registry()
        reg.blocks["d3phys"] = R.StateBlockSpec("d3phys", "M24", {k: (np.dtype(dt), sh) for k, (dt, sh) in D3PHYS_FIELDS.items()})
        reg.stages += R.make_stage("mesh_contact", None, None, 195, self._mesh_contact, owner="M24", hz=MESH_HZ, phase_slot=0,
                                   fidelity=R.Fidelity.L1 | R.Fidelity.L2,
                                   writes=("d3phys.p_safe", "d3phys.clear_m", "d3phys.clear_tick", "d3phys.mesh_crash"))
        reg.stages += R.make_stage("pose_history", None, None, 198, self._pose_history, owner="M24", hz=self.cp.physics_hz,
                                   phase_slot=0, fidelity=R.Fidelity.L1 | R.Fidelity.L2, writes=("d3phys.v_prev_enu", "d3phys.ph_init"),
                                   reads=("p", "v", "q", "omega", "landed", "crash_sub"))
        self.events = _Events()
        self.f = FleetSim(cfg, world=_GroundView(self.world) if self.world is not None else None, reg=reg)
        self.f.build_pipeline()
        self.ctx = StageCtx(profiles=self.f.T, paths=self.f.PB, events=self.events)
        self._spawn_all()
        self.slots = np.arange(len(self.uavs), dtype=np.int64)
        self.history = PoseHistoryBuffer(self.uavs, hz=self.cp.physics_hz, capacity_s=self.history_s)
        self._t = np.zeros(self.n_steps, np.int64)
        self._blk = np.zeros((self.n_steps, len(self.uavs), STATE_N))
        self._j = 0
        self._landed_prev = self.f.S.landed[self.slots].copy()
        self._pending: list[TierCommand] = []
        self._mesh_events: list[ContactEvent] = []
        self._seq = 0
        self._calls = 0
        self.mesh_stats = {"calls": 0, "checked": 0, "exact": 0, "crashes": 0}
        self.t_k_ns = 0
        self.digest = hashlib.sha256()
        self._ticks = 0
        self._physics_ns = 0

    # ------------------------------------------------------------------ 组装
    def _ground_h(self, xy: np.ndarray) -> float:
        """D2 `contact` 所见的地表高（DSM 最近格，ENU 向上；世界缺省为 0）。"""
        if self.world is None:
            return 0.0
        g = self.world.dsm_grid()
        a = np.asarray(g.a)
        h, w = a.shape
        c = int(np.clip(math.floor((float(xy[0]) - g.x0_m) / g.cell_m), 0, w - 1))
        r = int(np.clip(math.floor((float(xy[1]) - g.y0_m) / g.cell_m), 0, h - 1))
        return float(a[r, c])

    def _spawn_all(self) -> None:
        from awr.sim.backends.base import EntitySpec, Kind
        from awr.sim.fleet import actions as ACT
        from awr.sim.fleet.stages.registry import Fidelity

        S, T = self.f.S, self.f.T
        g = set(self.uavs if self.grounded is None else self.grounded)
        vehicles = dict(self.vehicles or {})
        self.profile_ids = {}
        for i, u in enumerate(self.uavs):
            (x, y, z), yaw = self.spawn[u]
            pid = resolve_profile(vehicles.get(u, "x500"), T)
            if int(T.frame_kind[T.index(pid)]) != 0:
                raise PhysError(630, f"{u}: vehicle {pid!r} is not a multirotor; the Fast tier flies multirotors only")
            self.profile_ids[u] = pid
            # home 取出生点地表（无世界时 D2 contact 以 home 高度为水平地面）；空中出生再把机体移到出生高度
            self.f.add(EntitySpec(u, Kind.UAV, pid, None, (float(x), float(y), self._ground_h(np.array([x, y]))), float(yaw)), slot=i,
                       agent_no=i, entity_id=u, fidelity=Fidelity.L1)
            S.lifecycle[i] = 4                                    # READY（独立装配，同 MockBackend）
            if "safety" in S.blocks and "armed" in S.blocks["safety"]:
                S.blocks["safety"]["armed"][i] = u not in g
            if u not in g:                                        # 空中出生：悬停推力、HOLD 锁定当前位置
                S.p[i, 2] = -(float(z) - BODY_Z_M)
                for arr in (S.p_prev, S.tr_x, S.target, S.pos_ref, S.pos_sp):
                    arr[i] = S.p[i]
                hov = float(T.hover[T.index(pid)])
                S.landed[i] = False
                S.in_contact[i] = False
                S.in_air[i] = True
                S.thrust[i] = hov
                S.thr_sp[i] = (0.0, 0.0, -hov)
                ACT.begin_hold(S, np.array([i]), 0.0)
        S.touch()

    def caps(self) -> dict:
        """TierCaps（M24-FR-002；Fast 由 Mock 能力与时钟档在运行期合成）。"""
        return {"tier": self.name, "physics_hz": self.cp.physics_hz, "pose_history_hz": self.cp.physics_hz, "steps_per_tick": self.n_steps,
                "clock_mode": "lockstep", "clock_profile": {"name": self.cp.name, "main_hz": self.cp.main_hz, "physics_hz": self.cp.physics_hz,
                                                            "l1_hz": self.cp.l1_hz},
                "ranked_allowed": False, "determinism": "1b", "requires_gpu": False, "max_vehicles": int(self.f.max_vehicles),
                "frame_kinds": ["multirotor"], "commands": list(COMMANDS), "mesh_contact": self.mesh_on, "kernel": self.f.kernel}

    # ------------------------------------------------------------------ 采样
    def _rows(self) -> np.ndarray:
        """当前状态的 PoseHistory 行（机体原点；比力用 d3phys.v_prev_enu 差分并更新之）。"""
        S = self.f.S
        sl = self.slots
        e = S.enu
        e.refresh(force=True)
        pos = np.array(e.pos[sl])
        pos[:, 2] += BODY_Z_M
        vel, q, om = np.array(e.vel[sl]), np.array(e.q_xyzw[sl]), np.array(e.omega_flu[sl])
        blk = S.blocks["d3phys"]
        first = blk["ph_init"][sl] == 0
        a_w = np.where(first[:, None], 0.0, (vel - blk["v_prev_enu"][sl]) / self.dt)
        f_b = _rotate_inv(q, a_w - G_W)
        blk["v_prev_enu"][sl] = vel
        blk["ph_init"][sl] = 1
        out = np.empty((len(sl), STATE_N))
        out[:, 0:3], out[:, 3:7], out[:, 7:10], out[:, 10:13], out[:, 13:16] = pos, q, vel, om, f_b
        return out

    def open(self) -> dict:
        S = self.f.S
        blk = S.blocks["d3phys"]
        sl = self.slots
        blk["p_safe"][sl] = S.p[sl]
        blk["clear_m"][sl] = 0.0
        init = self._rows()
        self.history.append_block(np.array([0], np.int64), init[None])
        self.digest.update(np.array([0], "<i8").tobytes() + init.astype("<f8").tobytes())
        return {**self.caps(), "t0_gz_ns": 0}

    def _pose_history(self, S, ctx) -> None:
        """stage `pose_history`（order 198，每物理步）：本 tick 的第 j 个样本；起飞（离地）记 LIFTOFF。"""
        j = self._j
        if j >= self.n_steps:
            raise PhysError(635, f"pose_history: more than {self.n_steps} physics steps in one tick")
        self._t[j] = int(ctx.t_ns)
        self._blk[j] = self._rows()
        self._j = j + 1
        sl = self.slots
        landed = S.landed[sl]
        up = np.flatnonzero(self._landed_prev & ~landed & (S.crash_sub[sl] == 0))
        for i in up:
            p = self._blk[j, i]
            self._mesh_events.append(ContactEvent(self.uavs[i], int(ctx.t_ns), "LIFTOFF", "dsm", (float(p[0]), float(p[1]), float(p[2])),
                                                  (0.0, 0.0, 1.0), float(np.linalg.norm(p[7:10]))))
        self._landed_prev = landed.copy()

    # ------------------------------------------------------------------ mesh_contact
    def _tile_of(self, prim: int):
        for t in self.world.tiles:
            if t.t0 <= prim < t.t0 + t.nt:
                return t
        return None

    def _normal(self, prim: int) -> tuple[float, float, float] | None:
        if prim == _MISS:
            return None
        t = self._tile_of(int(prim))
        if t is None:
            return None
        a, b, c = (t.V[int(k)].astype(np.float64) for k in t.F[int(prim) - t.t0])
        n = np.cross(b - a, c - a)
        L = float(np.linalg.norm(n))
        return None if L == 0.0 else tuple(float(x) for x in n / L)

    def _mesh_contact(self, S, ctx) -> None:
        """stage `mesh_contact`（order 195，50 Hz）：§6.4.4，门控为净空下界（上次精确净空减去其后位移）。"""
        if not self.mesh_on:
            return
        self._calls += 1
        self.mesh_stats["calls"] += 1
        sl = self.slots
        idx = sl[S.active[sl] & S.in_air[sl] & (S.crash_sub[sl] == 0)]
        if idx.size == 0:
            return
        blk = S.blocks["d3phys"]
        up = np.array([0.0, 0.0, BODY_Z_M])
        p = _ned2enu(S.p[idx]) + up
        p0 = _ned2enu(blk["p_safe"][idx]) + up
        seg = p - p0
        L = np.linalg.norm(seg, axis=1)
        r = np.asarray(self.f.T.collision_r, np.float64)[S.profile_id[idx]]
        lb = blk["clear_m"][idx].astype(np.float64) - L                # 本周期网格距离的下界（线段上任一点）
        refresh = (self._calls - 1) % REFRESH_EVERY == 0
        need = lb < r + MARGIN_M
        J = np.flatnonzero(need | refresh)
        self.mesh_stats["checked"] += int(idx.size)
        crash = np.zeros(idx.size, bool)
        where = np.zeros((idx.size, 3))
        prim = np.full(idx.size, _MISS, np.int64)
        if J.size:
            self.mesh_stats["exact"] += int(J.size)
            hit = np.zeros(J.size, bool)
            t_hit = np.full(J.size, np.inf)
            ph = np.full(J.size, _MISS, np.int64)
            mv = L[J] > 1e-9
            if mv.any():
                jj = J[mv]
                th, pr, _ = self.world.raycast_batch(p0[jj], seg[jj], float(np.max(L[jj] + r[jj])))
                th = th.astype(np.float64)
                nz = np.array([abs((self._normal(int(x)) or (0.0, 0.0, 0.0))[2]) for x in pr])
                down_hit = (seg[jj, 2] < 0) & (nz >= COS_NADIR)              # 下降撞到朝上表面：地表，交给 contact
                hit[mv] = (th <= L[jj]) & ~down_hit
                t_hit[mv], ph[mv] = th, pr.astype(np.int64)
            d, c, pc = self.world.closest_points_batch(p[J], CLEAR_CAP_M)
            pc = pc.astype(np.int64)
            dirc = (c - p[J]) / np.maximum(d, 1e-9)[:, None]
            down_cl = -dirc[:, 2] >= COS_NADIR                                  # 最近点在机体下方 45° 锥内：地表
            near = (d < r[J]) & ~down_cl
            crash[J] = hit | near
            u = np.where(L[J] > 1e-9, 1.0, 0.0)[:, None] * seg[J] / np.maximum(L[J], 1e-9)[:, None]
            where[J] = np.where(hit[:, None], p0[J] + u * np.where(np.isfinite(t_hit), t_hit, 0.0)[:, None], c)
            prim[J] = np.where(hit, ph, pc)
            blk["clear_m"][idx[J]] = np.minimum(d, CLEAR_CAP_M)
            blk["clear_tick"][idx[J]] = int(ctx.tick)
        rest = np.ones(idx.size, bool)
        rest[J] = False
        blk["clear_m"][idx[rest]] = np.maximum(lb[rest], 0.0)
        for k in np.flatnonzero(crash):
            self._crash(S, int(idx[k]), int(ctx.t_ns), where[k], int(prim[k]), p[k])
        ok = idx[~crash]
        blk["p_safe"][ok] = S.p[ok]

    def _crash(self, S, s: int, t_ns: int, at: np.ndarray, prim: int, p_body: np.ndarray) -> None:
        """坠毁响应（§6.4.4 表）：回到 p_safe，水平速度清零、只保留下降分量，电机失能。"""
        from awr.sim.fleet import kernels_contact as KC
        from awr.sim.fleet import kernels_l1 as K

        blk = S.blocks["d3phys"]
        speed = float(np.linalg.norm(S.v[s]))
        S.p[s] = blk["p_safe"][s]
        S.p_prev[s] = S.p[s]
        S.v[s, 0] = S.v[s, 1] = 0.0
        S.v[s, 2] = max(float(S.v[s, 2]), 0.0)
        S.omega[s] = 0.0
        S.thrust[s] = 0.0
        S.thr_sp[s] = 0.0
        S.ctrl_mode[s] = K.M_KILLED
        S.crash_sub[s] = KC.CRASH_COLLISION_WORLD
        S.mode_evt[s] |= K.E_COLLISION
        blk["mesh_crash"][s] = 1
        S.touch()
        self.mesh_stats["crashes"] += 1
        n = self._normal(prim)
        if n is not None and float(np.dot(n, p_body - at)) < 0:                # 法向朝向机体一侧
            n = (-n[0], -n[1], -n[2])
        sem = int(self.world.semantic_of(np.array([prim], np.uint32))[0]) if prim != _MISS else None
        self._mesh_events.append(ContactEvent(self.uavs[s], t_ns, "COLLISION_WORLD", "mesh", tuple(float(x) for x in at), n, speed,
                                              None if prim == _MISS else prim, sem))

    # ------------------------------------------------------------------ PhysicsTier
    def submit(self, cmds: Sequence[TierCommand]) -> list[DispatchResult]:
        out = []
        for c in cmds:
            if c.uav not in self.uavs:
                out.append(DispatchResult(c.uav, False, 630, detail="unknown vehicle"))
            elif c.kind not in COMMANDS:
                out.append(DispatchResult(c.uav, False, 630, detail=f"{c.kind} is not a tier command"))
            elif c.kind == "follow_path" and (c.path_enu_m is None or not 1 <= len(c.path_enu_m) <= MAX_PATH_POINTS):
                out.append(DispatchResult(c.uav, False, 630, detail=f"follow_path needs 1..{MAX_PATH_POINTS} points"))
            elif c.kind == "orbit" and (c.orbit is None or not 1.0 <= c.orbit.radius_m <= 1000.0):
                out.append(DispatchResult(c.uav, False, 630, detail="orbit radius must be in [1, 1000] m"))
            else:
                self._pending.append(c)
                out.append(DispatchResult(c.uav, True))
        return out

    def _apply(self, c: TierCommand, s: int, t: float) -> None:
        from awr.sim.fleet import actions as ACT
        from awr.sim.fleet import kernels_l1 as K

        S = self.f.S
        sa = np.array([s])
        up = np.array([0.0, 0.0, BODY_Z_M])
        prev = self.tracker.kind(c.uav)
        if int(S.ctrl_mode[s]) == K.M_PATH and c.kind != "follow_path":
            ACT.release_path(S, self.f.PB, sa)
        if c.kind == "velocity":
            if int(S.ctrl_mode[s]) != K.M_VELOCITY or not bool(S.vel_sess[s]):
                ACT.begin_velocity(S, sa, False, math.inf, True, t)
            self._seq += 1
            v = c.vel_enu_mps or (0.0, 0.0, 0.0)
            self.f.mailbox.put(s, np.asarray(v, np.float64), float(c.yaw_rate_rps or 0.0), 0, self._seq, 0, 0)
        elif c.kind == "goto":
            goal = _enu2ned(np.asarray(c.target_enu_m, np.float64) - up)
            ACT.begin_goto(S, sa, goal, float(c.speed_mps) if c.speed_mps is not None else np.nan, None, t,
                           stop_motion=prev != "goto")                    # 同一 goto 的目标更新（导航误差换算）不先刹停
        elif c.kind == "takeoff":
            ACT.begin_takeoff(S, sa, float(c.alt_m if c.alt_m is not None else 2.5), t)
        elif c.kind == "hover":
            ACT.begin_hold(S, sa, t)
        elif c.kind == "land":
            ACT.begin_land(S, sa, None, t)
        elif c.kind == "kill":
            ACT.begin_kill(S, sa, t)
        elif c.kind == "orbit":
            o = c.orbit
            T = self.f.T
            sp = float(c.speed_mps) if c.speed_mps is not None else float(T.LT[S.limits_id[s], K.L_CRUISE])
            ACT.begin_orbit(S, sa, _enu2ned(np.asarray(o.center_enu_m, np.float64) - up), float(o.radius_m), sp, bool(o.cw),
                            float(o.turns or 0.0), "center", None, t)
        elif c.kind == "follow_path":
            w = _enu2ned(np.asarray(c.path_enu_m, np.float64).reshape(-1, 3) - up)
            if ACT.begin_path(S, self.f.PB, self.f.T, s, w, float(c.speed_mps) if c.speed_mps is not None else np.nan, None, t) is None:
                raise PhysError(630, f"{c.uav}: path buffer full")
        self.tracker.set(c.uav, c.kind)
        S.touch()

    def advance(self, k: int) -> TickResult:
        w0 = time.perf_counter_ns()
        S = self.f.S
        t_cmd = self.ctx.t_ns * 1e-9
        keep = []
        for c in sorted(self._pending, key=lambda c: c.apply_tick):
            if c.apply_tick > k:
                keep.append(c)
                continue
            s = self.uavs.index(c.uav)
            if int(S.crash_sub[s]) != 0:
                continue
            self._apply(c, s, t_cmd)
        self._pending = keep
        self._j = 0
        self.events.items.clear()
        self._mesh_events.clear()
        self.f.step(self.ctx, self.main_per_tick)
        if self._j != self.n_steps:
            raise PhysError(635, f"tick {k}: {self._j} physics steps captured, expected {self.n_steps}")
        t_start = self.t_k_ns
        self.t_k_ns = int(self._t[-1])
        self.history.append_block(self._t, self._blk)
        self.digest.update(self._t.astype("<i8").tobytes() + self._blk.astype("<f8").tobytes())
        if self.retain_s is not None:
            self.history.release_before(self.t_k_ns - int(self.retain_s * 1e9))
        self._ticks += 1
        contacts = sorted(self._contacts(), key=lambda e: (e.t_ns, self.uavs.index(e.uav)))
        dt_ns = time.perf_counter_ns() - w0
        self._physics_ns += dt_ns
        return TickResult(k, t_start, self.t_k_ns, self.n_steps, tuple(contacts), self.status(), TickTiming(physics=dt_ns))

    def _contacts(self) -> list[ContactEvent]:
        out = list(self._mesh_events)
        for name, t, uav, f in self.events.items:
            if uav not in self.uavs:
                continue
            pos = tuple(float(x) for x in (f.get("pos_enu_m") or (0.0, 0.0, 0.0)))
            if name == "sim.contact.touchdown":
                out.append(ContactEvent(uav, t, "TOUCHDOWN", "dsm", pos, (0.0, 0.0, 1.0), 0.0))
            elif name == "sim.contact.collision":
                kind = str(f.get("kind"))
                if kind not in ("COLLISION_WORLD", "IMPACT", "COLLISION_UAV"):
                    continue
                out.append(ContactEvent(uav, t, kind, "dsm", pos, (0.0, 0.0, 1.0) if kind == "IMPACT" else None, float(f.get("speed_mps", 0.0))))
        return out

    def status(self) -> Mapping[str, VehicleStatus]:
        S = self.f.S
        mesh = S.blocks["d3phys"]["mesh_crash"]
        out = {}
        for s, u in enumerate(self.uavs):
            cs = int(S.crash_sub[s])
            cause = "mesh" if mesh[s] else _CRASH_CAUSE.get(cs, "world") if cs else "none"
            out[u] = derive_status(StatusSignals(crash_cause=cause, landed=bool(S.landed[s]), in_air=bool(S.in_air[s]),
                                                 cmd_mode=_LANDING_MODES.get(int(S.ctrl_mode[s]), self.tracker.kind(u).upper())))
        return out

    def close(self) -> dict:
        return {"ticks": self._ticks, "t_end_ns": self.t_k_ns, "pose_sha256": self.digest.hexdigest(), "tier": self.name,
                "kernel": self.f.kernel, "kernel_fallback": self.f.kernel_fallback, "physics_hz": self.cp.physics_hz,
                "steps_per_tick": self.n_steps, "clock_profile": self.caps()["clock_profile"], "profiles": dict(self.profile_ids),
                "mesh_contact": {"enabled": self.mesh_on, **self.mesh_stats}, "physics_wall_s": round(self._physics_ns * 1e-9, 3)}
