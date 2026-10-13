"""有效体、建议半径与禁入体（M18-FR-016、FR-017；M18-M19 PRD §6.3.3；AWR-04 §7.3、§8.1、§9.2；契约
`target/keepout.schema.json`、`target/target.schema.json#/$defs/suggested_radius`）。

| 函数 | 规则 |
|---|---|
| `effective_radius(t, body, model, env, conservative=)` | 视觉 `min(radius_max, r_vis)`；声学 `min(radius_max, r_ac)`；保守口径 a + 0.2、L_bg − 3 dB |
| `suggested_radius(t, model, env)` | 不受 radius_max 截断（上限 2000 m）：`{now_m, day_m, night_m, conservative_max_m}`；采纳值 `ceil(cons/10)·10` |
| `keepout_volumes(t, state, mode, margin_m, conservative=, moving_as_circle=)` | geometric：启用配置体外扩 margin；physical：每体半径取 `min(radius_max, max_model r_eff,cons)` 后外扩；运动识别物的扇形按整圆 |

有效半径按参数键缓存（`EffCache`，≤ 256 条）：键为（判据参数、机型、环境键、警觉度两位小数、保守），环境背景变化 ≥ 1 dB
或警觉度变化 ≥ 0.05 时自然失效（键变化）。rf 敏感体只入 schema，不产生禁入体（M18-FR-026）；radar 敏感体在识别物配置 `radar_rx`
时（P1，M18-FR-030）按雷达准则给出有效半径并产生禁入体，未配置时只入 schema。D2.1：声学有效半径计入类别频段修正
`hearing_band_adj_db`（缺省不修正，与 D2 逐位一致）。
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .acoustic import acoustic_radius, threshold_bands
from .envview import TargetEnv, conservative, day_night, fallback_env
from .geometry import Volume, body_volumes, is_moving
from .model import TargetConfig, TargetState1, band_adj_of, radar_rx_of
from .radar import gamma_rain_db_km, radar_radius
from .signature import P600, VehicleSignature
from .visual import visual_radius

__all__ = ["DEFAULT_MARGIN_M", "SUGGEST_CAP_M", "EffCache", "SuggestedRadius", "effective_radius", "keepout_volumes", "max_horizontal_radius",
           "radius_kinds", "suggested_radius"]

SUGGEST_CAP_M = 2000.0
DEFAULT_MARGIN_M = 20.0 + 3.0 * 0.02  # m = 20 m + 3sigma_hold（RTK 档，AWR-04 §8.1；M20 传入实际值）


class EffCache:
    """有效半径 LRU（≤ 256 条，M18-NFR-002）；命中率供 M18-AC-015。"""

    def __init__(self, cap: int = 256) -> None:
        self.cap = cap
        self.d: OrderedDict[tuple, float] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple, fn: Any) -> float:
        v = self.d.get(key)
        if v is not None:
            self.hits += 1
            self.d.move_to_end(key)
            return v
        self.misses += 1
        v = float(fn())
        self.d[key] = v
        if len(self.d) > self.cap:
            self.d.popitem(last=False)
        return v

    @property
    def hit_rate(self) -> float:
        n = self.hits + self.misses
        return self.hits / n if n else 0.0


_GLOBAL = EffCache()


def _param_key(t: TargetConfig, body: dict) -> tuple:
    adj = t.raw.get("hearing_band_adj_db")
    rx = radar_rx_of(t.raw) if body.get("kind") == "radar" else None
    return (body.get("kind"), round(float(t.raw.get("visual_theta0_arcmin", 1.0)), 3), round(float(t.raw.get("hearing_th_dba", 20.0)), 2),
            round(float(t.raw.get("acoustic_adv_db", 0.0)), 2), tuple(adj) if isinstance(adj, list) else None,
            tuple(sorted(rx.items())) if rx else None)


def radius_kinds(t: TargetConfig) -> tuple[str, ...]:
    """产生有效体与禁入体的敏感体类别：视觉、声学；配置 `radar_rx` 时另有雷达（P1）。"""
    return ("visual", "acoustic", "radar") if radar_rx_of(t.raw) else ("visual", "acoustic")


def _raw_radius(t: TargetConfig, body: dict, model: VehicleSignature, env: TargetEnv, cons: bool, alertness: float | None) -> float:
    a = float(t.raw.get("alertness", 0.5)) if alertness is None else float(alertness)
    kind = body.get("kind")
    if kind == "visual":
        return visual_radius(model, env, float(t.raw.get("visual_theta0_arcmin", 1.0)), a, conservative=cons, cap_m=SUGGEST_CAP_M)
    if kind == "acoustic":
        e = conservative(env) if cons else env
        aa = min(1.2, a + 0.2) if cons else a
        thr = threshold_bands(e.l_bg_bands, model.masking_db(), float(t.raw.get("hearing_th_dba", 20.0)), aa,
                              float(t.raw.get("acoustic_adv_db", 0.0)))
        return min(SUGGEST_CAP_M, acoustic_radius(model.lw_bands(), thr, adj_db=band_adj_of(t.raw)))
    rx = radar_rx_of(t.raw)
    if kind == "radar" and rx is not None:
        return min(SUGGEST_CAP_M, radar_radius(rx, float(getattr(model, "rcs_m2", 0.05)), gamma_rain_db_km(getattr(env, "rain_mmph", 0.0))))
    return float(body.get("radius_max_m", 0.0))


def effective_radius(t: TargetConfig, body: int, model: VehicleSignature, env: TargetEnv, *, conservative: bool,
                     alertness: float | None = None, cache: EffCache | None = None, truncate: bool = True) -> float:
    """第 `body` 个敏感体对机型 `model` 的有效半径（m）：`min(radius_max, r_eff)`（`truncate = False` 时不截断，上限 2000 m）。"""
    b = t.raw["sensitivity"][body]
    a = float(t.raw.get("alertness", 0.5)) if alertness is None else float(alertness)
    c = cache or _GLOBAL
    key = (_param_key(t, b), model.model_id, model.lights_nav or model.lights_strobe, env.key(), env.profile, round(a, 2), bool(conservative),
           round(float(getattr(env, "rain_mmph", 0.0)), 1) if b.get("kind") == "radar" else None)
    r = c.get(key, lambda: _raw_radius(t, b, model, env, conservative, a))
    return min(float(b["radius_max_m"]), r) if truncate else r


@dataclass(frozen=True)
class SuggestedRadius:
    model_id: str
    basis: dict
    volumes: list[dict]

    def to_wire(self) -> dict:
        return {"model_id": self.model_id, "basis": self.basis, "volumes": self.volumes}


def suggested_radius(t: TargetConfig, model: VehicleSignature, cal: TargetEnv | None = None, *, cache: EffCache | None = None,
                     alertness: float | None = None) -> SuggestedRadius:
    """physical 模型给出的建议半径（17 §17.6.5；SB-46）：只对视觉与声学体；保守口径为 AWR-04 §7.3。"""
    env = cal or fallback_env()
    d_env, n_env = day_night(env.profile, env.mor_m)
    a = float(t.raw.get("alertness", 0.5)) if alertness is None else float(alertness)
    vols = []
    for i, b in enumerate(t.raw.get("sensitivity") or []):
        if b.get("kind") not in ("visual", "acoustic"):
            continue
        kw = {"cache": cache, "alertness": a, "truncate": False}
        now = effective_radius(t, i, model, env, conservative=False, **kw)
        day = effective_radius(t, i, model, d_env, conservative=False, **kw)
        night = effective_radius(t, i, model, n_env, conservative=False, **kw)
        cons = max(effective_radius(t, i, model, d_env, conservative=True, **kw),
                   effective_radius(t, i, model, n_env, conservative=True, **kw))
        vols.append({"idx": i, "kind": b["kind"], "radius_cfg_m": float(b["radius_max_m"]),
                     "radius_suggested_m": float(min(3000.0, math.ceil(cons / 10.0) * 10.0)),
                     "now_m": round(now, 2), "day_m": round(day, 2), "night_m": round(night, 2), "conservative_max_m": round(cons, 2),
                     "margin_db": None})
    basis = {"light": env.band, "l_bg_dba": round(env.l_bg_dba, 2), "alertness_used": round(min(1.2, a + 0.2), 3), "conservative": True}
    return SuggestedRadius(model.model_id, basis, vols)


def keepout_volumes(t: TargetConfig, state: TargetState1, mode: str, margin_m: float, *, conservative: bool = True,
                    moving_as_circle: bool = True, models: Sequence[VehicleSignature] | None = None, env: TargetEnv | None = None,
                    cache: EffCache | None = None) -> list[Volume]:
    """禁入体（M18-FR-016；签名冻结于 WP-01，追加仅关键字参数 `models`、`env`、`cache`）。

    geometric：启用的视觉、声学配置体外扩 `margin_m`；physical：每体半径取 `min(radius_max, r_eff)`（`conservative` 时为保守
    口径；机型取 `models` 中的最大值，缺省 P600；环境缺省取日间与夜间的较大值）后外扩。运动识别物（`moving_as_circle` 且
    `motion.mode ≠ static` 或速度 > 0.1 m/s）的扇形按整圆柱处理。"""
    bodies = list(t.raw.get("sensitivity") or [])
    kinds = radius_kinds(t)
    radius_of: list[float | None] | None = None
    if mode == "physical":
        ms = list(models) if models else [P600]
        envs = [env] if env is not None else list(day_night("rural", math.inf))
        radius_of = []
        for i, b in enumerate(bodies):
            if b.get("kind") not in kinds:
                radius_of.append(None)
                continue
            r = max(effective_radius(t, i, m, e, conservative=conservative, alertness=state.alertness, cache=cache) for m in ms for e in envs)
            radius_of.append(max(r, 0.5))
    moving = is_moving(t.raw.get("motion"), state.speed_mps) or state.moving
    return body_volumes(bodies, state.pos, state.yaw_rad, margin_m=float(margin_m), moving_as_circle=bool(moving_as_circle and moving),
                        radius_of=radius_of, kinds=kinds)


def max_horizontal_radius(vols: Sequence[Volume]) -> float:
    return float(max((v.r_m for v in vols), default=0.0))

