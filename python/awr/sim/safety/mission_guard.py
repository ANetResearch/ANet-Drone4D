"""mission_guard stage（10 Hz【仿真】，order 121，phase 13）：围栏、高度、净空、restricted、`d_free_fence_m`、LOC（ext）
（M09 §6.7.2、§6.7.3；FR-041 至 FR-043）。只写候选与条件位；`d_free_fence_m` 的唯一写者。

每机围栏条件状态机（§6.7.3）：INSIDE/NEAR（margin < 5 m 置 GEO_NEAR，≥ 7 m 持续 2 s 清除）→ BREACH（margin < 0 或进入
nofly：FLYING 时提出 CORRECTING/GEOFENCE，目标为回拉点）→ 余量 ≥ 1.5 m 且不在 nofly 内时恢复 FLYING/HOVER（恢复例外）；
越界深度 > 10 m 或回拉 > 15 s 时提出 RTL（AUTO，FAR_OUT / CORRECT_TIMEOUT）；回拉目标不合法时直接 RTL（NO_LEGAL_TARGET）。
高度：z > effective_max_z → CORRECTING/ALT_MAX（目标 max_z − 0.25）；`z − dsm < 0.5` → CORRECTING/ALT_MIN（目标 dsm + 0.75）；
TAKING_OFF、LANDING、RTL/FINAL、LANDED 豁免。进入 restricted 只告警。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from . import kernels as KN
from .flight_fsm import S_FLY_HOVER, Origin
from .state import COND, FS, SUBV

if TYPE_CHECKING:
    from .service import SafetyRuntime

__all__ = ["MissionGuard"]

S_COR_GEO = SUBV[(FS.CORRECTING, "GEOFENCE")]
S_COR_MAX = SUBV[(FS.CORRECTING, "ALT_MAX")]
S_COR_MIN = SUBV[(FS.CORRECTING, "ALT_MIN")]
S_FLY_VEL = SUBV[(FS.FLYING, "VELOCITY")]
GEO_INSIDE, GEO_NEAR, GEO_BREACH, GEO_RTL = 0, 1, 2, 3


class MissionGuard:
    def __init__(self, rt: SafetyRuntime) -> None:
        self.rt = rt

    def step(self, ctx: Any, shard: tuple[int, int] = (0, 1)) -> None:
        """10 Hz；分片（k, n）时只处理 slot % n == k 的机体（每机仍为 10 Hz，ADR-070）。"""
        rt = self.rt
        act = rt.act_idx
        k, n = shard
        if n > 1 and act.size:
            act = act[act % n == k]
        if act.size:
            self._wind_limit(act)
        geo = rt.geo
        if act.size == 0 or geo is None or not geo.valid:
            return
        self._fence(act, geo)

    def _wind_limit(self, act: np.ndarray) -> None:
        """ext：机体处平均风 > 机型 `wind_rating_mps` 时告警（SAF.ENV.WIND_LIMIT，边沿；环境服务不可用时跳过）。"""
        rt, S = self.rt, self.rt.S
        rating = rt.wind_rating
        if rating is None:
            return
        air = act[S.in_air[act]]
        w = rt.wind_at(air)
        if w is None or air.size == 0:
            return
        sp = np.hypot(w[:, 0], w[:, 1])
        lim = rating[S.profile_id[air]]
        over = np.isfinite(lim) & (sp > lim)
        was = (rt.sb["cond"][air] & np.uint64(1 << 18)) != 0
        new = air[over & ~was]
        if new.size:
            rt.set_cond(new, "WIND_LIMIT", True)
            rt.sink.add_many(new, rt.code("SAF.ENV.WIND_LIMIT"), rt.t_ns, values=sp[over & ~was])
        rt.set_cond(air[~over & was], "WIND_LIMIT", False)

    def _sync_cond(self, air: np.ndarray, name: str, want: np.ndarray) -> None:
        """条件位同步为 want：只对与当前位不同的机体调用 set_cond（与逐位 set True/False 等价，FX-SIM1）。"""
        rt = self.rt
        cur = (rt.sb["cond"][air] & np.uint64(1 << COND[name])) != 0
        diff = cur != want
        if diff.any():
            rt.set_cond(air[diff & want], name, True)
            rt.set_cond(air[diff & ~want], name, False)

    def _fence(self, act: np.ndarray, geo: Any) -> None:
        rt, S, sb = self.rt, self.rt.S, self.rt.sb
        P = rt.params.fence
        t = rt.t_ns
        t_s = t * 1e-9
        ground = act[~S.in_air[act]]
        if ground.size:
            for name in ("GEO_NEAR", "GEO_BREACH", "GEO_RESTRICTED", "ALT_MAX", "ALT_MIN"):
                rt.set_cond(ground, name, False)
            sb["d_free_fence_m"][ground] = np.inf
        air = act[S.in_air[act]]
        if air.size == 0:
            return
        pos = S.enu.pos
        o = geo.scan(pos, air)
        if KN.HAVE_NUMBA and getattr(geo, "use_numba", False):
            # 融合核先写四个状态字段并逐机判断是否有任何边沿、条件位变化、越界或 CORRECTING/Velocity：无事件的机体只需提交
            # near_ok_since 并清 Velocity 自由距离；有事件的机体（大机群稳态下没有或极少）走下面的原实现（逐机独立，其中的
            # 赋值与核相同，near_ok_since 未改动；事件按 slot 升序，与全量走原实现时的相对次序相同）
            dsm = geo.dsm(pos[air, :2])
            ns_new = np.empty(air.size)
            evf = np.empty(air.size, np.uint8)
            hits = KN.fence_core(np.ascontiguousarray(air, np.int64), o.margin, o.nofly, o.restr, o.near, o.near_d, pos, dsm,
                                 sb["fs"], sb["sub"], sb["cond"], sb["near_ok_since"], float(P.warn_margin_m),
                                 float(P.near_rearm_m), float(P.near_rearm_s), float(P.min_clear_m), float(geo.max_z),
                                 float(t_s), 0, 2, COND["GEO_BREACH"], COND["ALT_MAX"], COND["ALT_MIN"], int(FS.TAKING_OFF),
                                 int(FS.LANDING), int(FS.RTL), int(FS.LANDED), int(FS.FLYING), int(FS.CORRECTING),
                                 int(S_FLY_VEL), sb["geo_margin_m"], sb["zone_hit"], sb["zone_near"], sb["clearance_m"],
                                 ns_new, evf)
            if hits == 0:
                sb["near_ok_since"][air] = ns_new
                sb["d_free_fence_m"][act] = np.inf
                return
            calm = evf == 0
            if calm.any():
                sb["near_ok_since"][air[calm]] = ns_new[calm]
                sb["d_free_fence_m"][air[calm]] = np.inf
            air = air[~calm]
            act = air  # 下面只用 act 求 Velocity 之外的机体（d_free_fence_m = inf）：地面机体已在上面处理
        margin = o.margin[air]
        sb["geo_margin_m"][air] = margin
        nf = o.nofly[air]
        rz = o.restr[air]
        sb["zone_hit"][air] = np.where(nf >= 0, nf, rz)
        sb["zone_near"][air] = np.where(o.near_d[air] < P.warn_margin_m, o.near[air], -1)
        z = pos[air, 2]
        dsm = geo.dsm(pos[air, :2])
        clear = z - dsm
        sb["clearance_m"][air] = clear
        fs = sb["fs"][air]
        sub = sb["sub"][air]
        # ---- GEO_NEAR（迟滞 2 m、2 s）
        near = (margin >= 0) & (margin < P.warn_margin_m)
        was_near = (sb["cond"][air] & np.uint64(1 << 0)) != 0
        new_near = air[near & ~was_near]
        if new_near.size:
            rt.set_cond(new_near, "GEO_NEAR", True)
            rt.sink.add_many(new_near, rt.code("SAF.GEOFENCE.NEAR"), t, values=margin[near & ~was_near],
                             threshold=P.warn_margin_m)
        far = margin >= P.near_rearm_m
        ns = sb["near_ok_since"][air]
        ns = np.where(far & was_near, np.where(np.isnan(ns), t_s, ns), np.nan)
        sb["near_ok_since"][air] = ns
        clr = air[was_near & far & (t_s - np.nan_to_num(ns, nan=t_s) >= P.near_rearm_s)]
        if clr.size:
            rt.set_cond(clr, "GEO_NEAR", False)
        # ---- restricted（只告警，边沿）
        in_r = rz >= 0
        was_r = (sb["cond"][air] & np.uint64(1 << 2)) != 0
        er = air[in_r & ~was_r]
        if er.size:
            rt.set_cond(er, "GEO_RESTRICTED", True)
            for s in er:
                rt.sink.add(int(s), rt.code("SAF.GEOFENCE.RESTRICTED"), t, detail=geo.zone_names[int(o.restr[s])])
        lr = air[~in_r & was_r]
        if lr.size:
            rt.set_cond(lr, "GEO_RESTRICTED", False)
        # ---- 越界（BREACH）
        breach = (margin < 0) | (nf >= 0)
        exempt = (fs == FS.TAKING_OFF) | (fs == FS.LANDING) | ((fs == FS.RTL) & (sub == 3)) | (fs == FS.LANDED)
        altmax = (z > geo.max_z) & ~exempt
        altmin = (clear >= 0) & (clear < P.min_clear_m) & ~exempt
        for name, want in (("GEO_BREACH", breach), ("ALT_MAX", altmax), ("ALT_MIN", altmin)):
            self._sync_cond(air, name, want)
        fly = fs == FS.FLYING
        for k in np.flatnonzero(fly & breach):
            s = int(air[k])
            tgt = geo.pullback_target(pos[s])
            depth = -float(margin[k])
            detail = geo.zone_names[int(nf[k])] if nf[k] >= 0 else "BORDER"
            if tgt is None:
                rt.fsm.propose(np.array([s]), int(FS.RTL), 0, Origin.AUTO, "SAF.GEOFENCE.CORRECT_TIMEOUT",
                               value=depth, detail="NO_LEGAL_TARGET")
            elif depth > P.hard_out_m:
                rt.fsm.propose(np.array([s]), int(FS.RTL), 0, Origin.AUTO, "SAF.GEOFENCE.FAR_OUT", value=depth,
                               thr=P.hard_out_m, detail=detail)
            else:
                rt.fsm.propose(np.array([s]), int(FS.CORRECTING), S_COR_GEO, Origin.AUTO, "SAF.GEOFENCE.BREACH",
                               value=depth, thr=0.0, detail=detail, target=tgt[None])
        for k in np.flatnonzero(fly & ~breach & altmax):
            s = int(air[k])
            rt.fsm.propose(np.array([s]), int(FS.CORRECTING), S_COR_MAX, Origin.AUTO, "SAF.ALT.MAX", value=float(z[k]),
                           thr=float(geo.max_z), target=geo.alt_target(pos[s], "ALT_MAX")[None])
        for k in np.flatnonzero(fly & ~breach & ~altmax & altmin):
            s = int(air[k])
            rt.fsm.propose(np.array([s]), int(FS.CORRECTING), S_COR_MIN, Origin.AUTO, "SAF.ALT.MIN", value=float(clear[k]),
                           thr=P.min_clear_m, target=geo.alt_target(pos[s], "ALT_MIN")[None])
        # ---- CORRECTING：恢复、超时、越界过深
        cor = fs == FS.CORRECTING
        if cor.any():
            ci = air[cor]
            m_c = margin[cor]
            since = sb["correct_since_ns"][ci]
            restored = (m_c >= P.restore_margin_m) & (nf[cor] < 0) & (clear[cor] >= P.min_clear_m) & (z[cor] <= geo.max_z)
            if restored.any():
                rt.fsm.propose(ci[restored], int(FS.FLYING), S_FLY_HOVER, Origin.AUTO, "SAF.GEOFENCE.RESTORED",
                               value=m_c[restored])
            far = ~restored & (-m_c > P.hard_out_m)
            if far.any():
                rt.fsm.propose(ci[far], int(FS.RTL), 0, Origin.AUTO, "SAF.GEOFENCE.FAR_OUT", value=-m_c[far],
                               thr=P.hard_out_m)
            tout = ~restored & ~far & ((t - since) * 1e-9 > P.correct_timeout_s)
            if tout.any():
                rt.fsm.propose(ci[tout], int(FS.RTL), 0, Origin.AUTO, "SAF.GEOFENCE.CORRECT_TIMEOUT",
                               value=(t - since[tout]) * 1e-9, thr=P.correct_timeout_s)
        # ---- Velocity 方向距离（FR-043）
        vi = air[(fs == FS.FLYING) & (sub == S_FLY_VEL)]
        other = np.setdiff1d(act, vi, assume_unique=False) if vi.size else act  # 无 Velocity 机体时免去两次排序去重
        sb["d_free_fence_m"][other] = np.inf
        if vi.size:
            vel = S.enu.vel[vi]
            d = geo.free_distance(pos[vi], vel)
            # 10 Hz 刷新：扣除一个周期的水平位移，避免两次刷新之间距离陈旧（偏保守）
            d = d - np.hypot(vel[:, 0], vel[:, 1]) * 0.1
            sb["d_free_fence_m"][vi] = np.where(np.isfinite(d), np.maximum(d, 1e-3), np.inf)
