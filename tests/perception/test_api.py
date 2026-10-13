"""M19-FR-025：感知 REST（17 §17.5 SB-38、SB-39、SB-66、SB-67；`python/awr/api/rest/perception.py` 的 `sandbox_router`）。

路由挂到 `/api/sandbox/v1/sessions/{sid}`（sandbox-api 的挂载方式）；总线替身把 `ctl/sim-core/query` 直接交给隔离登记表中 M19 登记的
查询处理函数（与 sim-core `_slow_query` 同一调用），台架为 `plib.PBench`：

- SB-38 帧与附加的 radar、acoustic、lidar；不存在的机体 404（107）；非法 vid 400；
- SB-39 分页、过滤与 422（level_min、limit）；since_seq 早于检测环 410（319）；
- SB-66 航迹（P1）；SB-67 可行窗口（P1）：可行、不可行原因、未知机型 404（520）、识别物不存在 404（543）、非成像传感器 409（560）、
  字段校验 422；sim-core 不可达 503（211）；
- 全部路由在沙盒 OpenAPI 设计表中，模块不导出 D1 `router`。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from plib import FakeEnv, FakeWorld, PBench, make_imaging, make_nonimg

from awr.api.problem import ApiProblem
from awr.api.rest import perception as PR
from awr.api.security import ApiPrincipal
from awr.contracts import bus_keys
from awr.sim.perception import gx40, mmw77

ROOT = Path(__file__).resolve().parents[2]
SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"


class _Bus:
    def __init__(self, b: PBench) -> None:
        self.b = b
        self.down = False
        self.calls: list[dict] = []

    async def call(self, key, msg, timeout=1.5, retries=0, retry_gap=0.0):
        if self.down:
            raise TimeoutError("down")
        assert key == bus_keys.CTL_QUERY
        self.calls.append(msg)
        spec = self.b.reg.queries.get(msg["op"])
        if spec is None:
            return {"v": 1, "code": 213}
        return spec.fn(msg, self.b.ctx)


@pytest.fixture
def env():
    from awr.sim.fleet.profiles import ProfileTable

    b = PBench(1, FakeWorld(), env=FakeEnv())
    b.ctx.profiles = ProfileTable()
    tid = b.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}})
    b.step(1)
    p = (0.0, 0.0, 60.0)
    b.platform(0, pos=p, imaging=[make_imaging(gx40(), p, (300.0, 0.0, 0.9), f_mm=48.0, name="eo")],
               nonimg=[make_nonimg("radar", mmw77(), p, name="radar")])
    b.run(2.5)
    app = FastAPI()

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status)

    def principal(request: Request) -> None:
        request.state.principal = ApiPrincipal("p-view", "viewer", "j", 0, "public", SID, "sandbox")

    app.include_router(PR.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
    bus = _Bus(b)
    app.state.awr = SimpleNamespace(kind="show", bus=bus, gateway=None)
    with b, TestClient(app) as c:
        yield SimpleNamespace(b=b, c=c, bus=bus, tid=tid)


def test_vehicle_perception(env):
    c = env.c
    r = c.get(f"{BASE}/vehicles/uav-01/perception")
    assert r.status_code == 200
    j = r.json()
    assert j["uav"] == "uav-01" and j["sensors"][0]["targets"][0]["target"] == env.tid
    assert j["radar"]["sensor"] == "radar" and j["radar"]["tracks"] == [] and j["acoustic"] is None   # 300 m 超出人的雷达检测距离
    assert c.get(f"{BASE}/vehicles/uav-09/perception").json()["code"] == 107
    assert c.get(f"{BASE}/vehicles/uav-09/perception").status_code == 404
    assert c.get(f"{BASE}/vehicles/UAV!/perception").status_code == 400


def test_detections(env):
    c = env.c
    j = c.get(f"{BASE}/detections").json()
    assert j["items"] and j["next_since_seq"] == j["items"][-1]["seq"]
    k = c.get(f"{BASE}/detections", params={"since_seq": j["items"][0]["seq"], "limit": 1}).json()
    assert len(k["items"]) == 1 and k["items"][0]["seq"] == j["items"][1]["seq"]
    assert c.get(f"{BASE}/detections", params={"target": env.tid, "sensor": "eo", "level_min": "R"}).status_code == 200
    assert c.get(f"{BASE}/detections", params={"level_min": "X"}).status_code == 422
    assert c.get(f"{BASE}/detections", params={"limit": 501}).status_code == 422
    ring = env.b.prt.ring
    for _ in range(ring.cap + 2):
        ring.push({"t_sim_ns": 0, "kind": "detect", "uav": "x", "sensor": "s", "sensor_kind": "radar", "target": None, "level_from": "none",
                   "level_to": "D"})
    r = c.get(f"{BASE}/detections", params={"since_seq": 1})
    assert r.status_code == 410 and r.json()["code"] == 319


def test_tracks_and_feasibility(env):
    c = env.c
    tr = c.get(f"{BASE}/tracks").json()["items"]
    assert tr and tr[0]["target"] == env.tid and len(tr[0]["cov_diag"]) == 6
    assert c.get(f"{BASE}/tracks", params={"target": "dog-09"}).json()["items"] == []
    body = {"target": env.tid, "model_id": "awr_q9", "sensor": "eo", "light": "day", "mor_m": 10000}
    r = c.post(f"{BASE}/perception/feasibility", json=body)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["feasible"] and j["d_lo_m"] == pytest.approx(320.1, abs=0.1) and 840 <= j["d_hi_m"] <= 860
    n = c.post(f"{BASE}/perception/feasibility", json={**body, "light": "night", "sensor": "lwir"}).json()
    assert n["feasible"] is True or n["why"] in ("narrow", "keepout_gt_range")
    assert c.post(f"{BASE}/perception/feasibility", json={**body, "model_id": "nope"}).status_code == 404
    assert c.post(f"{BASE}/perception/feasibility", json={**body, "target": "dog-09"}).json()["code"] == 543
    r = c.post(f"{BASE}/perception/feasibility", json={**body, "sensor": "radar"})
    assert r.status_code == 409 and r.json()["code"] == 560
    assert c.post(f"{BASE}/perception/feasibility", json={**body, "light": "noon"}).status_code == 422
    assert c.post(f"{BASE}/perception/feasibility", json={**body, "extra": 1}).status_code == 422
    assert c.post(f"{BASE}/perception/feasibility", json={"target": env.tid}).status_code == 422


def test_sim_unavailable(env):
    env.bus.down = True
    r = env.c.get(f"{BASE}/detections")
    assert r.status_code == 503 and r.json()["code"] == 211


def test_routes_in_design_table():
    sys.path.insert(0, str(ROOT / "tools" / "contracts"))
    import sandbox_api

    want = {k for k in sandbox_api.route_keys() if "perception" in k[1] or k[1].endswith("/detections") or k[1].endswith("/tracks")}
    have = {(m.lower(), "/sessions/{sid}" + r.path) for r in PR.sandbox_router.routes for m in r.methods}
    assert want and want <= have, want - have
    assert not hasattr(PR, "router")
