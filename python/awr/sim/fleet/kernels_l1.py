"""numba 融合核 `tick_l1`：refgen → pos_ctrl → att_ctrl → motor → aero → integrate，逐机一次循环（M08-FR-038；M08 §6.5、§9.2）。

与 numpy oracle（`setpoint.py`、`px4lite.py`、`aero.py`）逐式对应（ADR-021 两项对拍，`tests/sim/test_kernel_parity.py`）：
运算顺序一致（范数一律先平方和后开方、同序累加），`fastmath=False`，不使用 `prange`（M08-FR-007）。numba 只允许出现在
`awr/sim/fleet/kernels_*.py`（M08-NFR-019）。内部坐标 NED/FRD，四元数 (w, x, y, z)。

参数按下标读取、核内不分配数组：
- `PT`：ProfileTable 矩阵 f64[P, 10]（列见 `P_*` 常量）；`LT`：限速配置矩阵 f64[L, 4]（列见 `L_*`）；
- `orb`：f64[N, 12] 绕飞状态（列见 `O_*`）；PathBuffer 为 CSR 行存储（`path.py`，段表列见 `SG_*`）；
- `cfg`：f64[8]（`C_*`）：dt、t_s、time_stretch、stop_motion、omega_fail、fault_enable。

L1 类别（每 tick 开始时按初始运动模式判定，oracle 同）：PARKED（IDLE，或 KILLED 且已触地/坠毁）不积分；FALL（空中 KILLED）
推力 0 自由落体；其余闭环。开环（SPOOLUP、TAKEOFF.0）在 refgen 之后判定。
"""

# ruff: noqa: SIM108, SIM109  （numba 核与 oracle 逐式对应：保留显式 if/else 与逐项比较）

from __future__ import annotations

import math

import numpy as np

from . import params_px4 as P

try:  # numba 不可用时由 FleetSim 自动退回 oracle（M08-FR-040；ADR-038）
    from numba import njit

    HAVE_NUMBA = True
    NUMBA_ERROR: str | None = None
except Exception as _e:  # pragma: no cover - 取决于环境
    HAVE_NUMBA = False
    NUMBA_ERROR = f"{type(_e).__name__}: {_e}"

    def njit(*args, **kwargs):  # type: ignore[no-redef]
        if args and callable(args[0]):
            return args[0]
        return lambda f: f


__all__ = ["HAVE_NUMBA", "NUMBA_ERROR", "tick_l1", "warmup"]

# ---------------------------------------------------------------- 常量（numba 以编译期常量读取模块全局量）
G = P.G
RHO0 = P.RHO0
K_RHO = P.K_RHO
XY_P = P.MPC_XY_P
Z_P = P.MPC_Z_P
KVP_XY = P.MPC_XY_VEL_P_ACC
KVI_XY = P.MPC_XY_VEL_I_ACC
KVD_XY = P.MPC_XY_VEL_D_ACC
KVP_Z = P.MPC_Z_VEL_P_ACC
KVI_Z = P.MPC_Z_VEL_I_ACC
KVD_Z = P.MPC_Z_VEL_D_ACC
Z_VEL_MAX_UP = P.MPC_Z_VEL_MAX_UP
Z_VEL_MAX_DN = P.MPC_Z_VEL_MAX_DN
Z_V_AUTO_UP = P.MPC_Z_V_AUTO_UP
Z_V_AUTO_DN = P.MPC_Z_V_AUTO_DN
ACC_UP = P.MPC_ACC_UP_MAX
ACC_DN = P.MPC_ACC_DOWN_MAX
JERK = P.MPC_JERK_AUTO
XY_ERR = P.MPC_XY_ERR_MAX
Z_ERR = P.MPC_Z_ERR_MAX
TILT = P.TILTMAX_RAD
THR_MIN = P.MPC_THR_MIN
THR_MAX = P.MPC_THR_MAX
XY_MARG = P.MPC_THR_XY_MARG
K_ROLL = P.MC_ROLL_P
K_PITCH = P.MC_PITCH_P
K_YAW = P.MC_YAW_P
RMAX_ROLL = math.radians(P.MC_ROLLRATE_MAX)
RMAX_PITCH = math.radians(P.MC_PITCHRATE_MAX)
YAWRAUTO = math.radians(P.MPC_YAWRAUTO_MAX)
TKO_SPEED = P.MPC_TKO_SPEED
TKO_RAMP = P.MPC_TKO_RAMP_T
SPOOLUP_T = P.COM_SPOOLUP_TIME
LAND_SPEED = P.MPC_LAND_SPEED
LAND_CRWL = P.MPC_LAND_CRWL
LAND_ALT1 = P.MPC_LAND_ALT1
LAND_ALT2 = P.MPC_LAND_ALT2
LAND_ALT3 = P.MPC_LAND_ALT3
LAND_FAST = P.LAND_FAST_SPEED
TD_RAMP = P.TOUCHDOWN_RAMP_S
DESC_ALT = P.RTL_DESCEND_ALT
VIA_ACC2 = P.RTL_VIA_ACCEPT_M * P.RTL_VIA_ACCEPT_M
V_LOCK = P.VEL_AXIS_LOCK_MPS
V_DRIFT = P.VEL_AXIS_DRIFT_M
V_PULL = P.VEL_AXIS_PULL
FREE_MARGIN = P.VEL_FREE_MARGIN_M
TWO_PI = 2.0 * math.pi

# 运动模式（CtrlMode，M08 §6.9.1）
M_IDLE, M_SPOOLUP, M_TAKEOFF, M_GOTO, M_PATH, M_ORBIT, M_HOLD, M_VELOCITY = 0, 1, 2, 3, 4, 5, 6, 7
M_LAND, M_RTL, M_OFFBOARD, M_ELAND, M_DESCENT, M_KILLED, M_KINEMATIC, M_TRAJ = 8, 9, 10, 11, 12, 13, 14, 15
# mode_evt 位
E_MODE, E_ARRIVED, E_PHASE, E_TOUCHDOWN, E_LIFTOFF, E_COLLISION = 1, 2, 4, 8, 16, 32
# ProfileTable 矩阵列
P_MASS, P_TMAX, P_HOVER, P_TAU, P_NROT, P_WMAX, P_AERO, P_KDV, P_CDA, P_CRD = range(10)
# 限速配置矩阵列
L_VXY, L_CRUISE, L_ACC, L_YAWRATE = range(4)
# orb 列
O_CX, O_CY, O_CZ, O_R, O_W, O_TH, O_TURN, O_GOAL, O_V, O_DIR, O_YAWB, O_YAW = range(12)
# PathBuffer 段表列（TOPP-lite 输出，`kernels_path.topp_lite_nb`）
SG_KIND, SG_P0, SG_U, SG_E, SG_R, SG_TH, SG_LEN, SG_T0, SG_T = 0, 1, 4, 7, 10, 11, 12, 13, 14
SG_TA, SG_TC, SG_VC, SG_V0, SG_V1, SG_A, SG_WP, SG_COLS = 15, 16, 17, 18, 19, 20, 21, 22
# cfg 列
C_DT, C_T, C_TS, C_SM, C_WFAIL, C_FAULTS = range(6)
# RTL 阶段（M09 RTL 子模式）
R_CLIMB, R_CRUISE, R_DESCEND, R_FINAL = 0, 1, 2, 3


