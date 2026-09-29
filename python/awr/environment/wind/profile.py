"""L0 风廓线 f(z_agl)（M07-FR-009；M07 §6.3.6；g06 §5.2；WindNinja windProfile.cpp L73–76）。

`log`：z ≤ d + z0 时为 0，否则 ln((z − d)/z0) / ln((z_ref − d)/z0)；`power`：(max(z, 0)/z_ref)^alpha；`uniform`：z > 0 时为 1。
物理侧 z_agl = z − dtm(x, y)（地形跟随）；前端同式。`f_adv = profile(adv_height_m)` 为锋面与湍流盒的对流速度系数。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np

from ..weather.presets import PRESETS

__all__ = ["DEFAULT_PROFILE", "f_adv", "profile", "profile_arr", "profile_cfg"]

DEFAULT_PROFILE: dict[str, Any] = dict(PRESETS["defaults"]["config"]["wind"]["profile"])


def profile(z_agl: float, kind: str = "log", z_ref: float = 10.0, z0: float = 0.5, d: float = 0.0, alpha: float = 0.25) -> float:
    if kind == "uniform":
        return 1.0 if z_agl > 0 else 0.0
    if kind == "power":
        return (max(z_agl, 0.0) / z_ref) ** alpha
    if z_agl <= d + z0:
        return 0.0
    return math.log((z_agl - d) / z0) / math.log((z_ref - d) / z0)


def profile_cfg(z_agl: float, cfg: Mapping[str, Any] | None = None) -> float:
    c = DEFAULT_PROFILE if cfg is None else cfg
    return profile(z_agl, c["kind"], c["z_ref_m"], c["z0_m"], c["d_m"], c["alpha"])


def f_adv(cfg: Mapping[str, Any] | None = None) -> float:
    c = DEFAULT_PROFILE if cfg is None else cfg
    return profile_cfg(float(c["adv_height_m"]), c)


def profile_arr(z_agl: np.ndarray, cfg: Mapping[str, Any] | None = None, out: np.ndarray | None = None) -> np.ndarray:
    """向量化廓线（与标量同式）。"""
    c = DEFAULT_PROFILE if cfg is None else cfg
    z = np.asarray(z_agl, np.float64)
    if out is None:
        out = np.empty_like(z)
    kind = c["kind"]
    if kind == "uniform":
        np.copyto(out, (z > 0).astype(np.float64))
        return out
    if kind == "power":
        np.power(np.maximum(z, 0.0) / float(c["z_ref_m"]), float(c["alpha"]), out=out)
        return out
    d, z0 = float(c["d_m"]), float(c["z0_m"])
    den = math.log((float(c["z_ref_m"]) - d) / z0)
    ok = z > d + z0
    with np.errstate(divide="ignore", invalid="ignore"):
        v = np.log(np.where(ok, (z - d) / z0, 1.0)) / den
    np.copyto(out, np.where(ok, v, 0.0))
    return out
