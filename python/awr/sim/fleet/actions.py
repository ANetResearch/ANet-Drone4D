"""运动模式进入动作（M08 §6.9.2 K01–K19；ingest 在 apply_tick 执行，CommandEngine 与 SupervisorQueue 共用）。

全部参数为内部 NED（CommandEngine 在准入边界做 ENU → NED 换算，M08 §6.5.1），按 slot 数组向量化；每个动作设置
`mode_t`、置 `EVT_MODE` 并 `touch()`。参考衔接规则（g08 §5.3；M08-FR-022）：已在 GOTO 或 PATH 时收到新导航目标，
保留参考状态并按 STOP_MOTION 判定是否先刹停；从其他模式进入时以机体状态初始化参考（PX4 行为）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from awr.world.georef.frames import yaw_ned_from_enu

from . import kernels_l1 as K
from . import params_px4 as P
from .path import MAX_PATH_POINTS, dedupe, topp_lite, total_time
from .state import EVT_MODE, YAWB

if TYPE_CHECKING:
    from .path import PathBuffer
    from .profiles import ProfileTable
    from .state import FleetState

__all__ = ["arm", "begin_eland", "begin_failsafe", "begin_goto", "begin_hold", "begin_kill", "begin_land", "begin_orbit",
           "begin_path", "begin_rtl", "begin_takeoff", "begin_traj", "begin_velocity", "disarm", "release_path",
           "set_offboard"]

NAV_KEEP = (K.M_GOTO, K.M_PATH)


def _s(slots) -> np.ndarray:
    return np.atleast_1d(np.asarray(slots, np.int64))


def _mark(S: FleetState, s: np.ndarray, mode: int, phase: int, t: float) -> None:
    S.ctrl_mode[s] = mode
    S.ctrl_phase[s] = phase
    S.mode_t[s] = t
    S.mode_evt[s] |= EVT_MODE
    S.td_t[s] = np.nan
    S.thr_cap[s] = 1.0
    S.touch()


def _init_ref(S: FleetState, s: np.ndarray) -> None:
    S.tr_x[s] = S.p[s]
    S.tr_v[s] = S.v[s]
    S.tr_a[s] = 0.0
    S.stopping[s] = False


def release_path(S: FleetState, PB: PathBuffer | None, s: np.ndarray) -> None:
    if PB is None:
        return
    for i in s:
        if S.path_len[i] > 0:
            PB.release(int(S.path_off[i]))
            S.path_len[i] = 0


def begin_takeoff(S: FleetState, slots, alt_m, t: float) -> None:
    """K01：记录起飞点，SPOOLUP 1 s 后 CLIMB 到 `ground + alt`（已在 SPOOLUP ≥ 1 s 时直接 CLIMB）。"""
    s = _s(slots)
    alt = np.broadcast_to(np.asarray(alt_m, np.float64), s.shape)
    spooled = (S.ctrl_mode[s] == K.M_SPOOLUP) & (t - S.mode_t[s] >= P.COM_SPOOLUP_TIME)
    S.target[s, 0] = S.p[s, 0]
    S.target[s, 1] = S.p[s, 1]
    S.target[s, 2] = S.ground_z[s] - alt
    S.land_xy[s] = S.p[s, :2]
    S.tr_x[s] = S.p[s]
    S.tr_v[s] = 0.0
    S.tr_a[s] = 0.0
    _mark(S, s, K.M_TAKEOFF, 0, t)
    S.ctrl_phase[s[spooled]] = 1


def begin_goto(S: FleetState, slots, goal_ned: np.ndarray, speed, yaw_ned, t: float, *, stop_motion: bool = True) -> None:
    """K04：GOTO；已在 GOTO/PATH 时 STOP_MOTION（‖tr_v‖ > 0.05 且夹角余弦 < 0.98 先刹停），否则以机体状态初始化参考。"""
    s = _s(slots)
    goal = np.broadcast_to(np.asarray(goal_ned, np.float64), (s.size, 3))
    keep = np.isin(S.ctrl_mode[s], NAV_KEEP)
    ks = s[keep]
    if ks.size:
        tv = S.tr_v[ks]
        d = goal[keep] - S.tr_x[ks]
        v = np.linalg.norm(tv, axis=1)
        c = (tv * d).sum(1) / (v * np.linalg.norm(d, axis=1) + 1e-9)
        S.stopping[ks] = (v > 0.05) & (c < 0.98) & bool(stop_motion)
    _init_ref(S, s[~keep])
    S.target[s] = goal
    sp = np.broadcast_to(np.asarray(speed, np.float64), s.shape)
    S.speed_cmd[s] = sp
    if yaw_ned is not None:
        y = np.broadcast_to(np.asarray(yaw_ned, np.float64), s.shape)
        ok = ~np.isnan(y)
        S.yaw_sp[s[ok]] = y[ok]
    _mark(S, s, K.M_GOTO, 0, t)


def begin_hold(S: FleetState, slots, t: float) -> None:
    """K05：刹停后锁定（HOLD.0 → HOLD.1）。IDLE、TAKEOFF.0 等非飞行模式以机体状态初始化参考。"""
    s = _s(slots)
    init = np.isin(S.ctrl_mode[s], (K.M_IDLE, K.M_SPOOLUP, K.M_KILLED, K.M_VELOCITY, K.M_OFFBOARD))
    _init_ref(S, s[init])
    S.target[s] = S.tr_x[s]
    _mark(S, s, K.M_HOLD, 0, t)


def begin_land(S: FleetState, slots, at_xy_ned: np.ndarray | None, t: float) -> None:
    """LAND：`at = here` 直接 DESCEND；给定落点（home 或 pos）先 GOTO 到落点上方（当前高度）再下降。"""
    s = _s(slots)
    init = np.isin(S.ctrl_mode[s], (K.M_IDLE, K.M_TAKEOFF, K.M_SPOOLUP, K.M_VELOCITY, K.M_OFFBOARD))
    _init_ref(S, s[init])
    if at_xy_ned is None:
        S.land_xy[s] = S.p[s, :2]
        _mark(S, s, K.M_LAND, 1, t)
        S.tr_x[s, :2] = S.p[s, :2]
    else:
        xy = np.broadcast_to(np.asarray(at_xy_ned, np.float64), (s.size, 2))
        S.land_xy[s] = xy
        S.target[s, 2] = S.tr_x[s, 2]
        _mark(S, s, K.M_LAND, 0, t)


def begin_land_home(S: FleetState, slots, t: float) -> None:
    """LAND `at = home`：先 GOTO 到 home 上方再下降。"""
    s = _s(slots)
    begin_land(S, s, S.home[s, :2].copy(), t)


def begin_rtl(S: FleetState, slots, z_rtl_up, v_rtl, t: float, via_ned_xy=None) -> None:
    """K10：RTL；阶段随 M09 FSM 的 RTL 子模式推进，refgen 按阶段选择目标（CLIMB 在当前 xy 升至 z_rtl）。
    `via_ned_xy`（k×2 或 2，NED 水平坐标，NaN 行为直飞）：CRUISE 先经绕行点再飞 home 上方（ADR-054），缺省直飞。"""
    s = _s(slots)
    init = np.isin(S.ctrl_mode[s], (K.M_IDLE, K.M_TAKEOFF, K.M_SPOOLUP, K.M_VELOCITY, K.M_OFFBOARD))
    _init_ref(S, s[init])
    S.z_rtl[s] = np.broadcast_to(np.asarray(z_rtl_up, np.float64), s.shape)
    S.v_rtl[s] = np.broadcast_to(np.asarray(v_rtl, np.float64), s.shape)
    if via_ned_xy is None:
        S.rtl_via[s] = np.nan
    else:
        S.rtl_via[s] = np.broadcast_to(np.asarray(via_ned_xy, np.float64), (s.size, 2))
    S.land_xy[s] = S.p[s, :2]
    S.stopping[s] = False
    _mark(S, s, K.M_RTL, 0, t)


def begin_eland(S: FleetState, slots, rate: float, t: float) -> None:
    s = _s(slots)
    S.land_xy[s] = S.p[s, :2]
    S.desc_v[s] = float(rate)
    S.tr_x[s, 2] = np.minimum(S.tr_x[s, 2], S.p[s, 2] + 1.0)
    _mark(S, s, K.M_ELAND, 0, t)


def begin_failsafe(S: FleetState, slots, rate: float, t: float) -> None:
    s = _s(slots)
    S.land_xy[s] = S.p[s, :2]
    S.desc_v[s] = float(rate)
    _mark(S, s, K.M_DESCENT, 0, t)


def begin_kill(S: FleetState, slots, t: float) -> None:
    s = _s(slots)
    S.thrust[s] = 0.0
    S.thr_sp[s] = 0.0
    _mark(S, s, K.M_KILLED, 0, t)


def arm(S: FleetState, slots, t: float) -> None:
    s = _s(slots)
    _mark(S, s, K.M_SPOOLUP, 0, t)


def disarm(S: FleetState, slots, t: float) -> None:
    s = _s(slots)
    S.thrust[s] = 0.0
    S.thr_sp[s] = 0.0
    _mark(S, s, K.M_IDLE, 0, t)


def begin_traj(S: FleetState, slots, t: float) -> None:
    """K18：运动提供者接管（TRAJ）；按机体状态初始化 tr_x/tr_v/tr_a。"""
    s = _s(slots)
    S.tr_x[s] = S.p[s]
    S.tr_v[s] = S.v[s]
    S.tr_a[s] = 0.0
    S.pos_ref[s] = S.p[s]
    _mark(S, s, K.M_TRAJ, 0, t)


_ENU_NED_PERM = np.array([1, 0, 2])
_NO_PSI = np.zeros(0)
_ENU_NED_SIGN = np.array([1.0, 1.0, -1.0])


def set_traj_enu(S: FleetState, slots, p_enu: np.ndarray, v_enu: np.ndarray, a_enu: np.ndarray,
                 psi_enu: np.ndarray | None = None) -> None:
    """TRAJ 设定点写入（M10 跟踪器 order 027 用；ENU 输入，fleet 内换算为 NED，M08-FR-086、NFR-019）。"""
    s = _s(slots)
    pa, va, aa = (np.asarray(x, np.float64).reshape(-1, 3) for x in (p_enu, v_enu, a_enu))
    ya = np.asarray(psi_enu, np.float64).reshape(-1) if psi_enu is not None else _NO_PSI
    same = pa.shape[0] == va.shape[0] == aa.shape[0] == s.size and (psi_enu is None or ya.size == s.size)
    if K.HAVE_NUMBA and s.size and same:
        # 融合写入（与下方 numpy 实现逐位相同，每 125 Hz 调用，FX-SIM1 固定开销优化）；广播形状走 numpy 路径
        K.set_traj_nb(s, pa, va, aa, ya, psi_enu is not None, S.tr_x, S.tr_v, S.tr_a, S.yaw_sp)
        return
    # (E, N, U) -> (N, E, -U)：列重排加符号，与 `enu_to_ned` 逐位相同
    S.tr_x[s] = pa[:, _ENU_NED_PERM] * _ENU_NED_SIGN
    S.tr_v[s] = va[:, _ENU_NED_PERM] * _ENU_NED_SIGN
    S.tr_a[s] = aa[:, _ENU_NED_PERM] * _ENU_NED_SIGN
    if psi_enu is not None:
        S.yaw_sp[s] = yaw_ned_from_enu(np.asarray(psi_enu, np.float64))


def set_traj_enu_rows(S: FleetState, slots, P_enu: np.ndarray, V_enu: np.ndarray, A_enu: np.ndarray,
                      PSI_enu: np.ndarray) -> None:
    """`set_traj_enu(S, slots, P[slots], V[slots], A[slots], PSI[slots])` 的等价写法：输入是按 slot 行组织的 (N, 3)、(N,)
    数组，只读 slots 行（M10 跟踪器 125 Hz 调用，省去四次花式下标拷贝；与 set_traj_enu 逐位相同，FX2-R3）。"""
    s = _s(slots)
    if K.HAVE_NUMBA and s.size:
        K.set_traj_rows_nb(s, P_enu, V_enu, A_enu, PSI_enu, S.tr_x, S.tr_v, S.tr_a, S.yaw_sp)
        return
    set_traj_enu(S, s, P_enu[s], V_enu[s], A_enu[s], PSI_enu[s])


def begin_velocity(S: FleetState, slots, frame_body: bool, vmax: float, hold_alt: bool, t: float) -> None:
    s = _s(slots)
    S.vel_cmd[s] = 0.0
    S.vel_frame[s] = 1 if frame_body else 0
    S.vel_vmax[s] = float(vmax) if vmax is not None and math.isfinite(vmax) else np.inf
    S.hold_alt[s] = bool(hold_alt)
    S.vel_yawrate[s] = 0.0
    S.axis_lock[s] = 0
    S.vel_sess[s] = True
    S.d_free[s] = np.inf
    _mark(S, s, K.M_VELOCITY, 0, t)


def set_offboard(S: FleetState, slots, pos_ned: np.ndarray, t: float) -> None:
    """OFFBOARD_POS（只用于 SIH 回归与测试，FR-030）。"""
    s = _s(slots)
    S.pos_sp[s] = np.broadcast_to(np.asarray(pos_ned, np.float64), (s.size, 3))
    _mark(S, s, K.M_OFFBOARD, 0, t)


def begin_path(S: FleetState, PB: PathBuffer, T: ProfileTable, slot: int, waypoints_ned: np.ndarray, speed: float,
               yaw_ned: np.ndarray | None, t: float) -> float | None:
    """PATH：以当前参考点（导航中）或机体位置为 w[0]，TOPP-lite 写入 PathBuffer；返回预测总时长，空间不足返回 None。"""
    i = int(slot)
    keep = int(S.ctrl_mode[i]) in NAV_KEEP
    start = S.tr_x[i].copy() if keep else S.p[i].copy()
    w = np.vstack([start[None], np.asarray(waypoints_ned, np.float64)])
    yw = np.full(len(w), np.nan)
    if yaw_ned is not None:
        yw[1:] = yaw_ned
    w, ki = dedupe(w)
    yw = yw[ki]
    if len(w) < 2:
        w = np.vstack([w, w[-1:] + np.array([0.0, 0.0, 0.0])])
        yw = np.append(yw, np.nan)
    if len(w) > MAX_PATH_POINTS + 1:
        return None
    lim = int(S.limits_id[i])
    cruise = min(float(speed) if speed is not None and math.isfinite(speed) else float(T.LT[lim, K.L_CRUISE]),
                 float(T.LT[lim, K.L_VXY]))
    u0 = w[1] - w[0]
    n0 = float(np.linalg.norm(u0))
    v_start = max(0.0, float(np.dot(S.tr_v[i] if keep else S.v[i], u0) / n0)) if n0 > 1e-9 else 0.0
    seg, vw = topp_lite(w, v_start, cruise, float(T.LT[lim, K.L_ACC]))
    release_path(S, PB, np.array([i]))
    off = PB.alloc(PB.rows_for(len(w)))
    if off is None:
        return None
    PB.write(off, w, yw, seg, vw)
    if not keep:
        _init_ref(S, np.array([i]))
    S.path_off[i] = off
    S.path_len[i] = len(seg)  # 段数（直线 + 圆弧）
    S.path_seg[i] = off
    S.path_tau[i] = 0.0
    S.stopping[i] = False
    _mark(S, np.array([i]), K.M_PATH, 0, t)
    return total_time(seg)


def begin_orbit(S: FleetState, slots, center_ned: np.ndarray, radius_m: float, speed: float, cw: bool, turns: float,
                yaw_behavior: str, yaw_fixed_ned: float | None, t: float) -> None:
    """ORBIT：距圆 > 1 m 时先以 GOTO 入圈；随后按 ACC_HOR/R 斜坡角速度绕圈并计圈（cw 为俯视顺时针，NED 相位递增）。"""
    s = _s(slots)
    c = np.asarray(center_ned, np.float64)
    OB = S.orb
    OB[s, K.O_CX], OB[s, K.O_CY], OB[s, K.O_CZ] = c[0], c[1], c[2]
    OB[s, K.O_R] = float(radius_m)
    OB[s, K.O_W] = 0.0
    OB[s, K.O_TURN] = 0.0
    OB[s, K.O_GOAL] = float(turns)
    OB[s, K.O_V] = float(speed)
    OB[s, K.O_DIR] = 1.0 if cw else -1.0
    OB[s, K.O_YAWB] = YAWB.get(yaw_behavior, 0.0)
    OB[s, K.O_YAW] = float(yaw_fixed_ned) if yaw_fixed_ned is not None else S.yaw_sp[s]
    keep = np.isin(S.ctrl_mode[s], (*NAV_KEEP, K.M_ORBIT))
    _init_ref(S, s[~keep])
    S.speed_cmd[s] = float(speed)
    ex = S.p[s, 0] - c[0]
    ey = S.p[s, 1] - c[1]
    de = np.sqrt(ex * ex + ey * ey)
    on = (np.abs(de - radius_m) <= 1.0) & (np.abs(S.p[s, 2] - c[2]) <= 1.0)
    _mark(S, s, K.M_ORBIT, 0, t)
    j = s[on]
    if j.size:
        OB[j, K.O_TH] = np.arctan2(ey[on], ex[on])
        S.ctrl_phase[j] = 1
        S.tr_x[j] = S.p[j]
