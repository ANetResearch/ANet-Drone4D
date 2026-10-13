"""地面照度近似与照度档（AWR-04 §6.5；ADR-102）。

几何太阳高度 h（度）：h ≥ 10° 时 `E = 1.2e5·sin h`；0–10° 线性降至 400 lx；民用晨昏（−6°–0°）对数插值 400 → 3 lx；
航海晨昏（−12°–−6°）3 → 0.008 lx；天文晨昏（−18°–−12°）0.008 → 0.001 lx（本文设定：AWR-04 写"更低为 0.001 lx"，
此处以对数插值衔接，保证连续且随高度单调）；更低为 0.001 lx；可选月光（满月 0.1 lx，缺省关闭）；云量 c 时乘 `(1 − 0.7·c)`。
照度档：日间 h ≥ 6°（0）、低照 −6° ≤ h < 6°（1）、夜间 h < −6°（2），编码同 `rt/enums.json` LightBand。
"""

from __future__ import annotations

import math

import numpy as np

__all__ = ["BAND_DAY", "BAND_LOW", "BAND_NAMES", "BAND_NIGHT", "E_FLOOR_LX", "MOON_FULL_LX", "illum_band", "illum_band_arr",
           "illuminance_lx", "illuminance_lx_arr"]

BAND_DAY, BAND_LOW, BAND_NIGHT = 0, 1, 2
BAND_NAMES = ("day", "low", "night")
BAND_DAY_MIN_DEG = 6.0
BAND_NIGHT_MAX_DEG = -6.0
E_SUN_LX = 1.2e5
E_HORIZON_LX = 400.0
E_CIVIL_LX = 3.0
E_NAUTICAL_LX = 0.008
E_FLOOR_LX = 0.001
MOON_FULL_LX = 0.1
CLOUD_K = 0.7
_E10 = E_SUN_LX * math.sin(math.radians(10.0))
_LG400, _LG3, _LG008, _LG001 = (math.log10(x) for x in (E_HORIZON_LX, E_CIVIL_LX, E_NAUTICAL_LX, E_FLOOR_LX))


def _clear_lx(h: float) -> float:
    if h >= 10.0:
        return E_SUN_LX * math.sin(math.radians(h))
    if h >= 0.0:
        return E_HORIZON_LX + (_E10 - E_HORIZON_LX) * h / 10.0
    if h >= -6.0:
        return 10.0 ** (_LG3 + (_LG400 - _LG3) * (h + 6.0) / 6.0)
    if h >= -12.0:
        return 10.0 ** (_LG008 + (_LG3 - _LG008) * (h + 12.0) / 6.0)
    if h >= -18.0:
        return 10.0 ** (_LG001 + (_LG008 - _LG001) * (h + 18.0) / 6.0)
    return E_FLOOR_LX


def illuminance_lx(el_deg: float, cover: float = 0.0, moon_lx: float = 0.0) -> float:
    c = min(1.0, max(0.0, float(cover)))
    return (_clear_lx(float(el_deg)) + float(moon_lx)) * (1.0 - CLOUD_K * c)


def illuminance_lx_arr(el_deg: np.ndarray, cover: float | np.ndarray = 0.0, moon_lx: float = 0.0) -> np.ndarray:
    h = np.asarray(el_deg, np.float64)
    e = np.full(h.shape, E_FLOOR_LX)
    e = np.where(h >= -18.0, 10.0 ** (_LG001 + (_LG008 - _LG001) * (h + 18.0) / 6.0), e)
    e = np.where(h >= -12.0, 10.0 ** (_LG008 + (_LG3 - _LG008) * (h + 12.0) / 6.0), e)
    e = np.where(h >= -6.0, 10.0 ** (_LG3 + (_LG400 - _LG3) * (h + 6.0) / 6.0), e)
    e = np.where(h >= 0.0, E_HORIZON_LX + (_E10 - E_HORIZON_LX) * h / 10.0, e)
    e = np.where(h >= 10.0, E_SUN_LX * np.sin(np.radians(h)), e)
    c = np.clip(np.asarray(cover, np.float64), 0.0, 1.0)
    return (e + moon_lx) * (1.0 - CLOUD_K * c)


def illum_band(el_deg: float) -> int:
    if el_deg >= BAND_DAY_MIN_DEG:
        return BAND_DAY
    return BAND_LOW if el_deg >= BAND_NIGHT_MAX_DEG else BAND_NIGHT


def illum_band_arr(el_deg: np.ndarray) -> np.ndarray:
    h = np.asarray(el_deg, np.float64)
    return np.where(h >= BAND_DAY_MIN_DEG, BAND_DAY, np.where(h >= BAND_NIGHT_MAX_DEG, BAND_LOW, BAND_NIGHT)).astype(np.uint8)
