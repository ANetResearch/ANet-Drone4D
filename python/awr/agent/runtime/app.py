"""agent-runtime 进程（`python -m awr.agent.runtime`；M14 §6.1、§6.16.3；M14-FR-021、FR-049、FR-066）。

进程状态机：

| 状态 | 进入条件 | 行为 | 退出 |
|---|---|---|---|
| STARTING | exec | 打开 Bus、attach StateRing（LOSSY 游标）；`layout_id` 不一致即退出（312） | → SYNCING |
| SYNCING | Bus 就绪 | 查 roster；读剧本 `agents` 块；注册 agent；打开并校验证据链（缺口补记）；从协调者链重建任务表；交还遗留 AGENT 租约；非终态任务置 input-required（interrupted） | → READY（`proc/agent-runtime/ready`） |
| READY | — | 正常服务；主循环写 `hb.agent-runtime` | sim-core 不可用 → DEGRADED；SIGTERM → STOPPING |
| DEGRADED | sim-core 心跳超时或 bus 调用 211 | 暂停分配（新任务保持 submitted）；REST 仍可读 | sim-core 恢复 → READY |
| STOPPING | SIGTERM | 取消非终态任务、交还租约、flush 证据链 | 退出 0 |

墙钟循环（FR-051）：调度器轮询 5/50 ms（RingClockDriver）、事件泵 20 ms、事件合批 50 ms、机体状态读取 2 Hz、状态发布 1 Hz、
证据与审计 fsync 1 s（线程池）、心跳 10 Hz。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

__all__ = ["AgentRuntimeApp", "main"]

log = logging.getLogger("awr.agent.app")

EXIT_LAYOUT = 312


class AgentRuntimeApp:
    def __init__(self, ctx: Any, *, bus: Any = None, ring: Any = None, cfg: Any = None) -> None:
        from .config import AgentRuntimeConfig

        self.ctx = ctx
        self.cfg = cfg or AgentRuntimeConfig.from_env()
        self.bus = bus
        self.ring = ring
        self.state = "STARTING"
        self.core: Any = None
        self.publisher: Any = None
        self.rpc: Any = None
        self.bridge: Any = None
        self.driver: Any = None
        self._stop = asyncio.Event()
        self._health: dict[str, str | None] = {}
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------ 状态
    def set_state(self, st: str) -> None:
        if st == self.state:
            return
        log.info("agent-runtime state", extra={"kv": {"from": self.state, "to": st}})
        self.state = st
        if self.core is not None:
            self.core.runtime_state = st
            self.core.tm.set_paused(st == "DEGRADED")
        if self.publisher is not None:
            self.publisher.emit_runtime_state(st)

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------ 启动
    def _attach_ring(self) -> Any:
        from awr.contracts import LAYOUT_ID
        from awr.runtime.statering import LOSSY, LayoutMismatch, StateRing

        path = Path(self.ctx.run_dir) / "state.sim-core"
        try:
            ring = StateRing.attach(path, expect_layout_id=LAYOUT_ID)
        except LayoutMismatch:
            log.error("StateRing layout mismatch", extra={"kv": {"path": str(path)}})
            raise SystemExit(EXIT_LAYOUT) from None
        except (FileNotFoundError, OSError):
            return None
        with contextlib.suppress(Exception):
            ring.register(LOSSY, "agent-runtime")
        return ring

    async def _wait_ring(self) -> Any:
        while not self._stop.is_set():
            r = self._attach_ring()
            if r is not None:
                return r
            await asyncio.sleep(0.2)
        return None

    def _secret(self) -> bytes:
        try:
            return self.ctx.read_secret()
        except (FileNotFoundError, OSError):
            return os.environ.get("AWR_SECRET", "awr-dev-secret").encode()

    async def start(self) -> None:
        from awr.runtime.bus import ZenohBus

        from .bridge_sim import BusSimBridge
        from .clock import RingClockDriver, SimScheduler
        from .core import RuntimeCore
        from .publisher import Publisher
        from .rpc import RuntimeRpc
        from .scenario import load_agents_block

        loop = asyncio.get_running_loop()
        if self.bus is None:
            self.bus = ZenohBus.open("agent-runtime", self.ctx, loop=loop)
        if self.ring is None:
            self.ring = await self._wait_ring()
        self.set_state("SYNCING")
        sched = SimScheduler(0)
        self.bridge = BusSimBridge(self.bus, ring=self.ring, loop=loop)
        hdr = self.bridge.header()
        if hdr is not None:
            sched.epoch, sched.segment = hdr[1], hdr[2]
            sched.rebase(hdr[0])
        block, _doc = load_agents_block(os.environ.get("AWR_SCENARIO"))
        # 事件 epoch 取进程重启序号（跨重启单调），订阅方据此重置 seq 跟踪（17 §9.5）
        self.publisher = Publisher(self.bus, epoch=int(getattr(self.ctx, "restart_count", 0) or 0) + 1)
        persist = Path(self.ctx.persist_dir)
        self.core = RuntimeCore(world_id=self.ctx.world_id, run_id=self.ctx.run_id, secret=self._secret(), sched=sched,
                                bridge=self.bridge, block=block, evidence_dir=persist / "agents" / "evidence",
                                audit_path=persist / "audit.agent-runtime.jsonl", world_seed=self.cfg.world_seed,
                                zero_latency=self.cfg.zero_latency, emit=self.publisher.emit, on_change=self.publisher.mark_dirty)
        self.core.runtime_state = self.state
        self.publisher.core = self.core
        self.rpc = RuntimeRpc(self.core)
        self.rpc.serve(self.bus)
        roster = await self.bridge.refresh_roster()
        self.bridge.read_rows()
        await self.core.register_coordinator()
        await self.core.register_from_roster([e for e in roster if e.get("lifecycle", "READY") in ("READY", 4)])
        interrupted = self.core.tm.restore(list(self.core.ledger(self.core.coord_aid).rows))
        if interrupted:
            log.warning("tasks interrupted by restart", extra={"kv": {"tasks": interrupted}})
        await self._release_leftover_leases()
        self.core.triggers.arm_time_triggers()
        self.driver = RingClockDriver(sched, self.bridge.header, busy_s=self.cfg.poll_busy_s, idle_s=self.cfg.poll_idle_s)
        self._ready = self.bus.ready()
        self.set_state("READY")

    async def _release_leftover_leases(self) -> None:
        """A15：按 return_to = previous 交还本进程名下（本 run 的 agent principal）的遗留 AGENT 租约。"""
        core = self.core
        for vid, aid in list(core.by_vehicle.items()):
            row = self.bridge.vehicle_row(vid)
            if row is None or row.owner != "AGENT":
                continue
            cid = core.guard.next_cid(aid)
            with contextlib.suppress(Exception):
                rep = await self.bridge.lease("release", vid, core.guard.principal(aid, cid), cid=cid, return_to="previous")
                log.info("leftover AGENT lease released", extra={"kv": {"uav": vid, "code": rep.get("code")}})

    # ------------------------------------------------------------ 循环
    async def _loop(self, period: float, fn: Any) -> None:
        while not self._stop.is_set():
            try:
                r = fn()
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("agent-runtime loop failed", extra={"kv": {"fn": getattr(fn, "__name__", str(fn))}})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), period)

    def _pump(self) -> None:
        self.bridge.pump()
        self.publisher.flush_events()
        self.publisher.publish_tasks()

    async def _sync_members(self) -> None:
        """剧本成员在 lifecycle 变为 READY 时注册为 agent（§6.3 生命周期）；roster 变化后补注册。"""
        core = self.core
        missing = [m for m in core.block.members if m.vehicle_id not in core.by_vehicle]
        if not missing:
            return
        by_id = {str(e["id"]): e for e in self.bridge.roster()}
        ready = []
        for m in missing:
            row = self.bridge.vehicle_row(m.vehicle_id)
            if m.vehicle_id in by_id and row is not None and row.lifecycle == "READY":
                ready.append(by_id[m.vehicle_id])
        if ready:
            await core.register_from_roster(ready)

    async def _status(self) -> None:
        await self._sync_members()
        self.publisher.publish_agents()
        for ag in self.core.agents.values():
            h = ag.health()
            if self._health.get(ag.aid, "__none__") != h:
                prev = self._health.get(ag.aid)
                self._health[ag.aid] = h
                self.publisher.emit("agent.health", {"aid": ag.aid, "vehicle_id": ag.vehicle_id, "health": h, "prev": prev}, 1)
        self._watch_degraded()

    def _watch_degraded(self) -> None:
        if self.ring is None:
            return
        try:
            age = float(self.ring.writer_age_ms())
        except Exception:
            age = 1e9
        if age > self.cfg.degraded_ring_age_ms and self.state == "READY":
            self.set_state("DEGRADED")
        elif age <= self.cfg.degraded_ring_age_ms and self.state == "DEGRADED":
            self.set_state("READY")

    async def _fsync(self) -> None:
        def sync() -> None:
            for lg in list(self.core.ledgers.values()):
                lg.sync()
            self.core.guard.audit.sync()

        await asyncio.to_thread(sync)

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self.ctx.add_stop_callback(lambda: loop.call_soon_threadsafe(self._stop.set))
        hb = self.ctx.heartbeat_writer()
        hb_task = asyncio.ensure_future(self._loop(self.cfg.heartbeat_period_s, hb.beat))
        try:
            await self.start()
            self._tasks = [asyncio.ensure_future(self.driver.run()),
                           asyncio.ensure_future(self._loop(self.cfg.pump_period_s, self._pump)),
                           asyncio.ensure_future(self._loop(self.cfg.rows_period_s, self.bridge.read_rows)),
                           asyncio.ensure_future(self._loop(self.cfg.status_period_s, self._status)),
                           asyncio.ensure_future(self._loop(self.cfg.fsync_period_s, self._fsync))]
            await self._stop.wait()
            await self.stop()
        finally:
            hb_task.cancel()
            with contextlib.suppress(Exception):
                hb.close()
        return 0

    async def stop(self) -> None:
        self.set_state("STOPPING")
        self.core.session_state = "closing"
        if self.driver is not None:
            self.driver.stop()
        await self.core.tm.stop_all()
        for vid, aid in list(self.core.guard.lease_holder.items()):
            cid = self.core.guard.next_cid(aid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.bridge.lease("release", vid, self.core.guard.principal(aid, cid), cid=cid,
                                                         return_to="previous"), 3.5)
        for t in self._tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            self.publisher.flush_events()
            self.publisher.publish_tasks(force=True)
        self.core.close()
        with contextlib.suppress(Exception):
            self.rpc.close()
            self._ready.close()
            self.publisher.close()
            self.bridge.close()
            self.bus.close()


def main(argv: list[str] | None = None) -> int:
    from awr.runtime.child import init_child

    ctx = init_child("agent-runtime")
    t0 = time.monotonic()
    code = asyncio.run(AgentRuntimeApp(ctx).run())
    log.info("agent-runtime exit", extra={"kv": {"code": code, "uptime_s": round(time.monotonic() - t0, 1)}})
    return code


if __name__ == "__main__":
    sys.exit(main())
