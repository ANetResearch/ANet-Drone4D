"""WS 端点 `GET /api/rt`（awr.rt.v1）：握手、版本协商与控制面分派（M11-FR-020、FR-025 至 FR-027、FR-029、FR-062；
AWR-17 §3.4、§6.2、§6.3、§8.3）。

握手顺序：Host（400 `323`）→ Origin（403 `303`）→ 子协议含 `awr.rt.v1`（否则 400 + `AWR-Supported-Protocols`）——这三步在
升级前以 `send_denial_response` 返回 problem+json → 以 `awr.rt.v1` 接受升级（不回显 bearer）→ api 停止中以 1001 关闭 →
token（`bearer.<token>`，失败发 `error{301|302}` 后 4401）→ 连接数（全部 ≤ 32、每 principal ≤ 8，配置了 `AWR_WS_MAX_PER_IP` 时
每个客户端地址 ≤ 该值（公开模式 4，全部 ≤ 64，ADR-082），`error 316` + 4429）→
serverInfo、全量 advertise、TIME、活动 status → 10 s 内等待 hello（4408；之前的其他 op 回 `error 300`）。hello：contracts
主版本不同发 `error 311` 并 4426；`role` 只能等于或低于 token 角色；记录 `client`、`tier`、`deviceClass`、`maxKbps`；
`resume.sessionId` 等于当前实例时补发 `seq > lastEventSeq` 的事件（环中已没有时下一帧置 GAP）。
上限：文本帧 ≤ 256 KiB 由 uvicorn `--ws-max-size 262144` 在协议层执行（1009）；二进制帧 > 4 KiB 以 1009 关闭；10 s 内 3 次
格式错误（非 JSON、缺 op、未知二进制 opcode、CLIENT_DATA 帧格式错误）以 1002 关闭；`ping` > 10 条/s、`clientStats` > 2 条/s
的部分忽略。二进制 CLIENT_DATA 在接收协程中立即转发（不等 tick，rpc.ClientPublish）。拒绝记审计 `auth.denied`（每来源每秒 ≤ 1 条）。

D2：每地址连接上限改按地址前缀计（IPv4 /32、IPv6 /64，M11-FR-209、FR-210）。沙盒端点 `/api/sandbox/v1/sessions/{sid}/rt`
（sandbox-api，`sandbox_rt_endpoint`；17 §17.7.1、§17.9.3）：Host、Origin、子协议同上 → 接受升级 → token：缺失 `error{301}`
+ 4401，签名或过期 `error{302}` + 4401，sid 不符 `error{503}` + 4410 → 会话不存在、ENDED、FAILED `error{305、506 或 510}` +
4410 → 连接数（全站 `4·N_max`、每前缀 4、每会话 4）`error{316}` + 4429 → 会话尚未就绪（SPAWNING、上下文绑定中或底图切换中）
时先发 `status{id: "sandbox.spawning", level: "info"}` 并等待其就绪（≤ `sandbox.spawn.timeout_s`），就绪后照 D1 握手
（serverInfo 带 `sandbox` 块）并 `removeStatus`；等待中会话结束以 4410 关闭，超时以 1013 关闭（客户端按退避重连）。
此后协议与 `/api/rt` 完全相同；沙盒内 `playback` 以 `error{115}` 拒绝。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import time
from typing import TYPE_CHECKING

from fastapi import WebSocket
from starlette.responses import JSONResponse

from awr.contracts.reasons import Reason
from awr.runtime.binding import is_sid
from awr.runtime.principal import TokenInvalid

from ..identity import subprotocol_token
from ..problem import PROBLEM_MEDIA, problem_body
from ..public import prefix_of
from ..security import ROLE_RANK, SandboxScopeError
from . import presence as P
from .awrrt import Capture, capture_dir
from .protocol import PROTOCOL, error_msg, jdump, status_msg
from .session import ClientSession

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["rt_endpoint", "sandbox_rt_endpoint"]

MAX_BINARY = 4096
STATS_FIELDS = ("fps", "frameMs", "frameP95Ms", "decodeMs", "heapMB", "droppedFrames", "pointBudget", "tier",
                "deviceClass", "latencyP95Ms", "dGlobalMs")


async def _deny(ws: WebSocket, code: int, status: int, headers: dict[str, str] | None = None) -> None:
    body = problem_body(code, status=status)
    resp = JSONResponse(body, status_code=status, headers=headers or {}, media_type=PROBLEM_MEDIA)
    try:
        await ws.send_denial_response(resp)
    except Exception:
        with contextlib.suppress(Exception):
            await ws.close(code=1008)


def _source(ws: WebSocket) -> str:
    """客户端地址（可信反向代理之后按 X-Forwarded-For，`public.client_ip`）：只用于连接计数与审计来源。"""
    c = ws.client
    st = ws.app.state.awr.settings
    return st.client_ip(c.host if c is not None else None, ws.headers.get("x-forwarded-for"), ws.headers.get("x-real-ip"))


_DENIED = object()


async def _pre_accept(ws: WebSocket, st: object, audit: object) -> object:
    """升级之前：Host（400 `323`）→ Origin（403 `303`）→ 子协议（400）。返回 bearer token（可为 None）或 _DENIED。"""
    if not st.host_allowed(ws.headers.get("host")):  # type: ignore[attr-defined]
        audit.deny(_source(ws), "host", code=int(Reason.HOST_FORBIDDEN), detail={"host": ws.headers.get("host")})  # type: ignore[attr-defined]
        await _deny(ws, int(Reason.HOST_FORBIDDEN), 400)
        return _DENIED
    origin = ws.headers.get("origin")
    if origin is not None and not st.origin_allowed(origin):  # type: ignore[attr-defined]
        audit.deny(_source(ws), "origin", code=int(Reason.ORIGIN_FORBIDDEN), detail={"origin": origin})  # type: ignore[attr-defined]
        await _deny(ws, int(Reason.ORIGIN_FORBIDDEN), 403)
        return _DENIED
    protos = list(ws.scope.get("subprotocols") or [])
    if PROTOCOL not in protos:
        await _deny(ws, int(Reason.PROTOCOL_UNSUPPORTED), 400, {"AWR-Supported-Protocols": PROTOCOL})
        return _DENIED
    return next((p[len("bearer."):] for p in protos if p.startswith("bearer.")), None)


async def _refuse(ws: WebSocket, code: int, close: int, message: str) -> None:
    await _send_json(ws, error_msg(code, "hello", message=message))
    with contextlib.suppress(Exception):
        await ws.close(code=close)


async def rt_endpoint(ws: WebSocket) -> None:
    ctx = ws.app.state.awr
    gw: Gateway = ctx.gateway
    st = ctx.settings
    audit = ctx.audit
    token = await _pre_accept(ws, st, audit)
    if token is _DENIED:
        return
    await ws.accept(subprotocol=PROTOCOL)
    if gw is None or gw.stopping:
        await ws.close(code=1001)
        return
    try:
        if not token:
            raise TokenInvalid("缺少 token")
        principal = ctx.tokens.verify(token)
        code = 0
    except TokenInvalid:
        principal, code = None, int(Reason.AUTH_REQUIRED) if not token else int(Reason.TOKEN_INVALID)
    if principal is None:
        audit.deny(_source(ws), "token", code=code)
        await _send_json(ws, error_msg(code, "hello", message="token 缺失、无效或过期"))
        await ws.close(code=4401)
        return
    total, mine = gw.conn_count(principal.id)
    ip = _source(ws)
    pfx = prefix_of(ip)
    per_ip = st.max_conns_per_ip is not None and gw.conn_count_prefix(pfx) >= st.max_conns_per_ip
    if total >= st.max_conns or mine >= st.max_conns_per_principal or per_ip:
        await _send_json(ws, error_msg(int(Reason.CONN_LIMIT), "hello", message="连接数超过上限"))
        await ws.close(code=4429)
        return
    # D2.1：身份令牌（子协议 ident.<令牌>，M11-FR-227）与观看席位（viewer 且未带身份令牌，M11-FR-223）
    ident = None
    idsvc = getattr(ctx, "identity", None)
    itok = subprotocol_token(list(ws.scope.get("subprotocols") or []))
    if itok and idsvc is not None:
        ident = idsvc.verify(itok)
    seats = getattr(ctx, "seats", None)
    take_seat = seats is not None and principal.role == "viewer" and ident is None
    if take_seat and not seats.admit(principal.id):
        m = error_msg(int(Reason.VIEWER_SEATS_FULL), "hello", message="实时观看席位已满，请排队")
        m["detail"] = {"queue_url": "/api/viewer/queue"}
        await _send_json(ws, m)
        await ws.close(code=P.CLOSE_VIEWER_SEATS_FULL)
        return
    try:
        await _serve(ws, gw, principal, ip, st, identity=ident, seat=take_seat)
    finally:
        if take_seat:
            seats.release()


async def sandbox_rt_endpoint(ws: WebSocket, sid: str) -> None:
    """沙盒 WS（见模块说明）。`ws.app.state.awr` 为 sandbox-api 的应用上下文（`kind = "sandbox"`，带 hub 与沙盒 token 服务）。"""
    app = ws.app.state.awr
    st = app.settings
    audit = app.audit
    token = await _pre_accept(ws, st, audit)
    if token is _DENIED:
        return
    await ws.accept(subprotocol=PROTOCOL)
    if getattr(app, "stopping", False):
        await ws.close(code=1001)
        return
    src = _source(ws)
    if not token:
        audit.deny(src, "token", code=int(Reason.AUTH_REQUIRED))
        await _refuse(ws, int(Reason.AUTH_REQUIRED), 4401, "token 缺失")
        return
    if not is_sid(sid):
        await _refuse(ws, int(Reason.NOT_FOUND), 4410, "沙盒不存在")
        return
    try:
        principal = app.tokens.verify(token, sid=sid)
    except SandboxScopeError:
        audit.deny(src, "sandbox_scope", code=503, detail={"path_sid": sid})
        await _refuse(ws, 503, 4410, "token 不属于该沙盒")
        return
    except TokenInvalid:
        audit.deny(src, "token", code=int(Reason.TOKEN_INVALID))
        await _refuse(ws, int(Reason.TOKEN_INVALID), 4401, "token 无效或过期")
        return
    hub = app.hub
    view = hub.view(sid)
    if view is None and hub.ended_reason(sid) is None:
        view = await hub.refresh(sid)  # 刚创建、池的变更尚未到达镜像
    if view is None or view.state in ("ENDED", "FAILED"):
        end = hub.ended_reason(sid)
        code = 510 if end == "preempted" else (506 if end is not None or view is not None else int(Reason.NOT_FOUND))
        await _refuse(ws, code, 4410, "沙盒不可用")
        return
    if view.state in ("PARKING", "PARKED"):
        view = await hub.refresh(sid)  # 镜像可能落后于刚完成的恢复
        if view is not None and view.state in ("PARKING", "PARKED"):
            m = error_msg(int(Reason.IDLE_RECLAIMED), "hello", message="沙盒已节能驻留，请先恢复")
            m["detail"] = {"reason": "parked", "resume_url": f"/api/sandbox/v1/sessions/{sid}/resume"}
            await _send_json(ws, m)
            with contextlib.suppress(Exception):
                await ws.close(code=P.CLOSE_IDLE_RECLAIMED)
            return
        if view is None:
            await _refuse(ws, 506, 4410, "沙盒不可用")
            return
    pfx = prefix_of(src)
    total, per_pfx, per_sid = hub.conn_counts(pfx, sid)
    ws_cfg = app.sb.ws
    if total >= hub.ws_max or per_pfx >= ws_cfg.per_prefix or per_sid >= ws_cfg.per_session:
        why = "site" if total >= hub.ws_max else ("prefix" if per_pfx >= ws_cfg.per_prefix else "session")
        await _refuse(ws, int(Reason.CONN_LIMIT), 4429, f"连接数超过上限（{why}）")
        return
    c = hub.get(sid)
    waited = False
    if c is None or not c.live:
        waited = True
        await _send_json(ws, status_msg("sandbox.spawning", "info", "沙盒正在启动", source="sandbox-api"))
        c = await hub.wait_live(sid, float(app.sb.spawn.timeout_s))
        if c is None:
            gone = hub.view(sid) is None
            await _refuse(ws, 506 if gone else int(Reason.SIM_UNAVAILABLE), 4410 if gone else 1013,
                          "沙盒已结束" if gone else "沙盒尚未就绪，请稍后重连")
            return
    gw = c.gateway
    if gw is None or gw.stopping:
        await ws.close(code=1013)
        return
    tier = "internal" if (view.raw.get("tier") == "internal") else "public"
    await _serve(ws, gw, principal, src, c.settings or st,
                 first=[{"op": "removeStatus", "ids": ["sandbox.spawning"]}] if waited else None, tier=tier)


async def _serve(ws: WebSocket, gw: Gateway, principal: object, ip: str, st: object, *,
                 first: list[dict] | None = None, identity: dict | None = None, seat: bool = False,
                 tier: str | None = None) -> None:
    """握手之后的连接生命周期（D1 与沙盒共用）：建 ClientSession、握手、等 hello、收发循环、清理。"""
    s = ClientSession(gw, ws, "c-" + secrets.token_hex(6), principal)
    s.client_ip = ip
    s.client_prefix = prefix_of(ip)
    s.seat_held = seat
    if identity is not None:
        s.identity_tier = "internal"
        s.identity_exp = int(identity.get("exp") or 0) or None
    elif tier == "internal":
        s.identity_tier = "internal"
    cap = capture_dir(st.profile)  # type: ignore[attr-defined]
    if cap is not None:
        s.capture = Capture(cap, s.conn_id, gw.session_id, principal.role)
    gw.add_session(s)
    s.sender_task = asyncio.ensure_future(s.sender())
    gw.handshake(s)
    for m in first or ():
        s.send_ctrl(m)
    t_hello = time.monotonic() + st.hello_timeout_s  # type: ignore[attr-defined]
    try:
        while not s.closing:
            timeout = None if s.hello else max(0.0, t_hello - time.monotonic())
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout)
            except TimeoutError:
                s.error(int(Reason.HELLO_TIMEOUT), "hello", message="10 s 内未收到 hello")
                await asyncio.sleep(0.05)
                await ws.close(code=4408)
                break
            if msg["type"] == "websocket.disconnect":
                break
            if s.capture is not None:
                s.capture.c2s(msg["bytes"] if msg.get("bytes") is not None else msg.get("text") or "")
            data = msg.get("bytes")
            if data is not None:
                if len(data) > MAX_BINARY:
                    await ws.close(code=1009)
                    break
                if not s.hello:
                    s.error(int(Reason.BAD_REQUEST), "clientData", message="hello 之前只接受 hello")
                    continue
                if not gw.cpub.on_binary(s, data):
                    s.error(int(Reason.BAD_REQUEST), "clientData", message="未知二进制 opcode 或 CLIENT_DATA 帧格式错误")
                    if _format_error(s):
                        await ws.close(code=1002)
                        break
                continue
            if await _on_text(gw, s, msg.get("text") or ""):
                break
    except Exception:
        pass
    finally:
        s.closing = True
        s.wake.set()
        gw.remove_session(s)
        if s.sender_task is not None:
            s.sender_task.cancel()
        if s.capture is not None:
            s.capture.close()


async def _send_json(ws: WebSocket, obj: dict) -> None:
    with contextlib.suppress(Exception):
        await ws.send_text(jdump(obj))


_DEFAULT_POWER = P.PowerView()


def _power_view(gw: Gateway) -> P.PowerView:
    """presence 限频取 serverInfo.power 的 `presence_min_s`（show-api 与 hub 设置；缺省 15 s）。"""
    pi = getattr(gw, "power_info", None) or {}
    v = pi.get("presence_min_s")
    if isinstance(v, (int, float)) and v != _DEFAULT_POWER.presence_min_s:
        return P.PowerView(presence_min_s=float(v))
    return _DEFAULT_POWER


def _format_error(s: ClientSession) -> bool:
    now = time.monotonic()
    s.fmt_errors.append(now)
    return len(s.fmt_errors) == 3 and now - s.fmt_errors[0] <= 10.0


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


async def _on_text(gw: Gateway, s: ClientSession, text: str) -> bool:
    """处理一条控制消息；返回 True 表示连接应结束。"""
    try:
        m = json.loads(text)
        op = m["op"]
        if not isinstance(op, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        s.error(int(Reason.BAD_REQUEST), "unknown", message="控制消息必须是含 op 的 JSON 对象")
        if _format_error(s):
            await s.ws.close(code=1002)
            return True
        return False
    if not s.hello:
        if op != "hello":
            s.error(int(Reason.BAD_REQUEST), op, message="hello 之前只接受 hello")
            return False
        return await _on_hello(gw, s, m)
    pid = s.principal.id
    if op == "presence":
        P.on_presence(s, m, _power_view(gw))
    elif op == "subscribe":
        subs = m.get("subs")
        if not isinstance(subs, list):
            s.error(int(Reason.BAD_REQUEST), op, message="subs 必须是数组")
        else:
            gw.interest.note_subscribe([str(x.get("topic", "")) for x in subs if isinstance(x, dict)])
            s.subscribe(subs)
            if not s.presence_seen:
                P.note_input(s)  # 不发 presence 的旧客户端：订阅变更计活动（M11-FR-221）
    elif op == "unsubscribe":
        s.unsubscribe(m.get("ids") or [])
        if not s.presence_seen:
            P.note_input(s)
    elif op == "ack":
        with contextlib.suppress(TypeError, ValueError):
            s.on_ack(int(m.get("frame", 0)), m.get("fps"), m.get("decodeMs"), m.get("lagMs"))
    elif op == "ping":
        if not gw.limiter.check(pid + "#" + s.conn_id, "ping"):
            s.on_ping(m.get("t", 0), m.get("srttMs"))
    elif op == "call":
        P.note_input(s)
        gw.rpc.handle_call(s, m)
    elif op == "cancel":
        gw.rpc.handle_cancel(s, m)
    elif op == "clientStats":
        if not gw.limiter.check(pid + "#" + s.conn_id, "stats"):
            s.client_stats = {k: m[k] for k in STATS_FIELDS if k in m}
            s.client_stats["t_unix_ns"] = str(time.time_ns())
    elif op == "hello":
        pass
    elif op == "advertise":
        gw.cpub.on_advertise(s, m.get("channels"))
    elif op == "unadvertise":
        gw.cpub.on_unadvertise(s, m.get("ids"))
    elif op == "playback" and gw.policy is not None:
        s.error(int(Reason.ROLE_FORBIDDEN), op, m.get("request_id") if isinstance(m.get("request_id"), str) else None,
                "沙盒不提供回放")
    elif op == "playback":
        pb = getattr(gw, "playback", None)
        if pb is None:
            s.error(int(Reason.SERVICE_UNAVAILABLE), op, m.get("request_id") if isinstance(m.get("request_id"), str)
                    else None, "回放服务不可用")
        else:
            pb.handle(s, m)
    else:
        s.error(int(Reason.UNKNOWN_OP), op[:64], message=f"不认识的 op：{op[:64]}")
    return False


async def _on_hello(gw: Gateway, s: ClientSession, m: dict) -> bool:
    major = str(m.get("contracts", "")).split(".")[0]
    if major != gw.settings.contracts.split(".")[0]:
        s.error(int(Reason.PROTOCOL_UNSUPPORTED), "hello", message=f"contracts 主版本不同：{m.get('contracts')}")
        await asyncio.sleep(0.05)
        await s.ws.close(code=4426)
        return True
    role = m.get("role")
    if isinstance(role, str) and role in ROLE_RANK and ROLE_RANK[role] <= ROLE_RANK.get(s.role, 0):
        s.role = role
    s.client = str(m.get("client", ""))[:64]
    s.tier = m.get("tier") if m.get("tier") in ("A", "B", "S") else None
    s.device_class = m.get("deviceClass") if isinstance(m.get("deviceClass"), str) else None
    kbps = _num(m.get("maxKbps"))
    s.max_kbps = kbps if kbps is not None and kbps > 0 else None
    if s.max_kbps:
        s.bucket.retune(None, s.max_kbps)
    s.hello = True
    res = m.get("resume")
    if isinstance(res, dict) and res.get("sessionId") == gw.session_id:
        with contextlib.suppress(TypeError, ValueError):
            since = int(res.get("lastEventSeq", 0))
            if since + 1 < gw.events.oldest and since < gw.events.newest:
                s.event_gap = True
                s.send_ctrl({"op": "status", "id": "events.gap", "level": "warning",
                             "message": "断线期间的部分事件已超出事件环，请经 /api/events 补拉", "source": "api",
                             "code": int(Reason.EVENTS_TRUNCATED)})
            s.pending_events.extend(gw.events.since(since))  # hello 在订阅之前：补发不按 filter 过滤
    pb = getattr(gw, "playback", None)
    if pb is not None:
        pb.on_hello(s)  # 回放模式：迟到者单独收到当前 playbackState（17 §6.11 补充约定第 5 条）
    return False