@njit(cache=True, fastmath=False)
def _vmax(j, a, d, vf):
    """PX4 TrajMath::computeMaxSpeedFromDistance。"""
    b = 4.0 * a * a / j
    c = -2.0 * a * d - vf * vf
    r = 0.5 * (-b + math.sqrt(b * b - 4.0 * c))
    return r if r > vf else vf


@njit(cache=True, fastmath=False)
def _clip(x, lo, hi):
    return min(max(x, lo), hi)


@njit(cache=True, fastmath=False)
def _land_speed(agl):
    if agl > LAND_ALT1:
        return LAND_FAST
    if agl > LAND_ALT2:
        return LAND_SPEED + (LAND_FAST - LAND_SPEED) * (agl - LAND_ALT2) / (LAND_ALT1 - LAND_ALT2)
    if agl > LAND_ALT3:
        return LAND_SPEED
    return LAND_CRWL


@njit(cache=True, fastmath=False)
def _stretch(i, p, tr_x, tr_v, ts_on):
    """time_stretch 系数 (ts_xy, ts_z)（PX4 PositionSmoothing::_generateTrajectory，只在机体落后于参考时放慢）。"""
    tsxy = 1.0
    tsz = 1.0
    if ts_on:
        ex = tr_x[i, 0] - p[i, 0]
        ey = tr_x[i, 1] - p[i, 1]
        ez = tr_x[i, 2] - p[i, 2]
        if ex * tr_v[i, 0] + ey * tr_v[i, 1] >= 0.0:
            tsxy = 1.0 - _clip(math.sqrt(ex * ex + ey * ey) / XY_ERR, 0.0, 1.0)
        if ez * tr_v[i, 2] >= 0.0:
            tsz = 1.0 - _clip(abs(ez) / Z_ERR, 0.0, 1.0)
    return tsxy, tsz


@njit(cache=True, fastmath=False)
def _advance(i, ax, ay, az, dt, ts_on, p, tr_x, tr_v, tr_a):
    tsxy, tsz = _stretch(i, p, tr_x, tr_v, ts_on)
    d0 = dt * tsxy
    d2 = dt * tsz
    jl = JERK * d0
    tr_a[i, 0] = tr_a[i, 0] + _clip(ax - tr_a[i, 0], -jl, jl)
    tr_v[i, 0] = tr_v[i, 0] + tr_a[i, 0] * d0
    tr_x[i, 0] = tr_x[i, 0] + tr_v[i, 0] * d0
    tr_a[i, 1] = tr_a[i, 1] + _clip(ay - tr_a[i, 1], -jl, jl)
    tr_v[i, 1] = tr_v[i, 1] + tr_a[i, 1] * d0
    tr_x[i, 1] = tr_x[i, 1] + tr_v[i, 1] * d0
    jz = JERK * d2
    tr_a[i, 2] = tr_a[i, 2] + _clip(az - tr_a[i, 2], -jz, jz)
    tr_v[i, 2] = tr_v[i, 2] + tr_a[i, 2] * d2
    tr_x[i, 2] = tr_x[i, 2] + tr_v[i, 2] * d2


@njit(cache=True, fastmath=False)
def _goto(i, tx, ty, tz, speed, lim, dt, ts_on, p, tr_x, tr_v, tr_a, stopping, LT):
    """PositionSmoothing-lite（g08 §5.1）+ STOP_MOTION；返回参考到达。"""
    acc = LT[lim, L_ACC]
    vxy_max = LT[lim, L_VXY]
    dx = tx - tr_x[i, 0]
    dy = ty - tr_x[i, 1]
    dz = tz - tr_x[i, 2]
    dxy = math.sqrt(dx * dx + dy * dy)
    cr = LT[lim, L_CRUISE] if math.isnan(speed) else speed
    cr = min(cr, vxy_max)
    vxy = min(cr, _vmax(JERK, acc, dxy, 0.0))
    vzc = Z_V_AUTO_UP if dz < 0.0 else Z_V_AUTO_DN
    vz = min(vzc, _vmax(JERK, ACC_UP, abs(dz), 0.0))
    ux = 0.0
    uy = 0.0
    if dxy > 1e-3:
        dd = max(dxy, 1e-6)
        ux = dx / dd
        uy = dy / dd
    sz = 1.0 if dz > 0.0 else (-1.0 if dz < 0.0 else 0.0)
    vdx = ux * vxy
    vdy = uy * vxy
    vdz = sz * vz
    if stopping[i]:
        vdx = 0.0
        vdy = 0.0
        vdz = 0.0
    ax = 2.0 * (vdx - tr_v[i, 0])
    ay = 2.0 * (vdy - tr_v[i, 1])
    az = 2.0 * (vdz - tr_v[i, 2])
    nxy = math.sqrt(ax * ax + ay * ay)
    s = min(1.0, acc / max(nxy, 1e-9))
    ax = ax * s
    ay = ay * s
    az = _clip(az, -ACC_UP, ACC_DN)
    _advance(i, ax, ay, az, dt, ts_on, p, tr_x, tr_v, tr_a)
    sp = math.sqrt(tr_v[i, 0] * tr_v[i, 0] + tr_v[i, 1] * tr_v[i, 1] + tr_v[i, 2] * tr_v[i, 2])
    stopping[i] = stopping[i] and sp >= 0.05
    ex = tx - tr_x[i, 0]
    ey = ty - tr_x[i, 1]
    ez = tz - tr_x[i, 2]
    return math.sqrt(ex * ex + ey * ey + ez * ez) < 0.05 and sp < 0.05


@njit(cache=True, fastmath=False)
def _brake(i, lim, dt, ts_on, p, tr_x, tr_v, tr_a, LT):
    """刹停（HOLD.0）：v_des = 0，同 GOTO 的加速度与 jerk 限幅；返回参考已停。"""
    acc = LT[lim, L_ACC]
    ax = 2.0 * (0.0 - tr_v[i, 0])
    ay = 2.0 * (0.0 - tr_v[i, 1])
    az = 2.0 * (0.0 - tr_v[i, 2])
    nxy = math.sqrt(ax * ax + ay * ay)
    s = min(1.0, acc / max(nxy, 1e-9))
    ax = ax * s
    ay = ay * s
    az = _clip(az, -ACC_UP, ACC_DN)
    _advance(i, ax, ay, az, dt, ts_on, p, tr_x, tr_v, tr_a)
    sp = math.sqrt(tr_v[i, 0] * tr_v[i, 0] + tr_v[i, 1] * tr_v[i, 1] + tr_v[i, 2] * tr_v[i, 2])
    return sp < 0.05


@njit(cache=True, fastmath=False)
def _to_hold(i, t, ctrl_mode, ctrl_phase, target, tr_x, tr_v, tr_a, mode_t, mode_evt, evt):
    """交回悬停（HOLD.1，目标锁定为当前参考点，无跳变）。"""
    ctrl_mode[i] = M_HOLD
    ctrl_phase[i] = 1
    target[i, 0] = tr_x[i, 0]
    target[i, 1] = tr_x[i, 1]
    target[i, 2] = tr_x[i, 2]
    tr_v[i, 0] = 0.0
    tr_v[i, 1] = 0.0
    tr_v[i, 2] = 0.0
    tr_a[i, 0] = 0.0
    tr_a[i, 1] = 0.0
    tr_a[i, 2] = 0.0
    mode_t[i] = t
    mode_evt[i] = mode_evt[i] | evt


