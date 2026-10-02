"""WS 端点 `GET /api/rt`（awr.rt.v1）：握手、版本协商与控制面分派（M11-FR-020、FR-025 至 FR-027、FR-029、FR-062；
AWR-17 §3.4、§6.2、§6.3、§8.3）。

握手顺序：Host（400 `323`）→ Origin（403 `303`）→ 子协议含 `awr.rt.v1`（否则 400 + `AWR-Supported-Protocols`）——这三步在
升级前以 `send_denial_response` 返回 problem+json → 以 `awr.rt.v1` 接受升级（不回显 bearer）→ api 停止中以 1001 关闭 →
token（`bearer.<token>`，失败发 `error{301|302}` 后 4401）→ 连接数（全部 ≤ 32、每 principal ≤ 8，`error 316` + 4429）→
serverInfo、全量 advertise、TIME、活动 status → 10 s 内等待 hello（4408；之前的其他 op 回 `error 300`）。hello：contracts
主版本不同发 `error 311` 并 4426；`role` 只能等于或低于 token 角色；记录 `client`、`tier`、`deviceClass`、`maxKbps`；
`resume.sessionId` 等于当前实例时补发 `seq > lastEventSeq` 的事件（环中已没有时下一帧置 GAP）。
上限：文本帧 ≤ 256 KiB 由 uvicorn `--ws-max-size 262144` 在协议层执行（1009）；二进制帧 > 4 KiB 以 1009 关闭；10 s 内 3 次
格式错误（非 JSON、缺 op、未知二进制 opcode、CLIENT_DATA 帧格式错误）以 1002 关闭；`ping` > 10 条/s、`clientStats` > 2 条/s
的部分忽略。二进制 CLIENT_DATA 在接收协程中立即转发（不等 tick，rpc.ClientPublish）。拒绝记审计 `auth.denied`（每来源每秒 ≤ 1 条）。
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
from awr.runtime.principal import TokenInvalid

from ..problem import PROBLEM_MEDIA, problem_body
from ..security import ROLE_RANK
from .awrrt import Capture, capture_dir
from .protocol import PROTOCOL, error_msg, jdump
from .session import ClientSession

if TYPE_CHECKING:
    from .gateway import Gateway

__all__ = ["rt_endpoint"]

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
    c = ws.client
    return c.host if c is not None else "?"


async def rt_endpoint(ws: WebSocket) -> None:
    ctx = ws.app.state.awr
    gw: Gateway = ctx.gateway
    st = ctx.settings
    audit = ctx.audit
    if not st.host_allowed(ws.headers.get("host")):
        audit.deny(_source(ws), "host", code=int(Reason.HOST_FORBIDDEN), detail={"host": ws.headers.get("host")})
        await _deny(ws, int(Reason.HOST_FORBIDDEN), 400)
        return
    origin = ws.headers.get("origin")
    if origin is not None and not st.origin_allowed(origin):
        audit.deny(_source(ws), "origin", code=int(Reason.ORIGIN_FORBIDDEN), detail={"origin": origin})
        await _deny(ws, int(Reason.ORIGIN_FORBIDDEN), 403)
        return
    protos = list(ws.scope.get("subprotocols") or [])
    if PROTOCOL not in protos:
        await _deny(ws, int(Reason.PROTOCOL_UNSUPPORTED), 400, {"AWR-Supported-Protocols": PROTOCOL})
        return
    token = next((p[len("bearer."):] for p in protos if p.startswith("bearer.")), None)
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
    if total >= st.max_conns or mine >= st.max_conns_per_principal:
        await _send_json(ws, error_msg(int(Reason.CONN_LIMIT), "hello", message="连接数超过上限"))
        await ws.close(code=4429)
        return
    s = ClientSession(gw, ws, "c-" + secrets.token_hex(6), principal)
    cap = capture_dir(st.profile)
    if cap is not None:
        s.capture = Capture(cap, s.conn_id, gw.session_id, principal.role)
    gw.add_session(s)
    s.sender_task = asyncio.ensure_future(s.sender())
    gw.handshake(s)
    t_hello = time.monotonic() + st.hello_timeout_s
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
    if op == "subscribe":
        subs = m.get("subs")
        if not isinstance(subs, list):
            s.error(int(Reason.BAD_REQUEST), op, message="subs 必须是数组")
        else:
            s.subscribe(subs)
    elif op == "unsubscribe":
        s.unsubscribe(m.get("ids") or [])
    elif op == "ack":
        with contextlib.suppress(TypeError, ValueError):
            s.on_ack(int(m.get("frame", 0)), m.get("fps"), m.get("decodeMs"), m.get("lagMs"))
    elif op == "ping":
        if not gw.limiter.check(pid + "#" + s.conn_id, "ping"):
            s.on_ping(m.get("t", 0), m.get("srttMs"))
    elif op == "call":
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
