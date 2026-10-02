"""M13 云台大机群路径的 numba 融合核（M13-FR-011、FR-016；M13 §6.5.2；FX2-R3，ADR-070）。

`GimbalBank._step_vec` 对动态集逐 (slot, 云台列) 求目标角、钳制限位、按 `rate_max·dt` 限速逼近。numpy 实现每次调用约 40 个
花式下标与 einsum 运算，N = 1000、ladder 环绕（全部 LOOK_AT 环绕中心）时约 2.8 ms/次（50 Hz，0.14 核），是稳态最大的
stage。本核在一次遍历内完成同一计算，运算与 `_step_vec` 逐项相同：

- 机体旋转矩阵按 `frames.quat_to_R` 的同一表达式；
- `einsum("nij,nj->ni")`、`einsum("nji,nj->ni")` 的三项和按 j = 0、1、2 顺序累加（与 c_einsum 对长度 3 的归约同序）；
- `np.minimum`、`np.maximum` 的 NaN 传播（任一操作数为 NaN 时结果为 NaN）以 `_nmin`、`_nmax` 复现；
- `arctan2`、`sqrt` 为同一 libm 实现；`d_z ** 2` 为 `d_z * d_z`。

`tests/sensors/test_gimbal.py::test_kernel_matches_vector_path` 与 numpy 路径逐位对拍。numba 不可用（或
`AWR_KERNEL=numpy`）时 `HAVE_NUMBA` 为 False，调用方退回 numpy 路径。
"""

from __future__ import annotations

import math
import os

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover - numba 缺失时退回 numpy 路径
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["HAVE_NUMBA", "TAB_COLS", "count_pairs", "gimbal_step", "gimbal_step_scan", "warmup"]

# 参数表列：az_min、az_max、el_min、el_max、rate_max、default_el
TAB_COLS = 6
_FIXED, _LOOK_AT, _LOOK_AT_AXIS, _NADIR, _FORWARD = 0, 1, 2, 3, 4


@njit(cache=True, fastmath=False)
def _nmax(a, b):
    if a != a or b != b:
        return math.nan
    return a if a >= b else b


@njit(cache=True, fastmath=False)
def _nmin(a, b):
    if a != a or b != b:
        return math.nan
    return a if a <= b else b


