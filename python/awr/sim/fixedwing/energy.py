"""固定翼与 VTOL 的功率与能量模型（M08-FR-214、FR-215 的 `register_power_model(1|2, model)`；AWR-04 §5.3；AWR-20 §5.2、§5.4、§5.5；
ADR-052、ADR-096）。

    固定翼段：P_elec = (½·rho·V_a³·S·C_D0 + 2n²W²/(rho·V_a·S·π·e·AR) + m·g·max(ḣ, 0))/η + P_av + ΔP_e，n = 1/cos φ
    VTOL MC 模式与过渡段：P = P_hover·(m'/m)^1.5 + ΔP_e（悬停推力口径；MC 模式按 L1 推力 `(T/T_hover)^1.5` 缩放）
    伞降：P_av；地面上锁：0

`FwPower.power_w(S, slots, ctx)` 读取 M21 运行时的逐机状态（空速、滚转、爬升率、模式），没有运行时状态的机体按巡航口径；
`leg_power_w(profile, v_air, v_climb, turn_load, rho)` 供估价、能量预检与 M09 能量 RTL；`rtl_energy_wh` 为固定翼的返航能量口径
（返航距离 ÷ 地速 × 巡航功率 + 回收能量，AWR-04 §5.3）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from awr.sim.catalog import derive as D

from .params import FwParams, ParamsCache

__all__ = ["CLIMB_ETA_MC", "FwPower", "endurance_s", "hover_power_w", "leg_power", "rtl_energy_wh"]

CLIMB_ETA_MC = 0.5  # MC 模式爬升功率 m·g·v_z/η 的效率（与 M08 core.power 一致）


def hover_power_w(p: FwParams, mass_kg: float | None = None) -> float:
    """VTOL 悬停（与过渡段）功率：`P_hover·(m'/m)^1.5 + ΔP_e`。"""
    m0 = p.aero.mass_kg
    m1 = m0 if mass_kg is None else float(mass_kg)
    return p.p_hover_w * (m1 / m0) ** 1.5 + p.extra_power_w if p.p_hover_w > 0 else math.nan


def leg_power(p: FwParams, v_air_mps: float, v_climb_mps: float = 0.0, turn_load: float = 1.0, rho: float = D.RHO0,
              *, mass_kg: float | None = None) -> float:
    """航段功率（W）：固定翼段按 P_elec；VTOL 在 `v_air < v_min` 时按悬停口径（加爬升项）。"""
    fw = p.aero if mass_kg is None else p.aero.with_mass(mass_kg)
    if rho != fw.rho:
        fw = D.FwAero(**{**fw.__dict__, "rho": float(rho)})
    if p.vtol and float(v_air_mps) < fw.v_min_mps - 1e-9:
        return hover_power_w(p, fw.mass_kg) + fw.mass_kg * D.G * max(float(v_climb_mps), 0.0) / CLIMB_ETA_MC
    v = max(float(v_air_mps), fw.v_min_mps)
    return fw.p_elec(v, n=max(float(turn_load), 1.0), climb_mps=float(v_climb_mps), extra_w=p.extra_power_w)


def endurance_s(p: FwParams, mode: str = "cruise") -> float:
    """续航（s）：`cruise`、`loiter`（V_loiter）或 `mission`（VTOL 标准任务剖面巡航段）。"""
    if p.e_use_wh is None:
        return math.nan
    fw = p.aero
    if mode == "loiter":
        return p.e_use_wh * 3600.0 / leg_power(p, fw.v_loiter_mps)
    if mode == "mission" and p.vtol:
        t_vert = p.hover_max_s + p.transition_fw_s + p.transition_mc_s
        e_v = hover_power_w(p) * t_vert / 3600.0
        return max(p.e_use_wh - e_v, 0.0) * 3600.0 / leg_power(p, fw.v_cruise_mps)
    return p.e_use_wh * 3600.0 / leg_power(p, fw.v_cruise_mps)


def rtl_energy_wh(p: FwParams, dist_m: float, v_ground_mps: float | None = None, *, alt_agl_m: float = 60.0) -> float:
    """固定翼返航能量（Wh）：`返航距离 ÷ 地速 × 巡航功率 + 回收能量`。回收：伞降为下降时间 × 航电功率；VTOL 为过渡 + 垂直下降
    的悬停功率。"""
    fw = p.aero
    vg = max(float(v_ground_mps if v_ground_mps is not None else fw.v_cruise_mps), 1.0)
    e = float(dist_m) / vg * leg_power(p, fw.v_cruise_mps) / 3600.0
    if p.vtol:
        t = p.transition_mc_s + max(float(alt_agl_m), 0.0) / max(p.mc_sink_mps, 0.1)
        e += hover_power_w(p) * t / 3600.0
    else:
        e += fw.p_avionics_w * max(float(alt_agl_m), 0.0) / max(p.chute_descent_mps, 0.1) / 3600.0
    return e


class FwPower:
    """`register_power_model(frame_kind, model)` 的实现（固定翼 1、VTOL 2 各登记一个实例）。"""

    def __init__(self, frame_kind: int, runtime_fn: Any = None) -> None:
        self.frame_kind = int(frame_kind)
        self._rt = runtime_fn  # () -> FwRuntime | None
        self._cache = ParamsCache()

    def _params(self, ctx: Any, pidx: int) -> FwParams | None:
        T = getattr(ctx, "profiles", None)
        return None if T is None else self._cache.get(T, pidx)

    def power_w(self, state: Any, slots: np.ndarray, ctx: Any) -> np.ndarray:
        sl = np.asarray(slots, np.int64)
        out = np.full(sl.size, np.nan)
        rt = self._rt() if callable(self._rt) else None
        for k, s in enumerate(sl.tolist()):
            p = self._params(ctx, int(state.profile_id[s]))
            if p is None:
                continue
            st = rt.slot_state(s) if rt is not None else None
            rho = float(state.rho[s]) if hasattr(state, "rho") else D.RHO0
            if st is not None:
                out[k] = st.power_w(p, rho, state, s)
                continue
            if bool(state.landed[s]):
                out[k] = 0.0
            elif p.vtol and (int(state.fidelity[s]) & 16) == 0:
                out[k] = hover_power_w(p)
            else:
                out[k] = leg_power(p, p.aero.v_cruise_mps, rho=rho)
        return out

    def leg_power_w(self, profile: Any, v_air_mps: float, v_climb_mps: float, turn_load: float, rho_kgm3: float = D.RHO0) -> float:
        p = self._cache.by_profile(profile)
        if p is None:
            return math.nan
        return leg_power(p, v_air_mps, v_climb_mps, turn_load, rho_kgm3)

    def hover_or_loiter_power_w(self, profile: Any, wind_mps: float, rho_kgm3: float = D.RHO0) -> float:
        """站位保持功率：固定翼按盘旋空速（随风抬高，§5.3）；VTOL 同固定翼段（悬停受 hover_max_s 限制）。"""
        p = self._cache.by_profile(profile)
        if p is None:
            return math.nan
        return leg_power(p, p.aero.loiter_airspeed(wind_mps), 0.0, 1.0, rho_kgm3)
