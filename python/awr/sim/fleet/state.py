"""FleetState：容量 N = 1024 的预分配 SoA（M08-FR-010、FR-011、FR-087；M08 §6.3.1）。

内部坐标 NED/FRD，四元数 (w, x, y, z)，只允许出现在 `awr/sim/fleet/**` 内（AWR-03 §5.3 第 3 条）；fleet 以外的代码经
只读 ENU 视图 `FleetState.enu` 读取机体状态（M08-FR-087）。视图与 tap 调用同一个换算核（`kernels_tap.enu_convert`，
与 M02 `ned_frd_to_enu_flu_batch` 逐位相同）；写 p、v、q、omega、pos_ref 的 stage 调用 `S.touch()` 置脏，本 tick 首次
访问时刷新一次（`EnuViews.refreshes` 计数，M08-AC-042）。视图数组只读（`writeable = False`）。

他模块的状态块经 `register_state_block()` 登记、由本类统一分配（`blocks[name][field]`，形状 (N, *shape)）。M09 未装配时
（walking skeleton），M08 以兜底定义分配 `safety`、`battery`、`mission` 三个块（字段按 M08 §6.3.1 的契约）。
"""

from __future__ import annotations

from enum import IntEnum

import numpy as np

from awr.world.georef.frames import enu_to_ned

from . import kernels_l1 as KL
from .params_px4 import ELAND_SPEED, RHO0
from .stages.registry import StateBlockSpec

__all__ = [
    "CAPACITY",
    "EVT_ARRIVED",
    "EVT_COLLISION",
    "EVT_LIFTOFF",
    "EVT_MODE",
    "EVT_PHASE",
    "EVT_TOUCHDOWN",
    "FALLBACK_BLOCKS",
    "ORB_COLS",
    "YAWB",
    "CtrlMode",
    "EnuViews",
    "FleetState",
]

CAPACITY = 1024
ORB_COLS = 12
YAWB = {"center": 0.0, "tangent": 1.0, "fixed": 2.0}


class CtrlMode(IntEnum):
    """运动模式（M08 §6.9.1）；FlightState 由 FSM 按运动模式与事件计算（P-08）。"""

    IDLE = 0
    SPOOLUP = 1
    TAKEOFF = 2
    GOTO = 3
    PATH = 4
    ORBIT = 5
    HOLD = 6
    VELOCITY = 7
    LAND = 8
    RTL = 9
    OFFBOARD_POS = 10
    ELAND = 11
    DESCENT_FF = 12
    KILLED = 13
    KINEMATIC = 14
    TRAJ = 15


# mode_evt 位（M08 §6.3.1）
EVT_MODE, EVT_ARRIVED, EVT_PHASE, EVT_TOUCHDOWN, EVT_LIFTOFF, EVT_COLLISION = 1, 2, 4, 8, 16, 32

_B = np.bool_
_F = np.float64

# M09 未装配时的兜底状态块（字段为 tap 与 CommandEngine 读取的契约，M08 §6.3.1）
FALLBACK_BLOCKS: dict[str, StateBlockSpec] = {
    "safety": StateBlockSpec("safety", "M08", {
        "fs": (np.dtype(np.uint8), ()), "sub": (np.dtype(np.uint8), ()),
        "flag_failsafe": (np.dtype(_B), ()), "flag_alert": (np.dtype(_B), ()), "flag_gcs": (np.dtype(_B), ()),
        "flag_fcu": (np.dtype(_B), ()), "flag_loc_ok": (np.dtype(_B), ()), "flag_loc_deg": (np.dtype(_B), ()),
        "locked": (np.dtype(_B), ()), "severity": (np.dtype(np.uint8), ()), "d_free_fence_m": (np.dtype(np.float32), ()),
        "armed": (np.dtype(_B), ()), "state_t": (np.dtype(_F), ()), "hold_reason": (np.dtype(np.uint8), ()),
    }),
    "battery": StateBlockSpec("battery", "M08", {
        "soc": (np.dtype(np.float32), ()), "battery_pct": (np.dtype(np.uint8), ()), "p_avg_w": (np.dtype(np.float32), ()),
    }),
    "mission": StateBlockSpec("mission", "M08", {
        "mission_item": (np.dtype(np.uint16), ()), "track_state": (np.dtype(np.uint8), ()),
    }),
}

