"""sandbox-api 的 REST 面：请求级上下文、沙盒鉴权与隔离、挂载与 OpenAPI、前缀限流（M11-AC-202、AC-203、AC-205、AC-207 的
功能部分；M11-FR-202、FR-203、FR-207 至 FR-210；17 §17.3）。领域路由尚未交付时以测试路由代替（同一挂载方式与依赖）。"""

from __future__ import annotations

import sys
import time
from typing import Annotated, Any

import httpx
import pytest
import rtc
from fakepool import SbStack
from fastapi import APIRouter, Depends, Request
from websockets.asyncio.client import connect

from awr.api.deps import SandboxOperator, SandboxViewer, ctx_for, sandbox_session
from awr.api.public import agg_prefix_of, prefix_of
from awr.api.ratelimit import sandbox_category_for_service, sandbox_rate_checks
from awr.runtime.sbauth import make_sandbox_token

PFX = "/api/sandbox/v1"

probe = APIRouter()


@probe.get("/probe")
async def probe_get(request: Request, p: SandboxViewer, ctx: Annotated[Any, Depends(ctx_for)]) -> dict:
    return {"sid": request.state.sandbox_sid, "run": ctx.settings.run_id, "world": ctx.settings.world_id,
            "principal": p.id, "roster": sorted(ctx.gateway.roster)}


@probe.post("/probe")
async def probe_post(request: Request, p: SandboxOperator) -> dict:
    return {"ok": True, "principal": p.id}


@probe.get("/session")
async def probe_session(request: Request, _p: SandboxViewer) -> dict:
    v = sandbox_session(request)
    return {"state": v.state, "world": v.world_id}


@probe.post("/vehicles")
async def probe_entity(request: Request, _p: SandboxOperator) -> dict:
    return {"ok": True}


@pytest.fixture(scope="module")
def sbx():
    # 会话 REST 桶的突发设得足够大：鉴权矩阵对同一会话连发约 10 个请求，快速 runner 上 burst 8 会在补充前耗尽而返回 429；
    # 限流行为只由 test_rest_rate_limits_by_sid_and_prefix 用实体类别（2/s）与公开前缀（突发 3）断言
    s = SbStack(extra_routers=[probe], sb_over={"rest": {"rps": 50, "burst": 64, "entity_rps": 2},
                                               "public_rps": 1, "public_burst": 3,
                                               "ws": {"per_session": 2, "per_prefix": 3}})
    yield s
    s.close()


def H(tok: str | None) -> dict:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def test_ctx_for_and_auth_matrix(sbx: SbStack) -> None:
    a = sbx.pool.add(principal="p-a")
    b = sbx.pool.add(principal="p-b")
    sbx.wait_live(a)
    sbx.wait_live(b)
    ta, tb = sbx.pool.token(a), sbx.pool.token(b)
    c = httpx.Client(base_url=sbx.base, timeout=10)
    sbx.wait(lambda: len(sbx.hub.get(a).gateway.roster) == 2, 5, "roster")
    r = c.get(f"{PFX}/sessions/{a}/probe", headers=H(ta))
    assert r.status_code == 200, r.text
    assert r.json() == {"sid": a, "run": a, "world": rtc.WORLD, "principal": "p-a", "roster": ["f001", "f002"]}
    assert c.post(f"{PFX}/sessions/{a}/probe", headers=H(ta)).status_code == 200
    # 他会话：403 503（读与写）
    for m in ("get", "post"):
        r = getattr(c, m)(f"{PFX}/sessions/{b}/probe", headers=H(ta))
        assert r.status_code == 403 and r.json()["code"] == 503, r.text
    # 缺失 401 301；篡改、过期、无 scope 401 302
    assert c.get(f"{PFX}/sessions/{a}/probe").json()["code"] == 301
    tampered = ta[:-3] + ("AAA" if not ta.endswith("AAA") else "BBB")
    expired = make_sandbox_token(sbx.pool.k_sbauth, principal_id="p-a", sid=a, mode="loopback",
                                 exp_unix_s=int(time.time()) - 5)[0]
    from awr.runtime.principal import encode_token, make_token_payload

    noscope = encode_token(make_token_payload("p-a", "operator", a, "loopback", now_s=int(time.time())), sbx.pool.k_sbauth)
    for t in (tampered, expired, noscope, "garbage"):
        r = c.get(f"{PFX}/sessions/{a}/probe", headers=H(t))
        assert r.status_code == 401 and r.json()["code"] == 302, (t[:20], r.text)
    # 观摩 token（viewer，P1）：可读不可写
    tv = sbx.pool.token(a, role="viewer")
    assert c.get(f"{PFX}/sessions/{a}/probe", headers=H(tv)).status_code == 200
    r = c.post(f"{PFX}/sessions/{a}/probe", headers=H(tv))
    assert r.status_code == 403 and r.json()["code"] == 115
    # 会话状态：SPAWNING 503 211 + Retry-After（需要 sim-core 的路由）；镜像路由照常
    sp = sbx.pool.add(state="SPAWNING", sim=False)
    sbx.wait(lambda: sbx.hub.view(sp) is not None, 5, "view")
    tsp = sbx.pool.token(sp)
    r = c.get(f"{PFX}/sessions/{sp}/probe", headers=H(tsp))
    assert r.status_code == 503 and r.json()["code"] == 211 and r.headers["retry-after"] == "1"
    assert c.get(f"{PFX}/sessions/{sp}/session", headers=H(tsp)).json() == {"state": "SPAWNING", "world": rtc.WORLD}
    # 未知 sid 404 305；已结束 410 506（抢占 510）
    unknown = "sb-aaaaaaaaaa"
    tu = make_sandbox_token(sbx.pool.k_sbauth, principal_id="p-x", sid=unknown, mode="loopback",
                            exp_unix_s=int(time.time()) + 60)[0]
    r = c.get(f"{PFX}/sessions/{unknown}/probe", headers=H(tu))
    assert r.status_code == 404 and r.json()["code"] == 305
    sbx.pool.end(b, reason="idle")
    sbx.wait(lambda: sbx.hub.ended_reason(b) == "idle", 5, "ended")
    r = c.get(f"{PFX}/sessions/{b}/probe", headers=H(tb))
    assert r.status_code == 410 and r.json()["code"] == 506
    sbx.pool.end(sp, reason="preempted")
    sbx.wait(lambda: sbx.hub.ended_reason(sp) == "preempted", 5, "ended")
    assert c.get(f"{PFX}/sessions/{sp}/session", headers=H(tsp)).json()["code"] == 510
    # 非法 sid 形状：路径校验 400/422 之外不泄露会话信息
    assert c.get(f"{PFX}/sessions/SB-NOPE/probe", headers=H(ta)).status_code in (400, 404, 422)


