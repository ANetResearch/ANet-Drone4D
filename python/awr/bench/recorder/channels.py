"""D3 观测通道命名（M27 §7.5.1；`packages/contracts/rec/mcap_channels.json`）。深度通道在帧索引中的传感器名为 `<cam>.depth`。"""

from __future__ import annotations

import re

__all__ = ["CAMERA_TOPIC", "camera_topic", "parse_camera_topic", "sensor_of"]

CAMERA_TOPIC = re.compile(r"^/uav/([^/]+)/camera/([A-Za-z0-9_]+)/(image|depth)$")


def camera_topic(uav: str, cam: str, kind: str = "image") -> str:
    if kind not in ("image", "depth"):
        raise ValueError(kind)
    return f"/uav/{uav}/camera/{cam}/{kind}"


def sensor_of(cam: str, kind: str) -> str:
    return cam if kind == "image" else f"{cam}.depth"


def parse_camera_topic(topic: str) -> tuple[str, str] | None:
    """返回 (uav, sensor_id)；非相机通道返回 None。"""
    m = CAMERA_TOPIC.match(topic)
    return (m.group(1), sensor_of(m.group(2), m.group(3))) if m else None