_VIEW_NAMES = ("pos", "vel", "acc", "q_xyzw", "q_sp_xyzw", "omega_flu", "pos_ref", "home")


class EnuViews:
    """只读 ENU/FLU 视图（M08-FR-087）：`pos`、`vel`、`acc`（ENU）、`q_xyzw`（WORLD←FLU）、`q_sp_xyzw`、`omega_flu`、
    `pos_ref`、`home`（均 N 行预分配）与切片函数；刷新调用 `kernels_tap.enu_convert`（numba 不可用时用 numpy 等价实现）。"""

    def __init__(self, S: FleetState) -> None:
        n = S.capacity
        self._S = S
        self._pos = np.zeros((n, 3))
        self._vel = np.zeros((n, 3))
        self._acc = np.zeros((n, 3))
        self._q = np.zeros((n, 4))
        self._q[:, 3] = 1.0
        self._q_sp = np.zeros((n, 4))
        self._q_sp[:, 3] = 1.0
        self._omega = np.zeros((n, 3))
        self._pos_ref = np.zeros((n, 3))
        self._home = np.zeros((n, 3))
        self._stamp = -1
        self.refreshes = 0
        self.use_numba = KL.HAVE_NUMBA
        self._ro = {k: v.view() for k, v in zip(_VIEW_NAMES, (self._pos, self._vel, self._acc, self._q, self._q_sp,
                                                              self._omega, self._pos_ref, self._home), strict=True)}
        for a in self._ro.values():
            a.flags.writeable = False

    def refresh(self, force: bool = False) -> None:
        S = self._S
        if self._stamp == S.version and not force:
            return
        idx = S.active_idx32()
        if self.use_numba:
            from .kernels_tap import enu_convert

            enu_convert(idx, S.p, S.v, S.a_meas, S.q, S.q_sp, S.omega, S.pos_ref, S.home, self._pos, self._vel, self._acc,
                        self._q, self._q_sp, self._omega, self._pos_ref, self._home)
        else:
            convert_numpy(S, idx, self)
        self._stamp = S.version
        self.refreshes += 1

    def __getattr__(self, name: str) -> np.ndarray:
        ro = self.__dict__.get("_ro")
        if ro is None or name not in ro:
            raise AttributeError(name)
        self.refresh()
        return ro[name]

    def pose_enu_flu(self, slots: np.ndarray) -> np.ndarray:
        """k×7：ENU 位置与 WORLD←FLU 四元数 [x,y,z,w]（拷贝）。"""
        self.refresh()
        return np.concatenate([self._pos[slots], self._q[slots]], axis=1)

    def acc_enu(self, slots: np.ndarray) -> np.ndarray:
        self.refresh()
        return self._acc[slots].copy()

    def omega_flu_slots(self, slots: np.ndarray) -> np.ndarray:
        """按 slot 切片的 FLU 角速度拷贝（M08 §6.3.1 的切片函数 `omega_flu(slots)`；与同名视图冲突，改用此名）。"""
        self.refresh()
        return self._omega[slots].copy()

    # ---- fleet 以外（CommandEngine、runtime、后端）读取的其余 NED 量的 ENU 换算（拷贝；M08-NFR-019）
    def land_xy(self, slots) -> np.ndarray:
        """降落点 ENU 水平坐标 (x_e, y_n)，k×2。"""
        xy = self._S.land_xy[slots]
        return np.stack([xy[..., 1], xy[..., 0]], axis=-1)

    def rtl_via(self, slots) -> np.ndarray:
        """RTL 巡航段尚未到达的绕行点 ENU 水平坐标 (x_e, y_n)，k×2；无绕行或已通过时为 NaN（ADR-054）。"""
        xy = self._S.rtl_via[slots]
        return np.stack([xy[..., 1], xy[..., 0]], axis=-1)

    def ground_up(self, slots) -> np.ndarray:
        """contact 所见地表高（DSM，ENU 向上为正，m）。"""
        return -self._S.ground_z[slots]

    def wind(self, slots) -> np.ndarray:
        """空气速度 ENU（m/s）。"""
        w = self._S.wind[slots]
        return np.stack([w[..., 1], w[..., 0], -w[..., 2]], axis=-1)