@njit(cache=True, fastmath=False)
def _descend(i, vz, dt, land_xy, p, tr_x, tr_v, tr_a):
    """下降剖面：xy 保持降落点，参考以 vz 下降且领先机体不超过 1 m（NED 向下为正）。"""
    tr_x[i, 0] = land_xy[i, 0]
    tr_x[i, 1] = land_xy[i, 1]
    tr_x[i, 2] = min(tr_x[i, 2] + vz * dt, p[i, 2] + 1.0)
    tr_v[i, 0] = 0.0
    tr_v[i, 1] = 0.0
    tr_v[i, 2] = vz
    tr_a[i, 0] = 0.0
    tr_a[i, 1] = 0.0
    tr_a[i, 2] = 0.0


@njit(cache=True, fastmath=False)
def _touchdown(i, t, land_xy, p, tr_x, tr_v, tr_a, thr_cap, td_t):
    """触地后 1 s 内推力上限斜坡到 THR_MIN（FR-026）。"""
    if math.isnan(td_t[i]):
        td_t[i] = t
    el = _clip((t - td_t[i]) / TD_RAMP, 0.0, 1.0)
    thr_cap[i] = 1.0 - el * (1.0 - THR_MIN / THR_MAX)
    tr_x[i, 0] = land_xy[i, 0]
    tr_x[i, 1] = land_xy[i, 1]
    tr_x[i, 2] = p[i, 2] + 0.5
    tr_v[i, 0] = 0.0
    tr_v[i, 1] = 0.0
    tr_v[i, 2] = LAND_CRWL
    tr_a[i, 0] = 0.0
    tr_a[i, 1] = 0.0
    tr_a[i, 2] = 0.0


@njit(cache=True, fastmath=False)
def _seg_eval(k, tau, pb_seg):
    """梯形速度剖面（TOPP-lite 段 k；圆弧段为 ta = 0、tc = T 的匀速段）在段内时间 tau 的 (s, sd, sdd)。"""
    v0 = pb_seg[k, SG_V0]
    v1 = pb_seg[k, SG_V1]
    a = pb_seg[k, SG_A]
    ta = pb_seg[k, SG_TA]
    tc = pb_seg[k, SG_TC]
    vc = pb_seg[k, SG_VC]
    T = pb_seg[k, SG_T]
    L = pb_seg[k, SG_LEN]
    if tau >= T:
        return L, v1, 0.0
    if tau < 0.0:
        tau = 0.0
    if tau < ta:
        return v0 * tau + 0.5 * a * tau * tau, v0 + a * tau, a
    da = (vc * vc - v0 * v0) / (2.0 * a)
    if tau < ta + tc:
        return da + vc * (tau - ta), vc, 0.0
    td = tau - ta - tc
    s = da + vc * tc + vc * td - 0.5 * a * td * td
    return min(s, L), vc - a * td, -a


