"""握手、鉴权、访问模式、Origin 与 Host（M11-AC-008、AC-009；M11-FR-020 至 FR-022、FR-025、FR-029、FR-069；AWR-17 §3.2、
§3.3、§6.2、§8.3；D1-AC-33）。FakeSim + 真实 Gateway：
- 握手顺序 serverInfo → advertise → TIME → status；缺子协议 HTTP 400（`AWR-Supported-Protocols`）；无效 token `error 302` +
  4401；缺 token 301 + 4401；hello 前其他 op 回 `error 300`；hello 超时 317 + 4408；contracts 主版本不同 311 + 4426；
  `hello.role` 只降不升；
- 回环模式放行 `http://localhost:<任意端口>` 与 `http://127.0.0.1:<任意端口>`，其他 Origin 403（WS 与 REST）；Host 不在白名单
  400 `323`；局域网模式缺管理口令签 operator 401 `304`，admin 任何模式都需要口令；
- 审计：`auth.token_issued` 只记 jti、`auth.denied` 每来源每秒 ≤ 1 条；任何日志与审计行不含 token。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import time

import fakesim
import httpx
import pytest
import rtc
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

PROTO = "awr.rt.v1"


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=1)
    yield s
    s.close()


async def _closed_code(ws) -> int:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else -1


def test_handshake_order_and_hello_rules(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("access"))

    async def run() -> None:
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=st.origin,
                           compression=None, max_size=None)
        assert ws.subprotocol == PROTO
        c = rtc.Client(ws)
        info, adv, _t = await c.handshake()
        assert rtc.ops_errors(info) == [] and rtc.ops_errors(adv) == []
        assert info["role"] == "operator" and info["seat"] == "held"
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "event", "rate": 0, "mode": "all"}]})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "error", 3)
        assert e["code"] == 300 and e["ref"]["op"] == "subscribe"
        await c.send({"op": "hello", "client": "t", "contracts": "1.0.0", "role": "admin"})  # 只降不升
        await c.send({"op": "call", "id": "acc-hover-01", "service": "uav/f001/cmd/hover", "args": {}})
        r = await c.result("acc-hover-01")
        assert r["status"] != "rejected" or r["code"] != 115
        await ws.close()
        # 以 viewer 身份 hello：降级生效
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=st.origin,
                           compression=None)
        c = rtc.Client(ws)
        await c.handshake()
        await c.send({"op": "hello", "client": "t", "contracts": "1.0.0", "role": "viewer"})
        await c.send({"op": "call", "id": "acc-hover-02", "service": "uav/f001/cmd/hover", "args": {}})
        assert (await c.result("acc-hover-02"))["code"] == 115
        await ws.close()
        # contracts 主版本不同 → error 311 + 4426
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=st.origin, compression=None)
        c = rtc.Client(ws)
        await c.handshake()
        await c.send({"op": "hello", "client": "t", "contracts": "9.0.0"})
        assert await _closed_code(ws) == 4426
        # hello 超时（本栈 2 s）→ 317 + 4408
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + tok["token"]], origin=st.origin, compression=None)
        t0 = time.monotonic()
        assert await _closed_code(ws) == 4408 and time.monotonic() - t0 >= 1.8

    asyncio.run(run())


def test_ws_denials(st) -> None:
    async def run() -> None:
        with pytest.raises(InvalidStatus) as ei:
            await connect(st.ws_url, subprotocols=["other"], origin=st.origin, compression=None)
        assert ei.value.response.status_code == 400
        assert ei.value.response.headers.get("AWR-Supported-Protocols") == PROTO
        with pytest.raises(InvalidStatus) as ei:
            await connect(st.ws_url, subprotocols=[PROTO, "bearer.x"], origin="http://evil.example", compression=None)
        assert ei.value.response.status_code == 403
        for origin in ("http://localhost:5173", "http://127.0.0.1:9999", "https://localhost"):
            ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer.v1.bad.sig"], origin=origin, compression=None)
            c = rtc.Client(ws)
            _, e = await c.recv()
            assert e["op"] == "error" and e["code"] == 302
            assert await _closed_code(ws) == 4401
        ws = await connect(st.ws_url, subprotocols=[PROTO], origin=st.origin, compression=None)
        _, e = await rtc.Client(ws).recv()
        assert e["code"] == 301
        assert await _closed_code(ws) == 4401

    asyncio.run(run())


def test_rest_origin_host_and_lan_secret(st) -> None:
    r = httpx.get(f"{st.base}/api/sys/info", headers={"Origin": "http://evil.example"}, timeout=5)
    assert r.status_code == 403 and r.json()["code"] == 303 and r.headers["content-type"].startswith(
        "application/problem+json")
    assert httpx.get(f"{st.base}/api/sys/info", headers={"Origin": "http://localhost:1234"}, timeout=5).status_code == 200
    conn = http.client.HTTPConnection("127.0.0.1", st.port, timeout=5)
    conn.request("GET", "/api/health/live", headers={"Host": "attacker.example"})
    resp = conn.getresponse()
    body = json.loads(resp.read())
    assert resp.status == 400 and body["code"] == 323
    conn.close()
    r = httpx.post(f"{st.base}/api/auth/token", json={"role": "admin"}, timeout=5)
    assert r.status_code == 401 and r.json()["code"] == 304
    r = httpx.post(f"{st.base}/api/auth/token", json={"role": "admin", "admin_secret": "wrong"}, timeout=5)
    assert r.status_code == 401
    lan = fakesim.GwStack(n=1, access_mode="lan", origins=["http://10.0.0.5:8000"])
    try:
        r = httpx.post(f"{lan.base}/api/auth/token", json={"role": "operator"}, timeout=5)
        assert r.status_code == 401 and r.json()["code"] == 304
        r = httpx.post(f"{lan.base}/api/auth/token", timeout=5,
                       json={"role": "operator", "admin_secret": lan.settings.admin_password})
        assert r.status_code == 200 and r.json()["access_mode"] == "lan"
        assert httpx.get(f"{lan.base}/api/sys/info", headers={"Origin": "http://10.0.0.5:8000"}, timeout=5).status_code == 200
    finally:
        lan.close()


def test_audit_and_no_token_in_logs(st, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    tok = rtc.token(st.base, "viewer", client="tests/0.1")
    audit = st.settings.run_dir / "audit.jsonl"

    def denied_lines() -> list[dict]:
        rows = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()] if audit.exists() else []
        return [x for x in rows if x["kind"] == "auth.denied" and x["detail"]["why"] == "token"]

    before = len(denied_lines())
    time.sleep(1.1)  # 让同源每秒 1 条的审计节流窗口（可能被同一 stack 上的前序用例占用）先过期
    for _ in range(5):
        httpx.get(f"{st.base}/api/auth/whoami", headers={"Authorization": "Bearer v1.bad.token"}, timeout=5)
    deadline = time.monotonic() + 5.0  # 审计写入是异步的：按条件轮询，不用固定等待
    while len(denied_lines()) - before < 1 and time.monotonic() < deadline:
        time.sleep(0.1)
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
    issued = [x for x in lines if x["kind"] == "auth.token_issued"]
    assert issued and issued[-1]["detail"]["jti"] and issued[-1]["detail"]["client"] == "tests/0.1"
    new_denied = len(denied_lines()) - before
    assert 1 <= new_denied <= 2  # 同一来源每秒至多 1 条；5 次请求在 1 s 内发出，最多跨 2 个窗口
    text = audit.read_text(encoding="utf-8") + "\n".join(r.getMessage() + str(getattr(r, "kv", "")) for r in caplog.records)
    assert tok["token"] not in text and tok["token"].split(".")[1] not in text
    assert st.settings.admin_password not in text
