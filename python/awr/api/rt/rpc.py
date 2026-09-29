"""RpcRouter：可信入口 ①–③、roster 路由、总线分发、在途表、批量聚合、CLIENT_DATA 与确认令牌（M11-FR-055 至 FR-064；
M11 §6.4.10、§6.4.11；AWR-17 §3.2、§3.5、§6.4 CLIENT_DATA、§7.1、§7.2、§7.5、§9.4）。

入口顺序（原因码优先级 = 步骤顺序，入口耗时 ≤ 1 ms）：call id 与服务名（300）→ ① 角色（viewer 写操作 115；admin 专属
服务 115）、会话模式（回放或切换中 118；api 停止中 213）、席位（116）→ ② 限流（`call` 50/s 突发 100，`env/*` 2/s，`sim/*`
5/s；111 + `retry_after_ms`）→ ③ 确认令牌（kill、escalate、`seat/takeover`、override 租约；112）→ 参数结构校验（`commands.json`
的 JSON Schema：类型与必填 300，schema 声明的边界 110；`follow_path.waypoints` 以 numpy 形状检查代替逐点校验）→ 路由：
`uav/{id}/cmd/*` 经 roster 查生产者 → `ctl/{producer}/cmd`（机体不在 roster 107；acquire、release → `ctl/sim-core/lease`）；
`fleet/cmd/*`、`mission/*`、`env/set`、`env/preset` → `ctl/sim-core/cmd`；`sim/*` → `ctl/sim-core/clock`；`seat/*` →
`ctl/sim-core/lease`；`env/query` → `ctl/sim-core/query`；`rec/*` → `ctl/recorder/*`（recorder 不可达 213）；`confirm/issue`
在 api 内处理。入口拒绝同时产生 api 事件 `cmd.rejected`。

总线调用 1 s 超时、间隔 0.3 s 同一 cid 重试 2 次，仍失败 `rejected 211`。Admission → `result`：accepted（effect UNVERIFIED、V1、
`native_ack`）、rejected（code、reason、message、remedy，effect UNAVAILABLE、V0，final）、duplicate（`duplicate: true` + 当前
状态）。此后 `evt/{producer}/cmd` 的 `cmd.*` 由 EventIngest 调用 `on_cmd_event` 按 cid 转成 `result`/`progress`（≤ 2 Hz）
只发给发起连接；Admission 回复之前到达的事件先缓存，accepted 发出后按序放行。在途表保存 cid → 最新 result，终态后保留 60 s：
同一 cid 重发时已终态的直接回放（`duplicate: true`），未终态或未知的转发给生产者（由其幂等表裁决），并把发起连接改绑到新连接。

批量 `fleet/cmd/{op}`（op ∈ rtl、land、hover、safety_stop、pause、resume、takeoff）：`batch_id` = 客户端 call id；按 roster
分组，每个生产者一条 Command（`cid = batch_id`、`uav` 为列表、`batch_id`），生产者逐机准入并在 `per_uav` 中回复；只回一条汇总
result `data{accepted_n, rejected_n, rejected_by_code, accepted, rejected}`，此后 `progress{data: {phase, counts}}` 与
`fleet.batch.progress` 事件（均 ≤ 2 Hz）汇总逐机进度，全部子调用终态后发 final。生产者回复 109（不支持批量）时，入口按
`<batch_id>:<vehicle_id>` 展开为逐机 Command（均带 `batch_id`）再汇总（兼容尚未实现批量准入的生产者，见实现报告）。

CLIENT_DATA（遥操作，FR-062）：客户端 advertise 的 `uav/{id}/setpoint` channel；operator 角色、本连接对该机存在已准入的活动
velocity 调用；`seq` 不递增或 `t_client_sim_ns < simNow − 200 ms` 的包丢弃计数；每 channel > 60 Hz 的部分丢弃计数；有效包
立即（不等 tick）转为 32 B raw 发布到 `ctl/sim-core/setpoint`；不满足条件时每个 channel 发一次 `error 322`。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, Any

import numpy as np
from jsonschema import Draft202012Validator

from awr.contracts import bus_keys
from awr.contracts import frame as F
from awr.contracts.commands import match_service
from awr.contracts.reasons import Reason
from awr.runtime.bus import BusError, BusTimeout

from ..ratelimit import _Bucket, category_for_service
from .protocol import CALL_ID_RE, result_msg

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["BATCH_OPS", "BatchAgg", "ClientPublish", "ConfirmTokens", "InFlight", "RpcRouter"]

INFLIGHT_KEEP_NS = 60_000_000_000
PROGRESS_MIN_NS = 500_000_000
BATCH_OPS = ("rtl", "land", "hover", "safety_stop", "pause", "resume", "takeoff")
RANGE_KEYWORDS = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "enum", "const",
                  "minLength", "maxLength", "oneOf"}
ADMIN_ONLY = {"seat/takeover"}
CONFIRM_ACTIONS = {"kill": "kill", "escalate": "escalate"}
TERMINAL = {"succeeded", "failed", "canceled", "timeout", "rejected"}
EFFECT_UNAVAILABLE = {"status": "UNAVAILABLE", "verify_trust": 0}
SIM_LAG_NS = 200_000_000
CD_RATE_HZ = 60.0
CONFIRM_TTL_S = 10.0


@cache
def _validator(service_name: str) -> Draft202012Validator | None:
    m = match_service(service_name)
    if m is None:
        return None
    return Draft202012Validator(m[0].args_schema)


def _check_schema(service_name: str, args: dict) -> tuple[int, dict] | None:
    """结构校验：返回 (code, detail) 或 None。follow_path 的 waypoints 用 numpy 形状检查（1000 点约 50 µs）。"""
    v = _validator(service_name)
    if v is None:
        return None
    check = args
    if "waypoints" in args and service_name.endswith("/cmd/follow_path"):
        wps = args["waypoints"]
        try:
            a = np.asarray(wps, np.float64)
        except (TypeError, ValueError):
            return int(Reason.BAD_REQUEST), {"field": "args.waypoints", "rule": "type"}
        if a.ndim != 2 or a.shape[1] != 3 or not np.isfinite(a).all():
            return int(Reason.BAD_REQUEST), {"field": "args.waypoints", "rule": "shape"}
        if not 2 <= a.shape[0] <= 1000:
            return int(Reason.PARAM_OUT_OF_RANGE), {"field": "args.waypoints", "rule": "maxItems" if a.shape[0] > 1000
                                                     else "minItems", "value": int(a.shape[0]), "range": [2, 1000]}
        check = {k: v for k, v in args.items() if k != "waypoints"} | {"waypoints": [[0.0, 0.0, 0.0]] * 2}
    err = next(iter(sorted(v.iter_errors(check), key=lambda e: list(e.path))), None)
    if err is None:
        return None
    code = int(Reason.PARAM_OUT_OF_RANGE) if err.validator in RANGE_KEYWORDS else int(Reason.BAD_REQUEST)
    return code, {"field": "args" + "".join(f".{p}" for p in err.path), "rule": str(err.validator)}


@dataclass
class InFlight:
    cid: str
    session: Any  # ClientSession 或 REST 调用方（commands.RestCaller）；断开时为 None
    principal_id: str
    service: str
    op: str
    uav: str | None
    kind: str  # cmd、clock、lease、query、batch、rec、api
    t_start_ns: int
    args: dict = field(default_factory=dict)
    silent: bool = False
    accepted_sent: bool = False
    final: bool = False
    t_final_ns: int = 0
    last_result: dict | None = None
    pending: list[dict] = field(default_factory=list)
    last_progress_ns: int = 0
    history: list[dict] = field(default_factory=list)
    final_ev: asyncio.Event | None = None


@dataclass
class BatchAgg:
    batch_id: str
    n: int
    op: str
    accepted: list[str] = field(default_factory=list)
    rejected: list[list] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)  # vehicle id -> 子调用状态
    codes: dict[str, int] = field(default_factory=dict)
    admitted: bool = False
    dirty: bool = False
    t_last_progress: int = 0
    final: bool = False

    def counts(self) -> dict[str, int]:
        c = {"accepted": 0, "running": 0, "succeeded": 0, "failed": 0, "canceled": 0, "rejected": len(self.rejected)}
        for st in self.states.values():
            key = "failed" if st == "timeout" else st
            if key in c:
                c[key] += 1
        return c

    def done(self) -> bool:
        return self.admitted and all(st in TERMINAL for st in self.states.values())

    def summary(self) -> dict[str, Any]:
        by_code = Counter(str(c) for _, c in self.rejected)
        return {"batch_id": self.batch_id, "op": self.op, "n": self.n, "accepted_n": len(self.accepted),
                "rejected_n": len(self.rejected), "rejected_by_code": dict(by_code), "accepted": list(self.accepted),
                "rejected": [list(x) for x in self.rejected]}


class ConfirmTokens:
    """确认令牌（ext，17 §3.2）：`c1.<b64url(payload)>.<b64url(sig)>`，绑定 principal、action、target，10 s（墙钟），单次使用。"""

    def __init__(self, key: bytes) -> None:
        self.key = key
        self.used: dict[str, float] = {}

    @staticmethod
    def _b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")

    def issue(self, principal_id: str, action: str, target: str) -> tuple[str, int]:
        exp = time.time() + CONFIRM_TTL_S
        body = json.dumps({"p": principal_id, "a": action, "t": target, "exp": round(exp, 3),
                           "n": os.urandom(8).hex()}, separators=(",", ":")).encode()
        mac = hmac.new(self.key, b"c1." + body, hashlib.sha256).digest()
        return f"c1.{self._b64(body)}.{self._b64(mac)}", int(exp * 1e9)

    def verify(self, token: Any, principal_id: str, action: str, target: str) -> bool:
        if not isinstance(token, str) or not token.startswith("c1.") or token.count(".") != 2:
            return False
        try:
            _, b, s = token.split(".")
            body = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
            sig = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            d = json.loads(body)
        except (ValueError, TypeError):
            return False
        if not hmac.compare_digest(hmac.new(self.key, b"c1." + body, hashlib.sha256).digest(), sig):
            return False
        now = time.time()
        self.used = {n: t for n, t in self.used.items() if t > now}
        if d.get("p") != principal_id or d.get("a") != action or d.get("t") != target or float(d.get("exp", 0)) < now:
            return False
        nonce = str(d.get("n"))
        if nonce in self.used:
            return False
        self.used[nonce] = float(d["exp"])
        return True


class RpcRouter:
    def __init__(self, gw: Gateway, confirm_key: bytes | None = None) -> None:
        self.gw = gw
        self.inflight: dict[str, InFlight] = {}
        self.batches: dict[str, BatchAgg] = {}
        self.tasks: set[asyncio.Task] = set()
        self.confirm = ConfirmTokens(confirm_key or os.urandom(32))
        self.stats = {"calls": 0, "rejected_entry": 0, "bus_timeouts": 0, "duplicates": 0, "batches": 0,
                      "batch_fallbacks": 0}

    # ------------------------------------------------------------ 入口
    def handle_call(self, s: Any, m: dict) -> None:
        """WS `call` 与 REST 命令镜像共用（s 为 ClientSession 或 RestCaller）。"""
        self.stats["calls"] += 1
        cid = m.get("id")
        service = m.get("service")
        args = m.get("args") if m.get("args") is not None else {}
        if not isinstance(cid, str) or not CALL_ID_RE.match(cid):
            s.send_ctrl(result_msg(str(cid or "")[:96] or "invalid", "rejected", int(Reason.BAD_REQUEST), final=True,
                                   detail={"field": "id"}, effect=EFFECT_UNAVAILABLE))
            return
        prev = self.inflight.get(cid)
        if prev is not None and prev.final and prev.last_result is not None:
            self.stats["duplicates"] += 1
            prev.session = s
            s.send_ctrl(prev.last_result | {"duplicate": True})
            return
        if not isinstance(service, str) or not isinstance(args, dict):
            self._reject(s, cid, str(service), int(Reason.BAD_REQUEST),
                         {"field": "service" if not isinstance(service, str) else "args"})
            return
        mm = match_service(service)
        if mm is None:
            self._reject(s, cid, service, int(Reason.BAD_REQUEST), {"field": "service"})
            return
        spec, params = mm
        write = spec.category != "query"
        gw = self.gw
        pid = s.principal.id
        role = s.role
        # ① 身份与角色、会话模式、席位
        if write and role == "viewer":
            self._reject(s, cid, service, int(Reason.ROLE_FORBIDDEN), {"role": role})
            return
        if service in ADMIN_ONLY and role != "admin":
            self._reject(s, cid, service, int(Reason.ROLE_FORBIDDEN), {"role": role, "need": "admin"})
            return
        if write and gw.stopping:
            self._reject(s, cid, service, int(Reason.SERVICE_UNAVAILABLE), {"why": "API_STOPPING"})
            return
        if write and gw.read_only_reason() is not None:
            self._reject(s, cid, service, int(Reason.READ_ONLY_MODE), {"mode": gw.read_only_reason()})
            return
        if write and service not in ADMIN_ONLY and not gw.is_seat_holder(pid):
            self._reject(s, cid, service, int(Reason.SEAT_TAKEN), {"seat": gw.seat_json()})
            return
        # ② 限流（call 桶 + 类别桶）
        extra = category_for_service(spec.op)
        for cat in ([extra] if extra else []) + ["call"]:
            retry = gw.limiter.check(pid, cat)
            if retry:
                self._reject(s, cid, service, int(Reason.RATE_LIMITED), {"category": cat}, retry_after_ms=retry)
                return
        # ③ 确认令牌
        act = self._confirm_action(spec.op, params, args)
        if act is not None and not self.confirm.verify(args.get("confirm_token"), pid, act[0], act[1]):
            self._reject(s, cid, service, int(Reason.CONFIRM_REQUIRED), {"action": act[0], "target": act[1]})
            return
        # 参数结构校验
        bad = _check_schema(service, args)
        if bad is not None:
            self._reject(s, cid, service, bad[0], bad[1])
            return
        if spec.op == "fleet/cmd":
            self._handle_batch(s, cid, service, params.get("op", ""), args)
            return
        if spec.op == "confirm/issue":
            self._confirm_issue(s, cid, service, args)
            return
        route = self._route(spec.op, spec.name, params, args)
        if isinstance(route, int):
            self._reject(s, cid, service, route, {"uav": params.get("id")})
            return
        key, kind, op, uav = route
        principal = gw.tokens.sign_principal(pid, role, cid, conn_id=getattr(s, "conn_id", None),
                                             seat=gw.is_seat_holder(pid))
        msg = self._build(kind, cid, op, uav, args, principal, params)
        self._open(cid, s, pid, service, op, uav, kind, args=args)
        self._spawn(self._dispatch(cid, key, kind, msg))

    def _confirm_action(self, op: str, params: dict, args: dict) -> tuple[str, str] | None:
        if op in CONFIRM_ACTIONS:
            return CONFIRM_ACTIONS[op], str(params.get("id"))
        if op == "seat/takeover":
            return "seat_takeover", "seat"
        if op == "acquire" and args.get("priority") == "override":
            return "lease_override", str(params.get("id"))
        return None

    def _route(self, op: str, name: str, params: dict, args: dict) -> tuple[str, str, str, str | None] | int:
        gw = self.gw
        prod = gw.settings.producer
        if name.startswith("uav/"):
            uav = params.get("id")
            e = gw.roster_entry(uav)
            if e is None and gw.roster_loaded:
                return int(Reason.NO_VEHICLE)
            # roster 尚未取回（api 刚重启，D1-AC-11a 的同 cid 重发）：交主生产者裁决（未知机体由其返回 107）
            producer = (e or {}).get("producer") or prod
            if op in ("acquire", "release"):
                return bus_keys.CTL_LEASE, "lease", op, uav
            return bus_keys.ctl_cmd(producer), "cmd", op, uav
        if op.startswith("sim/"):
            return bus_keys.CTL_CLOCK, "clock", op, None
        if op in ("seat/release", "seat/takeover"):
            return bus_keys.CTL_LEASE, "lease", op, None
        if op == "env/query":
            return bus_keys.CTL_QUERY, "query", op, None
        if op.startswith("rec/"):
            return bus_keys.ctl_recorder(op.split("/", 1)[1]), "rec", op, None
        return bus_keys.ctl_cmd(prod), "cmd", op, None

    def _build(self, kind: str, cid: str, op: str, uav: str | None, args: dict, principal: dict,
               params: dict | None = None, batch_id: str | None = None) -> dict:
        gw = self.gw
        if kind == "cmd":
            a = dict(args)
            if op.startswith("mission/") and params and params.get("mid"):
                a["mid"] = params["mid"]
            return {"v": 1, "cid": cid, "op": op, "uav": uav, "args": a, "principal": principal, "lease": None,
                    "t_wall_ns": time.time_ns(), "epoch_seen": gw.clock.global_epoch, "batch_id": batch_id}
        if kind == "clock":
            a = {k: args[k] for k in ("ticks", "rate", "scenario_id", "profile") if k in args}
            return {"v": 1, "cid": cid, "op": op.split("/", 1)[1], "args": a, "principal": principal}
        if kind == "lease":
            if op == "seat/release":
                return {"v": 1, "cid": cid, "op": "seat_release", "principal": principal}
            if op == "seat/takeover":
                return {"v": 1, "cid": cid, "op": "seat_takeover", "principal": principal,
                        "confirm_token": args.get("confirm_token")}
            m = {"v": 1, "cid": cid, "op": op, "uav": uav, "owner": "OPERATOR", "principal": principal}
            if op == "release":
                m["return_to"] = args.get("return_to", "previous")
            else:
                m["priority"] = args.get("priority", "normal")
            return m
        if kind == "rec":
            return {"v": 1, "cid": cid, "principal": principal}
        return {"v": 1, "op": op, "args": args, "principal": principal}

    def _spawn(self, coro: Any) -> None:
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def _open(self, cid: str, s: Any, pid: str, service: str, op: str, uav: str | None, kind: str, *,
              silent: bool = False, args: dict | None = None) -> InFlight:
        f = self.inflight.get(cid)
        if f is not None and not f.final:
            f.session = s  # 同一 cid 由新连接重发：改绑（FR-064）
            return f
        f = InFlight(cid, s, pid, service, op, uav, kind, time.monotonic_ns(), args=dict(args or {}), silent=silent)
        self.inflight[cid] = f
        return f

    def _reject(self, s: Any, cid: str, service: str, code: int, detail: Any = None, *,
                retry_after_ms: int | None = None) -> None:
        """入口拒绝：rejected + final，effect UNAVAILABLE、V0；同时产生 api 事件 `cmd.rejected`（FR-070）。"""
        self.stats["rejected_entry"] += 1
        r = result_msg(cid, "rejected", code, final=True, detail=detail, retry_after_ms=retry_after_ms,
                       effect=EFFECT_UNAVAILABLE)
        s.send_ctrl(r)
        uav = service.split("/")[1] if service.startswith("uav/") and service.count("/") >= 2 else None
        self.gw.emit_api_event("cmd.rejected", 1, {"op": service, "code": int(code), "entry": True},
                               uav=uav, cid=cid)
        if code in (int(Reason.ROLE_FORBIDDEN), int(Reason.SEAT_TAKEN), int(Reason.CONFIRM_REQUIRED)):
            self.gw.audit.write("cmd.rejected", principal_id=s.principal.id, role=s.role, cid=cid, uav=uav, code=code,
                                detail={"service": service})

    async def _dispatch(self, cid: str, key: str, kind: str, msg: dict) -> None:
        try:
            rep = await self.gw.bus.call(key, msg, timeout=1.0, retries=2, retry_gap=0.3)
        except BusTimeout:
            self.stats["bus_timeouts"] += 1
            code = int(Reason.SERVICE_UNAVAILABLE) if kind == "rec" else int(Reason.SIM_UNAVAILABLE)
            self._final(cid, result_msg(cid, "rejected", code, final=True, effect=EFFECT_UNAVAILABLE))
            return
        except BusError:
            self._final(cid, result_msg(cid, "rejected", int(Reason.SERVICE_UNAVAILABLE), final=True,
                                        effect=EFFECT_UNAVAILABLE))
            return
        f = self.inflight.get(cid)
        if f is None or f.final:
            return
        if not isinstance(rep, dict):
            self._final(cid, result_msg(cid, "rejected", int(Reason.INTERNAL_ERROR), final=True, effect=EFFECT_UNAVAILABLE))
            return
        if kind == "cmd":
            self._on_admission(f, rep)
        elif kind == "query":
            code = int(rep.get("code", 0) or 0)
            if code:
                self._final(cid, result_msg(cid, "rejected", code, final=True, detail=rep.get("detail"),
                                            effect=EFFECT_UNAVAILABLE))
            else:
                self._final(cid, result_msg(cid, "succeeded", 0, final=True, data=rep,
                                            effect={"status": "OK", "verify_trust": 4, "simulated": True}))
        else:
            if kind == "lease" and isinstance(rep.get("seat"), dict):
                self.gw.update_seat(rep["seat"])
            code = int(rep.get("code", 0) or 0)
            data = {k: rep[k] for k in ("clock", "lease", "seat", "segment") if rep.get(k) is not None}
            if rep.get("status") == "rejected" or code:
                self._final(cid, result_msg(cid, "rejected", code or int(Reason.STATE), final=True, data=data or None,
                                            effect=EFFECT_UNAVAILABLE))
                return
            self._send(f, result_msg(cid, "accepted", 0, effect={"status": "UNVERIFIED", "verify_trust": 1,
                                                                  "native_ack": True, "protocol": "inproc",
                                                                  "requested": f.service}))
            f.accepted_sent = True
            self._final(cid, result_msg(cid, "succeeded", 0, final=True, data=data or None,
                                        effect={"status": "OK", "verify_trust": 4, "simulated": True,
                                                "latency_ms": self._latency_ms(f)}))
            if f.op == "seat/takeover":
                self.gw.on_seat_takeover(f.principal_id)
            if f.op in ("seat/release",):
                self.gw.audit.write("seat.release", principal_id=f.principal_id, cid=cid)

    def _on_admission(self, f: InFlight, adm: dict) -> None:
        st = adm.get("status")
        cid = f.cid
        if st == "accepted":
            self._send(f, result_msg(cid, "accepted", 0, effect={"status": "UNVERIFIED", "verify_trust": 1,
                                                                  "native_ack": True, "protocol": "inproc",
                                                                  "requested": f.service},
                                     warnings=adm.get("warnings")))
            f.accepted_sent = True
            pend, f.pending = f.pending, []
            for m in pend:
                self._deliver(f, m)
            return
        if st == "duplicate":
            cs = adm.get("call_state") or {}
            status = cs.get("status", "accepted")
            if status not in ("accepted", "rejected", "running", "succeeded", "failed", "canceled", "timeout"):
                status = "accepted"
            final = bool(cs.get("final"))
            eff = cs.get("effect") if isinstance(cs.get("effect"), dict) and cs.get("effect") else \
                {"status": "UNVERIFIED", "verify_trust": 1}
            m = result_msg(cid, status, int(cs.get("code", 0) or 0), final=True if final else None, duplicate=True,
                           effect=eff)
            self.stats["duplicates"] += 1
            f.accepted_sent = True
            if final:
                self._final(cid, m)
            else:
                self._send(f, m)
                pend, f.pending = f.pending, []
                for mm in pend:
                    self._deliver(f, mm)
            return
        code = int(adm.get("code") or int(Reason.STATE))
        det = adm.get("detail")
        self._final(cid, result_msg(cid, "rejected", code, final=True, detail=det if isinstance(det, (dict, str)) else None,
                                    dup_of=adm.get("dup_of"), effect=EFFECT_UNAVAILABLE))

    def _latency_ms(self, f: InFlight) -> int:
        return max(0, (time.monotonic_ns() - f.t_start_ns) // 1_000_000)

    # ------------------------------------------------------------ 批量（FR-061）
    def _handle_batch(self, s: Any, cid: str, service: str, bop: str, args: dict) -> None:
        gw = self.gw
        if bop not in BATCH_OPS:
            self._reject(s, cid, service, int(Reason.BAD_REQUEST), {"field": "service", "allowed": list(BATCH_OPS)})
            return
        inner = args.get("args") if isinstance(args.get("args"), dict) else {}
        bad = _check_schema(f"uav/x/cmd/{bop}", inner)
        if bad is not None:
            det = dict(bad[1])
            det["field"] = det.get("field", "args").replace("args", "args.args", 1)
            self._reject(s, cid, service, bad[0], det)
            return
        vehicles = args.get("vehicles")
        ids = gw.roster_ids() if vehicles == "*" else list(dict.fromkeys(v for v in vehicles if isinstance(v, str)))
        f = self._open(cid, s, s.principal.id, service, f"fleet/cmd/{bop}", None, "batch", args=args)
        if cid in self.batches and not self.batches[cid].final:
            agg = self.batches[cid]
            s.send_ctrl(result_msg(cid, "accepted", 0, duplicate=True, data=agg.summary() | {"counts": agg.counts()},
                                   effect={"status": "UNVERIFIED", "verify_trust": 1}))
            return
        agg = self.batches[cid] = BatchAgg(cid, len(ids), bop)
        self.stats["batches"] += 1
        groups: dict[str, list[str]] = {}
        for vid in ids:
            e = gw.roster_entry(vid)
            if e is None:
                agg.rejected.append([vid, int(Reason.NO_VEHICLE)])
            else:
                groups.setdefault(e.get("producer") or gw.settings.producer, []).append(vid)
        self._spawn(self._dispatch_batch(f, agg, groups, bop, inner, s.principal.id, s.role,
                                         getattr(s, "conn_id", None)))

    async def _dispatch_batch(self, f: InFlight, agg: BatchAgg, groups: dict[str, list[str]], bop: str, inner: dict,
                              pid: str, role: str, conn_id: str | None) -> None:
        gw = self.gw
        cid = agg.batch_id
        seat = gw.is_seat_holder(pid)
        for producer, vids in groups.items():
            principal = gw.tokens.sign_principal(pid, role, cid, conn_id=conn_id, seat=seat)
            msg = self._build("cmd", cid, f"fleet/cmd/{bop}", vids, inner, principal, batch_id=cid)  # type: ignore[arg-type]
            try:
                adm = await gw.bus.call(bus_keys.ctl_cmd(producer), msg, timeout=1.0, retries=2, retry_gap=0.3)
            except BusTimeout:
                agg.rejected += [[v, int(Reason.SIM_UNAVAILABLE)] for v in vids]
                continue
            except BusError:
                agg.rejected += [[v, int(Reason.SERVICE_UNAVAILABLE)] for v in vids]
                continue
            adm = adm if isinstance(adm, dict) else {}
            per = adm.get("per_uav") if isinstance(adm.get("per_uav"), dict) else None
            if adm.get("status") in ("accepted", "duplicate") and per is not None:
                agg.accepted += [str(v) for v in per.get("accepted") or []]
                agg.rejected += [[str(x[0]), int(x[1])] for x in per.get("rejected") or [] if len(x) == 2]
            elif adm.get("status") == "rejected" and int(adm.get("code") or 0) == int(Reason.BACKEND_UNSUPPORTED):
                self.stats["batch_fallbacks"] += 1
                await self._expand_batch(agg, producer, vids, bop, inner, pid, role, conn_id, seat)
            elif adm.get("status") in ("accepted", "duplicate"):
                agg.accepted += vids
            else:
                code = int(adm.get("code") or int(Reason.STATE))
                agg.rejected += [[v, code] for v in vids]
        agg.admitted = True
        for vid in agg.accepted:
            agg.states.setdefault(vid, "accepted")
        if f.final:
            return
        if agg.accepted:
            self._send(f, result_msg(cid, "accepted", 0, data=agg.summary(),
                                     effect={"status": "UNVERIFIED", "verify_trust": 1, "native_ack": True,
                                             "protocol": "inproc", "requested": f.service}))
            f.accepted_sent = True
            agg.dirty = True
        else:
            codes = Counter(c for _, c in agg.rejected)
            code = codes.most_common(1)[0][0] if codes else int(Reason.NO_VEHICLE)
            agg.final = True
            self._final(cid, result_msg(cid, "rejected", code, final=True, data=agg.summary(), effect=EFFECT_UNAVAILABLE))

    async def _expand_batch(self, agg: BatchAgg, producer: str, vids: list[str], bop: str, inner: dict, pid: str,
                            role: str, conn_id: str | None, seat: bool) -> None:
        """生产者不支持批量准入时的回退：逐机 Command（cid = `<batch_id>:<vehicle_id>`，带 batch_id）。"""
        gw = self.gw
        key = bus_keys.ctl_cmd(producer)

        async def one(vid: str) -> tuple[str, dict | None, int]:
            sub = f"{agg.batch_id}:{vid}"[:96]
            principal = gw.tokens.sign_principal(pid, role, sub, conn_id=conn_id, seat=seat)
            msg = self._build("cmd", sub, bop, vid, inner, principal, batch_id=agg.batch_id)
            try:
                return vid, await gw.bus.call(key, msg, timeout=1.0, retries=2, retry_gap=0.3), 0
            except BusTimeout:
                return vid, None, int(Reason.SIM_UNAVAILABLE)
            except BusError:
                return vid, None, int(Reason.SERVICE_UNAVAILABLE)

        for vid, rep, err in await asyncio.gather(*(one(v) for v in vids)):
            if err:
                agg.rejected.append([vid, err])
            elif isinstance(rep, dict) and rep.get("status") in ("accepted", "duplicate"):
                agg.accepted.append(vid)
            else:
                agg.rejected.append([vid, int((rep or {}).get("code") or int(Reason.STATE))])

    def _batch_event(self, ev: dict) -> None:
        agg = self.batches.get(str(ev.get("batch_id")))
        if agg is None or agg.final:
            return
        vid = ev.get("uav")
        sub = str(ev.get("kind", "")).split(".", 1)[-1]
        if not isinstance(vid, str) or sub not in ("accepted", "running", "succeeded", "failed", "canceled", "timeout",
                                                    "rejected"):
            return
        prev = agg.states.get(vid)
        if prev in TERMINAL:
            return
        if sub == "accepted" and prev is not None:
            return
        agg.states[vid] = sub
        code = (ev.get("data") or {}).get("code")
        if isinstance(code, int) and code:
            agg.codes[vid] = code
        agg.dirty = True

    def tick(self, t_mono: int) -> None:
        """每 tick：批量进度（≤ 2 Hz）与终态汇总。"""
        for bid, agg in list(self.batches.items()):
            if agg.final or not agg.admitted or not agg.dirty:
                continue
            done = agg.done()
            if not done and t_mono - agg.t_last_progress < PROGRESS_MIN_NS:
                continue
            agg.dirty = False
            agg.t_last_progress = t_mono
            counts = agg.counts()
            f = self.inflight.get(bid)
            if f is not None and not f.final:
                self._send(f, {"op": "progress", "id": bid, "data": {"phase": "executing", "counts": counts}})
            self.gw.emit_api_event("fleet.batch.progress", 0, {"batch_id": bid, "counts": counts})
            if done:
                self._finish_batch(agg, counts)

    def _finish_batch(self, agg: BatchAgg, counts: dict[str, int]) -> None:
        agg.final = True
        states = list(agg.states.values())
        if all(st == "succeeded" for st in states):
            status, code = "succeeded", 0
        elif any(st in ("failed", "timeout") for st in states):
            fails = Counter(agg.codes.get(v, 0) for v, st in agg.states.items() if st in ("failed", "timeout"))
            status, code = "failed", fails.most_common(1)[0][0] or int(Reason.STATE)
        else:
            cans = Counter(agg.codes.get(v, 0) for v, st in agg.states.items() if st == "canceled")
            status, code = "canceled", cans.most_common(1)[0][0] or int(Reason.CANCELLED)
        eff = {"status": "OK", "verify_trust": 4, "simulated": True} if status == "succeeded" else \
            {"status": "FAILED" if status == "failed" else "UNVERIFIED", "verify_trust": 2}
        self._final(agg.batch_id, result_msg(agg.batch_id, status, code, final=True,
                                             data=agg.summary() | {"counts": counts}, effect=eff))

    # ------------------------------------------------------------ 确认令牌（ext）
    def _confirm_issue(self, s: Any, cid: str, service: str, args: dict) -> None:
        action, target = str(args.get("action")), str(args.get("target"))
        if action not in ("kill", "escalate", "remove_force", "seat_takeover", "lease_override"):
            self._reject(s, cid, service, int(Reason.PARAM_OUT_OF_RANGE), {"field": "args.action"})
            return
        token, exp = self.confirm.issue(s.principal.id, action, target)
        f = self._open(cid, s, s.principal.id, service, "confirm/issue", None, "api", args=args)
        f.accepted_sent = True
        self._final(cid, result_msg(cid, "succeeded", 0, final=True,
                                    data={"confirm_token": token, "exp_unix_ns": str(exp)},
                                    effect={"status": "OK", "verify_trust": 4, "simulated": True}))

    # ------------------------------------------------------------ 生产者事件 -> result/progress
    def on_cmd_event(self, ev: dict) -> None:
        if ev.get("batch_id"):
            self._batch_event(ev)
            return
        kind = ev.get("kind", "")
        cid = ev.get("cid")
        f = self.inflight.get(cid) if cid else None
        if f is None or f.final:
            return
        sub = kind.split(".", 1)[1] if "." in kind else ""
        data = ev.get("data") or {}
        if sub in ("accepted", "rejected"):
            return  # 由 Admission 回复驱动
        if sub == "progress":
            now = time.monotonic_ns()
            if now - f.last_progress_ns < PROGRESS_MIN_NS:
                return
            f.last_progress_ns = now
            prog = dict(data.get("progress") or {})
            if prog.get("phase") not in ("admitted", "planning", "executing", "paused"):
                prog["phase"] = "executing"
            m = {"op": "progress", "id": f.cid, "data": prog}
        elif sub in ("running", "succeeded", "failed", "canceled", "timeout"):
            final = sub != "running"
            eff = data.get("effect") if isinstance(data.get("effect"), dict) and data.get("effect") else \
                {"status": "UNVERIFIED", "verify_trust": 2}
            m = result_msg(f.cid, sub, int(data.get("code", 0) or 0), final=True if final else None, effect=eff,
                           warnings=data.get("warnings") if isinstance(data.get("warnings"), list) else None)
        else:
            return
        if not f.accepted_sent:
            f.pending.append(m)
            return
        self._deliver(f, m)

    def _deliver(self, f: InFlight, m: dict) -> None:
        if m.get("op") == "result" and m.get("final"):
            self._final(f.cid, m)
        else:
            self._send(f, m)

    def _send(self, f: InFlight, m: dict) -> None:
        if m.get("op") == "result":
            f.last_result = m
            f.history.append({"status": m["status"], "code": m["code"], "t_sim_ns": int(self.gw.clock.t_sim_ns),
                              "t_wall_ns": str(time.time_ns())})
            if len(f.history) > 32:
                del f.history[:-32]
        s = f.session
        if s is not None and not s.closing and not f.silent:
            s.send_ctrl(m)

    def _final(self, cid: str, m: dict) -> None:
        f = self.inflight.get(cid)
        if f is None or f.final:
            return
        f.final = True
        f.t_final_ns = time.monotonic_ns()
        self._send(f, m)
        if f.final_ev is not None:
            f.final_ev.set()

    def wait_event(self, f: InFlight) -> asyncio.Event:
        if f.final_ev is None:
            f.final_ev = asyncio.Event()
            if f.final:
                f.final_ev.set()
        return f.final_ev

    # ------------------------------------------------------------ cancel、生产者重启、清理
    def handle_cancel(self, s: Any, m: dict) -> bool:
        """`cancel{id}`：机体调用转为 `uav/{id}/cmd/cancel{call_id}`；非机体调用或已终态返回 `error 105`。"""
        target = m.get("id")
        f = self.inflight.get(target) if isinstance(target, str) else None
        if f is None or f.final or f.kind != "cmd" or not f.uav:
            s.error(int(Reason.STATE), "cancel", target if isinstance(target, (str, int)) else None,
                    "调用不存在、已终态或不是机体调用")
            return False
        gw = self.gw
        if s.role == "viewer" or not gw.is_seat_holder(s.principal.id):
            s.error(int(Reason.ROLE_FORBIDDEN if s.role == "viewer" else Reason.SEAT_TAKEN), "cancel", target)
            return False
        ccid = f"{target}:cancel"[:96]
        e = gw.roster_entry(f.uav)
        producer = (e or {}).get("producer", gw.settings.producer)
        principal = gw.tokens.sign_principal(s.principal.id, s.role, ccid, conn_id=getattr(s, "conn_id", None),
                                             seat=gw.is_seat_holder(s.principal.id))
        msg = self._build("cmd", ccid, "cancel", f.uav, {"call_id": target}, principal)
        self._open(ccid, s, s.principal.id, f"uav/{f.uav}/cmd/cancel", "cancel", f.uav, "cmd", silent=True)
        self._spawn(self._dispatch(ccid, bus_keys.ctl_cmd(producer), "cmd", msg))
        return True

    def on_producer_restart(self, producer: str) -> None:
        """生产者无 checkpoint 重启：其名下已 accepted 未终态的调用以 `failed 212` 结束（FR-059）。"""
        eff = {"status": "FAILED", "verify_trust": 2, "message": "sim-core 重开，调用作废"}
        for f in list(self.inflight.values()):
            if not f.final and f.accepted_sent and f.kind == "cmd":
                self._final(f.cid, result_msg(f.cid, "failed", int(Reason.SIM_ROLLBACK), final=True, effect=eff))
        for agg in list(self.batches.values()):
            if agg.final or not agg.admitted:
                continue
            for vid, st in agg.states.items():
                if st not in TERMINAL:
                    agg.states[vid] = "failed"
                    agg.codes[vid] = int(Reason.SIM_ROLLBACK)
            self._finish_batch(agg, agg.counts())

    def gc(self) -> None:
        now = time.monotonic_ns()
        for cid in [c for c, f in self.inflight.items() if f.final and now - f.t_final_ns > INFLIGHT_KEEP_NS]:
            del self.inflight[cid]
            self.batches.pop(cid, None)

    def detach_session(self, s: Any) -> None:
        for f in self.inflight.values():
            if f.session is s:
                f.session = None

    def active_velocity(self, s: Any, uav: str) -> InFlight | None:
        for f in self.inflight.values():
            if f.session is s and f.uav == uav and f.op == "velocity" and not f.final and f.accepted_sent:
                return f
        return None


class ClientPublish:
    """CLIENT_DATA（C→S，opcode 0x20）：客户端 advertise 的 setpoint channel 与 32 B raw 转发（FR-062；17 §6.4、§9.4）。"""

    def __init__(self, gw: Gateway) -> None:
        self.gw = gw
        self._pub: Any = None
        self.stats = {"forwarded": 0, "dropped_seq": 0, "dropped_late": 0, "dropped_rate": 0, "denied": 0}

    def on_advertise(self, s: Any, channels: Any) -> None:
        gw = self.gw
        for c in channels if isinstance(channels, list) else []:
            cid = c.get("id") if isinstance(c, dict) else None
            topic = c.get("topic") if isinstance(c, dict) else None
            if not isinstance(cid, int) or not 1 <= cid <= 255 or not isinstance(topic, str):
                s.error(int(Reason.BAD_REQUEST), "advertise", cid if isinstance(cid, int) else None, "channel id 须为 1–255")
                continue
            parts = topic.split("/")
            if (len(parts) != 3 or parts[0] != "uav" or parts[2] != "setpoint" or c.get("encoding") != "raw"
                    or c.get("schemaName") != "awr.VelSetpoint16.v1"):
                s.error(int(Reason.CLIENT_PUBLISH_DENIED), "advertise", cid, "只允许 uav/{id}/setpoint（raw、VelSetpoint16）")
                continue
            if s.role == "viewer":
                s.error(int(Reason.CLIENT_PUBLISH_DENIED), "advertise", cid, "客户端发布需要 operator 角色")
                continue
            no = gw.agent_no_of(parts[1])
            if no is None:
                s.error(int(Reason.CLIENT_PUBLISH_DENIED), "advertise", cid, f"机体不在 roster：{parts[1]}")
                continue
            s.cpub[cid] = {"uav": parts[1], "agent_no": no, "last_seq": -1, "denied": False,
                           "bucket": _Bucket(CD_RATE_HZ, 6.0, time.monotonic())}

    def on_unadvertise(self, s: Any, ids: Any) -> None:
        for i in ids if isinstance(ids, list) else []:
            s.cpub.pop(i, None)

    def on_binary(self, s: Any, data: bytes) -> bool:
        """处理一条 CLIENT_DATA；返回 False 表示帧格式错误（由调用方计入 1002 判据）。"""
        hdr = F.CLIENT_DATA_HDR
        if len(data) < hdr.size or data[0] != F.OP_CLIENT_DATA:
            return False
        _op, flags, chid, seq, t_client = hdr.unpack_from(data, 0)
        payload = data[hdr.size:hdr.size + 16]
        if len(payload) != 16:
            return False
        st = s.cpub.get(chid)
        gw = self.gw
        if st is None:
            self._deny(s, chid, None, "该 channel 未 advertise")
            return True
        vel = gw.rpc.active_velocity(s, st["uav"])
        if vel is None or s.role == "viewer":
            self._deny(s, chid, st, "该机没有本连接发起的活动 velocity 调用")
            return True
        st["denied"] = False
        b = st["bucket"]
        b.refill(time.monotonic())
        if b.tokens < 1.0:
            self.stats["dropped_rate"] += 1
            s.stats["cd_dropped"] += 1
            return True
        b.tokens -= 1.0
        if seq <= st["last_seq"]:
            self.stats["dropped_seq"] += 1
            s.stats["cd_dropped"] += 1
            return True
        if t_client < gw.clock.sim_now_ns() - SIM_LAG_NS:
            self.stats["dropped_late"] += 1
            s.stats["cd_dropped"] += 1
            return True
        st["last_seq"] = seq
        frame = 1 if (vel.args or {}).get("frame") == "body" else 0
        pkt = F.SETPOINT_BUS.pack(st["agent_no"], flags, frame, seq, t_client) + bytes(payload)
        if self._pub is None:
            self._pub = gw.bus.publisher(bus_keys.CTL_SETPOINT)
        self._pub.put(pkt)
        self.stats["forwarded"] += 1
        s.stats["cd_forwarded"] += 1
        return True

    def _deny(self, s: Any, chid: int, st: dict | None, why: str) -> None:
        self.stats["denied"] += 1
        s.stats["cd_dropped"] += 1
        key = f"_cd_denied_{chid}"
        if st is not None:
            if st.get("denied"):
                return
            st["denied"] = True
        elif s.stats.get(key):
            return
        else:
            s.stats[key] = 1
        s.error(int(Reason.CLIENT_PUBLISH_DENIED), "clientData", chid, why)

    def close(self) -> None:
        if self._pub is not None:
            self._pub.close()
            self._pub = None

