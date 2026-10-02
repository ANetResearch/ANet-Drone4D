"""recorder 进程：状态机、主循环、总线收件箱、分段与谱系（M12 §6.6；FR-030 至 FR-039；12 §4.10 R01–R07）。

`RecorderCore` 不依赖进程与 zenoh（测试以 LocalRing + LocalBus 驱动 `step()`）；`main()` 负责 init_child、ZenohBus、
StateRing 附着、心跳与 20 ms 墙钟循环。

主循环每轮：心跳 → 总线收件箱（事件、`state/*`、roster 回复、兴趣集、控制）→ 环头部一致性读（segment、rate）→ LOSSY
`drain()`（overrun → `recorder.gap`）→ 逐帧：生产者纪元变化写谱系、桶规则选块与 Full64、低频关键块/增量到期 → meta
（10 s）与磁盘守卫（10 s）→ 1 s 性能摘要。

状态机：OFF --R01 剧本 `record` 或 rec/start--> RECORDING --R02 segment 变化（换段）/ R03 纪元变化（谱系）/ R04 缺口-->
RECORDING --R05 rec/stop、Session CLOSING、SIGTERM / R06 可用空间 < 5 GB--> CLOSING --队列排空并 finish--> OFF；
R07：磁盘不足时 rec/start 返回 460。自动开录只对剧本 `record = true`（缺省 true）且剧本已加载的会话；ladder 剧本必须为
false，`fleet_ladder --with-recorder` 经 rec/start 显式开录（M12 §6.6.1 R01，F-22）。
"""

from __future__ import annotations

import logging
import os
import queue
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msgpack

from awr.contracts import CONTRACTS_VERSION, LAYOUT_ID, bus_keys
from awr.contracts.reasons import Reason
from awr.runtime.bus import Bus, BusError
from awr.runtime.events import EventPublisher, EventSubscriber
from awr.runtime.quota import disk_status
from awr.runtime.statering import LOSSY, StateRing

from .config import RecorderCfg
from .formats import (
    RFLAG_DELTA,
    RFLAG_KEYFRAME,
    T_AGENT_TASKS,
    T_BLOCK,
    T_CLOCK,
    T_ENV,
    T_EVENT,
    T_EXT,
    T_ROSTER,
    T_SAFETY,
    agent_topic,
    full_topic,
    mission_topic,
    pose_topic,
    prefix,
)
from .keydelta import KeyDeltaBlocker, parse_batch
from .meta import MetaFile, seg_file
from .policy import MarkedSet, RecordingPolicy
from .selector import FrameSelector, MarkedRows
from .writer import Kind, McapSegmentWriter, SegmentSpec, SegmentStats

__all__ = ["RecorderCore", "main"]

log = logging.getLogger("awr.recorder")

OFF, RECORDING, CLOSING = "OFF", "RECORDING", "CLOSING"
PRODUCER = "recorder"
SIM = "sim-core"


def _pack(x: Any) -> bytes:
    return msgpack.packb(x, use_bin_type=True)


def _unpack(raw: bytes) -> Any:
    return msgpack.unpackb(raw, raw=False, strict_map_key=False)


def _state_key(const: str, producer: str) -> str:
    """把 `state/sim-core/<x>` 常量换成另一生产者（mission、env 在 bus_keys 中没有按生产者的构造函数）。"""
    parts = const.split("/")
    parts[1] = producer
    return "/".join(parts)


