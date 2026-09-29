"""REST 框架与契约、可观测性（M11-AC-034、AC-035；M11-FR-068、FR-069、FR-080、FR-081、FR-085、FR-086；AWR-17 §4、§6.5）：
- `rest/*.py` 按文件名排序自动发现；全部错误为 problem+json 且 code 在 `reasons.json`；响应带 `AWR-API-Version` 与
  `X-Request-Id`（回显客户端值）；`/api/**` 缺省 `Cache-Control: no-store`；
- R54 `POST /api/sys/restart`（admin；未受监管 503 `213`）、R53 `sys/procs`、R59 `sys/config`、R60 `sys/perf`（窗口越界 422
  `110`）、R57/R58 `rt/topics`、`rt/inspect`（ext）、R03 `auth/confirm`（ext）、R47–R49 perf-report（ext）、R55 audit（ext）；
- `perf/server` 1 Hz，含 FR-085 全部字段并符合 `perf_server.schema.json`；`/api/sys/perf?window_s=60` 返回 p50、p95、p99、max。
"""

from __future__ import annotations

import asyncio
import json
import time

import fakesim
import httpx
import pytest
import rtc

from awr.api.main import discover_routers
from awr.contracts.reasons import info as reason_info


@pytest.fixture(scope="module")
def st():
    s = fakesim.GwStack(n=2, supervisor=True)
    yield s
    s.close()


def _admin(st) -> dict:
    return rtc.token(st.base, "admin", rtc.hint_of("restadmin"), admin_secret=st.settings.admin_password)


def _problem_ok(r: httpx.Response) -> None:
    assert r.headers["content-type"].startswith("application/problem+json"), r.headers
    b = r.json()
    assert reason_info(b["code"]) is not None and b["status"] == r.status_code and b["request_id"]
    assert rtc.schema_errors("rest/problem.schema.json", b) == []


def test_discovery_headers_and_problems(st) -> None:
    names = [n for n, _ in discover_routers()]
    assert names == sorted(names) and {"auth", "commands", "sessions", "sys", "worlds"} <= set(names)
    r = httpx.get(f"{st.base}/api/health/live", headers={"X-Request-Id": "req-abc"}, timeout=5)
    assert r.status_code == 200 and r.headers["x-request-id"] == "req-abc"
    assert r.headers["awr-api-version"] == "1" and r.headers["cache-control"] == "no-store"
    assert r.headers["cross-origin-embedder-policy"] == "require-corp"
    for resp in (httpx.get(f"{st.base}/api/nope", timeout=5), httpx.get(f"{st.base}/api/worlds", timeout=5),
                 httpx.get(f"{st.base}/api/auth/whoami", headers={"Authorization": "Bearer v1.x.y"}, timeout=5),
                 httpx.delete(f"{st.base}/api/health/live", timeout=5),
                 httpx.post(f"{st.base}/api/auth/token", json={"role": "god"}, timeout=5)):
        assert resp.status_code in (400, 401, 404, 405)
        _problem_ok(resp)
    v = rtc.token(st.base, "viewer")
    h = {"Authorization": f"Bearer {v['token']}"}
    r = httpx.get(f"{st.base}/api/sys/metrics", headers=h, timeout=5)
    assert r.status_code == 404  # R78 为 V0.5
    r = httpx.post(f"{st.base}/api/sys/restart", json={"name": "recorder"}, headers=h, timeout=5)
    assert r.status_code == 403 and r.json()["code"] == 115


def test_sys_endpoints(st) -> None:
    a = _admin(st)
    h = {"Authorization": f"Bearer {a['token']}"}
    cfg = httpx.get(f"{st.base}/api/sys/config", timeout=5).json()
    assert cfg == {"worlds_base": "/worlds", "static_split": False, "access_mode": "loopback", "tick_hz": 60,
                   "rate_classes": [1, 2, 5, 10, 15, 20, 30, 60]}
    procs = httpx.get(f"{st.base}/api/sys/procs", headers=h, timeout=5).json()
    assert rtc.schema_errors("rt/payloads/sys_procs.schema.json", procs) == []
    r = httpx.post(f"{st.base}/api/sys/restart", json={"name": "recorder", "reset_breaker": True}, headers=h, timeout=5)
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    assert st.sup.restarts[-1]["name"] == "recorder" and st.sup.restarts[-1]["reset_breaker"] is True
    r = httpx.post(f"{st.base}/api/sys/restart", json={"name": "nope"}, headers=h, timeout=5)
    assert r.status_code == 404 and r.json()["code"] == 110
    time.sleep(2.2)
    r = httpx.get(f"{st.base}/api/sys/perf?window_s=60", headers=h, timeout=5)
    body = r.json()
    f = body["fields"]
    for k in ("api.cpu_pct", "api.tick_age_p99_ms", "api.n_clients", "sim.rtf", "sim.n_active"):
        assert set(f[k]) == {"p50", "p95", "p99", "max", "count"} and f[k]["count"] >= 1, k
    assert body["t_from_unix_ns"].isdigit() and body["t_to_unix_ns"].isdigit()
    r = httpx.get(f"{st.base}/api/sys/perf?window_s=3", headers=h, timeout=5)
    assert r.status_code == 422 and r.json()["code"] == 110
    topics = httpx.get(f"{st.base}/api/rt/topics", headers=h, timeout=5).json()
    assert any(c["topic"] == "swarm/uav/state" for c in topics["channels"]) and topics["tick_hz"] == 60
    ins = httpx.get(f"{st.base}/api/rt/inspect", headers=h, timeout=5).json()
    assert {"tick", "clients", "channels", "event_ring"} <= set(ins)
    aud = httpx.get(f"{st.base}/api/sys/audit?kind=auth.", headers=h, timeout=5)
    assert aud.status_code == 200 and all(x["kind"].startswith("auth.") for x in aud.json()["items"])


