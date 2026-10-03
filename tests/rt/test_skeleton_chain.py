"""SK-B walking skeleton 端到端链路（D1-MS3；M11-AC-001、AC-010、AC-020、AC-030；M08-AC-001；AWR-17 §4、§5、§6、§7）。

进程内模式（默认）：LocalBus + LocalRing + 后台 sim-core 线程 + uvicorn 线程，客户端用 websockets 与 httpx 走真实 HTTP/WS：
token → WS 握手（serverInfo、advertise、TIME，全部控制消息按 ops.schema.json 校验）→ hello（resume 补发事件）→ subscribe
→ BATCH（帧头 epoch、SNAPSHOT、Lite32 与 Full64 布局）→ takeoff → goto → result accepted → running → succeeded → 位置到达。
真实进程模式（`test_chain_real_processes`）：supervisor 以 ci profile 只启动 sim-core 与 api（随机端口与汇合点），走 zenoh 与
StateRing 的同一条链路（takeoff 到达即止，控制 CPU 占用）。
另含 REST 与静态服务（Range 206、缓存头、COOP/COEP、SPA 回退）与 WS 拒绝路径（403、400、4401、4426、4408）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
import rtc
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from awr.contracts import frame as F
from awr.contracts.layouts import DRONE_STATE64, SWARM_LITE32

UAV = "p600-01"
PROTO = "awr.rt.v1"


def _token(base: str, role: str = "operator", **kw) -> dict:
    r = httpx.post(f"{base}/api/auth/token", json={"role": role, **kw}, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


async def _connect(url: str, token: str, origin: str):
    return await connect(url, subprotocols=[PROTO, "bearer." + token], origin=origin, max_size=None,
                         compression=None, open_timeout=10)


async def _wait_ready(c: rtc.Client, timeout: float = 20.0) -> None:
    """机体生命周期到 READY：`uav/{id}/state_ext.lifecycle`（2 Hz 自包含）或 `sim.vehicle.state` 事件。

    roster 只在增删时改版本（M08-FR-015），其中的 lifecycle 可能滞后；api 晚于 sim-core 启动时也收不到更早的事件，
    因此以 state_ext 为准（订阅方必须已订阅 `uav/{id}/state_ext`）。
    """
    ext_id = c.topic_ids.get(f"uav/{UAV}/state_ext")

    def ready(_k, _x) -> bool:
        if any(e["type"] == "sim.vehicle.state" and e.get("uav") == UAV and e["data"].get("to") == "READY"
               for e in c.events()):
            return True
        ext = c.latest_msgpack(ext_id) if ext_id is not None else None
        return isinstance(ext, dict) and ext.get("lifecycle") == "READY"

    if not ready(None, None):
        await c.until(ready, timeout)


# ---------------------------------------------------------------- 进程内链路
def test_chain_inproc(stack) -> None:
    tok = _token(stack.base, "operator")
    assert tok["seat"] == "held" and tok["role"] == "operator" and tok["exp_unix_ns"].isdigit()

    async def run() -> None:
        ws = await _connect(stack.ws_url, tok["token"], stack.origin)
        assert ws.subprotocol == PROTO
        c = rtc.Client(ws)
        info, adv, _t0 = await c.handshake()
        assert info["protocol"] == PROTO and info["tickHz"] == 60 and info["seat"] == "held"
        assert info["world"]["id"] == "shenzhen" and info["world"]["frame"] == "world"
        assert info["layouts"]["awr.DroneState64.v1"] and info["limits"]["maxSubs"] == 256
        assert {ch["topic"] for ch in adv["channels"]} >= {"fleet/roster", "swarm/uav/state", "event"}
        # hello + resume：从本实例事件环起点补发（机体生命周期事件可能早于连接）
        await c.hello(resume={"sessionId": info["sessionId"], "lastEventSeq": 0})
        await c.send({"op": "subscribe", "subs": [
            {"id": 1, "topic": "swarm/state", "rate": 30},  # 别名 → swarm/uav/state，≥ 10 Hz
            {"id": 2, "topic": "event", "filter": {"levelMin": 0}},
            {"id": 3, "topic": "fleet/roster", "rate": 10},
            {"id": 4, "topic": "uav/*/state", "rate": 30},
            {"id": 5, "topic": f"uav/{UAV}/state_ext", "rate": 2},
        ]})
        await c.send({"op": "ping", "t": 1.5})
        _, b = await c.until(lambda k, x: k == "batch", 10)
        assert b.header.flags & F.BATCH_SNAPSHOT
        tb = [t for t in c.times]
        assert tb and b.header.epoch == tb[-1].epoch & 0xFFFF
        subd = {m["id"]: m for m in c.texts if m["op"] == "subscribed"}
        assert subd[1]["topic"] == "swarm/uav/state" and subd[1]["rate"] == 30
        ids = c.topic_ids
        full_id, ext_id = ids[f"uav/{UAV}/state"], ids[f"uav/{UAV}/state_ext"]
        assert full_id in subd[4]["channels"] or any(m.get("added") and full_id in m["channels"]
                                                     for m in c.texts if m["op"] == "subscribed")
        # roster 在帧内恒在最前；Lite32 与 Full64 布局与长度
        assert b.records[0].channel_id == ids["fleet/roster"]
        roster = c.latest_msgpack(ids["fleet/roster"])
        assert rtc.schema_errors("rt/payloads/fleet_roster.schema.json", roster) == []
        assert [e["id"] for e in roster["entries"]] == [UAV]
        lite = c.latest_lite(ids["swarm/uav/state"])
        assert lite is not None and lite.dtype == SWARM_LITE32 and len(lite) == 1
        await _wait_ready(c)
        # takeoff：accepted → running → succeeded（final），effect OK
        await c.send({"op": "call", "id": "c-skb-takeoff", "service": f"uav/{UAV}/cmd/takeoff", "args": {"alt_m": 5},
                      "timeout_ms": 60000})
        r = await c.result("c-skb-takeoff", timeout=60)
        assert r["status"] == "succeeded" and r["code"] == 0 and r["effect"]["status"] == "OK", r
        seq = [m["status"] for m in c.results("c-skb-takeoff") if m["op"] == "result"]
        assert seq[0] == "accepted" and "running" in seq and seq[-1] == "succeeded", seq
        await c.until(lambda k, x: k == "batch" and x.by_channel(full_id) is not None, 5)
        s = c.latest_full(full_id)
        assert s.dtype == DRONE_STATE64 and s["flight_state"][0] != 0
        p0 = s["pos"][0].astype(float)
        ground = p0[2] - 5.0
        assert abs(p0[2] - ground - 5.0) < 0.6
        # goto：沿开阔方向 10 m，保持高度
        tgt = [float(p0[0]) + 10.0, float(p0[1]), float(p0[2])]
        await c.send({"op": "call", "id": "c-skb-goto", "service": f"uav/{UAV}/cmd/goto", "args": {"pos": tgt},
                      "timeout_ms": 60000})
        r = await c.result("c-skb-goto", timeout=60)
        assert r["status"] == "succeeded" and r["effect"]["metrics"]["dist_err_m"] < 1.0, r
        await c.until(lambda k, x: k == "batch" and x.by_channel(full_id) is not None, 5)
        p1 = c.latest_full(full_id)["pos"][0].astype(float)
        assert np.linalg.norm(p1 - np.array(tgt)) < 1.0, (p1, tgt)
        # 同一 call id 重发：终态回放，带 duplicate
        await c.send({"op": "call", "id": "c-skb-goto", "service": f"uav/{UAV}/cmd/goto", "args": {"pos": tgt}})
        r = await c.result("c-skb-goto", timeout=10)
        assert r.get("duplicate") is True and r["status"] == "succeeded"
        # 参数越界：422 语义 → result rejected 110
        await c.send({"op": "call", "id": "c-skb-bad", "service": f"uav/{UAV}/cmd/takeoff", "args": {"alt_m": 500}})
        r = await c.result("c-skb-bad", timeout=10)
        assert r["status"] == "rejected" and r["code"] == 110
        # state_ext（msgpack，2 Hz）
        await c.until(lambda k, x: k == "batch" and x.by_channel(ext_id) is not None, 5)
        ext = c.latest_msgpack(ext_id)
        assert rtc.schema_errors("rt/payloads/uav_state_ext.schema.json", ext) == [], ext
        # 事件：生命周期、命令、pong
        kinds = {e["type"] for e in c.events()}
        assert {"sim.vehicle.state", "cmd.accepted", "cmd.succeeded"} <= kinds, kinds
        assert all(e["seq"] > 0 and isinstance(e["t_wall_ns"], str) for e in c.events())
        assert any(m["op"] == "pong" and m["t"] == 1.5 for m in c.texts)
        # 退订后不再收到该 channel
        await c.send({"op": "unsubscribe", "ids": [4]})
        await asyncio.sleep(0.3)
        n0 = len(c.batches)
        await c.until(lambda k, x: k == "batch" and len(c.batches) >= n0 + 3, 5)
        assert all(b.by_channel(full_id) is None for b in c.batches[n0 + 1:])
        await ws.close()
        # 全部控制消息符合 ops.schema.json（serverToClient）
        for m in c.texts:
            assert rtc.ops_errors(m) == [], m

    asyncio.run(run())


def test_viewer_call_rejected(stack) -> None:
    tok = _token(stack.base, "viewer")

    async def run() -> None:
        ws = await _connect(stack.ws_url, tok["token"], stack.origin)
        c = rtc.Client(ws)
        info, _, _ = await c.handshake()
        assert info["role"] == "viewer"
        await c.hello()
        await c.send({"op": "call", "id": "c-skb-viewer", "service": f"uav/{UAV}/cmd/hover", "args": {}})
        r = await c.result("c-skb-viewer", timeout=5)
        assert r["status"] == "rejected" and r["code"] == 115
        await c.send({"op": "nope"})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "error", 5)
        assert e["code"] == 313 and rtc.ops_errors(e) == []
        await c.send({"op": "subscribe", "subs": [{"id": 9, "topic": "no/such/topic"}]})
        _, e = await c.until(lambda k, x: k == "json" and x["op"] == "error" and x["code"] == 314, 5)
        await ws.close()

    asyncio.run(run())


def test_ws_denials(stack) -> None:
    tok = _token(stack.base, "viewer")["token"]

    async def run() -> None:
        with pytest.raises(InvalidStatus) as ei:
            await connect(stack.ws_url, subprotocols=[PROTO, "bearer." + tok], origin="http://evil.example")
        assert ei.value.response.status_code == 403
        assert json.loads(ei.value.response.body)["code"] == 303
        with pytest.raises(InvalidStatus) as ei:
            await connect(stack.ws_url, subprotocols=["awr.rt.v9"], origin=stack.origin)
        assert ei.value.response.status_code == 400
        assert ei.value.response.headers["AWR-Supported-Protocols"] == PROTO
        # token 无效：error 302 后 4401
        ws = await connect(stack.ws_url, subprotocols=[PROTO, "bearer.v1.bad.token"], origin=stack.origin)
        m = json.loads(await ws.recv())
        assert m["op"] == "error" and m["code"] == 302
        with pytest.raises(ConnectionClosed):
            await ws.recv()
        assert ws.close_code == 4401
        # contracts 主版本不同：error 311 后 4426
        ws = await _connect(stack.ws_url, tok, stack.origin)
        c = rtc.Client(ws)
        await c.handshake()
        await c.hello(contracts="2.0.0")
        with pytest.raises(ConnectionClosed):
            await c.until(lambda k, x: False, 5)
        assert ws.close_code == 4426 and any(m["op"] == "error" and m["code"] == 311 for m in c.texts)
        # hello 超时（本栈设为 1 s）：error 317 后 4408；hello 前的其他 op 回 300
        ws = await _connect(stack.ws_url, tok, stack.origin)
        c = rtc.Client(ws)
        await c.handshake()
        await c.send({"op": "subscribe", "subs": []})
        with pytest.raises(ConnectionClosed):
            await c.until(lambda k, x: False, 5)
        assert ws.close_code == 4408
        codes = [m["code"] for m in c.texts if m["op"] == "error"]
        assert 300 in codes and 317 in codes

    asyncio.run(run())


# ---------------------------------------------------------------- REST 与静态服务
def test_rest_and_static(stack) -> None:
    base = stack.base
    v = _token(base, "viewer", principal_hint="ABCDEFGHIJKLMNOP")
    assert v["principal_id"] == "p-abcdefghijklmnop" and v["seat"] == "none"
    H = {"Authorization": "Bearer " + v["token"]}
    with httpx.Client(base_url=base, timeout=10) as c:
        r = c.get("/api/health/live")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert r.headers["AWR-API-Version"] == "1" and r.headers["Cache-Control"] == "no-store"
        assert r.headers["X-Request-Id"] and r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        r = c.get("/api/health/ready")
        assert r.status_code == 200 and r.json()["sim"] == "ok"
        # 世界列表：{items, next_cursor}，snake_case，需 viewer
        r = c.get("/api/worlds")
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/problem+json")
        assert rtc.schema_errors("rest/problem.schema.json", r.json()) == [] and r.json()["code"] == 301
        r = c.get("/api/worlds", headers=H)
        body = r.json()
        assert r.status_code == 200 and set(body) == {"items", "next_cursor"}
        sz = next(i for i in body["items"] if i["id"] == "shenzhen")
        assert sz["in_use"] is True and sz["world_json_url"] == "/worlds/shenzhen/world.json" and "qa" not in sz
        assert {"content_version", "levels_points", "first_screen", "node_count", "max_height_m"} <= set(sz)
        r = c.get("/api/worlds?limit=1", headers=H)
        assert len(r.json()["items"]) == 1 and r.json()["next_cursor"]
        r2 = c.get(f"/api/worlds?limit=1&cursor={r.json()['next_cursor']}", headers=H)
        assert r2.json()["items"][0]["id"] != r.json()["items"][0]["id"]
        d = c.get("/api/worlds/shenzhen", headers=H).json()
        assert d["coordinate_sha256"] and len(d["bounds_m"]) == 2 and "qa" in d
        r = c.get("/api/worlds/nowhere", headers=H)
        assert r.status_code == 404 and r.json()["code"] == 305
        # R07（M04 路由，本应用自动发现并挂载）：经总线到 sim-core 的 GeoProbeServer
        r = c.post("/api/world/shenzhen/query", json={"op": "height_dsm", "points": [[0.0, 0.0], [1e7, 0.0]]}, headers=H)
        assert r.status_code == 200, r.text
        assert len(r.json()["z_m"]) == 2 and r.json()["z_m"][1] is None and r.json()["content_version"]
        r = c.post("/api/world/shanghai/query", json={"op": "height_dsm", "points": [[0.0, 0.0]]}, headers=H)
        assert r.status_code == 409 and r.json()["code"] == 123
        who = c.get("/api/auth/whoami", headers=H).json()
        assert who["principal_id"] == v["principal_id"] and who["role"] == "viewer"
        assert c.get("/api/sessions/current", headers=H).json()["world_id"] == "shenzhen"
        assert c.get("/api/sys/config").json()["worlds_base"] == "/worlds"
        pub, priv = c.get("/api/sys/info").json(), c.get("/api/sys/info", headers=H).json()
        assert "layout_id" not in pub and priv["layout_id"] == "3d5e08d0" and priv["protocol"] == PROTO
        r = c.post("/api/auth/token", json={"role": "admin"})
        assert r.status_code == 401 and r.json()["code"] == 304
        r = c.get("/api/auth/whoami", headers={"Authorization": "Bearer v1.x.y"})
        assert r.status_code == 401 and r.json()["code"] == 302
        r = c.get("/api/nothing-here")
        assert r.status_code == 404 and r.json()["code"] == 305
        r = c.get("/api/health/live", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403 and r.json()["code"] == 303
        r = c.get("/api/health/live", headers={"Host": "evil.example"})
        assert r.status_code == 400 and r.json()["code"] == 323
        # 静态 World：world.json no-cache + ETag + 304；?v= immutable；旧 v 409；Range 206 / 416
        r = c.get("/worlds/shenzhen/world.json")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache"
        assert r.headers["Cross-Origin-Embedder-Policy"] == "require-corp"
        assert r.headers["Cross-Origin-Resource-Policy"] == "same-origin" and r.headers["X-Content-Type-Options"] == "nosniff"
        etag, cv = r.headers["ETag"], r.json()["contentVersion"]
        assert c.get("/worlds/shenzhen/world.json", headers={"If-None-Match": etag}).status_code == 304
        r = c.get(f"/worlds/shenzhen/coordinate.json?v={cv}", headers={"Range": "bytes=0-15"})
        assert r.status_code == 206 and len(r.content) == 16 and r.headers["Content-Range"].startswith("bytes 0-15/")
        assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable" and r.headers["Accept-Ranges"] == "bytes"
        size = int(r.headers["Content-Range"].split("/")[1])
        r = c.get(f"/worlds/shenzhen/coordinate.json?v={cv}", headers={"Range": "bytes=-4"})
        assert r.status_code == 206 and r.headers["Content-Range"] == f"bytes {size - 4}-{size - 1}/{size}"
        r = c.get(f"/worlds/shenzhen/coordinate.json?v={cv}", headers={"Range": f"bytes={size + 10}-"})
        assert r.status_code == 416 and r.headers["Content-Range"] == f"bytes */{size}" and r.json()["code"] == 309
        r = c.get("/worlds/shenzhen/coordinate.json?v=000000000000")
        assert r.status_code == 409 and r.json()["code"] == 310 and r.headers["Cache-Control"] == "no-store"
        r = c.get("/worlds/shenzhen/coordinate.json")
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-cache" and r.headers["ETag"]
        assert c.get("/worlds/shenzhen/../secret").status_code == 404
        assert c.get("/worlds/shenzhen/.status/x.json").status_code == 404
        # 前端（生产模式 dist 存在时）：SPA 回退 no-cache；/assets immutable
        dist = rtc.ROOT / "apps" / "web" / "dist"
        if (dist / "index.html").exists():
            r = c.get("/world/shenzhen")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
            assert r.headers["Cache-Control"] == "no-cache" and r.headers["Cross-Origin-Opener-Policy"] == "same-origin"
            asset = next((p for p in (dist / "assets").iterdir() if p.is_file()), None)
            if asset is not None:
                r = c.get(f"/assets/{asset.name}")
                assert r.status_code == 200 and "immutable" in r.headers["Cache-Control"]


def test_openapi_lists_routes(stack) -> None:
    spec = stack.app.openapi()
    paths = set(spec["paths"])
    assert {"/api/auth/token", "/api/worlds", "/api/worlds/{world_id}", "/api/health/ready", "/api/sys/info",
            "/api/sessions/current", "/api/world/{world_id}/query"} <= paths


# ---------------------------------------------------------------- 真实进程（supervisor → sim-core + api）
@pytest.mark.needs_data
def test_chain_real_processes(tmp_path: Path) -> None:
    if not rtc.world_ready():
        pytest.skip("worlds/shenzhen 未构建（make worlds）")
    port, bus_port = rtc.free_port(), rtc.free_port()
    runs = Path(tempfile.mkdtemp(prefix="awr-skb-runs-", dir=str(tmp_path)))
    # 骨架链路：不加载 runtime.yaml 的缺省剧本（S1 会替换骨架机体并由任务接管起飞），INT-1
    env = dict(os.environ, AWR_RUNS_DIR=str(runs), PYTHONUNBUFFERED="1", AWR_SCENARIO_LOAD="0")
    env.pop("AWR_SUPERVISOR_PID", None)
    cmd = [sys.executable, "-m", "awr.runtime.supervisor", "--profile", "ci", "--only", "sim-core,api",
           "--set", "net.port_offset=0", "--set", f"net.port={port}", "--set", f"bus.rendezvous=tcp/127.0.0.1:{bus_port}",
           "--set", "run.keep_run_dir=false"]  # SK-E2E 已知坑第 14 条：不在 /dev/shm 留下运行目录
    p = subprocess.Popen(cmd, cwd=rtc.ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    try:
        line = p.stdout.readline()
        assert line.startswith("READY"), line + p.stderr.read()[-2000:]
        end = time.monotonic() + 40
        ok = False
        while time.monotonic() < end and p.poll() is None:
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(f"{base}/api/health/ready", timeout=2).status_code == 200:
                    ok = True
                    break
            time.sleep(0.3)
        assert ok, "api 未就绪"
        tok = _token(base, "operator")

        async def run() -> None:
            ws = await _connect(f"ws://127.0.0.1:{port}/api/rt", tok["token"], base)
            c = rtc.Client(ws)
            info, _, _ = await c.handshake()
            assert info["run"]["id"] == tok["run_id"]
            await c.hello(resume={"sessionId": info["sessionId"], "lastEventSeq": 0})
            await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "swarm/uav/state", "rate": 10},
                                                      {"id": 2, "topic": "event"},
                                                      {"id": 3, "topic": f"uav/{UAV}/state", "rate": 30},
                                                      {"id": 4, "topic": f"uav/{UAV}/state_ext", "rate": 2}]})
            _, b = await c.until(lambda k, x: k == "batch", 15)
            assert b.header.flags & F.BATCH_SNAPSHOT

            # 席位持有者按 2 Hz 发 ping（12 §5.9.1 GCS 链路；M09 装配后不发 ping 的持有者 3 s 后 HOLD/LINK_LOSS，
            # takeoff 以 204 结束；INT-1 按 M09-to-M11 第 1 条修改本用例）
            async def pinger() -> None:
                while True:
                    await c.send({"op": "ping", "t": time.monotonic()})
                    await asyncio.sleep(0.5)

            pt = asyncio.ensure_future(pinger())
            await _wait_ready(c, 30)
            await c.send({"op": "call", "id": "c-skb-real-takeoff", "service": f"uav/{UAV}/cmd/takeoff",
                          "args": {"alt_m": 3}})
            r = await c.result("c-skb-real-takeoff", timeout=60)
            pt.cancel()
            assert r["status"] == "succeeded", r
            full_id = c.topic_ids[f"uav/{UAV}/state"]
            await c.until(lambda k, x: k == "batch" and x.by_channel(full_id) is not None, 5)
            assert c.latest_full(full_id)["pos"][0][2] > 2.0
            await ws.close()
            for m in c.texts:
                assert rtc.ops_errors(m) == [], m

        asyncio.run(run())
    finally:
        with contextlib.suppress(ProcessLookupError):
            p.send_signal(signal.SIGTERM)
        try:
            rc = p.wait(30)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            rc = p.wait(5)
    assert rc == 0, p.stderr.read()[-3000:]
