"""气动与积分的 numpy oracle（M08 §6.5.4；M08-FR-032 至 FR-035、FR-039）。

- composite：`F = −½rho·CdA·‖v_r‖·v_r − ΣΩ·c_rd·(v_r − (v_r·b3)·b3)`，`ΣΩ = n·ω_max·√clip(T̂, 0, 1)`；linear：`F = −K_dv·v_r`；
- 风**只**经相对空速 `v_r = v − w` 进入（ADR-024）；推力 `T = T̂·thrust_scale·T_max·(rho/rho0)^k_rho`（执行器故障乘子只作用于
  机体推力，FR-035；缺桨按剩余桨数折算，ext FR-091）；
- 积分：半隐式 Euler（先 v 后 p），积分前拷贝 `p_prev`（contact 判撞墙用）。
与融合核 `kernels_l1.tick_l1` 的 aero/integrate 段逐式对应；`aero` 把合力写入 `S.force`，`integrate` 读取。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from . import kernels_l1 as K

if TYPE_CHECKING:
    from .state import FleetState

__all__ = ["aero", "aero_force", "integrate"]


def aero_force(v_rel: np.ndarray, bz: np.ndarray, thrust: np.ndarray, rho: np.ndarray, PT: np.ndarray,
               pid: np.ndarray) -> np.ndarray:
    """纯气动力（不含推力与重力），N（M08-AC-012 对拍用）。"""
    vrx, vry, vrz = v_rel[:, 0], v_rel[:, 1], v_rel[:, 2]
    bzx, bzy, bzz = bz[:, 0], bz[:, 1], bz[:, 2]
    comp = PT[pid, K.P_AERO] != 0.0
    spd = np.sqrt(vrx * vrx + vry * vry + vrz * vrz)
    so = PT[pid, K.P_NROT] * PT[pid, K.P_WMAX] * np.sqrt(np.minimum(np.maximum(thrust, 0.0), 1.0))
    dotb = vrx * bzx + vry * bzy + vrz * bzz
    kq = 0.5 * rho * PT[pid, K.P_CDA]
    kr = so * PT[pid, K.P_CRD]
    kd = PT[pid, K.P_KDV]
    out = np.empty((len(pid), 3))
    for c, vr, bc in ((0, vrx, bzx), (1, vry, bzy), (2, vrz, bzz)):
        out[:, c] = np.where(comp, -kq * spd * vr - kr * (vr - dotb * bc), -kd * vr)
    return out


def aero(S: FleetState, PT: np.ndarray, idx: np.ndarray, *, faults: bool = False) -> None:
    """推力 + 气动合力写入 `S.force`（N，NED）。"""
    if idx.size == 0:
        return
    q = S.q
    w0, x0, y0, z0 = q[idx, 0], q[idx, 1], q[idx, 2], q[idx, 3]
    bzx = 2.0 * (x0 * z0 + w0 * y0)
    bzy = 2.0 * (y0 * z0 - w0 * x0)
    bzz = 1.0 - 2.0 * (x0 * x0 + y0 * y0)
    pid = S.profile_id[idx]
    rh = S.rho[idx].astype(np.float64)
    th = S.thrust[idx]
    sc = S.thrust_scale[idx].astype(np.float64)
    if faults:
        mo = S.motor_ok[idx]
        bad = mo != 255
        if bad.any():
            nrot = PT[pid, K.P_NROT]
            nok = np.zeros(idx.size)
            for b in range(8):
                nok += np.where(b < nrot, (mo >> b) & 1, 0)
            sc = np.where(bad, sc * (nok / nrot), sc)
    Tn = th * sc * PT[pid, K.P_TMAX] * (rh / K.RHO0) ** K.K_RHO
    fx = -bzx * Tn
    fy = -bzy * Tn
    fz = -bzz * Tn
    vrx = S.v[idx, 0] - S.wind[idx, 0]
    vry = S.v[idx, 1] - S.wind[idx, 1]
    vrz = S.v[idx, 2] - S.wind[idx, 2]
    comp = PT[pid, K.P_AERO] != 0.0
    spd = np.sqrt(vrx * vrx + vry * vry + vrz * vrz)
    so = PT[pid, K.P_NROT] * PT[pid, K.P_WMAX] * np.sqrt(np.minimum(np.maximum(th, 0.0), 1.0))
    dotb = vrx * bzx + vry * bzy + vrz * bzz
    px_ = vrx - dotb * bzx
    py_ = vry - dotb * bzy
    pz_ = vrz - dotb * bzz
    kq = 0.5 * rh * PT[pid, K.P_CDA]
    kr = so * PT[pid, K.P_CRD]
    kd = PT[pid, K.P_KDV]
    S.force[idx, 0] = np.where(comp, fx + (-kq * spd * vrx - kr * px_), fx + -kd * vrx)
    S.force[idx, 1] = np.where(comp, fy + (-kq * spd * vry - kr * py_), fy + -kd * vry)
    S.force[idx, 2] = np.where(comp, fz + (-kq * spd * vrz - kr * pz_), fz + -kd * vrz)


def integrate(S: FleetState, PT: np.ndarray, idx: np.ndarray, dt: float) -> None:
    """a = F/m + g；半隐式 Euler：v += a·dt，p += v·dt；a_meas ← a。"""
    if idx.size == 0:
        return
    m = PT[S.profile_id[idx], K.P_MASS]
    ax = S.force[idx, 0] / m + 0.0
    ay = S.force[idx, 1] / m + 0.0
    az = S.force[idx, 2] / m + K.G
    S.p_prev[idx] = S.p[idx]
    for c, a in ((0, ax), (1, ay), (2, az)):
        vv = S.v[idx, c] + a * dt
        S.v[idx, c] = vv
        S.p[idx, c] = S.p[idx, c] + vv * dt
        S.a_meas[idx, c] = a
