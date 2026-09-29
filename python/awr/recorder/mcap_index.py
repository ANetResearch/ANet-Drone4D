"""SegmentIndex：谱系裁剪的逐 channel 消息索引（M12 §6.7.2；FR-042；RK-M12-02、RK-M12-09、RK-M12-12）。

打开一段录制时读 summary（已关闭段），否则按记录线性遍历（OPEN 段或崩溃后无 summary 的段，只读到最后一个完整 chunk）；
对每个 chunk 的每个 channel 读 MessageIndex 记录（未压缩，numpy 视图），按 chunk 所属纪元的谱系有效窗口
`[t_start_k, min_{j>k} restored_j)` 裁剪后拼接为按时刻有序的数组（时刻、chunk 序号、chunk 内偏移、段内写入序号）。
回滚重跑时新旧纪元在仿真时间上重叠，裁剪后每个时刻只有一个纪元，seek 自然落在"覆盖 t 的最新纪元"。

- `last_le(ch, t)`：不晚于 t 的最后一条（同刻取最后写入），O(log n)；`exact(ch, t)`、`range(ch, t0, t1)`（t0 < t ≤ t1）；
- `/event` 由多个生产者合流，按时刻稳定排序并保留 `mseq → 索引下标` 的映射（`.evx` 的 mseq）；
- 逆序 `iter_messages` 在 N = 1000、10 min 下每次 seek 648–838 ms，本模块不使用（契约测试禁止）；
- 尾部 chunk 的 MessageIndex 不完整时（写到一半崩溃）该 chunk 丢弃，读到上一个完整 chunk（M12-AC-040）。
"""

from __future__ import annotations

import io
import logging
import struct
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
from mcap.data_stream import ReadDataStream
from mcap.reader import SeekingReader
from mcap.records import Channel, Metadata

from .formats import T_EVENT

__all__ = ["ChunkInfo", "MsgRef", "SegmentIndex", "read_record_at"]

log = logging.getLogger("awr.recorder.index")

MAGIC = b"\x89MCAP0\r\n"
OP_HEADER, OP_FOOTER, OP_SCHEMA, OP_CHANNEL, OP_MESSAGE, OP_CHUNK, OP_MSGIDX = 1, 2, 3, 4, 5, 6, 7
OP_CHUNKIDX, OP_ATTACH, OP_ATTACHIDX, OP_STATS, OP_META, OP_METAIDX, OP_SUMOFF, OP_DATAEND = 8, 9, 10, 11, 12, 13, 14, 15
MI = np.dtype([("t", "<u8"), ("off", "<u8")])
_REC = struct.Struct("<BQ")
_CHUNK_HDR = struct.Struct("<QQQI")


@dataclass
class MsgRef:
    t: int
    chunk: int
    off: int


@dataclass
class ChunkInfo:
    start: int  # Chunk 记录在文件中的偏移
    length: int
    t0: int
    t1: int
    compression: str
    uncompressed_size: int
    msg_index: dict[int, int] = field(default_factory=dict)  # channel -> MessageIndex 记录偏移


@dataclass
class ChannelInfo:
    id: int
    topic: str
    encoding: str
    metadata: dict[str, str]


def read_record_at(f: BinaryIO, off: int) -> tuple[int, bytes]:
    f.seek(off)
    hdr = f.read(_REC.size)
    op, ln = _REC.unpack(hdr)
    return op, f.read(ln)


def _chunk_header(body: bytes | memoryview) -> tuple[int, int, int, int, str, int, int]:
    """(t0, t1, uncompressed_size, crc, compression, records_off, records_len) of a Chunk body."""
    t0, t1, usize, crc = _CHUNK_HDR.unpack_from(body, 0)
    p = _CHUNK_HDR.size
    cl = struct.unpack_from("<I", body, p)[0]
    comp = bytes(body[p + 4:p + 4 + cl]).decode()
    p += 4 + cl
    rl = struct.unpack_from("<Q", body, p)[0]
    return t0, t1, usize, crc, comp, p + 8, rl


