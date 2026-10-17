"""Foxglove protobuf 封装（M27-FR-045）：`foxglove.CompressedImage`（data 为 Agent 收到的 JPEG 原样）与 `foxglove.RawImage`
（`32FC1` 平面深度，小端 f32）。MCAP schema 数据为含依赖的 FileDescriptorSet。

L2-depth 深度的权威存档为 f32 + 字节重排 + zstd（`depthcodec`，无损），通道元数据 `compression = "bshuf4-zstd"` 标记；RawImage 的 `encoding`、`width`、`height`、`step` 仍描述解码后的 32FC1 图像。
"""

from __future__ import annotations

from functools import cache

import numpy as np
from foxglove_schemas_protobuf.CompressedImage_pb2 import CompressedImage
from foxglove_schemas_protobuf.RawImage_pb2 import RawImage
from mcap_protobuf.schema import build_file_descriptor_set

from awr.bench.recorder.depthcodec import DEPTH_COMPRESSION, bshuf4_unzstd, bshuf4_zstd

__all__ = ["DEPTH_COMPRESSION", "SCHEMAS", "compressed_image", "decode_image", "raw_image_f32", "schema_data"]

SCHEMAS = {"foxglove.CompressedImage": CompressedImage, "foxglove.RawImage": RawImage}


@cache
def schema_data(name: str) -> bytes:
    return build_file_descriptor_set(SCHEMAS[name]).SerializeToString()


def _stamp(m, t_ns: int) -> None:
    m.timestamp.seconds, m.timestamp.nanos = divmod(int(t_ns), 1_000_000_000)


def compressed_image(t_ns: int, frame_id: str, data: bytes, fmt: str = "jpeg") -> bytes:
    m = CompressedImage(frame_id=frame_id, data=data, format=fmt)
    _stamp(m, t_ns)
    return m.SerializeToString()


def raw_image_f32(t_ns: int, frame_id: str, depth: np.ndarray, *, compression: str | None = DEPTH_COMPRESSION) -> bytes:
    a = np.ascontiguousarray(depth, "<f4")
    h, w = a.shape
    raw = a.tobytes()
    data = bshuf4_zstd(raw) if compression == DEPTH_COMPRESSION else raw
    m = RawImage(frame_id=frame_id, width=w, height=h, encoding="32FC1", step=4 * w, data=data)
    _stamp(m, t_ns)
    return m.SerializeToString()


def decode_image(schema: str, data: bytes, compression: str | None = None) -> dict:
    """返回 {t_ns, frame_id, codec, payload, width?, height?}；payload 为交付字节（JPEG 或 f32 行主序，存档压缩已解开）。"""
    m = SCHEMAS[schema]()
    m.ParseFromString(data)
    out = {"t_ns": m.timestamp.seconds * 1_000_000_000 + m.timestamp.nanos, "frame_id": m.frame_id, "payload": m.data}
    if schema == "foxglove.CompressedImage":
        out["codec"] = m.format
    else:
        out.update(codec=m.encoding, width=m.width, height=m.height)
        if compression == DEPTH_COMPRESSION:
            out["payload"] = bshuf4_unzstd(m.data)
    return out
