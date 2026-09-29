"""统一打分函数（M10-FR-059，D1 桩，V0.6 实现；n03 §3.9）。

    S(path) = Σ_j value_j·e^{−λ_j·(t_start_j − t_open_j)} − fuel·len
    t_start = max(t_open, t_arrive)；可行：t_start ≤ t_close − duration、兼容矩阵与电量可行
    出价 bid = max_pos[S(path ⊕ j) − S(path)]（SSI 拍卖；V1.0 CBBA 改为边际增益以满足 DMG）

D1 只交付接口与参考实现（用于单测与 M14 对接评审），不进入任何运行路径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["TaskWindow", "marginal_bid", "path_score"]


@dataclass(frozen=True)
class TaskWindow:
    value: float
    lam: float
    t_open_s: float
    t_close_s: float
    duration_s: float


def path_score(tasks: list[TaskWindow], arrive_s: list[float], fuel_per_m: float, length_m: float) -> float:
    """按给定到达时刻求 S(path)；任一任务时间窗不可行时返回 −inf。"""
    s = 0.0
    for w, ta in zip(tasks, arrive_s, strict=True):
        t_start = max(w.t_open_s, ta)
        if t_start > w.t_close_s - w.duration_s:
            return -math.inf
        s += w.value * math.exp(-w.lam * (t_start - w.t_open_s))
    return s - fuel_per_m * length_m


def marginal_bid(score_with: float, score_without: float) -> float:
    return max(0.0, score_with - score_without) if math.isfinite(score_with) else 0.0
