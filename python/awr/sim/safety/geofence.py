"""GeofenceModel、回拉目标、STOP_MOTION 折线与准入第⑧步（M09 §6.6.3、§6.7；FR-032、FR-033、FR-040 至 FR-043）。

几何权威是 M04 WorldQuery / ZoneIndex（`zones.border`、`nofly`、`restricted`、`path_coarse_check`、`height_dsm`、
`clearance`）；本模块只做组合：
- 把 ZoneIndex 的棱柱展开为 numba 核的边数组（border 与各 zone 的全部环，奇偶规则下洞环自然抵消）；
- `effective_max_z = min(border.max_z_m, 机型最大高度（profile 有该字段时）, scenario.safety.max_z_m)`；
- 回拉目标（§6.7.4）：border 越界取最近边的**内法向**、进入 nofly 取**外法向**，内移 2 m；nofly 顶部更近时越顶；高度钳制；
  目标不合法返回 None（调用方提出 RTL，detail = NO_LEGAL_TARGET）；
- 第⑧步：折线 `[p, p_stop, goal]`（goto 取 M08 已构造的折线，其第 2 点即 `FleetSim.p_stop`）、follow_path、orbit 外接 16 边形、
  land{pos}；先查 ABOVE_MAX_Z，再调用 M04 `path_coarse_check(buffer 1 m, goal_clear 2 m, active)`，原因原样映射为 102 的 detail；
  含 MAYBE 或 `zone_deferred` 时 `needs_fine`（CommandEngine 交 plan-pool 细校验，调用停在 accepted）。
坐标一律 World ENU。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from awr.contracts.reasons import Reason
from awr.sim.core.admission import AdmitResult
from awr.sim.fleet import kernels_l1 as KL

from . import kernels as KN
from .params import FenceParams

__all__ = ["REMEDY", "GeofenceModel", "stop_point_enu"]

REMEDY = {
    "OUT_OF_BORDER": "目标越出世界边界，请在边界内重新选点",
    "GOAL_IN_ZONE": "目标位于禁飞区 {zone}，请更换目标或改用安全转场",
    "PATH_CROSSES_ZONE": "航线穿越禁飞区 {zone}，请改用安全转场或调整航点",
    "GOAL_IN_OBSTACLE": "目标点低于建筑或地面净空，请抬高目标高度",
    "ABOVE_MAX_Z": "目标高于允许的最大高度 {max_z} m，请降低目标高度或改用安全转场",
    "PATH_OBSTACLE": "航线可能穿越建筑，请改用安全转场（route = safe_transit）",
}


def stop_point_enu(S: Any, LT: np.ndarray, slots: np.ndarray) -> np.ndarray:
    """STOP_MOTION 刹停点（ENU）：`p + unit(v)·(v_h²/(2a) + v_h·a/(2j))`，与 M08 `setpoint.p_stop` 同一公式（ENU 视图计算）。"""
    s = np.asarray(slots, np.int64)
    v = S.enu.vel[s]
    vh = np.hypot(v[:, 0], v[:, 1])
    a = LT[S.limits_id[s], KL.L_ACC]
    d = vh * vh / (2.0 * a) + vh * a / (2.0 * KL.JERK)
    nv = np.linalg.norm(v, axis=1)
    u = np.where(nv[:, None] > 1e-6, v / np.maximum(nv, 1e-9)[:, None], 0.0)
    return S.enu.pos[s] + u * d[:, None]


def _edges(polys: list[list[np.ndarray]]) -> tuple[np.ndarray, np.ndarray]:
    ea, eb = [], []
    for poly in polys:
        for ring in poly:
            r = np.asarray(ring, np.float64)[:, :2]
            ea.append(r)
            eb.append(np.roll(r, -1, axis=0))
    if not ea:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.concatenate(ea), np.concatenate(eb)


@dataclass
class ScanOut:
    margin: np.ndarray
    border: np.ndarray
    nofly: np.ndarray
    restr: np.ndarray
    near: np.ndarray
    near_d: np.ndarray


class GeofenceModel:
    """由 M04 ZoneIndex 构造；`world = None`（平坦地面、未加载世界）时 `valid = False`，围栏检查全部跳过。"""

    def __init__(self, world: Any, params: FenceParams, *, active_zone_ids: Sequence[str] | None = None,
                 profile_max_z: float | None = None, cap_m: float = 50.0, use_numba: bool | None = None) -> None:
        self.world = world
        self.P = params
        self.cap_m = float(cap_m)
        self.use_numba = KN.HAVE_NUMBA if use_numba is None else bool(use_numba and KN.HAVE_NUMBA)
        zones = getattr(world, "zones", None) if world is not None else None
        self.valid = zones is not None
        self.zones = zones
        self.zone_names: list[str] = []
        if not self.valid:
            self.border_zmax = math.inf
            self.border_zmin = -math.inf
            self.max_z = math.inf
            return
        b = zones.border
        self.bea, self.beb = _edges(b.polygons)
        self.border_zmax = float(b.zmax)
        self.border_zmin = float(b.zmin)
        prisms = list(zones.nofly) + list(zones.restricted)
        self.prisms = prisms
        self.zone_names = [p.zone_id for p in prisms]
        ea, eb, st, ct = [], [], [], []
        off = 0
        for p in prisms:
            a, c = _edges(p.polygons)
            ea.append(a)
            eb.append(c)
            st.append(off)
            ct.append(len(a))
            off += len(a)
        self.zea = np.concatenate(ea) if ea else np.zeros((0, 2))
        self.zeb = np.concatenate(eb) if eb else np.zeros((0, 2))
        self.zstart = np.asarray(st, np.int64)
        self.zcount = np.asarray(ct, np.int64)
        self.zmin = np.asarray([p.zmin for p in prisms], np.float64)
        self.zmax = np.asarray([p.zmax for p in prisms], np.float64)
        self.zbbox = np.asarray([p.bbox for p in prisms], np.float64).reshape(-1, 4)
        self.zkind = np.asarray([1 if p.kind == "nofly" else 2 for p in prisms], np.int64)
        self.set_active(active_zone_ids)
        caps = [self.border_zmax]
        if profile_max_z is not None:
            caps.append(float(profile_max_z))
        if params.max_z_m is not None:
            caps.append(float(params.max_z_m))
        self.max_z = min(caps)
        n = 1024
        self.out = ScanOut(np.full(n, np.inf), np.full(n, np.inf), np.full(n, -1, np.int64), np.full(n, -1, np.int64),
                           np.full(n, -1, np.int64), np.full(n, np.inf))

    def set_active(self, zone_ids: Sequence[str] | None) -> None:
        """剧本 `zones.active`（border 恒生效）；None 为全部 nofly 与 restricted。"""
        self.active_ids = None if zone_ids is None else list(zone_ids)
        if not self.valid:
            return
        ids = None if zone_ids is None else set(zone_ids)
        self.zactive = np.asarray([ids is None or p.zone_id in ids for p in self.prisms], np.bool_)

    # ---------------------------------------------------------------- 运行期扫描
    def scan(self, pos: np.ndarray, idx: np.ndarray) -> ScanOut:
        o = self.out
        if pos.shape[0] > o.margin.shape[0]:
            n = pos.shape[0]
            self.out = o = ScanOut(np.full(n, np.inf), np.full(n, np.inf), np.full(n, -1, np.int64),
                                   np.full(n, -1, np.int64), np.full(n, -1, np.int64), np.full(n, np.inf))
        fn = KN.geofence_scan if self.use_numba else KN.geofence_scan_np
        fn(np.ascontiguousarray(pos, np.float64), np.asarray(idx, np.int64), self.bea, self.beb, self.zea, self.zeb,
           self.zstart, self.zcount, self.zmin, self.zmax, self.zbbox, self.zkind, self.zactive, self.cap_m,
           o.margin, o.border, o.nofly, o.restr, o.near, o.near_d)
        return o

    def point_status(self, xyz: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(在 border 内, 在任一生效 nofly 内, 边界余量)；供预检与目标合法性检查（numpy oracle 路径）。"""
        P = np.asarray(xyz, np.float64).reshape(-1, 3)
        n = len(P)
        if not self.valid:
            return np.ones(n, np.bool_), np.zeros(n, np.bool_), np.full(n, np.inf)
        o = ScanOut(np.full(n, np.inf), np.full(n, np.inf), np.full(n, -1, np.int64), np.full(n, -1, np.int64),
                    np.full(n, -1, np.int64), np.full(n, np.inf))
        KN.geofence_scan_np(P, np.arange(n), self.bea, self.beb, self.zea, self.zeb, self.zstart, self.zcount, self.zmin,
                            self.zmax, self.zbbox, self.zkind, self.zactive, self.cap_m, o.margin, o.border, o.nofly,
                            o.restr, o.near, o.near_d)
        return o.border >= 0, o.nofly >= 0, o.margin

    def dsm(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if self.world is None:
            return np.zeros(len(xy))
        return np.asarray(self.world.height_dsm(xy), np.float64)

    # ---------------------------------------------------------------- 回拉目标（§6.7.4）
    def pullback_target(self, p: np.ndarray) -> np.ndarray | None:
        if not self.valid:
            return None
        p = np.asarray(p, np.float64)
        inset = self.P.inset_m
        inb, innf, _ = self.point_status(p[None])
        tgt = p.copy()
        if not inb[0]:
            q, n = self._nearest_edge(p[:2], self.bea, self.beb)
            if not self._contains_border(q + 0.01 * n):
                n = -n
            tgt[:2] = q + inset * n
        elif innf[0]:
            k = self._zone_at(p)
            if k < 0:
                return None
            s0, c = int(self.zstart[k]), int(self.zcount[k])
            q, n = self._nearest_edge(p[:2], self.zea[s0:s0 + c], self.zeb[s0:s0 + c])
            if self._in_zone_xy(k, q + 0.01 * n):
                n = -n
            dh = float(np.linalg.norm(q - p[:2]))
            if math.isfinite(self.zmax[k]) and self.zmax[k] - p[2] < dh:
                tgt[2] = float(self.zmax[k]) + inset
            else:
                tgt[:2] = q + inset * n
        dsm = float(self.dsm(tgt[None, :2])[0])
        lo = max(self.border_zmin + 1.0 if math.isfinite(self.border_zmin) else -math.inf, dsm + self.P.target_clear_m)
        hi = self.max_z - 1.0
        tgt[2] = min(max(tgt[2], lo), hi)
        if tgt[2] < dsm + self.P.target_clear_m:
            return None
        inb2, innf2, _ = self.point_status(tgt[None])
        if not inb2[0] or innf2[0]:
            return None
        return tgt

    def alt_target(self, p: np.ndarray, kind: str) -> np.ndarray:
        tgt = np.asarray(p, np.float64).copy()
        if kind == "ALT_MAX":
            tgt[2] = self.max_z - self.P.max_z_offset_m
        else:
            tgt[2] = float(self.dsm(tgt[None, :2])[0]) + self.P.min_clear_target_m
        return tgt

    def _contains_border(self, xy: np.ndarray) -> bool:
        ins, _ = KN._poly_dist_np(np.asarray(xy, np.float64)[None], self.bea, self.beb)
        return bool(ins[0])

    def _in_zone_xy(self, k: int, xy: np.ndarray) -> bool:
        s0, c = int(self.zstart[k]), int(self.zcount[k])
        ins, _ = KN._poly_dist_np(np.asarray(xy, np.float64)[None], self.zea[s0:s0 + c], self.zeb[s0:s0 + c])
        return bool(ins[0])

    def _zone_at(self, p: np.ndarray) -> int:
        for k in range(len(self.prisms)):
            if self.zkind[k] == 1 and self.zactive[k] and self.zmin[k] <= p[2] <= self.zmax[k] and self._in_zone_xy(k, p[:2]):
                return k
        return -1

    @staticmethod
    def _nearest_edge(xy: np.ndarray, ea: np.ndarray, eb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        d = eb - ea
        L2 = (d * d).sum(1)
        t = np.clip(((xy - ea) * d).sum(1) / np.where(L2 > 0, L2, 1.0), 0.0, 1.0)
        q = ea + d * t[:, None]
        dist = np.linalg.norm(q - xy, axis=1)
        k = int(np.argmin(dist))
        e = d[k] / max(math.sqrt(float(L2[k])), 1e-12)
        n = np.array([-e[1], e[0]])  # 左法向；调用方按包含关系定向
        return q[k], n

    # ---------------------------------------------------------------- Velocity 方向距离（FR-043）
    def free_distance(self, pos: np.ndarray, vel: np.ndarray) -> np.ndarray:
        """沿水平速度方向到 border 出口或 nofly 入口的距离（射线对多边形边求交）；无约束为 +inf。"""
        P = np.asarray(pos, np.float64).reshape(-1, 3)
        V = np.asarray(vel, np.float64).reshape(-1, 3)
        out = np.full(len(P), np.inf)
        if not self.valid or len(P) == 0:
            return out
        sp = np.hypot(V[:, 0], V[:, 1])
        mv = sp > 1e-3
        if not mv.any():
            return out
        u = np.zeros((len(P), 2))
        u[mv] = V[mv, :2] / sp[mv, None]
        ea = np.concatenate([self.bea, self.zea[self._active_edge_mask()]]) if len(self.zea) else self.bea
        eb = np.concatenate([self.beb, self.zeb[self._active_edge_mask()]]) if len(self.zeb) else self.beb
        o = P[:, None, :2]
        r = u[:, None, :]
        s = (eb - ea)[None]
        rxs = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
        qp = ea[None] - o
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qp[..., 0] * s[..., 1] - qp[..., 1] * s[..., 0]) / rxs
            w = (qp[..., 0] * r[..., 1] - qp[..., 1] * r[..., 0]) / rxs
        hit = (rxs != 0) & (t > 1e-9) & (w >= 0) & (w <= 1)
        t = np.where(hit, t, np.inf)
        out[mv] = t.min(1)[mv]
        return out

    def _active_edge_mask(self) -> np.ndarray:
        m = np.zeros(len(self.zea), np.bool_)
        for k in range(len(self.prisms)):
            if self.zactive[k] and self.zkind[k] == 1:
                s0, c = int(self.zstart[k]), int(self.zcount[k])
                m[s0:s0 + c] = True
        return m

    # ---------------------------------------------------------------- 准入第⑧步（FR-032）
    def polyline(self, S: Any, LT: np.ndarray, op: str, args: dict, slot: int, m08_poly: np.ndarray | None) -> np.ndarray | None:
        p = S.enu.pos[slot].copy()
        if op == "goto":
            if m08_poly is not None and len(m08_poly) >= 3:
                return np.asarray(m08_poly, np.float64)
            ps = stop_point_enu(S, LT, np.array([slot]))[0]
            return np.array([p, ps, args["pos"]], np.float64)
        ps = stop_point_enu(S, LT, np.array([slot]))[0]
        if op == "follow_path" and isinstance(args.get("waypoints"), list):
            return np.vstack([p[None], ps[None], np.asarray(args["waypoints"], np.float64)])
        if op == "orbit" and args.get("center") is not None and args.get("radius_m") is not None:
            c = np.asarray(args["center"], np.float64)
            n = int(self.P.orbit_poly_segments)
            R = (float(args["radius_m"]) + 1.0) / math.cos(math.pi / n)  # 外接正多边形
            th = np.arange(n + 1) * 2.0 * math.pi / n
            ring = np.stack([c[0] + R * np.cos(th), c[1] + R * np.sin(th), np.full(n + 1, c[2])], 1)
            d0 = p[:2] - c[:2]
            nd = float(np.linalg.norm(d0))
            entry = c.copy() if nd < 1e-6 else np.array([c[0] + d0[0] / nd * float(args["radius_m"]),
                                                          c[1] + d0[1] / nd * float(args["radius_m"]), c[2]])
            return np.vstack([p[None], ps[None], entry[None], ring])
        if op == "land":
            at = args.get("at", "here")
            if isinstance(at, dict) and at.get("pos") is not None:
                g = np.asarray(at["pos"], np.float64)
                return np.array([p, ps, [g[0], g[1], p[2]]], np.float64)
            if at == "home":
                h = S.enu.home[slot]
                return np.array([p, ps, [h[0], h[1], p[2]]], np.float64)
        return None

    def admit(self, poly: np.ndarray | None, op: str) -> AdmitResult:
        if poly is None or not self.valid:
            return AdmitResult()
        viol: list[tuple[str, int]] = []
        if float(np.max(poly[:, 2])) > self.max_z:
            viol.append(("ABOVE_MAX_Z", int(np.argmax(poly[:, 2]))))
        try:
            r = self._coarse_chunked(poly)
        except Exception as e:  # 折线越界（长度等）由第⑥步把关；此处按违例处理
            return AdmitResult(int(Reason.GEOFENCE_REJECT), {"why": "PATH_INVALID", "message": str(e)[:120]})
        viol += list(r.reasons)
        if viol:
            why, seg = viol[0]
            zone = self._zone_of_reason(why, poly, seg)
            det = {"why": why, "seg": int(seg), "zone": zone, "max_z_m": round(self.max_z, 2),
                   "remedy": REMEDY.get(why, "").format(zone=zone or "", max_z=round(self.max_z, 1)),
                   "extra": [w for w, _ in viol[1:5]]}
            return AdmitResult(int(Reason.GEOFENCE_REJECT), det)
        needs = (not bool(r.all_proven)) or bool(np.asarray(r.zone_deferred).any())
        return AdmitResult(0, None, needs_fine=needs)

    def _coarse_chunked(self, poly: np.ndarray) -> Any:
        """M04 `path_coarse_check` 单次最多 1000 个顶点（path.MAX_VERTICES）；准入折线 = 当前位置 + 刹停点 + 命令航点，
        1000 个航点的 follow_path（M10 螺旋扫描）因此有 1002 个顶点。按相邻窗口重叠 1 个顶点分段检查、合并结论
        （航段下标换算回整条折线；终点净空只对最后一段判定）。INT-1：修复 S1 下段螺旋被 102 PATH_INVALID 反复拒绝。"""
        from types import SimpleNamespace

        from awr.world.geometry.path import MAX_VERTICES

        n = len(poly)
        if n <= MAX_VERTICES:
            return self.world.path_coarse_check(poly, buffer_m=self.P.path_buffer_m, goal_clear_m=self.P.goal_clear_m,
                                                active_zone_ids=self.active_ids)
        reasons: list[tuple[str, int]] = []
        deferred: list[np.ndarray] = []
        proven = True
        step = MAX_VERTICES - 1
        for s0 in range(0, n - 1, step):
            last = s0 + step >= n - 1
            part = poly[s0:min(s0 + MAX_VERTICES, n)]
            r = self.world.path_coarse_check(part, buffer_m=self.P.path_buffer_m,
                                             goal_clear_m=self.P.goal_clear_m if last else -1e9,
                                             active_zone_ids=self.active_ids)
            reasons += [(why, int(seg) + s0) for why, seg in r.reasons]
            deferred.append(np.asarray(r.zone_deferred, np.bool_))
            proven = proven and bool(r.all_proven)
        return SimpleNamespace(reasons=reasons, zone_deferred=np.concatenate(deferred) if deferred else np.zeros(0, np.bool_),
                               all_proven=proven and not reasons)

    def _zone_of_reason(self, why: str, poly: np.ndarray, seg: int) -> str | None:
        if why not in ("GOAL_IN_ZONE", "PATH_CROSSES_ZONE") or not self.valid:
            return None
        a, b = poly[max(seg, 0)], poly[min(seg + 1, len(poly) - 1)]
        for k, pz in enumerate(self.prisms):
            if pz.kind != "nofly" or not self.zactive[k]:
                continue
            if bool(pz.segments_cross(a[None], b[None])[0]) or bool(pz.contains(poly[-1:])[0]):
                return pz.zone_id
        return None
