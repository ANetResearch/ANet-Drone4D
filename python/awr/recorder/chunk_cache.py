"""ChunkCache 与 ReadAhead 线程（M12 §6.7.3；FR-043）。

- ChunkCache：LRU，键为 chunk 序号，值为解压后的记录区字节；上限 64 MB（约 60 个 ×1 chunk）；线程安全；
- `message(ref)`：按 SegmentIndex 给出的 (chunk, 偏移) 取一条 Message 记录：(channel, sequence, log_time, data)；
- ReadAhead：从 t_play 所在 chunk 起按文件顺序预解压，窗口 `[t_play, t_play + max(2 s, 1.5 s × rate)]`；seek 时从目标
  chunk 重新开始；`ready_until()` 为从 t_play 所在 chunk 起连续已解压 chunk 的最大结束时刻（段尾为 +∞）。
"""

from __future__ import annotations

import collections
import struct
import threading
from typing import NamedTuple

import numpy as np
import zstandard

from .mcap_index import MsgRef, SegmentIndex, _chunk_header, read_record_at

__all__ = ["ChunkCache", "Msg", "ReadAhead"]

_REC = struct.Struct("<BQ")
_MSG = struct.Struct("<HIQQ")
FOREVER = 2 ** 63 - 1


class Msg(NamedTuple):
    channel: int
    sequence: int
    log_time: int
    data: bytes


class ChunkCache:
    def __init__(self, index: SegmentIndex, cap_bytes: int = 64 << 20) -> None:
        self.index = index
        self.cap = cap_bytes
        self.lru: collections.OrderedDict[int, bytes] = collections.OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()
        self.io_lock = threading.Lock()
        self.stats = {"hits": 0, "misses": 0, "evictions": 0}

    def has(self, ci: int) -> bool:
        with self.lock:
            return ci in self.lru

    def get(self, ci: int) -> bytes:
        with self.lock:
            b = self.lru.get(ci)
            if b is not None:
                self.lru.move_to_end(ci)
                self.stats["hits"] += 1
                return b
        b = self._load(ci)
        with self.lock:
            if ci not in self.lru:
                self.lru[ci] = b
                self.bytes += len(b)
                self.stats["misses"] += 1
                while self.bytes > self.cap and len(self.lru) > 1:
                    _k, v = self.lru.popitem(last=False)
                    self.bytes -= len(v)
                    self.stats["evictions"] += 1
            return self.lru[ci]

    def _load(self, ci: int) -> bytes:
        c = self.index.chunks[ci]
        with self.io_lock:  # 文件句柄与 SegmentIndex 共用
            _op, body = read_record_at(self.index.f, c.start)
        _t0, _t1, usize, _crc, comp, ro, rl = _chunk_header(body)
        data = body[ro:ro + rl]
        if comp == "zstd":
            return zstandard.ZstdDecompressor().decompress(data, max_output_size=usize)
        return bytes(data)

    def message(self, ref: MsgRef) -> Msg:
        raw = self.get(ref.chunk)
        _op, ln = _REC.unpack_from(raw, ref.off)
        ch, seq, lt, _pt = _MSG.unpack_from(raw, ref.off + _REC.size)
        start = ref.off + _REC.size + _MSG.size
        return Msg(ch, seq, lt, raw[start:ref.off + _REC.size + ln])

    def clear(self) -> None:
        with self.lock:
            self.lru.clear()
            self.bytes = 0


class ReadAhead:
    def __init__(self, cache: ChunkCache, *, min_window_ns: int = 2_000_000_000, wall_window_s: float = 1.5) -> None:
        self.cache = cache
        self.index = cache.index
        self.min_window_ns = min_window_ns
        self.wall_window_s = wall_window_s
        self.t_play = 0
        self.rate = 1.0
        self.gen = 0
        self.cv = threading.Condition()
        self.stop_flag = False
        self.thread = threading.Thread(target=self._main, name="replay-readahead", daemon=True)
        self.thread.start()

    def window_ns(self) -> int:
        return max(self.min_window_ns, int(self.wall_window_s * self.rate * 1e9))

    def restart(self, t_ns: int, rate: float | None = None) -> None:
        with self.cv:
            self.t_play = int(t_ns)
            if rate is not None:
                self.rate = rate
            self.gen += 1
            self.cv.notify()

    def advance(self, t_ns: int) -> None:
        with self.cv:
            self.t_play = int(t_ns)
            self.cv.notify()

    def _first_chunk(self, t_ns: int) -> int:
        """第一个结束时刻 ≥ t 的 chunk（按累计最大结束时刻二分，chunk 时间段可有毫秒级交错）。"""
        chunks = self.index.chunks
        if len(chunks) != getattr(self, "_n_t1", -1):
            self._t1max = np.maximum.accumulate(np.array([c.t1 for c in chunks], np.int64)) if chunks else np.zeros(0, np.int64)
            self._n_t1 = len(chunks)
        return int(np.searchsorted(self._t1max, t_ns, side="left"))

    def ready_until(self) -> int:
        """从 t_play 所在 chunk 起连续已解压 chunk 的最大结束时刻；到段尾且段已关闭为 +∞。"""
        chunks = self.index.chunks
        ci = self._first_chunk(self.t_play)
        until = self.t_play
        while ci < len(chunks) and self.cache.has(ci):
            until = max(until, chunks[ci].t1)
            ci += 1
        if ci >= len(chunks):
            return FOREVER if self.index.closed else max(until, self.index.data_end_ns)
        return until

    def _main(self) -> None:
        while True:
            with self.cv:
                if self.stop_flag:
                    return
                t, gen = self.t_play, self.gen
            chunks = self.index.chunks
            ci = self._first_chunk(t)
            hi = t + self.window_ns()
            did = False
            while ci < len(chunks) and chunks[ci].t0 <= hi:
                with self.cv:
                    if self.stop_flag or self.gen != gen:
                        break
                if not self.cache.has(ci):
                    try:
                        self.cache.get(ci)
                    except Exception:
                        break
                    did = True
                ci += 1
            with self.cv:
                if self.stop_flag:
                    return
                if not did and self.gen == gen:
                    self.cv.wait(0.02)

    def close(self) -> None:
        with self.cv:
            self.stop_flag = True
            self.cv.notify()
        self.thread.join(2)