def test_sandbox_token_rejected_by_show_api() -> None:
    """沙盒 token 不能用于 show-api（D2-AC-03、29）：K_auth 不同即 302；show-api 也显式拒绝带 scope 的 token。"""
    import fakesim

    from awr.api.security import TokenService
    from awr.runtime.principal import TokenInvalid, encode_token

    s = fakesim.GwStack(n=1)
    try:
        tok = make_sandbox_token(b"k" * 32, principal_id="p-a", sid="sb-abcdefghij", mode="loopback",
                                 exp_unix_s=int(time.time()) + 60)[0]
        r = httpx.get(f"{s.base}/api/auth/whoami", headers=H(tok), timeout=10)
        assert r.status_code == 401 and r.json()["code"] == 302
        ts = TokenService(s.settings)
        payload = {"v": 1, "sub": "p-a", "role": "operator", "run": s.settings.run_id, "mode": "loopback",
                   "iat": int(time.time()), "exp": int(time.time()) + 60, "jti": "0" * 16, "scope": "sandbox",
                   "sid": "sb-abcdefghij"}
        with pytest.raises(TokenInvalid):
            ts.verify(encode_token(payload, ts.k_auth))
    finally:
        s.close()


def test_mount_openapi_and_health(sbx: SbStack, monkeypatch: pytest.MonkeyPatch) -> None:
    from awr.api import sandbox_main
    from awr.api.rest import sys as rest_sys

    c = httpx.Client(base_url=sbx.base, timeout=10)
    doc = c.get(f"{PFX}/openapi.json").json()
    assert doc["openapi"].startswith("3.1") and doc["info"]["title"] == "ANet Drone4D Sandbox API"
    assert f"{PFX}/sessions/{{sid}}/rt" in doc["paths"] and f"{PFX}/openapi.json" in doc["paths"]
    assert "bearerAuth" in doc["components"]["securitySchemes"]
    rt_topics = {t["topic"] for t in doc["x-awr-realtime"]["topics"]}
    assert {"swarm/target/state", "sandbox/session", "fleet/roster"} <= rt_topics
    assert not any(s["service"].startswith(("sys/", "rec/")) for s in doc["x-awr-realtime"]["services"])
    h = c.get(f"{PFX}/health").json()
    assert h["ok"] and h["pool"] == "up"
    # D1 路由不在 sandbox-api：/api/runs、/api/auth/token 404
    assert c.get("/api/runs").status_code == 404 and c.post("/api/auth/token", json={}).status_code == 404
    # 拒绝清单：sys.py 即使导出 sandbox_router 也不挂载（FR-203）
    r = APIRouter()

    @r.get("/leak")
    async def leak() -> dict:
        return {}

    monkeypatch.setattr(rest_sys, "sandbox_router", r, raising=False)
    app = sandbox_main.create_sandbox_app(sbx.settings, sb=sbx.sb, state_dir=sbx.state_dir, bus_kind="local")
    assert not any(str(getattr(rt, "path", "")).endswith("/leak") for rt in app.routes)
    assert all(p.startswith(PFX) for p in sandbox_main.live_openapi()["paths"])


