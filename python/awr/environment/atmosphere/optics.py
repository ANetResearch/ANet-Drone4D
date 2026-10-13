"""光学厚度与点消光（M07-FR-006、FR-020；M07 §6.3.4、§6.3.12；g06 §4.3、§4.4；ADR-023）。

`optical_depth(z0_agl, rd_z, L)`：指数霾（标高 H = 1500 m）的 Quilez 解析积分 + 平顶雾层 + 云底以下的均匀降水层；
光学层一律以 `coordinate.ground.zM` 为 AGL 基准（两端无需 DTM）。透过率 T = exp(−tau)。Kim 波长换算以
V2 = ln50 / sigma_bg（km）代入。与 `engine/environment/atmosphere/optics.ts` 逐行对应。

D2（WP-09，AWR-04 §6.3 第 4 步、ADR-101）：按波长的消光 `sigma_lambda(d, λ, rho_v)` 与路径光学厚度 `optical_depth_lambda_arr`：
气溶胶（霾与雾层）按 Kim 指数 `(λ/550)^−q(V2)` 缩放，降水与波长无关；LWIR（λ ≥ 8 µm）另加水汽项
`sigma_wv = (0.03 + 0.012·rho_v) /km`，rho_v（g/m³）由相对湿度与日均气温按 Magnus 式求得（`water_vapour_gm3`）。550 nm 与 D1 逐位相同。
"""

from __future__ import annotations

import math

import numpy as np

from ..weather.derive import K_V2, Derived
from ..weather.presets import C

__all__ = ["FLAG_BELOW_CLOUD_PRECIP", "FLAG_IN_FOG_LAYER", "H_HAZE", "LWIR_MIN_NM", "flat_len", "kim_factor", "kim_q", "lidar_two_way",
           "optical_depth", "optical_depth_arr", "optical_depth_lambda_arr", "sigma_at", "sigma_at_arr", "sigma_lambda", "sigma_wv_per_m",
           "water_vapour_gm3"]

H_HAZE = float(C["haze_scale_h_m"])
FLAG_IN_FOG_LAYER = 16  # rt/enums.json EnvFlags
FLAG_BELOW_CLOUD_PRECIP = 32


def flat_len(ro_z: float, rd_z: float, L: float, top: float) -> float:
    """射线段 [0, L] 位于 z < top 部分的长度。"""
    t0, t1 = 0.0, L
    if abs(rd_z) > 1e-5:
        tc = (top - ro_z) / rd_z
        if rd_z > 0:
            t1 = min(t1, tc)
        else:
            t0 = max(t0, tc)
    elif ro_z > top:
        return 0.0
    return max(t1 - t0, 0.0)


def optical_depth(z0_agl: float, rd_z: float, L: float, d: Derived, fog_top: float, cloud_base: float, H: float = H_HAZE) -> float:
    a = d.sigma_haze0 * math.exp(-z0_agl / H)
    k = rd_z * L / H
    od = a * L * ((1.0 - math.exp(-k)) / k if abs(k) > 1e-4 else 1.0)
    if d.sigma_fog > 0:
        od += d.sigma_fog * flat_len(z0_agl, rd_z, L, fog_top)
    if d.sigma_precip > 0:
        od += d.sigma_precip * flat_len(z0_agl, rd_z, L, cloud_base)
    return od


def sigma_at(z_agl: float, d: Derived, fog_top: float, cloud_base: float) -> tuple[float, int]:
    s = d.sigma_haze0 * math.exp(-z_agl / H_HAZE)
    flags = 0
    if z_agl < fog_top:
        s += d.sigma_fog
        flags |= FLAG_IN_FOG_LAYER
    if z_agl < cloud_base:
        s += d.sigma_precip
        if d.sigma_precip > 0:
            flags |= FLAG_BELOW_CLOUD_PRECIP
    return s, flags


def kim_q(v2_km: float) -> float:
    if v2_km > 50:
        return 1.6
    if v2_km > 6:
        return 1.3
    if v2_km > 1:
        return 0.16 * v2_km + 0.34
    if v2_km > 0.5:
        return v2_km - 0.5
    return 0.0


LWIR_MIN_NM = 8000.0


def water_vapour_gm3(rh: float, t_c: float) -> float:
    """水汽密度 rho_v（g/m³）：Magnus 饱和水汽压 `e_s = 6.112·exp(17.62·T/(243.12 + T))` hPa，`rho_v = 216.7·rh·e_s/(T + 273.15)`。"""
    t = float(t_c)
    es = 6.112 * math.exp(17.62 * t / (243.12 + t))
    return 216.7 * min(1.0, max(0.0, float(rh))) * es / (t + 273.15)


def sigma_wv_per_m(rho_v_gm3: float) -> float:
    """LWIR 水汽吸收（1/m）：0.03 + 0.012·rho_v /km（AWR-04 §6.3；中纬度夏季约 0.15–0.27 /km）。"""
    return (0.03 + 0.012 * max(0.0, float(rho_v_gm3))) / 1000.0


