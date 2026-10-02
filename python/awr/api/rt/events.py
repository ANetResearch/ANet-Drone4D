"""EventIngest 与全局 EventRing（M11-FR-065 至 FR-070；M11 §6.4.12；AWR-17 §6.12、§9.5）。

`awr.runtime.events.EventSubscriber` 订阅 `evt/**`（回调线程只入队），在 Gateway tick 中 `pump()`：按 (producer, epoch)
去重、缺口补拉 `_replay`、重排后按生产者序交付（缺口补齐前该生产者的后续事件暂存，1 s 未补齐则报告缺口并放行）。交付的事件：
- `cmd.*`：先交给 RpcRouter 转成 `result`/`progress`（只发给发起连接）；批量子调用（带 `batch_id`）只汇总，不进 EventRing；
  `cmd.progress` 不进 EventRing；
- `env.keyframe`：只更新 `env/state` 的自包含 latest（EnvCache），不进 EventRing；
- `sim.started`、`sim.reset`：触发 roster 重取，并作纪元判定的一致性校验（与头部矛盾时以头部为准，计 `epoch_mismatch`）；
- `proc.state`（supervisor）：更新进程状态表与 `status{id: "proc.<name>"}`；`seat.*`（sim-core）：刷新席位缓存；
- 其余并入全局 EventRing（65,536 条），分配 Gateway 全局 seq（实例内从 1 起），再按各连接订阅的 `filter{types, levelMin}` 放入
  其待发列表，每 tick 1 条用 `event`、≥ 2 条合并为 `events`（≤ 256 项/条）。
api 自身产生的事件（`fleet.batch.progress`、`cmd.rejected`、`proc.state`、`sys.shutting_down`、`session.*`）经 `emit_api()`
直接并入同一 EventRing（producer = "api"）。无法补齐的缺口：订阅了事件的连接下一帧置 GAP，并发 `status{id: "events.gap"}`。
WS 事件字段：`seq`、`t_sim_ns`、`t_wall_ns`（十进制字符串）、`type`（= kind）、`level`（= severity）、`producer`、`uav`、`cid`、`data`。

EventRing 的存储（M11 §6.3.4，FX2-R3-gateway）：每条事件在并入时只编码一次为 WS 事件 JSON（UTF-8 字节，WS `event`/`events`
与 REST `/api/events` 直接拼接，不再逐连接重复编码），环内另存 (type, level) 索引供过滤；每满 256 条封块并以 zlib（level 1）压缩，
只有最新的未满块保持明文。此前环项是完整的 Python dict（约 1 KB/条，RSS 约 1–2 KB/条），65,536 条满环可达 65–130 MB，
在 soak（约 30 条/s）中表现为 api RSS 线性增长约 1.8 MB/min、30 min 不到平台（D1-AC-29）；压缩后实测约 30 B/条（9 倍），
满环约 2–3 MB。条数语义不变：环至少保留最近 65,536 条（按块淘汰，最多多保留一个块）；压缩字节另设安全上限 8 MiB
（事件异常大、压缩率很差时提前按块淘汰，保证内存有界）。
"""

from __future__ import annotations

import json
import logging
import math
import sys
import time
import zlib
from array import array
from collections import deque
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from awr.runtime.events import EventSubscriber

from .protocol import status_msg

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["EVENT_BLOCK", "EVENT_RING", "EVENT_RING_MAX_BYTES", "EventIngest", "EventRing", "encode_event"]

log = logging.getLogger("awr.api.events")

EVENT_RING = 65536
EVENT_BLOCK = 256  # 封块条数：每块 zlib 压缩一次（实测 soak 事件 275 B/条 JSON → 30 B/条，0.6 ms/块）
EVENT_RING_MAX_BYTES = 8 << 20  # 已封块的压缩字节上限（安全网；正常负载下 65,536 条约 2–3 MB）
EVENT_ZLEVEL = 1
# 首次见到生产者时，首条 seq ≤ 1024 即从 1 起补拉（生产者 _replay 环 4096 条，不会截断）：api 晚于 sim-core 启动时，
# sim-core 启动阶段的事件（scenario.loaded、mission.created 等）也进入 EventRing（M16-to-M11 第 1 条，INT-1）。
# 订阅之前发出、此后再无后续事件的辅助生产者（recorder 等）由 Gateway 在其就绪时 `probe()`，同一上限（FX-GW）
BACKFILL_FIRST_MAX = 1024
NOT_IN_RING = {"cmd.progress", "env.keyframe", "path.changed"}
EPOCH_CHECK_NS = 1_000_000_000


