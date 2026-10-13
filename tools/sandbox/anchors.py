#!/usr/bin/env python3
"""世界锚点：沙盒场景模板（T1–T6）与 S7 的几何来源（WP-13，M16；AWR-04 §10.4、ADR-112）。

每个世界一份 `scenarios/sandbox/anchors/<world>.json`（`awr.sandbox.anchors.v1`），由本工具在已构建的世界几何（M04 WorldQuery：
DSM 柱体语义、DTM、视线）上搜索并逐项验证后写出；`gen_templates.py` 只读锚点文件生成模板（不需要世界数据，CI 可逐字节复核）。

锚点与验证条件（缺省参数见 `Params`）：

| 锚点 | 条件 |
|---|---|
| `nest_box` 机巢箱 | 自 D1 free 剧本的平坦地块（`CityFacts.free_pad`）起螺旋搜索：半径 `r_flat_nest` 内 HAG ≤ `hag_flat`；距世界边界 ≥ `margin_m`；在 zones 外扩 `zone_buffer_m` 之外 |
| `nest_pad` VTOL 起降台 | 距机巢箱 40–90 m、同样平坦、在值守区外 |
| `aoi_guard` 值守与巡查区（T3、T5、S7） | 边长 `aoi_side_m` 的正方形；中心距机巢箱 `aoi_side/2 + 40…320 m`；在世界内、zones 外；开阔比例（HAG ≤ 1.5 m）≥ `open_min`；`H_floor = P99(DSM) + 20 m ≤ h_cap_guard`；机巢在区外；面积 ≤ 1 km² |
| `target_static` 静止的人 | 值守区中心附近的开阔格（HAG ≤ 0.3 m，半径 3 m）；8 个方向、水平 180 m、AGL 60 m 的视点中视线可见 ≥ 6 个；朝向背离机巢（扇形敏感体背对接近方向） |
| `walk_regions` 游走区（3 个） | 60 m 见方、在值守区内、开阔比例 ≥ 0.6，面积 100 m²–1 km²（M18 请求第 3 条）；第 1 个供 T5 与 S7 |
| `watch_point` 定点站位（T1、T6） | 自静止的人朝机巢方向水平 `watch_dist_m`；该点 AGL 60 m 与人头视线可见；DSM 余量 ≥ 30 m |
| `polyline` 巡线（T2） | 值守区中心为心、±150 m × ±110 m 的四个角点（逆时针，自距机巢最近的角起）；各点 DSM 低于 AGL 70 m 至少 30 m |
| `aoi_scan` GSD 扫描验证区（T4） | 边长 `scan_side_m`；中心距机巢 120–420 m；开阔比例 ≥ `open_min`；`H_floor ≤ h_cap_scan`；与值守区不重叠 |
| `road` 车辆路径（S7） | 值守区中心附近的东西或南北向线段（`road_len_m`，偏移 0、±40、±80 m），离静止的人 ≥ 30 m，沿线采样开阔比例最高者 |

用法：
    python tools/sandbox/anchors.py --world synthcity            # 搜索并写出（覆盖）
    python tools/sandbox/anchors.py --all
    python tools/sandbox/anchors.py --check [--world w]          # 按世界几何复核已提交的锚点（退出码 0 通过、1 不通过、14 世界缺失）
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sbcommon as C

SCHEMA = "awr.sandbox.anchors.v1"
EXIT_OK, EXIT_FAIL, EXIT_ENV = 0, 1, 14


@dataclass(frozen=True)
class Params:
    margin_m: float = 60.0            # 距世界边界
    zone_buffer_m: float = 60.0       # zones 外扩
    hag_flat: float = 0.6             # 机巢平坦判据（HAG，m）
    r_flat_nest: float = 10.0         # 机巢平坦半径（m）
    clear40_m: float = 4.0            # 机巢 40 m 内障碍高度上限（爬升与 VTOL 过渡净空）
    clear80_m: float = 20.0           # 机巢 80 m 内障碍高度上限
    aoi_side_m: float = 400.0         # 值守区边长（面积 0.16 km²）
    scan_side_m: float = 160.0        # 扫描验证区边长
    open_min: float = 0.35            # 开阔比例下限（HAG ≤ 1.5 m，4 m 格取最大，偏保守）
    h_cap_guard: float = 120.0        # 值守区 H_floor 上限（AGL 口径，m）
    h_cap_scan: float = 110.0         # 扫描区 H_floor 上限（低于 area_scan_gsd 缺省 H_cap 120 m）
    watch_dist_m: float = 180.0       # 定点站位到静止的人的水平距离
    walk_side_m: float = 60.0
    road_len_m: float = 360.0
    los_agl_m: float = 60.0
    los_ring_m: float = 180.0


# ---------------------------------------------------------------- 世界几何
class WorldLayers:
    """4 m 降采样的 HAG（2×2 取最大，偏保守）与 M04 WorldQuery（DSM、DTM、视线）。"""

    def __init__(self, world_id: str, worlds_dir: Path | None = None) -> None:
        from awr.world.geometry.query import open_world_query

        base = Path(worlds_dir or os.environ.get("AWR_WORLDS_DIR") or C.ROOT / "worlds")
        self.world_id = world_id
        self.q = open_world_query(base / world_id)
        g = self.q.dsm_grid()
        a = np.asarray(g.a, np.float32)
        h, w = (a.shape[0] // 2) * 2, (a.shape[1] // 2) * 2
        a = a[:h, :w].reshape(h // 2, 2, w // 2, 2).max(axis=(1, 3))
        self.cell = float(g.cell_m) * 2.0
        self.x0, self.y0 = float(g.x0_m), float(g.y0_m)
        ny, nx = a.shape
        xs = self.x0 + (np.arange(nx) + 0.5) * self.cell
        ys = self.y0 + (np.arange(ny) + 0.5) * self.cell
        XX, YY = np.meshgrid(xs, ys)
        grd = self.q.ground_dtm(np.stack([XX.ravel(), YY.ravel()], 1)).reshape(a.shape).astype(np.float32)
        self.dsm = a
        self.ground = grd
        self.hag = a - grd
        self.xs, self.ys = xs, ys
        b = self.q.bounds_m
        self.bounds = ((float(b[0][0]), float(b[0][1])), (float(b[1][0]), float(b[1][1])))
        from awr.datasets.scenarios.authoring import facts

        self.facts = facts(world_id)
        self.zones = [(tuple(map(float, z.center)), float(z.radius_m), z.zone_id) for z in self.facts.zones]

    # 栅格访问
    def _ij(self, x: float, y: float) -> tuple[int, int]:
        i = int(np.clip((y - self.y0) // self.cell, 0, self.hag.shape[0] - 1))
        j = int(np.clip((x - self.x0) // self.cell, 0, self.hag.shape[1] - 1))
        return i, j

    def window(self, arr: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
        i0, j0 = self._ij(x0, y0)
        i1, j1 = self._ij(x1, y1)
        return arr[i0:i1 + 1, j0:j1 + 1]

    def disk(self, arr: np.ndarray, x: float, y: float, r: float) -> np.ndarray:
        w = self.window(arr, x - r, y - r, x + r, y + r)
        i0, j0 = self._ij(x - r, y - r)
        ii, jj = np.mgrid[0:w.shape[0], 0:w.shape[1]]
        cx = self.x0 + (j0 + jj + 0.5) * self.cell
        cy = self.y0 + (i0 + ii + 0.5) * self.cell
        m = (cx - x) ** 2 + (cy - y) ** 2 <= max(r, self.cell * 0.75) ** 2
        return w[m]

    def hag_max(self, x: float, y: float, r: float) -> float:
        v = self.disk(self.hag, x, y, r)
        return float(v.max()) if v.size else math.inf

    def open_frac_rect(self, P: list) -> float:
        xs = [float(p[0]) for p in P]
        ys = [float(p[1]) for p in P]
        w = self.window(self.hag, min(xs), min(ys), max(xs), max(ys))
        return float((w <= 1.5).mean()) if w.size else 0.0

    def h_floor_rect(self, P: list) -> float:
        """`H_floor = P99(DSM) + 20 m`，以区域地面中位数为基准（AGL 口径）。"""
        xs = [float(p[0]) for p in P]
        ys = [float(p[1]) for p in P]
        d = self.window(self.dsm, min(xs), min(ys), max(xs), max(ys))
        g = self.window(self.ground, min(xs), min(ys), max(xs), max(ys))
        if not d.size:
            return math.inf
        return float(np.percentile(d, 99) - np.median(g) + 20.0)

    def dsm_at(self, x: float, y: float) -> float:
        return float(self.q.height_dsm(np.array([[x, y]], np.float64))[0])

    def ground_at(self, x: float, y: float) -> float:
        return float(self.q.ground_dtm(np.array([[x, y]], np.float64))[0])

    def los(self, a: tuple[float, float, float], b: tuple[float, float, float]) -> bool:
        return bool(self.q.segment_los(np.asarray(a, np.float64), np.asarray(b, np.float64)))

    # 约束
    def in_bounds(self, P: list, margin: float) -> bool:
        (x0, y0), (x1, y1) = self.bounds
        return all(x0 + margin <= float(p[0]) <= x1 - margin and y0 + margin <= float(p[1]) <= y1 - margin for p in P)

    def clear_of_zones(self, P: list, buffer_m: float) -> bool:
        """多边形（或点集）与 zones 圆（外扩 buffer）不相交。"""
        for (zx, zy), r, _zid in self.zones:
            rr = r + buffer_m
            if len(P) >= 3 and (C.inside((zx, zy), P) or C.dist_to_polygon_edge((zx, zy), P) <= rr):
                return False
            if len(P) < 3 and any(C.dist(p, (zx, zy)) <= rr for p in P):
                return False
        return True


# ---------------------------------------------------------------- 搜索
def _spiral(x: float, y: float, step: float, r_max: float):
    yield (x, y)
    r = step
    while r <= r_max:
        n = max(8, int(2 * math.pi * r / step))
        for k in range(n):
            a = 2 * math.pi * k / n
            yield (x + r * math.cos(a), y + r * math.sin(a))
        r += step


def _flat_point(L: WorldLayers, x: float, y: float, r_flat: float, P: Params, avoid: list | None = None,
                r_max: float = 400.0) -> tuple[float, float] | None:
    for cx, cy in _spiral(x, y, 4.0, r_max):
        if not L.in_bounds([(cx, cy)], P.margin_m) or not L.clear_of_zones([(cx, cy)], P.zone_buffer_m):
            continue
        if avoid and any(C.inside((cx, cy), poly) or C.dist_to_polygon_edge((cx, cy), poly) < 20.0 for poly in avoid):
            continue
        if L.hag_max(cx, cy, r_flat) <= P.hag_flat:
            return (cx, cy)
    return None


def _sky_visible(L: WorldLayers, x: float, y: float, P: Params) -> int:
    zt = L.ground_at(x, y) + 1.5
    n = 0
    for k in range(8):
        a = 2 * math.pi * k / 8
        vx, vy = x + P.los_ring_m * math.cos(a), y + P.los_ring_m * math.sin(a)
        vz = max(L.ground_at(vx, vy), L.dsm_at(vx, vy) - 20.0) + P.los_agl_m
        n += L.los((vx, vy, vz), (x, y, zt))
    return n


def _open_target(L: WorldLayers, x: float, y: float, P: Params, region: list | None = None, r_max: float = 60.0
                 ) -> tuple[float, float] | None:
    for cx, cy in _spiral(x, y, 4.0, r_max):
        if region is not None and not C.inside((cx, cy), region):
            continue
        cx, cy = C.pt((cx, cy))
        if L.hag_max(cx, cy, 3.0) <= 0.3 and not depressed(L, cx, cy) and _sky_visible(L, cx, cy, P) >= 6:
            return (cx, cy)
    return None


def _watch_point(L: WorldLayers, tgt: tuple[float, float], facing: float, P: Params) -> tuple[float, float] | None:
    """定点站位：在静止的人背后的 ±105° 弧内（扇形敏感体半角 60° 之外留 15° 余量）、水平 `watch_dist_m`，AGL 60 m 与人头视线可见；
    自正背后起按 15° 交替向两侧搜索。"""
    zt = L.ground_at(*tgt) + 1.5
    for k in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6, 7, -7):
        a = facing + math.pi + math.radians(15.0 * k)
        w = tuple(C.pt((tgt[0] + P.watch_dist_m * math.cos(a), tgt[1] + P.watch_dist_m * math.sin(a))))
        if not L.clear_of_zones([w], P.zone_buffer_m) or not L.in_bounds([w], P.margin_m):
            continue
        if L.hag_max(*w, 12.0) > P.los_agl_m - 35.0:                  # 站位下方净空 ≥ 35 m
            continue
        if all(L.los((w[0], w[1], L.ground_at(*w) + agl), (tgt[0], tgt[1], zt)) for agl in (P.los_agl_m - 5.0, P.los_agl_m)):
            return w
    return None


def _polyline(L: WorldLayers, cx: float, cy: float, nest: tuple[float, float]) -> list | None:
    for hx, hy in ((150.0, 110.0), (130.0, 100.0), (170.0, 120.0), (110.0, 80.0), (190.0, 140.0)):
        for ox, oy in ((0.0, 0.0), (30.0, 0.0), (-30.0, 0.0), (0.0, 30.0), (0.0, -30.0)):
            x, y = cx + ox, cy + oy
            corners = [(x - hx, y - hy), (x + hx, y - hy), (x + hx, y + hy), (x - hx, y + hy)]
            if all(L.ground_at(*p) + 70.0 - L.dsm_at(*p) >= 30.0 and L.hag_max(*p, 10.0) <= 40.0 for p in corners):
                k0 = min(range(4), key=lambda i: C.dist(corners[i], nest))
                return corners[k0:] + corners[:k0]
    return None


def _walk_regions(L: WorldLayers, aoi: list, tgt: tuple[float, float], away: float, P: Params) -> list | None:
    """三个游走区（60 m 见方、在值守区内、开阔比例 ≥ 0.5、彼此中心距 ≥ 90 m、距静止的人 ≥ 80 m）：期望位置为远侧
    （背离机巢 120 m）与左右两侧，按到期望位置的距离贪心选取。"""
    cx, cy = C.centroid(aoi)
    fx, fy = math.cos(away), math.sin(away)
    px, py = -fy, fx
    want = [(cx + o[0] * fx + o[1] * px, cy + o[0] * fy + o[1] * py) for o in ((120.0, 0.0), (40.0, 110.0), (40.0, -110.0))]
    xs = [float(p[0]) for p in aoi]
    ys = [float(p[1]) for p in aoi]
    h = P.walk_side_m / 2.0
    cand = []
    for x in np.arange(min(xs) + h + 4.0, max(xs) - h - 4.0 + 1e-6, 20.0):
        for y in np.arange(min(ys) + h + 4.0, max(ys) - h - 4.0 + 1e-6, 20.0):
            if C.dist((x, y), tgt) < 80.0:
                continue
            reg = C.square(float(x), float(y), P.walk_side_m)
            of = L.open_frac_rect(reg)
            if of >= 0.52 and not any(depressed(L, float(q[0]), float(q[1])) for q in [*reg, (x, y)]):
                cand.append((float(x), float(y), reg))
    out: list = []
    for w in want:
        cand.sort(key=lambda c: (C.dist((c[0], c[1]), w), c[0], c[1]))
        pick = next((c for c in cand if all(C.dist((c[0], c[1]), C.centroid(r)) >= 90.0 for r in out)), None)
        if pick is None:
            return None
        out.append(pick[2])
    return out


SLOT_OFFSETS = ((-5.0, -2.5), (0.0, -2.5), (5.0, -2.5), (-5.0, 2.5), (0.0, 2.5), (5.0, 2.5), (-5.0, 0.0), (0.0, 0.0))
SLOT_SPREAD_MAX = 0.4      # 槽位 DSM 极差上限（M08 出生高度容差 [DSM − 0.05, DSM + 0.5]，机巢 z 取槽位 DSM 最大值）


def slot_dsm(L: WorldLayers, x: float, y: float) -> tuple[float, float]:
    """机巢槽位（M20 `slot_offsets`，6 槽与 2 槽的并集，3 列 5 m 网格）处 DSM 的最小值与最大值（精确 DSM，非降采样）。"""
    xy = np.array([[x + dx, y + dy] for dx, dy in SLOT_OFFSETS], np.float64)
    h = L.q.height_dsm(xy)
    return float(np.min(h)), float(np.max(h))


def nest_z(L: WorldLayers, x: float, y: float) -> float:
    """机巢的显式高度：槽位 DSM 最大值向上取 1 cm（各槽位出生高度落在 M08 容差内，M20 槽位一律用机巢高度）。"""
    return math.ceil(slot_dsm(L, x, y)[1] * 100.0 - 1e-6) / 100.0


def depressed(L: WorldLayers, x: float, y: float, r: float = 150.0) -> bool:
    """低洼（河道、水面、下沉广场）：地面比 ±r 范围的地面中位数低 1 m 以上。世界包没有水体图层，以此排除合成城市的河道等。"""
    g = L.window(L.ground, x - r, y - r, x + r, y + r)
    return bool(g.size) and L.ground_at(x, y) < float(np.median(g)) - 1.0


def _clear_site(L: WorldLayers, x: float, y: float, P: Params) -> bool:
    """机巢场址：半径 r_flat 内平坦、槽位 DSM 极差 ≤ 0.4 m，40 m 内无高于 clear40 的障碍、80 m 内无高于 clear80 的障碍
    （爬升与过渡的净空）。"""
    if L.hag_max(x, y, P.r_flat_nest) > P.hag_flat or L.hag_max(x, y, 40.0) > P.clear40_m or L.hag_max(x, y, 80.0) > P.clear80_m:
        return False
    lo, hi = slot_dsm(L, x, y)
    return hi - lo <= SLOT_SPREAD_MAX and not depressed(L, x, y)


def _nest_site(L: WorldLayers, x: float, y: float, P: Params, *, avoid: list, r_max: float = 160.0,
               keep_from: tuple[tuple[float, float], float] | None = None) -> tuple[float, float] | None:
    for cx, cy in _spiral(x, y, 8.0, r_max):
        cx, cy = C.pt((cx, cy))
        if not L.in_bounds([(cx, cy)], P.margin_m) or not L.clear_of_zones([(cx, cy)], P.zone_buffer_m):
            continue
        if any(C.inside((cx, cy), poly) or C.dist_to_polygon_edge((cx, cy), poly) < 30.0 for poly in avoid):
            continue
        if keep_from is not None and C.dist((cx, cy), keep_from[0]) < keep_from[1]:
            continue
        if _clear_site(L, cx, cy, P):
            return (cx, cy)
    return None


def _aoi_candidates(L: WorldLayers, ref: tuple[float, float], side: float, d_list: tuple[float, ...], P: Params,
                    h_cap: float, avoid: list | None = None, open_min: float | None = None, avoid_pts: tuple = ()):
    om = P.open_min if open_min is None else open_min
    for d in d_list:
        for k in range(24 if d > 0 else 1):
            a = math.radians(15.0 * k)
            cx, cy = ref[0] + d * math.cos(a), ref[1] + d * math.sin(a)
            aoi = C.square(cx, cy, side)
            if not L.in_bounds(aoi, 30.0) or not L.clear_of_zones(aoi, P.zone_buffer_m):
                continue
            if avoid and (any(C.inside(p, poly) for poly in avoid for p in aoi) or any(C.inside(p, aoi) for poly in avoid for p in poly)
                          or any(C.dist(C.centroid(poly), (cx, cy)) < side for poly in avoid)):
                continue
            if any(C.inside(q, aoi) or C.dist_to_polygon_edge(q, aoi) < 30.0 for q in avoid_pts):
                continue
            of = L.open_frac_rect(aoi)
            if of < om:
                continue
            hf = L.h_floor_rect(aoi)
            if hf > h_cap:
                continue
            yield (of - d / 1500.0, d, k, aoi, of, hf)


def _edge_point(aoi: list, toward: tuple[float, float], out_m: float) -> tuple[float, float]:
    """值守区边界上朝向 `toward` 的一侧、向外 out_m 的点。"""
    cx, cy = C.centroid(aoi)
    h = (max(float(p[0]) for p in aoi) - min(float(p[0]) for p in aoi)) / 2.0
    dx, dy = toward[0] - cx, toward[1] - cy
    if abs(dx) >= abs(dy):
        return (cx + math.copysign(h + out_m, dx if dx else 1.0), cy)
    return (cx, cy + math.copysign(h + out_m, dy if dy else 1.0))


CLEAR_LEVELS = ((4.0, 20.0), (8.0, 35.0), (15.0, 60.0))   # 机巢净空分级放宽（稠密城区：40 m / 80 m 内障碍高度上限）


def search(world_id: str, P: Params | None = None, worlds_dir: Path | None = None) -> dict[str, Any]:
    """按净空分级依次尝试（`CLEAR_LEVELS`），取第一个成功的级别；所用级别写入锚点文件的 params。"""
    L = WorldLayers(world_id, worlds_dir)
    if P is not None:
        return _search(L, P)
    err: Exception | None = None
    for c40, c80 in CLEAR_LEVELS:
        try:
            return _search(L, Params(clear40_m=c40, clear80_m=c80))
        except RuntimeError as e:
            err = e
    assert err is not None
    raise err


def _search(L: WorldLayers, P: Params) -> dict[str, Any]:
    world_id = L.world_id
    ref = tuple(map(float, L.facts.free_pad))
    best = None
    for om in (0.45, P.open_min):
        cands = sorted(_aoi_candidates(L, ref, P.aoi_side_m, (0.0, 75.0, 150.0, 225.0, 300.0, 375.0, 450.0, 525.0, 600.0), P, P.h_cap_guard,
                                            open_min=om),
                       key=lambda c: (-c[0], c[1], c[2]))
        for _s, _d, _k, aoi, of, hf in cands:
            cx, cy = C.centroid(aoi)
            nest = None
            for side_ref in (ref, (cx, cy - 1.0), (cx - 1.0, cy), (cx, cy + 1.0), (cx + 1.0, cy)):
                e = _edge_point(aoi, side_ref if C.dist(side_ref, (cx, cy)) > 1e-6 else (cx, cy - 1.0), 70.0)
                nest = _nest_site(L, e[0], e[1], P, avoid=[aoi])
                if nest is not None:
                    break
            if nest is None:
                continue
            tgt = _open_target(L, cx, cy, P, region=C.square(cx, cy, 120.0))
            if tgt is None:
                continue
            away = math.atan2(tgt[1] - nest[1], tgt[0] - nest[0])          # 朝向背离机巢
            watch = _watch_point(L, tgt, away, P)
            if watch is None:
                continue
            regs = _walk_regions(L, aoi, tgt, away, P)
            if regs is None:
                continue
            pad = None
            ux, uy = nest[0] - cx, nest[1] - cy                              # 背离值守区中心
            for rot in (90.0, -90.0, 0.0, 45.0, -45.0):
                a = math.atan2(uy, ux) + math.radians(rot)
                q0 = (nest[0] + 70.0 * math.cos(a), nest[1] + 70.0 * math.sin(a))
                pad = _nest_site(L, q0[0], q0[1], P, avoid=[aoi], r_max=40.0)
                if pad is not None and 40.0 <= C.dist(pad, nest) <= 110.0:
                    break
                pad = None
            if pad is None:
                continue
            # 定点模板的机巢：站位外侧 40–160 m，距静止的人 ≥ 220 m（演示敏感体 150 m + 外扩与余量）
            u = (watch[0] - tgt[0], watch[1] - tgt[1])
            n_u = math.hypot(*u) or 1.0
            nw = _nest_site(L, watch[0] + 80.0 * u[0] / n_u, watch[1] + 80.0 * u[1] / n_u, P, avoid=[], r_max=160.0,
                            keep_from=(tgt, 220.0))
            if nw is None and C.dist(nest, watch) <= 300.0 and C.dist(nest, tgt) >= 220.0:
                nw = nest                                                    # 退回值守区旁的机巢箱
            if nw is None:
                continue
            best = (aoi, of, hf, nest, pad, tgt, away, watch, regs, nw)
            break
        if best is not None:
            break
    if best is None:
        raise RuntimeError(f"{world_id}: no guard area satisfies the anchor conditions")
    aoi, of, hf, nest, pad, tgt, away, watch, regs, nw = best
    cx, cy = C.centroid(aoi)
    # 巡线：值守区中心附近为心的矩形四角（逆时针，自距机巢最近的角起），各角 DSM 余量 ≥ 30 m
    poly = _polyline(L, cx, cy, nest)
    if poly is None:
        raise RuntimeError(f"{world_id}: no polyline with clearance around the guard area")
    # 扫描验证区：距机巢 140–420 m，不与值守区重叠
    scan = next(iter(sorted(_aoi_candidates(L, nest, P.scan_side_m, (140.0, 200.0, 260.0, 340.0, 420.0), P, P.h_cap_scan,
                                            avoid=[aoi], avoid_pts=(nest, pad, nw)), key=lambda c: (-c[0], c[1], c[2]))), None)
    if scan is None:
        raise RuntimeError(f"{world_id}: no GSD scan area satisfies the anchor conditions")
    # 车辆路径：穿过值守区中心的东西或南北向线段
    road = None
    for horiz in (True, False):
        for off in (0.0, 40.0, -40.0, 80.0, -80.0):
            a = (cx - P.road_len_m / 2, cy + off) if horiz else (cx + off, cy - P.road_len_m / 2)
            b = (cx + P.road_len_m / 2, cy + off) if horiz else (cx + off, cy + P.road_len_m / 2)
            s = np.linspace(0.0, 1.0, 73)
            pts = np.c_[a[0] + s * (b[0] - a[0]), a[1] + s * (b[1] - a[1])]
            if min(C.dist(q, tgt) for q in pts) < 30.0 or any(depressed(L, float(x), float(y)) for x, y in pts[::6]):
                continue                                          # 不贴近静止的人，不走河道与低洼
            f = float(np.mean([L.hag_max(x, y, 2.0) <= 0.5 for x, y in pts]))
            if road is None or f > road[0] + 1e-9:
                road = (f, a, b)
    doc = {
        "schema": SCHEMA, "world_id": world_id, "tool": "tools/sandbox/anchors.py", "params": asdict(P),
        "bounds_m": [C.pt(L.bounds[0]), C.pt(L.bounds[1])],
        "nest_box": [*C.pt(nest), nest_z(L, *nest)], "nest_pad": [*C.pt(pad), nest_z(L, *pad)],
        "nest_watch": [*C.pt(nw), nest_z(L, *nw)],
        "aoi_guard": C.ccw([C.pt(p) for p in aoi]), "aoi_scan": C.ccw([C.pt(p) for p in scan[3]]),
        "target_static": {"pos": C.pt(tgt), "yaw_rad": C.yaw(away)},
        "walk_regions": [C.ccw([C.pt(p) for p in r]) for r in regs],
        "watch_point": C.pt(watch), "polyline": [C.pt(p) for p in poly],
        "road": [C.pt(road[1]), C.pt(road[2])],
    }
    doc["checks"] = measure(doc, L, P)
    return doc


# ---------------------------------------------------------------- 复核
def measure(doc: dict, L: WorldLayers, P: Params) -> dict[str, Any]:
    """锚点的实测值（写入文件，`--check` 时重算并与条件比较）。"""
    aoi, scan = doc["aoi_guard"], doc["aoi_scan"]
    tg = doc["target_static"]["pos"]
    w = doc["watch_point"]
    zw = L.ground_at(*w) + P.los_agl_m
    return {
        "nest_box_hag_max_m": C.rnd(L.hag_max(*doc["nest_box"][:2], P.r_flat_nest), 2),
        "nest_pad_hag_max_m": C.rnd(L.hag_max(*doc["nest_pad"][:2], P.r_flat_nest), 2),
        "nest_pad_dist_m": C.rnd(C.dist(doc["nest_box"], doc["nest_pad"])),
        "nest_clear40_m": [C.rnd(L.hag_max(*doc[k][:2], 40.0), 2) for k in ("nest_box", "nest_pad", "nest_watch")],
        "nest_clear80_m": [C.rnd(L.hag_max(*doc[k][:2], 80.0), 2) for k in ("nest_box", "nest_pad", "nest_watch")],
        "nest_watch_hag_max_m": C.rnd(L.hag_max(*doc["nest_watch"][:2], P.r_flat_nest), 2),
        "nest_slot_dsm_m": [[C.rnd(v, 2) for v in slot_dsm(L, *doc[k][:2])] for k in ("nest_box", "nest_pad", "nest_watch")],
        "nest_watch_target_dist_m": C.rnd(C.dist(doc["nest_watch"], doc["target_static"]["pos"])),
        "aoi_guard_area_km2": C.rnd(C.area(aoi) / 1e6, 4), "aoi_guard_open_frac": C.rnd(L.open_frac_rect(aoi), 3),
        "aoi_guard_h_floor_m": C.rnd(L.h_floor_rect(aoi)),
        "aoi_guard_nest_dist_m": C.rnd(C.dist(doc["nest_box"], C.centroid(aoi))),
        "aoi_scan_area_km2": C.rnd(C.area(scan) / 1e6, 4), "aoi_scan_open_frac": C.rnd(L.open_frac_rect(scan), 3),
        "aoi_scan_h_floor_m": C.rnd(L.h_floor_rect(scan)),
        "target_hag_max_m": C.rnd(L.hag_max(*tg, 3.0), 2), "target_sky_los_8": _sky_visible(L, *tg, P),
        "watch_target_dist_m": C.rnd(C.dist(w, tg)), "watch_hag_max_m": C.rnd(L.hag_max(*w, 12.0), 2),
        "watch_los": L.los((w[0], w[1], zw), (tg[0], tg[1], L.ground_at(*tg) + 1.5)),
        "depressed": [k for k in ("nest_box", "nest_pad", "nest_watch") if depressed(L, *doc[k][:2])]
        + (["target_static"] if depressed(L, *doc["target_static"]["pos"]) else [])
        + [f"walk_regions[{i}]" for i, r in enumerate(doc["walk_regions"]) if any(depressed(L, *q) for q in r)],
        "walk_open_frac": [C.rnd(L.open_frac_rect(r), 3) for r in doc["walk_regions"]],
        "walk_area_m2": [C.rnd(C.area(r)) for r in doc["walk_regions"]],
        "polyline_clear_m": [C.rnd(L.ground_at(*p) + 70.0 - L.dsm_at(*p)) for p in doc["polyline"]],
        "road_open_frac": C.rnd(float(np.mean([L.hag_max(x, y, 2.0) <= 0.5 for x, y in np.c_[
            np.linspace(doc["road"][0][0], doc["road"][1][0], 73), np.linspace(doc["road"][0][1], doc["road"][1][1], 73)]])), 3),
    }


def problems(doc: dict, L: WorldLayers, P: Params | None = None) -> list[str]:
    """按 `Params` 的条件复核锚点；返回问题列表（空为通过）。"""
    P = P or Params(**{k: v for k, v in (doc.get("params") or {}).items() if k in Params.__dataclass_fields__})
    m = measure(doc, L, P)
    out = []
    if m["nest_box_hag_max_m"] > P.hag_flat:
        out.append(f"nest_box not flat: {m['nest_box_hag_max_m']}")
    if m["nest_pad_hag_max_m"] > P.hag_flat:
        out.append(f"nest_pad not flat: {m['nest_pad_hag_max_m']}")
    if not 30.0 <= m["nest_pad_dist_m"] <= 100.0:
        out.append(f"nest_pad distance {m['nest_pad_dist_m']}")
    if m["nest_watch_hag_max_m"] > P.hag_flat or m["nest_watch_target_dist_m"] < 220.0:
        out.append(f"nest_watch: hag {m['nest_watch_hag_max_m']}, target distance {m['nest_watch_target_dist_m']}")
    for k, (lo, hi) in zip(("nest_box", "nest_pad", "nest_watch"), m["nest_slot_dsm_m"], strict=True):
        z = float(doc[k][2])
        if not (z >= hi - 1e-6 and z - lo <= 0.5):
            out.append(f"{k}: z {z} outside the slot DSM range [{lo}, {hi}] + spawn tolerance")
    if max(m["nest_clear40_m"]) > P.clear40_m or max(m["nest_clear80_m"]) > P.clear80_m:
        out.append(f"nest clearance: 40 m {m['nest_clear40_m']}, 80 m {m['nest_clear80_m']}")
    out.extend(f"{k} outside bounds or inside a zone" for k in ("nest_box", "nest_pad", "nest_watch", "watch_point")
               if not L.in_bounds([doc[k][:2]], P.margin_m * 0.5) or not L.clear_of_zones([doc[k][:2]], P.zone_buffer_m * 0.5))
    for k in ("aoi_guard", "aoi_scan"):
        poly = doc[k]
        if not L.in_bounds(poly, 0.0) or not L.clear_of_zones(poly, P.zone_buffer_m * 0.5):
            out.append(f"{k} outside bounds or touching a zone")
        if C.area(poly) > 1e6:
            out.append(f"{k} area > 1 km2")
        out.extend(f"{n} inside {k}" for n in ("nest_box", "nest_pad", "nest_watch") if C.inside(doc[n][:2], poly))
    if m["depressed"]:
        out.append(f"in a depression (river or water level): {m['depressed']}")
    if m["aoi_guard_open_frac"] < P.open_min or m["aoi_scan_open_frac"] < P.open_min:
        out.append(f"open fraction low: guard {m['aoi_guard_open_frac']} scan {m['aoi_scan_open_frac']}")
    if m["aoi_guard_h_floor_m"] > P.h_cap_guard or m["aoi_scan_h_floor_m"] > P.h_cap_scan:
        out.append(f"H_floor high: guard {m['aoi_guard_h_floor_m']} scan {m['aoi_scan_h_floor_m']}")
    if m["target_hag_max_m"] > 0.3 or m["target_sky_los_8"] < 6:
        out.append(f"static target not open or not visible: hag {m['target_hag_max_m']} los {m['target_sky_los_8']}/8")
    if not C.inside(doc["target_static"]["pos"], doc["aoi_guard"]):
        out.append("static target outside the guard area")
    if not m["watch_los"]:
        out.append("watch point has no line of sight to the static target")
    if m["watch_hag_max_m"] > P.los_agl_m - 35.0:
        out.append(f"watch point clearance {m['watch_hag_max_m']}")
    for i, (of, a) in enumerate(zip(m["walk_open_frac"], m["walk_area_m2"], strict=True)):
        if of < 0.5 or not 100.0 <= a <= 1e6 or not all(C.inside(p, doc["aoi_guard"]) for p in doc["walk_regions"][i]):
            out.append(f"walk region {i}: open {of}, area {a}")
    if min(m["polyline_clear_m"]) < 30.0:
        out.append(f"polyline clearance {m['polyline_clear_m']}")
    return out


def anchors_path(world_id: str) -> Path:
    return C.ANCHORS / f"{world_id}.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools/sandbox/anchors.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--world", action="append", choices=C.WORLDS)
    g.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true", help="复核已提交的锚点（不写文件）")
    a = ap.parse_args(argv)
    worlds = list(C.WORLDS) if a.all else list(a.world)
    base = Path(os.environ.get("AWR_WORLDS_DIR") or C.ROOT / "worlds")
    rc = EXIT_OK
    for w in worlds:
        if not (base / w / "world.json").exists():
            print(f"{w}: world not built", file=sys.stderr)
            rc = max(rc, EXIT_ENV)
            continue
        if a.check:
            doc = C.load_json(anchors_path(w))
            probs = problems(doc, WorldLayers(w, base))
            print(f"{w}: {'OK' if not probs else 'FAIL'} {'; '.join(probs)}")
            if probs:
                rc = max(rc, EXIT_FAIL)
            continue
        doc = search(w, worlds_dir=base)
        probs = problems(doc, WorldLayers(w, base))
        changed = C.write_canonical(doc, anchors_path(w))
        print(f"{w}: {'written' if changed else 'unchanged'} {anchors_path(w).relative_to(C.ROOT)}"
              f"{' PROBLEMS ' + '; '.join(probs) if probs else ''}")
        if probs:
            rc = max(rc, EXIT_FAIL)
    return rc


if __name__ == "__main__":
    sys.exit(main())
