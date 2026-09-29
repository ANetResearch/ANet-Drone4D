"""归一化一阶 Gauss-Markov 的精确离散化（M13 §6.5.5、§6.5.6；GNSS、IMU 偏置、气压计、电量共用）。

状态以归一化量 z ~ N(0, 1) 存储：`z ← φ·z + √(1 − φ²)·n`，φ = e^{−Δt/τ}；物理量 = sigma·z。与步长无关（统计量对任意 Δt 一致），
修正了 Pegasus GPS 偏置更新的衰减项未乘 dt 的缺陷（M13 §1.3）。只依赖 numpy。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["phi", "sigma_from_rw", "step", "step_inplace"]


def phi(dt_s: float | np.ndarray, tau_s: float | np.ndarray) -> np.ndarray:
    return np.exp(-np.asarray(dt_s, np.float64) / np.asarray(tau_s, np.float64))


def sigma_from_rw(rw: float, tau_s: float) -> float:
    """随机游走强度 RW 与 τ 给出连续 GM 的稳态标准差 sigma_b = RW·√(τ/2)。"""
    return float(rw) * math.sqrt(float(tau_s) / 2.0)


def step(z: np.ndarray, ph: np.ndarray | float, n: np.ndarray) -> np.ndarray:
    ph = np.asarray(ph, np.float64)
    return ph * z + np.sqrt(1.0 - ph * ph) * n


def step_inplace(z: np.ndarray, ph: np.ndarray | float, n: np.ndarray) -> None:
    ph = np.asarray(ph, np.float64)
    z *= ph
    z += np.sqrt(1.0 - ph * ph) * n
