"""PlanPoolClient（sim-core 内，M10-FR-030、FR-031、FR-037；D2：M10-FR-202、FR-212；M10 §6.4.1、§7.4.3、§15.3.2；
ADR-039、ADR-049、ADR-113；10 §8.5）。

- 进程模型：`ProcessPoolExecutor(spawn, W = 1)`，spawn 前 `OMP_NUM_THREADS = 1`；worker 以 (world_id, contentVersion,
  coordinate.sha256) 打开并缓存 GeoWorld（绑定不一致拒绝，123 PLAN_GRID_MISMATCH）；worker 设 PDEATHSIG 后复核父进程 pid
  （sim-core 先行被 SIGKILL 时立即退出），亲和性避开 sim-core 所钉的核，继承的负 nice 恢复为 0（ADR-070）；
- 队列：优先级（0 交互 > 1 任务启动 > 2 编队与覆盖 > 3 后台）+ 提交序；W 个在途；同 `dedupe_key` 的新作业取代旧作业
  （旧结果到达后丢弃，SUPERSEDED）；
- 准入：提交前按作业注册表执行该类别的 `validate_input`（M10-FR-212），不通过即在下一次 drain 以 585（或校验给出的码）
  结束、不提交；
- 生效：done_callback 只把 future 放入 inbox（线程安全）；`drain(tick)` 在 sim-core 的 stage 内（步边界）取出结果，
  回调 `on_done(result, apply_tick)`，并把 `plan_result{job_id, result_sha256, status}` 与实际 apply_tick 写入输入日志；
- 预算：墙钟超过 `budget_ms × 3` 仍未返回判 PLAN_TIMEOUT（125），结果到达后丢弃；
- 崩溃：`BrokenProcessPool` 首次重建进程池并重交在途作业；同一作业第二次崩溃以 214 PLANNER_CRASHED 结束；
- 模式：`AWR_PLAN_POOL` = process（缺省）| thread（`--inproc`）| inline（测试：提交即执行，结果仍在下一次 drain 生效）
  | remote（沙盒 sim-core：经本会话总线 query `ctl/plan/submit` 交给 sandbox-pool 宿主的共享规划服务）。

remote 模式（M10-FR-202，ADR-113）：请求体 `PlanRequest.to_wire()`，query 超时 = 预算 × 3 + 5 s，不重试；超时、强杀、
冻结与配额由服务端判定并随回复返回（125、586、585、111、214）。回复在总线回调线程只入队，由本客户端的解码线程
解码（结果 > 1 MB 时服务端写入 `<AWR_RUN_DIR>/plan/<job>.msgpack`，这里校验路径与 sha256 后读取并删除），再按原有
语义在下一次 `drain(tick)` 生效、写输入日志；被取代或取消的在途作业另发 `ctl/plan/cancel`（尽力而为）。在途 query 数
上限 `remote_inflight`（缺省 4，`AWR_PLAN_REMOTE_INFLIGHT`），其余在本地按优先级排队，因此服务端每会话 8 个的上限
不会被正常客户端触发。

计时（预算、超时）为墙钟（ADR-045 允许：plan-pool 作业预算属墙钟域），生效 tick 为仿真域。

批处理（M08-FR-221、FR-222；ADR-106；D2-AC-32）：`batch_of()`（M10 运行时绑定为 `ctx.batch`）返回 BatchCtl 时，作业照常在进程池
（或线程、inline、远端服务）执行，但结果不再"到达即在下一步边界生效"：每个作业在提交时定下生效 tick
`due = request_tick + BatchCtl.lat_ticks(kind, budget_ms)`，到达的结果暂存，`drain(tick)` 只按 (due, 提交序) 生效 due ≤ tick 的作业；
到期仍未返回的作业由主循环阻塞等待（批处理专用），墙钟等待超过 `BatchCtl.wait_limit_s`（60 s）以 `BatchCtl.fail()` 结束批处理
（退出码 3）；批处理不做墙钟超时判定。生效 tick 因此只取决于仿真状态，与墙钟和机器负载无关（D2-AC-32 第 1 轮：`follow_path`
的结果按到达时刻生效，同一快照两次批处理在 20.87 / 20.89 s 分叉）。
"""

