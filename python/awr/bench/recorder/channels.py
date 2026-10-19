"""D3 观测通道命名（M27 §7.5.1；`packages/contracts/rec/mcap_channels.json`）。深度通道在帧索引中的传感器名为 `<cam>.depth`；
LiDAR 观测 `/uav/{id}/lidar/{name}` 与真值 `/eval/hidden/lidar/{uav}/{name}` 的传感器名为 `<name>`（与相机名不重复）。

热成像观测 `/uav/{id}/thermal/{name}`（docs/05 §5.6.3 v1.3；M25-FR-189；D3-AC-61、63）：`foxglove.RawImage`，`encoding = 16UC1`、
`width`、`height`、`step = 2·W` 描述解码后的 u16 图像（0.01 K/LSB），`data` 为 Agent 收到的 `ThermalObs.temp_ck` 字节原样（u16 小端 +
zstd 单帧，即 M25 帧记录 `agent_view.temp_ck` 的交付字节），通道元数据 `codec = 16UC1`、`compression = zstd`、`unit = 0.01 K`；
开发运行的 `agc_jpeg` 预览为 `foxglove.CompressedImage`（`codec = jpeg`）。帧索引中的传感器名为 `<name>`，FrameStore 返回的载荷与
sha256 即交付字节。`log_time` 取 `ThermalObs.t_mid_ns`，MCAP `sequence` 为 seq。"""

from __future__ import annotations

import re

__all__ = ["CAMERA_TOPIC", "LIDAR_TOPIC", "THERMAL_CODEC", "THERMAL_COMPRESSION", "THERMAL_TOPIC", "TRUTH_CAMERA_TOPIC", "TRUTH_LIDAR_TOPIC",
           "camera_topic", "decode_thermal_message", "imu_topic", "lidar_topic", "parse_camera_topic", "parse_obs_topic", "parse_truth_topic",
           "sensor_of", "thermal_message", "thermal_topic", "truth_camera_topic", "truth_lidar_topic"]

CAMERA_TOPIC = re.compile(r"^/uav/([^/]+)/camera/([A-Za-z0-9_]+)/(image|depth)$")
TRUTH_CAMERA_TOPIC = re.compile(r"^/eval/hidden/camera/([^/]+)/([A-Za-z0-9_]+)$")
LIDAR_TOPIC = re.compile(r"^/uav/([^/]+)/lidar/([A-Za-z0-9_]+)$")
THERMAL_TOPIC = re.compile(r"^/uav/([^/]+)/thermal/([A-Za-z0-9_]+)$")
THERMAL_CODEC, THERMAL_COMPRESSION, THERMAL_UNIT = "16UC1", "zstd", "0.01 K"
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


def thermal_topic(uav: str, name: str) -> str:
    return f"/uav/{uav}/thermal/{name}"


def parse_obs_topic(topic: str) -> tuple[str, str] | None:
    """相机、热成像与 LiDAR 观测通道 → (uav, sensor_id)。"""
    r = parse_camera_topic(topic)
    if r is not None:
        return r
    m = LIDAR_TOPIC.match(topic) or THERMAL_TOPIC.match(topic)
    return (m.group(1), m.group(2)) if m else None


def thermal_message(uav: str, obs: dict) -> tuple[str, dict[str, str], bytes]:
    """ThermalObs → (`foxglove` schema 名, 通道元数据, 消息字节)；`temp_ck` 原样放入 `data`，不解压、不重编码。

    `radiometric_u16` 先校验 zstd 帧头声明的解压长度为 2·W·H（不解压，O(1)）；声明缺失或不符时抛 ValueError，防止把截断或错尺寸
    的载荷写成合法存档。"""
    from foxglove_schemas_protobuf.RawImage_pb2 import RawImage

    from awr.bench.recorder.foxglove import compressed_image

    w, h, t_ns = int(obs["width"]), int(obs["height"]), int(obs["t_mid_ns"])
    data = bytes(obs["temp_ck"])
    fid = f"{uav}/{obs['sensor_id']}"
    if obs["codec"] == "agc_jpeg":
        return "foxglove.CompressedImage", {"codec": "jpeg", "therm_codec": "agc_jpeg"}, compressed_image(t_ns, fid, data)
    if obs["codec"] != "radiometric_u16":
        raise ValueError(f"thermal codec {obs['codec']!r}")
    import zstandard

    try:
        n = zstandard.frame_content_size(data)
    except zstandard.ZstdError as e:
        raise ValueError(f"temp_ck is not a zstd frame: {e}") from e
    if n != 2 * w * h:
        raise ValueError(f"temp_ck zstd frame declares {n} bytes, expected {2 * w * h} ({w}x{h} u16)")
    m = RawImage(frame_id=fid, width=w, height=h, encoding=THERMAL_CODEC, step=2 * w, data=data)
    m.timestamp.seconds, m.timestamp.nanos = divmod(t_ns, 1_000_000_000)
    meta = {"codec": THERMAL_CODEC, "compression": THERMAL_COMPRESSION, "unit": THERMAL_UNIT, "therm_codec": "radiometric_u16"}
    return "foxglove.RawImage", meta, m.SerializeToString()


def decode_thermal_message(data: bytes, *, decode: bool = False) -> dict:
    """热成像通道消息 → {t_ns, frame_id, codec, width, height, payload[, temp_ck]}；`payload` 为交付字节（zstd 原样），`decode = True` 时
    另给出 (H, W) uint16 的 `temp_ck`（0.01 K/LSB）。"""
    from foxglove_schemas_protobuf.RawImage_pb2 import RawImage

    m = RawImage()
    m.ParseFromString(data)
    out = {"t_ns": m.timestamp.seconds * 1_000_000_000 + m.timestamp.nanos, "frame_id": m.frame_id, "codec": m.encoding, "width": m.width,
           "height": m.height, "payload": m.data}
    if decode:
        import numpy as np
        import zstandard

        n = 2 * m.width * m.height
        raw = zstandard.ZstdDecompressor().decompress(m.data, max_output_size=n)
        if len(raw) != n:
            raise ValueError(f"temp_ck payload has {len(raw)} bytes, expected {n}")
        out["temp_ck"] = np.frombuffer(raw, "<u2").reshape(m.height, m.width).astype(np.uint16)
    return out


def parse_truth_topic(topic: str) -> tuple[str, str] | None:
    m = TRUTH_CAMERA_TOPIC.match(topic) or TRUTH_LIDAR_TOPIC.match(topic)
    return (m.group(1), m.group(2)) if m else None
