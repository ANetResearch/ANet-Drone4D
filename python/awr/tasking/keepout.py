"""禁入体（M20-FR-016、FR-035、FR-044；M20 §6.4；AWR-04 §8.1、§9.4）。

2.5D 投影：识别物的禁入体（M18 `keepout_volumes`，半径已含外扩 m = 20 m + 3·sigma_hold；运动识别物的扇形按整圆）与 [z_ground, H_cap]
相交即在其水平圆内全高度禁入（球与半球取水平最大半径，保守），因此只能水平绕行。本模块：

- `circles(keepouts, extra_m)`：k×3 数组 (x, y, r)；
- `inside(xy, C)`、`signed_margin(xy, C)`：点在禁入圆内、到最近禁入圆边界的带符号距离；
- `seg_blocked(a, b, C)`：线段穿越的禁入圆；`free_intervals(a, b, C)`：线段在全部禁入圆外的参数区间（解析）；
- `detour_length(a, b, C)`：解析绕行长度（切线 + 外侧圆弧，多个圆依次累加，ETA 与能量估计用，M20-FR-035）；
- `plan_path(a, b, C)`：可见图最短路（外切 16 边形顶点 + 起终点，Dijkstra），航线一律在禁入圆外；
- `clip_polyline(P, C)`：折线被禁入圆截断后的各段；`path_clear(P, C, step)`：1 m 采样校验与最小余量（M20-AC-008）；
- `from_volumes(vols, …)`：M18 KeepoutVolume（dict 或 Volume 对象）→ `Keepout`。
"""

from __future__ import annotations

import heapq
import math
from typing import Any

import numpy as np

from .geom import circle_polygon, resample
from .model import Keepout

__all__ = ["NPOLY", "circles", "clip_polyline", "detour_length", "free_intervals", "from_volumes", "inside", "path_clear", "path_length",
           "plan_path", "plan_route", "seg_blocked", "seg_dist", "signed_margin"]

NPOLY = 16              # 绕行用外切多边形的边数
INFLATE_M = 3.0         # 绕行顶点相对禁入圆的额外余量（m）
EPS = 1e-6


def circles(keepouts: list[Keepout] | np.ndarray | None, extra_m: float = 0.0) -> np.ndarray:
    if keepouts is None:
        return np.zeros((0, 3))
    if isinstance(keepouts, np.ndarray):
        C = np.asarray(keepouts, np.float64).reshape(-1, 3).copy()
        C[:, 2] += float(extra_m)
        return C
    if not keepouts:
        return np.zeros((0, 3))
    return np.array([[k.x, k.y, k.r + float(extra_m)] for k in keepouts], np.float64)


def from_volumes(vols: list[Any], *, target: str | None = None, moving: bool = False, extra_m: float = 0.0,
                 z_lo: float = -1e9, z_hi: float = 1e9) -> list[Keepout]:
    """M18 禁入体（契约 KeepoutVolume dict 或 `awr.sim.targets.Volume`）→ 2.5D 禁入圆；高度区间与 [z_lo, z_hi] 不相交的体忽略。"""
    out: list[Keepout] = []
    for k, v in enumerate(vols):
        if isinstance(v, dict):
            c = v.get("center_enu_m") or v.get("center") or (0.0, 0.0, 0.0)
            r, z0, z1 = float(v["r_m"]), float(v.get("z_min_m", -1e9)), float(v.get("z_max_m", 1e9))
            vid = str(v.get("id") or f"{target}/{k}")
        else:
            c = v.center
            r, z0, z1 = float(v.r_m), float(v.z_min_m), float(v.z_max_m)
            vid = f"{target}/{getattr(v, 'vol_idx', k)}"
        if z1 < z_lo or z0 > z_hi:
            continue
        out.append(Keepout(vid, float(c[0]), float(c[1]), r + float(extra_m), z0, z1, target, moving))
    return out


