"""MeshWorldQuery（AWR-05 §5.1.6；M04 §15.1 扩展点 7；M22-FR-047；ADR-131）：网格为几何真值的世界查询。

继承 `DsmWorldQuery` 的全部 2.5D 方法（高度、走廊、贴地，网格派生的 2 m 栅格，语义与 D2 相同）；3D 方法（`ray_hit`、
`segment_los`、`los_batch`、`free_distance`）覆盖为网格精确求交，另加 `raycast_batch`、`closest_batch` 与 `semantic_of`。

求交数据为 `mesh.raycast`（`awr-trimesh@1`）：每瓦片一个 Open3D `RaycastingScene`（Embree），顶点为瓦片局部原点下的
float32 相对坐标；射线段先裁到世界高度范围，再以平面外接框与瓦片 AABB 相交选出候选瓦片，在该瓦片局部帧下以 float32 求交，
取各瓦片最近命中；命中距离按命中三角形平面以 float64 重算（`_plane_t`），Embree 只决定命中哪个三角形。
D2 接口语义保持（D3-AC-05）：起点在柱体内的 `ray_hit` 返回离开柱体后的首个命中，探针服务的 `ray_hit_iter`、`segment_los_iter`
走同一网格求交，`free_distance` 条数上限与错误码、`ready_event` 字段集合同 D2（网格概况见 `mesh_info`）。
全局三角形号 `prim_id` = 瓦片 `t0` + 瓦片内序号（即 `tri` 数组行号）；静态网格的实例号为 0。

`walkable_height`（M22-FR-046、M22-AC-015；D3-AC-07）：自导航面高度上方 2 m 竖直向下与网格求交，取首个命中（即不高于起点的
最高表面）。求交不经 Embree，而按显式次序的 float64 运算定义（`WalkIndex`、`_walk_batch`），以便前端
`apps/web/src/engine/actors/tape/walkable.ts` 逐位复现（golden `packages/contracts/golden/event/walkable-small.*`）。

尚未提供（随 NavMesh 图层落地）：`nav_path`。
"""

from __future__ import annotations

import functools
import importlib.util
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.world.geometry.query import MAX_FREE_RAYS, DsmWorldQuery, open_dsm_world_query
from awr.world.geometry.traverse import exit_param
from awr.world.geometry.types import PARAM_OUT_OF_RANGE as GEO_PARAM_OUT_OF_RANGE
from awr.world.geometry.types import GeoError, GeoLoadError, Hit

HAVE_NUMBA = importlib.util.find_spec("numba") is not None and os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"


class _LazyJit:
    """惰性 numba 内核：首次调用时才导入 numba 并编译（同 D2 M04 `kernels.py` 的惰性导入：打开世界与 2.5D、3D 查询不导入
    numba，D2 用例 `test_no_numba_import_without_need` 在 MeshWorldQuery 下同样成立）；`py_func` 为 Python 原函数。"""

    def __init__(self, f, kw: dict):
        self.py_func, self._kw, self._fn = f, kw, None
        functools.update_wrapper(self, f)

    def __call__(self, *args):
        if self._fn is None:
            if HAVE_NUMBA:
                from numba import njit as _njit

                self._fn = _njit(**self._kw)(self.py_func)
            else:  # pragma: no cover
                self._fn = self.py_func
        return self._fn(*args)


def njit(*a, **k):
    def deco(f):
        return _LazyJit(f, k)

    return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["MISS", "WALK_ABOVE_M", "WALK_CELL_M", "WALK_PROFILES", "MeshTile", "MeshWorldQuery", "WalkIndex", "open_mesh_world_query"]

