"""命令与 REST（M18-AC-017；M18-FR-019、FR-020；17 §17.5 SB-40 至 SB-46、SB-65；契约 target/target.schema.json）。

`python/awr/api/rest/targets.py` 的 `sandbox_router` 挂到 `/api/sandbox/v1/sessions/{sid}`（sandbox-api 的挂载方式），真实 sim-core 在
后台线程运行（LocalBus 同一命名空间），网关用最小替身（签名 principal、席位）：

- 增删改查与 state、suggested_radius 的回复形状通过契约；增删改在 ≤ 1 s【墙钟】内反映到列表；
- 错误：缺 class 或 pos 422；参数越界 422（字段级 detail）；不存在 404 `543`；上限 409 `505`；未知机型 404 `520`；
- viewer token 的写入全部 403；SB-65 公开路由返回模板原文并支持 ETag；全部路由都在沙盒 OpenAPI 设计表中（SB-40 至 SB-46、SB-65）。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from ctlib import errors
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from simlib import CoreHarness

from awr.api.problem import ApiProblem
from awr.api.rest import targets as TR
from awr.api.security import ApiPrincipal
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, sign_principal
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"


class _Gw:
    def __init__(self, h: CoreHarness, bus: LocalBus) -> None:
        self.bus = bus
        self.settings = SimpleNamespace(producer="sim-core")
        self.clock = SimpleNamespace(global_epoch=1)

        def sign(pid, role, cid, conn_id=None, seat=False):
            pr = Principal(pid, role, "api", conn_id, seat)
            d = pr.fields()
            d["sig"] = sign_principal(pr, cid, h.k_entry)
            return d

        self.tokens = SimpleNamespace(sign_principal=sign)

    def is_seat_holder(self, pid: str) -> bool:
        return True


@pytest.fixture
def env():
    with R.isolated_registry() as reg:
        from awr.sim.targets import plugin

        plugin.install()
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
        bus = LocalBus.open("api", namespace=h.bus.namespace, announce=False)
        stop = threading.Event()
        lock = threading.Lock()

        def pump() -> None:
            while not stop.is_set():
                with lock:
                    h.W[0] += h.core.clock.tick_ns
                    h.core.iterate()
                time.sleep(0.0005)

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        app = FastAPI()
        role = {"r": "operator"}

        @app.exception_handler(ApiProblem)
        async def _p(request: Request, exc: ApiProblem):
            return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status)

        def principal(request: Request) -> None:
            request.state.principal = ApiPrincipal("p-op", role["r"], "j", 0, "public", SID, "sandbox")

        app.include_router(TR.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
        app.include_router(TR.sandbox_public_router, prefix="/api/sandbox/v1")
        app.state.awr = SimpleNamespace(kind="show", bus=bus, gateway=_Gw(h, bus))
        h.seat("p-op")
        try:
            with TestClient(app) as c:
                yield SimpleNamespace(h=h, c=c, lock=lock, role=role)
        finally:
            stop.set()
            th.join(2.0)
            bus.close()
            h.close()


def test_crud_and_shapes(env) -> None:
    c = env.c
    r = c.post(f"{BASE}/targets", json={"class": "person", "pos": [20.0, 5.0], "label": "访客"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert tid == "person-01" and r.json()["cfg"]["label"] == "访客" and r.headers["location"] == f"targets/{tid}"
    assert errors("target/target.schema.json", r.json()["cfg"]) == []
    t0 = time.monotonic()
    lst = c.get(f"{BASE}/targets").json()
    assert time.monotonic() - t0 < 1.0 and [i["id"] for i in lst["items"]] == [tid] and lst["rev"] >= 1
    assert c.get(f"{BASE}/targets", params={"class": "dog"}).json()["items"] == []
    assert c.get(f"{BASE}/targets", params={"state": "unaware"}).json()["items"][0]["id"] == tid
    got = c.get(f"{BASE}/targets/{tid}").json()
    assert got["class"] == "person" and errors("target/target.schema.json", got) == []
    p = c.patch(f"{BASE}/targets/{tid}", json={"alertness": 0.8, "sensitivity": [{"kind": "visual", "shape": "sphere", "radius_max_m": 40}]})
    assert p.status_code == 200 and p.json()["alertness"] == 0.8 and len(p.json()["sensitivity"]) == 1 and p.json()["cfg_rev"] == 2
    time.sleep(0.6)
    st = c.get(f"{BASE}/targets/{tid}/state").json()
    assert errors("rt/payloads/target_detail.schema.json", st) == [] and st["id"] == tid
    sr = c.get(f"{BASE}/targets/{tid}/suggested_radius", params={"model_id": "p600_mid360", "light": "night", "bg": "rural"}).json()
    assert sr["model_id"] == "p600_mid360" and sr["volumes"][0]["kind"] == "visual"
    assert c.delete(f"{BASE}/targets/{tid}").status_code == 204
    r = c.get(f"{BASE}/targets/{tid}")
    assert r.status_code == 404 and r.json()["code"] == 543


def test_errors(env) -> None:
    c = env.c
    assert c.post(f"{BASE}/targets", json={"class": "person"}).status_code == 422
    r = c.post(f"{BASE}/targets", json={"class": "person", "pos": [0, 0], "alertness": 4})
    assert r.status_code == 422 and r.json()["code"] == 110 and r.json()["detail"]["field"] == "alertness"
    r = c.post(f"{BASE}/targets", json={"class": "person", "pos": [0, 0], "sound": {"waveform": "tone", "f0_hz": 1, "lw_dba": 1,
                                                                                    "period_s": 1, "duty": 1}})
    assert r.status_code == 422 and r.json()["code"] == 541
    assert c.patch(f"{BASE}/targets/nope", json={"alertness": 0.1}).json()["code"] == 543
    assert c.delete(f"{BASE}/targets/nope").status_code == 404
    with env.lock:
        env.h.core.entities["target"].limit = 1
    assert c.post(f"{BASE}/targets", json={"class": "object", "pos": [0, 0]}).status_code == 201
    r = c.post(f"{BASE}/targets", json={"class": "object", "pos": [5, 0]})
    assert r.status_code == 409 and r.json()["code"] == 505
    r = c.get(f"{BASE}/targets/object-01/suggested_radius", params={"model_id": "nope_model"})
    assert r.status_code == 404 and r.json()["code"] == 520


def test_viewer_cannot_write_and_public_classes(env) -> None:
    c = env.c
    env.role["r"] = "viewer"
    assert c.post(f"{BASE}/targets", json={"class": "person", "pos": [0, 0]}).status_code == 403
    assert c.patch(f"{BASE}/targets/person-01", json={}).status_code == 403
    assert c.delete(f"{BASE}/targets/person-01").status_code == 403
    assert c.get(f"{BASE}/targets").status_code == 200
    r = c.get("/api/sandbox/v1/target_classes")
    assert r.status_code == 200 and r.json()["schema"] == "awr.target.class_defaults.v1"
    assert c.get("/api/sandbox/v1/target_classes", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_routes_in_design_table() -> None:
    sys.path.insert(0, str(ROOT / "tools" / "contracts"))
    import sandbox_api

    want = {k for k in sandbox_api.route_keys() if "/targets" in k[1] or k[1] == "/target_classes"}
    have = set()
    for r in TR.sandbox_router.routes:
        for m in r.methods:
            have.add((m.lower(), "/sessions/{sid}" + r.path))
    for r in TR.sandbox_public_router.routes:
        for m in r.methods:
            have.add((m.lower(), r.path))
    assert want and want <= have, want - have
    assert not hasattr(TR, "router")   # show-api 的 D1 REST 面不变
