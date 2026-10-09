"""按感知等级反推的多机扫描规划（AWR-04 §8.3 的子集；M20 `area_scan_gsd`）。

给定 AOI、目标类别与所需等级（缺省行人 R 级、P ≥ 0.9）、成像传感器与焦距范围、航高上限 `H_cap`、净空 `c`、
离轴上限 `θ_max`，以及规划所用的**能见度**（消光系数 sigma）与**单架次续航**：

1. 对每个候选航高 H（AGL）：障碍格为 `DSM − DTM > H − c`，其占比不超过 `obstacle_max`；
   条带边缘的斜距 `R_e = H / cos θ_e`，对比度 `C_app = |C0|·τ(R_e)`，`τ = exp(−sigma·R_e)`；所需焦距
   `f ≥ N_req·R_e·p'/(d_c·k_c)`（`d_c` 取天底关键维度，最不利），并满足 GSD 上限；`f > f_max` 或 `k_c = 0` 时 H 不可行。
2. 由 f 得 GSD `g = H·p'/f`、条带宽 `w = min(W_out·g, 2·H·tan θ_max)`、间距 `d = (1 − s)·w`；按最优扫描角生成蛇形航带、
   剔除障碍格；单机时间 `T_1` 最小者为所选航高。
3. 架数：`T_max = min(t_cap, 0.8·t_sortie − 2·t_transit)`；取 makespan ≤ T_max 的最小 n（代价均衡切分 + Hungarian）。

能见度与续航由调用方传入：感知天气的规划器传入当前环境的 sigma 与按当前风计算的续航；不感知天气的基线传入晴天值。
`evaluate_scan` 用**真实**环境（τ、视线、续航）评估一份计划实际能否在所需等级上捕获目标。纯算法，不依赖 awr.sim 运行期。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from awr.sim.perception.levels import (
    EoSensor,
    Target,
    apparent_contrast,
    critical_dim,
    k_contrast,
    n_required,
    perception_level,
)

from .lanes import Seg, bcd_cells, best_sweep_angle, clip_lanes, lanes_for_angle, order_cells, seq_time
from .partition import assign_chunks, chunk_cost, split_balanced
from .plan import aoi_samples, predict_coverage

__all__ = ["GsdScanPlan", "ScanRequest", "evaluate_scan", "plan_gsd_scan", "sortie_endurance_s"]

HeightFn = Callable[[np.ndarray], np.ndarray]
PowerFn = Callable[[np.ndarray, np.ndarray], np.ndarray]  # (v_air_mps, v_z_mps) -> W


@dataclass(frozen=True)
class ScanRequest:
    aoi: np.ndarray
    level: str = "R"
    target: Target = field(default_factory=Target)
    p_req: float = 0.9
    gsd_cap_m: float | None = None
    side_overlap: float = 0.1
    theta_max_deg: float = 15.0
    h_cap_agl_m: float = 120.0
    h_min_agl_m: float = 20.0
    clearance_m: float = 20.0
    obstacle_max: float = 0.05
    speed_mps: float = 8.0
    t_cap_s: float = 600.0
    k_max: int = 32
    h_step_m: float = 2.0


@dataclass
class GsdScanPlan:
    feasible: bool
    why: str
    h_agl_m: float = math.nan
    z_fly_m: float = math.nan
    f_mm: float = math.nan
    gsd_m: float = math.nan
    width_m: float = math.nan
    spacing_m: float = math.nan
    theta_rad: float = 0.0
    k_c_plan: float = math.nan
    n_eff_edge_plan: float = math.nan
    obstacle_frac: float = math.nan
    seq: list[Seg] = field(default_factory=list)
    chunks: list[list[Seg]] = field(default_factory=list)
    vehicle_chunk: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    vehicle_reversed: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    n_drones: int = 0
    t_single_s: float = math.nan
    makespan_s: float = math.nan
    t_max_s: float = math.nan
    t_transit_s: float = math.nan
    length_m: float = 0.0
    coverage_pred: float = math.nan
    candidates: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        keys = ("feasible", "why", "h_agl_m", "z_fly_m", "f_mm", "gsd_m", "width_m", "spacing_m", "k_c_plan",
                "n_eff_edge_plan", "obstacle_frac", "n_drones", "t_single_s", "makespan_s", "t_max_s", "t_transit_s",
                "length_m", "coverage_pred")
        out = {}
        for k in keys:
            v = getattr(self, k)
            out[k] = round(float(v), 4) if isinstance(v, (float, np.floating)) else v
        return out


def sortie_endurance_s(power: PowerFn, e_use_wh: float, v_mps: float, wind_xy: np.ndarray | tuple[float, float],
                       reserve: float = 0.2, n_heading: int = 72) -> float:
    """以速度 v 巡航、航向均匀分布时的单架次可用续航（s）：`(1 − reserve)·E_use / mean_ψ P(|v·u_ψ − w|)`。"""
    w = np.asarray(wind_xy, np.float64)[:2]
    psi = np.linspace(0.0, 2.0 * math.pi, n_heading, endpoint=False)
    U = np.c_[np.cos(psi), np.sin(psi)] * float(v_mps)
    v_air = np.hypot(U[:, 0] - w[0], U[:, 1] - w[1])
    p = float(np.mean(np.asarray(power(v_air, np.zeros_like(v_air)), np.float64)))
    return (1.0 - reserve) * float(e_use_wh) * 3600.0 / max(p, 1e-6)


def _required_f(req: ScanRequest, s: EoSensor, h: float, theta_e: float, sigma: float, d_c: float,
                n_req: float) -> tuple[float, float]:
    r_e = h / max(math.cos(theta_e), 1e-6)
    kc = float(k_contrast(apparent_contrast(req.target, math.exp(-sigma * r_e))))
    if kc <= 0.0:
        return math.inf, kc
    return n_req * r_e * s.pitch_m / (d_c * kc) * 1e3, kc


def _config_for_h(req: ScanRequest, s: EoSensor, h: float, sigma: float, d_c: float, n_req: float) -> dict | None:
    th_max = math.radians(req.theta_max_deg)
    # 边缘离轴角取决于条带宽，条带宽又取决于 f：先按 θ_max 求 f（保守），再按实际 θ_e 复核一次
    f, kc = _required_f(req, s, h, th_max, sigma, d_c, n_req)
    if not math.isfinite(f):
        return None
    if req.gsd_cap_m:
        f = max(f, h * s.pitch_m / req.gsd_cap_m * 1e3)
    f = max(f, s.f_min_mm)
    if f > s.f_max_mm + 1e-9:
        return None
    g = h * s.pitch_m / (f * 1e-3)
    w_img = s.w_out_px * g
    w = min(w_img, 2.0 * h * math.tan(th_max))
    theta_e = math.atan(w / (2.0 * h))
    f2, kc2 = _required_f(req, s, h, theta_e, sigma, d_c, n_req)
    if f2 < f - 1e-6 and f2 >= s.f_min_mm:
        f, kc = max(f2, f if req.gsd_cap_m is None else h * s.pitch_m / req.gsd_cap_m * 1e3), kc2
        g = h * s.pitch_m / (f * 1e-3)
        w = min(s.w_out_px * g, 2.0 * h * math.tan(th_max))
        theta_e = math.atan(w / (2.0 * h))
    r_e = h / math.cos(theta_e)
    n_edge = d_c * f * 1e-3 / (r_e * s.pitch_m) * float(k_contrast(apparent_contrast(req.target, math.exp(-sigma * r_e))))
    return {"h": h, "f": f, "g": g, "w": w, "theta_e": theta_e, "k_c": kc, "n_eff_edge": n_edge}


def plan_gsd_scan(req: ScanRequest, sensor: EoSensor, height_fn: HeightFn, ground_fn: HeightFn, *, sigma_ext_per_m: float,
                  t_sortie_s: float, homes: np.ndarray, a_mps2: float = 2.0) -> GsdScanPlan:
    P = np.asarray(req.aoi, np.float64)[:, :2]
    samples, res = aoi_samples(P)
    dsm = np.asarray(height_fn(samples), np.float64)
    dtm = np.asarray(ground_fn(samples), np.float64)
    z_g = float(np.median(dtm))
    agl_obj = dsm - dtm
    d_c = float(critical_dim(req.target, math.pi / 2))
    n_req = n_required(req.level, req.p_req)
    homes = np.asarray(homes, np.float64).reshape(-1, 3)
    centroid = P.mean(0)
    t_transit = float(np.min(np.linalg.norm(homes[:, :2] - centroid, axis=1))) / req.speed_mps if len(homes) else 0.0
    t_max = min(req.t_cap_s, 0.8 * t_sortie_s - 2.0 * t_transit)
    best: tuple[float, dict] | None = None
    cands: list[dict] = []
    for h in np.arange(req.h_min_agl_m, req.h_cap_agl_m + 1e-9, req.h_step_m):
        obst = agl_obj > h - req.clearance_m
        of = float(obst.mean())
        cfg = _config_for_h(req, sensor, float(h), sigma_ext_per_m, d_c, n_req)
        row = {"h": float(h), "obstacle_frac": of, "feasible": cfg is not None and of <= req.obstacle_max}
        if cfg is not None:
            row.update({k: float(v) for k, v in cfg.items()})
        if row["feasible"]:
            spacing = (1.0 - req.side_overlap) * cfg["w"]
            theta, t1 = best_sweep_angle(P, spacing, req.speed_mps, None, a_mps2)
            row.update({"spacing": spacing, "theta": theta, "t_single": t1})
            if best is None or t1 < best[0] - 1e-6:
                best = (t1, row)
        cands.append(row)
    if best is None:
        return GsdScanPlan(False, "height", candidates=cands)
    c = best[1]
    h = c["h"]
    z_fly = z_g + h
    lanes, spacing = lanes_for_angle(P, c["spacing"], c["theta"], None)
    xs = np.arange(P[:, 0].min(), P[:, 0].max() + res, res)
    ys = np.arange(P[:, 1].min(), P[:, 1].max() + res, res)
    X, Y = np.meshgrid(xs + res / 2, ys + res / 2)
    z = np.asarray(height_fn(np.c_[X.ravel(), Y.ravel()]), np.float64).reshape(X.shape)
    blocked = z > z_fly - req.clearance_m
    lanes = clip_lanes(lanes, blocked, (float(xs[0]), float(ys[0]), res), 6.0)
    cells = bcd_cells(lanes, c["theta"])
    start = homes[0, :2] if len(homes) else P[0]
    seq = order_cells(cells, start)
    t1 = seq_time(seq, req.speed_mps, a_mps2)
    free = ~(agl_obj > h - req.clearance_m)
    cov = predict_coverage(samples[free], seq, c["w"]) if free.any() else 0.0
    plan = GsdScanPlan(True, "", h_agl_m=h, z_fly_m=z_fly, f_mm=c["f"], gsd_m=c["g"], width_m=c["w"], spacing_m=spacing,
                       theta_rad=c["theta"], k_c_plan=c["k_c"], n_eff_edge_plan=c["n_eff_edge"],
                       obstacle_frac=c["obstacle_frac"], seq=seq, t_single_s=t1, t_max_s=t_max, t_transit_s=t_transit,
                       length_m=float(sum(np.linalg.norm(b - a) for a, b in seq)), coverage_pred=cov, candidates=cands)
    if t_max <= 0:
        plan.feasible, plan.why = False, "endurance"
        return plan
    for k in range(1, req.k_max + 1):
        chunks = split_balanced(seq, k, req.speed_mps, a_mps2)
        mk = max(chunk_cost(ch, req.speed_mps, a_mps2) for ch in chunks) + t_transit
        if mk <= t_max or k == req.k_max:
            plan.chunks, plan.makespan_s, plan.n_drones = chunks, mk, k
            if mk > t_max:
                plan.feasible, plan.why = False, "fleet_short"
            break
    hk = np.repeat(homes[:1], plan.n_drones, axis=0) if len(homes) else np.zeros((plan.n_drones, 3))
    asg = assign_chunks(plan.chunks, hk[:, :2], req.speed_mps)
    plan.vehicle_chunk, plan.vehicle_reversed = asg.chunk_of, asg.reversed_
    return plan


def evaluate_scan(plan: GsdScanPlan, req: ScanRequest, sensor: EoSensor, targets_xyz: np.ndarray, *,
                  optical_depth: Callable[[np.ndarray, np.ndarray], np.ndarray], los: Callable[[np.ndarray, np.ndarray], np.ndarray]
                  | None, t_sortie_true_s: float, a_mps2: float = 2.0) -> dict:
    """用真实环境评估：每个目标取最近的、离轴角不超过条带半宽的条带过点为观测站；该过点须在本架次真实续航内飞到
    （按块内累计用时），否则视为未观测。返回逐目标等级与汇总（捕获率、观测率、能量中断的块数）。"""
    T = np.asarray(targets_xyz, np.float64).reshape(-1, 3)
    n = len(T)
    out = {"captured": np.zeros(n, bool), "observed": np.zeros(n, bool), "level": np.zeros(n, np.int8),
           "n_eff": np.zeros(n), "range_m": np.full(n, np.nan), "limiting": np.zeros(n, np.int8)}
    if (not plan.feasible and plan.why == "height") or not plan.chunks:
        out["summary"] = {"capture_rate": 0.0, "observed_rate": 0.0, "chunks_cut": 0, "n_targets": n}
        return out
    budget = (0.8 * t_sortie_true_s) - plan.t_transit_s  # 块内可用时间（真实续航扣除往返转场与 20% 余量）
    half = plan.width_m / 2.0
    best_d = np.full(n, np.inf)
    best_p = np.zeros((n, 3))
    cut = 0
    for ch in plan.chunks:
        t_acc = 0.0
        truncated = False
        for p0, p1 in ch:
            L = float(np.linalg.norm(p1 - p0))
            t_seg = L / req.speed_mps + req.speed_mps / a_mps2
            if t_acc >= budget:
                truncated = True
                break
            frac = min(1.0, (budget - t_acc) / t_seg) if t_seg > 0 else 1.0
            q1 = p0 + (p1 - p0) * frac
            d = q1 - p0
            L2 = float(d @ d)
            if L2 > 1e-12:
                tt = np.clip(((T[:, :2] - p0) @ d) / L2, 0.0, 1.0)
                q = p0 + tt[:, None] * d
                dist = np.linalg.norm(T[:, :2] - q, axis=1)
                better = dist < best_d
                best_d = np.where(better, dist, best_d)
                best_p[better, :2] = q[better]
            if frac < 1.0:
                truncated = True
                break
            t_acc += t_seg
        cut += int(truncated)
    best_p[:, 2] = plan.z_fly_m
    obs = best_d <= half + 1e-6
    out["observed"] = obs
    if obs.any():
        cam = best_p[obs]
        tg = T[obs]
        dv = tg - cam
        r = np.linalg.norm(dv, axis=1)
        elev = np.arcsin(np.clip(-dv[:, 2] / np.maximum(r, 1e-6), -1.0, 1.0))
        tau = np.exp(-np.maximum(np.asarray(optical_depth(cam, tg), np.float64), 0.0))
        kl = np.asarray(los(cam, tg), np.float64) if los is not None else 1.0
        res = perception_level(req.target, sensor, r, elev, plan.f_mm, tau, kl, p_req=req.p_req)
        need = {"D": 1, "R": 2, "I": 3}[req.level]
        out["level"][obs] = res["level"]
        out["n_eff"][obs] = res["n_eff"]
        out["range_m"][obs] = r
        out["limiting"][obs] = res["limiting"]
        out["captured"][obs] = res["level"] >= need
    out["summary"] = {"capture_rate": float(out["captured"].mean()) if n else 0.0,
                      "observed_rate": float(obs.mean()) if n else 0.0, "chunks_cut": cut, "n_targets": n}
    return out
