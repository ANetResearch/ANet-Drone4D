"""事件平面：EventPublisher 按步合批与 `_replay`，EventSubscriber 缺口补拉、重排、去重（M11-AC-004；M11-FR-008、FR-009）。"""

from __future__ import annotations

import time

import msgpack
import pytest
import rtlib

from awr.contracts import bus_keys as K
from awr.runtime.bus import LocalBus
from awr.runtime.events import EventPublisher, EventSubscriber, category_of


class Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.gaps: list[tuple] = []
        self.calls = 0

    def on_events(self, producer: str, evs: list[dict]) -> None:
        self.calls += 1
        self.events.extend(evs)

    def on_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        self.gaps.append((producer, epoch, lo, hi))

    @property
    def seqs(self) -> list[int]:
        return [e["seq"] for e in self.events]


def pump_until(sub: EventSubscriber, pred, timeout: float = 3.0, also=None) -> bool:
    """宿主主循环：pump 订阅者；also 为生产者侧每轮调用（例如 pub.flush()，应答排队的 _replay）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if also is not None:
            also()
        sub.pump()
        if pred():
            return True
        time.sleep(0.005)
    sub.pump()
    return bool(pred())


def pair(kind: str, namespace: str):
    a, b = rtlib.open_pair(kind, namespace)
    if kind == "zenoh":
        time.sleep(0.3)
    return a, b


def test_category_mapping() -> None:
    assert category_of("cmd.accepted") == "cmd"
    assert category_of("safety.geofence") == "safety"
    assert category_of("seat.acquired") == "lease"
    assert category_of("roster.changed") == "sim"
    assert category_of("proc.state") == "proc"
    assert category_of("fleet.batch.progress") == "cmd"


def test_emit_batch_flush_and_order(bus_kind: str, namespace: str) -> None:
    a, b = pair(bus_kind, namespace)
    sink = Sink()
    raw_msgs: list[str] = []
    b.subscribe("evt/**", lambda k, p: raw_msgs.append(k))
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=3)
    try:
        if bus_kind == "zenoh":
            time.sleep(0.3)
        for i in range(5):
            pub.emit("cmd.accepted" if i % 2 else "safety.separation", t_sim_ns=i * 4_000_000, uav="uav0001", cid=f"c{i}",
                     severity=2, value=4.0)
        assert pub.flush() == 2  # 两个 category，各一次 put
        assert pub.flush() == 0
        assert pump_until(sub, lambda: len(sink.events) == 5)
        assert sorted(sink.seqs) == [1, 2, 3, 4, 5]
        e = sink.events[0]
        assert set(e) == {"seq", "epoch", "producer", "kind", "severity", "t_sim_ns", "t_wall_ns", "uav", "cid", "data"}
        assert e["epoch"] == 3 and e["producer"] == "sim-core" and e["data"] == {"value": 4.0}
        errs = rtlib.schema_errors("bus/event.schema.json", e)
        assert errs == []
        assert sorted(set(raw_msgs)) == [K.evt("sim-core", "cmd"), K.evt("sim-core", "safety")]
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_dropped_message_recovered_via_replay_in_order(bus_kind: str, namespace: str) -> None:
    a, b = pair(bus_kind, namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=1)
    dropped = []

    def flt(key: str, raw: bytes) -> bool:
        evs = msgpack.unpackb(raw)
        if any(e["seq"] == 4 for e in evs) and not dropped:
            dropped.append(time.monotonic())
            return False
        return True

    sub.filter = flt
    try:
        if bus_kind == "zenoh":
            time.sleep(0.3)
        for i in range(1, 11):
            pub.emit("sim.clock", t_sim_ns=i, step=i)
            pub.flush()  # 每步一条消息；第 4 条被丢弃
            sub.pump()
            if i == 6:
                assert sink.seqs == [1, 2, 3]  # 缺口补齐前后续事件暂存，不越序交付
            time.sleep(0.005)
        assert pump_until(sub, lambda: len(sink.events) == 10, 3.0, also=pub.flush)
        assert sink.seqs == list(range(1, 11))
        assert sink.gaps == []
        assert time.monotonic() - dropped[0] < 1.0
        assert sub.stats["gaps_detected"] >= 1 and sub.stats["gaps_filled"] >= 1 and sub.stats["replays"] >= 1
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_truncated_replay_reports_gap_and_releases_buffer(namespace: str) -> None:
    a, b = pair("local", namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=1, ring=4)
    sub.filter = lambda key, raw: not (3 <= msgpack.unpackb(raw)[0]["seq"] <= 8)
    try:
        for i in range(1, 13):
            pub.emit("sim.clock", t_sim_ns=i)
            pub.flush()
        t0 = time.monotonic()
        assert pump_until(sub, lambda: len(sink.events) == 6, 3.0, also=pub.flush)
        assert time.monotonic() - t0 < 0.5  # truncated 立即报告，不等 1 s
        # 环只剩 9..12：3..8 无法补齐，报告缺口后按序放行
        assert sink.gaps == [("sim-core", 1, 3, 8)]
        assert sink.seqs == [1, 2, 9, 10, 11, 12]
        assert sub.stats["events_lost"] == 6
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_no_replay_service_gives_up_after_timeout(namespace: str) -> None:
    a, b = pair("local", namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, gap_timeout_s=0.3)
    pub = EventPublisher(a, "sim-core", epoch=1, serve_replay=False)
    sub.filter = lambda key, raw: msgpack.unpackb(raw)[0]["seq"] != 2
    try:
        for i in range(1, 5):
            pub.emit("sim.clock", t_sim_ns=i)
            pub.flush()
        sub.pump()
        assert sink.seqs == [1]
        t0 = time.monotonic()
        assert pump_until(sub, lambda: sink.gaps, 3.0)
        assert 0.2 <= time.monotonic() - t0 < 2.0  # _replay 无回复：到缺口超时（此处 0.3 s）才放弃
        assert sink.gaps == [("sim-core", 1, 2, 2)] and sink.seqs == [1, 3, 4]
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_epoch_change_resets_tracking_and_stale_ignored(namespace: str) -> None:
    a, b = pair("local", namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=1)
    try:
        for i in range(3):
            pub.emit("sim.clock", t_sim_ns=i)
        pub.flush()
        old = pub.replay(0, 1)
        pub.set_epoch(2)
        pub.emit("sim.started", t_sim_ns=0, reason="crash_restart")
        pub.flush()
        sub.pump()
        assert [(e["epoch"], e["seq"]) for e in sink.events] == [(1, 1), (1, 2), (1, 3), (2, 1)]
        sub.feed("sim-core", msgpack.packb(old["events"]))  # 旧纪元的迟到消息：忽略
        assert len(sink.events) == 4 and sink.gaps == []
        assert pub.replay(0, 1)["truncated"] is True  # 旧纪元无法补拉
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_duplicates_and_first_seen_mid_stream(namespace: str) -> None:
    a, b = pair("local", namespace)
    sink = Sink()
    pub = EventPublisher(a, "sim-core", epoch=1)
    for i in range(5):  # 订阅者启动之前的事件
        pub.emit("sim.clock", t_sim_ns=i)
    pub.flush()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    try:
        pub.emit("sim.clock", t_sim_ns=5)
        pub.flush()
        sub.pump()
        assert sink.seqs == [6] and sub.stats["replays"] == 0  # 首次见到该生产者：不回补订阅前的事件
        raw = msgpack.packb([pub.replay(5, 1)["events"][0]])
        sub.feed("sim-core", raw)
        sub.feed("sim-core", raw)
        assert sink.seqs == [6] and sub.stats["duplicates"] == 2
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_held_buffer_limit_forces_give_up(namespace: str) -> None:
    a, b = pair("local", namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, max_held=5, gap_timeout_s=30)
    pub = EventPublisher(a, "sim-core", epoch=1, serve_replay=False)
    sub.filter = lambda key, raw: msgpack.unpackb(raw)[0]["seq"] != 2
    try:
        for i in range(1, 9):
            pub.emit("sim.clock", t_sim_ns=i)
            pub.flush()
            sub.pump()
        assert sink.gaps == [("sim-core", 1, 2, 2)]
        assert sink.seqs == [1, 3, 4, 5, 6, 7, 8]
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_replay_semantics() -> None:
    bus = LocalBus.open("p", namespace="ev-replay-unit")
    pub = EventPublisher(bus, "sim-core", epoch=1, ring=4)
    try:
        for i in range(6):
            pub.emit("sim.clock", t_sim_ns=i)
        r = pub.replay(2, 1)
        assert [e["seq"] for e in r["events"]] == [3, 4, 5, 6] and r["truncated"] is False
        r = pub.replay(1, 1)
        assert [e["seq"] for e in r["events"]] == [3, 4, 5, 6] and r["truncated"] is True
        assert pub.replay(6, 1) == {"v": 1, "events": [], "truncated": False}
        assert rtlib.schema_errors("bus/replay.schema.json", pub.replay(2, 1), "#/$defs/replayRep") == []
    finally:
        pub.close()
        bus.close()


@pytest.mark.parametrize("kind", ["zenoh"])
def test_570_events_per_s_no_gap_no_reorder(kind: str, namespace: str) -> None:
    """功能级：570 条/s × 3 s 经 ZenohBus，缺口与乱序 0（60 s 门禁见 tools/bench/ipc/bench_cmd.py）。"""
    a, b = pair(kind, namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=1)
    try:
        time.sleep(0.3)
        k = 0
        acc = 0.0
        # 250 Hz × 3 s 按迭代计数（共 1710 条）而不是墙钟窗口：全量并发负载下 3 s 墙钟内的迭代数不足，条数断言偶发失败
        # （FX-GW，INT-1 §7.11 负载敏感用例；缺口与乱序的判定不变）
        while k < 750:
            k += 1
            acc += 570 / 250
            while acc >= 1:
                acc -= 1
                pub.emit("safety.separation" if k % 3 else "cmd.succeeded", t_sim_ns=k * 4_000_000, severity=2)
            pub.flush()
            if k % 4 == 0:
                sub.pump()
            time.sleep(0.004)
        assert pump_until(sub, lambda: len(sink.events) == pub.last_seq, 3.0, also=pub.flush)
        assert sink.seqs == list(range(1, pub.last_seq + 1))
        assert sink.gaps == [] and pub.last_seq >= 1700
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_tail_drop_recovered_by_quiet_probe(bus_kind: str, namespace: str) -> None:
    """尾部批次被丢弃、生产者此后不再发事件：没有后续 seq 可检出缺口，由尾部探测（静默 ≥ 0.5 s）在 1 s 内补齐（D1-AC-10）。"""
    a, b = pair(bus_kind, namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap)
    pub = EventPublisher(a, "sim-core", epoch=1)
    dropped: list[float] = []

    def flt(key: str, raw: bytes) -> bool:
        if any(e["seq"] >= 4 for e in msgpack.unpackb(raw)):
            dropped.append(time.monotonic())
            return False
        return True

    sub.filter = flt
    try:
        if bus_kind == "zenoh":
            time.sleep(0.3)
        for i in range(1, 6):
            pub.emit("cmd.succeeded", t_sim_ns=i, cid=f"c{i}")
            if i in (3, 5):
                pub.flush()  # 两条消息：1–3 送达，4–5 被丢弃（尾部批次）
        assert pump_until(sub, lambda: len(sink.events) == 3, 2.0, also=pub.flush)
        assert dropped and sub.stats["gaps_detected"] == 0  # 没有后续消息，缺口检出不了
        assert pump_until(sub, lambda: len(sink.events) == 5, 2.0, also=pub.flush)
        assert time.monotonic() - dropped[0] < 1.0
        assert sink.seqs == [1, 2, 3, 4, 5] and sink.gaps == []
        assert sub.stats["tail_probes"] >= 1 and sub.stats["tail_recovered"] == 2 and sub.stats["replays"] == 0
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_tail_probe_quiet_producer_and_backoff(namespace: str) -> None:
    """持续发事件的生产者不触发尾部探测；静默生产者按周期探测、无新事件时什么都不交付；生产者不可达时退避。"""
    a, b = pair("local", namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, tail_probe_s=0.1)
    pub = EventPublisher(a, "sim-core", epoch=1)
    mute = EventPublisher(a, "job-worker", epoch=1, serve_replay=False)
    try:
        t_end = time.monotonic() + 0.4
        while time.monotonic() < t_end:  # 每 20 ms 一条：不足 0.1 s 静默，不探测
            pub.emit("sim.clock", t_sim_ns=1)
            pub.flush()
            sub.pump()
            time.sleep(0.02)
        assert sub.stats["tail_probes"] == 0
        mute.emit("job.progress", t_sim_ns=0)
        mute.flush()
        assert pump_until(sub, lambda: sub.stats["tail_probes"] >= 3, 2.0, also=pub.flush)
        n = len(sink.events)
        assert pump_until(sub, lambda: sub.stats["tail_errors"] >= 1, 3.0, also=pub.flush)  # job-worker 无 _replay
        assert len(sink.events) == n and sink.gaps == []
        tr = sub._trackers["job-worker"]
        assert tr.tail_every_s > 0.1  # 退避
        mute.emit("job.progress", t_sim_ns=1)
        mute.flush()
        sub.pump()
        assert tr.tail_every_s == 0.1  # 实时消息到达后恢复
    finally:
        sub.close()
        pub.close()
        mute.close()
        b.close()
        a.close()
