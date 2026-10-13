"""M20 规划作业的提交与生效（M20-FR-036、FR-071、FR-072；M20 §6.13；ADR-113；d2-WP-15-to-M20、d2-WP-02-to-M20 第 6 条）。

- 实时：经 M10 运行时的 `PlanPoolClient`（沙盒为 remote 模式，交共享规划服务；共享展示为 process 模式）提交 `PlanRequest`，结果在
  M10 的 `drain(tick)` 步边界回调（写输入日志）；记录墙钟时延，指数平均 p95 `L95`（初值 0.7 s）给出 `t_plan = max(2 s, r·L95)`；
- 批处理（`ctx.batch`）：在提交 tick 同步执行（`plan_kinds.run_kind`），结果在 `request_tick + L_sim(kind)` 生效（分配 1 s、站位 2 s、
  扫描 5 s、评估 5 s【仿真】，经 `BatchCtl.lat_ticks`），因此逐字节确定且时延口径不优于实时；
- 没有规划池（单测、M10 未装配）：同步执行，下一次 `drain` 生效；
- 沙盒会话（`AWR_PLAN_POOL = remote`）中规划池尚未就绪（拉起后 M10 运行时绑定之前的数秒）：作业留在本地等待，就绪后提交给共享规划服务，
  不在 sim-core 内同步执行（ADR-113"重计算放进可杀进程"；D2-FX-R2：就绪后立即的 dry-run 曾在会话进程内执行扫描与分配，装入
  scipy.optimize 与 scipy.linalg 后会话匿名页常驻多约 25 MB，D2-AC-28 的 S 级 ≤ 150 MB）；等待超过 `REMOTE_WAIT_S` 仍未就绪时退回同步执行。
"""

from __future__ import annotations

import itertools
import logging
import os
import sys
import time
from collections.abc import Callable
from typing import Any

from awr.tasking.plan_kinds import BUDGETS, run_kind

__all__ = ["L_SIM_S", "JobRunner"]


def _plain(o: Any) -> Any:
    """结果摘要用的纯 JSON 形状（numpy 数组与标量转列表与数；墙钟量 `stats`、`plan_ms` 不参与摘要）。"""
    import numpy as np

    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items() if k not in ("stats", "plan_ms")}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    return o

log = logging.getLogger("awr.sim.tasking")
L_SIM_S = {"tasking.allocate": 1.0, "tasking.station": 2.0, "tasking.scan": 5.0, "tasking.sustain": 5.0}
L95_INIT_S = 0.7
T_PLAN_MIN_S = 2.0
REMOTE_WAIT_S = 20.0          # 沙盒会话等待规划池就绪的上限（墙钟），超过退回同步执行


