"""sandbox-pool 进程：`python -m awr.sandbox.pool`（M17-FR-024 至 FR-031；AWR-04 §4.9、ADR-089、ADR-113；17 §17.10）。

由 supervisor 按 D1 语义监管（心跳 `hb.sandbox-pool` 10 Hz、`stale_s` 5 s），启动时经 `AWR_CGROUP=sandbox/ctl` 进入
`sandbox/ctl/`。单 asyncio 事件循环装配：

- broker（纯逻辑）与它的副作用实现 `_Effects`：拉起队列（同一时刻至多 `spawn.concurrency` 个会话处于实际拉起阶段）、停止与清理、
  切换底图、载入模板与恢复快照、时钟调用、事件与审计；
- 池命名空间 `awr/_sandbox/pool`：`sys/session/{op}` 服务（`K_pool` 验签，principal 只认 `svc:sandbox-api`，运维 CLI 另以
  `svc:sandbox-ctl` 调用 `ops`、`list`、`get`；±5 s 时间窗），1 Hz 摘要 `state/sandbox-pool/sessions`，事件
  `evt/sandbox-pool/session`（D1 事件形状的批，按 `data.sid` 由 sandbox-api 转发到会话事件环）；
- 每会话总线会话（`sessionbus`）与共享规划服务（`planhost`）；
- 1 Hz：broker tick（空闲、寿命、抢占、派发、治理回调、降级阶梯）、健康检查（StateRing 心跳年龄超过 `stale_s` 且过了启动宽限即
  判挂死：SIGUSR1 转储栈后 SIGKILL，按崩溃处理）、磁盘配额、快照文件清理；≤ 1 Hz 合批落盘 `state.json`；
- 启动：读 `state.json`，按 pid、starttime 与 `AWR_RUN` 核对并以 pidfd 接管仍在运行的沙盒 sim-core（M17-FR-027）；
- 停机（SIGTERM）：停止接受创建，对全部会话导出快照并以 `maintenance` 结束，`stop.grace_s`（12 s）内完成（M17-FR-031）。

D2.1：池命名空间增加 `sys/session/{park, resume, stats}`（17 §17.20.4）。`awr.api` 不能依赖 `awr.sandbox`（AWR-03 §4.2
第 1 条），因此公开容量文档 `awr.capacity.v1` 由池组装（`stats` 回复的 `capacity`：池统计 + show-api 状态文件 + 构建信息 + 节能
阈值，见 `capacity.py`），内部档创建时 sandbox-api 把 `X-AWR-Identity` 原样交给池，由池的 `identity.IdentityService` 验签并查
撤销表（`var/identity/revoked.json`，同一撤销表驱动内部档会话的降档）。驻留与恢复的副作用见 `park.PoolParkMixin`；驻留会话
（PARKED）随 `state.json` 与 `var/sandbox/parked/<sid>/` 持久化，池重启后仍可恢复，维护停机时保留（不随停机结束）。P1：
`pair`（指挥器配对码）与 `reserve`（扩展进程组按需启用时的内存额度预留，只接受 `svc:supervisor`）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from awr.contracts import bus_keys
from awr.contracts.enums import TIMESTATE_NAMES, TimeState
from awr.runtime.binding import RunBinding
from awr.runtime.bus import LocalBus, open_bus, pack
from awr.runtime.procutil import write_private
from awr.runtime.sbauth import POOL_PRINCIPAL, verify_pool_request
from awr.runtime.statering import LayoutMismatch, RingNotReady, StateRing

from .adopt import ExitWatcher, verify_process
from .broker import Broker, Effects, err
from .cgroups import CgroupManager
from .config import PoolSettings
from .ladder import LadderInput
from .model import (
    ACTIVE,
    DRAINING,
    END_STATES,
    IDLE,
    LIVE_STATES,
    OFFERED,
    PARKED,
    PARKING,
    READY,
    SPAWNING,
    SessionRecord,
    Ticket,
    ns,
)
from .park import META_NAME, PoolParkMixin, read_json
from .planhost import PlanHost
from .sessionbus import SessionBus
from .spawner import Launcher, LaunchHandle, ProcessLauncher
from .state import StateStore
from .templates import TemplateCatalog, build_session_config
from .tokens import Keys
from .worlds import WorldTable, load_costs

__all__ = ["CTL_PRINCIPAL", "OPS", "Pool", "main"]

log = logging.getLogger("awr.sandbox.pool")

CTL_PRINCIPAL = "svc:sandbox-ctl"
OPS = ("create", "get", "list", "keepalive", "stop", "rate", "activity", "queue", "world", "template", "restore", "snapshot",
       "meta", "worlds", "templates", "challenge", "batch", "ops", "park", "resume", "stats", "pair", "reserve")
CTL_OPS = ("ops", "list", "get", "meta")
SUPERVISOR_PRINCIPAL = "svc:supervisor"     # 扩展进程组按需启用（M11-FR-232）只调用 reserve
TICK_S = 1.0
TRIM_DELAY_S = 2.0
FLUSH_S = 0.1
HB_S = 0.1
DISK_CHECK_S = 10.0
EXPORT_TIMEOUT_S = 2.0
TS_NAMES = TIMESTATE_NAMES
REJECT_KINDS = ("quota.denied", "pow.failed", "spawn.refused")
STARVED_STALE_FACTOR = 5.0   # 饥饿（限流或 CPU 压力下仍在消耗 CPU）时心跳容限的倍数（2 s → 10 s，D2-FX-R2）
PROGRESS_MIN_CORE = 0.02     # 检查周期内进程 CPU ≥ 0.02 核视为仍在运行


class _Effects(Effects):
    def __init__(self, pool: Pool) -> None:
        self.p = pool

    def spawn(self, rec: SessionRecord, cfg_doc: dict | None, *, restart: bool = False) -> None:
        self.p.spawn_q.put_nowait((rec.sid, cfg_doc, restart))

    def stop(self, rec: SessionRecord, *, export: bool = True) -> None:
        self.p.task(self.p._stop_session(rec.sid, export), f"stop:{rec.sid}")

    def switch(self, rec: SessionRecord, cfg_doc: dict) -> None:
        self.p.task(self.p._switch(rec.sid, cfg_doc), f"switch:{rec.sid}")

    def kill(self, rec: SessionRecord, why: str) -> None:
        h = self.p.handles.get(rec.sid)
        pid = h.pid if h is not None else rec.pid
        if pid:
            self.p.launcher.kill(pid, dump=why == "hung")

    def clock(self, rec: SessionRecord, op: str, args: dict) -> None:
        self.p.task(self.p._clock(rec.sid, op, args), f"clock:{rec.sid}")

    def reset(self, rec: SessionRecord, cfg_doc: dict, reason: str) -> None:
        self.p.task(self.p._reset(rec.sid, cfg_doc, reason), f"reset:{rec.sid}")

    def event(self, kind: str, sid: str, data: dict, severity: int = 1) -> None:
        self.p.emit(kind, sid, data, severity)

    def audit(self, kind: str, **kv: Any) -> None:
        self.p.audit_write(kind, kv)

    def changed(self) -> None:
        self.p.store.mark()

    def ticket_changed(self, t: Ticket) -> None:
        if t.state == OFFERED:
            self.p.emit("sandbox.slot_ready", t.sid, {"ticket_id": t.ticket_id, "claim_deadline_unix_ns": ns(t.offer_deadline),
                                                      "kind": t.kind})
        self.p.store.mark()

    def park(self, rec: SessionRecord) -> None:
        self.p.task(self.p._park_session(rec.sid), f"park:{rec.sid}")

    def resume(self, rec: SessionRecord) -> None:
        self.p.spawn_q.put_nowait((rec.sid, None, "resume"))

    def discard_park(self, rec: SessionRecord) -> None:
        self.p.task(self.p._discard_park(rec.sid), f"discard:{rec.sid}")


class Pool(PoolParkMixin):
    def __init__(self, settings: PoolSettings, *, keys: Keys | None = None, worlds: WorldTable | None = None,
                 templates: TemplateCatalog | None = None, launcher: Launcher | None = None,
                 cgroups: CgroupManager | None = None, planhost: PlanHost | bool | None = None, pool_bus: Any = None,
                 bus_factory: Callable[[str], Any] | None = None, bus_kind: str = "zenoh", run_ctx: Any = None,
                 clock: Callable[[], float] = time.time, audit: Any = None, ring_cls: type = StateRing) -> None:
        self.s = settings
        self.clock = clock
        self.keys = keys
        self.worlds = worlds
        self.templates = templates
        self.launcher = launcher
        self.cgroups = cgroups
        self.planhost_arg = planhost
        self.planhost: PlanHost | None = None
        self.pool_bus = pool_bus
        self._own_pool_bus = pool_bus is None
        self.bus_factory = bus_factory
        self.bus_kind = bus_kind
        self.run_ctx = run_ctx
        self.ring_cls = ring_cls
        self.audit = audit
        self.store = StateStore(settings.state_dir / "state.json")
        self.broker: Broker | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.spawn_q: asyncio.Queue = asyncio.Queue()
        self.handles: dict[str, LaunchHandle] = {}
        self.bindings: dict[str, RunBinding] = {}
        self.sbus: dict[str, SessionBus] = {}
        self.spawning: set[str] = set()
        self.expected_exit: set[str] = set()
        self.exitw: ExitWatcher | None = None
        self.tasks: set[asyncio.Task] = set()
        self._loops: list[asyncio.Task] = []
        self._handles: list[Any] = []
        self._ev: list[dict] = []
        self._ev_seq = 0
        self._pub_events: Any = None
        self._pub_summary: Any = None
        self._deny_last: dict[str, float] = {}
        self._show_ring: Any = None
        self.stopping = False
        self.started = asyncio.Event()
        self.metrics = {"requests": 0, "rejected_sig": 0, "spawns": 0, "spawn_failed": 0, "adopted": 0, "hung": 0, "parks": 0,
                        "resumes": 0, "late_heartbeat": 0}
        self._cpu_seen: dict[str, tuple[float, float, int]] = {}   # sid -> (单调秒, 进程 CPU 秒, pid)：挂死判定的进展
        self._trim_handle: asyncio.TimerHandle | None = None
        self._resume_planned_fallback: set[str] = set()
        self.identity: Any = None             # identity.IdentityService（内部档令牌验签与撤销表）

    # ------------------------------------------------------------ 组装
    def task(self, coro: Any, name: str = "") -> asyncio.Task:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)

        def done(x: asyncio.Task) -> None:
            self.tasks.discard(x)
            if not x.cancelled() and x.exception() is not None:
                log.error("pool task failed", exc_info=x.exception(), extra={"kv": {"task": name}})

        t.add_done_callback(done)
        return t

    def _open_bus(self, namespace: str) -> Any:
        if self.bus_factory is not None:
            return self.bus_factory(namespace)
        kw: dict[str, Any] = {"namespace": namespace, "announce": False}
        ctx = self.run_ctx
        if self.bus_kind == "zenoh" and (ctx is None or ctx.zenoh_config is None) and self.s.rendezvous:
            kw["connect"] = [self.s.rendezvous]
        try:
            return open_bus("sandbox-pool", ctx, kind=self.bus_kind, loop=self.loop, **kw)
        except Exception:
            log.exception("bus open failed; using an isolated local bus", extra={"kv": {"namespace": namespace}})
            return LocalBus.open("sandbox-pool", namespace=f"{namespace}/isolated", loop=self.loop, announce=False)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        s = self.s
        s.state_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(s.state_dir, 0o700)
        for d in (s.runs_dir, s.snapshots_dir, s.parked_dir):
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
        if self.keys is None:
            self.keys = await asyncio.to_thread(Keys.load, s.state_dir, mode=s.access_mode)
        if self.worlds is None:
            costs = load_costs(s.sb.world_costs_path())
            self.worlds = await asyncio.to_thread(WorldTable, s.worlds_dir, costs, allow=s.worlds_allow,
                                                  allow_derive=s.allow_derive)
        if self.templates is None:
            from awr.runtime.config import ROOT

            self.templates = TemplateCatalog(ROOT / "scenarios" / "sandbox")
        if self.cgroups is None:
            self.cgroups = CgroupManager(s.sb, env=s.env or None, delegated=s.delegated)
        if s.delegated is None:
            s.delegated = self.cgroups.delegated
        # 会话父组 cpu.max = sandbox/cpu.max − C_ctl（D2-FX-R2；cgroup-exec.sh 已按同一公式写入，此处以池的配置复写）
        self.cgroups.ensure_sessions_parent(s.cpu_max_sandbox() - s.sb.costs.c_ctl)
        if self.launcher is None:
            self.launcher = ProcessLauncher(s, self.cgroups, ring_cls=self.ring_cls)
        if self.audit is None:
            from awr.api.audit import AuditWriter

            self.audit = AuditWriter(s.state_dir / "audit.jsonl")
        if isinstance(self.planhost_arg, PlanHost):
            self.planhost = self.planhost_arg
        else:
            leaf = self.cgroups.ensure_plan_leaf()
            self.planhost = await asyncio.to_thread(PlanHost, s, cgroup_leaf=leaf, enabled=self.planhost_arg is not False)
        self.broker = Broker(s, self.worlds, self.templates, self.keys, _Effects(self), clock=self.clock)
        self.broker.load(self.store.load())
        await asyncio.to_thread(self._warm)
        if self.identity is None:
            from .identity import IdentityService, default_state_dir

            idir = s.identity_dir or default_state_dir(s.env or None)
            self.identity = await asyncio.to_thread(IdentityService.from_env, s.env or None, state_dir=idir)
        self.broker.is_revoked = self.identity.is_revoked
        if not s.sb.enabled and s.env.get("AWR_SANDBOX_ENABLED") == "0":
            self.broker.accepting = False
        self.exitw = ExitWatcher(self.loop, self._on_proc_exit)
        if self.pool_bus is None:
            self.pool_bus = self._open_bus(bus_keys.POOL_NAMESPACE)
        await self._adopt_all()
        self._pub_events = self.pool_bus.publisher(bus_keys.EVT_SANDBOX_POOL)
        self._pub_summary = self.pool_bus.publisher(bus_keys.STATE_SANDBOX_POOL)
        loop = self.loop
        for op in OPS:   # 回调线程只入队（PY-CB-01）：在事件循环中起任务处理
            self._handles.append(self.pool_bus.serve(bus_keys.sys_session(op),
                                                     lambda req, op=op: loop.call_soon_threadsafe(self._post, op, req)))
        with contextlib.suppress(Exception):
            self._handles.append(self.pool_bus.ready())
        conc = max(1, int(s.sb.spawn.concurrency))
        self._loops += [asyncio.ensure_future(self._spawn_worker()) for _ in range(conc)]
        self._loops += [asyncio.ensure_future(self._tick_loop()), asyncio.ensure_future(self._flush_loop())]
        if self.run_ctx is not None:
            self._loops.append(asyncio.ensure_future(self._heartbeat()))
        self.store.mark()
        self.started.set()
        log.info("sandbox-pool ready", extra={"kv": {"n_max": self.broker.n_max(), "q_cores": s.quota_cores(),
                                                       "delegated": s.is_delegated(), "public": s.public,
                                                       "sessions": len(self.broker.live()),
                                                       "planning": self.planhost.available if self.planhost else False}})
        self.audit_write("pool.start", {"n_max": self.broker.n_max(), "delegated": bool(s.delegated)})

    async def stop(self, *, maintenance: bool = True, grace_s: float | None = None) -> None:
        """停机：停止接受创建并以 maintenance 结束全部会话（导出快照，不等待告知），grace_s 内完成。"""
        if self.stopping:
            return
        self.stopping = True
        b = self.broker
        if b is not None and maintenance:
            prev = b.accepting
            b.accepting = False
            b.drain_all(0.0, "maintenance")
            end = time.monotonic() + (11.0 if grace_s is None else grace_s)
            while time.monotonic() < end and (any(r.state in LIVE_STATES for r in b.sessions.values()) or self.tasks):
                self._flush()
                await asyncio.sleep(0.05)
            b.accepting = prev     # 维护停机不改变运维的接受开关（状态文件记录 `accept off`）
        for h in self._handles:
            with contextlib.suppress(Exception):
                h.close()
        self._handles.clear()
        for t in self._loops:
            t.cancel()
        for t in self._loops:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._loops.clear()
        if getattr(self, "batches", None):
            from .batch import cancel_all

            cancel_all(self)
        for t in list(self.tasks):
            t.cancel()
        self._flush()
        if b is not None:
            with contextlib.suppress(Exception):
                self.store.save(b.dump())
        for sid in list(self.sbus):
            self._close_sbus(sid)
        if self.exitw is not None:
            self.exitw.close()
        if self.planhost is not None:
            await asyncio.to_thread(self.planhost.close)
        if self._own_pool_bus and self.pool_bus is not None:
            with contextlib.suppress(Exception):
                self.pool_bus.close()
        if self.audit is not None:
            await asyncio.to_thread(self.audit.close)

    # ------------------------------------------------------------ 池命名空间服务
    def _post(self, op: str, req: Any) -> None:
        self.task(self._serve(op, req), f"serve:{op}")

    async def _serve(self, op: str, req: Any) -> None:
        self.metrics["requests"] += 1
        try:
            m = req.msg()
        except Exception:
            m = None
        cid = m.get("cid") if isinstance(m, dict) else None
        allowed = (POOL_PRINCIPAL, CTL_PRINCIPAL) if op in CTL_OPS else \
            ((SUPERVISOR_PRINCIPAL,) if op == "reserve" else (POOL_PRINCIPAL,))
        if not await self._started_or_wait() or not verify_pool_request(self.keys.k_pool, op, m, principals=allowed):
            self.metrics["rejected_sig"] += 1
            if isinstance(m, dict):
                self.audit_write("pool.denied", {"op": op, "principal": str(m.get("principal"))[:40]}, reject_key="sig")
            req.reply_msg({"v": 1, "cid": cid, "status": "rejected", "code": 115, "http": 403,
                           "detail": {"why": "signature"}, "result": None})
            return
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        try:
            rep = await self.dispatch(op, args, principal=str(m.get("principal")))
        except Exception as e:
            log.exception("pool op failed", extra={"kv": {"op": op}})
            rep = err(500, 211, why="pool_error", error=type(e).__name__)
        rep = {"v": 1, "cid": cid, **rep}
        req.reply_msg(rep)
        self._flush()

    async def _started_or_wait(self) -> bool:
        if self.broker is not None:
            return True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.started.wait(), 5.0)
        return self.broker is not None

    async def dispatch(self, op: str, args: dict, *, principal: str = POOL_PRINCIPAL) -> dict[str, Any]:
        """池操作（`sys/session/{op}` 的 args → 统一回复）；测试与运维 CLI 也直接调用。"""
        b = self.broker
        assert b is not None
        sid = str(args.get("sid") or "")
        if op == "create":
            if args.get("identity_token") is not None:
                args = dict(args)
                ident = self._verify_identity(args.pop("identity_token"))
                if "code" in ident:
                    return ident
                args["identity"] = ident
            return b.create(args)
        if op == "get":
            return b.get(sid)
        if op == "list":
            return b.list()
        if op == "keepalive":
            return b.keepalive(sid)
        if op == "stop":
            return b.stop(sid, str(args.get("reason") or "user"))
        if op == "rate":
            return b.rate(sid, args.get("rate"))
        if op == "activity":
            return b.activity(args.get("items"))
        if op == "queue":
            return b.queue_op(args)
        if op == "world":
            return b.world_switch(sid, str(args.get("world") or ""), args.get("template"))
        if op == "template":
            return b.template(sid, args.get("template"))
        if op == "restore":
            return b.restore(sid, args.get("snapshot"))
        if op == "snapshot":
            return await self._snapshot_op(sid)
        if op == "meta":
            return b.meta(args.get("addr"))
        if op == "worlds":
            return b.worlds_view()
        if op == "templates":
            w = args.get("world")
            return b.templates_view(str(w) if w else None)
        if op == "challenge":
            return b.challenge(args.get("addr"))
        if op == "batch":
            from .batch import batch_op

            return await batch_op(self, args)
        if op == "ops":
            return await self._ops(args, principal)
        if op == "park":
            return b.park(sid, str(args.get("reason") or "user"))
        if op == "resume":
            return b.resume(sid, token_exp=args.get("token_exp"))
        if op == "pair":
            return b.pair(args)
        if op == "reserve":
            return b.reserve(args.get("mem_mb"), args.get("ttl_s"))
        if op == "stats":
            from .capacity import assemble, build_info, pool_stats, read_show_status, show_status_path

            st = pool_stats(b)
            env = dict(os.environ)
            env.setdefault("AWR_SHM_ROOT", str(self.s.shm_root))
            env.update(self.s.env or {})
            doc = assemble(st, read_show_status(show_status_path(env)), build_info(), self.s.power.public_view())
            return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": {**st, "capacity": doc}}
        return err(404, 305, op=op)

    def _verify_identity(self, token: Any) -> dict[str, Any]:
        """身份令牌 → `{jti, sub, exp}`；无效为 401 `302`（`why = identity_<原因>`），部署未启用为 403 `115`。"""
        from .identity import IdentityInvalid

        try:
            if self.identity is None:
                raise IdentityInvalid("disabled")
            p = self.identity.verify_token(str(token))
        except IdentityInvalid as e:
            if e.why == "disabled":
                return err(403, 115, why="PUBLIC_READONLY")
            return err(401, 302, why=f"identity_{e.why}")
        return {"jti": str(p.get("jti")), "sub": str(p.get("sub")), "exp": int(p.get("exp") or 0)}

    async def _snapshot_op(self, sid: str) -> dict[str, Any]:
        b = self.broker
        assert b is not None
        rec = b.sessions.get(sid)
        if rec is None:
            return err(410, 506, end_reason=None, snapshot_available=False)
        if rec.state in END_STATES:
            p = rec.snapshot_path
            if p and Path(p).exists():
                try:
                    return {"status": "ok", "http": 200, "code": 0, "detail": None,
                            "result": json.loads(Path(p).read_text(encoding="utf-8"))}
                except (OSError, ValueError):
                    pass
            return err(410, 506, end_reason=rec.end_reason, snapshot_available=False)
        if rec.state == PARKED or (rec.state == SPAWNING and rec.resuming):
            snap = read_json(Path(rec.park_dir or self._park_dir(sid)) / "snapshot.json")
            if isinstance(snap, dict):
                return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": snap}
            return err(503, 211, why="snapshot_unavailable", retry_after_s=1)
        if rec.state == SPAWNING:
            return err(503, 211, state=rec.state, retry_after_s=1)
        sb = self.sbus.get(sid)
        snap = await sb.snapshot(timeout=EXPORT_TIMEOUT_S) if sb is not None else None
        if snap is None:
            return err(503, 211, why="snapshot_unavailable", retry_after_s=1)
        if len(json.dumps(snap, separators=(",", ":")).encode()) > b.sb.limits.snapshot_kib * 1024:
            return err(422, 512, why="size")
        return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": snap}

    async def _ops(self, args: dict, principal: str) -> dict[str, Any]:
        """运维（`awr.sandbox.ctl`，M17-FR-065）：status、drain、accept、limits、stop。"""
        b = self.broker
        assert b is not None
        cmd = str(args.get("cmd") or "status")
        self.audit_write("ops." + cmd, {"principal": principal, "args": {k: v for k, v in args.items() if k != "cmd"}})
        if cmd == "status":
            return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": self.status()}
        if cmd == "drain":
            sids = b.drain_all(float(args.get("notice_s", b.sb.notice_s)), "maintenance")
            return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": {"draining": sids}}
        if cmd == "accept":
            b.set_accepting(bool(args.get("on", True)))
            return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": {"accepting": b.accepting}}
        if cmd == "limits":
            v = args.get("max_sessions")
            n = b.set_max_sessions(None if v in (None, "auto") else int(v))
            return {"status": "ok", "http": 200, "code": 0, "detail": None, "result": {"n_max": n}}
        if cmd == "stop":
            return b.stop(str(args.get("sid") or ""), "maintenance")
        return err(422, 300, field="cmd")

    def status(self) -> dict[str, Any]:
        b = self.broker
        assert b is not None
        now = self.clock()
        return {"n_max": b.n_max(), "q_cores": round(self.s.quota_cores(), 3), "delegated": bool(self.s.is_delegated()),
                "accepting": b.accepting, "degrade_level": f"L{b.level}", "queue_len": len(b.queue.waiting()),
                "offered": len(b.queue.offered()), "public": self.s.public,
                "sessions": [{"sid": r.sid, "state": r.state, "world": r.world, "template": r.template,
                              "expires_in_s": round(r.expires - now, 1), "granted_rate": r.granted_rate, "c_i": round(r.c_i, 4),
                              "anon_mb": round(r.anon_mb, 1), "ws": r.ws, "pid": r.pid, "epoch": r.epoch,
                              # 崩溃、挂死与看门狗重启次数（与 epoch 不同，不含驻留恢复；d2-FX-R2-deploy-to-M17 第 1 条，D2-INT）
                              "restarts": int(r.restarts),
                              "end_reason": r.end_reason, "tier": r.tier, "park_reason": r.park_reason,
                              "resume_mode": r.resume_mode, "park_bytes": r.park_bytes} for r in b.sessions.values()],
                "internal": {"reserved_slots": b.reserved_slots(), "in_use": b.n_internal()},
                "parked": sum(1 for r in b.sessions.values() if r.state == PARKED),
                "planning": self.planhost.stats() if self.planhost else None, "metrics": dict(self.metrics),
                "ladder": {k: v for k, v in b.ladder.last.items()}}

    # ------------------------------------------------------------ 事件、审计与摘要
    def emit(self, kind: str, sid: str | None, data: dict, severity: int = 1) -> None:
        self._ev_seq += 1
        d = dict(data)
        if sid is not None:
            d["sid"] = sid
        self._ev.append({"seq": self._ev_seq, "epoch": 1, "producer": "sandbox-pool", "kind": kind, "severity": int(severity),
                         "t_sim_ns": 0, "t_wall_ns": time.time_ns(), "uav": None, "cid": None, "data": d})

    def _flush(self) -> None:
        if self._ev and self._pub_events is not None:
            evs, self._ev = self._ev, []
            with contextlib.suppress(Exception):
                self._pub_events.put(pack(evs))
        b = self.broker
        if b is not None and self.store.flush_due():
            try:
                self.store.save(b.dump())
            except OSError:
                log.exception("state save failed")

    def audit_write(self, kind: str, kv: dict, *, reject_key: str | None = None) -> None:
        if self.audit is None:
            return
        if kind in REJECT_KINDS or reject_key is not None:   # 每来源每秒至多 1 条拒绝类记录（M17-FR-066）
            src = f"{kind}:{kv.get('prefix_h') or reject_key}"
            now = time.monotonic()
            if now - self._deny_last.get(src, -10.0) < 1.0:
                return
            self._deny_last[src] = now
            if len(self._deny_last) > 4096:
                self._deny_last = {k: v for k, v in self._deny_last.items() if now - v < 1.0}
        with contextlib.suppress(Exception):
            self.audit.write(kind, principal_id="svc:sandbox-pool", detail=kv)

    def publish_summary(self) -> None:
        b = self.broker
        if b is None or self._pub_summary is None:
            return
        items = [b.summary(r) for r in b.sessions.values() if r.state not in END_STATES]
        with contextlib.suppress(Exception):
            self._pub_summary.put(pack({"v": 1, "t_unix_ns": str(time.time_ns()), "sessions": items, "n_max": b.n_max(),
                                        "queue_len": len(b.queue.waiting()), "degrade_level": f"L{b.level}"}))

    # ------------------------------------------------------------ 每会话总线
    def _open_sbus(self, rec: SessionRecord, b: RunBinding) -> SessionBus | None:
        cur = self.sbus.get(rec.sid)
        if cur is not None and cur.bus.namespace.startswith(b.namespace):
            return cur
        if cur is not None:
            self._close_sbus(rec.sid)
        try:
            secret = Path(b.secret_path).read_bytes() if b.secret_path else b""
        except OSError:
            return None
        assert self.loop is not None
        sb = SessionBus(rec.sid, self._open_bus(b.namespace), principal_id=rec.principal_id, secret=secret, loop=self.loop,
                        on_perf=self._on_perf)
        sb.start()
        self.sbus[rec.sid] = sb
        if self.planhost is not None:
            self.planhost.attach(rec.sid, sb.bus)
        return sb

    def _close_sbus(self, sid: str) -> None:
        if self.planhost is not None:
            self.planhost.detach(sid)
        sb = self.sbus.pop(sid, None)
        if sb is not None:
            sb.close()

    def _on_perf(self, sid: str, perf: dict) -> None:
        if self.broker is not None:
            self.broker.on_perf(sid, perf)

    # ------------------------------------------------------------ 拉起
    def _prepare_secret(self, rec: SessionRecord, b: RunBinding) -> None:
        rd = self.s.runs_dir / rec.sid
        rd.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(rd, 0o700)
        sec = Path(b.secret_path) if b.secret_path else rd / "secret"
        if not sec.exists():
            write_private(sec, os.urandom(32))

    async def _spawn_worker(self) -> None:
        while True:
            sid, cfg_doc, restart = await self.spawn_q.get()
            b_ = self.broker
            rec = b_.sessions.get(sid) if b_ is not None else None
            if rec is None or rec.state in END_STATES or rec.state == DRAINING:
                continue
            self.spawning.add(sid)
            try:
                if restart == "resume":
                    await self._resume_one(rec)
                else:
                    await self._spawn_one(rec, cfg_doc, bool(restart))
            except Exception:
                log.exception("spawn failed", extra={"kv": {"sid": sid}})
                if b_ is not None:
                    b_.on_spawn_failed(sid, "internal")
            finally:
                self.spawning.discard(sid)
                self._flush()

    async def _spawn_one(self, rec: SessionRecord, cfg_doc: dict | None, restart: bool) -> None:
        bk = self.broker
        assert bk is not None and self.launcher is not None and self.exitw is not None
        sid = rec.sid
        b = bk.binding(rec)
        self.bindings[sid] = b
        self._prepare_secret(rec, b)
        if cfg_doc is not None:
            self.launcher.write_config(rec, cfg_doc)
        self._open_sbus(rec, b)
        self.metrics["spawns"] += 1
        try:
            h = await self.launcher.launch(rec, b, None, restart=restart)
        except Exception as e:
            log.exception("launch failed", extra={"kv": {"sid": sid}})
            self.metrics["spawn_failed"] += 1
            bk.on_spawn_failed(sid, f"launch:{type(e).__name__}")
            return
        self.handles[sid] = h
        bk.on_spawned(sid, h.pid, h.starttime)
        if h.proc is not None:
            self.exitw.watch_child(sid, h.proc)
        else:
            self.exitw.watch_pid(sid, h.pid)
        why = await self.launcher.wait_ready(rec, b, h, self.s.sb.spawn.timeout_s)
        self._settle(sid)
        cur = bk.sessions.get(sid)
        if cur is None or cur.state == DRAINING or cur.state in END_STATES:
            return       # 拉起期间被结束：停止流程负责终止进程与清理
        if why is None:
            bk.on_ready(sid)
            return
        log.warning("spawn not ready", extra={"kv": {"sid": sid, "why": why}})
        self.metrics["spawn_failed"] += 1
        self.expected_exit.add(sid)
        self.handles.pop(sid, None)
        await self.launcher.terminate(h.pid, grace_s=1.0)
        self.exitw.unwatch(sid)
        self.expected_exit.discard(sid)
        if cur.switch is None:
            self._close_sbus(sid)
        bk.on_spawn_failed(sid, "timeout" if why == "timeout" else "crash")

    async def _resume_one(self, rec: SessionRecord) -> None:
        """恢复驻留会话（SB-70 或恢复票认领）：按驻留目录准备配置、checkpoint 与环，拉起后核对恢复口径（M17-FR-073）。"""
        bk = self.broker
        assert bk is not None and self.launcher is not None and self.exitw is not None
        sid = rec.sid
        b = bk.binding(rec)
        self.bindings[sid] = b
        self._prepare_secret(rec, b)
        await asyncio.to_thread(shutil.rmtree, b.run_dir, True)
        cfg_doc, env, t_sim = await asyncio.to_thread(self._prepare_resume, rec, b)
        if cfg_doc is not None:
            self.launcher.write_config(rec, cfg_doc)
        self._open_sbus(rec, b)
        self.metrics["spawns"] += 1
        self.metrics["resumes"] += 1
        try:
            h = await self.launcher.launch(rec, b, None, restart=False, extra_env=env)
        except Exception as e:
            log.exception("resume launch failed", extra={"kv": {"sid": sid}})
            self.metrics["spawn_failed"] += 1
            bk.on_spawn_failed(sid, f"launch:{type(e).__name__}")
            return
        self.handles[sid] = h
        bk.on_spawned(sid, h.pid, h.starttime)
        if h.proc is not None:
            self.exitw.watch_child(sid, h.proc)
        else:
            self.exitw.watch_pid(sid, h.pid)
        why = await self.launcher.wait_ready(rec, b, h, self.s.sb.spawn.timeout_s)
        self._settle(sid)
        cur = bk.sessions.get(sid)
        if cur is None or cur.state == DRAINING or cur.state in END_STATES:
            return
        if why is None:
            bk.on_ready(sid)
            self.task(self._finish_resume(sid, b, t_sim), f"resumed:{sid}")
            return
        log.warning("resume not ready", extra={"kv": {"sid": sid, "why": why}})
        self.metrics["spawn_failed"] += 1
        self.expected_exit.add(sid)
        self.handles.pop(sid, None)
        await self.launcher.terminate(h.pid, grace_s=1.0)
        self.exitw.unwatch(sid)
        self.expected_exit.discard(sid)
        bk.on_spawn_failed(sid, "timeout" if why == "timeout" else "crash")

    def schedule_trim(self, delay_s: float = TRIM_DELAY_S) -> None:
        """会话离开（驻留完成、结束）后归还已释放的堆页（gc + malloc_trim，去抖；D2-FX-R2，D2-AC-06/45）。"""
        if self.loop is None or self._trim_handle is not None:
            return

        def run() -> None:
            self._trim_handle = None
            from awr.runtime.procutil import trim_heap

            with contextlib.suppress(Exception):
                trim_heap()

        self._trim_handle = self.loop.call_later(delay_s, run)

    def _warm(self) -> None:
        """启动预热（D2-FX-R2，D2-AC-06/45）：首个会话才用到的一次性分配（快照 schema 校验器与契约注册表、容量与身份模块、
        模板目录）在启动时完成，使"创建前基线"包含它们，驻留与结束后的回落只反映逐会话的残留。"""
        from . import batch, capacity, identity  # noqa: F401
        from .snapshot import _validator

        with contextlib.suppress(Exception):
            _validator()
        if self.templates is not None and self.worlds is not None:
            for w in list(self.worlds.items):
                with contextlib.suppress(Exception):
                    self.templates.ids(w)
        with contextlib.suppress(Exception):
            build_session_config("synthcity", template_id=None, template=None, snapshot=None, calendar=None,
                                 acoustic_bg=None, seed=1, limits=self.s.limits_obj(), tz="Asia/Shanghai")

    def _settle(self, sid: str) -> None:
        """拉起结束：叶子组 cpu.weight 回到 100（拉起期间加权，见 cgroups.SPAWN_WEIGHT）。"""
        if self.cgroups is not None:
            with contextlib.suppress(Exception):
                self.cgroups.settle(sid)

    def _on_proc_exit(self, sid: str, pid: int, code: int | None) -> None:
        h = self.handles.get(sid)
        if h is not None and h.pid == pid:
            self.handles.pop(sid, None)
        if sid in self.expected_exit or sid in self.spawning:
            return
        b = self.broker
        if b is None:
            return
        rec = b.sessions.get(sid)
        if rec is None or rec.state == DRAINING:
            return
        log.warning("sim-core exited", extra={"kv": {"sid": sid, "pid": pid, "code": code}})
        b.on_exit(sid, code)
        self._flush()

    # ------------------------------------------------------------ 停止、切换、重置、时钟
    async def _wait_spawn_done(self, sid: str) -> None:
        end = time.monotonic() + self.s.sb.spawn.timeout_s + 5.0
        while sid in self.spawning and time.monotonic() < end:
            await asyncio.sleep(0.05)

    async def _stop_session(self, sid: str, export: bool) -> None:
        bk = self.broker
        assert bk is not None and self.launcher is not None
        rec = bk.sessions.get(sid)
        if rec is None:
            return
        await self._wait_spawn_done(sid)
        snap_path = None
        sb = self.sbus.get(sid)
        if export and sb is not None and sid in self.handles:
            snap = await sb.snapshot(timeout=EXPORT_TIMEOUT_S)
            if snap is not None:
                snap_path = await asyncio.to_thread(self._save_snapshot, sid, snap)
        if rec.park_dir:          # 恢复失败的驻留会话：驻留目录中的配置快照另存，随后删除驻留目录
            kept = await asyncio.to_thread(self._park_snapshot_out, rec)
            snap_path = snap_path or kept
            rec.park_dir = None
        h = self.handles.pop(sid, None)
        pid = h.pid if h is not None else rec.pid
        self.expected_exit.add(sid)
        if pid:
            await self.launcher.terminate(pid, grace_s=5.0)
        if self.exitw is not None:
            self.exitw.unwatch(sid)
        self.expected_exit.discard(sid)
        self._close_sbus(sid)
        b = self.bindings.pop(sid, None) or bk.binding(rec)
        await asyncio.to_thread(self.launcher.cleanup, rec, b)
        bk.on_ended(sid, snap_path)
        self._flush()
        self.schedule_trim()

    def _save_snapshot(self, sid: str, snap: dict) -> str:
        p = self.s.snapshots_dir / f"{sid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        write_private(p, json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        return str(p)

    async def _switch(self, sid: str, cfg_doc: dict) -> None:
        """切换底图（M17-FR-010）：备份当前配置快照、停止旧进程（保留 run 目录与 secret）、按新世界拉起。"""
        bk = self.broker
        assert bk is not None and self.launcher is not None
        rec = bk.sessions.get(sid)
        if rec is None:
            return
        await self._wait_spawn_done(sid)
        sw = rec.switch or {}
        sb = self.sbus.get(sid)
        if sb is not None and sid in self.handles and not sw.get("rollback"):
            snap = await sb.snapshot(timeout=EXPORT_TIMEOUT_S)
            if snap is not None and rec.switch is not None:
                w = self.worlds.get(sw.get("from_world")) if self.worlds else None
                rec.switch["backup_cfg"] = build_session_config(sw.get("from_world", rec.world), template_id=sw.get("from_template"),
                                                                template=None, snapshot=snap, calendar=None,
                                                                acoustic_bg=snap.get("acoustic_bg"), seed=rec.seed,
                                                                limits=self.s.limits_obj(), tz=w.tz if w else None)
        h = self.handles.pop(sid, None)
        self.expected_exit.add(sid)
        if h is not None:
            await self.launcher.terminate(h.pid, grace_s=5.0)
        if self.exitw is not None:
            self.exitw.unwatch(sid)
        self.expected_exit.discard(sid)
        self._close_sbus(sid)
        old = self.bindings.pop(sid, None)
        if old is not None:
            await asyncio.to_thread(self.launcher.cleanup, rec, old, keep_run_dir=True)
        cur = bk.sessions.get(sid)
        if cur is not None and cur.state == SPAWNING:
            self.spawn_q.put_nowait((sid, cfg_doc, False))

    async def _reset(self, sid: str, cfg_doc: dict, reason: str) -> None:
        bk = self.broker
        assert bk is not None and self.launcher is not None
        rec = bk.sessions.get(sid)
        if rec is None:
            return
        with contextlib.suppress(OSError):
            await asyncio.to_thread(self.launcher.write_config, rec, cfg_doc)
        sb = self.sbus.get(sid)
        rep = await sb.reset(cfg_doc) if sb is not None else None
        if sb is not None and isinstance(rep, dict) and rep.get("code"):
            await sb.seat_claim()
            rep = await sb.reset(cfg_doc)
        code = int(rep.get("code") or 0) if isinstance(rep, dict) else 211
        bk.on_reset(sid, code, rep.get("problems") if isinstance(rep, dict) else None)
        self._flush()

    async def _clock(self, sid: str, op: str, args: dict) -> None:
        for attempt in range(3):
            sb = self.sbus.get(sid)
            if sb is not None and (op == "play" or attempt > 0):
                await sb.seat_claim()        # 席位固定给会话 principal（幂等）；不依赖 sandbox-api 先登记
            rep = await sb.clock(op, args) if sb is not None else None
            if isinstance(rep, dict) and not rep.get("code"):
                clk = rep.get("clock") if isinstance(rep.get("clock"), dict) else {}
                if self.broker is not None and isinstance(clk.get("state"), str):
                    self.broker.on_ring(sid, clk["state"], clk.get("rate"))
                return
            await asyncio.sleep(0.5 * (attempt + 1))
        log.warning("clock op not applied", extra={"kv": {"sid": sid, "op": op}})

    # ------------------------------------------------------------ 接管
    async def _adopt_all(self) -> None:
        bk = self.broker
        assert bk is not None and self.exitw is not None
        for rec in list(bk.sessions.values()):
            if rec.state in END_STATES:
                continue
            if rec.state == PARKED:
                if rec.drain_at is not None:          # 结束驻留会话的清理在重启前未完成
                    self.task(self._discard_park(rec.sid), f"discard:{rec.sid}")
                elif not (self._park_dir(rec.sid) / META_NAME).exists() and not rec.park_dir:
                    log.warning("parked session without a park directory", extra={"kv": {"sid": rec.sid}})
                continue
            if rec.state == SPAWNING and rec.resuming and rec.park_dir and Path(rec.park_dir).is_dir() and \
                    not verify_process(rec.pid, rec.pid_starttime, rec.sid):
                rec.state, rec.resuming, rec.pid = PARKED, False, None   # 恢复未完成：回到驻留，访客可再次恢复
                rec.parked_at = rec.parked_at or self.clock()
                self.store.mark()
                continue
            b = bk.binding(rec)
            self.bindings[rec.sid] = b
            alive = verify_process(rec.pid, rec.pid_starttime, rec.sid)
            if alive:
                self.metrics["adopted"] += 1
                self.handles[rec.sid] = LaunchHandle(int(rec.pid or 0), rec.pid_starttime, None, 0)
                self.exitw.watch_pid(rec.sid, int(rec.pid or 0))
                self._open_sbus(rec, b)
                if rec.state == DRAINING:
                    self.task(self._stop_session(rec.sid, True), f"stop:{rec.sid}")
                elif rec.state == PARKING:
                    self.task(self._park_session(rec.sid), f"park:{rec.sid}")
                elif rec.state == SPAWNING:
                    self.task(self._await_adopted_ready(rec.sid), f"ready:{rec.sid}")
                log.info("adopted sandbox sim-core", extra={"kv": {"sid": rec.sid, "pid": rec.pid, "state": rec.state}})
                continue
            if rec.pid:
                log.warning("sandbox sim-core not adoptable", extra={"kv": {"sid": rec.sid, "pid": rec.pid}})
            if rec.state == DRAINING:
                rec.pid = None
                self.task(self._stop_session(rec.sid, False), f"stop:{rec.sid}")
            elif rec.state == PARKING:
                rec.pid = None                  # 进程已不在：按持久镜像中的最新 checkpoint 与会话配置完成驻留
                self.task(self._park_session(rec.sid), f"park:{rec.sid}")
            elif rec.state == SPAWNING:
                rec.pid = None
                bk.on_spawn_failed(rec.sid, "pool_restart")
            else:
                bk.on_exit(rec.sid, None)
        await asyncio.to_thread(self._gc_orphans)

    async def _await_adopted_ready(self, sid: str) -> None:
        bk = self.broker
        assert bk is not None and self.launcher is not None
        rec = bk.sessions.get(sid)
        h = self.handles.get(sid)
        if rec is None or h is None:
            return
        why = await self.launcher.wait_ready(rec, self.bindings[sid], h, self.s.sb.spawn.timeout_s)
        if why is None:
            bk.on_ready(sid)
        else:
            bk.on_spawn_failed(sid, why)

    def _gc_orphans(self) -> None:
        """池自己的回收：删除不属于任何会话的 `runs/sb-*`、`/dev/shm/awr/sb-*` 与过期快照（D1 `gc_runs` 不管 `var/sandbox`）。"""
        bk = self.broker
        if bk is None:
            return
        keep = set(bk.sessions)
        for root in (self.s.runs_dir, self.s.shm_root, self.s.parked_dir):
            with contextlib.suppress(OSError):
                orphans = [p for p in root.iterdir() if p.name.startswith("sb-") and p.name not in keep and p.is_dir()]
                if root == self.s.shm_root and orphans:
                    # tmpfs 根可能由同一主机上的其他池共用（测试栈、并行的 e2e）：仍有进程以该 sid 运行的目录不属于"孤儿"
                    live = _live_runs({p.name for p in orphans})
                    orphans = [p for p in orphans if p.name not in live]
                for p in orphans:
                    shutil.rmtree(p, ignore_errors=True)
        with contextlib.suppress(OSError):
            for p in self.s.parked_dir.iterdir():   # 驻留中途中断留下的临时目录
                sid = p.name[1:].removesuffix(".tmp") if p.name.startswith(".") else None
                r = bk.sessions.get(sid) if sid else None
                if sid and (r is None or r.state != PARKING):
                    shutil.rmtree(p, ignore_errors=True)
        snaps = {r.snapshot_path for r in bk.sessions.values() if r.snapshot_path}
        with contextlib.suppress(OSError):
            for p in self.s.snapshots_dir.iterdir():
                if str(p) not in snaps:
                    with contextlib.suppress(OSError):
                        p.unlink()

    # ------------------------------------------------------------ 周期任务
    async def _tick_loop(self) -> None:
        last_disk = -1e9
        last_gc = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            try:
                now_m = time.monotonic()
                if now_m - last_disk >= DISK_CHECK_S:
                    last_disk = now_m
                    used = await asyncio.to_thread(self.launcher.run_dir_bytes) if self.launcher else 0
                    full = used > self.s.sb.disk.total_mb * (1 << 20)
                    if full and self.broker is not None and not self.broker.disk_full:
                        log.error("disk.low", extra={"kv": {"used_mb": round(used / (1 << 20), 1)}})
                    if self.broker is not None:
                        self.broker.disk_full = full
                self._health()
                self.broker.tick(self._readings())
                self.publish_summary()
                if now_m - last_gc >= 60.0:
                    last_gc = now_m
                    await asyncio.to_thread(self._gc_orphans)
            except Exception:
                log.exception("pool tick failed")
            self._flush()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_S)
            self._flush()

    async def _heartbeat(self) -> None:
        hb = self.run_ctx.heartbeat_writer()
        while True:
            with contextlib.suppress(OSError):
                hb.beat()
            await asyncio.sleep(HB_S)

    def _starved(self) -> bool:
        """沙盒组或会话父组正处于 CPU 限流或压力之下（降级阶梯最近一次读数）。"""
        bk = self.broker
        if bk is None:
            return False
        last, c = bk.ladder.last, self.s.sb.ladder
        thr, psi = last.get("throttle"), last.get("psi_cpu")
        return bk.level >= 2 or (isinstance(thr, float) and thr > c.l2_throttle) or \
            (isinstance(psi, (int, float)) and psi > c.l2_psi)

    def _health(self) -> None:
        """挂死判定（M17-FR-008）：StateRing 心跳年龄 > `stale_s`（2 s）且过了启动宽限。D2-FX-R2：沙盒组处于 CPU 限流或压力之下、
        且该进程在最近一个检查周期内仍在消耗 CPU 时，心跳迟到是饥饿而不是无响应，容限放宽到 `STARVED_STALE_FACTOR × stale_s`
        （D2-ACC-1 变体 A 中饥饿的会话被反复判挂死重启，重启的冷启动又加重饥饿）；超过放宽后的容限仍按挂死处理。"""
        bk = self.broker
        if bk is None or self.launcher is None:
            return
        stale_ms = self.s.sb.spawn.stale_s * 1000
        grace = self.s.sb.spawn.startup_grace_s
        now = self.clock()
        mono = time.monotonic()
        starved = self._starved()
        live = set()
        for rec in bk.live():
            if rec.state not in (READY, ACTIVE, IDLE):
                continue
            h = self.handles.get(rec.sid)
            b = self.bindings.get(rec.sid)
            if h is None or b is None or rec.sid in self.spawning:
                continue
            live.add(rec.sid)
            age, hdr = self.launcher.health(b, h.spawn_mono_ns)
            if hdr is not None:
                st = TS_NAMES.get(int(getattr(hdr, "clock_state", -1)))
                bk.on_ring(rec.sid, st, getattr(hdr, "rate_milli", 0) / 1000.0)
            stats = self.cgroups.leaf_stats(rec.sid, h.pid) if self.cgroups else {}
            if "anon_mb" in stats:
                rec.anon_mb = float(stats["anon_mb"])
            progressing = False
            cpu_s = stats.get("cpu_s")
            if isinstance(cpu_s, (int, float)):
                prev = self._cpu_seen.get(rec.sid)
                if prev is not None and prev[2] == h.pid and mono > prev[0]:
                    progressing = (float(cpu_s) - prev[1]) / (mono - prev[0]) >= PROGRESS_MIN_CORE
                self._cpu_seen[rec.sid] = (mono, float(cpu_s), h.pid)
            limit = stale_ms * (STARVED_STALE_FACTOR if starved and progressing else 1.0)
            if (age is None or age > limit) and now - (rec.spawn_at or now) >= grace:
                self.metrics["hung"] += 1
                log.warning("sandbox sim-core hung", extra={"kv": {"sid": rec.sid, "age_ms": age, "starved": starved,
                                                                    "progressing": progressing}})
                self.audit_write("session.hung", {"sid": rec.sid, "age_ms": age, "starved": starved})
                self._cpu_seen.pop(rec.sid, None)
                self.launcher.kill(h.pid, dump=True)
            elif age is not None and age > stale_ms:
                self.metrics["late_heartbeat"] += 1
        for sid in [k for k in self._cpu_seen if k not in live]:
            self._cpu_seen.pop(sid, None)

    def _readings(self) -> LadderInput:
        inp = LadderInput(now=self.clock())
        g = self.cgroups.group_stats() if self.cgroups else None
        if g:
            inp.cpu_stat, inp.mem_mb, inp.psi_cpu, inp.psi_mem = g.get("cpu_stat"), g.get("mem_mb"), g.get("psi_cpu"), g.get("psi_mem")
            inp.cpu_stat_sb = g.get("cpu_stat_sb")
        inp.show_rtf = self._show_rtf()
        return inp

    def _show_rtf(self) -> float | None:
        p = self.s.show_ring
        if p is None:
            return None
        try:
            if self._show_ring is None:
                from awr.contracts import LAYOUT_ID

                self._show_ring = StateRing.attach(p, expect_layout_id=LAYOUT_ID)
            h = self._show_ring.header()
        except (RingNotReady, LayoutMismatch, OSError, ValueError):
            self._show_ring = None
            return None
        if int(h.clock_state) != int(TimeState.PLAYING) or h.rtf_milli <= 0:
            return None
        return h.rtf_milli / 1000.0 / max(h.rate_milli / 1000.0, 1e-3)


def _live_runs(names: set[str]) -> set[str]:
    """names 中仍有进程（环境 `AWR_RUN = <sid>`，同一用户可读）在运行的 sid。"""
    from awr.runtime.procutil import environ_of

    out: set[str] = set()
    with contextlib.suppress(OSError):
        for d in Path("/proc").iterdir():
            if not d.name.isdigit():
                continue
            env = environ_of(int(d.name), ("AWR_RUN",))
            run = env.get("AWR_RUN") if env else None
            if run in names:
                out.add(run)
                if out == names:
                    break
    return out


# ---------------------------------------------------------------- 进程入口
async def _amain(settings: PoolSettings, ctx: Any) -> int:
    pool = Pool(settings, run_ctx=ctx)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if ctx is not None:
        ctx.add_stop_callback(lambda: loop.call_soon_threadsafe(stop.set))
    await pool.start()
    await stop.wait()
    await pool.stop(maintenance=True, grace_s=max(1.0, float(os.environ.get("AWR_STOP_GRACE_S") or 11.0)))
    return 0


def main(argv: list[str] | None = None) -> int:
    from awr.runtime import cgroup
    from awr.runtime.child import init_child
    from awr.runtime.config import ConfigError, sandbox_from_env

    ctx = init_child("sandbox-pool")
    cgroup.join_from_env()
    try:
        sb = sandbox_from_env()
    except ConfigError as e:
        log.error("sandbox config invalid", extra={"kv": {"err": str(e)}})
        return 2
    settings = PoolSettings.from_env(sb)
    return asyncio.run(_amain(settings, ctx))


if __name__ == "__main__":
    raise SystemExit(main())
