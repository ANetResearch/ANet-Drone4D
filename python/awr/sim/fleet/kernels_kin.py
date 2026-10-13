"""numba ENU/FLU → NED/FRD 写入核（`kinematic_write.write_kinematic`，M08-FR-211；M08 §15.3.5）。

`tap`/ENU 视图换算核（`kernels_tap.enu_convert`）的逆：`p_ned = (p.y, p.x, −p.z)`；四元数输入 WORLD←FLU 的 (x, y, z, w)，
设 a, b, c, d 为其四个分量，则 NED/FRD 四元数 (w, x, y, z) = ((c+d), (a+b), (a−b), (d−c))·(1/√2)；`ω_frd = (ω_x, −ω_y, −ω_z)`。
写 p 前把旧位置存入 p_prev（contact 的撞墙判据读取）。numba 只出现在 `awr/sim/fleet/kernels_*.py`。
"""

from __future__ import annotations

import numpy as np

from .kernels_l1 import njit
from .kernels_tap import INV_SQRT2

__all__ = ["kin_write", "kin_write_numpy"]


@njit(cache=True, fastmath=False)
def kin_write(slots, p_enu, v_enu, q_xyzw, omega_flu, acc_enu, ref_enu, p, p_prev, v, q, omega, a_meas, pos_ref):
    for k in range(slots.shape[0]):
        i = slots[k]
        p_prev[i, 0] = p[i, 0]
        p_prev[i, 1] = p[i, 1]
        p_prev[i, 2] = p[i, 2]
        p[i, 0] = p_enu[k, 1]
        p[i, 1] = p_enu[k, 0]
        p[i, 2] = -p_enu[k, 2]
        v[i, 0] = v_enu[k, 1]
        v[i, 1] = v_enu[k, 0]
        v[i, 2] = -v_enu[k, 2]
        a_meas[i, 0] = acc_enu[k, 1]
        a_meas[i, 1] = acc_enu[k, 0]
        a_meas[i, 2] = -acc_enu[k, 2]
        pos_ref[i, 0] = ref_enu[k, 1]
        pos_ref[i, 1] = ref_enu[k, 0]
        pos_ref[i, 2] = -ref_enu[k, 2]
        a = q_xyzw[k, 0]
        b = q_xyzw[k, 1]
        c = q_xyzw[k, 2]
        d = q_xyzw[k, 3]
        q[i, 0] = (c + d) * INV_SQRT2
        q[i, 1] = (a + b) * INV_SQRT2
        q[i, 2] = (a - b) * INV_SQRT2
        q[i, 3] = (d - c) * INV_SQRT2
        omega[i, 0] = omega_flu[k, 0]
        omega[i, 1] = -omega_flu[k, 1]
        omega[i, 2] = -omega_flu[k, 2]


def kin_write_numpy(slots, p_enu, v_enu, q_xyzw, omega_flu, acc_enu, ref_enu, p, p_prev, v, q, omega, a_meas, pos_ref):
    """`kin_write` 的 numpy 等价实现（逐位相同）。"""
    i = np.asarray(slots, np.int64)
    p_prev[i] = p[i]
    for src, dst in ((p_enu, p), (v_enu, v), (acc_enu, a_meas), (ref_enu, pos_ref)):
        dst[i, 0] = src[:, 1]
        dst[i, 1] = src[:, 0]
        dst[i, 2] = -src[:, 2]
    a, b, c, d = q_xyzw[:, 0], q_xyzw[:, 1], q_xyzw[:, 2], q_xyzw[:, 3]
    q[i, 0] = (c + d) * INV_SQRT2
    q[i, 1] = (a + b) * INV_SQRT2
    q[i, 2] = (a - b) * INV_SQRT2
    q[i, 3] = (d - c) * INV_SQRT2
    omega[i, 0] = omega_flu[:, 0]
    omega[i, 1] = -omega_flu[:, 1]
    omega[i, 2] = -omega_flu[:, 2]