@njit(cache=True, fastmath=False)
def tick_l1(idx, cfg,
            p, v, a_meas, p_prev, q, omega, thrust, thr_cap, thr_sp, q_sp, yaw_sp, vel_int,
            ctrl_mode, ctrl_phase, mode_evt, mode_t, target, pos_sp, vel_cmd, tr_x, tr_v, tr_a, pos_ref,
            stopping, speed_cmd, z_rtl, v_rtl, land_xy, rtl_via, home, ground_z, agl, in_contact, landed, crash_sub,
            td_t, path_off, path_len, path_seg, path_tau, pb_yaw, pb_seg,
            orb, desc_v, vel_frame, vel_vmax, vel_yawrate, hold_alt, axis_lock, axis_anchor, d_free,
            wind, rho, thrust_scale, motor_ok,
            profile_id, limits_id, rtl_phase, PT, LT):
    dt = cfg[C_DT]
    t = cfg[C_T]
    ts_on = cfg[C_TS] != 0.0  # cfg[C_SM]（stop_motion）由 ingest 的 begin_goto 使用，核内不读
    w_fail = cfg[C_WFAIL]
    faults = cfg[C_FAULTS] != 0.0
    for kk in range(idx.shape[0]):
        i = idx[kk]
        mode0 = ctrl_mode[i]
        pid = profile_id[i]
        lim = limits_id[i]
        # ------------------------------------------------ L1 类别
        if mode0 == M_IDLE or (mode0 == M_KILLED and landed[i]):
            tr_x[i, 0] = p[i, 0]
            tr_x[i, 1] = p[i, 1]
            tr_x[i, 2] = p[i, 2]
            for k in range(3):
                tr_v[i, k] = 0.0
                tr_a[i, k] = 0.0
                thr_sp[i, k] = 0.0
                pos_ref[i, k] = p[i, k]
                vel_int[i, k] = 0.0
            thrust[i] = 0.0
            continue
        fall = mode0 == M_KILLED
        # ------------------------------------------------ refgen（M08 §6.5.2）
        if not fall:
            ph = ctrl_phase[i]
            if mode0 == M_GOTO:
                if _goto(i, target[i, 0], target[i, 1], target[i, 2], speed_cmd[i], lim, dt, ts_on, p, tr_x, tr_v, tr_a,
                         stopping, LT):
                    _to_hold(i, t, ctrl_mode, ctrl_phase, target, tr_x, tr_v, tr_a, mode_t, mode_evt, E_ARRIVED)
            elif mode0 == M_HOLD:
                if ph == 0:
                    if _brake(i, lim, dt, ts_on, p, tr_x, tr_v, tr_a, LT):
                        target[i, 0] = tr_x[i, 0]
                        target[i, 1] = tr_x[i, 1]
                        target[i, 2] = tr_x[i, 2]
                        ctrl_phase[i] = 1
                        mode_evt[i] = mode_evt[i] | E_PHASE
                else:
                    for k in range(3):
                        tr_x[i, k] = target[i, k]
                        tr_v[i, k] = 0.0
                        tr_a[i, k] = 0.0
            elif mode0 == M_TAKEOFF:
                if ph == 0:  # SPOOLUP：推力斜坡到 THR_MIN，参考不动
                    el = t - mode_t[i]
                    frac = _clip(el / SPOOLUP_T, 0.0, 1.0)
                    thr_sp[i, 0] = 0.0
                    thr_sp[i, 1] = 0.0
                    thr_sp[i, 2] = -THR_MIN * frac
                    for k in range(3):
                        tr_x[i, k] = p[i, k]
                        tr_v[i, k] = 0.0
                        tr_a[i, k] = 0.0
                    if el >= SPOOLUP_T:
                        ctrl_phase[i] = 1
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_PHASE
                else:  # CLIMB：xy 保持；垂直速度 3 s 斜坡到 MPC_TKO_SPEED
                    tgt = target[i, 2]
                    dz = tgt - tr_x[i, 2]
                    el = t - mode_t[i]
                    vr = TKO_SPEED * _clip(el / TKO_RAMP, 0.0, 1.0)
                    vb = _vmax(JERK, ACC_UP, abs(dz), 0.0)
                    v_up = min(vr, vb)
                    vz = -v_up if dz < 0.0 else 0.0
                    nz = tr_x[i, 2] + vz * dt
                    if dz < 0.0:
                        nz = max(nz, tgt)
                    else:
                        nz = tgt
                    tr_x[i, 0] = target[i, 0]
                    tr_x[i, 1] = target[i, 1]
                    tr_x[i, 2] = nz
                    tr_v[i, 0] = 0.0
                    tr_v[i, 1] = 0.0
                    tr_v[i, 2] = vz if nz > tgt else 0.0
                    tr_a[i, 0] = 0.0
                    tr_a[i, 1] = 0.0
                    # 斜坡段加速度前馈（否则速度环靠积分追斜坡，斜坡结束时超调）
                    tr_a[i, 2] = -TKO_SPEED / TKO_RAMP if dz < 0.0 and nz > tgt and el < TKO_RAMP and vr <= vb else 0.0
                    alt = max(ground_z[i] - tgt, 0.0)
                    if abs(p[i, 2] - tgt) < max(0.3, 0.05 * alt) and abs(v[i, 2]) < 0.3:
                        _to_hold(i, t, ctrl_mode, ctrl_phase, target, tr_x, tr_v, tr_a, mode_t, mode_evt, E_ARRIVED)
                        target[i, 2] = tgt
                        tr_x[i, 2] = tgt
            elif mode0 == M_LAND:
                if ph == 0:  # GOTO 到降落点上方
                    if _goto(i, land_xy[i, 0], land_xy[i, 1], target[i, 2], speed_cmd[i], lim, dt, ts_on, p, tr_x, tr_v,
                             tr_a, stopping, LT):
                        ctrl_phase[i] = 1
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_PHASE
                elif ph == 1:
                    _descend(i, _land_speed(agl[i]), dt, land_xy, p, tr_x, tr_v, tr_a)
                    if in_contact[i]:
                        ctrl_phase[i] = 2
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_PHASE
                else:
                    _touchdown(i, t, land_xy, p, tr_x, tr_v, tr_a, thr_cap, td_t)
            elif mode0 == M_ELAND or mode0 == M_DESCENT:
                if in_contact[i]:
                    _touchdown(i, t, land_xy, p, tr_x, tr_v, tr_a, thr_cap, td_t)
                elif mode0 == M_ELAND:
                    _descend(i, desc_v[i], dt, land_xy, p, tr_x, tr_v, tr_a)
                else:  # FAILSAFE/DESCENT：xy 冻结，垂直只给前馈下坠
                    tr_x[i, 0] = land_xy[i, 0]
                    tr_x[i, 1] = land_xy[i, 1]
                    tr_x[i, 2] = p[i, 2]
                    tr_v[i, 0] = 0.0
                    tr_v[i, 1] = 0.0
                    tr_v[i, 2] = desc_v[i]
                    tr_a[i, 0] = 0.0
                    tr_a[i, 1] = 0.0
                    tr_a[i, 2] = 0.0
            elif mode0 == M_RTL:
                rp = rtl_phase[i]
                if rp != ph:
                    ctrl_phase[i] = rp
                    mode_evt[i] = mode_evt[i] | E_PHASE
                if rp < R_FINAL:
                    if rp == R_CLIMB:
                        gx = land_xy[i, 0]
                        gy = land_xy[i, 1]
                        gz = -z_rtl[i]
                    elif rp == R_CRUISE:
                        gx = home[i, 0]
                        gy = home[i, 1]
                        gz = -z_rtl[i]
                        vx = rtl_via[i, 0]
                        if vx == vx:  # 绕行点（ADR-054）：参考点到达接受半径前先飞向它，之后清为 NaN 并转向 home
                            vy = rtl_via[i, 1]
                            ddx = vx - tr_x[i, 0]
                            ddy = vy - tr_x[i, 1]
                            if ddx * ddx + ddy * ddy > VIA_ACC2:
                                gx = vx
                                gy = vy
                            else:
                                rtl_via[i, 0] = math.nan
                                rtl_via[i, 1] = math.nan
                    else:
                        gx = home[i, 0]
                        gy = home[i, 1]
                        gz = home[i, 2] - DESC_ALT
                    target[i, 0] = gx
                    target[i, 1] = gy
                    target[i, 2] = gz
                    _goto(i, gx, gy, gz, v_rtl[i], lim, dt, ts_on, p, tr_x, tr_v, tr_a, stopping, LT)
                else:  # FINAL：LAND 剖面（at = home），触地转 LAND.2
                    land_xy[i, 0] = home[i, 0]
                    land_xy[i, 1] = home[i, 1]
                    _descend(i, _land_speed(agl[i]), dt, land_xy, p, tr_x, tr_v, tr_a)
                    if in_contact[i]:
                        ctrl_mode[i] = M_LAND
                        ctrl_phase[i] = 2
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_PHASE
            elif mode0 == M_PATH:
                off = path_off[i]
                last = off + path_len[i] - 1
                tsxy, tsz = _stretch(i, p, tr_x, tr_v, ts_on)
                path_tau[i] = path_tau[i] + dt * min(tsxy, tsz)
                k = path_seg[i]
                while k < last and path_tau[i] >= pb_seg[k, SG_T0] + pb_seg[k, SG_T]:
                    k += 1
                path_seg[i] = k
                s, sd, sdd = _seg_eval(k, path_tau[i] - pb_seg[k, SG_T0], pb_seg)
                ux = pb_seg[k, SG_U]
                uy = pb_seg[k, SG_U + 1]
                uz = pb_seg[k, SG_U + 2]
                if pb_seg[k, SG_KIND] == 0.0:
                    tr_x[i, 0] = pb_seg[k, SG_P0] + ux * s
                    tr_x[i, 1] = pb_seg[k, SG_P0 + 1] + uy * s
                    tr_x[i, 2] = pb_seg[k, SG_P0 + 2] + uz * s
                    tr_v[i, 0] = ux * sd
                    tr_v[i, 1] = uy * sd
                    tr_v[i, 2] = uz * sd
                    tr_a[i, 0] = ux * sdd
                    tr_a[i, 1] = uy * sdd
                    tr_a[i, 2] = uz * sdd
                else:  # 圆弧：p = p0 + r·(sinφ·u + (1 − cosφ)·e)，切向 cosφ·u + sinφ·e，向心加速度 sd²/r
                    ex = pb_seg[k, SG_E]
                    ey = pb_seg[k, SG_E + 1]
                    ez = pb_seg[k, SG_E + 2]
                    r = pb_seg[k, SG_R]
                    ph = s / r
                    cs = math.cos(ph)
                    sn = math.sin(ph)
                    ac = sd * sd / r
                    tr_x[i, 0] = pb_seg[k, SG_P0] + r * (sn * ux + (1.0 - cs) * ex)
                    tr_x[i, 1] = pb_seg[k, SG_P0 + 1] + r * (sn * uy + (1.0 - cs) * ey)
                    tr_x[i, 2] = pb_seg[k, SG_P0 + 2] + r * (sn * uz + (1.0 - cs) * ez)
                    ux, uy, uz = cs * ux + sn * ex, cs * uy + sn * ey, cs * uz + sn * ez
                    tr_v[i, 0] = ux * sd
                    tr_v[i, 1] = uy * sd
                    tr_v[i, 2] = uz * sd
                    tr_a[i, 0] = ux * sdd + ac * (cs * ex - sn * pb_seg[k, SG_U])
                    tr_a[i, 1] = uy * sdd + ac * (cs * ey - sn * pb_seg[k, SG_U + 1])
                    tr_a[i, 2] = uz * sdd + ac * (cs * ez - sn * pb_seg[k, SG_U + 2])
                yw = pb_yaw[off + int(pb_seg[k, SG_WP])]
                if not math.isnan(yw):
                    yaw_sp[i] = yw
                elif ux * ux + uy * uy > 0.01:
                    yaw_sp[i] = math.atan2(uy, ux)
                if k >= last and path_tau[i] >= pb_seg[k, SG_T0] + pb_seg[k, SG_T]:
                    _to_hold(i, t, ctrl_mode, ctrl_phase, target, tr_x, tr_v, tr_a, mode_t, mode_evt, E_ARRIVED)
            elif mode0 == M_ORBIT:
                cx = orb[i, O_CX]
                cy = orb[i, O_CY]
                cz = orb[i, O_CZ]
                R = orb[i, O_R]
                if ph == 0:  # 入圈：GOTO 到圆上最近点
                    ex = tr_x[i, 0] - cx
                    ey = tr_x[i, 1] - cy
                    de = math.sqrt(ex * ex + ey * ey)
                    th0 = math.atan2(ey, ex) if de > 1e-6 else 0.0
                    gx = cx + R * math.cos(th0)
                    gy = cy + R * math.sin(th0)
                    if _goto(i, gx, gy, cz, speed_cmd[i], lim, dt, ts_on, p, tr_x, tr_v, tr_a, stopping, LT):
                        ctrl_phase[i] = 1
                        orb[i, O_TH] = th0
                        orb[i, O_W] = 0.0
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_PHASE
                else:
                    acc = LT[lim, L_ACC]
                    w_max = min(orb[i, O_V], math.sqrt(acc * R)) / R
                    dr = orb[i, O_DIR]
                    wa = min(abs(orb[i, O_W]) + acc / R * dt, w_max)
                    orb[i, O_W] = dr * wa
                    tsxy, tsz = _stretch(i, p, tr_x, tr_v, ts_on)
                    dth = orb[i, O_W] * dt * min(tsxy, tsz)
                    orb[i, O_TH] = orb[i, O_TH] + dth
                    orb[i, O_TURN] = orb[i, O_TURN] + abs(dth) / TWO_PI
                    th = orb[i, O_TH]
                    c = math.cos(th)
                    sn = math.sin(th)
                    tr_x[i, 0] = cx + R * c
                    tr_x[i, 1] = cy + R * sn
                    tr_x[i, 2] = cz
                    vt = wa * R
                    tr_v[i, 0] = dr * (-sn) * vt
                    tr_v[i, 1] = dr * c * vt
                    tr_v[i, 2] = 0.0
                    ac = wa * wa * R
                    tr_a[i, 0] = -c * ac
                    tr_a[i, 1] = -sn * ac
                    tr_a[i, 2] = 0.0
                    yb = orb[i, O_YAWB]
                    if yb == 0.0:
                        yaw_sp[i] = math.atan2(cy - p[i, 1], cx - p[i, 0])
                    elif yb == 1.0:
                        yaw_sp[i] = math.atan2(tr_v[i, 1], tr_v[i, 0])
                    else:
                        yaw_sp[i] = orb[i, O_YAW]
                    gl = orb[i, O_GOAL]
                    if gl > 0.0 and orb[i, O_TURN] >= gl:
                        ctrl_mode[i] = M_HOLD
                        ctrl_phase[i] = 0
                        mode_t[i] = t
                        mode_evt[i] = mode_evt[i] | E_ARRIVED
            elif mode0 == M_VELOCITY:
                vx = vel_cmd[i, 0]
                vy = vel_cmd[i, 1]
                vz = vel_cmd[i, 2]
                if vel_frame[i] == 1:  # body FLU -> NED（按当前航向）
                    qw = q[i, 0]
                    qx = q[i, 1]
                    qy = q[i, 2]
                    qz = q[i, 3]
                    psi = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
                    cp = math.cos(psi)
                    sp_ = math.sin(psi)
                    n_ = vx * cp + vy * sp_
                    e_ = vx * sp_ - vy * cp
                    vx = n_
                    vy = e_
                    vz = -vz
                vm = min(vel_vmax[i], LT[lim, L_VXY])
                nxy = math.sqrt(vx * vx + vy * vy)
                s = min(1.0, vm / max(nxy, 1e-6))
                vx = vx * s
                vy = vy * s
                vz = _clip(vz, -Z_VEL_MAX_UP, Z_VEL_MAX_DN)
                lk = int(axis_lock[i])
                for k in range(3):
                    bit = 1 << k
                    vk = vx if k == 0 else (vy if k == 1 else vz)
                    can = k < 2 or hold_alt[i]
                    if can and abs(vk) <= V_LOCK:
                        if (lk & bit) == 0:
                            axis_anchor[i, k] = p[i, k]
                            lk = lk | bit
                        drift = p[i, k] - axis_anchor[i, k]
                        vk = -V_PULL * drift if abs(drift) > V_DRIFT else 0.0
                    else:
                        lk = lk & (7 - bit)
                    if k == 0:
                        vx = vk
                    elif k == 1:
                        vy = vk
                    else:
                        vz = vk
                axis_lock[i] = lk
                spd = math.sqrt(vx * vx + vy * vy + vz * vz)
                if spd > 1e-6:
                    vl = _vmax(JERK, LT[lim, L_ACC], max(d_free[i] - FREE_MARGIN, 0.0), 0.0)
                    if spd > vl:
                        r = vl / spd
                        vx = vx * r
                        vy = vy * r
                        vz = vz * r
                tr_x[i, 0] = p[i, 0]
                tr_x[i, 1] = p[i, 1]
                tr_x[i, 2] = p[i, 2]
                tr_v[i, 0] = vx
                tr_v[i, 1] = vy
                tr_v[i, 2] = vz
                tr_a[i, 0] = 0.0
                tr_a[i, 1] = 0.0
                tr_a[i, 2] = 0.0
                yaw_sp[i] = yaw_sp[i] + vel_yawrate[i] * dt
            elif mode0 == M_OFFBOARD:
                for k in range(3):
                    tr_x[i, k] = pos_sp[i, k]
                    tr_v[i, k] = 0.0
                    tr_a[i, k] = 0.0
            elif mode0 == M_SPOOLUP:
                el = t - mode_t[i]
                frac = _clip(el / SPOOLUP_T, 0.0, 1.0)
                thr_sp[i, 0] = 0.0
                thr_sp[i, 1] = 0.0
                thr_sp[i, 2] = -THR_MIN * frac
                for k in range(3):
                    tr_x[i, k] = p[i, k]
                    tr_v[i, k] = 0.0
                    tr_a[i, k] = 0.0
            # M_TRAJ：tr_x/tr_v/tr_a/yaw_sp 由运动提供者的跟踪器 stage（order 027）写入
        # ------------------------------------------------ pos_ctrl（PX4 PositionControl，M08 §6.5.3）
        mode = ctrl_mode[i]
        hover = PT[pid, P_HOVER]
        yaw = yaw_sp[i]
        open_loop = fall or mode == M_SPOOLUP or (mode == M_TAKEOFF and ctrl_phase[i] == 0)
        if open_loop:
            if fall:
                thr_sp[i, 0] = 0.0
                thr_sp[i, 1] = 0.0
                thr_sp[i, 2] = 0.0
            for k in range(3):
                vel_int[i, k] = 0.0
                pos_ref[i, k] = p[i, k]
            h = yaw / 2.0
            q_sp[i, 0] = math.cos(h)
            q_sp[i, 1] = 0.0
            q_sp[i, 2] = 0.0
            q_sp[i, 3] = math.sin(h)
        else:
            is_vel = mode == M_VELOCITY
            ff = mode != M_OFFBOARD and not is_vel
            if is_vel:
                vs0 = tr_v[i, 0]
                vs1 = tr_v[i, 1]
                vs2 = tr_v[i, 2]
            else:
                f0 = tr_v[i, 0] if ff else 0.0
                f1 = tr_v[i, 1] if ff else 0.0
                f2 = tr_v[i, 2] if ff else 0.0
                vs0 = (tr_x[i, 0] - p[i, 0]) * XY_P + f0
                vs1 = (tr_x[i, 1] - p[i, 1]) * XY_P + f1
                vs2 = (tr_x[i, 2] - p[i, 2]) * Z_P + f2
            nxy = math.sqrt(vs0 * vs0 + vs1 * vs1)
            s = min(1.0, LT[lim, L_VXY] / max(nxy, 1e-6))
            vs0 = vs0 * s
            vs1 = vs1 * s
            vs2 = _clip(vs2, -(TKO_SPEED if mode == M_TAKEOFF else Z_VEL_MAX_UP), Z_VEL_MAX_DN)  # PX4 起飞 speed_up 约束
            vi0 = vel_int[i, 0]
            vi1 = vel_int[i, 1]
            vi2 = _clip(vel_int[i, 2], -G, G)
            ve0 = vs0 - v[i, 0]
            ve1 = vs1 - v[i, 1]
            ve2 = vs2 - v[i, 2]
            fa0 = tr_a[i, 0] if ff else 0.0
            fa1 = tr_a[i, 1] if ff else 0.0
            fa2 = tr_a[i, 2] if ff else 0.0
            acs0 = ve0 * KVP_XY + vi0 - a_meas[i, 0] * KVD_XY + fa0
            acs1 = ve1 * KVP_XY + vi1 - a_meas[i, 1] * KVD_XY + fa1
            acs2 = ve2 * KVP_Z + vi2 - a_meas[i, 2] * KVD_Z + fa2
            bx_ = -acs0
            by_ = -acs1
            bz_ = G
            nb = math.sqrt(bx_ * bx_ + by_ * by_ + bz_ * bz_)
            bx_ = bx_ / nb
            by_ = by_ / nb
            bz_ = bz_ / nb
            ang = math.acos(_clip(bz_, -1.0, 1.0))
            rn = math.sqrt(bx_ * bx_ + by_ * by_)
            if rn > 1e-6:
                rd = max(rn, 1e-9)
                rx = bx_ / rd
                ry = by_ / rd
            else:
                rx = 1.0
                ry = 0.0
            ang_l = min(ang, TILT)
            ca = math.cos(ang_l)
            sa = math.sin(ang_l)
            bx_ = ca * 0.0 + sa * rx
            by_ = ca * 0.0 + sa * ry
            bz_ = ca * 1.0 + sa * 0.0
            thr_max = THR_MAX * float(thr_cap[i])
            thr_z = acs2 * (hover / G) - hover
            coll = min(thr_z / bz_, -THR_MIN)
            t0 = bx_ * coll
            t1 = by_ * coll
            t2 = bz_ * coll
            txy = math.sqrt(t0 * t0 + t1 * t1)
            axy = min(txy, XY_MARG)
            t2 = max(t2, -math.sqrt(max(thr_max * thr_max - axy * axy, 0.0)))
            txm = math.sqrt(max(thr_max * thr_max - t2 * t2, 0.0))
            if txy > txm:
                sc = txm / max(txy, 1e-9)
                t0 = t0 * sc
                t1 = t1 * sc
            if (t2 >= -THR_MIN and ve2 >= 0.0) or (t2 <= -thr_max and ve2 <= 0.0):
                ve2 = 0.0
            gh = G / hover
            ap0 = t0 * gh
            ap1 = t1 * gh
            if acs0 * acs0 + acs1 * acs1 > ap0 * ap0 + ap1 * ap1:
                arw = 2.0 / KVP_XY
                ve0 = ve0 - arw * (acs0 - ap0)
                ve1 = ve1 - arw * (acs1 - ap1)
            vel_int[i, 0] = vi0 + ve0 * KVI_XY * dt
            vel_int[i, 1] = vi1 + ve1 * KVI_XY * dt
            vel_int[i, 2] = vi2 + ve2 * KVI_Z * dt
            thr_sp[i, 0] = t0
            thr_sp[i, 1] = t1
            thr_sp[i, 2] = t2
            # bodyzToAttitude(-thr/|thr|, yaw)
            tn = math.sqrt(t0 * t0 + t1 * t1 + t2 * t2)
            z0 = -t0 / tn
            z1 = -t1 / tn
            z2 = -t2 / tn
            yc0 = -math.sin(yaw)
            yc1 = math.cos(yaw)
            yc2 = 0.0
            x0 = yc1 * z2 - yc2 * z1
            x1 = yc2 * z0 - yc0 * z2
            x2 = yc0 * z1 - yc1 * z0
            nx = math.sqrt(x0 * x0 + x1 * x1 + x2 * x2)
            x0 = x0 / nx
            x1 = x1 / nx
            x2 = x2 / nx
            y0 = z1 * x2 - z2 * x1
            y1 = z2 * x0 - z0 * x2
            y2 = z0 * x1 - z1 * x0
            # quat_from_R，R 的列为 (x, y, z)
            tr_ = x0 + y1 + z2
            qw = math.sqrt(max(0.0, 1.0 + tr_)) / 2.0
            qx = math.sqrt(max(0.0, 1.0 + x0 - y1 - z2)) / 2.0
            qy = math.sqrt(max(0.0, 1.0 - x0 + y1 - z2)) / 2.0
            qz = math.sqrt(max(0.0, 1.0 - x0 - y1 + z2)) / 2.0
            qx = math.copysign(qx, y2 - z1)
            qy = math.copysign(qy, z0 - x2)
            qz = math.copysign(qz, x1 - y0)
            nq = math.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
            q_sp[i, 0] = qw / nq
            q_sp[i, 1] = qx / nq
            q_sp[i, 2] = qy / nq
            q_sp[i, 3] = qz / nq
            pos_ref[i, 0] = tr_x[i, 0]
            pos_ref[i, 1] = tr_x[i, 1]
            pos_ref[i, 2] = tr_x[i, 2]
        # ------------------------------------------------ att_ctrl（四元数 P + 理想速率环）
        w0 = q[i, 0]
        x0 = q[i, 1]
        y0 = q[i, 2]
        z0 = q[i, 3]
        if fall:
            wr0 = 0.0
            wr1 = 0.0
            wr2 = 0.0
        else:
            a0 = w0
            a1 = -x0
            a2 = -y0
            a3 = -z0
            b0 = q_sp[i, 0]
            b1 = q_sp[i, 1]
            b2 = q_sp[i, 2]
            b3 = q_sp[i, 3]
            e0 = a0 * b0 - a1 * b1 - a2 * b2 - a3 * b3
            e1 = a0 * b1 + a1 * b0 + a2 * b3 - a3 * b2
            e2 = a0 * b2 - a1 * b3 + a2 * b0 + a3 * b1
            e3 = a0 * b3 + a1 * b2 - a2 * b1 + a3 * b0
            sg = e0 + 1e-12
            sgn = 1.0 if sg > 0.0 else (-1.0 if sg < 0.0 else 0.0)
            e1 = e1 * sgn
            e2 = e2 * sgn
            e3 = e3 * sgn
            ymax = min(YAWRAUTO, LT[lim, L_YAWRATE])
            wr0 = _clip(2.0 * e1 * K_ROLL, -RMAX_ROLL, RMAX_ROLL)
            wr1 = _clip(2.0 * e2 * K_PITCH, -RMAX_PITCH, RMAX_PITCH)
            wr2 = _clip(2.0 * e3 * K_YAW, -ymax, ymax)
            if open_loop and in_contact[i]:
                wr0 = 0.0
                wr1 = 0.0
                wr2 = 0.0
            if faults and motor_ok[i] != 255:  # 缺桨（ext，FR-091）：不受控滚转
                wr0 = wr0 + w_fail
        rv0 = wr0 * dt
        rv1 = wr1 * dt
        rv2 = wr2 * dt
        an = math.sqrt(rv0 * rv0 + rv1 * rv1 + rv2 * rv2)
        if an > 1e-12:
            fs_ = math.sin(an / 2.0) / max(an, 1e-12)
        else:
            fs_ = 0.5
        d0 = math.cos(an / 2.0)
        d1 = rv0 * fs_
        d2 = rv1 * fs_
        d3 = rv2 * fs_
        n0 = w0 * d0 - x0 * d1 - y0 * d2 - z0 * d3
        n1 = w0 * d1 + x0 * d0 + y0 * d3 - z0 * d2
        n2 = w0 * d2 - x0 * d3 + y0 * d0 + z0 * d1
        n3 = w0 * d3 + x0 * d2 - y0 * d1 + z0 * d0
        nn = math.sqrt(n0 * n0 + n1 * n1 + n2 * n2 + n3 * n3)
        q[i, 0] = n0 / nn
        q[i, 1] = n1 / nn
        q[i, 2] = n2 / nn
        q[i, 3] = n3 / nn
        omega[i, 0] = wr0
        omega[i, 1] = wr1
        omega[i, 2] = wr2
        # ------------------------------------------------ motor（一阶滞后精确离散）
        if fall:
            thrust[i] = 0.0
        else:
            am = 1.0 - math.exp(-dt / PT[pid, P_TAU])
            cmd = math.sqrt(thr_sp[i, 0] * thr_sp[i, 0] + thr_sp[i, 1] * thr_sp[i, 1] + thr_sp[i, 2] * thr_sp[i, 2])
            thrust[i] = thrust[i] + (cmd - thrust[i]) * am
        # ------------------------------------------------ aero + integrate（风只经 v_r 进入；半隐式 Euler）
        w0 = q[i, 0]
        x0 = q[i, 1]
        y0 = q[i, 2]
        z0 = q[i, 3]
        bzx = 2.0 * (x0 * z0 + w0 * y0)
        bzy = 2.0 * (y0 * z0 - w0 * x0)
        bzz = 1.0 - 2.0 * (x0 * x0 + y0 * y0)
        rh = float(rho[i])
        th = thrust[i]
        sc_ = float(thrust_scale[i])
        if faults and motor_ok[i] != 255:
            nok = 0
            for b in range(int(PT[pid, P_NROT])):
                if (motor_ok[i] >> b) & 1:
                    nok += 1
            sc_ = sc_ * (nok / PT[pid, P_NROT])
        Tn = th * sc_ * PT[pid, P_TMAX] * (rh / RHO0) ** K_RHO
        fx = -bzx * Tn
        fy = -bzy * Tn
        fz = -bzz * Tn
        vrx = v[i, 0] - wind[i, 0]
        vry = v[i, 1] - wind[i, 1]
        vrz = v[i, 2] - wind[i, 2]
        if PT[pid, P_AERO] != 0.0:
            spd = math.sqrt(vrx * vrx + vry * vry + vrz * vrz)
            so = PT[pid, P_NROT] * PT[pid, P_WMAX] * math.sqrt(_clip(th, 0.0, 1.0))
            dotb = vrx * bzx + vry * bzy + vrz * bzz
            px_ = vrx - dotb * bzx
            py_ = vry - dotb * bzy
            pz_ = vrz - dotb * bzz
            kq = 0.5 * rh * PT[pid, P_CDA]
            kr = so * PT[pid, P_CRD]
            fx = fx + (-kq * spd * vrx - kr * px_)
            fy = fy + (-kq * spd * vry - kr * py_)
            fz = fz + (-kq * spd * vrz - kr * pz_)
        else:
            kd = PT[pid, P_KDV]
            fx = fx + -kd * vrx
            fy = fy + -kd * vry
            fz = fz + -kd * vrz
        m = PT[pid, P_MASS]
        ax_ = fx / m + 0.0
        ay_ = fy / m + 0.0
        az_ = fz / m + G
        p_prev[i, 0] = p[i, 0]
        p_prev[i, 1] = p[i, 1]
        p_prev[i, 2] = p[i, 2]
        v[i, 0] = v[i, 0] + ax_ * dt
        v[i, 1] = v[i, 1] + ay_ * dt
        v[i, 2] = v[i, 2] + az_ * dt
        p[i, 0] = p[i, 0] + v[i, 0] * dt
        p[i, 1] = p[i, 1] + v[i, 1] * dt
        p[i, 2] = p[i, 2] + v[i, 2] * dt
        a_meas[i, 0] = ax_
        a_meas[i, 1] = ay_
        a_meas[i, 2] = az_


