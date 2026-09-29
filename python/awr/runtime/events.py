"""事件平面：EventPublisher（按步合批、seq、`_replay`）与 EventSubscriber（缺口补拉、重排、去重）。

依据：AWR-17 §9.5、§6.12；ADR-018；g05 §3.4；M11-FR-008、FR-009、§6.4.12。

- 事件结构（`packages/contracts/bus/event.schema.json`）：`seq`、`epoch`、`producer`、`kind`、`severity`、`t_sim_ns`、
  `t_wall_ns`、`uav`、`cid`、`data`；批量子调用另带 `batch_id`（待登记，见实现报告）。
- 发布：`emit()` 在 (producer, epoch) 内分配单调 seq、追加到 4096 条环与本轮批；`flush()` 每轮迭代每个 category 至多
  一次 put（msgpack 数组，DROP），并在宿主线程内应答排队的 `evt/<producer>/_replay` 请求（回调线程只入队）。
- 订阅：按 (producer, epoch) 跟踪已交付的最大连续 seq；发现缺口立即补拉 `_replay{since, epoch}`；补齐前后续事件进入
  重排缓冲（≤ 4096 条），补齐后按 seq 交付，因此每个生产者的交付顺序与发布顺序一致；1 s（墙钟）未补齐或 `truncated`
  时报告缺口 [lo, hi] 并放行缓冲；epoch 变化丢弃旧 epoch 的跟踪；同一 (producer, epoch, seq) 只交付一次。
"""

from __future__ import annotations

import logging
import queue
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from awr.contracts import bus_keys

from .bus import Bus, Handle, Request, pack, unpack

__all__ = ["DEFAULT_RING", "EventPublisher", "EventSubscriber", "category_of"]

log = logging.getLogger("awr.runtime.events")

DEFAULT_RING = 4096
GAP_TIMEOUT_S = 1.0
MAX_HELD = 4096
REPLAY_DELAY_S = 0.02  # 缺口出现后等待交错到达的同步消息，再补拉（远小于 1 s 的补齐时限）
REPLAY_KEY_SUFFIX = bus_keys.evt_replay("x").rsplit("/", 1)[1]  # "_replay"

# kind 前缀 -> 事件 category（key `evt/<producer>/<category>`，17 §9.3）；前缀本身即 category 时直接使用
_CATEGORY_ALIASES = {
    "anet": "agent",            # anet.evidence.gap 与其他 agent 事件同在 evt/agent-runtime/agent（M14-to-M11 第 2 条，INT-1）
    "roster": "sim",
    "uav": "sim",
    "seat": "lease",
    "recorder": "rec",
    "fleet": "cmd",
    "session": "sim",
    "scenario": "mission",
    "formation": "mission",
    "sys": "proc",
}


def category_of(kind: str) -> str:
    head = kind.split(".", 1)[0]
    if head in bus_keys.EVENT_CATEGORIES:
        return head
    return _CATEGORY_ALIASES.get(head, "sim")


