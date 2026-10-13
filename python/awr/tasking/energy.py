"""分段闭式能量估计（M20-FR-035；M20 §6.7"估计项"；ADR-052 口径）。

功率按角色取机型摘要（`ModelInfo.p_role`：悬停、巡航、盘旋；sim-core 插件按 M09 同式 `P_hover·(T/T_hover)^1.5` 与 M21 `fw_power`
预先求得），爬升另加 `m·g·Δz/η`（η = 0.5，M10 同值）；风只按保守的地速折减计入（`v_g = max(v − 0.3·w, 0.5·v)`）。估计只用于
分配与派遣判定，实际航线的能量终审由 M10 能量预检（`EnergyModel.path_wh`）完成。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ModelInfo, WorkItem

__all__ = ["ETA_CLIMB", "MIN_SHIFT_S", "G", "SortieEnergy", "climb_wh", "ground_speed", "leg_wh", "sortie_energy", "work_time_s"]

G = 9.80665
ETA_CLIMB = 0.5
MIN_SHIFT_S = 300.0      # 持续性工作项的最短班次（能量可行性判据：候选机至少能在岗 5 min）
V_DESCENT_MPS = 1.5


def ground_speed(v_mps: float, wind_mps: float) -> float:
    v = max(float(v_mps), 0.1)
    return max(v - 0.3 * max(float(wind_mps), 0.0), 0.5 * v)


def climb_wh(m: ModelInfo, dz_m: float) -> float:
    """离地爬升 dz（m）的能量：悬停功率 × 爬升时长 + 势能 / η。"""
    dz = max(float(dz_m), 0.0)
    t = dz / max(m.climb_mps, 0.1)
    return (m.p_role("hover") * t + m.mass_kg * G * dz / ETA_CLIMB) / 3600.0


def leg_wh(m: ModelInfo, length_m: float, v_mps: float, wind_mps: float = 0.0, role: str = "cruise") -> float:
    return m.p_role(role) * float(length_m) / ground_speed(v_mps, wind_mps) / 3600.0


def work_time_s(item: WorkItem, m: ModelInfo, *, persistent_shift_s: float | None = None) -> float:
    """工作项的作业时长：航段为 L/v 加停留；站位为驻留；持续性工作项取一个班次（缺省最短班次）。"""
    if item.persistent:
        return float(persistent_shift_s if persistent_shift_s is not None else min(max(item.workload_s, 0.0) or MIN_SHIFT_S, MIN_SHIFT_S))
    if item.path is not None and len(item.path) >= 2:
        v = float(item.needs.get("speed_mps") or m.v_cruise_mps)
        return item.length_m / max(v, 0.1) + float(sum(item.dwell.values()))
    return max(float(item.workload_s), 0.0)


@dataclass(frozen=True)
class SortieEnergy:
    e_launch_wh: float
    e_out_wh: float
    e_work_wh: float
    e_back_wh: float
    t_climb_s: float
    t_out_s: float
    t_work_s: float
    t_back_s: float

    @property
    def e_need_wh(self) -> float:
        return self.e_launch_wh + self.e_out_wh + self.e_work_wh + self.e_back_wh


def sortie_energy(m: ModelInfo, item: WorkItem, *, l_out_m: float, l_back_m: float, h_climb_m: float, airborne: bool, wind_mps: float = 0.0,
                  persistent_shift_s: float | None = None) -> SortieEnergy:
    """一个架次承载该工作项的能量分解：起飞爬升（在巢时）+ 转场 + 作业 + 返航与降落。"""
    v = m.v_cruise_mps
    vg = ground_speed(v, wind_mps)
    t_climb = 0.0 if airborne else max(h_climb_m, 0.0) / max(m.climb_mps, 0.1)
    e_launch = 0.0 if airborne else climb_wh(m, h_climb_m)
    t_out = float(l_out_m) / vg
    e_out = m.p_role("cruise") * t_out / 3600.0
    t_work = work_time_s(item, m, persistent_shift_s=persistent_shift_s)
    role = item.role
    if role == "hover" and not m.hovers:
        role = "loiter"
    e_work = m.p_role(role) * t_work / 3600.0
    t_back = float(l_back_m) / vg
    t_land = max(h_climb_m, 0.0) / V_DESCENT_MPS
    e_back = (m.p_role("cruise") * t_back + m.p_role("hover") * t_land) / 3600.0
    return SortieEnergy(e_launch, e_out, e_work, e_back, t_climb, t_out, t_work, t_back + t_land)
