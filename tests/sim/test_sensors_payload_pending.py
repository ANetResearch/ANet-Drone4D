"""SB-34 的载荷状态在 M13 装配之前（issue #60；M08-FR-227；17 §17.6.3）。

M13 在 sensors stage 中绑定状态块并装配 slot，装配之前没有载荷状态（新会话认领前、暂停中添加、驻留恢复后的首个 stage 之前）：

- sim-core 的 `fleet/vehicle_sensors` 回复 `payload_pending`（只在总线回复中，HTTP 正文仍只有契约字段）；装配之后回复带 `payload`；
- SB-34 路由在 `payload_pending` 时重查直到装配完成，至多 `PAYLOAD_WAIT_S`；会话已认领且时钟暂停时不等待。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from simlib import CoreHarness

from awr.api.rest import fleet as F
from awr.api.security import ApiPrincipal
from awr.contracts.enums import TimeState
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
SID = "sb-k3m7q2x7ab"
BASE = f"/api/sandbox/v1/sessions/{SID}"


def test_vehicle_sensors_reports_pending_until_m13_assembles() -> None:
    from awr.sim.sensors import plugin

    inst = json.loads((ROOT / "packages/contracts/vehicle/instance.schema.json").read_text(encoding="utf-8"))
    allowed = set(inst["$defs"]["sensors"]["properties"])
    with R.isolated_registry() as reg:
        plugin.install()
        try:
            h = CoreHarness(n=0, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, ready=False)
            try:
                code, rep = h.core.admit_add({"model_id": "p600_mid360", "pos_enu_m": [0.0, 0.0, 0.0], "label": "A"})
                assert code == 0, rep
                vid = rep["id"]
                q = h.core._local_query("fleet/vehicle_sensors", {"id": vid})  # 尚无 sensors stage
                assert q["code"] == 0 and q.get("payload_pending") is True, q
                assert "payload" not in q["item"] and set(q["item"]) <= allowed
                h.advance(0.1)
                q = h.core._local_query("fleet/vehicle_sensors", {"id": vid})
                assert q["code"] == 0 and "payload_pending" not in q, q
                assert set(q["item"]) <= allowed and q["item"]["payload"]["uav"] == vid
                assert set(q["item"]["payload"]["gimbal"]) >= {"pitch_rad", "yaw_rad", "mode"}
            finally:
                h.close()
        finally:
            plugin.uninstall()


class _Seq:
    """`_query` 的替身：前 n 次回复 `payload_pending`，之后回复带 payload。"""

    def __init__(self, n: int) -> None:
        self.n = n
        self.calls = 0

    async def __call__(self, request, op: str, args: dict, **kw) -> dict:
        assert op == "fleet/vehicle_sensors" and args == {"id": "p600-01"}
        self.calls += 1
        if self.calls <= self.n:
            return {"v": 1, "code": 0, "item": {"sensors": []}, "payload_pending": True}
        return {"v": 1, "code": 0, "item": {"sensors": [], "payload": {"uav": "p600-01", "gimbal": {}, "sensors": []}}}


def _client(clock_state: int, view_state: str | None) -> TestClient:
    app = FastAPI()

    def principal(request: Request) -> None:
        request.state.principal = ApiPrincipal("p-op", "operator", "j", 0, "public", SID, "sandbox")
        request.state.sandbox_sid = SID

    app.include_router(F.sandbox_router, prefix="/api/sandbox/v1/sessions/{sid}", dependencies=[Depends(principal)])
    gw = SimpleNamespace(clock=SimpleNamespace(state=clock_state, global_epoch=1))
    hub = SimpleNamespace(view=lambda sid: SimpleNamespace(state=view_state) if view_state else None)
    app.state.awr = SimpleNamespace(kind="sandbox", gateway=gw, hub=hub)
    return TestClient(app)


@pytest.mark.parametrize(("clock", "view"), [(TimeState.PLAYING, "ACTIVE"), (TimeState.STOPPED, "READY"),
                                             (TimeState.PAUSED, "READY")])
def test_sb34_waits_for_assembly(monkeypatch, clock, view) -> None:
    q = _Seq(3)
    monkeypatch.setattr(F, "_query", q)
    monkeypatch.setattr(F, "PAYLOAD_POLL_S", 0.001)
    monkeypatch.setattr(F, "ctx_for", lambda request: request.app.state.awr)
    with _client(int(clock), view) as c:
        body = c.get(f"{BASE}/vehicles/p600-01/sensors").json()
    assert body["payload"]["uav"] == "p600-01" and q.calls == 4


def test_sb34_does_not_wait_while_paused_and_claimed(monkeypatch) -> None:
    q = _Seq(1000)
    monkeypatch.setattr(F, "_query", q)
    monkeypatch.setattr(F, "ctx_for", lambda request: request.app.state.awr)
    with _client(int(TimeState.PAUSED), "ACTIVE") as c:
        body = c.get(f"{BASE}/vehicles/p600-01/sensors").json()
    assert body == {"sensors": []} and q.calls == 1


def test_sb34_wait_is_bounded(monkeypatch) -> None:
    q = _Seq(10_000)
    monkeypatch.setattr(F, "_query", q)
    monkeypatch.setattr(F, "PAYLOAD_POLL_S", 0.001)
    monkeypatch.setattr(F, "PAYLOAD_WAIT_S", 0.05)
    monkeypatch.setattr(F, "ctx_for", lambda request: request.app.state.awr)
    with _client(int(TimeState.PLAYING), "ACTIVE") as c:
        body = c.get(f"{BASE}/vehicles/p600-01/sensors").json()
    assert body == {"sensors": []} and 2 <= q.calls < 10_000