# ---------------------------------------------------------------- 发布端
class EventPublisher:
    """生产者侧事件发布器；emit/flush/set_epoch 只能在宿主主循环线程调用。"""

    def __init__(self, bus: Bus, producer: str, epoch: int, ring: int = DEFAULT_RING, *, serve_replay: bool = True) -> None:
        self.bus = bus
        self.producer = producer
        self.epoch = int(epoch)
        self.ring_size = int(ring)
        self._seq = 0
        self._ring: deque[dict] = deque(maxlen=self.ring_size)
        self._batches: dict[str, list[dict]] = {}
        self._pubs: dict[str, Any] = {}
        self._replay_q: queue.SimpleQueue[Request] = queue.SimpleQueue()
        self.stats = {"emitted": 0, "puts": 0, "replays": 0, "replay_truncated": 0}
        self._replay_handle: Handle | None = None
        if serve_replay:
            self._replay_handle = bus.serve(bus_keys.evt_replay(producer), self._replay_q.put)
        # 全部类别的发布者在构造时声明（INT-1）：zenoh 新声明的发布者与既有订阅者完成匹配之前的首个 put 可能丢失，
        # 而消费者首次见到某生产者时不回补之前的序号（EventSubscriber._tracker），于是每个类别的首条事件
        # （rec.started、scenario.loaded 等）偶发不可见。提前声明让匹配在首个事件之前完成。
        for cat in bus_keys.EVENT_CATEGORIES:
            try:
                self._pubs[cat] = bus.publisher(bus_keys.evt(producer, cat))
            except Exception:
                break

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, kind: str, *, t_sim_ns: int, severity: int = 0, uav: str | None = None, cid: str | None = None,
             batch_id: str | None = None, fields: dict[str, Any] | None = None, **data: Any) -> int:
        """追加事件并返回 seq；实际发送在 flush()。

        data 字段以关键字参数给出；与参数名冲突的字段（例如 `sim.contact.collision` 的 `kind`，M08 §7.4）经 `fields` 字典给出，
        两者合并（SK-B 追加的可选参数，向后兼容）。
        """
        if fields:
            data = {**fields, **data}
        self._seq += 1
        ev = {"seq": self._seq, "epoch": self.epoch, "producer": self.producer, "kind": kind, "severity": int(severity),
              "t_sim_ns": int(t_sim_ns), "t_wall_ns": time.time_ns(), "uav": uav, "cid": cid, "data": data}
        if batch_id is not None:
            ev["batch_id"] = batch_id
        self._ring.append(ev)
        self._batches.setdefault(category_of(kind), []).append(ev)
        self.stats["emitted"] += 1
        return self._seq

    def flush(self) -> int:
        """每个 category 至多一次 put（msgpack 数组）；随后应答排队的 _replay 请求。返回 put 次数。"""
        puts = 0
        if self._batches:
            batches, self._batches = self._batches, {}
            for cat, evs in batches.items():
                pub = self._pubs.get(cat)
                if pub is None:
                    pub = self._pubs[cat] = self.bus.publisher(bus_keys.evt(self.producer, cat))
                pub.put(pack(evs))
                puts += 1
            self.stats["puts"] += puts
        self.serve_replays()
        return puts

    def serve_replays(self, limit: int = 64) -> int:
        n = 0
        while n < limit:
            try:
                req = self._replay_q.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                m = req.msg()
                req.reply_msg(self.replay(int(m.get("since", 0)), int(m.get("epoch", self.epoch))))
            except Exception:
                log.exception("event replay failed", extra={"kv": {"producer": self.producer}})
                req.close()
        return n

    def replay(self, since: int, epoch: int) -> dict:
        """环中 seq > since 的事件；超出环范围（或 epoch 不同）时 truncated = True。"""
        self.stats["replays"] += 1
        if epoch != self.epoch:
            self.stats["replay_truncated"] += 1
            return {"v": 1, "events": [], "truncated": True}
        oldest = self._ring[0]["seq"] if self._ring else self._seq + 1
        truncated = since + 1 < oldest and since < self._seq
        evs = [e for e in self._ring if e["seq"] > since] if since < self._seq else []
        if truncated:
            self.stats["replay_truncated"] += 1
        return {"v": 1, "events": evs, "truncated": truncated}

    def set_epoch(self, epoch: int) -> None:
        """生产者纪元变化：seq 从 1 重新计数，旧纪元的环清空（其缺口只能报告，不能补拉）。"""
        if int(epoch) == self.epoch:
            return
        self.flush()
        self.epoch = int(epoch)
        self._seq = 0
        self._ring.clear()

    def close(self) -> None:
        self.flush()
        if self._replay_handle is not None:
            self._replay_handle.close()
        while True:
            try:
                self._replay_q.get_nowait().close()
            except queue.Empty:
                break


# ---------------------------------------------------------------- 订阅端
@dataclass
class _Tracker:
    epoch: int
    delivered: int
    held: dict[int, dict] = field(default_factory=dict)
    gap_since: int | None = None  # 单调时钟 ns
    replay_inflight: bool = False
    replay_tried: bool = False  # 本次缺口已发起过补拉（进展后可再次发起）


