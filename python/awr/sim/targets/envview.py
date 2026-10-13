"""识别物判据所需的环境样本（M07 EnvPerceptionView 的子集；M18-M19 PRD §6.11；d2-WP-09-to-M18）。

`TargetEnv`：照度档（day、low、night）、MOR、550 nm 消光系数、24 带 A 计权声学背景与总级、背景剖面。来源依次为：
`ctx.env.sky_sample(t)`（M07 WP-09，1 s【仿真】量化）→ 回退（未装配 M07 时：日间、τ = 1、rural 日间背景，并计数
`env_fallback`，PRD §6.11 最后一段）。`day_night(profile, mor)` 给出建议半径用的日间与夜间样本（AWR-04 §7.3 剖面）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray

__all__ = ["K_MOR", "TargetEnv", "conservative", "day_night", "env_at", "fallback_env", "profile_bands"]

K_MOR = math.log(20.0)  # ADR-023：sigma_550 = ln 20 / MOR
_LEVELS = {"rural": (40.0, 30.0), "suburban": (50.0, 40.0), "urban": (60.0, 50.0)}


def _shape(profile: str) -> NDArray[np.float64]:
    try:
        from awr.environment.sky.acoustic import REL_SHAPE_DB

        return np.asarray(REL_SHAPE_DB[profile], float)
    except Exception:  # M07 未装配：宽平谱（能量和 0 dB）
        return np.full(24, -10.0 * math.log10(24.0))


def profile_bands(profile: str, total_dba: float) -> NDArray[np.float64]:
    return float(total_dba) + _shape(profile)


@dataclass(frozen=True)
class TargetEnv:
    band: str                         # day | low | night
    mor_m: float
    l_bg_bands: NDArray[np.float64]   # [24] A 计权带级 dB(A)
    l_bg_dba: float
    profile: str = "rural"
    illum_lx: float = 1e5
    t_ns: int = 0
    fallback: bool = False
    rain_mmph: float = 0.0            # D2.1：雷达雨衰（P1 radar 敏感体与 M19 雷达场）

    @property
    def sigma550(self) -> float:
        return K_MOR / self.mor_m if self.mor_m > 0 and math.isfinite(self.mor_m) else 0.0

    def tau550(self, r_m: float | NDArray) -> float | NDArray:
        return np.exp(-self.sigma550 * np.asarray(r_m, float))

    def key(self) -> tuple:
        """有效半径缓存键的环境部分：照度档、背景总级取整（d2-WP-09 第 3 条）、MOR 取 100 m 网格。"""
        return (self.band, round(self.l_bg_dba), round(min(self.mor_m, 1e6) / 100.0))


def fallback_env(t_ns: int = 0, profile: str = "rural") -> TargetEnv:
    day = _LEVELS[profile][0]
    return TargetEnv("day", math.inf, profile_bands(profile, day), day, profile, 1e5, t_ns, True)


def env_at(env: Any, t_ns: int) -> TargetEnv:
    """M07 环境服务在 t 的样本；不可用时回退。"""
    fn = getattr(env, "sky_sample", None)
    if callable(fn):
        try:
            s = fn(int(t_ns))
            bands = np.asarray(getattr(s, "l_bg_bands_dba", None), float)
            if bands.shape != (24,):
                bands = profile_bands(str(s.acoustic_profile), float(s.l_bg_dba))
            return TargetEnv(str(s.band), float(s.mor_m), bands, float(s.l_bg_dba), str(s.acoustic_profile), float(s.illum_lx), int(t_ns),
                             False, float(getattr(s, "rain_mmph", 0.0) or 0.0))
        except Exception:
            pass
    return fallback_env(int(t_ns))


def day_night(profile: str, mor_m: float) -> tuple[TargetEnv, TargetEnv]:
    """建议半径的日间与夜间样本（剖面日夜级，同一 MOR）。"""
    d, n = _LEVELS.get(profile, _LEVELS["rural"])
    return (TargetEnv("day", mor_m, profile_bands(profile, d), d, profile, 1e5),
            TargetEnv("night", mor_m, profile_bands(profile, n), n, profile, 0.001))


def conservative(e: TargetEnv) -> TargetEnv:
    """保守口径的环境部分：背景 −3 dB（警觉度 +0.2 由判据处理，AWR-04 §7.3）。"""
    return replace(e, l_bg_bands=e.l_bg_bands - 3.0, l_bg_dba=e.l_bg_dba - 3.0)