def inside(xy: np.ndarray, C: np.ndarray, margin: float = 0.0) -> np.ndarray:
    P = np.atleast_2d(np.asarray(xy, np.float64))[:, :2]
    if len(C) == 0:
        return np.zeros(len(P), bool)
    d = np.hypot(P[:, None, 0] - C[None, :, 0], P[:, None, 1] - C[None, :, 1]) - C[None, :, 2]
    return (d < -float(margin) - EPS).any(axis=1)        # 边界上（数值误差内）视为圆外


def signed_margin(xy: np.ndarray, C: np.ndarray) -> np.ndarray:
    """到最近禁入圆边界的带符号距离（外正内负）；无禁入圆时 +inf。"""
    P = np.atleast_2d(np.asarray(xy, np.float64))[:, :2]
    if len(C) == 0:
        return np.full(len(P), np.inf)
    d = np.hypot(P[:, None, 0] - C[None, :, 0], P[:, None, 1] - C[None, :, 1]) - C[None, :, 2]
    return d.min(axis=1)


def seg_dist(U: np.ndarray, V: np.ndarray, C: np.ndarray) -> np.ndarray:
    """线段 U[i]→V[i] 到圆心 C[j] 的距离（m×k）。"""
    U = np.asarray(U, np.float64)[:, :2]
    V = np.asarray(V, np.float64)[:, :2]
    D = V - U
    dd = np.maximum((D * D).sum(axis=1), 1e-12)
    W = C[None, :, :2] - U[:, None, :]
    t = np.clip((W * D[:, None, :]).sum(axis=2) / dd[:, None], 0.0, 1.0)
    Q = U[:, None, :] + t[:, :, None] * D[:, None, :]
    return np.hypot(C[None, :, 0] - Q[:, :, 0], C[None, :, 1] - Q[:, :, 1])


def seg_blocked(a, b, C: np.ndarray) -> np.ndarray:
    """线段 a→b 穿越的禁入圆下标（距离 < r）。"""
    if len(C) == 0:
        return np.zeros(0, np.int64)
    d = seg_dist(np.asarray(a, float)[None, :2], np.asarray(b, float)[None, :2], C)[0]
    return np.flatnonzero(d < C[:, 2] - EPS)


def free_intervals(a, b, C: np.ndarray) -> list[tuple[float, float]]:
    """线段 a + t(b − a)，t ∈ [0, 1] 在全部禁入圆外的区间（解析求交、区间并的补集）。"""
    a = np.asarray(a, np.float64)[:2]
    d = np.asarray(b, np.float64)[:2] - a
    dd = float(d @ d)
    if dd < 1e-12:
        return [] if len(C) and bool(inside(a[None], C)[0]) else [(0.0, 1.0)]
    blocked: list[tuple[float, float]] = []
    for cx, cy, r in C:
        f = a - np.array([cx, cy])
        B = 2.0 * float(f @ d)
        Cc = float(f @ f) - r * r
        disc = B * B - 4 * dd * Cc
        if disc <= 0:
            continue
        sq = math.sqrt(disc)
        t1, t2 = (-B - sq) / (2 * dd), (-B + sq) / (2 * dd)
        lo, hi = max(t1, 0.0), min(t2, 1.0)
        if hi > lo:
            blocked.append((lo, hi))
    if not blocked:
        return [(0.0, 1.0)]
    blocked.sort()
    merged = [list(blocked[0])]
    for lo, hi in blocked[1:]:
        if lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out: list[tuple[float, float]] = []
    t = 0.0
    for lo, hi in merged:
        if lo > t + 1e-9:
            out.append((t, lo))
        t = max(t, hi)
    if t < 1.0 - 1e-9:
        out.append((t, 1.0))
    return out


def _around(p: np.ndarray, c: np.ndarray, r: float) -> tuple[float, float]:
    d = float(np.hypot(*(p - c)))
    rr = min(r, d - 1e-6) if d > 1e-6 else r
    return math.sqrt(max(d * d - rr * rr, 0.0)), math.acos(min(1.0, rr / max(d, 1e-9)))


