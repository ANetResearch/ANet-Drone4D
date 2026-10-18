"""路径跟踪器（M24 §6.5.7；P0a 版）：把 TierCommand 换算为逐步速度设定（ENU m/s、偏航角速度），Realistic 与 SITL 共用。

P0a 以 tick 为周期做位置反馈（每 tick 用 tick 起点状态求一次设定，批内零阶保持）：
- velocity、hover：设定即指令（hover 为零速度）；
- goto：v = sat(K·e, speed)，K = (0.95, 0.95, 1.0) s⁻¹，水平与竖直分别限幅；到达半径 0.3 m 内转悬停；
- takeoff：以不超过 `takeoff_vz_mps` 的速度爬升至起飞点上方 alt_m（v_z = min(K_z·e_z, takeoff_vz)），水平保持起飞点；
- land：以 `land_vz_mps` 下降，触地后由宿主失能电机；
- kill：电机零转速（enabled = false）。
航点时间参数化（topp_lite）、orbit 与 follow_path 的参考轨迹在 P0b 接入；未实现的种类返回 630。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from awr.phys import PhysError
from awr.phys.adapter import TierCommand

__all__ = ["Setpoint", "Tracker", "TrackerLimits"]

K_POS = np.array([0.95, 0.95, 1.0])


@dataclass(frozen=True)
class TrackerLimits:
    v_xy_mps: float = 12.0
    v_z_up_mps: float = 3.0
    v_z_dn_mps: float = 2.0
    yaw_rate_rps: float = 1.0
    takeoff_vz_mps: float = 1.5
    land_vz_mps: float = 0.8
    arrive_m: float = 0.3


@dataclass(frozen=True)
class Setpoint:
    v_enu: tuple[float, float, float]
    yaw_rate: float = 0.0
    enabled: bool = True


@dataclass
class _Ref:
    cmd: TierCommand
    origin: np.ndarray | None = None


@dataclass
class Tracker:
    limits: TrackerLimits = field(default_factory=TrackerLimits)
    _ref: dict[str, _Ref] = field(default_factory=dict)

    def set(self, cmd: TierCommand, pos: np.ndarray) -> None:
        if cmd.kind in ("orbit", "follow_path"):
            raise PhysError(630, f"{cmd.kind} is not available in the P0a tracker")
        self._ref[cmd.uav] = _Ref(cmd, np.asarray(pos, np.float64).copy())

    def kind(self, uav: str) -> str:
        r = self._ref.get(uav)
        return r.cmd.kind if r else "hover"

    def setpoint(self, uav: str, pos: np.ndarray, vel: np.ndarray) -> Setpoint:
        r = self._ref.get(uav)
        L = self.limits
        if r is None or r.cmd.kind == "hover":
            return Setpoint((0.0, 0.0, 0.0), float((r.cmd.yaw_rate_rps or 0.0) if r else 0.0))
        c = r.cmd
        if c.kind == "kill":
            return Setpoint((0.0, 0.0, 0.0), 0.0, enabled=False)
        if c.kind == "velocity":
            v = np.asarray(c.vel_enu_mps, np.float64)
            return Setpoint(self._sat(v), float(np.clip(c.yaw_rate_rps or 0.0, -L.yaw_rate_rps, L.yaw_rate_rps)))
        if c.kind == "takeoff":
            z_goal = float(r.origin[2]) + float(c.alt_m or 0.0)
            e = np.array([r.origin[0] - pos[0], r.origin[1] - pos[1], z_goal - pos[2]])
            v = K_POS * e
            v[2] = min(v[2], L.takeoff_vz_mps)                  # 匀速爬升，接近目标高度时按位置误差收敛
            return Setpoint(self._sat(v))
        if c.kind == "land":
            return Setpoint((0.0, 0.0, -L.land_vz_mps))
        if c.kind == "goto":
            e = np.asarray(c.target_enu_m, np.float64) - pos
            if float(np.linalg.norm(e)) < L.arrive_m:
                return Setpoint(tuple(float(x) for x in K_POS * e))
            v = K_POS * e
            sp = c.speed_mps
            if sp is not None and np.linalg.norm(v[:2]) > sp:
                v[:2] *= sp / np.linalg.norm(v[:2])
            return Setpoint(self._sat(v))
        raise PhysError(630, f"command kind {c.kind!r} is not available")

    def _sat(self, v: np.ndarray) -> tuple[float, float, float]:
        L = self.limits
        v = np.asarray(v, np.float64).copy()
        h = float(np.hypot(v[0], v[1]))
        if h > L.v_xy_mps:
            v[:2] *= L.v_xy_mps / h
        v[2] = min(max(v[2], -L.v_z_dn_mps), L.v_z_up_mps)
        return (float(v[0]), float(v[1]), float(v[2]))


def yaw_of(q_xyzw) -> float:
    x, y, z, w = (float(a) for a in q_xyzw)
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
