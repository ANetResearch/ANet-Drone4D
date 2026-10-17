"""事件舞台可走面（M23 §6.3、§6.5、§6.6.2 的 P0a 过渡实现）：栅格高度场、吸附、A*、拉直与受限圆角。

世界包尚无 Recast NavMesh 图层（M22 navmesh 未交付）时，编译器在事件舞台范围内由 raycast 网格自上而下求交得到 1 m 高度场，
按 profile 的可跨台阶（行人 0.45 m、车辆 0.30 m）标记边沿、车辆再腐蚀 1 格，取种子锚点所在的 4 连通分量（无种子时取最大分量）
为主岛。路径为 8 连通整数代价 A*（正交 10、对角 14，对角须两侧正交格可走），平局按格号裁决；随后贪心视线拉直并按 §6.6.2 做
受限圆角。全部运算为整数或显式次序的 float64，与输入次序无关；`sha256` 覆盖高度场与可走位，写入磁带头 `navmesh_sha256`。
NavMesh 图层就绪后由 Detour 实现替换（同一接口：`snap`、`path`、`los`）。
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
from dataclasses import dataclass, field

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

__all__ = ["CELL_M", "PROFILES", "NavGrid", "Snap", "build_nav", "rounded_length"]

CELL_M = 1.0
# 可跨台阶（m，每 1 m 格）、额外腐蚀格数
PROFILES = {"person": {"climb_m": 0.45, "erode": 0}, "vehicle": {"climb_m": 0.30, "erode": 1}}
SNAP_OK_M, SNAP_WARN_M = 3.0, 8.0


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
    profile: str
    x0: float
    y0: float
    nx: int
    ny: int
    z: np.ndarray                        # (ny, nx) float64，NaN 为无表面
    walk: np.ndarray                     # (ny, nx) bool，已限于主岛
    sha256: str = ""
    _w8: np.ndarray = field(default=None, repr=False)          # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._w8 = np.ascontiguousarray(self.walk.ravel().astype(np.uint8))

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