from __future__ import annotations

import contextlib
import hashlib
import heapq
import itertools
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .jobs import (
    BUDGET_MS,
    PlanRequest,
    PlanResult,
    ensure_plan_kinds,
    query_timeout_s,
    rejection,
    result_digest,
    validate_request,
)

__all__ = ["DoneFn", "PlanPoolClient", "RemoteReplyError"]

log = logging.getLogger("awr.sim.planning.pool")

DoneFn = Callable[[PlanResult, int], None]
SPAWN_GRACE_NS = 20_000_000_000   # spawn 与 import 冷启动（本机 8 核负载高时 1–5 s）期间的超时宽限
REMOTE_INFLIGHT = 4
REMOTE_GUARD_NS = 2_000_000_000   # 本地兜底：query 超时之后仍无任何回调时判超时
NO_SERVICE_S = 0.5                # query 在此之内以"无回复"结束：没有服务方（池未声明 ctl/plan/submit）
MODES = ("process", "thread", "inline", "remote")


class RemoteReplyError(RuntimeError):
    """remote 回复不合法（路径越界、sha256 不符、解码失败）。"""


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
    due: int = -1                # 批处理：固定延迟的生效 tick（模块文档末段）；实时运行为 −1


@dataclass
class _Stats:
    submitted: int = 0
    applied: int = 0
    failed: int = 0
    superseded: int = 0
    timeouts: int = 0
    crashes: int = 0
    rebuilds: int = 0
    rejected: int = 0
    last_ms: float = 0.0
    by_kind_ms: dict = field(default_factory=dict)


