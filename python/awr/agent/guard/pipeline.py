"""TrustedGuard 与 CommandPort（M14 §6.14；M14-FR-052–058；ADR-016、ADR-027；n03 §3.8）。

agent-runtime 是 agent 命令唯一的可信入口；能力处理器（与 V1.0 的 LLM/MCP）是不可信指挥官，只能经 CommandPort 下发。
固定顺序、失败即关闭：

| 步 | 检查 | 失败码 |
|---|---|---|
| G0 | 结构：op 在白名单；参数 JSON Schema（commands.json）；数值有限；航点 ≤ 64 | 115；300/110 |
| ① | 身份与角色：已注册 AID、角色 agent（协调者不能指挥机体）；Session 非回放、非关闭中 | 115；118 |
| ② | 限流：合计 20/s 突发 40；每 AID 5/s 突发 10 | 111 |
| ③ | 确认令牌：agent 永远拿不到（kill、escalate、override） | 115 |
| A1 | 能力绑定：目标机 = 该 AID 绑定的机体；命令属于该 AID 的活动委派；op ∈ 能力 `ops` | 482 |
| A2 | 租约前置：非安全类要求本 AID 持有 AGENT 租约；安全类（land、hover、rtl）只对自有租约机体 | 100 |
| A3 | 任务包络：空间目标到任务目标的水平距离 ≤ R_env、离地高度在能力范围、速度 ≤ 限速 | 110（ENVELOPE） |
| A4 | 语义前置：机体不健康时只允许 hover、land、rtl | 105 |
| 签名 | principal `agent:<aid>`，HMAC `K_entry`（17 §9.4） | — |
| 转发 | `ctl/sim-core/cmd` 或 `lease`；1 s × 3【墙钟】，仍失败 211 | 211 |
| 审计 | 放行与拒绝都写审计；拒绝另写证据链 `agent.guard.rejected`；被拒命令不发出任何 bus 消息 | — |

检查器自身异常时整条流水线返回 `470 AGENT_GUARD_INTERNAL`，不下发。生产者侧仍执行 ④–⑩（ADR-016）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from awr.runtime.principal import Principal, sign_principal

from ..runtime.types import Effect, EffectStatus
from .audit import GuardAudit
from .policy import CONFIRM_OPS, OP_WHITELIST, SAFETY_OPS, Envelope, GuardPolicy, check_args
from .ratelimit import RateLimiter

__all__ = ["Admission", "AgentBinding", "CallResult", "CommandPort", "DelegationScope", "LeaseDenied", "TrustedGuard"]

log = logging.getLogger("awr.agent.guard")

Admission = dict[str, Any]
REASON_NAMES = {0: None, 100: "LEASE_DENIED", 105: "STATE", 110: "PARAM_OUT_OF_RANGE", 111: "RATE_LIMITED", 115: "ROLE_FORBIDDEN",
                118: "REPLAY_MODE", 211: "BUS_TIMEOUT", 300: "BAD_REQUEST", 470: "AGENT_GUARD_INTERNAL",
                482: "AGENT_SCOPE_FORBIDDEN"}


class LeaseDenied(Exception):
    def __init__(self, code: int, detail: Any = None) -> None:
        super().__init__(f"lease denied {code}")
        self.code = int(code)
        self.detail = detail


@dataclass(frozen=True)
class CallResult:
    status: str  # succeeded、failed、canceled、timeout、rejected
    code: int
    cid: str
    reason: str | None = None
    effect: Effect | None = None
    raw: Mapping[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


@dataclass
class AgentBinding:
    aid: str
    vehicle_id: str | None
    role: Literal["agent", "coordinator"] = "agent"
    ledger: Any = None
    healthy: Callable[[], str | None] | None = None


@dataclass
class DelegationScope:
    aid: str
    ix: str
    capability: str
    ops: frozenset[str]
    envelope: Envelope | None = None
    cids: set[str] = field(default_factory=set)


class TrustedGuard:
    def __init__(self, *, bridge: Any, k_entry: bytes, sched: Any, policy: GuardPolicy | None = None,
                 limiter: RateLimiter | None = None, audit: GuardAudit | None = None,
                 session_state: Callable[[], str] = lambda: "live") -> None:
        self.bridge = bridge
        self.k_entry = k_entry
        self.sched = sched
        self.policy = policy or GuardPolicy()
        self.limiter = limiter or RateLimiter(rate_total=self.policy.rate_total, burst_total=self.policy.burst_total,
                                              rate_aid=self.policy.rate_aid, burst_aid=self.policy.burst_aid)
        self.audit = audit or GuardAudit(None)
        self.session_state = session_state
        self.agents: dict[str, AgentBinding] = {}
        self.scopes: dict[tuple[str, str], DelegationScope] = {}
        self.lease_holder: dict[str, str] = {}  # vehicle_id -> aid（本进程名下的 AGENT 租约）
        self.lease_listeners: list[Callable[[str, str, str], None]] = []  # (event, vehicle_id, aid)
        self._seq = 0
        self.stats = {"accepted": 0, "rejected": 0, "internal": 0, "forwarded": 0}
        self.fault_inject: Callable[[str], None] | None = None  # 测试注入：在检查器中抛异常（470 用例）

    # ------------------------------------------------------------ 登记
    def bind(self, b: AgentBinding) -> None:
        self.agents[b.aid] = b

    def unbind(self, aid: str) -> None:
        self.agents.pop(aid, None)
        for k in [k for k in self.scopes if k[0] == aid]:
            self.scopes.pop(k, None)

    def open_scope(self, aid: str, ix: str, capability: str, ops: frozenset[str]) -> DelegationScope:
        sc = DelegationScope(aid, ix, capability, frozenset(ops))
        self.scopes[(aid, ix)] = sc
        return sc

    def set_envelope(self, aid: str, ix: str, env: Envelope) -> None:
        sc = self.scopes.get((aid, ix))
        if sc is not None:
            sc.envelope = env

    def close_scope(self, aid: str, ix: str) -> None:
        self.scopes.pop((aid, ix), None)

    def holds_lease(self, aid: str, vehicle_id: str) -> bool:
        return self.lease_holder.get(vehicle_id) == aid

    def on_lease_event(self, ev: Mapping[str, Any]) -> None:
        """sim-core 的 `lease.*` 事件：AGENT 被 OPERATOR 抢占时撤销本地持有记录并通知（A10）。"""
        kind = str(ev.get("kind", ""))
        uav = ev.get("uav")
        data = ev.get("data") or {}
        if not uav:
            return
        holder = self.lease_holder.get(str(uav))
        if holder is None:
            return
        if kind == "lease.preempted" and data.get("owner") == "AGENT" and data.get("holder") in (None, f"agent:{holder}"):
            self.lease_holder.pop(str(uav), None)
            self._lease_ev(holder, str(uav), "preempted", by=data.get("by"))
        elif kind == "lease.expired" and data.get("owner") == "AGENT":
            self.lease_holder.pop(str(uav), None)
            self._lease_ev(holder, str(uav), "released")

    def forget_leases(self) -> None:
        """剧本重置或 sim-core 重启：租约已被生产者清空。"""
        self.lease_holder.clear()

    def _lease_ev(self, aid: str, vehicle: str, what: str, **extra: Any) -> None:
        b = self.agents.get(aid)
        if b is not None and b.ledger is not None:
            lvl = {"acquired": "agent.lease.acquired", "released": "agent.lease.released", "preempted": "agent.lease.preempted"}[what]
            b.ledger.append(lvl, {"vehicle_id": vehicle, "owner": "AGENT", "holder": aid, **{k: v for k, v in extra.items() if v is not None}})
        for cb in list(self.lease_listeners):
            try:
                cb(what, vehicle, aid)
            except Exception:
                log.exception("lease listener failed")

    # ------------------------------------------------------------ principal
    def principal(self, aid: str, cid: str) -> dict[str, Any]:
        pr = Principal(f"agent:{aid}", "agent", "agent-runtime", None, False)
        d = pr.fields()
        d["sig"] = sign_principal(pr, cid, self.k_entry)
        return d

    def next_cid(self, aid: str) -> str:
        self._seq += 1
        return f"ag-{aid[-8:]}-{self._seq:06d}"

    # ------------------------------------------------------------ 流水线
    def _deny(self, aid: str, op: str, uav: str | None, ix: str | None, cid: str, code: int, detail: Any) -> Admission:
        self.stats["rejected"] += 1
        adm = {"v": 1, "cid": cid, "status": "rejected", "code": int(code), "reason": REASON_NAMES.get(int(code)), "detail": detail}
        self.audit.write({"kind": "agent.cmd", "t_sim_ns": self.sched.now_ns(), "principal_id": f"agent:{aid}", "role": "agent",
                          "entry": "agent-runtime", "op": op, "uav": uav, "cid": cid, "ix": ix, "status": "rejected",
                          "code": int(code), "detail": detail})
        b = self.agents.get(aid)
        if b is not None and b.ledger is not None:
            b.ledger.append("agent.guard.rejected", {"op": str(op), "code": int(code), "detail": str(detail) if detail else None,
                                                     "cid": cid, "uav": uav, "ix": ix})
        return adm

    def check(self, aid: str, op: str, uav: str | None, args: Mapping[str, Any], ix: str | None) -> tuple[int, Any]:
        """G0、①–③、A1–A4（不下发、不审计）；返回 (code, detail)。"""
        if self.fault_inject is not None:
            self.fault_inject(op)
        # G0
        if op in CONFIRM_OPS:
            return 115, "CONFIRM_TOKEN_REQUIRED"
        if op not in OP_WHITELIST:
            return 115, "OP_NOT_ALLOWED"
        if not isinstance(args, Mapping):
            return 300, "ARGS"
        code, det = check_args(op, args)
        if code:
            return code, det
        # ①
        b = self.agents.get(aid)
        if b is None:
            return 115, "UNKNOWN_AID"
        if b.role != "agent":
            return 115, "COORDINATOR"
        st = self.session_state()
        if st in ("replay", "closing"):
            return 118, st.upper()
        # ②
        if not self.limiter.allow(aid):
            return 111, "RATE"
        # ③（确认令牌类操作已在 G0 拒绝；租约 override 也需要令牌）
        if op == "acquire" and args.get("priority") == "override":
            return 115, "CONFIRM_TOKEN_REQUIRED"
        # A1
        if uav is None or uav != b.vehicle_id:
            return 482, "OTHER_VEHICLE"
        sc = self.scopes.get((aid, ix or ""))
        if sc is None:
            return 482, "NO_DELEGATION"
        if op not in sc.ops:
            return 482, "OP_NOT_IN_CAPABILITY"
        if op == "cancel" and str(args.get("call_id", "")) not in sc.cids:
            return 482, "NOT_OWN_CALL"
        # A2
        if op not in ("acquire", "release", "cancel") and not self.holds_lease(aid, uav):
            return 100, "NO_AGENT_LEASE" if op not in SAFETY_OPS else "NOT_OWN_LEASE"
        if op == "release" and not self.holds_lease(aid, uav):
            return 100, "NOT_OWN_LEASE"
        # A3
        if sc.envelope is not None:
            why = sc.envelope.violates(op, args)
            if why is not None:
                return 110, f"ENVELOPE {why}"
        # A4
        h = b.healthy() if b.healthy is not None else None
        if h is not None and op not in SAFETY_OPS and op not in ("release", "cancel"):
            return 105, f"UNHEALTHY {h}"
        return 0, None

    async def submit(self, aid: str, op: str, uav: str | None, args: Mapping[str, Any], *, ix: str | None,
                     cid: str | None = None) -> Admission:
        cid = cid or self.next_cid(aid)
        try:
            code, det = self.check(aid, op, uav, args, ix)
        except Exception as ex:
            self.stats["internal"] += 1
            log.exception("guard checker raised")
            return self._deny(aid, op, uav, ix, cid, 470, f"GUARD_INTERNAL {type(ex).__name__}")
        if code:
            return self._deny(aid, op, uav, ix, cid, code, det)
        principal = self.principal(aid, cid)
        try:
            if op in ("acquire", "release"):
                rep = await self.bridge.lease(op, str(uav), principal, cid=cid,
                                              return_to=str(args.get("return_to", "previous")))
            else:
                msg = {"v": 1, "cid": cid, "op": op, "uav": uav, "args": dict(args), "principal": principal, "lease": None,
                       "t_wall_ns": 0, "epoch_seen": int(getattr(self.sched, "epoch", 0)), "batch_id": None}
                sc = self.scopes.get((aid, ix or ""))
                if sc is not None:
                    sc.cids.add(cid)
                rep = await self.bridge.command(msg)
        except Exception as ex:
            self.stats["rejected"] += 1
            self.audit.write({"kind": "agent.cmd", "t_sim_ns": self.sched.now_ns(), "principal_id": f"agent:{aid}", "op": op,
                              "uav": uav, "cid": cid, "ix": ix, "status": "rejected", "code": 211, "detail": type(ex).__name__})
            return {"v": 1, "cid": cid, "status": "rejected", "code": 211, "reason": "BUS_TIMEOUT", "detail": None}
        self.stats["forwarded"] += 1
        rep = dict(rep or {})
        rep.setdefault("cid", cid)
        status = rep.get("status", "rejected")
        rcode = int(rep.get("code", 0) or 0)
        if op in ("acquire", "release") and status == "accepted" and rcode == 0:
            if op == "acquire":
                self.lease_holder[str(uav)] = aid
                self._lease_ev(aid, str(uav), "acquired")
            else:
                self.lease_holder.pop(str(uav), None)
                self._lease_ev(aid, str(uav), "released")
        self.stats["accepted" if status == "accepted" else "rejected"] += 1
        self.audit.write({"kind": "agent.cmd", "t_sim_ns": self.sched.now_ns(), "principal_id": f"agent:{aid}", "role": "agent",
                          "entry": "agent-runtime", "op": op, "uav": uav, "cid": cid, "ix": ix, "status": status, "code": rcode,
                          "detail": rep.get("detail")})
        return rep

    def port_for(self, aid: str, ix: str, *, vehicle_id: str | None = None) -> CommandPort:
        return CommandPort(self, aid, ix, vehicle_id or (self.agents[aid].vehicle_id if aid in self.agents else None))


class CommandPort:
    """交给能力处理器的唯一出口（M14 §9.2）：所有命令都经 TrustedGuard。"""

    def __init__(self, guard: TrustedGuard, aid: str, ix: str, vehicle_id: str | None) -> None:
        self._g = guard
        self._aid = aid
        self._ix = ix
        self._uav = vehicle_id
        self.results: list[CallResult] = []

    async def call(self, op: str, *, timeout_s: float = 600.0, **args: Any) -> CallResult:
        """下发并等到终态（仿真超时）。"""
        adm = await self._g.submit(self._aid, op, self._uav, args, ix=self._ix)
        cid = str(adm.get("cid"))
        if adm.get("status") != "accepted":
            r = CallResult("rejected", int(adm.get("code", 0) or 0), cid, adm.get("reason"), None, adm)
            self.results.append(r)
            return r
        if op in ("acquire", "release"):
            r = CallResult("succeeded", 0, cid, None, Effect(EffectStatus.OK, verify_trust=4, simulated=True), adm)
            self.results.append(r)
            return r
        try:
            res = await self._g.sched.wait_for(self._g.bridge.wait_result(cid, 1e9), timeout_s)
        except TimeoutError:
            r = CallResult("timeout", 202, cid, "TIMEOUT", None, None)
            self.results.append(r)
            return r
        eff = res.get("effect") or {}
        e = None
        if isinstance(eff, Mapping) and "status" in eff:
            try:
                e = Effect.from_dict({"verify_trust": 0, **eff})
            except (ValueError, KeyError):
                e = None
        r = CallResult(str(res.get("status")), int(res.get("code", 0) or 0), cid, res.get("reason"), e, res)
        self.results.append(r)
        return r

    async def call_nowait(self, op: str, **args: Any) -> str:
        adm = await self._g.submit(self._aid, op, self._uav, args, ix=self._ix)
        if adm.get("status") != "accepted":
            raise LeaseDenied(int(adm.get("code", 0) or 0), adm.get("detail")) if int(adm.get("code", 0) or 0) == 100 \
                else RuntimeError(f"command rejected {adm.get('code')}")
        return str(adm.get("cid"))

    async def acquire(self) -> None:
        adm = await self._g.submit(self._aid, "acquire", self._uav, {}, ix=self._ix)
        if adm.get("status") != "accepted":
            raise LeaseDenied(int(adm.get("code", 0) or 100), adm.get("detail"))

    async def release(self, return_to: Literal["previous", "none"] = "previous") -> None:
        if self._uav is None or not self._g.holds_lease(self._aid, self._uav):
            return
        await self._g.submit(self._aid, "release", self._uav, {"return_to": return_to}, ix=self._ix)
