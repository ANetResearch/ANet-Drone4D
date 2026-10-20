"""事件舞台可走面（M23 §6.3、§6.5、§6.6.2；ADR-196 决策 (4)、(5)）：Detour 后端（NavMesh 图层 + navsvc）与栅格夹具后端。

Detour 后端 `DetourNav`（正式口径）：世界包含 `detour-navmesh@1` 图层时，编译器经 `awr.world.glb.derive.navmesh.NavSvc`（每个舞台一个
navsvc 子进程，行人与车辆共用）做吸附（findNearestPoly）、寻路（findPath + findStraightPath，走廊须完整抵达终点多边形）、视线
（raycast）与导航面高度（getPolyHeight）。navsvc 的坐标为三维整数毫米，平面锚点的竖直初值取求交网格自上而下的首个命中（再上抬
0.5 m）：点在可走多边形正上方时 Detour 取竖直最近者（即自上而下首个可走层），否则取三维最近者，吸附再以该层高度做一次窄竖直窗口
的复查，取水平距离较小者。路径点为 Detour 拉直点，首末点替换为调用方给出的平面点（与栅格后端同一约定），随后按 §6.6.2 做受限圆角，
圆角切点的高度沿路径三维折线插值，视线以一次 `batch` 请求批量判定。磁带头 `navmesh_sha256` 取图层 `bin.sha256`（即 navsvc `hello`
的 profile 哈希），`provenance` 取 navsvc 的 recast-navigation 版本、WASM sha256 与 Node 版本。全部请求按调用次序发出、应答为整数毫米，
同一 NavMesh 与同一事件的编译结果逐字节相同（M23-NFR-004）。

栅格后端 `NavGrid`（`awr-navgrid@1`，只供无 NavMesh 图层的测试夹具，以此编译的磁带不得用于正式运行）：编译器在事件舞台范围内由
raycast 网格自上而下求交得到 1 m 高度场，按 profile 的可跨台阶（行人 0.45 m、车辆 0.30 m）标记边沿、车辆再腐蚀 1 格，取种子锚点所在
的 4 连通分量（无种子时取最大分量）为主岛。路径为 8 连通整数代价 A*（正交 10、对角 14，对角须两侧正交格可走），平局按格号裁决；
随后贪心视线拉直并按 §6.6.2 做受限圆角。全部运算为整数或显式次序的 float64，与输入次序无关；`sha256` 覆盖高度场与可走位。

后端选择（`build_stage_nav`）：`auto`（缺省）在图层存在且 Node 与 navsvc 依赖可用时取 Detour，否则退回栅格并记录日志告警；
`detour` 强制 Detour（排名运行的编译以此调用，不可用即报配置错误）；`grid` 强制栅格。环境变量 `AWR_EVENT_NAV` 给出缺省值。
两种后端提供同一接口：`snap`、`path`、`route`、`los`、`z_at`、`z_at_batch`、`walkable`，以及舞台 1 m 栅格视图
（`x0`、`y0`、`nx`、`ny`、`walk`、`center`、`cell_of`，供背景演员按 zone 抽样）；Detour 后端另提供 `walkable_batch`
（目标搜索抽样）、`offmesh_dist_batch`（门 3 `S-OFFMESH`）与 `nav_z_batch`（`MeshWorldQuery.attach_nav` 的导航面高度）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import weakref
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

try:
    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f

        return deco(a[0]) if a and callable(a[0]) else deco

__all__ = ["BACKENDS", "CELL_M", "PROFILES", "DetourNav", "NavGrid", "Snap", "build_nav", "build_stage_nav", "navmesh_profiles",
           "rounded_length"]

CELL_M = 1.0
# 可跨台阶（m，每 1 m 格）、额外腐蚀格数
PROFILES = {"person": {"climb_m": 0.45, "erode": 0}, "vehicle": {"climb_m": 0.30, "erode": 1}}
SNAP_OK_M, SNAP_WARN_M = 3.0, 8.0
GRID_FORMAT, DETOUR_FORMAT = "awr-navgrid@1", "detour-navmesh@1"
BACKENDS = ("auto", "detour", "grid")
GRID_PROVENANCE = {"recast_navigation": f"none ({GRID_FORMAT})", "wasm_sha256": "0" * 64, "node": "none"}
# Detour 后端（ADR-196）：竖直初值上抬、竖直搜索半宽、吸附复查竖直半宽、可走与高度查询的水平半宽、离面距离的水平搜索半宽（m）
PROBE_ABOVE_M = 0.5
PROBE_EXT_V_M = 60.0
LEVEL_EXT_V_M = 2.0
WALK_EXT_H_M = 0.5
HEIGHT_EXT_H_M = 2.0
OFFMESH_EXT_H_M = 2.0
BATCH_ITEMS = 20000                      # 单次 navsvc batch 请求的条数上限（行长约 2 MB）
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snap:
    anchor: str
    p: tuple[float, float, float]        # 吸附点（world 帧 m），z 为高度场高度
    cell: int
    dist_m: float
    status: str                          # ok、warn、error
    why: str | None = None               # island、far、none


@dataclass
class NavGrid:
    """栅格夹具后端（`awr-navgrid@1`）：1 m 高度场与主岛可走位。"""

    profile: str
    x0: float
    y0: float
    nx: int
    ny: int
    z: np.ndarray                        # (ny, nx) float64，NaN 为无表面
    walk: np.ndarray                     # (ny, nx) bool，已限于主岛
    sha256: str = ""
    _w8: np.ndarray = field(default=None, repr=False)          # type: ignore[assignment]

    kind = "grid"
    nav_format = GRID_FORMAT

    def __post_init__(self) -> None:
        self._w8 = np.ascontiguousarray(self.walk.ravel().astype(np.uint8))

    def provenance(self) -> dict[str, str]:
        """磁带清单 `provenance` 的寻路字段（栅格后端无 recast-navigation）。"""
        return dict(GRID_PROVENANCE)

    # ------------------------------------------------------------------ 坐标
    def cell_of(self, x: float, y: float) -> int:
        ix, iy = math.floor((x - self.x0) / CELL_M), math.floor((y - self.y0) / CELL_M)
        if not (0 <= ix < self.nx and 0 <= iy < self.ny):
            return -1
        return iy * self.nx + ix

    def center(self, c: int) -> tuple[float, float]:
        iy, ix = divmod(int(c), self.nx)
        return self.x0 + (ix + 0.5) * CELL_M, self.y0 + (iy + 0.5) * CELL_M

    def z_at(self, x: float, y: float) -> float:
        c = self.cell_of(x, y)
        return float("nan") if c < 0 else float(self.z.ravel()[c])

    def z_at_batch(self, xy: np.ndarray) -> np.ndarray:
        ix = np.floor((xy[:, 0] - self.x0) / CELL_M).astype(np.int64)
        iy = np.floor((xy[:, 1] - self.y0) / CELL_M).astype(np.int64)
        ok = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        out = np.full(len(xy), np.nan)
        out[ok] = self.z[iy[ok], ix[ok]]
        return out

    def walkable(self, x: float, y: float) -> bool:
        c = self.cell_of(x, y)
        return c >= 0 and bool(self._w8[c])

    # ------------------------------------------------------------------ 查询
    def snap(self, anchor: str, x: float, y: float, max_m: float = SNAP_WARN_M) -> Snap:
        """最近的主岛格；锚点所在格可走时原样返回（距离 0）。距离 ≤ 3 m 通过、≤ 8 m 告警、更远或不在主岛为错误。"""
        c = self.cell_of(x, y)
        if c >= 0 and self._w8[c]:
            return Snap(anchor, (x, y, float(self.z.ravel()[c])), c, 0.0, "ok")
        r = math.ceil(max_m * 2 / CELL_M) + 1
        ix0, iy0 = math.floor((x - self.x0) / CELL_M), math.floor((y - self.y0) / CELL_M)
        best = None
        for iy in range(max(0, iy0 - r), min(self.ny, iy0 + r + 1)):
            for ix in range(max(0, ix0 - r), min(self.nx, ix0 + r + 1)):
                k = iy * self.nx + ix
                if not self._w8[k]:
                    continue
                cx, cy = self.x0 + (ix + 0.5) * CELL_M, self.y0 + (iy + 0.5) * CELL_M
                d = math.hypot(cx - x, cy - y)
                if best is None or (d, k) < best[:2]:
                    best = (d, k, cx, cy)
        if best is None:
            return Snap(anchor, (x, y, self.z_at(x, y)), -1, float("inf"), "error", "none")
        d, k, cx, cy = best
        st = "ok" if d <= SNAP_OK_M else "warn" if d <= max_m else "error"
        return Snap(anchor, (cx, cy, float(self.z.ravel()[k])), k, d, st, None if st != "error" else "far")

    def los(self, a, b) -> bool:
        """线段 a→b（平面）经过的全部格可走（超覆盖 DDA）。"""
        return bool(_los(self._w8, self.nx, self.ny, self.x0, self.y0, float(a[0]), float(a[1]), float(b[0]), float(b[1])))

    def path(self, a, b) -> np.ndarray | None:
        """a、b 为可走点（平面 m）；返回拉直并圆角后的折线 (K, 2) float64，首末点为 a、b；不连通返回 None。"""
        ca, cb = self.cell_of(a[0], a[1]), self.cell_of(b[0], b[1])
        if ca < 0 or cb < 0 or not self._w8[ca] or not self._w8[cb]:
            return None
        if ca == cb or self.los(a, b):
            return np.array([a[:2], b[:2]], np.float64)
        cells = _astar(self._w8, self.nx, self.ny, ca, cb)
        if len(cells) == 0:
            return None
        iy, ix = np.divmod(cells, self.nx)
        pts = np.stack([self.x0 + (ix + 0.5) * CELL_M, self.y0 + (iy + 0.5) * CELL_M], 1)
        pts[0], pts[-1] = a[:2], b[:2]
        return self.round_corners(self._pull(pts))

    def route(self, pts) -> np.ndarray | None:
        """依次经过 pts（含 via）的折线；任一段不连通返回 None。"""
        out = []
        for p, q in itertools.pairwise(pts):
            seg = self.path(p, q)
            if seg is None:
                return None
            out.append(seg if not out else seg[1:])
        return np.concatenate(out) if out else np.array([pts[0][:2]], np.float64)

    def _pull(self, pts: np.ndarray) -> np.ndarray:
        keep = [0]
        i, n = 0, len(pts)
        while i < n - 1:
            j = i + 1
            while j + 1 < n and self.los(pts[i], pts[j + 1]):
                j += 1
            keep.append(j)
            i = j
        return pts[keep]

    def round_corners(self, P: np.ndarray) -> np.ndarray:
        """§6.6.2：内部拐点以切距 c = min(0.5, |u|/4, |w|/4) 的二次 Bézier 4 等分点替换（a→b 可见时）。"""
        if len(P) < 3:
            return P
        out = [P[0]]
        for j in range(1, len(P) - 1):
            p, u, w = P[j], P[j] - P[j - 1], P[j + 1] - P[j]
            lu, lw = math.hypot(u[0], u[1]), math.hypot(w[0], w[1])
            if lu == 0 or lw == 0:
                continue
            c = min(0.5, lu / 4, lw / 4)
            a, b = p - c * u / lu, p + c * w / lw
            if self.los(a, b):
                out.extend((1 - s) * (1 - s) * a + 2 * (1 - s) * s * p + s * s * b for s in (0.0, 0.25, 0.5, 0.75, 1.0))
            else:
                out.append(p)
        out.append(P[-1])
        return np.asarray(out, np.float64)


def rounded_length(P: np.ndarray) -> float:
    d = np.diff(np.asarray(P, np.float64), axis=0)
    return float(np.sum(np.hypot(d[:, 0], d[:, 1])))


# ---------------------------------------------------------------------- 构建
def surface_grid(q, x0: float, y0: float, nx: int, ny: int) -> np.ndarray:
    """格中心自上而下求交：返回 (ny, nx) 最高表面高度（float64，未命中 NaN）。"""
    ix, iy = np.meshgrid(np.arange(nx), np.arange(ny))
    xs = x0 + (ix.ravel() + 0.5) * CELL_M
    ys = y0 + (iy.ravel() + 0.5) * CELL_M
    ztop = float(getattr(q, "_zhi", 1000.0)) + 10.0
    z = np.full(nx * ny, np.nan)
    step = 1 << 20
    for s in range(0, nx * ny, step):
        o = np.stack([xs[s:s + step], ys[s:s + step], np.full(min(step, nx * ny - s), ztop)], 1)
        d = np.tile([0.0, 0.0, -1.0], (len(o), 1))
        t, _p, _i = q.raycast_batch(o, d, ztop + 2000.0)
        hit = np.isfinite(t)
        z[s:s + step][hit] = ztop - t[hit].astype(np.float64)
    return z.reshape(ny, nx)


def _walk_mask(z: np.ndarray, climb: float, erode: int) -> np.ndarray:
    ok = np.isfinite(z)
    zf = np.where(ok, z, 0.0)
    w = ok.copy()
    for ax, sh in ((0, 1), (0, -1), (1, 1), (1, -1)):
        zn, okn = np.roll(zf, sh, ax), np.roll(ok, sh, ax)
        edge = ~okn | (np.abs(zn - zf) > climb)
        if ax == 0:
            edge[0 if sh == 1 else -1, :] = True
        else:
            edge[:, 0 if sh == 1 else -1] = True
        w &= ~edge
    for _ in range(erode):
        e = w.copy()
        for ax, sh in ((0, 1), (0, -1), (1, 1), (1, -1)):
            e &= np.roll(w, sh, ax)
        e[0, :] = e[-1, :] = e[:, 0] = e[:, -1] = False
        w = e
    return w


def build_nav(q, bbox: tuple[float, float, float, float], profile: str, seeds: list[tuple[float, float]] = (), *,
              z: np.ndarray | None = None) -> NavGrid:
    """bbox = (xmin, ymin, xmax, ymax) world 帧 m（向外取整到格）；seeds 为种子锚点平面坐标。`z` 可复用另一 profile 的高度场。"""
    from scipy import ndimage

    x0, y0 = math.floor(bbox[0] / CELL_M) * CELL_M, math.floor(bbox[1] / CELL_M) * CELL_M
    nx, ny = math.ceil((bbox[2] - x0) / CELL_M), math.ceil((bbox[3] - y0) / CELL_M)
    if z is None:
        z = surface_grid(q, x0, y0, nx, ny)
    pr = PROFILES[profile]
    w = _walk_mask(z, pr["climb_m"], pr["erode"])
    lab, n = ndimage.label(w, structure=[[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    keep: set[int] = set()
    for sx, sy in seeds:
        ix, iy = math.floor((sx - x0) / CELL_M), math.floor((sy - y0) / CELL_M)
        r = 3
        win = lab[max(0, iy - r):iy + r + 1, max(0, ix - r):ix + r + 1]
        keep |= set(int(v) for v in np.unique(win) if v)
    if not keep and n:
        cnt = np.bincount(lab.ravel())
        cnt[0] = 0
        keep = {int(np.argmax(cnt))}
    main = np.isin(lab, sorted(keep))
    h = hashlib.sha256()
    h.update(f"awr-navgrid@1|{profile}|{CELL_M}|{x0}|{y0}|{nx}|{ny}|{pr['climb_m']}|{pr['erode']}".encode())
    h.update(np.packbits(main.ravel()).tobytes())
    h.update(np.where(np.isfinite(z), np.floor(z * 1000.0 + 0.5), -(2 ** 31)).astype("<i4").tobytes())
    return NavGrid(profile, float(x0), float(y0), int(nx), int(ny), z, main, h.hexdigest())


# ---------------------------------------------------------------------- Detour 后端（ADR-196）
def round_corners_with(P: np.ndarray, los_many: Callable[[list[tuple[np.ndarray, np.ndarray]]], list[bool]]) -> np.ndarray:
    """§6.6.2 受限圆角（两种后端同一算法）：P 为 (K, 2) 或 (K, 3) 折线（第 3 列为高度，只用于视线查询的竖直初值）；内部拐点以切距
    c = min(0.5, |u|/4, |w|/4) 的二次 Bézier 4 等分点替换（a→b 可见时），否则保留拐点。返回 (M, 2) 平面折线。"""
    P = np.asarray(P, np.float64)
    if len(P) < 3:
        return np.ascontiguousarray(P[:, :2])
    cand: list[tuple[int, np.ndarray, np.ndarray, np.ndarray]] = []
    for j in range(1, len(P) - 1):
        p, u, w = P[j], P[j] - P[j - 1], P[j + 1] - P[j]
        lu, lw = math.hypot(u[0], u[1]), math.hypot(w[0], w[1])
        if lu == 0 or lw == 0:
            continue
        c = min(0.5, lu / 4, lw / 4)
        cand.append((j, p - c * u / lu, p + c * w / lw, p))
    vis = los_many([(a, b) for _j, a, b, _p in cand]) if cand else []
    by_j = {j: (a, b, p, ok) for (j, a, b, p), ok in zip(cand, vis, strict=True)}
    out = [P[0, :2]]
    for j in range(1, len(P) - 1):
        if j not in by_j:
            continue
        a, b, p, ok = by_j[j]
        if ok:
            a2, b2, p2 = a[:2], b[:2], p[:2]
            out.extend((1 - s) * (1 - s) * a2 + 2 * (1 - s) * s * p2 + s * s * b2 for s in (0.0, 0.25, 0.5, 0.75, 1.0))
        else:
            out.append(p[:2])
    out.append(P[-1, :2])
    return np.asarray(out, np.float64)


def _mm(v: float) -> int:
    return math.floor(float(v) * 1000.0 + 0.5)


def _ext_mm(h_m: float, v_m: float) -> list[int]:
    """Recast 轴向半宽 (x, y 竖直, z) 的整数毫米。"""
    return [_mm(h_m), _mm(v_m), _mm(h_m)]


class _Service:
    """舞台共用的 navsvc 进程（行人与车辆的 DetourNav 共享）；全部持有者释放或解释器退出时发送 quit。"""

    def __init__(self, svc: Any) -> None:
        self.svc = svc
        self.info = dict(svc.info)
        self._fin = weakref.finalize(self, svc.close)

    def call(self, op: str, **fields: Any) -> dict:
        return self.svc.call(op, **fields)

    def batch(self, items: list[dict]) -> list[dict]:
        """逐项应答（与单条请求相同）；超过 `BATCH_ITEMS` 时按原次序分段发送。"""
        out: list[dict] = []
        for s in range(0, len(items), BATCH_ITEMS):
            out += self.svc.call("batch", items=items[s:s + BATCH_ITEMS])["results"]
        return out

    def close(self) -> None:
        self._fin()


class DetourNav:
    """Detour 后端（`detour-navmesh@1`，ADR-196 决策 (4)）：与 `NavGrid` 同一接口，查询经 navsvc。

    `surface(xy) -> z` 给出平面点的竖直初值（求交网格自上而下的首个命中，无命中为 NaN）；`bbox` 为舞台范围，只用于 1 m 栅格视图
    （`walk`、`z`，首次访问时以 navsvc 批量计算）。`sha256` 为图层 `bin.sha256`。
    """

    kind = "detour"
    nav_format = DETOUR_FORMAT

    def __init__(self, service: _Service, profile: str, bbox: tuple[float, float, float, float],
                 surface: Callable[[np.ndarray], np.ndarray]) -> None:
        profiles = service.info.get("profiles") or {}
        if profile not in profiles:
            raise KeyError(f"navsvc has no {profile!r} profile (loaded: {sorted(profiles)})")
        self.svc, self.profile, self.sha256 = service, profile, str(profiles[profile])
        self._surface = surface
        self.x0, self.y0 = math.floor(bbox[0] / CELL_M) * CELL_M, math.floor(bbox[1] / CELL_M) * CELL_M
        self.nx, self.ny = math.ceil((bbox[2] - self.x0) / CELL_M), math.ceil((bbox[3] - self.y0) / CELL_M)
        self._walk: np.ndarray | None = None
        self._z: np.ndarray | None = None
        self._zref: float | None = None

    def provenance(self) -> dict[str, str]:
        """磁带清单 `provenance` 的寻路字段（M23 §6.7.6）：recast-navigation 版本、WASM sha256、Node 版本。"""
        i = self.svc.info
        return {"recast_navigation": str(i["recast_version"]), "wasm_sha256": str(i["wasm_sha256"]), "node": str(i["node"])}

    # ------------------------------------------------------------------ 竖直初值
    def _ref_z(self) -> float:
        """舞台参考高度：16 × 16 网格上网格命中的中位数（点无网格命中时作竖直初值）。"""
        if self._zref is None:
            gx = self.x0 + (np.arange(16) + 0.5) * self.nx * CELL_M / 16
            gy = self.y0 + (np.arange(16) + 0.5) * self.ny * CELL_M / 16
            X, Y = np.meshgrid(gx, gy)
            z = np.asarray(self._surface(np.stack([X.ravel(), Y.ravel()], 1)), np.float64)
            self._zref = float(np.median(z[np.isfinite(z)])) if np.isfinite(z).any() else 0.0
        return self._zref

    def _probe(self, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(竖直初值 float64，网格命中 float64（无命中 NaN）)。"""
        top = np.asarray(self._surface(np.asarray(xy, np.float64).reshape(-1, 2)), np.float64).reshape(-1)
        return np.where(np.isfinite(top), top, self._ref_z()) + PROBE_ABOVE_M, top

    def _xyz_mm(self, xy: np.ndarray, z: np.ndarray) -> list[list[int]]:
        """(N, 2) 平面点与 (N,) 高度 → 整数毫米 [E, N, U]（floor(v·1000 + 0.5)，与 `_mm` 逐位相同）。"""
        P = np.c_[np.asarray(xy, np.float64).reshape(-1, 2), np.asarray(z, np.float64).reshape(-1)]
        return np.floor(P * 1000.0 + 0.5).astype(np.int64).tolist()

    def _pt3(self, p) -> np.ndarray:
        """调用方的点（平面或带高度）→ (3,) float64；无有限高度时取竖直初值。"""
        p = np.asarray(p, np.float64).reshape(-1)
        if len(p) >= 3 and math.isfinite(p[2]):
            return p[:3].copy()
        z, _top = self._probe(p[None, :2])
        return np.array([p[0], p[1], z[0]])

    def _snaps(self, xy: np.ndarray, ext_h_m: float) -> list[dict]:
        z, _top = self._probe(xy)
        ext = _ext_mm(ext_h_m, PROBE_EXT_V_M)
        return self.svc.batch([{"op": "snap", "profile": self.profile, "p": p, "ext": ext} for p in self._xyz_mm(xy, z)])

    # ------------------------------------------------------------------ 查询
    def snap(self, anchor: str, x: float, y: float, max_m: float = SNAP_WARN_M) -> Snap:
        """水平距离最近的可走点：竖直宽窗口吸附；不在多边形正上方时以命中层高度做窄窗口复查，取水平距离较小者（相等取前者）。
        距离 ≤ 3 m 通过、≤ `max_m` 告警、更远为错误 `far`；搜索框内无可走面为错误 `none`（裁剪掉的岛不参与，故不另报 `island`）。"""
        (r,) = self._snaps(np.array([[x, y]]), max_m)
        if not r.get("found"):
            z, _top = self._probe(np.array([[x, y]]))
            return Snap(anchor, (x, y, float(z[0] - PROBE_ABOVE_M)), -1, float("inf"), "error", "none")
        if not r.get("over"):
            r2 = self.svc.call("snap", profile=self.profile, p=[_mm(x), _mm(y), int(r["p"][2])], ext=_ext_mm(max_m, LEVEL_EXT_V_M))
            if r2.get("found") and r2["dist_mm"] < r["dist_mm"]:
                r = r2
        d = r["dist_mm"] / 1000.0
        q = r["p"]
        st = "ok" if d <= SNAP_OK_M else "warn" if d <= max_m else "error"
        return Snap(anchor, (q[0] / 1000.0, q[1] / 1000.0, q[2] / 1000.0), int(r["ref"]), d, st, None if st != "error" else "far")

    def los_many(self, pairs: Sequence[tuple[Any, Any]]) -> list[bool]:
        """批量视线：每对 (a, b) 自 a 的吸附点沿可走面 raycast 到 b 无边界阻挡。"""
        if not pairs:
            return []
        A = np.array([self._pt3(a) for a, _b in pairs])
        B = np.array([self._pt3(b) for _a, b in pairs])
        items = [{"op": "los", "profile": self.profile, "a": [_mm(v) for v in a], "b": [_mm(v) for v in b]} for a, b in zip(A, B, strict=True)]
        return [bool(r.get("ok") and r.get("found") and r.get("clear")) for r in self.svc.batch(items)]

    def los(self, a, b) -> bool:
        return self.los_many([(a, b)])[0]

    def path_raw(self, a, b) -> dict:
        """navsvc `path` 原始应答（世界帧整数毫米）；a、b 为平面或带高度的点。"""
        pa, pb = self._pt3(a), self._pt3(b)
        return self.svc.call("path", profile=self.profile, a=[_mm(v) for v in pa], b=[_mm(v) for v in pb])

    def path(self, a, b) -> np.ndarray | None:
        """Detour 寻路：走廊须完整抵达终点多边形，否则返回 None；拉直点首末替换为 a、b（平面），再做受限圆角，返回 (K, 2) float64。"""
        r = self.path_raw(a, b)
        if not r.get("found") or not r.get("complete"):
            return None
        P = np.asarray(r["points"], np.float64).reshape(-1, 3) / 1000.0
        a2, b2 = np.asarray(a, np.float64)[:2], np.asarray(b, np.float64)[:2]
        if len(P) < 2:
            return np.array([a2, b2], np.float64)
        P[0, :2], P[-1, :2] = a2, b2
        return round_corners_with(P, self.los_many)

    def route(self, pts) -> np.ndarray | None:
        """依次经过 pts（含 via）的折线；任一段不连通返回 None。"""
        out = []
        for p, q in itertools.pairwise(pts):
            seg = self.path(p, q)
            if seg is None:
                return None
            out.append(seg if not out else seg[1:])
        return np.concatenate(out) if out else np.array([np.asarray(pts[0], np.float64)[:2]], np.float64)

    def round_corners(self, P: np.ndarray) -> np.ndarray:
        return round_corners_with(P, self.los_many)

    def _heights(self, xy: np.ndarray, ext_h_m: float) -> list[dict]:
        z, _top = self._probe(xy)
        ext = _ext_mm(ext_h_m, PROBE_EXT_V_M)
        return self.svc.batch([{"op": "height", "profile": self.profile, "p": p, "ext": ext} for p in self._xyz_mm(xy, z)])

    def nav_z_batch(self, xy: np.ndarray) -> np.ndarray:
        """(N,) 导航面高度（getPolyHeight；水平 2 m 内无可走面为 NaN），重复点只查询一次。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if len(xy) == 0:
            return np.zeros(0)
        u, inv = np.unique(xy, axis=0, return_inverse=True)
        res = self._heights(u, HEIGHT_EXT_H_M)
        hz = np.array([r["z_mm"] / 1000.0 if r.get("ok") and r.get("found") else np.nan for r in res], np.float64)
        return hz[np.asarray(inv).reshape(-1)]

    def z_at_batch(self, xy: np.ndarray) -> np.ndarray:
        """贴地初值 `z_hint`（只用于选取贴地候选层，见 `ground.ground_z_batch` 的窗口 [z − 2, z + 1] m）：舞台内取所在 1 m 格中心的
        导航面高度（栅格视图 `z`，一次批量求得后复用）；格中心不在可走面上或在舞台外时取该点的导航面高度，再无则取网格自上而下
        首个命中（与栅格后端的高度场同义），皆无为 NaN。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        out = np.full(len(xy), np.nan)
        if len(xy) == 0:
            return out
        ix = np.floor((xy[:, 0] - self.x0) / CELL_M).astype(np.int64)
        iy = np.floor((xy[:, 1] - self.y0) / CELL_M).astype(np.int64)
        ok = (ix >= 0) & (ix < self.nx) & (iy >= 0) & (iy < self.ny)
        out[ok] = self.z[iy[ok], ix[ok]]
        bad = ~np.isfinite(out)
        if bad.any():
            out[bad] = self.nav_z_batch(xy[bad])
            bad = ~np.isfinite(out)
            if bad.any():
                out[bad] = np.asarray(self._surface(xy[bad]), np.float64).reshape(-1)
        return out

    def z_at(self, x: float, y: float) -> float:
        return float(self.z_at_batch(np.array([[x, y]]))[0])

    def walkable_batch(self, xy: np.ndarray) -> np.ndarray:
        """(N,) bool：点在保留的可走多边形正上方（自上而下首个可走层）。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        if len(xy) == 0:
            return np.zeros(0, bool)
        return np.array([bool(r.get("ok") and r.get("found") and r.get("over")) for r in self._snaps(xy, WALK_EXT_H_M)], bool)

    def walkable(self, x: float, y: float) -> bool:
        return bool(self.walkable_batch(np.array([[x, y]]))[0])

    def offmesh_dist_batch(self, xy: np.ndarray) -> np.ndarray:
        """(N,) 到可走面的水平距离（m）：多边形正上方为 0，水平 2 m 内无可走面为 inf（门 3 `S-OFFMESH`）。"""
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        out = np.full(len(xy), np.inf)
        for i, r in enumerate(self._snaps(xy, OFFMESH_EXT_H_M) if len(xy) else []):
            if r.get("ok") and r.get("found"):
                out[i] = 0.0 if r.get("over") else r["dist_mm"] / 1000.0
        return out

    # ------------------------------------------------------------------ 舞台 1 m 栅格视图（背景演员 zone 抽样）
    def cell_of(self, x: float, y: float) -> int:
        ix, iy = math.floor((x - self.x0) / CELL_M), math.floor((y - self.y0) / CELL_M)
        if not (0 <= ix < self.nx and 0 <= iy < self.ny):
            return -1
        return iy * self.nx + ix

    def center(self, c: int) -> tuple[float, float]:
        iy, ix = divmod(int(c), self.nx)
        return self.x0 + (ix + 0.5) * CELL_M, self.y0 + (iy + 0.5) * CELL_M

    def _centers(self) -> np.ndarray:
        ix, iy = np.meshgrid(np.arange(self.nx), np.arange(self.ny))
        return np.stack([self.x0 + (ix.ravel() + 0.5) * CELL_M, self.y0 + (iy.ravel() + 0.5) * CELL_M], 1)

    def _grid(self) -> None:
        """一次批量 `height`（水平半宽 0.5 m）求格中心的可走位（在可走多边形正上方）与导航面高度。"""
        res = self._heights(self._centers(), WALK_EXT_H_M)
        over = np.array([bool(r.get("ok") and r.get("found") and r.get("over")) for r in res], bool)
        z = np.array([r["z_mm"] / 1000.0 if o else np.nan for r, o in zip(res, over, strict=True)], np.float64)
        self._walk, self._z = over.reshape(self.ny, self.nx), z.reshape(self.ny, self.nx)

    @property
    def walk(self) -> np.ndarray:
        """(ny, nx) bool：格中心在可走多边形正上方（首次访问时计算）。"""
        if self._walk is None:
            self._grid()
        return self._walk

    @property
    def z(self) -> np.ndarray:
        """(ny, nx) float64：格中心的导航面高度（格中心不在可走面上为 NaN）。"""
        if self._z is None:
            self._grid()
        return self._z


def navmesh_profiles(world_dir: Path | str) -> dict[str, dict]:
    """世界包的 NavMesh 图层（world.json 中 type = navmesh、format = detour-navmesh@1）：profile → 图层清单（读失败为空）。"""
    w = Path(world_dir)
    try:
        doc = json.loads((w / "world.json").read_text(encoding="utf-8"))
        out = {}
        for L in doc.get("layers", []):
            if L.get("type") == "navmesh" and L.get("format", DETOUR_FORMAT) == DETOUR_FORMAT:
                nd = json.loads((w / L["href"]).read_text(encoding="utf-8"))
                out[str(nd["profile"])] = nd
        return out
    except (OSError, ValueError, KeyError):
        return {}


def mesh_surface(q) -> Callable[[np.ndarray], np.ndarray]:
    """平面点 → 求交网格自上而下首个命中的高度（无命中 NaN）：`MeshWorldQuery.walkable_surface`（float64 显式次序内核）。

    `q` 由此被 DetourNav 强引用；反向的 `attach_nav` 登记只持弱引用（`_weak_nav_z`），不形成循环，舞台释放时 navsvc 随即退出。"""
    def surface(xy: np.ndarray) -> np.ndarray:
        P = np.asarray(xy, np.float64).reshape(-1, 2)
        out = np.empty(len(P))
        step = 1 << 20
        for s in range(0, len(P), step):
            out[s:s + step] = q.walkable_surface(P[s:s + step], np.inf)[0]
        return out

    return surface


def _weak_nav_z(nav: DetourNav) -> Callable[[np.ndarray], np.ndarray]:
    """`MeshWorldQuery.attach_nav` 的登记函数：弱引用 DetourNav；舞台已释放时视为无导航面（NaN）。"""
    ref = weakref.WeakMethod(nav.nav_z_batch)

    def nav_z(xy: np.ndarray) -> np.ndarray:
        fn = ref()
        return fn(xy) if fn is not None else np.full(len(np.asarray(xy).reshape(-1, 2)), np.nan)

    return nav_z


def build_stage_nav(q, bbox: tuple[float, float, float, float], seeds: Sequence[tuple[float, float]] = (), *,
                    world_dir: Path | str | None = None, backend: str | None = None, node: str | None = None,
                    profiles: Sequence[str] = ("person", "vehicle")) -> dict[str, Any]:
    """事件舞台的可走面对象 {profile: DetourNav | NavGrid}（ADR-196 决策 (4)、(5)）。

    `backend` 缺省取环境变量 `AWR_EVENT_NAV`（再缺省 `auto`）：`auto` 在 `world_dir` 含该 profile 的 NavMesh 图层且 navsvc 可启动时取
    Detour，否则退回栅格并记日志告警；`detour` 不可用即抛出 `ConfigError`（排名运行的编译）；`grid` 一律取栅格（测试夹具）。
    Detour 后端以 `q.attach_nav` 登记导航面高度（弱引用 `nav.nav_z_batch`）（`walkable_height` 由此取 Detour 高度）。
    """
    from awr.world.ingest.types import ConfigError

    mode = (backend or os.environ.get("AWR_EVENT_NAV") or "auto").strip().lower()
    if mode not in BACKENDS:
        raise ConfigError(f"AWR_EVENT_NAV / backend must be one of {BACKENDS}, got {mode!r}")
    layers = navmesh_profiles(world_dir) if world_dir is not None and mode != "grid" else {}
    out: dict[str, Any] = {}
    want = [p for p in profiles if p in layers]
    if mode == "detour" and len(want) != len(profiles):
        raise ConfigError(f"world has no detour-navmesh@1 layer for {sorted(set(profiles) - set(want))} (ADR-196: formal events need Detour)")
    if want:
        from awr.world.glb.derive.navmesh import NavSvc, NavSvcError

        try:
            service = _Service(NavSvc(world_dir, node=node))
        except (ConfigError, NavSvcError, OSError) as e:
            if mode == "detour":
                raise ConfigError(f"navsvc unavailable: {e}") from e
            log.warning("navsvc unavailable (%s); event stage falls back to %s (fixture only, not for formal runs)", e, GRID_FORMAT)
            want = []
        else:
            surf = mesh_surface(q)
            for p in want:
                out[p] = DetourNav(service, p, bbox, surf)
                if hasattr(q, "attach_nav"):
                    q.attach_nav(p, _weak_nav_z(out[p]))
    z = None
    for p in profiles:
        if p not in out:
            if mode == "auto" and world_dir is not None and layers:
                log.warning("profile %s uses %s (no usable NavMesh layer)", p, GRID_FORMAT)
            g = build_nav(q, bbox, p, list(seeds), z=z)
            z = g.z
            out[p] = g
    return out


# ---------------------------------------------------------------------- 内核
@njit(cache=True)
def _los(w, nx, ny, x0, y0, ax, ay, bx, by):  # pragma: no cover - numba
    fx, fy = (ax - x0) / 1.0, (ay - y0) / 1.0
    tx, ty = (bx - x0) / 1.0, (by - y0) / 1.0
    ix, iy = math.floor(fx), math.floor(fy)
    jx, jy = math.floor(tx), math.floor(ty)
    dx, dy = tx - fx, ty - fy
    sx = 1 if dx > 0 else -1
    sy = 1 if dy > 0 else -1
    inf = 1e300
    tdx = abs(1.0 / dx) if dx != 0 else inf
    tdy = abs(1.0 / dy) if dy != 0 else inf
    tmx = ((ix + 1 - fx) if dx > 0 else (fx - ix)) * tdx if dx != 0 else inf
    tmy = ((iy + 1 - fy) if dy > 0 else (fy - iy)) * tdy if dy != 0 else inf
    n = abs(jx - ix) + abs(jy - iy) + 2
    for _ in range(n):
        if ix < 0 or iy < 0 or ix >= nx or iy >= ny or w[iy * nx + ix] == 0:
            return False
        if ix == jx and iy == jy:
            return True
        if abs(tmx - tmy) < 1e-12:                         # 恰过格角：两侧格都须可走（保守）
            if ix + sx < 0 or ix + sx >= nx or iy + sy < 0 or iy + sy >= ny:
                return False
            if w[iy * nx + ix + sx] == 0 or w[(iy + sy) * nx + ix] == 0:
                return False
            ix += sx
            iy += sy
            tmx += tdx
            tmy += tdy
        elif tmx < tmy:
            ix += sx
            tmx += tdx
        else:
            iy += sy
            tmy += tdy
    return True


@njit(cache=True)
def _astar(w, nx, ny, start, goal):  # pragma: no cover - numba
    n = nx * ny
    big = np.int64(1) << 60
    g = np.full(n, big, np.int64)
    par = np.full(n, -1, np.int64)
    closed = np.zeros(n, np.uint8)
    heap = np.empty(16, np.int64)
    hn = 0
    gx, gy = goal % nx, goal // nx
    g[start] = 0
    sxx, syy = start % nx, start // nx
    ddx, ddy = abs(sxx - gx), abs(syy - gy)
    f0 = 10 * max(ddx, ddy) + 4 * min(ddx, ddy)
    heap[0] = (f0 << 32) | start
    hn = 1
    DX = (1, -1, 0, 0, 1, 1, -1, -1)
    DY = (0, 0, 1, -1, 1, -1, 1, -1)
    while hn > 0:
        top = heap[0]
        hn -= 1
        last = heap[hn]
        i = 0
        while True:                                        # 下沉
            lc = 2 * i + 1
            if lc >= hn:
                break
            rc = lc + 1
            m = rc if rc < hn and heap[rc] < heap[lc] else lc
            if heap[m] < last:
                heap[i] = heap[m]
                i = m
            else:
                break
        if hn > 0:
            heap[i] = last
        c = top & 0xFFFFFFFF
        if closed[c]:
            continue
        closed[c] = 1
        if c == goal:
            break
        cx, cy = c % nx, c // nx
        for k in range(8):
            ex, ey = cx + DX[k], cy + DY[k]
            if ex < 0 or ey < 0 or ex >= nx or ey >= ny:
                continue
            e = ey * nx + ex
            if w[e] == 0 or closed[e]:
                continue
            if k >= 4 and (w[cy * nx + ex] == 0 or w[ey * nx + cx] == 0):
                continue
            ng = g[c] + (10 if k < 4 else 14)
            if ng < g[e]:
                g[e] = ng
                par[e] = c
                hx, hy = abs(ex - gx), abs(ey - gy)
                f = ng + 10 * max(hx, hy) + 4 * min(hx, hy)
                if hn == len(heap):
                    nh = np.empty(2 * len(heap), np.int64)
                    nh[:hn] = heap[:hn]
                    heap = nh
                j = hn                                     # 上浮
                key = (f << 32) | e
                while j > 0:
                    p = (j - 1) // 2
                    if heap[p] > key:
                        heap[j] = heap[p]
                        j = p
                    else:
                        break
                heap[j] = key
                hn += 1
    if not closed[goal]:
        return np.empty(0, np.int64)
    m = 0
    c = goal
    while c != -1:
        m += 1
        c = par[c]
    out = np.empty(m, np.int64)
    c = goal
    for i in range(m - 1, -1, -1):
        out[i] = c
        c = par[c]
    return out
