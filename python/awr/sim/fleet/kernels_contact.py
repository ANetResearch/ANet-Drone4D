"""numba contact 核（125 Hz，M08-FR-036；M08 §6.5.5、§9.2）。

地表高度直接索引 M04 `dsm_grid()`（dsm_eff，2 m，f32 只读，行主序，第 0 行在南）：按柱体语义取所在格柱顶（最近格，
禁止双线性）；AGL 用 `dtm_grid()`（10 m）格心双线性（与 `awr.world.geometry.grids.Grid.bilinear` 同式）。世界未加载时
（`flat = 1`）以各机出生点高度为水平地面。判据（FleetConfig）：穿入 > `pen_m` 且水平进入更高栅格（高差 > `wall_step_m`）
判 COLLISION_WORLD（退回上一步所在格并停在该格地表）；下降速度 > `impact_vz` 判 IMPACT；否则夹持。触地检测：
LAND 类（LAND、ELAND、DESCENT_FF、RTL FINAL）接触且 ‖v‖ < 0.25 持续 `land_land_s`；其他模式按 r20（‖vz‖ < 0.25、
‖vxy‖ < 1.5、‖ω‖ < 20 °/s、推力 < 0.3·hover）持续 `land_s`。事件写入 `ev_out`（slot, code），返回条数：
code 1 COLLISION_WORLD、2 IMPACT、3 TOUCHDOWN、4 LIFTOFF。
"""

# ruff: noqa: SIM108, SIM109  （numba 核与 oracle 逐式对应：保留显式 if/else 与逐项比较）

from __future__ import annotations

import math

import numpy as np

from . import params_px4 as P
from .kernels_l1 import njit

__all__ = ["CRASH_COLLISION_WORLD", "CRASH_IMPACT", "EV_IMPACT", "EV_LIFTOFF", "EV_TOUCHDOWN", "EV_WORLD", "contact",
           "dsm_nearest", "dtm_bilinear", "uav_hits"]

M_IDLE, M_SPOOLUP, M_TAKEOFF, M_LAND, M_RTL, M_ELAND, M_DESCENT, M_KILLED = 0, 1, 2, 8, 9, 11, 12, 13
E_TOUCHDOWN, E_LIFTOFF, E_COLLISION = 8, 16, 32
CRASH_COLLISION_WORLD, CRASH_IMPACT = 2, 4  # FLIGHT_SUB[CRASHED] 下标 + 1（0 为无）：TILT 1、WORLD 2、UAV 3、IMPACT 4
EV_WORLD, EV_IMPACT, EV_TOUCHDOWN, EV_LIFTOFF = 1, 2, 3, 4
R_FINAL = 3
LAND_VZ = P.LAND_DETECT_VZ
LAND_VXY = P.LAND_DETECT_VXY
LAND_W = P.LAND_DETECT_OMEGA
LAND_THR = P.LAND_DETECT_THR


@njit(cache=True, fastmath=False)
def dsm_nearest(a, aff, x, y):
    """柱体语义最近格（界外钳制，M04 `Grid.nearest`）。"""
    h = a.shape[0]
    w = a.shape[1]
    c = math.floor((x - aff[0]) / aff[2])
    r = math.floor((y - aff[1]) / aff[2])
    c = min(max(c, 0), w - 1)
    r = min(max(r, 0), h - 1)
    return float(a[r, c])


