"""独立参考实现（M19-AC-003）：Johnson 三级判据与 TTPF，只用 math，不导入 awr.sim.perception。

TTPF：P(N) = (N/N50)^E / (1 + (N/N50)^E)，E = 2.7 + 0.7·N/N50（NVESD 目标传递概率函数）；N50 以像素计（每周期 2 像素）：
D 1.0、R 4.0、I 6.4 周期；P = 0.9 时 N/N50 = 1.75，即 D 3.5、R 14、I 22.4 px。
"""

from __future__ import annotations

import math

N50_CYCLES = {"D": 1.0, "R": 4.0, "I": 6.4}


def n50_px(level: str) -> float:
    return 2.0 * N50_CYCLES[level]


def ttpf(n: float, n50: float) -> float:
    x = max(n, 0.0) / n50
    if x == 0.0:
        return 0.0
    lp = (2.7 + 0.7 * x) * math.log(x)
    if lp > 700:
        return 1.0
    xe = math.exp(lp)
    return xe / (1.0 + xe)


def ratio_at(p: float) -> float:
    """解 P(x) = p 的 x（二分）。"""
    lo, hi = 1e-6, 100.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if ttpf(mid, 1.0) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def pixels(d_c_m: float, f_mm: float, r_m: float, p_um: float) -> float:
    return d_c_m * f_mm / (r_m * p_um * 1e-3)


def range_for(d_c_m: float, f_mm: float, n_px: float, p_um: float) -> float:
    return d_c_m * f_mm / (n_px * p_um * 1e-3)
