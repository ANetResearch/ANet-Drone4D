"""平面几何助手（world ENU，m）：多边形面积与质心、点在多边形、折线长度与按弧长取点、最小外接圆（Welzl）、多边形偏移、外切多边形。"""

from __future__ import annotations

import math

import numpy as np

from awr.swarm.coverage.lanes import polygon_area
from awr.swarm.coverage.plan import point_in_polygon

__all__ = ["centroid", "circle_polygon", "cut_polyline", "min_enclosing_circle", "offset_polygon", "point_at", "point_in_polygon",
           "polygon_area", "polyline_length", "resample", "ring", "signed_area"]


def ring(P) -> np.ndarray:
    """多边形顶点（N×2，去掉与首点重合的末点）。"""
    A = np.atleast_2d(np.asarray(P, np.float64))[:, :2]
    if len(A) >= 2 and np.allclose(A[0], A[-1]):
        A = A[:-1]
    return A


def signed_area(P) -> float:
    A = ring(P)
    x, y = A[:, 0], A[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def centroid(P) -> np.ndarray:
    A = ring(P)
    a = signed_area(A)
    if abs(a) < 1e-9:
        return A.mean(axis=0)
    x, y = A[:, 0], A[:, 1]
    x1, y1 = np.roll(x, -1), np.roll(y, -1)
    c = x * y1 - x1 * y
    return np.array([float(((x + x1) * c).sum()) / (6 * a), float(((y + y1) * c).sum()) / (6 * a)])


def polyline_length(P, closed: bool = False) -> float:
    A = np.asarray(P, np.float64)
    if len(A) < 2:
        return 0.0
    L = float(np.linalg.norm(np.diff(A[:, :2], axis=0), axis=1).sum())
    if closed:
        L += float(np.linalg.norm(A[0, :2] - A[-1, :2]))
    return L


def _cum(P: np.ndarray) -> np.ndarray:
    return np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(P[:, :2], axis=0), axis=1))]


def point_at(P, s: float) -> np.ndarray:
    """折线上弧长 s 处的点（按水平弧长，z 线性插值；超界截断）。"""
    A = np.asarray(P, np.float64)
    if len(A) == 1:
        return A[0].copy()
    c = _cum(A)
    s = min(max(float(s), 0.0), float(c[-1]))
    k = int(np.searchsorted(c, s, side="right") - 1)
    k = min(max(k, 0), len(A) - 2)
    seg = c[k + 1] - c[k]
    f = 0.0 if seg <= 1e-12 else (s - c[k]) / seg
    return A[k] + (A[k + 1] - A[k]) * f


def cut_polyline(P, s0: float, s1: float) -> np.ndarray:
    """弧长区间 [s0, s1] 的子折线（含两端插值点）。"""
    A = np.asarray(P, np.float64)
    c = _cum(A)
    s0, s1 = max(0.0, float(s0)), min(float(c[-1]), float(s1))
    if s1 <= s0:
        return np.vstack([point_at(A, s0)])
    inner = A[(c > s0 + 1e-9) & (c < s1 - 1e-9)]
    return np.vstack([point_at(A, s0), inner, point_at(A, s1)])


def resample(P, step_m: float) -> np.ndarray:
    """按水平弧长等距重采样（含端点），用于 1 m 禁入校验与航线复核。"""
    A = np.asarray(P, np.float64)
    if len(A) < 2:
        return A.copy()
    c = _cum(A)
    n = max(2, math.ceil(c[-1] / max(step_m, 1e-6)) + 1)
    s = np.linspace(0.0, c[-1], n)
    out = np.empty((n, A.shape[1]))
    for j in range(A.shape[1]):
        out[:, j] = np.interp(s, c, A[:, j])
    return out


def min_enclosing_circle(P) -> tuple[float, float, float]:
    """最小外接圆 (cx, cy, r)：Welzl 的迭代形式（点按确定顺序，期望线性时间对小多边形无关紧要）。"""
    pts = [tuple(map(float, p)) for p in ring(P)]
    if not pts:
        return 0.0, 0.0, 0.0

    def circ2(a, b):
        cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        return cx, cy, math.hypot(a[0] - cx, a[1] - cy)

    def circ3(a, b, c):
        ax, ay, bx, by, cx, cy = *a, *b, *c
        d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(d) < 1e-12:
            cands = [circ2(a, b), circ2(a, c), circ2(b, c)]
            return max(cands, key=lambda t: t[2])
        ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
        uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
        return ux, uy, math.hypot(ax - ux, ay - uy)

    def inside(c, p):
        return math.hypot(p[0] - c[0], p[1] - c[1]) <= c[2] + 1e-7

    c = (pts[0][0], pts[0][1], 0.0)
    for i, p in enumerate(pts):
        if inside(c, p):
            continue
        c = (p[0], p[1], 0.0)
        for j in range(i):
            q = pts[j]
            if inside(c, q):
                continue
            c = circ2(p, q)
            for k in range(j):
                if not inside(c, pts[k]):
                    c = circ3(p, q, pts[k])
    return c


def offset_polygon(P, d_m: float, miter_max: float = 3.0) -> np.ndarray:
    """多边形外偏（d > 0 向外，d < 0 向内）：各边平移后相邻两线求交（斜接），斜接长度超过 miter_max·|d| 时截为两点（倒角）。"""
    A = ring(P)
    if abs(d_m) < 1e-9 or len(A) < 3:
        return A.copy()
    if signed_area(A) < 0:
        A = A[::-1]
    n = len(A)
    out: list[np.ndarray] = []
    for i in range(n):
        p_prev, p, p_next = A[i - 1], A[i], A[(i + 1) % n]
        e0 = p - p_prev
        e1 = p_next - p
        n0 = np.array([e0[1], -e0[0]]) / max(np.linalg.norm(e0), 1e-12)
        n1 = np.array([e1[1], -e1[0]]) / max(np.linalg.norm(e1), 1e-12)
        q0 = p + n0 * d_m
        q1 = p + n1 * d_m
        cross = e0[0] * e1[1] - e0[1] * e1[0]
        if abs(cross) < 1e-9:
            out.append(q0)
            continue
        t = ((q1 - q0)[0] * e1[1] - (q1 - q0)[1] * e1[0]) / cross
        m = q0 + e0 * t
        if np.linalg.norm(m - p) > miter_max * abs(d_m):
            out.extend([q0, q1])
        else:
            out.append(m)
    return np.asarray(out)


def circle_polygon(cx: float, cy: float, r: float, n: int = 16) -> np.ndarray:
    """外切正 n 边形（完全包含该圆）：顶点半径 r / cos(π/n)。"""
    R = r / math.cos(math.pi / n)
    a = np.arange(n) * 2.0 * math.pi / n
    return np.c_[cx + R * np.cos(a), cy + R * np.sin(a)]