class PlanPoolClient:
    def __init__(self, world_key: tuple[str, str, str] | None, *, worlds_dir: str | None = None, world: Any = None,
                 workers: int = 1, cpu: int | None = None, mode: str | None = None, inputlog: Any = None,
                 max_tasks_per_child: int = 500, bus: Any = None, result_dir: str | os.PathLike | None = None,
                 remote_inflight: int | None = None) -> None:
        self.world_key = tuple(world_key) if world_key else None
        self.worlds_dir = worlds_dir
        self.world = world
        self.workers = max(1, int(workers))
        self.cpu = cpu
        self.mode = (mode or os.environ.get("AWR_PLAN_POOL") or "process").strip().lower()
        if self.mode not in MODES:
            self.mode = "process"
        self.mode_requested = self.mode
        self.bus = bus
        if self.mode == "remote" and bus is None:
            # 沙盒 sim-core 必须给出本会话总线；缺失时退回独立规划进程（功能可用，但内存按 ADR-113 超出预算）
            log.error("AWR_PLAN_POOL=remote without a bus; falling back to process mode")
            self.mode = "process"
        self.result_dir = Path(result_dir) if result_dir else _default_result_dir()
        n_in = remote_inflight if remote_inflight is not None else os.environ.get("AWR_PLAN_REMOTE_INFLIGHT")
        self.remote_inflight = max(1, int(n_in)) if n_in else REMOTE_INFLIGHT
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
        # remote：在途 query（主线程增删）与解码线程
        self._inflight: set[str] = set()
        self._rx: queue.SimpleQueue | None = None
        self._rx_thread: threading.Thread | None = None
        self._closed = False
        # 批处理（模块文档末段）：batch_of() → BatchCtl 或 None；_got 为已到达、等待生效 tick 的结果（job_id → (item, err)）
        self.batch_of: Callable[[], Any] | None = None
        self._got: dict[str, tuple[Any, BaseException | None]] = {}
        ensure_plan_kinds()
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
        self._closed = True
        if self._exec is not None:
            with contextlib.suppress(Exception):
                self._exec.shutdown(wait=False, cancel_futures=True)
            self._exec = None
        if self.mode == "remote":
            for jid in list(self._inflight):     # 释放服务端的排队名额（尽力而为）
                self._remote_cancel(jid)
            self._inflight.clear()
            if self._rx is not None:
                self._rx.put(None)

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
        ctl = self._batch()
        if ctl is not None:
            try:
                lat = int(ctl.lat_ticks(req.kind, float(req.budget_ms)))
            except Exception:
                lat = 1
            job.due = int(req.request_tick) + max(1, lat)
        self._jobs[req.job_id] = job
        self._latest[req.dedupe_key] = req.job_id
        self.stats.submitted += 1
        rej = validate_request(req)
        if rej is not None:                      # 输入配额（M10-FR-212）：不提交，下一次 drain 生效
            self.stats.rejected += 1
            job.state = "RUNNING"
            self._arrive(req.job_id, rej, None)
            if self.on_submit is not None:
                self.on_submit(req.job_id)
            return req.job_id
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

    def _batch(self) -> Any:
        fn = self.batch_of
        if fn is None:
            return None
        try:
            return fn()
        except Exception:  # pragma: no cover - 绑定方的访问器不应抛错
            return None

    def _collect(self, jid: str, item: Any, err: BaseException | None, tick: int) -> None:
        """批处理：到达的结果暂存到生效 tick；已取代或已失败的作业丢弃；进程池崩溃的首次重交立即处理（不生效、不暂存）。"""
        from concurrent.futures.process import BrokenProcessPool

        self._arrived.discard(jid)
        self._inflight.discard(jid)
        j = self._jobs.get(jid)
        if j is None:
            return
        if j.state in ("SUPERSEDED", "FAILED"):
            self._jobs.pop(jid, None)
            return
        if err is None and item is not None and not isinstance(item, (PlanResult, tuple)):
            try:
                item.result()
            except BrokenProcessPool as e:
                err = e
            except Exception:
                pass
        if isinstance(err, BrokenProcessPool) and j.retries < 1:
            self._on_error(j, err, tick)          # 重建进程池并重交（不生效）
            return
        self._got[jid] = (item, err)

    def _drain_batch(self, tick: int, ctl: Any) -> int:
        """批处理的步边界生效（模块文档末段）：生效 tick 已到的作业按 (due, 提交序) 生效，未返回的阻塞等待。"""
        while True:
            try:
                jid, item, err = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._collect(jid, item, err, tick)
        for j in self._jobs.values():
            if j.due < 0:  # 批处理开始之前提交的作业（例如绑定时的预热）：按同一规则补定生效 tick
                try:
                    lat = int(ctl.lat_ticks(j.req.kind, float(j.req.budget_ms)))
                except Exception:
                    lat = 1
                j.due = int(j.req.request_tick) + max(1, lat)
        due = sorted((j.due, j.seq, jid) for jid, j in self._jobs.items()
                     if j.state in ("QUEUED", "RUNNING") and 0 <= j.due <= tick)
        missing = [jid for _d, _s, jid in due if jid not in self._got]
        if missing:
            t0 = time.monotonic()
            limit = float(getattr(ctl, "wait_limit_s", 60.0) or 60.0)
            kind = self._jobs[missing[0]].req.kind
            while missing:
                if self.mode != "inline":
                    self._dispatch()
                left = limit - (time.monotonic() - t0)
                if left <= 0.0:
                    with contextlib.suppress(Exception):
                        ctl.record_wait(kind, time.monotonic() - t0)
                    ctl.fail(f"plan wait > {limit:g} s: {kind}")
                    return 0
                try:
                    jid, item, err = self._inbox.get(timeout=min(left, 0.25))
                except queue.Empty:
                    continue
                self._collect(jid, item, err, tick)
                missing = [x for x in missing if x not in self._got and x in self._jobs]
            with contextlib.suppress(Exception):
                ctl.record_wait(kind, time.monotonic() - t0)
        n = 0
        for _d, _s, jid in due:
            got = self._got.pop(jid, None)
            if got is not None:
                n += self._take(jid, got[0], got[1], tick)
        if self.mode != "inline":
            self._dispatch()
        return n

    def _arrive(self, jid: str, item: Any, err: BaseException | None) -> None:
        """结果进入 inbox（done_callback 线程或提交线程）：先入队、后登记已到达（`pending_of` 据此判定）。"""
        self._inbox.put((jid, item, err))
        self._arrived.add(jid)

    def pending_of(self, job_ids: set[str] | list[str]) -> list[str]:
        """给定作业中尚未到达（仍在排队或执行、结果未进入 inbox）的作业；已取代、已失败或已生效的作业不算。"""
        out = []
        for jid in job_ids:
            j = self._jobs.get(jid)
            if j is not None and j.state in ("QUEUED", "RUNNING") and jid not in self._arrived and jid not in self._got:
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
        # 排队中，或（批处理）结果已到达、尚未到生效 tick：直接丢弃
        if j.state == "QUEUED" or self._got.pop(job_id, None) is not None:
            self._jobs.pop(job_id, None)
        else:
            if self.mode == "remote" and j.state == "RUNNING" and job_id in self._inflight:
                self._remote_cancel(job_id)
            j.state = "SUPERSEDED"
        self.stats.superseded += 1

    def dispatch(self) -> None:
        """把排队作业交给空闲 worker（`drain` 之外的调用点：M10 开局屏障的就绪判据在时钟保持期间调用，ADR-073）。"""
        if self.mode != "inline":
            self._dispatch()

    def pending(self) -> int:
        return sum(1 for j in self._jobs.values() if j.state in ("QUEUED", "RUNNING"))

    def _running(self) -> int:
        if self.mode == "remote":
            return len(self._inflight)
        return sum(1 for j in self._jobs.values() if j.state in ("RUNNING", "SUPERSEDED") and j.fut is not None
                   and not j.fut.done())

    def _cap(self) -> int:
        return self.remote_inflight if self.mode == "remote" else self.workers

    def _dispatch(self) -> None:
        while self._heap and self._running() < self._cap():
            _p, _s, jid = heapq.heappop(self._heap)
            j = self._jobs.get(jid)
            if j is None or j.state != "QUEUED":
                continue
            if self.mode == "remote":
                self._start_remote(j)
            else:
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

    # ------------------------------------------------------------ remote（M10-FR-202）
    def _start_remote(self, j: _Job) -> None:
        from awr.contracts import bus_keys

        self._ensure_rx()
        j.state = "RUNNING"
        j.t_start_ns = time.monotonic_ns()
        jid = j.req.job_id
        try:
            msg = j.req.to_wire()
        except Exception as e:                   # 载荷不可编码：按 300 结束，不提交
            self._arrive(jid, rejection(j.req, 300, f"BAD_REQUEST: {type(e).__name__}: {str(e)[:160]}"), None)
            return
        self._inflight.add(jid)
        t0 = time.monotonic()
        rx = self._rx

        def on_reply(rep: Any, err: BaseException | None, jid: str = jid, t0: float = t0) -> None:
            rx.put((jid, rep, err, time.monotonic() - t0))   # 总线回调线程：只入队

        try:
            self.bus.call_cb(bus_keys.ctl_plan("submit"), msg, on_reply, timeout=query_timeout_s(j.req.budget_ms),
                             retries=0)
        except Exception as e:
            rx.put((jid, None, e, 0.0))

    def _remote_cancel(self, jid: str) -> None:
        from awr.contracts import bus_keys

        with contextlib.suppress(Exception):
            self.bus.call_cb(bus_keys.ctl_plan("cancel"), {"v": 1, "job_id": jid}, lambda rep, err: None, timeout=1.0,
                             retries=0)

    def _ensure_rx(self) -> None:
        if self._rx_thread is not None:
            return
        self._rx = queue.SimpleQueue()
        self._rx_thread = threading.Thread(target=self._rx_loop, name="plan-remote-rx", daemon=True)
        self._rx_thread.start()

    def _rx_loop(self) -> None:
        """解码线程：回复 → PlanResult（或异常）后入 inbox；读取外置结果文件也在这里，不占 sim-core 主循环。"""
        rx = self._rx
        while True:
            it = rx.get()
            if it is None:
                return
            jid, rep, err, dt = it
            if err is None:
                try:
                    res: Any = self._decode_reply(jid, rep)
                except Exception as e:
                    res, err = None, e
            else:
                res = (err, dt)
                err = None
            self._arrive(jid, res, err)

    def _decode_reply(self, jid: str, rep: Any) -> PlanResult | tuple[str, Any]:
        if not isinstance(rep, dict) or str(rep.get("job_id")) != jid:
            raise RemoteReplyError("reply does not match the job")
        status = str(rep.get("status") or "error")
        if status in ("superseded", "canceled"):
            return ("dropped", status)
        if rep.get("result_path"):
            p = Path(str(rep["result_path"]))
            raw = self._read_result_file(p, str(rep.get("result_bytes_sha256") or ""))
            res = PlanResult.from_bytes(raw)
        elif rep.get("result") is not None:
            res = PlanResult.from_bytes(rep["result"])
        else:
            code = int(rep.get("code") or 0) or 320
            detail = str(rep.get("detail") or status.upper())
            res = PlanResult(jid, status if status != "ok" else "error", (), None, dict(rep.get("stats") or {}), detail,
                             rep.get("remedy"), result_digest((), None, {"d": detail}), code)
            if rep.get("retry_after_s") is not None:
                res.stats["retry_after_s"] = rep["retry_after_s"]
            if rep.get("quota"):
                res.stats["quota"] = rep["quota"]
            return res
        if res.job_id != jid:
            raise RemoteReplyError("result job_id mismatch")
        sha = rep.get("result_sha256")
        if sha and sha != res.result_sha256:
            raise RemoteReplyError("result_sha256 mismatch")
        return res

    def _read_result_file(self, p: Path, sha: str) -> bytes:
        root = self.result_dir.resolve()
        try:
            rp = p.resolve()
        except OSError as e:
            raise RemoteReplyError(f"result path: {e}") from None
        if root not in rp.parents:
            raise RemoteReplyError(f"result path outside {root}")
        try:
            raw = rp.read_bytes()
        finally:
            with contextlib.suppress(OSError):
                rp.unlink()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise RemoteReplyError("result file sha256 mismatch")
        return raw

    # ------------------------------------------------------------ 生效（步边界）
    def drain(self, tick: int) -> int:
        ctl = self._batch()
        if ctl is not None:
            return self._drain_batch(int(tick), ctl)
        n = 0
        while True:
            try:
                jid, item, err = self._inbox.get_nowait()
            except queue.Empty:
                break
            self._arrived.discard(jid)
            self._inflight.discard(jid)
            n += self._take(jid, item, err, tick)
        self._check_timeouts(tick)
        if self.mode != "inline":
            self._dispatch()
        if len(self._arrived) > 1024:  # 回调线程"先入队后登记"与本线程"取出即清除"交错时留下的陈旧条目
            for j in [x for x in list(self._arrived) if x not in self._jobs]:
                self._arrived.discard(j)  # 原地清除（回调线程可能同时 add）
        return n

    def _take(self, jid: str, item: Any, err: BaseException | None, tick: int) -> int:
        """一个已到达的结果（或错误）在本步边界的处理；返回生效（含失败生效）的作业数。"""
        j = self._jobs.get(jid)
        if j is None:
            return 0
        res: PlanResult | None = None
        if isinstance(item, tuple) and item and isinstance(item[0], BaseException):
            return self._on_remote_error(j, item[0], float(item[1]), tick)
        if isinstance(item, tuple) and item and item[0] == "dropped":
            if j.state == "SUPERSEDED":
                self._jobs.pop(jid, None)
                return 0
            item, err = None, RemoteReplyError(f"job {item[1]} by the plan service")
        if err is None and item is not None and not isinstance(item, PlanResult):
            try:
                res = item.result()
            except Exception as e:
                err = e
        elif isinstance(item, PlanResult):
            res = item
        if err is not None:
            return self._on_error(j, err, tick)
        if j.state == "SUPERSEDED":
            self._jobs.pop(jid, None)
            return 0
        if j.state == "FAILED":           # 已判超时：结果到达后丢弃
            self._jobs.pop(jid, None)
            return 0
        self._apply(j, res, tick)
        return 1

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

    def _fail(self, j: _Job, status: str, code: int, detail: str, tick: int, remedy: str | None = None) -> None:
        res = PlanResult(j.req.job_id, status, (), None, {}, detail, remedy, result_digest((), None, {"d": detail}), code)
        self._apply(j, res, tick)

    def _on_remote_error(self, j: _Job, err: BaseException, dt_s: float, tick: int) -> int:
        """remote query 没有得到回复：很快结束为"无服务方"（213），否则为超时（125 PLAN_TIMEOUT）。"""
        if j.state in ("SUPERSEDED", "FAILED"):
            self._jobs.pop(j.req.job_id, None)
            return 0
        from awr.runtime.bus import BusClosed, BusTimeout

        if isinstance(err, BusTimeout) and dt_s >= NO_SERVICE_S:
            self.stats.timeouts += 1
            self._remote_cancel(j.req.job_id)
            self._fail(j, "timeout", 125, "PLAN_TIMEOUT", tick, "简化任务或稍后重试")
        elif isinstance(err, (BusTimeout, BusClosed)):
            self._fail(j, "error", 213, "PLAN_SERVICE_UNAVAILABLE", tick, "稍后重试")
        else:
            self._fail(j, "error", 213, f"PLAN_SERVICE_ERROR: {str(err)[:160]}", tick, "稍后重试")
        return 1

    def _on_error(self, j: _Job, err: BaseException, tick: int) -> int:
        from concurrent.futures.process import BrokenProcessPool

        if j.state in ("SUPERSEDED", "FAILED"):   # 已取代或已判超时：不再回调（池若已损坏，下一次提交时重建）
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
            if self.mode == "remote":
                # 超时由服务端判定并回复；这里只兜底"query 已超时却没有任何回调"的情形
                limit = int(query_timeout_s(int(j.req.budget_ms)) * 1e9) + REMOTE_GUARD_NS
            else:
                grace = SPAWN_GRACE_NS if self.mode == "process" and j.t_start_ns - self._fresh_ns < SPAWN_GRACE_NS else 0
                limit = int(j.req.budget_ms) * 3 * 1_000_000 + grace
            if now - j.t_start_ns > limit:
                self.stats.timeouts += 1
                res = PlanResult(j.req.job_id, "timeout", (), None, {}, "PLAN_TIMEOUT", "简化任务或稍后重试",
                                 result_digest((), None, {"d": "PLAN_TIMEOUT"}), 125)
                cb = j.cb
                j.state = "FAILED"            # 保留条目，结果到达后丢弃
                self.stats.failed += 1
                if self.mode == "remote":
                    self._inflight.discard(j.req.job_id)
                    self._remote_cancel(j.req.job_id)
                try:
                    cb(res, int(tick))
                except Exception:
                    log.exception("plan timeout callback failed")

    def snapshot(self) -> dict:
        s = self.stats
        out = {"mode": self.mode, "state": self.state, "pending": self.pending(), "submitted": s.submitted,
               "applied": s.applied, "failed": s.failed, "superseded": s.superseded, "timeouts": s.timeouts,
               "crashes": s.crashes, "rebuilds": s.rebuilds, "last_ms": s.last_ms, "rejected": s.rejected}
        if self.mode == "remote" or self.mode_requested == "remote":
            out["requested"] = self.mode_requested
            out["inflight"] = len(self._inflight)
        return out


def _default_result_dir() -> Path:
    """外置结果目录：`<AWR_RUN_DIR>/plan`（沙盒 run 为 `/dev/shm/awr/<sid>/plan`，与服务端写入位置相同）。"""
    rd = os.environ.get("AWR_RUN_DIR")
    if rd:
        return Path(rd) / "plan"
    return Path("/dev/shm/awr") / os.environ.get("AWR_RUN", "local") / "plan"
