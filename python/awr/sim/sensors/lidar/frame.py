"""`awr.sensor.lidar_frame.v1` 编码与校验、`awr.LidarScan8.v1` 打包（M13-FR-052、§7.2.4；AWR-16 §14.3；M02 §6.3.2；D1 桩）。

帧 = JSON 头（`sensor/lidar_frame.schema.json`）+ SoA 载荷（小端，20 B/点：x、y、z f32（雷达系 FLU，m），offset_time u32（ns，
相对 timebase），reflectivity u8，tag u8，line u8，pad u8，按流依次排列）。M13 夹具文件的容器：UTF-8 JSON 头一行 + `\n` + 载荷。
校验规则与 M02 导入器共用：①line < 4；②offset_time 帧内非递减；③对时模式下 timebase 对齐 100 ms 网格（≤ 1 ms）；
④sync_type = none 告警；⑦point_count ≤ 22 000；（⑤ IMU 单位、⑥ model 与 dev_type 不适用于不含 IMU、dev_type 的夹具）。
LidarScan8（V0.2 扫描下行，本文提议）：48 B 头 + 8 B/点（i16 x/y/z cm、u8 refl、u8 tag_line）。
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

__all__ = ["MAX_POINTS", "decode_lidar_frame", "encode_lidar_frame", "pack_scan8", "validate_lidar_frame"]

MAX_POINTS = 22000
GRID_NS = 100_000_000
STREAMS = (("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("offset_time", "<u4"), ("reflectivity", "u1"), ("tag", "u1"),
           ("line", "u1"), ("pad", "u1"))


def encode_lidar_frame(hdr: dict[str, Any], pts: dict[str, np.ndarray]) -> bytes:
    n = len(pts["x"])
    h = dict(hdr)
    h["point_count"] = n
    head = json.dumps(h, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    parts = [head, b"\n"]
    for name, dt in STREAMS:
        a = np.zeros(n, dt) if name == "pad" else np.ascontiguousarray(np.asarray(pts[name]).astype(dt))
        parts.append(a.tobytes())
    return b"".join(parts)


def decode_lidar_frame(buf: bytes) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    i = buf.index(b"\n")
    hdr = json.loads(buf[:i].decode("utf-8"))
    n = int(hdr["point_count"])
    off = i + 1
    out: dict[str, np.ndarray] = {}
    for name, dt in STREAMS:
        size = np.dtype(dt).itemsize * n
        out[name] = np.frombuffer(buf[off:off + size], dt)
        off += size
    if off != len(buf):
        raise ValueError(f"lidar_frame payload size mismatch: {len(buf) - i - 1} B for {n} points")
    return hdr, out


def validate_lidar_frame(hdr: dict[str, Any], pts: dict[str, np.ndarray]) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)：schema 结构 + M02 规则 ①②③④⑦。"""
    from jsonschema import Draft202012Validator

    from awr.contracts._paths import contracts_root
    from awr.sim.sensors.spec import _registry  # 复用 contracts schema 注册表

    schema = json.loads((contracts_root() / "sensor" / "lidar_frame.schema.json").read_text(encoding="utf-8"))
    v = Draft202012Validator(schema, registry=_registry())
    err = [f"schema {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:120]}" for e in v.iter_errors(hdr)]
    warn: list[str] = []
    n = int(hdr.get("point_count", -1))
    if n != len(pts["x"]):
        err.append("point_count differs from payload")
    if n > MAX_POINTS:
        err.append(f"rule 7: point_count {n} > {MAX_POINTS}")
    if len(pts["line"]) and int(np.max(pts["line"])) >= 4:
        err.append("rule 1: line >= 4")
    ot = np.asarray(pts["offset_time"], np.int64)
    if ot.size > 1 and np.any(np.diff(ot) < 0):
        err.append("rule 2: offset_time decreases within the frame")
    st = hdr.get("sync_type")
    if st == "none":
        warn.append("rule 4: sync_type none")
    elif st in ("ptp", "gps_pps", "sim"):
        tb = int(hdr.get("raw_timebase_ns", hdr.get("timebase_ns", 0)))
        r = tb % GRID_NS
        if min(r, GRID_NS - r) > 1_000_000:
            err.append("rule 3: timebase not aligned to the 100 ms grid")
    return err, warn


def pack_scan8(t_frame_ns: int, frame_seq: int, pos: np.ndarray, q_xyzw: np.ndarray, xyz_sensor_m: np.ndarray,
               refl: np.ndarray, tag: np.ndarray, line: np.ndarray, *, decim: int = 1, flags: int = 0) -> bytes:
    """`awr.LidarScan8.v1` 载荷（不含通用 blob 头；V0.2 由 17 登记后接入扫描下行）。"""
    n = len(xyz_sensor_m)
    head = np.zeros(1, [("t", "<i8"), ("seq", "<u4"), ("n", "<u4"), ("pos", "<f4", (3,)), ("q", "<f4", (4,)),
                        ("decim", "<u2"), ("flags", "<u2")])
    head["t"], head["seq"], head["n"] = int(t_frame_ns), int(frame_seq), n
    head["pos"], head["q"], head["decim"], head["flags"] = pos, q_xyzw, int(decim), int(flags)
    body = np.zeros(n, [("x", "<i2"), ("y", "<i2"), ("z", "<i2"), ("refl", "u1"), ("tl", "u1")])
    cm = np.clip(np.round(np.asarray(xyz_sensor_m, np.float64) * 100.0), -32768, 32767).astype(np.int16)
    body["x"], body["y"], body["z"] = cm[:, 0], cm[:, 1], cm[:, 2]
    body["refl"] = refl
    body["tl"] = (np.asarray(tag, np.uint8) & 0x3F) | (np.asarray(line, np.uint8) << 6)
    return head.tobytes() + body.tobytes()