class RecorderCore:
    def __init__(self, *, bus: Bus, ring_path: Path, persist_dir: Path, run_id: str, world_id: str, cfg: RecorderCfg,
                 scenario: dict[str, Any] | None = None, ring_cls: type[StateRing] = StateRing,
                 mono_ns: Callable[[], int] = time.monotonic_ns, autostart: bool | None = None,
                 event_epoch: int = 1) -> None:
        self.bus = bus
        self.ring_path = Path(ring_path)
        self.ring_cls = ring_cls
        self.persist_dir = Path(persist_dir)
        self.run_id = run_id
        self.world_id = world_id
        self.cfg = cfg
        self.mono_ns = mono_ns
        self.policy = RecordingPolicy.from_cfg(cfg.policy)
        self.state = OFF
        self.ring: StateRing | None = None
        self._ident_n = 0
        self.inbox: queue.SimpleQueue[tuple] = queue.SimpleQueue()
        self.meta = MetaFile(self.persist_dir, run_id)
        self.writer = McapSegmentWriter(queue_bytes=cfg.queue_mb << 20, flush_wall_s=cfg.flush_wall_s, on_closed=self._on_closed)
        self.selector = FrameSelector(self.policy)
        self.rows = MarkedRows()
        self.marks = MarkedSet(self.policy.max_marked, self.policy.full_all_if_n_le)
        self.ext = KeyDeltaBlocker(self.policy.state_ext_hz, self.policy.keyframe_every_ns)
        self.saf = KeyDeltaBlocker(self.policy.safety_hz, self.policy.keyframe_every_ns)
        # 事件纪元 = 重启次数 + 1（与 agent-runtime、job-worker 一致；17 §9.5）：此前恒为 1，recorder 重启后 seq 从 1 重新计数而
        # 纪元不变，消费者（api、agent-runtime）按 (producer, epoch, seq) 去重会把新事件当作重复丢弃，直到 seq 超过旧值（FX-GW）
        self.events = EventPublisher(bus, PRODUCER, int(event_epoch))
        self.subscriber = EventSubscriber(bus, on_events=self._on_bus_events, on_gap=self._on_event_gap)
        self.seg_k = -1
        self.sim_segment = -1
        self.prod_epoch = -1
        self.last_frame_t = -1
        self.last_frame_epoch = -1
        self.hdr_rate = 1.0
        self.roster: dict[str, Any] | None = None
        self.roster_version = -1
        self.agent_id: dict[int, str] = {}
        self.marked: frozenset[int] = frozenset()
        self.last_clock: dict[str, Any] | None = None
        self.last_env: bytes | None = None
        self.last_env_t = 0
        self.mission_last: dict[str, bytes] = {}
        self.agent_last: dict[str, bytes] = {}
        self.pose_next_t = 0
        self.restored_hint: int | None = None
        self.pending_lineage: dict[str, int] | None = None
        self.interest_at = 0
        self.interest_marks: list[int] | None = None
        self.stats = {"frames": 0, "overrun": 0, "gaps": 0, "blocks": 0, "full": 0, "events": 0, "segments": 0,
                      "reordered": 0, "rotations": 0}
        self._next_meta = 0
        self._next_disk = 0
        self._next_perf = 0
        self._roster_query_inflight = False
        self.scenario = scenario
        rec = bool((scenario or {}).get("record", True))
        self.autostart = autostart if autostart is not None else (scenario is not None and rec)
        self._scenario_marked(scenario)
        self.handles = [
            bus.serve(bus_keys.ctl_recorder("start"), lambda r: self.inbox.put(("ctl", "start", r))),
            bus.serve(bus_keys.ctl_recorder("stop"), lambda r: self.inbox.put(("ctl", "stop", r))),
            bus.serve(bus_keys.ctl_recorder("status"), lambda r: self.inbox.put(("ctl", "status", r))),
            bus.subscribe(bus_keys.state_ext(SIM), lambda k, raw: self.inbox.put(("ext", raw))),
            bus.subscribe(bus_keys.state_safety(SIM), lambda k, raw: self.inbox.put(("safety", raw))),
            bus.subscribe(bus_keys.state_sensor(SIM), lambda k, raw: self.inbox.put(("sensor", raw))),
            bus.subscribe(bus_keys.STATE_ENV, lambda k, raw: self.inbox.put(("env", raw))),
            bus.subscribe(bus_keys.STATE_MISSION, lambda k, raw: self.inbox.put(("mission", raw))),
            bus.subscribe(bus_keys.state_agent("agents"), lambda k, raw: self.inbox.put(("agents", raw))),
            bus.subscribe(bus_keys.state_agent("tasks"), lambda k, raw: self.inbox.put(("tasks", raw))),
            bus.subscribe(bus_keys.CTL_INTEREST, lambda k, raw: self.inbox.put(("interest", raw))),
        ]

    # ------------------------------------------------------------ 剧本
    def _scenario_marked(self, sc: dict[str, Any] | None) -> None:
        ids = [str(v.get("vehicle_id")) for v in (sc or {}).get("vehicles", []) if isinstance(v, dict) and v.get("marked")]
        self.marks.set_scenario_ids(ids)

    # ------------------------------------------------------------ 主循环
    def step(self) -> None:
        now = self.mono_ns()
        self._drain_inbox()
        self.subscriber.pump()
        ring = self._attach()
        if ring is not None:
            hdr = ring.header()
            self.hdr_rate = hdr.rate_milli / 1000 if hdr.rate_milli > 0 else self.hdr_rate
            if hdr.roster_version != self.roster_version and not self._roster_query_inflight:
                self._query_roster()
            if self.state == OFF and self.autostart and self.roster is not None:
                self.autostart = False
                self.start(reason="scenario")
            self.set_sim_segment(int(hdr.segment))
            frames, overrun = ring.drain()
            if overrun:
                self.stats["overrun"] += overrun
                if self.state == RECORDING:
                    self._gap(overrun, frames)
            for f in frames:
                self._on_frame(f)
        if self.state == RECORDING and self.writer.error is not None:
            log.error("writer failed, stopping", extra={"kv": {"error": repr(self.writer.error)}})
            self.stop("error")
        if now >= self._next_meta:
            self._next_meta = now + int(self.cfg.meta_every_s * 1e9)
            self._update_meta()
        if now >= self._next_disk:
            self._next_disk = now + int(self.cfg.disk_check_s * 1e9)
            if self.state == RECORDING and disk_status(self.persist_dir, min_gb=self.cfg.disk_min_gb).low:
                self.stop("disk_low")
        if self.interest_marks is not None and now - self.interest_at >= int(self.cfg.interest_debounce_s * 1e9):
            marks, self.interest_marks = self.interest_marks, None
            if self.marks.on_interest_marks(marks):
                self._update_marked()
        if now >= self._next_perf:
            self._next_perf = now + int(self.cfg.perf_every_s * 1e9)
            log.debug("perf/rec", extra={"kv": self.perf()})
        self.events.flush()

    # ------------------------------------------------------------ 直接输入（synth 与测试：不经环与总线）
    def set_sim_segment(self, segment: int, epoch: int | None = None) -> None:
        """环头部 segment：变化时换段（R02）；epoch 为新仿真段的生产者纪元（环头部一致性读，synth 显式给出）。"""
        if segment == self.sim_segment:
            return
        prev = self.sim_segment
        self.sim_segment = segment
        if epoch is not None:
            self.prod_epoch = epoch
        elif self.ring is not None:
            self.prod_epoch = int(self.ring.header().epoch)
        if self.state == RECORDING and prev >= 0:
            self._rotate("scenario_reset")

    def feed_frame(self, f: Any, rate: float | None = None) -> None:
        if rate is not None:
            self.hdr_rate = rate
        self._on_frame(f)

    def feed_events(self, producer: str, evs: list[dict]) -> None:
        self._on_bus_events(producer, evs)

    def feed_state(self, kind: str, raw: bytes) -> None:
        """kind ∈ ext、safety、sensor、env、mission、agents、tasks、interest（与总线订阅相同的载荷）。"""
        self.inbox.put((kind, raw))
        self._drain_inbox()

    def feed_roster(self, rep: dict[str, Any]) -> None:
        self._on_roster(rep)

    def perf(self) -> dict[str, Any]:
        w = self.writer
        return {"state": self.state, "segment": self.seg_k, "queue_bytes": w.q_bytes, "dropped_full": w.dropped["full"],
                "dropped_block": w.dropped["block"], "overrun": self.stats["overrun"], "msgs": w.msgs_total,
                "bytes": w.bytes_total, "reordered": self.rows.reordered}

    def _attach(self) -> StateRing | None:
        if self.ring is not None:
            self._ident_n += 1
            if self._ident_n % 50 == 0:  # 约 1 s：环文件被替换（sim-core 无 checkpoint 重启）时重新附着
                ino = self.ring.identity()[0]
                if ino and ino != self.ring.mapped_ino:
                    self.ring.close()
                    self.ring = None
                    return self._attach()
            return self.ring
        try:
            r = self.ring_cls.attach(self.ring_path, expect_layout_id=LAYOUT_ID)
        except Exception:  # 环尚未创建、布局不符（312）或写者重启中：下一轮重试
            return None
        r.register(LOSSY, "recorder")
        self.ring = r
        return r

    # ------------------------------------------------------------ 总线输入
    def _drain_inbox(self) -> None:
        for _ in range(4096):
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                return
            kind = item[0]
            try:
                if kind == "ctl":
                    self._on_ctl(item[1], item[2])
                elif kind == "roster":
                    self._on_roster(item[1])
                elif kind == "interest":
                    m = _unpack(item[1])
                    if isinstance(m, dict) and isinstance(m.get("marks"), list):
                        self.interest_marks = [int(x) for x in m["marks"]]
                        self.interest_at = self.mono_ns()
                elif kind == "env":
                    self._on_env(bytes(item[1]))
                elif self.state != RECORDING:
                    continue
                elif kind == "ext":
                    self.ext.offer(parse_batch(item[1]), max(self.roster_version, 0))
                elif kind == "safety":
                    self.saf.offer(parse_batch(item[1]), max(self.roster_version, 0))
                elif kind == "sensor":
                    self._on_sensor(item[1])
                elif kind == "mission":
                    self._on_mission(item[1])
                elif kind in ("agents", "tasks"):
                    self._on_agents(kind, item[1])
            except Exception:
                log.exception("recorder input failed", extra={"kv": {"kind": kind}})

    def _on_bus_events(self, producer: str, evs: list[dict]) -> None:
        if producer in (PRODUCER, "replay"):
            return
        for ev in evs:
            kind = str(ev.get("kind", ""))
            if kind == "sim.restarted":
                d = ev.get("data") or {}
                self.restored_hint = int(d.get("restored_t_sim_ns", ev.get("t_sim_ns", 0)) or 0)
            elif kind == "roster.changed":
                self._query_roster()
            elif kind in ("session.closing", "sys.shutting_down") and self.state == RECORDING:
                self.stop("session_closing")
            if self.state != RECORDING:
                continue
            t = int(ev.get("t_sim_ns", 0) or 0)
            self._put(Kind.KEEP, T_EVENT, t, prefix(int(ev.get("epoch", 0))) + _pack(ev), int(ev.get("seq", 0)))
            self.stats["events"] += 1
            if kind == "sim.clock":
                d = ev.get("data") or {}
                self.last_clock = {"state": d.get("state"), "rate": d.get("rate"), "t_sim_ns": t}
                self._put(Kind.KEEP, T_CLOCK, t, prefix(0) + _pack(self.last_clock))
            elif kind == "env.keyframe" and isinstance(ev.get("data"), dict):
                self.last_env = _pack(ev["data"])
                self.last_env_t = t
                self._put(Kind.KEEP, T_ENV, t, prefix(0, RFLAG_KEYFRAME) + self.last_env)

    def _on_event_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        log.warning("event gap", extra={"kv": {"producer": producer, "epoch": epoch, "lo": lo, "hi": hi}})

    def _query_roster(self) -> None:
        self._roster_query_inflight = True

        def done(rep: Any, err: BaseException | None) -> None:  # 回调线程：只入队
            self.inbox.put(("roster", None if err is not None else rep))

        try:
            self.bus.call_cb(bus_keys.ctl_roster(SIM), {"v": 1}, done, timeout=1.0)
        except (BusError, RuntimeError):
            self._roster_query_inflight = False

    def _on_roster(self, rep: Any) -> None:
        self._roster_query_inflight = False
        if not isinstance(rep, dict) or "entries" not in rep:
            return
        self.roster = rep
        self.roster_version = int(rep.get("roster_version", 0))
        self.agent_id = {int(e["agent_no"]): str(e["id"]) for e in rep.get("entries", []) if "agent_no" in e and "id" in e}
        self.marks.resolve_scenario(rep.get("entries", []))
        self._update_marked()
        if self.state == RECORDING:
            self._put(Kind.KEEP, T_ROSTER, max(self.last_frame_t, 0), prefix(max(self.prod_epoch, 0)) + _pack(rep))

    def _on_env(self, raw: bytes) -> None:
        try:
            t = int((_unpack(raw) or {}).get("t_ns", self.last_frame_t))
        except Exception:
            t = self.last_frame_t
        self.last_env = raw
        self.last_env_t = t
        if self.state == RECORDING:
            self._put(Kind.KEEP, T_ENV, max(t, 0), prefix(max(self.prod_epoch, 0), RFLAG_KEYFRAME) + raw)

    def _on_sensor(self, raw: bytes) -> None:
        m = _unpack(raw)
        if not isinstance(m, dict) or not isinstance(m.get("rows"), (bytes, bytearray)):
            return
        t = int(m.get("t_sim_ns", self.last_frame_t))
        if t < self.pose_next_t:
            return
        b = int(1e9 / self.policy.pose_hz)
        self.pose_next_t = (t // b + 1) * b
        rows = m["rows"]
        for i in range(len(rows) // 48):
            row = rows[i * 48:(i + 1) * 48]
            a = int.from_bytes(row[0:2], "little")
            if a not in self.marked:
                continue
            vid = self.agent_id.get(a)
            if vid is None:
                continue
            self._put(Kind.FULL, pose_topic(vid, self._sensor_name(a, row[2])), t, prefix(max(self.prod_epoch, 0)) + bytes(row))

    def _sensor_name(self, agent_no: int, sensor_no: int) -> str:
        for e in (self.roster or {}).get("entries", []):
            if int(e.get("agent_no", -1)) == agent_no:
                ss = e.get("sensors") or []
                if 0 <= sensor_no < len(ss):
                    s = ss[sensor_no]
                    return str(s.get("name", s) if isinstance(s, dict) else s)
        return f"s{sensor_no}"

    def _on_mission(self, raw: bytes) -> None:
        m = _unpack(raw)
        items = m if isinstance(m, list) else (m.get("items") or m.get("missions") or [m]) if isinstance(m, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = str(it.get("mid") or it.get("mission_id") or "")
            if not mid:
                continue
            b = _pack(it)
            if self.mission_last.get(mid) == b:
                continue
            self.mission_last[mid] = b
            self._put(Kind.KEEP, mission_topic(mid), max(self.last_frame_t, 0), prefix(max(self.prod_epoch, 0)) + b)

    def _on_agents(self, kind: str, raw: bytes) -> None:
        m = _unpack(raw)
        if kind == "tasks":
            b = _pack(m)
            if self.agent_last.get("__tasks__") != b:
                self.agent_last["__tasks__"] = b
                self._put(Kind.KEEP, T_AGENT_TASKS, max(self.last_frame_t, 0), prefix(max(self.prod_epoch, 0)) + b)
            return
        items = m if isinstance(m, list) else (m.get("items") or []) if isinstance(m, dict) else []
        for it in items:
            if not isinstance(it, dict):
                continue
            aid = str(it.get("aid") or it.get("agent_id") or "")
            if not aid:
                continue
            b = _pack(it)
            if self.agent_last.get(aid) != b:
                self.agent_last[aid] = b
                self._put(Kind.KEEP, agent_topic(aid), max(self.last_frame_t, 0), prefix(max(self.prod_epoch, 0)) + b)

    # ------------------------------------------------------------ 控制（ctl/recorder/*）
    def _on_ctl(self, op: str, req: Any) -> None:
        msg = req.msg() if hasattr(req, "msg") else {}
        cid = (msg or {}).get("cid")
        base = {"v": 1, "cid": cid}
        if op == "start":
            if self.state == RECORDING:
                rep = base | {"status": "accepted", "code": 0}
            elif disk_status(self.persist_dir, min_gb=self.cfg.disk_min_gb).low:
                rep = base | {"status": "rejected", "code": int(Reason.REC_DISK_LOW)}
            elif self._attach() is None:
                rep = base | {"status": "rejected", "code": int(Reason.SIM_UNAVAILABLE)}
            else:
                self.start(reason="rec/start")
                rep = base | {"status": "accepted", "code": 0}
        elif op == "stop":
            if self.state == RECORDING:
                self.stop("stop")
            rep = base | {"status": "accepted", "code": 0}
        else:
            rep = base | {"status": "accepted", "code": 0}
        rep |= {"state": self.state, "segment": self.seg_k, "marked": sorted(self.marked), "perf": self.perf()}
        req.reply_msg(rep)

    # ------------------------------------------------------------ 段
    def _binding(self) -> dict[str, Any]:
        b = self.meta.binding
        return {"world_id": b.get("world_id") or self.world_id, "content_version": b.get("content_version", ""),
                "coordinate_sha256": b.get("coordinate_sha256") or "0" * 64, "layout_id": LAYOUT_ID,
                "contracts_version": CONTRACTS_VERSION}

    def start(self, reason: str = "rec/start") -> None:
        if self.state == RECORDING:
            return
        self.state = RECORDING
        self._open_segment()
        self.events.emit("rec.started", t_sim_ns=max(self.last_frame_t, 0), severity=1, segment=self.seg_k,
                         file=seg_file(self.seg_k), policy={"swarm_hz": self.policy.swarm_hz, "full_hz": self.policy.full_hz},
                         reason=reason)

    def _open_segment(self) -> None:
        k = self.meta.next_segment_no()
        self.seg_k = k
        self.stats["segments"] += 1
        ring = self.ring
        hdr = ring.header() if ring is not None else None
        self.prod_epoch = int(hdr.epoch) if hdr is not None else max(self.prod_epoch, 0)
        self.sim_segment = int(hdr.segment) if hdr is not None else max(self.sim_segment, 0)
        t0 = int(hdr.t_sim_ns) if hdr is not None else max(self.last_frame_t, 0)
        self.selector.reset()
        self.ext.reset()
        self.saf.reset()
        self.mission_last.clear()
        self.agent_last.clear()
        self.pose_next_t = 0
        n = len(self.agent_id)
        tracks = sorted(self.marks.scenario) if n > self.policy.full_all_if_n_le else sorted(self.agent_id)[:16]
        binding = self._binding()
        self.meta.set_binding(binding)
        spec = SegmentSpec(
            k=k, path=self.persist_dir / seg_file(k), ovw_path=self.persist_dir / f"rec-{k:03d}.ovw",
            evx_path=self.persist_dir / f"rec-{k:03d}.evx",
            binding={**{kk: str(v) for kk, v in binding.items()}, "run_id": self.run_id, "segment": str(k),
                     "sim_segment": str(self.sim_segment), "epoch_start": str(self.prod_epoch),
                     "ovw_tracks": ",".join(str(a) for a in tracks[:16])},
            tracks=tracks[:16], agent_of={v: a for a, v in self.agent_id.items()})
        self.writer.start_segment(spec)
        coverage = "all" if n <= 64 else "interest"
        self.meta.open_segment(k, epoch_start=self.prod_epoch, t_start_ns=t0, sim_segment=self.sim_segment, detail_coverage=coverage)
        self.meta.set_marked([self.agent_id[a] for a in sorted(self.marked) if a in self.agent_id])
        self.meta.save()
        # 段首消息：roster、时钟、最近 EnvKeyframe；低频关键块在第一帧到期（KeyDelta 已复位）
        ep = prefix(max(self.prod_epoch, 0))
        if self.roster is not None:
            self._put(Kind.KEEP, T_ROSTER, t0, ep + _pack(self.roster))
        else:
            self._query_roster()
        clock = self.last_clock or ({"state": int(hdr.clock_state & 0x0F), "rate": hdr.rate_milli / 1000, "t_sim_ns": t0}
                                    if hdr is not None else None)
        if clock is not None:
            self._put(Kind.KEEP, T_CLOCK, t0, prefix(0) + _pack(clock))
        if self.last_env is not None:
            self._put(Kind.KEEP, T_ENV, max(min(self.last_env_t, t0), 0), prefix(max(self.prod_epoch, 0), RFLAG_KEYFRAME) + self.last_env)
        self.writer.metadata("awr.marks", {"t_ns": t0, "marked": ",".join(str(a) for a in sorted(self.marked))})

    def _rotate(self, reason: str) -> None:
        """R02：结束当前段并打开下一个文件段。"""
        self.stats["rotations"] += 1
        self._end_segment(reason)
        self._open_segment()

    def _end_segment(self, reason: str) -> None:
        t_end = max(self.last_frame_t, 0)
        self.writer.end_segment(t_end, reason)
        self.meta.update_segment(self.seg_k, t_end_ns=t_end)
        self.meta.save()

    def stop(self, reason: str = "stop") -> None:
        if self.state != RECORDING:
            return
        self.state = CLOSING
        k = self.seg_k
        self._end_segment(reason)
        self.writer.drain(self.cfg.close_timeout_s)
        self.state = OFF
        self.events.emit("rec.stopped", t_sim_ns=max(self.last_frame_t, 0), severity=2 if reason == "disk_low" else 1,
                         segment=k, reason=reason)
        self.events.flush()

    def _on_closed(self, st: SegmentStats) -> None:
        """writer 线程：段已 finish；meta 置 CLOSED（与主线程互斥由 MetaFile 锁保证）。"""
        s = self.meta.segment(st.k)
        dec = None
        if st.max_block_dt > 60_000_000:
            dec = {"active": True, "rate_max": round(st.max_block_dt / 40_000_000 * 2, 3), "sim_resolution_s": round(st.max_block_dt / 1e9, 3)}
        t_end = max(st.t_last, int((s or {}).get("t_end_ns") or 0))
        self.meta.update_segment(st.k, t_end_ns=t_end, nbytes=st.nbytes, events=st.events, decimation=dec, state="CLOSED")
        self.meta.save()

    def _update_meta(self) -> None:
        st = self.writer.stats
        if self.state == RECORDING and st is not None and st.k == self.seg_k:
            dec = None
            if st.max_block_dt > 60_000_000:
                dec = {"active": True, "rate_max": round(st.max_block_dt / 40_000_000 * 2, 3), "sim_resolution_s": round(st.max_block_dt / 1e9, 3)}
            self.meta.update_segment(self.seg_k, t_end_ns=max(st.t_last, self.last_frame_t), nbytes=st.nbytes, events=st.events,
                                     decimation=dec)
            self.meta.save()

    def _update_marked(self) -> None:
        cur = self.marks.current(sorted(self.agent_id))
        if cur == self.marked:
            return
        self.marked = cur
        self.meta.set_marked([self.agent_id[a] for a in sorted(cur) if a in self.agent_id])
        if self.state == RECORDING:
            self.writer.metadata("awr.marks", {"t_ns": max(self.last_frame_t, 0), "marked": ",".join(str(a) for a in sorted(cur))})

    # ------------------------------------------------------------ 帧
    def _put(self, kind: Kind, topic: str, t_ns: int, data: bytes, seq: int = 0) -> None:
        if self.state == RECORDING:
            self.writer.put(kind, topic, t_ns, data, seq)

    def _gap(self, overrun: int, frames: list) -> None:
        t_from = self.last_frame_t
        t_to = frames[0].t_sim_ns if frames else t_from
        dropped = self.writer.take_dropped()
        self.stats["gaps"] += 1
        ev = {"overrun": int(overrun), "dropped": dropped, "t_from_ns": int(t_from), "t_to_ns": int(t_to)}
        self.events.emit("recorder.gap", t_sim_ns=max(t_to, 0), severity=2, **ev)
        self.meta.add_gap(self.seg_k, t_from_ns=t_from, t_to_ns=t_to, overrun=overrun, dropped=dropped)

    def _on_frame(self, f: Any) -> None:
        self.stats["frames"] += 1
        if self.state == RECORDING and self.prod_epoch >= 0 and f.epoch != self.prod_epoch:
            self._lineage(f)
        self.prod_epoch = f.epoch
        if self.state != RECORDING:
            self.last_frame_t = f.t_sim_ns
            return
        blk, full = self.selector.select(f.t_sim_ns, self.hdr_rate)
        ep = prefix(f.epoch)
        if blk:
            self._put(Kind.BLOCK, T_BLOCK, f.t_sim_ns, ep + self.rows.block_payload(f.lite, f.n_rows, f.roster_version), f.frame_seq)
            self.stats["blocks"] += 1
        if full and self.marked:
            for a, row in self.rows.rows(f.full, f.roster_version, f.n_rows, self.marked):
                vid = self.agent_id.get(a)
                if vid is not None:
                    self._put(Kind.FULL, full_topic(vid), f.t_sim_ns, ep + row, f.frame_seq)
                    self.stats["full"] += 1
        for blocker, topic in ((self.ext, T_EXT), (self.saf, T_SAFETY)):
            out = blocker.poll(f.t_sim_ns)
            if out is not None:
                payload, key = out
                self._put(Kind.KEEP, topic, f.t_sim_ns, prefix(f.epoch, RFLAG_KEYFRAME if key else RFLAG_DELTA) + payload)
        self.last_frame_t = f.t_sim_ns
        self.last_frame_epoch = f.epoch

    def _lineage(self, f: Any) -> None:
        """R03：checkpoint 恢复（生产者纪元变化）：先 FLUSH，再写 awr.lineage；.evx 回写作废标志。"""
        restored = self.restored_hint if self.restored_hint is not None else f.t_sim_ns
        self.restored_hint = None
        last = self.last_frame_t
        e = {"epoch": int(f.epoch), "restored_t_ns": int(restored), "last_t_ns": int(last),
             "invalid_from_ns": int(restored), "invalid_to_ns": int(last)}
        self.writer.lineage(e)
        self.meta.add_lineage(self.seg_k, epoch=int(f.epoch), restored_t_ns=int(restored), last_t_ns=int(last))
        self.selector.reset()
        self.ext.reset()
        self.saf.reset()
        self.events.emit("rec.lineage", t_sim_ns=int(restored), severity=1, epoch=int(f.epoch), restored_t_ns=int(restored),
                         last_t_ns=int(last))

    def close(self) -> None:
        if self.state == RECORDING:
            self.stop("stop")
        self.writer.close(self.cfg.close_timeout_s)
        for h in self.handles:
            h.close()
        self.subscriber.close()
        self.events.close()
        if self.ring is not None:
            self.ring.unregister()
            self.ring.close()


def main() -> int:
    from awr.runtime.bus import ZenohBus
    from awr.runtime.child import init_child
    from awr.runtime.heartbeat import Heartbeat

    from .config import load_recorder_cfg
    from .policy import load_scenario

    ctx = init_child("recorder")
    cfg = load_recorder_cfg()
    bus = ZenohBus.open("recorder", ctx)
    auto = os.environ.get("AWR_REC_AUTOSTART")
    core = RecorderCore(bus=bus, ring_path=ctx.run_dir / "state.sim-core", persist_dir=ctx.persist_dir, run_id=ctx.run_id,
                        world_id=ctx.world_id, cfg=cfg, scenario=load_scenario(),
                        autostart=None if auto is None else auto == "1", event_epoch=int(ctx.restart_count) + 1)
    hb = Heartbeat(ctx.hb_path)
    ready = bus.ready()
    loop_ns = cfg.loop_ms * 1_000_000
    try:
        while not ctx.stopping:
            t0 = time.monotonic_ns()
            hb.beat()
            core.step()
            dt = t0 + loop_ns - time.monotonic_ns()
            if dt > 0:
                time.sleep(dt / 1e9)
        core.stop("session_closing")
    finally:
        core.close()
        ready.close()
        bus.close()
    return 0
