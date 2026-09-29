"""机群增删与只读查询（M08-AC-030、AC-009 的查询部分；M08-FR-015、FR-067、FR-080、FR-081；AWR-12 §5.14）。

REST 路由 `python/awr/api/rest/fleet.py` 属 M11（本工作包经请求文件交付转发约定），这里在 sim-core 侧验证其转发目标：
`ctl/sim-core/cmd` 的 `fleet/add`、`fleet/remove` 与 `ctl/sim-core/query` 的 `fleet/profiles|profile|caps|vehicles`。
- add 后 ≤ 1 s 出现在 roster 快照与 `roster.changed`；生命周期事件 PENDING → STARTING → BOOTED → READY 完整；
- SPAWN_TOO_CLOSE、UNKNOWN_PROFILE、CAPACITY、102（出界 / 禁飞）、ID_EXISTS 各得对应码；非席位持有者 116；
- 地面 remove → STOPPED → REMOVED；空中非强制 remove 进入 DRAINING（以 safety 名义降落）后移除；强制 remove 需确认令牌；
- agent_no 不复用；`fleet/profile` 返回 11 行孪生表与 7 条检查。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.reasons import Reason
from awr.sim.fleet.stages import registry as R


class _Poly:
    def __init__(self, x0: float, x1: float, y0: float, y1: float) -> None:
        self.b = (x0, x1, y0, y1)

    def contains_xy(self, xy: np.ndarray) -> np.ndarray:
        x0, x1, y0, y1 = self.b
        return (xy[:, 0] >= x0) & (xy[:, 0] <= x1) & (xy[:, 1] >= y0) & (xy[:, 1] <= y1)


class FlatWorld:
    """M04 替身：平地、边界 ±1000 m、禁飞区 x ∈ [200, 300]。"""

    def __init__(self) -> None:
        g = SimpleNamespace(a=np.zeros((4, 4), np.float32), x0_m=-2000.0, y0_m=-2000.0, cell_m=1000.0)
        self._g = g
        nofly = _Poly(200, 300, -1000, 1000)
        self.zones = SimpleNamespace(border=_Poly(-1000, 1000, -1000, 1000), nofly=[nofly],
                                     contains=lambda xyz, kinds: np.zeros(len(xyz), bool))

    def dsm_grid(self):
        return self._g

    def dtm_grid(self):
        return self._g

    def height_dsm(self, xy: np.ndarray) -> np.ndarray:
        return np.zeros(len(xy))

    def ground_dtm(self, xy: np.ndarray) -> np.ndarray:
        return np.zeros(len(xy))

    def ready_event(self) -> dict:
        return {}


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        x = CoreHarness(n=2, reg=reg, world=FlatWorld(), spawn_xy=(0.0, 0.0), spacing=10.0)
        ev: list[tuple[str, dict]] = []
        orig = x.core.events.emit

        def rec(kind, **kw):
            ev.append((kind, kw))
            return orig(kind, **kw)

        x.core.events.emit = rec
        x.ev = ev
        try:
            yield x
        finally:
            x.close()


def _add(h: CoreHarness, **args) -> dict:
    return h.cmd("fleet/add", args, uav=None)


def test_add_lifecycle_and_roster(h: CoreHarness) -> None:
    t0 = h.t
    adm = _add(h, profile_id="x500", home_enu_m=[40.0, 0.0, 0.0], vehicle_id="x500-a")
    assert adm["status"] == "accepted", adm
    vid = adm["detail"]["id"]
    assert vid == "x500-a" and adm["detail"]["lifecycle"] == "STARTING"
    snap = h.core.roster.snapshot()
    assert any(v["id"] == vid for v in snap["entries"])
    assert any(k == "roster.changed" for k, _ in h.ev)
    assert h.until(lambda: h.core.roster.resolve(vid).lifecycle == 4, 1.0)
    assert h.t - t0 <= 1.0
    seq = [kw.get("to") for k, kw in h.ev if k == "sim.vehicle.state" and kw.get("uav") == vid]
    assert seq == ["PENDING", "STARTING", "BOOTED", "READY"], seq
    first = next(kw for k, kw in h.ev if k == "sim.vehicle.state" and kw.get("uav") == vid)
    assert first.get("agent_no") is not None and first.get("profile_id") == "x500"
    items = h.core._local_query("fleet/vehicles", {"id": vid})["items"]
    assert items[0]["lifecycle"] == "READY" and items[0]["profile_id"] == "x500"


def test_add_rejections(h: CoreHarness) -> None:
    cases = [
        ({"profile_id": "nope", "home_enu_m": [40.0, 0.0, 0.0]}, int(Reason.PARAM_OUT_OF_RANGE), "UNKNOWN_PROFILE"),
        ({"profile_id": "x500", "home_enu_m": [1.0, 0.0, 0.0]}, int(Reason.PARAM_OUT_OF_RANGE), "SPAWN_TOO_CLOSE"),
        ({"profile_id": "x500", "home_enu_m": [5000.0, 0.0, 0.0]}, int(Reason.GEOFENCE_REJECT), "OUT_OF_BORDER"),
        ({"profile_id": "x500", "home_enu_m": [250.0, 0.0, 0.0]}, int(Reason.GEOFENCE_REJECT), "IN_NOFLY"),
        ({"profile_id": "x500", "home_enu_m": [40.0, 0.0, 0.0], "vehicle_id": h.ids()[0]}, int(Reason.STATE), "ID_EXISTS"),
    ]
    for args, code, why in cases:
        adm = _add(h, **args)
        assert adm["code"] == code and adm["detail"]["why"] == why, (args, adm)
    assert h.cmd("fleet/add", {"profile_id": "x500", "home_enu_m": [60.0, 0.0, 0.0]}, uav=None, pid="p-op2")["code"] == \
        int(Reason.SEAT_TAKEN)


def test_capacity(monkeypatch) -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=3, reg=reg, fleet_cfg={"capacity": 3}, ready=False)
        try:
            adm = _add(h, profile_id="x500", home_enu_m=[500.0, 0.0, 0.0])
            assert adm["code"] == int(Reason.PARAM_OUT_OF_RANGE) and adm["detail"]["why"] == "CAPACITY"
        finally:
            h.close()


def test_remove_ground_air_and_agent_no_not_reused(h: CoreHarness) -> None:
    a, b = h.ids()
    no_a = h.core.roster.resolve(a).agent_no
    adm = h.cmd("fleet/remove", {}, uav=a)
    assert adm["status"] == "accepted" and adm["detail"]["lifecycle"] == "STOPPED"
    h.advance(0.1)
    assert h.core.roster.resolve(a) is None
    assert [kw.get("to") for k, kw in h.ev if k == "sim.vehicle.state" and kw.get("uav") == a][-2:] == ["STOPPED", "REMOVED"]
    h.takeoff(10.0, b)
    assert h.cmd("fleet/remove", {"force": True}, uav=b)["code"] == int(Reason.CONFIRM_REQUIRED)
    adm = h.cmd("fleet/remove", {}, uav=b)
    assert adm["detail"]["lifecycle"] == "DRAINING"
    assert h.until(lambda: h.core.roster.resolve(b) is None, 60.0)
    assert "DRAINING" in [kw.get("to") for k, kw in h.ev if k == "sim.vehicle.state" and kw.get("uav") == b]
    adm = _add(h, profile_id="x500", home_enu_m=[40.0, 0.0, 0.0])
    assert adm["status"] == "accepted"
    assert adm["detail"]["agent_no"] > no_a and adm["detail"]["agent_no"] not in (no_a, no_a + 1)


def test_profile_queries(h: CoreHarness) -> None:
    items = h.core._local_query("fleet/profiles", {})["items"]
    assert {"x500", "x500_sih", "p600_mid360"} <= {i["profile_id"] for i in items}
    d = h.core._local_query("fleet/profile", {"profile_id": "p600_mid360"})["item"]
    assert len(d["twin"]) == 11 and len(d["checks"]) == 7
    assert h.core._local_query("fleet/profile", {"profile_id": "zz"})["code"] == int(Reason.NOT_FOUND)
    caps = h.core._local_query("fleet/caps", {})["items"]
    assert set(caps) == {"mock", "replay"}
