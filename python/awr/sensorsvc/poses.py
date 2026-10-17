"""PoseHistory 与传感器位姿（M25 §6.2.5；M25-FR-020、021）：逐物理步末状态，位置线性插值、姿态球面插值，范围外 646 不外推；
`T_world_sensor = T_world_base · T_base_mount · R_gimbal(az, el)`（复用 awr.sim.sensors.frames）。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field

import numpy as np

from awr.sensorsvc.errors import SensorSvcError
from awr.sim.sensors.frames import R_FLU_OPT, R_to_quat_xyzw, gimbal_R, mount_R, quat_to_R

__all__ = ["Mount", "PoseHistory", "q_world_opt", "sensor_pose", "slerp"]


def slerp(q0: np.ndarray, q1: np.ndarray, u: float) -> np.ndarray:
    """[x, y, z, w]；q1 先翻到 q0 同半球。"""
    q0 = np.asarray(q0, np.float64)
    q1 = np.asarray(q1, np.float64)
    d = float(q0 @ q1)
    if d < 0:
        q1, d = -q1, -d
    if d > 0.9995:
        q = q0 + u * (q1 - q0)
        return q / np.linalg.norm(q)
    th = math.acos(min(d, 1.0))
    s = math.sin(th)
    return (math.sin((1 - u) * th) / s) * q0 + (math.sin(u * th) / s) * q1


@dataclass(frozen=True)
class Mount:
    """标称挂载：位置（FLU，m）与 ZYX 角（度，pitch 为正时光轴下俯）。"""

    xyz_m: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rpy_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def R(self) -> np.ndarray:
        return mount_R(self.rpy_deg)


@dataclass
class _Gimbal:
    t: list[int] = field(default_factory=list)
    az: list[float] = field(default_factory=list)       # rad
    el: list[float] = field(default_factory=list)
    zoom: list[float] = field(default_factory=list)     # f_mm，0 = 无变焦


class PoseHistory:
    """单架无人机的位姿历史；`t_ns` 严格递增。云台按 sensor_no 分列，样本间线性插值，首样本之前取首样本值。"""

    def __init__(self, uav: str, hz: int = 250) -> None:
        self.uav = uav
        self.hz = int(hz)
        self.t: list[int] = []
        self.p: list[np.ndarray] = []
        self.q: list[np.ndarray] = []
        self.gimbals: dict[int, _Gimbal] = {}

    def __len__(self) -> int:
        return len(self.t)

    def append(self, t_ns: int, pos_enu_m, q_ws_xyzw) -> None:
        t_ns = int(t_ns)
        if self.t and t_ns <= self.t[-1]:
            raise SensorSvcError(642, f"pose t_ns must increase: {t_ns} after {self.t[-1]}")
        q = np.asarray(q_ws_xyzw, np.float64)
        self.t.append(t_ns)
        self.p.append(np.asarray(pos_enu_m, np.float64).copy())
        self.q.append(q / np.linalg.norm(q))

    def append_gimbal(self, sensor_no: int, t_ns: int, az_rad: float, el_rad: float, zoom_f_mm: float = 0.0) -> None:
        g = self.gimbals.setdefault(int(sensor_no), _Gimbal())
        if g.t and int(t_ns) < g.t[-1]:
            raise SensorSvcError(642, "gimbal samples must not go back in time")
        g.t.append(int(t_ns))
        g.az.append(float(az_rad))
        g.el.append(float(el_rad))
        g.zoom.append(float(zoom_f_mm))

    def trim_before(self, t_ns: int) -> None:
        """保留窗口：删除早于 t_ns 的样本，但保留覆盖 t_ns 的那一个。"""
        n = max(bisect_right(self.t, int(t_ns)) - 1, 0)
        if n:
            del self.t[:n], self.p[:n], self.q[:n]

    def pose_at(self, t_ns: int) -> tuple[np.ndarray, np.ndarray]:
        t_ns = int(t_ns)
        if not self.t or t_ns < self.t[0] or t_ns > self.t[-1]:
            span = f"[{self.t[0]}, {self.t[-1]}]" if self.t else "empty"
            raise SensorSvcError(646, f"t_ns {t_ns} outside pose history {span}", uav=self.uav, t_ns=t_ns)
        n = bisect_right(self.t, t_ns) - 1
        if self.t[n] == t_ns:
            return self.p[n].copy(), self.q[n].copy()
        u = (t_ns - self.t[n]) / (self.t[n + 1] - self.t[n])
        return (1 - u) * self.p[n] + u * self.p[n + 1], slerp(self.q[n], self.q[n + 1], u)

    def gimbal_at(self, sensor_no: int, t_ns: int) -> tuple[float, float, float] | None:
        g = self.gimbals.get(int(sensor_no))
        if g is None or not g.t:
            return None
        n = bisect_right(g.t, int(t_ns)) - 1
        if n < 0:
            return g.az[0], g.el[0], g.zoom[0]
        if n == len(g.t) - 1 or g.t[n] == t_ns:
            return g.az[n], g.el[n], g.zoom[n]
        u = (t_ns - g.t[n]) / (g.t[n + 1] - g.t[n])
        return ((1 - u) * g.az[n] + u * g.az[n + 1], (1 - u) * g.el[n] + u * g.el[n + 1], (1 - u) * g.zoom[n] + u * g.zoom[n + 1])


def sensor_pose(H: PoseHistory, mount: Mount, sensor_no: int, t_ns: int) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float]]:
    """返回 (pos_enu_m, R_ws WORLD←FLU, (az, el, zoom))；无云台样本时云台角为 0（挂载常量）。"""
    p_b, q_b = H.pose_at(t_ns)
    g = H.gimbal_at(sensor_no, t_ns) or (0.0, 0.0, 0.0)
    R_wb = quat_to_R(q_b)
    pos = p_b + R_wb @ np.asarray(mount.xyz_m, np.float64)
    return pos, R_wb @ mount.R @ gimbal_R(g[0], g[1]), g


def q_world_opt(R_ws: np.ndarray) -> np.ndarray:
    """真值外参的四元数（ENU←RDF，[x, y, z, w]）。"""
    return R_to_quat_xyzw(np.asarray(R_ws, np.float64) @ R_FLU_OPT)
