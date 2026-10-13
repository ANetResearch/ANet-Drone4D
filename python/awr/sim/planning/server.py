"""沙盒共享规划服务端核心 `PlanServer`（M10-FR-203、FR-204、FR-212；M10 §15.3.2、§15.5 算法 3、§15.6.1；ADR-113；
AWR-04 §4.9、§11.5；M17-FR-032 至 FR-034 的宿主契约）。

由 sandbox-pool（M17 `planhost`）宿主：全部沙盒会话共用 W 个（缺省 1，可配 2）可杀 worker 进程，作业按会话轮转的
公平队列调度。本模块是库：不拥有总线与 cgroup，宿主用 `attach_bus(sid, bus)` 在每个会话命名空间内声明
`ctl/plan/submit` 与 `ctl/plan/cancel`（17 §17.10.1），或直接调用 `submit`/`cancel`/`drop_session`。

| 规则 | 实现 |
|---|---|
| 准入 | 已冻结 → 586（`retry_after_s`）；类别未登记 → 300 UNKNOWN_KIND；`validate_input` 不通过 → 585（列出超限项）；会话在途与排队合计已达 8 → 111；同会话同 `dedupe_key`（或同 job_id）的旧作业被取代（回复 superseded） |
| 调度 | 有空闲 worker 时，从轮转指针的下一个"有待执行作业且未冻结"的会话取（优先级升序、提交序升序）的第一个作业 |
| 超时 | 作业计时从 worker 装载完世界（`begin`）起，墙钟超过 `预算 × 3`（预算取作业给出值，且不超过登记预算的 3 倍）即 SIGKILL 该 worker 并重建（≤ 5 s；numba 核在就绪前装载）；作业回复 `125 PLAN_FAILED`（detail PLAN_TIMEOUT）；同会话 10 min 内 3 次超时 → 冻结 5 min，冻结时排队中的作业一并回复 586 |
| 崩溃 | worker 意外退出：作业首次崩溃重新排到本会话队首，第二次回复 `214 PLANNER_CRASHED` |
| 回复 | `{v, job_id, status, code, detail, result_sha256, result 或 result_path + result_bytes_sha256}`；结果编码由 worker 完成（`PlanResult.to_bytes`），> 1 MB 时写入 `<shm_root>/<sid>/plan/<job>.msgpack`（0600，原子改名） |
| 期限 | wire 提交按客户端 query 超时（`query_timeout_s(budget)`）记回复期限；排队超过期限的作业不再执行（125 QUEUE_EXPIRED，服务不可用时 213） |
| 世界 | worker 以 memmap 只读打开派生缓存与 Grid25，世界 LRU ≤ 4；会话结束后其余会话不再使用的世界由 worker 解除映射（只映射活动世界）；公开模式 `allow_derive = false` 缓存缺失以 123（derived_cache_missing）失败 |

线程模型：一个调度线程独占 worker 管道（`multiprocessing.connection.wait` + 自管道唤醒）；`submit`、`cancel`、
`drop_session`、`stats` 可在任意线程调用（一把锁）；`submit_wire` 只入队（总线回调线程可用），解码与准入在调度线程。
回复回调在锁外、在调度线程或调用线程中执行，只允许把回复交给总线（`Request.reply_msg`）或入队。

确定性：调度只改变作业的执行先后与结果的生效 tick，不改变单个作业的结果（M10-NFR-202）。
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import itertools
import json
import logging
import os
import re
import signal
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from multiprocessing.connection import wait as mp_wait
from pathlib import Path
from typing import Any

import msgpack

from .jobs import (
    TIMEOUT_FACTOR,
    WIRE_V,
    PlanRequest,
    ensure_plan_kinds,
    get_plan_kind,
    query_timeout_s,
    validate_request,
)

__all__ = ["PlanServer", "Ticket"]

log = logging.getLogger("awr.sim.planning.server")

ReplyFn = Callable[[dict], Any]

QUEUE_MAX = 8                         # 每会话在途 + 排队（M10 §15.4；M17-FR-032）
FREEZE_WINDOW_S = 600.0               # 10 min 内
FREEZE_COUNT = 3                      # 3 次超时
FREEZE_S = 300.0                      # 冻结 5 min
RESULT_INLINE_MAX = 1 << 20           # 结果载荷外置阈值 1 MB
READY_TIMEOUT_S = 60.0                # worker 启动（spawn + import + numba 核装载）超过此时长视为失败并重建
WORLD_LOAD_LIMIT_S = 30.0             # 作业的世界装载阶段上限（服务成本，不计入作业预算；冷派生缓存热打开 ≤ 0.3 s）
RESPAWN_BACKOFF_MAX_S = 5.0
UNKNOWN_BUDGET_CAP_MS = 10_000
POLL_MAX_S = 0.05
SID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


class Ticket:
    """一次提交的回执：`status` 为 QUEUED 或终态回复的 status；`wait()` 等待终态回复。"""

    __slots__ = ("_ev", "code", "detail", "job_id", "reply", "sid", "status")

    def __init__(self, sid: str, job_id: str) -> None:
        self.sid = sid
        self.job_id = job_id
        self.status = "QUEUED"
        self.code = 0
        self.detail: str | None = None
        self.reply: dict | None = None
        self._ev = threading.Event()

    def _set(self, reply: dict) -> None:
        self.reply = reply
        self.status = str(reply.get("status"))
        self.code = int(reply.get("code") or 0)
        self.detail = reply.get("detail")
        self._ev.set()

    @property
    def done(self) -> bool:
        return self._ev.is_set()

    def wait(self, timeout: float | None = None) -> dict | None:
        self._ev.wait(timeout)
        return self.reply

    def __repr__(self) -> str:  # pragma: no cover
        return f"Ticket({self.sid}, {self.job_id}, {self.status}, {self.code})"


class _Inbox:
    """跨线程入队：总线回调线程只调用 `put`（追加并写自管道唤醒调度线程，PY-CB-01）。"""

    __slots__ = ("fd", "q")

    def __init__(self, wake_fd: int) -> None:
        self.q: deque = deque()
        self.fd = wake_fd

    def put(self, item: tuple) -> None:
        self.q.append(item)
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.write(self.fd, b"x")


@dataclass(eq=False)
class _SJob:
    sid: str
    req: PlanRequest
    seq: int
    ticket: Ticket
    reply_cb: ReplyFn | None
    budget_ms: int
    t_rx: float
    deadline: float | None
    state: str = "QUEUED"             # QUEUED、RUNNING、DONE、TIMEOUT、FAILED、CANCELED、SUPERSEDED
    retries: int = 0
    t_start: float = 0.0
    replied: bool = False
    phase: str = "load"               # load（worker 装载世界）→ run（计时 3 × 预算）

    @property
    def key(self) -> tuple[int, int]:
        return (int(self.req.priority), self.seq)


@dataclass
class _Sess:
    sid: str
    heap: list = field(default_factory=list)              # (prio, seq, job)
    jobs: dict = field(default_factory=dict)              # job_id → _SJob（未回复的）
    latest: dict = field(default_factory=dict)            # dedupe_key → job_id
    timeouts: deque = field(default_factory=deque)        # 超时时刻（单调秒）
    frozen_until: float = 0.0
    worlds: set = field(default_factory=set)              # 本会话作业用过的 world_id（会话结束后据此解除映射）
    n: dict = field(default_factory=lambda: {"submitted": 0, "done": 0, "timeouts": 0, "rejected": 0, "crashes": 0,
                                             "superseded": 0, "canceled": 0, "expired": 0, "freezes": 0})

    def active(self) -> int:
        return sum(1 for j in self.jobs.values() if j.state in ("QUEUED", "RUNNING") and not j.replied)


@dataclass(eq=False)
class _Worker:
    idx: int
    proc: Any = None
    conn: Any = None
    state: str = "dead"               # starting、idle、busy、dead
    job: _SJob | None = None
    pid: int = 0
    t_spawn: float = 0.0
    t_ready: float = 0.0
    n_jobs: int = 0
    n_spawn: int = 0
    backoff_s: float = 0.0
    t_next_spawn: float = 0.0


class PlanServer:
    def __init__(self, *, workers: int = 1, worlds_dir: str | os.PathLike | None, allow_derive: bool,
                 cgroup_leaf: str | None = None, cache_root: str | os.PathLike | None = None, lru_worlds: int = 4,
                 queue_max: int = QUEUE_MAX, shm_root: str | os.PathLike = "/dev/shm/awr",
                 result_inline_max: int = RESULT_INLINE_MAX, nice: int = 10, freeze_window_s: float = FREEZE_WINDOW_S,
                 freeze_count: int = FREEZE_COUNT, freeze_s: float = FREEZE_S, autostart: bool = True) -> None:
        self.n_workers = max(1, int(workers))
        self.cfg = {"worlds_dir": str(worlds_dir) if worlds_dir else None, "allow_derive": bool(allow_derive),
                    "cgroup_leaf": cgroup_leaf, "cache_root": str(cache_root) if cache_root else None,
                    "lru_worlds": int(lru_worlds), "nice": int(nice), "parent_pid": os.getpid()}
        self.queue_max = int(queue_max)
        self.shm_root = Path(shm_root)
        self.result_inline_max = int(result_inline_max)
        self.freeze_window_s = float(freeze_window_s)
        self.freeze_count = int(freeze_count)
        self.freeze_s = float(freeze_s)
        self._lock = threading.RLock()
        self._sess: dict[str, _Sess] = {}
        self._order: list[str] = []               # 轮转顺序（会话首次提交的先后）
        self._rr = -1
        self._seq = itertools.count()
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._inbox = _Inbox(self._wake_w)        # 跨线程入队：(op, sid, 请求体, 回复函数, t_rx, 总线)
        self._workers = [_Worker(i) for i in range(self.n_workers)]
        self._handles: dict[str, list] = {}
        self._thread: threading.Thread | None = None
        self._stop = False
        self._pending_cbs: list[tuple[ReplyFn | None, Ticket | None, dict]] = []
        self._retain: set[str] | None = None        # 待下发给 worker 的活动世界集合（会话结束后）
        self.counters = {"dispatched": 0, "done": 0, "timeouts": 0, "crashes": 0, "rebuilds": 0, "kills": 0,
                         "rebuild_ms_last": 0.0, "rebuild_ms_max": 0.0, "result_files": 0}
        ensure_plan_kinds()
        if autostart:
            self.start()

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop = False
            for w in self._workers:
                self._spawn(w)
            self._thread = threading.Thread(target=self._loop, name="plan-server", daemon=True)
            self._thread.start()

    def close(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop = True
            for s in self._sess.values():
                for j in list(s.jobs.values()):
                    self._reply(j, "error", 213, "PLAN_SERVICE_STOPPED", state="CANCELED")
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        for w in self._workers:
            self._stop_worker(w, graceful=True)
        for h in [h for hs in self._handles.values() for h in hs]:
            with contextlib.suppress(Exception):
                h.close()
        self._handles.clear()
        self._flush_cbs()
        self._inbox.fd = -1                         # 迟到的总线回调不再写已关闭（可能被复用）的描述符
        for fd in (self._wake_r, self._wake_w):
            with contextlib.suppress(OSError):
                os.close(fd)
        self._wake_r = self._wake_w = -1

    def __enter__(self) -> PlanServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _wake(self) -> None:
        if self._wake_w >= 0:
            with contextlib.suppress(OSError):
                os.write(self._wake_w, b"x")

    # ------------------------------------------------------------ 提交与取消
    def submit(self, sid: str, req: PlanRequest, on_done: ReplyFn | None = None, *,
               deadline_s: float | None = None) -> Ticket:
        """入队或立即拒绝（585、586、111、300）。`on_done(reply)` 在终态回复时调用一次；`deadline_s` 为回复期限
        （单调秒，缺省不设）。"""
        _check_sid(sid)
        with self._lock:
            t = self._admit(sid, req, on_done, deadline_s)
        self._wake()
        self._flush_cbs()
        return t

    def submit_wire(self, sid: str, msg: bytes | dict, reply: ReplyFn) -> None:
        """总线入口（`ctl/plan/submit` 请求体，原始字节或已解码 dict）：只入队，解码与准入在调度线程。"""
        _check_sid(sid)
        self._inbox.put(("submit", sid, msg, reply, time.monotonic(), None))

    def cancel_wire(self, sid: str, msg: bytes | dict, reply: ReplyFn | None) -> None:
        _check_sid(sid)
        self._inbox.put(("cancel", sid, msg, reply, time.monotonic(), None))

    def cancel(self, sid: str, job_id: str) -> bool:
        """取消（幂等）：排队中移出、执行中的结果丢弃；原提交以 status = canceled 回复。"""
        with self._lock:
            s = self._sess.get(sid)
            j = s.jobs.get(job_id) if s is not None else None
            ok = j is not None and not j.replied
            if ok:
                s.n["canceled"] += 1
                self._reply(j, "canceled", 0, "CANCELED", state="CANCELED")
        self._flush_cbs()
        return ok

    def drop_session(self, sid: str) -> None:
        """会话结束：清空队列（回复 canceled，detail SESSION_ENDED），在途作业的结果丢弃，删除外置结果目录。"""
        with self._lock:
            s = self._sess.pop(sid, None)
            if sid in self._order:
                i = self._order.index(sid)
                self._order.pop(i)
                if self._rr >= i:
                    self._rr -= 1
            if s is not None:
                for j in list(s.jobs.values()):
                    self._reply(j, "canceled", 0, "SESSION_ENDED", state="CANCELED")
                if s.worlds:                                # 只映射活动世界：其余会话不再用的世界由 worker 解除映射
                    self._retain = set().union(*(x.worlds for x in self._sess.values()))
            hs = self._handles.pop(sid, [])
        for h in hs:
            with contextlib.suppress(Exception):
                h.close()
        self._flush_cbs()
        d = self.shm_root / sid / "plan"
        if d.is_dir():
            for p in d.glob("*.msgpack*"):
                with contextlib.suppress(OSError):
                    p.unlink()

    # ------------------------------------------------------------ 总线宿主
    def attach_bus(self, sid: str, bus: Any, *, key_prefix: str | None = None) -> list:
        """声明本会话的 `ctl/plan/submit` 与 `ctl/plan/cancel`（M17 宿主调用；17 §17.10.1）。`bus` 为该会话命名空间
        （`awr/<world>/<sid>`）的总线时用相对 key；宿主用一个无前缀会话为全部会话声明时传 `key_prefix` = 会话命名空间
        （绝对 key）。切换底图或会话结束时调用 `detach_bus(sid)`。请求离开总线的 5 s 未回复清扫：规划 query 可持续
        预算 × 3 + 5 s。"""
        from awr.contracts import bus_keys

        _check_sid(sid)

        def key(op: str) -> str:
            rel = bus_keys.ctl_plan(op)
            return f"{key_prefix.rstrip('/')}/{rel}" if key_prefix else rel

        inbox = self._inbox

        def on_submit(req: Any) -> None:            # 总线回调线程：只入队（PY-CB-01）；解码、准入与清扫豁免在调度线程
            inbox.put(("submit", sid, req, None, None, bus))

        def on_cancel(req: Any) -> None:
            inbox.put(("cancel", sid, req, None, None, bus))

        hs = [bus.serve(key("submit"), on_submit), bus.serve(key("cancel"), on_cancel)]
        with self._lock:
            self._handles.setdefault(sid, []).extend(hs)
        return hs

    def detach_bus(self, sid: str, *, drop: bool = True) -> None:
        """撤销本会话的 queryable；`drop`（缺省）同时按会话结束处理队列（切换底图后旧作业不再有意义）。"""
        if drop:
            self.drop_session(sid)
            return
        with self._lock:
            hs = self._handles.pop(sid, [])
        for h in hs:
            with contextlib.suppress(Exception):
                h.close()

    # ------------------------------------------------------------ 统计
    def stats(self) -> dict:
        now = time.monotonic()
        with self._lock:
            sess = {}
            for sid in self._order:
                s = self._sess[sid]
                sess[sid] = {"queued": sum(1 for j in s.jobs.values() if j.state == "QUEUED" and not j.replied),
                             "running": sum(1 for j in s.jobs.values() if j.state == "RUNNING" and not j.replied),
                             "frozen_s": round(max(0.0, s.frozen_until - now), 3),
                             "recent_timeouts": len(s.timeouts)} | dict(s.n)
            return {"workers": [{"idx": w.idx, "pid": w.pid, "state": w.state, "jobs": w.n_jobs, "spawns": w.n_spawn,
                                 "job": w.job.req.job_id if w.job is not None else None} for w in self._workers],
                    "sessions": sess} | dict(self.counters)

    def worker_pids(self) -> list[int]:
        with self._lock:
            return [w.pid for w in self._workers if w.pid and w.state != "dead"]

    def wait_ready(self, timeout: float = 30.0) -> bool:
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            with self._lock:
                if all(w.state in ("idle", "busy") for w in self._workers):
                    return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------ 准入（持锁）
    def _session(self, sid: str) -> _Sess:
        s = self._sess.get(sid)
        if s is None:
            s = self._sess[sid] = _Sess(sid)
            self._order.append(sid)
        return s

    def _admit(self, sid: str, req: PlanRequest, cb: ReplyFn | None, deadline: float | None) -> Ticket:
        now = time.monotonic()
        t = Ticket(sid, req.job_id)
        s = self._session(sid)
        s.n["submitted"] += 1
        if req.world_key:
            s.worlds.add(str(req.world_key[0]))
        spec = get_plan_kind(req.kind)
        cap = spec.budget_ms * TIMEOUT_FACTOR if spec is not None else UNKNOWN_BUDGET_CAP_MS
        j = _SJob(sid, req, next(self._seq), t, cb, max(1, min(int(req.budget_ms), int(cap))), now, deadline)

        def reject(status: str, code: int, detail: str, **extra: Any) -> Ticket:
            s.n["rejected"] += 1
            self._reply(j, status, code, detail, state="FAILED", **extra)
            return t

        if self._stop:
            return reject("error", 213, "PLAN_SERVICE_STOPPED")
        if now < s.frozen_until:
            return reject("error", 586, "PLAN_THROTTLED", retry_after_s=round(s.frozen_until - now, 3))
        if spec is None:
            return reject("error", 300, "UNKNOWN_KIND", remedy=f"未登记的规划作业类别 {str(req.kind)[:64]!r}")
        rej = validate_request(req)
        if rej is not None:
            return reject(rej.status, rej.code, str(rej.detail), remedy=rej.remedy,
                          quota=(rej.stats or {}).get("quota"))
        old_ids = {s.latest.get(req.dedupe_key), req.job_id} - {None}
        for oid in old_ids:
            o = s.jobs.get(oid)
            if o is not None and not o.replied:
                s.n["superseded"] += 1
                self._reply(o, "superseded", 0, "SUPERSEDED", state="SUPERSEDED")
        if s.active() >= self.queue_max:
            return reject("error", 111, "RATE_LIMITED", remedy=f"每会话在途与排队作业 ≤ {self.queue_max}")
        s.jobs[req.job_id] = j
        s.latest[req.dedupe_key] = req.job_id
        heapq.heappush(s.heap, (j.key[0], j.key[1], j))
        return t

    # ------------------------------------------------------------ 回复（持锁登记，锁外回调）
    def _reply(self, j: _SJob, status: str, code: int, detail: str | None, *, state: str, **extra: Any) -> None:
        if j.replied:
            return
        j.replied = True
        j.state = state
        msg: dict = {"v": WIRE_V, "job_id": j.req.job_id, "status": status, "code": int(code)}
        if detail is not None:
            msg["detail"] = detail
        msg.update({k: v for k, v in extra.items() if v is not None})
        s = self._sess.get(j.sid)
        if s is not None and s.jobs.get(j.req.job_id) is j:
            s.jobs.pop(j.req.job_id, None)
            if s.latest.get(j.req.dedupe_key) == j.req.job_id:
                s.latest.pop(j.req.dedupe_key, None)
        self._pending_cbs.append((j.reply_cb, j.ticket, msg))

    def _flush_cbs(self) -> None:
        with self._lock:
            cbs, self._pending_cbs = self._pending_cbs, []
        for cb, t, msg in cbs:
            if t is not None:
                t._set(msg)
            if cb is not None:
                try:
                    cb(msg)
                except Exception:
                    log.warning("plan reply failed", extra={"kv": {"job": msg.get("job_id")}}, exc_info=True)

    # ------------------------------------------------------------ worker 进程
    def _spawn(self, w: _Worker) -> None:
        """以独立解释器启动 worker（`python -c`，经 socketpair 上的 multiprocessing Connection 通信）：不重新导入宿主的
        `__main__`（sandbox-pool 的事件循环与总线不进入 worker 的匿名页），宿主也无需 `if __name__ == "__main__"` 守护。"""
        import socket
        import subprocess
        import sys
        from multiprocessing.connection import Connection

        a, b = socket.socketpair()
        env = dict(os.environ)
        env.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
                   AWR_PLAN_WORKER_CFG=json.dumps(self.cfg))
        root = str(Path(__file__).resolve().parents[3])          # python/（awr 包的父目录）
        env["PYTHONPATH"] = os.pathsep.join([root, *[x for x in env.get("PYTHONPATH", "").split(os.pathsep) if x]])
        code = f"from awr.sim.planning.server import _worker_cli; _worker_cli({b.fileno()})"
        try:
            p = subprocess.Popen([sys.executable, "-c", code], pass_fds=[b.fileno()], env=env,
                                 stdin=subprocess.DEVNULL, close_fds=True)
        finally:
            b.close()
        w.proc, w.conn, w.pid = p, Connection(a.detach()), int(p.pid)
        w.state, w.job = "starting", None
        w.t_spawn = time.monotonic()
        w.n_spawn += 1

    def _stop_worker(self, w: _Worker, *, graceful: bool = False) -> None:
        p, c = w.proc, w.conn
        if graceful and c is not None:
            with contextlib.suppress(Exception):
                c.send(("stop",))
        if p is not None:
            if graceful:
                with contextlib.suppress(Exception):
                    p.wait(0.5)
            if p.poll() is None:
                with contextlib.suppress(OSError):
                    os.kill(p.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    p.wait(2.0)
        if c is not None:
            with contextlib.suppress(Exception):
                c.close()
        w.proc = w.conn = None
        w.state = "dead"

    def _kill(self, w: _Worker) -> None:
        self.counters["kills"] += 1
        self._stop_worker(w, graceful=False)
        self._respawn(w)

    def _respawn(self, w: _Worker) -> None:
        """标记重建：曾就绪过的 worker（超时强杀、作业中崩溃）立即重建；启动期间就退出的按指数退避（≤ 5 s），
        避免插件导入错误等造成的重启风暴。"""
        now = time.monotonic()
        w.state = "dead"
        w.job = None
        if w.t_ready >= w.t_spawn > 0:
            w.backoff_s = 0.0
            w.t_next_spawn = now
        else:
            w.backoff_s = min(RESPAWN_BACKOFF_MAX_S, max(0.2, w.backoff_s * 2))
            w.t_next_spawn = now + w.backoff_s
        self.counters["rebuilds"] += 1

    # ------------------------------------------------------------ 调度线程
    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._stop:
                    break
            try:
                self._step()
            except Exception:
                log.exception("plan server loop error")
                time.sleep(0.05)
            self._flush_cbs()

    def _step(self) -> None:
        self._drain_inbox()
        with self._lock:
            conns = {w.conn: w for w in self._workers if w.conn is not None}
            timeout = self._next_wakeup()
        ready = mp_wait([*conns, self._wake_r], timeout)
        if self._wake_r in ready:
            with contextlib.suppress(OSError):
                while os.read(self._wake_r, 4096):
                    pass
        with self._lock:
            for c in ready:
                w = conns.get(c)
                if w is not None and w.conn is c:
                    self._on_worker_msg(w)
            self._check_workers()
            self._send_retain()
            self._check_timeouts()
            self._expire_queued()
            self._dispatch()

    def _next_wakeup(self) -> float:
        now = time.monotonic()
        t = POLL_MAX_S
        for w in self._workers:
            if w.state == "busy" and w.job is not None:
                t = min(t, w.job.t_start + self._limit_s(w.job) - now)
            elif w.state == "dead":
                t = min(t, w.t_next_spawn - now)
        return max(0.0, t)

    def _drain_inbox(self) -> None:
        q = self._inbox.q
        while q:
            op, sid, raw, reply, t_rx, bus = q.popleft()
            if bus is not None:                       # 总线请求：回复经 Request.reply_msg
                req_obj = raw
                if op == "submit":                    # 移出总线的 5 s 未回复清扫：规划 query 可持续 预算 × 3 + 5 s
                    untrack = getattr(bus, "_untrack", None)
                    if untrack is not None:
                        with contextlib.suppress(Exception):
                            untrack(req_obj)
                raw, reply = req_obj.payload, req_obj.reply_msg
                t_rx = float(getattr(req_obj, "t_rx", 0.0) or time.monotonic())
            try:
                msg = (msgpack.unpackb(raw, raw=False, strict_map_key=False)
                       if isinstance(raw, (bytes, bytearray, memoryview)) else dict(raw))
            except Exception:
                msg = None
            if op == "cancel":
                jid = str((msg or {}).get("job_id") or "")
                ok = self.cancel(sid, jid) if jid else False
                if reply is not None:
                    with contextlib.suppress(Exception):
                        reply({"v": WIRE_V, "job_id": jid, "status": "ok" if ok else "unknown", "code": 0})
                continue
            try:
                req = PlanRequest.from_wire(msg or {})
            except ValueError as e:
                if reply is not None:
                    with contextlib.suppress(Exception):
                        reply({"v": WIRE_V, "job_id": str((msg or {}).get("job_id") or ""), "status": "error",
                               "code": 300, "detail": "BAD_REQUEST", "remedy": str(e)[:200]})
                continue
            with self._lock:
                self._admit(sid, req, reply, t_rx + query_timeout_s(req.budget_ms))
            self._flush_cbs()

    def _on_worker_msg(self, w: _Worker) -> None:
        try:
            msg = w.conn.recv()
        except (EOFError, OSError):
            self._on_worker_died(w)
            return
        kind = msg[0]
        if kind == "begin":                            # 世界已就绪：作业计时从此开始
            j = w.job
            if j is not None and j.req.job_id == msg[1]:
                j.phase = "run"
                j.t_start = time.monotonic()
            return
        if kind == "ready":
            w.state = "idle"
            w.t_ready = time.monotonic()
            ms = round((w.t_ready - w.t_spawn) * 1000.0, 1)
            self.counters["rebuild_ms_last"] = ms
            self.counters["rebuild_ms_max"] = max(self.counters["rebuild_ms_max"], ms)
            return
        if kind != "done":
            return
        _, jid, blob, meta = msg
        j = w.job
        w.job = None
        w.state = "idle"
        w.n_jobs += 1
        if j is None or j.req.job_id != jid:
            return
        self.counters["done"] += 1
        s = self._sess.get(j.sid)
        if j.replied:                                 # 已被取代、取消或会话已结束：丢弃结果
            return
        if s is not None:
            s.n["done"] += 1
        extra: dict = {"result_sha256": meta.get("sha") or ""}
        if meta.get("remedy"):
            extra["remedy"] = meta["remedy"]
        if len(blob) > self.result_inline_max:
            try:
                path, sha = self._write_result(j, blob)
                extra.update(result_path=str(path), result_bytes_sha256=sha)
            except OSError as e:
                self._reply(j, "error", 320, f"RESULT_SPILL_FAILED: {e}"[:200], state="FAILED")
                return
        else:
            extra["result"] = blob
        self._reply(j, str(meta.get("status") or "error"), int(meta.get("code") or 0), meta.get("detail"),
                    state="DONE", **extra)

    def _write_result(self, j: _SJob, blob: bytes) -> tuple[Path, str]:
        d = self.shm_root / j.sid / "plan"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = _SAFE.sub("_", j.req.job_id)[:80] + "-" + hashlib.sha1(j.req.job_id.encode()).hexdigest()[:8]
        p = d / f"{name}.msgpack"
        tmp = d / f"{name}.msgpack.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        os.replace(tmp, p)
        self.counters["result_files"] += 1
        return p, hashlib.sha256(blob).hexdigest()

    def _on_worker_died(self, w: _Worker) -> None:
        j = w.job
        was_starting = w.state == "starting"
        self._stop_worker(w)
        self._respawn(w)
        if was_starting:
            log.warning("plan worker died during startup", extra={"kv": {"idx": w.idx}})
            return
        self.counters["crashes"] += 1
        if j is None or j.replied:
            return
        s = self._sess.get(j.sid)
        if s is not None:
            s.n["crashes"] += 1
        log.warning("plan worker crashed", extra={"kv": {"job": j.req.job_id, "retries": j.retries}})
        if j.retries >= 1 or s is None:
            self._reply(j, "error", 214, "PLANNER_CRASHED", state="FAILED")
            return
        j.retries += 1
        j.state = "QUEUED"
        heapq.heappush(s.heap, (j.key[0], j.key[1], j))           # 原优先级与提交序：排在本会话队首

    def _check_workers(self) -> None:
        now = time.monotonic()
        for w in self._workers:
            if w.state == "dead" and now >= w.t_next_spawn and not self._stop:
                self._spawn(w)
            elif w.state == "starting" and now - w.t_spawn > READY_TIMEOUT_S:
                log.warning("plan worker not ready in time", extra={"kv": {"idx": w.idx}})
                self._stop_worker(w)
                self._respawn(w)
            elif w.state in ("idle", "busy") and w.proc is not None and w.proc.poll() is not None:
                self._on_worker_died(w)

    def _check_timeouts(self) -> None:
        now = time.monotonic()
        for w in self._workers:
            j = w.job
            if w.state != "busy" or j is None:
                continue
            if now - j.t_start <= self._limit_s(j):
                continue
            log.warning("plan job timeout; killing worker", extra={"kv": {"job": j.req.job_id, "sid": j.sid,
                                                                              "phase": j.phase}})
            w.job = None
            self._kill(w)
            s = self._sess.get(j.sid)
            if j.phase == "load":                       # 世界装载超时属服务侧：不计入会话的超时次数
                if not j.replied:
                    self._reply(j, "error", 123, "WORLD_LOAD_TIMEOUT", state="FAILED")
                continue
            self.counters["timeouts"] += 1
            if not j.replied:
                self._reply(j, "timeout", 125, "PLAN_TIMEOUT", state="TIMEOUT", remedy="简化任务或稍后重试")
            if s is not None:
                s.n["timeouts"] += 1
                self._note_timeout(s, now)

    def _send_retain(self) -> None:
        """把活动世界集合交给各 worker（排在其后续作业之前处理；worker 不回复）：不在集合中的世界解除映射。"""
        keep = self._retain
        if keep is None:
            return
        self._retain = None
        for w in self._workers:
            if w.conn is not None and w.state != "dead":
                with contextlib.suppress(OSError, ValueError):
                    w.conn.send(("retain", sorted(keep)))

    @staticmethod
    def _limit_s(j: _SJob) -> float:
        return WORLD_LOAD_LIMIT_S if j.phase == "load" else j.budget_ms * TIMEOUT_FACTOR / 1000.0

    def _note_timeout(self, s: _Sess, now: float) -> None:
        s.timeouts.append(now)
        while s.timeouts and now - s.timeouts[0] > self.freeze_window_s:
            s.timeouts.popleft()
        if len(s.timeouts) < self.freeze_count:
            return
        s.timeouts.clear()
        s.frozen_until = now + self.freeze_s
        s.n["freezes"] += 1
        log.warning("plan session throttled", extra={"kv": {"sid": s.sid, "freeze_s": self.freeze_s}})
        for j in list(s.jobs.values()):
            if j.state == "QUEUED" and not j.replied:
                self._reply(j, "error", 586, "PLAN_THROTTLED", state="FAILED", retry_after_s=round(self.freeze_s, 3))

    def _expire_queued(self) -> None:
        now = time.monotonic()
        up = any(w.state in ("idle", "busy") for w in self._workers)
        for s in self._sess.values():
            for j in list(s.jobs.values()):
                if j.state == "QUEUED" and not j.replied and j.deadline is not None and now >= j.deadline:
                    s.n["expired"] += 1
                    if up:
                        self._reply(j, "timeout", 125, "QUEUE_EXPIRED", state="TIMEOUT")
                    else:
                        self._reply(j, "error", 213, "PLAN_SERVICE_UNAVAILABLE", state="FAILED")

    def _pop_next(self) -> _SJob | None:
        """轮转：从指针的下一个会话起，取第一个"有待执行作业且未冻结"的会话的最高优先级作业。"""
        n = len(self._order)
        now = time.monotonic()
        for k in range(n):
            i = (self._rr + 1 + k) % n
            s = self._sess[self._order[i]]
            if now < s.frozen_until:
                continue
            while s.heap:
                _p, _q, j = s.heap[0]
                if j.state != "QUEUED" or j.replied:
                    heapq.heappop(s.heap)
                    continue
                heapq.heappop(s.heap)
                self._rr = i
                return j
        return None

    def _dispatch(self) -> None:
        for w in self._workers:
            if w.state != "idle" or self._stop:
                continue
            while True:
                j = self._pop_next()
                if j is None:
                    return
                if j.deadline is not None and time.monotonic() >= j.deadline:
                    s = self._sess.get(j.sid)
                    if s is not None:
                        s.n["expired"] += 1
                    self._reply(j, "timeout", 125, "QUEUE_EXPIRED", state="TIMEOUT")
                    continue
                try:
                    w.conn.send(("job", j.req.job_id, j.req))
                except (OSError, ValueError, BrokenPipeError):
                    j.state = "QUEUED"
                    s = self._sess.get(j.sid)
                    if s is not None:
                        heapq.heappush(s.heap, (j.key[0], j.key[1], j))
                    self._on_worker_died(w)
                    break
                j.state = "RUNNING"
                j.phase = "load"
                j.t_start = time.monotonic()
                w.job = j
                w.state = "busy"
                self.counters["dispatched"] += 1
                break


def _check_sid(sid: str) -> None:
    if not isinstance(sid, str) or not SID_RE.match(sid):
        raise ValueError(f"bad session id {sid!r}")


def _worker_cli(fd: int) -> None:
    """worker 进程入口（`_spawn` 以 `python -c` 调用）：配置取环境变量 `AWR_PLAN_WORKER_CFG`。"""
    from multiprocessing.connection import Connection

    _worker_entry(Connection(fd), json.loads(os.environ.get("AWR_PLAN_WORKER_CFG") or "{}"))


def _trim(*, collect: bool = True) -> None:
    try:
        from awr.runtime.procutil import trim_heap

        trim_heap(collect=collect)
    except Exception:  # pragma: no cover - 只影响内存回落
        pass


def _worker_entry(conn: Any, cfg: dict) -> None:
    """worker 进程主循环（spawn）：初始化后发 ready，逐个执行作业并回送已编码的结果。"""
    from . import worker as W
    from .jobs import PlanResult, result_digest

    W.init_server_worker(cfg)
    conn.send(("ready", os.getpid()))
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg and msg[0] == "retain":
            if W.retain_worlds(msg[1]):
                _trim()
            continue
        if not msg or msg[0] != "job":
            return
        _, jid, req = msg
        W.preload_world(req.world_key, req.payload)    # 世界装载不计入作业预算
        try:
            conn.send(("begin", jid))
        except (EOFError, OSError):
            return
        res = W.worker_main(req)
        W.release_scratch()
        try:
            blob = res.to_bytes()
        except Exception as e:
            d = f"RESULT_ENCODE: {type(e).__name__}: {str(e)[:120]}"
            res = PlanResult(jid, "error", (), None, {}, d, None, result_digest((), None, {"d": d}), 320)
            blob = res.to_bytes()
        try:
            conn.send(("done", jid, blob, {"status": res.status, "code": int(res.code or 0), "detail": res.detail,
                                           "remedy": res.remedy, "sha": res.result_sha256}))
        except (EOFError, OSError):
            return
        # 结果送出之后、读下一个作业之前归还作业的临时分配（只 malloc_trim，不计入任何作业的预算与 3 × 预算强杀；D2-FX-R2：
        # 规划 worker PSS 与驻留后的沙盒组回落，D2-AC-28/45）
        _trim(collect=False)
