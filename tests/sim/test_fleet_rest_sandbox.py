"""沙盒车辆路由（M08-AC-213；M08-FR-226、FR-227；M08 §15.3.8；17 §17.6.3；契约 vehicle/instance.schema.json）。

`python/awr/api/rest/fleet.py` 的 `sandbox_router` 挂载到 `/api/sandbox/v1/sessions/{sid}`（sandbox-api 的挂载方式），经请求级上下文
`ctx_for` 取运行上下文；上下文与网关用最小替身（真实 sim-core 进程内运行于后台线程，LocalBus 同一命名空间）：

- SB-28 至 SB-34 的回复与契约形状一致（列表条目、创建回复、全部状态 `{t_sim_ns, state, state_ext, fw}`、传感器 `{sensors[]}`）；
- 创建：未知机型 404 `520`、`nest_id` 与 `home_enu_m` 都缺省 422、机队上限 409 `505`；沙盒实例缺省限速档为
  `catalog.limits_default`（p600_std）；
- PATCH 改标签与限速档；DELETE 202；SB-35、SB-36 经网关调用入口（替身转发到 sim-core 的命令与租约服务）；
- 全部路由都在 sandbox OpenAPI 设计表（`tools/contracts/sandbox_api.py` SB-28 至 SB-37）中出现。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from simlib import CoreHarness

from awr.api.problem import ApiProblem
from awr.api.rest import fleet as F
from awr.api.security import ApiPrincipal
from awr.contracts import bus_keys
from awr.runtime.bus import LocalBus
from awr.runtime.principal import Principal, sign_principal
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"


class _Rpc:
    """网关 RpcRouter 的替身：`uav/{id}/cmd/<op>` 转为 sim-core 命令（acquire、release 转租约服务），回送首个 result。"""

    def __init__(self, gw: _Gw) -> None:
        self.gw = gw

    def handle_call(self, caller, m: dict) -> None:
        parts = m["service"].split("/")
        uav, op = parts[1], "/".join(parts[3:])
        cid = m["id"]
        p = self.gw.tokens.sign_principal(caller.principal.id, caller.principal.role, cid, conn_id=None, seat=True)
        if op in ("acquire", "release"):
            key, msg = bus_keys.CTL_LEASE, {"v": 1, "cid": cid, "op": op, "uav": uav, "principal": p,
                                             "return_to": m["args"].get("return_to", "previous")}
        else:
            key, msg = bus_keys.ctl_cmd("sim-core"), {"v": 1, "cid": cid, "op": op, "uav": uav, "args": m["args"],
                                                      "principal": p, "lease": None, "t_wall_ns": 0, "epoch_seen": 1,
                                                      "batch_id": None}
        loop = asyncio.get_running_loop()

        def cb(rep, err) -> None:
            r = rep or {}
            res = {"op": "result", "id": cid, "status": r.get("status", "rejected"), "code": int(r.get("code") or 0),
                   "final": r.get("status") == "rejected", "detail": r.get("detail"), "data": {"lease": r.get("lease")}}
            loop.call_soon_threadsafe(caller.send_ctrl, res)

        self.gw.bus.call_cb(key, msg, cb, timeout=2.0, retries=0)


class _Gw:
    def __init__(self, h: CoreHarness, bus: LocalBus) -> None:
        self.bus = bus
        self.settings = SimpleNamespace(producer="sim-core")
        self.clock = SimpleNamespace(global_epoch=1)
        self.rpc = _Rpc(self)

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
        app = FastAPI()

        @app.exception_handler(ApiProblem)
        async def _p(request: Request, exc: ApiProblem):
            from fastapi.responses import JSONResponse

            return JSONResponse({"code": exc.code, "detail": exc.detail}, status_code=exc.status)

        def principal(request: Request) -> None:
            request.state.principal = ApiPrincipal("p-op", "operator", "j", 0, "public", SID, "sandbox")

        app.include_router(F.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
        app.state.awr = SimpleNamespace(kind="show", bus=bus, gateway=_Gw(h, bus))
        h.seat("p-op")
        try:
            with TestClient(app) as c:
                yield SimpleNamespace(h=h, c=c, lock=lock)
        finally:
            stop.set()
            th.join(2.0)
            bus.close()
            h.close()


def _wait_ready(e, vid: str) -> None:
    t_end = time.monotonic() + 10
    while time.monotonic() < t_end:
        it = e.c.get(f"{BASE}/vehicles/{vid}").json()
        if it.get("lifecycle") == "READY":
            return
        time.sleep(0.05)
    raise AssertionError("not READY")


def test_sandbox_vehicle_routes(env) -> None:
    e = env
    r = e.c.post(f"{BASE}/vehicles", json={"model_id": "p600_mid360", "home_enu_m": [0.0, 0.0, None], "label": "A-01"})
    assert r.status_code == 201, r.text
    cr = r.json()
    assert set(cr) == {"id", "agent_no", "lifecycle", "model_id", "nest_id"} and cr["model_id"] == "p600_mid360"
    vid = cr["id"]
    assert vid == "p600-01"
    assert e.c.post(f"{BASE}/vehicles", json={"model_id": "nope_x"}).status_code == 422  # 二选一缺失
    r = e.c.post(f"{BASE}/vehicles", json={"model_id": "nope_x", "home_enu_m": [5.0, 0.0, 0.0]})
    assert r.status_code == 404 and r.json()["code"] == 520
    _wait_ready(e, vid)
    lst = e.c.get(f"{BASE}/vehicles").json()["items"]
    inst = json.loads((ROOT / "packages/contracts/vehicle/instance.schema.json").read_text(encoding="utf-8"))
    allowed = set(inst["properties"])
    assert len(lst) == 1 and set(lst[0]) <= allowed and set(inst["required"]) <= set(lst[0])
    it = lst[0]
    assert (it["model_id"], it["category"], it["label"], it["manual"], it["work"]) == ("p600_mid360", "multirotor", "A-01",
                                                                                      False, None)
    st = e.c.get(f"{BASE}/vehicles/{vid}/state").json()
    assert set(st) == {"t_sim_ns", "state", "state_ext", "fw"} and st["fw"] is None
    assert st["state"]["limits_profile"] == "p600_std" and st["state"]["model_version"] == "2.0.0"
    assert st["state"]["frame_kind"] == 0 and len(st["state"]["q_xyzw"]) == 4 and "lifecycle" in st["state_ext"]
    sen = e.c.get(f"{BASE}/vehicles/{vid}/sensors").json()
    assert set(sen) <= {"sensors", "payload"} and isinstance(sen["sensors"], list)
    r = e.c.patch(f"{BASE}/vehicles/{vid}", json={"label": "A-02", "limits_profile": "px4_default"})
    assert r.status_code == 200 and r.json()["label"] == "A-02"
    assert e.h.core.instance_items(vid)[0]["label"] == "A-02"
    r = e.c.post(f"{BASE}/vehicles/{vid}:acquire", json={"mode": "command"})
    assert r.status_code == 200 and r.json()["status"] == "accepted", r.text
    assert r.json()["data"]["lease"]["owner"] == "OPERATOR"
    r = e.c.post(f"{BASE}/vehicles/{vid}/commands", json={"op": "takeoff", "args": {"alt_m": 8.0}})
    assert r.status_code == 200 and r.json()["status"] == "accepted", r.text
    r = e.c.post(f"{BASE}/vehicles/{vid}/commands", json={"op": "acquire"})
    assert r.status_code == 422
    r = e.c.post(f"{BASE}/vehicles/{vid}:release", json={})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    r = e.c.delete(f"{BASE}/vehicles/{vid}")
    assert r.status_code == 202 and r.json()["lifecycle"] in ("DRAINING", "STOPPED")
    assert e.c.get(f"{BASE}/vehicles/nope-01").status_code == 404


def test_sandbox_vehicle_limit(env) -> None:
    e = env
    e.h.core.limits["vehicles"] = 2
    for k in range(2):
        assert e.c.post(f"{BASE}/vehicles", json={"model_id": "x500", "home_enu_m": [10.0 * k, 0.0, 0.0]}).status_code == 201
    r = e.c.post(f"{BASE}/vehicles", json={"model_id": "x500", "home_enu_m": [30.0, 0.0, 0.0]})
    assert r.status_code == 409 and r.json()["code"] == 505


def test_routes_in_sandbox_design_table() -> None:
    sys.path.insert(0, str(ROOT / "tools" / "contracts"))
    import sandbox_api as SA

    want = {(ep.method.upper(), ep.path) for ep in SA.ENDPOINTS if "rest/fleet.py" in ep.owner}
    got = set()
    for r in F.sandbox_router.routes:
        for m in r.methods:
            got.add((m, "/sessions/{sid}" + r.path))
    assert want == got, (want ^ got)
