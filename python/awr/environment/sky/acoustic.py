"""声学背景剖面（AWR-04 §7.3；M18-FR-008、FR-012；M19 声阵列 SNR；M18-M19 PRD §6.11）。

会话配置 `acoustic_bg ∈ {rural, suburban, urban}`：日间与夜间 A 计权总级 rural 40/30、suburban 50/40、urban 60/50 dB(A)；
晨昏按太阳高度在 −6°（夜）到 +6°（日）之间线性过渡；降水按雨强加 `(5 + 5·rain_k)·min(1, R_eff/0.5)` dB（R_eff ≥ 0.5 mm/h 时
+5 至 +10 dB，`rain_k = ln(1 + R)/ln 51` 与 derive 同式；0–0.5 mm/h 线性起步，本文设定，避免细雨时的阶跃）。
谱形：24 个 1/3 倍频程带（50 Hz–10 kHz，与 M18 声源同一带表），每个剖面一条 A 计权相对谱（本文设定的参考谱形：urban 取
EN 1793-3 归一化道路交通噪声谱并外推到 50 Hz 与 6.3–10 kHz，rural 为风与植被为主的宽平谱，suburban 介于二者之间），
按能量和归一化到 0 dB，带级 `L_b = L_total + rel_b`，因此各带能量和恰为 `L_total`。A 计权按 IEC 61672 解析式在名义中心频率处取值。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

__all__ = ["A_WEIGHT_DB", "BANDS_HZ", "LEVELS_DBA", "N_BANDS", "PROFILES", "REL_SHAPE_DB", "a_weight_db", "band_levels_dba", "level_dba",
           "rain_boost_db", "total_dba"]

BANDS_HZ: tuple[float, ...] = (50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0,
                               1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0, 10000.0)
N_BANDS = len(BANDS_HZ)
PROFILES = ("rural", "suburban", "urban")
LEVELS_DBA: dict[str, tuple[float, float]] = {"rural": (40.0, 30.0), "suburban": (50.0, 40.0), "urban": (60.0, 50.0)}  # (日, 夜)
_SHAPE_RAW: dict[str, tuple[float, ...]] = {
    "rural": (-30, -28, -26, -24, -22, -20, -18, -16, -14, -13, -12, -11, -11, -11, -11, -11, -11, -12, -13, -14, -16, -18, -21, -24),
    "suburban": (-26, -25, -23, -21, -20, -18, -16, -15, -14, -13, -12, -11, -10, -9, -9, -10, -10, -11, -12, -14, -16, -18, -21, -24),
    "urban": (-24, -23, -21, -20, -20, -18, -16, -15, -14, -13, -12, -11, -9, -8, -9, -10, -11, -13, -15, -16, -18, -20, -22, -25),
}


def a_weight_db(f_hz: float) -> float:
    """IEC 61672-1 A 计权（dB）。"""
    f2 = f_hz * f_hz
    ra = (12194.0**2 * f2 * f2) / ((f2 + 20.6**2) * math.sqrt((f2 + 107.7**2) * (f2 + 737.9**2)) * (f2 + 12194.0**2))
    return 20.0 * math.log10(ra) + 2.0


A_WEIGHT_DB: tuple[float, ...] = tuple(a_weight_db(f) for f in BANDS_HZ)


def _normalise(shape: tuple[float, ...]) -> tuple[float, ...]:
    tot = 10.0 * math.log10(sum(10.0 ** (x / 10.0) for x in shape))
    return tuple(float(x) - tot for x in shape)


REL_SHAPE_DB: dict[str, tuple[float, ...]] = {k: _normalise(v) for k, v in _SHAPE_RAW.items()}


def rain_boost_db(rain_eff_mmh: float) -> float:
    r = max(0.0, float(rain_eff_mmh))
    if r <= 0.0:
        return 0.0
    rain_k = min(1.0, max(0.0, math.log1p(r) / math.log(51.0)))
    return (5.0 + 5.0 * rain_k) * min(1.0, r / 0.5)


def level_dba(profile: str, el_deg: float, rain_eff_mmh: float = 0.0) -> float:
    """A 计权总级：昼夜按太阳高度线性过渡，加降水增量。"""
    day, night = LEVELS_DBA[profile]
    k = min(1.0, max(0.0, (float(el_deg) + 6.0) / 12.0))
    return night + (day - night) * k + rain_boost_db(rain_eff_mmh)


def band_levels_dba(profile: str, total: float) -> NDArray[np.float64]:
    """[24] A 计权带级（能量和 = total）。"""
    return float(total) + np.asarray(REL_SHAPE_DB[profile], np.float64)


def total_dba(bands: NDArray[np.float64]) -> float:
    return float(10.0 * np.log10(np.sum(10.0 ** (np.asarray(bands, np.float64) / 10.0))))