def warmup() -> bool:
    """N = 2 的哑数组调用一次全部核（启动序列 FR-005 的 numba 预热；缓存命中时为毫秒级）。耗时由调用方（runtime）计时：
    仿真路径不读墙钟（ADR-045、ADR-049 不变量 5）。numba 不可用时返回 False。"""
    if not HAVE_NUMBA:
        return False
    from .kernels_contact import contact
    from .kernels_path import topp_lite_nb
    from .kernels_tap import enu_convert
    from .state import FleetState

    S = FleetState(4)
    S.active[:2] = True
    S.fidelity[:2] = 1
    S.ctrl_mode[:2] = M_HOLD
    S.landed[:2] = False
    PT = np.zeros((1, 10))
    PT[0] = (2.0, 34.0, 0.58, 0.03, 4.0, 1000.0, 1.0, 0.0, 0.02, 8e-5)
    LT = np.array([[12.0, 5.0, 3.0, 3.49]])
    idx = np.arange(2, dtype=np.int32)
    run_l1(S, PT, LT, None, idx, 0.008, 0.0, 3.0, 0.0, 0.0, np.zeros(S.capacity, np.uint8))
    g = np.zeros((2, 2), np.float32)
    aff = np.array([0.0, 0.0, 2.0])
    ev = np.zeros((S.capacity, 2), np.int32)
    prm = np.array([0.3, 1.0, 3.0, 1.0, 0.5, 0.0])
    # 运行期签名：世界的 DSM、DTM 网格是只读内存映射（M04），contact 收到只读 float32 网格；合成世界与测试为可写数组。
    # 两种都预热，避免首个 contact 调用在主循环内编译（D1 验收第 1 轮 4.1）
    g_ro = g.copy()
    g_ro.flags.writeable = False
    for gg, gt in ((g, g), (g_ro, g_ro), (g_ro, g), (g, g_ro)):
        contact(idx, 0.0, S.p, S.v, S.p_prev, S.q, S.omega, S.thrust, S.thr_sp, S.thr_cap, S.home, gg, aff, gt, aff, 0.0,
                S.ctrl_mode, S.ctrl_phase, S.mode_t, S.td_t, S.profile_id, PT, S.in_contact, S.landed, S.in_air,
                S.contact_t, S.crash_sub, S.ground_z, S.agl, S.mode_evt, np.zeros(S.capacity, np.uint8), prm, ev)
    enu_convert(idx, S.p, S.v, S.a_meas, S.q, S.q_sp, S.omega, S.pos_ref, S.home, S.enu._pos, S.enu._vel, S.enu._acc,
                S.enu._q, S.enu._q_sp, S.enu._omega, S.enu._pos_ref, S.enu._home)
    from .kernels_tap import enu_to_ned_rows

    enu_to_ned_rows(np.arange(2, dtype=np.int64), np.zeros((2, 3)), S.wind)
    w = np.zeros((3, 3))
    w[1, 0] = 10.0
    w[2, 0] = 10.0
    w[2, 1] = 10.0
    topp_lite_nb(w, 0.0, 5.0, 3.0, 3.0, 1.5, 2.0, np.zeros((4, SG_COLS)), np.zeros(3))
    z3 = np.zeros((2, 3))
    set_traj_nb(np.arange(2, dtype=np.int64), z3, z3, z3, np.zeros(2), True, S.tr_x, S.tr_v, S.tr_a, S.yaw_sp)
    set_traj_rows_nb(np.arange(2, dtype=np.int64), z3, z3, z3, np.zeros(2), S.tr_x, S.tr_v, S.tr_a, S.yaw_sp)
    # cmd_watch 预筛核（大机群路径）
    from . import kernels_watch

    kernels_watch.warmup()
    # 机间碰撞核（大机群路径）
    from .kernels_contact import uav_hits

    pp = np.zeros((3, 3))
    pp[1, 0] = 0.5
    uav_hits(pp, pp.copy(), np.full(3, 0.5), 6.0, np.zeros((8, 2), np.int64))
    # tap 融合核（Full64、Lite32 的结构化数组字段视图）
    from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32

    from .stages.tap import TapStage
    from .state import FALLBACK_BLOCKS

    St = FleetState(4, blocks=FALLBACK_BLOCKS)
    tap = TapStage(None, lease_owner=lambda: np.zeros(St.capacity, np.uint8), roster_version=lambda: 0)
    if tap.use_kernel and tap._kernel_ok(St):
        tap.fill(St, np.zeros(4, DRONE_STATE64), np.zeros(4, SWARM_LITE32), idx)
    return True


