"""契约 1.3.0 的帧与轨迹契约（AWR-17 §18.8.1–§18.8.3；M27 §7.5.3；WP-D3-01）：frame_index.v1、d3frames.v1、tracks.v1 的 JSON 头与
x-layout 二进制布局（记录字节数与 struct 计算一致）。"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ctlib import errors, load

FMT = {"u8": "B", "u16": "H", "u32": "I", "u64": "Q", "i64": "q", "f32": "f", "f64": "d"}


def _fmt(fields: list) -> str:
    out = "<"
    for _, t in fields:
        if t in FMT:
            out += FMT[t]
        elif t.startswith("u8[") and t[3:-1].isdigit():
            out += f"{t[3:-1]}s"
    return out


def test_record_sizes() -> None:
    fi = load("bench/frame_index.v1.schema.json")["x-layout"]
    assert struct.calcsize(_fmt(fi["record"]["fields"])) == fi["record"]["bytes"] == 32
    tk = load("bench/tracks.v1.schema.json")["x-layout"]
    assert struct.calcsize(_fmt(tk["record"]["fields"])) == tk["record"]["bytes"] == 56
    fr = load("bench/frames.v1.schema.json")["x-layout"]
    fixed = [f for f in fr["record"]["fields"] if f[0] != "payload"]
    assert struct.calcsize(_fmt(fixed)) == fr["record"]["fixed_bytes"] == 48
    assert struct.calcsize(_fmt(fr["header"][:3])) == 8 and struct.calcsize(_fmt(tk["header"][:3])) == 8


def test_frame_index_and_headers() -> None:
    idx = {"schema": "drone4d.bench.frame_index.v1", "run_id": "run-k2m4q7xz3a", "uav_id": "uav-01", "sensor_id": "cam0",
           "channel": "/uav/uav-01/camera/cam0/image", "encoding": "foxglove.CompressedImage", "codec": "jpeg", "agent_delivered": True,
           "period_ns": 100_000_000, "items": [{"seq": 842, "t_ns": 84_196_000_000, "bytes": 331_264, "sha256": "9" * 64}], "next_seq": 843}
    assert errors("bench/frame_index.v1.schema.json", idx) == []
    assert errors("bench/frame_index.v1.schema.json", {**idx, "sensor_id": "cam0.depth", "encoding": "foxglove.RawImage", "codec": "32FC1"}) == []
    assert errors("bench/frame_index.v1.schema.json", {**idx, "items": idx["items"] * 1001})
    hdr = {"run_id": "r1", "uav_id": "uav-01", "sensor_id": "cam0", "encoding": "foxglove.CompressedImage", "codec": "jpeg", "n": 3,
           "seq_from": 0, "seq_to": 20, "stride": 10}
    assert errors("bench/frames.v1.schema.json", hdr) == []
    assert errors("bench/frames.v1.schema.json", {**hdr, "stride": 0})
    tk = {"run_id": "r1", "hz": 10, "t0_ns": 0, "n_samples": 3600, "uavs": [{"uav_id": "uav-01", "primary_camera": "cam0"}]}
    assert errors("bench/tracks.v1.schema.json", tk) == []
