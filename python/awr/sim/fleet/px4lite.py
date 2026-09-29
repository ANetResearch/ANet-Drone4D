"""PX4-lite L1 控制级联的 numpy oracle：pos_ctrl、att_ctrl、motor（M08 §6.5.3、§6.5.4；M08-FR-016 至 FR-019、FR-035、FR-039）。

由 `.cache/research/g08/fleetsim_g08.py`（`st_pos_ctrl`、`st_att`、`st_motor`）迁移，修订：参数按 ProfileTable 与限速配置
矩阵逐机取值；推力上限乘 `thr_cap`（TOUCHDOWN 斜坡）；只用 PX4 原生抗积分饱和（FR-018）；与融合核 `kernels_l1.tick_l1`
逐式对应（范数显式写为平方和开方）。开环（SPOOLUP、TAKEOFF.0）与 FALL（空中 KILLED）在 refgen 之后判定。
内部坐标 NED/FRD，四元数 (w, x, y, z)。另提供四元数助手（tap 以外的 fleet 代码使用）。
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from . import kernels_l1 as K

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["att_ctrl_l1", "bz_from_quat", "motor", "open_loop_mask", "pos_ctrl", "qmul", "quat_from_R", "quat_from_yaw",
           "tilt_deg", "yaw_from_quat"]


def _clip(x, lo, hi):
    return np.minimum(np.maximum(x, lo), hi)


# ---------------------------------------------------------------- 四元数助手 (w, x, y, z)
def quat_from_R(R: np.ndarray) -> np.ndarray:
    tr = R[:, 0, 0] + R[:, 1, 1] + R[:, 2, 2]
    w = np.sqrt(np.maximum(0.0, 1.0 + tr)) / 2.0
    x = np.sqrt(np.maximum(0.0, 1.0 + R[:, 0, 0] - R[:, 1, 1] - R[:, 2, 2])) / 2.0
    y = np.sqrt(np.maximum(0.0, 1.0 - R[:, 0, 0] + R[:, 1, 1] - R[:, 2, 2])) / 2.0
    z = np.sqrt(np.maximum(0.0, 1.0 - R[:, 0, 0] - R[:, 1, 1] + R[:, 2, 2])) / 2.0
    x = np.copysign(x, R[:, 2, 1] - R[:, 1, 2])
    y = np.copysign(y, R[:, 0, 2] - R[:, 2, 0])
    z = np.copysign(z, R[:, 1, 0] - R[:, 0, 1])
    n = np.sqrt(w * w + x * x + y * y + z * z)
    return np.stack([w / n, x / n, y / n, z / n], 1)


def bz_from_quat(q: np.ndarray) -> np.ndarray:
    """机体 z 轴（向下）在 NED 中的方向（R 的第三列）。"""
    w, x, y, z = q.T
    return np.stack([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)], 1)


def qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    w1, x1, y1, z1 = a.T
    w2, x2, y2, z2 = b.T
    return np.stack([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
                     w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2], axis=1)


def quat_from_yaw(yaw_ned) -> np.ndarray:
    h = np.asarray(yaw_ned, dtype=np.float64) / 2.0
    return np.stack([np.cos(h), np.zeros_like(h), np.zeros_like(h), np.sin(h)], axis=-1)


def yaw_from_quat(q: np.ndarray) -> np.ndarray:
    w, x, y, z = q[..., 0], q[..., 1], q[..., 2], q[..., 3]
    return np.arctan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


def tilt_deg(q: np.ndarray) -> np.ndarray:
    """机体倾角（机体 z 轴与竖直的夹角），°。"""
    x, y = q[..., 1], q[..., 2]
    return np.degrees(np.arccos(np.clip(1.0 - 2.0 * (x * x + y * y), -1.0, 1.0)))


def open_loop_mask(S: FleetState, i: np.ndarray, fall: np.ndarray) -> np.ndarray:
    """refgen 之后的开环判定：FALL、SPOOLUP、TAKEOFF.0（核内同式）。"""
    m = S.ctrl_mode[i]
    return fall | (m == K.M_SPOOLUP) | ((m == K.M_TAKEOFF) & (S.ctrl_phase[i] == 0))


# ---------------------------------------------------------------- 位置控制（PX4 PositionControl）
def pos_ctrl(S: FleetState, PT: np.ndarray, LT: np.ndarray, idx: np.ndarray, fall: np.ndarray, dt: float) -> None:
    """位置 P → 速度 PID（原生 ARW）→ 加速度 → 推力矢量（倾角限幅、垂直优先饱和）→ bodyzToAttitude。"""
    if idx.size == 0:
        return
    ol = open_loop_mask(S, idx, fall)
    o = idx[ol]
    if o.size:
        f = o[fall[ol]]
        S.thr_sp[f] = 0.0
        S.vel_int[o] = 0.0
        S.pos_ref[o] = S.p[o]
        S.q_sp[o] = quat_from_yaw(S.yaw_sp[o])
    i = idx[~ol]
    if i.size == 0:
        return
    mode = S.ctrl_mode[i]
    lim = S.limits_id[i]
    hover = PT[S.profile_id[i], K.P_HOVER]
    yaw = S.yaw_sp[i]
    p, v, trx, trv, tra = S.p, S.v, S.tr_x, S.tr_v, S.tr_a
    is_vel = mode == K.M_VELOCITY
    ff = (mode != K.M_OFFBOARD) & ~is_vel
    f0 = np.where(ff, trv[i, 0], 0.0)
    f1 = np.where(ff, trv[i, 1], 0.0)
    f2 = np.where(ff, trv[i, 2], 0.0)
    vs0 = np.where(is_vel, trv[i, 0], (trx[i, 0] - p[i, 0]) * K.XY_P + f0)
    vs1 = np.where(is_vel, trv[i, 1], (trx[i, 1] - p[i, 1]) * K.XY_P + f1)
    vs2 = np.where(is_vel, trv[i, 2], (trx[i, 2] - p[i, 2]) * K.Z_P + f2)
    nxy = np.sqrt(vs0 * vs0 + vs1 * vs1)
    s = np.minimum(1.0, LT[lim, K.L_VXY] / np.maximum(nxy, 1e-6))
    vs0 = vs0 * s
    vs1 = vs1 * s
    vs2 = _clip(vs2, -np.where(mode == K.M_TAKEOFF, K.TKO_SPEED, K.Z_VEL_MAX_UP), K.Z_VEL_MAX_DN)  # PX4 起飞 speed_up 约束
    vi0 = S.vel_int[i, 0]
    vi1 = S.vel_int[i, 1]
    vi2 = _clip(S.vel_int[i, 2], -K.G, K.G)
    ve0 = vs0 - v[i, 0]
    ve1 = vs1 - v[i, 1]
    ve2 = vs2 - v[i, 2]
    fa0 = np.where(ff, tra[i, 0], 0.0)
    fa1 = np.where(ff, tra[i, 1], 0.0)
    fa2 = np.where(ff, tra[i, 2], 0.0)
    am = S.a_meas
    acs0 = ve0 * K.KVP_XY + vi0 - am[i, 0] * K.KVD_XY + fa0
    acs1 = ve1 * K.KVP_XY + vi1 - am[i, 1] * K.KVD_XY + fa1
    acs2 = ve2 * K.KVP_Z + vi2 - am[i, 2] * K.KVD_Z + fa2
    bx_ = -acs0
    by_ = -acs1
    bz_ = np.full(i.size, K.G)
    nb = np.sqrt(bx_ * bx_ + by_ * by_ + bz_ * bz_)
    bx_ = bx_ / nb
    by_ = by_ / nb
    bz_ = bz_ / nb
    ang = np.arccos(_clip(bz_, -1.0, 1.0))
    rn = np.sqrt(bx_ * bx_ + by_ * by_)
    big = rn > 1e-6
    rd = np.maximum(rn, 1e-9)
    rx = np.where(big, bx_ / rd, 1.0)
    ry = np.where(big, by_ / rd, 0.0)
    ang_l = np.minimum(ang, K.TILT)
    ca = np.cos(ang_l)
    sa = np.sin(ang_l)
    bx_ = ca * 0.0 + sa * rx
    by_ = ca * 0.0 + sa * ry
    bz_ = ca * 1.0 + sa * 0.0
    thr_max = K.THR_MAX * S.thr_cap[i].astype(np.float64)
    thr_z = acs2 * (hover / K.G) - hover
    coll = np.minimum(thr_z / bz_, -K.THR_MIN)
    t0 = bx_ * coll
    t1 = by_ * coll
    t2 = bz_ * coll
    txy = np.sqrt(t0 * t0 + t1 * t1)
    axy = np.minimum(txy, K.XY_MARG)
    t2 = np.maximum(t2, -np.sqrt(np.maximum(thr_max * thr_max - axy * axy, 0.0)))
    txm = np.sqrt(np.maximum(thr_max * thr_max - t2 * t2, 0.0))
    over = txy > txm
    sc = txm / np.maximum(txy, 1e-9)
    t0 = np.where(over, t0 * sc, t0)
    t1 = np.where(over, t1 * sc, t1)
    sat = ((t2 >= -K.THR_MIN) & (ve2 >= 0.0)) | ((t2 <= -thr_max) & (ve2 <= 0.0))
    ve2 = np.where(sat, 0.0, ve2)
    gh = K.G / hover
    ap0 = t0 * gh
    ap1 = t1 * gh
    need = acs0 * acs0 + acs1 * acs1 > ap0 * ap0 + ap1 * ap1
    arw = 2.0 / K.KVP_XY
    ve0 = np.where(need, ve0 - arw * (acs0 - ap0), ve0)
    ve1 = np.where(need, ve1 - arw * (acs1 - ap1), ve1)
    S.vel_int[i, 0] = vi0 + ve0 * K.KVI_XY * dt
    S.vel_int[i, 1] = vi1 + ve1 * K.KVI_XY * dt
    S.vel_int[i, 2] = vi2 + ve2 * K.KVI_Z * dt
    S.thr_sp[i, 0] = t0
    S.thr_sp[i, 1] = t1
    S.thr_sp[i, 2] = t2
    tn = np.sqrt(t0 * t0 + t1 * t1 + t2 * t2)
    z0 = -t0 / tn
    z1 = -t1 / tn
    z2 = -t2 / tn
    yc0 = -np.sin(yaw)
    yc1 = np.cos(yaw)
    yc2 = 0.0
    x0 = yc1 * z2 - yc2 * z1
    x1 = yc2 * z0 - yc0 * z2
    x2 = yc0 * z1 - yc1 * z0
    nx = np.sqrt(x0 * x0 + x1 * x1 + x2 * x2)
    x0 = x0 / nx
    x1 = x1 / nx
    x2 = x2 / nx
    y0 = z1 * x2 - z2 * x1
    y1 = z2 * x0 - z0 * x2
    y2 = z0 * x1 - z1 * x0
    tr_ = x0 + y1 + z2
    qw = np.sqrt(np.maximum(0.0, 1.0 + tr_)) / 2.0
    qx = np.sqrt(np.maximum(0.0, 1.0 + x0 - y1 - z2)) / 2.0
    qy = np.sqrt(np.maximum(0.0, 1.0 - x0 + y1 - z2)) / 2.0
    qz = np.sqrt(np.maximum(0.0, 1.0 - x0 - y1 + z2)) / 2.0
    qx = np.copysign(qx, y2 - z1)
    qy = np.copysign(qy, z0 - x2)
    qz = np.copysign(qz, x1 - y0)
    nq = np.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
    S.q_sp[i, 0] = qw / nq
    S.q_sp[i, 1] = qx / nq
    S.q_sp[i, 2] = qy / nq
    S.q_sp[i, 3] = qz / nq
    S.pos_ref[i] = trx[i]


# ---------------------------------------------------------------- 姿态（四元数 P + 理想速率环）
def att_ctrl_l1(S: FleetState, LT: np.ndarray, idx: np.ndarray, fall: np.ndarray, dt: float, *,
                faults: bool = False, w_fail: float = 0.0) -> None:
    if idx.size == 0:
        return
    q = S.q
    w0, x0, y0, z0 = q[idx, 0].copy(), q[idx, 1].copy(), q[idx, 2].copy(), q[idx, 3].copy()
    b0, b1, b2, b3 = S.q_sp[idx, 0], S.q_sp[idx, 1], S.q_sp[idx, 2], S.q_sp[idx, 3]
    a0, a1, a2, a3 = w0, -x0, -y0, -z0
    e0 = a0 * b0 - a1 * b1 - a2 * b2 - a3 * b3
    e1 = a0 * b1 + a1 * b0 + a2 * b3 - a3 * b2
    e2 = a0 * b2 - a1 * b3 + a2 * b0 + a3 * b1
    e3 = a0 * b3 + a1 * b2 - a2 * b1 + a3 * b0
    sg = e0 + 1e-12
    sgn = np.where(sg > 0.0, 1.0, np.where(sg < 0.0, -1.0, 0.0))
    e1 = e1 * sgn
    e2 = e2 * sgn
    e3 = e3 * sgn
    ymax = np.minimum(K.YAWRAUTO, LT[S.limits_id[idx], K.L_YAWRATE])
    wr0 = _clip(2.0 * e1 * K.K_ROLL, -K.RMAX_ROLL, K.RMAX_ROLL)
    wr1 = _clip(2.0 * e2 * K.K_PITCH, -K.RMAX_PITCH, K.RMAX_PITCH)
    wr2 = _clip(2.0 * e3 * K.K_YAW, -ymax, ymax)
    ground = open_loop_mask(S, idx, fall) & S.in_contact[idx]
    zero = fall | ground
    wr0 = np.where(zero, 0.0, wr0)
    wr1 = np.where(zero, 0.0, wr1)
    wr2 = np.where(zero, 0.0, wr2)
    if faults:
        bad = ~fall & (S.motor_ok[idx] != 255)
        wr0 = np.where(bad, wr0 + w_fail, wr0)
    rv0 = wr0 * dt
    rv1 = wr1 * dt
    rv2 = wr2 * dt
    an = np.sqrt(rv0 * rv0 + rv1 * rv1 + rv2 * rv2)
    fs_ = np.where(an > 1e-12, np.sin(an / 2.0) / np.maximum(an, 1e-12), 0.5)
    d0 = np.cos(an / 2.0)
    d1 = rv0 * fs_
    d2 = rv1 * fs_
    d3 = rv2 * fs_
    n0 = w0 * d0 - x0 * d1 - y0 * d2 - z0 * d3
    n1 = w0 * d1 + x0 * d0 + y0 * d3 - z0 * d2
    n2 = w0 * d2 - x0 * d3 + y0 * d0 + z0 * d1
    n3 = w0 * d3 + x0 * d2 - y0 * d1 + z0 * d0
    nn = np.sqrt(n0 * n0 + n1 * n1 + n2 * n2 + n3 * n3)
    q[idx, 0] = n0 / nn
    q[idx, 1] = n1 / nn
    q[idx, 2] = n2 / nn
    q[idx, 3] = n3 / nn
    S.omega[idx, 0] = wr0
    S.omega[idx, 1] = wr1
    S.omega[idx, 2] = wr2


def motor(S: FleetState, PT: np.ndarray, idx: np.ndarray, fall: np.ndarray, dt: float) -> None:
    """推力一阶滞后精确离散：T̂ += (‖thr_sp‖ − T̂)·(1 − e^{−dt/τ})；FALL（KILLED）推力 0。"""
    if idx.size == 0:
        return
    a = 1.0 - np.exp(-dt / PT[S.profile_id[idx], K.P_TAU])
    ts = S.thr_sp
    cmd = np.sqrt(ts[idx, 0] * ts[idx, 0] + ts[idx, 1] * ts[idx, 1] + ts[idx, 2] * ts[idx, 2])
    th = S.thrust[idx]
    S.thrust[idx] = np.where(fall, 0.0, th + (cmd - th) * a)


_ = math
