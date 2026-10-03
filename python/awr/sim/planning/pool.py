"""PlanPoolClient（sim-core 内，M10-FR-030、FR-031、FR-037；M10 §6.4.1、§7.4.3；ADR-039、ADR-049；10 §8.5）。

- 进程模型：`ProcessPoolExecutor(spawn, W = 1)`，spawn 前 `OMP_NUM_THREADS = 1`；worker 以 (world_id, contentVersion,
  coordinate.sha256) 打开并缓存 GeoWorld（绑定不一致拒绝，123 PLAN_GRID_MISMATCH）；worker 设 PDEATHSIG 后复核父进程 pid
  （sim-core 先行被 SIGKILL 时立即退出），亲和性避开 sim-core 所钉的核，继承的负 nice 恢复为 0（ADR-070）；
- 队列：优先级（0 交互 > 1 任务启动 > 2 编队与覆盖 > 3 后台）+ 提交序；W 个在途；同 `dedupe_key` 的新作业取代旧作业
  （旧结果到达后丢弃，SUPERSEDED）；
- 生效：done_callback 只把 future 放入 inbox（线程安全）；`drain(tick)` 在 sim-core 的 stage 内（步边界）取出结果，
  回调 `on_done(result, apply_tick)`，并把 `plan_result{job_id, result_sha256, status}` 与实际 apply_tick 写入输入日志；
- 预算：墙钟超过 `budget_ms × 3` 仍未返回判 PLAN_TIMEOUT（125），结果到达后丢弃；
- 崩溃：`BrokenProcessPool` 首次重建进程池并重交在途作业；同一作业第二次崩溃以 214 PLANNER_CRASHED 结束；
- 模式：`AWR_PLAN_POOL` = process（缺省）| thread（`--inproc`）| inline（测试：提交即执行，结果仍在下一次 drain 生效）。

计时（预算、超时）为墙钟（ADR-045 允许：plan-pool 作业预算属墙钟域），生效 tick 为仿真域。
"""

from __future__ import annotations

import contextlib
import heapq
import itertools
import logging
import os
import queue
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .jobs import BUDGET_MS, PlanRequest, PlanResult, result_digest

__all__ = ["DoneFn", "PlanPoolClient"]

log = logging.getLogger("awr.sim.planning.pool")

DoneFn = Callable[[PlanResult, int], None]
SPAWN_GRACE_NS = 20_000_000_000   # spawn 与 import 冷启动（本机 8 核负载高时 1–5 s）期间的超时宽限


@dataclass
class _Job:
    req: PlanRequest
    cb: DoneFn
    seq: int
    t_submit_ns: int = 0
    t_start_ns: int = 0
    retries: int = 0
    fut: Any = None
    state: str = "QUEUED"        # QUEUED、RUNNING、APPLIED、FAILED、SUPERSEDED


@dataclass
class _Stats:
    submitted: int = 0
    applied: int = 0
    failed: int = 0
    superseded: int = 0
    timeouts: int = 0
    crashes: int = 0
    rebuilds: int = 0
    last_ms: float = 0.0
    by_kind_ms: dict = field(default_factory=dict)


