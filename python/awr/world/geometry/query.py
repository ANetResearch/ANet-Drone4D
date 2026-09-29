"""`WorldQuery` 的 numpy 实现 `DsmWorldQuery` 与装载入口 `open_world_query`（M04 §6.4、§7.1；D1-core）。

全部查询为纯函数：不使用线程、不使用 BLAS、不修改内部状态（非默认 buffer 的膨胀栅格缓存除外，对结果无影响）。
坐标一律 world ENU、float64；栅格值 float32。只读 `geometry/**` 与 `semantic/zones.geojson`（P-03）。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal, Protocol

import numpy as np

from . import path as _path
from .cache import derive_key, entry_dir, load_entry, verify_entry, write_entry
from .derive import derive_arrays, obs_counts
from .grids import Grid
from .heightmap import corridor_max_sampled, inflate, max_pyramid
from .traverse import exit_param, first_hit, first_hit_iter, max_along
from .types import (
    DERIVE_VERSION,
    NEG,
    PARAM_OUT_OF_RANGE,
    CoarseResult,
    GeoError,
    GeoLoadError,
    GeoParams,
    GridView,
    Hit,
    PathValidResult,
    TransitProfile,
)
from .zones import ZoneIndex, ZoneRaster

MAX_RAY_M = 5000.0
MAX_RADIUS_M = 50.0
MAX_CONTACT_R_M = 2.0
MAX_FREE_RAYS = 16
MAX_LOS_BATCH = 64


class WorldQuery(Protocol):
    """P-07 冻结；V0.2/V0.3 只追加方法（M04 §7.1）。"""

    world_id: str
    content_version: str
    derive_sha8: str
    coordinate_sha256: str
    bounds_m: np.ndarray
    zones: ZoneIndex

    def dsm_grid(self) -> GridView: ...
    def dtm_grid(self) -> GridView: ...
    def height_dsm(self, xy: np.ndarray, *, raw: bool = False, oob: Literal["clamp", "nan"] = "clamp") -> np.ndarray: ...
    def ground_dtm(self, xy: np.ndarray, *, oob: Literal["clamp", "nan"] = "clamp") -> np.ndarray: ...
    def agl(self, xyz: np.ndarray) -> np.ndarray: ...
    def column_max_within(self, xy: np.ndarray, radius_m: float = 0.0) -> np.ndarray: ...
    def clearance(self, xyz: np.ndarray, radius_m: float = 0.0) -> np.ndarray: ...
    def contact_mask(self, xyz: np.ndarray, radius_m: float) -> np.ndarray: ...
    def probe(self, xy: np.ndarray) -> list[dict]: ...
    def ray_hit(self, origin: np.ndarray, direction: np.ndarray, max_range_m: float = 5000.0) -> Hit: ...
    def segment_los(self, a: np.ndarray, b: np.ndarray, eps_m: float = 0.5) -> bool: ...
    def los_batch(self, A: np.ndarray, B: np.ndarray, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray: ...
    def free_distance(self, origins: np.ndarray, dirs: np.ndarray, max_m: float) -> np.ndarray: ...
    def heightmap_top_along(self, A: np.ndarray, B: np.ndarray, *, tol_m: float = 100.0, max_samples: int = 30000,
                            exact: bool = False) -> np.ndarray | float: ...
    def path_coarse_check(self, polyline: np.ndarray, *, buffer_m: float = 1.0, goal_clear_m: float = 2.0,
                          goal_radius_m: float = 0.0, active_zone_ids: Sequence[str] | None = None) -> CoarseResult: ...
    def path_valid(self, polyline: np.ndarray, *, buffer_m: float = 1.0, steps_per_m: int = 20,
                   active_zone_ids: Sequence[str] | None = None, endpoint_radius_m: float | None = None,
                   endpoint_clear_m: float = 0.5) -> PathValidResult: ...
    def safe_transit_profile(self, a: np.ndarray, b: np.ndarray, *, margin_m: float = 5.0,
                             z_ceiling_m: float | None = None) -> TransitProfile: ...
    def terrain_profile(self, polyline: np.ndarray, ds_m: float = 2.0) -> dict[str, np.ndarray]: ...
    def grid_2p5d(self, res_m: float = 4.0) -> tuple[np.ndarray, dict]: ...
    def iter_op(self, op: str, args: dict) -> Iterator[None]: ...


def _xy(a) -> np.ndarray:
    a = np.asarray(a, np.float64)
    if a.ndim == 1:
        a = a[None]
    if a.shape[-1] < 2:
        raise GeoError(300, "points must have at least 2 components")
    return a[:, :2]


def _xyz(a) -> np.ndarray:
    a = np.asarray(a, np.float64)
    if a.ndim == 1:
        a = a[None]
    if a.shape[-1] != 3:
        raise GeoError(300, "points must be (n, 3)")
    return a


def _ro(a: np.ndarray) -> np.ndarray:
    v = a.view()
    v.flags.writeable = False
    return v


class DsmWorldQuery:
    """numpy 实现（柱体语义 DSM、观测感知闭运算、Height_map 与金字塔、精确遍历、走廊采样、每米 20 步细校验）。"""

    def __init__(self, *, world_id: str, content_version: str, coordinate_sha256: str, derive_sha8: str,
                 bounds_m: np.ndarray, dsm_raw: Grid, dtm: Grid, arrays: dict[str, np.ndarray], zones: ZoneIndex,
                 params: GeoParams, qa: dict, cache_dir: Path | None = None, cache_state: str = "built", load_ms: float = 0.0):
        self.world_id = world_id
        self.content_version = content_version
        self.coordinate_sha256 = coordinate_sha256
        self.derive_sha8 = derive_sha8
        self.bounds_m = np.asarray(bounds_m, np.float64)
        self.params = params
        self.zones = zones
        self.qa = qa
        self.cache_dir = cache_dir
        self.cache_state = cache_state
        self.load_ms = load_ms
        x0, y0, cell = dsm_raw.x0, dsm_raw.y0, dsm_raw.cell
        self.dsm_raw = dsm_raw
        self.dtm = dtm
        self.eff = Grid(arrays["dsm_eff"], x0, y0, cell)
        self.dil1 = Grid(arrays["dsm_dil1"], x0, y0, cell)
        self.hm = Grid(arrays["hm"], x0, y0, cell)
        self.obs_n = arrays["obs_n"]

        def pyr(prefix: str, base: np.ndarray, start: int = 1) -> list:
            out = [base] if start == 1 else [None] * start
            i = start
            while f"{prefix}_L{i}" in arrays:
                out.append(arrays[f"{prefix}_L{i}"])
                i += 1
            return out

        self.pyr_dsm = pyr("pyr_dsm", arrays["dsm_eff"])
        self.pyr_hm = pyr("pyr_hm", arrays["hm"])
        self.pyrdil_hm = pyr("pyrdil_hm", None, start=2)
        self.infl1 = Grid(arrays["inflated_1m"], x0, y0, cell)
        self.pyr_infl1 = pyr("pyr_inflated_1m", arrays["inflated_1m"])
        self._infl_cache: dict[float, tuple[Grid, list[np.ndarray]]] = {1.0: (self.infl1, self.pyr_infl1)}
        if zones.raster is None:
            zones.raster = ZoneRaster.from_mask(arrays["zone_raster_8m"], x0, y0, params.zone_raster_cell_m)
        if not (params.hm_dilate_cells * cell >= 1.0 and params.hm_safe_m >= 1.0):   # FR-020 单调性前提（默认 buffer 1 m）
            raise GeoLoadError("GEO_GRID_INVALID", "hm_dilate_cells*cell and hm_safe_m must be >= 1 m (coarse/fine monotonicity)")

    # ---------------------------------------------------------------- 只读栅格
    def dsm_grid(self) -> GridView:
        return GridView(_ro(np.asarray(self.eff.a)), self.eff.x0, self.eff.y0, self.eff.cell)

    def dtm_grid(self) -> GridView:
        return GridView(_ro(np.asarray(self.dtm.a)), self.dtm.x0, self.dtm.y0, self.dtm.cell)

    # ---------------------------------------------------------------- 点查询
    def height_dsm(self, xy, *, raw: bool = False, oob: Literal["clamp", "nan"] = "clamp") -> np.ndarray:
        p = _xy(xy)
        return (self.dsm_raw if raw else self.eff).nearest(p[:, 0], p[:, 1], oob)

    def ground_dtm(self, xy, *, oob: Literal["clamp", "nan"] = "clamp") -> np.ndarray:
        p = _xy(xy)
        return self.dtm.bilinear(p[:, 0], p[:, 1], oob).astype(np.float32)

    def agl(self, xyz) -> np.ndarray:
        p = _xyz(xyz)
        return (p[:, 2] - self.dtm.bilinear(p[:, 0], p[:, 1])).astype(np.float32)

    def column_max_within(self, xy, radius_m: float = 0.0) -> np.ndarray:
        """max{dsm_eff[c] : 格 c 的正方形与圆盘 (xy, r) 相交}；r = 0 即所在格（精确，向量化）。"""
        r = float(radius_m)
        if not (0.0 <= r <= MAX_RADIUS_M) or not math.isfinite(r):
            raise GeoError(PARAM_OUT_OF_RANGE, f"radius_m must be in [0, {MAX_RADIUS_M}]")
        g = self.eff
        p = _xy(xy)
        fx, fy = g.fidx(p[:, 0], p[:, 1])
        c = np.floor(fx).astype(np.int64)
        rw = np.floor(fy).astype(np.int64)
        if r == 0.0:
            return np.asarray(g.a[np.clip(rw, 0, g.h - 1), np.clip(c, 0, g.w - 1)], np.float32)
        k = math.ceil(r / g.cell)
        off = np.arange(-k, k + 1)
        DY, DX = np.meshgrid(off, off, indexing="ij")
        DY = DY.ravel()[None]
        DX = DX.ravel()[None]
        out = np.empty(len(p), np.float32)
        step = max(1, 2_000_000 // DX.size)
        for s in range(0, len(p), step):
            e = min(len(p), s + step)
            ux = (fx[s:e] - c[s:e])[:, None]
            uy = (fy[s:e] - rw[s:e])[:, None]
            ddx = np.where(DX < 0, ux + (-DX - 1), np.where(DX > 0, 1 - ux + (DX - 1), 0.0)) * g.cell
            ddy = np.where(DY < 0, uy + (-DY - 1), np.where(DY > 0, 1 - uy + (DY - 1), 0.0)) * g.cell
            ok = ddx * ddx + ddy * ddy <= r * r
            cc = np.clip(c[s:e, None] + DX, 0, g.w - 1)
            rr = np.clip(rw[s:e, None] + DY, 0, g.h - 1)
            out[s:e] = np.where(ok, g.a[rr, cc], NEG).max(1)
        return out

    def clearance(self, xyz, radius_m: float = 0.0) -> np.ndarray:
        p = _xyz(xyz)
        return (p[:, 2] - self.column_max_within(p[:, :2], radius_m)).astype(np.float32)

    def contact_mask(self, xyz, radius_m: float) -> np.ndarray:
        """两段式：先以 `z − r ≤ dsm_dil1` 预筛，只对命中行做精确圆盘判定（与精确判定逐元素相等，r ≤ 格宽）。"""
        r = float(radius_m)
        if not (0.0 <= r <= MAX_CONTACT_R_M) or r > self.eff.cell:
            raise GeoError(PARAM_OUT_OF_RANGE, f"radius_m must be in [0, min({MAX_CONTACT_R_M}, cell)]")
        p = _xyz(xyz)
        z = p[:, 2] - r
        flag = z <= self.dil1.nearest(p[:, 0], p[:, 1])
        out = np.zeros(len(p), bool)
        idx = np.flatnonzero(flag)
        if idx.size:
            out[idx] = z[idx] <= self.column_max_within(p[idx, :2], r)
        return out

    def probe(self, xy) -> list[dict]:
        p = _xy(xy)
        dsm = self.eff.nearest(p[:, 0], p[:, 1], "nan")
        raw = self.dsm_raw.nearest(p[:, 0], p[:, 1], "nan")
        dtm = self.dtm.bilinear(p[:, 0], p[:, 1], "nan")
        inb = self.zones.border.contains_xy(p)
        out = []
        for i in range(len(p)):
            zs = [z.zone_id for z in self.zones.nofly + self.zones.restricted if z.contains_xy(p[i:i + 1])[0]]

            def f(v):
                return None if not np.isfinite(v) else float(v)

            out.append({"dsm_z_m": f(dsm[i]), "dsm_raw_z_m": f(raw[i]), "dtm_z_m": f(dtm[i]),
                        "hag_m": f(float(dsm[i]) - float(dtm[i])), "in_border": bool(inb[i]), "zones": zs})
        return out

    # ---------------------------------------------------------------- 线查询
    def _ray_args(self, origin, direction, max_range_m) -> tuple[np.ndarray, np.ndarray, float]:
        o = np.asarray(origin, np.float64).reshape(3)
        d = np.asarray(direction, np.float64).reshape(3)
        L = float(max_range_m)
        if not (np.all(np.isfinite(o)) and np.all(np.isfinite(d))):
            raise GeoError(300, "BAD_VECTOR")
        if not (0.0 < L <= MAX_RAY_M):
            raise GeoError(PARAM_OUT_OF_RANGE, f"max_range_m must be in (0, {MAX_RAY_M}]")
        n = float(np.linalg.norm(d))
        if n == 0:
            raise GeoError(300, "BAD_VECTOR")
        return o, d / n, L

    def ray_hit_iter(self, origin, direction, max_range_m: float = MAX_RAY_M):
        o, d, L = self._ray_args(origin, direction, max_range_m)
        b = o + d * L
        t_lo = 0.0
        h0 = float(self.eff.nearest(o[0], o[1], "nan")[0])
        inside = bool(np.isfinite(h0) and o[2] <= h0)
        if inside:
            te = exit_param(self.eff, o, b)
            if te is None:
                return self._miss(inside)
            t_lo = te
        res = yield from first_hit_iter(self.eff, self.pyr_dsm, o, b, t_lo, 1.0)
        if res is None:
            return self._miss(inside)
        t, kind, (r, c), prev = res
        pnt = o + (b - o) * t
        if kind == "top":
            normal = np.array([0.0, 0.0, 1.0])
        else:
            if prev is not None and prev[1] != c:
                axis = 0
            elif prev is not None and prev[0] != r:
                axis = 1
            else:
                axis = 0 if abs(d[0]) >= abs(d[1]) else 1
            normal = np.zeros(3)
            normal[axis] = -math.copysign(1.0, d[axis]) if d[axis] != 0 else -1.0
        cx = self.eff.x0 + (c + 0.5) * self.eff.cell
        cy = self.eff.y0 + (r + 0.5) * self.eff.cell
        top = float(self.eff.a[r, c])
        surface = "dtm" if top - float(self.dtm.bilinear(cx, cy)[0]) <= 1.0 else "dsm"
        gz = float(self.dtm.bilinear(pnt[0], pnt[1])[0])
        return Hit(True, pnt, float(t * L), kind, normal, surface, (int(r), int(c)), gz, float(pnt[2] - gz), inside)

    @staticmethod
    def _miss(inside: bool) -> Hit:
        return Hit(False, None, float("nan"), None, None, "none", None, float("nan"), float("nan"), inside)

    def ray_hit(self, origin, direction, max_range_m: float = MAX_RAY_M) -> Hit:
        gen = self.ray_hit_iter(origin, direction, max_range_m)
        try:
            while True:
                next(gen)
        except StopIteration as done:
            return done.value

    def segment_los_iter(self, a, b, eps_m: float = 0.5):
        a = np.asarray(a, np.float64).reshape(3)
        b = np.asarray(b, np.float64).reshape(3)
        if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
            raise GeoError(300, "BAD_VECTOR")
        L = float(np.linalg.norm(b - a))
        if 2 * eps_m >= L:
            return True, None
        e = eps_m / L
        res = yield from first_hit_iter(self.eff, self.pyr_dsm, a, b, e, 1 - e)
        if res is None:
            return True, None
        return False, a + (b - a) * res[0]

    def segment_los(self, a, b, eps_m: float = 0.5) -> bool:
        gen = self.segment_los_iter(a, b, eps_m)
        try:
            while True:
                next(gen)
        except StopIteration as done:
            return bool(done.value[0])

    def los_batch(self, A, B, step_m: float = 1.0, eps_m: float = 0.5) -> np.ndarray:
        """向量化采样视线（≤ 64 对）；1 m 步长只保证弦长 ≥ 1 m 的格被采到（已知近似，M04-FR-014）。"""
        A = _xyz(A)
        B = _xyz(B)
        if len(A) != len(B) or len(A) > MAX_LOS_BATCH:
            raise GeoError(PARAM_OUT_OF_RANGE, f"pairs must be <= {MAX_LOS_BATCH}")
        if step_m <= 0:
            raise GeoError(PARAM_OUT_OF_RANGE, "step_m must be > 0")
        L = np.linalg.norm(B - A, axis=1)
        n = np.maximum(2, np.ceil(np.maximum(L - 2 * eps_m, 0) / step_m).astype(np.int64) + 1)
        start = np.zeros(len(A), np.int64)
        np.cumsum(n[:-1], out=start[1:])
        sid = np.repeat(np.arange(len(A)), n)
        j = np.arange(int(n.sum())) - start[sid]
        with np.errstate(divide="ignore", invalid="ignore"):
            e = np.where(L > 0, eps_m / L, 0.0)
        s = e[sid] + (1 - 2 * e[sid]) * j / (n[sid] - 1)
        q = A[sid] + (B[sid] - A[sid]) * s[:, None]
        g = self.eff
        r, c, inside = g.idx(q[:, 0], q[:, 1])
        blocked = inside & (q[:, 2] <= g.a[r, c])
        vis = ~(np.maximum.reduceat(blocked.astype(np.int8), start) > 0)
        return np.where(2 * eps_m < L, vis, True)

    def free_distance(self, origins, dirs, max_m: float) -> np.ndarray:
        Os = _xyz(origins)
        D = _xyz(dirs)
        if len(Os) != len(D) or len(Os) > MAX_FREE_RAYS:
            raise GeoError(PARAM_OUT_OF_RANGE, f"rays must be <= {MAX_FREE_RAYS}")
        if not (0 < max_m <= MAX_RAY_M):
            raise GeoError(PARAM_OUT_OF_RANGE, f"max_m must be in (0, {MAX_RAY_M}]")
        out = np.full(len(Os), float(max_m), np.float64)
        for i in range(len(Os)):
            n = float(np.linalg.norm(D[i]))
            if n == 0:
                continue
            b = Os[i] + D[i] / n * max_m
            res = first_hit(self.eff, self.pyr_dsm, Os[i], b)
            if res is not None:
                out[i] = res[0] * max_m
        return out

    # ---------------------------------------------------------------- 走廊上界
    def heightmap_top_along(self, A, B, *, tol_m: float = 100.0, max_samples: int = 30000, exact: bool = False):
        single = np.asarray(A).ndim == 1
        A2 = _xy(A)
        B2 = _xy(B)
        if exact:
            out = np.array([max_along(self.hm, np.r_[a, 0.0], np.r_[b, 0.0]) for a, b in zip(A2, B2, strict=True)], np.float32)
        else:
            out = corridor_max_sampled(self.pyrdil_hm, self.hm.x0, self.hm.y0, self.hm.cell, A2, B2, tol_m, max_samples)
        return float(out[0]) if single else out

    # ---------------------------------------------------------------- 细校验栅格（非默认 buffer 进程内缓存，最多 4 个）
    def inflated(self, buffer_m: float) -> tuple[Grid, list[np.ndarray]]:
        key = round(float(buffer_m), 3)
        got = self._infl_cache.get(key)
        if got is None:
            a = inflate(np.asarray(self.eff.a), key, self.eff.cell)
            got = (Grid(a, self.eff.x0, self.eff.y0, self.eff.cell), max_pyramid(a, self.params.pyr_min_cells))
            if len(self._infl_cache) >= 5:
                self._infl_cache.pop(next(k for k in self._infl_cache if k != 1.0))
            self._infl_cache[key] = got
        return got

    # ---------------------------------------------------------------- 路径（path.py）
    def path_coarse_check(self, polyline, *, buffer_m: float = 1.0, goal_clear_m: float = 2.0, goal_radius_m: float = 0.0,
                          active_zone_ids: Sequence[str] | None = None) -> CoarseResult:
        return _path.path_coarse_check(self, polyline, buffer_m=buffer_m, goal_clear_m=goal_clear_m,
                                       goal_radius_m=goal_radius_m, active_zone_ids=active_zone_ids)

    def path_valid(self, polyline, *, buffer_m: float = 1.0, steps_per_m: int = 20, active_zone_ids: Sequence[str] | None = None,
                   endpoint_radius_m: float | None = None, endpoint_clear_m: float = 0.5) -> PathValidResult:
        return _path.path_valid(self, polyline, buffer_m=buffer_m, steps_per_m=steps_per_m, active_zone_ids=active_zone_ids,
                                endpoint_radius_m=endpoint_radius_m, endpoint_clear_m=endpoint_clear_m)

    def safe_transit_profile(self, a, b, *, margin_m: float = 5.0, z_ceiling_m: float | None = None) -> TransitProfile:
        return _path.safe_transit_profile(self, a, b, margin_m=margin_m, z_ceiling_m=z_ceiling_m)

    def terrain_profile(self, polyline, ds_m: float = 2.0) -> dict[str, np.ndarray]:
        return _path.terrain_profile(self, polyline, ds_m)

    def grid_2p5d(self, res_m: float = 4.0) -> tuple[np.ndarray, dict]:
        return _path.grid_2p5d(self, res_m)

    # ---------------------------------------------------------------- 探针服务的生成器接口
    def iter_op(self, op: str, args: dict) -> Iterator[None]:
        from .probe import op_generator

        return op_generator(self, op, args)

    def ready_event(self) -> dict:
        """`geo.ready` 事件字段（M04-FR-006）。"""
        mem = sum(int(getattr(a, "nbytes", 0)) for a in (self.eff.a, self.hm.a, self.dil1.a, self.infl1.a))
        mem += sum(int(p.nbytes) for p in self.pyr_dsm[1:] + self.pyr_hm[1:] + [p for p in self.pyrdil_hm if p is not None])
        return {"world_id": self.world_id, "content_version": self.content_version, "derive_sha8": self.derive_sha8,
                "cache": self.cache_state, "load_ms": round(self.load_ms, 1),
                "qa": {"empty_frac": self.qa.get("empty_frac"), "pits_filled_frac": self.qa.get("pits_filled_frac"),
                       "raised_frac": self.qa.get("raised_frac"), "dsm_max_m": self.qa.get("dsm_max_m"),
                       "mem_mib": round(mem / 2**20, 1)}}


# ==================================================================== 装载


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def open_world_query(world_dir: Path, cache_root: Path | None = None, params: GeoParams | None = None,
                     allow_derive: bool = True) -> DsmWorldQuery:
    """读取 world.json 定位图层、校验 sidecar 与坐标绑定、映射或派生缓存（M04-FR-001、FR-005）。

    `cache_root` 缺省为 `<worlds>/.geo-cache`（与世界目录同级）。任一不符抛 GeoLoadError。
    """
    t0 = time.perf_counter()
    params = params or GeoParams()
    world_dir = Path(world_dir)
    try:
        w = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise GeoLoadError("GEO_MISSING_FILE", str(world_dir / "world.json")) from e
    except (OSError, json.JSONDecodeError) as e:
        raise GeoLoadError("GEO_GRID_INVALID", f"world.json: {e}") from e
    layers = {L.get("id"): L for L in w.get("layers", [])}
    for lid in ("terrain.dtm", "terrain.dsm", "semantic.zones"):
        L = layers.get(lid)
        if L is None or L.get("status") != "ready":
            raise GeoLoadError("GEO_MISSING_FILE", f"layer {lid} missing or not ready")
    coord_sha = (w.get("coordinate") or {}).get("sha256")
    cpath = world_dir / (w.get("coordinate") or {}).get("href", "coordinate.json")
    if not cpath.exists():
        raise GeoLoadError("GEO_MISSING_FILE", str(cpath))
    if _sha256(cpath) != coord_sha:
        raise GeoLoadError("GEO_SHA_MISMATCH", "coordinate.json sha256 != world.coordinate.sha256")
    cv = str(w.get("contentVersion"))
    dsm = Grid.open(world_dir / layers["terrain.dsm"]["href"], expect_kind="dsm", expect_cell=2.0)
    dtm = Grid.open(world_dir / layers["terrain.dtm"]["href"], expect_kind="dtm", expect_cell=10.0)
    if abs(dsm.x0 - dtm.x0) > 1e-6 or abs(dsm.y0 - dtm.y0) > 1e-6:
        raise GeoLoadError("GEO_GRID_INVALID", "dsm and dtm originXY differ")
    zones = ZoneIndex.load(world_dir / layers["semantic.zones"]["href"], expect_coord_sha=coord_sha)
    key = derive_key(cv, coord_sha, params)
    root = Path(cache_root) if cache_root is not None else world_dir.parent / ".geo-cache"
    d = entry_dir(root, str(w["id"]), cv, key)
    state = "hit"
    if not verify_entry(d):
        if not allow_derive:
            raise GeoLoadError("GEO_CACHE_MISS", str(d))
        n, src = obs_counts(world_dir, dsm, layers)
        tb = time.perf_counter()
        ds = derive_arrays(dsm, dtm, zones, params, n)
        meta = {"derive_version": DERIVE_VERSION, "params": params.to_json(), "content_version": cv,
                "coordinate_sha256": coord_sha, "world_id": w["id"], "derive_sha8": key, "obs_source": src,
                "dsm": {"x0": dsm.x0, "y0": dsm.y0, "cell": dsm.cell, "shape": list(dsm.a.shape)},
                "built_unix_ns": time.time_ns(), "build_ms": round((time.perf_counter() - tb) * 1000, 1)}
        write_entry(d, ds, meta)
        state = "built"
        zones.raster = None
    m, arrays = load_entry(d)
    b = w.get("bounds") or {"min": [0, 0, 0], "max": [0, 0, 0]}
    return DsmWorldQuery(world_id=str(w["id"]), content_version=cv, coordinate_sha256=coord_sha, derive_sha8=key,
                         bounds_m=np.array([b["min"], b["max"]], np.float64), dsm_raw=dsm, dtm=dtm, arrays=arrays, zones=zones,
                         params=params, qa=m.get("qa", {}), cache_dir=d, cache_state=state,
                         load_ms=(time.perf_counter() - t0) * 1000)
