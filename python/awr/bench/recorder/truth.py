"""相机真值消息 `awr.sensor.camera_gt.v1`（M27 §7.5.1 `/eval/hidden/camera/{uav}/{name}`，只写入 `truth/run_<id>.mcap`）。

一条消息即一帧的 `drone4d.frame.v1` 记录及其载荷块（frame.v1 中 `ref` 指"同一 MCAP 消息的附加段"）：

    magic "D3GT" | version u16 = 1 | n_blocks u16 | json_len u32 | frame.v1 JSON (UTF-8)
    n_blocks × ( name_len u16 | name (UTF-8，= ref) | len u32 | bytes )

小端；块按 ref 字典序。真值帧接口（BN-52）原样返回本容器（`application/x-d3gt`）；`decode=raw` 时先经 `decode_blocks`
把 zstd 与 bshuf4-zstd 块解成原始小端数组，记录中对应条目的 `codec` 改为 `raw`（浏览器侧无需 zstd 解码器）。
"""

from __future__ import annotations

import json
import struct

__all__ = ["CONTENT_TYPE", "decode_blocks", "pack_camera_gt", "unpack_camera_gt"]

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


def decode_blocks(rec: dict, blocks: dict[str, bytes]) -> tuple[dict, dict[str, bytes]]:
    """返回新的 (记录, 块)：`ground_truth` 中 codec 为 zstd 或 bshuf4-zstd 的条目解为原始字节，codec 记为 raw。"""
    import copy

    import zstandard

    from awr.bench.recorder.depthcodec import DEPTH_COMPRESSION, bshuf4_unzstd

    rec = copy.deepcopy(rec)
    out = dict(blocks)
    dz = zstandard.ZstdDecompressor()
    for ent in (rec.get("ground_truth") or {}).values():
        if not isinstance(ent, dict) or ent.get("ref") not in out:
            continue
        if ent.get("codec") == "zstd":
            out[ent["ref"]] = dz.decompress(out[ent["ref"]])
        elif ent.get("codec") == DEPTH_COMPRESSION:
            out[ent["ref"]] = bshuf4_unzstd(out[ent["ref"]])
        else:
            continue
        ent["codec"] = "raw"
    return rec, out
