"""SensorBackend 接口与 FakeBackend（M13-FR-004；M13 §7.1；ADR-048；D1 桩）。

`SensorBackend`：`open(world, rigs)`、`request(slots, poses_enu, t_sim_ns) -> int`（非阻塞）、`poll() -> list[SensorFrame]`
（在步顶锁存，结果写输入日志）。Gazebo（V0.6）、Isaac（V0.8）实现同一接口，UI 与协议不变。

FakeBackend：同步地按 `T_world_base · T_base_mount · R_gimbal` 给出传感器位姿（与自研 Camera 实现同一公式），用于
M13-AC-004"后端可替换"：把它装到 SensorPosePacker 的 `pose_source` 上时，SensorPose48 字节与 UI 快照不变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np

from ..frames import gimbal_R, quat_to_R
from ..models import SensorCaps
from ..spec import SensorSpec

__all__ = ["FakeBackend", "SensorBackend", "SensorFrame"]


@dataclass
class SensorFrame:
    slot: int
    sensor: str
    t_sim_ns: int
    pos_enu_m: np.ndarray
    R_ws: np.ndarray
    payload: dict = field(default_factory=dict)


@runtime_checkable
class SensorBackend(Protocol):
    caps: SensorCaps

    def open(self, world: Any, rigs: dict[int, list[SensorSpec]]) -> None: ...
    def request(self, slots: np.ndarray, poses_enu: np.ndarray, t_sim_ns: int) -> int: ...
    def poll(self) -> list[SensorFrame]: ...


class FakeBackend:
    """测试替身：请求即完成，poll 返回上一批（晚 1 步锁存的语义由调用方保证）。"""

    caps = SensorCaps(pose=True, noise=False, detect=False, raycast="none")

    def __init__(self) -> None:
        self.world: Any = None
        self.rigs: dict[int, list[SensorSpec]] = {}
        self._out: list[SensorFrame] = []
        self.requests = 0

    def open(self, world: Any, rigs: dict[int, list[SensorSpec]]) -> None:
        self.world = world
        self.rigs = dict(rigs)

    def request(self, slots: np.ndarray, poses_enu: np.ndarray, t_sim_ns: int) -> int:
        self.requests += 1
        out = []
        PQ = np.asarray(poses_enu, np.float64).reshape(-1, 7)
        for i, s in enumerate(np.asarray(slots).reshape(-1)):
            for spec in self.rigs.get(int(s), []):
                p, R = self.pose(PQ[i:i + 1], spec, np.zeros(1), np.full(1, spec.gimbal.default_el_rad if spec.gimbal else 0.0))
                out.append(SensorFrame(int(s), spec.name, int(t_sim_ns), p[0], R[0]))
        self._out = out
        return len(out)

    def poll(self) -> list[SensorFrame]:
        out, self._out = self._out, []
        return out

    @staticmethod
    def pose(PQ: np.ndarray, spec: SensorSpec, az: np.ndarray, el: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """与 `pose_pack.default_pose_source` 同一接口（SensorPosePacker.pose_source）。"""
        Rb = quat_to_R(np.asarray(PQ, np.float64)[:, 3:7])
        pos = np.asarray(PQ, np.float64)[:, :3] + Rb @ spec.mount_t
        Rs = Rb @ spec.mount_R
        if spec.gimbal is not None:
            Rs = Rs @ gimbal_R(az, el)
        return pos, Rs
