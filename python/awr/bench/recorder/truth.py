"""相机真值消息 `awr.sensor.camera_gt.v1`（M27 §7.5.1 `/eval/hidden/camera/{uav}/{name}`，只写入 `truth/run_<id>.mcap`）。

一条消息即一帧的 `drone4d.frame.v1` 记录及其载荷块（frame.v1 中 `ref` 指"同一 MCAP 消息的附加段"）：

    magic "D3GT" | version u16 = 1 | n_blocks u16 | json_len u32 | frame.v1 JSON (UTF-8)
    n_blocks × ( name_len u16 | name (UTF-8，= ref) | len u32 | bytes )

小端；块按 ref 字典序。真值帧接口（BN-52）原样返回本容器（`application/x-d3gt`）。
"""

from __future__ import annotations

import json
import struct

__all__ = ["CONTENT_TYPE", "pack_camera_gt", "unpack_camera_gt"]

MAGIC = b"D3GT"
CONTENT_TYPE = "application/x-d3gt"


def pack_camera_gt(rec: dict, blocks: dict[str, bytes]) -> bytes:
    js = json.dumps(rec, separators=(",", ":"), ensure_ascii=False).encode()
    parts = [MAGIC, struct.pack("<HHI", 1, len(blocks), len(js)), js]
    for name in sorted(blocks):
        nb = name.encode()
        parts += [struct.pack("<H", len(nb)), nb, struct.pack("<I", len(blocks[name])), blocks[name]]
    return b"".join(parts)


def unpack_camera_gt(b: bytes) -> tuple[dict, dict[str, bytes]]:
    if b[:4] != MAGIC:
        raise ValueError("not an awr.sensor.camera_gt.v1 message")
    ver, n, jl = struct.unpack_from("<HHI", b, 4)
    if ver != 1:
        raise ValueError(f"camera_gt version {ver}")
    p = 12
    rec = json.loads(b[p:p + jl])
    p += jl
    blocks = {}
    for _ in range(n):
        (nl,) = struct.unpack_from("<H", b, p)
        name = b[p + 2:p + 2 + nl].decode()
        p += 2 + nl
        (ln,) = struct.unpack_from("<I", b, p)
        blocks[name] = b[p + 4:p + 4 + ln]
        p += 4 + ln
    return rec, blocks