class EventSubscriber:
    """消费者侧（Gateway、recorder、agent-runtime）。回调线程只入队，`pump()`/`feed()` 由宿主主循环调用。

    on_events(producer, events)：按生产者序交付的事件（每次 pump 每个生产者至多一次调用）；
    on_gap(producer, epoch, lo, hi)：无法补齐的缺口（1 s 超时、`truncated` 或缓冲满）。
    """

    def __init__(self, bus: Bus, *, on_events: Callable[[str, list[dict]], None],
                 on_gap: Callable[[str, int, int, int], None], pattern: str = "evt/**", subscribe: bool = True,
                 gap_timeout_s: float = GAP_TIMEOUT_S, max_held: int = MAX_HELD, replay_delay_s: float = REPLAY_DELAY_S,
                 backfill_first_max: int = 0) -> None:
        self.bus = bus
        # 实例内首次见到某生产者、且其首条 seq - 1 ≤ backfill_first_max 时，从 seq 1 起补拉（生产者刚启动，订阅建立之前
        # 发出的事件仍在其 _replay 环内）。缺省 0 不回补；Gateway 打开，使 sim-core 启动时的 scenario.loaded 等事件
        # 在 api 连接之后仍可见（M16-to-M11 第 1 条，INT-1）。
        self.backfill_first_max = int(backfill_first_max)
        self.on_events = on_events
        self.on_gap = on_gap
        self.gap_timeout_s = gap_timeout_s
        self.max_held = max_held
        self.replay_delay_s = replay_delay_s
        self._inbox: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self._trackers: dict[str, _Tracker] = {}
        self._out: dict[str, list[dict]] = {}
        self.stats = {"received": 0, "delivered": 0, "duplicates": 0, "gaps_detected": 0, "gaps_filled": 0,
                      "gaps_reported": 0, "events_lost": 0, "replays": 0, "epoch_resets": 0, "malformed": 0}
        self.filter: Callable[[str, bytes], bool] | None = None  # 测试注入：返回 False 的消息被丢弃（模拟丢包）
        self._handle: Handle | None = bus.subscribe(pattern, self._on_sample) if subscribe else None

    # ------------------------------------------------------------ 回调线程
    def _on_sample(self, key: str, raw: bytes) -> None:
        self._inbox.put(("evt", key, raw))

    # ------------------------------------------------------------ 宿主线程
    def pump(self, max_items: int = 100_000, now_mono_ns: int | None = None) -> int:
        """处理入队的事件消息与补拉回复，随后检查缺口超时；返回处理的消息数。"""
        n = 0
        while n < max_items:
            try:
                item = self._inbox.get_nowait()
            except queue.Empty:
                break
            n += 1
            if item[0] == "evt":
                _, key, raw = item
                parts = key.split("/")
                if len(parts) != 3 or parts[2] == REPLAY_KEY_SUFFIX:
                    continue
                if self.filter is not None and not self.filter(key, raw):
                    continue
                self.feed(parts[1], raw, _flush=False)
            else:
                _, producer, epoch, rep, err = item
                self.on_replay_reply(producer, epoch, rep, err, _flush=False)
        self.check(time.monotonic_ns() if now_mono_ns is None else now_mono_ns, _flush=False)
        self._flush_out()
        return n

    def feed(self, producer: str, raw: bytes, *, _flush: bool = True) -> None:
        try:
            evs = unpack(raw)
        except Exception:
            self.stats["malformed"] += 1
            return
        if isinstance(evs, dict):
            evs = [evs]
        for ev in evs:
            if not isinstance(ev, dict) or "seq" not in ev or "epoch" not in ev:
                self.stats["malformed"] += 1
                continue
            self.stats["received"] += 1
            self._accept(producer, ev)
        if _flush:
            self._flush_out()

    def _tracker(self, producer: str, epoch: int, first_seq: int) -> _Tracker:
        tr = self._trackers.get(producer)
        if tr is not None and tr.epoch == epoch:
            return tr
        if tr is None:
            # 实例内首次见到该生产者：不回补订阅开始之前的事件（客户端经 REST 或自包含 channel 恢复），
            # 除非生产者刚启动（见 backfill_first_max）：此时从 0 起跟踪，缺口经 _replay 补齐
            backfill = 1 < first_seq <= self.backfill_first_max + 1
            tr = _Tracker(epoch, 0 if backfill else first_seq - 1)
        else:
            # 新纪元：丢弃旧纪元的跟踪与缓冲，从 0 起跟踪（缺首条即补拉）
            self.stats["epoch_resets"] += 1
            tr = _Tracker(epoch, 0)
        self._trackers[producer] = tr
        return tr

    def _accept(self, producer: str, ev: dict) -> None:
        s, epoch = int(ev["seq"]), int(ev["epoch"])
        cur = self._trackers.get(producer)
        if cur is not None and cur.epoch != epoch and self._is_stale_epoch(cur, epoch):
            self.stats["duplicates"] += 1
            return
        tr = self._tracker(producer, epoch, s)
        if s <= tr.delivered or s in tr.held:
            self.stats["duplicates"] += 1
            return
        if s == tr.delivered + 1:
            self._deliver(producer, ev)
            tr.delivered = s
            while (nxt := tr.held.pop(tr.delivered + 1, None)) is not None:
                self._deliver(producer, nxt)
                tr.delivered += 1
            if tr.gap_since is not None:
                self.stats["gaps_filled"] += 1
                # 最早的缺口已补齐；缓冲中仍有后续缺口时视为新缺口并重新计时
                tr.gap_since = time.monotonic_ns() if tr.held else None
                tr.replay_tried = False
                if tr.held:
                    self.stats["gaps_detected"] += 1
            return
        tr.held[s] = ev
        if tr.gap_since is None:
            tr.gap_since = time.monotonic_ns()
            self.stats["gaps_detected"] += 1
        # 不立即补拉：同一步按 category 分开发布的消息可能交错到达，缺口先等 replay_delay_s（由 check() 触发）

    def _is_stale_epoch(self, cur: _Tracker, epoch: int) -> bool:
        """补拉回复或迟到消息属于已被替代的旧纪元（u32 纪元只增不减；回绕按差值判断）。"""
        return ((cur.epoch - epoch) & 0xFFFFFFFF) < 0x80000000

    def _request_replay(self, producer: str, tr: _Tracker) -> None:
        if tr.replay_inflight:
            return
        tr.replay_inflight = True
        self.stats["replays"] += 1
        epoch = tr.epoch
        inbox = self._inbox

        def on_reply(rep: Any, err: BaseException | None) -> None:  # 回调线程：只入队
            inbox.put(("replay", producer, epoch, rep, err))

        self.bus.call_cb(bus_keys.evt_replay(producer), {"v": 1, "since": tr.delivered, "epoch": epoch}, on_reply,
                         timeout=0.5, retries=1, retry_gap=0.1)

    def on_replay_reply(self, producer: str, epoch: int, reply: dict | None, err: BaseException | None, *,
                        _flush: bool = True) -> None:
        tr = self._trackers.get(producer)
        if tr is None or tr.epoch != epoch:
            return
        tr.replay_inflight = False
        before = tr.delivered
        for ev in (reply or {}).get("events", []) or []:
            if isinstance(ev, dict) and "seq" in ev and int(ev.get("epoch", epoch)) == epoch:
                self._accept(producer, ev)
        if tr.held:
            if (reply or {}).get("truncated"):
                self._give_up(producer, tr)  # 超出生产者环：已无可补
            elif err is None and tr.delivered > before:
                self._request_replay(producer, tr)  # 有进展但仍有缺口（补拉期间又出现新缺口）
            # 无回复或无进展：等待 check() 的 1 s 超时后报告缺口并放行缓冲（M11-AC-004），避免补拉风暴
        if _flush:
            self._flush_out()

    def check(self, now_mono_ns: int, *, gap_timeout_s: float | None = None, max_held: int | None = None,
              _flush: bool = True) -> None:
        """缺口超过 gap_timeout_s（默认 1 s）或缓冲达到 max_held（默认 4096）时放弃并报告。"""
        to_ns = int((self.gap_timeout_s if gap_timeout_s is None else gap_timeout_s) * 1e9)
        mh = self.max_held if max_held is None else max_held
        delay_ns = int(self.replay_delay_s * 1e9)
        for producer, tr in list(self._trackers.items()):
            if tr.gap_since is None:
                continue
            if now_mono_ns - tr.gap_since > to_ns or len(tr.held) >= mh:
                self._give_up(producer, tr)
            elif not tr.replay_inflight and not tr.replay_tried and now_mono_ns - tr.gap_since >= delay_ns:
                tr.replay_tried = True
                self._request_replay(producer, tr)
        if _flush:
            self._flush_out()

    def _give_up(self, producer: str, tr: _Tracker) -> None:
        if not tr.held:
            tr.gap_since = None
            return
        expect = tr.delivered + 1
        for s in sorted(tr.held):
            if s > expect:
                self.stats["gaps_reported"] += 1
                self.stats["events_lost"] += s - expect
                log.warning("event gap reported", extra={"kv": {"producer": producer, "epoch": tr.epoch, "lo": expect,
                                                                "hi": s - 1}})
                self.on_gap(producer, tr.epoch, expect, s - 1)
            self._deliver(producer, tr.held[s])
            expect = s + 1
        tr.delivered = expect - 1
        tr.held.clear()
        tr.gap_since = None
        tr.replay_tried = False

    def _deliver(self, producer: str, ev: dict) -> None:
        self._out.setdefault(producer, []).append(ev)
        self.stats["delivered"] += 1

    def _flush_out(self) -> None:
        if not self._out:
            return
        out, self._out = self._out, {}
        for producer, evs in out.items():
            self.on_events(producer, evs)

    def tracked(self) -> dict[str, tuple[int, int, int]]:
        """{producer: (epoch, delivered, held)}，供诊断。"""
        return {p: (t.epoch, t.delivered, len(t.held)) for p, t in self._trackers.items()}

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
