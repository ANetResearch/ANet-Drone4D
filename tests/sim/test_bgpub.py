"""空闲窗口发布（`awr.sim.runtime.bgpub.GatedPublisher`，ADR-073 第 3 条）。

- 有足够长的空闲窗口时在窗口内发布（不计为强制）；
- 没有窗口时 FORCE_S 之后照常发布（有界时延）；
- 同一发布者在途时新载荷替换旧载荷，只发最新一份；不同发布者各发各的。
"""

from __future__ import annotations

import threading
import time

from awr.runtime.checkpoint import IdleGate
from awr.sim.runtime.bgpub import GatedPublisher


class _Pub:
    def __init__(self, gate: IdleGate | None = None) -> None:
        self.got: list[bytes] = []
        self.in_window: list[bool] = []
        self.gate = gate
        self.ev = threading.Event()

    def put(self, payload: bytes) -> None:
        self.got.append(payload)
        self.in_window.append(bool(self.gate is not None and self.gate.is_set()))
        self.ev.set()


def _wait(pred, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.005)
    return False


def test_publishes_inside_idle_window() -> None:
    gate = IdleGate()
    gp = GatedPublisher(gate, est_ns=1_000_000)
    pub = _Pub(gate)
    try:
        gp.put(pub, b"a")
        time.sleep(0.05)
        assert pub.got == []  # 没有窗口：等待
        gate.open(time.monotonic_ns() + 50_000_000)
        assert _wait(lambda: pub.got == [b"a"])
        assert pub.in_window == [True]
        assert gp.stats["puts"] == 1 and gp.stats["forced"] == 0
    finally:
        gate.close()
        gp.close()


def test_short_window_waits_then_forced_after_bound() -> None:
    gate = IdleGate()
    gp = GatedPublisher(gate, est_ns=5_000_000)
    pub = _Pub(gate)
    try:
        gate.open(time.monotonic_ns() + 1_000_000)  # 剩余 1 ms < 5 ms × 1.25 + 0.6 ms
        t0 = time.monotonic()
        gp.put(pub, b"x")
        assert pub.ev.wait(2.0)
        assert time.monotonic() - t0 >= GatedPublisher.FORCE_S * 0.9
        assert gp.stats["forced"] == 1
    finally:
        gate.close()
        gp.close()


def test_latest_payload_replaces_pending_per_publisher() -> None:
    gate = IdleGate()
    gp = GatedPublisher(gate)
    a, b, c = _Pub(), _Pub(), _Pub()
    try:
        gp.put(c, b"c1")  # 线程取走 c1 后在等窗口；其后的载荷在队列中
        assert _wait(lambda: not gp._order)
        gp.put(a, b"a1")
        gp.put(b, b"b1")
        gp.put(a, b"a2")
        gate.open(time.monotonic_ns() + 100_000_000)
        assert _wait(lambda: bool(a.got and b.got and c.got))
        assert a.got == [b"a2"] and b.got == [b"b1"] and c.got == [b"c1"]
        assert gp.stats["replaced"] == 1
    finally:
        gate.close()
        gp.close()


def test_put_parts_joins_on_publisher_thread() -> None:
    """`put_parts`（ADR-074 第 3 条）：头部与各段在发布线程上拼接，载荷与主线程拼接逐字节相同；同一发布者在途时同样替换。"""
    gate = IdleGate()
    gp = GatedPublisher(gate, est_ns=1_000_000)
    pub = _Pub(gate)
    try:
        parts = [b"\x92\x01\xa1a", b"\x92\x02\xa1b"]
        gp.put_parts(pub, b"\x92", parts)
        gate.open(time.monotonic_ns() + 50_000_000)
        assert _wait(lambda: pub.got == [b"\x92" + b"".join(parts)])
        assert gp.stats["puts"] == 1 and gp.stats["errors"] == 0
    finally:
        gate.close()
        gp.close()


def test_submit_runs_once_inside_window_without_replacing() -> None:
    """`submit`（ADR-074 第 2 条）：一次性任务在空闲窗口内执行，多次提交互不替换。"""
    gate = IdleGate()
    gp = GatedPublisher(gate, est_ns=1_000_000)
    got: list[tuple[int, bool]] = []
    try:
        for k in range(3):
            gp.submit(lambda k=k: got.append((k, gate.is_set())))
        time.sleep(0.05)
        assert got == []
        gate.open(time.monotonic_ns() + 200_000_000)
        assert _wait(lambda: len(got) == 3)
        assert [k for k, _ in got] == [0, 1, 2] and all(w for _, w in got)
        assert gp.stats["replaced"] == 0
    finally:
        gate.close()
        gp.close()
