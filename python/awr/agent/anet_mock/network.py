"""MockNetwork（`AgentNetwork` 的进程内实现；M14 §6.12；M14-FR-044–048）。

消息时序（§6.12.2，全部为仿真时间，L 为键控抽样 U(0.9, 1.1) s，d_resp = 0.05 s）：

```text
find：t_send + 0.2 s
即时能力（task.quote、agent.describe、agent.state）：
  请求投递 t_send（提供方立即开始处理）；回复投递 max(t_done + d_resp, t_send + L(ix/rt))
长任务（两段式能力）：
  请求投递 t_send + L(ix/rt) − d_resp；第一段回执 t_first + d_resp；
  进度第 n 条 t_event + L(ix/upd/n) − d_resp；最终结果 t_event + L(ix/res) − d_resp
取消：t + L(ix/cancel) − d_resp 送达提供方（处理器收到取消：hover、交还租约，以 UNVERIFIED 结束）
```

`ix = "ix_" + sha256(task_id ‖ kind ‖ round ‖ provider_aid)[0:32]`（kind 为 quote 或 exec，round 为该任务对该提供方同类委派的序号）。
委派线格式按 d05 §2.3 简化：`DelegateReq{ix, task_doc{capability, args, requires}, requester, provider}`；最终结果带回执，
请求方按 7 项绑定校验（`daemon.verify_receipt`），失败即 FAILED（486 RECEIPT_INVALID）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..capabilities.catalog import Catalog, catalog
from ..runtime.clock import SimScheduler
from ..runtime.evidence import sha256_cid
from ..runtime.network import AgentView, RecordingSink, TaskResult
from ..runtime.types import CapabilityCall, Effect, EffectStatus, Phase, Update
from . import identity as ID
from .daemon import MockDaemon, verify_receipt
from .hub import MockHub
from .relay import MockRelay

__all__ = ["MockNetwork", "make_ix"]

log = logging.getLogger("awr.agent.anet_mock")

KEEP_FINISHED_S = 120.0


def make_ix(task_id: str, kind: str, round_: int, provider_aid: str) -> str:
    h = hashlib.sha256(f"{task_id}\x00{kind}\x00{round_}\x00{provider_aid}".encode()).hexdigest()
    return "ix_" + h[:32]


@dataclass
class _Deleg:
    ix: str
    requester: str
    provider: str
    capability: str
    args: dict[str, Any]
    task_id: str
    kind: str
    long: bool
    t_send_ns: int
    request_cid: str
    q: asyncio.Queue
    final: asyncio.Future
    task: asyncio.Task | None = None
    n_upd: int = 0
    first_sent: bool = False
    cancel_sent: bool = False
    done: bool = False
    last_t: int = 0  # 同一委派的消息按投递顺序 FIFO（中继按收件箱顺序投递）
    timers: list[Any] = field(default_factory=list)


class MockNetwork:
    """`AgentNetwork` 的 Mock 实现：MockHub + 每 AID 一个 MockDaemon + MockRelay。"""

    network = "mock"

    def __init__(self, sched: SimScheduler, *, secret: bytes = b"awr-dev-secret", run_id: str = "local", world_seed: int = 0,
                 latency: tuple[float, float] = (0.9, 1.1), find_latency_s: float = 0.2, d_resp_s: float = 0.05,
                 zero_latency: bool = False, cat: Catalog | None = None,
                 ledger_for: Callable[[str], Any] | None = None) -> None:
        self.sched = sched
        self.secret = secret
        self.run_id = run_id
        self.cat = cat or catalog()
        self.hub = MockHub()
        self.relay = MockRelay(sched, world_seed=world_seed, latency=latency, find_latency_s=find_latency_s, d_resp_s=d_resp_s,
                               zero=zero_latency)
        self.daemons: dict[str, MockDaemon] = {}
        self.views: dict[str, AgentView] = {}
        self._delegs: dict[str, _Deleg] = {}
        self._rounds: dict[tuple[str, str, str], int] = {}
        self.ledger_for = ledger_for
        self.tamper: Callable[[_Deleg, dict[str, Any], dict[str, Any]], tuple[dict[str, Any], dict[str, Any]]] | None = None
        self.stats = {"delegations": 0, "finds": 0, "receipt_fail": 0, "unavailable": 0}

    # ------------------------------------------------------------ 注册
    def key_of(self, aid: str) -> bytes | None:
        d = self.daemons.get(aid)
        return d.key if d is not None else None

    async def register(self, agent: Any) -> str:
        aid = agent.aid
        caps = list(agent.capabilities())
        key = ID.agent_key(self.secret, self.run_id, aid)
        ledger = self.ledger_for(aid) if self.ledger_for is not None else getattr(agent, "ledger", None)
        self.daemons[aid] = MockDaemon(aid, agent.agent_no, agent, key=key, ledger=ledger)
        self.hub.register(aid, agent.agent_no, caps)
        man = agent.describe()
        self.views[aid] = AgentView(aid, ID.aid_short(aid), int(agent.agent_no), str(getattr(agent, "vehicle_id", "")),
                                    str(getattr(agent, "name", getattr(agent, "vehicle_id", ""))), "uav",
                                    str(getattr(agent, "profile_id", "")),
                                    tuple(c for c in caps if not c.startswith(("agent.", "task.", "blackboard."))),
                                    self.network, str(man.get("manifest_sha256", "")), bool(getattr(agent, "coordinator", False)))
        return aid

    async def unregister(self, aid: str) -> None:
        d = self.daemons.pop(aid, None)
        if d is not None:
            d.alive = False
        self.hub.unregister(aid)
        self.views.pop(aid, None)

    def view(self, aid: str) -> AgentView | None:
        return self.views.get(aid)

    # ------------------------------------------------------------ 发现
    async def find(self, requester: str, pattern: str, *, t0_ns: int | None = None) -> list[AgentView]:
        """发现在 t0 + 0.2 s【仿真】返回；t0 缺省为当前时刻。检出触发的任务以检出事件的仿真时刻为 t0，
        使 agent-runtime 收到事件的墙钟滞后不进入协作时间线（§6.13 规则 ⑥）。"""
        self.stats["finds"] += 1
        t0 = self.sched.now_ns() if t0_ns is None else int(t0_ns)
        await self.sched.sleep_until(t0 + round(self.relay.find_latency_s * 1e9))
        return [self.views[aid] for _no, aid in self.hub.find(pattern) if aid in self.views]

    # ------------------------------------------------------------ 委派
    async def delegate(self, requester: str, provider: str, capability: str, args: Mapping[str, Any], *, task_id: str,
                       attempt: int | None = None) -> str:
        kind = "quote" if capability == "task.quote" else "exec"
        k = (task_id, kind, provider)
        rnd = self._rounds.get(k, 0) + 1 if attempt is None else int(attempt)
        self._rounds[k] = max(self._rounds.get(k, 0), rnd)
        ix = make_ix(task_id, kind, rnd, provider)
        while ix in self._delegs:  # 同键重复委派：追加轮次，保持确定
            rnd += 1
            self._rounds[k] = rnd
            ix = make_ix(task_id, kind, rnd, provider)
        req = {"ix": ix, "task_doc": {"capability": capability, "args": dict(args),
                                      "requires": [{"id": capability, "type": "capability", "necessity": "must"}]},
               "requester": requester, "provider": provider}
        loop = asyncio.get_running_loop()
        d = _Deleg(ix, requester, provider, capability, dict(args), task_id, kind, self.cat.long_running(capability),
                   self.sched.now_ns(), sha256_cid(req), asyncio.Queue(), loop.create_future())
        self._delegs[ix] = d
        self.stats["delegations"] += 1
        L_rt = self.relay.L_ns(f"{ix}/rt")
        daemon = self.daemons.get(provider)
        if daemon is None or not daemon.alive:
            self._schedule_final(d, d.t_send_ns + L_rt, Effect(EffectStatus.UNAVAILABLE, message="provider unreachable"), None)
            return ix
        if d.long:
            t_req = d.t_send_ns + max(0, L_rt - self.relay.d_resp_ns)
            prep = getattr(daemon.provider, "prepare", None)
            if d.kind == "exec" and callable(prep):
                # 委派在途期间提供方预取只读准备（观测点、执行前复核估价），投递时直接取用；不改变任何状态（§6.12.2）
                try:
                    prep(CapabilityCall(d.capability, d.args, d.ix, d.requester, d.task_id, d.ix))
                except Exception:
                    log.exception("provider prepare failed", extra={"kv": {"cap": d.capability}})
            d.timers.append(self.relay.deliver_at(t_req, lambda: self._on_request(d)))
        else:
            d.timers.append(self.relay.deliver_at(d.t_send_ns, lambda: self._on_request(d)))
        return ix

    def _on_request(self, d: _Deleg) -> None:
        daemon = self.daemons.get(d.provider)
        now = self.sched.now_ns()
        if daemon is None or not daemon.alive:
            self._reply_early(d, Effect(EffectStatus.UNAVAILABLE, message="provider unreachable"))
            return
        daemon.stats["received"] += 1
        if daemon.resolve(d.capability) is None:
            daemon.stats["unresolved"] += 1
            self._reply_early(d, Effect(EffectStatus.UNAVAILABLE, message="not served"))
            return
        if d.long:
            if daemon.long_full():
                daemon.stats["busy"] += 1
                self._reply_early(d, Effect(EffectStatus.UNAVAILABLE, message="BUSY"))
                return
            daemon.long_calls.add(d.ix)
            if d.kind == "exec":
                daemon.append("agent.task.awarded", {"task_id": d.task_id, "aid": d.provider, "ix": d.ix,
                                                     "requester": d.requester, "capability": d.capability})
        _ = now
        d.task = asyncio.ensure_future(self._run_handler(d, daemon))

    def _reply_early(self, d: _Deleg, eff: Effect) -> None:
        self.stats["unavailable"] += 1
        now = self.sched.now_ns()
        if d.long:
            t = now + self.relay.d_resp_ns
        else:
            t = max(now + self.relay.d_resp_ns, d.t_send_ns + self.relay.L_ns(f"{d.ix}/rt"))
        self._schedule_final(d, t, eff, None)

    async def _run_handler(self, d: _Deleg, daemon: MockDaemon) -> None:
        sink = RecordingSink()
        sink.on_phase = lambda p, eta, prog: self._on_phase(d, daemon, p, eta, prog, sink)
        call = CapabilityCall(d.capability, d.args, d.ix, d.requester, d.task_id, d.ix)
        last: Effect | None = None
        try:
            agen = daemon.provider.invoke(call, sink)
            async for eff in agen:
                if d.long and not d.first_sent and eff.status is EffectStatus.UNVERIFIED:
                    d.first_sent = True
                    t = self._at(d, self.sched.now_ns() + self.relay.d_resp_ns)
                    upd = Update(d.ix, "first", eff, Phase.QUEUED, eff.metrics.get("eta_s"), 0.0, t_sim_ns=t)
                    d.timers.append(self.relay.deliver_at(t, lambda u=upd: self._put(d, u)))
                    continue
                last = eff
        except asyncio.CancelledError:
            last = Effect(EffectStatus.UNVERIFIED, message="canceled")
        except Exception as ex:
            log.exception("capability handler failed", extra={"kv": {"cap": d.capability}})
            last = Effect(EffectStatus.FAILED, message=f"handler error: {type(ex).__name__}")
        finally:
            daemon.long_calls.discard(d.ix)
        if last is None:
            last = Effect(EffectStatus.FAILED, message="no result")
        now = self.sched.now_ns()
        if d.long:
            # 结果产生时刻：处理器声明的事件时刻（例如 hover 终态，§6.13 规则 ⑥），缺省为处理器结束的时刻
            t_evt = getattr(sink, "t_event_ns", None)
            t0 = int(t_evt) if t_evt and int(t_evt) >= d.t_send_ns else now
            t = t0 + max(0, self.relay.L_ns(f"{d.ix}/res") - self.relay.d_resp_ns)
            if not d.first_sent:
                t = now + self.relay.d_resp_ns  # 第一段即终态（拒单、BUSY、参数非法）
        else:
            t = max(now + self.relay.d_resp_ns, d.t_send_ns + self.relay.L_ns(f"{d.ix}/rt"))
        records = sink.records()
        if d.kind == "exec":
            daemon.append("agent.task.effect", {"task_id": d.task_id, "ix": d.ix, "status": last.status.value,
                                                "verify_trust": last.verify_trust, "simulated": last.simulated,
                                                "metrics": dict(last.metrics), "artifacts_n": len(last.artifacts),
                                                "receipt_verified": None})
        self._schedule_final(d, t, last, records, daemon)

    def _on_phase(self, d: _Deleg, daemon: MockDaemon, p: Phase, eta: float | None, prog: float | None,
                  sink: RecordingSink | None = None) -> None:
        d.n_upd += 1
        n = d.n_upd
        now = self.sched.now_ns()
        # 处理器已声明结果产生时刻（HandlerCtx.event_time）之后的阶段（returning）以该时刻为起点，与最终结果一致；
        # 否则同一委派按投递顺序 FIFO 时，最终结果会被这条未锚定的进度推迟（§6.13 规则 ⑥）
        t_evt = getattr(sink, "t_event_ns", None) if sink is not None else None
        t0 = int(t_evt) if t_evt and d.t_send_ns <= int(t_evt) <= now else now
        daemon.append("agent.task.phase", {"task_id": d.task_id, "ix": d.ix, "phase": Phase(p).value,
                                           "eta_s": None if eta is None else round(float(eta), 3),
                                           "progress": None if prog is None else round(float(prog), 3)})
        t = self._at(d, t0 + max(0, self.relay.L_ns(f"{d.ix}/upd/{n}") - self.relay.d_resp_ns))
        upd = Update(d.ix, "progress", Effect(EffectStatus.UNVERIFIED), Phase(p), eta, prog, t_sim_ns=t)
        d.timers.append(self.relay.deliver_at(t, lambda: self._put(d, upd)))

    def _schedule_final(self, d: _Deleg, t_ns: int, eff: Effect, records: Mapping[str, Any] | None,
                        daemon: MockDaemon | None = None) -> None:
        deliverable = {"effect": eff.to_dict(), "records": dict(records or {})}
        receipt = daemon.receipt(ix=d.ix, requester=d.requester, request_cid=d.request_cid, deliverable=deliverable,
                                 t_ns=self.sched.now_ns()) if daemon is not None else None

        def deliver() -> None:
            deliv, rc = deliverable, receipt
            if self.tamper is not None:
                deliv, rc = self.tamper(d, dict(deliv), dict(rc or {}))
            ok, why = verify_receipt(rc, deliv, ix=d.ix, requester=d.requester, provider=d.provider,
                                     request_cid=d.request_cid, key_of=self.key_of)
            e = Effect.from_dict(deliv["effect"])
            if daemon is not None and not ok:
                self.stats["receipt_fail"] += 1
                e = replace(e, status=EffectStatus.FAILED, message=f"RECEIPT_INVALID {why}", verify_trust=0, simulated=False)
            upd = Update(d.ix, "final", e, None, None, None, receipt=rc, receipt_ok=ok if daemon is not None else False,
                         t_sim_ns=self.sched.now_ns(), records=deliv.get("records"))
            self._put(d, upd)

        d.timers.append(self.relay.deliver_at(self._at(d, t_ns), deliver))

    def _at(self, d: _Deleg, t_ns: int) -> int:
        t = max(int(t_ns), d.last_t)
        d.last_t = t
        return t

    def _put(self, d: _Deleg, u: Update) -> None:
        if d.done:
            return
        d.q.put_nowait(u)
        if u.kind == "final":
            d.done = True
            if not d.final.done():
                d.final.set_result(TaskResult(d.ix, u.effect, u.receipt, bool(u.receipt_ok), u.records, u.t_sim_ns))
            self.sched.call_later(KEEP_FINISHED_S, lambda: self._delegs.pop(d.ix, None))

    # ------------------------------------------------------------ 请求方读取
    async def updates(self, requester: str, ix: str) -> AsyncIterator[Update]:
        d = self._delegs.get(ix)
        if d is None:
            return
        while True:
            u = await d.q.get()
            yield u
            if u.kind == "final":
                return

    async def result(self, requester: str, ix: str, timeout_s: float) -> TaskResult:
        d = self._delegs.get(ix)
        if d is None:
            raise KeyError(ix)
        return await self.sched.wait_for(asyncio.shield(d.final), timeout_s)

    async def cancel(self, requester: str, ix: str) -> None:
        d = self._delegs.get(ix)
        if d is None or d.done or d.cancel_sent:
            return
        d.cancel_sent = True
        t = self.sched.now_ns() + max(0, self.relay.L_ns(f"{ix}/cancel") - self.relay.d_resp_ns)

        def fire() -> None:
            if d.task is not None and not d.task.done():
                d.task.cancel()
            elif d.task is None and not d.done:
                for h in d.timers:
                    with contextlib.suppress(Exception):
                        h.cancel()
                self._schedule_final(d, self.sched.now_ns(), Effect(EffectStatus.UNVERIFIED, message="canceled"), None)

        d.timers.append(self.relay.deliver_at(t, fire))

    def active(self) -> int:
        return sum(1 for d in self._delegs.values() if not d.done)
