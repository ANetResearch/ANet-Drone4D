"""区域覆盖规划（M10 §6.5.12；FR-019、FR-048–FR-051；r26 §3.8、§3.9）。

    plan_coverage(aoi, holes, k, homes, sensor, altitude, speed):
      1. 高度：fly_over → z_fly = max(z_g + agl, P99.9(DSM∩AOI) + clearance)，h_eff = z_fly − P95(DSM∩AOI)；
         fixed_agl（ext）→ z_fly = z_g + agl，阻塞格 = DSM > z_fly − clearance；
         per_lane（ext，S4）→ 每条航带 z_lane = max(DSM 在 ±W/2 走廊内) + clearance
      2. 间距 s、触发间距 b；扫描角 θ* = argmin_θ T_single（候选：各边方向与 0°, 5°, …, 175°）
      3. 航带（凹多边形、洞）；4. fixed_agl 裁剪与 BCD-lite；5. 胞排序
      6. 代价均衡切分（段内切点）；7. Hungarian 分配到机库
纯算法：高度由调用方以回调给出（`height_fn(xy) -> z`、`ground_fn(xy) -> z`，world z，m），不依赖 awr.sim。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .lanes import (
    Seg,
    bcd_cells,
    best_sweep_angle,
    boustrophedon,
    clip_lanes,
    lanes_for_angle,
    order_cells,
    polygon_area,
    seq_time,
)
from .partition import assign_chunks, chunk_cost, split_balanced
from .sensor import DEFAULT_HFOV_DEG, DEFAULT_VFOV_DEG, swath

__all__ = ["CoveragePlan", "aoi_samples", "plan_coverage", "point_in_polygon", "predict_coverage"]

HeightFn = Callable[[np.ndarray], np.ndarray]


def point_in_polygon(xy: np.ndarray, poly, holes=None) -> np.ndarray:
    """偶奇规则（洞按同一规则扣除）。"""
    xy = np.asarray(xy, np.float64)
    inside = np.zeros(len(xy), bool)
    for ring in [poly, *(holes or [])]:
        R = np.asarray(ring, np.float64)[:, :2]
        if len(R) >= 2 and np.allclose(R[0], R[-1]):
            R = R[:-1]
        A, B = R, np.roll(R, -1, axis=0)
        x, y = xy[:, 0:1], xy[:, 1:2]
        cond = ((A[None, :, 1] <= y) & (B[None, :, 1] > y)) | ((B[None, :, 1] <= y) & (A[None, :, 1] > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = A[None, :, 0] + (y - A[None, :, 1]) * (B[None, :, 0] - A[None, :, 0]) / (B[None, :, 1] - A[None, :, 1])
        cross = cond & (xi > x)
        inside ^= (cross.sum(axis=1) % 2 == 1)
    return inside


def aoi_samples(poly, holes=None, max_cells: int = 65536) -> tuple[np.ndarray, float]:
    """AOI 内格心采样：res = max(2, sqrt(面积/max_cells))（M10 §6.3.5 CoverageGrid 口径）。"""
    P = np.asarray(poly, np.float64)[:, :2]
    area = max(polygon_area(P), 1.0)
    res = max(2.0, math.sqrt(area / max_cells))
    x0, y0 = P.min(0)
    x1, y1 = P.max(0)
    xs = np.arange(x0 + res / 2, x1, res)
    ys = np.arange(y0 + res / 2, y1, res)
    if xs.size == 0 or ys.size == 0:
        return P[:1].copy(), res
    X, Y = np.meshgrid(xs, ys)
    xy = np.c_[X.ravel(), Y.ravel()]
    m = point_in_polygon(xy, P, holes)
    return xy[m] if m.any() else P[:1].copy(), res


def predict_coverage(samples: np.ndarray, segs: list[Seg], width_m: float) -> float:
    """以航带为中线、宽 W 的走廊覆盖 AOI 格心的比例（预测覆盖率）。"""
    if len(samples) == 0 or not segs:
        return 0.0
    cov = np.zeros(len(samples), bool)
    half = width_m / 2.0
    for p0, p1 in segs:
        d = p1[:2] - p0[:2]
        L2 = float(d @ d)
        if L2 < 1e-12:
            continue
        t = np.clip(((samples - p0[:2]) @ d) / L2, 0.0, 1.0)
        q = p0[:2] + t[:, None] * d
        cov |= np.linalg.norm(samples - q, axis=1) <= half + 1e-6
    return float(cov.mean())


@dataclass
class CoveragePlan:
    theta_rad: float
    spacing_m: float
    trigger_m: float
    width_m: float
    z_fly_m: float
    h_eff_m: float
    mode: str
    lanes: list[list[Seg]]
    chunks: list[list[Seg]]                       # 每块的有序 2D 航段
    chunk_z: list[list[float]]                    # 每块各航段的巡航高度（world z）
    vehicle_chunk: np.ndarray                     # 机体 i → 块号
    vehicle_reversed: np.ndarray
    coverage_pred: float
    balance: float
    t_single_s: float
    stats: dict = field(default_factory=dict)

    def polyline_of(self, i: int) -> np.ndarray:
        """机体 i 的 3D 作业折线（航段首尾点，按作业顺序；段间直线衔接）。"""
        j = int(self.vehicle_chunk[i])
        if j < 0:
            return np.zeros((0, 3))
        segs, zs = self.chunks[j], self.chunk_z[j]
        pts = []
        order = range(len(segs) - 1, -1, -1) if self.vehicle_reversed[i] else range(len(segs))
        for k in order:
            p0, p1 = segs[k]
            if self.vehicle_reversed[i]:
                p0, p1 = p1, p0
            z = zs[k]
            pts += [[p0[0], p0[1], z], [p1[0], p1[1], z]]
        P = np.asarray(pts, np.float64)
        keep = np.r_[True, np.linalg.norm(np.diff(P, axis=0), axis=1) > 1e-6]
        return P[keep]


def _fly_over_alt(samples: np.ndarray, height_fn: HeightFn, ground_fn: HeightFn, agl_m: float,
                  clearance_m: float) -> tuple[float, float, float]:
    dsm = np.asarray(height_fn(samples), np.float64)
    gnd = np.asarray(ground_fn(samples), np.float64)
    z_g = float(np.median(gnd))
    z_fly = max(z_g + agl_m, float(np.percentile(dsm, 99.9)) + clearance_m)
    h_eff = z_fly - float(np.percentile(dsm, 95))
    return z_fly, h_eff, z_g


def _lane_alt(seg: Seg, half_w: float, height_fn: HeightFn, clearance_m: float, step_m: float = 4.0) -> float:
    p0, p1 = seg
    d = p1 - p0
    L = float(np.linalg.norm(d))
    if L < 1e-9:
        return float(height_fn(p0[None])[0]) + clearance_m
    u = d / L
    nrm = np.array([-u[1], u[0]])
    ts = np.linspace(0.0, 1.0, max(2, int(L / step_m) + 1))
    ws = np.linspace(-half_w, half_w, max(3, int(2 * half_w / step_m) + 1))
    pts = (p0[None, None, :] + ts[:, None, None] * d[None, None, :] + ws[None, :, None] * nrm[None, None, :]).reshape(-1, 2)
    return float(np.max(height_fn(pts))) + clearance_m


def plan_coverage(aoi, holes, k: int, homes: np.ndarray, sensor: dict, altitude: dict, speed_mps: float,
                  height_fn: HeightFn, ground_fn: HeightFn, sweep_angle_deg: float | str | None = None,
                  a_mps2: float = 2.0, min_lane_m: float = 6.0, blocked: tuple[np.ndarray, tuple] | None = None,
                  exact_split: bool = True) -> CoveragePlan:
    """blocked：fixed_agl 模式的阻塞栅格 (mask, (x0, y0, res))；缺省时按 height_fn 在 AOI 采样构造。"""
    P = np.asarray(aoi, np.float64)[:, :2]
    samples, res = aoi_samples(P, holes)
    mode = str((altitude or {}).get("mode", "fly_over"))
    agl = float((altitude or {}).get("agl_m", 60.0))
    clr = float((altitude or {}).get("clearance_m", 10.0))
    hfov = float(sensor.get("hfov_deg", DEFAULT_HFOV_DEG))
    vfov = float(sensor.get("vfov_deg", DEFAULT_VFOV_DEG))
    side = float(sensor.get("side_overlap", 0.7))
    front = float(sensor.get("front_overlap", 0.8))
    if mode == "fly_over":
        z_fly, h_eff, z_g = _fly_over_alt(samples, height_fn, ground_fn, agl, clr)
    else:
        z_g = float(np.median(np.asarray(ground_fn(samples), np.float64)))
        z_fly, h_eff = z_g + agl, agl
    sw = swath(h_eff, side_overlap=side, front_overlap=front, hfov_deg=hfov, vfov_deg=vfov,
               spacing_m=sensor.get("spacing_m"))
    if sweep_angle_deg is None or sweep_angle_deg == "auto":
        theta, _t = best_sweep_angle(P, sw.spacing_m, speed_mps, holes, a_mps2)
    else:
        theta = math.radians(float(sweep_angle_deg))
    lanes, spacing = lanes_for_angle(P, sw.spacing_m, theta, holes)
    if mode == "fixed_agl":
        if blocked is None:
            xs = np.arange(P[:, 0].min(), P[:, 0].max() + res, res)
            ys = np.arange(P[:, 1].min(), P[:, 1].max() + res, res)
            X, Y = np.meshgrid(xs + res / 2, ys + res / 2)
            z = np.asarray(height_fn(np.c_[X.ravel(), Y.ravel()]), np.float64).reshape(X.shape)
            mask = z > z_fly - clr
            blocked = (mask, (float(xs[0]), float(ys[0]), res))
        lanes = clip_lanes(lanes, blocked[0], blocked[1], min_lane_m)
        cells = bcd_cells(lanes, theta)
        start = np.asarray(homes, np.float64)[0, :2] if len(homes) else P[0]
        seq = order_cells(cells, start)
    else:
        seq = boustrophedon(lanes)
        # 入口靠近第一个机库时更省：比较两端
        if len(homes) and seq:
            h0 = np.asarray(homes, np.float64)[0, :2]
            if float(np.linalg.norm(seq[-1][1] - h0)) < float(np.linalg.norm(seq[0][0] - h0)):
                seq = [(b, a) for a, b in seq[::-1]]
    t_single = seq_time(seq, speed_mps, a_mps2)
    k = max(1, min(int(k), max(1, len(seq) * 4)))
    chunks = split_balanced(seq, k, speed_mps, a_mps2, exact=exact_split) if seq else []
    half_w = sw.width_m / 2.0
    chunk_z: list[list[float]] = []
    for ch in chunks:
        if mode == "per_lane":
            chunk_z.append([_lane_alt(s, half_w, height_fn, clr) for s in ch])
        else:
            chunk_z.append([z_fly] * len(ch))
    homes_a = np.asarray(homes, np.float64).reshape(-1, 3)[:, :2] if len(homes) else np.zeros((0, 2))
    if len(homes_a) and chunks:
        asg = assign_chunks(chunks, homes_a, speed_mps)
        v_chunk, v_rev = asg.chunk_of, asg.reversed_
    else:
        v_chunk, v_rev = np.zeros(0, np.int64), np.zeros(0, bool)
    costs = np.array([chunk_cost(c, speed_mps, a_mps2) for c in chunks]) if chunks else np.zeros(1)
    balance = float(costs.max() / max(costs.mean(), 1e-9)) if costs.size and costs.mean() > 0 else 1.0
    cov = predict_coverage(samples, seq, sw.width_m)
    stats = {"n_lanes": int(sum(1 for L in lanes if L)), "n_segments": len(seq), "n_chunks": len(chunks),
             "theta_deg": round(math.degrees(theta) % 180.0, 2), "spacing_m": round(spacing, 3),
             "trigger_m": round(sw.trigger_m, 3), "z_fly_m": round(z_fly, 2), "h_eff_m": round(h_eff, 2),
             "z_ground_m": round(z_g, 2), "len_m": round(sum(float(np.linalg.norm(b - a)) for a, b in seq), 1),
             "coverage_pred": round(cov, 4), "balance": round(balance, 4), "res_m": round(res, 3)}
    return CoveragePlan(theta, spacing, sw.trigger_m, sw.width_m, z_fly, h_eff, mode, lanes, chunks, chunk_z, v_chunk,
                        v_rev, cov, balance, t_single, stats)
