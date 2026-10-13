"""光学与消光（M19-FR-002、FR-024；AWR-04 §6.1、§6.3 第 4 步；AWR-20 §6.2–§6.4；ADR-023、ADR-101）。

- `HFOV(f) = 2·atan(W_crop·p/2f)`；F 数按型号的插值律（`linear_f` 或 `linear_ln_f`）；`p_eff = p·W_crop/W_out`；
  有效像元 `p' = max(p_eff, λ·F#)`（衍射：550 nm F3.2 为 1.76 µm，只约束 4K；LWIR 12 µm、F1.0 时 λF = 10 µm 不约束）；
- `GSD = R·p'/f`、`N = d_c·f/(R·p')`（f 为 mm、p 为 µm）；
- 均匀消光（可行窗口与计算器）：`sigma_550 = ln20/MOR`，其他波长乘 Kim 比 `(λ/550)^−q(V2)`，`V2 = ln50/sigma_550`（km，M07 `kim_q`
  同一口径），LWIR 另加水汽项 `(0.03 + 0.012·rho_v)/km`。sim-core 内的路径透过率直接用 M07 `optical_depth`（指数霾、雾层、降水）。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from awr.environment.atmosphere.optics import kim_q, sigma_wv_per_m
from awr.environment.weather.derive import K_V2

from .params import K_MOR, LAMBDA_NM
from .specs import SensorSpecP

__all__ = ["f_number", "gsd_m", "hfov_rad", "lambda_um", "p_eff_um", "p_prime_um", "pixels_on_target", "sigma_per_m", "tau", "vfov_rad"]


def lambda_um(kind: str) -> float:
    return LAMBDA_NM.get(kind, 550.0) / 1000.0


def f_number(sn: SensorSpecP, f_mm: float) -> float:
    if sn.f_max_mm <= sn.f_min_mm:
        return sn.f_number_min
    if sn.f_law == "linear_ln_f":
        u = (math.log(max(f_mm, 1e-9)) - math.log(sn.f_min_mm)) / (math.log(sn.f_max_mm) - math.log(sn.f_min_mm))
    else:
        u = (f_mm - sn.f_min_mm) / (sn.f_max_mm - sn.f_min_mm)
    return sn.f_number_min + (sn.f_number_max - sn.f_number_min) * min(max(u, 0.0), 1.0)


def _out(sn: SensorSpecP, resolution: str | None) -> tuple[int, int] | None:
    o = resolution or sn.analysis
    return sn.outputs.get(o) if sn.outputs else None


def p_eff_um(sn: SensorSpecP, resolution: str | None = None) -> float:
    """等效像元：`p·W_crop/W_out`（输出档）；LWIR 与虚拟像元为原生像元（数字变焦不改变 N）。"""
    o = _out(sn, resolution)
    if o is None:
        return sn.pixel_um
    w_out, crop = o
    return sn.pixel_um * crop / w_out


def p_prime_um(sn: SensorSpecP, f_mm: float, resolution: str | None = None) -> float:
    """有效像元 `p' = max(p_eff, λ·F#)`（λ 按传感器类别：EO 0.55、NIR 0.85、LWIR 10 µm）。"""
    return max(p_eff_um(sn, resolution), lambda_um(sn.kind) * f_number(sn, f_mm))


def hfov_rad(sn: SensorSpecP, f_mm: float, resolution: str | None = None) -> float:
    o = _out(sn, resolution)
    crop = o[1] if o is not None else sn.w_native_px
    return 2.0 * math.atan(crop * sn.pixel_um * 1e-3 / (2.0 * f_mm))


def vfov_rad(sn: SensorSpecP, f_mm: float, resolution: str | None = None) -> float:
    o = resolution or sn.analysis
    crop_h = sn.crop_h_px.get(o, sn.h_native_px) if sn.crop_h_px else sn.h_native_px
    return 2.0 * math.atan(crop_h * sn.pixel_um * 1e-3 / (2.0 * f_mm))


def gsd_m(r_m: float | NDArray, p_um: float, f_mm: float) -> float | NDArray:
    return np.asarray(r_m, float) * p_um * 1e-3 / f_mm if np.ndim(r_m) else float(r_m) * p_um * 1e-3 / f_mm


def pixels_on_target(d_c_m: float | NDArray, f_mm: float, r_m: float | NDArray, p_um: float) -> float | NDArray:
    out = np.asarray(d_c_m, float) * f_mm / (np.maximum(np.asarray(r_m, float), 1e-9) * p_um * 1e-3)
    return float(out) if np.ndim(out) == 0 else out


def sigma_per_m(mor_m: float, lam_nm: float, rho_v_gm3: float = 7.5) -> float:
    """均匀消光系数（1/m）：sigma_550 = ln20/MOR，Kim 波长换算（V2 = ln50/sigma_550，km），LWIR 加水汽项（M07 同一式）。"""
    if not (mor_m > 0.0) or not math.isfinite(mor_m):
        s = 0.0
    else:
        s550 = K_MOR / float(mor_m)
        s = s550 if lam_nm == 550.0 else s550 * (float(lam_nm) / 550.0) ** -kim_q(K_V2 / s550 / 1000.0)
    if lam_nm >= 8000.0:
        s += sigma_wv_per_m(rho_v_gm3)
    return float(s)


def tau(mor_m: float, lam_nm: float, r_m: float | NDArray, rho_v_gm3: float = 7.5) -> float | NDArray:
    out = np.exp(-sigma_per_m(mor_m, lam_nm, rho_v_gm3) * np.asarray(r_m, float))
    return float(out) if np.ndim(out) == 0 else out
