"""ClientSession：每连接的订阅、到期位、发送时装帧、credit 窗口、令牌桶、L4 拥塞与控制面 FIFO（M11 §6.3.3、§6.4.3–§6.4.7；
M11-FR-030 至 FR-044；AWR-17 §6.7、§6.8、§6.9）。

- 每连接一个 sender task：先排空控制面（JSON 与 TIME），再在"有到期位、未拥塞、未因令牌桶顺延、`frame_seq − acked < W`"时按
  当时各 channel 的最新值装帧（发送时装帧：单槽只保存待发意图，装帧后才推进 `last_seq`，尾帧保证不依赖数据源继续发布）；
- 对齐网格：`k % period_ticks == 0` 且 `channel.seq ≠ last_seq`（或 snapshot）置到期位；上一 tick 的待发意图尚未发出而本 tick
  又产生新到期时计一次 `slot_overwrites`（语义等同单槽覆盖，17 §6.8）；窗口满时计 `credit_skips`；
- 帧内记录按 (priority 升序, channel_id 升序)，`fleet/roster` 恒在最前；帧 ≤ 1 MiB，超预算记录保留到期位顺延
  （`bucket_defers`），每帧第一条记录不受预算限制；
- 订阅：rate 向上量化到 rate class（0 为原生但不超过 tick），swarm ≥ 10 Hz，`uav/*/…` 的 msgpack 通配 ≤ 2 Hz；同一 channel
  多订阅取最高 rate、最高优先级；订阅建立后的下一帧带 SNAPSHOT；同一订阅 id 再次 subscribe 只改 rate/priority；通配与尚不存在
  的精确 topic 在 channel 出现后以 `subscribed{added: true}` 补发；每连接订阅 ≤ 256、≥ 30 Hz 的 Full64 channel ≤ 64（315）；
- L4 拥塞：单次 send > 200 ms 或连续 3 次 > 50 ms 置 congested，暂停装帧（控制面照常）并发 `status net.congested`；此后发送
  连续 3 次 < 20 ms 时恢复并 `removeStatus`；
- 控制面有界 FIFO 1024：满时 `status{level: error, code: 318}` 直接写 socket（1 s 超时）后以 1013 关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from awr.contracts import frame as F
from awr.contracts import topics as T
from awr.runtime.bus import key_matches

from .channels import Channel, canonical_topic
from .protocol import error_msg, jdump, status_msg
from .scheduler import SRTT_DEFAULT_S, SubChan, TokenBucket, credit_window

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["CTRL_MAX", "MAX_HIGH_RATE_FULL", "MAX_SUBS", "ClientSession"]

CTRL_MAX = 1024
MAX_SUBS = 256
MAX_HIGH_RATE_FULL = 64
FRAME_MAX = 1 << 20
FRAME_HDR_B = 16
SWARM_MIN_HZ = 10
WILDCARD_MSGPACK_MAX_HZ = 2
EVENTS_PER_MSG = 256
TICK_HZ = T.TICK_HZ
SEND_SLOW_NS = 50_000_000
SEND_STUCK_NS = 200_000_000
SEND_FAST_NS = 20_000_000
SRTT_CLIENT_TTL_NS = 5_000_000_000


class ClientSession:
    def __init__(self, gw: Gateway, ws: Any, conn_id: str, principal: Any) -> None:
        self.gw = gw
        self.ws = ws
        self.conn_id = conn_id
        self.principal = principal
        self.role = principal.role
        self.ctrl: deque[str | bytes] = deque()
        self.wake = asyncio.Event()
        self.closing = False
        self.hello = False
        self.client = ""
        self.tier: str | None = None
        self.device_class: str | None = None
        self.max_kbps: float | None = None
        self.subs: dict[int, dict[str, Any]] = {}
        self.subchans: dict[int, SubChan] = {}
        self.order: list[SubChan] = []
        self.frame_seq = 0
        self.acked = 0
        self.window = credit_window(10, SRTT_DEFAULT_S)
        self.has_due = False
        self.wait_next_tick = False
        self.congested = False
        self._slow_sends = 0
        self._fast_sends = 0
        self.events_on = False
        self.events_filter: dict[str, Any] = {}
        self.pending_events: list[dict] = []
        self.event_gap = False
        self.srtt_client_ms: float | None = None
        self.srtt_client_mono = 0
        self.send_times: dict[int, tuple[int, int]] = {}  # frame_seq -> (单调发送时刻, 字节数)
        self.ack_delay: deque[int] = deque(maxlen=32)
        self.bucket = TokenBucket()
        self.acked_bytes = 0
        self._acked_mark = (0, time.monotonic())
        self.acked_bps_ewma: float | None = None
        self._bytes_mark = (0, time.monotonic())
        self.kbps = 0.0
        self.last_ping_mono = 0
        self.fmt_errors: deque[float] = deque(maxlen=3)
        self.cpub: dict[int, dict[str, Any]] = {}  # CLIENT_DATA channel id -> 状态（rpc.ClientPublish）
        self.client_stats: dict[str, Any] = {}
        self.last_frame: bytes = b""
        self.capture: Any = None  # rt.awrrt.Capture（AWR_RT_CAPTURE，dev/test）
        self.stats = {"frames": 0, "bytes": 0, "ctrl_msgs": 0, "ctrl_bytes": 0, "credit_skips": 0, "bucket_defers": 0,
                      "slot_overwrites": 0, "ctrl_hwm": 0, "fps": 0.0, "decode_ms": 0.0, "lag_ms": 0.0, "ticks_due": 0,
                      "congestions": 0, "cd_forwarded": 0, "cd_dropped": 0}
        self.t_open = time.monotonic()
        self.sender_task: asyncio.Task | None = None
        self._bg: asyncio.Future | None = None

    # ------------------------------------------------------------ 控制面
    def send_ctrl(self, m: dict | str | bytes) -> None:
        if self.closing:
            return
        if len(self.ctrl) >= CTRL_MAX:
            self.closing = True
            self.wake.set()
            self._bg = asyncio.ensure_future(self._close_backlog())
            return
        self.ctrl.append(m if isinstance(m, (str, bytes)) else jdump(m))
        if len(self.ctrl) > self.stats["ctrl_hwm"]:
            self.stats["ctrl_hwm"] = len(self.ctrl)
        self.wake.set()

    async def _close_backlog(self) -> None:
        """控制面积压（318）：status 直接写 socket（不经队列，1 s 超时），随后以 1013 关闭。"""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.send_text(jdump(status_msg("net.backlog", "error", "控制面积压，连接关闭",
                                                                     code=318))), 1.0)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.ws.close(code=1013), 1.0)

    def error(self, code: int, op: str, rid: Any = None, message: str | None = None) -> None:
        self.send_ctrl(error_msg(code, op, rid, message))

    async def sender(self) -> None:
        try:
            while not self.closing:
                while self.ctrl and not self.closing:
                    m = self.ctrl.popleft()
                    t0 = time.monotonic_ns()
                    if isinstance(m, bytes):
                        await self.ws.send_bytes(m)
                    else:
                        await self.ws.send_text(m)
                    self._on_ctrl_sent(len(m), time.monotonic_ns() - t0)
                    if self.capture is not None:
                        self.capture.s2c(m)
                if self.closing:
                    break
                if self.can_assemble():
                    frame = self.assemble()
                    if frame is not None:
                        t0 = time.monotonic_ns()
                        await self.ws.send_bytes(frame)
                        self._on_sent(len(frame), time.monotonic_ns() - t0)
                        if self.capture is not None:
                            self.capture.s2c(frame)
                    continue
                self.wake.clear()
                if self.ctrl or self.can_assemble():
                    continue
                await self.wake.wait()
        except (asyncio.CancelledError, Exception):
            self.closing = True

    def can_assemble(self) -> bool:
        return (self.has_due and not self.congested and not self.wait_next_tick
                and self.frame_seq - self.acked < self.window)

    # ------------------------------------------------------------ L4 拥塞（M11 §6.4.7）
    def _on_sent(self, n: int, send_ns: int) -> None:
        self.stats["frames"] += 1
        self.stats["bytes"] += n
        self.bucket.last_frame_b = n
        self._congestion_sample(send_ns)

    def _on_ctrl_sent(self, n: int, send_ns: int) -> None:
        self.stats["ctrl_msgs"] += 1
        self.stats["ctrl_bytes"] += n
        self._congestion_sample(send_ns)

    def _congestion_sample(self, send_ns: int) -> None:
        if not self.congested:
            if send_ns > SEND_STUCK_NS:
                self._set_congested(True)
                return
            self._slow_sends = self._slow_sends + 1 if send_ns > SEND_SLOW_NS else 0
            if self._slow_sends >= 3:
                self._set_congested(True)
            return
        self._fast_sends = self._fast_sends + 1 if send_ns < SEND_FAST_NS else 0
        if self._fast_sends >= 3:
            self._set_congested(False)

    def _set_congested(self, on: bool) -> None:
        self.congested = on
        self._slow_sends = self._fast_sends = 0
        if on:
            self.stats["congestions"] += 1
            self.send_ctrl(status_msg("net.congested", "warning", "网络拥塞，暂停数据面", source="api"))
        else:
            self.send_ctrl({"op": "removeStatus", "ids": ["net.congested"]})
            self.wake.set()

    # ------------------------------------------------------------ 数据面：到期位与装帧
    def mark_due(self, k: int) -> None:
        """对齐网格置到期位（M11 §6.4.3）；在 tick 中对每个已 hello 的连接调用。"""
        self.wait_next_tick = False
        pending_before = self.has_due
        fresh = False
        any_due = False
        for sc in self.order:
            ch = sc.channel
            if ch.seq == 0:
                continue  # 尚无值：snapshot 保留，首值到达后补发
            if sc.snapshot or (k % sc.period_ticks == 0 and ch.seq != sc.last_seq):
                sc.due = True
                fresh = True
            if sc.due:
                any_due = True
        if not any_due:
            self.has_due = False
            return
        self.has_due = True
        self.stats["ticks_due"] += 1
        if self.frame_seq - self.acked >= self.window:
            self.stats["credit_skips"] += 1
        elif pending_before and fresh:
            self.stats["slot_overwrites"] += 1  # 上一 tick 的待发意图尚未发出，被本 tick 合并
        self.wake.set()

    def assemble(self) -> bytes | None:
        """发送时装帧（M11 §6.4.4）：按当时各 channel 的最新值装帧、分配 frame_seq；装帧后才推进 last_seq。"""
        gw = self.gw
        frame_t = gw.frame_t_sim_ns
        budget = self.bucket.take_all()
        recs: list[bytes] = []
        flags = 0
        size = FRAME_HDR_B
        deferred = False
        for sc in self.order:
            if not sc.due:
                continue
            ch = sc.channel
            if ch.seq == 0 or (ch.seq == sc.last_seq and not sc.snapshot):
                sc.due = False
                continue
            reset = sc.seen_reset_gen != ch.reset_gen
            rec = ch.record(frame_t, reset)
            if recs and (len(rec) > budget or size + len(rec) > FRAME_MAX):
                self.stats["bucket_defers"] += 1
                deferred = True
                continue
            recs.append(rec)
            budget -= len(rec)
            size += len(rec)
            sc.last_seq, sc.due, sc.seen_reset_gen = ch.seq, False, ch.reset_gen
            if sc.snapshot:
                flags |= F.BATCH_SNAPSHOT
                sc.snapshot = False
        self.bucket.give_back(budget)
        self.has_due = any(sc.due for sc in self.order)
        self.wait_next_tick = deferred
        if not recs:
            return None
        if self.event_gap:
            flags |= F.BATCH_GAP
            self.event_gap = False
        if gw.mode == "replay":
            flags |= F.BATCH_REPLAY
        self.frame_seq += 1
        frame = F.encode_batch(flags, gw.clock.global_epoch, self.frame_seq, frame_t, recs)
        self.send_times[self.frame_seq] = (time.monotonic_ns(), len(frame))
        if len(self.send_times) > 64:
            for key in sorted(self.send_times)[:-64]:
                del self.send_times[key]
        self.last_frame = frame
        return frame

    def on_epoch_bump(self, time_bytes: bytes) -> None:
        """全局纪元变化：先排入 TIME，再把全部有值订阅置 snapshot 与到期位（TIME 先于 SNAPSHOT，17 §6.10）。"""
        for sc in self.order:
            sc.snapshot = True
            sc.due = sc.channel.seq != 0
        self.has_due = any(sc.due for sc in self.order)
        self.send_ctrl(time_bytes)

    # ------------------------------------------------------------ ack、ping、窗口、令牌桶
    def on_ack(self, frame: int, fps: Any = None, decode_ms: Any = None, lag_ms: Any = None) -> None:
        if isinstance(fps, (int, float)):
            self.stats["fps"] = float(fps)
        if isinstance(decode_ms, (int, float)):
            self.stats["decode_ms"] = float(decode_ms)
        if isinstance(lag_ms, (int, float)):
            self.stats["lag_ms"] = float(lag_ms)
        if frame <= self.acked or frame > self.frame_seq:
            return
        now = time.monotonic_ns()
        ent = self.send_times.get(frame)
        if ent is not None:
            self.ack_delay.append(now - ent[0])
        for fs in range(self.acked + 1, frame + 1):
            e = self.send_times.get(fs)
            if e is not None:
                self.acked_bytes += e[1]
        self.acked = frame
        if self.has_due:
            self.wake.set()

    def on_ping(self, t: Any, srtt_ms: Any) -> None:
        now = time.monotonic_ns()
        self.last_ping_mono = now
        self.gw.note_ping(self.principal.id, now)
        if isinstance(srtt_ms, (int, float)) and 0 <= srtt_ms < 10_000:
            self.srtt_client_ms, self.srtt_client_mono = float(srtt_ms), now
        c = self.gw.clock
        self.send_ctrl({"op": "pong", "t": t if isinstance(t, (int, float)) else 0, "server_ns": c.srv_now_ns(),
                        "sim_ns": c.sim_now_ns(now), "epoch": c.epoch_u16, "unix_ns": str(time.time_ns())})

    def srtt_s(self) -> float:
        now = time.monotonic_ns()
        if self.srtt_client_ms is not None and now - self.srtt_client_mono < SRTT_CLIENT_TTL_NS:
            return self.srtt_client_ms / 1e3
        if self.ack_delay:
            return min(self.ack_delay) / 1e9
        return SRTT_DEFAULT_S

    def recompute_window(self) -> None:
        max_rate = max((sc.rate for sc in self.order), default=10)
        self.window = credit_window(max_rate, self.srtt_s())

    def tick_1hz(self) -> None:
        """每 1 s（墙钟）：窗口重算、令牌桶按已确认字节速率 EWMA 调整、下行 kbps。"""
        self.recompute_window()
        now = time.monotonic()
        ab, at = self._acked_mark
        dt = max(1e-3, now - at)
        rate = (self.acked_bytes - ab) / dt
        self._acked_mark = (self.acked_bytes, now)
        self.acked_bps_ewma = rate if self.acked_bps_ewma is None else 0.5 * self.acked_bps_ewma + 0.5 * rate
        self.bucket.retune(self.acked_bps_ewma, self.max_kbps)
        total = self.stats["bytes"] + self.stats["ctrl_bytes"]
        b0, t0 = self._bytes_mark
        self.kbps = (total - b0) * 8 / 1000.0 / max(1e-3, now - t0)
        self._bytes_mark = (total, now)

    # ------------------------------------------------------------ 订阅
    def _resort(self) -> None:
        self.order = sorted(self.subchans.values(), key=lambda sc: sc.sort_key)

    def _rate_for(self, topic: str, rate: Any, chans: list[Channel]) -> int:
        r = T.quantize_rate(float(rate or 0))
        if topic.startswith("swarm/"):
            r = max(r, SWARM_MIN_HZ)
        if "*" in topic and topic.startswith("uav/") and (any(c.encoding == "msgpack" for c in chans)
                                                         or _wild_msgpack(topic)):
            r = min(r, WILDCARD_MSGPACK_MAX_HZ)
        return r

    def high_rate_full(self, exclude_sid: int | None = None) -> int:
        n = 0
        for sc in self.order:
            if sc.channel.is_full64:
                rates = [self.subs[r]["rate"] for r in sc.refs if r != exclude_sid and r in self.subs]
                if rates and max(rates) >= 30:
                    n += 1
        return n

    def subscribe(self, subs: list) -> None:
        gw = self.gw
        changed = False
        for s in subs:
            if not isinstance(s, dict):
                self.error(300, "subscribe", None, "subscribe 项必须是对象")
                continue
            sid, topic = s.get("id"), s.get("topic")
            if not isinstance(sid, int) or isinstance(sid, bool) or not isinstance(topic, str):
                self.error(300, "subscribe", sid if isinstance(sid, (int, str)) else None, "subscribe 项缺少 id 或 topic")
                continue
            retry = gw.limiter.check(self.principal.id, "sub")
            if retry:
                self.error(111, "subscribe", sid, f"订阅操作过于频繁，{retry} ms 后重试")
                continue
            topic = canonical_topic(topic)
            if not T.valid_topic(topic, allow_wildcards=True):
                self.error(314, "subscribe", sid, f"topic 语法非法：{topic}")
                continue
            if sid not in self.subs and len(self.subs) >= MAX_SUBS:
                self.error(315, "subscribe", sid, "订阅数超过上限 256")
                continue
            prio = s.get("priority")
            prio = int(prio) if isinstance(prio, int) and not isinstance(prio, bool) and 0 <= prio <= 3 else None
            if topic == "event":
                self._subscribe_events(sid, s)
                continue
            if "*" not in topic and T.match_topic(topic) is None:
                self.error(314, "subscribe", sid, f"topic 不在 topics.json：{topic}")
                continue
            spec = T.match_topic(topic)
            if topic == "perf/clients" and self.role != "admin":
                self.error(115, "subscribe", sid, "perf/clients 需要 admin")
                continue
            if spec is not None and spec.kind == "client":
                self.error(314, "subscribe", sid, "客户端发布 topic 不可订阅")
                continue
            chans = gw.registry.match(topic)
            rate = self._rate_for(topic, s.get("rate", 0), chans)
            update = sid in self.subs
            full_new = [c for c in chans if c.is_full64 and (c.id not in self.subchans
                                                            or self.subchans[c.id].rate < 30)]
            full_pat = not chans and spec is not None and spec.schema_name == "awr.DroneState64.v1"
            if rate >= 30 and (full_new or full_pat) and \
                    self.high_rate_full(exclude_sid=sid if update else None) + max(1, len(full_new)) > MAX_HIGH_RATE_FULL:
                self.error(315, "subscribe", sid, "≥ 30 Hz 的 Full64 channel 超过上限 64")
                continue
            mode = s.get("mode", "latest") if s.get("mode") in ("latest", "all", "m4") else "latest"
            if update and self.subs[sid]["topic"] == topic:
                # 同一订阅 id 再次 subscribe：只改 rate、priority（不重发 SNAPSHOT，17 §6.7 第 5 条）
                sub = self.subs[sid]
                sub.update(rate=rate, mode=mode, prio=prio)
                for cid in sub["channels"]:
                    sc = self.subchans.get(cid)
                    if sc is not None:
                        self._rerate(sc)
                self._resort()
                changed = True
                self.send_ctrl({"op": "subscribed", "id": sid, "topic": topic, "channels": list(sub["channels"]),
                                "rate": rate, "mode": mode})
                continue
            if update:
                self._unref(sid)
            self.subs[sid] = {"topic": topic, "rate": rate, "mode": mode, "prio": prio,
                              "channels": [c.id for c in chans]}
            for c in chans:
                self._ref(sid, c, fresh=not update)
            self._resort()
            changed = True
            self.send_ctrl({"op": "subscribed", "id": sid, "topic": topic, "channels": [c.id for c in chans],
                            "rate": rate, "mode": mode})
        if changed:
            gw.on_subs_changed(self)

    def _subscribe_events(self, sid: int, s: dict) -> None:
        f = s.get("filter") if isinstance(s.get("filter"), dict) else {}
        self.events_on = True
        self.events_filter = {"types": [t for t in (f.get("types") or []) if isinstance(t, str)],
                              "levelMin": int(f.get("levelMin", 0)) if isinstance(f.get("levelMin"), int) else 0}
        self.subs[sid] = {"topic": "event", "rate": 0, "mode": "all", "prio": 0, "channels": [self.gw.event_ch.id]}
        self.send_ctrl({"op": "subscribed", "id": sid, "topic": "event", "channels": [self.gw.event_ch.id], "rate": 0,
                        "mode": "all"})

    def _ref(self, sid: int, c: Channel, *, fresh: bool) -> None:
        sc = self.subchans.get(c.id)
        if sc is None:
            sc = self.subchans[c.id] = SubChan(c)
            c.subscribers += 1
            self.gw.on_subchan_added(self, sc)
        elif fresh:
            sc.snapshot = True
        sc.refs.add(sid)
        self._rerate(sc)

    def _rerate(self, sc: SubChan) -> None:
        subs = [self.subs[r] for r in sc.refs if r in self.subs]
        sc.set_rate((s["rate"] for s in subs), (s["prio"] for s in subs if s.get("prio") is not None))

    def _unref(self, sid: int) -> None:
        sub = self.subs.get(sid)
        if not sub:
            return
        for cid in sub["channels"]:
            sc = self.subchans.get(cid)
            if sc is None:
                continue
            sc.refs.discard(sid)
            if not sc.refs:
                del self.subchans[cid]
                sc.channel.subscribers -= 1
            else:
                self._rerate(sc)
        self._resort()

    def unsubscribe(self, ids: list) -> None:
        changed = False
        for sid in ids if isinstance(ids, list) else []:
            if self.gw.limiter.check(self.principal.id, "sub"):
                self.error(111, "unsubscribe", sid if isinstance(sid, (int, str)) else None, "订阅操作过于频繁")
                continue
            sub = self.subs.get(sid)
            if sub is None:
                continue
            if sub["topic"] == "event":
                self.events_on = False
                self.pending_events.clear()
            self._unref(sid)
            self.subs.pop(sid, None)
            changed = True
        if changed:
            self.gw.on_subs_changed(self)

    def on_new_channels(self, chans: list[Channel]) -> None:
        """新 channel 出现：匹配现有订阅（通配或尚不存在的精确 topic）即加入并补发 `subscribed{added: true}`。"""
        changed = False
        for sid, sub in list(self.subs.items()):
            if sub["topic"] == "event":
                continue
            added = [c for c in chans if c.id not in sub["channels"] and c.kind != "event"
                     and (c.topic == sub["topic"] or ("*" in sub["topic"] and key_matches(sub["topic"], c.topic)))]
            if not added:
                continue
            if sub["rate"] >= 30 and any(c.is_full64 for c in added):
                room = MAX_HIGH_RATE_FULL - self.high_rate_full()
                full = [c for c in added if c.is_full64]
                if len(full) > room:
                    drop = {c.id for c in full[max(0, room):]}
                    added = [c for c in added if c.id not in drop]
                    self.error(315, "subscribe", sid, "≥ 30 Hz 的 Full64 channel 超过上限 64，新增 channel 未加入")
                if not added:
                    continue
            for c in added:
                sub["channels"].append(c.id)
                self._ref(sid, c, fresh=True)
            changed = True
            self.send_ctrl({"op": "subscribed", "id": sid, "topic": sub["topic"], "channels": [c.id for c in added],
                            "rate": sub["rate"], "mode": sub["mode"], "added": True})
        self._resort()
        if changed:
            self.gw.on_subs_changed(self)

    def on_removed_channels(self, ids: list[int]) -> None:
        gone = set(ids)
        for sub in self.subs.values():
            sub["channels"] = [c for c in sub["channels"] if c not in gone]
        for cid in gone:
            sc = self.subchans.pop(cid, None)
            if sc is not None:
                sc.channel.subscribers -= 1
        self._resort()

    def release(self) -> None:
        for sid in list(self.subs):
            self._unref(sid)
        self.subs.clear()
        self.subchans.clear()
        self.order = []

    # ------------------------------------------------------------ 事件
    def event_ok(self, ev: dict) -> bool:
        f = self.events_filter
        if not f:
            return True
        types = f.get("types")
        if types and not any(ev["type"].startswith(t) for t in types):
            return False
        return ev["level"] >= int(f.get("levelMin", 0))

    def flush_events(self) -> None:
        """同一 tick 的事件：1 条用 `event`，≥ 2 条合并为 `events`（每条 ≤ 256 项，17 §6.12）。"""
        if not self.pending_events:
            return
        evs, self.pending_events = self.pending_events, []
        for i in range(0, len(evs), EVENTS_PER_MSG):
            chunk = evs[i:i + EVENTS_PER_MSG]
            if len(chunk) == 1:
                self.send_ctrl({"op": "event", **chunk[0]})
            else:
                self.send_ctrl({"op": "events", "items": chunk})

    # ------------------------------------------------------------ 诊断
    def perf_row(self) -> dict[str, Any]:
        fps = self.stats["fps"] or self.client_stats.get("fps") or 0.0  # ack.fps 优先，其次 clientStats.fps
        return {"conn_id": self.conn_id, "fps": float(fps) if isinstance(fps, (int, float)) else 0.0,
                "window": int(self.window),
                "credit_skips": int(self.stats["credit_skips"]), "bucket_defers": int(self.stats["bucket_defers"]),
                "slot_overwrites": int(self.stats["slot_overwrites"]), "ctrl_queue_hwm": int(self.stats["ctrl_hwm"]),
                "srtt_ms": round(self.srtt_s() * 1e3, 3), "kbps": round(self.kbps, 1)}

    def inspect_row(self) -> dict[str, Any]:
        return {"conn_id": self.conn_id, "principal_id": self.principal.id, "role": self.role, "client": self.client,
                "tier": self.tier, "subs": [{"id": sid, "topic": s["topic"], "rate": s["rate"], "mode": s["mode"]}
                                            for sid, s in sorted(self.subs.items())],
                "window": self.window, "acked": self.acked, "frame_seq": self.frame_seq,
                "credit_skips": self.stats["credit_skips"], "bucket_defers": self.stats["bucket_defers"],
                "slot_overwrites": self.stats["slot_overwrites"], "srtt_ms": round(self.srtt_s() * 1e3, 3),
                "kbps": round(self.kbps, 1), "ctrl_queue_len": len(self.ctrl), "congested": self.congested,
                "bucket_rate_bps": round(self.bucket.rate), "client_stats": dict(self.client_stats)}


def _wild_msgpack(pattern: str) -> bool:
    """通配 topic 可能匹配的 topics.json 条目中是否有 msgpack 编码（channel 尚未出现时按模式判断）。"""
    probe = pattern.replace("**", "x").replace("*", "x")
    spec = T.match_topic(probe)
    return spec is not None and spec.encoding == "msgpack"