def detour_length(a, b, C: np.ndarray) -> float:
    """解析绕行长度（M20 §6.7 route_estimate）：起终点连线与各禁入圆求交，相交时以"两条切线 + 外侧圆弧"替代弦长，多个圆依次累加。
    起点或终点在圆内时该圆按端点到圆心的距离收缩（估计值，实际航线由 `plan_path` 给出）。"""
    a = np.asarray(a, np.float64)[:2]
    b = np.asarray(b, np.float64)[:2]
    L = float(np.hypot(*(b - a)))
    if len(C) == 0 or L < 1e-9:
        return L
    extra = 0.0
    for j in seg_blocked(a, b, C):
        c = C[j, :2]
        r = float(C[j, 2])
        ta, aa = _around(a, c, r)
        tb, ab = _around(b, c, r)
        va, vb = a - c, b - c
        gam = math.acos(max(-1.0, min(1.0, float(va @ vb) / max(np.linalg.norm(va) * np.linalg.norm(vb), 1e-12))))
        arc = max(0.0, gam - aa - ab)
        # 单圆的最短绕行（切线 + 圆弧 + 切线）替换整条弦 |ab|；多个圆各自相对端点累加（偏保守的估计）
        extra += max(0.0, ta + tb + r * arc - L)
    return L + extra


def path_length(P) -> float:
    A = np.asarray(P, np.float64)
    if len(A) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(A[:, :2], axis=0), axis=1).sum())


def _graph_path(a: np.ndarray, b: np.ndarray, C: np.ndarray, use: np.ndarray, inflate: float, node_ok: Any = None) -> np.ndarray | None:
    nodes = [a, b]
    for j in use:
        cx, cy, r = C[j]
        nodes.extend(circle_polygon(cx, cy, r + inflate, NPOLY))
    N = np.asarray(nodes, np.float64)
    keep = ~inside(N, C, margin=-1e-6)
    if node_ok is not None and len(N) > 2:
        keep[2:] &= np.asarray(node_ok(N[2:]), bool)       # 世界边界与禁飞区（D2-FX-R2：绕行顶点越界时细校验 102）
    keep[0] = keep[1] = True
    idx = np.flatnonzero(keep)
    N = N[idx]
    n = len(N)
    iu, ju = np.triu_indices(n, 1)
    ok = np.ones(len(iu), bool)
    Cu = C[use]
    step = 20000
    for s in range(0, len(iu), step):
        sl = slice(s, s + step)
        d = seg_dist(N[iu[sl]], N[ju[sl]], Cu)
        ok[sl] = ~(d < Cu[None, :, 2] - EPS).any(axis=1)
    W = np.full((n, n), np.inf)
    L = np.hypot(N[iu, 0] - N[ju, 0], N[iu, 1] - N[ju, 1])
    W[iu[ok], ju[ok]] = L[ok]
    W[ju[ok], iu[ok]] = L[ok]
    dist = np.full(n, np.inf)
    prev = np.full(n, -1, np.int64)
    dist[0] = 0.0
    done = np.zeros(n, bool)
    pq = [(0.0, 0)]
    while pq:
        du, u = heapq.heappop(pq)
        if done[u]:
            continue
        done[u] = True
        if u == 1:
            break
        row = W[u]
        nd = du + row
        better = (nd < dist) & ~done
        for v in np.flatnonzero(better):
            dist[v] = nd[v]
            prev[v] = u
            heapq.heappush(pq, (float(nd[v]), int(v)))
    if not np.isfinite(dist[1]):
        return None
    path = [1]
    while path[-1] != 0:
        path.append(int(prev[path[-1]]))
    return N[path[::-1]]