def convert_numpy(S: FleetState, idx: np.ndarray, o: EnuViews) -> None:
    """`kernels_tap.enu_convert` 的 numpy 等价实现（逐位相同）。"""
    from .kernels_tap import INV_SQRT2

    for src, dst in ((S.p, o._pos), (S.v, o._vel), (S.a_meas, o._acc), (S.pos_ref, o._pos_ref), (S.home, o._home)):
        dst[idx, 0] = src[idx, 1]
        dst[idx, 1] = src[idx, 0]
        dst[idx, 2] = -src[idx, 2]
    for src, dst in ((S.q, o._q), (S.q_sp, o._q_sp)):
        w, x, y, z = src[idx, 0], src[idx, 1], src[idx, 2], src[idx, 3]
        dst[idx, 0] = (x + y) * INV_SQRT2
        dst[idx, 1] = (x - y) * INV_SQRT2
        dst[idx, 2] = (w - z) * INV_SQRT2
        dst[idx, 3] = (w + z) * INV_SQRT2
    o._omega[idx, 0] = S.omega[idx, 0]
    o._omega[idx, 1] = -S.omega[idx, 1]
    o._omega[idx, 2] = -S.omega[idx, 2]


# FleetState 中参与 checkpoint 的核心数组名（状态块另计；ids 为 Python 列表，走 meta）
CORE_ARRAYS = (
    "active", "fidelity", "lifecycle", "agent_no", "profile_id", "limits_id", "home", "p", "v", "p_prev", "a_meas", "q",
    "omega", "thrust", "thr_cap", "thr_sp", "q_sp", "yaw_sp", "vel_int", "ctrl_mode", "ctrl_phase", "mode_evt", "mode_t",
    "target", "pos_sp", "vel_cmd", "tr_x", "tr_v", "tr_a", "pos_ref", "stopping", "speed_cmd", "z_rtl", "v_rtl", "land_xy",
    "rtl_via", "wind", "rho", "env_flags", "env_gust", "ground_z", "agl", "in_contact", "landed", "in_air", "contact_t", "crash_sub",
    "td_t", "thrust_scale", "motor_ok", "est_age_s", "sp_wall_ns", "vel_sess", "axis_anchor", "axis_lock", "path_off",
    "path_len", "path_seg", "path_tau", "orb", "desc_v", "vel_frame", "vel_vmax", "vel_yawrate", "hold_alt", "d_free",
    "force", "kin_t0",
)


