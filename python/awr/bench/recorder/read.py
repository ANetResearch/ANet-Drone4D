"""帧读取（M27-FR-052；AWR-17 §18.8：BN-49 帧索引、BN-50 单帧、BN-51 区间帧 `d3frames.v1`）。

按 `frames.idx` 定位 `raw.mcap` 中的消息，块缓存复用 D2 `ChunkCache`；返回交付字节（`foxglove.CompressedImage.data` 即 Agent 收到
的 JPEG；深度为 f32 行主序，存档的字节重排 + zstd 在此解开），索引与容器中的 sha256 取交付字节。交付字节的长度与 sha256 按帧缓存，
索引分页不重复解码。
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

from awr.bench.recorder.channels import camera_topic, parse_obs_topic
from awr.bench.recorder.foxglove import decode_image
from awr.bench.recorder.frames_idx import FramesIdx
from awr.bench.recorder.lidar import LIDAR_CODEC
from awr.recorder.chunk_cache import ChunkCache
from awr.recorder.mcap_index import MsgRef, SegmentIndex

__all__ = ["CONTENT_TYPES", "MAX_FRAMES_PER_STRIDE", "MAX_RANGE_BYTES", "Frame", "FrameLimitError", "FrameStore"]

MAX_FRAMES_PER_STRIDE = 300
MAX_RANGE_BYTES = 64 << 20
MAX_INDEX_ITEMS = 1000
CONTENT_TYPES = {"jpeg": "image/jpeg", "32FC1": "application/octet-stream"}
_SCHEMA_OF_CODEC = {"jpeg": "foxglove.CompressedImage", "32FC1": "foxglove.RawImage"}


class FrameLimitError(ValueError):
    code = 698

    def __init__(self, detail: dict) -> None:
        super().__init__(f"698 FRAME_REQUEST_LIMIT {detail}")
        self.detail = detail


@dataclass
class Frame:
    seq: int
    t_ns: int
    codec: str
    payload: bytes
    width: int | None = None
    height: int | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES.get(self.codec, "application/octet-stream")


class FrameStore:
    """一个 Run 目录（`raw.mcap`、`frames.idx`）的只读视图；线程安全由 ChunkCache 保证。"""

    def __init__(self, run_dir: Path, run_id: str | None = None) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id or self.run_dir.name
        self.idx = FramesIdx.open(self.run_dir / "frames.idx")
        self.seg = SegmentIndex.build(self.run_dir / "raw.mcap", allow_open=False)
        self.cache = ChunkCache(self.seg)
        self.chunk_no = {c.start: i for i, c in enumerate(self.seg.chunks)}
        self.meta: dict[tuple[str, str], dict] = {}
        self._digest: dict[tuple[str, str, int], tuple[int, str]] = {}
        for ch in self.seg.channels.values():
            us = parse_obs_topic(ch.topic)
            if us is not None:
                self.meta[us] = {"channel": ch.topic, **ch.metadata}

    def close(self) -> None:
        self.seg.close()

    def channel_info(self, uav: str, sensor: str) -> dict | None:
        return self.meta.get((uav, sensor))

    def _frame(self, uav: str, sensor: str, r) -> Frame:
        info = self.meta[(uav, sensor)]
        codec = info.get("codec", "jpeg")
        m = self.cache.message(MsgRef(int(r["t_ns"]), self.chunk_no[int(r["chunk_offset"])], int(r["msg_offset"])))
        if codec == LIDAR_CODEC:                                     # awr.sensor.lidar_frame.v2：消息即载荷
            f = Frame(int(r["seq"]), int(r["t_ns"]), codec, bytes(m.data), None, None)
            self._digest[(uav, sensor, f.seq)] = (len(f.payload), f.sha256)
            return f
        d = decode_image(_SCHEMA_OF_CODEC[codec], m.data, info.get("compression"))
        f = Frame(int(r["seq"]), int(r["t_ns"]), d["codec"], d["payload"], d.get("width"), d.get("height"))
        self._digest[(uav, sensor, f.seq)] = (len(f.payload), f.sha256)
        return f

    def _digest_of(self, uav: str, sensor: str, r) -> tuple[int, str]:
        k = (uav, sensor, int(r["seq"]))
        if k not in self._digest:
            self._frame(uav, sensor, r)
        return self._digest[k]

    def frame(self, uav: str, sensor: str, seq: int) -> Frame | None:
        r = self.idx.find(uav, sensor, seq)
        return None if r is None else self._frame(uav, sensor, r)

    def latest_seq(self, uav: str, sensor: str) -> int | None:
        s = self.idx.span(uav, sensor, 0, 2 ** 32 - 1)
        return int(s["seq"][-1]) if len(s) else None

    def index_page(self, uav: str, sensor: str, seq_from: int = 0, seq_to: int | None = None) -> dict | None:
        """BN-49：`bench/frame_index.v1`，每页 ≤ 1000 条，`next_seq` 为下一页起点。"""
        info = self.channel_info(uav, sensor)
        if info is None:
            return None
        rs = self.idx.span(uav, sensor, seq_from, 2 ** 32 - 1 if seq_to is None else seq_to)
        page, more = rs[:MAX_INDEX_ITEMS], rs[MAX_INDEX_ITEMS:MAX_INDEX_ITEMS + 1]
        items = [{"seq": int(r["seq"]), "t_ns": int(r["t_ns"]), "bytes": n, "sha256": h}
                 for r in page for n, h in [self._digest_of(uav, sensor, r)]]
        codec = info.get("codec", "jpeg")
        return {"schema": "drone4d.bench.frame_index.v1", "run_id": self.run_id, "uav_id": uav, "sensor_id": sensor, "channel": info["channel"],
                "encoding": _SCHEMA_OF_CODEC[codec], "codec": codec, "agent_delivered": info.get("agent_delivered", "true") == "true",
                "period_ns": int(info.get("period_ns", 100_000_000)), "items": items,
                "next_seq": int(more["seq"][0]) if len(more) else None}

    def range_container(self, uav: str, sensor: str, seq_from: int, seq_to: int, stride: int = 1) -> bytes | None:
        """BN-51：`d3frames.v1` 容器；缺失序号不占记录；超出 300 × stride 帧或 64 MiB 时 698。"""
        info = self.channel_info(uav, sensor)
        if info is None:
            return None
        stride = max(int(stride), 1)
        if seq_to < seq_from or seq_to - seq_from + 1 > MAX_FRAMES_PER_STRIDE * stride:
            raise FrameLimitError({"max_frames": MAX_FRAMES_PER_STRIDE, "max_bytes": MAX_RANGE_BYTES})
        rs = [r for r in self.idx.span(uav, sensor, seq_from, seq_to) if (int(r["seq"]) - seq_from) % stride == 0]
        codec = info.get("codec", "jpeg")
        head = {"run_id": self.run_id, "uav_id": uav, "sensor_id": sensor, "encoding": _SCHEMA_OF_CODEC[codec], "codec": codec, "n": len(rs),
                "seq_from": int(seq_from), "seq_to": int(seq_to), "stride": stride}
        hj = json.dumps(head, separators=(",", ":")).encode()
        parts = [b"D3FR", struct.pack("<HHI", 1, 0, len(hj)), hj]
        total = sum(len(p) for p in parts) + 32
        for r in rs:
            f = self._frame(uav, sensor, r)
            total += 48 + len(f.payload)
            if total > MAX_RANGE_BYTES:
                raise FrameLimitError({"max_frames": MAX_FRAMES_PER_STRIDE, "max_bytes": MAX_RANGE_BYTES})
            parts += [struct.pack("<IqI", f.seq, f.t_ns, len(f.payload)), hashlib.sha256(f.payload).digest(), f.payload]
        body = b"".join(parts)
        return body + hashlib.sha256(body).digest()


class TruthStore:
    """真值文件 `<event>/truth/run_<id>.mcap` 与其索引 `run_<id>.frames.idx` 的只读视图（BN-52）：按 (uav, cam, seq) 返回
    `camera_gt.v1` 消息字节。"""

    def __init__(self, mcap_path: Path, idx_path: Path) -> None:
        self.idx = FramesIdx.open(idx_path)
        self.seg = SegmentIndex.build(mcap_path, allow_open=False)
        self.cache = ChunkCache(self.seg)
        self.chunk_no = {c.start: i for i, c in enumerate(self.seg.chunks)}

    def close(self) -> None:
        self.seg.close()

    def get(self, uav: str, cam: str, seq: int) -> tuple[int, bytes] | None:
        r = self.idx.find(uav, cam, seq)
        if r is None:
            return None
        m = self.cache.message(MsgRef(int(r["t_ns"]), self.chunk_no[int(r["chunk_offset"])], int(r["msg_offset"])))
        return int(r["t_ns"]), bytes(m.data)


def topic_for(uav: str, sensor: str) -> str:
    cam, _, kind = sensor.partition(".")
    return camera_topic(uav, cam, "depth" if kind == "depth" else "image")
