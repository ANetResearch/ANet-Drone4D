"""传感器侧的"不完整即未知"联动（M25 §6.6.2、§6.8.6；M22 §7.3.6、M22-FR-043；D3-AC-04）。

世界包 quality 阶段（W10）写出的质量信息由本模块装载一次，供网格 LiDAR 真值 `void` 位与相机真值 `invalid_mask` 第 1–4 位共用：

- 求交网格逐三角形质量位 `q.u8`（`awr.world.package.streams`，三角形序同 `MeshWorldQuery` 的 prim_id）：bit0 nodata（三角形重心
  在覆盖外，即"覆盖多边形判定在加载时烘焙进逐三角形质量位"）、bit2 stretched、bit5 water；
- 洞缘三角形（邻接开放边界环的三角形，M25 §6.6.2 第 3 位、§6.8.6 第 1 行）：世界包的逐三角形 bit1 置位时直接采用；当前构建
  只在质量栅格上置 hole 位（洞内部），于是在装载时由洞格（膨胀 `HOLE_DILATE_CELLS` 格）所触及的三角形子集上求网格边界边得到。
  两端点都落在膨胀洞格内的边，其所有邻接三角形的包围盒都触及这些格，因而该子集上"只属于一个三角形"的判定与全网格一致；
  离洞 ≤ 2 格的外边界或悬空构件外边界也会计入（保守，只会多标）；
- 覆盖多边形 `semantic/quality.geojson` 的 `scan-footprint`（LiDAR 未命中下视射线以射线起点是否在覆盖内判定 `void`）。

判定规则：

| 输出 | 规则 |
|---|---|
| LiDAR `void`（bit6） | 最近交点为静态三角形且该三角形为拉伸面、洞缘或覆盖外（§6.8.6 第 1、4 行）；或射线与静态网格在任意距离上都无交点、仰角 < 0 且起点 xy 在覆盖多边形内（第 2 行）。量程外的有效表面不计为 void；演员命中不置位 |
| 相机 `invalid_mask` bit1–4 | 静态像素（实例 0、深度 > 0）由交付深度反投影得到前景表面点；该点落在被标记三角形上（到三角形平面的距离 ≤ max(`PLANE_TOL_M`, `PLANE_TOL_REL` × 视线距离)，平面内越出三角形边 ≤ `EDGE_TOL_M`）时取该三角形的拉伸、覆盖外、洞缘、水面位；水面另取静态语义码 8。演员像素不置位 |

相机侧不求交：被标记三角形（crop 约 5.8 万个，占 2%）按 xy 包围盒登记到质量格（CSR），每个前景点只与其所在格登记的三角形
做点在三角形上的判定。与按像素视线求交取首个三角形相比，crop 720p 三个视角逐像素一致率 99.980%–99.994%，差异全部为多标
（与被标记三角形共面相接或相距 1 cm 内的表面），无漏标；本机 8 核共享时每帧约 0.35–0.4 s，约为整帧求交的 1/4。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

__all__ = ["CAM_HOLE_EDGE", "CAM_OUTSIDE_COVERAGE", "CAM_STRETCHED", "CAM_WATER", "EDGE_TOL_M", "PLANE_TOL_M", "PLANE_TOL_REL", "SEM_WATER",
           "SensorQuality", "hole_edge_triangles", "points_in_rings"]

# 世界包质量位（M22 §7.3.6；与 awr.world.glb.derive.quality 的 BIT_* 相同取值，读取方不依赖构建模块）
Q_NODATA, Q_HOLE, Q_STRETCHED, Q_WATER = 1, 2, 4, 32
# 相机 invalid_mask 位（M25 §6.6.2；frame.INVALID_BITS 的第 1–4 位）
CAM_STRETCHED, CAM_OUTSIDE_COVERAGE, CAM_HOLE_EDGE, CAM_WATER = 2, 4, 8, 16
SEM_WATER = 8                       # d3-semantic@1 水面
HOLE_DILATE_CELLS = 2               # 洞格膨胀（洞环顶点所在格的格心可能在洞外）
WELD_M = 1e-3                       # 与 quality 阶段的边界环拓扑同一焊接尺度（M22-FR-024）
PLANE_TOL_M, PLANE_TOL_REL = 0.01, 1e-4   # 前景点到三角形平面的容差（米；相对视线距离）：外观网格 float32 坐标（≤ 0.2 mm）与交付深度的误差远小于此
EDGE_TOL_M = 1e-3                   # 前景点在三角形平面内越出三角形边的容差（米；覆盖坐标舍入）
CHUNK_PAIRS = 2_000_000             # 每批 (像素, 三角形) 判定对数上限
MISS_TMAX_M = 1.0e5                 # LiDAR 未命中射线的无限量程复查


def points_in_rings(P: np.ndarray, rings: list[np.ndarray]) -> np.ndarray:
    """偶奇规则：P (n, 2) 是否落在环组内（环不闭合或闭合均可）。"""
    P = np.asarray(P, np.float64).reshape(-1, 2)
    inside = np.zeros(len(P), bool)
    for r in rings:
        r = np.asarray(r, np.float64)[:, :2]
        if len(r) >= 2 and np.allclose(r[0], r[-1]):
            r = r[:-1]
        if len(r) < 3:
            continue
        a, b = r, np.roll(r, -1, 0)
        for (xa, ya), (xb, yb) in zip(a, b, strict=True):
            m = (ya <= P[:, 1]) != (yb <= P[:, 1])
            if m.any():
                xi = xa + (P[m, 1] - ya) * (xb - xa) / (yb - ya)
                inside[np.flatnonzero(m)[P[m, 0] < xi]] ^= True
    return inside


def _cells(xy: np.ndarray, origin: tuple[float, float], cell: float, H: int, W: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """世界 xy → (行, 列, 是否在栅格内)；行 0 在南，与质量栅格同一约定。"""
    c = np.floor((xy[..., 0] - origin[0]) / cell).astype(np.int64)
    r = np.floor((xy[..., 1] - origin[1]) / cell).astype(np.int64)
    ok = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    return r, c, ok


def _tri_cell_boxes(V: np.ndarray, F: np.ndarray, origin, cell: float, H: int, W: int):
    """逐三角形 xy 包围盒覆盖的格区间（裁到栅格内）：(r0, r1, c0, c1)。"""
    vc = np.floor((V[:, 0] - origin[0]) / cell).astype(np.int64)
    vr = np.floor((V[:, 1] - origin[1]) / cell).astype(np.int64)
    tc, tr = vc[F], vr[F]
    return (np.clip(tr.min(1), 0, H - 1), np.clip(tr.max(1), 0, H - 1), np.clip(tc.min(1), 0, W - 1), np.clip(tc.max(1), 0, W - 1))


def _box_any(mask: np.ndarray, r0, r1, c0, c1) -> np.ndarray:
    """每个格区间内是否有 mask 置位的格（二维前缀和）。"""
    S = np.zeros((mask.shape[0] + 1, mask.shape[1] + 1), np.int64)
    S[1:, 1:] = np.cumsum(np.cumsum(mask, 0, dtype=np.int64), 1)
    return (S[r1 + 1, c1 + 1] - S[r0, c1 + 1] - S[r1 + 1, c0] + S[r0, c0]) > 0


def hole_edge_triangles(V: np.ndarray, F: np.ndarray, Q: np.ndarray, origin: tuple[float, float], cell: float, *,
                        dilate: int = HOLE_DILATE_CELLS, weld_m: float = WELD_M) -> np.ndarray:
    """洞缘三角形 (len(F),) bool：拥有一条网格边界边（焊接后只属于一个三角形）且该边两端点都在膨胀洞格内的三角形。"""
    from scipy import ndimage

    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    out = np.zeros(len(F), bool)
    hole = (np.asarray(Q) & Q_HOLE) > 0
    if not hole.any() or not len(F):
        return out
    H, W = hole.shape
    R = ndimage.binary_dilation(hole, structure=np.ones((3, 3), bool), iterations=int(dilate))
    sub = np.flatnonzero(_box_any(R, *_tri_cell_boxes(V, F, origin, cell, H, W)))
    if not len(sub):
        return out
    T = F[sub]
    used = np.unique(T)
    key = np.round(V[used] / weld_m).astype(np.int64)
    _u, wid = np.unique(key, axis=0, return_inverse=True)
    lut = np.full(len(V), -1, np.int64)
    lut[used] = wid.reshape(-1)
    Tw = lut[T]
    good = (Tw[:, 0] != Tw[:, 1]) & (Tw[:, 1] != Tw[:, 2]) & (Tw[:, 2] != Tw[:, 0])
    vr, vc, vok = _cells(V[:, :2], origin, cell, H, W)
    vin = np.zeros(len(V), bool)
    vin[vok] = R[vr[vok], vc[vok]]
    nw = int(Tw.max()) + 1
    owner = np.repeat(np.arange(len(T)), 3)
    ea = np.stack([Tw[:, 0], Tw[:, 1], Tw[:, 2]], 1).ravel()
    eb = np.stack([Tw[:, 1], Tw[:, 2], Tw[:, 0]], 1).ravel()
    oa = np.stack([T[:, 0], T[:, 1], T[:, 2]], 1).ravel()
    ob = np.stack([T[:, 1], T[:, 2], T[:, 0]], 1).ravel()
    keep = np.repeat(good, 3)
    ek = np.minimum(ea, eb) * nw + np.maximum(ea, eb)
    _uk, inv, cnt = np.unique(ek[keep], return_inverse=True, return_counts=True)
    boundary = np.zeros(len(ek), bool)
    boundary[np.flatnonzero(keep)] = cnt[inv.reshape(-1)] == 1
    edge = boundary & vin[oa] & vin[ob]
    out[sub[np.unique(owner[edge])]] = True
    return out


@dataclass
class SensorQuality:
    """一个世界的传感器侧质量信息（只读；装载后跨帧、跨传感器共用）。"""
    world_dir: Path
    tri_q: np.ndarray                    # (n_tri,) u8 求交网格逐三角形质量位（q.u8）
    hole_edge: np.ndarray                # (n_tri,) bool 洞缘三角形
    footprint: list[np.ndarray]          # 覆盖多边形的环（世界 E、N）
    grid_origin: tuple[float, float]
    grid_cell: float
    grid_shape: tuple[int, int]
    cover_cells: np.ndarray              # (H, W) bool 格心在覆盖多边形内
    cover_edge: np.ndarray               # (H, W) bool 覆盖多边形边界经过的格（膨胀 1 格），其中的点逐点判定
    lidar_void_tri: np.ndarray           # (n_tri,) bool LiDAR void 三角形（拉伸、洞缘、覆盖外）
    cam_tri_bits: np.ndarray             # (n_tri,) u8 相机 invalid_mask 第 1–4 位
    cam: _FlaggedTriangles               # 相机标记三角形的格索引与几何
    cache_root: Path | None = None
    stats: dict = field(default_factory=dict)
    _q: object | None = None

    # ------------------------------------------------------------------ 装载
    @classmethod
    def load(cls, world_dir: Path, *, q=None, cache_root: Path | None = None) -> SensorQuality | None:
        """按 world.json 的 `quality.raster`、`semantic.quality` 与 `mesh.raycast`（含 `q` 数据流）装载；任一缺失（D2 世界或
        quality 阶段之前的构建）时返回 None，调用方按原规则（仅"无几何下视"）输出。`q` 为已打开的 MeshWorldQuery（LiDAR 未命中射线的无限量程复查复用）。"""
        from awr.world.glb.raster import trimesh_world
        from awr.world.package.streams import read_stream
        from awr.world.package.validate import rasterize_rings

        world_dir = Path(world_dir)
        try:
            doc = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        L = {x.get("id"): x for x in doc.get("layers", []) if isinstance(x, dict)}
        if not all(k in L for k in ("quality.raster", "semantic.quality", "mesh.raycast")):
            return None
        rc = world_dir / L["mesh.raycast"]["href"]
        tri_q = read_stream(rc / "index.json", "q")
        if tri_q is None:
            return None
        sc = json.loads((world_dir / L["quality.raster"]["href"]).read_text(encoding="utf-8"))
        H, W = int(sc["height"]), int(sc["width"])
        Q = np.fromfile((world_dir / L["quality.raster"]["href"]).parent / sc["href"], np.uint8).reshape(H, W)
        org = (float(sc["originXY"][0]), float(sc["originXY"][1]))
        cell = float(sc["cellM"])
        fc = json.loads((world_dir / L["semantic.quality"]["href"]).read_text(encoding="utf-8"))
        rings = []
        for f in fc.get("features", []):
            if (f.get("properties") or {}).get("kind") == "scan-footprint":
                g = f["geometry"]
                polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
                rings += [np.asarray(r, np.float64)[:, :2] for p in polys for r in p]
        xyz, tri = trimesh_world(rc / "index.json")                # 世界坐标与全局顶点下标（三角形序同 prim_id）
        tri_q = np.asarray(tri_q, np.uint8)
        if len(tri_q) != len(tri):
            raise ValueError(f"q.u8 has {len(tri_q)} values for {len(tri)} raycast triangles")
        he = hole_edge_triangles(xyz, tri, Q, org, cell) | ((tri_q & Q_HOLE) > 0)
        cover = rasterize_rings(rings, org, cell, H, W) if rings else np.zeros((H, W), bool)
        edge = np.zeros((H, W), bool)
        for r in rings:                                            # 覆盖多边形边经过的格：沿边以 1/4 格步长取样
            a = np.asarray(r, np.float64)
            b = np.roll(a, -1, 0)
            for p0, p1 in zip(a, b, strict=True):
                n = max(2, int(np.ceil(np.linalg.norm(p1 - p0) / (0.25 * cell))) + 1)
                s = p0 + (p1 - p0) * np.linspace(0.0, 1.0, n)[:, None]
                rr, cc, ok = _cells(s, org, cell, H, W)
                edge[rr[ok], cc[ok]] = True
        from scipy import ndimage

        edge = ndimage.binary_dilation(edge, structure=np.ones((3, 3), bool))
        void_tri = ((tri_q & (Q_NODATA | Q_STRETCHED)) > 0) | he
        cam = np.zeros(len(tri), np.uint8)
        cam[(tri_q & Q_STRETCHED) > 0] |= CAM_STRETCHED
        cam[(tri_q & Q_NODATA) > 0] |= CAM_OUTSIDE_COVERAGE
        cam[he] |= CAM_HOLE_EDGE
        cam[(tri_q & Q_WATER) > 0] |= CAM_WATER
        ft = _FlaggedTriangles.build(xyz, tri, cam, org, cell, H, W)
        stats = {"triangles": len(tri), "hole_edge": int(he.sum()), "lidar_void_triangles": int(void_tri.sum()),
                 "camera_flagged_triangles": len(ft.bits), "camera_cell_entries": len(ft.cell_tri), "footprint_rings": len(rings)}
        return cls(world_dir, tri_q, he, rings, org, cell, (H, W), cover, edge, void_tri, cam, ft, cache_root, stats, q)

    def query(self):
        """LiDAR 复查用的 MeshWorldQuery（装载与调用时都未给出则按世界目录打开一次）。"""
        if self._q is None:
            from awr.world.mesh.query import open_mesh_world_query

            self._q = open_mesh_world_query(self.world_dir, self.cache_root)
        return self._q

    # ------------------------------------------------------------------ 覆盖判定
    def in_coverage(self, xy: np.ndarray) -> np.ndarray:
        """世界 xy (n, 2) 是否在覆盖多边形内：边界格内的点逐点按多边形判定，其余取格心判定。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        H, W = self.grid_shape
        r, c, ok = _cells(xy, self.grid_origin, self.grid_cell, H, W)
        rc, cc = np.clip(r, 0, H - 1), np.clip(c, 0, W - 1)
        out = ok & self.cover_cells[rc, cc]
        exact = ok & self.cover_edge[rc, cc]
        if exact.any():
            out[exact] = points_in_rings(xy[exact], self.footprint)
        return out

    # ------------------------------------------------------------------ LiDAR
    def lidar_void(self, prim: np.ndarray, static_hit: np.ndarray, miss_down: np.ndarray, o: np.ndarray, d: np.ndarray, *,
                   q=None) -> np.ndarray:
        """M25 §6.8.6 的 `void`：`prim` 为静态最近交点三角形（未命中 MISS），`static_hit` 为最近交点是静态网格的槽位，
        `miss_down` 为量程内未命中（静态与演员）且仰角 < 0 的槽位；`o`、`d` 为槽位射线起点与单位方向（世界系）；`q` 为求交用的
        MeshWorldQuery（缺省 `query()`）。"""
        n = len(prim)
        v = np.zeros(n, bool)
        p = np.asarray(prim, np.int64)
        sh = np.asarray(static_hit, bool) & (p >= 0) & (p < len(self.lidar_void_tri))
        v[sh] = self.lidar_void_tri[p[sh]]
        md = np.flatnonzero(miss_down)
        if len(md):
            t, _prim, _n = (q if q is not None else self.query())._cast(np.ascontiguousarray(o[md], np.float64), np.ascontiguousarray(d[md], np.float64),
                                                     np.full(len(md), MISS_TMAX_M))
            nohit = md[~np.isfinite(t)]                             # 任意距离上都无静态交点（量程外的有效表面不计）
            if len(nohit):
                v[nohit] = self.in_coverage(o[nohit, :2])
        return v

    # ------------------------------------------------------------------ 相机
    def camera_bits(self, *, cm, R_ws: np.ndarray, pos_enu_m: np.ndarray, depth_m: np.ndarray, instance: np.ndarray,
                    semantic: np.ndarray | None = None) -> np.ndarray:
        """`invalid_mask` 第 1–4 位 (H, W) u8（M25 §6.6.2）：像素视线取交付分辨率像素中心（`colmap_center_0.5`），`cm.dist` 非零时先
        去畸变；前景点 = `pos_enu_m` + 平面深度 × 视线（曝光中点位姿）。"""
        from awr.sensorsvc.camera.proj import R_FLU_OPT, undistort_normalized

        depth = np.asarray(depth_m, np.float32)
        Hh, Ww = depth.shape
        out = np.zeros((Hh, Ww), np.uint8)
        static = (depth > 0) & (np.asarray(instance) == 0)
        if semantic is not None:
            out[static & (np.asarray(semantic) == SEM_WATER)] |= CAM_WATER
        ft = self.cam
        pix = np.flatnonzero(static)
        self.stats["last_camera_pairs"] = 0
        if not len(pix) or not len(ft.bits):
            return out
        yy, xx = np.divmod(pix, Ww)
        x = (xx + 0.5 - cm.cx * Ww / cm.width) / (cm.fx * Ww / cm.width)
        y = (yy + 0.5 - cm.cy * Hh / cm.height) / (cm.fy * Hh / cm.height)
        if any(float(k) != 0.0 for k in cm.dist):
            x, y = undistort_normalized(x, y, cm.dist)
        Rwo = np.asarray(R_ws, np.float64) @ R_FLU_OPT
        z = depth.ravel()[pix].astype(np.float64)
        X = np.asarray(pos_enu_m, np.float64) + z[:, None] * (np.stack([x, y, np.ones(len(x))], 1) @ Rwo.T)
        s = z * np.sqrt(x * x + y * y + 1.0)                        # 视线距离
        H, W = self.grid_shape
        r, c, _ok = _cells(X[:, :2], self.grid_origin, self.grid_cell, H, W)
        cid = np.clip(r, 0, H - 1) * W + np.clip(c, 0, W - 1)        # 栅格外的点归到边缘格（越出栅格的三角形裁到边缘格登记）
        b = ft.lookup(X, cid, np.maximum(PLANE_TOL_M, PLANE_TOL_REL * s), self.stats)
        hit = b > 0
        out.reshape(-1)[pix[hit]] |= b[hit]
        return out


