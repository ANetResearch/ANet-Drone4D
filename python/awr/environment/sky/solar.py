"""NOAA 太阳位置（AWR-04 §6.5；ADR-102；NOAA Solar Calculator，基于 Meeus《Astronomical Algorithms》低精度式）。

输入 UNIX 毫秒（UTC，不含 ΔT）与 WGS84 纬度、经度（东经为正）；输出几何太阳高度（不含大气折射）、方位（自真北顺时针）、
赤纬、均时差与真太阳时。照度档、热交叉与声学剖面一律用几何高度；`refraction_deg` 只供与带折射的参考值对拍与显示。
方位用 atan2 形式（与 NOAA 表格的 acos 形式数学等价，天顶附近数值稳定）。与 `engine/environment/sky/solar.ts` 逐式对应。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

__all__ = ["MS_PER_DAY", "SunPos", "refraction_deg", "solar_position", "solar_position_arr", "sun_dir_enu"]

MS_PER_DAY = 86_400_000.0
JD_UNIX0 = 2440587.5
JD_J2000 = 2451545.0
_D2R = math.pi / 180.0
_R2D = 180.0 / math.pi


@dataclass(frozen=True, slots=True)
class SunPos:
    az_deg: float        # 自真北顺时针，[0, 360)
    el_deg: float        # 几何高度（不含折射）
    decl_deg: float      # 视赤纬
    eot_min: float       # 均时差（分钟）
    tst_min: float       # 真太阳时（分钟，[0, 1440)）
    ha_deg: float        # 时角，[-180, 180)


def solar_position(unix_ms: float, lat_deg: float, lon_deg: float) -> SunPos:
    jd = unix_ms / MS_PER_DAY + JD_UNIX0
    jc = (jd - JD_J2000) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    m = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    mr = m * _D2R
    c = (math.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + math.sin(2.0 * mr) * (0.019993 - 0.000101 * jc)
         + math.sin(3.0 * mr) * 0.000289)
    omega = (125.04 - 1934.136 * jc) * _D2R
    app_long = (l0 + c - 0.00569 - 0.00478 * math.sin(omega)) * _D2R
    obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = (obliq0 + 0.00256 * math.cos(omega)) * _D2R
    decl = math.asin(math.sin(obliq) * math.sin(app_long))
    y = math.tan(obliq / 2.0) ** 2
    l0r = l0 * _D2R
    eot = 4.0 * _R2D * (y * math.sin(2.0 * l0r) - 2.0 * e * math.sin(mr) + 4.0 * e * y * math.sin(mr) * math.cos(2.0 * l0r)
                        - 0.5 * y * y * math.sin(4.0 * l0r) - 1.25 * e * e * math.sin(2.0 * mr))
    min_utc = (unix_ms / 60000.0) % 1440.0
    tst = (min_utc + eot + 4.0 * lon_deg) % 1440.0
    ha = tst / 4.0 - 180.0
    lat = lat_deg * _D2R
    har = ha * _D2R
    cz = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(har)
    cz = min(1.0, max(-1.0, cz))
    el = 90.0 - math.acos(cz) * _R2D
    az = (math.atan2(math.sin(har), math.cos(har) * math.sin(lat) - math.tan(decl) * math.cos(lat)) * _R2D + 180.0) % 360.0
    return SunPos(az, el, decl * _R2D, eot, tst, ha)


def solar_position_arr(unix_ms: np.ndarray, lat_deg: float, lon_deg: float) -> tuple[np.ndarray, np.ndarray]:
    """向量化（预报与可持续性评估用）：-> (az_deg, el_deg)；与标量式同一公式（逐元素误差在 1e-9° 量级）。"""
    t = np.asarray(unix_ms, np.float64)
    jc = (t / MS_PER_DAY + JD_UNIX0 - JD_J2000) / 36525.0
    l0 = np.mod(280.46646 + jc * (36000.76983 + jc * 0.0003032), 360.0)
    mr = np.radians(357.52911 + jc * (35999.05029 - 0.0001537 * jc))
    e = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)
    c = np.sin(mr) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + np.sin(2.0 * mr) * (0.019993 - 0.000101 * jc) + np.sin(3.0 * mr) * 0.000289
    omega = np.radians(125.04 - 1934.136 * jc)
    app_long = np.radians(l0 + c - 0.00569 - 0.00478 * np.sin(omega))
    obliq0 = 23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    obliq = np.radians(obliq0 + 0.00256 * np.cos(omega))
    decl = np.arcsin(np.sin(obliq) * np.sin(app_long))
    y = np.tan(obliq / 2.0) ** 2
    l0r = np.radians(l0)
    eot = 4.0 * _R2D * (y * np.sin(2.0 * l0r) - 2.0 * e * np.sin(mr) + 4.0 * e * y * np.sin(mr) * np.cos(2.0 * l0r)
                        - 0.5 * y * y * np.sin(4.0 * l0r) - 1.25 * e * e * np.sin(2.0 * mr))
    tst = np.mod(np.mod(t / 60000.0, 1440.0) + eot + 4.0 * lon_deg, 1440.0)
    har = np.radians(tst / 4.0 - 180.0)
    lat = math.radians(lat_deg)
    cz = np.clip(math.sin(lat) * np.sin(decl) + math.cos(lat) * np.cos(decl) * np.cos(har), -1.0, 1.0)
    el = 90.0 - np.degrees(np.arccos(cz))
    az = np.mod(np.degrees(np.arctan2(np.sin(har), np.cos(har) * math.sin(lat) - np.tan(decl) * math.cos(lat))) + 180.0, 360.0)
    return az, el


def refraction_deg(el_deg: float) -> float:
    """NOAA 近似大气折射（标准大气，度）；带折射高度 = 几何高度 + 本值。"""
    if el_deg > 85.0:
        return 0.0
    te = math.tan(el_deg * _D2R)
    if el_deg > 5.0:
        r = 58.1 / te - 0.07 / te**3 + 0.000086 / te**5
    elif el_deg > -0.575:
        r = 1735.0 + el_deg * (-518.2 + el_deg * (103.4 + el_deg * (-12.79 + el_deg * 0.711)))
    else:
        r = -20.772 / te
    return r / 3600.0


def sun_dir_enu(az_deg: float, el_deg: float) -> tuple[float, float, float]:
    """单位太阳方向（ENU，指向太阳）；方位自网格北顺时针（与 config.sun 同一约定）。"""
    a, h = az_deg * _D2R, el_deg * _D2R
    return math.sin(a) * math.cos(h), math.cos(a) * math.cos(h), math.sin(h)
