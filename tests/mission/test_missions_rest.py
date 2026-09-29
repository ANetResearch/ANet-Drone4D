"""剧本与任务 REST（M10-FR-065；M10-AC-025、AC-029；AWR-17 §4.3.4、§4.3.7）：R10、R11、R23、R24、R25、R26、R65 与任务控制。

api 路由经 `ctl/sim-core/query` 转发：测试以替身总线把查询直接交给进程内 SimCore 上的 M10 查询路由（plan-pool inline）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from harness import Sim

from awr.api.main import _install_handlers
from awr.api.rest import scenarios as SC
from awr.api.security import ApiPrincipal
from awr.world.geometry.fake import fake_world_query

ROOT = Path(__file__).resolve().parents[2]


class FakeBus:
    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.msgs: list = []

    async def call(self, key: str, msg: dict, *, timeout: float = 1.0, retries: int = 0):
        self.msgs.append(msg)
        self.sim.advance(0.05)
        spec = self.sim.reg.queries.get(msg["op"])
        return spec.fn(msg, self.sim.core.ctx)


@pytest.fixture
def env(tmp_path, monkeypatch):
    sd = tmp_path / "scenarios"
    sd.mkdir()
    shutil.copy(ROOT / "packages/contracts/fixtures/scenario/s1-shenzhen-facade.json", sd / "s1-shenzhen-facade.json")
    (sd / "catalog.json").write_text("{}")
    monkeypatch.setenv("AWR_SCENARIOS_DIR", str(sd))
    sim = Sim(fake_world_query(tmp_path), n=2)
    sim.advance(1.0)
    app = FastAPI()
    _install_handlers(app)
    role = {"r": "operator"}

    @app.middleware("http")
    async def auth(request: Request, call_next):
        request.state.principal = ApiPrincipal("p-op", role["r"], "j", 0, "loopback")
        return await call_next(request)

    gw = SimpleNamespace(tokens=SimpleNamespace(sign_principal=lambda pid, r, cid, conn_id=None, seat=False:
                                                {"principal_id": pid, "role": r, "entry": "api", "seat": seat}),
                         is_seat_holder=lambda pid: True)
    app.state.awr = SimpleNamespace(bus=FakeBus(sim), gateway=gw)
    app.include_router(SC.router)
    with TestClient(app) as c:
        yield c, sim, role
    sim.close()


def test_r10_r11(env) -> None:
    c, _sim, _ = env
    r = c.get("/api/scenarios", params={"world_id": "shenzhen"})
    assert r.status_code == 200
    it = r.json()["items"]
    assert [x["id"] for x in it] == ["s1-shenzhen-facade"] and it[0]["n_vehicles"] == 2 and len(it[0]["sha256"]) == 64
    assert c.get("/api/scenarios", params={"world_id": "suzhou"}).json()["items"] == []
    r = c.get("/api/scenarios/s1-shenzhen-facade")
    assert r.status_code == 200 and json.loads(r.content)["scenario_id"] == "s1-shenzhen-facade"
    assert c.get("/api/scenarios/nope").json()["code"] == 305
    assert c.get("/api/scenarios/..%2Fx").status_code in (400, 404)


def test_r25_r23_r24_and_control(env) -> None:
    c, sim, _ = env
    vid = sim.ids[0]
    body = {"generator": "follow_path", "vehicle_ids": [vid],
            "params": {"waypoints_enu_m": [[-150, -60, 40], [-60, -60, 40]], "speed_mps": 5}}
    r = c.post("/api/missions", json=body, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 201 and r.json()["state"] == "IDLE"
    mid = r.json()["mid"]
    assert c.post("/api/missions", json=body, headers={"Idempotency-Key": "k1"}).json()["mid"] == mid
    items = c.get("/api/missions").json()["items"]
    assert [x["mid"] for x in items] == [mid] and items[0]["generator"] == "follow_path"
    sim.advance(0.5)
    d = c.get(f"/api/missions/{mid}").json()
    assert d["mid"] == mid and d["tracks"][0]["vehicle_id"] == vid and d["paths"] and d["plan"]["state"] == "ok"
    assert c.get("/api/missions/nope").json()["code"] == 305
    assert c.post(f"/api/missions/{mid}/start").json()["status"] == "accepted"
    assert sim.rt.missions.missions[mid].state == "RUNNING"
    assert c.post(f"/api/missions/{mid}/start").json()["code"] == 105
    assert c.post(f"/api/missions/{mid}/abort").status_code == 200
    bad = dict(body, params={"waypoints_enu_m": [[0, 0, 0]], "bogus": 1})
    r = c.post("/api/missions", json=bad)
    assert r.status_code == 422 and r.json()["code"] == 110 and r.json()["detail"]["pointer"].startswith("/params")


def test_r26_r65_preview(env) -> None:
    c, sim, role = env
    role["r"] = "viewer"
    vid = sim.ids[0]
    body = {"generator": "lawnmower", "vehicle_ids": [vid],
            "params": {"polygon_enu_m": [[-160, -110], [40, -110], [40, 110], [-160, 110]],
                       "altitude": {"mode": "fly_over", "agl_m": 60}}}
    r = c.post("/api/missions/preview", json=body)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["status"] == "ok" and p["paths"][0]["vehicle_id"] == vid and p["stats"]["coverage_pred"] >= 0.99
    assert p["energy_precheck"]["per_vehicle"][0]["id"] == vid and p["region"]
    assert len(p["paths"][0]["polyline_enu_m"]) <= 2000
    g = c.get(f"/api/missions/preview/{p['preview_id']}")
    assert g.status_code == 200 and g.json()["preview_id"] == p["preview_id"]
    assert c.get("/api/missions/preview/pv-000000").json()["detail"]["reason"] == "PREVIEW_EXPIRED"
    # 过期（60 s 墙钟）
    sim.W[0] += 61_000_000_000
    assert c.get(f"/api/missions/preview/{p['preview_id']}").status_code == 404
    # viewer 不能创建
    assert c.post("/api/missions", json=body).status_code == 403
