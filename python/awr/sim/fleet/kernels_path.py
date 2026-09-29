"""numba 版 TOPP-lite 时间参数化（M08-FR-023；M08 §6.5.2；1000 点 ≤ 1 ms）。

与 `path.topp_lite_py`（Python 参考实现）逐式对应。numba 只允许出现在 `awr/sim/fleet/kernels_*.py`。

航点转弯限速取 PX4 `computeMaxSpeedInWaypoint`：`v = √(a·d·tan(alpha/2))`（alpha 为两段在航点处张开的夹角，`d = min(d_acc, 半段长)`）。
该式恰好对应一段与两腿相切、半径 `r = v²/a` 的圆弧（切点距航点 `d = r / tan(alpha/2)`），因此参考在航点处以圆弧过渡
（速度方向连续，向心加速度 = a 作为前馈），直线段两端各缩短 d。前后向梯形速度剖面保证加减速可行。
输出 `out`（≤ 2n−1 行 × `SG_COLS` 列，每行一段；列见 `kernels_l1.SG_*`）：kind（0 直线、1 圆弧）、起点 p0、方向 u（直线方向或
圆弧起始切向）、圆弧指向圆心的单位向量 e、半径 r、转角 θ、长度、段起始轨迹时间 t0、段时长 T、加速段 ta、匀速段 tc、段内最高速 vc、
起止速度 v0、v1、加速度 a、目标航点下标（相对 w[0]）；`vw_out`（n+1）为航点速度。返回段数（总时长为最后一段 t0 + T）。
"""

from __future__ import annotations

import math

import numpy as np

from .kernels_l1 import njit

__all__ = ["topp_lite_nb"]


@njit(cache=True, fastmath=False)
def topp_lite_nb(w, v_start, v_c, a, vz_up, vz_dn, d_acc, out, vw_out):
    n = w.shape[0] - 1
    L = np.empty(n)
    U = np.empty((n, 3))
    vseg = np.empty(n)
    for k in range(n):
        dx = w[k + 1, 0] - w[k, 0]
        dy = w[k + 1, 1] - w[k, 1]
        dz = w[k + 1, 2] - w[k, 2]
        Lk = math.sqrt(dx * dx + dy * dy + dz * dz)
        L[k] = Lk
        inv = 1.0 / max(Lk, 1e-12)
        U[k, 0] = dx * inv
        U[k, 1] = dy * inv
        U[k, 2] = dz * inv
        vzl = vz_up if U[k, 2] < 0.0 else vz_dn
        vseg[k] = min(v_c, vzl / max(abs(U[k, 2]), 1e-6))
    # 航点处：张角 alpha、切点距离上限 dmax、转弯限速
    alpha = np.full(n + 1, math.pi)
    dmax = np.zeros(n + 1)
    vw = np.zeros(n + 1)
    vw[0] = min(v_start, vseg[0])
    vw[n] = 0.0
    for k in range(1, n):
        c = -(U[k - 1, 0] * U[k, 0] + U[k - 1, 1] * U[k, 1] + U[k - 1, 2] * U[k, 2])
        al = math.acos(min(max(c, -1.0), 1.0))
        alpha[k] = al
        dm = min(d_acc, 0.5 * min(L[k - 1], L[k]))
        dmax[k] = dm
        vt = math.sqrt(a * dm * math.tan(min(al, math.pi - 1e-6) / 2.0))
        vw[k] = min(min(vseg[k - 1], vseg[k]), vt)
    # 直线部分长度（两端扣除切点距离上限，保守）用于前后向可行性
    Ls = np.empty(n)
    for k in range(n):
        Ls[k] = max(L[k] - dmax[k] - dmax[k + 1], 0.0)
    for k in range(n - 1, -1, -1):
        vw[k] = min(vw[k], math.sqrt(vw[k + 1] * vw[k + 1] + 2.0 * a * Ls[k]))
    for k in range(n):
        vw[k + 1] = min(vw[k + 1], math.sqrt(vw[k] * vw[k] + 2.0 * a * Ls[k]))
    # 实际圆弧：r = v²/a，切点距离 d = r / tan(alpha/2)（≤ dmax）
    darc = np.zeros(n + 1)
    rarc = np.zeros(n + 1)
    for k in range(1, n):
        th = math.pi - alpha[k]
        if th > 1e-3 and vw[k] > 1e-6:
            r = vw[k] * vw[k] / a
            d = r / math.tan(alpha[k] / 2.0)
            if d > dmax[k]:
                d = dmax[k]
                r = d * math.tan(alpha[k] / 2.0)
            darc[k] = d
            rarc[k] = r
    for k in range(n + 1):
        vw_out[k] = vw[k]
    t = 0.0
    m = 0
    for k in range(n):
        # 直线段 k
        Lk = max(L[k] - darc[k] - darc[k + 1], 0.0)
        v0 = vw[k]
        v1 = vw[k + 1]
        vc = min(vseg[k], math.sqrt((2.0 * a * Lk + v0 * v0 + v1 * v1) / 2.0))
        vc = max(vc, max(v0, v1))
        ta = (vc - v0) / a
        td = (vc - v1) / a
        da = (vc * vc - v0 * v0) / (2.0 * a)
        dd = (vc * vc - v1 * v1) / (2.0 * a)
        tc = max(0.0, (Lk - da - dd) / vc) if vc > 1e-9 else 0.0
        T = ta + tc + td
        out[m, 0] = 0.0
        for c in range(3):
            out[m, 1 + c] = w[k, c] + U[k, c] * darc[k]
            out[m, 4 + c] = U[k, c]
            out[m, 7 + c] = 0.0
        out[m, 10] = 0.0
        out[m, 11] = 0.0
        out[m, 12] = Lk
        out[m, 13] = t
        out[m, 14] = T
        out[m, 15] = ta
        out[m, 16] = tc
        out[m, 17] = vc
        out[m, 18] = v0
        out[m, 19] = v1
        out[m, 20] = a
        out[m, 21] = k + 1
        t = t + T
        m += 1
        # 航点 k+1 处的圆弧
        j = k + 1
        if j < n and rarc[j] > 0.0:
            th = math.pi - alpha[j]
            ex = U[j, 0] + U[k, 0] * math.cos(alpha[j])
            ey = U[j, 1] + U[k, 1] * math.cos(alpha[j])
            ez = U[j, 2] + U[k, 2] * math.cos(alpha[j])
            ne = math.sqrt(ex * ex + ey * ey + ez * ez)
            r = rarc[j]
            v = vw[j]
            Ta = r * th / v
            out[m, 0] = 1.0
            for c in range(3):
                out[m, 1 + c] = w[j, c] - U[k, c] * darc[j]
                out[m, 4 + c] = U[k, c]
            out[m, 7] = ex / ne
            out[m, 8] = ey / ne
            out[m, 9] = ez / ne
            out[m, 10] = r
            out[m, 11] = th
            out[m, 12] = r * th
            out[m, 13] = t
            out[m, 14] = Ta
            out[m, 15] = 0.0
            out[m, 16] = Ta
            out[m, 17] = v
            out[m, 18] = v
            out[m, 19] = v
            out[m, 20] = a
            out[m, 21] = j
            t = t + Ta
            m += 1
    return m
