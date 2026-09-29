"""McapSegmentWriter：writer 线程、有界队列、1 s flush、metadata、分段与谱系（M12 §6.6.4、§6.6.5；FR-034 至 FR-037）。

- 主线程只入队（`put`、`metadata`、`lineage`、`start_segment`、`end_segment`）；mcap 1.5.0 的 zstd 压缩在 `add_message`/`flush`
  所在线程同步执行，因此全部写入在 writer 线程完成（zstandard 压缩时释放 GIL，不影响主循环）；
- 队列上限 64 MB：先丢 Full64（75 %），再丢块消息（100 %）；事件、环境、roster、时钟、低频块与 metadata 永不丢弃；
- 每 1 s（墙钟）`flush()` 结束当前 chunk，并在同一时刻刷写 `.ovw`、`.evx`（kill -9 丢失 ≤ 1 s）；
- `add_message` 一律用关键字参数（mcap 1.5.0 的 publish_time 在 data 之后），`log_time = publish_time = t_sim_ns`；
- channel 惰性注册，metadata 写 `producer`、`record_policy`、`backfill`（16 §13.3）；schema 数据为契约引用；
- 谱系：`lineage()` 先入队 FLUSH（新纪元从新 chunk 开始），再写 `awr.lineage` metadata（含可选键 `chunk_start`）。
"""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from mcap.writer import CompressionType, IndexType, Writer

from awr.contracts import CONTRACTS_VERSION

from .formats import T_EVENT, channel_spec, writer_profile
from .sidecar import SidecarBuilder

__all__ = ["Kind", "McapSegmentWriter", "SegmentSpec", "SegmentStats"]

log = logging.getLogger("awr.recorder.writer")


class Kind(IntEnum):
    FULL = 1  # 标记机 Full64（先丢）
    BLOCK = 2  # 整群块（后丢）
    KEEP = 3  # 事件、环境、roster、时钟、低频块：永不丢弃


@dataclass
class SegmentSpec:
    k: int
    path: Path
    ovw_path: Path | None
    evx_path: Path | None
    binding: dict[str, str]
    tracks: list[int]
    agent_of: dict[str, int] = field(default_factory=dict)
    attachments: list[tuple[str, str, bytes]] = field(default_factory=list)  # (name, media_type, data)


@dataclass
class SegmentStats:
    k: int
    path: Path
    msgs: int = 0
    events: int = 0
    t_first: int = -1
    t_last: int = -1
    nbytes: int = 0
    chunks: int = 0
    min_block_dt: int = 0
    max_block_dt: int = 0
    reason: str = ""


@dataclass
class _Msg:
    kind: Kind
    topic: str
    t_ns: int
    data: bytes
    seq: int


_FLUSH = object()


