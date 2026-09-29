"""合同网打分（M14 §6.10.3–§6.10.5；M14-FR-024；ADR-036）。V0.6 起并入 `awr/swarm/allocation/score.py`（FR-075）。

```text
U = w_c·conf_expected − w_t·eta_s/η_norm − w_e·energy_wh/E_norm − w_l·load − w_r·risk        不可行 → U = −∞
w = {conf 1.0, eta 0.6, energy 0.3, load 0.2, risk 0.5}
η_norm = R_ref / v_cruise（R_ref = 600 m）；E_norm = 0.10 · usable_frac · capacity_wh（battery 为 null 时能量项取 0）
U 量化：round(U, 3)；同分 agent_no 小者优先

c01(x) = min(1, max(0, x))
risk = 0.5·c01(wind/L.wind) + 0.3·c01(rain/L.rain) + 0.2·c01((mor_ref − mor)/(mor_ref − L.visibility))，mor_ref = 10·L.visibility
conf_expected（退化式）= P0·exp(−(alt_agl/r_fp)²)·exp(−3.912·alt_agl/mor)
```
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["DEFAULT_WEIGHTS", "E_NORM_FRAC", "R_REF_M", "EnvAtTarget", "Limits", "ScoreNorm", "Weights", "c01",
           "conf_expected_fallback", "norm_for", "rank_key", "risk_of", "score"]

R_REF_M = 600.0
E_NORM_FRAC = 0.10


@dataclass(frozen=True)
class Weights:
    conf: float = 1.0
    eta: float = 0.6
    energy: float = 0.3
    load: float = 0.2
    risk: float = 0.5

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> Weights:
        if not d:
            return cls()
        base = cls()
        return cls(**{k: float(d.get(k, getattr(base, k))) for k in ("conf", "eta", "energy", "load", "risk")})


DEFAULT_WEIGHTS = Weights()


@dataclass(frozen=True)
class ScoreNorm:
    eta_s: float
    energy_wh: float | None  # None → 能量项为 0


@dataclass(frozen=True)
class Limits:
    wind_mps: float = 12.0
    rain_mmh: float = 10.0
    visibility_m: float = 200.0

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> Limits:
        d = d or {}
        return cls(float(d.get("wind_mps", 12.0)), float(d.get("rain_mmh", 10.0)), float(d.get("visibility_m", 200.0)))


@dataclass(frozen=True)
class EnvAtTarget:
    wind_mps: float = 0.0
    rain_mmh: float = 0.0
    mor_m: float = 20000.0


def _val(x: Any) -> Any:
    """params.yaml 的取值可能是 `{value, conf, src}` 对象。"""
    return x.get("value") if isinstance(x, Mapping) and "value" in x else x


def norm_for(profile: Mapping[str, Any]) -> ScoreNorm:
    """由机型 profile（`awr.vehicle.v1` 字典）推导归一化常数（§6.10.3）；`anet.score_norm` 存在时直接采用。"""
    sn = ((profile.get("anet") or {}).get("score_norm")) if isinstance(profile.get("anet"), Mapping) else None
    if isinstance(sn, Mapping) and "eta_s" in sn:
        e = sn.get("energy_wh")
        return ScoreNorm(float(sn["eta_s"]), None if e is None else float(e))
    lim = profile.get("limits_profiles") or {}
    name = lim.get("default") if isinstance(lim, Mapping) else None
    px4 = ((lim.get(name) or {}).get("px4_params") or {}) if name else {}
    v = float(px4.get("MPC_XY_CRUISE") or px4.get("MPC_XY_VEL_MAX") or 5.0)
    v = min(v, float(px4.get("MPC_XY_VEL_MAX") or v))
    bat = profile.get("battery")
    e_norm = None
    if isinstance(bat, Mapping):
        e_use = float(_val(bat.get("usable_frac")) or 0.0) * float(_val(bat.get("capacity_wh")) or 0.0)
        e_norm = E_NORM_FRAC * e_use if e_use > 0 else None
    return ScoreNorm(R_REF_M / v, e_norm)


def c01(x: float) -> float:
    return min(1.0, max(0.0, x))


def risk_of(env: EnvAtTarget, limits: Limits) -> float:
    """分项限幅的风险（修正 d05 整体限幅缺陷，§6.10.4）。"""
    mor_ref = 10.0 * limits.visibility_m
    vis_term = c01((mor_ref - env.mor_m) / (mor_ref - limits.visibility_m)) if mor_ref > limits.visibility_m else 0.0
    return (0.5 * c01(env.wind_mps / limits.wind_mps if limits.wind_mps > 0 else 1.0)
            + 0.3 * c01(env.rain_mmh / limits.rain_mmh if limits.rain_mmh > 0 else 1.0)
            + 0.2 * vis_term)


def conf_expected_fallback(p0: float, r_fp_m: float, alt_agl_m: float, mor_m: float) -> float:
    """估价回复缺 `conf_expected` 时的退化式（§6.10.5；LOS 取 1）。"""
    vis = math.exp(-3.912 * alt_agl_m / mor_m) if mor_m > 0 else 0.0
    return p0 * math.exp(-((alt_agl_m / r_fp_m) ** 2)) * vis


def score(q: Mapping[str, Any], norm: ScoreNorm, w: Weights, risk: float) -> float:
    """U（量化到 1e-3）；不可行为 −inf。q 需要 feasible、conf_expected、eta_s、energy_wh、load。"""
    if not bool(q.get("feasible")):
        return -math.inf
    energy = 0.0 if norm.energy_wh is None or norm.energy_wh <= 0 else float(q.get("energy_wh", 0.0)) / norm.energy_wh
    u = (w.conf * float(q.get("conf_expected", 0.0)) - w.eta * float(q.get("eta_s", 0.0)) / norm.eta_s
         - w.energy * energy - w.load * float(q.get("load", 0.0)) - w.risk * float(risk))
    return round(u, 3)


def rank_key(score_: float, agent_no: int) -> tuple[float, int]:
    """排序键：U 降序、agent_no 升序（12 §6.10 规则 3）。"""
    return (-score_ if math.isfinite(score_) else math.inf, agent_no)
