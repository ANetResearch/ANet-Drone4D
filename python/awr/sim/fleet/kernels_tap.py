"""numba NED/FRD → ENU/FLU 换算核（tap 与 ENU 只读视图共用，M08-FR-050、FR-087；M08 §6.5.1）。

公式与 M02 唯一实现 `awr.world.georef.frames.ned_frd_to_enu_flu_batch` 逐位相同：`p_enu = (p.y, p.x, −p.z)`；
`q_xyzw = ((x+y), (x−y), (w−z), (w+z))·(1/√2)`（输入 (w,x,y,z)，WORLD←FLU）；`ω_flu = (ω_x, −ω_y, −ω_z)`
（`tests/sim/test_views.py` 与 M02 golden 对拍）。numba 只允许出现在 `awr/sim/fleet/kernels_*.py`。
"""

from __future__ import annotations

from awr.world.georef.frames import _INV_SQRT2

from .kernels_l1 import njit

__all__ = ["INV_SQRT2", "enu_convert"]

INV_SQRT2 = float(_INV_SQRT2)


@njit(cache=True, fastmath=False)
def enu_convert(idx, p, v, a, q, q_sp, omega, pos_ref, home, o_pos, o_vel, o_acc, o_q, o_qsp, o_omega, o_ref, o_home):
    for kk in range(idx.shape[0]):
        i = idx[kk]
        o_pos[i, 0] = p[i, 1]
        o_pos[i, 1] = p[i, 0]
        o_pos[i, 2] = -p[i, 2]
        o_vel[i, 0] = v[i, 1]
        o_vel[i, 1] = v[i, 0]
        o_vel[i, 2] = -v[i, 2]
        o_acc[i, 0] = a[i, 1]
        o_acc[i, 1] = a[i, 0]
        o_acc[i, 2] = -a[i, 2]
        o_ref[i, 0] = pos_ref[i, 1]
        o_ref[i, 1] = pos_ref[i, 0]
        o_ref[i, 2] = -pos_ref[i, 2]
        o_home[i, 0] = home[i, 1]
        o_home[i, 1] = home[i, 0]
        o_home[i, 2] = -home[i, 2]
        w = q[i, 0]
        x = q[i, 1]
        y = q[i, 2]
        z = q[i, 3]
        o_q[i, 0] = (x + y) * INV_SQRT2
        o_q[i, 1] = (x - y) * INV_SQRT2
        o_q[i, 2] = (w - z) * INV_SQRT2
        o_q[i, 3] = (w + z) * INV_SQRT2
        w = q_sp[i, 0]
        x = q_sp[i, 1]
        y = q_sp[i, 2]
        z = q_sp[i, 3]
        o_qsp[i, 0] = (x + y) * INV_SQRT2
        o_qsp[i, 1] = (x - y) * INV_SQRT2
        o_qsp[i, 2] = (w - z) * INV_SQRT2
        o_qsp[i, 3] = (w + z) * INV_SQRT2
        o_omega[i, 0] = omega[i, 0]
        o_omega[i, 1] = -omega[i, 1]
        o_omega[i, 2] = -omega[i, 2]
