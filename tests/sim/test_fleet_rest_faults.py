"""REST R16、R62（故障注入与清除，D1-ext）改经 `ctl/sim-core/cmd` 的 `fault/inject`、`fault/clear`（FX-SIM2；ADR-058 第 8 条）：
TestClient + 网关与总线替身，断言转发的命令形状、admission 的 `detail.result` 映射为 202 `{fault_id, apply_tick}`、非席位 409 116、
生产者拒绝码按 HTTP 映射（107 → 404）。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from awr.api.problem import ApiProblem, problem
from awr.api.rest import fleet as F
from awr.api.security import ApiPrincipal
from awr.contracts import bus_keys


class Bus:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    async def call(self, key, msg, *, timeout=1.0, retries=0, retry_gap=0.3):
        self.sent.append((key, msg))
        if msg.get("uav") == "nope":
            return {"v": 1, "cid": msg["cid"], "status": "rejected", "code": 107, "detail": {"uav": "nope"}}
        if msg["op"] == "fault/inject":
            return {"v": 1, "cid": msg["cid"], "status": "accepted", "code": 0, "apply_tick": 11,
                    "detail": {"result": {"fault_id": "f000001", "apply_tick": 11}}}
        return {"v": 1, "cid": msg["cid"], "status": "accepted", "code": 0, "apply_tick": 12, "detail": {"result": {"apply_tick": 12}}}


class Gw:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(producer="sim-core")
        self.clock = SimpleNamespace(global_epoch=1)
        self.tokens = SimpleNamespace(sign_principal=lambda pid, role, cid, conn_id=None, seat=False:
                                      {"principal_id": pid, "role": role, "entry": "api", "seat": seat, "sig": "x"})

    def is_seat_holder(self, pid: str) -> bool:
        return pid == "p-op"


def make_app(bus: Bus) -> FastAPI:
    app = FastAPI()
    app.include_router(F.router)

    @app.exception_handler(ApiProblem)
    async def _problem(request: Request, exc: ApiProblem):
        return problem(exc.code, status=exc.status, detail=exc.detail, headers=exc.headers)

    @app.middleware("http")
    async def auth(request: Request, call_next):
        tok = request.headers.get("Authorization", "")
        if tok.startswith("Bearer "):
            role, pid = tok[7:].split(":")
            request.state.principal = ApiPrincipal(pid, role, "jti", 0, "loopback")
        return await call_next(request)

    app.state.awr = SimpleNamespace(gateway=Gw(), bus=bus)
    return app


def test_fault_routes_forward_commands() -> None:
    bus = Bus()
    c = TestClient(make_app(bus))
    h = {"Authorization": "Bearer operator:p-op"}
    r = c.post("/api/fleet/vehicles/p600-01/faults", json={"kind": "thrust_loss", "params": {"frac": 0.2}, "duration_s": 3},
               headers=h)
    assert r.status_code == 202 and r.json() == {"fault_id": "f000001", "apply_tick": 11}, r.text
    key, msg = bus.sent[-1]
    assert key == bus_keys.ctl_cmd("sim-core") and msg["op"] == "fault/inject" and msg["uav"] == "p600-01"
    assert msg["args"] == {"kind": "thrust_loss", "params": {"frac": 0.2}, "at_s": None, "duration_s": 3.0}
    assert msg["principal"]["principal_id"] == "p-op" and msg["cid"].startswith("rf-fault-")
    r = c.delete("/api/fleet/vehicles/p600-01/faults/f000001", headers=h)
    assert r.status_code == 204 and bus.sent[-1][1]["op"] == "fault/clear" and bus.sent[-1][1]["args"] == {"fault_id": "f000001"}
    r = c.post("/api/fleet/vehicles/p600-01/faults", json={"kind": "thrust_loss"}, headers={"Authorization": "Bearer operator:p-x"})
    assert r.status_code == 409 and r.json()["code"] == 116
    r = c.post("/api/fleet/vehicles/nope/faults", json={"kind": "thrust_loss"}, headers=h)
    assert r.status_code == 404 and r.json()["code"] == 107
