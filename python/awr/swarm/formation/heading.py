"""编队航向滤波（M10-FR-043；M10 §6.5.11；r26 §3.4）：二阶临界阻尼，转动率钳到 w_fmax。

    acc_f = wrap(ψA − ψ_f)/τψ² − 2·w_f/τψ；w_f ← clip(w_f + acc_f·dt, ±w_fmax)；ψ_f ← ψ_f + w_f·dt
    w_fmax = a_max / (V + max|r_i|·|ω|)，τψ ≥ 1 s

跟踪核（`awr.sim.planning.kernels_track`）内联同一公式；本函数为参考实现与单测对照。
"""

from __future__ import annotations

import math

__all__ = ["heading_filter_step", "w_fmax", "wrap_pi"]


def wrap_pi(a: float) -> float:
    return (a + math.pi) % (2.0 * math.pi) - math.pi


def w_fmax(a_max_mps2: float, v_mps: float, r_max_m: float, w_rad_s: float) -> float:
    return float(a_max_mps2) / max(float(v_mps) + float(r_max_m) * abs(float(w_rad_s)), 1e-6)


def heading_filter_step(psi_f: float, w_f: float, psi_a: float, dt: float, tau_psi_s: float = 2.0,
                        w_max: float = 1.0) -> tuple[float, float]:
    tau = max(float(tau_psi_s), 1.0)
    alpha = wrap_pi(psi_a - psi_f) / (tau * tau) - 2.0 * w_f / tau
    w_f = min(max(w_f + alpha * dt, -w_max), w_max)
    return wrap_pi(psi_f + w_f * dt), w_f
