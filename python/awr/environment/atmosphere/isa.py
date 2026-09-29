"""ISA 热力学（M07-FR-015；M07 §6.3.11；g06 §2.1；AWR-03 §5.5）。

h_msl = coordinate.anchor.hMslM + z；T_isa = 288.15 − 0.0065*h；T = T_isa + isa_dt_c；p = 101325*(T_isa/288.15)^5.25588；
rho = p / (287.053*T)。`rh` 为占位（V0.4 起供传感器使用）。
"""

from __future__ import annotations

import numpy as np

__all__ = ["isa", "isa_arr"]


def isa(h_msl_m: float, isa_dt_c: float) -> dict[str, float]:
    t_isa = 288.15 - 0.0065 * h_msl_m
    t = t_isa + isa_dt_c
    p = 101325.0 * (t_isa / 288.15) ** 5.25588
    return {"temperature_c": t - 273.15, "pressure_pa": p, "rho_kgm3": p / (287.053 * t)}


def isa_arr(h_msl_m: np.ndarray, isa_dt_c: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """-> (temperature_c, pressure_pa, rho_kgm3)。"""
    h = np.asarray(h_msl_m, np.float64)
    t_isa = 288.15 - 0.0065 * h
    t = t_isa + isa_dt_c
    p = 101325.0 * (t_isa / 288.15) ** 5.25588
    return t - 273.15, p, p / (287.053 * t)