def kim_factor(d: Derived, lam_nm: float) -> float:
    """气溶胶消光的波长比 (λ/550)^−q，q 按 Kim 由 V2 = ln50/sigma_bg（km）给出。"""
    v2_km = K_V2 / d.sigma_bg / 1000.0
    return (lam_nm / 550.0) ** -kim_q(v2_km)


def sigma_lambda(d: Derived, lam_nm: float, rho_v_gm3: float = 0.0) -> float:
    """地面点消光（1/m）：sigma_bg·(λ/550)^−q + sigma_precip，LWIR 另加水汽项。"""
    v2_km = K_V2 / d.sigma_bg / 1000.0
    s = d.sigma_bg * (lam_nm / 550.0) ** -kim_q(v2_km) + d.sigma_precip
    if lam_nm >= LWIR_MIN_NM:
        s += sigma_wv_per_m(rho_v_gm3)
    return s


def lidar_two_way(d: Derived, lam_nm: float, r_m: float) -> float:
    return math.exp(-2.0 * sigma_lambda(d, lam_nm) * r_m)


# ---------------------------------------------------------------- 向量化（query、optical_depth 批量）
def _flat_len_arr(ro_z: np.ndarray, rd_z: np.ndarray, L: np.ndarray, top: float) -> np.ndarray:
    big = np.abs(rd_z) > 1e-5
    with np.errstate(divide="ignore", invalid="ignore"):
        tc = np.where(big, (top - ro_z) / np.where(big, rd_z, 1.0), 0.0)
    t1 = np.where(big & (rd_z > 0), np.minimum(L, tc), L)
    t0 = np.where(big & (rd_z <= 0), np.maximum(0.0, tc), 0.0)
    out = np.maximum(t1 - t0, 0.0)
    return np.where(~big & (ro_z > top), 0.0, out)


def optical_depth_arr(z0_agl: np.ndarray, rd_z: np.ndarray, L: np.ndarray, d: Derived, fog_top: float, cloud_base: float) -> np.ndarray:
    z0_agl, rd_z, L = (np.asarray(x, np.float64) for x in (z0_agl, rd_z, L))
    a = d.sigma_haze0 * np.exp(-z0_agl / H_HAZE)
    k = rd_z * L / H_HAZE
    small = np.abs(k) <= 1e-4
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(small, 1.0, (1.0 - np.exp(-k)) / np.where(small, 1.0, k))
    od = a * L * f
    if d.sigma_fog > 0:
        od = od + d.sigma_fog * _flat_len_arr(z0_agl, rd_z, L, fog_top)
    if d.sigma_precip > 0:
        od = od + d.sigma_precip * _flat_len_arr(z0_agl, rd_z, L, cloud_base)
    return od


def optical_depth_lambda_arr(z0_agl: np.ndarray, rd_z: np.ndarray, L: np.ndarray, d: Derived, fog_top: float, cloud_base: float,
                             lam_nm: float, rho_v_gm3: float = 0.0) -> np.ndarray:
    """按波长的路径光学厚度：气溶胶（霾与雾层）× Kim 波长比 + 降水 + LWIR 水汽（沿程均匀）；550 nm 时与 optical_depth_arr 逐位相同。"""
    if lam_nm == 550.0:
        return optical_depth_arr(z0_agl, rd_z, L, d, fog_top, cloud_base)
    z0_agl, rd_z, L = (np.asarray(x, np.float64) for x in (z0_agl, rd_z, L))
    a = d.sigma_haze0 * np.exp(-z0_agl / H_HAZE)
    k = rd_z * L / H_HAZE
    small = np.abs(k) <= 1e-4
    with np.errstate(divide="ignore", invalid="ignore"):
        f = np.where(small, 1.0, (1.0 - np.exp(-k)) / np.where(small, 1.0, k))
    aer = a * L * f
    if d.sigma_fog > 0:
        aer = aer + d.sigma_fog * _flat_len_arr(z0_agl, rd_z, L, fog_top)
    od = kim_factor(d, lam_nm) * aer
    if d.sigma_precip > 0:
        od = od + d.sigma_precip * _flat_len_arr(z0_agl, rd_z, L, cloud_base)
    if lam_nm >= LWIR_MIN_NM:
        od = od + sigma_wv_per_m(rho_v_gm3) * L
    return od


def sigma_at_arr(z_agl: np.ndarray, d: Derived, fog_top: float, cloud_base: float) -> tuple[np.ndarray, np.ndarray]:
    z = np.asarray(z_agl, np.float64)
    s = d.sigma_haze0 * np.exp(-z / H_HAZE)
    flags = np.zeros(z.shape, np.uint8)
    fog = z < fog_top
    s = s + np.where(fog, d.sigma_fog, 0.0)
    flags |= np.where(fog, FLAG_IN_FOG_LAYER, 0).astype(np.uint8)
    below = z < cloud_base
    s = s + np.where(below, d.sigma_precip, 0.0)
    if d.sigma_precip > 0:
        flags |= np.where(below, FLAG_BELOW_CLOUD_PRECIP, 0).astype(np.uint8)
    return s, flags
