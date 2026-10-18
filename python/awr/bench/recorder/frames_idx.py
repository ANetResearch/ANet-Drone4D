"""`frames.idx`（M27 §7.5.3；contracts bench/frame_index.v1 `x-layout`）：Run 关闭后由 `raw.mcap` 生成，只存偏移不存帧数据。

头：魔数 `D3FIDX01`、头 JSON 长度 u32、头 JSON `{raw_mcap_sha256, uavs[], sensors[], n_records}`；记录 32 B 小端
`uav_no u16, sensor_no u16, seq u32, t_ns i64, chunk_offset u64, msg_offset u32, size u32`，按 (uav_no, sensor_no, seq) 升序；
尾：sha256（覆盖头与全部记录）。`size` 为 MCAP 消息数据长度（protobuf 封装后的字节）。
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from awr.bench.recorder.channels import parse_obs_topic
from awr.recorder.chunk_cache import ChunkCache
from awr.recorder.mcap_index import MsgRef, SegmentIndex

__all__ = ["MAGIC", "REC", "FramesIdx", "build_frames_idx"]

MAGIC = b"D3FIDX01"
REC = np.dtype([("uav_no", "<u2"), ("sensor_no", "<u2"), ("seq", "<u4"), ("t_ns", "<i8"), ("chunk_offset", "<u8"), ("msg_offset", "<u4"),
                ("size", "<u4")])
assert REC.itemsize == 32


def _file_sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while b := f.read(1 << 22):
            h.update(b)
    return h.hexdigest()


def build_frames_idx(raw_mcap: Path, out: Path, *, parse: Callable[[str], tuple[str, str] | None] = parse_obs_topic) -> dict:
    """扫描已关闭的 `raw.mcap` 的相机通道（image、depth），写出 `frames.idx`，返回头 JSON。真值文件以
    `parse = parse_truth_topic` 生成同格式的 `truth/frames.idx`（头中 `raw_mcap_sha256` 为真值文件的摘要）。"""
    ix = SegmentIndex.build(Path(raw_mcap), allow_open=False)
    cache = ChunkCache(ix)
    rows: list[tuple[str, str, int, int, int, int, int]] = []
    try:
        for cid, ch in ix.channels.items():
            us = parse(ch.topic)
            if us is None or cid not in ix.idx:
                continue
            t, c, o, _ = ix.idx[cid]
            for ti, ci, oi in zip(t.tolist(), c.tolist(), o.tolist(), strict=True):
                m = cache.message(MsgRef(ti, ci, oi))
                rows.append((us[0], us[1], m.sequence, ti, ix.chunks[ci].start, oi, len(m.data)))
    finally:
        ix.close()
    uavs = sorted({r[0] for r in rows})
    sensors = sorted({r[1] for r in rows})
    un = {u: i for i, u in enumerate(uavs)}
    sn = {s: i for i, s in enumerate(sensors)}
    a = np.zeros(len(rows), REC)
    if rows:
        a["uav_no"] = [un[r[0]] for r in rows]
        a["sensor_no"] = [sn[r[1]] for r in rows]
        a["seq"] = [r[2] for r in rows]
        a["t_ns"] = [r[3] for r in rows]
        a["chunk_offset"] = [r[4] for r in rows]
        a["msg_offset"] = [r[5] for r in rows]
        a["size"] = [r[6] for r in rows]
        a = a[np.lexsort((a["seq"], a["sensor_no"], a["uav_no"]))]
        key = (a["uav_no"].astype(np.uint64) << np.uint64(48)) | (a["sensor_no"].astype(np.uint64) << np.uint64(32)) | a["seq"].astype(np.uint64)
        if np.any(key[1:] == key[:-1]):
            raise ValueError("duplicate (uav, sensor, seq) in raw.mcap")
    head = {"raw_mcap_sha256": _file_sha256(Path(raw_mcap)), "uavs": uavs, "sensors": sensors, "n_records": len(a)}
    hj = json.dumps(head, separators=(",", ":"), sort_keys=True).encode()
    body = MAGIC + struct.pack("<I", len(hj)) + hj + a.tobytes()
    tmp = Path(out).with_suffix(".idx.tmp")
    tmp.write_bytes(body + hashlib.sha256(body).digest())
    tmp.replace(out)
    return head


@dataclass
class FramesIdx:
    header: dict
    rec: np.ndarray
    key: np.ndarray

    @classmethod
    def open(cls, path: Path) -> FramesIdx:
        b = Path(path).read_bytes()
        if b[:8] != MAGIC or len(b) < 12 + 32:
            raise ValueError(f"{path}: not a frames.idx file")
        if hashlib.sha256(b[:-32]).digest() != b[-32:]:
            raise ValueError(f"{path}: trailer sha256 mismatch")
        (hl,) = struct.unpack_from("<I", b, 8)
        head = json.loads(b[12:12 + hl])
        rec = np.frombuffer(b, REC, count=head["n_records"], offset=12 + hl)
        key = (rec["uav_no"].astype(np.uint64) << np.uint64(48)) | (rec["sensor_no"].astype(np.uint64) << np.uint64(32)) | rec["seq"].astype(np.uint64)
        return cls(head, rec, key)

    def _k(self, uav: str, sensor: str, seq: int) -> int | None:
        try:
            return (self.header["uavs"].index(uav) << 48) | (self.header["sensors"].index(sensor) << 32) | int(seq)
        except ValueError:
            return None

    def find(self, uav: str, sensor: str, seq: int) -> np.void | None:
        k = self._k(uav, sensor, seq)
        if k is None or not 0 <= seq < 2 ** 32:
            return None
        i = int(np.searchsorted(self.key, np.uint64(k)))
        return self.rec[i] if i < len(self.key) and int(self.key[i]) == k else None

    def span(self, uav: str, sensor: str, seq_from: int, seq_to: int) -> np.ndarray:
        """seq ∈ [seq_from, seq_to] 的记录（升序）。"""
        lo = self._k(uav, sensor, max(seq_from, 0))
        if lo is None or seq_to < seq_from:
            return self.rec[:0]
        hi = self._k(uav, sensor, min(seq_to, 2 ** 32 - 1))
        i = int(np.searchsorted(self.key, np.uint64(lo)))
        j = int(np.searchsorted(self.key, np.uint64(hi), side="right"))
        return self.rec[i:j]

    def channels(self) -> list[tuple[str, str]]:
        u, s = self.rec["uav_no"], self.rec["sensor_no"]
        pairs = sorted({(int(a), int(b)) for a, b in zip(u.tolist(), s.tolist(), strict=True)})
        return [(self.header["uavs"][a], self.header["sensors"][b]) for a, b in pairs]