def test_perf_server_topic(st) -> None:
    tok = rtc.token(st.base, "viewer")

    async def run() -> None:
        c = await rtc.open_client(st, tok["token"])
        await c.send({"op": "subscribe", "subs": [{"id": 1, "topic": "perf/server", "rate": 1, "mode": "latest"},
                                                  {"id": 2, "topic": "swarm/state", "rate": 10, "mode": "latest"}]})
        pid = c.topic_ids["perf/server"]
        t = []
        await c.until(lambda k, x: k == "batch" and x.by_channel(pid) is not None, 3)  # 订阅快照（立即重发）
        while len(t) < 3:
            await c.until(lambda k, x: k == "batch" and x.by_channel(pid) is not None, 3)
            t.append(time.monotonic())
        await c.drain(0.1)
        m = c.latest_msgpack(pid)
        assert rtc.schema_errors("rt/payloads/perf_server.schema.json", m) == []
        assert {"cpu_pct", "loop_lag_p99_ms", "tick_age_p50_ms", "tick_age_p99_ms", "tick_overruns", "encodes_per_s",
                "bytes_per_s", "n_clients"} <= set(m["api"])
        row = next(r for r in m["clients"] if r["conn_id"] == next(x for x in c.texts if x["op"] == "serverInfo")["connId"])
        assert {"fps", "window", "credit_skips", "bucket_defers", "slot_overwrites", "ctrl_queue_hwm", "srtt_ms",
                "kbps"} <= set(row)
        assert {"rtf", "step_p50_us", "step_p99_us", "step_max_us", "catchup_saturated", "stage_ms_per_s", "n_active",
                "kernel", "cpu_pct"} <= set(m["sim"])
        assert 0.7 <= (t[-1] - t[0]) / (len(t) - 1) <= 1.4  # 1 Hz
        await c.ws.close()

    asyncio.run(run())


def test_ext_confirm_and_perf_report(st) -> None:
    op = rtc.token(st.base, "operator", rtc.hint_of("restadmin"), admin_secret=st.settings.admin_password)
    h = {"Authorization": f"Bearer {op['token']}"}
    r = httpx.post(f"{st.base}/api/auth/confirm", json={"action": "kill", "target": "f001"}, headers=h, timeout=5)
    assert r.status_code == 200 and r.json()["confirm_token"].startswith("c1.")
    ct = r.json()["confirm_token"]

    async def run() -> None:
        c = await rtc.open_client(st, op["token"])
        await c.send({"op": "call", "id": "kill-noconf-1", "service": "uav/f001/cmd/kill", "args": {"confirm_token": "x"}})
        assert (await c.result("kill-noconf-1"))["code"] == 112
        await c.send({"op": "call", "id": "kill-conf-01", "service": "uav/f001/cmd/kill", "args": {"confirm_token": ct}})
        r1 = await c.result("kill-conf-01")
        assert r1["code"] != 112
        await c.send({"op": "call", "id": "kill-conf-02", "service": "uav/f001/cmd/kill", "args": {"confirm_token": ct}})
        assert (await c.result("kill-conf-02"))["code"] == 112  # 单次使用
        await c.send({"op": "call", "id": "conf-issue-1", "service": "confirm/issue",
                      "args": {"action": "escalate", "target": "f002"}})
        r2 = await c.result("conf-issue-1")
        assert r2["status"] == "succeeded" and r2["data"]["confirm_token"].startswith("c1.")
        await c.ws.close()

    asyncio.run(run())
    rep = json.loads((fakesim.rtc.ROOT / "packages/contracts/perf/perf-report.schema.json").read_text())
    assert rep["$id"]
    r = httpx.post(f"{st.base}/api/sys/perf-report", json={"schema": "awr.perf.report.v1"}, headers=h, timeout=10)
    assert r.status_code == 400 and r.json()["code"] == 300 and "pointer" in r.json()["detail"]


def test_ext_seat_takeover() -> None:
    """席位接管（ext）：admin（席位被占时仍可签发、不占席）→ R03 确认令牌 → `seat/takeover` → 旧持有者连接收到 error 116
    并以 4403 关闭；新持有者可写。"""
    s = fakesim.GwStack(n=1)
    try:
        op = rtc.token(s.base, "operator", rtc.hint_of("oldholder"))
        adm = rtc.token(s.base, "admin", rtc.hint_of("takeoveradmin"), admin_secret=s.settings.admin_password)
        assert adm["seat"] == "none"
        h = {"Authorization": f"Bearer {adm['token']}"}
        ct = httpx.post(f"{s.base}/api/auth/confirm", json={"action": "seat_takeover", "target": "seat"}, headers=h,
                        timeout=5).json()["confirm_token"]

        async def run() -> None:
            old = await rtc.open_client(s, op["token"])
            new = await rtc.open_client(s, adm["token"])
            await new.send({"op": "call", "id": "take-over-001", "service": "seat/takeover",
                            "args": {"confirm_token": ct}})
            r = await new.result("take-over-001")
            assert r["status"] == "succeeded", r
            from websockets.exceptions import ConnectionClosed

            code = None
            try:
                await old.until(lambda k, x: False, 3)
            except ConnectionClosed as e:
                code = e.rcvd.code
            assert code == 4403 and any(m["op"] == "error" and m["code"] == 116 for m in old.texts)
            await new.send({"op": "call", "id": "take-hover-01", "service": "uav/f001/cmd/hover", "args": {}})
            assert (await new.result("take-hover-01"))["status"] == "succeeded"
            await new.ws.close()

        asyncio.run(run())
    finally:
        s.close()
