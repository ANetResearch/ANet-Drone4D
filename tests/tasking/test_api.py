"""沙盒编排 REST（M20-AC-003、AC-014 的 REST 部分；M20-FR-080、FR-082、FR-084；17 §17.5 SB-47 至 SB-59、SB-68，§17.6.6 至 §17.6.8）。

`python/awr/api/rest/tasking.py` 的 `sandbox_router` 挂到 `/api/sandbox/v1/sessions/{sid}`（sandbox-api 的挂载方式），真实 sim-core
（M09、M18、M20，sandbox100，平地）在后台线程推进（LocalBus 同一命名空间），网关用最小替身（签名 principal、席位）：

- 机巢增删改查：创建同时放入机体（`vehicle_ids`）、对象通过契约；`inventory` 只读 422 `110`；第 5 个机巢 409 `505`；有机体时删除 409 `105`；
- 任务：dry-run 200 预览（契约 preview）、不可行 422 `580` 与 remedies、超规模 422 `585`；创建 201 + Location、控制 `:start|:pause|:resume|
  :cancel`、`include=plan`、可持续性（24–168 h）、快照与外部计划（过期 409 `105`、不可行 422 `584`、通过 200）；viewer 写入 403，
  viewer dry-run 允许；
- 全部 SB-47 至 SB-59、SB-68 路由都在沙盒 OpenAPI 设计表中（`missing_design_routes` 不再列出编排路由）。
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from ctlib import errors, registry
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from m20core import Core

from awr.api.problem import ApiProblem
from awr.api.rest import tasking as TR
from awr.api.security import ApiPrincipal
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, sign_principal

SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"


def _v(ref: str, obj) -> list[str]:
    from jsonschema import Draft202012Validator

    v = Draft202012Validator({"$ref": f"https://schemas.anet-drone.dev/tasking/1/{ref}"}, registry=registry())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in v.iter_errors(obj)]


class _Gw:
    def __init__(self, core: Core, bus: LocalBus) -> None:
        self.bus = bus
        self.settings = SimpleNamespace(producer="sim-core")
        self.clock = SimpleNamespace(global_epoch=1)
        h = core.h

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
    core = Core()
    h = core.h
    bus = LocalBus.open("api", namespace=h.bus.namespace, announce=False)
    stop = threading.Event()
    lock = threading.Lock()

    def pump() -> None:
        while not stop.is_set():
            with lock:
                for _ in range(4):
                    h.W[0] += 8 * h.core.clock.tick_ns
                    h.core.iterate()
            time.sleep(0.0005)

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    app = FastAPI()
    role = {"r": "operator"}

    @app.exception_handler(ApiProblem)
    async def _p(request: Request, exc: ApiProblem):
        from awr.api.problem import problem_body

        return JSONResponse(problem_body(exc.code, status=exc.status, detail=exc.detail), status_code=exc.status or problem_body(exc.code)["status"])

    def principal(request: Request) -> None:
        request.state.principal = ApiPrincipal("p-op", role["r"], "j", 0, "public", SID, "sandbox")

    app.include_router(TR.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
    app.state.awr = SimpleNamespace(kind="show", bus=bus, gateway=_Gw(core, bus))
    try:
        with TestClient(app) as c:
            yield SimpleNamespace(core=core, c=c, lock=lock, role=role)
    finally:
        stop.set()
        th.join(2.0)
        bus.close()
        core.close()


def _wait(pred, timeout_s: float = 20.0) -> bool:
    t_end = time.monotonic() + timeout_s
    while time.monotonic() < t_end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_nest_crud_and_errors(env) -> None:
    c = env.c
    r = c.post(f"{BASE}/nests", json={"name": "东侧机巢", "pos": [0.0, 0.0, None], "kind": "box", "slots": 4,
                                      "inventory": [{"model_id": "p600_mid360", "count": 2}]}, headers={"Idempotency-Key": "k-nest-1"})
    assert r.status_code == 201, r.text
    body = r.json()
    nid = body["id"]
    assert r.headers["location"] == f"nests/{nid}" and len(body["vehicle_ids"]) == 2
    assert _v("nest.schema.json", body) == []
    assert _wait(lambda: sum(i.get("count", 0) for i in (c.get(f"{BASE}/nests/{nid}").json().get("inventory") or [])) == 2)
    lst = c.get(f"{BASE}/nests").json()
    assert [n["id"] for n in lst["items"]] == [nid] and _v("nest.schema.json#/$defs/list", lst) == []
    p = c.patch(f"{BASE}/nests/{nid}", json={"name": "改名", "wind_max_mps": 10})
    assert p.status_code == 200 and p.json()["name"] == "改名" and p.json()["wind_max_mps"] == 10
    r = c.patch(f"{BASE}/nests/{nid}", json={"inventory": []})
    assert r.status_code == 422 and r.json()["code"] == 110 and r.json()["detail"]["why"] == "INVENTORY_READONLY"
    r = c.patch(f"{BASE}/nests/{nid}", json={"slots": 99})
    assert r.status_code == 422 and r.json()["code"] == 110
    r = c.delete(f"{BASE}/nests/{nid}")
    assert r.status_code == 409 and r.json()["code"] == 105
    assert c.get(f"{BASE}/nests/n-99").status_code == 404
    ids = [c.post(f"{BASE}/nests", json={"pos": [100.0 * k, 300.0, None]}).json()["id"] for k in range(1, 4)]
    r = c.post(f"{BASE}/nests", json={"pos": [0.0, 500.0, None]})
    assert r.status_code == 409 and r.json()["code"] == 505
    assert c.delete(f"{BASE}/nests/{ids[-1]}").status_code == 204
    r = c.post(f"{BASE}/nests", json={"pos": [0.0, 0.0], "kind": "launcher", "inventory": [{"model_id": "p600_mid360", "count": 1}]})
    assert r.status_code == 409 and r.json()["code"] == 525
    env.role["r"] = "viewer"
    assert c.post(f"{BASE}/nests", json={"pos": [5.0, 5.0]}).status_code == 403
    assert c.get(f"{BASE}/nests").status_code == 200


def test_task_preview_create_control_and_plan(env) -> None:
    c = env.c
    nid = c.post(f"{BASE}/nests", json={"pos": [0.0, 0.0, None], "inventory": [{"model_id": "p600_mid360", "count": 3}]}).json()["id"]
    assert _wait(lambda: len(c.get(f"{BASE}/tasking/snapshot").json().get("vehicles") or []) == 3)
    scan = {"type": "area_scan_gsd", "params": {"polygon": [[200, -150], [500, -150], [500, 150], [200, 150]], "gsd_cm": 3.0, "t_max_s": 240}}
    r = c.post(f"{BASE}/tasks", params={"dry_run": "true"}, json=scan)
    assert r.status_code == 200, r.text
    pv = r.json()
    assert _v("task.schema.json#/$defs/preview", pv) == [] and pv["n_vehicles"] >= 2 and pv["scan"]["h_m"] > 0
    g = c.get(f"{BASE}/tasks/previews/{pv['preview_id']}")
    assert g.status_code == 200 and g.json()["preview_id"] == pv["preview_id"]
    assert c.get(f"{BASE}/tasks/previews/pv-aaaaaaaa").status_code == 404
    # 不可行：单站远距视锥（580 single_station_far_view）与超规模（585）
    bad = {"type": "area_scan_gsd", "params": {**scan["params"], "strategy": "single_station"}}
    r = c.post(f"{BASE}/tasks", params={"dry_run": "true"}, json=bad)
    assert r.status_code == 422 and r.json()["code"] == 580 and r.json()["detail"]["why"] == "single_station_far_view"
    assert r.json()["detail"]["remedies"]
    huge = {"type": "area_patrol", "params": {"polygon": [[0, 0], [3000, 0], [3000, 3000], [0, 3000]]}}
    r = c.post(f"{BASE}/tasks", params={"dry_run": "true"}, json=huge)
    assert r.status_code == 422 and r.json()["code"] == 585
    r = c.post(f"{BASE}/tasks", json={"type": "nope", "params": {}})
    assert r.status_code == 422 and r.json()["code"] == 110
    # 创建（复用预览）、include=plan、控制
    r = c.post(f"{BASE}/tasks", json={**scan, "label": "扫描", "preview_id": pv["preview_id"]}, headers={"Idempotency-Key": "k-task-1"})
    assert r.status_code == 201, r.text
    tid = r.json()["tid"]
    assert r.headers["location"] == f"tasks/{tid}"
    assert _wait(lambda: c.get(f"{BASE}/tasks/{tid}").json()["state"] in ("READY", "RUNNING"))
    if c.get(f"{BASE}/tasks/{tid}").json()["state"] == "READY":
        assert c.post(f"{BASE}/tasks/{tid}:start").json()["state"] == "RUNNING"
    obj = c.get(f"{BASE}/tasks/{tid}", params={"include": "plan"}).json()
    assert errors("tasking/task.schema.json", obj) == [] and obj["plan"]["geometry"]["strips"]
    assert _v("task.schema.json#/$defs/list", c.get(f"{BASE}/tasks").json()) == []
    r = c.post(f"{BASE}/tasks/{tid}:resume")
    assert r.status_code == 409 and r.json()["code"] == 105
    assert c.post(f"{BASE}/tasks/{tid}:pause").json()["state"] == "PAUSED"
    assert c.post(f"{BASE}/tasks/{tid}:resume").json()["state"] == "RUNNING"
    su = c.get(f"{BASE}/tasks/{tid}/sustainability", params={"horizon_h": 24}).json()
    assert su["verdict"] == "not_applicable"
    assert c.get(f"{BASE}/tasks/{tid}/sustainability", params={"horizon_h": 5}).status_code == 422
    assert c.post(f"{BASE}/tasks/{tid}:cancel").json()["state"] == "CANCELED"
    assert c.get(f"{BASE}/tasks/t-99").status_code == 404
    assert c.get(f"{BASE}/relay/{tid}/stats").status_code == 404
    # 外部计划：过期 409 105；不可行 422 584；通过 200
    point = {"type": "point_watch", "params": {"point": [150.0, 80.0], "dwell_s": None}, "common": {"allocator": "external",
                                                                                                    "external_fallback": "none"}}
    tid2 = c.post(f"{BASE}/tasks", json=point).json()["tid"]
    assert _wait(lambda: c.get(f"{BASE}/tasks/{tid2}").json()["state"] in ("READY", "RUNNING"))
    if c.get(f"{BASE}/tasks/{tid2}").json()["state"] == "READY":
        c.post(f"{BASE}/tasks/{tid2}:start")
    assert _wait(lambda: any(w["tid"] == tid2 for w in c.get(f"{BASE}/tasking/snapshot").json()["work_items"]))
    snap = c.get(f"{BASE}/tasking/snapshot").json()
    assert _v("snapshot.schema.json", snap) == []
    wid = next(w["wid"] for w in snap["work_items"] if w["tid"] == tid2)
    uav = snap["vehicles"][0]["id"]
    r = c.post(f"{BASE}/tasking/plans", json={"based_on_revision": snap["revision"] - 1, "allocator": "external",
                                               "assignments": [{"wid": wid, "uav": uav, "order": 0}]})
    assert r.status_code == 409 and r.json()["code"] == 105
    snap = c.get(f"{BASE}/tasking/snapshot").json()
    r = c.post(f"{BASE}/tasking/plans", json={"based_on_revision": snap["revision"], "allocator": "external",
                                               "assignments": [{"wid": "nope.0", "uav": uav, "order": 0}]})
    assert r.status_code == 422 and r.json()["code"] == 584 and r.json()["detail"]["violations"]
    snap = c.get(f"{BASE}/tasking/snapshot").json()
    r = c.post(f"{BASE}/tasking/plans", json={"based_on_revision": snap["revision"], "allocator": "external",
                                               "assignments": [{"wid": wid, "uav": uav, "order": 0}]})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["plan_id"].startswith("pl-")
    # viewer：写入 403，dry-run 允许
    env.role["r"] = "viewer"
    assert c.post(f"{BASE}/tasks", json=point).status_code == 403
    assert c.post(f"{BASE}/tasks/{tid2}:cancel").status_code == 403
    assert c.post(f"{BASE}/tasks", params={"dry_run": "true"}, json={"type": "point_watch", "params": {"point": [60.0, 20.0]}}).status_code == 200
    del nid


def test_routes_cover_design_table() -> None:
    from awr.api.sandbox_main import missing_design_routes

    left = [r for r in missing_design_routes() if any(s in r[1] for s in ("/nests", "/tasks", "/tasking", "/relay"))]
    assert left == []


def test_api_quota_table_matches_plan_quota() -> None:
    from awr.sim.planning.jobs import PLAN_QUOTA

    for k, v in TR.PLAN_QUOTA_API.items():
        assert PLAN_QUOTA[k] == v
