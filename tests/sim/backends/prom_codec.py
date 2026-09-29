"""Prometheus 地面站协议帧编解码（测试替身；由 `.cache/research/r19/codec.py` 迁入，r19 §3.12）。

帧：`"am"` + u32 LE 载荷长度 + u8 msg_id + u8 robot_id + JSON（紧凑、键序同 nlohmann dump）+ u16 LE CRC-16/ARC（覆盖头与载荷）。
"""

from __future__ import annotations

import json
import struct


def crc16_arc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def encode(msg_id: int, robot_id: int, obj: dict) -> bytes:
    payload = json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()
    body = b"am" + struct.pack("<IBB", len(payload), msg_id, robot_id) + payload
    return body + struct.pack("<H", crc16_arc(body))


def decode(buf: bytes) -> tuple[int, int, dict]:
    if buf[:2] != b"am":
        raise ValueError("bad magic")
    n, msg_id, robot_id = struct.unpack_from("<IBB", buf, 2)
    body = buf[:8 + n]
    (crc,) = struct.unpack_from("<H", buf, 8 + n)
    if crc != crc16_arc(body):
        raise ValueError("crc")
    return msg_id, robot_id, json.loads(buf[8:8 + n])
