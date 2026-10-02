"""stage `contact`（order 090，every 2，125 Hz）：地表夹持、撞墙、硬着陆、触地检测（M08-FR-036；M08 §6.5.5）。

numba 模式调用 `kernels_contact.contact`；oracle 模式调用本文件的 `contact_numpy`（向量化，逐式对应）。地表读 M04
`dsm_grid()`（柱体最近格）与 `dtm_grid()`（格心双线性）的只读栅格视图；世界未加载时退化为各机出生点高度的水平面。
事件：`sim.contact.collision{kind, pos_enu_m, speed_mps}`（severity 3）、`sim.contact.touchdown{pos_enu_m}`（机体 id 在外层 uav）。
坐标：内部 NED；ENU 栅格高度 h 换算为 `ground_z = −h`（M08 §6.5.1）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from .. import kernels_contact as KC
from .. import kernels_l1 as K
from ..collide import UavCollider
from .l1 import l1_indices, rtl_phase_of

if TYPE_CHECKING:
    from ..pipeline import StageCtx
    from ..state import FleetState

__all__ = ["ContactCfg", "ContactStage", "contact_numpy"]


@dataclass(frozen=True)
class ContactCfg:
    wall_step_m: float = 1.0
    pen_m: float = 0.3
    impact_vz_mps: float = 3.0
    land_detect_s: float = 1.0
    land_detect_land_s: float = 0.5

    def prm(self) -> np.ndarray:
        return np.array([self.pen_m, self.wall_step_m, self.impact_vz_mps, self.land_detect_s, self.land_detect_land_s, 0.0])


def _nearest(a: np.ndarray, aff: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = a.shape
    c = np.clip(np.floor((x - aff[0]) / aff[2]).astype(np.int64), 0, w - 1)
    r = np.clip(np.floor((y - aff[1]) / aff[2]).astype(np.int64), 0, h - 1)
    return a[r, c].astype(np.float64)


def _bilinear(a: np.ndarray, aff: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    h, w = a.shape
    fx = (x - aff[0]) / aff[2]
    fy = (y - aff[1]) / aff[2]
    gx = np.minimum(np.maximum(fx - 0.5, 0.0), float(max(w - 1, 0)))
    gy = np.minimum(np.maximum(fy - 0.5, 0.0), float(max(h - 1, 0)))
    c0 = np.minimum(gx.astype(np.int64), max(w - 2, 0))
    r0 = np.minimum(gy.astype(np.int64), max(h - 2, 0))
    tx = gx - c0
    ty = gy - r0
    dc = 1 if w >= 2 else 0
    dr = 1 if h >= 2 else 0
    v00 = a[r0, c0].astype(np.float64)
    v01 = a[r0, c0 + dc].astype(np.float64)
    v10 = a[r0 + dr, c0].astype(np.float64)
    v11 = a[r0 + dr, c0 + dc].astype(np.float64)
    return (v00 * (1 - tx) + v01 * tx) * (1 - ty) + (v10 * (1 - tx) + v11 * tx) * ty


def contact_numpy(S: FleetState, idx: np.ndarray, t_s: float, dsm, dsm_aff, dtm, dtm_aff, flat: float, PT: np.ndarray,
                  rtl_sub: np.ndarray, prm: np.ndarray, ev_out: np.ndarray) -> int:
    """`kernels_contact.contact` 的 numpy oracle（同一判据与顺序；事件按 slot 升序写入 ev_out）。"""
    if idx.size == 0:
        return 0
    pen_m, wall_step, impact_vz, land_s, land_land_s = prm[:5]
    i = idx.astype(np.int64)
    p, v = S.p, S.v
    if flat != 0.0:
        h = -S.home[i, 2]
        g = h.copy()
    else:
        h = _nearest(dsm, dsm_aff, p[i, 1], p[i, 0])
        g = _bilinear(dtm, dtm_aff, p[i, 1], p[i, 0])
    S.ground_z[i] = -h
    z_u = -p[i, 2]
    S.agl[i] = z_u - g
    mode = S.ctrl_mode[i]
    pin = (mode == K.M_IDLE) | (mode == K.M_SPOOLUP) | ((mode == K.M_TAKEOFF) & (S.ctrl_phase[i] == 0)) | \
        ((mode == K.M_KILLED) & S.landed[i])
    events: list[tuple[int, int]] = []
    pi = i[pin]
    if pi.size:
        p[pi, 2] = -h[pin]
        v[pi] = 0.0
        S.omega[pi] = 0.0
        lv = pi[S.crash_sub[pi] == 0]
        if lv.size:
            q = S.q
            qw, qx, qy, qz = q[lv, 0], q[lv, 1], q[lv, 2], q[lv, 3]
            psi = np.arctan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))
            q[lv, 0] = np.cos(psi / 2.0)
            q[lv, 1] = 0.0
            q[lv, 2] = 0.0
            q[lv, 3] = np.sin(psi / 2.0)
        S.in_contact[pi] = True
        S.landed[pi] = True
        S.in_air[pi] = False
        S.agl[pi] = h[pin] - g[pin]
        S.contact_t[pi] = np.nan
    a = ~pin
    ai, ha, za = i[a], h[a], z_u[a]
    if ai.size == 0:
        return _write_events(events, ev_out)
    pen = ha - za
    touching = pen > 0.0
    S.in_contact[ai[~touching]] = False
    crashed = np.zeros(ai.size, bool)
    if touching.any():
        ti = ai[touching]
        ht = ha[touching]
        h_prev = ht.copy() if flat != 0.0 else _nearest(dsm, dsm_aff, S.p_prev[ti, 1], S.p_prev[ti, 0])
        pre = S.crash_sub[ti] > 0  # 已坠毁（机间碰撞）后落地：停在地表，不再重复判定
        wall = ~pre & (pen[touching] > pen_m) & ((ht - h_prev) > wall_step)
        impact = ~pre & ~wall & (v[ti, 2] > impact_vz)
        di = ti[pre]
        if di.size:
            p[di, 2] = -ht[pre]
            v[di] = 0.0
            S.in_contact[di] = True
            S.landed[di] = True
            S.in_air[di] = False
            S.contact_t[di] = np.nan
        wi = ti[wall]
        if wi.size:
            p[wi, 0] = S.p_prev[wi, 0]
            p[wi, 1] = S.p_prev[wi, 1]
            p[wi, 2] = -h_prev[wall]
            S.ground_z[wi] = -h_prev[wall]
            S.crash_sub[wi] = KC.CRASH_COLLISION_WORLD
        ii = ti[impact]
        if ii.size:
            p[ii, 2] = -ht[impact]
            S.crash_sub[ii] = KC.CRASH_IMPACT
        ci = ti[wall | impact]
        if ci.size:
            events += [(int(s), KC.EV_WORLD if S.crash_sub[s] == KC.CRASH_COLLISION_WORLD else KC.EV_IMPACT) for s in ci]
            S.ctrl_mode[ci] = K.M_KILLED
            S.thrust[ci] = 0.0
            S.thr_sp[ci] = 0.0
            S.omega[ci] = 0.0
            S.mode_evt[ci] |= K.E_COLLISION
            S.in_contact[ci] = True
            S.landed[ci] = True
            S.in_air[ci] = False
            S.contact_t[ci] = np.nan
        ok = ~(wall | impact | pre)
        oi = ti[ok]
        if oi.size:
            p[oi, 2] = -ht[ok]
            v[oi, 2] = np.minimum(v[oi, 2], 0.0)
            S.in_contact[oi] = True
        crashed[np.flatnonzero(touching)[wall | impact | pre]] = True
    ri = ai[~crashed]
    rh = ha[~crashed]
    rz = za[~crashed]
    if ri.size:
        m = S.ctrl_mode[ri]
        v0, v1, v2 = v[ri, 0], v[ri, 1], v[ri, 2]
        land_cls = (m == K.M_LAND) | (m == K.M_ELAND) | (m == K.M_DESCENT) | ((m == K.M_RTL) & (rtl_sub[ri] == 3))
        w0, w1, w2 = S.omega[ri, 0], S.omega[ri, 1], S.omega[ri, 2]
        ic = S.in_contact[ri]
        still_l = ic & (np.sqrt(v0 * v0 + v1 * v1 + v2 * v2) < 0.25)
        still_g = ic & (np.abs(v2) < KC.LAND_VZ) & (np.sqrt(v0 * v0 + v1 * v1) < KC.LAND_VXY) & \
            (np.sqrt(w0 * w0 + w1 * w1 + w2 * w2) < KC.LAND_W) & (S.thrust[ri] < KC.LAND_THR * PT[S.profile_id[ri], 2])
        still = np.where(land_cls, still_l, still_g)
        need = np.where(land_cls, land_land_s, land_s)
        ct = S.contact_t[ri]
        ct = np.where(still, np.where(np.isnan(ct), t_s, ct), np.nan)
        done = still & (t_s - ct >= need - 1e-9)
        S.contact_t[ri] = np.where(done, np.nan, ct)
        di = ri[done]
        if di.size:
            S.landed[di] = True
            S.td_t[di] = np.nan
            v[di] = 0.0
            S.thr_sp[di] = 0.0
            nk = di[S.ctrl_mode[di] != K.M_KILLED]
            S.ctrl_mode[nk] = K.M_IDLE
            S.ctrl_phase[nk] = 0
            S.mode_t[nk] = t_s
            S.thrust[di] = 0.0
            S.thr_cap[di] = 1.0
            S.mode_evt[di] |= K.E_TOUCHDOWN
            events += [(int(s), KC.EV_TOUCHDOWN) for s in di]
        up = S.landed[ri] & ~S.in_contact[ri] & (rz - rh > 0.1)
        ui = ri[up]
        if ui.size:
            S.landed[ui] = False
            S.mode_evt[ui] |= K.E_LIFTOFF
            events += [(int(s), KC.EV_LIFTOFF) for s in ui]
        S.in_air[ri] = ~S.landed[ri]
    return _write_events(events, ev_out)


def _write_events(events: list[tuple[int, int]], ev_out: np.ndarray) -> int:
    events.sort()
    for k, (s, c) in enumerate(events):
        ev_out[k, 0] = s
        ev_out[k, 1] = c
    return len(events)


class ContactStage:
    def __init__(self, world: Any = None, cfg: ContactCfg | None = None, *, PT: np.ndarray | None = None,
                 kernel: str = "numba", capacity: int = 1024) -> None:
        self.world = world
        self.cfg = cfg or ContactCfg()
        self.prm = self.cfg.prm()
        self.PT = PT
        self.kernel = kernel if (kernel != "numba" or K.HAVE_NUMBA) else "numpy"
        self.ev = np.zeros((capacity, 2), np.int32)
        self.collider = UavCollider(capacity)
        self.uav_every = 5  # 机间碰撞 25 Hz（contact 每 5 次调用一次，D1-ext FR-037）
        # 按 tick 定相（偶数 tick 2m，m % 5 == UAV_PHASE，即 tick ≡ 6 mod 10，落在 50 Hz guard 的相位上）：此前按调用计数，
        # 相位取决于首次有机体的 tick（N = 1000 时恰与 env 同 tick，该类 tick 叠加约 0.5 ms），且计数不随 checkpoint 恢复
        # （恢复后相位改变）。ADR-065。uav_phase < 0 时不在本 stage 检查（sim-core 的 pipeline 改由 `collide` stage 在奇数
        # tick ≡ 9 mod 10 检查，ADR-070）
        self.uav_phase = 3
        self.n_calls = 0
        self.flat = 1.0
        g = np.zeros((1, 1), np.float32)
        aff = np.array([0.0, 0.0, 1.0])
        self.dsm, self.dsm_aff, self.dtm, self.dtm_aff = g, aff, g, aff
        if world is not None:
            d = world.dsm_grid()
            t = world.dtm_grid()
            self.dsm = np.asarray(d.a)
            self.dsm_aff = np.array([d.x0_m, d.y0_m, d.cell_m], np.float64)
            self.dtm = np.ascontiguousarray(t.a, np.float32)
            self.dtm_aff = np.array([t.x0_m, t.y0_m, t.cell_m], np.float64)
            self.flat = 0.0

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        c = getattr(S, "l1_tick_cache", None)
        if c is not None and c[0] == S.tick:
            idx, rs = c[1], c[2]
        else:
            idx, rs = l1_indices(S), None
        if idx.size == 0:
            return
        PT = self.PT if self.PT is not None else ctx.profiles.PT
        t_s = ctx.t_ns * 1e-9
        if rs is None:
            rs = rtl_phase_of(S)
        if self.kernel == "numba":
            nev = KC.contact(idx, t_s, S.p, S.v, S.p_prev, S.q, S.omega, S.thrust, S.thr_sp, S.thr_cap, S.home, self.dsm,
                             self.dsm_aff, self.dtm, self.dtm_aff, self.flat, S.ctrl_mode, S.ctrl_phase, S.mode_t, S.td_t,
                             S.profile_id, PT, S.in_contact, S.landed, S.in_air, S.contact_t, S.crash_sub, S.ground_z,
                             S.agl, S.mode_evt, rs, self.prm, self.ev)
        else:
            nev = contact_numpy(S, idx, t_s, self.dsm, self.dsm_aff, self.dtm, self.dtm_aff, self.flat, PT, rs, self.prm,
                                self.ev)
        S.touch()
        if nev and ctx.events is not None:
            self._emit(S, ctx, int(nev))
        self.n_calls += 1
        if self.uav_phase >= 0 and (int(ctx.tick) // 2) % self.uav_every == self.uav_phase and ctx.profiles is not None:
            self.check_uav(S, ctx, idx)

    def check_uav(self, S: FleetState, ctx: StageCtx, idx: np.ndarray) -> None:
        """机间碰撞检查与 `sim.contact.collision`（COLLISION_UAV）事件。"""
        pairs = self.collider.check(S, idx, ctx.profiles.collision_r)
        if pairs and ctx.events is not None:
            for a, b in pairs:
                for s_ in (a, b):
                    pos = [round(float(S.p[s_, 1]), 3), round(float(S.p[s_, 0]), 3), round(float(-S.p[s_, 2]), 3)]
                    ctx.events.emit("sim.contact.collision", t_sim_ns=ctx.t_ns, severity=3, uav=S.ids[s_],
                                    fields={"kind": "COLLISION_UAV", "pos_enu_m": pos,
                                            "speed_mps": round(float(np.linalg.norm(S.v[s_])), 3),
                                            "other": S.ids[b if s_ == a else a]})


    def _emit(self, S: FleetState, ctx: StageCtx, nev: int) -> None:
        for k in range(nev):
            s, code = int(self.ev[k, 0]), int(self.ev[k, 1])
            pos = [round(float(S.p[s, 1]), 3), round(float(S.p[s, 0]), 3), round(float(-S.p[s, 2]), 3)]
            if code in (KC.EV_WORLD, KC.EV_IMPACT):
                kind = "COLLISION_WORLD" if code == KC.EV_WORLD else "IMPACT"
                ctx.events.emit("sim.contact.collision", t_sim_ns=ctx.t_ns, severity=3, uav=S.ids[s],
                                fields={"kind": kind, "pos_enu_m": pos, "speed_mps": round(float(np.linalg.norm(S.v[s])), 3)})
            elif code == KC.EV_TOUCHDOWN:
                ctx.events.emit("sim.contact.touchdown", t_sim_ns=ctx.t_ns, severity=0, uav=S.ids[s], pos_enu_m=pos)


class CollideStage:
    """机间碰撞检查 stage（`collide`，every 10、phase 9，25 Hz；ADR-070）：在奇数 tick 检查，此前在 contact stage 内
    （tick ≡ 6 mod 10，偶数 tick），与 l1 组、contact、guard 同 tick，N = 1000 时该类 tick 叠加约 0.5 ms。奇数 tick 不积分，
    检查所见的位置与上一个偶数 tick 积分后的位置相同；后果（KILLED、推力 0）在下一个偶数 tick 生效。"""

    def __init__(self, contact: ContactStage) -> None:
        self.contact = contact
        contact.uav_phase = -1

    def __call__(self, S: FleetState, ctx: StageCtx) -> None:
        if ctx.profiles is None:
            return
        idx = l1_indices(S)
        if idx.size == 0:
            return
        self.contact.check_uav(S, ctx, idx)