MISS = np.uint32(0xFFFFFFFF)
MAX_RAYS = 1 << 22
PARAM_OUT_OF_RANGE = 301
INSIDE_SKIP_M = 1e-3                # 起点在柱体内：离开柱体后再前进 1 mm 起求交（不把离开所经的面当作命中）
CELL_PROBE_M = 1e-4                 # 命中点沿射线前进 0.1 mm 定所属格
PLANE_PARALLEL = 1e-9               # |n·d| ≤ 1e-9·|n| 视为与三角形平面平行，保留 Embree 距离
PLANE_TOL_M = 1e-2                  # float64 重算与 Embree float32 距离之差超过 1 cm 时保留 Embree 距离
WALK_ABOVE_M = 2.0                  # 求交起点在导航面高度上方 2 m（M22 §6 时序图 "自导航面高度 + 2 m 向下网格求交"）
WALK_CELL_M = 2.0                   # 候选三角形格索引的格边长（只影响计算量，不影响结果）
WALK_BARY_EPS = 1e-9                # 重心坐标容差（同 awr.sim.events.ground）
WALK_DET_MIN = 1e-12                # 平面投影退化（竖直面）阈值
WALK_PROFILES = ("person", "vehicle")
NavSurface = Callable[[np.ndarray], np.ndarray]     # (N, 2) world 帧 m → (N,) 导航面高度（无导航面为 NaN）


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


def _plane_t(tile: MeshTile, prim: np.ndarray, o: np.ndarray, d: np.ndarray, t32: np.ndarray) -> np.ndarray:
    """Embree 命中距离按命中三角形的平面以 float64 重算：顶点为瓦片局部 float32 按值转 float64，起点为 float64 世界坐标减瓦片
    原点（不经 float32 舍入）；Embree 只决定命中哪个三角形。近平行或与 float32 距离相差超过 `PLANE_TOL_M` 时保留 float32 距离。"""
    P = tile.V[tile.F[prim].astype(np.int64)].astype(np.float64)              # (k, 3, 3) 局部
    n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
    den = np.einsum("ij,ij->i", n, d)
    num = np.einsum("ij,ij->i", n, P[:, 0] - (o - tile.origin))
    with np.errstate(divide="ignore", invalid="ignore"):
        t = num / den
    keep = (np.abs(den) > PLANE_PARALLEL * np.linalg.norm(n, axis=1)) & np.isfinite(t) & (np.abs(t - t32) <= PLANE_TOL_M)
    return np.where(keep, np.maximum(t, 0.0), t32)


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


