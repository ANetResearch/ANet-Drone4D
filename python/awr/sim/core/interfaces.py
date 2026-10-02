"""M08 定义、M09 与 M10 实现的协议（M08 §7.1.5，签名在 D1-MS1 冻结；M08-FR-086、FR-089、FR-090）。

- `EnergyModel`（M09-FR-052、FR-053）：估价、RTL 规划缓存、沿轨迹能量积分；经 `register_energy_model()` 登记；
- `SafetyHooks`（M09）：M08 在规定时机调用的八个钩子，全部只入候选或写状态块，不阻塞；经 `register_safety_hooks()` 登记；
- `MotionProvider`（M10-FR-015）：接管 follow_path、orbit、非直飞 goto 等命令运动的对象；经 `register_motion_provider()`
  （`awr/sim/core/command.py`）登记，同一 op 只允许一个提供者。

`EnvironmentService` 与后端协议在 `awr/sim/backends/base.py`（M07 可以 import 的模块）。M09、M10 位于 awr.sim.*，可以 import
本模块；M08 不 import M09、M10（AWR-10 §3.3 规则 1）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

if TYPE_CHECKING:
    from awr.sim.backends.base import EnvironmentService

__all__ = ["CallRef", "EnergyModel", "EstimatePath", "EstimateResult", "LeaseEvent", "MotionProvider", "RtlPlan",
           "SafetyHooks", "VehicleProfile"]


@dataclass(frozen=True)
class VehicleProfile:
    """估价与能量模型读取的机型参数子集（全集见 M08 `ProfileTable` 与 AWR-16 §11）。"""

    profile_id: str
    model: str
    mass_kg: float
    t_max_n: float
    n_rot: int
    omega_max_rad_s: float
    cda_m2: float
    c_rd: float
    k_dv: float
    aero_model: str
    status: str = "placeholder"
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EstimatePath:
    """估价路径：ENU 航点（起点 → 转场高度 → 目标 → 停留 → 返航）与各腿速度、停留时长。"""

    points_enu_m: np.ndarray  # k×3
    speeds_mps: np.ndarray  # k-1
    dwell_s: float = 0.0


@dataclass(frozen=True)
class EstimateResult:
    eta_s: float
    energy_wh: float
    soc_after_pct: float
    feasible: bool
    code: int = 0


@dataclass(frozen=True)
class RtlPlan:
    z_rtl_m: float
    v_c_mps: float
    t_rtl_s: float
    # 绕行点（World ENU 水平坐标）；None 为原地爬升后直飞 home（ADR-054：返航路线由 M09 按 M04 走廊上界选定，
    # t_rtl 与 rtl 分发的实际路线一致）
    via_enu_m: tuple[float, float] | None = None


@dataclass(frozen=True)
class CallRef:
    """运动提供者收到的调用引用。"""

    cid: str
    op: str
    source: str


@dataclass(frozen=True)
class LeaseEvent:
    kind: str  # acquired、released、preempted、returned、seat_claim、seat_grace、seat_resume、seat_expire、seat_release
    slots: np.ndarray
    owner: str | None
    holder: str | None


@runtime_checkable
class EnergyModel(Protocol):
    def estimate(self, profile: VehicleProfile, soc: float, path: EstimatePath, env: EnvironmentService) -> EstimateResult: ...

    def rtl_plan(self, slot: int) -> RtlPlan: ...

    def path_wh(self, profile_id: str, samples: np.ndarray, env: EnvironmentService | None = None) -> float: ...


@runtime_checkable
class SafetyHooks(Protocol):
    def on_stream_watchdog(self, slots: np.ndarray) -> None: ...

    def on_spawn(self, slots: np.ndarray, profile_ids: np.ndarray, home_enu_m: np.ndarray, initial_soc: np.ndarray) -> None: ...

    def on_remove(self, slots: np.ndarray) -> None: ...

    def apply_operator(self, slot: int, cmd: Any, t_apply_ns: int) -> None: ...

    def on_lease_event(self, ev: LeaseEvent) -> None: ...

    def on_gcs_beacon(self, principal_id: str | None, seat_state: str, ping_age_ms: int, t_recv_ns: int,
                      paused_ns: int) -> None: ...

    def on_agent_liveliness(self, alive: bool) -> None: ...

    def matrix_verdict(self, slots: np.ndarray, op: str) -> np.ndarray: ...


@runtime_checkable
class MotionProvider(Protocol):
    name: str
    ops: tuple[str, ...]

    def start(self, call: CallRef, slots: np.ndarray, args: dict, apply_tick: int) -> str: ...

    def cancel(self, cid: str, slots: np.ndarray) -> None: ...
