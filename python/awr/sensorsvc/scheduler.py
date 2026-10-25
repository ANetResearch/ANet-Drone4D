"""M25 调度器与背压（M25 §6.14.4、FR-123、FR-124；AWR-05 §5.4.7、ADR-162、ADR-173；D3-AC-17、D3-AC-22）。

三级队列按优先级"交互预览 > 在线闭环 > 离线重渲染"取批，生产窗口内以各级已用 GPU 时间对份额（10% / 60% / 30%）的亏损选级，
同级内按会话轮转；每个会话有在途上限，`submit` 非阻塞，超限的请求返回 641 `SENSOR_BACKPRESSURE`。收到 641 后的行为由会话
模式决定（调用方实现，本模块提供两种调用方辅助）：

- `benchmark`（锁步）：`wait_all` 阻塞调用方（Env 不推进仿真时钟），在途降低后按原 `(uav, sensor, seq)` 重提交，直至全部到期帧
  就绪；不降频、不转离线，观测集合只由 Run 规格决定（ADR-162）；
- `preview`（交互预览）：`PreviewStream` 按 `PreviewRate` 生成预览请求，641 使预览帧率减半（下限 0.5 Hz），连续 5 s 无 641 后
  每 5 s 恢复一级；被拒的预览帧丢弃、不记录。Benchmark 运行的观测墙预览不经本队列（由写入器旁路从交付帧降采样，ADR-173）。

执行方式：`threaded=True` 时由一个工作线程取批执行；`threaded=False` 时在调用方线程内于 `poll` 中执行（GL 上下文只属于创建它的
线程，进程内光栅相机须用此方式）。`delay_s` 为注入的每帧人为延迟（D3-AC-17 背压用例：在途上限 1、50 ms）。生产函数
`produce(batch) -> [(request, frame)]` 由会话提供（M25 worker 后端的进程内替身），同一会话的请求按提交顺序执行。
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from awr.sensorsvc.errors import SensorSvcError

__all__ = ["BATCH_MAX", "CLASSES", "CLASS_OF", "FRAME_WATCHDOG_S", "MODES", "PREVIEW_HZ", "PREVIEW_MIN_HZ", "RECOVER_NS", "SHARES",
           "FrameKey", "PreviewRate", "PreviewStream", "Request", "Scheduler", "Session", "SubmitResult", "WaitStats", "wait_all"]

MODES = ("benchmark", "preview", "offline")
CLASSES = ("preview", "online", "offline")                         # 优先级从高到低
CLASS_OF = {"benchmark": "online", "preview": "preview", "offline": "offline"}
SHARES = {"preview": 0.10, "online": 0.60, "offline": 0.30}       # 生产窗口内 GPU 时间份额上限（AWR-05 §4.7）
BATCH_MAX = 16                                                     # 720p 批上限（M25 §6.14.3）
FRAME_WATCHDOG_S = 60.0                                            # sensorsvc.frame_watchdog_s
PREVIEW_HZ, PREVIEW_MIN_HZ, PREVIEW_MAX_HZ = 2.0, 0.5, 5.0
RECOVER_NS = 5_000_000_000                                         # 连续 5 s 无 641 后每 5 s 恢复一级
NS = 1_000_000_000


class FrameKey(NamedTuple):
    uav: str
    sensor: str
    seq: int


@dataclass(frozen=True)
class Request:
    key: FrameKey
    quality: str                                                   # preview | online | offline
    payload: Any = None


@dataclass
class SubmitResult:
    accepted: list[Request]
    rejected: list[tuple[Request, int]]

    @property
    def backpressure(self) -> bool:
        return bool(self.rejected)


Produce = Callable[[list[Request]], Sequence[tuple[Request, Any]]]


@dataclass
class Session:
    id: str
    mode: str
    cls: str
    cap: int
    produce: Produce
    inflight: int = 0
    max_inflight: int = 0
    submitted: int = 0
    accepted: int = 0
    rejected_641: int = 0
    completed: int = 0
    closed: bool = False
    error: BaseException | None = None
    queue: deque = field(default_factory=deque)
    outbox: list = field(default_factory=list)

    def stats(self) -> dict:
        return {"mode": self.mode, "class": self.cls, "inflight_cap": self.cap, "submitted": self.submitted, "accepted": self.accepted,
                "rejected_641": self.rejected_641, "completed": self.completed, "max_inflight": self.max_inflight}


class Scheduler:
    """一个 M25 worker 的三级队列（M25 §6.14.4 的 `Scheduler`）。"""

    def __init__(self, *, delay_s: float = 0.0, shares: dict[str, float] | None = None, batch_max: int = BATCH_MAX,
                 threaded: bool = True) -> None:
        self.delay_s = float(delay_s)
        self.share = dict(shares or SHARES)
        self.batch_max = int(batch_max)
        self.threaded = threaded
        self.used_s = dict.fromkeys(CLASSES, 0.0)
        self.counts = {c: {"submitted": 0, "accepted": 0, "rejected_641": 0, "completed": 0} for c in CLASSES}
        self.sessions: dict[str, Session] = {}
        self._rr: dict[str, deque[str]] = {c: deque() for c in CLASSES}
        self._cv = threading.Condition()
        self._stop = False
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._loop, name="sensorsvc-worker", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ 会话
    def open(self, session_id: str, mode: str, *, produce: Produce, inflight_cap: int) -> Session:
        if mode not in MODES:
            raise SensorSvcError(640, f"session mode {mode!r}")
        if inflight_cap < 1:
            raise SensorSvcError(640, f"inflight cap must be >= 1, got {inflight_cap}")
        with self._cv:
            if session_id in self.sessions:
                raise SensorSvcError(640, f"session {session_id!r} is already open")
            s = Session(session_id, mode, CLASS_OF[mode], int(inflight_cap), produce)
            self.sessions[session_id] = s
            self._rr[s.cls].append(session_id)
            return s

    def close_session(self, sess: Session) -> dict:
        with self._cv:
            sess.closed = True
            sess.queue.clear()
            self.sessions.pop(sess.id, None)
            if sess.id in self._rr[sess.cls]:
                self._rr[sess.cls].remove(sess.id)
            return sess.stats()

    # ------------------------------------------------------------------ 提交与取回
    def submit(self, sess: Session, reqs: Sequence[Request]) -> SubmitResult:
        """非阻塞：按顺序接受至在途上限，其余返回 641（不排队、不改变会话的任何参数）。"""
        acc, rej = [], []
        with self._cv:
            if sess.closed:
                raise SensorSvcError(640, f"session {sess.id!r} is closed")
            for r in reqs:
                if r.quality != sess.cls:
                    raise SensorSvcError(642, f"request quality {r.quality!r} does not match the {sess.mode} session class {sess.cls!r}")
                sess.submitted += 1
                self.counts[sess.cls]["submitted"] += 1
                if sess.inflight >= sess.cap:
                    rej.append((r, 641))
                    continue
                sess.queue.append(r)
                sess.inflight += 1
                sess.max_inflight = max(sess.max_inflight, sess.inflight)
                acc.append(r)
            sess.accepted += len(acc)
            sess.rejected_641 += len(rej)
            self.counts[sess.cls]["accepted"] += len(acc)
            self.counts[sess.cls]["rejected_641"] += len(rej)
            if acc:
                self._cv.notify_all()
        return SubmitResult(acc, rej)

    def poll(self, sess: Session, timeout: float | None = 0.0) -> list[tuple[Request, Any]]:
        """取回已完成的帧（完成顺序）。线程模式下最多等待 `timeout` 秒至少一帧；同步模式下在本线程内执行批直至有帧或队列空。"""
        if not self.threaded:
            while not sess.outbox and sess.error is None and self._pending():
                self.step()
        with self._cv:
            if self.threaded and not sess.outbox and sess.error is None and timeout:
                self._cv.wait_for(lambda: bool(sess.outbox) or sess.error is not None or self._stop, timeout)
            if sess.error is not None:
                err, sess.error = sess.error, None
                raise SensorSvcError(648, f"session {sess.id}: frame production failed: {err!r}") from err
            out, sess.outbox = sess.outbox, []
            return out

    # ------------------------------------------------------------------ 执行
    def _pending(self) -> bool:
        return any(s.queue for s in self.sessions.values())

    def _next_batch(self) -> tuple[Session, list[Request]] | None:
        """加权亏损选级（已用 GPU 时间 / 份额最小者，同值按优先级），同级内会话轮转；调用方持锁。"""
        live = [c for c in CLASSES if any(self.sessions[i].queue for i in self._rr[c])]
        if not live:
            return None
        cls = min(live, key=lambda c: (self.used_s[c] / max(self.share.get(c, 0.0), 1e-9), CLASSES.index(c)))
        rr = self._rr[cls]
        for _ in range(len(rr)):
            sid = rr[0]
            rr.rotate(-1)
            s = self.sessions[sid]
            if s.queue:
                n = min(self.batch_max, len(s.queue))
                return s, [s.queue.popleft() for _ in range(n)]
        return None

    def step(self) -> bool:
        """执行一个批；无待执行请求时返回 False。"""
        with self._cv:
            nb = self._next_batch()
        if nb is None:
            return False
        sess, batch = nb
        t0 = time.perf_counter()
        try:
            res = list(sess.produce(batch))
            if self.delay_s > 0:
                time.sleep(self.delay_s * len(batch))
        except Exception as e:                                 # 生产失败转交调用方（648）
            with self._cv:
                sess.inflight -= len(batch)
                sess.error = e
                self._cv.notify_all()
            return True
        with self._cv:
            self.used_s[sess.cls] += time.perf_counter() - t0
            sess.inflight -= len(batch)
            sess.completed += len(res)
            self.counts[sess.cls]["completed"] += len(res)
            if not sess.closed:
                sess.outbox.extend(res)
            self._cv.notify_all()
        return True

    def drain(self) -> int:
        """同步模式：执行全部待执行请求，返回批数。"""
        n = 0
        while self.step():
            n += 1
        return n

    def _loop(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._stop or self._pending())
                if self._stop:
                    return
            self.step()

    def stats(self) -> dict:
        with self._cv:
            return {"used_s": dict(self.used_s), "classes": {c: dict(v) for c, v in self.counts.items()}}

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread is not None:
            self._thread.join(10)
            self._thread = None


# ---------------------------------------------------------------------- Benchmark 锁步
@dataclass
class WaitStats:
    requests: int = 0
    rejected_641: int = 0
    resubmits: int = 0
    wait_s: float = 0.0                                            # 本次等待的墙钟（仿真时钟在此期间不推进）
    backpressure_s: float = 0.0                                    # 其中自首个 641 至全部键被接受的墙钟


def wait_all(sched: Scheduler, sess: Session, reqs: Sequence[Request], *, watchdog_s: float = FRAME_WATCHDOG_S
             ) -> tuple[dict[FrameKey, Any], WaitStats]:
    """Benchmark 锁步（M25 §6.14.5、§7.1.3 `wait_all`）：提交全部到期帧（按键排序），641 的键在在途降低后按原键重提交，全部就绪
    才返回；调用方在返回前不推进仿真时钟。不降频、不转离线、不丢帧。单帧在途超过看门狗时以 648 结束。"""
    if sess.mode != "benchmark":
        raise SensorSvcError(640, f"wait_all is the benchmark lockstep path, session mode is {sess.mode!r}")
    pending = sorted(reqs, key=lambda r: r.key)
    if len({r.key for r in pending}) != len(pending):
        raise SensorSvcError(642, "duplicate (uav, sensor, seq) keys in one lockstep wait")
    st = WaitStats(requests=len(pending))
    got: dict[FrameKey, Any] = {}
    t0 = t_last = time.perf_counter()
    t_bp: float | None = None
    first = True
    while len(got) < st.requests:
        if pending:
            r = sched.submit(sess, pending)
            if not first and r.accepted:
                st.resubmits += len(r.accepted)
            first = False
            pending = [q for q, _ in r.rejected]
            st.rejected_641 += len(r.rejected)
            if pending and t_bp is None:
                t_bp = time.perf_counter()
            if not pending and t_bp is not None:
                st.backpressure_s += time.perf_counter() - t_bp
                t_bp = None
        out = sched.poll(sess, timeout=min(watchdog_s, 1.0))
        now = time.perf_counter()
        if out:
            t_last = now
        elif now - t_last > watchdog_s or (not sched.threaded and not pending and sess.inflight == 0):
            raise SensorSvcError(648, f"session {sess.id}: {st.requests - len(got)} frame(s) not delivered within {watchdog_s:.0f} s")
        for q, fr in out:
            got[q.key] = fr
    if t_bp is not None:
        st.backpressure_s += time.perf_counter() - t_bp
    st.wait_s = time.perf_counter() - t0
    return got, st


# ---------------------------------------------------------------------- 交互预览
class PreviewRate:
    """交互预览帧率（M25 FR-124、§6.14.4）：基准 2 Hz（可配 ≤ 5 Hz）；每次 641 降一级（减半，下限 0.5 Hz）；
    最近一次 641 之后连续 5 s 无 641 时恢复一级，此后每 5 s 再恢复一级，直至基准帧率。时刻为调用方给出的单调纳秒。"""

    def __init__(self, base_hz: float = PREVIEW_HZ, *, min_hz: float = PREVIEW_MIN_HZ, recover_ns: int = RECOVER_NS) -> None:
        if not (0 < min_hz <= base_hz <= PREVIEW_MAX_HZ):
            raise SensorSvcError(649, f"preview rate {base_hz} Hz outside [{min_hz}, {PREVIEW_MAX_HZ}]")
        self.base_hz, self.min_hz, self.recover_ns = float(base_hz), float(min_hz), int(recover_ns)
        self.max_level = math.floor(math.log2(self.base_hz / self.min_hz) + 1e-9)
        self.level = 0
        self._calm_ns: int | None = None                           # 下一次可恢复一级的计时起点
        self.events: list[tuple[int, str, float]] = []            # (t_ns, "down" | "up", 新帧率)

    def hz(self) -> float:
        return max(self.base_hz / (1 << self.level), self.min_hz)

    def on_backpressure(self, now_ns: int) -> None:
        if self.level < self.max_level:
            self.level += 1
            self.events.append((int(now_ns), "down", self.hz()))
        self._calm_ns = int(now_ns)

    def update(self, now_ns: int) -> float:
        while self.level > 0 and self._calm_ns is not None and now_ns - self._calm_ns >= self.recover_ns:
            self._calm_ns += self.recover_ns
            self.level -= 1
            self.events.append((self._calm_ns, "up", self.hz()))
        return self.hz()


class PreviewStream:
    """交互预览会话的调用方（内部工作区观测墙的交互预览模式，M25 UC-10）：每个预览周期为各 `(uav, sensor)` 提交一帧
    `preview` 请求；641 时降频，负载回落后恢复；被拒的帧丢弃。预览帧不进入运行记录。"""

    def __init__(self, sched: Scheduler, session_id: str, sources: Sequence[tuple[str, str]], *, produce: Produce,
                 rate: PreviewRate | None = None, inflight_cap: int | None = None) -> None:
        self.sched = sched
        self.sources = list(sources)
        self.rate = rate or PreviewRate()
        cap = inflight_cap or max(1, math.ceil(2 * self.rate.base_hz)) * len(self.sources)    # 预览：2 × 预览帧率
        self.sess = sched.open(session_id, "preview", produce=produce, inflight_cap=cap)
        self.seq = 0
        self.next_ns: int | None = None
        self.dropped = 0
        self.delivered = 0

    def pump(self, now_ns: int) -> SubmitResult | None:
        """到达下一预览时刻时提交一轮请求；返回本次提交结果（未到时刻返回 None）。"""
        hz = self.rate.update(now_ns)
        if self.next_ns is not None and now_ns < self.next_ns:
            return None
        reqs = [Request(FrameKey(u, s, self.seq), "preview") for u, s in self.sources]
        self.seq += 1
        r = self.sched.submit(self.sess, reqs)
        if r.rejected:
            self.dropped += len(r.rejected)
            self.rate.on_backpressure(now_ns)
            hz = self.rate.hz()
        self.next_ns = int(now_ns + round(NS / hz))
        return r

    def frames(self) -> list[tuple[Request, Any]]:
        out = self.sched.poll(self.sess, timeout=0.0)
        self.delivered += len(out)
        return out

    def close(self) -> dict:
        return {**self.sched.close_session(self.sess), "dropped": self.dropped, "delivered": self.delivered, "hz": self.rate.hz(),
                "rate_events": list(self.rate.events)}