@njit(cache=True, fastmath=False)
def set_traj_nb(s, p, v, a, psi, has_psi, tr_x, tr_v, tr_a, yaw_sp):
    """TRAJ 设定点写入（ENU -> NED：(E, N, U) -> (N, E, −U)；偏航 `wrap_pi(π/2 − ψ)`），与 `actions.set_traj_enu` 的
    numpy 实现逐位相同（M10 跟踪器 125 Hz 调用，FX-SIM1）。"""
    for k in range(s.shape[0]):
        i = s[k]
        tr_x[i, 0] = p[k, 1]
        tr_x[i, 1] = p[k, 0]
        tr_x[i, 2] = -p[k, 2]
        tr_v[i, 0] = v[k, 1]
        tr_v[i, 1] = v[k, 0]
        tr_v[i, 2] = -v[k, 2]
        tr_a[i, 0] = a[k, 1]
        tr_a[i, 1] = a[k, 0]
        tr_a[i, 2] = -a[k, 2]
        if has_psi:
            w = ((math.pi / 2 - psi[k]) + math.pi) % (2.0 * math.pi) - math.pi
            if w == -math.pi:
                w = math.pi
            yaw_sp[i] = w


@njit(cache=True, fastmath=False)
def set_traj_rows_nb(s, P, V, A, PSI, tr_x, tr_v, tr_a, yaw_sp):
    """`set_traj_nb` 的按 slot 行读取版本（输入 P、V、A、PSI 的第 s[k] 行；同一运算，逐位相同）。"""
    for k in range(s.shape[0]):
        i = s[k]
        tr_x[i, 0] = P[i, 1]
        tr_x[i, 1] = P[i, 0]
        tr_x[i, 2] = -P[i, 2]
        tr_v[i, 0] = V[i, 1]
        tr_v[i, 1] = V[i, 0]
        tr_v[i, 2] = -V[i, 2]
        tr_a[i, 0] = A[i, 1]
        tr_a[i, 1] = A[i, 0]
        tr_a[i, 2] = -A[i, 2]
        w = ((math.pi / 2 - PSI[i]) + math.pi) % (2.0 * math.pi) - math.pi
        if w == -math.pi:
            w = math.pi
        yaw_sp[i] = w