@dataclass
class WalkIndex:
    """`walkable_height` 的候选三角形索引：每瓦片一张 2 m 格 CSR（格范围取瓦片顶点的平面包围盒），三角形登记到其平面闭包围盒
    覆盖的全部格。顶点按值读自 `xyz`（瓦片局部 float32），转 float64 后加瓦片原点；不另存世界帧顶点。

    判定（与索引无关，前端逐位复现）：三角形 g 在 (x, y) 处命中，当且仅当 (x, y) 在其三个顶点的平面闭包围盒内、投影行列式
    |d| ≥ 1e-12、三个重心坐标均 ≥ −1e-9；命中高度 z = l1·z1 + l2·z2 + l3·z3（逐项从左到右求和）；起点 z_top 以下（z ≤ z_top）的
    命中中取 z 最大者，z 相同取全局三角形号最小者。
    """

    xyz: np.ndarray         # (nv, 3) float32 瓦片局部
    tri: np.ndarray         # (nt, 3) uint32 瓦片内顶点号
    tile_of: np.ndarray     # (nt,) int32
    org: np.ndarray         # (K, 3) float64 瓦片原点
    v0: np.ndarray          # (K,) int64
    lo: np.ndarray          # (K, 2) float64 瓦片顶点平面包围盒
    hi: np.ndarray
    gx0: np.ndarray         # (K,) int64 格号原点 floor(x / 2)
    gy0: np.ndarray
    gnx: np.ndarray
    gny: np.ndarray
    coff: np.ndarray        # (K,) int64 瓦片首格在 start 中的偏移
    start: np.ndarray       # (Σ 格数 + 1,) int64 CSR 起点
    items: np.ndarray       # int32 全局三角形号（格内升序）

    @classmethod
    def build(cls, xyz: np.ndarray, tri: np.ndarray, tiles: list[dict]) -> WalkIndex:
        """xyz、tri 为 `awr-trimesh@1` 的 `xyz.f32`、`tri.u32` 全流；tiles 为 index.json 的 `tiles`（`origin_m`、`v0`、`nv`、`t0`、`nt`）。"""
        xyz = np.ascontiguousarray(np.asarray(xyz, np.float32).reshape(-1, 3))
        tri = np.ascontiguousarray(np.asarray(tri, np.uint32).reshape(-1, 3))
        K = len(tiles)
        org = np.array([t["origin_m"] for t in tiles], np.float64).reshape(K, 3)
        v0 = np.array([t["v0"] for t in tiles], np.int64)
        tile_of = np.zeros(len(tri), np.int32)
        lo, hi = np.full((K, 2), np.inf), np.full((K, 2), -np.inf)          # 空瓦片的包围盒为空集
        gx0, gy0, gnx, gny, coff = (np.zeros(K, np.int64) for _ in range(5))
        spans, off = [], 0
        for k, t in enumerate(tiles):
            t0, nt = int(t["t0"]), int(t["nt"])
            tile_of[t0:t0 + nt] = k
            if nt == 0:
                spans.append(np.zeros((0, 4), np.int64))
                gnx[k] = gny[k] = 0
                coff[k] = off
                continue
            P = xyz[v0[k] + tri[t0:t0 + nt].astype(np.int64)].astype(np.float64)[:, :, :2] + org[k, :2]    # (nt, 3, 2)
            mn, mx = P.min(1), P.max(1)
            lo[k], hi[k] = mn.min(0), mx.max(0)
            gx0[k], gy0[k] = math.floor(lo[k, 0] / WALK_CELL_M), math.floor(lo[k, 1] / WALK_CELL_M)
            gnx[k] = math.floor(hi[k, 0] / WALK_CELL_M) - gx0[k] + 1
            gny[k] = math.floor(hi[k, 1] / WALK_CELL_M) - gy0[k] + 1
            coff[k] = off
            off += int(gnx[k] * gny[k])
            spans.append(np.stack([np.floor(mn[:, 0] / WALK_CELL_M).astype(np.int64) - gx0[k],
                                   np.floor(mx[:, 0] / WALK_CELL_M).astype(np.int64) - gx0[k],
                                   np.floor(mn[:, 1] / WALK_CELL_M).astype(np.int64) - gy0[k],
                                   np.floor(mx[:, 1] / WALK_CELL_M).astype(np.int64) - gy0[k]], 1))
        span = np.concatenate(spans) if spans else np.zeros((0, 4), np.int64)
        start, items = _walk_register(np.ascontiguousarray(span), tile_of, gnx, coff, off)
        return cls(xyz, tri, tile_of, org, v0, lo, hi, gx0, gy0, gnx, gny, coff, start, items)

    def surface(self, xy: np.ndarray, z_top: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(N, 2) 平面点、(N,) 起点高度（NaN 不求交，+inf 取最高表面）→ (z float64，无命中 NaN；prim int64，无命中 −1)。"""
        xy = np.ascontiguousarray(np.asarray(xy, np.float64).reshape(-1, 2))
        zt = np.ascontiguousarray(np.asarray(z_top, np.float64).reshape(-1))
        return _walk_batch(self.xyz, self.tri, self.tile_of, self.org, self.v0, self.lo, self.hi, self.gx0, self.gy0, self.gnx,
                           self.coff, self.start, self.items, xy, zt)


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
        q._walk_src = (xyz, tri, idx["tiles"])
        q._walk = None
        q.nav_surface = {}
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
            hit = np.flatnonzero(np.isfinite(th))
            if hit.size:
                th[hit] = _plane_t(tile, r["primitive_ids"].numpy()[hit].astype(np.int64), o[ii[hit]], d[ii[hit]], th[hit])
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
        """首个网格命中。起点在 2 m 柱体内（z ≤ 所在格 `dsm_eff`）时语义同 D2：从射线离开柱体处（`exit_param`）后
        `INSIDE_SKIP_M` 起求交，返回离开后的首个命中；`dist_m` 仍自起点计。"""
        o, d, L = self._ray_args(origin, direction, max_range_m)
        h0 = float(self.eff.nearest(o[0], o[1], "nan")[0])
        inside = bool(np.isfinite(h0) and o[2] <= h0)
        s0 = 0.0
        if inside:
            te = exit_param(self.eff, o, o + d * L)
            if te is None or te * L + INSIDE_SKIP_M >= L:
                return self._miss(inside)
            s0 = te * L + INSIDE_SKIP_M
        t, _prim, nrm = self._cast((o + d * s0)[None], d[None], np.array([L - s0]))
        if not np.isfinite(t[0]):
            return self._miss(inside)
        dist = float(t[0]) + s0
        p = o + d * dist
        n = nrm[0].astype(np.float64)
        n = n / max(float(np.linalg.norm(n)), 1e-12)
        if n @ d > 0:
            n = -n                                                # 法线朝向射线来向
        gz = float(self.dtm.bilinear(p[0], p[1])[0])
        q = p + d * CELL_PROBE_M                                  # 命中面所属的格：沿射线略进一步（侧面落在格边界上）
        r, c, ok = self.eff.idx(np.array([q[0]]), np.array([q[1]]))
        return Hit(True, p, dist, "top" if n[2] >= 0.7 else "side", n, "dtm" if p[2] - gz <= 1.0 else "dsm",
                   (int(r[0]), int(c[0])) if ok[0] else None, gz, float(p[2] - gz), inside)

    def ray_hit_iter(self, origin, direction, max_range_m: float = 5000.0):
        """探针服务（`GeoProbeServer`）的生成器接口：网格求交一次完成，先让出一次以保持分片调度形状。"""
        yield None
        return self.ray_hit(origin, direction, max_range_m)

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

    def _segment_los(self, a, b, eps_m: float) -> tuple[bool, np.ndarray | None]:
        a = np.asarray(a, np.float64).reshape(3)
        b = np.asarray(b, np.float64).reshape(3)
        if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
            raise GeoError(300, "BAD_VECTOR")
        L = float(np.linalg.norm(b - a))
        if 2 * eps_m >= L:
            return True, None
        d = (b - a) / L
        t, _, _ = self._cast((a + d * eps_m)[None], d[None], np.array([L - 2 * eps_m]))
        if not np.isfinite(t[0]):
            return True, None
        return False, a + d * (eps_m + float(t[0]))

    def segment_los(self, a, b, eps_m: float = 0.5) -> bool:
        return self._segment_los(a, b, eps_m)[0]

    def segment_los_iter(self, a, b, eps_m: float = 0.5):
        """探针服务的生成器接口：返回 (是否可见, 首个遮挡点或 None)，与 `segment_los`、`los_batch` 同一求交路径。"""
        yield None
        return self._segment_los(a, b, eps_m)

    def free_distance(self, origins, dirs, max_m: float) -> np.ndarray:
        """方向自由距离（float64，未命中为 max_m）；条数上限与错误码同 D2（≤ 16 条，110）。"""
        org = np.asarray(origins, np.float64).reshape(-1, 3)
        dv = np.asarray(dirs, np.float64).reshape(-1, 3)
        if len(org) != len(dv) or len(org) > MAX_FREE_RAYS:
            raise GeoError(GEO_PARAM_OUT_OF_RANGE, f"rays must be <= {MAX_FREE_RAYS}")
        if not (0 < max_m <= 5000.0):
            raise GeoError(GEO_PARAM_OUT_OF_RANGE, "max_m must be in (0, 5000]")
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

    def closest_points_batch(self, p, max_m: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """到网格最近表面的最近点（M24 `mesh_contact` 的贴近判定）：(距离 float64，超过 max_m 记 max_m；最近点 (N, 3) float64
        世界帧，未找到时为查询点本身；全局 prim_id uint32，未找到为 MISS)。瓦片筛选与 `closest_batch` 相同。"""
        import open3d as o3d

        P = np.asarray(p, np.float64).reshape(-1, 3)
        best = np.full(len(P), float(max_m), np.float64)
        pts = P.copy()
        prim = np.full(len(P), MISS, np.uint32)
        for tile in self.tiles:
            gap = np.maximum(np.maximum(tile.lo - P, P - tile.hi), 0.0)
            m = np.linalg.norm(gap, axis=1) < best
            if not m.any():
                continue
            ii = np.flatnonzero(m)
            r = tile.scene.compute_closest_points(o3d.core.Tensor((P[ii] - tile.origin).astype(np.float32)))
            c = r["points"].numpy().astype(np.float64) + tile.origin
            d = np.linalg.norm(c - P[ii], axis=1)
            better = d < best[ii]
            jj = ii[better]
            best[jj], pts[jj] = d[better], c[better]
            prim[jj] = (r["primitive_ids"].numpy()[better].astype(np.int64) + tile.t0).astype(np.uint32)
        return best, pts, prim

    def semantic_of(self, prim_id) -> np.ndarray:
        """anet-classes@1 静态类码；无 `cls.u8` 流或未命中（MISS）时为 0（unclassified）。"""
        pid = np.asarray(prim_id, np.uint32)
        out = np.zeros(pid.shape, np.uint8)
        if self.semantic is not None:
            ok = pid < len(self.semantic)
            out[ok] = self.semantic[pid[ok]]
        return out

    # ------------------------------------------------------------------ 可走面高度
    def attach_nav(self, profile: str, surface: NavSurface | None) -> None:
        """登记 profile 的导航面高度函数（NavMesh 图层的 Detour `getPolyHeight`，或事件编译器的过渡栅格）；None 撤销。"""
        if profile not in WALK_PROFILES:
            raise GeoError(PARAM_OUT_OF_RANGE, f"profile must be one of {WALK_PROFILES}")
        if surface is None:
            self.nav_surface.pop(profile, None)
        else:
            self.nav_surface[profile] = surface

    def walk_index(self) -> WalkIndex:
        """首次调用时建索引（只读原流，每三角形约 4–16 B 索引）。"""
        if self._walk is None:
            self._walk = WalkIndex.build(*self._walk_src)
        return self._walk

    def walkable_surface(self, xy, z_top) -> tuple[np.ndarray, np.ndarray]:
        """自 z_top 竖直向下的首个网格命中：(z float64，无命中 NaN；prim_id int64，无命中 −1)。"""
        P = np.asarray(xy, np.float64).reshape(-1, 2)
        if len(P) > MAX_RAYS:
            raise GeoError(PARAM_OUT_OF_RANGE, f"points must be <= {MAX_RAYS}")
        if not np.all(np.isfinite(P)):
            raise GeoError(300, "BAD_VECTOR")
        zt = np.broadcast_to(np.asarray(z_top, np.float64), (len(P),))
        return self.walk_index().surface(P, zt)

    def walkable_height(self, xy, profile: str = "person", nav_z=None) -> np.ndarray:
        """可走面高度（float32，无导航面或无命中为 NaN）：自导航面高度 + 2 m 竖直向下的首个网格命中。

        导航面高度依次取：参数 `nav_z`（(N,)，NaN 表示该点无导航面）；`attach_nav` 登记的该 profile 导航面函数；均无时为过渡口径
        （NavMesh 图层就绪前）：起点取 +inf，即该点的最高网格表面，与事件编译器自上而下的过渡高度场同义，profile 不参与。
        """
        if profile not in WALK_PROFILES:
            raise GeoError(PARAM_OUT_OF_RANGE, f"profile must be one of {WALK_PROFILES}")
        P = np.asarray(xy, np.float64).reshape(-1, 2)
        if nav_z is not None:
            nz = np.asarray(nav_z, np.float64).reshape(-1)
            nz = np.full(len(P), nz[0]) if len(nz) == 1 else nz
        elif profile in self.nav_surface:
            nz = np.asarray(self.nav_surface[profile](P), np.float64).reshape(-1)
        else:
            nz = np.full(len(P), np.inf)
        if len(nz) != len(P):
            raise GeoError(300, "nav_z must have one value per point")
        z, _prim = self.walkable_surface(P, nz + WALK_ABOVE_M)
        return z.astype(np.float32)

    def mesh_info(self) -> dict:
        """网格图层概况（`ready_event` 保持 D2 `geo.ready` 字段集合不变，网格信息由此单独提供）。"""
        return {"tiles": len(self.tiles), "triangles": self.n_triangles, "semantic": self.semantic is not None}


def open_mesh_world_query(world_dir: Path, cache_root: Path | None = None, params=None, allow_derive: bool | None = None) -> MeshWorldQuery:
    """扩展点 7 的工厂（`MESH_FACTORY`）：DSM 部分同 `open_dsm_world_query`，再装载 `mesh.raycast` 瓦片。"""
    return MeshWorldQuery.from_dsm(open_dsm_world_query(world_dir, cache_root, params, allow_derive), Path(world_dir))


# ---------------------------------------------------------------------- walkable 内核（numba 不做 FMA 合并，与前端逐位一致）
@njit(cache=True)
def _walk_register(span, tile_of, gnx, coff, n_cells):  # pragma: no cover - numba
    cnt = np.zeros(n_cells + 1, np.int64)
    for g in range(len(span)):
        k = tile_of[g]
        for cy in range(span[g, 2], span[g, 3] + 1):
            for cx in range(span[g, 0], span[g, 1] + 1):
                cnt[coff[k] + cy * gnx[k] + cx + 1] += 1
    for c in range(n_cells):
        cnt[c + 1] += cnt[c]
    items = np.empty(cnt[n_cells], np.int32)
    fill = cnt[:n_cells].copy()
    for g in range(len(span)):                             # g 升序 → 格内按全局三角形号升序
        k = tile_of[g]
        for cy in range(span[g, 2], span[g, 3] + 1):
            for cx in range(span[g, 0], span[g, 1] + 1):
                c = coff[k] + cy * gnx[k] + cx
                items[fill[c]] = g
                fill[c] += 1
    return cnt, items


@njit(cache=True)
def _walk_batch(xyz, tri, tile_of, org, v0, lo, hi, gx0, gy0, gnx, coff, start, items, xy, zt):  # pragma: no cover - numba
    n = len(xy)
    out = np.full(n, np.nan)
    prim = np.full(n, -1, np.int64)
    for p in range(n):
        x, y, top = xy[p, 0], xy[p, 1], zt[p]
        if math.isnan(top):                                # 无导航面
            continue
        bz, bid = 0.0, -1
        for k in range(len(v0)):
            if x < lo[k, 0] or x > hi[k, 0] or y < lo[k, 1] or y > hi[k, 1]:
                continue
            c = coff[k] + (math.floor(y / 2.0) - gy0[k]) * gnx[k] + (math.floor(x / 2.0) - gx0[k])
            ox, oy, oz = org[k, 0], org[k, 1], org[k, 2]
            b = v0[k]
            for s in range(start[c], start[c + 1]):
                g = items[s]
                i1, i2, i3 = b + tri[g, 0], b + tri[g, 1], b + tri[g, 2]
                x1, y1, z1 = float(xyz[i1, 0]) + ox, float(xyz[i1, 1]) + oy, float(xyz[i1, 2]) + oz
                x2, y2, z2 = float(xyz[i2, 0]) + ox, float(xyz[i2, 1]) + oy, float(xyz[i2, 2]) + oz
                x3, y3, z3 = float(xyz[i3, 0]) + ox, float(xyz[i3, 1]) + oy, float(xyz[i3, 2]) + oz
                if x < min(x1, x2, x3) or x > max(x1, x2, x3) or y < min(y1, y2, y3) or y > max(y1, y2, y3):
                    continue
                d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
                if abs(d) < 1e-12:
                    continue
                l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
                l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
                l3 = 1 - l1 - l2
                if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                    continue
                z = l1 * z1 + l2 * z2 + l3 * z3
                if not (z <= top):
                    continue
                if bid < 0 or z > bz or (z == bz and g < bid):
                    bz, bid = z, g
        if bid >= 0:
            out[p] = bz
            prim[p] = bid
    return out, prim
