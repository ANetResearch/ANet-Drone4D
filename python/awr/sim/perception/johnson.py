"""Johnson 准则与目标传递概率函数 TTPF（M19-FR-003；ADR-100；AWR-04 §6.2）。

`P(N) = x^E/(1 + x^E)`，`x = N/N50`（N50 以像素计，每周期 2 像素），`E = 2.7 + 0.7·x`；P = 0.9 时 x = 1.75。三级阈值（P ≥ 0.9）：
D 3.5、R 14、I 22.4 px。`N_req` 的来源优先级见 `n_req_for`（M19-FR-015）。
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .params import LEVEL_INDEX, LEVEL_NAMES, LEVELS, N50_PX, N_P90

__all__ = ["level_name", "level_of", "level_req_of", "n_req_for", "ttpf"]

_E0 = float(LEVELS["ttpf"]["e0"])
_E1 = float(LEVELS["ttpf"]["e1"])


def ttpf(n: float | NDArray, n50_px: float) -> float | NDArray:
    """目标传递概率函数（标量或数组）。"""
    x = np.maximum(np.asarray(n, dtype=float), 0.0) / float(n50_px)
    e = _E0 + _E1 * x
    with np.errstate(over="ignore", invalid="ignore"):
        xe = np.power(x, e)
        out = np.where(np.isinf(xe), 1.0, xe / (1.0 + xe))
    return float(out) if np.ndim(out) == 0 else out


def n_req_for(level_req: str | None = None, n_req_px: float | None = None, *, task_level: str | None = None,
              task_n_px: float | None = None) -> float:
    """所需像素（M19-FR-015）：任务覆盖（M20 `set_requirement`）> 识别物 `n_req_px` > 识别物 `level_req` 的 P ≥ 0.9 像素 > 缺省 R。"""
    if task_n_px is not None:
        return float(task_n_px)
    if task_level is not None:
        return N_P90[task_level]
    if n_req_px is not None:
        return float(n_req_px)
    return N_P90[level_req or "R"]


def level_req_of(n_req: float, fallback: str = "R") -> str:
    """所需像素对应的等级名（恰为某级阈值时取该级；否则取不超过它的最高级，用于 TTPF 的 N50）。"""
    for k, v in N_P90.items():
        if abs(v - n_req) < 1e-9:
            return k
    best = None
    for k in ("D", "R", "I"):
        if n_req >= N_P90[k]:
            best = k
    return best or fallback


def level_of(n_eff: float | NDArray) -> int | NDArray:
    """有效像素 → 等级（0 NONE、1 D、2 R、3 I）。"""
    a = np.asarray(n_eff, float)
    out = (a >= N_P90["D"]).astype(np.int8) + (a >= N_P90["R"]) + (a >= N_P90["I"])
    return int(out) if np.ndim(out) == 0 else out.astype(np.int8)


def level_name(level: int) -> str:
    return LEVEL_NAMES[int(level)]


def level_index(name: str) -> int:
    return LEVEL_INDEX[name]


N50_OF = N50_PX
