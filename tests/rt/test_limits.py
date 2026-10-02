"""限流与上限（M11-AC-010；M11-FR-026、FR-027、FR-080；AWR-17 §3.4、§4.1）：
- 第 33 个连接 4429（全部 ≤ 32）；每 principal 第 9 个连接 4429；
- 第 257 个订阅 `error 315`；≥ 30 Hz 的 Full64 channel 超过 64 `error 315`；
- 257 KiB 文本帧 1009（uvicorn `ws_max_size = 262144`）；10 s 内 3 次格式错误 1002；
- subscribe/unsubscribe 20 条/s、突发 100（超出 `error 111`）；`ping` > 10 条/s 的部分忽略；
- REST：请求体 > 1 MiB 413 `307`；Idempotency-Key 重放返回原响应（`Idempotent-Replayed: true`），同 key 不同请求体 422 `321`；
  REST 命令镜像计入同一 principal 的 `call` 桶（429 `111` + Retry-After）。
"""

from __future__ import annotations

import asyncio

import fakesim
import httpx
import pytest
import rtc
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

PROTO = "awr.rt.v1"


@pytest.fixture(scope="module")
def st():
    # hello 超时放宽到 30 s：test_connection_limits 先开 32 个不发 hello 的连接，负载下开完之前最早的连接会被 4408 关闭，
    # 第 33 个就不再撞上限（INT-1 §7.11）；本模块没有依赖 hello 超时的断言
    s = fakesim.GwStack(n=70, sim_kw={"sensors": False, "hz": 30.0}, hello_timeout_s=30.0)
    yield s
    s.close()


async def _code(ws) -> int:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else -1


def test_connection_limits(st) -> None:
    async def run() -> None:
        one = rtc.token(st.base, "viewer", rtc.hint_of("connlimitone"))["token"]
        others = [rtc.token(st.base, "viewer")["token"] for _ in range(25)]  # 先取令牌，缩短开连接的窗口
        opened = [await rtc.open_client(st, one, hello=False) for _ in range(8)]
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + one], origin=st.origin, compression=None)
        assert await _code(ws) == 4429  # 每 principal ≤ 8
        opened += [await rtc.open_client(st, t, hello=False) for t in others[:24]]
        ws = await connect(st.ws_url, subprotocols=[PROTO, "bearer." + others[24]], origin=st.origin, compression=None)
        c = rtc.Client(ws)
        _, e = await c.recv()
        assert e["op"] == "error" and e["code"] == 316
        assert await _code(ws) == 4429  # 全部 ≤ 32
        for c in opened:
            await c.ws.close()

    asyncio.run(run())


def test_subscription_limits_and_rate(st) -> None:
    tok = rtc.token(st.base, "viewer")["token"]

    async def run() -> None:
        await asyncio.sleep(0.2)
        c = await rtc.open_client(st, tok)
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/*/state", "rate": 30, "mode": "latest"}]})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "error", 3)
        assert e["code"] == 315  # 70 架 × 30 Hz Full64 > 64
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "uav/*/state", "rate": 10, "mode": "latest"}]})
        await c.until(lambda k, x: k == "json" and x["op"] == "subscribed" and x["id"] == 1, 3)
        errs = 0
        for i in range(2, 258):
            await c.send({"op": "subscribe", "subs": [{"id": i, "topic": f"uav/f{(i % 70) + 1:03d}/state_ext",
                                                       "rate": 2, "mode": "latest"}]})
            await asyncio.sleep(0.052)  # 不触发 20 条/s 的订阅限流
        await c.drain(0.3)
        codes = [m["code"] for m in c.texts if m["op"] == "error"]
        errs = codes.count(315)
        assert errs == 2, codes  # 已有 1 个 + 255 个 = 256；第 257、258 个 315
        # 订阅操作限流：20 条/s、突发 100；一次 150 条中超出的部分 111
        await asyncio.sleep(5.2)
        await c.send({"op": "unsubscribe", "ids": list(range(2, 152))})
        await c.drain(0.3)
        assert 40 <= [m["code"] for m in c.texts if m["op"] == "error"].count(111) <= 50
        await c.ws.close()

    asyncio.run(run())


def test_frame_size_and_format_errors(st) -> None:
    tok = rtc.token(st.base, "viewer")["token"]

    async def run() -> None:
        c = await rtc.open_client(st, tok)
        await c.ws.send("x" * (257 * 1024))
        assert await _code(c.ws) == 1009
        c = await rtc.open_client(st, tok)
        for _ in range(3):
            await c.ws.send("{not json")
        assert await _code(c.ws) == 1002
        c = await rtc.open_client(st, tok)
        for i in range(30):
            await c.send({"op": "ping", "t": float(i)})
        await c.drain(0.5)
        pongs = [m for m in c.texts if m["op"] == "pong"]
        assert 8 <= len(pongs) <= 13  # > 10 条/s 的部分忽略（突发 10）
        await c.ws.close()

    asyncio.run(run())


def test_rest_body_limit_idempotency_and_call_bucket(st) -> None:
    tok = rtc.token(st.base, "operator", rtc.hint_of("restlimits"))
    h = {"Authorization": f"Bearer {tok['token']}"}
    r = httpx.post(f"{st.base}/api/commands", content=b"{" + b" " * (1 << 20) + b"}", headers=h | {
        "Content-Type": "application/json"}, timeout=10)
    assert r.status_code == 413 and r.json()["code"] == 307
    ik = {"Idempotency-Key": "k-0001"}
    r1 = httpx.post(f"{st.base}/api/auth/token", json={"role": "viewer"}, headers=ik, timeout=5)
    r2 = httpx.post(f"{st.base}/api/auth/token", json={"role": "viewer"}, headers=ik, timeout=5)
    assert r1.status_code == r2.status_code == 200 and r1.json() == r2.json()
    assert r2.headers.get("idempotent-replayed") == "true" and "idempotent-replayed" not in r1.headers
    r3 = httpx.post(f"{st.base}/api/auth/token", json={"role": "viewer", "client": "x"}, headers=ik, timeout=5)
    assert r3.status_code == 422 and r3.json()["code"] == 321

    async def burst() -> list[httpx.Response]:
        async with httpx.AsyncClient(timeout=20, limits=httpx.Limits(max_connections=200)) as cl:
            return await asyncio.gather(*(cl.post(f"{st.base}/api/commands", headers=h, json={
                "id": f"rl-rest-{i:04d}", "service": "uav/f001/cmd/hover", "args": {}, "timeout_ms": 500})
                for i in range(150)))

    got = asyncio.run(burst())
    limited = [g for g in got if g.status_code == 429]
    assert limited and limited[0].json()["code"] == 111 and int(limited[0].headers["retry-after"]) >= 1
