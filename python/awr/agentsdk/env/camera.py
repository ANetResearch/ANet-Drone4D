"""L1 标称相机与云台（M26 §6.6.3；P0a）：针孔 1920 × 1080、HFOV 60°、无畸变，挂载于机头下方三轴云台。

云台角相对机体航向：`yaw_deg` 逆时针为正（与 ENU 偏航同口径），`pitch_deg` 向上为正（−90 为正下视）；指令角按
`rate_max_dps` 每控制步限速趋近。光学帧取 forward / right / down 三个 ENU 单位向量。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

__all__ = ["CameraModel", "Gimbal", "cam_axes", "project"]


@dataclass(frozen=True)
class CameraModel:
    sensor_id: str = "eo"
    width: int = 1920
    height: int = 1080
    hfov_deg: float = 60.0
    rate_hz: float = 10.0
    pitch_range_deg: tuple[float, float] = (-90.0, 30.0)
    yaw_range_deg: tuple[float, float] = (-150.0, 150.0)
    rate_max_dps: float = 90.0
    mount_t_bs_m: tuple[float, float, float] = (0.12, 0.0, -0.08)
    model_ref: str = "d4d_eo1080@1"

    @property
    def fx(self) -> float:
        return (self.width / 2) / math.tan(math.radians(self.hfov_deg) / 2)

    @property
    def K(self) -> list[float]:
        f = self.fx
        return [f, 0.0, self.width / 2, 0.0, f, self.height / 2, 0.0, 0.0, 1.0]

    def config(self) -> dict:
        """agent_config.v1 `sensors[]` 条目。"""
        return {"sensor_id": self.sensor_id, "kind": "camera", "model_ref": self.model_ref, "rate_hz": self.rate_hz,
                "mount_nominal": {"t_bs_m": list(self.mount_t_bs_m), "q_bs_xyzw": [0.0, 0.0, 0.0, 1.0]},
                "width": self.width, "height": self.height, "K": self.K, "dist": [0.0] * 5,
                "gimbal": {"pitch_range_deg": list(self.pitch_range_deg), "yaw_range_deg": list(self.yaw_range_deg),
                           "rate_max_dps": self.rate_max_dps}, "controllable": True}


@dataclass
class Gimbal:
    pitch_deg: float = -30.0
    yaw_deg: float = 0.0
    cmd_pitch_deg: float = -30.0
    cmd_yaw_deg: float = 0.0
    cam: CameraModel = field(default_factory=CameraModel)

    def command(self, pitch_deg: float, yaw_deg: float) -> bool:
        """→ 是否被限幅。"""
        lo, hi = self.cam.pitch_range_deg
        p = min(max(pitch_deg, lo), hi)
        ylo, yhi = self.cam.yaw_range_deg
        y = (yaw_deg + 180.0) % 360.0 - 180.0
        yc = min(max(y, ylo), yhi)
        self.cmd_pitch_deg, self.cmd_yaw_deg = p, yc
        return p != pitch_deg or yc != y

    def slew(self, dt_s: float) -> None:
        m = self.cam.rate_max_dps * dt_s
        self.pitch_deg += min(max(self.cmd_pitch_deg - self.pitch_deg, -m), m)
        self.yaw_deg += min(max(self.cmd_yaw_deg - self.yaw_deg, -m), m)


def cam_axes(yaw_body_rad: float, pitch_deg: float, yaw_rel_deg: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """→ (forward, right, down)，ENU 单位向量。"""
    yw = yaw_body_rad + math.radians(yaw_rel_deg)
    pt = math.radians(pitch_deg)
    f = np.array([math.cos(pt) * math.cos(yw), math.cos(pt) * math.sin(yw), math.sin(pt)])
    r = np.array([math.sin(yw), -math.cos(yw), 0.0])
    return f, r, np.cross(f, r)


def project(cam: CameraModel, o: np.ndarray, axes, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """pts (n, 3) → (u, v, z)；z 为沿光轴深度。"""
    f, r, d = axes
    rel = pts - o
    z = rel @ f
    with np.errstate(divide="ignore", invalid="ignore"):
        u = cam.width / 2 + cam.fx * (rel @ r) / z
        v = cam.height / 2 + cam.fx * (rel @ d) / z
    return u, v, z
