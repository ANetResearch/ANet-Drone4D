"""网关启动时补拉辅助生产者的早期事件（FX-WEB2-to-M11 第 2 条；17 §9.5 补充）：
recorder 在 api 订阅之前发出唯一一条 `rec.started`、此后再无后续事件时，首见补拉不会触发；Gateway 在 `proc/recorder/ready`
（带历史）出现时以纪元"重启次数 + 1"探测 `evt/recorder/_replay`，该事件进入 EventRing（`GET /api/events` 与 WS `event`）。
FakeSim + 真实 Gateway；recorder 以 LocalBus 上的真实 EventPublisher 代替。
"""

from __future__ import annotations

import asyncio
import threading
import time

import fakesim
import httpx
import rtc

from awr.runtime.bus import LocalBus
from awr.runtime.events import EventPublisher


class _Recorder:
    """只发事件的 recorder 替身：主循环线程应答 `_replay`（与真实进程相同，只在宿主线程应答）。"""

    def __init__(self, namespace: str, epoch: int = 1) -> None:
        self.bus = LocalBus.open("recorder", namespace=namespace)
        self.pub = EventPublisher(self.bus, "recorder", epoch)
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.ready = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_ev.is_set():
            with self.lock:
                self.pub.serve_replays()
            time.sleep(0.005)

    def emit(self, kind: str, **data) -> None:
        with self.lock:
            self.pub.emit(kind, t_sim_ns=1_000_000, severity=1, **data)
            self.pub.flush()

    def close(self) -> None:
        self.stop_ev.set()
        self.thread.join(2)
        if self.ready is not None:
            self.ready.close()
        self.pub.close()
        self.bus.close()


def _events(st: fakesim.GwStack, h: dict, prefix: str) -> list[dict]:
    r = httpx.get(f"{st.base}/api/events?since=0&types={prefix}", headers=h, timeout=5)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_recorder_event_before_api_subscription_is_backfilled() -> None:
    st = fakesim.GwStack(n=2)
    rec = _Recorder(st.settings.namespace)
    try:
        st.stop_api()
        rec.emit("rec.started", segment=0)  # api 未订阅：只留在 recorder 的 _replay 环里
        rec.ready = rec.bus.ready()
        st.restart_api()  # 新 Gateway：从未见过 recorder，ready（带历史）立即回调 → 探测
        tok = rtc.token(st.base, "viewer", rtc.hint_of("evprobe"))
        h = {"Authorization": f"Bearer {tok['token']}"}
        end = time.monotonic() + 5
        items: list[dict] = []
        while time.monotonic() < end and not items:
            items = _events(st, h, "rec.")
            time.sleep(0.05)
        assert [(e["type"], e["producer"], e["data"].get("segment")) for e in items] == [("rec.started", "recorder", 0)]
        assert st.gw.stats.get("event_probes") == 1
        assert st.gw.events.sub.stats["probe_hits"] == 1
        # 之后的事件按正常路径到达（不重复、无补拉）
        replays = st.gw.events.sub.stats["replays"]
        rec.emit("rec.stopped", reason="user")
        end = time.monotonic() + 5
        while time.monotonic() < end and len(items) < 2:
            items = _events(st, h, "rec.")
            time.sleep(0.05)
        assert [e["type"] for e in items] == ["rec.started", "rec.stopped"]
        assert st.gw.events.sub.stats["replays"] == replays

        # WS：迟到的客户端经 hello.resume（lastEventSeq 0）拿到同样的事件
        async def run() -> None:
            c = await rtc.open_client(st, tok["token"], hello=False)
            await c.hello(resume={"sessionId": c.texts[0]["sessionId"], "lastEventSeq": 0})
            await c.until(lambda k, x: k == "json" and any(e["type"] == "rec.started" for e in c.events()), 3)
            await c.ws.close()

        asyncio.run(run())
    finally:
        rec.close()
        st.close()


def test_unknown_epoch_probe_is_harmless() -> None:
    """纪元猜错（recorder 实际纪元 2，未受监管时按 1 探测）：撤销跟踪，此后的首条事件按首见规则补拉，不丢不重。"""
    st = fakesim.GwStack(n=2)
    rec = _Recorder(st.settings.namespace, epoch=2)
    try:
        st.stop_api()
        rec.emit("rec.started", segment=0)
        rec.ready = rec.bus.ready()
        st.restart_api()
        tok = rtc.token(st.base, "viewer", rtc.hint_of("evprobemiss"))
        h = {"Authorization": f"Bearer {tok['token']}"}
        end = time.monotonic() + 5
        while time.monotonic() < end and st.gw.events.sub.stats["probe_misses"] < 1:
            time.sleep(0.05)
        assert st.gw.events.sub.stats["probe_misses"] == 1
        assert _events(st, h, "rec.") == []
        rec.emit("rec.stopped", reason="user")  # 首见：seq 2 ≤ 1025 → 从 1 起补拉
        end = time.monotonic() + 5
        items: list[dict] = []
        while time.monotonic() < end and len(items) < 2:
            items = _events(st, h, "rec.")
            time.sleep(0.05)
        assert [e["type"] for e in items] == ["rec.started", "rec.stopped"]
    finally:
        rec.close()
        st.close()


def test_supervised_probe_uses_restart_count_epoch() -> None:
    """受监管：等首个 `sys/procs` 回复，按 recorder 的重启次数 + 1（此处 2）探测，命中。"""
    st = fakesim.GwStack(n=2, supervisor=True)
    st.sup.proc_restarts["recorder"] = 1
    rec = _Recorder(st.settings.namespace, epoch=2)
    try:
        st.stop_api()
        rec.emit("rec.started", segment=1)
        rec.ready = rec.bus.ready()
        st.restart_api()
        tok = rtc.token(st.base, "viewer", rtc.hint_of("evprobesup"))
        h = {"Authorization": f"Bearer {tok['token']}"}
        end = time.monotonic() + 6
        items: list[dict] = []
        while time.monotonic() < end and not items:
            items = _events(st, h, "rec.")
            time.sleep(0.05)
        assert [(e["type"], e["data"].get("segment")) for e in items] == [("rec.started", 1)]
        stats = st.gw.events.sub.stats
        assert stats["probe_hits"] == 1 and stats["probe_misses"] == 0
        assert st.gw.events.sub.tracked()["recorder"][0] == 2
    finally:
        rec.close()
        st.close()