class SegmentIndex:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.f: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - 与索引同生命周期，close() 关闭
        self.channels: dict[int, ChannelInfo] = {}
        self.topic_ch: dict[str, int] = {}
        self.chunks: list[ChunkInfo] = []
        self.metadata: list[tuple[str, dict[str, str]]] = []
        self.idx: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self.by_ord: dict[int, np.ndarray] = {}
        self.n_seen: dict[int, int] = defaultdict(int)
        self.closed = False
        self.scan_end = 0  # OPEN 段：下一次追读的文件偏移
        self.data_start_ns = 0
        self.data_end_ns = 0
        self.file_bytes = 0

    # ------------------------------------------------------------ 构建
    @classmethod
    def build(cls, path: Path, *, allow_open: bool = True) -> SegmentIndex:
        ix = cls(path)
        try:
            summ = SeekingReader(ix.f).get_summary()
        except Exception:
            summ = None
        if summ is not None and summ.chunk_indexes is not None:
            ix.closed = True
            for cid, ch in summ.channels.items():
                ix._add_channel(cid, ch.topic, ch.message_encoding, dict(ch.metadata))
            for ci in summ.chunk_indexes:
                ix.chunks.append(ChunkInfo(ci.chunk_start_offset, ci.chunk_length, ci.message_start_time, ci.message_end_time,
                                           ci.compression, ci.uncompressed_size, dict(ci.message_index_offsets)))
            for mi in summ.metadata_indexes:
                op, body = read_record_at(ix.f, mi.offset)
                if op == OP_META:
                    m = Metadata.read(ReadDataStream(io.BytesIO(body)))
                    ix.metadata.append((m.name, dict(m.metadata)))
            ix.f.seek(0, io.SEEK_END)
            ix.file_bytes = ix.f.tell()
        else:
            if not allow_open:
                raise ValueError(f"segment has no summary: {path}")
            ix._scan(8)
        ix._build_arrays(0)
        return ix

    def _add_channel(self, cid: int, topic: str, enc: str, meta: dict[str, str]) -> None:
        self.channels[cid] = ChannelInfo(cid, topic, enc, meta)
        self.topic_ch[topic] = cid

    def _scan(self, start: int) -> int:
        """线性遍历记录（OPEN 段）；返回新增的完整 chunk 数。"""
        f = self.f
        f.seek(0, io.SEEK_END)
        size = f.tell()
        self.file_bytes = size
        pos = start
        new = 0
        pending: ChunkInfo | None = None
        pending_end = 0
        while pos + _REC.size <= size:
            f.seek(pos)
            op, ln = _REC.unpack(f.read(_REC.size))
            end = pos + _REC.size + ln
            if end > size or op == 0 or op > OP_DATAEND:
                # 截断的记录：若它是 MessageIndex 之外的合法记录（下一个 chunk 或 metadata 已开始写），上一个 chunk 的
                # 索引组必然已完整（writer 在一次写入中写完 chunk 与其全部 MessageIndex）
                if pending is not None and 0 < op <= OP_DATAEND and op != OP_MSGIDX:
                    self._accept_chunk(pending)
                    new += 1
                    self.scan_end = pending_end
                break
            if op == OP_CHUNK:
                hdr = f.read(min(ln, 256))  # 先读本记录（_accept_chunk 会移动文件位置）
                if pending is not None:  # 上一个 chunk 的索引组已完整
                    self._accept_chunk(pending)
                    new += 1
                    self.scan_end = pending_end
                t0, t1, usize, _crc, comp, _ro, _rl = _chunk_header(hdr)
                pending = ChunkInfo(pos, _REC.size + ln, t0, t1, comp, usize, {})
                pending_end = end
            elif op == OP_MSGIDX:
                body = f.read(ln)
                if pending is not None:
                    cid = struct.unpack_from("<H", body, 0)[0]
                    pending.msg_index[cid] = pos
                    pending_end = end
            elif op == OP_META:
                body = f.read(ln)
                if pending is not None:
                    self._accept_chunk(pending)
                    new += 1
                    pending = None
                m = Metadata.read(ReadDataStream(io.BytesIO(body)))
                self.metadata.append((m.name, dict(m.metadata)))
                self.scan_end = end
            elif op == OP_CHANNEL:
                body = f.read(ln)
                ch = Channel.read(ReadDataStream(io.BytesIO(body)))
                self._add_channel(ch.id, ch.topic, ch.message_encoding, dict(ch.metadata))
            elif op in (OP_DATAEND, OP_FOOTER):
                if pending is not None:
                    self._accept_chunk(pending)
                    new += 1
                    pending = None
                self.scan_end = end
                if op == OP_DATAEND:
                    self.closed = True
            pos = end
        # 尾部 chunk：其后没有新的记录，MessageIndex 组可能不完整 → 暂不接受（下一次追读时再判断）
        return new

    def _accept_chunk(self, c: ChunkInfo) -> None:
        # chunk 内的 Channel 记录：出现未知 channel 时解压该 chunk 取 Channel 定义
        if any(cid not in self.channels for cid in c.msg_index):
            raw = self._decompress(c)
            p = 0
            while p + _REC.size <= len(raw):
                op, ln = _REC.unpack_from(raw, p)
                if op == OP_CHANNEL:
                    ch = Channel.read(ReadDataStream(io.BytesIO(bytes(raw[p + _REC.size:p + _REC.size + ln]))))
                    self._add_channel(ch.id, ch.topic, ch.message_encoding, dict(ch.metadata))
                p += _REC.size + ln
        self.chunks.append(c)

    def _decompress(self, c: ChunkInfo) -> bytes:
        import zstandard

        _op, body = read_record_at(self.f, c.start)
        _t0, _t1, usize, _crc, comp, ro, rl = _chunk_header(body)
        data = body[ro:ro + rl]
        if comp == "zstd":
            return zstandard.ZstdDecompressor().decompress(data, max_output_size=usize)
        if comp == "":
            return bytes(data)
        raise ValueError(f"unsupported chunk compression {comp!r}")

    # ------------------------------------------------------------ 谱系与数组
    def lineage(self) -> list[dict[str, int]]:
        out = []
        for name, m in self.metadata:
            if name == "awr.lineage":
                try:
                    out.append({k: int(v) for k, v in m.items()})
                except ValueError:
                    continue
        return out

    def binding(self) -> dict[str, str]:
        for name, m in self.metadata:
            if name == "awr.binding":
                return dict(m)
        return {}

    def segment_end(self) -> dict[str, str] | None:
        for name, m in self.metadata:
            if name == "awr.segment_end":
                return dict(m)
        return None

    def _chunk_windows(self) -> list[int]:
        """每个 chunk 的有效上界（仿真时间，ns）：该 chunk 所属纪元之后各纪元 restored_t 的最小值。"""
        lin = self.lineage()
        starts = [x.get("chunk_start", -1) for x in lin]
        uppers = []
        big = 2 ** 63 - 1
        for ci, c in enumerate(self.chunks):
            k = 0  # 所属纪元序号：chunk_start ≤ ci 的谱系条目数
            for j, cs in enumerate(starts):
                # 优先 chunk_start；缺失时按 chunk 起始时刻不早于 restored_t 回退检测
                if (0 <= cs <= ci) or (cs < 0 and lin[j].get("restored_t_ns", big) <= c.t0):
                    k = j + 1
            later = [x["restored_t_ns"] for x in lin[k:] if "restored_t_ns" in x]
            uppers.append(min(later) if later else big)
        return uppers

    def _build_arrays(self, first_chunk: int) -> None:
        uppers = self._chunk_windows()
        parts: dict[int, list] = defaultdict(list)
        f = self.f
        for ci in range(first_chunk, len(self.chunks)):
            c = self.chunks[ci]
            hi = uppers[ci]
            for cid, off in sorted(c.msg_index.items(), key=lambda x: x[1]):
                op, body = read_record_at(f, off)
                if op != OP_MSGIDX:
                    continue
                n = struct.unpack_from("<I", body, 2)[0] // 16
                a = np.frombuffer(body, MI, count=n, offset=6)
                ords = np.arange(self.n_seen[cid], self.n_seen[cid] + n, dtype=np.uint32)
                self.n_seen[cid] += n
                keep = a["t"] < np.uint64(hi) if hi < 2 ** 63 - 1 else np.ones(n, bool)
                parts[cid].append((a["t"][keep].astype(np.int64), np.full(int(keep.sum()), ci, np.int32), a["off"][keep].copy(),
                                   ords[keep]))
        for cid, ps in parts.items():
            t, c, o, w = (np.concatenate(x) for x in zip(*ps, strict=True))
            if cid in self.idx:
                t0, c0, o0, w0 = self.idx[cid]
                t, c, o, w = np.concatenate([t0, t]), np.concatenate([c0, c]), np.concatenate([o0, o]), np.concatenate([w0, w])
            if len(t) > 1 and np.any(t[1:] < t[:-1]):
                k = np.lexsort((w, t))
                t, c, o, w = t[k], c[k], o[k], w[k]
            self.idx[cid] = (t, c, o, w)
            if self.channels.get(cid) is not None and self.channels[cid].topic == T_EVENT:
                bo = np.full(self.n_seen[cid], -1, np.int64)
                bo[w] = np.arange(len(w))
                self.by_ord[cid] = bo
        ts = [v[0] for v in self.idx.values() if len(v[0])]
        if ts:
            self.data_start_ns = int(min(int(x[0]) for x in ts))
            self.data_end_ns = int(max(int(x[-1]) for x in ts))

    def refresh_tail(self) -> int:
        """OPEN 段：读取新增的完整 chunk；返回新增索引条目数。"""
        if self.closed:
            return 0
        before = sum(len(v[0]) for v in self.idx.values())
        n0 = len(self.chunks)
        new = self._scan(max(self.scan_end, 8))
        if new:
            self._build_arrays(n0)
        return sum(len(v[0]) for v in self.idx.values()) - before

    # ------------------------------------------------------------ 查询
    def ch(self, topic: str) -> int | None:
        return self.topic_ch.get(topic)

    def topics(self) -> list[str]:
        return sorted(self.topic_ch)

    def count(self, ch: int | None) -> int:
        return 0 if ch is None or ch not in self.idx else len(self.idx[ch][0])

    def last_le(self, ch: int | None, t_ns: int) -> MsgRef | None:
        if ch is None or ch not in self.idx:
            return None
        t, c, o, _ = self.idx[ch]
        k = int(np.searchsorted(t, t_ns, side="right")) - 1
        return None if k < 0 else MsgRef(int(t[k]), int(c[k]), int(o[k]))

    def exact(self, ch: int | None, t_ns: int) -> MsgRef | None:
        r = self.last_le(ch, t_ns)
        return r if r is not None and r.t == t_ns else None

    def range(self, ch: int | None, t0_ns: int, t1_ns: int) -> Iterator[MsgRef]:
        """t0 < t ≤ t1，按时刻（同刻按写入）顺序。"""
        if ch is None or ch not in self.idx:
            return
        t, c, o, _ = self.idx[ch]
        a = int(np.searchsorted(t, t0_ns, side="right"))
        b = int(np.searchsorted(t, t1_ns, side="right"))
        for k in range(a, b):
            yield MsgRef(int(t[k]), int(c[k]), int(o[k]))

    def range_count(self, ch: int | None, t0_ns: int, t1_ns: int) -> tuple[int, int]:
        if ch is None or ch not in self.idx:
            return 0, 0
        t = self.idx[ch][0]
        return int(np.searchsorted(t, t0_ns, side="right")), int(np.searchsorted(t, t1_ns, side="right"))

    def at(self, ch: int, k: int) -> MsgRef:
        t, c, o, _ = self.idx[ch]
        return MsgRef(int(t[k]), int(c[k]), int(o[k]))

    def memory_bytes(self) -> int:
        return sum(sum(a.nbytes for a in v) for v in self.idx.values()) + sum(v.nbytes for v in self.by_ord.values())

    def stats(self) -> dict[str, Any]:
        dur = max(1e-9, (self.data_end_ns - self.data_start_ns) / 1e9)
        ev = self.count(self.ch(T_EVENT))
        return {"bytes_per_sim_s": self.file_bytes / dur, "events_per_sim_s": ev / dur, "entries": sum(len(v[0]) for v in self.idx.values()),
                "chunks": len(self.chunks), "channels": len(self.channels), "memory_bytes": self.memory_bytes()}

    def close(self) -> None:
        self.f.close()
