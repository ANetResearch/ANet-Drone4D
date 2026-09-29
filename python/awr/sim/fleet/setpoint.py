"""参考生成器 refgen 的 numpy oracle（M08 §6.5.2；M08-FR-020 至 FR-031、FR-039、FR-086）。

与 numba 融合核 `kernels_l1.tick_l1` 的 refgen 段逐式对应（ADR-021 对拍）：每个 tick 开始时按初始运动模式把 `idx`
分组，各组向量化执行；运算顺序、分支与核内标量写法一致（范数显式写为平方和开方，`np.where` 两支都按核内公式计算）。

模式：GOTO（PositionSmoothing-lite + STOP_MOTION + time_stretch）、HOLD（刹停 → 锁定）、TAKEOFF（SPOOLUP 1 s → CLIMB）、
LAND（GOTO → DESCEND → TOUCHDOWN）、ELAND、DESCENT_FF、RTL（阶段取 M09 RTL 子模式）、PATH（TOPP-lite CSR 路径）、
ORBIT（入圈 → 绕圈，计圈）、VELOCITY（零速轴保持、方向自由距离限速）、OFFBOARD_POS、SPOOLUP；TRAJ 由运动提供者写入。
坐标 NED；`t_s` 为仿真秒。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import kernels_l1 as K

if TYPE_CHECKING:
    from .path import PathBuffer
    from .state import FleetState

__all__ = ["CLS_ACTIVE", "CLS_FALL", "CLS_NONE", "CLS_PARKED", "classify", "land_speed", "refgen", "vmax_from_dist"]

CLS_NONE, CLS_ACTIVE, CLS_FALL, CLS_PARKED = 0, 1, 2, 3


def vmax_from_dist(j: float, a, dist, vf: float = 0.0):
    """PX4 TrajMath::computeMaxSpeedFromDistance（向量化，与核 `_vmax` 同式）。"""
    a = np.asarray(a, dtype=np.float64)
    d = np.asarray(dist, dtype=np.float64)
    b = 4.0 * a * a / j
    c = -2.0 * a * d - vf * vf
    r = 0.5 * (-b + np.sqrt(b * b - 4.0 * c))
    return np.where(r > vf, r, vf)


def land_speed(agl) -> np.ndarray:
    """LAND 剖面（NED 向下为正）：AGL > 10 m 1.5 m/s；5–10 m 线性降到 0.7；1–5 m 0.7；≤ 1 m 0.3（MPC_LAND_*）。"""
    a = np.asarray(agl, dtype=np.float64)
    mid = K.LAND_SPEED + (K.LAND_FAST - K.LAND_SPEED) * (a - K.LAND_ALT2) / (K.LAND_ALT1 - K.LAND_ALT2)
    return np.where(a > K.LAND_ALT1, K.LAND_FAST,
                    np.where(a > K.LAND_ALT2, mid, np.where(a > K.LAND_ALT3, K.LAND_SPEED, K.LAND_CRWL)))


def _clip(x, lo, hi):
    return np.minimum(np.maximum(x, lo), hi)


def classify(S: FleetState, idx: np.ndarray) -> np.ndarray:
    """L1 类别（tick 开始时按初始运动模式）：PARKED（IDLE 或已触地的 KILLED）、FALL（空中 KILLED，含机间碰撞后下坠）、ACTIVE。"""
    m = S.ctrl_mode[idx]
    killed = m == K.M_KILLED
    parked = (m == K.M_IDLE) | (killed & S.landed[idx])
    return np.where(parked, CLS_PARKED, np.where(killed, CLS_FALL, CLS_ACTIVE)).astype(np.uint8)


def park(S: FleetState, i: np.ndarray) -> None:
    if i.size == 0:
        return
    S.tr_x[i] = S.p[i]
    S.tr_v[i] = 0.0
    S.tr_a[i] = 0.0
    S.thr_sp[i] = 0.0
    S.pos_ref[i] = S.p[i]
    S.vel_int[i] = 0.0
    S.thrust[i] = 0.0


# ---------------------------------------------------------------- 公共片段
def _stretch(S: FleetState, i: np.ndarray, ts_on: bool) -> tuple[np.ndarray, np.ndarray]:
    n = i.size
    if not ts_on:
        return np.ones(n), np.ones(n)
    p, tx, tv = S.p, S.tr_x, S.tr_v
    ex = tx[i, 0] - p[i, 0]
    ey = tx[i, 1] - p[i, 1]
    ez = tx[i, 2] - p[i, 2]
    tsxy = np.where(ex * tv[i, 0] + ey * tv[i, 1] >= 0.0, 1.0 - _clip(np.sqrt(ex * ex + ey * ey) / K.XY_ERR, 0.0, 1.0), 1.0)
    tsz = np.where(ez * tv[i, 2] >= 0.0, 1.0 - _clip(np.abs(ez) / K.Z_ERR, 0.0, 1.0), 1.0)
    return tsxy, tsz


def _advance(S: FleetState, i: np.ndarray, ax, ay, az, dt: float, ts_on: bool) -> None:
    tsxy, tsz = _stretch(S, i, ts_on)
    d0 = dt * tsxy
    d2 = dt * tsz
    ta, tv, tx = S.tr_a, S.tr_v, S.tr_x
    jl = K.JERK * d0
    for k, ak in ((0, ax), (1, ay)):
        a = ta[i, k] + _clip(ak - ta[i, k], -jl, jl)
        ta[i, k] = a
        vv = tv[i, k] + a * d0
        tv[i, k] = vv
        tx[i, k] = tx[i, k] + vv * d0
    jz = K.JERK * d2
    a = ta[i, 2] + _clip(az - ta[i, 2], -jz, jz)
    ta[i, 2] = a
    vv = tv[i, 2] + a * d2
    tv[i, 2] = vv
    tx[i, 2] = tx[i, 2] + vv * d2


def _speed3(a: np.ndarray, i: np.ndarray) -> np.ndarray:
    return np.sqrt(a[i, 0] * a[i, 0] + a[i, 1] * a[i, 1] + a[i, 2] * a[i, 2])


def _goto(S: FleetState, LT: np.ndarray, i: np.ndarray, tx, ty, tz, speed, dt: float, ts_on: bool) -> np.ndarray:
    """PositionSmoothing-lite + STOP_MOTION（核 `_goto` 同式）；返回参考到达掩码。"""
    lim = S.limits_id[i]
    acc = LT[lim, K.L_ACC]
    vxy_max = LT[lim, K.L_VXY]
    trx, trv = S.tr_x, S.tr_v
    dx = tx - trx[i, 0]
    dy = ty - trx[i, 1]
    dz = tz - trx[i, 2]
    dxy = np.sqrt(dx * dx + dy * dy)
    cr = np.where(np.isnan(speed), LT[lim, K.L_CRUISE], speed)
    cr = np.minimum(cr, vxy_max)
    vxy = np.minimum(cr, vmax_from_dist(K.JERK, acc, dxy, 0.0))
    vzc = np.where(dz < 0.0, K.Z_V_AUTO_UP, K.Z_V_AUTO_DN)
    vz = np.minimum(vzc, vmax_from_dist(K.JERK, K.ACC_UP, np.abs(dz), 0.0))
    big = dxy > 1e-3
    dd = np.maximum(dxy, 1e-6)
    ux = np.where(big, dx / dd, 0.0)
    uy = np.where(big, dy / dd, 0.0)
    sz = np.where(dz > 0.0, 1.0, np.where(dz < 0.0, -1.0, 0.0))
    st = S.stopping[i]
    vdx = np.where(st, 0.0, ux * vxy)
    vdy = np.where(st, 0.0, uy * vxy)
    vdz = np.where(st, 0.0, sz * vz)
    ax = 2.0 * (vdx - trv[i, 0])
    ay = 2.0 * (vdy - trv[i, 1])
    az = 2.0 * (vdz - trv[i, 2])
    nxy = np.sqrt(ax * ax + ay * ay)
    s = np.minimum(1.0, acc / np.maximum(nxy, 1e-9))
    ax = ax * s
    ay = ay * s
    az = _clip(az, -K.ACC_UP, K.ACC_DN)
    _advance(S, i, ax, ay, az, dt, ts_on)
    sp = _speed3(trv, i)
    S.stopping[i] = st & (sp >= 0.05)
    ex = tx - trx[i, 0]
    ey = ty - trx[i, 1]
    ez = tz - trx[i, 2]
    return (np.sqrt(ex * ex + ey * ey + ez * ez) < 0.05) & (sp < 0.05)


def _brake(S: FleetState, LT: np.ndarray, i: np.ndarray, dt: float, ts_on: bool) -> np.ndarray:
    acc = LT[S.limits_id[i], K.L_ACC]
    trv = S.tr_v
    ax = 2.0 * (0.0 - trv[i, 0])
    ay = 2.0 * (0.0 - trv[i, 1])
    az = 2.0 * (0.0 - trv[i, 2])
    nxy = np.sqrt(ax * ax + ay * ay)
    s = np.minimum(1.0, acc / np.maximum(nxy, 1e-9))
    ax = ax * s
    ay = ay * s
    az = _clip(az, -K.ACC_UP, K.ACC_DN)
    _advance(S, i, ax, ay, az, dt, ts_on)
    return _speed3(trv, i) < 0.05


def _to_hold(S: FleetState, j: np.ndarray, t: float, evt: int) -> None:
    if j.size == 0:
        return
    S.ctrl_mode[j] = K.M_HOLD
    S.ctrl_phase[j] = 1
    S.target[j] = S.tr_x[j]
    S.tr_v[j] = 0.0
    S.tr_a[j] = 0.0
    S.mode_t[j] = t
    S.mode_evt[j] |= evt


def _descend(S: FleetState, i: np.ndarray, vz, dt: float) -> None:
    S.tr_x[i, 0] = S.land_xy[i, 0]
    S.tr_x[i, 1] = S.land_xy[i, 1]
    S.tr_x[i, 2] = np.minimum(S.tr_x[i, 2] + vz * dt, S.p[i, 2] + 1.0)
    S.tr_v[i, 0] = 0.0
    S.tr_v[i, 1] = 0.0
    S.tr_v[i, 2] = vz
    S.tr_a[i] = 0.0


def _touchdown(S: FleetState, i: np.ndarray, t: float) -> None:
    if i.size == 0:
        return
    td = S.td_t[i]
    td = np.where(np.isnan(td), t, td)
    S.td_t[i] = td
    el = _clip((t - td) / K.TD_RAMP, 0.0, 1.0)
    S.thr_cap[i] = (1.0 - el * (1.0 - K.THR_MIN / K.THR_MAX)).astype(np.float32)
    S.tr_x[i, 0] = S.land_xy[i, 0]
    S.tr_x[i, 1] = S.land_xy[i, 1]
    S.tr_x[i, 2] = S.p[i, 2] + 0.5
    S.tr_v[i, 0] = 0.0
    S.tr_v[i, 1] = 0.0
    S.tr_v[i, 2] = K.LAND_CRWL
    S.tr_a[i] = 0.0


def _phase(S: FleetState, j: np.ndarray, ph: int, t: float) -> None:
    if j.size:
        S.ctrl_phase[j] = ph
        S.mode_t[j] = t
        S.mode_evt[j] |= K.E_PHASE


# ---------------------------------------------------------------- 各模式
def _ref_takeoff(S: FleetState, i: np.ndarray, dt: float, t: float) -> None:
    ph = S.ctrl_phase[i]
    i0, i1 = i[ph == 0], i[ph != 0]
    if i0.size:
        el = t - S.mode_t[i0]
        frac = _clip(el / K.SPOOLUP_T, 0.0, 1.0)
        S.thr_sp[i0, 0] = 0.0
        S.thr_sp[i0, 1] = 0.0
        S.thr_sp[i0, 2] = -K.THR_MIN * frac
        S.tr_x[i0] = S.p[i0]
        S.tr_v[i0] = 0.0
        S.tr_a[i0] = 0.0
        _phase(S, i0[el >= K.SPOOLUP_T], 1, t)
    if i1.size:
        tgt = S.target[i1, 2]
        dz = tgt - S.tr_x[i1, 2]
        el = t - S.mode_t[i1]
        vr = K.TKO_SPEED * _clip(el / K.TKO_RAMP, 0.0, 1.0)
        vb = vmax_from_dist(K.JERK, K.ACC_UP, np.abs(dz), 0.0)
        v_up = np.minimum(vr, vb)
        vz = np.where(dz < 0.0, -v_up, 0.0)
        nz = S.tr_x[i1, 2] + vz * dt
        nz = np.where(dz < 0.0, np.maximum(nz, tgt), tgt)
        S.tr_x[i1, 0] = S.target[i1, 0]
        S.tr_x[i1, 1] = S.target[i1, 1]
        S.tr_x[i1, 2] = nz
        S.tr_v[i1, 0] = 0.0
        S.tr_v[i1, 1] = 0.0
        S.tr_v[i1, 2] = np.where(nz > tgt, vz, 0.0)
        S.tr_a[i1, 0] = 0.0
        S.tr_a[i1, 1] = 0.0
        S.tr_a[i1, 2] = np.where((dz < 0.0) & (nz > tgt) & (el < K.TKO_RAMP) & (vr <= vb), -K.TKO_SPEED / K.TKO_RAMP, 0.0)
        alt = np.maximum(S.ground_z[i1] - tgt, 0.0)
        arr = (np.abs(S.p[i1, 2] - tgt) < np.maximum(0.3, 0.05 * alt)) & (np.abs(S.v[i1, 2]) < 0.3)
        j = i1[arr]
        if j.size:
            _to_hold(S, j, t, K.E_ARRIVED)
            S.target[j, 2] = tgt[arr]
            S.tr_x[j, 2] = tgt[arr]


def _ref_land(S: FleetState, LT: np.ndarray, i: np.ndarray, dt: float, t: float, ts_on: bool) -> None:
    ph = S.ctrl_phase[i]
    i0, i1, i2 = i[ph == 0], i[ph == 1], i[ph >= 2]
    if i0.size:
        arr = _goto(S, LT, i0, S.land_xy[i0, 0], S.land_xy[i0, 1], S.target[i0, 2], S.speed_cmd[i0], dt, ts_on)
        _phase(S, i0[arr], 1, t)
    if i1.size:
        _descend(S, i1, land_speed(S.agl[i1]), dt)
        _phase(S, i1[S.in_contact[i1]], 2, t)
    if i2.size:
        _touchdown(S, i2, t)


def _ref_eland(S: FleetState, i: np.ndarray, dt: float, t: float, mode0: np.ndarray) -> None:
    c = S.in_contact[i]
    _touchdown(S, i[c], t)
    el = i[~c & (mode0 == K.M_ELAND)]
    if el.size:
        _descend(S, el, S.desc_v[el], dt)
    ds = i[~c & (mode0 == K.M_DESCENT)]
    if ds.size:
        S.tr_x[ds, 0] = S.land_xy[ds, 0]
        S.tr_x[ds, 1] = S.land_xy[ds, 1]
        S.tr_x[ds, 2] = S.p[ds, 2]
        S.tr_v[ds, 0] = 0.0
        S.tr_v[ds, 1] = 0.0
        S.tr_v[ds, 2] = S.desc_v[ds]
        S.tr_a[ds] = 0.0


def _ref_rtl(S: FleetState, LT: np.ndarray, i: np.ndarray, dt: float, t: float, ts_on: bool, rtl_phase) -> None:
    rp = rtl_phase[i].astype(np.uint8)
    ch = rp != S.ctrl_phase[i]
    if ch.any():
        j = i[ch]
        S.ctrl_phase[j] = rp[ch]
        S.mode_evt[j] |= K.E_PHASE
    nav = rp < K.R_FINAL
    n = i[nav]
    if n.size:
        r = rp[nav]
        home = S.home
        gx = np.where(r == K.R_CLIMB, S.land_xy[n, 0], home[n, 0])
        gy = np.where(r == K.R_CLIMB, S.land_xy[n, 1], home[n, 1])
        gz = np.where(r == K.R_DESCEND, home[n, 2] - K.DESC_ALT, -S.z_rtl[n])
        S.target[n, 0] = gx
        S.target[n, 1] = gy
        S.target[n, 2] = gz
        _goto(S, LT, n, gx, gy, gz, S.v_rtl[n], dt, ts_on)
    f = i[~nav]
    if f.size:
        S.land_xy[f] = S.home[f, :2]
        _descend(S, f, land_speed(S.agl[f]), dt)
        c = f[S.in_contact[f]]
        if c.size:
            S.ctrl_mode[c] = K.M_LAND
            _phase(S, c, 2, t)


def _seg_eval(PB: PathBuffer, k: np.ndarray, tau: np.ndarray):
    G = PB.seg
    v0 = G[k, K.SG_V0]
    v1 = G[k, K.SG_V1]
    a = G[k, K.SG_A]
    ta = G[k, K.SG_TA]
    tc = G[k, K.SG_TC]
    vc = G[k, K.SG_VC]
    T = G[k, K.SG_T]
    L = G[k, K.SG_LEN]
    done = tau >= T
    tau_c = np.maximum(tau, 0.0)
    acc = tau_c < ta
    s_a = v0 * tau_c + 0.5 * a * tau_c * tau_c
    sd_a = v0 + a * tau_c
    da = (vc * vc - v0 * v0) / (2.0 * a)
    cru = tau_c < ta + tc
    s_c = da + vc * (tau_c - ta)
    td = tau_c - ta - tc
    s_d = da + vc * tc + vc * td - 0.5 * a * td * td
    s_d = np.minimum(s_d, L)
    sd_d = vc - a * td
    s = np.where(done, L, np.where(acc, s_a, np.where(cru, s_c, s_d)))
    sd = np.where(done, v1, np.where(acc, sd_a, np.where(cru, vc, sd_d)))
    sdd = np.where(done, 0.0, np.where(acc, a, np.where(cru, 0.0, -a)))
    return s, sd, sdd


def _ref_path(S: FleetState, PB: PathBuffer, i: np.ndarray, dt: float, t: float, ts_on: bool) -> None:
    """PATH：段表（直线 + 航点圆弧过渡，`kernels_path.topp_lite_nb`）按轨迹时间取 p/v/a（与 `tick_l1` 的 PATH 分支同式）。"""
    G = PB.seg
    last = S.path_off[i] + S.path_len[i] - 1
    tsxy, tsz = _stretch(S, i, ts_on)
    tau = S.path_tau[i] + dt * np.minimum(tsxy, tsz)
    S.path_tau[i] = tau
    k = S.path_seg[i].astype(np.int64)
    while True:
        adv = (k < last) & (tau >= G[k, K.SG_T0] + G[k, K.SG_T])
        if not adv.any():
            break
        k = k + adv
    S.path_seg[i] = k
    s, sd, sdd = _seg_eval(PB, k, tau - G[k, K.SG_T0])
    u0 = [G[k, K.SG_U + c] for c in range(3)]
    p0 = [G[k, K.SG_P0 + c] for c in range(3)]
    arc = G[k, K.SG_KIND] != 0.0
    u = list(u0)
    for c in range(3):
        S.tr_x[i, c] = p0[c] + u0[c] * s
        S.tr_v[i, c] = u0[c] * sd
        S.tr_a[i, c] = u0[c] * sdd
    if arc.any():
        ka = k[arc]
        ia = i[arc]
        sa, sda, sdda = s[arc], sd[arc], sdd[arc]
        e = [G[ka, K.SG_E + c] for c in range(3)]
        ua = [u0[c][arc] for c in range(3)]
        r = G[ka, K.SG_R]
        ph = sa / r
        cs = np.cos(ph)
        sn = np.sin(ph)
        ac = sda * sda / r
        ta = [cs * ua[c] + sn * e[c] for c in range(3)]
        for c in range(3):
            S.tr_x[ia, c] = p0[c][arc] + r * (sn * ua[c] + (1.0 - cs) * e[c])
            S.tr_v[ia, c] = ta[c] * sda
            S.tr_a[ia, c] = ta[c] * sdda + ac * (cs * e[c] - sn * ua[c])
            u[c] = u[c].copy()
            u[c][arc] = ta[c]
    yw = PB.yaw[S.path_off[i] + G[k, K.SG_WP].astype(np.int64)]
    has = ~np.isnan(yw)
    tang = ~has & (u[0] * u[0] + u[1] * u[1] > 0.01)
    S.yaw_sp[i[has]] = yw[has]
    if tang.any():
        S.yaw_sp[i[tang]] = np.arctan2(u[1][tang], u[0][tang])
    arr = (k >= last) & (tau >= G[k, K.SG_T0] + G[k, K.SG_T])
    _to_hold(S, i[arr], t, K.E_ARRIVED)


def _ref_orbit(S: FleetState, LT: np.ndarray, i: np.ndarray, dt: float, t: float, ts_on: bool) -> None:
    OB = S.orb
    ph = S.ctrl_phase[i]
    i0, i1 = i[ph == 0], i[ph != 0]
    if i0.size:
        cx, cy, cz, R = OB[i0, K.O_CX], OB[i0, K.O_CY], OB[i0, K.O_CZ], OB[i0, K.O_R]
        ex = S.tr_x[i0, 0] - cx
        ey = S.tr_x[i0, 1] - cy
        de = np.sqrt(ex * ex + ey * ey)
        th0 = np.where(de > 1e-6, np.arctan2(ey, ex), 0.0)
        gx = cx + R * np.cos(th0)
        gy = cy + R * np.sin(th0)
        arr = _goto(S, LT, i0, gx, gy, cz, S.speed_cmd[i0], dt, ts_on)
        j = i0[arr]
        if j.size:
            OB[j, K.O_TH] = th0[arr]
            OB[j, K.O_W] = 0.0
            _phase(S, j, 1, t)
    if i1.size:
        cx, cy, cz, R = OB[i1, K.O_CX], OB[i1, K.O_CY], OB[i1, K.O_CZ], OB[i1, K.O_R]
        acc = LT[S.limits_id[i1], K.L_ACC]
        w_max = np.minimum(OB[i1, K.O_V], np.sqrt(acc * R)) / R
        dr = OB[i1, K.O_DIR]
        wa = np.minimum(np.abs(OB[i1, K.O_W]) + acc / R * dt, w_max)
        OB[i1, K.O_W] = dr * wa
        tsxy, tsz = _stretch(S, i1, ts_on)
        dth = OB[i1, K.O_W] * dt * np.minimum(tsxy, tsz)
        th = OB[i1, K.O_TH] + dth
        OB[i1, K.O_TH] = th
        OB[i1, K.O_TURN] = OB[i1, K.O_TURN] + np.abs(dth) / K.TWO_PI
        c = np.cos(th)
        sn = np.sin(th)
        S.tr_x[i1, 0] = cx + R * c
        S.tr_x[i1, 1] = cy + R * sn
        S.tr_x[i1, 2] = cz
        vt = wa * R
        S.tr_v[i1, 0] = dr * (-sn) * vt
        S.tr_v[i1, 1] = dr * c * vt
        S.tr_v[i1, 2] = 0.0
        ac = wa * wa * R
        S.tr_a[i1, 0] = -c * ac
        S.tr_a[i1, 1] = -sn * ac
        S.tr_a[i1, 2] = 0.0
        yb = OB[i1, K.O_YAWB]
        y_c = np.arctan2(cy - S.p[i1, 1], cx - S.p[i1, 0])
        y_t = np.arctan2(S.tr_v[i1, 1], S.tr_v[i1, 0])
        S.yaw_sp[i1] = np.where(yb == 0.0, y_c, np.where(yb == 1.0, y_t, OB[i1, K.O_YAW]))
        gl = OB[i1, K.O_GOAL]
        done = (gl > 0.0) & (OB[i1, K.O_TURN] >= gl)
        j = i1[done]
        if j.size:
            S.ctrl_mode[j] = K.M_HOLD
            S.ctrl_phase[j] = 0
            S.mode_t[j] = t
            S.mode_evt[j] |= K.E_ARRIVED


def _ref_velocity(S: FleetState, LT: np.ndarray, i: np.ndarray, dt: float) -> None:
    lim = S.limits_id[i]
    vx = S.vel_cmd[i, 0].copy()
    vy = S.vel_cmd[i, 1].copy()
    vz = S.vel_cmd[i, 2].copy()
    body = S.vel_frame[i] == 1
    if body.any():
        q = S.q
        qw, qx, qy, qz = q[i, 0], q[i, 1], q[i, 2], q[i, 3]
        psi = np.arctan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
        cp = np.cos(psi)
        sp = np.sin(psi)
        n_ = vx * cp + vy * sp
        e_ = vx * sp - vy * cp
        vx = np.where(body, n_, vx)
        vy = np.where(body, e_, vy)
        vz = np.where(body, -vz, vz)
    vm = np.minimum(S.vel_vmax[i], LT[lim, K.L_VXY])
    nxy = np.sqrt(vx * vx + vy * vy)
    s = np.minimum(1.0, vm / np.maximum(nxy, 1e-6))
    vx = vx * s
    vy = vy * s
    vz = _clip(vz, -K.Z_VEL_MAX_UP, K.Z_VEL_MAX_DN)
    lk = S.axis_lock[i].astype(np.int64)
    comps = [vx, vy, vz]
    for k in range(3):
        bit = 1 << k
        vk = comps[k]
        can = np.ones(i.size, bool) if k < 2 else S.hold_alt[i]
        lock = can & (np.abs(vk) <= K.V_LOCK)
        new = lock & ((lk & bit) == 0)
        if new.any():
            S.axis_anchor[i[new], k] = S.p[i[new], k]
        lk = np.where(lock, lk | bit, lk & (7 - bit))
        drift = S.p[i, k] - S.axis_anchor[i, k]
        comps[k] = np.where(lock, np.where(np.abs(drift) > K.V_DRIFT, -K.V_PULL * drift, 0.0), vk)
    vx, vy, vz = comps
    S.axis_lock[i] = lk.astype(np.uint8)
    spd = np.sqrt(vx * vx + vy * vy + vz * vz)
    vl = vmax_from_dist(K.JERK, LT[lim, K.L_ACC], np.maximum(S.d_free[i] - K.FREE_MARGIN, 0.0), 0.0)
    cut = (spd > 1e-6) & (spd > vl)
    r = np.where(cut, vl / np.where(cut, spd, 1.0), 1.0)
    vx = np.where(cut, vx * r, vx)
    vy = np.where(cut, vy * r, vy)
    vz = np.where(cut, vz * r, vz)
    S.tr_x[i] = S.p[i]
    S.tr_v[i, 0] = vx
    S.tr_v[i, 1] = vy
    S.tr_v[i, 2] = vz
    S.tr_a[i] = 0.0
    S.yaw_sp[i] = S.yaw_sp[i] + S.vel_yawrate[i] * dt


def refgen(S: FleetState, LT: np.ndarray, PB: PathBuffer | None, idx: np.ndarray, dt: float, t_s: float, *,
           time_stretch: bool = True, rtl_phase: np.ndarray | None = None) -> None:
    """按初始运动模式生成参考（只处理 ACTIVE 类 slot；PARKED 与 FALL 由调用方另行处理）。"""
    if idx.size == 0:
        return
    ts = bool(time_stretch)
    mode0 = S.ctrl_mode[idx].copy()
    ph0 = S.ctrl_phase[idx].copy()

    def grp(m: int) -> np.ndarray:
        return idx[mode0 == m]

    g = grp(K.M_GOTO)
    if g.size:
        arr = _goto(S, LT, g, S.target[g, 0], S.target[g, 1], S.target[g, 2], S.speed_cmd[g], dt, ts)
        _to_hold(S, g[arr], t_s, K.E_ARRIVED)
    h = grp(K.M_HOLD)
    if h.size:
        hp = ph0[mode0 == K.M_HOLD]
        h0, h1 = h[hp == 0], h[hp != 0]
        if h0.size:
            stop = _brake(S, LT, h0, dt, ts)
            j = h0[stop]
            if j.size:
                S.target[j] = S.tr_x[j]
                S.ctrl_phase[j] = 1
                S.mode_evt[j] |= K.E_PHASE
        if h1.size:
            S.tr_x[h1] = S.target[h1]
            S.tr_v[h1] = 0.0
            S.tr_a[h1] = 0.0
    k = grp(K.M_TAKEOFF)
    if k.size:
        _ref_takeoff(S, k, dt, t_s)
    ld = grp(K.M_LAND)
    if ld.size:
        _ref_land(S, LT, ld, dt, t_s, ts)
    el = idx[(mode0 == K.M_ELAND) | (mode0 == K.M_DESCENT)]
    if el.size:
        _ref_eland(S, el, dt, t_s, mode0[(mode0 == K.M_ELAND) | (mode0 == K.M_DESCENT)])
    r = grp(K.M_RTL)
    if r.size:
        rp = rtl_phase if rtl_phase is not None else np.zeros(S.capacity, np.uint8)
        _ref_rtl(S, LT, r, dt, t_s, ts, rp)
    pa = grp(K.M_PATH)
    if pa.size and PB is not None:
        _ref_path(S, PB, pa, dt, t_s, ts)
    o = grp(K.M_ORBIT)
    if o.size:
        _ref_orbit(S, LT, o, dt, t_s, ts)
    ve = grp(K.M_VELOCITY)
    if ve.size:
        _ref_velocity(S, LT, ve, dt)
    ob = grp(K.M_OFFBOARD)
    if ob.size:
        S.tr_x[ob] = S.pos_sp[ob]
        S.tr_v[ob] = 0.0
        S.tr_a[ob] = 0.0
    sp = grp(K.M_SPOOLUP)
    if sp.size:
        el_ = t_s - S.mode_t[sp]
        frac = _clip(el_ / K.SPOOLUP_T, 0.0, 1.0)
        S.thr_sp[sp, 0] = 0.0
        S.thr_sp[sp, 1] = 0.0
        S.thr_sp[sp, 2] = -K.THR_MIN * frac
        S.tr_x[sp] = S.p[sp]
        S.tr_v[sp] = 0.0
        S.tr_a[sp] = 0.0


def p_stop(S: FleetState, LT: np.ndarray, slots: np.ndarray) -> np.ndarray:
    """刹停点 `p + unit(v)·(v²/(2a) + v·a/(2j))`（NED，供 M09 围栏折线 [p, p_stop, goal]，FR-022）。"""
    s = np.asarray(slots, np.int64)
    v = S.v[s]
    vh = np.sqrt(v[:, 0] * v[:, 0] + v[:, 1] * v[:, 1])
    a = LT[S.limits_id[s], K.L_ACC]
    d = vh * vh / (2.0 * a) + vh * a / (2.0 * K.JERK)
    nv = np.sqrt(v[:, 0] * v[:, 0] + v[:, 1] * v[:, 1] + v[:, 2] * v[:, 2])
    u = np.where(nv[:, None] > 1e-6, v / np.maximum(nv, 1e-9)[:, None], 0.0)
    return S.p[s] + u * d[:, None]


def p_stop_enu(S: FleetState, LT: np.ndarray, slots: np.ndarray) -> np.ndarray:
    """`p_stop` 的 ENU 形式（CommandEngine ⑧ 折线用）。"""
    ps = p_stop(S, LT, slots)
    return np.stack([ps[:, 1], ps[:, 0], -ps[:, 2]], axis=1)


_ = math  # math 仅供类型检查器识别常量来源
