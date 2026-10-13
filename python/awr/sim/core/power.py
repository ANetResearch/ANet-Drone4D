"""按构型分派的功率模型（M08-FR-214、FR-215；ADR-052、ADR-096；M08 §15.3.2）。

- `register_power_model(frame_kind, model)`（`fleet/stages/registry.py`）：每个构型一次（0 多旋翼、1 固定翼、2 VTOL），重复登记失败；
  M09 登记多旋翼模型（D1 公式 `P = P_hover·(T/T_hover)^1.5` 原样迁入），M21 登记 `fixed_wing` 与 `vtol`（AWR-04 §5.3 的 `P_elec`
  与 VTOL 各模式功率）；
- `power_by_frame(S, slots, ctx)`：按 `frame_kind` 分组调用各模型的 `power_w`，结果按输入 slot 次序（f64[n]，W）。M09 battery stage
  的 SOC 积分、估价、能量 RTL 与任务能量预检（`EnergyModel.path_wh`）都经它或 `leg_power_w` 取功率（一处功率、处处同源）；
- 多旋翼没有登记模型时用本模块的 `MultirotorPower`（与 D1 M09 同一公式，只作兜底：walking skeleton 与 M08 单模块测试），固定翼
  与 VTOL 没有登记模型的机体拒绝 spawn（110，detail NO_POWER_MODEL，`SimCore.admit_add`）。
"""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ..fleet import kernels_l1 as K
from ..fleet.stages import registry as R

__all__ = ["CLIMB_ETA", "MultirotorPower", "PowerModel", "hover_power_w", "leg_power_w", "power_by_frame"]

CLIMB_ETA = 0.5  # 爬升功率 m·g·v_z/η 的效率（与 M10 energy.py 的估价同一口径）
G = 9.80665


@runtime_checkable
class PowerModel(Protocol):
    frame_kind: int

    def power_w(self, state: Any, slots: np.ndarray, ctx: Any) -> np.ndarray: ...

    def leg_power_w(self, profile: Any, v_air_mps: float, v_climb_mps: float, turn_load: float, rho_kgm3: float) -> float: ...


def _val(x: Any) -> Any:
    return x.get("value") if isinstance(x, dict) and "value" in x else x


def hover_power_w(profile: Any) -> float:
    """机型的悬停功率（`battery.p_hover_w`；没有电池模型时 NaN）。"""
    bat = getattr(profile, "battery", None)
    v = _val(bat.get("p_hover_w")) if isinstance(bat, dict) else None
    return float(v) if isinstance(v, (int, float)) else math.nan


class MultirotorPower:
    """多旋翼兜底模型：`P = P_hover·(T/T_hover)^1.5`（推力取 `S.thrust`，悬停推力比取 PT 的 P_HOVER 列）。"""

    frame_kind = 0

    def __init__(self) -> None:
        self._cache: tuple[int, np.ndarray] | None = None

    def _p_hover(self, T: Any) -> np.ndarray:
        key = id(T.PT)
        if self._cache is None or self._cache[0] != key:
            self._cache = (key, np.array([hover_power_w(T.profiles[k]) for k in T.ids], np.float64))
        return self._cache[1]

    def power_w(self, state: Any, slots: np.ndarray, ctx: Any) -> np.ndarray:
        T = ctx.profiles
        pid = state.profile_id[slots].astype(np.int64)
        ph = self._p_hover(T)[pid]
        hov = T.PT[pid, K.P_HOVER]
        r = np.where(hov > 0, state.thrust[slots] / np.maximum(hov, 1e-9), 0.0)
        return ph * np.power(np.maximum(r, 0.0), 1.5)

    def leg_power_w(self, profile: Any, v_air_mps: float, v_climb_mps: float, turn_load: float, rho_kgm3: float) -> float:
        return hover_power_w(profile) + profile.mass_kg * G * max(float(v_climb_mps), 0.0) / CLIMB_ETA

    def hover_or_loiter_power_w(self, profile: Any, wind_mps: float, rho_kgm3: float) -> float:
        return hover_power_w(profile)


_FALLBACK = MultirotorPower()


def _model(kind: int) -> Any:
    m = R.registry().power.get(int(kind))
    if m is None and int(kind) == 0:
        return _FALLBACK
    return m


def power_by_frame(state: Any, slots: np.ndarray, ctx: Any) -> np.ndarray:
    """按构型分组调用登记的功率模型（保持 slot 次序）；没有模型的构型为 NaN。"""
    sl = np.asarray(slots, np.int64)
    out = np.full(sl.size, np.nan)
    if sl.size == 0:
        return out
    fk = state.frame_kind[sl]
    for k in np.unique(fk).tolist():
        m = _model(int(k))
        if m is None:
            continue
        sel = fk == k
        out[sel] = np.asarray(m.power_w(state, sl[sel], ctx), np.float64)
    return out


def leg_power_w(profile: Any, v_air_mps: float, v_climb_mps: float = 0.0, turn_load: float = 1.0,
                rho_kgm3: float = 1.225) -> float:
    """估价与预检用的航段功率（W）：按 `profile.frame_kind` 分派；没有模型时 NaN。"""
    m = _model(int(getattr(profile, "frame_kind", 0)))
    if m is None:
        return math.nan
    return float(m.leg_power_w(profile, float(v_air_mps), float(v_climb_mps), float(turn_load), float(rho_kgm3)))
