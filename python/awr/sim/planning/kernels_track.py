"""跟踪核（M10-FR-015、FR-016；M10 §6.5.8、§6.5.9、§6.5.11；g08 §5.2）：numba 核与同语义的 numpy oracle。

每个 L1 tick（125 Hz）对 TRAJ 槽位求值，输出 World ENU 的 p、v、a 与 ψ_enu（M10 代码中不出现 NED；写 FleetState 的
`tr_x/tr_v/tr_a/yaw_sp` 由 tracker 调用 M02 的唯一换算实现完成）。

槽位种类 `kind`：0 无；1 B-spline（可属于群组时钟）；2 环绕（解析）；3 编队成员（锚点 B-spline + 旋转槽位偏移）；
4 刹停保持（等待规划结果时按 a_brake 刹停到静止点）；5 直线段（粗校验证明无障碍的直飞与入圆段，梯形速度剖面，
静止起止，时间受 time_stretch 缩放）。

- time_stretch（PX4 PositionSmoothing）：`e_xy·v_xy ≥ 0` 时 `f_xy = 1 − clip(|e_xy|/2.0, 0, 1)`，z 轴 1.0 m，`f = min(f_xy, f_z)`；
- 时钟斜坡：`dτ/dt = rate`，rate 以斜率 `a_brake / max(|v(τ)|, 0.5)` 在 0 与 1 之间变化（暂停停在轨迹上）；
  时间缩放后 `v_out = v·r`，`a_out = a·r² + v·ṙ`；
- 航向：0 保持；1 lookahead（`ψ = atan2(p(τ + t_fwd) − p(τ))`，水平位移 < 0.1 m 时保持）；2 切向；
  3 看向点或竖直轴线（`yaw_arg[0:2]`）；4 固定（`yaw_arg[0]`）；输出航向按 YAW_RATE_MAX（45°/s，PX4 自动模式航向
  速率量级）限幅地逼近目标航向（FX-SIM2：环绕入圆时目标航向一步翻转 180°，姿态环耦合出 28° 倾角误差，触发 M09
  FastGuard `TILT_ERR_KILL`）；
- 环绕：`ω ← ω + clip(ω_t·rate − ω, ±(a_tan/R)·dt)`，`θ ← θ + dir·ω·dt·f`，`θ_acc ← θ_acc + ω·dt·f`；
  `turns > 0` 时剩余转角不大于刹车角 `ω²/(2·a_max)` 即 `ω_t ← 0`（累计转角停在 2π·turns），ω 降到 0 后置 done；
- 群组时钟：成员的 `rate·f` 取最小值推进 `τ_g`（任一成员落后，全队一起放慢）；编队航向二阶临界阻尼滤波。

数组布局（与 `awr.sim.mission.tracker.STATE_FIELDS` 一致）：orb 为 9 列 `cx, cy, cz, R, ω, ω_t, θ, θ_acc, a_max`，
刹停槽位复用 orb 前 7 列 `p0(3), v0(3), t_b`；直线段复用 orb 前 9 列 `a(3), b(3), t, T, v_peak`。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["HAVE_NUMBA", "K_BRAKE", "K_BSPLINE", "K_FORM", "K_LINE", "K_NONE", "K_ORBIT", "ORB_COLS", "Y_FIXED", "Y_LOOKAHEAD",
           "Y_NONE", "Y_PATH", "Y_POINT", "track_step", "track_step_numpy", "warmup"]

K_NONE, K_BSPLINE, K_ORBIT, K_FORM, K_BRAKE, K_LINE = 0, 1, 2, 3, 4, 5
Y_NONE, Y_LOOKAHEAD, Y_PATH, Y_POINT, Y_FIXED = 0, 1, 2, 3, 4
ORB_COLS = 9
TWO_PI = 2.0 * math.pi
YAW_RATE_MAX = math.radians(45.0)

try:
    from numba import njit as _njit

    HAVE_NUMBA = True
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def _njit(*a, **k):
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]


@_njit(cache=True, fastmath=False, inline="always")
def _wrap(a):
    return (a + math.pi) % TWO_PI - math.pi


@_njit(cache=True, fastmath=False)
def _bs_eval(Q, off, nseg, ts, tau, p, v, a):
    """在 τ 处求值（钳到 [0, T]）；返回是否已到端（τ ≥ T）。"""
    T = nseg * ts
    end = tau >= T
    tq = tau
    if tq < 0.0:
        tq = 0.0
    if tq > T:
        tq = T
    i = int(tq / ts)
    if i > nseg - 1:
        i = nseg - 1
    if i < 0:
        i = 0
    u = tq / ts - i
    u2 = u * u
    u3 = u2 * u
    b0 = (1.0 - 3.0 * u + 3.0 * u2 - u3) / 6.0
    b1 = (4.0 - 6.0 * u2 + 3.0 * u3) / 6.0
    b2 = (1.0 + 3.0 * u + 3.0 * u2 - 3.0 * u3) / 6.0
    b3 = u3 / 6.0
    d0 = (-1.0 + 2.0 * u - u2) / 2.0
    d1 = (-4.0 * u + 3.0 * u2) / 2.0
    d2 = (1.0 + 2.0 * u - 3.0 * u2) / 2.0
    d3 = u2 / 2.0
    e0 = 1.0 - u
    e1 = 3.0 * u - 2.0
    e2 = 1.0 - 3.0 * u
    e3 = u
    base = off + i
    its = 1.0 / ts
    for d in range(3):
        q0 = Q[base, d]
        q1 = Q[base + 1, d]
        q2 = Q[base + 2, d]
        q3 = Q[base + 3, d]
        p[d] = b0 * q0 + b1 * q1 + b2 * q2 + b3 * q3
        if end:
            v[d] = 0.0
            a[d] = 0.0
        else:
            v[d] = (d0 * q0 + d1 * q1 + d2 * q2 + d3 * q3) * its
            a[d] = (e0 * q0 + e1 * q1 + e2 * q2 + e3 * q3) * its * its
    return end


@_njit(cache=True, fastmath=False)
def _stretch(p, v, pe, emax_xy, emax_z):
    ex = p[0] - pe[0]
    ey = p[1] - pe[1]
    ez = p[2] - pe[2]
    fxy = 1.0
    fz = 1.0
    if ex * v[0] + ey * v[1] >= 0.0:
        fxy = 1.0 - min(math.sqrt(ex * ex + ey * ey) / emax_xy, 1.0)
    if ez * v[2] >= 0.0:
        fz = 1.0 - min(abs(ez) / emax_z, 1.0)
    return min(fxy, fz)


@_njit(cache=True, fastmath=False)
def track_step(idx, kind, off, nseg, ts, tau, rate, rate_tgt, rate_dot, yaw_mode, yaw_arg, psi, group, orb, orb_dir,
               orb_turns, slot_off, done, Q, G_tau, G_fmin, G_psi, G_w, G_off, G_nseg, G_ts, G_wmax, G_taupsi,
               G_active, p_enu, dt, emax_xy, emax_z, a_brake, t_fwd, a_tan, out_p, out_v, out_a, out_psi):
    p = np.empty(3)
    v = np.empty(3)
    a = np.empty(3)
    pf = np.empty(3)
    vf = np.empty(3)
    af = np.empty(3)
    ng = G_tau.shape[0]
    for g in range(ng):
        G_fmin[g] = 1.0
    for j in range(idx.shape[0]):
        k = idx[j]
        kd = kind[k]
        if kd == 0:
            continue
        g = group[k]
        r = rate[k]
        f = 1.0
        vabs = 0.0
        tq = 0.0
        end = False
        if kd == 1 or kd == 3:
            tq = G_tau[g] if g >= 0 else tau[k]
            if kd == 3:
                end = _bs_eval(Q, G_off[g], G_nseg[g], G_ts[g], tq, p, v, a)
                c = math.cos(G_psi[g])
                s = math.sin(G_psi[g])
                rx = c * slot_off[k, 0] - s * slot_off[k, 1]
                ry = s * slot_off[k, 0] + c * slot_off[k, 1]
                w = G_w[g]
                p[0] += rx
                p[1] += ry
                p[2] += slot_off[k, 2]
                if not end:
                    v[0] += -w * ry
                    v[1] += w * rx
                    a[0] += -w * w * rx
                    a[1] += -w * w * ry
            else:
                end = _bs_eval(Q, off[k], nseg[k], ts[k], tq, p, v, a)
            if end:
                done[k] = 1
            vabs = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
        elif kd == 2:
            R = orb[k, 3]
            w = orb[k, 4]
            wt = orb[k, 5] * rate_tgt[k]
            amax = orb[k, 8] * dt
            dw = wt - w
            if dw > amax:
                dw = amax
            if dw < -amax:
                dw = -amax
            w_new = w + dw
            th = orb[k, 6]
            dr = orb_dir[k]
            ct = math.cos(th)
            st = math.sin(th)
            p[0] = orb[k, 0] + R * ct
            p[1] = orb[k, 1] + R * st
            p[2] = orb[k, 2]
            v[0] = -dr * R * w_new * st
            v[1] = dr * R * w_new * ct
            v[2] = 0.0
            wd = dw / dt
            a[0] = -R * w_new * w_new * ct - dr * R * wd * st
            a[1] = -R * w_new * w_new * st + dr * R * wd * ct
            a[2] = 0.0
            f = _stretch(p, v, p_enu[k], emax_xy, emax_z)
            orb[k, 4] = w_new
            orb[k, 6] = _wrap(th + dr * w_new * dt * f)
            orb[k, 7] += w_new * dt * f
            if orb_turns[k] > 0.0:
                rem = TWO_PI * orb_turns[k] - orb[k, 7]
                al = orb[k, 8] if orb[k, 8] > 1e-9 else 1e-9
                if rem <= w_new * w_new / (2.0 * al) + w_new * dt:
                    orb[k, 5] = 0.0          # 提前刹车：累计转角停在 2π·turns
                if rem <= 0.0 or (orb[k, 5] == 0.0 and w_new <= 1e-6):
                    orb[k, 5] = 0.0
                    if w_new <= 1e-6:
                        done[k] = 1
            for d in range(3):
                out_p[k, d] = p[d]
                out_v[k, d] = v[d]
                out_a[k, d] = a[d]
        elif kd == 5:  # 直线段（梯形速度剖面，静止起止）
            T = orb[k, 7]
            vpk = orb[k, 8]
            t = orb[k, 6]
            dx = orb[k, 3] - orb[k, 0]
            dy = orb[k, 4] - orb[k, 1]
            dz = orb[k, 5] - orb[k, 2]
            L = math.sqrt(dx * dx + dy * dy + dz * dz)
            fin = t >= T or L < 1e-9 or vpk <= 0.0
            s_ = L
            sv = 0.0
            sa = 0.0
            if not fin:
                ta = T - L / vpk
                if ta < 1e-9:
                    ta = 1e-9
                acc = vpk / ta
                if t < ta:
                    s_ = 0.5 * acc * t * t
                    sv = acc * t
                    sa = acc
                elif t < T - ta:
                    s_ = 0.5 * acc * ta * ta + vpk * (t - ta)
                    sv = vpk
                else:
                    r = T - t
                    s_ = L - 0.5 * acc * r * r
                    sv = acc * r
                    sa = -acc
            inv = 1.0 / L if L > 1e-9 else 0.0
            p[0] = orb[k, 0] + dx * s_ * inv
            p[1] = orb[k, 1] + dy * s_ * inv
            p[2] = orb[k, 2] + dz * s_ * inv
            v[0] = dx * sv * inv
            v[1] = dy * sv * inv
            v[2] = dz * sv * inv
            a[0] = dx * sa * inv
            a[1] = dy * sa * inv
            a[2] = dz * sa * inv
            for d in range(3):
                out_p[k, d] = p[d]
                out_v[k, d] = v[d]
                out_a[k, d] = a[d]
            f = _stretch(p, v, p_enu[k], emax_xy, emax_z)
            orb[k, 6] = t + dt * f
            if fin:
                done[k] = 1
        else:  # 4 刹停保持
            tb = orb[k, 6]
            vx = orb[k, 3]
            vy = orb[k, 4]
            vz = orb[k, 5]
            sp = math.sqrt(vx * vx + vy * vy + vz * vz)
            t_stop = sp / a_brake if sp > 1e-9 else 0.0
            te = tb if tb < t_stop else t_stop
            for d in range(3):
                u = orb[k, 3 + d] / sp if sp > 1e-9 else 0.0
                out_p[k, d] = orb[k, d] + orb[k, 3 + d] * te - 0.5 * a_brake * te * te * u
                out_v[k, d] = orb[k, 3 + d] - a_brake * te * u if tb < t_stop else 0.0
                out_a[k, d] = -a_brake * u if tb < t_stop else 0.0
            orb[k, 6] = tb + dt
            if tb >= t_stop:
                done[k] = 1
        if kd == 1 or kd == 3:
            rd = rate_dot[k]
            for d in range(3):
                out_p[k, d] = p[d]
                out_v[k, d] = v[d] * r
                out_a[k, d] = a[d] * r * r + v[d] * rd
            f = _stretch(out_p[k], out_v[k], p_enu[k], emax_xy, emax_z)
            slope = a_brake / max(vabs, 0.5)
            tg = rate_tgt[k]
            rn = r
            if r < tg:
                rn = min(tg, r + slope * dt)
            elif r > tg:
                rn = max(tg, r - slope * dt)
            rate[k] = rn
            rate_dot[k] = (rn - r) / dt
            if g >= 0:
                x = r * f
                if x < G_fmin[g]:
                    G_fmin[g] = x
            else:
                tau[k] = tq + dt * r * f
        # 航向
        ym = yaw_mode[k]
        ps = psi[k]
        if ym == 1:
            if kd == 1 or kd == 3:
                if kd == 3:
                    _bs_eval(Q, G_off[g], G_nseg[g], G_ts[g], tq + t_fwd, pf, vf, af)
                    c = math.cos(G_psi[g])
                    s = math.sin(G_psi[g])
                    pf[0] += c * slot_off[k, 0] - s * slot_off[k, 1]
                    pf[1] += s * slot_off[k, 0] + c * slot_off[k, 1]
                else:
                    _bs_eval(Q, off[k], nseg[k], ts[k], tq + t_fwd, pf, vf, af)
                dx = pf[0] - out_p[k, 0]
                dy = pf[1] - out_p[k, 1]
                if dx * dx + dy * dy >= 0.01:
                    ps = math.atan2(dy, dx)
            else:
                vx = out_v[k, 0]
                vy = out_v[k, 1]
                if vx * vx + vy * vy >= 0.01:
                    ps = math.atan2(vy, vx)
        elif ym == 2:
            vx = out_v[k, 0]
            vy = out_v[k, 1]
            if vx * vx + vy * vy >= 0.01:
                ps = math.atan2(vy, vx)
        elif ym == 3:
            dx = yaw_arg[k, 0] - out_p[k, 0]
            dy = yaw_arg[k, 1] - out_p[k, 1]
            if dx * dx + dy * dy >= 0.01:
                ps = math.atan2(dy, dx)
        elif ym == 4:
            ps = yaw_arg[k, 0]
        dps = _wrap(ps - psi[k])
        lim = YAW_RATE_MAX * dt
        if dps > lim:
            ps = _wrap(psi[k] + lim)
        elif dps < -lim:
            ps = _wrap(psi[k] - lim)
        psi[k] = ps
        out_psi[k] = ps
    # 群组时钟与编队航向滤波
    for g in range(ng):
        if G_active[g] == 0:
            continue
        G_tau[g] += dt * G_fmin[g]
        if G_taupsi[g] > 0.0:
            _bs_eval(Q, G_off[g], G_nseg[g], G_ts[g], G_tau[g], p, v, a)
            if v[0] * v[0] + v[1] * v[1] >= 0.01:
                psa = math.atan2(v[1], v[0])
                tp = G_taupsi[g]
                al = _wrap(psa - G_psi[g]) / (tp * tp) - 2.0 * G_w[g] / tp
                w = G_w[g] + al * dt
                if w > G_wmax[g]:
                    w = G_wmax[g]
                if w < -G_wmax[g]:
                    w = -G_wmax[g]
                G_w[g] = w
                G_psi[g] = _wrap(G_psi[g] + w * dt)
            else:
                G_w[g] = 0.0
        elif G_taupsi[g] < 0.0:   # 对齐（aligned / path）：ψ_f = ψA
            _bs_eval(Q, G_off[g], G_nseg[g], G_ts[g], G_tau[g], p, v, a)
            if v[0] * v[0] + v[1] * v[1] >= 0.01:
                G_psi[g] = math.atan2(v[1], v[0])
            G_w[g] = 0.0


def _bs_eval_np(Q, off, nseg, ts, tau):
    T = nseg * ts
    end = tau >= T
    tq = np.clip(tau, 0.0, T)
    i = np.clip((tq / ts).astype(np.int64), 0, np.maximum(nseg - 1, 0))
    u = tq / ts - i
    u2 = u * u
    u3 = u2 * u
    B = np.stack([(1 - 3 * u + 3 * u2 - u3) / 6, (4 - 6 * u2 + 3 * u3) / 6, (1 + 3 * u + 3 * u2 - 3 * u3) / 6, u3 / 6], 1)
    D = np.stack([(-1 + 2 * u - u2) / 2, (-4 * u + 3 * u2) / 2, (1 + 2 * u - 3 * u2) / 2, u2 / 2], 1)
    E = np.stack([1 - u, 3 * u - 2, 1 - 3 * u, u], 1)
    base = off + i
    q = np.stack([Q[base], Q[base + 1], Q[base + 2], Q[base + 3]], 1)   # (n, 4, 3)
    its = (1.0 / ts)[:, None]
    p = np.einsum("nk,nkd->nd", B, q)
    v = np.einsum("nk,nkd->nd", D, q) * its
    a = np.einsum("nk,nkd->nd", E, q) * its * its
    v[end] = 0.0
    a[end] = 0.0
    return p, v, a, end


def _stretch_np(p, v, pe, emax_xy, emax_z):
    e = p - pe
    fxy = np.where(e[:, 0] * v[:, 0] + e[:, 1] * v[:, 1] >= 0.0,
                   1.0 - np.minimum(np.hypot(e[:, 0], e[:, 1]) / emax_xy, 1.0), 1.0)
    fz = np.where(e[:, 2] * v[:, 2] >= 0.0, 1.0 - np.minimum(np.abs(e[:, 2]) / emax_z, 1.0), 1.0)
    return np.minimum(fxy, fz)


def track_step_numpy(idx, kind, off, nseg, ts, tau, rate, rate_tgt, rate_dot, yaw_mode, yaw_arg, psi, group, orb,
                     orb_dir, orb_turns, slot_off, done, Q, G_tau, G_fmin, G_psi, G_w, G_off, G_nseg, G_ts, G_wmax,
                     G_taupsi, G_active, p_enu, dt, emax_xy, emax_z, a_brake, t_fwd, a_tan, out_p, out_v, out_a,
                     out_psi):
    """numpy oracle：与 `track_step` 同签名、同语义（向量化；numba 不可用时机群钳到 300 架）。"""
    idx = np.asarray(idx, np.int64)
    G_fmin[:] = 1.0
    kd = kind[idx]
    # ---- B-spline 与编队成员
    m = idx[(kd == 1) | (kd == 3)]
    tq_all = np.zeros(len(kind))
    if m.size:
        g = group[m]
        grp = g >= 0
        tq = np.where(grp, G_tau[np.maximum(g, 0)], tau[m])
        tq_all[m] = tq
        form = kind[m] == 3
        o = np.where(form, G_off[np.maximum(g, 0)], off[m])
        ns = np.where(form, G_nseg[np.maximum(g, 0)], nseg[m])
        tss = np.where(form, G_ts[np.maximum(g, 0)], ts[m])
        p, v, a, end = _bs_eval_np(Q, o, ns, tss, tq)
        if form.any():
            gi = g[form]
            c, s = np.cos(G_psi[gi]), np.sin(G_psi[gi])
            so = slot_off[m[form]]
            rx = c * so[:, 0] - s * so[:, 1]
            ry = s * so[:, 0] + c * so[:, 1]
            w = G_w[gi]
            p[form, 0] += rx
            p[form, 1] += ry
            p[form, 2] += so[:, 2]
            ne = ~end[form]
            v[form, 0] += np.where(ne, -w * ry, 0.0)
            v[form, 1] += np.where(ne, w * rx, 0.0)
            a[form, 0] += np.where(ne, -w * w * rx, 0.0)
            a[form, 1] += np.where(ne, -w * w * ry, 0.0)
        done[m[end]] = 1
        vabs = np.linalg.norm(v, axis=1)
        r = rate[m].copy()
        rd = rate_dot[m]
        out_p[m] = p
        out_v[m] = v * r[:, None]
        out_a[m] = a * (r * r)[:, None] + v * rd[:, None]
        f = _stretch_np(out_p[m], out_v[m], p_enu[m], emax_xy, emax_z)
        slope = a_brake / np.maximum(vabs, 0.5)
        tg = rate_tgt[m]
        rn = np.where(r < tg, np.minimum(tg, r + slope * dt), np.where(r > tg, np.maximum(tg, r - slope * dt), r))
        rate[m] = rn
        rate_dot[m] = (rn - r) / dt
        tau[m[~grp]] = tq[~grp] + dt * r[~grp] * f[~grp]
        if grp.any():
            np.minimum.at(G_fmin, g[grp], r[grp] * f[grp])
    # ---- 环绕
    m2 = idx[kd == 2]
    if m2.size:
        R = orb[m2, 3]
        w = orb[m2, 4]
        wt = orb[m2, 5] * rate_tgt[m2]
        amax = orb[m2, 8] * dt
        dw = np.clip(wt - w, -amax, amax)
        wn = w + dw
        th = orb[m2, 6]
        dr = orb_dir[m2].astype(np.float64)
        ct, st = np.cos(th), np.sin(th)
        p = np.stack([orb[m2, 0] + R * ct, orb[m2, 1] + R * st, orb[m2, 2]], 1)
        v = np.stack([-dr * R * wn * st, dr * R * wn * ct, np.zeros(m2.size)], 1)
        wd = dw / dt
        a = np.stack([-R * wn * wn * ct - dr * R * wd * st, -R * wn * wn * st + dr * R * wd * ct, np.zeros(m2.size)], 1)
        f = _stretch_np(p, v, p_enu[m2], emax_xy, emax_z)
        orb[m2, 4] = wn
        orb[m2, 6] = (th + dr * wn * dt * f + math.pi) % TWO_PI - math.pi
        orb[m2, 7] += wn * dt * f
        tr = orb_turns[m2] > 0.0
        rem = TWO_PI * orb_turns[m2] - orb[m2, 7]
        al = np.maximum(orb[m2, 8], 1e-9)
        brake = tr & (rem <= wn * wn / (2.0 * al) + wn * dt)
        orb[m2[brake], 5] = 0.0
        stop = tr & ((rem <= 0.0) | ((orb[m2, 5] == 0.0) & (wn <= 1e-6)))
        orb[m2[stop], 5] = 0.0
        done[m2[stop & (wn <= 1e-6)]] = 1
        out_p[m2], out_v[m2], out_a[m2] = p, v, a
    # ---- 刹停保持
    m4 = idx[kd == 4]
    if m4.size:
        tb = orb[m4, 6]
        v0 = orb[m4, 3:6]
        sp = np.linalg.norm(v0, axis=1)
        t_stop = np.where(sp > 1e-9, sp / a_brake, 0.0)
        te = np.minimum(tb, t_stop)
        u = np.where(sp[:, None] > 1e-9, v0 / np.maximum(sp, 1e-12)[:, None], 0.0)
        moving = (tb < t_stop)[:, None]
        out_p[m4] = orb[m4, 0:3] + v0 * te[:, None] - 0.5 * a_brake * (te * te)[:, None] * u
        out_v[m4] = np.where(moving, v0 - a_brake * te[:, None] * u, 0.0)
        out_a[m4] = np.where(moving, -a_brake * u, 0.0)
        orb[m4, 6] = tb + dt
        done[m4[tb >= t_stop]] = 1
    # ---- 直线段（梯形速度剖面）
    m5 = idx[kd == 5]
    if m5.size:
        T = orb[m5, 7]
        vpk = orb[m5, 8]
        t = orb[m5, 6]
        dd = orb[m5, 3:6] - orb[m5, 0:3]
        L = np.linalg.norm(dd, axis=1)
        fin = (t >= T) | (L < 1e-9) | (vpk <= 0.0)
        vps = np.where(vpk > 0.0, vpk, 1.0)
        ta = np.maximum(T - L / vps, 1e-9)
        acc = vps / ta
        r = T - t
        s_ = np.where(t < ta, 0.5 * acc * t * t, np.where(t < T - ta, 0.5 * acc * ta * ta + vps * (t - ta),
                                                               L - 0.5 * acc * r * r))
        sv = np.where(t < ta, acc * t, np.where(t < T - ta, vps, acc * r))
        sa = np.where(t < ta, acc, np.where(t < T - ta, 0.0, -acc))
        s_ = np.where(fin, L, s_)
        sv = np.where(fin, 0.0, sv)
        sa = np.where(fin, 0.0, sa)
        u = np.where(L[:, None] > 1e-9, dd / np.where(L > 1e-9, L, 1.0)[:, None], 0.0)
        p = orb[m5, 0:3] + u * s_[:, None]
        v = u * sv[:, None]
        a = u * sa[:, None]
        out_p[m5], out_v[m5], out_a[m5] = p, v, a
        f = _stretch_np(p, v, p_enu[m5], emax_xy, emax_z)
        orb[m5, 6] = t + dt * f
        done[m5[fin]] = 1
    # ---- 航向
    ym = yaw_mode[idx]
    ps = psi[idx].copy()
    ov = out_v[idx]
    op = out_p[idx]
    vv = ov[:, 0] ** 2 + ov[:, 1] ** 2
    look = ym == 1
    if look.any():
        li = idx[look]
        lk = kind[li]
        bsm = (lk == 1) | (lk == 3)
        if bsm.any():
            lb = li[bsm]
            g = group[lb]
            form = kind[lb] == 3
            o = np.where(form, G_off[np.maximum(g, 0)], off[lb])
            ns = np.where(form, G_nseg[np.maximum(g, 0)], nseg[lb])
            tss = np.where(form, G_ts[np.maximum(g, 0)], ts[lb])
            pf, _vf, _af, _e = _bs_eval_np(Q, o, ns, tss, tq_all[lb] + t_fwd)
            if form.any():
                gi = g[form]
                c, s = np.cos(G_psi[gi]), np.sin(G_psi[gi])
                so = slot_off[lb[form]]
                pf[form, 0] += c * so[:, 0] - s * so[:, 1]
                pf[form, 1] += s * so[:, 0] + c * so[:, 1]
            dx = pf[:, 0] - out_p[lb, 0]
            dy = pf[:, 1] - out_p[lb, 1]
            sel = np.flatnonzero(look)[bsm]
            ok = dx * dx + dy * dy >= 0.01
            ps[sel[ok]] = np.arctan2(dy[ok], dx[ok])
        other = np.flatnonzero(look)[~bsm]
        ok = vv[other] >= 0.01
        ps[other[ok]] = np.arctan2(ov[other[ok], 1], ov[other[ok], 0])
    tan = (ym == 2) & (vv >= 0.01)
    ps[tan] = np.arctan2(ov[tan, 1], ov[tan, 0])
    pt = ym == 3
    if pt.any():
        dx = yaw_arg[idx[pt], 0] - op[pt, 0]
        dy = yaw_arg[idx[pt], 1] - op[pt, 1]
        ok = dx * dx + dy * dy >= 0.01
        sel = np.flatnonzero(pt)[ok]
        ps[sel] = np.arctan2(dy[ok], dx[ok])
    fx = ym == 4
    ps[fx] = yaw_arg[idx[fx], 0]
    prev = psi[idx]
    dps = (ps - prev + math.pi) % TWO_PI - math.pi
    lim = YAW_RATE_MAX * dt
    ps = (prev + np.clip(dps, -lim, lim) + math.pi) % TWO_PI - math.pi
    psi[idx] = ps
    out_psi[idx] = ps
    # ---- 群组
    for gg in np.flatnonzero(G_active):
        G_tau[gg] += dt * G_fmin[gg]
        if G_taupsi[gg] != 0.0:
            _p, v, _a, _e = _bs_eval_np(Q, np.array([G_off[gg]]), np.array([G_nseg[gg]]), np.array([G_ts[gg]]),
                                        np.array([G_tau[gg]]))
            vx, vy = float(v[0, 0]), float(v[0, 1])
            if G_taupsi[gg] > 0.0:
                if vx * vx + vy * vy >= 0.01:
                    tp = G_taupsi[gg]
                    al = ((math.atan2(vy, vx) - G_psi[gg] + math.pi) % TWO_PI - math.pi) / (tp * tp) - 2.0 * G_w[gg] / tp
                    w = min(max(G_w[gg] + al * dt, -G_wmax[gg]), G_wmax[gg])
                    G_w[gg] = w
                    G_psi[gg] = (G_psi[gg] + w * dt + math.pi) % TWO_PI - math.pi
                else:
                    G_w[gg] = 0.0
            else:
                if vx * vx + vy * vy >= 0.01:
                    G_psi[gg] = math.atan2(vy, vx)
                G_w[gg] = 0.0


def warmup() -> float:
    """以 N = 2 的哑数组调用一次，触发 JIT（或读取缓存）；返回耗时（秒，墙钟只用于日志）。"""
    import time

    t0 = time.perf_counter()
    n = 2
    Q = np.zeros((16, 3))
    Q[:, 0] = np.arange(16)
    args = dict(idx=np.arange(n, dtype=np.int32), kind=np.array([1, 2], np.uint8), off=np.zeros(n, np.int64),
                nseg=np.full(n, 13, np.int32), ts=np.full(n, 0.5), tau=np.zeros(n), rate=np.ones(n), rate_tgt=np.ones(n),
                rate_dot=np.zeros(n), yaw_mode=np.array([1, 3], np.uint8), yaw_arg=np.zeros((n, 3)), psi=np.zeros(n),
                group=np.full(n, -1, np.int32), orb=np.zeros((n, ORB_COLS)), orb_dir=np.ones(n, np.int8),
                orb_turns=np.zeros(n), slot_off=np.zeros((n, 3)), done=np.zeros(n, np.uint8), Q=Q,
                G_tau=np.zeros(1), G_fmin=np.ones(1), G_psi=np.zeros(1), G_w=np.zeros(1), G_off=np.zeros(1, np.int64),
                G_nseg=np.ones(1, np.int32), G_ts=np.full(1, 0.5), G_wmax=np.ones(1), G_taupsi=np.zeros(1),
                G_active=np.zeros(1, np.uint8), p_enu=np.zeros((n, 3)), dt=0.008, emax_xy=2.0, emax_z=1.0, a_brake=2.0,
                t_fwd=1.0, a_tan=2.0, out_p=np.zeros((n, 3)), out_v=np.zeros((n, 3)), out_a=np.zeros((n, 3)),
                out_psi=np.zeros(n))
    args["orb"][1, 3] = 3.0
    args["orb"][1, 5] = 0.5
    args["orb"][1, 8] = 1.0
    track_step(**args)
    # 运行期签名：跟踪器 stage 传入 M08 的只读 ENU 视图 `S.enu.pos`（numba 把只读数组当作另一种类型）。只预热可写
    # 变体时，S1 起飞后首次进入跟踪会在主循环内编译，被 supervisor 的 2 s 活性阈值杀掉（D1 验收第 1 轮 4.1）
    pr = np.zeros((n, 3))
    pr.flags.writeable = False
    args["p_enu"] = pr
    track_step(**args)
    return time.perf_counter() - t0
