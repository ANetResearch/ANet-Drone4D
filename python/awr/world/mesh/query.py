"""MeshWorldQuery（AWR-05 §5.1.6；M04 §15.1 扩展点 7；M22-FR-047；ADR-131）：网格为几何真值的世界查询。

继承 `DsmWorldQuery` 的全部 2.5D 方法（高度、走廊、贴地，网格派生的 2 m 栅格，语义与 D2 相同）；3D 方法（`ray_hit`、
`segment_los`、`los_batch`、`free_distance`）覆盖为网格精确求交，另加 `raycast_batch`、`closest_batch` 与 `semantic_of`。

求交数据为 `mesh.raycast`（`awr-trimesh@1`）：每瓦片一个 Open3D `RaycastingScene`（Embree），顶点为瓦片局部原点下的
float32 相对坐标；射线段先裁到世界高度范围，再以平面外接框与瓦片 AABB 相交选出候选瓦片，在该瓦片局部帧下以 float32 求交，
取各瓦片最近命中。
全局三角形号 `prim_id` = 瓦片 `t0` + 瓦片内序号（即 `tri` 数组行号）；静态网格的实例号为 0。

尚未提供（P0a-2 随 NavMesh 落地）：`walkable_height`、`nav_path`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.world.geometry.query import DsmWorldQuery, open_dsm_world_query
from awr.world.geometry.types import GeoError, GeoLoadError, Hit

__all__ = ["MISS", "MeshTile", "MeshWorldQuery", "open_mesh_world_query"]

MISS = np.uint32(0xFFFFFFFF)
MAX_RAYS = 1 << 22
PARAM_OUT_OF_RANGE = 301


@dataclass
class MeshTile:
    id: int
    origin: np.ndarray            # (3,) float64，世界帧
    lo: np.ndarray                # AABB（世界帧）
    hi: np.ndarray
    t0: int
    nt: int
    scene: object                 # open3d.t.geometry.RaycastingScene
    V: np.ndarray                 # (nv, 3) float32 局部
    F: np.ndarray                 # (nt, 3) uint32


def _unit(d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    n = np.linalg.norm(d, axis=1)
    if not np.all(np.isfinite(d)) or np.any(n == 0):
        raise GeoError(300, "BAD_VECTOR")
    return d / n[:, None], n


def _segment_boxes(o: np.ndarray, d: np.ndarray, tmax: np.ndarray, zlo: float, zhi: float, pad: float = 1e-3):
    """射线段先裁到世界高度范围 [zlo, zhi]，返回裁剪后线段的平面外接框 (xmin, xmax, ymin, ymax) 与是否非空；每批只算一次，
    各瓦片只做四次比较（瓦片 AABB 与线段外接框相交即为候选）。"""
    oz, dz = o[:, 2], d[:, 2]
    with np.errstate(divide="ignore", invalid="ignore"):
        ta = (zlo - pad - oz) / dz
        tb = (zhi + pad - oz) / dz
    inside = (oz >= zlo - pad) & (oz <= zhi + pad)
    flat = dz == 0
    t0 = np.where(flat, np.where(inside, 0.0, np.inf), np.maximum(np.minimum(ta, tb), 0.0))
    t1 = np.where(flat, np.where(inside, tmax, -np.inf), np.minimum(np.maximum(ta, tb), tmax))
    ok = t0 <= t1
    t0 = np.where(ok, t0, 0.0)
    t1 = np.where(ok, t1, 0.0)
    x0, x1 = o[:, 0] + d[:, 0] * t0, o[:, 0] + d[:, 0] * t1
    y0, y1 = o[:, 1] + d[:, 1] * t0, o[:, 1] + d[:, 1] * t1
    return np.minimum(x0, x1) - pad, np.maximum(x0, x1) + pad, np.minimum(y0, y1) - pad, np.maximum(y0, y1) + pad, ok


class MeshWorldQuery(DsmWorldQuery):
    """`DsmWorldQuery` 的子类；由 `open_mesh_world_query` 构造（复用 DSM 装载结果，再装载网格瓦片）。"""

    tiles: list[MeshTile]
    n_triangles: int
    semantic: np.ndarray | None

    @classmethod
    def from_dsm(cls, base: DsmWorldQuery, world_dir: Path) -> MeshWorldQuery:
        import open3d as o3d

        q = cls.__new__(cls)
        q.__dict__.update(base.__dict__)
        doc = json.loads((Path(world_dir) / "world.json").read_text(encoding="utf-8"))
        lay = next((L for L in doc.get("layers", []) if L.get("role") == "geometry" and L.get("type") == "mesh"
                    and L.get("format") == "awr-trimesh@1"), None)
        if lay is None:
            raise GeoLoadError("GEO_MISSING_FILE", "world has no awr-trimesh@1 geometry layer")
        d = Path(world_dir) / lay["href"]
        idx_path = d / "index.json"
        if not idx_path.exists():
            raise GeoLoadError("GEO_MISSING_FILE", str(idx_path))
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
        if idx.get("coordinate_sha256") != base.coordinate_sha256:
            raise GeoLoadError("GEO_SHA_MISMATCH", "mesh.raycast coordinate_sha256 differs from the world header")
        xyz = np.fromfile(d / "xyz.f32", "<f4").reshape(-1, 3)
        tri = np.fromfile(d / "tri.u32", "<u4").reshape(-1, 3)
        if len(xyz) != idx["counts"]["vertices"] or len(tri) != idx["counts"]["triangles"]:
            raise GeoLoadError("GEO_GRID_INVALID", "mesh.raycast stream sizes do not match index.json counts")
        cls_path = d / "cls.u8"
        q.semantic = np.fromfile(cls_path, np.uint8) if cls_path.exists() else None
        q.tiles = []
        for t in idx["tiles"]:
            V = np.ascontiguousarray(xyz[t["v0"]:t["v0"] + t["nv"]])
            F = np.ascontiguousarray(tri[t["t0"]:t["t0"] + t["nt"]])
            sc = o3d.t.geometry.RaycastingScene()
            sc.add_triangles(o3d.core.Tensor(V), o3d.core.Tensor(F))
            sc.cast_rays(o3d.core.Tensor(np.array([[0, 0, 0, 0, 0, -1]], np.float32)))   # 装载时建 BVH，首个查询不付建树开销
            q.tiles.append(MeshTile(int(t["id"]), np.asarray(t["origin_m"], np.float64), np.asarray(t["aabb_m"]["min"], np.float64),
                                    np.asarray(t["aabb_m"]["max"], np.float64), int(t["t0"]), int(t["nt"]), sc, V, F))
        q.n_triangles = len(tri)
        q._zlo = min((float(t.lo[2]) for t in q.tiles), default=0.0)
        q._zhi = max((float(t.hi[2]) for t in q.tiles), default=0.0)
        return q

    # ------------------------------------------------------------------ 网格求交
    def _cast(self, o: np.ndarray, d: np.ndarray, tmax: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """o (N, 3) float64 世界帧、d 单位方向、tmax (N,)：返回 t（未命中 inf）、prim_id（未命中 MISS）、局部法线 (N, 3)。"""
        import open3d as o3d

        n = len(o)
        t_best = np.full(n, np.inf, np.float64)
        prim = np.full(n, MISS, np.uint32)
        nrm = np.zeros((n, 3), np.float32)
        xmin, xmax, ymin, ymax, ok = _segment_boxes(o, d, tmax, self._zlo, self._zhi)
        for tile in self.tiles:
            m = ok & (xmin <= tile.hi[0]) & (xmax >= tile.lo[0]) & (ymin <= tile.hi[1]) & (ymax >= tile.lo[1])
            if not m.any():
                continue
            ii = np.flatnonzero(m)
            rays = np.hstack([(o[ii] - tile.origin).astype(np.float32), d[ii].astype(np.float32)])
            r = tile.scene.cast_rays(o3d.core.Tensor(rays))
            th = r["t_hit"].numpy().astype(np.float64)
            better = np.isfinite(th) & (th <= tmax[ii]) & (th < t_best[ii])
            if better.any():
                jj = ii[better]
                t_best[jj] = th[better]
                prim[jj] = (r["primitive_ids"].numpy()[better].astype(np.int64) + tile.t0).astype(np.uint32)
                nrm[jj] = r["primitive_normals"].numpy()[better]
        return t_best, prim, nrm

    def raycast_batch(self, o, d, tmax: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(t_m float32，未命中 inf；prim_id uint32，未命中 0xFFFFFFFF；inst_id uint32，静态网格 0)。方向不必归一。"""
        org = np.asarray(o, np.float64).reshape(-1, 3)
        dirs = np.asarray(d, np.float64).reshape(-1, 3)
        if len(org) != len(dirs) or len(org) > MAX_RAYS:
            raise GeoError(PARAM_OUT_OF_RANGE, f"rays must be <= {MAX_RAYS} and pair up")
        if not tmax > 0:
            raise GeoError(PARAM_OUT_OF_RANGE, "tmax must be > 0")
        if not len(org):
            return np.zeros(0, np.float32), np.zeros(0, np.uint32), np.zeros(0, np.uint32)
        unit, _ = _unit(dirs)
        t, prim, _ = self._cast(org, unit, np.full(len(org), float(tmax)))
        return t.astype(np.float32), prim, np.zeros(len(org), np.uint32)

    def ray_hit(self, origin, direction, max_range_m: float = 5000.0) -> Hit:
        o, d, L = self._ray_args(origin, direction, max_range_m)
        h0 = float(self.eff.nearest(o[0], o[1], "nan")[0])
        inside = bool(np.isfinite(h0) and o[2] <= h0)
        t, _prim, nrm = self._cast(o[None], d[None], np.array([L]))
        if not np.isfinite(t[0]):
            return self._miss(inside)
        p = o + d * t[0]
        n = nrm[0].astype(np.float64)
        n = n / max(float(np.linalg.norm(n)), 1e-12)
        if n @ d > 0:
            n = -n                                                # 法线朝向射线来向
        gz = float(self.dtm.bilinear(p[0], p[1])[0])
        r, c, ok = self.eff.idx(np.array([p[0]]), np.array([p[1]]))
        return Hit(True, p, float(t[0]), "top" if n[2] >= 0.7 else "side", n, "dtm" if p[2] - gz <= 1.0 else "dsm",
                   (int(r[0]), int(c[0])) if ok[0] else None, gz, float(p[2] - gz), inside)

    def los_batch(self, A, B, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray:
        """精确视线（两端各裁 eps，语义同 D2；`step_m` 只为签名兼容）。"""
        A, B = self._los_args(A, B, step_m)
        L = np.linalg.norm(B - A, axis=1)
        out = np.ones(len(A), bool)
        m = 2 * eps_m < L
        if m.any():
            d = (B[m] - A[m]) / L[m, None]
            t, _, _ = self._cast(A[m] + d * eps_m, d, L[m] - 2 * eps_m)
            out[m] = ~np.isfinite(t)
        return out

    def los_batch_np(self, A, B, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray:
        return self.los_batch(A, B, step_m, eps_m)

    def segment_los(self, a, b, eps_m: float = 0.5) -> bool:
        a = np.asarray(a, np.float64).reshape(1, 3)
        b = np.asarray(b, np.float64).reshape(1, 3)
        if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
            raise GeoError(300, "BAD_VECTOR")
        return bool(self.los_batch(a, b, 1.0, eps_m)[0])

    def free_distance(self, origins, dirs, max_m: float) -> np.ndarray:
        org = np.asarray(origins, np.float64).reshape(-1, 3)
        dv = np.asarray(dirs, np.float64).reshape(-1, 3)
        if len(org) != len(dv) or len(org) > MAX_RAYS:
            raise GeoError(PARAM_OUT_OF_RANGE, f"rays must be <= {MAX_RAYS} and pair up")
        if not (0 < max_m <= 5000.0):
            raise GeoError(PARAM_OUT_OF_RANGE, "max_m must be in (0, 5000]")
        out = np.full(len(org), float(max_m), np.float64)
        ok = np.linalg.norm(dv, axis=1) > 0                       # 零方向保持 max_m（同 D2）
        if ok.any():
            unit, _ = _unit(dv[ok])
            t, _, _ = self._cast(org[ok], unit, np.full(int(ok.sum()), float(max_m)))
            out[ok] = np.where(np.isfinite(t), t, float(max_m))
        return out

    def closest_batch(self, p, max_m: float) -> np.ndarray:
        """到网格最近表面的距离（float32，超过 max_m 记 max_m）。"""
        import open3d as o3d

        P = np.asarray(p, np.float64).reshape(-1, 3)
        best = np.full(len(P), float(max_m), np.float64)
        for tile in self.tiles:
            gap = np.maximum(np.maximum(tile.lo - P, P - tile.hi), 0.0)
            m = np.linalg.norm(gap, axis=1) < best
            if not m.any():
                continue
            ii = np.flatnonzero(m)
            dist = tile.scene.compute_distance(o3d.core.Tensor((P[ii] - tile.origin).astype(np.float32))).numpy()
            best[ii] = np.minimum(best[ii], dist)
        return best.astype(np.float32)

    def semantic_of(self, prim_id) -> np.ndarray:
        """anet-classes@1 静态类码；无 `cls.u8` 流或未命中（MISS）时为 0（unclassified）。"""
        pid = np.asarray(prim_id, np.uint32)
        out = np.zeros(pid.shape, np.uint8)
        if self.semantic is not None:
            ok = pid < len(self.semantic)
            out[ok] = self.semantic[pid[ok]]
        return out

    def ready_event(self) -> dict:
        ev = super().ready_event()
        ev["mesh"] = {"tiles": len(self.tiles), "triangles": self.n_triangles, "semantic": self.semantic is not None}
        return ev


def open_mesh_world_query(world_dir: Path, cache_root: Path | None = None, params=None, allow_derive: bool | None = None) -> MeshWorldQuery:
    """扩展点 7 的工厂（`MESH_FACTORY`）：DSM 部分同 `open_dsm_world_query`，再装载 `mesh.raycast` 瓦片。"""
    return MeshWorldQuery.from_dsm(open_dsm_world_query(world_dir, cache_root, params, allow_derive), Path(world_dir))
