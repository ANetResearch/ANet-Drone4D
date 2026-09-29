"""派生量 derive(s)（M07-FR-005；M07 §6.3.3；g06 §4.1；ADR-023）。

能见度只有 MOR 一个真值：sigma = ln20 / MOR（K_MOR = ln 20，presets.json `constants.k_mor`）；状态量 `mor_bg_m` 不含降水，
降水消光加性叠加，`mor_m` 为界面主数字（地面总 MOR）。3.912 只用于传感器波长换算（atmosphere.optics）。
与 `engine/environment/state/derive.ts` 逐行对应，golden `derive.json`（12 预设 + 1000 组随机状态）。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ..wind.profile import profile_cfg
from .presets import BASE, COVER, DUST, FOG_TOP, GUST_AMP, MOR_BG, RAIN, SIGMA_REF, SNOW, SPEED_REF, TOP, W_MEAN, C

__all__ = ["DERIVED_KEYS", "Derived", "clamp", "derive", "mor_bg_from_total", "puddle_target", "smoothstep"]

K_MOR = float(C["k_mor"])
K_V2 = float(C["k_v2"])

DERIVED_KEYS = ("rain_eff_mmh", "snow_eff_mmh", "sigma_rain", "sigma_snow", "sigma_precip", "sigma_bg", "sigma_fog", "sigma_haze0",
                "sigma_ground", "mor_m", "rain_k", "snow_k", "sun_vis", "mp_lambda", "cloud_od", "v_rain_mps", "v_snow_mps",
                "wet_target", "vmax_vis_mps")


def smoothstep(a: float, b: float, x: float) -> float:
    t = min(max((x - a) / (b - a), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def clamp(x: float, a: float, b: float) -> float:
    return min(max(x, a), b)


@dataclass(slots=True)
class Derived:
    rain_eff_mmh: float = 0.0
    snow_eff_mmh: float = 0.0
    sigma_rain: float = 0.0
    sigma_snow: float = 0.0
    sigma_precip: float = 0.0
    sigma_bg: float = 0.0
    sigma_fog: float = 0.0
    sigma_haze0: float = 0.0
    sigma_ground: float = 0.0
    mor_m: float = 0.0
    rain_k: float = 0.0
    snow_k: float = 0.0
    sun_vis: float = 0.0
    mp_lambda: float = 0.0
    cloud_od: float = 0.0
    v_rain_mps: float = 0.0
    v_snow_mps: float = 0.0
    wet_target: float = 0.0
    vmax_vis_mps: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return asdict(self)


_G0, _G1 = (float(x) for x in C["precip_gate_cover"])
_RA, _RB = float(C["rain_sigma_coef"]), float(C["rain_sigma_exp"])
_SA, _SB = float(C["snow_sigma_coef"]), float(C["snow_sigma_exp"])
_MOR_MIN, _MOR_MAX = float(C["mor_bg_min_m"]), float(C["mor_bg_max_m"])
_FOG_FRAC = float(C["fog_fraction"])
_WET_GAIN = float(C["wet_gain"])
_PUDDLE_EXP = float(C["puddle_exp"])
_LOG51 = math.log(51.0)


def derive(s: Sequence[float], prof: Mapping[str, Any] | None = None, out: Derived | None = None) -> Derived:
    d = Derived() if out is None else out
    gate = smoothstep(_G0, _G1, s[COVER])
    R = s[RAIN] * gate
    S = s[SNOW] * gate
    d.rain_eff_mmh = R
    d.snow_eff_mmh = S
    d.sigma_rain = _RA * R ** _RB if R > 0 else 0.0
    d.sigma_snow = _SA * S ** _SB if S > 0 else 0.0
    d.sigma_precip = d.sigma_rain + d.sigma_snow
    d.sigma_bg = K_MOR / clamp(s[MOR_BG], _MOR_MIN, _MOR_MAX)
    d.sigma_fog = _FOG_FRAC * d.sigma_bg if s[FOG_TOP] > 0 else 0.0
    d.sigma_haze0 = d.sigma_bg - d.sigma_fog
    d.sigma_ground = d.sigma_haze0 + d.sigma_fog + d.sigma_precip
    d.mor_m = K_MOR / d.sigma_ground
    d.rain_k = clamp(math.log1p(R) / _LOG51, 0.0, 1.0)
    d.snow_k = clamp(math.log1p(10.0 * S) / _LOG51, 0.0, 1.0)
    d.sun_vis = (1.0 - 0.9 * s[COVER] ** 1.5) * (1.0 - 0.6 * s[DUST])
    d.mp_lambda = 4.1 * max(R, 0.1) ** -0.21
    d.cloud_od = clamp((s[TOP] - s[BASE]) * 0.022 * 0.35, 0.0, 6.0)
    d.v_rain_mps = 9.65 - 10.3 * math.exp(-0.6 * clamp(4.0 / d.mp_lambda, 0.3, 5.0))
    d.v_snow_mps = 0.6 + 0.9 * d.snow_k
    d.wet_target = clamp(_WET_GAIN * d.rain_k, 0.0, 1.0)
    d.vmax_vis_mps = s[SPEED_REF] * profile_cfg(150.0, prof) + s[GUST_AMP] + 3.0 * s[SIGMA_REF] + abs(s[W_MEAN])
    return d


def puddle_target(d: Derived) -> float:
    return d.wet_target ** _PUDDLE_EXP


def mor_bg_from_total(mor_total: float, rain_mmh: float, snow_mmh: float, cover: float) -> float:
    """预设作者换算：METAR 式总 MOR -> 背景 MOR（不含降水，M07 §6.5）。"""
    from .presets import presets

    s = presets().default_vector()
    s[COVER], s[TOP], s[BASE], s[RAIN], s[SNOW] = cover, 0.0, 0.0, rain_mmh, snow_mmh
    s[MOR_BG], s[FOG_TOP], s[DUST] = _MOR_MAX, 0.0, 0.0
    d = derive(s)
    sbg = max(K_MOR / mor_total - d.sigma_precip, K_MOR / _MOR_MAX)
    return K_MOR / sbg