@njit(cache=True, fastmath=False)
def gimbal_step(S_, K_, tab_i, tab_ok, tab_f, tab_mt, tab_MR, g_mode, g_az, g_el, g_paz, g_pel, g_tgt, g_lim, g_dyn,
                pos, quat, dt, exit_eps):
    """逐对推进一步；返回撞限位的对数（`stats["limited"]` 增量）。tab_i[i] 为第 i 对的参数表行（-1 或 tab_ok 为假：
    该对没有云台规格，退出动态集）。"""
    n = S_.shape[0]
    n_lim = 0
    for i in range(n):
        s = S_[i]
        k = K_[i]
        ti = tab_i[i]
        if ti < 0 or not tab_ok[ti]:
            g_dyn[s, k] = False
            continue
        az_min = tab_f[ti, 0]
        az_max = tab_f[ti, 1]
        el_min = tab_f[ti, 2]
        el_max = tab_f[ti, 3]
        rate = tab_f[ti, 4]
        mode = np.int64(g_mode[s, k])
        az = g_az[s, k]
        el = g_el[s, k]
        az_t = g_paz[s, k]
        el_t = g_pel[s, k]
        if mode == _NADIR:
            az_t = 0.0
            el_t = el_min
        elif mode == _FORWARD:
            az_t = 0.0
            el_t = tab_f[ti, 5]
        la = mode == _LOOK_AT
        la = la or mode == _LOOK_AT_AXIS  # numba 核内保持标量比较
        if la:
            x = quat[s, 0]
            y = quat[s, 1]
            z = quat[s, 2]
            w = quat[s, 3]
            xx = x * x
            yy = y * y
            zz = z * z
            xy = x * y
            xz = x * z
            yz = y * z
            wx = w * x
            wy = w * y
            wz = w * z
            r00 = 1.0 - 2.0 * (yy + zz)
            r01 = 2.0 * (xy - wz)
            r02 = 2.0 * (xz + wy)
            r10 = 2.0 * (xy + wz)
            r11 = 1.0 - 2.0 * (xx + zz)
            r12 = 2.0 * (yz - wx)
            r20 = 2.0 * (xz - wy)
            r21 = 2.0 * (yz + wx)
            r22 = 1.0 - 2.0 * (xx + yy)
            m0 = tab_mt[ti, 0]
            m1 = tab_mt[ti, 1]
            m2 = tab_mt[ti, 2]
            pm0 = pos[s, 0] + (r00 * m0 + r01 * m1 + r02 * m2)
            pm1 = pos[s, 1] + (r10 * m0 + r11 * m1 + r12 * m2)
            pm2 = pos[s, 2] + (r20 * m0 + r21 * m1 + r22 * m2)
            t0 = g_tgt[s, k, 0]
            t1 = g_tgt[s, k, 1]
            t2 = pm2 if mode == _LOOK_AT_AXIS else g_tgt[s, k, 2]
            dw0 = t0 - pm0
            dw1 = t1 - pm1
            dw2 = t2 - pm2
            # Rbᵀ·dw（einsum "nji,nj->ni"）
            b0 = r00 * dw0 + r10 * dw1 + r20 * dw2
            b1 = r01 * dw0 + r11 * dw1 + r21 * dw2
            b2 = r02 * dw0 + r12 * dw1 + r22 * dw2
            # MRᵀ·b
            d0 = tab_MR[ti, 0, 0] * b0 + tab_MR[ti, 1, 0] * b1 + tab_MR[ti, 2, 0] * b2
            d1 = tab_MR[ti, 0, 1] * b0 + tab_MR[ti, 1, 1] * b1 + tab_MR[ti, 2, 1] * b2
            d2 = tab_MR[ti, 0, 2] * b0 + tab_MR[ti, 1, 2] * b1 + tab_MR[ti, 2, 2] * b2
            h = math.sqrt(d0 * d0 + d1 * d1)
            nn = math.sqrt(h * h + d2 * d2)
            e = math.atan2(d2, h)
            a = math.atan2(d1, d0)
            if h < 1e-3 * nn:
                a = az
            az_t = a
            el_t = e
        az_c = _nmin(_nmax(az_t, az_min), az_max)
        el_c = _nmin(_nmax(el_t, el_min), el_max)
        lim = (az_c != az_t) or (el_c != el_t)
        if lim:
            n_lim += 1
        step = rate * dt
        az = az + _nmin(_nmax(az_c - az, -step), step)
        el = el + _nmin(_nmax(el_c - el, -step), step)
        g_lim[s, k] = lim
        done = (not la) and abs(az_c - az) <= exit_eps and abs(el_c - el) <= exit_eps
        if done:
            az = az_c
            el = el_c
        g_az[s, k] = az
        g_el[s, k] = el
        if done:
            g_dyn[s, k] = False
    return n_lim


@njit(cache=True, fastmath=False)
def count_pairs(dyn, active):
    """动态集中属于活动机体的 (slot, 列) 对数（`step` 的路径选择：与 np.nonzero(dyn[rows]) 的对数相同）。"""
    n = 0
    for s in range(dyn.shape[0]):
        if active[s]:
            for k in range(dyn.shape[1]):
                if dyn[s, k]:
                    n += 1
    return n


@njit(cache=True, fastmath=False, inline="always")
def _same(a, b):
    return a == b or (a != a and b != b)


