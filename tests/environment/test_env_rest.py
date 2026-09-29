"""R27–R31 环境 REST（M07 §7.3；17 §4.3.8；M07-FR-008）：最新帧、展开形式、presets ETag/304、query 与 set/preset 转发。

以最小 FastAPI 应用挂载 `awr.api.rest.env.router`（真实 Gateway 链路由 tests/rt 覆盖）：鉴权中间件替身写入 principal，
Gateway 替身提供 EnvCache 最新字节与 RpcRouter.handle_call，总线替身服务 `ctl/sim-core/query`。
"""

from __future__ import annotations

import hashlib

import msgpack
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from awr.api.problem import ApiProblem, problem
from awr.api.rest import env as R
from awr.api.security import ApiPrincipal
from awr.contracts.presets import PRESETS_JSON, PRESETS_SHA256
from awr.environment.field import EnvironmentServiceImpl


class _Ch:
    payload: bytes | None = None


class _Env:
    def __init__(self) -> None:
        self.ch = _Ch()


class _Rpc:
    def __init__(self, svc: EnvironmentServiceImpl) -> None:
        self.svc = svc
        self.calls: list[dict] = []

    def handle_call(self, caller, m: dict) -> None:
        self.calls.append(m)
        if m["service"] == "env/query":
            rep = self.svc.handle_query({"args": m["args"]})
            st = "succeeded" if rep["code"] == 0 else "rejected"
            caller.send_ctrl({"op": "result", "id": m["id"], "status": st, "code": rep["code"], "data": rep, "final": True})
        elif m["args"].get("name") == "tornado":
            caller.send_ctrl({"op": "result", "id": m["id"], "status": "rejected", "code": 440, "detail": {"name": "tornado"}})
        else:
            caller.send_ctrl({"op": "result", "id": m["id"], "status": "accepted", "code": 0})


class _Gw:
    def __init__(self, svc: EnvironmentServiceImpl) -> None:
        self.env = _Env()
        self.rpc = _Rpc(svc)


class _Ctx:
    def __init__(self, svc: EnvironmentServiceImpl) -> None:
        self.gateway = _Gw(svc)


class _Bus:
    def __init__(self, svc: EnvironmentServiceImpl) -> None:
        self.svc = svc

    async def call(self, key, msg, **kw):
        if msg["op"] == "env/state":
            return {"v": 1, "code": 0, "frame": self.svc.state_at(msg["args"]["t_ns"])}
        return {"v": 1, "code": 213}


@pytest.fixture()
def client():
    svc = EnvironmentServiceImpl(world_id="t", world_seed=1, initial_preset="rain", load_assets=False)
    svc.on_env_tick_all(0)
    svc.on_env_tick_all(50)
    app = FastAPI()
    app.state.awr = _Ctx(svc)
    app.state.bus = _Bus(svc)
    app.state.awr.gateway.env.ch.payload = svc.heartbeat_bytes()

    @app.middleware("http")
    async def auth(request: Request, call_next):
        role = request.headers.get("x-role", "viewer")
        request.state.principal = ApiPrincipal("p-test", role, "j", 0, "loopback")
        return await call_next(request)

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail, headers=exc.headers)

    app.include_router(R.router)
    c = TestClient(app)
    c.svc = svc  # type: ignore[attr-defined]
    return c


def test_state_latest_and_expand(client):
    r = client.get("/api/env/state")
    assert r.status_code == 200
    f = r.json()
    assert f["schema"] == "awr.env.keyframe.v1" and len(f["from"]) == 21 and f["config"]["presets_sha256"] == PRESETS_SHA256
    x = client.get("/api/env/state?expand=1").json()
    assert x["to"]["precip"]["rain_mmh"] == 6 and x["to"]["wind"]["dir_from_deg"] == 270


def test_state_at_t(client):
    t = client.svc.t_grid_ns + 3_000_000_000
    f = client.get(f"/api/env/state?t_ns={t}").json()
    assert f["t_ns"] == t and f["anchors"]["t_ns"] == t and f["anchors"]["s_m"] > 0


def test_presets_etag(client):
    r = client.get("/api/env/presets")
    assert r.status_code == 200 and r.content == PRESETS_JSON
    assert r.headers["etag"] == f'"{PRESETS_SHA256}"' == f'"{hashlib.sha256(r.content).hexdigest()}"'
    assert client.get("/api/env/presets", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_query_and_limits(client):
    r = client.post("/api/env/query", json={"points": [[0, 0, 50], [10, 0, 80]], "fields": ["WIND", "THERMO"]})
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["n"] == 2 and len(d["wind_mps"]) == 2 and "rho_kgm3" in d
    assert client.post("/api/env/query", json={"points": [[0, 0, 1]] * 257}).status_code == 422
    assert client.post("/api/env/query", json={"points": [[0, 0, 1]], "bogus": 1}).status_code == 422


def test_set_and_preset_mirror(client):
    assert client.post("/api/env/preset", json={"name": "fog"}, headers={"x-role": "viewer"}).status_code == 403
    r = client.post("/api/env/preset", json={"name": "fog", "id": "c-1"}, headers={"x-role": "operator"})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert client.app.state.awr.gateway.rpc.calls[-1] == {"op": "call", "id": "c-1", "service": "env/preset", "args": {"name": "fog"}}
    r = client.post("/api/env/preset", json={"name": "tornado"}, headers={"x-role": "operator"})
    assert r.status_code == 422 and r.json()["code"] == 440
    r = client.post("/api/env/set", json={"patch": {"wind": {"speed_ref_mps": 8}}, "duration_s": 3}, headers={"x-role": "operator"})
    assert r.status_code == 200
    assert client.app.state.awr.gateway.rpc.calls[-1]["args"] == {"patch": {"wind": {"speed_ref_mps": 8}}, "duration_s": 3.0}


def test_state_not_ready():
    app = FastAPI()
    svc = EnvironmentServiceImpl(world_id="t", load_assets=False)
    app.state.awr = _Ctx(svc)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.principal = ApiPrincipal("p", "viewer", "j", 0, "loopback")
        return await call_next(request)

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail)

    app.include_router(R.router)
    assert TestClient(app).get("/api/env/state").status_code == 503
    _ = msgpack
