"""确定性竖向栅格化核（M22 §6.6.6；V-GLB-04；D3-AC-02 "dsm_2m 与网格竖向射线抽样差 < 1 cm"）。

竖向射线命中等价于三角形在水平面上的覆盖测试，因此以栅格化代替 BVH 求交，结果与线程数、ISA 无关。每个 `cell` 格取 k × k 个
采样点 `x = x0 + (j + 0.5) · cell / k`、`y = y0 + (i + 0.5) · cell / k`（j = c·k + a，i = r·k + b，a、b ∈ [0, k)）；边函数与
重心插值均为 float64，边界含在内，竖直面与退化面（|2A| < 1e-12）不命中。归约：每格最高命中 z（无命中为 −inf）与有命中的采样数
（0–k²）。校验侧 `awr.world.package.validate.vertical_hits` 以 numpy 逐式复算同一采样位置（V-GLB-04），两者逐位一致。

三角形输入取 `awr-trimesh@1` 写出后的坐标（`float64(origin_m) + float64(xyz.f32)`，`trimesh_world`），使构建与校验读取同一份几何。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

try:  # numba 0.67.0（ADR-038）；缺失或 AWR_KERNEL=numpy 时走逐块 numpy 路径（结果相同）
    import os

    from numba import njit

    HAVE_NUMBA = os.environ.get("AWR_KERNEL", "").strip().lower() != "numpy"
except Exception:  # pragma: no cover - 取决于环境
    HAVE_NUMBA = False

    def njit(*a, **k):  # type: ignore[no-redef]
        def deco(f):
            return f
        return deco if not (a and callable(a[0])) else a[0]

__all__ = ["HAVE_NUMBA", "raster_top", "trimesh_world"]


@njit(cache=True, fastmath=False, nogil=True)
def _raster_kernel(V, F, x0, y0, cell, k, H, W, top, hit):  # pragma: no cover - numba 编译执行（numpy 对拍覆盖）
    s = cell / k
    NX = W * k
    NY = H * k
    for f in range(F.shape[0]):
        a = F[f, 0]
        b = F[f, 1]
        c = F[f, 2]
        ax, ay, az = V[a, 0], V[a, 1], V[a, 2]
        bx, by, bz = V[b, 0], V[b, 1], V[b, 2]
        cx, cy, cz = V[c, 0], V[c, 1], V[c, 2]
        A2 = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        if abs(A2) < 1e-12:
            continue
        j0 = math.floor((min(ax, bx, cx) - x0) / s - 0.5)
        j1 = math.ceil((max(ax, bx, cx) - x0) / s - 0.5)
        i0 = math.floor((min(ay, by, cy) - y0) / s - 0.5)
        i1 = math.ceil((max(ay, by, cy) - y0) / s - 0.5)
        j0 = max(j0, 0)
        i0 = max(i0, 0)
        j1 = min(j1, NX - 1)
        i1 = min(i1, NY - 1)
        for i in range(i0, i1 + 1):
            r = i // k
            bb = i - r * k
            for j in range(j0, j1 + 1):
                cc = j // k
                aa = j - cc * k
                px = x0 + ((cc * k + aa) + 0.5) * s
                py = y0 + ((r * k + bb) + 0.5) * s
                w0 = (bx - px) * (cy - py) - (cx - px) * (by - py)
                w1 = (cx - px) * (ay - py) - (ax - px) * (cy - py)
                w2 = (ax - px) * (by - py) - (bx - px) * (ay - py)
                if A2 > 0:
                    if w0 < 0 or w1 < 0 or w2 < 0:
                        continue
                elif w0 > 0 or w1 > 0 or w2 > 0:
                    continue
                z = (w0 * az + w1 * bz + w2 * cz) / A2
                q = r * W + cc
                if z > top[q]:
                    top[q] = z
                hit[q * k * k + bb * k + aa] = 1


def _raster_numpy(V, F, x0, y0, cell, k, H, W, top, hit, chunk: int = 200_000) -> None:
    """numpy 路径：按三角形分块展开采样包围盒，逐式同 `_raster_kernel`。"""
    s = cell / k
    NX, NY = W * k, H * k
    for f0 in range(0, len(F), chunk):
        T = V[F[f0:f0 + chunk]]
        ax, ay, az = T[:, 0, 0], T[:, 0, 1], T[:, 0, 2]
        bx, by, bz = T[:, 1, 0], T[:, 1, 1], T[:, 1, 2]
        cx, cy, cz = T[:, 2, 0], T[:, 2, 1], T[:, 2, 2]
        A2 = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
        j0 = np.maximum(np.floor((np.minimum(np.minimum(ax, bx), cx) - x0) / s - 0.5).astype(np.int64), 0)
        j1 = np.minimum(np.ceil((np.maximum(np.maximum(ax, bx), cx) - x0) / s - 0.5).astype(np.int64), NX - 1)
        i0 = np.maximum(np.floor((np.minimum(np.minimum(ay, by), cy) - y0) / s - 0.5).astype(np.int64), 0)
        i1 = np.minimum(np.ceil((np.maximum(np.maximum(ay, by), cy) - y0) / s - 0.5).astype(np.int64), NY - 1)
        ok = (np.abs(A2) >= 1e-12) & (j0 <= j1) & (i0 <= i1)
        t = np.flatnonzero(ok)
        nw = j1[t] - j0[t] + 1
        m = nw * (i1[t] - i0[t] + 1)
        tt = np.repeat(t, m)
        off = np.arange(int(m.sum())) - np.repeat(np.r_[0, np.cumsum(m)[:-1]], m)
        ww = np.repeat(nw, m)
        i = np.repeat(i0[t], m) + off // ww
        j = np.repeat(j0[t], m) + off % ww
        r, bb = i // k, i % k
        cc, aa = j // k, j % k
        px = x0 + ((cc * k + aa) + 0.5) * s
        py = y0 + ((r * k + bb) + 0.5) * s
        Ax, Ay, Az, Bx, By, Bz, Cx, Cy, Cz, D = (v[tt] for v in (ax, ay, az, bx, by, bz, cx, cy, cz, A2))
        w0 = (Bx - px) * (Cy - py) - (Cx - px) * (By - py)
        w1 = (Cx - px) * (Ay - py) - (Ax - px) * (Cy - py)
        w2 = (Ax - px) * (By - py) - (Bx - px) * (Ay - py)
        inside = np.where(D > 0, (w0 >= 0) & (w1 >= 0) & (w2 >= 0), (w0 <= 0) & (w1 <= 0) & (w2 <= 0))
        z = (w0 * Az + w1 * Bz + w2 * Cz) / D
        q = (r * W + cc)[inside]
        np.maximum.at(top, q, z[inside])
        hit[(q * k * k + (bb * k + aa)[inside])] = 1


def raster_top(V: np.ndarray, F: np.ndarray, origin_xy: tuple[float, float], cell: float, H: int, W: int, k: int = 4,
               *, use_numba: bool | None = None) -> tuple[np.ndarray, np.ndarray]:
    """返回 (每格最高命中 z float64 (H, W)，无命中为 −inf；每格有命中的采样数 uint8 (H, W))。"""
    V = np.ascontiguousarray(V, np.float64)
    F = np.ascontiguousarray(F, np.int64)
    top = np.full(H * W, -np.inf)
    hit = np.zeros(H * W * k * k, np.uint8)
    x0, y0 = float(origin_xy[0]), float(origin_xy[1])
    if (HAVE_NUMBA if use_numba is None else use_numba) and len(F):
        _raster_kernel(V, F, x0, y0, float(cell), int(k), int(H), int(W), top, hit)
    elif len(F):
        _raster_numpy(V, F, x0, y0, float(cell), int(k), int(H), int(W), top, hit)
    n = hit.reshape(H * W, k * k).sum(1, dtype=np.int64).astype(np.uint8)
    return top.reshape(H, W), n.reshape(H, W)


def trimesh_world(index_json: Path) -> tuple[np.ndarray, np.ndarray]:
    """读取 `awr-trimesh@1`：(世界坐标 float64 (n, 3)，全局顶点下标 int64 (m, 3))；坐标 = float64(origin_m) + float64(xyz)。"""
    index_json = Path(index_json)
    doc = json.loads(index_json.read_text(encoding="utf-8"))
    st = {s["name"]: s for s in doc["streams"]}
    xyz = np.fromfile(index_json.parent / st["xyz"]["href"], "<f4").reshape(-1, 3)
    tri = np.fromfile(index_json.parent / st["tri"]["href"], "<u4").reshape(-1, 3)
    V = np.empty(xyz.shape, np.float64)
    F = np.empty(tri.shape, np.int64)
    for t in doc["tiles"]:
        v0, nv, t0, nt = int(t["v0"]), int(t["nv"]), int(t["t0"]), int(t["nt"])
        V[v0:v0 + nv] = xyz[v0:v0 + nv].astype(np.float64) + np.asarray(t["origin_m"], np.float64)
        F[t0:t0 + nt] = tri[t0:t0 + nt].astype(np.int64) + v0
    return V, F
