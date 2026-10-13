"""七类任务的分解（M20-FR-010 至 FR-025；M20 §6.5、§6.6；AWR-04 §8.1；17 §17.6.7 的参数与缺省）与 `register_decomposer`（M20-FR-083）。

| 类型 | 工作项 |
|---|---|
| point_watch | 1 个 `station`（驻留 dwell_s；null 为持续性，按班次接替） |
| polyline_patrol | 航线（auto_detour 时禁入圆外绕行；航点在禁入圆内 583）；once/pingpong/laps 按单架次可飞长度切成 `segment`；duration_s 为持续性 `segment`（循环） |
| area_patrol | g_req = 俯视关键维度 / N_req → 反推航高与条带（不补洞）；n = ceil(T_cov / T_rev)；均衡切成 n 个持续性 `subregion`；禁入洞的看洞点作为子区周期起点的停留航点 |
| area_scan_gsd | `scan.plan_scan`：每组一个 `strip_group`（同组不同机，截止 T_max）；多架次时为同一机体的链式 `strip_group` |
| perimeter_patrol | 偏移环（禁入圆外绕行）L；n = ceil(L / (v·T_rev))；n 个等相位持续性 `perimeter_phase` |
| relay_watch | 由运行时的接力控制器生成站位工作项（本函数只校验参数） |
| area_guard | 单机能在 T_rev 内完成"巡边一圈 + 区域一遍"时为 1 个持续性 `guard_cycle`，否则拆成 area_patrol 与 perimeter_patrol 两个子任务 |

几何一律 world ENU（m）；高度：AGL 相对 DTM，并保证航段走廊内 DSM 净空 ≥ 10 m（超过 H_cap 时给出 warning `h_cap`）。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .geom import centroid, cut_polyline, offset_polygon, point_at, polyline_length, ring, signed_area
from .keepout import circles, inside, plan_path, plan_route, seg_blocked
from .model import PRIORITY_OF, RELAY_PRIORITY, Camera, Snapshot, TaskingError, WorkItem
from .scan import A_MPS2, ScanSeg, ScanVehicle, build_raster, plan_scan, reverse_gsd, seq_time_s, split_segs, strips_for

__all__ = ["DEFAULTS", "N_REQ_PX", "DecompCtx", "class_top_dim", "decompose", "g_req_m", "register_decomposer"]

N_REQ_PX = {"D": 3.5, "R": 14.0, "I": 22.4}
H_CAP_M = 120.0
CLEAR_PATH_M = 10.0
LOOK_SEP_M = 40.0         # 不同任务看洞点之间的最小水平间距
LAYER_SEP_M = 10.0        # 航线类工作项与其他任务（范围重叠的）航线的作业高度差下限：不足时本任务整体降低该值
LAYER_XY_PAD_M = 30.0     # 航线范围（外接矩形）重叠判定的外扩
LAYER_AGL_MIN_M = 40.0    # 降低后的最低作业 AGL（不满足时不降）
LOOK_IN_DWELL_S = 15.0          # t_hold（1 s 捕获建立）+ 云台转向与变焦（约 4 s）+ 10 s（M20 §6.4 第 4 条；D2-FX-R2 由 11 s 调整）
LOOK_IN_DWELL_FW_S = 40.0       # 固定翼与垂起机：以看洞点为心盘旋约一圈
LOOK_RING_M = (15.0, 35.0, 60.0)  # 看洞站位：洞边界外 0–60 m 环带（M20 §6.4 第 4 条）
LOOK_AGL_M = (60.0, 80.0, 100.0, 120.0)
LOOK_BEARING_DEG = 15.0
LOOK_RAY_FRAC = (0.95, 0.5, 0.05)  # 与 M18 共享视线缓存同一端点
LOOK_TARGET_H_M = 1.75
LOOK_LOS_MAX = 192              # 每个洞最多复核的候选（三射线）
FW_FLAT_TYPES = ("area_patrol", "perimeter_patrol", "area_guard", "polyline_patrol")   # 固定翼恒定高度的巡查类（GSD 扫描除外）
DETOUR_STEP_M = 5.0             # 巡边环与禁入圆求交的加密步长
DEFAULTS: dict[str, dict] = {
    "point_watch": {"alt_m": 60.0, "alt_ref": "agl", "dwell_s": 300.0, "loiter_radius_m": 80.0},
    "polyline_patrol": {"mode": "loop", "laps": 3, "alt_m": 60.0, "sensor_facing": "down", "auto_detour": True},
    "area_patrol": {"n_vehicles": "auto", "target_class": "person", "level_req": "R", "revisit_s": 300.0, "duration_s": 1800.0,
                    "theta_max_deg": 15.0, "sidelap": 0.1},
    "area_scan_gsd": {"gsd_cm": 3.0, "sidelap": 0.1, "frontlap": 0.1, "theta_max_deg": 15.0, "t_max_s": None, "h_cap_m": 120.0,
                      "clearance_m": 20.0, "allow_multi_sortie": False, "n_vehicles": "auto", "strategy": "strips"},
    "perimeter_patrol": {"offset_m": 0.0, "direction": "ccw", "revisit_s": 300.0, "n_vehicles": "auto", "alt_m": 60.0},
    "relay_watch": {"level_req": None, "duration_s": None},
    "area_guard": {"target_class": "person", "level_req": "R", "revisit_s": 300.0, "perimeter_offset_m": 20.0},
}
_TOP_DIM_FALLBACK = {"person": 0.387, "dog": 0.52, "vehicle": 2.846, "uav": 0.35, "machine": 0.837, "object": 0.49}
_TOP_DIM: dict[str, float] | None = None


def class_top_dim(cls: str) -> float:
    """俯视关键维度 √(l·w)（契约 `target/class_defaults.json`；人 0.39 m）。"""
    global _TOP_DIM
    if _TOP_DIM is None:
        try:
            from awr.contracts._paths import contracts_root

            doc = json.loads((contracts_root() / "target" / "class_defaults.json").read_text(encoding="utf-8"))
            _TOP_DIM = {k: math.sqrt(float(v["size_m"]["l"]) * float(v["size_m"]["w"])) for k, v in doc["classes"].items()}
        except Exception:
            _TOP_DIM = dict(_TOP_DIM_FALLBACK)
    return float(_TOP_DIM.get(cls, _TOP_DIM_FALLBACK.get(cls, 0.5)))


def g_req_m(cls: str, level: str) -> float:
    """所需 GSD = 俯视关键维度 / N_req，向下取整到 mm（人 R 级 0.39 / 14 → 2.7 cm，AWR-04 §8.1）。"""
    g = class_top_dim(cls) / N_REQ_PX.get(level, 14.0)
    return math.floor(g * 1000.0) / 1000.0


@dataclass
class DecompCtx:
    snap: Snapshot
    height_dsm: Callable[[np.ndarray], np.ndarray]
    ground_dtm: Callable[[np.ndarray], np.ndarray]
    tid: str
    now: float = 0.0
    priority: int = 5
    allowed_nests: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    los_fn: Callable | None = None          # (A N×3, B N×3) -> bool[N]（M04 los_batch；没有世界时 None）
    free_fn: Callable | None = None         # xyz -> bool[N]（世界边界内、禁飞区外）
    border_fn: Callable | None = None       # (xy, inset_m) -> bool[N]（离世界边界 ≥ inset 且不在禁飞区，绕行顶点用）
    target_classes: dict = field(default_factory=dict)   # 识别物 → 类别（看洞只给任务目标类别的洞）
    avoid_pts: list = field(default_factory=list)        # 其他任务的看洞点与接力站位 [(x, y)]（看洞点水平间距 ≥ LOOK_SEP_M）
    avoid_layers: list = field(default_factory=list)     # 其他任务的航线层 [(x0, y0, x1, y1, z_med)]（作业高度差 ≥ LAYER_SEP_M）

    @property
    def C(self) -> np.ndarray:
        return circles(self.snap.keepouts)

    def fw_only(self) -> bool:
        ms = self.models_for()
        return bool(ms) and all(m.frame != "multirotor" for m in ms)

    def r_turn(self) -> float:
        """固定翼与垂起机的转弯半径（全部可用机型都是固定翼时的规划外扩量），否则 0。按作业高度的风（快照 `wind_hi_mps`，机巢上方
        120 m AGL，再加 1 m/s 余量；缺失时取机巢 10 m 风）由设计风 10 m/s 的盘旋半径下限换算：`R ∝ (V_a + w)²`（AWR-04 §5.3，V_a
        取盘旋空速约 15 m/s），再留 20% 余量，下限 80 m（M21 `r_loiter_default`，与 `Executor.fw_loiter_r` 同一口径；D2-FX-R2：此前取
        10 m 风、下限 40 m，执行时的盘旋圈比规划大 15–45 m，看洞盘旋反复被守卫撤离）。"""
        if not self.fw_only():
            return 0.0
        hi = self.snap.wind_hi_mps
        w = max(float(self.snap.wind_mps or 0.0), 0.0 if hi is None else float(hi) + 1.0, 3.0)
        r = max(float(m.r_loiter_min_m or 0.0) for m in self.models_for())
        return max(80.0, 1.2 * r * ((15.0 + w) / 25.0) ** 2)

    def node_ok(self, inset_m: float | None = None) -> Callable | None:
        """绕行顶点的可用判据（世界边界内缩 inset，缺省 30 m，固定翼再加转弯半径；禁飞区外）。没有世界时 None。"""
        if self.border_fn is None:
            return None
        ins = float(inset_m if inset_m is not None else 30.0 + 0.5 * self.r_turn())
        return lambda xy: self.border_fn(np.asarray(xy, float)[:, :2], ins)

    def plan_path(self, a, b, C: np.ndarray | None = None) -> np.ndarray | None:
        return plan_path(a, b, self.C if C is None else C, node_ok=self.node_ok())

    def plan_route(self, pts, C: np.ndarray | None = None, *, closed: bool = False) -> np.ndarray | None:
        return plan_route(pts, self.C if C is None else C, closed=closed, node_ok=self.node_ok())

    def dtm(self, xy) -> np.ndarray:
        return np.asarray(self.ground_dtm(np.atleast_2d(np.asarray(xy, float))[:, :2]), float)

    def dsm(self, xy) -> np.ndarray:
        return np.asarray(self.height_dsm(np.atleast_2d(np.asarray(xy, float))[:, :2]), float)

    def models_for(self) -> list:
        """可用机型：快照中有机体的机型（没有机体时为全部机型），按允许清单过滤。"""
        have = {v.model_id for v in self.snap.vehicles if not self.allowed_nests or v.nest_id in self.allowed_nests}
        ms = [m for k, m in sorted(self.snap.models.items())
              if (not self.allowed_models or k in self.allowed_models) and (not have or k in have)]
        return ms

    def ref_model(self):
        ms = self.models_for()
        if not ms:
            raise TaskingError(582, "no_models")
        return max(ms, key=lambda m: (m.endurance_s("cruise"), m.id))

    def cams(self) -> list[Camera]:
        out: dict[str, Camera] = {}
        for m in self.models_for():
            for c in m.cams:
                if c.kind in ("eo", "nir"):
                    out.setdefault(c.name, c)
        return list(out.values())

    def scan_vehicles(self, cam_name: str | None = None) -> list[ScanVehicle]:
        out = []
        for v in self.snap.vehicles:
            if v.state not in ("ready", "standby", "charging"):
                continue
            m = self.snap.models.get(v.model_id)
            if m is None or (self.allowed_models and m.id not in self.allowed_models):
                continue
            if self.allowed_nests and v.nest_id not in self.allowed_nests:
                continue
            if cam_name is not None and not any(c.name == cam_name for c in m.cams):
                continue
            n = self.snap.nests.get(v.nest_id) if v.nest_id else None
            out.append(ScanVehicle(v.id, m, v.nest_id, tuple(n.pos) if n is not None else tuple(v.pos), float(v.t_ready_s),  # type: ignore[arg-type]
                                   float(v.e_avail_wh), float(n.launch_interval_s) if n is not None else 30.0,
                                   float(n.turnaround_s + n.swap_s) if n is not None else 240.0))
        return out

    def path_z(self, P2: np.ndarray, alt_m: float, alt_ref: str = "agl") -> np.ndarray:
        """折线顶点高度：AGL（相对 DTM）且相邻航段走廊（±5 m）内 DSM 净空 ≥ 10 m；绝对高度时原样。"""
        P2 = np.asarray(P2, float)[:, :2]
        if alt_ref == "abs":
            return np.full(len(P2), float(alt_m))
        z = self.dtm(P2) + float(alt_m)
        for i in range(len(P2) - 1):
            a, b = P2[i], P2[i + 1]
            L = float(np.hypot(*(b - a)))
            n = max(2, int(L / 5.0) + 1)
            u = (b - a) / max(L, 1e-9)
            nrm = np.array([-u[1], u[0]])
            pts = np.vstack([a + (b - a) * t + nrm * o for t in np.linspace(0, 1, n) for o in (-5.0, 0.0, 5.0)])
            top = float(self.dsm(pts).max()) + CLEAR_PATH_M
            z[i] = max(z[i], top)
            z[i + 1] = max(z[i + 1], top)
        cap = self.dtm(P2) + H_CAP_M
        if (z > cap + 1e-6).any() and "h_cap" not in self.warnings:
            self.warnings.append("h_cap")
        return z


_DECOMPOSERS: dict[str, Callable[[dict, DecompCtx], dict]] = {}


def register_decomposer(task_type: str, fn: Callable[[dict, DecompCtx], dict]) -> None:
    _DECOMPOSERS[task_type] = fn


def _params(spec: dict) -> dict:
    t = str(spec.get("type"))
    return {**DEFAULTS.get(t, {}), **{k: v for k, v in (spec.get("params") or {}).items() if v is not None or k in ("dwell_s", "duration_s",
                                                                                                                     "t_max_s")}}


def _poly(params: dict, key: str = "polygon") -> np.ndarray:
    P = ring(np.asarray(params.get(key) or [], float))
    if len(P) < 3:
        raise TaskingError(110, "polygon", field=key)
    return P


def _n_user(v: Any) -> int | None:
    return None if v in (None, "auto") else int(v)


def _item(ctx: DecompCtx, k: int | str, kind: str, **kw: Any) -> WorkItem:
    kw.setdefault("priority", ctx.priority)
    return WorkItem(f"{ctx.tid}.{k}", ctx.tid, kind, allowed_nests=ctx.allowed_nests, allowed_models=ctx.allowed_models, **kw)


def _legs(uav: str | None, kind: str, P: np.ndarray) -> dict:
    return {"uav": uav, "kind": kind, "polyline": np.asarray(P, float)}


# ------------------------------------------------------------------------------------------------ 各类型
def _point_watch(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    pt = list(p.get("point") or [])
    if len(pt) < 2:
        raise TaskingError(110, "point")
    xy = np.array([float(pt[0]), float(pt[1])])
    if len(ctx.C) and bool(inside(xy[None], ctx.C)[0]):
        raise TaskingError(583, "keepout", point=[float(pt[0]), float(pt[1])])
    alt = float(p.get("alt_m", 60.0))
    z = alt if p.get("alt_ref") == "abs" else float(ctx.dtm(xy)[0]) + alt
    z = max(z, float(ctx.dsm(xy)[0]) + CLEAR_PATH_M)
    dwell = p.get("dwell_s", 300.0)
    r_lo = float(p.get("loiter_radius_m") or 80.0)
    fw = [m for m in ctx.models_for() if m.frame == "fixed_wing"]
    if fw and len(fw) == len(ctx.models_for()) and p.get("alt_ref") != "abs":
        z, r_lo = _fw_wind_station(ctx, fw, xy, z, r_lo)
    if fw and all(m.r_loiter_min_m > r_lo + 1e-9 for m in fw) and len(fw) == len(ctx.models_for()):
        raise TaskingError(110, "loiter_radius", min_m=round(min(m.r_loiter_min_m for m in fw), 1), value=r_lo)
    look = p.get("look") or {}
    look_at = None
    if look.get("mode") == "point" and look.get("point"):
        look_at = tuple(float(x) for x in look["point"])
    w = _item(ctx, 0, "station", persistent=dwell is None, workload_s=float(dwell or 0.0), pos=(float(xy[0]), float(xy[1]), z),
              needs={"role": "hover", "fw_ok": True}, loiter_radius_m=r_lo, look_at=look_at,  # type: ignore[arg-type]
              target=look.get("target") if look.get("mode") == "target" else None)
    return {"items": [w], "geometry": {"stations": [{"id": w.wid, "pos": [float(xy[0]), float(xy[1]), z], "target": w.target, "role": "primary",
                                                     "uav": None, "loiter_radius_m": r_lo}]}}


def _wind_at_agl(ctx: DecompCtx, agl: float) -> float | None:
    """快照的两个风速样本（机巢上方 10 m `wind_mps` 与 120 m `wind_hi_mps`）按对数律插值到 agl；缺 120 m 样本时 None。"""
    hi = ctx.snap.wind_hi_mps
    if hi is None:
        return None
    lo = float(ctx.snap.wind_mps)
    b = (float(hi) - lo) / math.log(12.0)
    return max(0.0, lo + b * math.log(max(float(agl), 1.0) / 10.0))


def _fw_wind_station(ctx: DecompCtx, fw: list, xy: np.ndarray, z: float, r: float) -> tuple[float, float]:
    """固定翼定点盘旋的风下复核（M20-FR-037 抗风）：作业高度的风超过机型抗风限（`wind_max_mps`，F1 15 m/s = v_cruise − 3）时盘旋
    不可保持（M21 改为逆风折线等待，会漂出边界），改取不高于原高度、风 ≤ 抗风限且盘旋圈（半径 ≥ 该风速下的盘旋半径下限 + 2%）上
    DSM 净空 ≥ CLEAR_PATH_M、离世界边界 ≥ 30 m 的最高 AGL（每 5 m 下调，最低 30 m），告警 `fw_wind_alt`；没有可用高度时原样（执行时
    M21 照常处理）。D2-FX-R2-tasking 第 2 次（D2-AC-16）：10 m/s 参考风在 150 m 高度约 19 m/s，F1 盘旋圈漂出边界 → CORRECTING → RTL。"""
    g = float(ctx.dtm(xy[None])[0])
    agl0 = float(z) - g
    w0 = _wind_at_agl(ctx, agl0)
    if w0 is None:
        return z, r
    rating = min(float(m.wind_max_mps) for m in fw)
    if w0 <= rating:
        return z, r
    th = np.linspace(0.0, 2.0 * math.pi, 36, endpoint=False)
    agl = math.floor(agl0 / 5.0) * 5.0
    while agl >= 30.0:
        w = _wind_at_agl(ctx, agl) or 0.0
        if w <= rating:
            r_need = max(float(r), 1.02 * max(m.r_loiter_at(w) for m in fw))
            # 盘旋圈两侧的环带（入圈与风中跟踪的偏差：内 30 m、外 40 m）上 DSM 净空都要满足
            P = np.vstack([np.c_[xy[0] + rr * np.cos(th), xy[1] + rr * np.sin(th)] for rr in (r_need - 30.0, r_need, r_need + 40.0)])
            ok = bool(np.all(ctx.dtm(P) + agl >= ctx.dsm(P) + CLEAR_PATH_M))
            if ok and ctx.border_fn is not None:
                ok &= bool(np.all(np.asarray(ctx.border_fn(P, 30.0), bool)))
            if ok and len(ctx.C):
                from .keepout import signed_margin

                ok &= bool(np.all(signed_margin(P, ctx.C) >= 0.0))
            if ok:
                if "fw_wind_alt" not in ctx.warnings:
                    ctx.warnings.append("fw_wind_alt")
                return g + agl, r_need
        agl -= 5.0
    return z, r


def _polyline(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    W = np.asarray(p.get("waypoints") or [], float)
    if W.ndim != 2 or len(W) < 2 or len(W) > 64:
        raise TaskingError(110, "waypoints")
    C = ctx.C
    bad = [int(i) for i in np.flatnonzero(inside(W[:, :2], C))] if len(C) else []
    if bad:
        raise TaskingError(583, "keepout", waypoints=bad)
    mode = str(p.get("mode", "loop"))
    if p.get("auto_detour", True):
        P2 = plan_route(W[:, :2], C, closed=(mode == "loop"))
        if P2 is None:
            raise TaskingError(583, "keepout", why_detail="no_detour")
    else:
        P2 = W[:, :2]
        hits = sorted({int(j) for k in range(len(P2) - 1) for j in seg_blocked(P2[k], P2[k + 1], C)}) if len(C) else []
        if hits:
            raise TaskingError(583, "keepout", segments_cross=len(hits))
    z = ctx.path_z(P2, float(p.get("alt_m", 60.0)))
    P = np.c_[P2, z]
    m = ctx.ref_model()
    v = float(p.get("speed_mps") or m.v_cruise_mps)
    duration = p.get("duration_s")
    needs = {"role": "cruise", "speed_mps": v, "facing": p.get("sensor_facing", "down"), "fw_ok": True}
    if duration is not None:
        w = _item(ctx, 0, "segment", persistent=True, workload_s=float(duration), path=P, closed=(mode == "loop"),
                  needs=dict(needs, repeat=mode))
        return {"items": [w], "geometry": {"legs": [_legs(None, "patrol", P)]}}
    laps = int(p.get("laps", 3)) if mode != "once" else 1
    seq = [P]
    seq.extend(P[::-1] if mode == "pingpong" and k % 2 == 1 else (np.vstack([P, P[:1]]) if mode == "loop" else P) for k in range(1, laps))
    if mode == "loop":
        seq[0] = np.vstack([P, P[:1]])
    full = np.vstack([s if k == 0 else s[1:] if np.allclose(s[0], seq[k - 1][-1]) else s for k, s in enumerate(seq)])
    L = polyline_length(full)
    t_on = 0.8 * m.endurance_s("cruise") - 2.0 * _mean_transit_s(ctx, full[0], m)
    L_s = max(v * max(t_on, 60.0), 50.0)
    k = max(1, math.ceil(L / L_s - 1e-9))
    items = []
    for i in range(k):
        Pi = cut_polyline(full, L * i / k, L * (i + 1) / k)
        items.append(_item(ctx, i, "segment", workload_s=polyline_length(Pi) / v, path=Pi, divisible=True, needs=dict(needs),
                           group=ctx.tid if k > 1 else None))
    return {"items": items, "geometry": {"legs": [_legs(None, "patrol", it.path) for it in items]}}


def _mean_transit_s(ctx: DecompCtx, p, m) -> float:
    ns = [n for n in ctx.snap.nests.values() if not ctx.allowed_nests or n.id in ctx.allowed_nests]
    if not ns:
        return 0.0
    d = min(float(np.hypot(p[0] - n.pos[0], p[1] - n.pos[1])) for n in ns)
    return d / max(m.v_cruise_mps, 0.1)


def _holes(P: np.ndarray, ctx: DecompCtx, *, ring_path: np.ndarray | None = None, cls: str | None = None) -> list[tuple[float, float, float, str | None]]:
    """与区域（或巡边环）相关的禁入洞：每个识别物取其最大禁入圆 (cx, cy, r, target)；圆心在多边形内、或圆与多边形边界（巡边环）相交。
    `cls` 给出时只取该类别识别物的洞（任务的 target_class；值守只为该类别转入接力，其余类别的洞不必看）。"""
    from .geom import point_in_polygon

    by: dict[str, tuple[float, float, float, str | None]] = {}
    for k in ctx.snap.keepouts:
        if cls and k.target and ctx.target_classes.get(str(k.target)) not in (None, cls):
            continue
        key = str(k.target or k.id)
        cur = by.get(key)
        if cur is None or k.r > cur[2]:
            by[key] = (float(k.x), float(k.y), float(k.r), k.target)
    out = []
    for key in sorted(by):
        cx, cy, r, tgt = by[key]
        near = bool(point_in_polygon(np.array([[cx, cy]]), P)[0])
        if not near:
            near = _poly_dist(P, cx, cy) <= r
        if not near and ring_path is not None and len(ring_path):
            near = _poly_dist(ring_path[:, :2], cx, cy) <= r + 3.5
        if near:
            out.append((cx, cy, r, tgt))
    return out


def _poly_dist(P: np.ndarray, cx: float, cy: float) -> float:
    """点到闭合折线（多边形边界）的最短距离。"""
    A = np.asarray(P, float)[:, :2]
    B = np.roll(A, -1, axis=0)
    D = B - A
    dd = np.maximum((D * D).sum(axis=1), 1e-12)
    t = np.clip(((np.array([cx, cy])[None, :] - A) * D).sum(axis=1) / dd, 0.0, 1.0)
    Q = A + t[:, None] * D
    return float(np.hypot(Q[:, 0] - cx, Q[:, 1] - cy).min())


def _look_ins(P: np.ndarray, C: np.ndarray, ctx: DecompCtx, alt_m: float, *, ring_path: np.ndarray | None = None,
              toward: np.ndarray | None = None, cls: str | None = None) -> list[dict]:
    """看洞站位（M20 §6.4 第 4 条，D2-FX-R2 按站位约束重做）：对每个禁入洞，在洞边界外 0–60 m 环带（固定翼再加盘旋半径，盘旋圈不进入
    禁入圆）、AGL ∈ {60, 80, 100, 120} m、方位每 15° 生成候选，过滤全部禁入圆外（≥ 5 m）、DSM 净空 ≥ 10 m、世界边界与禁飞区，按
    "三射线（识别物脚点上方 0.95/0.5/0.05·h）全可见 → 离洞近 → 朝区域质心 → AGL 接近巡查高度"排序，复核视线后取第一个。

    返回 [{pos: (x, y, z), target, center: (cx, cy), r_m, los}]（`target` 为禁入体所属识别物，执行时云台指向其当前位置并按所需
    等级变焦）。此前只取洞边界外 15 m、朝质心方向、巡查高度的单点，不复核视线、不指向不变焦（第 1 轮 T5 全程 D 级）。"""
    out: list[dict] = []
    c0 = centroid(P) if toward is None else np.asarray(toward, float)
    r_t = ctx.r_turn()
    for cx, cy, r, tgt in _holes(P, ctx, ring_path=ring_path, cls=cls):
        u0 = c0 - np.array([cx, cy])
        n0 = float(np.hypot(*u0))
        b0 = math.atan2(u0[1], u0[0]) if n0 > 1e-6 else 0.0
        bearings = np.radians(np.arange(0.0, 360.0, LOOK_BEARING_DEG))
        rows = []
        for dr in LOOK_RING_M:
            d = r + dr + (r_t + 10.0 if r_t > 0 else 0.0)
            for b in bearings:
                for agl in sorted(set(LOOK_AGL_M) | {float(min(max(alt_m, 60.0), H_CAP_M))}):
                    dev = abs(math.atan2(math.sin(b - b0), math.cos(b - b0)))
                    rows.append((cx + d * math.cos(b), cy + d * math.sin(b), agl, d, dev))
        A = np.asarray(rows, float)
        xy = A[:, :2]
        z = np.maximum(ctx.dtm(xy) + A[:, 2], ctx.dsm(xy) + CLEAR_PATH_M)
        ok = (z - ctx.dtm(xy)) <= H_CAP_M + 1e-6
        if len(C):
            from .keepout import signed_margin

            # 禁入圆外 ≥ 5 m；固定翼的盘旋圈（半径 r_t）整圈在禁入圆外
            ok &= signed_margin(xy, C) >= 5.0 + (r_t if r_t > 0 else 0.0)
        if ctx.free_fn is not None and ok.any():
            ii = np.flatnonzero(ok)
            ok[ii] &= np.asarray(ctx.free_fn(np.c_[xy[ii], z[ii]]), bool)
        if ctx.avoid_pts and ok.any():
            # 其他任务的看洞点与站位（D2-FX-R2-tasking 第 2 次：区域巡查与巡边对同一个洞取了同一个看洞点，两架在同一点上下相撞）
            Av = np.asarray(ctx.avoid_pts, float).reshape(-1, 2)
            dmin = np.min(np.hypot(xy[:, None, 0] - Av[None, :, 0], xy[:, None, 1] - Av[None, :, 1]), axis=1)
            ok &= dmin >= LOOK_SEP_M
        nk = ctx.node_ok()                     # 固定翼盘旋圈不出世界边界
        if nk is not None and ok.any():
            ii = np.flatnonzero(ok)
            ok[ii] &= np.asarray(nk(xy[ii]), bool)
        idx = np.flatnonzero(ok)
        if idx.size == 0:
            continue
        # 预排序：离洞近 → 朝质心 → AGL 接近巡查高度
        key = np.lexsort((np.abs(A[idx, 2] - alt_m), np.round(A[idx, 4], 3), A[idx, 3]))
        idx = idx[key]
        best = None
        if ctx.los_fn is not None:
            g = float(ctx.dtm(np.array([[cx, cy]]))[0])
            for s0 in range(0, min(idx.size, LOOK_LOS_MAX), 64):
                ch = idx[s0:s0 + 64]
                Pa = np.repeat(np.c_[xy[ch], z[ch]], 3, axis=0)
                Pb = np.tile(np.array([[cx, cy, 0.0]]), (3 * ch.size, 1))
                Pb[:, 2] = g + np.tile(np.asarray(LOOK_RAY_FRAC) * LOOK_TARGET_H_M, ch.size)
                vis = np.asarray(ctx.los_fn(Pa, Pb), bool).reshape(ch.size, 3)
                full = np.flatnonzero(vis.all(axis=1))
                if full.size:
                    best = (int(ch[full[0]]), True)
                    break
                if best is None:
                    two = np.flatnonzero(vis.sum(axis=1) >= 2)
                    if two.size:
                        best = (int(ch[two[0]]), False)
        else:
            best = (int(idx[0]), True)
        if best is None:
            if "look_in_los" not in ctx.warnings:
                ctx.warnings.append("look_in_los")
            best = (int(idx[0]), False)
        j, los = best
        out.append({"pos": (float(xy[j, 0]), float(xy[j, 1]), float(z[j])), "target": tgt, "center": (cx, cy), "r_m": r, "los": bool(los)})
    return out


def _home_xy(ctx: DecompCtx, P: np.ndarray) -> np.ndarray | None:
    """任务可用机巢中离区域质心最近者的水平位置（任务限定机型时只看有该机型在巢的机巢）；没有机巢时 None。"""
    allow_m = set(ctx.allowed_models or ())
    nests = [n for n in ctx.snap.nests.values() if (not ctx.allowed_nests or n.id in ctx.allowed_nests) and not n.crewed]
    if allow_m:
        has = {v.nest_id for v in ctx.snap.vehicles if v.model_id in allow_m}
        nests = [n for n in nests if n.id in has] or nests
    if not nests:
        return None
    c = centroid(P)
    n = min(nests, key=lambda q: (float(np.hypot(q.pos[0] - c[0], q.pos[1] - c[1])), q.id))
    return np.asarray(n.pos[:2], float)


def _look_pts(looks: list[dict]) -> np.ndarray:
    return np.asarray([q["pos"] for q in looks], float).reshape(-1, 3)


def _area_lanes(P: np.ndarray, ctx: DecompCtx, cls: str, level: str, theta: float, sidelap: float, alt_m: float | None) -> dict:
    """巡查条带（§6.5.2）：按 g_req 反推航高与条带宽；不补洞；禁入圆截断（全部可用机型为固定翼时截断圆外扩转弯半径，条带端点
    的掉头不进入禁入圆，并按 2·R_turn 跳行）。"""
    g = g_req_m(cls, level)
    r_t = ctx.r_turn()
    C = ctx.C.copy() if len(ctx.C) else ctx.C
    if r_t > 0 and len(C):
        C[:, 2] += r_t
    cams = ctx.cams()
    m = ctx.ref_model()
    R = build_raster(P, ctx.height_dsm, ctx.ground_dtm, res=4.0)
    if cams:
        try:
            G = reverse_gsd(R, g, cams, theta_max_deg=theta)
        except TaskingError:
            G = reverse_gsd(R, max(g, 0.05), cams, theta_max_deg=theta)
            ctx.warnings.append("gsd_relaxed")
        h, w = G.h_m, G.w_m
    else:
        G = None
        h, w = float(alt_m or 60.0), 2.0 * float(alt_m or 60.0) * math.tan(math.radians(theta))
    if alt_m is not None:
        h = float(alt_m)
    d = max((1.0 - sidelap) * w, 5.0)
    v = m.v_cruise_mps
    if G is not None:
        G.d_m = d
        seq, th, _blk = strips_for(R, G, P, d, v, C, None, fw_r_min_m=r_t)
    else:
        from awr.swarm.coverage.lanes import bcd_cells, best_sweep_angle, lanes_for_angle, order_cells

        th, _t = best_sweep_angle(P, d, v)
        lanes, _ = lanes_for_angle(P, d, th)
        seq = order_cells(bcd_cells(lanes, th), centroid(P))
    segs: list[ScanSeg] = []
    for a, b in seq:
        a = np.asarray(a, float)
        b = np.asarray(b, float)
        for piece in _clip_seg(a, b, C):
            z = ctx.path_z(piece, h)
            segs.append(ScanSeg(np.c_[piece, z], "strip"))
    return {"segs": segs, "h_m": h, "w_m": w, "d_m": d, "g_m": g, "theta": th, "v": v, "cam": None if G is None else G.cam.name}


def _clip_seg(a: np.ndarray, b: np.ndarray, C: np.ndarray) -> list[np.ndarray]:
    from .keepout import free_intervals

    if len(C) == 0:
        return [np.vstack([a[:2], b[:2]])]
    out = []
    C = np.asarray(C, float).copy()
    C[:, 2] += 0.5                       # 截断点离禁入圆 0.5 m（数值稳健）
    for t0, t1 in free_intervals(a, b, C):
        p0, p1 = a[:2] + (b[:2] - a[:2]) * t0, a[:2] + (b[:2] - a[:2]) * t1
        if float(np.hypot(*(p1 - p0))) >= 6.0:
            out.append(np.vstack([p0, p1]))
    return out


def _segs_path(segs: list[ScanSeg], C: np.ndarray, ctx: DecompCtx, h: float) -> np.ndarray:
    """有序段 → 连续折线（段间转场在禁入圆外）。"""
    pts: list[np.ndarray] = []
    for s in segs:
        if pts:
            link = ctx.plan_path(pts[-1][:2], s.first[:2], C) if len(C) else None
            if link is not None and len(link) > 2:
                z = ctx.path_z(link, h)
                pts.extend(np.c_[link[1:-1], z[1:-1]])
        pts.extend(np.asarray(s.P, float))
    return np.asarray(pts, float).reshape(-1, 3)


def _area_patrol(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    P = _poly(p)
    cls, level = str(p.get("target_class", "person")), str(p.get("level_req", "R"))
    L = _area_lanes(P, ctx, cls, level, float(p.get("theta_max_deg", 15.0)), float(p.get("sidelap", 0.1)), p.get("alt_m"))
    segs = L["segs"]
    if not segs:
        raise TaskingError(580, "area_empty")
    looks = _look_ins(P, ctx.C, ctx, L["h_m"], cls=cls)
    dwell_s = LOOK_IN_DWELL_FW_S if ctx.fw_only() else LOOK_IN_DWELL_S
    t_cov = seq_time_s(segs, L["v"]) + dwell_s * len(looks)
    rev = float(p.get("revisit_s", 300.0))
    n_user = _n_user(p.get("n_vehicles"))
    n = n_user if n_user is not None else max(1, math.ceil(t_cov / rev - 1e-9))
    if n_user is not None and t_cov / n > rev:
        ctx.warnings.append("revisit_exceeded")
    groups = split_segs(segs, n, L["v"])
    items = []
    geo_sub, legs = [], []
    duration = p.get("duration_s", 1800.0)
    for k, g in enumerate(groups):
        path = _segs_path(g, ctx.C, ctx, L["h_m"])
        dwell: dict[int, float] = {}
        look_map: dict[int, str | None] = {}
        if looks:
            # 看洞点：离该子区起点最近的洞，作为周期起点的停留航点（首个周期先访问）
            mine = [q for q in looks if _nearest_group(np.asarray(q["pos"], float), groups) == k]
            if mine:
                path, dwell, look_map = _prepend_looks(path, mine, ctx, dwell_s)
        w = _item(ctx, k, "subregion", persistent=True, workload_s=float(duration or 0.0), path=path, dwell=dwell, group=ctx.tid,
                  needs={"role": "cruise", "repeat": "pingpong", "speed_mps": L["v"], "level": level, "gsd_m": L["g_m"],
                         "cams": [L["cam"]] if L["cam"] else None, "fw_ok": True, "looks": {str(i): t for i, t in look_map.items()}})
        items.append(w)
        geo_sub.append(_hull(path[:, :2]))
        legs.append(_legs(None, "patrol", path))
    return {"items": items, "geometry": {"subregions": geo_sub, "legs": legs,
                                         "stations": [{"id": f"{ctx.tid}.look{i}", "pos": [float(x) for x in q["pos"]], "target": q["target"],
                                                       "role": "primary", "uav": None} for i, q in enumerate(looks)]},
            "preview": {"n_vehicles": len(items), "makespan_s": t_cov, "revisit_s": rev, "h_m": L["h_m"], "gsd_m": L["g_m"],
                        "why_n": {"rule": "user_fixed" if n_user is not None else "makespan", "n_minus_1_makespan_s": None}},
            "t_cov_s": t_cov, "lanes": L}


def _prepend_looks(path: np.ndarray, looks: list[dict], ctx: DecompCtx, dwell_s: float) -> tuple[np.ndarray, dict[int, float], dict[int, str | None]]:
    """把看洞站位作为周期起点的停留航点插到航线前（看洞点之间与到航线起点的连线在禁入圆外绕行，中间点取航线高度）。
    返回 (航线, 停留 {序号: 秒}, 看洞 {序号: 识别物})。"""
    pre = _look_pts(looks)
    tg = [q.get("target") for q in looks]
    route = ctx.plan_route(np.vstack([pre[:, :2], path[:1, :2]])) if len(ctx.C) else None
    if route is not None and len(route) > len(pre) + 1:
        # 连接段的高度按走廊 DSM 净空（D2-FX-R2：此前中间点直接取航线首点高度，穿越高楼时细校验 102）
        zz = np.maximum(ctx.path_z(route, max(float(path[0, 2]) - float(ctx.dtm(path[:1, :2])[0]), 30.0)), float(path[0, 2]))
        idx = []
        for q in pre:
            j = int(np.argmin(np.hypot(route[:, 0] - q[0], route[:, 1] - q[1])))
            zz[j] = q[2]
            idx.append(j)
        full = np.c_[route[:-1], zz[:-1]]
        out = np.vstack([full, path])
        return out, {i: dwell_s for i in idx}, dict(zip(idx, tg, strict=True))
    z0 = ctx.path_z(np.vstack([pre[:, :2], path[:1, :2]]), 30.0)
    pre = pre.copy()
    pre[:, 2] = np.maximum(pre[:, 2], z0[:-1])
    out = np.vstack([pre, path])
    return out, {i: dwell_s for i in range(len(pre))}, {i: tg[i] for i in range(len(pre))}


def _close_loop(path: np.ndarray, ctx: DecompCtx, h: float) -> np.ndarray:
    """循环航线的闭合段（末点 → 首点）在禁入圆外：经可见图绕行的中间点追加到末尾（执行器按 closed 回到首点）。"""
    if len(ctx.C) == 0 or len(path) < 2:
        return path
    link = ctx.plan_path(path[-1, :2], path[0, :2])
    if link is None or len(link) <= 2:
        return path
    return np.vstack([path, np.c_[link[1:-1], ctx.path_z(link, h)[1:-1]]])


def _nearest_group(q: np.ndarray, groups: list[list[ScanSeg]]) -> int:
    best, bk = math.inf, 0
    for k, g in enumerate(groups):
        d = min(float(np.hypot(*(s.first[:2] - q[:2]))) for s in g)
        if d < best:
            best, bk = d, k
    return bk


def _hull(xy: np.ndarray) -> list[list[float]]:
    pts = sorted({(round(float(x), 2), round(float(y), 2)) for x, y in np.asarray(xy)[:, :2]})
    if len(pts) <= 2:
        return [list(p) for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [list(p) for p in lower[:-1] + upper[:-1]]


def _scan(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    P = _poly(p)
    g = float(p.get("gsd_cm", 3.0)) / 100.0
    cams = ctx.cams()
    if not cams:
        raise TaskingError(580, "no_capability", need="rgb.zoom")
    vehicles = ctx.scan_vehicles()
    nests = [n for n in ctx.snap.nests.values() if not ctx.allowed_nests or n.id in ctx.allowed_nests]
    start = None
    if nests:
        c0 = centroid(P)
        start = min((np.asarray(n.pos[:2], float) for n in nests), key=lambda q: float(np.hypot(*(q - c0))))
    out = plan_scan(P, g, cams, vehicles, ctx.height_dsm, ctx.ground_dtm, keepouts=ctx.C, h_cap_m=float(p.get("h_cap_m", 120.0)),
                    clearance_m=float(p.get("clearance_m", 20.0)), theta_max_deg=float(p.get("theta_max_deg", 15.0)),
                    sidelap=float(p.get("sidelap", 0.1)), frontlap=float(p.get("frontlap", 0.1)), t_max_s=p.get("t_max_s"),
                    allow_multi_sortie=bool(p.get("allow_multi_sortie", False)), n_user=_n_user(p.get("n_vehicles")),
                    strategy=str(p.get("strategy", "strips")), start_xy=start, now=ctx.now, reserve=ctx.snap.reserve)
    items = []
    legs = []
    deadline = ctx.now + float(out["t_max_s"]) if not out.get("multi_sortie") else None
    for k, gr in enumerate(out["groups"]):
        parts = gr.get("sorties") or [gr]
        chain = f"{ctx.tid}.g{k}" if len(parts) > 1 else None
        for si, part in enumerate(parts):
            wid = f"g{k}" if len(parts) == 1 else f"g{k}.s{si}"
            dwell = {int(i): float(d) for i, d in part.get("dwell") or []}
            needs = {"role": "cruise", "speed_mps": out["v_mps"], "cams": [out["cam"]], "gsd_m": g, "triggers": list(part.get("triggers") or []),
                     "prefer": gr["uav"], "fw_ok": True}
            if chain:
                needs["chain"] = chain
            items.append(_item(ctx, wid, "strip_group", workload_s=float(part["length_m"]) / max(out["v_mps"], 0.1) + sum(dwell.values()),
                               deadline_s=deadline, path=np.asarray(part["path"], float), dwell=dwell, group=ctx.tid, needs=needs,
                               multi_sortie=bool(chain), sortie_order=si))
            legs.append(_legs(gr["uav"], "strip", part["path"]))
    S = out["strips"]
    strips = [{"id": f"s{k}", "p0": [float(r[0]), float(r[1]), float(r[4])], "p1": [float(r[2]), float(r[3]), float(r[4])], "uav": None}
              for k, r in enumerate(S)]
    fills = [{"pos": [float(x) for x in f], "dwell_s": 0.0} for f in out["fills"]]
    cov = out["coverage"]
    prev = {"n_vehicles": out["n"], "makespan_s": out["makespan_s"], "t_max_s": out["t_max_s"], "why_n": out["why_n"],
            "makespan_curve": [{"n": c["n"], "makespan_s": c["makespan_s"]} for c in out["makespan_curve"]],
            "scan": {"h_m": out["h_m"], "f_mm": out["f_mm"], "p_eff_um": out["p_eff_um"], "strip_w_m": out["strip_w_m"],
                     "spacing_m": out["spacing_m"], "n_strips": out["n_strips"], "theta_e_deg": out["theta_e_deg"],
                     "footprint_bound_m": out["footprint_bound_m"], "h_floor_m": out["h_floor_m"]},
            "coverage": {"direct_pct": cov["direct_pct"], "visible_pct": cov["visible_pct"], "fill_waypoints": cov["fill_waypoints"],
                         "obstacle_pct": cov["obstacle_pct"], "cells": cov["cells"]}}
    if out.get("multi_sortie"):
        ctx.warnings.append("multi_sortie")
    return {"items": items, "geometry": {"strips": strips, "fill_waypoints": fills, "legs": legs}, "preview": prev,
            "scan_detail": {"invisible_cells": cov["invisible_cells"], "uncovered_visible_cells": cov.get("uncovered_visible_cells", 0),
                            "length_m": out["length_m"], "v_mps": out["v_mps"], "work_s_single": out["work_s_single"]}}


def _ring_path(P: np.ndarray, offset: float, direction: str, C: np.ndarray, ctx: DecompCtx, alt: float) -> np.ndarray:
    R = offset_polygon(P, offset) if abs(offset) > 1e-9 else ring(P)
    if (signed_area(R) > 0) != (direction == "ccw"):
        R = R[::-1]
    # 固定翼：环收进世界边界内缩带（30 m + 0.5·R_turn，转角提前转弯）；绕行沿禁入圆外切多边形（圆弧上的 22.5° 转角，固定翼可跟随，
    # 不再外扩转弯半径：运动识别物的静态包络已含守卫口径 v·τ_pred 与 10 m 余量）
    ins = 30.0 + 0.5 * ctx.r_turn()
    R = _inset_ring(R, ctx, ins)
    if len(C):
        Rr = ring_detour(R, C, node_ok=ctx.node_ok(ins))
        if Rr is None:
            raise TaskingError(583, "keepout", why_detail="perimeter_blocked")
        R = Rr
    z = ctx.path_z(np.vstack([R, R[:1]]), alt)[:-1]
    return np.c_[R, z]


def _inset_ring(R: np.ndarray, ctx: DecompCtx, inset_m: float | None = None) -> np.ndarray:
    """环顶点收进世界边界内缩带：越界的顶点沿指向区域质心的方向内移（巡边偏移环在边界附近时）。"""
    ok_fn = ctx.node_ok(inset_m)
    if ok_fn is None:
        return R
    A = np.asarray(R, float).copy()
    c = centroid(A)
    for k in range(len(A)):
        for _ in range(16):
            if bool(np.asarray(ok_fn(A[k:k + 1]), bool)[0]):
                break
            A[k] = c + (A[k] - c) * 0.93
    return A


def ring_detour(R: np.ndarray, C: np.ndarray, step_m: float = DETOUR_STEP_M, node_ok: Any = None) -> np.ndarray | None:
    """闭合环 R（N×2，不重复首点）在禁入圆处绕行（M20-FR-015）：各边按 step_m 加密（保留原顶点），落在禁入圆内的连续一段由其前后
    两个圆外点之间的可见图最短绕行（`plan_path`，外切多边形顶点外扩 3 m）代替；全部在圆内时 None。D2-FX-R2：此前顶点落在圆内
    且剩余顶点 < 3 时整环交给 `plan_route`，端点在圆内即无解，巡边与敏感体相交时一律 583（第 1 轮 D2-AC-16）。"""
    A = np.asarray(R, float)[:, :2]
    if len(C) == 0 or len(A) < 3:
        return A
    pts = []
    for k in range(len(A)):
        a, b = A[k], A[(k + 1) % len(A)]
        L = float(np.hypot(*(b - a)))
        n = max(1, math.ceil(L / step_m))
        pts.extend(a + (b - a) * (i / n) for i in range(n))
    D = np.asarray(pts, float)
    ins = inside(D, C)
    if not ins.any():
        return A
    if ins.all():
        return None
    k0 = int(np.flatnonzero(~ins)[0])
    D = np.roll(D, -k0, axis=0)
    ins = np.roll(ins, -k0)
    n = len(D)
    out: list[np.ndarray] = []
    i = 0
    while i < n:
        if not ins[i]:
            out.append(D[i])
            i += 1
            continue
        j = i
        while j < n and ins[j]:
            j += 1
        a = D[i - 1]
        b = D[j % n]
        seg = plan_path(a, b, C, node_ok=node_ok)
        if seg is None:
            return None
        out.extend(np.asarray(seg[1:-1], float))
        i = j
    return _simplify_ring(np.asarray(out, float))


def _simplify_ring(R: np.ndarray, tol_m: float = 0.3) -> np.ndarray:
    """去掉闭合环上的共线点（加密点；偏离相邻两个保留点连线 < tol_m）：每轮删去互不相邻的共线点，直到不再变化。"""
    idx = np.arange(len(R))
    for _ in range(64):
        if len(idx) <= 3:
            break
        P = R[idx]
        Pa, Pb = np.roll(P, 1, axis=0), np.roll(P, -1, axis=0)
        d = Pb - Pa
        L = np.hypot(d[:, 0], d[:, 1])
        dev = np.abs(d[:, 0] * (P[:, 1] - Pa[:, 1]) - d[:, 1] * (P[:, 0] - Pa[:, 0])) / np.maximum(L, 1e-9)
        proj = ((P - Pa) * d).sum(axis=1) / np.maximum(L * L, 1e-9)
        cand = (dev < tol_m) & (proj > 0.0) & (proj < 1.0) & (L > 1e-9)
        if not cand.any():
            break
        drop = np.zeros(len(idx), bool)
        for t in np.flatnonzero(cand):                 # 互不相邻
            if not drop[(t - 1) % len(idx)]:
                drop[t] = True
        if drop[0] and drop[-1]:
            drop[-1] = False
        if drop.sum() > len(idx) - 3:
            break
        idx = idx[~drop]
    return R[idx]


def _perimeter(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    P = _poly(p)
    ringP = _ring_path(P, float(p.get("offset_m", 0.0)), str(p.get("direction", "ccw")), ctx.C, ctx, float(p.get("alt_m", 60.0)))
    L = polyline_length(ringP, closed=True)
    m = ctx.ref_model()
    v = m.v_cruise_mps
    rev = float(p.get("revisit_s", 300.0))
    n_user = _n_user(p.get("n_vehicles"))
    n = n_user if n_user is not None else max(1, math.ceil(L / (v * rev) - 1e-9))
    closed = np.vstack([ringP, ringP[:1]])
    # 禁入体处插入看洞航段（M20-FR-015）：与巡边环相交或位于区域内的洞，各给离其最近的相位
    looks = _look_ins(P, ctx.C, ctx, float(p.get("alt_m", 60.0)), ring_path=ringP, cls=p.get("target_class"))
    dwell_s = LOOK_IN_DWELL_FW_S if ctx.fw_only() else LOOK_IN_DWELL_S
    items = []
    stations = []
    starts = []
    rots = []
    for k in range(n):
        s0 = L * k / n
        rot = np.vstack([cut_polyline(closed, s0, L), cut_polyline(closed, 0.0, s0)[1:]]) if s0 > 0 else ringP.copy()
        if np.allclose(rot[0], rot[-1]):
            rot = rot[:-1]
        rots.append(rot)
        starts.append(rot[0])
        stations.append(point_at(closed, s0))
    for k in range(n):
        rot = rots[k]
        dwell: dict[int, float] = {}
        look_map: dict[int, str | None] = {}
        mine = [q for q in looks if int(np.argmin([float(np.hypot(*(np.asarray(q["pos"][:2]) - st[:2]))) for st in starts])) == k]
        if mine:
            rot, dwell, look_map = _prepend_looks(rot, mine, ctx, dwell_s)
            rot = _close_loop(rot, ctx, float(p.get("alt_m", 60.0)))
        items.append(_item(ctx, k, "perimeter_phase", persistent=True, workload_s=float(p.get("duration_s") or 0.0), path=rot, closed=True,
                           group=ctx.tid, dwell=dwell,
                           needs={"role": "cruise", "repeat": "loop", "speed_mps": v, "phase_s": L * k / n / v, "fw_ok": True,
                                  "looks": {str(i): t for i, t in look_map.items()}}))
    return {"items": items, "geometry": {"legs": [_legs(None, "patrol", ringP)],
                                         "stations": [{"id": f"{ctx.tid}.look{i}", "pos": [float(x) for x in q["pos"]], "target": q["target"],
                                                       "role": "primary", "uav": None} for i, q in enumerate(looks)]},
            "preview": {"n_vehicles": n, "makespan_s": L / v, "revisit_s": rev,
                        "why_n": {"rule": "user_fixed" if n_user is not None else "makespan", "n_minus_1_makespan_s": None}},
            "ring_len_m": L}


def _relay(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    if not p.get("target"):
        raise TaskingError(110, "target")
    return {"items": [], "geometry": {}, "relay": {"target": p["target"], "level_req": p.get("level_req"), "duration_s": p.get("duration_s")}}


def _guard(spec: dict, ctx: DecompCtx) -> dict:
    p = _params(spec)
    P = _poly(p)
    rev = float(p.get("revisit_s", 300.0))
    cls, level = str(p.get("target_class", "person")), str(p.get("level_req", "R"))
    L = _area_lanes(P, ctx, cls, level, 15.0, 0.1, None)
    try:
        ringP: np.ndarray | None = _ring_path(P, float(p.get("perimeter_offset_m", 20.0)), "ccw", ctx.C, ctx, L["h_m"])
    except TaskingError as e:
        # D2-INT：巡边环在世界边界内缩带内无法绕开禁入圆（例：suzhou T5 的区域贴近世界北缘，静止与游走两个识别物的运动包络
        # 禁入圆把环的南北两边都盖住，绕行只能越出边界）时，值守不整体判 583，而是去掉巡边段（告警 perimeter_blocked），
        # 保留看洞与区域巡查；单独的巡边任务（perimeter_patrol）仍判 583。
        if e.detail.get("why_detail") != "perimeter_blocked" or not L["segs"]:
            raise
        ringP = None
        ctx.warnings.append("perimeter_blocked")
    L_p = polyline_length(ringP, closed=True) if ringP is not None else 0.0
    # 值守周期从机巢一侧开始（D2-FX-R2-tasking 第 2 次，D2-AC-34：会话开始后 ≤ 60 s【仿真】首次明确捕获）：看洞点优先取朝向机巢的一侧，周期内按离
    # 机巢由近到远排序（此前朝区域质心，synthcity T5 的首个看洞点在识别物另一侧，绕禁入圈 606 m，首次捕获 159.7 s）
    home = _home_xy(ctx, P)
    looks = _look_ins(P, ctx.C, ctx, L["h_m"], ring_path=ringP, cls=cls, toward=home)
    if home is not None and len(looks) > 1:
        looks = sorted(looks, key=lambda q: (float(np.hypot(q["pos"][0] - home[0], q["pos"][1] - home[1])), str(q.get("target"))))
    dwell_s = LOOK_IN_DWELL_FW_S if ctx.fw_only() else LOOK_IN_DWELL_S
    v = L["v"]
    t_cyc = L_p / v + seq_time_s(L["segs"], v) + dwell_s * len(looks) + 2 * v / A_MPS2
    if t_cyc <= rev and (L["segs"] or looks):
        # 值守周期（M20 §6.5.3）：先看洞，再巡边一圈，再扫一遍区域，最后禁入圆外回到首个看洞点（闭合段经可见图绕行）
        body = _segs_path(L["segs"], ctx.C, ctx, L["h_m"]) if ringP is None else np.vstack([ringP, ringP[:1]])
        if L["segs"] and ringP is not None:
            area = _segs_path(L["segs"], ctx.C, ctx, L["h_m"])
            link = ctx.plan_path(ringP[0, :2], area[0, :2]) if len(ctx.C) else None
            mid = [] if link is None or len(link) <= 2 else list(np.c_[link[1:-1], ctx.path_z(link, L["h_m"])[1:-1]])
            body = np.vstack([body, *([np.asarray(mid)] if mid else []), area])
        dwell: dict[int, float] = {}
        look_map: dict[int, str | None] = {}
        path = body
        if looks:
            path, dwell, look_map = _prepend_looks(body, looks, ctx, dwell_s)
        path = _close_loop(path, ctx, L["h_m"])
        w = _item(ctx, 0, "guard_cycle", persistent=True, workload_s=0.0, path=path, dwell=dwell, closed=True,
                  needs={"role": "cruise", "repeat": "loop", "speed_mps": v, "level": level, "gsd_m": L["g_m"], "fw_ok": True,
                         "looks": {str(i): t for i, t in look_map.items()}})
        return {"items": [w], "geometry": {"subregions": [P.tolist()], "legs": [_legs(None, "patrol", path)],
                                           "stations": [{"id": f"{ctx.tid}.look{i}", "pos": [float(x) for x in q["pos"]], "target": q["target"],
                                                         "role": "primary", "uav": None} for i, q in enumerate(looks)]},
                "preview": {"n_vehicles": 1, "makespan_s": t_cyc, "revisit_s": rev, "why_n": {"rule": "makespan", "n_minus_1_makespan_s": None}},
                "mode": "merged", "t_cyc_s": t_cyc}
    common = {"on_detect": "relay"}
    children = [
        {"type": "area_patrol", "label": None, "params": {"polygon": P.tolist(), "target_class": cls, "level_req": level, "revisit_s": rev,
                                                          "duration_s": None}, "common": common},
    ]
    if ringP is not None:
        children.append({"type": "perimeter_patrol", "label": None,
                         "params": {"polygon": P.tolist(), "offset_m": float(p.get("perimeter_offset_m", 20.0)), "revisit_s": rev},
                         "common": common})
    return {"items": [], "geometry": {"subregions": [P.tolist()]}, "children": children, "mode": "split", "t_cyc_s": t_cyc,
            "preview": {"n_vehicles": 0, "makespan_s": t_cyc, "revisit_s": rev, "why_n": {"rule": "makespan", "n_minus_1_makespan_s": None}}}


register_decomposer("point_watch", _point_watch)
register_decomposer("polyline_patrol", _polyline)
register_decomposer("area_patrol", _area_patrol)
register_decomposer("area_scan_gsd", _scan)
register_decomposer("perimeter_patrol", _perimeter)
register_decomposer("relay_watch", _relay)
register_decomposer("area_guard", _guard)


def decompose(spec: dict, ctx: DecompCtx) -> dict:
    """任务请求（`type`、`params`、`common`）→ {items, geometry, preview?, children?, relay?, warnings}；不可行抛 TaskingError。"""
    t = str(spec.get("type"))
    fn = _DECOMPOSERS.get(t)
    if fn is None:
        raise TaskingError(110, "type", value=t)
    common = spec.get("common") or {}
    ctx.priority = RELAY_PRIORITY if t == "relay_watch" else PRIORITY_OF.get(str(common.get("priority", "normal")), 5)
    ctx.allowed_nests = tuple(common.get("allowed_nests") or ())
    ctx.allowed_models = tuple(common.get("allowed_models") or ())
    out = fn(spec, ctx)
    if ctx.fw_only() and t in FW_FLAT_TYPES:
        _flatten_fw(out.get("items") or [])
    _separate_layers(out.get("items") or [], ctx)
    for w in out.get("items") or []:
        _safe_profile(w, ctx)
    out.setdefault("geometry", {})
    out["warnings"] = sorted(set(ctx.warnings) | set(out.get("warnings") or []))
    return out


def _corridor_top(ctx: DecompCtx, a: np.ndarray, b: np.ndarray) -> float:
    """a → b 水平走廊（±5 m，每 5 m）的 DSM 最大值 + CLEAR_PATH_M（与 `path_z` 同一口径）。"""
    a2, b2 = np.asarray(a, float)[:2], np.asarray(b, float)[:2]
    L = float(np.hypot(*(b2 - a2)))
    n = max(2, int(L / 5.0) + 1)
    u = (b2 - a2) / max(L, 1e-9)
    nrm = np.array([-u[1], u[0]])
    pts = np.vstack([a2 + (b2 - a2) * t + nrm * o for t in np.linspace(0, 1, n) for o in (-5.0, 0.0, 5.0)])
    return float(ctx.dsm(pts).max()) + CLEAR_PATH_M


def path_layer(P: np.ndarray) -> tuple[float, float, float, float, float]:
    """航线的层：(x0, y0, x1, y1, 高度中位数)。"""
    P = np.asarray(P, float).reshape(-1, 3)
    return (float(P[:, 0].min()), float(P[:, 1].min()), float(P[:, 0].max()), float(P[:, 1].max()), float(np.median(P[:, 2])))


def layers_conflict(a: tuple, b: tuple) -> bool:
    """两条航线范围（外扩 LAYER_XY_PAD_M）重叠且高度中位数相差 < LAYER_SEP_M。"""
    pad = LAYER_XY_PAD_M
    return (a[0] - pad <= b[2] and b[0] - pad <= a[2] and a[1] - pad <= b[3] and b[1] - pad <= a[3]
            and abs(float(a[4]) - float(b[4])) < LAYER_SEP_M - 1e-6)


def _separate_layers(items: list, ctx: DecompCtx) -> None:
    """本任务的航线类工作项与其他任务（`ctx.avoid_layers`）范围重叠而作业高度相差 < LAYER_SEP_M 时，本任务全部航线整体降低
    LAYER_SEP_M（最多两档；降低后最低 AGL < LAYER_AGL_MIN_M 时不降，告警 `layer_shared`）。降低只会使 GSD 更细；走廊 DSM 净空随后由
    `_safe_profile` 复核。D2-FX-R2-tasking 第 2 次（D2-AC-16）：区域巡查与巡边在同一高度（135 m）沿交叉航线反复相遇，M09 第 3 次让行即
    OSCILLATION 保持 HOLD，两机悬停到能量 RTL。"""
    if not ctx.avoid_layers:
        return
    paths = [w for w in items if w.path is not None and len(w.path) >= 2 and w.kind != "station"]
    if not paths:
        return
    for _k in range(2):
        mine = [path_layer(w.path) for w in paths]
        if not any(layers_conflict(a, b) for a in mine for b in ctx.avoid_layers):
            return
        lo = min(float(np.min(np.asarray(w.path, float)[:, 2] - ctx.dtm(np.asarray(w.path, float)[:, :2]))) for w in paths)
        if lo - LAYER_SEP_M < LAYER_AGL_MIN_M:
            break
        for w in paths:
            P = np.asarray(w.path, float).copy()
            P[:, 2] -= LAYER_SEP_M
            w.path = P
    if "layer_shared" not in ctx.warnings:
        ctx.warnings.append("layer_shared")


def _safe_profile(w: WorkItem, ctx: DecompCtx) -> None:
    """作业航线的逐段高度复核（含循环航线的闭合段）：某段两端较低者低于走廊 DSM 净空高度时，在该端点处插入垂直过渡点（先垂直到
    净空高度、水平飞过、再垂直回到原高度），停留航点（看洞）与看洞映射按新序号重排。D2-FX-R2-tasking 第 2 次（验收第 1 轮 D2-AC-16）：看洞点与
    闭合段的高度此前不复核（`_prepend_looks` 以看洞高度覆盖走廊高度、`_close_loop` 直连时不插点），巡边与值守航线在
    (21.7, −215.7, 127) → (47.1, −353.3, 60) 的斜降段穿过 110 m 的楼，细校验 102 每架次三次后返航。端点自身的高度已满足 DSM 净空，
    垂直过渡段安全。"""
    P = getattr(w, "path", None)
    if P is None or len(P) < 2 or not callable(getattr(ctx, "dsm", None)):
        return
    P = np.asarray(P, float)
    try:
        out: list[np.ndarray] = [P[0]]
        idx = {0: 0}
        changed = False

        def leg(a: np.ndarray, b: np.ndarray) -> list[np.ndarray]:
            top = _corridor_top(ctx, a, b)
            if min(float(a[2]), float(b[2])) >= top - 0.5:
                return [b]
            mid = []
            if float(a[2]) < top - 0.5:
                mid.append(np.array([a[0], a[1], top]))
            if float(b[2]) < top - 0.5:
                mid.append(np.array([b[0], b[1], top]))
            return [*mid, b]

        for i in range(len(P) - 1):
            seg = leg(P[i], P[i + 1])
            changed |= len(seg) > 1
            out.extend(seg)
            idx[i + 1] = len(out) - 1
        if getattr(w, "closed", False) and len(P) > 2:
            tail = leg(P[-1], P[0])[:-1]           # 闭合段（执行器在末尾补首点）
            changed |= len(tail) > 0
            out.extend(tail)
    except Exception:
        return
    if not changed:
        return
    w.path = np.asarray(out, float)
    w.dwell = {idx[i]: v for i, v in (w.dwell or {}).items() if i in idx}
    looks = (w.needs or {}).get("looks")
    if looks:
        w.needs = dict(w.needs)
        w.needs["looks"] = {str(idx[int(k)]): v for k, v in looks.items() if int(k) in idx}


def _flatten_fw(items: list) -> None:
    """全部可用机型为固定翼或垂起机时，作业航线取整条航线的最高点作为恒定高度（D2-FX-R2：S7 中值守航线在楼群上空 160 m、条带
    110 m 之间起伏，固定翼在 110–120 m 撤离后转场，爬升率跟不上前方 133 m 的楼而撞楼；固定翼只在作业开始前一次爬升到位）。
    看洞停留点（航线上的驻留顶点）随之抬高，水平位置不变。"""
    for w in items:
        P = getattr(w, "path", None)
        if P is None or len(P) < 2:
            continue
        P = np.asarray(P, float).copy()
        z = float(P[:, 2].max())
        P[:, 2] = z
        w.path = P
