"""空闲窗口发布（ADR-073 第 3 条）：大载荷总线发布移出主循环。

`state/sim-core/ext`（state_ext，2 Hz【墙钟】，N = 1000 时载荷数百 KB）在主循环慢任务内 `put`：zenoh-python 把载荷复制进
ZBytes、分片并写入各订阅者的发送队列，复制与分片的大部分时间持有 GIL。没有远端订阅者时约 0.3 ms；api 与 recorder 都
订阅时生产口径单次 4.2 ms（中位）、最大 6.7 ms（3 个 flight60 客户端并发自测），落在任意一轮上即是一个 4–7 ms 的单步。

做法：主循环只拼接载荷（字节串，交出后只读）并交给本线程；本线程等一个剩余 ≥ 估计耗时 × 1.25 + 余量的主循环空闲窗口
（`IdleGate.wait_slot`，与 checkpoint 写线程同一个门控）再 `put`，FORCE_S【墙钟】内等不到窗口时照常发布（有界时延）。
同一发布者在途时新载荷替换旧载荷（只发最新一份；state_ext 本就是周期全量快照）。线程按 cpuaff 规则落在后台核。
估计耗时取实测上包络（更大时取实测、每次至多翻倍，更小时每次向实测靠拢 10%）。
`put_parts(pub, head, parts)`（ADR-074 第 3 条）：主线程只交出已编码的各段，拼接也在本线程的空闲窗口内完成（计入估计
耗时）。N = 1000 时载荷约 0.6 MB，主线程上的拼接（两次大块分配与复制，本机虚拟机首次触页代价高）生产口径 1–5 ms。
`submit(fn)`（ADR-074 第 2 条）：同一门控下执行一次性的大载荷发送（`fleet/vehicles` 全表回复约 0.7 MB，拼接加 zenoh 回复
在主循环内约 9 ms）；各次提交互不替换。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any

__all__ = ["GatedPublisher"]

log = logging.getLogger("awr.sim.runtime.bgpub")


class _Once:
    """`submit` 的一次性任务（以对象身份为键，不与其他提交替换）。"""

    __slots__ = ("fn",)

    def __init__(self, fn: Any) -> None:
        self.fn = fn


class GatedPublisher:
    MARGIN_NS = 600_000
    FORCE_S = 0.25  # state_ext 周期 0.5 s：最长推迟半个周期

    def __init__(self, gate: Any, *, est_ns: int = 1_000_000) -> None:
        self.gate = gate
        self._cv = threading.Condition(threading.Lock())
        self._pending: dict[int, tuple[Any, Any]] = {}  # id(pub) -> (pub, bytes | (head, parts))
        self._order: deque[int] = deque()
        self._stop = False
        self.est_ns = float(est_ns)
        self.stats: dict[str, Any] = {"puts": 0, "forced": 0, "replaced": 0, "errors": 0, "max_ms": 0.0}
        self.thread = threading.Thread(target=self._run, name="awr-ext-pub", daemon=True)
        self.thread.start()

    def put(self, pub: Any, payload: bytes) -> None:
        """主线程：交出载荷（之后不得再修改）。同一发布者尚未发出的上一份被替换。"""
        self._put(pub, payload)

    def put_parts(self, pub: Any, head: bytes, parts: list[bytes]) -> None:
        """主线程：交出头部与各段（之后不得再修改），由本线程在空闲窗口内拼接为 `head + b"".join(parts)` 后发布。"""
        self._put(pub, (head, parts))

    def submit(self, fn: Any) -> None:
        """主线程：在空闲窗口内执行 `fn()`（一次性发送；不与其他提交互相替换）。"""
        self._put(_Once(fn), None)

    def _put(self, pub: Any, payload: Any) -> None:
        with self._cv:
            k = id(pub)
            if k in self._pending:
                self.stats["replaced"] += 1
            else:
                self._order.append(k)
            self._pending[k] = (pub, payload)
            self._cv.notify()

    def _next(self) -> tuple[Any, Any] | None:
        with self._cv:
            while not self._order and not self._stop:
                self._cv.wait()
            if not self._order:
                return None
            return self._pending.pop(self._order.popleft())

    def _run(self) -> None:
        while True:
            item = self._next()
            if item is None:
                return
            pub, payload = item
            ok = self.gate.wait_slot(self.FORCE_S, need_ns=int(self.est_ns * 1.25) + self.MARGIN_NS)
            t0 = time.perf_counter_ns()
            try:
                if isinstance(pub, _Once):
                    pub.fn()
                else:
                    if isinstance(payload, tuple):
                        head, parts = payload
                        payload = b"".join((head, *parts))
                    pub.put(payload)
            except Exception:
                self.stats["errors"] += 1
                log.exception("gated publish failed")
                continue
            dt = float(time.perf_counter_ns() - t0)
            est = self.est_ns
            self.est_ns = min(dt, 2.0 * est) if dt > est else est + (dt - est) * 0.1
            self.stats["puts"] += 1
            self.stats["forced"] += 0 if ok else 1
            self.stats["max_ms"] = max(self.stats["max_ms"], round(dt / 1e6, 3))

    def close(self, timeout_s: float = 2.0) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self.thread.join(timeout=timeout_s)