class JobRunner:
    def __init__(self, rt: Any) -> None:
        self.rt = rt
        self.due: list[tuple[int, int, str, Callable[[dict], None], dict]] = []
        self.seq = itertools.count()
        self.l95_s = L95_INIT_S
        self.inflight: dict[str, tuple[float, str]] = {}
        self.stats = {"submitted": 0, "applied": 0, "failed": 0, "inline": 0, "pool": 0, "batch": 0, "deferred": 0}
        self.waiting: list[tuple[float, str, str, dict, Callable[[dict], None], str | None, int]] = []   # 沙盒：等待规划池就绪
        self.remote = os.environ.get("AWR_PLAN_POOL", "") == "remote"
        self.mode_override: str | None = None    # 测试：inline
        self.digests: list[tuple[str, str]] = []  # (类别, 结果 sha256)：生效顺序的作业结果摘要（M20-NFR-004、AC-027 的对拍量）

    # ------------------------------------------------------------ 池
    def _pool(self) -> Any:
        if self.mode_override == "inline":
            return None
        mod = sys.modules.get("awr.sim.mission")
        fn = getattr(mod, "installed_runtime", None) if mod is not None else None
        try:
            m10 = fn() if callable(fn) else None
        except Exception:
            m10 = None
        pool = getattr(m10, "pool", None) if m10 is not None and getattr(m10, "bound", False) else None
        if pool is None or getattr(pool, "state", "ok") == "down" or getattr(pool, "mode", "") == "inline":
            return None
        return pool, m10

    def t_plan_s(self) -> float:
        """派遣与接替阈值中的规划时延项（M20-FR-071）：批处理为 L_sim 之和（站位 + 分配），实时为 max(2 s, r·L95)。"""
        ctx = self.rt.ctx
        if ctx is not None and getattr(ctx, "batch", None) is not None:
            return L_SIM_S["tasking.station"] + L_SIM_S["tasking.allocate"]
        r = float(getattr(ctx, "rate", 1.0) or 1.0) if ctx is not None else 1.0
        return max(T_PLAN_MIN_S, r * self.l95_s)

    # ------------------------------------------------------------ 提交
    def submit(self, kind: str, payload: dict, cb: Callable[[dict], None], *, key: str | None = None) -> str:
        rt = self.rt
        tick = int(rt.tick)
        n = next(self.seq)
        jid = f"{kind}:{key or 'm20'}:{tick}:{n}"
        self.stats["submitted"] += 1
        ctx = rt.ctx
        if ctx is not None and getattr(ctx, "batch", None) is not None:
            t0 = time.monotonic()
            extra = run_kind(kind, payload, rt.world)
            try:
                ctx.batch.record_wait(kind, time.monotonic() - t0)
                lat = int(ctx.batch.lat_ticks(kind, L_SIM_S.get(kind, 1.0) * 1000.0))
            except Exception:
                lat = 1
            self.due.append((tick + max(1, lat), n, jid, cb, extra))
            self.stats["batch"] += 1
            return jid
        pp = self._pool()
        if pp is None and self.remote and self.mode_override != "inline":
            self.waiting.append((time.monotonic(), jid, kind, payload, cb, key, n))
            self.stats["deferred"] += 1
            return jid
        if pp is None:
            extra = run_kind(kind, payload, rt.world)
            self.due.append((tick + 1, n, jid, cb, extra))
            self.stats["inline"] += 1
            return jid
        self._to_pool(pp, jid, kind, payload, cb, key, tick)
        return jid

    def _to_pool(self, pp: tuple[Any, Any], jid: str, kind: str, payload: dict, cb: Callable[[dict], None], key: str | None,
                 tick: int) -> None:
        pool, m10 = pp
        from awr.sim.planning.jobs import PRIO_BACKGROUND, PRIO_INTERACTIVE, PRIO_START, PlanRequest

        prio = {"tasking.scan": PRIO_START, "tasking.allocate": PRIO_INTERACTIVE, "tasking.station": PRIO_INTERACTIVE}.get(kind, PRIO_BACKGROUND)
        req = PlanRequest(jid, kind, tuple(m10.world_key) if m10.world_key else ("", "", ""), (), payload, {}, prio, tick, BUDGETS[kind],
                          f"m20:{key or jid}")
        self.inflight[jid] = (time.monotonic(), kind)

        def done(res: Any, apply_tick: int, jid: str = jid) -> None:
            t0k = self.inflight.pop(jid, None)
            if t0k is not None:
                dt = time.monotonic() - t0k[0]
                # 指数平均的上包络（近似 p95）：大于当前值时快速跟随，小于时缓慢回落
                a = 0.5 if dt > self.l95_s else 0.05
                self.l95_s = (1 - a) * self.l95_s + a * dt
            extra = dict(getattr(res, "extra", None) or {})
            if not extra:
                extra = {"ok": False, "code": int(getattr(res, "code", 0) or 125), "why": str(getattr(res, "detail", None) or "plan_failed")}
                self.stats["failed"] += 1
            self._digest(jid, extra)
            try:
                cb(extra)
                self.stats["applied"] += 1
            except Exception:
                log.exception("tasking job callback failed", extra={"kv": {"job": jid}})

        pool.submit(req, done)
        self.stats["pool"] += 1

    def _release_waiting(self, tick: int) -> None:
        """沙盒：规划池就绪后按提交顺序交给共享规划服务；等待超过 `REMOTE_WAIT_S` 时退回同步执行（下一次 drain 生效）。"""
        pp = self._pool()
        now = time.monotonic()
        keep = []
        for t0, jid, kind, payload, cb, key, n in self.waiting:
            if pp is not None:
                self._to_pool(pp, jid, kind, payload, cb, key, tick)
            elif now - t0 >= REMOTE_WAIT_S:
                log.warning("plan pool unavailable; running the job in-process", extra={"kv": {"job": jid}})
                self.due.append((tick + 1, n, jid, cb, run_kind(kind, payload, self.rt.world)))
                self.stats["inline"] += 1
            else:
                keep.append((t0, jid, kind, payload, cb, key, n))
        self.waiting = keep

    def drain(self, tick: int) -> int:
        if self.waiting:
            self._release_waiting(tick)
        if not self.due:
            return 0
        ready = sorted((d for d in self.due if d[0] <= tick), key=lambda d: (d[0], d[1]))
        if not ready:
            return 0
        self.due = [d for d in self.due if d[0] > tick]
        for _t, _n, jid, cb, extra in ready:
            self._digest(jid, extra)
            try:
                cb(extra)
                self.stats["applied"] += 1
            except Exception:
                log.exception("tasking job callback failed", extra={"kv": {"job": jid}})
        return len(ready)

    def _digest(self, jid: str, extra: dict) -> None:
        from awr.tasking.model import canonical_sha256

        if len(self.digests) >= 4096:
            del self.digests[:1024]
        try:
            self.digests.append((jid.split(":", 1)[0], canonical_sha256(_plain(extra))))
        except Exception:
            self.digests.append((jid.split(":", 1)[0], "unhashable"))

    def flush_inline(self) -> int:
        """同步模式（无规划池、非批处理）下立即生效全部已完成作业（dry-run 在同一查询内给出结果）；其他模式不动作。"""
        ctx = self.rt.ctx
        if (ctx is not None and getattr(ctx, "batch", None) is not None) or self.inflight or self.waiting:
            return 0
        n = 0
        while self.due:
            n += self.drain(max(d[0] for d in self.due))
        return n

    def pending(self) -> int:
        return len(self.due) + len(self.inflight) + len(self.waiting)