def test_prefix_keys_and_categories() -> None:
    assert prefix_of("203.0.113.9") == "203.0.113.9/32" and agg_prefix_of("203.0.113.9") == "203.0.113.0/24"
    assert prefix_of("::ffff:203.0.113.9") == "203.0.113.9/32"
    assert prefix_of("2001:db8:1:2:aaaa::1") == prefix_of("2001:db8:1:2:bbbb::7") == "2001:db8:1:2::/64"
    assert agg_prefix_of("2001:db8:1:2::1") == agg_prefix_of("2001:db8:1:ff::1") == "2001:db8:1::/48"
    sid = "sb-abcdefghij"
    assert sandbox_rate_checks("GET", f"{PFX}/worlds", "1.2.3.4/32") == [("pfx:1.2.3.4/32", "sbx_public")]
    assert sandbox_rate_checks("GET", f"{PFX}/health", "x") == []
    assert sandbox_rate_checks("POST", f"{PFX}/sessions/{sid}/clock", "x") == [(f"sid:{sid}", "sbx_rest"),
                                                                              (f"sid:{sid}", "sbx_clock")]
    assert (f"sid:{sid}", "sbx_entity") in sandbox_rate_checks("DELETE", f"{PFX}/sessions/{sid}/targets/tg-1", "x")
    assert (f"sid:{sid}", "sbx_task") in sandbox_rate_checks("POST", f"{PFX}/sessions/{sid}/tasks", "x")
    assert sandbox_category_for_service("sim/speed") == ["sbx_clock", "sbx_call"]
    assert sandbox_category_for_service("target/add") == ["sbx_entity", "sbx_call"]
    assert sandbox_category_for_service("goto") == ["sbx_call"]


def test_rest_rate_limits_by_sid_and_prefix(sbx: SbStack) -> None:
    sid = sbx.pool.add()
    sbx.wait_live(sid)
    tok = sbx.pool.token(sid)
    c = httpx.Client(base_url=sbx.base, timeout=10)
    # 实体类别 2/s：第 3 个立即请求 429 111 + Retry-After
    codes = [c.post(f"{PFX}/sessions/{sid}/vehicles", headers=H(tok)).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429
    # 公开路由按前缀（突发 3）：伪造 X-Forwarded-For 不改变键（未配置可信代理）
    seen = []
    for i in range(5):
        r = c.get(f"{PFX}/health_nope", headers={"X-Forwarded-For": f"198.51.100.{i}"})
        seen.append(r.status_code)
    assert seen.count(429) >= 2, seen
    r = c.get(f"{PFX}/nope2")
    assert r.status_code == 429 and r.json()["code"] == 111 and int(r.headers["retry-after"]) >= 1


def test_ws_connection_limits(sbx: SbStack) -> None:
    """每会话上限（本栈 2）与每前缀上限（本栈 3）以 4429 + error 316 拒绝（FR-210）。"""
    import asyncio

    a = sbx.pool.add()
    b = sbx.pool.add()
    sbx.wait_live(a)
    sbx.wait_live(b)

    async def one(sid: str) -> Any:
        ws = await connect(sbx.ws_url(sid), subprotocols=[rtc.PROTO, f"bearer.{sbx.pool.token(sid)}"],
                           origin=sbx.origin, open_timeout=10)
        c = rtc.Client(ws)
        _, first = await c.until(lambda k, x: k == "json" and x["op"] in ("serverInfo", "error"), 5)
        return c, first

    async def go() -> None:
        c1, f1 = await one(a)
        c2, f2 = await one(a)
        _c3, f3 = await one(a)
        assert f1["op"] == f2["op"] == "serverInfo" and f3["op"] == "error" and f3["code"] == 316
        c4, f4 = await one(b)
        assert f4["op"] == "serverInfo"
        _c5, f5 = await one(b)  # 本前缀第 4 条
        assert f5["op"] == "error" and f5["code"] == 316
        for c in (c1, c2, c4):
            await c.ws.close()

    asyncio.run(go())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_rest_modules_do_not_read_app_state_awr() -> None:
    """M11-AC-201 的静态检查：领域 `rest/*.py` 经请求级上下文 `ctx_for` 取运行上下文，不直接读 `request.app.state.awr`
    （否则 sandbox-api 中会串到应用级上下文）。"""
    import ast
    from pathlib import Path

    rest = Path(__file__).resolve().parents[2] / "python" / "awr" / "api" / "rest"
    def hit(n: ast.AST) -> bool:
        return (isinstance(n, ast.Attribute) and n.attr == "awr" and isinstance(n.value, ast.Attribute)
                and n.value.attr == "state" and isinstance(n.value.value, ast.Attribute) and n.value.value.attr == "app")

    bad = [f"{p.name}:{n.lineno}" for p in sorted(rest.glob("*.py"))
           for n in ast.walk(ast.parse(p.read_text(encoding="utf-8"))) if hit(n)]
    assert bad == [], bad