def plan_path(a, b, C: np.ndarray, *, inflate: float = INFLATE_M, max_iter: int = 6, node_ok: Any = None) -> np.ndarray | None:
    """禁入圆外的最短水平路径（N×2，含起终点）；起点或终点在禁入圆内、或被完全包围时 None。只把与当前路径相交的圆加入可见图
    （迭代至路径与全部圆不相交，≤ max_iter 轮），因此禁入圆多时计算量只随相关圆增长。"""
    a = np.asarray(a, np.float64)[:2]
    b = np.asarray(b, np.float64)[:2]
    if len(C) == 0:
        return np.vstack([a, b])
    if bool(inside(np.vstack([a, b]), C).any()):
        return None
    use: list[int] = sorted(set(seg_blocked(a, b, C).tolist()))
    if not use:
        return np.vstack([a, b])
    for _ in range(max_iter):
        P = _graph_path(a, b, C, np.asarray(use, np.int64), inflate, node_ok)
        if P is None:
            return None
        hits: set[int] = set()
        for k in range(len(P) - 1):
            hits |= set(seg_blocked(P[k], P[k + 1], C).tolist())
        new = sorted(hits - set(use))
        if not new:
            return P
        use = sorted(set(use) | set(new))
    return None


def plan_route(points, C: np.ndarray, *, closed: bool = False, node_ok: Any = None) -> np.ndarray | None:
    """依次连接各点的禁入圆外折线（每段 `plan_path`）；任一段无解时 None。"""
    pts = np.asarray(points, np.float64)[:, :2]
    if closed and len(pts) > 1:
        pts = np.vstack([pts, pts[:1]])
    out: list[np.ndarray] = [pts[:1]]
    for k in range(len(pts) - 1):
        seg = plan_path(pts[k], pts[k + 1], C, node_ok=node_ok)
        if seg is None:
            return None
        out.append(seg[1:])
    P = np.vstack(out)
    return P[:-1] if closed and len(P) > 1 else P


def clip_polyline(P, C: np.ndarray, *, closed: bool = False, min_len_m: float = 1.0) -> tuple[list[np.ndarray], list[int]]:
    """折线（N×2 或 N×3）被禁入圆截断：返回 (禁入圆外的各段（保持顺序，z 线性插值）, 截断它的圆下标)。"""
    A = np.asarray(P, np.float64)
    if closed and len(A) > 1:
        A = np.vstack([A, A[:1]])
    pieces: list[list[np.ndarray]] = [[]]
    hit: set[int] = set()
    for k in range(len(A) - 1):
        a, b = A[k], A[k + 1]
        iv = free_intervals(a, b, C)
        hit |= set(seg_blocked(a, b, C).tolist())
        if iv == [(0.0, 1.0)]:
            if not pieces[-1]:
                pieces[-1].append(a)
            pieces[-1].append(b)
            continue
        for t0, t1 in iv:
            p0 = a + (b - a) * t0
            p1 = a + (b - a) * t1
            if t0 > 1e-9 and pieces[-1]:
                pieces.append([])
            if not pieces[-1]:
                pieces[-1].append(p0)
            pieces[-1].append(p1)
        if not iv or iv[-1][1] < 1.0 - 1e-9:
            pieces.append([])
    out = [np.asarray(p) for p in pieces if len(p) >= 2 and path_length(np.asarray(p)) >= min_len_m]
    return out, sorted(hit)


def path_clear(P, C: np.ndarray, step_m: float = 1.0) -> tuple[bool, float]:
    """按 step_m 水平采样的禁入校验：(全部在禁入圆外, 最小余量 m)（M20-AC-008：全部 M20 航线按 1 m 采样均在禁入体外）。"""
    A = np.asarray(P, np.float64)
    if len(C) == 0 or len(A) == 0:
        return True, math.inf
    S = resample(A, step_m) if len(A) >= 2 else A
    m = signed_margin(S, C)
    mn = float(m.min())
    return mn >= -1e-6, mn
