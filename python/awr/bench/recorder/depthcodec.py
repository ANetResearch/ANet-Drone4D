"""f32 深度的无损存档形式 `bshuf4-zstd`（AWR-05 §1154；AWR-17 §18.8.1）：4 字节元素按字节位拆成 4 个平面（b0 平面、b1 平面……）后
整体 zstd。用于 `raw.mcap` 的 L2-depth 通道与真值 `gt_depth` 块；只依赖 numpy 与 zstandard，传感器服务与录制共用。
"""

from __future__ import annotations

import numpy as np

__all__ = ["DEPTH_COMPRESSION", "ZSTD_LEVEL", "bshuf4_unzstd", "bshuf4_zstd"]

DEPTH_COMPRESSION = "bshuf4-zstd"
ZSTD_LEVEL = 3


def bshuf4_zstd(raw: bytes) -> bytes:
    import zstandard

    planes = np.frombuffer(raw, np.uint8).reshape(-1, 4).T
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(np.ascontiguousarray(planes).tobytes())


def bshuf4_unzstd(data: bytes) -> bytes:
    import zstandard

    planes = np.frombuffer(zstandard.ZstdDecompressor().decompress(data), np.uint8).reshape(4, -1)
    return np.ascontiguousarray(planes.T).tobytes()