class McapSegmentWriter:
    def __init__(self, *, queue_bytes: int = 64 << 20, flush_wall_s: float = 1.0,
                 on_closed: Callable[[SegmentStats], None] | None = None) -> None:
        self.queue_bytes = int(queue_bytes)
        self.drop_at = {Kind.FULL: self.queue_bytes * 3 // 4, Kind.BLOCK: self.queue_bytes}
        self.flush_ns = int(flush_wall_s * 1e9)
        self.on_closed = on_closed
        self.q: collections.deque[Any] = collections.deque()
        self.cv = threading.Condition()
        self.q_bytes = 0
        self.dropped = {"full": 0, "block": 0}
        self.dropped_since_gap = {"full": 0, "block": 0}
        self.stats: SegmentStats | None = None
        self.closed_stats: list[SegmentStats] = []
        self.busy = False
        self._stop = False
        self._error: BaseException | None = None
        self._w: Writer | None = None
        self._f: Any = None
        self._channels: dict[str, int] = {}
        self._schemas: dict[str, int] = {}
        self._side: SidecarBuilder | None = None
        self._last_block_t = -1
        self.msgs_total = 0
        self.bytes_total = 0
        self.thread = threading.Thread(target=self._main, name="rec-writer", daemon=True)
        self.thread.start()

    # ------------------------------------------------------------ 主线程
    def _enqueue(self, item: Any, nbytes: int = 0) -> None:
        with self.cv:
            self.q.append(item)
            self.q_bytes += nbytes
            self.cv.notify()

    def put(self, kind: Kind, topic: str, t_ns: int, data: bytes, seq: int = 0) -> bool:
        limit = self.drop_at.get(kind)
        if limit is not None and self.q_bytes + len(data) > limit:
            key = "full" if kind == Kind.FULL else "block"
            self.dropped[key] += 1
            self.dropped_since_gap[key] += 1
            return False
        self._enqueue(_Msg(kind, topic, int(t_ns), data, int(seq)), len(data))
        return True

    def start_segment(self, spec: SegmentSpec) -> None:
        self._enqueue(("open", spec))

    def metadata(self, name: str, data: dict[str, Any]) -> None:
        self._enqueue(("meta", name, {k: str(v) for k, v in data.items()}))

    def flush(self) -> None:
        self._enqueue(_FLUSH)

    def lineage(self, entry: dict[str, Any]) -> None:
        """先 FLUSH，保证新纪元从新 chunk 开始；`chunk_start` 在 writer 线程填入。"""
        self._enqueue(_FLUSH)
        self._enqueue(("lineage", dict(entry)))

    def end_segment(self, t_end_ns: int, reason: str) -> None:
        self._enqueue(("end", int(t_end_ns), reason))

    def drain(self, timeout_s: float = 5.0) -> bool:
        """等待队列排空且线程空闲；返回是否在时限内完成。"""
        deadline = time.monotonic() + timeout_s
        with self.cv:
            while self.q or self.busy:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cv.wait(min(left, 0.05))
        return True

    def close(self, timeout_s: float = 5.0) -> bool:
        ok = self.drain(timeout_s)
        with self.cv:
            self._stop = True
            self.cv.notify()
        self.thread.join(timeout_s)
        return ok

    def take_dropped(self) -> dict[str, int]:
        d = dict(self.dropped_since_gap)
        self.dropped_since_gap = {"full": 0, "block": 0}
        return d

    @property
    def error(self) -> BaseException | None:
        return self._error

    # ------------------------------------------------------------ writer 线程
    def _main(self) -> None:
        last = time.monotonic_ns()
        while True:
            with self.cv:
                while not self.q and not self._stop:
                    if time.monotonic_ns() - last >= self.flush_ns:
                        break
                    self.cv.wait(0.05)
                if self._stop and not self.q:
                    break
                item = self.q.popleft() if self.q else None
                if isinstance(item, _Msg):
                    self.q_bytes -= len(item.data)
                self.busy = item is not None
            try:
                if item is _FLUSH or time.monotonic_ns() - last >= self.flush_ns:
                    self._flush()
                    last = time.monotonic_ns()
                if item is not None and item is not _FLUSH:
                    self._handle(item)
            except BaseException as e:  # 写失败（磁盘满等）：记录并继续排空队列，主线程据 error 停录
                self._error = e
                log.exception("recorder writer failed")
            finally:
                with self.cv:
                    self.busy = False
                    self.cv.notify_all()
        try:
            if self._w is not None:
                self._finish("shutdown", self.stats.t_last if self.stats else 0)
        except BaseException as e:
            self._error = e

    def _handle(self, item: Any) -> None:
        if isinstance(item, _Msg):
            self._write_msg(item)
            return
        op = item[0]
        if op == "open":
            if self._w is not None:
                self._finish("rotate", self.stats.t_last if self.stats else 0)
            self._open(item[1])
        elif op == "meta":
            if self._w is not None:
                self._w.add_metadata(item[1], item[2])
        elif op == "lineage":
            if self._w is not None and self.stats is not None:
                e = item[1]
                e["chunk_start"] = self.stats.chunks
                self._w.add_metadata("awr.lineage", {k: str(v) for k, v in e.items()})
                if self._side is not None:
                    self._side.on_lineage(int(e["epoch"]), int(e["restored_t_ns"]), int(e["last_t_ns"]))
        elif op == "end":
            if self._w is not None:
                self._w.add_metadata("awr.segment_end", {"t_end_ns": str(item[1]), "reason": item[2]})
                self._finish(item[2], item[1])

    def _open(self, spec: SegmentSpec) -> None:
        prof = writer_profile()
        spec.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(spec.path, "wb")  # noqa: SIM115 - 生命周期跨越多次调用，由 _finish 关闭
        w = Writer(self._f, chunk_size=int(prof.get("chunk_size", 4 << 20)), compression=CompressionType.ZSTD,
                   index_types=IndexType.ALL, repeat_channels=True, repeat_schemas=True, use_chunking=True,
                   use_statistics=True, use_summary_offsets=True, enable_crcs=True)
        w.start(profile="awr", library=f"awr.recorder contracts {CONTRACTS_VERSION}")
        self._w = w
        self._channels = {}
        self._schemas = {}
        self._last_block_t = -1
        self.stats = SegmentStats(spec.k, spec.path)
        w.add_metadata("awr.binding", dict(spec.binding))
        for name, media, data in spec.attachments:
            w.add_attachment(create_time=time.time_ns(), log_time=0, name=name, media_type=media, data=data)
        self._side = SidecarBuilder(spec.ovw_path, spec.evx_path, segment=spec.k, tracks=spec.tracks,
                                    roster_agent_of=dict(spec.agent_of))

    def _channel(self, topic: str) -> int:
        ch = self._channels.get(topic)
        if ch is not None:
            return ch
        assert self._w is not None
        spec = channel_spec(topic)
        sid = self._schemas.get(spec.schema)
        if sid is None:
            data = json.dumps({"$ref": spec.schema, "contracts": CONTRACTS_VERSION}).encode()
            sid = self._w.register_schema(name=spec.schema, encoding=spec.schema_encoding, data=data)
            self._schemas[spec.schema] = sid
        ch = self._w.register_channel(topic=topic, message_encoding=spec.message_encoding, schema_id=sid,
                                      metadata={"record_policy": spec.record_policy, "backfill": spec.backfill,
                                                "producer": "sim-core" if topic != T_EVENT else "*"})
        self._channels[topic] = ch
        return ch

    def _write_msg(self, m: _Msg) -> None:
        w, st = self._w, self.stats
        if w is None or st is None:
            return
        w.add_message(channel_id=self._channel(m.topic), log_time=m.t_ns, data=m.data, publish_time=m.t_ns, sequence=m.seq & 0xFFFFFFFF)
        st.msgs += 1
        self.msgs_total += 1
        self.bytes_total += len(m.data)
        if st.t_first < 0 or m.t_ns < st.t_first:
            st.t_first = m.t_ns
        if m.t_ns > st.t_last:
            st.t_last = m.t_ns
        if m.topic == T_EVENT:
            st.events += 1
        elif m.kind == Kind.BLOCK:
            if self._last_block_t >= 0 and m.t_ns > self._last_block_t:
                dt = m.t_ns - self._last_block_t
                st.min_block_dt = dt if st.min_block_dt == 0 else min(st.min_block_dt, dt)
                st.max_block_dt = max(st.max_block_dt, dt)
            self._last_block_t = m.t_ns
        if self._side is not None:
            self._side.on_message(m.topic, m.t_ns, m.data)

    def _flush(self) -> None:
        if self._w is None or self.stats is None:
            return
        self._w.flush()
        self.stats.chunks = self._chunk_count()
        self.stats.nbytes = self._f.tell()
        if self._side is not None:
            self._side.flush()

    def _chunk_count(self) -> int:
        # mcap Writer 不公开 chunk 计数：统计对象中 chunk_count（私有名改写）
        st = getattr(self._w, "_Writer__statistics", None)
        return int(getattr(st, "chunk_count", 0)) if st is not None else 0

    def _finish(self, reason: str, t_end_ns: int) -> None:
        w, st = self._w, self.stats
        if w is None or st is None:
            return
        w.finish()
        st.nbytes = self._f.tell()
        st.chunks = self._chunk_count()
        st.reason = reason
        if t_end_ns and t_end_ns > st.t_last:
            st.t_last = t_end_ns
        self._f.close()
        if self._side is not None:
            self._side.close()
        self._w = None
        self._f = None
        self._side = None
        self.closed_stats.append(st)
        if self.on_closed is not None:
            try:
                self.on_closed(st)
            except Exception:
                log.exception("on_closed failed")
