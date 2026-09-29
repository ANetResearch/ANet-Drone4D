"""剧本几何复核（M16 §6.2.4、§6.4.3、§6.4.8、§7.3.4、§9.2；SCN-E002、SCN-E003）。

- 航迹：分段线性 `(t, x, y, z)` 关键点 → 统一时刻采样 → 两两最小三维间距（`min_pairwise_separation`）；
- S1 运动学航迹（移植 `.cache/research/biz12/s1_sep.py`）：home 上空爬升、转场、自上而下整圈螺旋、12 §5.8.3 返航；
- 区域相交：航线折线加缓冲与 zones 多边形求交（纯 numpy，不依赖 shapely）；
- ladder：错时起飞、入圆、环绕、返航各阶段机间三维距离的构造值（M16 §6.4.8 表）；
- `plan_polylines(doc)`：由剧本文件近似出出生点、任务航线与返航线（供区域相交与 soak 分离断言）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

__all__ = ["expanding_square", "ladder_min_separation", "min_pairwise_separation", "plan_polylines",
           "point_in_ring", "polyline_intersects_zone", "polyline_zone_distance", "s1_tracks", "sample_track"]


# ---------------------------------------------------------------- 航迹
def sample_track(pts: np.ndarray, t: np.ndarray) -> np.ndarray:
    """pts：(k, 4) 的 (t, x, y, z) 关键点（t 单调）；返回 (len(t), 3)，区间外保持端点。"""
    return np.stack([np.interp(t, pts[:, 0], pts[:, i]) for i in (1, 2, 3)], axis=1)


def min_pairwise_separation(tracks: dict[str, np.ndarray]) -> tuple[float, tuple[str, str], int]:
    """同一时刻序列上的两两最小三维距离：返回 (距离, (id_a, id_b), 采样下标)。"""
    ids = sorted(tracks)
    best, pair, k = math.inf, ("", ""), -1
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            d = np.linalg.norm(tracks[a] - tracks[b], axis=1)
            j = int(np.argmin(d))
            if float(d[j]) < best:
                best, pair, k = float(d[j]), (a, b), j
    return best, pair, k


def s1_tracks(terrain: Any, plan: dict, dt: float = 0.1) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """S1 两机运动学航迹（12 §7.2 定稿；3 m/s 爬升、6 m/s 转场与螺旋、2 m/s 返航爬升、1.5 m/s 下降、末段 1 m/s）。"""
    c, r, v, dz_nom = plan["center"], plan["radius_m"], plan["speed_mps"], plan["dz_nom_m"]
    keys: dict[str, np.ndarray] = {}
    for vid in ("p600-01", "p600-02"):
        home, (z_top, z_bot) = plan[vid]["home"], plan[vid]["z"]
        zh = terrain.surface(*home)
        az = math.atan2(home[1] - c[1], home[0] - c[0])
        entry = (c[0] + r * math.cos(az), c[1] + r * math.sin(az))
        pts: list[tuple[float, float, float, float]] = [(0.0, home[0], home[1], zh)]

        def go(p: tuple[float, float, float], dur: float, pts: list = pts) -> None:
            pts.append((pts[-1][0] + max(dur, 1e-6), *p))

        zc = max(terrain.top_along(home, entry) + 5.0, z_top)
        go((home[0], home[1], zc), (zc - zh) / 3.0)
        go((entry[0], entry[1], z_top), math.hypot(entry[0] - home[0], entry[1] - home[1]) / v)
        revs = math.ceil((z_top - z_bot) / dz_nom - 1e-9)
        n = revs * 72
        for k in range(1, n + 1):
            f = k / n
            th = az + 2 * math.pi * revs * f
            go((c[0] + r * math.cos(th), c[1] + r * math.sin(th), z_top - (z_top - z_bot) * f), 2 * math.pi * r * revs / n / v)
        z_rtl = max(z_bot, zh + 30.0, terrain.top_along(entry, home) + 5.0)
        go((entry[0], entry[1], z_rtl), (z_rtl - z_bot) / 2.0)
        go((home[0], home[1], z_rtl), math.hypot(entry[0] - home[0], entry[1] - home[1]) / v)
        go((home[0], home[1], zh + 10.0), (z_rtl - zh - 10.0) / 1.5)
        go((home[0], home[1], zh), 10.0)
        keys[vid] = np.asarray(pts, np.float64)
    t_end = max(p[-1, 0] for p in keys.values())
    t = np.arange(0.0, t_end + dt, dt)
    return t, {vid: sample_track(p, t) for vid, p in keys.items()}


# ---------------------------------------------------------------- 多边形
def point_in_ring(xy: np.ndarray, ring: np.ndarray) -> np.ndarray:
    """射线法（ring 可闭合可不闭合）。"""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    rg = np.asarray(ring, np.float64)
    if np.allclose(rg[0], rg[-1]):
        rg = rg[:-1]
    x, y = xy[:, 0:1], xy[:, 1:2]
    x1, y1 = rg[:, 0][None], rg[:, 1][None]
    x2, y2 = np.roll(rg[:, 0], -1)[None], np.roll(rg[:, 1], -1)[None]
    cond = (y1 > y) != (y2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xin = (x2 - x1) * (y - y1) / np.where(y2 - y1 == 0, 1e-300, y2 - y1) + x1
    return np.count_nonzero(cond & (x < xin), axis=1) % 2 == 1


def _seg_seg_dist(p1: np.ndarray, q1: np.ndarray, p2: np.ndarray, q2: np.ndarray) -> np.ndarray:
    """2D 线段两两距离（广播）：p1,q1 形状 (a, 1, 2)，p2,q2 形状 (1, b, 2)。"""

    def pt_seg(p, a, b):
        ab = b - a
        L2 = np.maximum((ab * ab).sum(-1), 1e-18)
        t = np.clip(((p - a) * ab).sum(-1) / L2, 0.0, 1.0)
        return np.linalg.norm(p - (a + ab * t[..., None]), axis=-1)

    d = np.minimum(np.minimum(pt_seg(p1, p2, q2), pt_seg(q1, p2, q2)), np.minimum(pt_seg(p2, p1, q1), pt_seg(q2, p1, q1)))

    def orient(a, b, c):
        return (b[..., 0] - a[..., 0]) * (c[..., 1] - a[..., 1]) - (b[..., 1] - a[..., 1]) * (c[..., 0] - a[..., 0])

    o1, o2 = orient(p1, q1, p2), orient(p1, q1, q2)
    o3, o4 = orient(p2, q2, p1), orient(p2, q2, q1)
    cross = (np.sign(o1) * np.sign(o2) < 0) & (np.sign(o3) * np.sign(o4) < 0)
    return np.where(cross, 0.0, d)


def _zone_rings(zone: dict) -> list[np.ndarray]:
    g = zone["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else list(g["coordinates"])
    return [np.asarray(poly[0], np.float64) for poly in polys]


def polyline_zone_distance(pl: np.ndarray, zone: dict) -> float:
    """折线（或单点）到区域多边形（外环）的最小水平距离；在区域内为 0。"""
    pl = np.asarray(pl, np.float64).reshape(-1, 2) if np.asarray(pl).ndim == 1 else np.asarray(pl, np.float64)[:, :2]
    best = math.inf
    for rg in _zone_rings(zone):
        if bool(point_in_ring(pl, rg).any()):
            return 0.0
        if len(pl) == 1:
            a = b = pl[:1]
        else:
            a, b = pl[:-1], pl[1:]
        e1, e2 = rg[:-1], rg[1:]
        d = _seg_seg_dist(a[:, None], b[:, None], e1[None], e2[None])
        best = min(best, float(d.min()))
    return best


def polyline_intersects_zone(pl: np.ndarray, zone: dict, buffer_m: float = 10.0) -> bool:
    return polyline_zone_distance(pl, zone) < buffer_m


# ---------------------------------------------------------------- 剧本航线近似
def expanding_square(datum: tuple[float, float], leg0: float, legs: int, first_heading_deg: float = 0.0,
                     turn: str = "ccw") -> np.ndarray:
    """扩展方形（首腿方位 0 为正东，逆时针；腿长 leg0、leg0、2·leg0、2·leg0、…，M10 §6.5.10）。"""
    pts = [np.asarray(datum, np.float64)]
    hdg = math.radians(first_heading_deg)
    s = 1.0 if turn == "ccw" else -1.0
    for k in range(legs):
        L = leg0 * (k // 2 + 1)
        pts.append(pts[-1] + L * np.array([math.cos(hdg), math.sin(hdg)]))
        hdg += s * math.pi / 2
    return np.asarray(pts)


def _circle(c, r, n=72) -> np.ndarray:
    a = np.linspace(0.0, 2 * math.pi, n + 1)
    return np.stack([c[0] + r * np.cos(a), c[1] + r * np.sin(a)], axis=1)


def _closed(poly) -> np.ndarray:
    p = np.asarray(poly, np.float64)[:, :2]
    return np.vstack([p, p[:1]]) if not np.allclose(p[0], p[-1]) else p


def _offset_polyline(pl: np.ndarray, off: float) -> np.ndarray:
    d = np.diff(pl, axis=0)
    n = np.stack([-d[:, 1], d[:, 0]], axis=1) / np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
    nn = np.vstack([n[:1], (n[:-1] + n[1:]) / 2, n[-1:]]) if len(n) > 1 else np.vstack([n, n])
    return pl + off * nn


def mission_polylines(m: dict, homes: dict[str, np.ndarray]) -> list[np.ndarray]:
    g, p = m["generator"], m.get("params") or {}
    if g == "helix_scan":
        return [_circle(p["center_enu_m"], p["radius_m"])]
    if g == "orbit":
        cs = [homes[v] for v in m["vehicle_ids"]] if p.get("center_enu_m") in (None, "home") else [p["center_enu_m"]]
        return [_circle(c, p["radius_m"], 24) for c in cs]
    if g == "lawnmower":
        return [_closed(p["polygon_enu_m"])]
    if g == "terrain_follow":
        if "area" in p:
            return [_closed(p["area"]["polygon_enu_m"])]
        return [np.asarray(p["path_enu_m"], np.float64)]
    if g == "expanding_square":
        return [expanding_square(tuple(p["datum_enu_m"]), p.get("leg0_m", 50.0), int(p.get("legs", 8)),
                                 p.get("first_heading_deg", 0.0), p.get("turn", "ccw"))]
    if g == "corridor":
        pl = np.asarray(p["polyline_enu_m"], np.float64)
        return [pl, _offset_polyline(pl, p["offset_m"]), _offset_polyline(pl, -p["offset_m"])]
    if g == "formation":
        pl = np.asarray(p.get("anchor_path_enu_m") or [], np.float64)
        return [pl] if len(pl) else []
    if g == "follow_path":
        return [np.asarray(p["waypoints_enu_m"], np.float64)[:, :2]]
    return []


def plan_polylines(doc: dict, vehicles: list[dict] | None = None, missions: list[dict] | None = None
                   ) -> dict[str, list[np.ndarray]]:
    """{'homes': [点], 'missions': [折线], 'returns': [任务区到出生点的直线]}（近似，用于区域相交与剧本间分离）。"""
    if vehicles is None or missions is None:
        from awr.sim.mission.scenario_loader import expand_vehicle_sets

        vehicles, missions = expand_vehicle_sets(doc)
    homes = {v["vehicle_id"]: np.asarray(v["home_enu_m"][:2], np.float64) for v in vehicles}
    out: dict[str, list[np.ndarray]] = {"homes": [h[None] for h in homes.values()], "missions": [], "returns": []}
    for m in missions:
        pls = mission_polylines(m, homes)
        out["missions"] += pls
        if not pls or m.get("generator") == "orbit":
            continue
        for vid in m.get("vehicle_ids") or []:
            h = homes.get(vid)
            if h is None:
                continue
            allp = np.vstack(pls)
            near = allp[int(np.argmin(np.linalg.norm(allp - h, axis=1)))]
            far = allp[int(np.argmax(np.linalg.norm(allp - h, axis=1)))]
            out["returns"] += [np.vstack([near, h]), np.vstack([far, h])]
    return out


# ---------------------------------------------------------------- ladder 构造值（M16 §6.4.8）
def ladder_min_separation(sets: list[dict], home_z: np.ndarray | None = None, ground_z: np.ndarray | None = None,
                          v_climb: float = 3.0, orbit_r: float = 3.0, dt: float = 0.25,
                          radius_m: float = 40.0) -> dict[str, float]:
    """按阶段给出机间三维距离的构造下界（相位任意的最坏情况）：

    climb：各层在 `start.at_s` 从出生点高度（`home_z`，缺省 0，即 AGL 基准）以 `v_climb` 爬升到 `agl_m`（相对 `ground_z`）；
    entry/orbit：到达本层高度后水平偏离出生点至多 `orbit_r`（相位任意：水平距离按两机各偏 `orbit_r` 的最坏情况扣减）；
    return：返航为爬升的逆过程（同速下降，低层先到），与爬升对称。只在水平距离 < `radius_m` 的机对之间计算。
    `climb` 与 `entry` 只统计两机都已离地的时刻（`min_separation_m` 的口径）；一机在地面的最小值另记 `ground_air`。
    """
    homes, layer, start, agl = [], [], [], []
    for li, s in enumerate(sets):
        lay = s["layout"]
        ox, oy = lay["origin_enu_m"][0], lay["origin_enu_m"][1]
        cols, sp = int(lay.get("cols") or math.ceil(math.sqrt(s["count"]))), float(lay["spacing_m"])
        mi = s.get("mission") or {}
        for k in range(int(s["count"])):
            r_, c_ = divmod(k, cols)
            homes.append((ox + c_ * sp, oy + r_ * sp))
            layer.append(li)
            start.append(float((mi.get("start") or {}).get("at_s", 0.0)))
            agl.append(float((mi.get("params") or {}).get("agl_m", 60.0)))
    H = np.asarray(homes, np.float64)
    z0 = np.zeros(len(H)) if home_z is None else np.asarray(home_z, np.float64)          # 出生高度（相对 ground）
    zt = np.asarray(agl, np.float64)                                                     # 目标高度（相对 ground）
    gz = np.zeros(len(H)) if ground_z is None else np.asarray(ground_z, np.float64)
    st = np.asarray(start, np.float64)
    t_arr = st + np.maximum(zt - z0, 0.0) / v_climb
    t_end = float(t_arr.max()) + 5.0
    ts = np.arange(0.0, t_end + dt, dt)
    # 候选机对：水平距离 < radius_m
    dx = H[:, None, :] - H[None, :, :]
    dh = np.linalg.norm(dx, axis=2)
    ii, jj = np.nonzero(np.triu(dh < radius_m, k=1))
    dh = dh[ii, jj]
    res = {"ground": float(dh.min()) if len(dh) else math.inf}
    ground_air, climb_min, entry_min = math.inf, math.inf, math.inf
    for t in ts:
        z = gz + np.clip(z0 + v_climb * np.maximum(t - st, 0.0), None, np.maximum(zt, z0))
        arrived = t >= t_arr
        # 已到达的机体入圆（最多偏离 orbit_r），未到达的在出生点正上方
        off = np.where(arrived, orbit_r, 0.0)
        hmin = np.maximum(dh - off[ii] - off[jj], 0.0)
        fly_i, fly_j = t > st[ii], t > st[jj]
        d3 = np.sqrt(hmin ** 2 + (z[ii] - z[jj]) ** 2)
        one = fly_i ^ fly_j                           # 一机在地面（不计入 min_separation_m，只作参考）
        if one.any():
            ground_air = min(ground_air, float(d3[one].min()))
        both_fly = fly_i & fly_j
        both_arr = arrived[ii] & arrived[jj]
        m = both_fly & ~both_arr
        if m.any():
            climb_min = min(climb_min, float(d3[m].min()))
        if both_arr.any():
            entry_min = min(entry_min, float(d3[both_arr].min()))
    zf = gz + np.maximum(zt, z0)
    orbit = np.sqrt(np.maximum(dh - 2 * orbit_r, 0.0) ** 2 + (zf[ii] - zf[jj]) ** 2)
    res.update({"ground_air": ground_air, "climb": climb_min, "entry": entry_min,
                "orbit": float(orbit.min()) if len(orbit) else math.inf, "return": climb_min})
    res["any_phase"] = min(res["climb"], res["entry"], res["orbit"], res["return"])
    return res
