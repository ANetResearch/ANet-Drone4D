"""D3 观测通道命名（M27 §7.5.1；`packages/contracts/rec/mcap_channels.json`）。深度通道在帧索引中的传感器名为 `<cam>.depth`；
LiDAR 观测 `/uav/{id}/lidar/{name}` 与真值 `/eval/hidden/lidar/{uav}/{name}` 的传感器名为 `<name>`（与相机名不重复）。"""

from __future__ import annotations

import re

__all__ = ["CAMERA_TOPIC", "LIDAR_TOPIC", "TRUTH_CAMERA_TOPIC", "TRUTH_LIDAR_TOPIC", "camera_topic", "imu_topic", "lidar_topic", "parse_camera_topic",
           "parse_obs_topic", "parse_truth_topic", "sensor_of", "truth_camera_topic", "truth_lidar_topic"]

CAMERA_TOPIC = re.compile(r"^/uav/([^/]+)/camera/([A-Za-z0-9_]+)/(image|depth)$")
TRUTH_CAMERA_TOPIC = re.compile(r"^/eval/hidden/camera/([^/]+)/([A-Za-z0-9_]+)$")
LIDAR_TOPIC = re.compile(r"^/uav/([^/]+)/lidar/([A-Za-z0-9_]+)$")
TRUTH_LIDAR_TOPIC = re.compile(r"^/eval/hidden/lidar/([^/]+)/([A-Za-z0-9_]+)$")


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


def truth_camera_topic(uav: str, cam: str) -> str:
    return f"/eval/hidden/camera/{uav}/{cam}"


def lidar_topic(uav: str, name: str) -> str:
    return f"/uav/{uav}/lidar/{name}"


def truth_lidar_topic(uav: str, name: str) -> str:
    return f"/eval/hidden/lidar/{uav}/{name}"


def imu_topic(uav: str, name: str) -> str:
    return f"/uav/{uav}/imu/{name}"


def parse_obs_topic(topic: str) -> tuple[str, str] | None:
    """相机与 LiDAR 观测通道 → (uav, sensor_id)。"""
    r = parse_camera_topic(topic)
    if r is not None:
        return r
    m = LIDAR_TOPIC.match(topic)
    return (m.group(1), m.group(2)) if m else None


def parse_truth_topic(topic: str) -> tuple[str, str] | None:
    m = TRUTH_CAMERA_TOPIC.match(topic) or TRUTH_LIDAR_TOPIC.match(topic)
    return (m.group(1), m.group(2)) if m else None