class FleetState:
    """M08 核心字段（M08 §6.3.1）与登记的状态块。"""

    def __init__(self, capacity: int = CAPACITY, blocks: dict[str, StateBlockSpec] | None = None) -> None:
        n = self.capacity = int(capacity)
        self.tick = 0
        self.t_ns = 0
        self.version = 0  # 每次写 p/v/q/omega/pos_ref 后 +1（ENU 视图的脏标志）
        self.active = np.zeros(n, _B)
        self.fidelity = np.zeros(n, np.uint8)
        self.lifecycle = np.zeros(n, np.uint8)  # A 轴（Lifecycle 枚举值），由 roster 同步（ingest）
        self.ids: list[str | None] = [None] * n  # 机体 id（事件外层 uav 字段用）
        self.agent_no = np.zeros(n, np.int32)
        self.profile_id = np.zeros(n, np.uint16)
        self.limits_id = np.zeros(n, np.uint8)
        self.home = np.zeros((n, 3))
        self.p = np.zeros((n, 3))
        self.v = np.zeros((n, 3))
        self.p_prev = np.zeros((n, 3))
        self.a_meas = np.zeros((n, 3))
        self.q = np.zeros((n, 4))
        self.q[:, 0] = 1.0
        self.omega = np.zeros((n, 3))
        self.thrust = np.zeros(n)
        self.thr_cap = np.ones(n, np.float32)
        self.thr_sp = np.zeros((n, 3))
        self.q_sp = self.q.copy()
        self.yaw_sp = np.zeros(n)
        self.vel_int = np.zeros((n, 3))
        self.ctrl_mode = np.zeros(n, np.uint8)
        self.ctrl_phase = np.zeros(n, np.uint8)
        self.mode_evt = np.zeros(n, np.uint8)
        self.mode_t = np.zeros(n)  # s【仿真】，进入当前模式或阶段的时刻（SPOOLUP、斜坡计时）
        self.target = np.zeros((n, 3))
        self.pos_sp = np.zeros((n, 3))
        self.vel_cmd = np.zeros((n, 3))
        self.tr_x = np.zeros((n, 3))
        self.tr_v = np.zeros((n, 3))
        self.tr_a = np.zeros((n, 3))
        self.pos_ref = np.zeros((n, 3))
        self.stopping = np.zeros(n, _B)
        self.speed_cmd = np.full(n, np.nan)
        self.z_rtl = np.zeros(n)
        self.v_rtl = np.full(n, np.nan)
        self.land_xy = np.zeros((n, 2))
        # RTL 巡航段的绕行点（NED 水平坐标，NaN 为直飞 home；ADR-054）：CRUISE 先飞向该点，参考到达 RTL_VIA_ACCEPT_M 内后清为 NaN
        self.rtl_via = np.full((n, 2), np.nan)
        self.wind = np.zeros((n, 3))
        self.rho = np.full(n, RHO0, np.float32)
        self.env_flags = np.zeros(n, np.uint8)
        self.env_gust = np.zeros(n, np.float32)
        self.ground_z = np.zeros(n)
        self.agl = np.zeros(n)
        self.in_contact = np.zeros(n, _B)
        self.landed = np.ones(n, _B)
        self.in_air = np.zeros(n, _B)
        self.contact_t = np.full(n, np.nan)
        self.crash_sub = np.zeros(n, np.uint8)
        self.td_t = np.full(n, np.nan)  # s【仿真】，触地斜坡起点
        self.thrust_scale = np.ones(n, np.float32)
        self.motor_ok = np.full(n, 0xFF, np.uint8)
        self.est_age_s = np.zeros(n, np.float32)
        self.sp_wall_ns = np.zeros(n, np.int64)
        self.vel_sess = np.zeros(n, _B)
        self.axis_anchor = np.zeros((n, 3))
        self.axis_lock = np.zeros(n, np.uint8)
        # PATH（CSR 路径缓冲下标）
        self.path_off = np.zeros(n, np.int32)
        self.path_len = np.zeros(n, np.int32)
        self.path_seg = np.zeros(n, np.int32)
        self.path_tau = np.zeros(n)
        # ORBIT（列见 kernels_l1.O_*）
        self.orb = np.zeros((n, ORB_COLS))
        # 下降剖面、Velocity
        self.desc_v = np.full(n, ELAND_SPEED)
        self.vel_frame = np.zeros(n, np.uint8)
        self.vel_vmax = np.full(n, np.inf)
        self.vel_yawrate = np.zeros(n)
        self.hold_alt = np.ones(n, _B)
        self.d_free = np.full(n, np.inf)
        # oracle 分 stage 执行时的暂存（aero → integrate）、L0 幽灵机时间偏移
        self.force = np.zeros((n, 3))
        self.l1_cls = np.zeros(n, np.uint8)
        self.kin_t0 = np.zeros(n)
        self.blocks: dict[str, dict[str, np.ndarray]] = {}
        self.block_specs: dict[str, StateBlockSpec] = {}
        for spec in (blocks or {}).values():
            self.add_block(spec)
        self.slot = np.arange(n, dtype=np.int32)
        self.slot.flags.writeable = False
        self._act_ver = -1
        self._act_idx = np.zeros(0, np.int32)
        self.enu = EnuViews(self)
        self.l1_tick_cache: tuple | None = None  # (tick, l1 下标, RTL 子阶段)：l1 stage 写、contact stage 同 tick 复用

    def add_block(self, spec: StateBlockSpec) -> None:
        if spec.name in self.blocks:
            raise ValueError(f"状态块已存在：{spec.name}")
        self.blocks[spec.name] = {f: np.zeros((self.capacity, *shape), dt) for f, (dt, shape) in spec.fields.items()}
        self.block_specs[spec.name] = spec

    def touch(self) -> None:
        self.version += 1

    def active_idx(self) -> np.ndarray:
        return np.flatnonzero(self.active)

    def active_idx32(self) -> np.ndarray:
        return np.flatnonzero(self.active).astype(np.int32)

    # ------------------------------------------------------------ ENU 视图与换算助手（M07、M09、M10、M13 使用）
    def pos_enu_view(self) -> np.ndarray:
        return self.enu.pos

    def vel_enu_view(self) -> np.ndarray:
        return self.enu.vel

    def set_wind_from_enu(self, w_enu: np.ndarray, slots: np.ndarray | None = None) -> None:
        """ENU 去向风 -> NED 空气速度 `w_ned = (w_n, w_e, −w_u)`（M08 §6.5.1；M07 env stage 调用）。按 slot 写入时 numba 可用
        走融合重排（与 `enu_to_ned` 逐位相同；N = 1000、50 Hz，此前约 85 µs/次，FX2-R3）。"""
        if slots is None:
            enu_to_ned(w_enu, out=self.wind)
        elif self.enu.use_numba and isinstance(w_enu, np.ndarray) and w_enu.dtype == np.float64 and w_enu.ndim == 2 \
                and w_enu.flags.c_contiguous and isinstance(slots, np.ndarray) and slots.dtype == np.int64 \
                and slots.ndim == 1 and w_enu.shape[0] == slots.size:
            from .kernels_tap import enu_to_ned_rows

            enu_to_ned_rows(slots, w_enu, self.wind)
        else:
            self.wind[slots] = enu_to_ned(w_enu)

    def set_rho(self, rho: np.ndarray, slots: np.ndarray | None = None) -> None:
        if slots is None:
            self.rho[:] = rho
        else:
            self.rho[slots] = rho

    # ------------------------------------------------------------ checkpoint
    def checkpoint_arrays(self, copy: bool = True) -> dict[str, np.ndarray]:
        """`copy=False`：返回数组本身（调用方负责拷贝；sim-core checkpoint 由 CheckpointStore.save 拷贝进双缓冲，此前先
        `.copy()` 一遍再拷贝一遍，N = 1000 约 1 ms）。"""
        cp = (lambda a: a.copy()) if copy else (lambda a: a)
        out = {k: cp(getattr(self, k)) for k in CORE_ARRAYS}
        for b, arrs in self.blocks.items():
            if self.block_specs[b].checkpoint:
                for f, a in arrs.items():
                    out[f"blk.{b}.{f}"] = cp(a)
        return out

    def restore_arrays(self, arrays: dict[str, np.ndarray]) -> None:
        for k, a in arrays.items():
            if k.startswith("blk."):
                _, b, f = k.split(".", 2)
                if b in self.blocks and f in self.blocks[b]:
                    np.copyto(self.blocks[b][f], a)
            elif k in CORE_ARRAYS:
                np.copyto(getattr(self, k), a)
        self.touch()
