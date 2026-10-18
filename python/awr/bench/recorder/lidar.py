"""LiDAR 观测与真值消息（M25 §6.8.7；契约 `sensor/v2/lidar_frame.v2`、`lidar_gt.v1`）。

观测：u32 header_len | header JSON | point_count × 20 B（f32 x、y、z，u32 offset_time，u8 reflectivity、tag、line、pad）；
真值：u32 header_len | header JSON | zstd(inst u16[slots] | flags u8[slots])。均为小端。
"""

from __future__ import annotations

import json
import struct

import numpy as np

__all__ = ["LIDAR_CODEC", "LIDAR_RECORD", "pack_lidar_frame", "pack_lidar_gt", "unpack_lidar_frame", "unpack_lidar_gt"]

LIDAR_CODEC = "lidar-v2"
LIDAR_RECORD = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("offset_time", "<u4"), ("reflectivity", "u1"), ("tag", "u1"),
                         ("line", "u1"), ("pad", "u1")])


def _head(header: dict) -> bytes:
    js = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode()
    return struct.pack("<I", len(js)) + js


def _split(b: bytes) -> tuple[dict, memoryview]:
    (n,) = struct.unpack_from("<I", b, 0)
    return json.loads(bytes(b[4:4 + n])), memoryview(b)[4 + n:]


def pack_lidar_frame(header: dict, points: np.ndarray) -> bytes:
    pts = np.ascontiguousarray(points, LIDAR_RECORD)
    if header.get("point_count") != len(pts):
        raise ValueError("point_count does not match the point array")
    return _head(header) + pts.tobytes()


def unpack_lidar_frame(b: bytes) -> tuple[dict, np.ndarray]:
    h, rest = _split(b)
    pts = np.frombuffer(rest, LIDAR_RECORD)
    if len(pts) != h["point_count"]:
        raise ValueError("truncated lidar frame")
    return h, pts


def pack_lidar_gt(header: dict, inst: np.ndarray, flags: np.ndarray) -> bytes:
    import zstandard

    if not (len(inst) == len(flags) == header["slots"]):
        raise ValueError("slot arrays do not match header.slots")
    body = np.ascontiguousarray(inst, "<u2").tobytes() + np.ascontiguousarray(flags, np.uint8).tobytes()
    return _head(header) + zstandard.ZstdCompressor(level=3).compress(body)


def unpack_lidar_gt(b: bytes) -> tuple[dict, np.ndarray, np.ndarray]:
    import zstandard

    h, rest = _split(b)
    n = h["slots"]
    body = zstandard.ZstdDecompressor().decompress(bytes(rest), max_output_size=3 * n)
    return h, np.frombuffer(body[:2 * n], "<u2"), np.frombuffer(body[2 * n:3 * n], np.uint8)