class PlanPoolClient:
    def __init__(self, world_key: tuple[str, str, str] | None, *, worlds_dir: str | None = None, world: Any = None,
                 workers: int = 1, cpu: int | None = None, mode: str | None = None, inputlog: Any = None,
                 max_tasks_per_child: int = 500) -> None:
        self.world_key = tuple(world_key) if world_key else None
        self.worlds_dir = worlds_dir
        self.world = world
        self.workers = max(1, int(workers))
        self.cpu = cpu
        self.mode = (mode or os.environ.get("AWR_PLAN_POOL") or "process").strip().lower()
        if self.mode not in ("process", "thread", "inline"):
            self.mode = "process"
        self.inputlog = inputlog
        self.max_tasks_per_child = max_tasks_per_child
        self._heap: list[tuple[int, int, str]] = []
        self._jobs: dict[str, _Job] = {}
        self._latest: dict[str, str] = {}        # dedupe_key → 最新 job_id
        self._inbox: queue.SimpleQueue = queue.SimpleQueue()
        self._seq = itertools.count()
        self._exec: Any = None
        self._fresh_ns = 0                       # 新建（或重建）进程池的时刻：spawn 冷启动期间作业超时判定放宽
        self.state = "ok"                        # ok、rebuilding、down
        self.stats = _Stats()
        # 剧本开局屏障（ADR-068 第 4 条、ADR-073 第 7 条）：on_submit(job_id) 在每次提交后调用（M10 设置期内置时钟保持）；
        # _arrived 为结果（或错误）已进入 inbox 的作业（done_callback 线程写入，主线程读取与清除）
        self.on_submit: Callable[[str], None] | None = None
        self._arrived: set[str] = set()
        if self.mode in ("inline", "thread"):
            from . import worker

            worker.set_world(self.world_key, world)

    # ------------------------------------------------------------ 进程池
    def _executor(self) -> Any:
        if self._exec is not None:
            return self._exec
        self._fresh_ns = time.monotonic_ns()
        if self.mode == "process":
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor

            from .worker import init_worker

            os.environ["OMP_NUM_THREADS"] = "1"
            os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
            os.environ.setdefault("MKL_NUM_THREADS", "1")
            parent_cpus: tuple[int, ...] | None = None
            with contextlib.suppress(OSError, AttributeError):
                parent_cpus = tuple(sorted(os.sched_getaffinity(0)))
            # 父进程 pid 与亲和性：worker 复核父进程是否已先行死亡（孤儿竞态），并避开 sim-core 所钉的核（FX2-R3，ADR-070）
            kw: dict[str, Any] = {"max_workers": self.workers, "mp_context": mp.get_context("spawn"),
                                  "initializer": init_worker,
                                  "initargs": (self.worlds_dir, self.world_key, self.cpu, os.getpid(), parent_cpus)}
            try:
                self._exec = ProcessPoolExecutor(max_tasks_per_child=self.max_tasks_per_child, **kw)
            except TypeError:  # pragma: no cover
                self._exec = ProcessPoolExecutor(**kw)
        elif self.mode == "thread":
            from concurrent.futures import ThreadPoolExecutor

            self._exec = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="plan-pool")
        return self._exec

    def _rebuild(self) -> None:
        old = self._exec
        self._exec = None
        self.stats.rebuilds += 1
        if old is not None:
            with contextlib.suppress(Exception):
                old.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        if self._exec is not None:
            with contextlib.suppress(Exception):
                self._exec.shutdown(wait=False, cancel_futures=True)
            self._exec = None

    # ------------------------------------------------------------ 提交
    def warm(self, tick: int = 0, *, grid25: bool = False, on_done: DoneFn | None = None) -> str:
        req = PlanRequest(f"warm:{self.world_key[0] if self.world_key else '-'}:0", "warm", self.world_key or ("", "", ""),
                          (), {"grid25": grid25}, {}, 3, tick, BUDGET_MS["warm"], "warm")
        return self.submit(req, on_done or (lambda r, t: None))

    def submit(self, req: PlanRequest, on_done: DoneFn) -> str:
        old = self._latest.get(req.dedupe_key)
        if old is not None and old in self._jobs and old != req.job_id:
            self._supersede(old)
        job = _Job(req, on_done, next(self._seq), time.monotonic_ns())
        self._jobs[req.job_id] = job
        self._latest[req.dedupe_key] = req.job_id
        self.stats.submitted += 1
        if self.mode == "inline":
            job.state = "RUNNING"
            job.t_start_ns = time.monotonic_ns()
            from .worker import worker_main

            try:
                res = worker_main(req)
                self._inbox.put((req.job_id, res, None))
            except Exception as e:  # pragma: no cover - worker_main 自身兜底
                self._inbox.put((req.job_id, None, e))
            self._arrived.add(req.job_id)
            if self.on_submit is not None:
                self.on_submit(req.job_id)
            return req.job_id
        heapq.heappush(self._heap, (int(req.priority), job.seq, req.job_id))
        self._dispatch()
        if self.on_submit is not None:
            self.on_submit(req.job_id)
        return req.job_id

    def _arrive(self, jid: str, item: Any, err: BaseException | None) -> None:
        """结果进入 inbox（done_callback 线程或提交线程）：先入队、后登记已到达（`pending_of` 据此判定）。"""
        self._inbox.put((jid, item, err))
        self._arrived.add(jid)

    def pending_of(self, job_ids: set[str] | list[str]) -> list[str]:
        """给定作业中尚未到达（仍在排队或执行、结果未进入 inbox）的作业；已取代、已失败或已生效的作业不算。"""
        out = []
        for jid in job_ids:
            j = self._jobs.get(jid)
            if j is not None and j.state in ("QUEUED", "RUNNING") and jid not in self._arrived:
                out.append(jid)
        return out

    def cancel(self, dedupe_key: str) -> None:
        jid = self._latest.pop(dedupe_key, None)
        if jid is not None and jid in self._jobs:
            self._supersede(jid)

    def _supersede(self, job_id: str) -> None:
        j = self._jobs.get(job_id)
        if j is None:
            return
        if j.state == "QUEUED":
            self._jobs.pop(job_id, None)
        else:
            j.state = "SUPERSEDED"
        self.stats.superseded += 1

    def dispatch(self) -> None:
        """把排队作业交给空闲 worker（`drain` 之外的调用点：M10 开局屏障的就绪判据在时钟保持期间调用，ADR-073）。"""
        if self.mode != "inline":
            self._dispatch()

    def pending(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in ("QUEUED", "RUNNING"))

    def _running(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in ("RUNNING", "SUPERSEDED") and j.fut is not None
                   and not j.fut.done())

    def _dispatch(self) -> None:
        while self._heap and self._running() < self.workers:
            _p, _s, jid = heapq.heappop(self._heap)
            j = self._jobs.get(jid)
            if j is None or j.state != "QUEUED":
                continue
            self._start(j)

    def _start(self, j: _Job) -> None:
        from .worker import worker_main

        j.state = "RUNNING"
        j.t_start_ns = time.monotonic_ns()
        jid = j.req.job_id
        try:
            fut = self._executor().submit(worker_main, j.req)
        except Exception as e:  # BrokenProcessPool 在 submit 时抛出
            self._arrive(jid, None, e)
            return
        j.fut = fut
        fut.add_done_callback(lambda f, jid=jid: self._arrive(jid, f, None))

    # ------------------------------------------------------------ 生效（步边界）
    def drain(self, tick: int) -> int:
        n = 0
        while True:
            try:
                jid, item, err = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._arrived.discard(jid)
            j = self._jobs.get(jid)
            if j is None:
                continue
            res: PlanResult | None = None
            if err is None and item is not None and not isinstance(item, PlanResult):
                try:
                    res = item.result()
                except Exception as e:
                    err = e
            elif isinstance(item, PlanResult):
                res = item
            if err is not None:
                n += self._on_error(j, err, tick)
                continue
            if j.state == "SUPERSEDED":
                self._jobs.pop(jid, None)
                continue
            if j.state == "FAILED":           # 已判超时：结果到达后丢弃
                self._jobs.pop(jid, None)
                continue
            self._apply(j, res, tick)
            n += 1
        self._check_timeouts(tick)
        if self.mode != "inline":
            self._dispatch()
        if len(self._arrived) > 1024:  # 回调线程"先入队后登记"与本线程"取出即清除"交错时留下的陈旧条目
            for j in [x for x in list(self._arrived) if x not in self._jobs]:
                self._arrived.discard(j)  # 原地清除（回调线程可能同时 add）
        return n

    def _apply(self, j: _Job, res: PlanResult, tick: int) -> None:
        self._jobs.pop(j.req.job_id, None)
        if self._latest.get(j.req.dedupe_key) == j.req.job_id:
            self._latest.pop(j.req.dedupe_key, None)
        j.state = "APPLIED" if res.ok else "FAILED"
        ms = (time.monotonic_ns() - j.t_submit_ns) / 1e6
        self.stats.last_ms = round(ms, 3)
        self.stats.by_kind_ms[j.req.kind] = round(ms, 3)
        if res.ok:
            self.stats.applied += 1
        else:
            self.stats.failed += 1
        if self.inputlog is not None:
            with contextlib.suppress(Exception):
                self.inputlog.append("plan_result", int(tick), {"job_id": j.req.job_id, "kind": j.req.kind,
                                                                "status": res.status, "sha256": res.result_sha256,
                                                                "request_tick": j.req.request_tick})
        try:
            j.cb(res, int(tick))
        except Exception:
            log.exception("plan result callback failed", extra={"kv": {"job": j.req.job_id}})

    def _fail(self, j: _Job, status: str, code: int, detail: str, tick: int) -> None:
        res = PlanResult(j.req.job_id, status, (), None, {}, detail, None, result_digest((), None, {"d": detail}), code)
        self._apply(j, res, tick)

    def _on_error(self, j: _Job, err: BaseException, tick: int) -> int:
        from concurrent.futures.process import BrokenProcessPool

        if j.state == "SUPERSEDED":
            self._jobs.pop(j.req.job_id, None)
            return 0
        if isinstance(err, BrokenProcessPool):
            self.stats.crashes += 1
            log.warning("plan-pool worker crashed", extra={"kv": {"job": j.req.job_id, "retries": j.retries}})
            if self._exec is not None:
                self._rebuild()
            if j.retries >= 1:
                self._fail(j, "error", 214, "PLANNER_CRASHED", tick)
                return 1
            j.retries += 1
            j.state = "QUEUED"
            j.fut = None
            heapq.heappush(self._heap, (int(j.req.priority), j.seq, j.req.job_id))
            # 其他在途作业随旧池一并失效：以 BrokenProcessPool 回调入 inbox 后各自重交
            return 0
        self._fail(j, "error", 320, f"{type(err).__name__}: {str(err)[:160]}", tick)
        return 1

    def _check_timeouts(self, tick: int) -> None:
        now = time.monotonic_ns()
        for j in list(self._jobs.values()):
            if j.state != "RUNNING" or not j.t_start_ns:
                continue
            grace = SPAWN_GRACE_NS if self.mode == "process" and j.t_start_ns - self._fresh_ns < SPAWN_GRACE_NS else 0
            if now - j.t_start_ns > int(j.req.budget_ms) * 3 * 1_000_000 + grace:
                self.stats.timeouts += 1
                res = PlanResult(j.req.job_id, "timeout", (), None, {}, "PLAN_TIMEOUT", "简化任务或稍后重试",
                                 result_digest((), None, {"d": "PLAN_TIMEOUT"}), 125)
                cb = j.cb
                j.state = "FAILED"            # 保留条目，结果到达后丢弃
                self.stats.failed += 1
                try:
                    cb(res, int(tick))
                except Exception:
                    log.exception("plan timeout callback failed")

    def snapshot(self) -> dict:
        s = self.stats
        return {"mode": self.mode, "state": self.state, "pending": self.pending(), "submitted": s.submitted,
                "applied": s.applied, "failed": s.failed, "superseded": s.superseded, "timeouts": s.timeouts,
                "crashes": s.crashes, "rebuilds": s.rebuilds, "last_ms": s.last_ms}
