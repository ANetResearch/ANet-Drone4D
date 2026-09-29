"""区域棱柱与索引（M04 §6.4.8、§7.2、FR-023、FR-024；AWR-16 §7）。

航段与棱柱求交：先按 `z(t) = z_a + t·(z_b − z_a)` 裁剪到 `[min_z, max_z]`（null 取 ∓∞），得到子段；子段与多边形
任一边相交，或子段任一端点在多边形内（含洞环），即相交。栅格预筛（8 m，内部格置 1 并膨胀 1 格）只用于跳过远离
禁飞区的航段，结论与暴力解相同。
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy import ndimage as ndi

from .heightmap import max_pyramid
from .types import GeoLoadError


@dataclass
class Prism:
    zone_id: str
    kind: str
    polygons: list[list[np.ndarray]]      # 每个多边形：[外环, 洞环...]，每环 (n,2)（不重复首点）
    zmin: float
    zmax: float
    label: str = ""
    bbox: np.ndarray = field(default_factory=lambda: np.zeros(4))
    edges_a: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    edges_b: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))

    def __post_init__(self):
        pts = np.concatenate([r for poly in self.polygons for r in poly])
        self.bbox = np.r_[pts.min(0), pts.max(0)]
        ea, eb = [], []
        for poly in self.polygons:
            for ring in poly:
                ea.append(ring)
                eb.append(np.roll(ring, -1, axis=0))
        self.edges_a = np.concatenate(ea)
        self.edges_b = np.concatenate(eb)

    @property
    def n_edges(self) -> int:
        return len(self.edges_a)

    # ---- 二维
    def contains_xy(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        out = np.zeros(len(xy), bool)
        bb = (xy[:, 0] >= self.bbox[0]) & (xy[:, 0] <= self.bbox[2]) & (xy[:, 1] >= self.bbox[1]) & (xy[:, 1] <= self.bbox[3])
        idx = np.flatnonzero(bb)
        if idx.size == 0:
            return out
        x = xy[idx, 0][:, None]
        y = xy[idx, 1][:, None]
        for poly in self.polygons:
            inside = np.zeros(idx.size, bool)
            for ring in poly:                      # 奇偶规则：洞环自然抵消
                x1, y1 = ring[:, 0][None], ring[:, 1][None]
                x2, y2 = np.roll(ring[:, 0], -1)[None], np.roll(ring[:, 1], -1)[None]
                cond = (y1 > y) != (y2 > y)
                with np.errstate(divide="ignore", invalid="ignore"):
                    xin = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                inside ^= ((cond & (x < xin)).sum(1) % 2 == 1)
            out[idx] |= inside
        return out

    def contains(self, xyz: np.ndarray) -> np.ndarray:
        xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
        return self.contains_xy(xyz[:, :2]) & (xyz[:, 2] >= self.zmin) & (xyz[:, 2] <= self.zmax)

    def _cross_edges_xy(self, P: np.ndarray, Q: np.ndarray) -> np.ndarray:
        """线段 P_i→Q_i 与任一边相交（含接触）。"""
        p = P[:, None, :2]
        r = (Q - P)[:, None, :2]
        e1 = self.edges_a[None]
        s = (self.edges_b - self.edges_a)[None]
        rxs = r[..., 0] * s[..., 1] - r[..., 1] * s[..., 0]
        qp = e1 - p
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (qp[..., 0] * s[..., 1] - qp[..., 1] * s[..., 0]) / rxs
            u = (qp[..., 0] * r[..., 1] - qp[..., 1] * r[..., 0]) / rxs
        return ((rxs != 0) & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)).any(1)

    def segments_cross(self, A: np.ndarray, B: np.ndarray) -> np.ndarray:
        """z 区间裁剪后的精确线段–棱柱求交。A、B：(m,3)。"""
        A = np.asarray(A, np.float64).reshape(-1, 3)
        B = np.asarray(B, np.float64).reshape(-1, 3)
        za = A[:, 2]
        dz = B[:, 2] - za
        with np.errstate(divide="ignore", invalid="ignore"):
            tmin = (self.zmin - za) / dz          # null 边界为 ∓∞，除法后仍为 ±∞
            tmax = (self.zmax - za) / dz
        lo = np.where(dz > 0, tmin, tmax)
        hi = np.where(dz > 0, tmax, tmin)
        flat = dz == 0
        if flat.any():
            inr = (za >= self.zmin) & (za <= self.zmax)
            lo = np.where(flat, np.where(inr, -np.inf, np.inf), lo)
            hi = np.where(flat, np.where(inr, np.inf, -np.inf), hi)
        t0 = np.maximum(lo, 0.0)
        t1 = np.minimum(hi, 1.0)
        ok = t0 <= t1
        out = np.zeros(len(A), bool)
        idx = np.flatnonzero(ok)
        if idx.size == 0:
            return out
        P = A[idx] + (B[idx] - A[idx]) * t0[idx, None]
        Q = A[idx] + (B[idx] - A[idx]) * t1[idx, None]
        bbA = np.minimum(P[:, :2], Q[:, :2])
        bbB = np.maximum(P[:, :2], Q[:, :2])
        near = (bbA[:, 0] <= self.bbox[2]) & (bbB[:, 0] >= self.bbox[0]) & (bbA[:, 1] <= self.bbox[3]) & (bbB[:, 1] >= self.bbox[1])
        j = np.flatnonzero(near)
        if j.size == 0:
            return out
        hit = self._cross_edges_xy(P[j], Q[j]) | self.contains_xy(P[j, :2]) | self.contains_xy(Q[j, :2])
        out[idx[j]] = hit
        return out

    def boundary_distance(self, xy: np.ndarray) -> np.ndarray:
        """到多边形各边的最小距离（m）。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        a = self.edges_a[None]
        d = (self.edges_b - self.edges_a)[None]
        p = xy[:, None, :]
        L2 = (d ** 2).sum(-1)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.clip(((p - a) * d).sum(-1) / np.where(L2 > 0, L2, 1), 0, 1)
        q = a + d * t[..., None]
        return np.sqrt(((p - q) ** 2).sum(-1)).min(1)


