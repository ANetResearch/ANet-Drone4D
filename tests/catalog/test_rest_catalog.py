"""目录 REST（CAT-AC-015；CAT-FR-004、FR-018；AWR-20 §9；17 §17.5 SB-21 至 SB-27、§17.6.2；契约 `vehicle/catalog.schema.json`）。

公开目录接口（`sandbox_public_router`，前缀 `/api/sandbox/v1`）：
- SB-21 条目通过契约 `$defs/model_summary`（additionalProperties: false），5 个可见机型按目录次序，参考机型 `reference = true`；
- SB-22 详情含 profile（通过 Vehicle Package schema 1.1）、params_table（契约行形状）、数值 derived、AWR-20 §9 的 catalog、slots、
  signature、fw（固定翼类）、checks；参考机型的 `catalog.disclaimer_zh` 为免责文本；隐藏机型与未知 id 404 `520`；
- SB-23 条目通过契约 `$defs/sensor_item`；
- ETag 与 `Cache-Control: public, max-age=300`，`If-None-Match` 命中 304；
会话路由（`sandbox_router`，真实 sim-core 在后台线程运行，M21 插件已装配）：
- SB-25 克隆 201（详情 `session_clone = true`、`base`、重算后的 derived），越界 422 `522` 与逐项 violations；SB-24 列表；SB-26 改名；
  被机队实例引用时 SB-27 删除 409 `105`，移除实例后 204；
- 全部路由都在沙盒 OpenAPI 设计表（`tools/contracts/sandbox_api.py` SB-21 至 SB-27）中出现。
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from catlib import ROOT, install
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from simlib import CoreHarness

from awr.api.problem import ApiProblem
from awr.api.rest import catalog as CR
from awr.api.rest import fleet as F
from awr.api.security import ApiPrincipal
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, sign_principal
from awr.sim.fleet.stages import registry as R
from awr.sim.schemas import contracts_registry

SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"
CAT_SCHEMA = json.loads((ROOT / "packages/contracts/vehicle/catalog.schema.json").read_text(encoding="utf-8"))


def _validator(ref: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": CAT_SCHEMA["$id"] + ref}, registry=contracts_registry())


def _app() -> FastAPI:
    app = FastAPI()

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status or 400)

    app.include_router(CR.sandbox_public_router, prefix="/api/sandbox/v1")
    return app


def test_public_catalog_routes() -> None:
    with TestClient(_app()) as c:
        r = c.get("/api/sandbox/v1/catalog/models")
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
        et = r.headers["etag"]
        items = r.json()["items"]
        assert [m["model_id"] for m in items] == ["p600_mid360", "awr_q1", "awr_q9", "awr_f1", "awr_v1"]
        v = _validator("#/$defs/model_summary")
        for m in items:
            assert list(v.iter_errors(m)) == [], m["model_id"]
            assert m["reference"] == m["model_id"].startswith("awr_")
        assert {m["model_id"]: m["category"] for m in items}["awr_v1"] == "vtol"
        r2 = c.get("/api/sandbox/v1/catalog/models", headers={"If-None-Match": et})
        assert r2.status_code == 304 and r2.headers["etag"] == et
        d = c.get("/api/sandbox/v1/catalog/models/awr_f1")
        assert d.status_code == 200 and d.headers["etag"]
        det = d.json()
        prof = Draft202012Validator({"$ref": "https://schemas.anet-drone.dev/vehicle/1/vehicle_profile.schema.json"},
                                    registry=contracts_registry())
        assert list(prof.iter_errors(det["profile"])) == []
        row = Draft202012Validator(CAT_SCHEMA["$defs"]["model_detail"]["properties"]["params_table"]["items"])
        assert det["params_table"] and all(not list(row.iter_errors(x)) for x in det["params_table"])
        assert all(isinstance(x, (int, float)) for x in det["derived"].values())
        assert det["derived"]["p_cruise_w"] == pytest.approx(201.4, rel=0.01) and det["derived"]["r_loiter_min_w10_m"] == \
            pytest.approx(96.5, rel=0.01)
        assert det["catalog"]["reference"] and det["catalog"]["disclaimer_zh"] == "模拟参考值，非任何真实产品"
        assert len(det["fw"]["loiter_radius_table"]) == 6 and det["fw"]["derived"]["v_stall_mps"] == pytest.approx(11.10, rel=0.01)
        assert all(x["pass"] for x in det["checks"]) and {s["slot"] for s in det["slots"]} == {"eo", "lwir", "gnss", "imu"}
        assert "ops.crewed_recovery" in det["capabilities"] and det["signature"]["rcs_m2"] == 0.10
        p600 = c.get("/api/sandbox/v1/catalog/models/p600_mid360").json()
        assert p600["catalog"]["disclaimer_zh"] is None and p600["twin"]
        assert {r["group"] for r in p600["params_table"]} >= {"飞行器", "飞控", "机载计算", "动力电池", "遥控器", "数传", "RTK", "充电器", "派生"}
        r = c.get("/api/sandbox/v1/catalog/models/x500")
        assert r.status_code == 404 and r.json()["code"] == 520
        assert c.get("/api/sandbox/v1/catalog/models/Bad-Id").status_code == 400
        s = c.get("/api/sandbox/v1/catalog/sensors")
        assert s.status_code == 200 and s.headers["etag"]
        sv = _validator("#/$defs/sensor_item")
        sitems = s.json()["items"]
        assert {x["sensor_type"] for x in sitems} == {"eo_gx40", "eo_z8", "nir_ill_l", "lwir640", "mmw77", "mic8", "mid360"}
        for x in sitems:
            assert list(sv.iter_errors(x)) == [], x["sensor_type"]
        gx = next(x for x in sitems if x["sensor_type"] == "eo_gx40")
        assert gx["kind"] == "eo" and gx["spec"]["f_mm"] == [4.8, 48.0] and not gx["reference"]
        assert next(x for x in sitems if x["sensor_type"] == "mmw77")["spec"]["derived"]["r_detect_m"]["person"] == \
            pytest.approx(151.4, abs=0.1)


# ------------------------------------------------------------------------------------------------ 会话路由
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
        install()
        h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
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
        app = _app()

        def principal(request: Request) -> None:
            request.state.principal = ApiPrincipal("p-op", "operator", "j", 0, "public", SID, "sandbox")

        app.include_router(CR.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
        app.include_router(F.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
        app.state.awr = SimpleNamespace(kind="show", bus=bus, gateway=_Gw(h, bus))
        h.seat("p-op")
        try:
            with TestClient(app) as c:
                yield SimpleNamespace(h=h, c=c)
        finally:
            stop.set()
            th.join(2.0)
            bus.close()
            h.close()


def test_session_clone_routes(env) -> None:
    c = env.c
    r = c.post(f"{BASE}/models", json={"from_model_id": "awr_q1", "label": "Q1 热像", "patch": {"sensors": [{"name": "lwir"}]}})
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["model_id"] == "awr_q1_c1" and d["session_clone"] and d["base"] == "awr_q1@1.0.0" and d["name_zh"] == "Q1 热像"
    assert d["mass_kg"] == pytest.approx(1.04) and d["derived"]["p_hover_w"] == pytest.approx(130.0, abs=0.1)
    assert d["default_mounts"] == {"lwir": True} and "lwir640" in d["default_sensors"]
    assert r.headers["location"] == "models/awr_q1_c1"
    r = c.post(f"{BASE}/models", json={"from_model_id": "awr_q1", "patch": {"mass_kg": 5.0}})
    assert r.status_code == 422 and r.json()["code"] == 522
    v = r.json()["detail"]["violations"]
    assert v[0]["field"] == "mass_kg" and v[0]["rule"] == "CL-2" and v[0]["limit"][1] == 1.05
    r = c.post(f"{BASE}/models", json={"from_model_id": "nope_x", "patch": {}})
    assert r.status_code == 404 and r.json()["code"] == 520
    lst = c.get(f"{BASE}/models").json()["items"]
    assert [m["model_id"] for m in lst] == ["awr_q1_c1"] and lst[0]["session_clone"]
    assert list(_validator("#/$defs/models").iter_errors({"items": lst})) == []
    r = c.patch(f"{BASE}/models/awr_q1_c1", json={"label": "Q1 改名"})
    assert r.status_code == 200 and r.json()["name_zh"] == "Q1 改名"
    r = c.post(f"{BASE}/vehicles", json={"model_id": "awr_q1_c1", "home_enu_m": [0.0, 0.0, None]})
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    r = c.delete(f"{BASE}/models/awr_q1_c1")
    assert r.status_code == 409 and r.json()["code"] == 105
    r = c.patch(f"{BASE}/models/awr_q1_c1", json={"patch": {"link": {"range_m": 9000.0}}})
    assert r.status_code == 409 and r.json()["code"] == 105
    assert c.delete(f"{BASE}/vehicles/{vid}").status_code == 202
    t_end = time.monotonic() + 10
    while time.monotonic() < t_end and c.get(f"{BASE}/vehicles").json()["items"]:
        time.sleep(0.05)
    assert c.delete(f"{BASE}/models/awr_q1_c1").status_code == 204
    assert c.get(f"{BASE}/models").json()["items"] == []


def test_routes_in_sandbox_design_table() -> None:
    sys.path.insert(0, str(ROOT / "tools" / "contracts"))
    import sandbox_api as SA

    want = {(ep.method.upper(), ep.path) for ep in SA.ENDPOINTS if "rest/catalog.py" in ep.owner}
    got = set()
    for r in CR.sandbox_public_router.routes:
        for m in r.methods:
            got.add((m, r.path))
    for r in CR.sandbox_router.routes:
        for m in r.methods:
            got.add((m, "/sessions/{sid}" + r.path))
    assert want == got, want ^ got
    assert not hasattr(CR, "router")  # 不导出 D1 router


def test_view_missing_is_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWR_CATALOG_VIEW", str(tmp_path / "none.json"))
    with TestClient(_app()) as c:
        r = c.get("/api/sandbox/v1/catalog/models")
        assert r.status_code == 503 and r.json()["code"] == 211