@njit(cache=True, fastmath=False)
def gimbal_step_scan(dyn, active, slot_rig, ncols, tab_ok, tab_f, tab_mt, tab_MR, tab_eq, g_mode, g_az, g_el, g_paz, g_pel,
                     g_tgt, g_lim, g_dyn, pos, quat, dt, exit_eps):
    """`gimbal_step` 的扫描版：按 slot 升序、列升序遍历动态集中属于活动机体的对（与 `step` 的行主序枚举同序），逐对推进。
    同一机体两列的全部输入（模式、当前角、参数角、目标与云台参数行）逐位相同时，第二列直接沿用第一列的结果（同一函数、
    同一输入，结果逐位相同；ladder 等环绕场景相机与热成像都对准环绕中心，省去一半三角函数，FX2-R3）。返回撞限位的对数。"""
    S1 = np.zeros(1, np.int64)
    K1 = np.zeros(1, np.int64)
    T1 = np.zeros(1, np.int64)
    n_lim = 0
    for s in range(dyn.shape[0]):
        if not active[s]:
            continue
        r = slot_rig[s]
        prev_k = -1
        g_az_prev = 0.0
        g_el_prev = 0.0
        for k in range(dyn.shape[1]):
            if not dyn[s, k]:
                continue
            ti = r * ncols + k if r >= 0 else -1
            if prev_k >= 0:
                tp = r * ncols + prev_k if r >= 0 else -1
                if (ti >= 0 and tp >= 0 and tab_eq[tp, ti] and g_mode[s, k] == g_mode[s, prev_k]
                        and _same(g_az[s, k], g_az_prev) and _same(g_el[s, k], g_el_prev)
                        and _same(g_paz[s, k], g_paz[s, prev_k]) and _same(g_pel[s, k], g_pel[s, prev_k])
                        and _same(g_tgt[s, k, 0], g_tgt[s, prev_k, 0]) and _same(g_tgt[s, k, 1], g_tgt[s, prev_k, 1])
                        and _same(g_tgt[s, k, 2], g_tgt[s, prev_k, 2])):
                    g_az[s, k] = g_az[s, prev_k]
                    g_el[s, k] = g_el[s, prev_k]
                    g_lim[s, k] = g_lim[s, prev_k]
                    g_dyn[s, k] = g_dyn[s, prev_k]
                    if g_lim[s, k]:
                        n_lim += 1
                    continue
            g_az_prev = g_az[s, k]
            g_el_prev = g_el[s, k]
            S1[0] = s
            K1[0] = k
            T1[0] = ti
            n_lim += gimbal_step(S1, K1, T1, tab_ok, tab_f, tab_mt, tab_MR, g_mode, g_az, g_el, g_paz, g_pel, g_tgt, g_lim,
                                 g_dyn, pos, quat, dt, exit_eps)
            prev_k = k
    return n_lim


def warmup() -> None:
    """以运行期类型调用一次：状态块为 (容量, 2) 的 C 连续数组，位姿为 ENU 只读视图（`S.enu.pos`、`S.enu.q`）。"""
    if not HAVE_NUMBA:
        return
    cap = 4
    S_ = np.zeros(1, np.int64)
    K_ = np.zeros(1, np.int64)
    tab_i = np.zeros(1, np.int64)
    tab_ok = np.ones(1, np.bool_)
    tab_f = np.zeros((1, TAB_COLS))
    tab_mt = np.zeros((1, 3))
    tab_MR = np.zeros((1, 3, 3))
    pos = np.zeros((cap, 3))
    quat = np.zeros((cap, 4))
    pos.flags.writeable = False
    quat.flags.writeable = False
    gimbal_step(S_, K_, tab_i, tab_ok, tab_f, tab_mt, tab_MR, np.zeros((cap, 2), np.uint8), np.zeros((cap, 2)),
                np.zeros((cap, 2)), np.zeros((cap, 2)), np.zeros((cap, 2)), np.zeros((cap, 2, 3)),
                np.zeros((cap, 2), np.bool_), np.zeros((cap, 2), np.bool_), pos, quat, 0.02, 1e-4)
    dyn = np.zeros((cap, 2), np.bool_)
    act = np.zeros(cap, np.bool_)
    count_pairs(dyn, act)
    gimbal_step_scan(dyn, act, np.zeros(cap, np.int32), 2, tab_ok, tab_f, tab_mt, tab_MR, np.zeros((1, 1), np.bool_),
                     np.zeros((cap, 2), np.uint8), np.zeros((cap, 2)), np.zeros((cap, 2)), np.zeros((cap, 2)),
                     np.zeros((cap, 2)), np.zeros((cap, 2, 3)), np.zeros((cap, 2), np.bool_), np.zeros((cap, 2), np.bool_),
                     pos, quat, 0.02, 1e-4)