_EMPTY_PATH: list = []


def run_l1(S, PT, LT, PB, idx, dt: float, t_s: float, flags: float, w_fail: float, faults: float, rtl_phase) -> None:
    """以 FleetState 的数组调用融合核（flags：bit0 time_stretch、bit1 stop_motion）。"""
    if PB is None:
        if not _EMPTY_PATH:
            from .path import EMPTY_PATH

            _EMPTY_PATH.append(EMPTY_PATH)
        pb = _EMPTY_PATH[0]
    else:
        pb = PB
    cfg = np.array([dt, t_s, float(int(flags) & 1), float((int(flags) >> 1) & 1), w_fail, faults, 0.0, 0.0])
    tick_l1(idx, cfg,
            S.p, S.v, S.a_meas, S.p_prev, S.q, S.omega, S.thrust, S.thr_cap, S.thr_sp, S.q_sp, S.yaw_sp, S.vel_int,
            S.ctrl_mode, S.ctrl_phase, S.mode_evt, S.mode_t, S.target, S.pos_sp, S.vel_cmd, S.tr_x, S.tr_v, S.tr_a,
            S.pos_ref, S.stopping, S.speed_cmd, S.z_rtl, S.v_rtl, S.land_xy, S.rtl_via, S.home, S.ground_z, S.agl,
            S.in_contact,
            S.landed, S.crash_sub, S.td_t,
            S.path_off, S.path_len, S.path_seg, S.path_tau, pb.yaw, pb.seg,
            S.orb, S.desc_v, S.vel_frame, S.vel_vmax, S.vel_yawrate, S.hold_alt, S.axis_lock, S.axis_anchor, S.d_free,
            S.wind, S.rho, S.thrust_scale, S.motor_ok,
            S.profile_id, S.limits_id, rtl_phase, PT, LT)
