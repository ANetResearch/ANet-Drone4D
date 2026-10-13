"""识别物雷达接收（反无人机设备类识别物的 radar 敏感体，P1；M18-FR-030；AWR-04 §15.10.1、§15.10.3；ADR-124）。

识别物配置 `radar_rx{snr_ref_db, r_ref_m, snr_req_db, fov_az_deg, fov_el_deg}` 且挂 `radar` 敏感体时，physical 判据：

    SNR = snr_ref + 10·lg sigma_U − 40·lg(r / r_ref) − 2·gamma_rain·r/1000 ≥ snr_req，且无人机位于接收视场内
    （|方位 − (航向 + 敏感体方位偏置)| ≤ fov_az_deg，|仰角| ≤ fov_el_deg，均为半角）。

sigma_U 为机型 RCS（`VehicleSignature.rcs_m2`）；gamma_rain = k·RR^alpha（dB/km，k = 1.0、alpha = 0.7，77 GHz 量级，与 M19 参考雷达 `mmw77` 同一
雨衰口径，AWR-20 §6.5）。geometric 模式与其他敏感体相同（只看配置体与驻留）。有效半径为 SNR = snr_req 的斜距（不受 radius_max
截断，上限 5000 m）。纯函数，不使用随机数（ADR-099）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from numpy.typing import NDArray

__all__ = ["RAIN_ALPHA", "RAIN_K", "R_MAX_RADAR", "gamma_rain_db_km", "radar_in_fov", "radar_radius", "radar_snr_db"]

RAIN_K = 1.0
RAIN_ALPHA = 0.7
R_MAX_RADAR = 5000.0


def gamma_rain_db_km(rain_mmph: float) -> float:
    rr = float(rain_mmph or 0.0)
    return RAIN_K * rr ** RAIN_ALPHA if rr > 0.0 else 0.0


def radar_snr_db(rx: dict, rcs_m2: float | NDArray, r_m: float | NDArray, gamma_db_km: float = 0.0) -> Any:
    """雷达接收 SNR（dB），r 按 ≥ 0.1 m 截断。"""
    r = np.maximum(np.asarray(r_m, float), 0.1)
    out = (float(rx["snr_ref_db"]) + 10.0 * np.log10(np.maximum(np.asarray(rcs_m2, float), 1e-9))
           - 40.0 * np.log10(r / float(rx["r_ref_m"])) - 2.0 * float(gamma_db_km) * r / 1000.0)
    return float(out) if np.ndim(out) == 0 else out


def radar_radius(rx: dict, rcs_m2: float, gamma_db_km: float = 0.0, *, iters: int = 60) -> float:
    """SNR = snr_req 的斜距（m）；无雨时为闭式 `r_ref·10^((snr_ref + 10·lg sigma − snr_req)/40)`，有雨时二分（[0.1, 5000] m）。"""
    req = float(rx["snr_req_db"])
    if gamma_db_km <= 0.0:
        r = float(rx["r_ref_m"]) * 10.0 ** ((float(rx["snr_ref_db"]) + 10.0 * math.log10(max(float(rcs_m2), 1e-9)) - req) / 40.0)
        return float(min(r, R_MAX_RADAR))
    lo, hi = 0.1, R_MAX_RADAR
    if radar_snr_db(rx, rcs_m2, lo, gamma_db_km) < req:
        return 0.0
    if radar_snr_db(rx, rcs_m2, hi, gamma_db_km) >= req:
        return hi
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        if radar_snr_db(rx, rcs_m2, mid, gamma_db_km) >= req:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def radar_in_fov(rx: dict, d: NDArray, axis_yaw_rad: float) -> NDArray:
    """d [n, 3]（无人机 − 识别物，ENU）是否在接收视场内（方位相对轴 `axis_yaw_rad`，仰角相对水平；半角）。"""
    d = np.asarray(d, float).reshape(-1, 3)
    rh = np.hypot(d[:, 0], d[:, 1])
    az = np.abs((np.arctan2(d[:, 1], d[:, 0]) - float(axis_yaw_rad) + math.pi) % (2.0 * math.pi) - math.pi)
    el = np.abs(np.arctan2(d[:, 2], rh))
    return (az <= math.radians(float(rx["fov_az_deg"])) + 1e-12) & (el <= math.radians(float(rx["fov_el_deg"])) + 1e-12)
