"""GatewayHub：sandbox-api 的多会话网关（M11-FR-204 至 FR-206、FR-210、FR-214、FR-215；AWR-04 §4.7、ADR-092；
17 §17.7.1、§17.10；M17 PRD §4.6、§7.5）。

- 池会话镜像（`SessionView`）：来自池命名空间 `awr/_sandbox/pool` 的 `sys/session/list`（启动时与每 2 s【墙钟】对账一次）、
  `state/sandbox-pool/sessions` 1 Hz 摘要与 `evt/sandbox-pool/session` 事件（收到即提前对账）。池不可达时镜像与已附着的
  上下文保持不变（环与会话总线不依赖池，FR-215），只有一次成功的 list 不再列出某会话时才判定其结束；
- `SessionContext` = {RunBinding、该会话自己的总线会话（namespace `awr/<world>/<sid>`）、按 RunBinding 实例化的 D1 Gateway
  （`drive = hub`）、按 per-run secret 构造的 TokenService（`K_entry` 签名进入 sim-core 的命令）、会话设置}；会话进入
  READY、ACTIVE、IDLE、DRAINING 且池给出绑定时创建（BINDING → LIVE），ENDED、FAILED 或对账不再列出时以 4410 关闭全部连接
  （先发 `error{506 或 510}`）并销毁；同一 sid 换世界（底图切换）时以 1012（reason `world_switch`）关闭连接、按新绑定重建；
- 驱动（FR-206）：一个 60 Hz【墙钟】循环按 sid 排序遍历 LIVE 上下文：有连接的全量 `tick_once`，无连接的每 6 个 tick 做一次
  轻量 tick（不读环、不编码）；单轮耗时 > 8 ms 计 `hub_overrun`；事件循环延迟 10 Hz 采样，写入各上下文的 perf；
- 1 Hz：为每个 LIVE 上下文发布 `sandbox/session`（`awr.sandbox.session_state.v1`，FR-214），并以一条
  `sys/session/activity{items: {sid: {ws_n, api_calls, last_api_unix_ns}}}` 向池批量上报活动（M17-FR-042）；
- 外部接口活动（issue #59）：不带 `X-AWR-Client: awr-web/…` 的会话内 REST 调用，以及客户端名不是 `awr-web` 的实时连接上的握手、
  presence 与 `call`（SDK、脚本与其他工具）；`sandbox/session.api_activity_age_s` 给出距最近一次的秒数，页面的节能空闲计时
  据此重置（页面自身的后台请求与连接不计入，空闲标签页照常节能）；
- 池事件 `sandbox.*`（带 `data.sid`）转发到对应上下文的 EventRing（producer `sandbox-pool`），池若已在会话命名空间直接发布
  同类事件（M17-FR-029）则不再转发，避免重复；
- 连接上限（FR-210）：全站 `ws_max_sbx = 4·N_max`（N_max 取池的 list 回复，缺省按 `sandbox:` 段公式）、每地址前缀与每会话
  各 `sandbox.ws.per_prefix`、`per_session`（4）。

`SandboxPolicy`（Gateway 的 `policy`）：入口拒绝清单、按 sid 的限流键与沙盒类别、`sim/speed` 经池授予、serverInfo 的 `sandbox`
块与 `clock.rate_cap`、CLIENT_DATA 上限 50 Hz、WS `call` 计为会话活动。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import msgpack

from awr.contracts import bus_keys
from awr.runtime.binding import RunBinding, is_sid
from awr.runtime.bus import BusError, unpack
from awr.runtime.sbauth import sign_pool_request

from ..ratelimit import sandbox_category_for_service
from .gateway import Gateway
from .protocol import error_msg

if TYPE_CHECKING:
    from ..security import TokenService
    from ..settings import ApiSettings

__all__ = ["HUB_HZ", "GatewayHub", "PoolClient", "PoolUnavailable", "SandboxPolicy", "SessionContext", "SessionView",
           "build_session_state", "limits_from_cfg"]

log = logging.getLogger("awr.api.hub")

HUB_HZ = 60
PERIOD_NS = 1_000_000_000 // HUB_HZ
LIGHT_EVERY = 6
OVERRUN_MS = 8.0
RECONCILE_S = 2.0
ACTIVITY_S = 1.0
ENDED_KEEP_S = 600.0
LIVE_STATES = ("READY", "ACTIVE", "IDLE", "DRAINING")
END_STATES = ("ENDED", "FAILED")
WEB_CLIENT = "awr-web"  # 页面客户端名的前缀（apps/web/src/net/clientName.ts）
PARK_STATES = ("PARKING", "PARKED")  # D2.1（17 §17.20.4）：不在 LIVE_STATES，不记入 ended
CLOSE_IDLE_RECLAIMED = 4430
DENY_PREFIXES = ("sys/", "rec/")
DENY_OPS = {"seat/takeover", "seat/release", "kill", "escalate", "sim/reset"}
LIMITED_BY = {"budget": "cpu_budget", "cpu_budget": "cpu_budget", "cap": "cost_cap", "cost_cap": "cost_cap",
              "measured": "cost_cap", "manual": "manual_control", "manual_control": "manual_control", "l2": "degraded",
              "degraded": "degraded", "pool_unavailable": "degraded"}
EXPIRING_REASONS = ("idle", "lifetime", "preempted", "reclaimed", "user")
CLOSE_SANDBOX_GONE = 4410
CLOSE_WORLD_SWITCH = 1012


LIMIT_KEYS = ("vehicles_max", "targets_max", "nests_max", "tasks_active_max", "cloned_models_max", "rest_rps", "rest_burst",
              "call_rps", "call_burst", "clock_rps", "entity_rps", "task_rps", "ws_max", "setpoint_hz_max", "snapshot_kib",
              "plan")
PLAN_LIMITS = {"area_max_m2": 1_000_000, "vertices_max": 64, "strips_max": 200, "work_items_max": 50,
               "station_candidates_max": 2400, "coverage_cells_max": 250_000}  # 17 §17.3.6 规划输入规模


def limits_from_cfg(sb: Any) -> dict[str, Any]:
    """`awr.sandbox.limits.v1`（17 §17.4.1）的完整对象：由 `sandbox:` 段推出（池给出的值优先，见 server_info_block）。"""
    lim, r = sb.limits, sb.rest
    return {"vehicles_max": lim.vehicles, "targets_max": lim.targets, "nests_max": lim.nests,
            "tasks_active_max": lim.active_tasks, "cloned_models_max": lim.cloned_models, "rest_rps": float(r.rps),
            "rest_burst": int(r.burst), "call_rps": float(r.call_rps), "call_burst": int(r.call_burst),
            "clock_rps": float(r.clock_rps), "entity_rps": float(r.entity_rps), "task_rps": float(r.task_rps),
            "ws_max": int(sb.ws.per_session), "setpoint_hz_max": float(r.setpoint_hz),
            "snapshot_kib": float(lim.snapshot_kib), "plan": dict(PLAN_LIMITS)}


class PoolUnavailable(Exception):
    """池命名空间无回复（池重启或未启动）：调用方按 503 `211` 或 `502{why: pool_unavailable}` 处理。"""

    code = 211


# ---------------------------------------------------------------- 池会话镜像
def _int_ns(v: Any) -> int | None:
    try:
        return int(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError):
        return None


@dataclass
class SessionView:
    """池会话表一行的镜像（字段名按 17 §17.4.1、§17.10.1；未知字段保留在 raw）。"""

    sid: str
    state: str
    world_id: str
    template_id: str | None = None
    principal_id: str | None = None
    expires_unix_ns: int | None = None
    max_expires_unix_ns: int | None = None
    renewable: bool = True
    renew_block_reason: str | None = None
    limits: dict | None = None
    binding: RunBinding | None = None
    end_reason: str | None = None
    epoch: int = 0
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_msg(cls, d: Any) -> SessionView | None:
        if not isinstance(d, dict):
            return None
        sid = d.get("sid") or (d.get("binding") or {}).get("run_id")
        if not is_sid(sid):
            return None
        b = None
        bd = d.get("binding") or d.get("run_binding")
        if isinstance(bd, dict):
            try:
                b = RunBinding.from_msg({"run_id": sid, **bd})
            except ValueError:
                b = None
        world = str(d.get("world_id") or d.get("world") or (b.world_id if b else ""))
        return cls(sid=str(sid), state=str(d.get("state") or "SPAWNING"), world_id=world,
                   template_id=d.get("template_id") if isinstance(d.get("template_id"), str) else d.get("template"),
                   principal_id=d.get("principal_id") if isinstance(d.get("principal_id"), str) else None,
                   expires_unix_ns=_int_ns(d.get("expires_unix_ns")),
                   max_expires_unix_ns=_int_ns(d.get("max_expires_unix_ns") or d.get("lifetime_max_unix_ns")),
                   renewable=bool(d.get("renewable", True)),
                   renew_block_reason=d.get("renew_block_reason") if isinstance(d.get("renew_block_reason"), str) else None,
                   limits=d.get("limits") if isinstance(d.get("limits"), dict) else None, binding=b,
                   end_reason=d.get("end_reason") if isinstance(d.get("end_reason"), str) else None,
                   epoch=int(d.get("epoch") or 0) if isinstance(d.get("epoch"), int) else 0, raw=dict(d))

    def server_info_block(self, defaults: dict | None = None) -> dict[str, Any]:
        """serverInfo.sandbox（17 §17.7.1）；limits 以 `sandbox:` 段推出的完整对象为底、池给出的字段覆盖。"""
        out: dict[str, Any] = {"sid": self.sid, "state": self.state, "world_id": self.world_id,
                               "template_id": self.template_id, "renewable": self.renewable}
        if self.expires_unix_ns is not None:
            out["expires_unix_ns"] = str(self.expires_unix_ns)
        lim = dict(defaults or {}) | {k: v for k, v in (self.limits or {}).items() if k in LIMIT_KEYS and v is not None}
        if lim:
            out["limits"] = lim
        return out


def is_web_client(name: str | None) -> bool:
    """页面客户端（REST 请求头 `X-AWR-Client` 与实时 hello 的 `client` 为 `awr-web/<版本>`）。"""
    return bool(name) and str(name).startswith(WEB_CLIENT)


def build_session_state(view: SessionView, summary: dict | None, gw: Gateway | None, *, now_ns: int | None = None,
                        api_activity_ns: int = 0) -> dict:
    """`sandbox/session` 载荷（`awr.sandbox.session_state.v1`，17 §17.7.4）：池摘要优先，缺项由镜像与网关补齐。
    `api_activity_ns` > 0 时给出 `api_activity_age_s`（距最近一次外部接口活动的秒数，与客户端时钟偏差无关，issue #59）。"""
    sm = summary or {}
    now = time.time_ns() if now_ns is None else now_ns
    exp = _int_ns(sm.get("expires_unix_ns")) or view.expires_unix_ns or now
    clk = sm.get("clock") if isinstance(sm.get("clock"), dict) else {}
    granted = clk.get("rate_granted", sm.get("granted_rate", 1.0))
    cap = clk.get("rate_cap", sm.get("rate_cap", 10.0))
    lb = LIMITED_BY.get(str(clk.get("limited_by", sm.get("limited_by")))) if (clk.get("limited_by") or
                                                                              sm.get("limited_by")) else None
    clock: dict[str, Any] = {"rate_granted": float(granted), "rate_cap": float(cap), "limited_by": lb}
    req = clk.get("rate_requested", sm.get("requested_rate"))
    if isinstance(req, (int, float)) and not isinstance(req, bool):
        clock["rate_requested"] = float(req)
    usage_in = sm.get("usage") if isinstance(sm.get("usage"), dict) else {}
    usage = {"vehicles": len(gw.roster) if gw is not None else int(usage_in.get("vehicles", 0)),
             "targets": len(gw.targets) if gw is not None else int(usage_in.get("targets", 0)),
             "nests": int(usage_in.get("nests", _count(gw, "nest/") if gw is not None else 0)),
             "tasks_active": int(usage_in.get("tasks_active", usage_in.get("tasks", 0))),
             "ws": len(gw.sessions) if gw is not None else int(usage_in.get("ws", 0))}
    if isinstance(usage_in.get("cloned_models"), int):
        usage["cloned_models"] = int(usage_in["cloned_models"])
    lvl = sm.get("degrade_level", "L0")
    lvl = f"L{lvl}" if isinstance(lvl, int) else (lvl if lvl in ("L0", "L1", "L2", "L3", "L4") else "L0")
    expiring = None
    ex = sm.get("expiring")
    if isinstance(ex, dict) and ex.get("reason") in EXPIRING_REASONS:
        in_s = ex.get("in_s")
        if not isinstance(in_s, (int, float)):
            at = _int_ns(ex.get("end_unix_ns") or ex.get("at_unix_ns"))
            in_s = max(0.0, (at - now) / 1e9) if at is not None else 0.0
        expiring = {"reason": ex["reason"], "in_s": max(0.0, float(in_s))}
        if ex.get("action") in ("end", "park"):
            expiring["action"] = ex["action"]  # D2.1（17 §17.20.4；d2-WP-04b-to-M11 第 4 条）
    rbr = sm.get("renew_block_reason", view.renew_block_reason)
    out: dict[str, Any] = {"sid": view.sid, "state": str(sm.get("state") or view.state), "world_id": view.world_id,
                           "template_id": view.template_id, "expires_unix_ns": str(int(exp)),
                           "expires_in_s": max(0, int((exp - now) // 1_000_000_000)),
                           "renewable": bool(sm.get("renewable", view.renewable)),
                           "renew_block_reason": rbr if rbr in ("queue_nonempty", "max_lifetime", "degraded") else None,
                           "clock": clock, "usage": usage, "queue_len": int(sm.get("queue_len", 0) or 0),
                           "degrade_level": lvl, "expiring": expiring}
    mx = _int_ns(sm.get("max_expires_unix_ns")) or view.max_expires_unix_ns
    if mx is not None:
        out["max_expires_unix_ns"] = str(mx)
    if isinstance(sm.get("limits_rev"), int):
        out["limits_rev"] = int(sm["limits_rev"])
    if api_activity_ns > 0:
        out["api_activity_age_s"] = round(max(0.0, (now - api_activity_ns) / 1e9), 1)
    return out


def _count(gw: Gateway, head: str) -> int:
    return sum(1 for t in gw.registry.by_topic if t.startswith(head))


# ---------------------------------------------------------------- 池客户端
class PoolClient:
    """池命名空间 `awr/_sandbox/pool` 的客户端：`sys/session/{op}` 以 `K_pool` 签名（principal `svc:sandbox-api`）。"""

    def __init__(self, bus: Any, k_pool: bytes) -> None:
        self.bus = bus
        self.k_pool = k_pool
        self.stats = {"calls": 0, "unavailable": 0}

    async def call(self, op: str, args: dict | None = None, *, timeout: float = 1.0, retries: int = 1) -> dict:
        """一次签名请求；无回复或总线错误抛 PoolUnavailable。"""
        self.stats["calls"] += 1
        msg = sign_pool_request(self.k_pool, op, args or {})
        try:
            rep = await self.bus.call(bus_keys.sys_session(op), msg, timeout=timeout, retries=retries, retry_gap=0.2)
        except BusError as e:
            self.stats["unavailable"] += 1
            raise PoolUnavailable(str(e)) from None
        if not isinstance(rep, dict):
            self.stats["unavailable"] += 1
            raise PoolUnavailable("pool reply is not a map")
        return rep

    def cast(self, op: str, args: dict | None, on_reply: Callable[[Any, BaseException | None], None],
             *, timeout: float = 1.0) -> None:
        """不等待的签名请求（活动上报）；回复在总线回调线程中交给 on_reply（只允许入队）。"""
        msg = sign_pool_request(self.k_pool, op, args or {})
        self.bus.call_cb(bus_keys.sys_session(op), msg, on_reply, timeout=timeout, retries=0)


# ---------------------------------------------------------------- 沙盒调用策略
class SandboxPolicy:
    """单个沙盒会话的 Gateway `policy`（见模块说明）。"""

    def __init__(self, hub: GatewayHub, sid: str) -> None:
        self.hub = hub
        self.sid = sid
        self.direct_kinds: set[str] = set()
        self.granted: float | None = None
        self.cap: float | None = None

    def deny_code(self, op: str, service: str) -> int | None:
        if op in DENY_OPS or service in DENY_OPS or any(service.startswith(p) or op.startswith(p) for p in DENY_PREFIXES):
            return 115
        return None

    def limit_key(self, pid: str) -> str:
        return f"sid:{self.sid}"

    def categories(self, op: str) -> list[str]:
        self.note_activity()
        return sandbox_category_for_service(op)

    def note_activity(self) -> None:
        self.hub.note_api(self.sid)

    def setpoint_hz(self) -> float:
        return float(self.hub.sb.rest.setpoint_hz)

    async def grant_rate(self, rate: float) -> tuple[float, dict]:
        """池的倍速授予 `sys/session/rate{sid, rate}` → (授予值, {rate_cap, limited_by})；池不可达时取 min(rate, 1)。"""
        try:
            rep = await self.hub.pool_call("rate", {"sid": self.sid, "rate": float(rate)}, timeout=1.0)
            res = rep.get("result") if isinstance(rep.get("result"), dict) else rep
            granted = float(res.get("granted", res.get("granted_rate", min(rate, 1.0))))
            cap = res.get("cap", res.get("rate_cap"))
            info = {"rate_cap": float(cap) if isinstance(cap, (int, float)) else None,
                    "limited_by": res.get("limited_by")}
        except PoolUnavailable:
            granted, info = min(float(rate), 1.0), {"rate_cap": None, "limited_by": "pool_unavailable"}
        self.granted = granted
        if info.get("rate_cap") is not None:
            self.cap = info["rate_cap"]
        return granted, info

    def rate_cap(self) -> float | None:
        if self.cap is not None:
            return self.cap
        sm = self.hub.summaries.get(self.sid) or {}
        clk = sm.get("clock") if isinstance(sm.get("clock"), dict) else {}
        cap = clk.get("rate_cap", sm.get("rate_cap"))
        return float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) else None

    def server_info_block(self) -> dict[str, Any] | None:
        v = self.hub.view(self.sid)
        return v.server_info_block(self.hub.default_limits) if v is not None else None

    def on_direct_pool_event(self, kind: str) -> None:
        self.direct_kinds.add(kind)


# ---------------------------------------------------------------- 会话上下文
class SessionContext:
    """一个沙盒会话的运行上下文；属性面与 show-api 的 ApiContext 相同（领域路由经 `ctx_for` 透明使用）。"""

    kind = "session"

    def __init__(self, hub: GatewayHub, view: SessionView, binding: RunBinding) -> None:
        self.hub = hub
        self.sid = view.sid
        self.view = view
        self.binding = binding
        self.principal_id = view.principal_id
        self.state = "BINDING"
        self.settings: ApiSettings | None = None
        self.tokens: TokenService | None = None
        self.bus: Any = None
        self.gateway: Gateway | None = None
        self.catalog = hub.app.catalog
        self.audit = hub.app.audit
        self.limiter = hub.app.limiter
        self.world_files = getattr(hub.app, "world_files", None)
        self.run_ctx = None
        self.world_info: dict[str, Any] = {}
        self.phase = secrets.randbelow(LIGHT_EVERY)
        self.started_mono = time.monotonic()

    @property
    def live(self) -> bool:
        return self.state == "LIVE" and self.gateway is not None

    async def open(self) -> None:
        """BINDING：读 per-run secret、打开会话总线、实例化并启动 Gateway（drive = hub），预置席位。"""
        from ..security import TokenService

        hub = self.hub
        b = self.binding
        secret = await asyncio.to_thread(b.read_secret)
        self.settings = hub.app.settings.for_binding(b, secret)
        self.tokens = TokenService(self.settings)
        self.world_info = await asyncio.to_thread(hub.world_info, b.world_id)
        self.bus = hub.bus_factory(b)
        kw: dict[str, Any] = {"binding": b, "drive": "hub", "event_ring": hub.sb.event_ring,
                              "seat_pinned": self.principal_id, "policy": SandboxPolicy(hub, self.sid),
                              "world_info": self.world_info, "audit": self.audit, "limiter": self.limiter,
                              "confirm_key": self.tokens.k_confirm}
        if hub.ring_cls is not None:
            kw["ring_cls"] = hub.ring_cls
        self.gateway = Gateway(self.settings, self.bus, self.tokens, **kw)
        self.gateway.power_info = getattr(hub.app, "power_info", None)  # D2.1 serverInfo.power（M11-FR-229）
        await self.gateway.start()
        self.gateway.registry.get_or_create("sandbox/session", entity=None, producer="sandbox-api")
        self.state = "LIVE"

    async def close(self, code: int = 1001, *, reason: str = "ended", err: int | None = None,
                    detail: dict | None = None) -> None:
        """以 code 关闭全部连接（先发 error{err, detail}），停止 Gateway，关闭会话总线（CLOSING → 删除）。"""
        self.state = "CLOSING"
        gw = self.gateway
        if gw is not None:
            for s in list(gw.sessions):
                if err is not None:
                    m = error_msg(err, "session", None, f"沙盒 {self.sid} 不可用（{reason}）")
                    if detail:
                        m["detail"] = dict(detail)
                    s.send_ctrl(m)
                s.flush_events()
            await asyncio.sleep(0.05)
            for s in list(gw.sessions):
                s.closing = True
                s.wake.set()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(s.ws.close(code=code, reason=reason[:120]), 1.0)
            with contextlib.suppress(Exception):
                await gw.stop()
        if self.bus is not None:
            with contextlib.suppress(Exception):
                self.bus.close()
        self.gateway = None
        self.bus = None


# ---------------------------------------------------------------- Hub
class GatewayHub:
    def __init__(self, app: Any, *, bus_factory: Callable[[RunBinding], Any], pool: PoolClient | None,
                 pool_bus: Any = None, ring_cls: type | None = None) -> None:
        self.app = app
        self.sb = app.sb
        self.bus_factory = bus_factory
        self.pool = pool
        self.pool_bus = pool_bus
        self.ring_cls = ring_cls
        self.views: dict[str, SessionView] = {}
        self.summaries: dict[str, dict] = {}
        self.contexts: dict[str, SessionContext] = {}
        self.ended: dict[str, tuple[float, str, bool]] = {}  # sid -> (单调秒, 结束原因, 快照可取)
        self.api_calls: dict[str, int] = {}
        self.last_api_ns: dict[str, int] = {}
        self.last_ext_ns: dict[str, int] = {}  # 外部接口活动（非页面客户端的会话内 REST，issue #59）
        self.n_max: int | None = None
        self.pool_up = False
        self.inbox: asyncio.Queue | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopping = False
        self.k = 0
        self.metrics = {"ticks": 0, "overruns": 0, "reconciles": 0, "reconcile_fail": 0, "attach_fail": 0,
                        "events_forwarded": 0, "light_ticks": 0}
        self._live_waiters: dict[str, list[asyncio.Future]] = {}
        self._tasks: list[asyncio.Task] = []
        self._handles: list[Any] = []
        self._reconcile_now: asyncio.Event | None = None
        self._lock: asyncio.Lock | None = None
        self._last_lag_ms = 0.0
        self._trim_handle: asyncio.TimerHandle | None = None
        self.default_limits = limits_from_cfg(self.sb)

    # ------------------------------------------------------------ 查询
    def get(self, sid: str) -> SessionContext | None:
        return self.contexts.get(sid)

    def view(self, sid: str) -> SessionView | None:
        return self.views.get(sid)

    def ended_reason(self, sid: str) -> str | None:
        e = self.ended.get(sid)
        return e[1] if e is not None else None

    def snapshot_available(self, sid: str) -> bool:
        e = self.ended.get(sid)
        if e is not None:
            return e[2]
        v = self.views.get(sid)
        return v is not None and bool(v.raw.get("snapshot_available", v.state not in END_STATES))

    def note_api(self, sid: str, client: str | None = None) -> None:
        """一次会话内 REST 调用（全部计入池的兜底驻留）；`client` 为请求头 `X-AWR-Client`，不是页面客户端时另记外部接口活动。"""
        now = time.time_ns()
        self.api_calls[sid] = self.api_calls.get(sid, 0) + 1
        self.last_api_ns[sid] = now
        if not is_web_client(client):
            self.last_ext_ns[sid] = now

    def external_activity_ns(self, sid: str, gw: Gateway | None) -> int:
        """最近一次外部接口活动（墙钟 ns，0 为没有）：外部 REST 与非页面客户端连接的最近交互（握手、presence、call）的最大值；
        hello 之前的连接不计（客户端名未知，页面重连时不算外部活动）。"""
        t = self.last_ext_ns.get(sid, 0)
        for s in list(gw.sessions) if gw is not None else []:
            if getattr(s, "hello", False) and not s.closing and not is_web_client(getattr(s, "client", "")):
                t = max(t, int(getattr(s, "last_input_ns", 0) or 0))
        return t

    @property
    def ws_max(self) -> int:
        return self.sb.ws_max_for(self.effective_n_max)

    @property
    def effective_n_max(self) -> int:
        return self.n_max if self.n_max is not None else self.sb.n_max()

    def conn_counts(self, prefix: str, sid: str) -> tuple[int, int, int]:
        """(全站, 本前缀, 本会话) 的 WS 连接数。"""
        total = per_pfx = per_sid = 0
        for s_id, c in self.contexts.items():
            gw = c.gateway
            if gw is None:
                continue
            for s in gw.sessions:
                total += 1
                if getattr(s, "client_prefix", None) == prefix:
                    per_pfx += 1
                if s_id == sid:
                    per_sid += 1
        return total, per_pfx, per_sid

    def world_info(self, world_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {"id": world_id, "frame": "world", "contentVersion": None, "coordinateSha256": None,
                                "anchorKind": None, "georeferenced": None}
        try:
            d = self.app.catalog.get(world_id)
        except Exception:
            d = None
        if d is not None:
            info.update(contentVersion=d.content_version, coordinateSha256=d.coordinate_sha256,
                        anchorKind=d.anchor_kind, georeferenced=d.georeferenced)
        return info

    async def pool_call(self, op: str, args: dict | None = None, *, timeout: float = 1.0) -> dict:
        """池命名空间请求（M17 的 rest/sandbox.py 经此调用 `sys/session/*`）；池不可达抛 PoolUnavailable。"""
        if self.pool is None:
            raise PoolUnavailable("no pool client")
        rep = await self.pool.call(op, args, timeout=timeout)
        self.pool_up = True
        return rep

    async def wait_live(self, sid: str, timeout: float) -> SessionContext | None:
        """等会话上下文进入 LIVE（WS 在 SPAWNING 期间接入，17 §17.7.1）；结束或超时返回 None。"""
        c = self.contexts.get(sid)
        if c is not None and c.live:
            return c
        if self.loop is None:
            return None
        fut: asyncio.Future = self.loop.create_future()
        self._live_waiters.setdefault(sid, []).append(fut)
        self._reconcile_soon()
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            return None
        finally:
            ws = self._live_waiters.get(sid)
            if ws and fut in ws:
                ws.remove(fut)

    def _wake_waiters(self, sid: str, c: SessionContext | None) -> None:
        for f in self._live_waiters.pop(sid, []):
            if not f.done():
                f.set_result(c)

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        self.loop = loop = asyncio.get_running_loop()
        self.inbox = asyncio.Queue()
        self._reconcile_now = asyncio.Event()
        self._lock = asyncio.Lock()
        pb = self.pool_bus
        inbox = self.inbox
        if pb is not None:
            # 总线回调线程只入队（PY-CB-01）
            self._handles += [
                pb.subscribe(bus_keys.STATE_SANDBOX_POOL,
                             lambda _k, raw: loop.call_soon_threadsafe(inbox.put_nowait, ("summary", bytes(raw)))),
                pb.subscribe(bus_keys.EVT_SANDBOX_POOL,
                             lambda _k, raw: loop.call_soon_threadsafe(inbox.put_nowait, ("event", bytes(raw)))),
            ]
        self._tasks += [asyncio.ensure_future(self._driver()), asyncio.ensure_future(self._lag_sampler()),
                        asyncio.ensure_future(self._housekeeping()), asyncio.ensure_future(self._consume())]
        await self.reconcile()

    async def stop(self) -> None:
        self.stopping = True
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks.clear()
        for h in self._handles:
            with contextlib.suppress(Exception):
                h.close()
        for sid in list(self.contexts):
            c = self.contexts.pop(sid)
            await c.close(1001, reason="shutdown")

    async def attach(self, view: SessionView) -> SessionContext | None:
        """会话 READY（或对账发现）且有绑定：建上下文（BINDING → LIVE）。失败记错误，下一次对账重试。"""
        if view.binding is None:
            return None
        c = SessionContext(self, view, view.binding)
        self.contexts[view.sid] = c
        try:
            await c.open()
        except Exception:
            self.metrics["attach_fail"] += 1
            log.exception("session context attach failed", extra={"kv": {"sid": view.sid}})
            self.contexts.pop(view.sid, None)
            with contextlib.suppress(Exception):
                await c.close(1011, reason="attach_failed")
            return None
        log.info("session context live", extra={"kv": {"sid": view.sid, "world": view.world_id}})
        self._wake_waiters(view.sid, c)
        return c

    async def detach(self, sid: str, *, code: int = CLOSE_SANDBOX_GONE, reason: str = "ended",
                     err: int | None = 506, detail: dict | None = None) -> None:
        c = self.contexts.pop(sid, None)
        if c is not None:
            await c.close(code, reason=reason, err=err, detail=detail)
            self._schedule_trim()
        self._wake_waiters(sid, None)

    def _schedule_trim(self, delay_s: float = 2.0) -> None:
        """上下文销毁（结束、驻留）后归还已释放的堆页（gc + malloc_trim，去抖；D2-FX-R2，D2-AC-06/45 的"沙盒组内存回落到
        创建前 + 10 MB"：D2-ACC-1 中 sandbox-api 驻留后比基线高约 5 MB）。"""
        if self.loop is None or self._trim_handle is not None:
            return

        def run() -> None:
            self._trim_handle = None
            from awr.runtime.procutil import trim_heap

            with contextlib.suppress(Exception):
                trim_heap()

        self._trim_handle = self.loop.call_later(delay_s, run)

    async def rebind(self, sid: str, view: SessionView) -> None:
        """底图切换（同一 sid、新世界或新绑定）：1012 `world_switch` 关闭连接，按新绑定重建（SWITCHING → LIVE）。"""
        c = self.contexts.pop(sid, None)
        if c is not None:
            c.state = "SWITCHING"
            await c.close(CLOSE_WORLD_SWITCH, reason="world_switch")
        if view.state in LIVE_STATES and view.binding is not None:
            await self.attach(view)

    def _end(self, sid: str, view: SessionView | None, reason: str | None) -> None:
        r = reason or (view.end_reason if view is not None else None) or "ended"
        snap = bool(view.raw.get("snapshot_available", True)) if view is not None else True
        self.ended[sid] = (time.monotonic(), r, snap)
        self.views.pop(sid, None)
        self.summaries.pop(sid, None)
        self.api_calls.pop(sid, None)
        self.last_api_ns.pop(sid, None)
        self.last_ext_ns.pop(sid, None)

    async def apply(self, views: list[SessionView], *, full: bool) -> None:
        """镜像更新与上下文生命周期（FR-205）。full = True 时未列出的会话视为结束。"""
        assert self._lock is not None
        async with self._lock:
            seen = set()
            for v in views:
                seen.add(v.sid)
                old = self.views.get(v.sid)
                if v.state in END_STATES:
                    self._end(v.sid, v, v.end_reason)
                    if v.sid in self.contexts:
                        await self.detach(v.sid, err=510 if v.end_reason == "preempted" else 506, reason=v.state.lower())
                    else:
                        self._wake_waiters(v.sid, None)
                    continue
                self.views[v.sid] = v
                self.ended.pop(v.sid, None)
                c = self.contexts.get(v.sid)
                if c is not None and v.state in PARK_STATES:
                    # D2.1 驻留（M11-FR-222；d2-WP-04b-to-M11 第 1 条）：残余连接 error{518, reason: parked} + 4430，销毁上下文，
                    # 不记入 ended；恢复时 SPAWNING（新绑定）→ READY 按 attach 重建
                    await self.detach(v.sid, code=CLOSE_IDLE_RECLAIMED, reason="parked", err=518,
                                      detail={"reason": "parked", "resume_url": f"/api/sandbox/v1/sessions/{v.sid}/resume"})
                    continue
                if c is not None:
                    switched = v.binding is not None and (v.binding.namespace != c.binding.namespace or
                                                          v.binding.ring_path != c.binding.ring_path)
                    if switched or (v.state == "SPAWNING" and old is not None and old.world_id != v.world_id):
                        await self.rebind(v.sid, v)
                    else:
                        c.view = v
                elif v.state in LIVE_STATES and v.binding is not None:
                    await self.attach(v)
            if full:
                for sid in [s for s in list(self.views) if s not in seen]:
                    v = self.views.get(sid)
                    self._end(sid, v, "gone")
                    if sid in self.contexts:
                        await self.detach(sid, err=506, reason="gone")
                for sid in [s for s in list(self.contexts) if s not in seen]:
                    self._end(sid, None, "gone")
                    await self.detach(sid, err=506, reason="gone")
            now = time.monotonic()
            for sid in [s for s, e in self.ended.items() if now - e[0] > ENDED_KEEP_S]:
                del self.ended[sid]

    async def reconcile(self) -> bool:
        """`sys/session/list` 对账；池不可达时保持现状（FR-215）。返回是否成功。"""
        if self.pool is None:
            return False
        try:
            rep = await self.pool.call("list", {}, timeout=1.0, retries=0)
        except PoolUnavailable:
            self.metrics["reconcile_fail"] += 1
            self.pool_up = False
            return False
        self.pool_up = True
        res = rep.get("result") if isinstance(rep.get("result"), dict) else rep
        items = res.get("sessions") if isinstance(res.get("sessions"), list) else res.get("items")
        if not isinstance(items, list):
            return False
        nm = res.get("n_max")
        if isinstance(nm, int) and not isinstance(nm, bool) and nm >= 0:
            self.n_max = nm
        views = [v for v in (SessionView.from_msg(d) for d in items) if v is not None]
        self.metrics["reconciles"] += 1
        await self.apply(views, full=True)
        return True

    async def refresh(self, sid: str) -> SessionView | None:
        """单个会话的即时对账（`sys/session/get{sid}`）：镜像中还没有该会话时（刚创建、池事件尚未到达）由 WS 接入与会话
        路由调用，M17 的创建路由也可在回复 201 之前调用。池不可达或池不认识该 sid 时返回 None（不改变现状）。"""
        if self.pool is None or not is_sid(sid):
            return self.views.get(sid)
        try:
            rep = await self.pool.call("get", {"sid": sid}, timeout=1.0, retries=0)
        except PoolUnavailable:
            return self.views.get(sid)
        res = rep.get("result") if isinstance(rep.get("result"), dict) else None
        v = SessionView.from_msg(res) if res is not None and rep.get("status") != "rejected" else None
        if v is not None:
            await self.apply([v], full=False)
        return self.views.get(sid)

    def _reconcile_soon(self) -> None:
        if self._reconcile_now is not None:
            self._reconcile_now.set()

    # ------------------------------------------------------------ 池消息
    async def _consume(self) -> None:
        assert self.inbox is not None
        while True:
            kind, raw = await self.inbox.get()
            try:
                if kind == "summary":
                    self._on_summary(raw)
                elif kind == "event":
                    self._on_pool_events(raw)
            except Exception:
                log.exception("pool message failed", extra={"kv": {"kind": kind}})

    def _on_summary(self, raw: bytes) -> None:
        m = unpack(raw)
        items = m.get("sessions") if isinstance(m, dict) else m
        if isinstance(m, dict) and isinstance(m.get("n_max"), int):
            self.n_max = int(m["n_max"])
        changed = False
        for d in items if isinstance(items, list) else []:
            if not isinstance(d, dict) or not is_sid(d.get("sid")):
                continue
            sid = str(d["sid"])
            self.summaries[sid] = d
            v = self.views.get(sid)
            st = d.get("state")
            if v is None or (isinstance(st, str) and st != v.state):
                changed = True
        if changed:
            self._reconcile_soon()

    def _on_pool_events(self, raw: bytes) -> None:
        m = unpack(raw)
        evs = m if isinstance(m, list) else [m]
        for ev in evs:
            if not isinstance(ev, dict):
                continue
            data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
            sid = data.get("sid") or ev.get("sid")
            kind = str(ev.get("kind") or "")
            if kind == "sandbox.state" or kind.startswith("session"):
                self._reconcile_soon()
            c = self.contexts.get(sid) if isinstance(sid, str) else None
            # D2.1：带 data.sid 的 session.switched（resume_snapshot 等）同样转发（d2-WP-04b-to-M11 第 5 条）
            if c is None or c.gateway is None or not (kind.startswith("sandbox.") or kind == "session.switched"):
                continue
            pol = c.gateway.policy
            if pol is not None and kind in pol.direct_kinds:
                continue
            gw = c.gateway
            gw.events.append(kind, int(ev.get("t_sim_ns") or gw.clock.t_sim_ns), int(ev.get("t_wall_ns") or time.time_ns()),
                             int(ev.get("severity") or 1), "sandbox-pool", None, None, data)
            self.metrics["events_forwarded"] += 1

    # ------------------------------------------------------------ 驱动
    async def _driver(self) -> None:
        t0 = time.monotonic_ns()
        k = 0
        while not self.stopping:
            k += 1
            deadline = t0 + k * PERIOD_NS
            lag = time.monotonic_ns() - deadline
            if lag > PERIOD_NS:
                skip = lag // PERIOD_NS
                k += int(skip)
                self.metrics["overruns"] += int(skip)
                continue
            if lag < 0:
                await asyncio.sleep(-lag / 1e9)
            else:
                await asyncio.sleep(0)
            self.k = k
            t = time.monotonic_ns()
            first = True
            for sid in sorted(self.contexts):
                c = self.contexts.get(sid)
                if c is None or not c.live or c.gateway is None:
                    continue
                gw = c.gateway
                busy = gw.has_subscribers()
                if not busy and (k + c.phase) % LIGHT_EVERY != 0:
                    continue
                if not first:
                    # 上下文之间让出事件循环（D2-FX-R2，D2-AC-04/42：N 个上下文的装帧不再连成一段阻塞，REST、容量与 WS 发送
                    # 可在其间执行；D2-ACC-1 中 sandbox-api 事件循环 p99 76–110 ms）
                    await asyncio.sleep(0)
                    c = self.contexts.get(sid)
                    if c is None or not c.live or c.gateway is None:
                        continue
                    gw = c.gateway
                first = False
                try:
                    if busy:
                        gw.tick_once(k)
                    else:
                        gw.tick_once(k, light=True)
                        self.metrics["light_ticks"] += 1
                except Exception:
                    log.exception("context tick failed", extra={"kv": {"sid": sid}})
            self.metrics["ticks"] += 1
            if (time.monotonic_ns() - t) / 1e6 > OVERRUN_MS:
                self.metrics["hub_overrun"] = self.metrics.get("hub_overrun", 0) + 1

    async def _lag_sampler(self) -> None:
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.1)
            lag = (time.monotonic() - t - 0.1) * 1e3
            self._last_lag_ms = lag
            for c in list(self.contexts.values()):
                if c.gateway is not None:
                    c.gateway.metrics.lag.add(lag)

    async def _housekeeping(self) -> None:
        """1 Hz：`sandbox/session` 发布与活动上报；每 2 s（或被事件提前）对账。"""
        assert self._reconcile_now is not None
        last_rec = time.monotonic()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._reconcile_now.wait(), ACTIVITY_S)
            now = time.monotonic()
            if self._reconcile_now.is_set() or now - last_rec >= RECONCILE_S:
                self._reconcile_now.clear()
                last_rec = now
                with contextlib.suppress(Exception):
                    await self.reconcile()
            self.publish_session_states()
            self.report_activity()

    def publish_session_states(self) -> None:
        now = time.time_ns()
        for sid, c in list(self.contexts.items()):
            gw = c.gateway
            v = self.views.get(sid)
            if gw is None or v is None:
                continue
            ch = gw.registry.by_topic.get("sandbox/session")
            if ch is None:
                continue
            st = build_session_state(v, self.summaries.get(sid), gw, now_ns=now,
                                     api_activity_ns=self.external_activity_ns(sid, gw))
            ch.publish(msgpack.packb(st, use_bin_type=True), gw.frame_t_sim_ns)

    def activity_items(self) -> dict[str, dict]:
        """每会话 `{ws_n, api_calls, last_api_unix_ns, last_input_unix_ns}`；D2.1 的 `last_input_unix_ns` 为全部连接最近交互
        （presence、call，握手时为当前）与会话内 REST 时刻的最大值（M11-FR-222；17 §17.20.4），池据此判定兜底驻留。"""
        out: dict[str, dict] = {}
        for sid in set(self.views) | set(self.contexts):
            c = self.contexts.get(sid)
            sess = list(c.gateway.sessions) if c is not None and c.gateway is not None else []
            item: dict[str, Any] = {"ws_n": len(sess), "api_calls": self.api_calls.get(sid, 0)}
            if sid in self.last_api_ns:
                item["last_api_unix_ns"] = str(self.last_api_ns[sid])
            inputs = [int(getattr(s, "last_input_ns", 0) or 0) for s in sess if not s.closing]
            if sid in self.last_api_ns:
                inputs.append(int(self.last_api_ns[sid]))
            if inputs and max(inputs) > 0:
                item["last_input_unix_ns"] = str(max(inputs))
            out[sid] = item
        return out

    def report_activity(self) -> None:
        """`sys/session/activity`（1 s 一批，M17-FR-042）；无会话时不发。调用计数发送后清零。"""
        if self.pool is None:
            return
        items = self.activity_items()
        if not items:
            return
        for sid in items:
            self.api_calls[sid] = 0
        with contextlib.suppress(Exception):
            self.pool.cast("activity", {"items": items}, lambda rep, err: None, timeout=1.0)

    def inspect(self) -> dict[str, Any]:
        return {"n_max": self.effective_n_max, "ws_max": self.ws_max, "pool_up": self.pool_up,
                "views": {sid: v.state for sid, v in sorted(self.views.items())},
                "contexts": {sid: c.state for sid, c in sorted(self.contexts.items())},
                "ended": {sid: e[1] for sid, e in sorted(self.ended.items())}, "metrics": dict(self.metrics),
                "lag_ms": round(self._last_lag_ms, 3)}

