"""航带生成与扫描角搜索（M10-FR-049、FR-050；M10 §6.5.12 第 2–5 步；r26 §3.8.3–§3.8.5）。

- `lanes_for_angle`：旋转后扫描线与多边形（含洞）各边求交，半开区间规则 `(a.y ≤ y < b.y) or (b.y ≤ y < a.y)` 防止
  顶点重复，交点排序后偶奇配对；凹多边形或洞得到同一扫描线上的多段；
- `best_sweep_angle`：候选为 AOI 各边方向与 {0°, 5°, …, 175°}，取单机时间最小者；
- `clip_lanes`（ext，`fixed_agl`）：沿航段以 res/2 采样阻塞栅格，保留连续空闲区段并丢弃 < min_lane_m 的片段；
- `bcd_cells`、`order_cells`（ext）：BCD-lite 分胞与贪心近邻排序（每个胞 4 种进入方式）；
- `boustrophedon`：单胞蛇形排序。
坐标为 world ENU（m）；多边形不要求闭合（首尾相同的点会被去掉）。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = [
    "Seg",
    "bcd_cells",
    "best_sweep_angle",
    "boustrophedon",
    "clip_lanes",
    "lanes_for_angle",
    "order_cells",
    "polygon_area",
    "seq_time",
    "sweep_candidates",
]

Seg = tuple[np.ndarray, np.ndarray]


def _ring(P) -> np.ndarray:
    P = np.asarray(P, np.float64)[:, :2]
    if len(P) >= 2 and np.allclose(P[0], P[-1]):
        P = P[:-1]
    return P


def polygon_area(P) -> float:
    P = _ring(P)
    x, y = P[:, 0], P[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _rot(th: float) -> np.ndarray:
    c, s = math.cos(th), math.sin(th)
    return np.array([[c, -s], [s, c]])


def _edges(poly, holes) -> tuple[np.ndarray, np.ndarray]:
    A, B = [], []
    for r in [poly, *(holes or [])]:
        R = _ring(r)
        if len(R) < 3:
            continue
        A.append(R)
        B.append(np.roll(R, -1, axis=0))
    return np.vstack(A), np.vstack(B)


def lanes_for_angle(poly, spacing_m: float, theta_rad: float, holes=None) -> tuple[list[list[Seg]], float]:
    """与方向 θ（ENU，自东逆时针）平行的航带；返回 (航带列表（按扫描顺序，每条为若干段）, 实际间距)。"""
    A, B = _edges(poly, holes)
    Rm = _rot(-theta_rad)
    Ar, Br = A @ Rm.T, B @ Rm.T
    ys_all = np.r_[Ar[:, 1], Br[:, 1]]
    ymin, ymax = float(ys_all.min()), float(ys_all.max())
    width = ymax - ymin
    nl = max(1, math.ceil(width / max(spacing_m, 1e-6) - 1e-9))
    ys = ymin + (np.arange(nl) + 0.5) * (width / nl)
    Rb = _rot(theta_rad)
    lanes: list[list[Seg]] = []
    for y in ys:
        cond = ((Ar[:, 1] <= y) & (Br[:, 1] > y)) | ((Br[:, 1] <= y) & (Ar[:, 1] > y))
        if not cond.any():
            lanes.append([])
            continue
        a, b = Ar[cond], Br[cond]
        xs = np.sort(a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1]))
        segs: list[Seg] = []
        for k in range(0, len(xs) - 1, 2):
            if xs[k + 1] - xs[k] < 1e-6:
                continue
            segs.append((Rb @ np.array([xs[k], y]), Rb @ np.array([xs[k + 1], y])))
        lanes.append(segs)
    return lanes, width / nl


def boustrophedon(lanes: list[list[Seg]], reverse_first: bool = False) -> list[Seg]:
    """蛇形排序：逐航带交替方向；同一航带内的多段按方向顺序。"""
    out: list[Seg] = []
    flip = reverse_first
    for segs in lanes:
        if not segs:
            continue
        ss = segs[::-1] if flip else segs
        out.extend(((p1, p0) if flip else (p0, p1)) for p0, p1 in ss)
        flip = not flip
    return out


def seq_time(seq: list[Seg], v_mps: float, a_mps2: float = 2.0) -> float:
    """有序航段的用时：每段直线梯形（L/V + V/a），段间衔接按直线（g/V）。"""
    t = 0.0
    for k, (p0, p1) in enumerate(seq):
        t += float(np.linalg.norm(p1 - p0)) / v_mps + v_mps / a_mps2
        if k + 1 < len(seq):
            t += float(np.linalg.norm(seq[k + 1][0] - p1)) / v_mps
    return t


def sweep_candidates(poly) -> list[float]:
    P = _ring(poly)
    d = np.roll(P, -1, axis=0) - P
    edge = [math.atan2(float(dy), float(dx)) % math.pi for dx, dy in d if math.hypot(dx, dy) > 1e-6]
    grid = [math.radians(a) for a in range(0, 180, 5)]
    out: list[float] = []
    for a in edge + grid:
        if all(abs(a - b) > 1e-6 for b in out):
            out.append(a)
    return out


def best_sweep_angle(poly, spacing_m: float, v_mps: float, holes=None, a_mps2: float = 2.0) -> tuple[float, float]:
    """返回 (θ*, 单机时间)；候选为各边方向与 0°, 5°, …, 175°。"""
    best = (0.0, math.inf)
    for th in sweep_candidates(poly):
        lanes, _ = lanes_for_angle(poly, spacing_m, th, holes)
        t = seq_time(boustrophedon(lanes), v_mps, a_mps2)
        if t < best[1] - 1e-9:
            best = (th, t)
    return best


def clip_lanes(lanes: list[list[Seg]], blocked: np.ndarray, geo: tuple[float, float, float],
               min_len_m: float = 6.0) -> list[list[Seg]]:
    """按阻塞栅格裁剪航段（`blocked[row, col]`，geo = (x0, y0, res)，第 0 行在南）。"""
    x0, y0, res = geo
    ny, nx = blocked.shape
    out: list[list[Seg]] = []
    for segs in lanes:
        new: list[Seg] = []
        for p0, p1 in segs:
            L = float(np.linalg.norm(p1 - p0))
            m = max(2, int(L / (res * 0.5)) + 1)
            ts = np.linspace(0.0, 1.0, m)
            pts = p0 + (p1 - p0) * ts[:, None]
            ix = np.clip(((pts[:, 0] - x0) / res).astype(np.int64), 0, nx - 1)
            iy = np.clip(((pts[:, 1] - y0) / res).astype(np.int64), 0, ny - 1)
            free = ~blocked[iy, ix]
            d = np.diff(np.r_[0, free.astype(np.int8), 0])
            st = np.flatnonzero(d == 1)
            en = np.flatnonzero(d == -1) - 1
            for s_, e_ in zip(st, en, strict=True):
                q0, q1 = pts[s_], pts[e_]
                if float(np.linalg.norm(q1 - q0)) >= min_len_m:
                    new.append((q0, q1))
        out.append(new)
    return out


def bcd_cells(lanes: list[list[Seg]], theta_rad: float) -> list[list[tuple[int, Seg]]]:
    """BCD-lite：航段延续某个开放胞，当且仅当它只与该胞末段重叠且该胞在新航带上只与它重叠（无分裂与合并事件）。"""
    Rm = _rot(-theta_rad)

    def interval(seg: Seg) -> tuple[float, float]:
        a, b = Rm @ seg[0], Rm @ seg[1]
        return min(a[0], b[0]), max(a[0], b[0])

    cells: list[list[tuple[int, Seg]]] = []
    open_: list[int] = []
    for li, segs in enumerate(lanes):
        iv = [interval(s) for s in segs]
        ov = {}
        for c in open_:
            ci = interval(cells[c][-1][1])
            ov[c] = [k for k, (a, b) in enumerate(iv) if min(b, ci[1]) - max(a, ci[0]) > 0]
        new_open: list[int] = []
        for k, s in enumerate(segs):
            cs = [c for c in open_ if k in ov[c]]
            if len(cs) == 1 and len(ov[cs[0]]) == 1:
                cells[cs[0]].append((li, s))
                new_open.append(cs[0])
            else:
                cells.append([(li, s)])
                new_open.append(len(cells) - 1)
        open_ = new_open
    return cells


def _cell_path(cell: list[tuple[int, Seg]], reverse_first: bool) -> list[Seg]:
    out: list[Seg] = []
    for j, (_li, (p0, p1)) in enumerate(cell):
        flip = (j % 2 == 1) ^ reverse_first
        out.append((p1, p0) if flip else (p0, p1))
    return out


def order_cells(cells: list[list[tuple[int, Seg]]], start: np.ndarray) -> list[Seg]:
    """贪心近邻：每个胞可从首航带或末航带、正向或反向进入（4 种）。"""
    remaining = list(range(len(cells)))
    pos = np.asarray(start, np.float64)[:2]
    seq: list[Seg] = []
    while remaining:
        best = None
        for c in remaining:
            for rev_lanes in (False, True):
                cl = cells[c][::-1] if rev_lanes else cells[c]
                for rev_first in (False, True):
                    path = _cell_path(cl, rev_first)
                    d = float(np.linalg.norm(path[0][0] - pos))
                    if best is None or d < best[0]:
                        best = (d, c, path)
        assert best is not None
        _, c, path = best
        seq.extend(path)
        pos = path[-1][1]
        remaining.remove(c)
    return seq
