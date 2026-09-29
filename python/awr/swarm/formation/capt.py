"""CAPT 集结与变形（M10-FR-044；M10 §6.5.11；r26 §3.6；Turpin、Michael、Kumar 2014）。

- 分配：`C_ij = ‖P_i − G_j‖²`，Hungarian 求解；
- 同步插值：全员共用五次 smoothstep `s(t) = 10t³ − 15t⁴ + 6t⁵`，`T = max(4 s, max_i‖G_{a_i} − P_i‖/(0.6·v_max))`；
- 保证条件：起点之间与终点之间的间距都 > 2√2·R_safe 时轨迹两两不交；
- 规划时按采样求预测最小间距 `d_min`：`d_min < min_sep_m` 时改为三段式错层变形（先竖直到 `z + rank_i·dz_r`，
  再各层同步水平插值，最后竖直回到同层；`dz_r = max(layer_dz_m, sqrt(min_sep_m² − d_min²) + 1 m)`，rank 两两不同），
  错层方案按采样复核仍不满足时判不可行（125 FORMATION_INFEASIBLE）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..allocation.hungarian import hungarian

__all__ = ["CaptPlan", "capt_assign", "capt_duration", "capt_plan", "capt_positions", "min_sep_linear",
           "min_sep_samples", "smoothstep5", "smoothstep5_d"]

T_MIN_S = 4.0
V_FRAC = 0.6


def capt_assign(P: np.ndarray, G: np.ndarray) -> np.ndarray:
    """成员 i（P 第 i 行）→ 槽位 a[i]（G 的行号）；代价为平方距离。"""
    P = np.asarray(P, np.float64)
    G = np.asarray(G, np.float64)
    C = ((P[:, None, :] - G[None, :, :]) ** 2).sum(-1)
    return hungarian(C)


def capt_duration(P: np.ndarray, G: np.ndarray, v_max_mps: float, t_min_s: float = T_MIN_S,
                  v_frac: float = V_FRAC) -> float:
    """G 已按分配排好序（第 i 行为成员 i 的目标）。"""
    d = np.linalg.norm(np.asarray(G, np.float64) - np.asarray(P, np.float64), axis=1)
    dmax = float(d.max()) if d.size else 0.0
    return max(float(t_min_s), dmax / (v_frac * float(v_max_mps)))


def smoothstep5(t):
    t = np.clip(t, 0.0, 1.0)
    return t * t * t * (10.0 - 15.0 * t + 6.0 * t * t)


def smoothstep5_d(t):
    t = np.clip(t, 0.0, 1.0)
    return 30.0 * t * t * (1.0 - t) * (1.0 - t)


def min_sep_linear(P: np.ndarray, G: np.ndarray, n_t: int = 200) -> float:
    """全员沿直线 P→G 同步插值（同一 s(t)）期间的最小两两距离。"""
    P = np.asarray(P, np.float64)
    G = np.asarray(G, np.float64)
    if len(P) < 2:
        return math.inf
    best = math.inf
    iu = np.triu_indices(len(P), 1)
    for s in np.linspace(0.0, 1.0, n_t):
        X = P + (G - P) * s
        D = np.linalg.norm(X[:, None, :] - X[None, :, :], axis=-1)[iu]
        best = min(best, float(D.min()))
    return best


def min_sep_samples(X: np.ndarray) -> float:
    """X (n_t, n, 3) 各时刻位置：最小两两距离。"""
    X = np.asarray(X, np.float64)
    n = X.shape[1]
    if n < 2:
        return math.inf
    iu = np.triu_indices(n, 1)
    D = np.linalg.norm(X[:, :, None, :] - X[:, None, :, :], axis=-1)[:, iu[0], iu[1]]
    return float(D.min())


@dataclass
class CaptPlan:
    assign: np.ndarray            # 成员 i → 槽位 assign[i]
    T_s: float                    # 水平插值时长
    d_min_m: float                # 直接插值的预测最小间距
    staggered: bool = False       # 是否三段式错层
    dz_m: np.ndarray = field(default_factory=lambda: np.zeros(0))   # 每成员错层高度（rank_i·dz_r）
    t_vert_s: float = 0.0         # 错层竖直段时长（上、下各一段）
    d_min_final_m: float = math.inf
    feasible: bool = True
    reason: str | None = None

    @property
    def total_s(self) -> float:
        return self.T_s + 2.0 * self.t_vert_s

    def to_json(self) -> dict:
        return {"assign": [int(x) for x in self.assign], "T_s": round(self.T_s, 3), "d_min_m": round(self.d_min_m, 3),
                "staggered": self.staggered, "dz_m": [round(float(x), 3) for x in self.dz_m],
                "t_vert_s": round(self.t_vert_s, 3), "d_min_final_m": round(self.d_min_final_m, 3),
                "feasible": self.feasible, "reason": self.reason}


def capt_positions(P: np.ndarray, Gs: np.ndarray, plan: CaptPlan, t_s: np.ndarray) -> np.ndarray:
    """按计划在时刻 t_s（(k,)）的成员位置 (k, n, 3)；Gs 为按分配排好序的目标（第 i 行为成员 i 的槽位）。"""
    P = np.asarray(P, np.float64)
    Gs = np.asarray(Gs, np.float64)
    t = np.atleast_1d(np.asarray(t_s, np.float64))
    out = np.empty((t.size, len(P), 3))
    if not plan.staggered:
        s = smoothstep5(t / max(plan.T_s, 1e-9))
        out[:] = P[None] + (Gs - P)[None] * s[:, None, None]
        return out
    tv, T = plan.t_vert_s, plan.T_s
    dz = plan.dz_m[None, :]
    for k, tk in enumerate(t):
        if tk <= tv:
            f = float(smoothstep5(tk / max(tv, 1e-9)))
            X = P.copy()
            X[:, 2] += (dz * f)[0]
        elif tk <= tv + T:
            f = float(smoothstep5((tk - tv) / max(T, 1e-9)))
            X = P + (Gs - P) * f
            X[:, 2] = P[:, 2] + (Gs[:, 2] - P[:, 2]) * f + dz[0]
        else:
            f = float(smoothstep5((tk - tv - T) / max(tv, 1e-9)))
            X = Gs.copy()
            X[:, 2] += (dz * (1.0 - f))[0]
        out[k] = X
    return out


def capt_plan(P: np.ndarray, G: np.ndarray, v_max_mps: float, *, min_sep_m: float = 10.0, layer_dz_m: float = 4.0,
              vz_mps: float = 1.5, rank: np.ndarray | None = None, n_t: int = 200) -> CaptPlan:
    """CAPT 变形规划：分配、时长、预测最小间距；不足时三段式错层并按采样复核（FR-044）。"""
    P = np.asarray(P, np.float64)
    G = np.asarray(G, np.float64)
    a = capt_assign(P, G)
    Gs = G[a]
    T = capt_duration(P, Gs, v_max_mps)
    d_min = min_sep_linear(P, Gs, n_t)
    plan = CaptPlan(a, T, d_min, d_min_final_m=d_min)
    if d_min >= min_sep_m:
        return plan
    n = len(P)
    dz_r = max(float(layer_dz_m), math.sqrt(max(min_sep_m ** 2 - d_min ** 2, 0.0)) + 1.0)
    rk = np.arange(n) if rank is None else np.asarray(rank, np.int64)
    if len(set(rk.tolist())) != n:
        raise ValueError("rank must be pairwise distinct")
    order = np.argsort(np.argsort(rk))
    dz = order.astype(np.float64) * dz_r
    t_vert = max(2.0, float(dz.max()) / max(vz_mps, 0.1) * 1.875) if dz.max() > 0 else 0.0   # smoothstep 峰值速度 = 1.875·Δ/T
    plan.staggered, plan.dz_m, plan.t_vert_s = True, dz, t_vert
    tt = np.linspace(0.0, plan.total_s, max(n_t * 3, 60))
    d_fin = min_sep_samples(capt_positions(P, Gs, plan, tt))
    plan.d_min_final_m = d_fin
    if d_fin < min_sep_m:
        plan.feasible = False
        plan.reason = "FORMATION_INFEASIBLE"
    return plan
