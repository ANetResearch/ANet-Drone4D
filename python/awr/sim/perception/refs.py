"""参考作用距离表（AWR-20 §6.9；CAT-FR-019，P1；d2-WP-08-to-M19 第 2 条）。

条件（AWR-20 §6.9）：AWR-04 §6.2 的判据与系数；所需像素取 P ≥ 0.9 的 D 3.5、R 14、I 22.4 px；目标为模板的人（平视最不利
d_c 0.7246 m、contrast0 0.4、rho_850 0.45、ΔT 8 K、ε 0.97）与轿车（侧向最不利 1.643 m、contrast0 0.5、rho_850 0.5、热斑 ΔT 50 K、
ε 0.90、占比 0.2）；静止、视线无遮挡、伪装与遮挡为 0；EO 为日间（k_light = 1、曝光 1/1000 s），NIR 与 LWIR 为夜间（曝光 1/30 s；
LWIR 杂波 1.0 K、k_cross = 1）；rho_v = 10 g/m³。数值为**斜距上限**（平视，ε = 0）；接力的可行窗口另由 `feasibility_window` 给出。

雷达：`R_D(sigma) = R_ref·(sigma/sigma_ref)^(1/4)`，R 级（P1）`R_D·10^(−6.8/40)`；MID-360：D 与 R 的点数距离，截断到量程。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .level import PairArrays, evaluate
from .nonimaging import lidar_r_max, radar_range_m
from .params import N_P90
from .specs import EnvSample, LidarSpec, RadarSpec, SensorSpecP, SensorState

__all__ = ["REF_TARGETS", "ref_ranges"]

REF_TARGETS: dict[str, dict[str, Any]] = {
    "person": {"d_c": 0.7246, "c0": 0.4, "rho850": 0.45, "dt": 8.0, "emis": 0.97, "hot": 1.0, "rcs": 0.5, "rho905": 0.4, "a_side": 0.525},
    "vehicle": {"d_c": 1.643, "c0": 0.5, "rho850": 0.5, "dt": 50.0, "emis": 0.90, "hot": 0.2, "rcs": 10.0, "rho905": 0.5, "a_side": 6.75},
}
RADAR_CLASSES = {"person": 0.5, "dog": 0.1, "uav_small": 0.01, "vehicle": 10.0, "machine": 2.0, "object": 0.3}


def _n_eff(sn: SensorSpecP, tgt: dict, R: np.ndarray, env: EnvSample, *, pixels_only: bool) -> np.ndarray:
    n = len(R)
    a = PairArrays(R, np.full(n, tgt["d_c"]), np.full(n, tgt["hot"]), np.full(n, tgt["c0"]), np.zeros(n), np.full(n, tgt["rho850"]),
                   np.zeros(n, bool), np.full(n, tgt["dt"]), np.full(n, 30.0), np.full(n, tgt["emis"]), np.zeros(n), np.ones(n), np.ones(n, bool),
                   np.zeros(n), np.ones(n, bool), np.ones(n, bool))
    mode = "nir" if sn.kind == "nir" else "auto"
    r = evaluate(SensorState(sn, sn.f_max_mm, mode), a, env, 14.0)  # type: ignore[arg-type]
    return r["n"] if pixels_only else r["n_eff"]


def _solve(sn: SensorSpecP, tgt: dict, env: EnvSample, n_req: float, *, pixels_only: bool) -> float:
    lo, hi = 0.1, 20000.0
    if _n_eff(sn, tgt, np.array([lo]), env, pixels_only=pixels_only)[0] < n_req:
        return 0.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if _n_eff(sn, tgt, np.array([mid]), env, pixels_only=pixels_only)[0] >= n_req:
            lo = mid
        else:
            hi = mid
    return lo


def ref_ranges(sensor: SensorSpecP | RadarSpec | LidarSpec, *, targets: tuple[str, ...] = ("person", "vehicle"),
               mors: tuple[float, ...] = (10000.0, 3000.0), pixels_only: bool = False) -> list[dict]:
    """参考作用距离（m，斜距上限）：成像传感器 `{target, light, mor_m, D_m, R_m, I_m}`；雷达 `{target, rcs_m2, D_m, R_m}`；
    MID-360 `{target, rho_905, D_m, R_m}`。"""
    rows: list[dict] = []
    if isinstance(sensor, RadarSpec):
        for cls, rcs in RADAR_CLASSES.items():
            d = radar_range_m(sensor, rcs)
            rows.append({"target": cls, "rcs_m2": rcs, "D_m": round(d, 1), "R_m": round(radar_range_m(sensor, rcs, snr_db=sensor.snr_r_db), 1)})
        return rows
    if isinstance(sensor, LidarSpec):
        for cls in targets:
            t = REF_TARGETS[cls]
            r_lim = float(lidar_r_max(sensor, t["rho905"]))
            rd = math.sqrt(sensor.pts_per_sr * t["a_side"] * sensor.frame_hz / sensor.n_d)
            rr = math.sqrt(sensor.pts_per_sr * t["a_side"] * sensor.frame_hz / sensor.n_r)
            rows.append({"target": cls, "rho_905": t["rho905"], "D_m": round(min(rd, r_lim), 1), "R_m": round(min(rr, r_lim), 1)})
        return rows
    light = "day" if sensor.kind == "eo" else "night"
    for cls in targets:
        t = REF_TARGETS[cls]
        for mor in mors:
            env = EnvSample(illum_lx=1.0e5 if light == "day" else 0.001, band=light, mor_m=float(mor), rho_v_gm3=10.0, k_cross=1.0)  # type: ignore[arg-type]
            row: dict[str, Any] = {"target": cls, "light": light, "mor_m": float(mor)}
            for lv in ("D", "R", "I"):
                row[f"{lv}_m"] = round(_solve(sensor, t, env, N_P90[lv], pixels_only=pixels_only), 1)
            rows.append(row)
            if pixels_only:
                break
    return rows
