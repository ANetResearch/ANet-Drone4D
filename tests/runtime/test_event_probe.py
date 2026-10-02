"""EventSubscriber 主动探测（`probe`，FX-GW；FX-WEB2-to-M11 第 2 条）：
- 订阅建立之前发出、此后再无后续事件的生产者（demo 下 recorder 的唯一一条 `rec.started`）经探测补拉；
- 纪元猜错（回复 truncated 且无事件）或无 `_replay` 服务：撤销跟踪，此后按首见规则处理；探测在途期间到达的真实事件不因
  猜测纪元被当作旧纪元丢弃；
- 历史超过首见补拉上限（生产者早已运行）：不回补，只把跟踪起点移到最新 seq；
- 已在跟踪的生产者不探测。
LocalBus；生产者 `_replay` 只在宿主线程 `serve_replays()` 时应答（与真实进程的主循环一致）。
"""

from __future__ import annotations

import time

from awr.runtime.bus import LocalBus
from awr.runtime.events import EventPublisher, EventSubscriber


class Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.gaps: list[tuple] = []

    def on_events(self, producer: str, evs: list[dict]) -> None:
        self.events.extend(evs)

    def on_gap(self, producer: str, epoch: int, lo: int, hi: int) -> None:
        self.gaps.append((producer, epoch, lo, hi))

    @property
    def seqs(self) -> list[tuple[int, int]]:
        return [(e["epoch"], e["seq"]) for e in self.events]


def pump_until(sub: EventSubscriber, pred, timeout: float = 3.0, also=None) -> bool:
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


def _pair(namespace: str):
    return LocalBus.open("recorder", namespace=namespace), LocalBus.open("api", namespace=namespace)


def test_probe_backfills_lone_event_emitted_before_subscription(namespace: str) -> None:
    a, b = _pair(namespace)
    pub = EventPublisher(a, "recorder", 1)
    pub.emit("rec.started", t_sim_ns=5, segment=0)
    pub.flush()  # 订阅者尚不存在：这条事件在总线上丢失，只留在生产者 _replay 环里
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, backfill_first_max=1024)
    try:
        sub.pump()
        assert sink.events == [] and sub.tracked() == {}  # 没有后续事件：首见补拉不会触发
        assert sub.probe("recorder", 1) is True
        assert sub.probe("recorder", 1) is False  # 探测在途（已在跟踪）不重复
        assert pump_until(sub, lambda: sink.events, 3.0, also=pub.serve_replays)
        assert [e["kind"] for e in sink.events] == ["rec.started"] and sink.seqs == [(1, 1)]
        assert sub.stats["probe_hits"] == 1 and sink.gaps == []
        pub.emit("rec.stopped", t_sim_ns=9, reason="user")
        pub.flush()
        assert pump_until(sub, lambda: len(sink.events) == 2, 2.0)
        assert sink.seqs == [(1, 1), (1, 2)] and sub.stats["replays"] == 0 and sub.stats["duplicates"] == 0
        assert sub.probe("recorder", 1) is False  # 已在跟踪
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_probe_with_wrong_epoch_reverts_to_first_seen(namespace: str) -> None:
    a, b = _pair(namespace)
    pub = EventPublisher(a, "recorder", 2)  # 重启过一次：纪元 2
    for i in range(3):
        pub.emit("rec.marker", t_sim_ns=i)
    pub.flush()
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, backfill_first_max=1024)
    try:
        assert sub.probe("recorder", 1)
        assert pump_until(sub, lambda: sub.stats["probe_misses"] == 1, 3.0, also=pub.serve_replays)
        assert sub.tracked() == {} and sink.events == []  # truncated 且无事件：恢复"未见过"
        pub.emit("rec.marker", t_sim_ns=3)
        pub.flush()
        # 首见规则：首条 seq 4 ≤ 1025 → 从 1 起补拉
        assert pump_until(sub, lambda: len(sink.events) == 4, 3.0, also=pub.serve_replays)
        assert sink.seqs == [(2, 1), (2, 2), (2, 3), (2, 4)] and sink.gaps == []
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_live_event_during_probe_not_dropped_as_stale_epoch(namespace: str) -> None:
    """探测猜的纪元（3）高于真实纪元（2）：回复到达之前的真实事件不能按"旧纪元"丢弃；迟到的探测回复被忽略。"""
    a, b = _pair(namespace)
    pub = EventPublisher(a, "recorder", 2)
    pub.emit("rec.marker", t_sim_ns=0)
    pub.flush()
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, backfill_first_max=1024)
    try:
        assert sub.probe("recorder", 3)  # 不应答 _replay：探测保持在途
        pub.emit("rec.marker", t_sim_ns=1)
        pub.flush()
        sub.pump()
        assert sub.tracked()["recorder"][0] == 2  # 首见规则接管（纪元 2），未计为重复
        assert sub.stats["duplicates"] == 0
        assert pump_until(sub, lambda: len(sink.events) == 2, 3.0, also=pub.serve_replays)
        assert sink.seqs == [(2, 1), (2, 2)] and sink.gaps == []
        sub.pump()
        assert sub.stats["probe_misses"] == 0 and sub.stats["probe_hits"] == 0  # 纪元 3 的回复被忽略
        assert sub.tracked()["recorder"] == (2, 2, 0)
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_probe_long_history_not_backfilled(namespace: str) -> None:
    a, b = _pair(namespace)
    pub = EventPublisher(a, "agent-runtime", 1)
    for i in range(1100):  # 早已运行：超过首见补拉上限（1024 + 1）
        pub.emit("agent.tick", t_sim_ns=i)
    pub.flush()
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, backfill_first_max=1024)
    try:
        assert sub.probe("agent-runtime", 1)
        assert pump_until(sub, lambda: sub.stats["probe_skipped"] == 1, 3.0, also=pub.serve_replays)
        assert sink.events == [] and sub.tracked()["agent-runtime"] == (1, 1100, 0)
        pub.emit("agent.tick", t_sim_ns=1100)
        pub.flush()
        assert pump_until(sub, lambda: sink.events, 2.0)
        assert sink.seqs == [(1, 1101)] and sink.gaps == [] and sub.stats["replays"] == 0
    finally:
        sub.close()
        pub.close()
        b.close()
        a.close()


def test_probe_without_replay_service_reverts(namespace: str) -> None:
    b = LocalBus.open("api", namespace=namespace)
    sink = Sink()
    sub = EventSubscriber(b, on_events=sink.on_events, on_gap=sink.on_gap, backfill_first_max=1024)
    try:
        assert sub.probe("job-worker", 1)
        assert pump_until(sub, lambda: sub.stats["probe_misses"] == 1, 5.0)
        assert sub.tracked() == {} and sink.events == [] and sink.gaps == []
    finally:
        sub.close()
        b.close()