@njit(cache=True, fastmath=False)
def dtm_bilinear(a, aff, x, y):
    """格心双线性（界外钳制，M04 `Grid.bilinear`）。"""
    h = a.shape[0]
    w = a.shape[1]
    fx = (x - aff[0]) / aff[2]
    fy = (y - aff[1]) / aff[2]
    gx = min(max(fx - 0.5, 0.0), float(max(w - 1, 0)))
    gy = min(max(fy - 0.5, 0.0), float(max(h - 1, 0)))
    c0 = min(int(gx), max(w - 2, 0))
    r0 = min(int(gy), max(h - 2, 0))
    tx = gx - c0
    ty = gy - r0
    dc = 1 if w >= 2 else 0
    dr = 1 if h >= 2 else 0
    v00 = float(a[r0, c0])
    v01 = float(a[r0, c0 + dc])
    v10 = float(a[r0 + dr, c0])
    v11 = float(a[r0 + dr, c0 + dc])
    return (v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty


@njit(cache=True, fastmath=False)
def contact(idx, t_s, p, v, p_prev, q, omega, thrust, thr_sp, thr_cap, home, dsm, dsm_aff, dtm, dtm_aff, flat,
            ctrl_mode, ctrl_phase, mode_t, td_t, profile_id, PT, in_contact, landed, in_air, contact_t, crash_sub,
            ground_z, agl, mode_evt, rtl_sub, prm, ev_out):
    pen_m = prm[0]
    wall_step = prm[1]
    impact_vz = prm[2]
    land_s = prm[3]
    land_land_s = prm[4]
    nev = 0
    for kk in range(idx.shape[0]):
        i = idx[kk]
        if flat != 0.0:
            h = -home[i, 2]
            g = h
        else:
            h = dsm_nearest(dsm, dsm_aff, p[i, 1], p[i, 0])
            g = dtm_bilinear(dtm, dtm_aff, p[i, 1], p[i, 0])
        ground_z[i] = -h
        z_u = -p[i, 2]
        agl[i] = z_u - g
        mode = ctrl_mode[i]
        pin = mode == M_IDLE or mode == M_SPOOLUP or (mode == M_TAKEOFF and ctrl_phase[i] == 0) or \
            (mode == M_KILLED and landed[i])
        if pin:
            p[i, 2] = -h
            for k in range(3):
                v[i, k] = 0.0
                omega[i, k] = 0.0
            if crash_sub[i] == 0:  # 在地面：调平，保留航向
                qw = q[i, 0]
                qx = q[i, 1]
                qy = q[i, 2]
                qz = q[i, 3]
                psi = math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
                q[i, 0] = math.cos(psi / 2.0)
                q[i, 1] = 0.0
                q[i, 2] = 0.0
                q[i, 3] = math.sin(psi / 2.0)
            in_contact[i] = True
            landed[i] = True
            in_air[i] = False
            agl[i] = h - g
            contact_t[i] = math.nan
            continue
        pen = h - z_u
        if pen <= 0.0:
            in_contact[i] = False
        else:
            if flat != 0.0:
                h_prev = h
            else:
                h_prev = dsm_nearest(dsm, dsm_aff, p_prev[i, 1], p_prev[i, 0])
            if crash_sub[i] > 0:  # 已坠毁（机间碰撞）后落地：停在地表，不再重复判定
                p[i, 2] = -h
                for k in range(3):
                    v[i, k] = 0.0
                in_contact[i] = True
                landed[i] = True
                in_air[i] = False
                contact_t[i] = math.nan
                continue
            wall = pen > pen_m and (h - h_prev) > wall_step
            impact = (not wall) and v[i, 2] > impact_vz
            if wall or impact:
                if wall:
                    p[i, 0] = p_prev[i, 0]
                    p[i, 1] = p_prev[i, 1]
                    p[i, 2] = -h_prev
                    ground_z[i] = -h_prev
                    crash_sub[i] = CRASH_COLLISION_WORLD
                    ev_out[nev, 1] = EV_WORLD
                else:
                    p[i, 2] = -h
                    crash_sub[i] = CRASH_IMPACT
                    ev_out[nev, 1] = EV_IMPACT
                ev_out[nev, 0] = i
                nev += 1
                ctrl_mode[i] = M_KILLED
                thrust[i] = 0.0
                for k in range(3):
                    thr_sp[i, k] = 0.0
                    omega[i, k] = 0.0
                mode_evt[i] = mode_evt[i] | E_COLLISION
                in_contact[i] = True
                landed[i] = True
                in_air[i] = False
                contact_t[i] = math.nan
                continue
            p[i, 2] = -h
            if v[i, 2] > 0.0:
                v[i, 2] = 0.0
            in_contact[i] = True
        # 触地检测
        v0 = v[i, 0]
        v1 = v[i, 1]
        v2 = v[i, 2]
        land_cls = mode == M_LAND or mode == M_ELAND or mode == M_DESCENT or (mode == M_RTL and rtl_sub[i] == R_FINAL)
        if land_cls:
            still = in_contact[i] and math.sqrt(v0 * v0 + v1 * v1 + v2 * v2) < 0.25
            need = land_land_s
        else:
            w0 = omega[i, 0]
            w1 = omega[i, 1]
            w2 = omega[i, 2]
            still = in_contact[i] and abs(v2) < LAND_VZ and math.sqrt(v0 * v0 + v1 * v1) < LAND_VXY and \
                math.sqrt(w0 * w0 + w1 * w1 + w2 * w2) < LAND_W and thrust[i] < LAND_THR * PT[profile_id[i], 2]
            need = land_s
        if still:
            if math.isnan(contact_t[i]):
                contact_t[i] = t_s
            if t_s - contact_t[i] >= need - 1e-9:
                landed[i] = True
                contact_t[i] = math.nan
                td_t[i] = math.nan
                for k in range(3):
                    v[i, k] = 0.0
                    thr_sp[i, k] = 0.0
                if mode != M_KILLED:
                    ctrl_mode[i] = M_IDLE
                    ctrl_phase[i] = 0
                    mode_t[i] = t_s
                thrust[i] = 0.0
                thr_cap[i] = 1.0
                mode_evt[i] = mode_evt[i] | E_TOUCHDOWN
                ev_out[nev, 0] = i
                ev_out[nev, 1] = EV_TOUCHDOWN
                nev += 1
        else:
            contact_t[i] = math.nan
        if landed[i] and not in_contact[i] and z_u - h > 0.1:
            landed[i] = False
            mode_evt[i] = mode_evt[i] | E_LIFTOFF
            ev_out[nev, 0] = i
            ev_out[nev, 1] = EV_LIFTOFF
            nev += 1
        in_air[i] = not landed[i]
    return nev


@njit(cache=True, fastmath=False)
def uav_hits(p0, p1, rad, win, out):
    """机间碰撞（COLLISION_UAV）的候选与判定（FX2-R2：替代 8 套半格平移网格的排序配对，N = 1000 时由约 3 ms 降到数十 µs）。

    p0、p1：(n, 3) 各机自上次检查以来线性运动段的起止点；rad：(n,) 碰撞半径；win：候选窗口（中点各轴之差都 < win，取
    `collide.CELL_M` = 6 m，覆盖原网格法的全部候选：同处某套 6 m 网格的一格即各轴之差 < 6 m）。判定与 `UavCollider._hits`
    同式：线段 CPA 距离 < rad_a + rad_b。命中对 (a, b)（a < b，局部下标）写入 out，返回条数；超过 out 容量时返回 −1。
    结果按 (a, b) 升序（调用方据此按升序施加后果，与原实现顺序相同）。"""
    n = p0.shape[0]
    mx = np.empty(n)
    for i in range(n):
        mx[i] = 0.5 * (p0[i, 0] + p1[i, 0])
    order = np.argsort(mx, kind="mergesort")
    sx = np.empty(n)
    sy = np.empty(n)
    sz = np.empty(n)
    for k in range(n):  # 按 x 排序后的连续中点数组（内层扫描只读连续内存）
        i = order[k]
        sx[k] = mx[i]
        sy[k] = 0.5 * (p0[i, 1] + p1[i, 1])
        sz[k] = 0.5 * (p0[i, 2] + p1[i, 2])
    nh = 0
    cap = out.shape[0]
    for ii in range(n):
        ax = sx[ii]
        ay = sy[ii]
        az = sz[ii]
        for jj in range(ii + 1, n):
            if sx[jj] - ax >= win:
                break
            if abs(sy[jj] - ay) >= win:
                continue
            if abs(sz[jj] - az) >= win:
                continue
            a = order[ii]
            b = order[jj]
            lo = a if a < b else b
            hi = b if a < b else a
            r00 = p0[lo, 0] - p0[hi, 0]
            r01 = p0[lo, 1] - p0[hi, 1]
            r02 = p0[lo, 2] - p0[hi, 2]
            d0 = (p1[lo, 0] - p1[hi, 0]) - r00
            d1 = (p1[lo, 1] - p1[hi, 1]) - r01
            d2 = (p1[lo, 2] - p1[hi, 2]) - r02
            dd = d0 * d0 + d1 * d1 + d2 * d2
            if dd > 1e-12:
                s = -(r00 * d0 + r01 * d1 + r02 * d2) / dd
                if s < 0.0:
                    s = 0.0
                elif s > 1.0:
                    s = 1.0
            else:
                s = 0.0
            m0 = r00 + s * d0
            m1 = r01 + s * d1
            m2 = r02 + s * d2
            if math.sqrt(m0 * m0 + m1 * m1 + m2 * m2) < rad[lo] + rad[hi]:
                if nh >= cap:
                    return -1
                out[nh, 0] = lo
                out[nh, 1] = hi
                nh += 1
    if nh > 1:
        key = np.empty(nh, np.int64)
        for k in range(nh):
            key[k] = out[k, 0] * n + out[k, 1]
        o = np.argsort(key, kind="mergesort")
        tmp = out[:nh].copy()
        for k in range(nh):
            out[k, 0] = tmp[o[k], 0]
            out[k, 1] = tmp[o[k], 1]
    return nh
