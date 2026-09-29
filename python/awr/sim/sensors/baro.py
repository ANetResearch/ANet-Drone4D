"""气压计纯函数（M13-FR-035；M13 §6.5.7；D1 桩，V0.4 用于 HIL_SENSOR）。

`p_meas = p_true·(1 + sigma_p·z_p) + 2.7 Pa·n`，z_p 为 GM（τ = 3600 s，精确离散）；`p_true` 取 M07 THERMO `pressure_pa`
（传感器位置，ISA 含 isa_dT）；`alt_baro = 44330.77·(1 − (p/101325)^0.190263)`（QNH = 101325 Pa）。sigma_p 取 AirSim 缺省的 1/5
（3.65e-4，约 37 Pa、约 3 m），本文设定，V0.4 用 ULog 对标后冻结。只依赖 numpy。
"""

from __future__ import annotations

import numpy as np

from .gm import phi

__all__ = ["P0_PA", "alt_from_pressure", "baro_measure", "baro_step", "isa_pressure"]

P0_PA = 101325.0
SIGMA_P = 3.65e-4
TAU_S = 3600.0
WHITE_PA = 2.7


def isa_pressure(h_msl_m: np.ndarray | float, isa_dt_k: float = 0.0) -> np.ndarray:
    """ISA 对流层气压（测试与无 M07 时的回退；isa_dT 只改温度，此处按标准气压式）。"""
    h = np.asarray(h_msl_m, np.float64)
    return P0_PA * (1.0 - 2.25577e-5 * h) ** 5.25588


def alt_from_pressure(p_pa: np.ndarray | float) -> np.ndarray:
    return 44330.77 * (1.0 - (np.asarray(p_pa, np.float64) / P0_PA) ** 0.190263)


def baro_step(z: np.ndarray, dt_s: float, n: np.ndarray, tau_s: float = TAU_S) -> np.ndarray:
    ph = float(phi(dt_s, tau_s))
    return ph * np.asarray(z, np.float64) + np.sqrt(1.0 - ph * ph) * np.asarray(n, np.float64)


def baro_measure(p_true_pa: np.ndarray, z: np.ndarray, n_white: np.ndarray, *, sigma_p: float = SIGMA_P,
                 white_pa: float = WHITE_PA) -> np.ndarray:
    return np.asarray(p_true_pa, np.float64) * (1.0 + sigma_p * np.asarray(z, np.float64)) + white_pa * np.asarray(n_white, np.float64)
