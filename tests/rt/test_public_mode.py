"""公开演示访问模式（ADR-082；AWR-17 §3.3 访问模式表第 3 行、§3.4；M11-FR-120 至 FR-124）。FakeSim + 真实 Gateway：

- 匿名只签发 viewer；operator 与 admin 在未配置管理口令时一律 403 `115`，配置后错误口令 401 `304`、正确口令放行；
- Origin 只认 `AWR_ORIGINS`（回环 Origin 也被拒），Host 白名单含部署域名；
- viewer 的写请求（命令、环境写入、机群增删、性能报告）被拒，`env/query` 与 world query 放行；演示站不提供的功能族 404；
- 世界白名单：其余世界的 REST 与静态请求 404，世界列表只含白名单；
- 可信代理之后按 X-Forwarded-For 计连接数与限流（每个地址的 WS 上限、token 签发桶）；
- 纯函数：`public.client_ip`、`world_of_path`、`public_policy` 与 `ApiSettings.from_env` 的公开模式口令来源。
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
from pathlib import Path

import fakesim
import httpx
import pytest
import rtc
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from awr.api.public import client_ip, parse_networks, public_policy, world_of_path
from awr.api.settings import ApiSettings

PROTO = "awr.rt.v1"
SITE = "https://drone4d.agentnetwork.org.cn"
HOST = "drone4d.agentnetwork.org.cn"


def _public(**over) -> fakesim.GwStack:
    kw = {"access_mode": "public", "origins": [SITE], "allowed_hosts_extra": [HOST], "admin_password": "",
          "trusted_proxies": ["127.0.0.1"], "worlds_allow": ["synthcity"], "max_conns": 64, "max_conns_per_ip": 2}
    return fakesim.GwStack(n=1, **(kw | over))


@pytest.fixture(scope="module")
def pub():
    s = _public()
    yield s
    s.close()


def _h(**kw: str) -> dict[str, str]:
    return {"Origin": SITE, **kw}


def test_viewer_only_without_admin_secret(pub) -> None:
    r = httpx.post(f"{pub.base}/api/auth/token", json={"role": "viewer"}, headers=_h(), timeout=5)
    assert r.status_code == 200 and r.json()["role"] == "viewer" and r.json()["access_mode"] == "public"
    assert r.json()["seat"] == "none"
    for role in ("operator", "admin"):
        r = httpx.post(f"{pub.base}/api/auth/token", json={"role": role, "admin_secret": "guess"}, headers=_h(), timeout=5)
        assert r.status_code == 403 and r.json()["code"] == 115 and r.json()["detail"]["why"] == "PUBLIC_READONLY"


def test_admin_secret_when_configured() -> None:
    s = _public(admin_password="s3cret-for-tests-0001")
    try:
        r = httpx.post(f"{s.base}/api/auth/token", json={"role": "operator"}, headers=_h(), timeout=5)
        assert r.status_code == 401 and r.json()["code"] == 304
        r = httpx.post(f"{s.base}/api/auth/token", json={"role": "operator", "admin_secret": "wrong"}, headers=_h(),
                       timeout=5)
        assert r.status_code == 401
        r = httpx.post(f"{s.base}/api/auth/token", json={"role": "operator", "admin_secret": "s3cret-for-tests-0001"},
                       headers=_h(), timeout=5)
        assert r.status_code == 200 and r.json()["seat"] == "held"
        tok = r.json()["token"]
        # operator 持口令时不受只读策略约束（路由照常校验）；演示站不提供的功能族对 operator 仍 404
        assert httpx.get(f"{s.base}/api/runs", headers=_h(Authorization=f"Bearer {tok}"), timeout=5).status_code == 404
    finally:
        s.close()


def test_origin_and_host(pub) -> None:
    ok = httpx.get(f"{pub.base}/api/sys/info", headers={"Origin": SITE}, timeout=5)
    assert ok.status_code == 200 and ok.json()["access_mode"] == "public"
    for origin in (pub.origin, "http://localhost:5173", "http://drone4d.agentnetwork.org.cn", "https://evil.example"):
        r = httpx.get(f"{pub.base}/api/sys/info", headers={"Origin": origin}, timeout=5)
        assert r.status_code == 403 and r.json()["code"] == 303, origin
    for host, want in ((HOST, 200), ("127.0.0.1", 200), ("attacker.example", 400)):
        conn = http.client.HTTPConnection("127.0.0.1", pub.port, timeout=5)
        conn.request("GET", "/api/health/live", headers={"Host": host})
        resp = conn.getresponse()
        resp.read()
        assert resp.status == want, host
        conn.close()

    async def ws_origin() -> None:
        tok = rtc.token(pub.base, "viewer")["token"]
        with pytest.raises(InvalidStatus) as ei:
            await connect(pub.ws_url, subprotocols=[PROTO, "bearer." + tok], origin=pub.origin, compression=None)
        assert ei.value.response.status_code == 403
        ws = await connect(pub.ws_url, subprotocols=[PROTO, "bearer." + tok], origin=SITE, compression=None)
        c = rtc.Client(ws)
        info, _adv, _t = await c.handshake()
        assert info["role"] == "viewer"
        await c.send({"op": "hello", "client": "t", "contracts": "1.0.0", "role": "operator"})  # 只降不升
        await c.send({"op": "call", "id": "pub-hover-01", "service": "uav/f001/cmd/hover", "args": {}})
        assert (await c.result("pub-hover-01"))["code"] == 115
        await c.send({"op": "call", "id": "pub-env-01", "service": "env/preset", "args": {"name": "fog"}})
        assert (await c.result("pub-env-01"))["code"] == 115
        await ws.close()

    asyncio.run(ws_origin())


def test_viewer_writes_and_hidden_families(pub) -> None:
    tok = rtc.token(pub.base, "viewer")["token"]
    auth = _h(Authorization=f"Bearer {tok}")
    denied = [("POST", "/api/env/preset", {"name": "fog"}), ("POST", "/api/env/set", {"patch": {}}),
              ("POST", "/api/fleet/vehicles", {"profile_id": "p600_mid360"}), ("POST", "/api/commands", {}),
              ("POST", "/api/missions", {}), ("POST", "/api/missions/preview", {}), ("POST", "/api/sessions", {}),
              ("DELETE", "/api/fleet/vehicles/f001", None)]
    for method, path, body in denied:
        r = httpx.request(method, f"{pub.base}{path}", json=body, headers=auth, timeout=5)
        assert r.status_code == 403 and r.json()["code"] == 115, (method, path, r.text)
    for path in ("/api/runs", "/api/jobs", "/api/recon/engines", "/api/agents", "/api/agent-tasks",
                 "/api/sys/perf-reports", "/api/sys/procs", "/api/rt/inspect", "/api/openapi.json"):
        r = httpx.get(f"{pub.base}{path}", headers=auth, timeout=5)
        assert r.status_code == 404 and r.json()["code"] == 305, path
    r = httpx.post(f"{pub.base}/api/sys/perf-report", json={}, headers=auth, timeout=5)
    assert r.status_code == 404
    # 只读查询照常（env/query 经 sim-core；FakeSim 不实现时只要求不被公开策略拒绝）
    r = httpx.post(f"{pub.base}/api/env/query", json={"points": [[0, 0, 50]]}, headers=auth, timeout=5)
    assert r.status_code not in (403, 404)
    # 只读模式下 sys/info 不暴露绑定与版本细节
    assert "bind" not in httpx.get(f"{pub.base}/api/sys/info", headers=auth, timeout=5).json()


def test_world_whitelist(pub) -> None:
    for path in ("/worlds/shenzhen/world.json", "/worlds/newyork/octree.bin", "/api/worlds/shenzhen"):
        r = httpx.get(f"{pub.base}{path}", headers=_h(), timeout=5)
        assert r.status_code == 404 and r.json()["code"] == 305, path
    tok = rtc.token(pub.base, "viewer")["token"]
    r = httpx.post(f"{pub.base}/api/world/shenzhen/query", json={"op": "height", "points": [[0, 0]]},
                   headers=_h(Authorization=f"Bearer {tok}"), timeout=5)
    assert r.status_code == 404
    items = httpx.get(f"{pub.base}/api/worlds", headers=_h(Authorization=f"Bearer {tok}"), timeout=5).json()["items"]
    assert {w["id"] for w in items} <= {"synthcity"}
    # 共享环境资产不算世界（缺文件时同样 404，但不是白名单拒绝）
    r = httpx.get(f"{pub.base}/worlds/_shared/env/none.awrv", headers=_h(), timeout=5)
    assert r.status_code == 404 and "world_id" not in (r.json().get("detail") or {})


async def _closed_code(ws) -> int:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed as e:
        return e.rcvd.code if e.rcvd else -1


def test_ws_per_ip_limit_behind_proxy(pub) -> None:
    async def run() -> None:
        tok = rtc.token(pub.base, "viewer")["token"]
        protos = [PROTO, "bearer." + tok]
        hold = []
        for _ in range(2):
            ws = await connect(pub.ws_url, subprotocols=protos, origin=SITE, compression=None,
                               additional_headers={"X-Forwarded-For": "203.0.113.7"})
            await rtc.Client(ws).handshake()
            hold.append(ws)
        ws = await connect(pub.ws_url, subprotocols=protos, origin=SITE, compression=None,
                           additional_headers={"X-Forwarded-For": "203.0.113.7"})
        _, e = await rtc.Client(ws).recv()
        assert e["op"] == "error" and e["code"] == 316
        assert await _closed_code(ws) == 4429
        # 另一个客户端地址不受影响（同一对端 127.0.0.1 是可信代理）
        other = await connect(pub.ws_url, subprotocols=protos, origin=SITE, compression=None,
                              additional_headers={"X-Forwarded-For": "198.51.100.9"})
        info, _a, _t = await rtc.Client(other).handshake()
        assert info["op"] == "serverInfo"
        for w in [*hold, other]:
            await w.close()

    asyncio.run(run())


def test_token_rate_limit_per_client_ip() -> None:
    s = _public()
    try:
        codes = [httpx.post(f"{s.base}/api/auth/token", json={"role": "viewer"}, timeout=5,
                            headers=_h(**{"X-Forwarded-For": "192.0.2.44"})).status_code for _ in range(12)]
        assert codes[:10] == [200] * 10 and codes[10] == 429
        r = httpx.post(f"{s.base}/api/auth/token", json={"role": "viewer"}, timeout=5,
                       headers=_h(**{"X-Forwarded-For": "192.0.2.45"}))
        assert r.status_code == 200
    finally:
        s.close()


def test_audit_source_is_client_ip(pub) -> None:
    httpx.post(f"{pub.base}/api/auth/token", json={"role": "admin"}, timeout=5,
               headers=_h(**{"X-Forwarded-For": "198.51.100.77"}))
    p = Path(pub.settings.persist_dir or pub.settings.run_dir) / "audit.jsonl"
    pub.ctx.audit.close()
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert any(r["kind"] == "auth.denied" and r["detail"].get("source") == "198.51.100.77" for r in rows)


# ---------------------------------------------------------------- 纯函数
def test_client_ip_rules() -> None:
    nets = parse_networks(["127.0.0.1", "10.0.0.0/8", "::1"])
    assert client_ip("203.0.113.1", "1.2.3.4", None, nets) == "203.0.113.1"          # 对端不可信：忽略转发头
    assert client_ip("127.0.0.1", "198.51.100.2", None, nets) == "198.51.100.2"
    assert client_ip("127.0.0.1", "6.6.6.6, 198.51.100.2, 10.1.2.3", None, nets) == "198.51.100.2"  # 自右向左第一个不可信
    assert client_ip("127.0.0.1", "garbage, 10.0.0.5", None, nets) == "10.0.0.5"     # 不可解析条目之前的内容不可信
    assert client_ip("127.0.0.1", None, "198.51.100.3", nets) == "198.51.100.3"
    assert client_ip("127.0.0.1", None, None, nets) == "127.0.0.1"
    assert client_ip("127.0.0.1", "198.51.100.2", None, []) == "127.0.0.1"           # 未配置可信代理


def test_world_of_path_and_policy() -> None:
    assert world_of_path("/worlds/shenzhen/world.json") == "shenzhen"
    assert world_of_path("/worlds/_shared/env/x.awrv") is None
    assert world_of_path("/api/worlds/newyork") == "newyork"
    assert world_of_path("/api/world/newyork/query") == "newyork"
    assert world_of_path("/api/worlds") is None and world_of_path("/world/shenzhen") is None
    assert public_policy("GET", "/api/runs", "viewer") == (404, 305)
    assert public_policy("GET", "/api/runs/r1/segments/a", None) == (404, 305)
    assert public_policy("GET", "/api/runs", "admin") is None
    assert public_policy("POST", "/api/auth/token", None) is None
    assert public_policy("POST", "/api/env/query", "viewer") is None
    assert public_policy("POST", "/api/world/synthcity/query", "viewer") is None
    assert public_policy("POST", "/api/env/preset", "viewer") == (403, 115)
    assert public_policy("POST", "/api/env/preset", "operator") is None
    assert public_policy("GET", "/api/worlds", None) is None


def test_settings_from_env_public(tmp_path: Path) -> None:
    env = {"AWR_ACCESS_MODE": "public", "AWR_ORIGINS": SITE, "AWR_RUNS_DIR": str(tmp_path), "AWR_RUN": "r1",
           "AWR_WORLDS_ALLOW": "synthcity", "AWR_TRUSTED_PROXIES": "127.0.0.1", "AWR_WS_MAX": "64",
           "AWR_WS_MAX_PER_IP": "4"}
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "admin.token").write_text("generated-by-supervisor\n", encoding="utf-8")
    s = ApiSettings.from_env(env)
    assert s.is_public and s.admin_password == "" and s.worlds_allow == ["synthcity"]
    assert s.max_conns == 64 and s.max_conns_per_ip == 4 and s.allowed_hosts >= {HOST, "127.0.0.1"}
    assert s.origin_allowed(SITE) and not s.origin_allowed("http://127.0.0.1:18640")
    sec = tmp_path / "admin.secret"
    sec.write_text("from-deploy-file\n", encoding="utf-8")
    os.chmod(sec, 0o600)
    s = ApiSettings.from_env(env | {"AWR_ADMIN_SECRET_FILE": str(sec)})
    assert s.admin_password == "from-deploy-file"
    lo = ApiSettings.from_env({"AWR_RUNS_DIR": str(tmp_path), "AWR_RUN": "r1"})
    assert lo.access_mode == "loopback" and lo.admin_password == "generated-by-supervisor" and lo.worlds_allow is None
