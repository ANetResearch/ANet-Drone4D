"""physical 视觉准则与有效视觉半径（M18-FR-011、FR-017；M18-M19 PRD §6.3.2、§6.3.3；AWR-04 §2.3、§7.3；ADR-097）。

准则（无人机位于配置体内时）：
- DSM 视线可见（识别物顶部 → 无人机的一条射线，经共享 LosCache，所有者 M18）；
- 角尺寸 `D_vis/r ≥ θ_th`，`D_vis = √A`（自识别物看无人机的仰角 e ≥ 45° 取顶面截面，否则侧面），`θ_th = θ_0/(0.5 + a)`，
  低照档乘 2；夜间不开灯只在 `night_visible_m`（缺省 30 m）内可见，开防撞灯（nav 或 strobe）视为可见；
- 表观对比度 `|C|·τ_550(r) ≥ 0.05`，C 取 `contrast_sky`（e > 0）或 `contrast_ground`。
有效半径：`r_vis = min(radius_max, D_vis/θ_th, r(C_app = 0.05))`，`r(C_app) = ln(|C|/0.05)/sigma_550`；夜间无灯为 night_visible_m。
保守口径 a + 0.2，并取顶面与侧面截面的较大者。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from .envview import TargetEnv
from .signature import VehicleSignature

__all__ = ["C_MIN", "theta_th_rad", "visual_ok", "visual_radius"]

C_MIN = 0.05
ARCMIN = math.pi / (180.0 * 60.0)


def theta_th_rad(theta0_arcmin: float, alertness: float, band: str) -> float:
    return float(theta0_arcmin) * ARCMIN / (0.5 + float(alertness)) * (2.0 if band == "low" else 1.0)


def _r_contrast(c: float, sigma: float) -> float:
    c = abs(float(c))
    if c <= C_MIN:
        return 0.0
    return math.inf if sigma <= 0.0 else math.log(c / C_MIN) / sigma


def visual_ok(r3: NDArray, elev_rad: NDArray, sigs: list[VehicleSignature], env: TargetEnv, theta0_arcmin: float, alertness: float,
              tau: NDArray | None = None) -> tuple[NDArray[np.bool_], NDArray]:
    """角尺寸、夜间灯光与对比度三项（视线另判）。返回 (ok[n], margin_ratio[n] = r_vis/r3)。"""
    r3 = np.maximum(np.asarray(r3, float), 1e-3)
    e = np.asarray(elev_rad, float)
    n = r3.size
    th = theta_th_rad(theta0_arcmin, alertness, env.band)
    top = np.array([s.area_m2[0] for s in sigs])
    side = np.array([s.area_m2[1] for s in sigs])
    dvis = np.sqrt(np.where(e >= math.radians(45.0), top, side))
    csky = np.array([s.contrast_sky for s in sigs])
    cgnd = np.array([s.contrast_ground for s in sigs])
    c = np.abs(np.where(e > 0.0, csky, cgnd))
    t = env.tau550(r3) if tau is None else np.asarray(tau, float)
    ok_c = c * t >= C_MIN
    if env.band == "night":
        lit = np.array([s.lights_nav or s.lights_strobe for s in sigs], bool)
        nv = np.array([s.night_visible_m for s in sigs])
        ok_ang = lit | (r3 <= nv)
        r_ang = np.where(lit, np.inf, nv)
    else:
        ok_ang = dvis / r3 >= th
        r_ang = dvis / th
    sig = env.sigma550
    r_c = np.array([_r_contrast(ci, sig) for ci in c]) if n else np.zeros(0)
    r_vis = np.minimum(r_ang, r_c)
    return ok_ang & ok_c, r_vis / r3


def visual_radius(sig: VehicleSignature, env: TargetEnv, theta0_arcmin: float, alertness: float, *, conservative: bool = False,
                  cap_m: float = math.inf) -> float:
    """有效视觉半径（m，未截断到 radius_max；以 cap_m 截断无界值）。"""
    a = min(1.2, alertness + 0.2) if conservative else alertness
    sig_c = env.sigma550
    r_c = _r_contrast(sig.contrast_sky, sig_c)
    if env.band == "night":
        r_ang = math.inf if (sig.lights_nav or sig.lights_strobe) else sig.night_visible_m
    else:
        area = max(sig.area_m2[0], sig.area_m2[1]) if conservative else sig.area_m2[1]
        r_ang = math.sqrt(area) / theta_th_rad(theta0_arcmin, a, env.band)
    return float(min(r_ang, r_c, cap_m))
