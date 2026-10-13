"""非成像传感器判据（M19-FR-007 至 FR-010；AWR-04 §6.2；AWR-20 §6.5–§6.7；M18-M19 PRD §6.8）。

```text
雷达：SNR = SNR_ref + 10·lg(sigma_k/sigma_ref) − 40·lg(R/R_ref) − 2·gamma_rain·R/1000，gamma_rain = k·RR^alpha（dB/km），SNR ≥ SNR_D → D
      sigma_k = sigma（swerling0）| −sigma·ln u（swerling1，u 取计数器 RNG 流 perception）
      P1：SNR ≥ 20 dB 持续 2 s → R；|v_r| < 0.3 m/s 的地面目标须 sigma ≥ 10·sigma0·A_cell，A_cell = R·Δθ_az·ΔR·sec ψ_g
声阵列：L_t,b = L_W,b(t) − 20·lg R − 11 − alpha_b·R/1000 − (10 dB 若缓存的中心射线被遮挡)
      L_ego,b = L_W,b(本机) − 20·lg r_ego − 11
      SNR_b = L_t,b + G − 10·lg(10^((L_ego,b − NR)/10) + 10^(L_bg,b/10))（100 Hz–8 kHz；背景由 A 计权带级换算为未计权）
      max SNR_b ≥ 6 dB → D；P1：≥ 3 个谐波带同时 ≥ 10 dB → R
MID-360：n = 3500·A_proj/R²·10·(1 − occ)（1 s 点数），≥ 10 → D、≥ 100 → R，且 0.1 ≤ R ≤ min(100, 40·(rho_905·τ²/0.1)^0.269)
```
纯函数，只依赖 numpy 与 M18 的带表（`awr.sim.targets.sound`、`acoustic.ALPHA_DB_KM`）。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from awr.sim.targets.acoustic import ALPHA_DB_KM
from awr.sim.targets.sound import A_W, BANDS_HZ, EXACT_HZ, N_BANDS

from .specs import AcousticSpec, LidarSpec, RadarSpec

__all__ = [
    "ACOUSTIC_BARRIER_DB", "a_cell_m2", "acoustic_band_mask", "acoustic_snr_bands", "acoustic_snr_matrix", "clutter_ok", "harmonic_bands", "lidar_points",
    "lidar_r_max", "radar_range_m", "radar_snr_db", "rain_db_per_km",
]

ACOUSTIC_BARRIER_DB = 10.0      # 中心射线被遮挡时的屏障衰减（本文设定，ISO 9613-2 单屏障 5–20 dB 的中值）


# ---------------------------------------------------------------- 雷达
def rain_db_per_km(spec: RadarSpec, rain_mmph: float) -> float:
    rr = max(float(rain_mmph), 0.0)
    return spec.rain_k * rr ** spec.rain_alpha if rr > 0 else 0.0


def radar_snr_db(spec: RadarSpec, sigma_m2: float | NDArray, r_m: float | NDArray, rain_mmph: float = 0.0) -> float | NDArray:
    s = np.maximum(np.asarray(sigma_m2, float), 1e-12)
    r = np.maximum(np.asarray(r_m, float), 1e-3)
    out = spec.snr_d_db + 10.0 * np.log10(s / spec.sigma_ref_m2) - 40.0 * np.log10(r / spec.r_ref_m) \
        - 2.0 * rain_db_per_km(spec, rain_mmph) * r / 1000.0
    return float(out) if np.ndim(out) == 0 else out


def radar_range_m(spec: RadarSpec, sigma_m2: float, rain_mmph: float = 0.0, *, snr_db: float | None = None) -> float:
    """SNR 等于门限（缺省 D 级）的距离（m，未截断到 r_max）；无雨时 `R_ref·(sigma/sigma_ref)^(1/4)`，有雨时二分。"""
    thr = spec.snr_d_db if snr_db is None else float(snr_db)
    r0 = spec.r_ref_m * (float(sigma_m2) / spec.sigma_ref_m2) ** 0.25 * 10.0 ** ((spec.snr_d_db - thr) / 40.0)
    if rain_db_per_km(spec, rain_mmph) <= 0.0:
        return r0
    lo, hi = 0.0, r0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if radar_snr_db(spec, sigma_m2, mid, rain_mmph) >= thr:
            lo = mid
        else:
            hi = mid
    return lo


def a_cell_m2(spec: RadarSpec, r_m: float | NDArray, graze_rad: float | NDArray) -> float | NDArray:
    """地杂波分辨单元面积 `R·Δθ_az·ΔR·sec ψ_g`（P1）。"""
    c = np.maximum(np.cos(np.asarray(graze_rad, float)), 1e-3)
    out = np.asarray(r_m, float) * math.radians(spec.az_res_deg) * spec.range_res_m / c
    return float(out) if np.ndim(out) == 0 else out


def clutter_ok(spec: RadarSpec, sigma_m2: float | NDArray, r_m: float | NDArray, graze_rad: float | NDArray) -> bool | NDArray:
    """地杂波门限（P1）：`sigma ≥ k·sigma0·A_cell`。"""
    out = np.asarray(sigma_m2, float) >= spec.clutter_k * 10.0 ** (spec.sigma0_db / 10.0) * np.asarray(a_cell_m2(spec, r_m, graze_rad))
    return bool(out) if np.ndim(out) == 0 else out


# ---------------------------------------------------------------- 声阵列
def acoustic_band_mask(spec: AcousticSpec) -> NDArray:
    """计入 SNR 的带（中心频率在 [band_hz] 内）。"""
    f = np.asarray(BANDS_HZ)
    return (f >= spec.band_hz[0] - 1e-6) & (f <= spec.band_hz[1] + 1e-6)


def acoustic_snr_bands(spec: AcousticSpec, lw_target: NDArray, r_m: float, lw_ego: NDArray, l_bg_a: NDArray, *, frame: str = "multirotor",
                       blocked: bool = False) -> NDArray:
    """各带 SNR（dB，[24]；不在频带内的带为 −inf）。`lw_target`、`lw_ego` 为未计权带声功率级，`l_bg_a` 为 A 计权带背景级。"""
    r = max(float(r_m), 0.1)
    lt = np.asarray(lw_target, float) - 20.0 * math.log10(r) - 11.0 - ALPHA_DB_KM * r / 1000.0 - (ACOUSTIC_BARRIER_DB if blocked else 0.0)
    r_ego = float(spec.r_ego_m.get(frame, spec.r_ego_m.get("multirotor", 0.5)))
    nr = float(spec.nr_db.get(frame, spec.nr_db.get("multirotor", 25.0)))
    le = np.asarray(lw_ego, float) - 20.0 * math.log10(r_ego) - 11.0
    lbg = np.asarray(l_bg_a, float) - A_W            # A 计权 → 未计权（同一带内信号与噪声同一计权）
    noise = 10.0 * np.log10(10.0 ** ((le - nr) / 10.0) + 10.0 ** (lbg / 10.0))
    snr = lt + spec.array_gain_db - noise
    return np.where(acoustic_band_mask(spec), snr, -np.inf)


def acoustic_snr_matrix(spec: AcousticSpec, lw_targets: NDArray, r_m: NDArray, lw_ego: NDArray, l_bg_a: NDArray, *, frame: str = "multirotor",
                        blocked: NDArray | None = None) -> NDArray:
    """`acoustic_snr_bands` 的按识别物向量化（[k, 24]；第 i 行与 `acoustic_snr_bands(spec, lw_targets[i], r_m[i], …, blocked=blocked[i])`
    逐位相同：同一运算次序，噪声项与识别物无关只算一次）。"""
    r = np.maximum(np.asarray(r_m, float).reshape(-1), 0.1)
    bar = np.where(np.asarray(blocked, bool).reshape(-1), ACOUSTIC_BARRIER_DB, 0.0) if blocked is not None else np.zeros(r.shape)
    lt = np.asarray(lw_targets, float) - (20.0 * np.log10(r))[:, None] - 11.0 - ALPHA_DB_KM[None, :] * r[:, None] / 1000.0 - bar[:, None]
    r_ego = float(spec.r_ego_m.get(frame, spec.r_ego_m.get("multirotor", 0.5)))
    nr = float(spec.nr_db.get(frame, spec.nr_db.get("multirotor", 25.0)))
    le = np.asarray(lw_ego, float) - 20.0 * math.log10(r_ego) - 11.0
    lbg = np.asarray(l_bg_a, float) - A_W
    noise = 10.0 * np.log10(10.0 ** ((le - nr) / 10.0) + 10.0 ** (lbg / 10.0))
    snr = lt + spec.array_gain_db - noise[None, :]
    return np.where(acoustic_band_mask(spec)[None, :], snr, -np.inf)


def harmonic_bands(f0_hz: float | None, n_h: int = 8) -> NDArray:
    """谐波 k·f0（k = 1..n_h）所在的带（bool[24]）。"""
    m = np.zeros(N_BANDS, bool)
    if not f0_hz or f0_hz <= 0:
        return m
    lo, hi = EXACT_HZ * 10.0 ** -0.05, EXACT_HZ * 10.0 ** 0.05
    for k in range(1, n_h + 1):
        f = k * float(f0_hz)
        hit = (f >= lo) & (f < hi)
        m |= hit
    return m


# ---------------------------------------------------------------- MID-360
def lidar_r_max(spec: LidarSpec, rho_eff: float | NDArray) -> float | NDArray:
    """量程 `min(hard_max, R_ref·(rho/rho_ref)^exp)`（rho 已乘 τ_905²）。"""
    r = spec.r_ref_m * np.power(np.maximum(np.asarray(rho_eff, float), 1e-9) / spec.rho_ref, spec.rho_exp)
    out = np.minimum(r, spec.hard_max_m)
    return float(out) if np.ndim(out) == 0 else out


def lidar_points(spec: LidarSpec, a_proj_m2: float | NDArray, r_m: float | NDArray, occ: float | NDArray = 0.0) -> float | NDArray:
    """1 s 内落在识别物上的点数 `n = rho_pts·A/R²·f_frame·(1 − occ)`。"""
    out = spec.pts_per_sr * np.asarray(a_proj_m2, float) / np.maximum(np.asarray(r_m, float), 1e-3) ** 2 * spec.frame_hz \
        * (1.0 - np.asarray(occ, float))
    return float(out) if np.ndim(out) == 0 else out
