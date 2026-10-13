"""LWIR 热背景：气温日变化、地表温度与发射率、8–14 µm 等效天空温度、昼夜热交叉（AWR-04 §6.2；ADR-101；M18-M19 PRD §6.11）。

本文设定的工程近似（参数与依据见 docs/impl/D2-WP-09-报告.md §2.4）：
- 日均气温 `T_mean = ISA(h_msl) + isa_dt`（与 THERMO 查询同一 ISA 基准；THERMO 本身不加日变化，D1 物理不变）；
  气温 `T_air = T_mean + A·cos(2π(tau − 15 h)/24 h)`，tau 为真太阳时（小时），半日较差 `A = 4 K·(1 − 0.6·c)`；
- 地表 `T_b = T_air + K_sol·max(sin h, 0)·(1 − 0.7·c)·(1 − 0.5·wet) − K_night·N(h)·(1 − 0.8·c)`，
  `N(h) = clamp((6° − h)/12°, 0, 1)`；地表类别给出 `eps_b`、`K_sol`、`K_night`；
- 天空 `T_sky = (1 − c)·(T_air − (40 − 20·rh) K) + c·(T_air − 0.0065·base_m)`（晴空窗口区远冷于气温，云底按 6.5 K/km）；
- 热交叉 `k_cross`：日出后 1 h 与日落前 1 h 内为 0.5，其余为 1（日出、日落按几何高度 −0.833° 判定；固定太阳模式恒为 1）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = ["DEFAULT_SURFACE", "H_RISE_DEG", "K_CROSS_LOW", "SURFACE_CLASSES", "SurfaceClass", "ThermalBg", "air_temp_c", "k_cross_of",
           "sky_temp_c", "surface_temp_c", "t_mean_c"]

H_RISE_DEG = -0.833
K_CROSS_LOW = 0.5
CROSS_WINDOW_S = 3600.0


@dataclass(frozen=True, slots=True)
class SurfaceClass:
    eps: float
    k_sol_k: float
    k_night_k: float


SURFACE_CLASSES: dict[str, SurfaceClass] = {
    "urban": SurfaceClass(0.93, 15.0, 2.5),       # 混凝土与沥青
    "vegetation": SurfaceClass(0.97, 6.0, 3.5),   # 草地与灌丛
    "soil": SurfaceClass(0.93, 12.0, 4.0),        # 裸土
    "water": SurfaceClass(0.98, 1.0, 0.5),        # 水面
}
DEFAULT_SURFACE = "urban"


@dataclass(frozen=True, slots=True)
class ThermalBg:
    """EnvPerceptionView.thermal_bg 的返回（M18-M19 PRD §6.11）：各为 [n] 数组。"""

    t_b_c: NDArray[np.float64]
    eps_b: NDArray[np.float64]
    t_sky_c: NDArray[np.float64]
    k_cross: NDArray[np.float64]


def t_mean_c(h_msl_m: float, isa_dt_c: float) -> float:
    return 15.0 - 0.0065 * float(h_msl_m) + float(isa_dt_c)


def air_temp_c(t_mean: float, tst_min: float, cover: float) -> float:
    c = min(1.0, max(0.0, cover))
    amp = 4.0 * (1.0 - 0.6 * c)
    return t_mean + amp * math.cos(2.0 * math.pi * (tst_min / 60.0 - 15.0) / 24.0)


def surface_temp_c(t_air: float, el_deg: float, cover: float, wetness: float, cls: SurfaceClass) -> float:
    c = min(1.0, max(0.0, cover))
    w = min(1.0, max(0.0, wetness))
    s = max(0.0, math.sin(math.radians(el_deg)))
    n = min(1.0, max(0.0, (6.0 - el_deg) / 12.0))
    return t_air + cls.k_sol_k * s * (1.0 - 0.7 * c) * (1.0 - 0.5 * w) - cls.k_night_k * n * (1.0 - 0.8 * c)


def sky_temp_c(t_air: float, rh: float, cover: float, cloud_base_m: float) -> float:
    c = min(1.0, max(0.0, cover))
    r = min(1.0, max(0.0, rh))
    clear = t_air - (40.0 - 20.0 * r)
    cloud = t_air - 0.0065 * max(0.0, cloud_base_m)
    return (1.0 - c) * clear + c * cloud


def k_cross_of(el_now: float, el_before: float, el_after: float) -> float:
    """el_before = h(t − 1 h)、el_after = h(t + 1 h)：太阳在地平线上且 1 h 前未升起或 1 h 后已落下时为 0.5。"""
    if el_now >= H_RISE_DEG and (el_before < H_RISE_DEG or el_after < H_RISE_DEG):
        return K_CROSS_LOW
    return 1.0