def _json_default(o: Any) -> Any:
    """事件 data 中 JSON 不能直接表示的值（msgpack bin、numpy 标量与数组、集合）：与 FastAPI jsonable_encoder 的处理一致。"""
    if isinstance(o, (bytes, bytearray, memoryview)):
        return bytes(o).decode("utf-8", "replace")
    if isinstance(o, (set, frozenset)):
        return list(o)
    for attr in ("tolist", "item"):
        f = getattr(o, attr, None)
        if callable(f):
            return f()
    return str(o)


def _finite(o: Any) -> Any:
    if not isinstance(o, (str, bytes, bytearray, dict, list, tuple)) and callable(getattr(o, "tolist", None)):
        o = o.tolist()  # numpy 标量与数组
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def encode_event(e: dict[str, Any]) -> bytes:
    """WS 事件对象 → 紧凑 JSON（UTF-8，`ensure_ascii=False`，与控制面 `jdump` 同一格式）。非有限浮点数写成 null
    （NaN/Infinity 不是合法 JSON，浏览器 `JSON.parse` 会整条失败）。"""
    try:
        s = json.dumps(e, separators=(",", ":"), ensure_ascii=False, default=_json_default, allow_nan=False)
    except ValueError:
        s = json.dumps(_finite(e), separators=(",", ":"), ensure_ascii=False, default=_json_default, allow_nan=False)
    return s.encode("utf-8")


class _Block:
    """已封块的一段连续事件：`z` = zlib(各条 JSON 首尾相接)，`offs` 为 n + 1 个偏移；`types` 为类型表下标，`levels` 为级别。"""

    __slots__ = ("first", "levels", "n", "offs", "types", "z")

    def __init__(self, first: int, z: bytes, offs: array, types: array, levels: bytes) -> None:
        self.first = first
        self.n = len(levels)
        self.z = z
        self.offs = offs
        self.types = types
        self.levels = levels


