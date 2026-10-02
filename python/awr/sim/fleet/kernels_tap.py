"""numba NED/FRD → ENU/FLU 换算核（tap 与 ENU 只读视图共用，M08-FR-050、FR-087；M08 §6.5.1）。

公式与 M02 唯一实现 `awr.world.georef.frames.ned_frd_to_enu_flu_batch` 逐位相同：`p_enu = (p.y, p.x, −p.z)`；
`q_xyzw = ((x+y), (x−y), (w−z), (w+z))·(1/√2)`（输入 (w,x,y,z)，WORLD←FLU）；`ω_flu = (ω_x, −ω_y, −ω_z)`
（`tests/sim/test_views.py` 与 M02 golden 对拍）。numba 只允许出现在 `awr/sim/fleet/kernels_*.py`。
"""

from __future__ import annotations

import numpy as np

from awr.world.georef.frames import _INV_SQRT2

from .kernels_l1 import njit

__all__ = ["INV_SQRT2", "enu_convert", "tap_fill"]

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


@njit(cache=True, fastmath=False)
def tap_fill(idx, agent_no, fs, sub, in_air, fidelity, lease_owner, f_loc_ok, f_failsafe, f_gcs, f_fcu, f_loc_deg,
             f_alert, locked, mission_item, battery_pct, p, v, q, omega, lut_unarmed, lut_init, lut_land,
             o_agent, o_fs, o_flags, o_mi, o_bp, o_ctrl, o_pos, o_vel, o_q, o_omega, o_dt,
             l_agent, l_fs, l_bp, l_pos, l_q, l_vel, l_flags, l_ctrl):
    """tap 的一次融合写入（FX2-R2，D1-AC-07/27/28；M08-AC-033 发布 p99 ≤ 300 µs @ N = 1000）：Full64 与 Lite32 的全部字段
    逐行一次写完。与 numpy 路径（`fuser.fs_bytes/flags_bytes/ctrl_bytes` + ENU 视图赋值 + `layouts.lite_from_full`）逐位
    相同：ENU 换算同 `enu_convert`；float64 → float32 为就近舍入；Lite32 的 q、vel 量化为 float32 值转 float64 后
    `rint(x · s)`（偶数舍入）并钳到 ±32767（`tests/sim/test_tap.py` 对拍）。mission_item、battery_pct 为 None 时传
    长度 0 的数组，分别写 0xFFFF 与 255。"""
    n = idx.shape[0]
    has_mi = mission_item.shape[0] > 0
    has_bp = battery_pct.shape[0] > 0
    for k in range(n):
        i = idx[k]
        f = fs[i]
        a = np.uint16(agent_no[i])
        fsb = np.uint8((f & 0x1F) | ((sub[i] & 7) << 5))
        fl = 0
        if not lut_unarmed[f]:
            fl |= 1
        if in_air[i]:
            fl |= 2
        if f_loc_ok[i]:
            fl |= 4
        if f_failsafe[i]:
            fl |= 8
        if f_gcs[i]:
            fl |= 16
        if f_fcu[i]:
            fl |= 32
        if f_loc_deg[i]:
            fl |= 64
        if f_alert[i]:
            fl |= 128
        lk = locked[i]
        owner = 5 if (f_failsafe[i] or lk) else np.int64(lease_owner[i])
        native = 0 if lut_init[f] else (3 if lut_land[f] else 2)
        pose = 3 if (fidelity[i] & 8) != 0 else 1
        ctrl = np.uint8((owner & 7) | ((1 if lk else 0) << 3) | ((native & 3) << 4) | (pose << 6))
        mi = np.uint16(mission_item[i]) if has_mi else np.uint16(0xFFFF)
        bp = np.uint8(battery_pct[i]) if has_bp else np.uint8(255)
        o_agent[k] = a
        o_fs[k] = fsb
        o_flags[k] = np.uint8(fl)
        o_mi[k] = mi
        o_bp[k] = bp
        o_ctrl[k] = ctrl
        o_dt[k] = 0
        px = np.float32(p[i, 1])
        py = np.float32(p[i, 0])
        pz = np.float32(-p[i, 2])
        o_pos[k, 0] = px
        o_pos[k, 1] = py
        o_pos[k, 2] = pz
        vx = np.float32(v[i, 1])
        vy = np.float32(v[i, 0])
        vz = np.float32(-v[i, 2])
        o_vel[k, 0] = vx
        o_vel[k, 1] = vy
        o_vel[k, 2] = vz
        w = q[i, 0]
        x = q[i, 1]
        y = q[i, 2]
        z = q[i, 3]
        q0 = np.float32((x + y) * INV_SQRT2)
        q1 = np.float32((x - y) * INV_SQRT2)
        q2 = np.float32((w - z) * INV_SQRT2)
        q3 = np.float32((w + z) * INV_SQRT2)
        o_q[k, 0] = q0
        o_q[k, 1] = q1
        o_q[k, 2] = q2
        o_q[k, 3] = q3
        o_omega[k, 0] = np.float32(omega[i, 0])
        o_omega[k, 1] = np.float32(-omega[i, 1])
        o_omega[k, 2] = np.float32(-omega[i, 2])
        l_agent[k] = a
        l_fs[k] = fsb
        l_bp[k] = bp
        l_pos[k, 0] = px
        l_pos[k, 1] = py
        l_pos[k, 2] = pz
        l_q[k, 0] = _q16(q0, 32767.0)
        l_q[k, 1] = _q16(q1, 32767.0)
        l_q[k, 2] = _q16(q2, 32767.0)
        l_q[k, 3] = _q16(q3, 32767.0)
        l_vel[k, 0] = _q16(vx, 100.0)
        l_vel[k, 1] = _q16(vy, 100.0)
        l_vel[k, 2] = _q16(vz, 100.0)
        l_flags[k] = np.uint8(fl)
        l_ctrl[k] = ctrl
    return n


@njit(cache=True, fastmath=False)
def _q16(x32, s):
    r = np.rint(np.float64(x32) * s)
    if r > 32767.0:
        r = 32767.0
    elif r < -32767.0:
        r = -32767.0
    return np.int16(r)


@njit(cache=True, fastmath=False)
def enu_to_ned_rows(sl, w_enu, out_ned):
    """out_ned[sl[k]] = (w_n, w_e, -w_u)：`frames.enu_to_ned` 的同一重排（M07 env stage 写 FleetState.wind，50 Hz；FX2-R3）。"""
    for k in range(sl.shape[0]):
        s = sl[k]
        out_ned[s, 0] = w_enu[k, 1]
        out_ned[s, 1] = w_enu[k, 0]
        out_ned[s, 2] = -w_enu[k, 2]
