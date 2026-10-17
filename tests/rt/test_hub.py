"""GatewayHub 与 sandbox-api 的会话生命周期（M11-AC-203、AC-206、AC-209 的功能部分；M11-FR-204 至 FR-206、FR-214、FR-215；
AWR-04 §4.6、§4.7；17 §17.7.1、§17.9.3）。FakePool + FakeSim，全部进程内（LocalBus、LocalRing），不依赖 awr.sandbox。"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import msgpack
import pytest
import rtc
from fakepool import SbStack
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from awr.api.rt.hub import SessionView, build_session_state
from awr.contracts import CONTRACTS_VERSION


async def open_sb(stack: SbStack, sid: str, tok: str | None, *, hello: bool = True, handshake: bool = True) -> rtc.Client:
    protos = [rtc.PROTO] + ([f"bearer.{tok}"] if tok else [])
    ws = await connect(stack.ws_url(sid), subprotocols=protos, origin=stack.origin, max_size=None, compression=None,
                       open_timeout=10)
    c = rtc.Client(ws)
    if handshake:
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo", 10)
        await c.until(lambda k, x: k == "time", 5)
        if hello:
            await c.send({"op": "hello", "client": "tests-sbx/0.1", "contracts": CONTRACTS_VERSION})
    return c


async def closed_with(c: rtc.Client, timeout: float = 6.0) -> tuple[int | None, list[dict]]:
    """收到关闭为止，返回 (关闭码, 期间的 error 消息)。"""
    errs: list[dict] = []
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            kind, x = await c.recv(end - time.monotonic())
            if kind == "json" and x["op"] == "error":
                errs.append(x)
    except ConnectionClosed as e:
        return (e.rcvd.code if e.rcvd else None), errs
    except TimeoutError:
        return None, errs
    return None, errs


@pytest.fixture(scope="module")
def sbx():
    s = SbStack()
    yield s
    s.close()


def test_ready_session_becomes_live_and_serves_ws(sbx: SbStack) -> None:
    sid = sbx.pool.add(principal="p-alice")
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)

    async def go() -> None:
        c = await open_sb(sbx, sid, tok)
        info = next(m for m in c.texts if m["op"] == "serverInfo")
        assert rtc.ops_errors(info) == [], rtc.ops_errors(info)
        assert "sandbox" in info["capabilities"] and info["run"]["id"] == sid
        assert info["sandbox"]["sid"] == sid and info["sandbox"]["state"] == "READY"
        assert info["clock"]["tick_hz"] == 100 and info["principal"] == "p-alice" and info["seat"] == "held"
        assert "playbackControl" not in info["capabilities"]
        assert "sandbox/session" in c.topic_ids
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "sandbox/session", "rate": 1},
                                                  {"id": 2, "topic": "fleet/roster", "rate": 10}]})
        cid = c.topic_ids["sandbox/session"]

        def ws1(k: str, x: Any) -> bool:
            if k != "batch" or x.by_channel(cid) is None:
                return False
            return msgpack.unpackb(x.payload(x.by_channel(cid)), raw=False)["usage"]["ws"] == 1

        _, b = await c.until(ws1, 5)  # 1 Hz：连接之后的下一条带 ws = 1
        st = msgpack.unpackb(b.payload(b.by_channel(cid)), raw=False)
        assert rtc.schema_errors("rt/payloads/sandbox_session_state.schema.json", st) == []
        assert st["sid"] == sid and st["state"] == "READY" and st["clock"]["rate_cap"] > 0
        # 席位固定给会话 principal：写命令经网关准入后到达该会话的 sim-core（以 per-run K_entry 验签）
        await c.send({"op": "call", "id": "c-hover-0001", "service": "uav/f001/cmd/hover", "args": {}})
        r = await c.result("c-hover-0001", final=False)
        assert r["status"] in ("accepted", "running", "succeeded"), r
        sim = sbx.pool.sims[sid]
        assert any(m.get("cid") == "c-hover-0001" for m in sim.commands)
        await c.ws.close()

    asyncio.run(go())


def test_denied_services_and_rate_grant(sbx: SbStack) -> None:
    """沙盒内 `sys/*`、`seat/takeover`、`rec/*`、`kill`、`escalate`、`sim/reset` 为 115；`sim/speed` 经池授予（M17-FR-039）。"""
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)

    async def go() -> None:
        c = await open_sb(sbx, sid, tok)
        for i, svc in enumerate(("rec/start", "seat/takeover", "uav/f001/cmd/kill", "uav/f001/cmd/escalate", "sim/reset")):
            await c.send({"op": "call", "id": f"c-deny-{i:04d}", "service": svc, "args": {}})
            r = await c.result(f"c-deny-{i:04d}")
            assert r["status"] == "rejected" and r["code"] == 115, (svc, r)
        await c.send({"op": "call", "id": "c-speed-0001", "service": "sim/speed", "args": {"rate": 10}})
        r = await c.result("c-speed-0001")
        assert r["status"] == "succeeded", r
        assert r["data"]["granted_rate"] == 5.0 and r["data"]["rate_cap"] == 5.0
        assert r["warnings"] == ["SANDBOX_RATE_LIMITED"]
        sim = sbx.pool.sims[sid]
        assert any(m.get("op") == "speed" and m.get("args", {}).get("rate") == 5.0 for m in sim.clock_ops)
        assert ("rate", {"sid": sid, "rate": 10.0}) in sbx.pool.calls
        await c.ws.close()

    asyncio.run(go())


def test_end_closes_with_4410_and_world_switch_1012(sbx: SbStack) -> None:
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)

    async def go() -> None:
        c = await open_sb(sbx, sid, tok)
        sbx.pool.switch_world(sid, "synthcity")
        code, _ = await closed_with(c)
        assert code == 1012
        sbx.pool.start_sim(sid)
        sbx.pool.set_state(sid, "READY")
        sbx.wait_live(sid)
        assert sbx.call_in_loop(lambda: sbx.hub.get(sid).binding.world_id) == "synthcity"
        c2 = await open_sb(sbx, sid, tok)
        info = next(m for m in c2.texts if m["op"] == "serverInfo")
        assert info["world"]["id"] == "synthcity"
        sbx.pool.end(sid, reason="preempted")
        code, errs = await closed_with(c2)
        assert code == 4410 and errs and errs[-1]["code"] == 510
        # 已结束：新连接以 4410 拒绝（error 510），REST 410
        c3 = await open_sb(sbx, sid, tok, handshake=False)
        code, errs = await closed_with(c3)
        assert code == 4410 and errs[-1]["code"] == 510

    asyncio.run(go())


def test_spawning_connection_waits_for_ready(sbx: SbStack) -> None:
    """SPAWNING 时接受连接、先发 status sandbox.spawning，就绪后照常握手并 removeStatus（17 §17.7.1）。"""
    sid = sbx.pool.add(state="SPAWNING", sim=False)
    sbx.wait(lambda: sbx.hub.view(sid) is not None, 5, "view")
    tok = sbx.pool.token(sid)

    async def go() -> None:
        protos = [rtc.PROTO, f"bearer.{tok}"]
        ws = await connect(sbx.ws_url(sid), subprotocols=protos, origin=sbx.origin, open_timeout=10)
        c = rtc.Client(ws)
        _, st = await c.until(lambda k, x: k == "json" and x["op"] == "status", 5)
        assert st["id"] == "sandbox.spawning"
        sbx.pool.start_sim(sid)
        sbx.pool.set_state(sid, "READY")
        await c.until(lambda k, x: k == "json" and x["op"] == "serverInfo", 8)
        await c.until(lambda k, x: k == "json" and x["op"] == "removeStatus" and "sandbox.spawning" in x["ids"], 5)
        await ws.close()

    asyncio.run(go())


def test_activity_reported_and_pool_down_keeps_contexts(sbx: SbStack) -> None:
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)
    n0 = len(sbx.pool.activity)

    async def go() -> None:
        c = await open_sb(sbx, sid, tok)
        await asyncio.sleep(1.5)
        await c.ws.close()

    asyncio.run(go())
    end = time.monotonic() + 3
    seen_ws1 = seen_ws0 = False
    while time.monotonic() < end and not (seen_ws1 and seen_ws0):
        for a in sbx.pool.activity[n0:]:
            it = (a.get("items") or {}).get(sid)
            if it is not None:
                seen_ws1 |= it["ws_n"] == 1
                seen_ws0 |= seen_ws1 and it["ws_n"] == 0
        time.sleep(0.1)
    assert seen_ws1 and seen_ws0, sbx.pool.activity[n0:]
    # 池不可达：已附着的上下文继续服务（FR-215），对账失败不判定结束
    sbx.pool.down()
    try:
        # 按条件等待池被判为不可达（负载高的 runner 上检测晚于固定的 2.5 s），再确认上下文仍在服务
        sbx.wait(lambda: not sbx.hub.pool_up, 10, "pool down")
        assert sbx.call_in_loop(lambda: sbx.hub.get(sid) is not None and sbx.hub.get(sid).live)
        r = httpx.get(f"{sbx.base}/api/sandbox/v1/health")
        assert r.json()["pool"] == "down" and r.json()["contexts"] >= 1
    finally:
        sbx.pool.up()
    sbx.wait(lambda: sbx.hub.pool_up, 4, "pool up")


def test_restart_rebuilds_contexts_and_token_survives() -> None:
    """kill（重启）sandbox-api：经 `sys/session/list` 重建上下文，持久化 token.key 使旧 token 仍有效（FR-215；M11-AC-206 功能部分）。"""
    s = SbStack()
    try:
        sid = s.pool.add()
        s.wait_live(sid)
        tok = s.pool.token(sid)
        s.restart_api()
        t0 = time.monotonic()
        s.wait_live(sid, 5)
        assert time.monotonic() - t0 < 3.0

        async def go() -> None:
            c = await open_sb(s, sid, tok)
            info = next(m for m in c.texts if m["op"] == "serverInfo")
            assert info["run"]["id"] == sid
            await c.ws.close()

        asyncio.run(go())
    finally:
        s.close()


def test_idle_context_light_ticks_do_not_read_the_ring(sbx: SbStack) -> None:
    """无连接的上下文只做轻量 tick（不读环、不编码，FR-206、M11-AC-204 的功能部分）；接入连接后转全量 tick。"""
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    gw = sbx.hub.get(sid).gateway
    full0, light0 = sbx.call_in_loop(lambda: (gw.stats["ticks"], sbx.hub.metrics["light_ticks"]))
    time.sleep(0.6)
    full1, light1 = sbx.call_in_loop(lambda: (gw.stats["ticks"], sbx.hub.metrics["light_ticks"]))
    assert full1 == full0 and light1 > light0
    # 轻量 tick 仍推进 GCS 心跳与兴趣下推（sim-core 的链路判据与按需生成照常）
    sim = sbx.pool.sims[sid]
    n_gcs = len(sim.gcs)
    end = time.monotonic() + 5.0  # 按条件等待（负载高的 runner 上 0.5 s 内不一定有新的心跳）
    while len(sim.gcs) <= n_gcs and time.monotonic() < end:
        time.sleep(0.05)
    assert len(sim.gcs) > n_gcs

    async def go() -> None:
        c = await open_sb(sbx, sid, sbx.pool.token(sid))
        await c.drain(0.5)
        await c.ws.close()

    asyncio.run(go())
    assert sbx.call_in_loop(lambda: gw.stats["ticks"]) > full1


def test_session_state_builder_defaults() -> None:
    v = SessionView.from_msg({"sid": "sb-abcdefghij", "state": "ACTIVE", "world_id": "synthcity",
                              "expires_unix_ns": str(time.time_ns() + 60 * 10**9)})
    assert v is not None
    st = build_session_state(v, {"clock": {"rate_granted": 2, "rate_cap": 5, "limited_by": "budget"},
                                 "expiring": {"reason": "idle", "end_unix_ns": str(time.time_ns() + 30 * 10**9)},
                                 "degrade_level": 2}, None)
    assert rtc.schema_errors("rt/payloads/sandbox_session_state.schema.json", st) == []
    assert st["clock"]["limited_by"] == "cpu_budget" and st["degrade_level"] == "L2" and 25 <= st["expiring"]["in_s"] <= 30
    assert 55 <= st["expires_in_s"] <= 60
    assert SessionView.from_msg({"sid": "nope"}) is None
    bad = SessionView.from_msg({"sid": "sb-abcdefghij", "state": "READY", "binding": {"world_id": "../x"}})
    assert bad is not None and bad.binding is None


def test_unknown_sid_and_bad_token_on_ws(sbx: SbStack) -> None:
    sid = sbx.pool.add()
    sbx.wait_live(sid)

    async def go() -> None:
        for tok, want_close, want_code in ((None, 4401, 301), ("v1.x.y", 4401, 302)):
            c = await open_sb(sbx, sid, tok, handshake=False)
            code, errs = await closed_with(c)
            assert code == want_close and errs and errs[-1]["code"] == want_code
        # 他会话的 token：503 + 4410
        other = sbx.pool.add()
        c = await open_sb(sbx, sid, sbx.pool.token(other), handshake=False)
        code, errs = await closed_with(c)
        assert code == 4410 and errs[-1]["code"] == 503

    asyncio.run(go())


def _dump(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False)[:400]


def test_parking_closes_with_518_4430_and_domain_routes_answer_515(sbx: SbStack) -> None:
    """D2.1（M11-FR-222，M11-AC-214；D2-INT）：池镜像进入 PARKING/PARKED 时残余连接 error{518, reason: parked} + 4430、上下文销毁但
    不记入结束；需要 sim-core 的会话路由 409 515（detail.resume_url）；新连接同样 518 + 4430；恢复后（SPAWNING → READY）重新附着。
    活动报告带 `last_input_unix_ns`（连接最近交互的最大值）。"""
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)
    n0 = len(sbx.pool.activity)

    async def go() -> None:
        c = await open_sb(sbx, sid, tok)
        await asyncio.sleep(1.5)
        sbx.pool.set_state(sid, "PARKING")
        code, errs = await closed_with(c)
        assert code == 4430 and errs and errs[-1]["code"] == 518 and errs[-1]["detail"]["reason"] == "parked", (code, errs)
        sbx.wait(lambda: sbx.hub.get(sid) is None, 5, "context destroyed")
        sbx.pool.set_state(sid, "PARKED")
        sbx.wait(lambda: sbx.hub.view(sid) is not None and sbx.hub.view(sid).state == "PARKED", 5, "parked mirror")
        assert sbx.call_in_loop(lambda: sbx.hub.ended_reason(sid)) is None
        r = httpx.get(f"{sbx.base}/api/sandbox/v1/sessions/{sid}/vehicles", headers={"authorization": f"Bearer {tok}"}, timeout=10)
        assert r.status_code == 409 and r.json()["code"] == 515, r.text[:300]
        assert r.json()["detail"]["resume_url"].endswith(f"/sessions/{sid}/resume")
        c2 = await open_sb(sbx, sid, tok, handshake=False)
        code2, errs2 = await closed_with(c2)
        assert code2 == 4430 and errs2 and errs2[-1]["code"] == 518
        sbx.pool.set_state(sid, "READY")
        sbx.wait_live(sid)
        c3 = await open_sb(sbx, sid, tok)
        await c3.ws.close()

    asyncio.run(go())
    items = [(a.get("items") or {}).get(sid) for a in sbx.pool.activity[n0:]]
    assert any(it and it.get("ws_n") == 1 and int(it.get("last_input_unix_ns") or 0) > 0 for it in items), items[-3:]


def test_external_api_activity_excludes_the_web_client(sbx: SbStack) -> None:
    """issue #59：外部接口活动只计不带 `X-AWR-Client: awr-web/…` 的会话 REST 与非页面客户端的实时连接；`sandbox/session` 的
    `api_activity_age_s` 为距最近一次的秒数（全部 REST 仍计入池的兜底驻留）。"""
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)
    url = f"{sbx.base}/api/sandbox/v1/sessions/{sid}/vehicles"
    ext = lambda: sbx.call_in_loop(lambda: sbx.hub.last_ext_ns.get(sid, 0))  # noqa: E731
    api = lambda: sbx.call_in_loop(lambda: sbx.hub.last_api_ns.get(sid, 0))  # noqa: E731
    httpx.get(url, headers={"authorization": f"Bearer {tok}", "x-awr-client": "awr-web/0.1.0"}, timeout=10)
    assert ext() == 0 and api() > 0
    t0 = time.time_ns()
    httpx.get(url, headers={"authorization": f"Bearer {tok}"}, timeout=10)
    assert ext() >= t0

    def ext_now() -> int:
        c = sbx.hub.get(sid)
        return sbx.hub.external_activity_ns(sid, c.gateway if c is not None else None)

    async def go() -> None:
        sbx.call_in_loop(lambda: sbx.hub.last_ext_ns.pop(sid, None))
        protos = [rtc.PROTO, f"bearer.{tok}"]
        ws = await connect(sbx.ws_url(sid), subprotocols=protos, origin=sbx.origin, max_size=None, compression=None, open_timeout=10)
        web = rtc.Client(ws)
        await web.until(lambda k, x: k == "json" and x["op"] == "serverInfo", 10)
        await web.send({"op": "hello", "client": "awr-web/0.1.0", "contracts": CONTRACTS_VERSION})
        await web.drain(0.3)
        assert sbx.call_in_loop(ext_now) == 0  # 页面自己的连接不计
        t1 = time.time_ns()
        sdk = await open_sb(sbx, sid, tok)  # 客户端名 tests-sbx：外部连接，握手计一次
        await sdk.drain(0.3)
        assert sbx.call_in_loop(ext_now) >= t1 - 10**9
        await sdk.ws.close()
        await web.ws.close()

    asyncio.run(go())
    v = SessionView.from_msg({"sid": sid, "state": "ACTIVE", "world_id": "synthcity", "expires_unix_ns": str(time.time_ns() + 60 * 10**9)})
    assert v is not None
    now = time.time_ns()
    st = build_session_state(v, {}, None, now_ns=now, api_activity_ns=now - 12_340_000_000)
    assert st["api_activity_age_s"] == 12.3
    assert rtc.schema_errors("rt/payloads/sandbox_session_state.schema.json", st) == []
    assert "api_activity_age_s" not in build_session_state(v, {}, None, now_ns=now)