class EventRing:
    """全局 EventRing（M11-FR-065；M11 §6.3.4）：按 Gateway 全局 seq 连续存放事件的 WS JSON 字节与 (type, level) 索引。

    - `append(seq, type, level, item)`：seq 必须连续递增；最新的未满块为明文，满 `block` 条封块压缩；
    - 淘汰按块：去掉最旧一块后仍不少于 `maxlen` 条、或压缩字节超过 `max_bytes` 时淘汰，因此至少保留最近 `maxlen` 条
      （字节上限触发时除外）；
    - `iter_since(seq, types, level_min)`：按 seq 升序产出 seq 之后的 (seq, type, level, item)；先用索引过滤，块内没有
      命中项时不解压；`since(seq)` 返回不过滤的 item 列表（resume 补发）。
    """

    def __init__(self, maxlen: int = EVENT_RING, *, block: int | None = None, max_bytes: int = EVENT_RING_MAX_BYTES,
                 zlevel: int = EVENT_ZLEVEL) -> None:
        self.maxlen = max(1, int(maxlen))
        self.block = int(block) if block else max(1, min(EVENT_BLOCK, self.maxlen // 8))
        self.max_bytes = int(max_bytes)
        self.zlevel = zlevel
        self._type_ids: dict[str, int] = {}
        self._type_names: list[str] = []
        self._blocks: deque[_Block] = deque()
        self._open: list[bytes] = []
        self._open_types = array("I")
        self._open_levels = bytearray()
        self._open_first = 1
        self.first = 1  # 环内最早一条的 seq（空环时为下一条的 seq）
        self.last = 0  # 环内最新一条的 seq
        self.zbytes = 0
        self.stats = {"sealed": 0, "evicted": 0, "evicted_bytes_cap": 0, "decompressed": 0}

    # ------------------------------------------------------------ 写
    def _type_id(self, kind: str) -> int:
        i = self._type_ids.get(kind)
        if i is None:
            i = self._type_ids[sys.intern(kind)] = len(self._type_names)
            self._type_names.append(kind)
        return i

    def append(self, seq: int, kind: str, level: int, item: bytes) -> None:
        if seq != self.last + 1:  # 不连续（不应发生）：清空后从该 seq 重新开始，保证位置换算正确
            self.clear(seq)
        if not self._open:
            self._open_first = seq
        self._open.append(item)
        self._open_types.append(self._type_id(kind))
        self._open_levels.append(max(0, min(255, int(level))))
        self.last = seq
        if len(self._open) >= self.block:
            self._seal()

    def _seal(self) -> None:
        items = self._open
        offs = array("I", [0])
        acc = 0
        for b in items:
            acc += len(b)
            offs.append(acc)
        z = zlib.compress(b"".join(items), self.zlevel)
        self._blocks.append(_Block(self._open_first, z, offs, self._open_types, bytes(self._open_levels)))
        self.zbytes += len(z)
        self.stats["sealed"] += 1
        self._open = []
        self._open_types = array("I")
        self._open_levels = bytearray()
        self._evict()

    def _evict(self) -> None:
        while self._blocks:
            b = self._blocks[0]
            over_n = len(self) - b.n >= self.maxlen
            over_b = self.zbytes > self.max_bytes
            if not (over_n or over_b):
                break
            self._blocks.popleft()
            self.zbytes -= len(b.z)
            self.first = b.first + b.n
            self.stats["evicted"] += 1
            if over_b and not over_n:
                self.stats["evicted_bytes_cap"] += 1

    def clear(self, next_seq: int | None = None) -> None:
        nxt = self.last + 1 if next_seq is None else int(next_seq)
        self._blocks.clear()
        self._open = []
        self._open_types = array("I")
        self._open_levels = bytearray()
        self.zbytes = 0
        self.first = nxt
        self.last = nxt - 1

    def set_capacity(self, maxlen: int, *, block: int | None = None, max_bytes: int | None = None) -> None:
        """改变容量（测试与诊断）：按新参数重建，保留最近的事件。"""
        keep = list(self.iter_since(self.first - 1))
        self.maxlen = max(1, int(maxlen))
        self.block = int(block) if block else max(1, min(EVENT_BLOCK, self.maxlen // 8))
        if max_bytes is not None:
            self.max_bytes = int(max_bytes)
        self.clear(keep[0][0] if keep else self.last + 1)
        for seq, kind, level, item in keep:
            self.append(seq, kind, level, item)  # 每次封块即按新容量淘汰

    # ------------------------------------------------------------ 读
    def __len__(self) -> int:
        return max(0, self.last - self.first + 1)

    @property
    def nbytes(self) -> int:
        """近似占用：压缩块 + 明文块的字节数（诊断）。"""
        return self.zbytes + sum(len(b) for b in self._open)

    def _items_of(self, b: _Block) -> bytes:
        self.stats["decompressed"] += 1
        return zlib.decompress(b.z)

    def iter_since(self, seq: int, types: list[str] | None = None, level_min: int = 0) -> Iterator[tuple[int, str, int, bytes]]:
        start = max(int(seq) + 1, self.first)
        if start > self.last:
            return
        names = self._type_names
        want: set[int] | None = None
        if types:
            want = {i for n, i in self._type_ids.items() if any(n.startswith(t) for t in types)}
            if not want:
                return

        def ok(t: int, lv: int) -> bool:
            return lv >= level_min and (want is None or t in want)

        if self._blocks:
            b0 = self._blocks[0].first
            bi = max(0, (start - b0) // self.block) if start >= b0 else 0
            for k in range(bi, len(self._blocks)):
                b = self._blocks[k]
                lo = max(0, start - b.first)
                if lo >= b.n:
                    continue
                hits = [i for i in range(lo, b.n) if ok(b.types[i], b.levels[i])]
                if not hits:
                    continue
                raw = self._items_of(b)
                offs = b.offs
                for i in hits:
                    yield b.first + i, names[b.types[i]], b.levels[i], raw[offs[i]:offs[i + 1]]
        lo = max(0, start - self._open_first)
        for i in range(lo, len(self._open)):
            t, lv = self._open_types[i], self._open_levels[i]
            if ok(t, lv):
                yield self._open_first + i, names[t], lv, self._open[i]

    def since(self, seq: int, limit: int | None = None) -> list[bytes]:
        out: list[bytes] = []
        for _s, _t, _l, item in self.iter_since(seq):
            out.append(item)
            if limit is not None and len(out) >= limit:
                break
        return out


class EventIngest:
    def __init__(self, gw: Gateway, *, subscribe: bool = True) -> None:
        self.gw = gw
        self.ring = EventRing(EVENT_RING)
        self.gseq = 0
        self.sub = EventSubscriber(gw.bus, on_events=self._on_events, on_gap=self._on_gap, subscribe=subscribe,
                                   backfill_first_max=BACKFILL_FIRST_MAX)
        self.stats = {"delivered": 0, "gaps": 0, "api_events": 0}
        self._epoch_checks: list[tuple[int, int, int, str]] = []  # (segment, epoch, t_mono, reason)

    @property
    def oldest(self) -> int:
        return self.ring.first if len(self.ring) else self.gseq + 1

    @property
    def newest(self) -> int:
        return self.gseq

    def pump(self) -> None:
        self.sub.pump()
        if self._epoch_checks:
            self._check_epoch_consistency()

    def _on_events(self, producer: str, evs: list[dict]) -> None:
        gw = self.gw
        for ev in evs:
            kind = str(ev.get("kind", ""))
            if kind.startswith("cmd."):
                gw.rpc.on_cmd_event(ev)
            if ev.get("batch_id"):
                continue
            if kind == "env.keyframe":
                gw.env.on_keyframe(ev)
                continue
            if kind == "path.changed":  # M10 内部事件：api 据此推送 uav/{id}/path，不转发客户端
                gw.on_path_changed(ev.get("data") if isinstance(ev.get("data"), dict) else {})
                continue
            if kind in NOT_IN_RING:
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            if kind in ("sim.started", "sim.reset") and producer == gw.settings.producer:
                gw.on_producer_started(ev)
                if isinstance(data.get("segment"), int) and isinstance(data.get("epoch"), int):
                    self._epoch_checks.append((data["segment"], data["epoch"], time.monotonic_ns(),
                                               str(data.get("reason", ""))))
            elif kind == "proc.state":
                gw.on_proc_state(data)
            elif kind.startswith("seat.") and producer == gw.settings.producer:
                gw.on_seat_event(kind, data)
            self.append(kind, ev.get("t_sim_ns", 0), ev.get("t_wall_ns", 0), ev.get("severity", 0), producer,
                        ev.get("uav"), ev.get("cid"), data)

    def _check_epoch_consistency(self) -> None:
        """`sim.started{reason}` 只作一致性校验：1 s 内头部未出现对应 (segment, epoch) 时以头部为准并计 `epoch_mismatch`。"""
        p = self.gw.source.p
        now = time.monotonic_ns()
        keep = []
        for seg, ep, t, reason in self._epoch_checks:
            if p.segment == seg and p.epoch == ep:
                continue
            if p.segment is not None and seg < p.segment:
                continue  # 已被更新的 segment 取代
            if now - t < EPOCH_CHECK_NS:
                keep.append((seg, ep, t, reason))
                continue
            self.gw.metrics.counters["epoch_mismatch"] += 1
            log.warning("epoch classification mismatch; ring header wins",
                        extra={"kv": {"event_segment": seg, "event_epoch": ep, "header_segment": p.segment,
                                      "header_epoch": p.epoch, "reason": reason}})
        self._epoch_checks = keep

    def append(self, kind: str, t_sim_ns: int, t_wall_ns: int, level: int, producer: str, uav: str | None,
               cid: str | None, data: dict) -> int:
        """分配全局 seq、编码一次 WS 事件 JSON 并入环，按各连接的 filter 放入其待发列表（项为 JSON 字节）；返回 seq。"""
        self.gseq += 1
        lv = max(0, min(3, int(level or 0)))
        item = encode_event({"seq": self.gseq, "t_sim_ns": int(t_sim_ns or 0), "t_wall_ns": str(int(t_wall_ns or 0)),
                             "type": kind, "level": lv, "producer": producer, "uav": uav, "cid": cid, "data": data})
        self.ring.append(self.gseq, kind, lv, item)
        self.stats["delivered"] += 1
        for s in self.gw.sessions:
            if s.hello and s.events_on and not s.closing and s.event_ok(kind, lv):
                s.pending_events.append(item)
        return self.gseq

    def emit_api(self, kind: str, level: int, data: dict, *, uav: str | None = None, cid: str | None = None) -> int:
        self.stats["api_events"] += 1
        return self.append(kind, self.gw.clock.t_sim_ns, time.time_ns(), level, "api", uav, cid, data)

    def since(self, seq: int, limit: int | None = None) -> list[bytes]:
        """`seq` 之后的事件（WS JSON 字节，按 seq 升序，不过滤）。"""
        return self.ring.since(seq, limit)

    def _on_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        self.stats["gaps"] += 1
        for s in self.gw.sessions:
            if s.events_on:
                s.event_gap = True
                s.send_ctrl(status_msg("events.gap", "warning", f"事件缺口 {producer} [{lo}, {hi}]", source=producer,
                                       code=319))

    def probe(self, producer: str, epoch: int) -> bool:
        """主动补拉尚未见过的辅助生产者（Gateway 在其 `proc/<name>/ready` 出现时调用；FX-WEB2-to-M11 第 2 条）：
        订阅之前发出、此后再无后续的事件（demo 下 recorder 的 `rec.started`）由此进入 EventRing；只回补刚启动的生产者
        （≤ BACKFILL_FIRST_MAX 条，与首见补拉同一规则）。"""
        return self.sub.probe(producer, epoch)

    def close(self) -> None:
        self.sub.close()