def _prism_from_feature(f: dict) -> Prism:
    g = f["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else list(g["coordinates"])
    rings = [[np.asarray(ring, np.float64)[:-1] if ring[0] == ring[-1] else np.asarray(ring, np.float64) for ring in poly]
             for poly in polys]
    p = f["properties"]
    zmin = -np.inf if p.get("min_z_m") is None else float(p["min_z_m"])
    zmax = np.inf if p.get("max_z_m") is None else float(p["max_z_m"])
    return Prism(p["zone_id"], p["kind"], rings, zmin, zmax, str(p.get("label", "")))


class ZoneRaster:
    """nofly 棱柱（不分 z）的保守栅格与膨胀金字塔：只用于跳过远离区域的航段。"""

    def __init__(self, prisms: list[Prism], x0: float, y0: float, width_m: float, height_m: float, cell_m: float = 8.0,
                 dilate_cells: int = 1):
        self.x0, self.y0, self.cell = x0, y0, cell_m
        W = max(1, math.ceil(width_m / cell_m))
        H = max(1, math.ceil(height_m / cell_m))
        m = np.zeros((H, W), bool)
        cx = x0 + (np.arange(W) + 0.5) * cell_m
        cy = y0 + (np.arange(H) + 0.5) * cell_m
        for z in prisms:
            c0 = max(0, int((z.bbox[0] - x0) // cell_m) - 1)
            c1 = min(W, int((z.bbox[2] - x0) // cell_m) + 2)
            r0 = max(0, int((z.bbox[1] - y0) // cell_m) - 1)
            r1 = min(H, int((z.bbox[3] - y0) // cell_m) + 2)
            if c1 <= c0 or r1 <= r0:
                continue
            XX, YY = np.meshgrid(cx[c0:c1], cy[r0:r1])
            inside = z.contains_xy(np.c_[XX.ravel(), YY.ravel()]).reshape(XX.shape)
            # 多边形很小（落在格心之间）时也要标记：顶点所在格置 1
            for poly in z.polygons:
                for ring in poly:
                    rc = np.clip(((ring[:, 1] - y0) // cell_m).astype(int), 0, H - 1)
                    cc = np.clip(((ring[:, 0] - x0) // cell_m).astype(int), 0, W - 1)
                    m[rc, cc] = True
            m[r0:r1, c0:c1] |= inside
        if dilate_cells > 0:
            m = ndi.binary_dilation(m, iterations=dilate_cells)
        self.mask = m
        self.pyr = max_pyramid(m.astype(np.float32))
        self.pyr_dil = [ndi.maximum_filter(p, size=3, mode="nearest") for p in self.pyr]

    @classmethod
    def from_mask(cls, mask: np.ndarray, x0: float, y0: float, cell_m: float) -> ZoneRaster:
        """由派生缓存中的掩码重建（掩码已膨胀）。"""
        self = cls.__new__(cls)
        self.x0, self.y0, self.cell = x0, y0, cell_m
        self.mask = np.asarray(mask, bool)
        self.pyr = max_pyramid(self.mask.astype(np.float32))
        self.pyr_dil = [ndi.maximum_filter(p, size=3, mode="nearest") for p in self.pyr]
        return self

    def segments_flag(self, A: np.ndarray, B: np.ndarray, L: int = 0) -> np.ndarray:
        """航段是否靠近任一 nofly（保守）：按格宽间距采样膨胀层。"""
        L = min(L, len(self.pyr) - 1)
        cl = self.cell * 2 ** L
        p = self.pyr_dil[L]
        n = len(A)
        if n == 0:
            return np.zeros(0, bool)
        seg_len = np.hypot(B[:, 0] - A[:, 0], B[:, 1] - A[:, 1])
        k = np.maximum(2, np.ceil(seg_len / cl).astype(np.int64) + 1)
        start = np.zeros(n, np.int64)
        np.cumsum(k[:-1], out=start[1:])
        sid = np.repeat(np.arange(n), k)
        s = (np.arange(int(k.sum())) - start[sid]) / (k[sid] - 1)
        x = A[sid, 0] + (B[sid, 0] - A[sid, 0]) * s
        y = A[sid, 1] + (B[sid, 1] - A[sid, 1]) * s
        c = np.clip(np.floor((x - self.x0) / cl).astype(np.int64), 0, p.shape[1] - 1)
        r = np.clip(np.floor((y - self.y0) / cl).astype(np.int64), 0, p.shape[0] - 1)
        return np.maximum.reduceat(p[r, c], start) > 0

    def points_flag(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        c = np.clip(np.floor((xy[:, 0] - self.x0) / self.cell).astype(np.int64), 0, self.mask.shape[1] - 1)
        r = np.clip(np.floor((xy[:, 1] - self.y0) / self.cell).astype(np.int64), 0, self.mask.shape[0] - 1)
        return self.pyr_dil[0][r, c] > 0


class ZoneIndex:
    """border（恰好 1 个）、nofly、restricted 棱柱。"""

    def __init__(self, fc: dict, *, source: str = "zones.geojson"):
        self.fc = fc
        self.source = source
        prisms = [_prism_from_feature(f) for f in fc.get("features", [])]
        borders = [p for p in prisms if p.kind == "border"]
        if len(borders) != 1:
            raise GeoLoadError("GEO_GRID_INVALID", f"{source}: {len(borders)} border features (exactly one required)")
        self.border: Prism = borders[0]
        self.nofly: list[Prism] = [p for p in prisms if p.kind == "nofly"]
        self.restricted: list[Prism] = [p for p in prisms if p.kind == "restricted"]
        self.by_id = {p.zone_id: p for p in prisms}
        self.raster: ZoneRaster | None = None
        self.coordinate_sha256 = (fc.get("awr") or {}).get("coordinate_sha256")

    @classmethod
    def load(cls, path: Path, expect_coord_sha: str | None = None) -> ZoneIndex:
        path = Path(path)
        try:
            fc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise GeoLoadError("GEO_MISSING_FILE", str(path)) from e
        except (OSError, json.JSONDecodeError) as e:
            raise GeoLoadError("GEO_GRID_INVALID", f"{path}: {e}") from e
        sha = (fc.get("awr") or {}).get("coordinate_sha256")
        if expect_coord_sha is not None and sha != expect_coord_sha:
            raise GeoLoadError("GEO_SHA_MISMATCH", f"{path.name}: coordinate_sha256 {sha} != world {expect_coord_sha}")
        zi = cls(fc, source=path.name)
        for p in zi.nofly + zi.restricted:
            for poly in p.polygons:
                for ring in poly:
                    if len(ring) > 1024:
                        raise GeoLoadError("GEO_GRID_INVALID", f"{p.zone_id}: ring has {len(ring)} vertices > 1024")
        return zi

    def build_raster(self, x0: float, y0: float, width_m: float, height_m: float, cell_m: float = 8.0) -> None:
        self.raster = ZoneRaster(self.nofly, x0, y0, width_m, height_m, cell_m)

    def select(self, kind: str, zone_ids: Sequence[str] | None = None) -> list[Prism]:
        pool = {"border": [self.border], "nofly": self.nofly, "restricted": self.restricted}[kind]
        if zone_ids is None:
            return list(pool)
        ids = set(zone_ids)
        return [p for p in pool if p.zone_id in ids]

    def contains(self, xyz: np.ndarray, kinds: set[str], zone_ids: Sequence[str] | None = None) -> np.ndarray:
        xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
        out = np.zeros(len(xyz), bool)
        for k in kinds:
            for p in self.select(k, zone_ids if k != "border" else None):
                out |= p.contains(xyz)
        return out

    def zone_ids_at(self, xyz: np.ndarray) -> list[list[str]]:
        xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
        res: list[list[str]] = [[] for _ in range(len(xyz))]
        for p in self.nofly + self.restricted:
            m = p.contains(xyz)
            for i in np.flatnonzero(m):
                res[i].append(p.zone_id)
        return res

    def segments_cross(self, A: np.ndarray, B: np.ndarray, kinds: set[str], zone_ids: Sequence[str] | None = None) -> np.ndarray:
        A = np.asarray(A, np.float64).reshape(-1, 3)
        B = np.asarray(B, np.float64).reshape(-1, 3)
        out = np.zeros(len(A), bool)
        for k in kinds:
            for p in self.select(k, zone_ids if k != "border" else None):
                out |= p.segments_cross(A, B)
        return out

    def border_signed_distance(self, xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        d = self.border.boundary_distance(xy)
        return np.where(self.border.contains_xy(xy), d, -d)

    def nearest_zone_distance(self, xy: np.ndarray, kinds: set[str], max_m: float) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        best = np.full(len(xy), np.inf)
        for k in kinds:
            for p in self.select(k):
                bb = p.bbox
                dx = np.maximum(np.maximum(bb[0] - xy[:, 0], xy[:, 0] - bb[2]), 0)
                dy = np.maximum(np.maximum(bb[1] - xy[:, 1], xy[:, 1] - bb[3]), 0)
                near = np.hypot(dx, dy) <= max_m
                idx = np.flatnonzero(near)
                if idx.size == 0:
                    continue
                d = np.where(p.contains_xy(xy[idx]), 0.0, p.boundary_distance(xy[idx]))
                best[idx] = np.minimum(best[idx], d)
        return np.where(best <= max_m, best, np.inf)