@dataclass
class _FlaggedTriangles:
    """相机标记三角形（`cam_tri_bits` 非 0）：按 xy 包围盒登记到质量格的 CSR 索引与点在三角形上判定所需的几何。"""
    bits: np.ndarray                     # (m,) u8
    cell_ptr: np.ndarray                 # (H·W + 1,) int64
    cell_tri: np.ndarray                 # (Σ,) int32 格内登记的三角形（标记三角形序）
    v0: np.ndarray                       # (m, 3)
    e1: np.ndarray
    e2: np.ndarray
    n: np.ndarray                        # (m, 3) 单位法线
    gram: np.ndarray                     # (m, 4) d00、d01、d11、行列式
    margin: np.ndarray                   # (m, 3) 重心坐标 b0、b1、b2 的平面内容差（EDGE_TOL_M / 对应高）

    @classmethod
    def build(cls, V: np.ndarray, F: np.ndarray, bits: np.ndarray, origin, cell: float, H: int, W: int) -> _FlaggedTriangles:
        fl = np.flatnonzero(bits)
        P = np.asarray(V, np.float64)[np.asarray(F, np.int64)[fl]]
        v0, e1, e2 = P[:, 0], P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
        cr = np.cross(e1, e2)
        a2 = np.linalg.norm(cr, axis=1)                              # 2 × 面积
        ok = a2 > 1e-12                                              # 退化三角形不登记（无前景像素）
        fl, v0, e1, e2, cr, a2 = fl[ok], v0[ok], e1[ok], e2[ok], cr[ok], a2[ok]
        d00, d01, d11 = (e1 * e1).sum(1), (e1 * e2).sum(1), (e2 * e2).sum(1)
        gram = np.stack([d00, d01, d11, d00 * d11 - d01 * d01], 1)
        margin = EDGE_TOL_M * np.stack([np.linalg.norm(e2 - e1, axis=1), np.sqrt(d11), np.sqrt(d00)], 1) / a2[:, None]
        r0, r1, c0, c1 = _tri_cell_boxes(np.asarray(V, np.float64), np.asarray(F, np.int64)[fl], origin, cell, H, W)
        hh, ww = r1 - r0 + 1, c1 - c0 + 1
        cnt = hh * ww
        tid = np.repeat(np.arange(len(fl), dtype=np.int64), cnt)
        k = np.arange(int(cnt.sum()), dtype=np.int64) - np.repeat(np.cumsum(cnt) - cnt, cnt)
        cid = (r0[tid] + k // ww[tid]) * W + (c0[tid] + k % ww[tid])
        o = np.argsort(cid, kind="stable")
        ptr = np.searchsorted(cid[o], np.arange(H * W + 1, dtype=np.int64))
        return cls(np.asarray(bits, np.uint8)[fl], ptr.astype(np.int64), tid[o].astype(np.int32), v0, e1, e2, cr / a2[:, None], gram, margin)

    def lookup(self, X: np.ndarray, cid: np.ndarray, plane_tol: np.ndarray, stats: dict) -> np.ndarray:
        """前景点 X (n, 3)（所在格 cid）落在其上的标记三角形位之并 (n,) u8。"""
        out = np.zeros(len(X), np.uint8)
        a, b = self.cell_ptr[cid], self.cell_ptr[cid + 1]
        cnt = b - a
        idx = np.flatnonzero(cnt)
        if not len(idx):
            return out
        csum = np.cumsum(cnt[idx])
        lo = 0
        while lo < len(idx):                                         # 按判定对数分批，限制内存
            hi = int(np.searchsorted(csum, (csum[lo - 1] if lo else 0) + CHUNK_PAIRS, "right"))
            hi = max(hi, lo + 1)
            sel = idx[lo:hi]
            n = cnt[sel]
            pi = np.repeat(sel, n)
            off = np.arange(int(n.sum()), dtype=np.int64) - np.repeat(np.cumsum(n) - n, n)
            t = self.cell_tri[a[pi] + off]
            w = X[pi] - self.v0[t]
            near = np.abs((w * self.n[t]).sum(1)) <= plane_tol[pi]
            pi, t, w = pi[near], t[near], w[near]
            g = self.gram[t]
            d20, d21 = (w * self.e1[t]).sum(1), (w * self.e2[t]).sum(1)
            b1 = (g[:, 2] * d20 - g[:, 1] * d21) / g[:, 3]
            b2 = (g[:, 0] * d21 - g[:, 1] * d20) / g[:, 3]
            m = self.margin[t]
            inside = (1.0 - b1 - b2 >= -m[:, 0]) & (b1 >= -m[:, 1]) & (b2 >= -m[:, 2])
            np.bitwise_or.at(out, pi[inside], self.bits[t[inside]])
            stats["last_camera_pairs"] = stats.get("last_camera_pairs", 0) + len(near)
            lo = hi
        return out
