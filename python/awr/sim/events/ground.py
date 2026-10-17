"""确定性贴地 `ground_z`（M23 §6.6.5；AWR-05 §5.2.3）：事件舞台子集的三角形 2 m 格索引与显式次序的 float64 重心坐标求值。

三角形取自 raycast 网格（`awr-trimesh@1`，瓦片局部 f32 顶点按值转 f64 后加瓦片原点），按全局三角形号（瓦片序 prim_id）升序；
每个三角形登记到其平面包围盒覆盖的全部 2 m 格（格内保持升序）。`ground_z` 在候选窗口 [z_hint − 2, z_hint + 1] m 内取
`(|z − z_hint|, id)` 最小者；无候选返回 None（调用方保留 z_hint 并记 S-ZGROUND 候选）。`ground_z_ref` 为逐标量参考实现，
`ground_z_batch` 为批量内核（numba 不做 FMA 合并），二者逐位一致（M23-AC-009）。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

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

__all__ = ["GROUND_CELL_M", "GroundIndex", "build_ground_index"]

GROUND_CELL_M = 2.0
Z_ABOVE, Z_BELOW = 1.0, 2.0


@dataclass
class GroundIndex:
    tri: np.ndarray         # (T, 3, 3) float64 world 帧顶点
    tid: np.ndarray         # (T,) int64 全局三角形号（升序）
    gx0: int                # 格索引原点（格号，floor(x / 2)）
    gy0: int
    gnx: int
    gny: int
    start: np.ndarray       # (gnx·gny + 1,) CSR 起点
    items: np.ndarray       # 局部三角形下标（格内按 tid 升序）

    def ground_z_ref(self, x: float, y: float, z_hint: float) -> float | None:
        cx, cy = math.floor(x / GROUND_CELL_M) - self.gx0, math.floor(y / GROUND_CELL_M) - self.gy0
        if not (0 <= cx < self.gnx and 0 <= cy < self.gny):
            return None
        c = cy * self.gnx + cx
        best = None
        for i in self.items[self.start[c]:self.start[c + 1]]:
            (x1, y1, z1), (x2, y2, z2), (x3, y3, z3) = (tuple(float(v) for v in p) for p in self.tri[i])
            d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if abs(d) < 1e-12:
                continue
            l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
            l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
            l3 = 1 - l1 - l2
            if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                continue
            z = l1 * z1 + l2 * z2 + l3 * z3
            if z > z_hint + Z_ABOVE or z < z_hint - Z_BELOW:
                continue
            key = (abs(z - z_hint), int(self.tid[i]))
            if best is None or key < best[0]:
                best = (key, z)
        return None if best is None else best[1]

    def ground_z_batch(self, xy: np.ndarray, z_hint: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """返回 (z, found)；未找到时 z = z_hint。"""
        xy = np.ascontiguousarray(xy, np.float64)
        zh = np.ascontiguousarray(z_hint, np.float64)
        return _batch(self.tri, self.tid, self.gx0, self.gy0, self.gnx, self.gny, self.start, self.items, xy, zh)


def build_ground_index(q, bbox: tuple[float, float, float, float], margin_m: float = 50.0) -> GroundIndex:
    """q 为 MeshWorldQuery；bbox = (xmin, ymin, xmax, ymax)，外扩 margin_m。"""
    lo = np.array([bbox[0] - margin_m, bbox[1] - margin_m])
    hi = np.array([bbox[2] + margin_m, bbox[3] + margin_m])
    tris, tids = [], []
    for t in q.tiles:
        if t.hi[0] < lo[0] or t.lo[0] > hi[0] or t.hi[1] < lo[1] or t.lo[1] > hi[1]:
            continue
        P = t.V.astype(np.float64)[t.F.astype(np.int64)] + t.origin      # (nt, 3, 3)
        mn, mx = P[:, :, :2].min(1), P[:, :, :2].max(1)
        m = (mx[:, 0] >= lo[0]) & (mn[:, 0] <= hi[0]) & (mx[:, 1] >= lo[1]) & (mn[:, 1] <= hi[1])
        tris.append(P[m])
        tids.append(t.t0 + np.nonzero(m)[0].astype(np.int64))
    tri = np.concatenate(tris) if tris else np.zeros((0, 3, 3))
    tid = np.concatenate(tids) if tids else np.zeros(0, np.int64)
    o = np.argsort(tid, kind="stable")
    tri, tid = np.ascontiguousarray(tri[o]), np.ascontiguousarray(tid[o])
    gx0, gy0 = math.floor(lo[0] / GROUND_CELL_M), math.floor(lo[1] / GROUND_CELL_M)
    gnx, gny = math.floor(hi[0] / GROUND_CELL_M) - gx0 + 1, math.floor(hi[1] / GROUND_CELL_M) - gy0 + 1
    mn, mx = tri[:, :, :2].min(1), tri[:, :, :2].max(1)
    ax0 = np.clip(np.floor(mn[:, 0] / GROUND_CELL_M).astype(np.int64) - gx0, 0, gnx - 1)
    ax1 = np.clip(np.floor(mx[:, 0] / GROUND_CELL_M).astype(np.int64) - gx0, 0, gnx - 1)
    ay0 = np.clip(np.floor(mn[:, 1] / GROUND_CELL_M).astype(np.int64) - gy0, 0, gny - 1)
    ay1 = np.clip(np.floor(mx[:, 1] / GROUND_CELL_M).astype(np.int64) - gy0, 0, gny - 1)
    start, items = _register(ax0, ax1, ay0, ay1, gnx, gny)
    return GroundIndex(tri, tid, gx0, gy0, gnx, gny, start, items)


@njit(cache=True)
def _register(ax0, ax1, ay0, ay1, gnx, gny):  # pragma: no cover - numba
    n = gnx * gny
    cnt = np.zeros(n + 1, np.int64)
    for i in range(len(ax0)):
        for cy in range(ay0[i], ay1[i] + 1):
            for cx in range(ax0[i], ax1[i] + 1):
                cnt[cy * gnx + cx + 1] += 1
    for c in range(n):
        cnt[c + 1] += cnt[c]
    items = np.empty(cnt[n], np.int64)
    fill = cnt[:n].copy()
    for i in range(len(ax0)):                              # i 升序 → 格内按 tid 升序
        for cy in range(ay0[i], ay1[i] + 1):
            for cx in range(ax0[i], ax1[i] + 1):
                c = cy * gnx + cx
                items[fill[c]] = i
                fill[c] += 1
    return cnt, items


@njit(cache=True)
def _batch(tri, tid, gx0, gy0, gnx, gny, start, items, xy, zh):  # pragma: no cover - numba
    n = len(xy)
    out = zh.copy()
    found = np.zeros(n, np.bool_)
    for k in range(n):
        x, y, h = xy[k, 0], xy[k, 1], zh[k]
        cx = math.floor(x / 2.0) - gx0
        cy = math.floor(y / 2.0) - gy0
        if cx < 0 or cy < 0 or cx >= gnx or cy >= gny:
            continue
        c = cy * gnx + cx
        bd, bid, bz = 0.0, -1, 0.0
        for s in range(start[c], start[c + 1]):
            i = items[s]
            x1, y1, z1 = tri[i, 0, 0], tri[i, 0, 1], tri[i, 0, 2]
            x2, y2, z2 = tri[i, 1, 0], tri[i, 1, 1], tri[i, 1, 2]
            x3, y3, z3 = tri[i, 2, 0], tri[i, 2, 1], tri[i, 2, 2]
            d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
            if abs(d) < 1e-12:
                continue
            l1 = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
            l2 = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
            l3 = 1 - l1 - l2
            if l1 < -1e-9 or l2 < -1e-9 or l3 < -1e-9:
                continue
            z = l1 * z1 + l2 * z2 + l3 * z3
            if z > h + 1.0 or z < h - 2.0:
                continue
            dz = abs(z - h)
            if bid < 0 or dz < bd or (dz == bd and tid[i] < bid):
                bd, bid, bz = dz, tid[i], z
        if bid >= 0:
            out[k] = bz
            found[k] = True
    return out, found
