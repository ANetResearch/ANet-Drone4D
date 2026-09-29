"""Mission 与 Track 状态机（M10-FR-002、FR-003、FR-007、FR-009、FR-054；M10-AC-002、AC-003 principal、AC-010）。

合成小世界上以真实 SimCore（FleetSim L1、CommandEngine 准入 ④–⑩）驱动：全部内部调用的 principal 为 `mission:<mid>`。
"""

from __future__ import annotations

import numpy as np
import pytest
from harness import Sim

from awr.contracts.enums import Owner
from awr.world.geometry.fake import fake_world_query

FP = {"waypoints_enu_m": [[-150.0, -60.0, 40.0], [-60.0, -60.0, 40.0], [-60.0, -120.0, 40.0]], "speed_mps": 6.0}


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    yield s
    s.close()


def _create(sim: Sim, mid: str = "m-fp", params: dict | None = None, **kw) -> str:
    spec = {"mission_id": mid, "generator": "follow_path", "vehicle_ids": [sim.ids[0]], "params": params or FP} | kw
    sim.rt.missions.create(spec, "operator")
    assert sim.until(lambda: sim.rt.missions.missions[mid].gen == "ok", 5.0, 0.1)
    return mid


def test_full_lifecycle_and_projection(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    assert eng.start(mid)["code"] == 0 and eng.missions[mid].state == "RUNNING"
    vid = sim.ids[0]
    slot = sim.slot(vid)
    B = sim.rt.tracker.B
    assert sim.until(lambda: eng.missions[mid].tracks[vid].state == "WORKING", 120.0)
    assert int(B["mission_item"][slot]) == 0 and int(B["track_state"][slot]) == 2
    assert sim.until(lambda: eng.missions[mid].state == "DONE", 200.0)
    assert eng.missions[mid].tracks[vid].state == "DONE" and not eng.missions[mid].incomplete
    assert int(B["mission_item"][slot]) == 0xFFFF
    seq = [e[2]["to"] for e in sim.kinds("track.state")]
    assert seq == ["TRANSIT", "WORKING", "RETURNING", "DONE"]
    ms = [e[2]["to"] for e in sim.kinds("mission.state")]
    assert ms == ["RUNNING", "DONE"]
    # 全部内部调用经准入，principal 为 mission:<mid>；返航后租约已释放
    acc = [e for e in sim.kinds("cmd.accepted") if (e[2].get("cid") or "").startswith("m10:")]
    assert {e[2]["op"] for e in acc} >= {"takeoff", "follow_path", "rtl"}
    assert sim.core.lease.lease_json(slot)["owner"] == "NONE"
    assert np.linalg.norm(sim.pos(vid)[:2] - sim.core.fleet.S.enu.home[slot][:2]) < 2.0


def test_start_guards(sim: Sim) -> None:
    eng = sim.rt.missions
    assert eng.start("nope")["code"] == 305
    mid = _create(sim)
    eng.missions[mid].state = "DONE"
    assert eng.start(mid)["code"] == 105
    assert eng.pause(mid)["code"] == 105 and eng.resume(mid)["code"] == 105


def test_pause_resume_keeps_path(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    vid = sim.ids[0]
    eng.start(mid)
    assert sim.until(lambda: eng.missions[mid].tracks[vid].state == "WORKING", 120.0)
    sim.advance(5.0)
    assert eng.pause(mid)["code"] == 0 and eng.missions[mid].state == "PAUSED"
    sim.advance(6.0)
    s = sim.slot(vid)
    assert np.linalg.norm(sim.core.fleet.S.enu.vel[s]) < 0.3
    p_pause = sim.pos(vid)
    sim.advance(10.0)
    assert np.linalg.norm(sim.pos(vid) - p_pause) < 0.3            # 停住，不偏离
    assert eng.resume(mid)["code"] == 0
    assert sim.until(lambda: eng.missions[mid].state == "DONE", 200.0)


def test_abort_hovers_and_releases(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    vid = sim.ids[0]
    eng.start(mid)
    assert sim.until(lambda: eng.missions[mid].tracks[vid].state == "WORKING", 120.0)
    assert eng.abort(mid)["code"] == 0
    assert eng.missions[mid].state == "ABORTED" and eng.missions[mid].tracks[vid].state == "DROPPED"
    sim.advance(4.0)
    s = sim.slot(vid)
    assert sim.core.lease.lease_json(s)["owner"] == "NONE"
    assert np.linalg.norm(sim.core.fleet.S.enu.vel[s]) < 0.5 and sim.core.fleet.S.enu.pos[s][2] > 10.0


def test_operator_takeover_suspends_then_resumes(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    vid = sim.ids[0]
    eng.start(mid)
    assert sim.until(lambda: eng.missions[mid].tracks[vid].state == "WORKING", 120.0)
    sim.advance(4.0)
    p = sim.pos(vid)
    adm = sim.cmd("goto", {"pos": [float(p[0]), float(p[1]) + 10.0, float(p[2])], "route": "direct"}, vid, "op-goto")
    assert adm["status"] == "accepted"
    sim.advance(0.5)
    t = eng.missions[mid].tracks[vid]
    assert t.state == "SUSPENDED" and t.resume is not None and t.resume["tau_s"] > 0
    assert sim.until(lambda: sim.results.get("op-goto", ("",))[0] == "succeeded", 30.0)
    s = sim.slot(vid)
    assert sim.core.lease.lease_json(s)["owner"] == "OPERATOR"
    rel = sim.core._lease_op({"v": 1, "cid": "rel", "op": "release", "uav": vid, "return_to": "previous",
                              "principal": {"principal_id": "p-op", "role": "operator", "entry": "api", "seat": True}})
    assert rel["code"] == 0 and sim.core.lease.lease_json(s)["owner"] == "MISSION"
    assert sim.until(lambda: t.state in ("TRANSIT", "WORKING"), 5.0)
    assert sim.until(lambda: eng.missions[mid].state == "DONE", 240.0)


def test_all_tracks_dropped_aborts(sim: Sim) -> None:
    mid = _create(sim)
    eng = sim.rt.missions
    vid = sim.ids[0]
    eng.start(mid)
    assert sim.until(lambda: eng.missions[mid].tracks[vid].state == "WORKING", 120.0)
    sim.core.engine.resolve_calls(np.array([sim.slot(vid)]), "failed", 208, "test crash")
    sim.advance(0.3)
    assert eng.missions[mid].tracks[vid].state == "DROPPED" and eng.missions[mid].state == "ABORTED"
    assert sim.kinds("mission.aborted")


def test_energy_reject_and_warn(sim: Sim) -> None:
    eng = sim.rt.missions
    vid = sim.ids[0]
    s = sim.slot(vid)
    sim.core.fleet.S.blocks["battery"]["soc"][s] = 0.22
    big = {"polygon_enu_m": [[-160, -110], [40, -110], [40, 110], [-160, 110]],
           "altitude": {"mode": "fly_over", "agl_m": 20}, "spacing_m": 8.0}
    eng.create({"mission_id": "m-big", "generator": "lawnmower", "vehicle_ids": [vid], "params": big}, "scenario",
               precheck="reject")
    assert sim.until(lambda: eng.missions["m-big"].gen != "pending", 20.0, 0.1)
    assert eng.missions["m-big"].gen == "ok", eng.missions["m-big"].gen_error
    r = eng.start("m-big")
    assert r["code"] == 119 and r["detail"]["vehicles"][0]["id"] == vid and eng.missions["m-big"].state == "IDLE"
    eng.create({"mission_id": "m-warn", "generator": "lawnmower", "vehicle_ids": [vid], "params": big}, "operator")
    assert sim.until(lambda: eng.missions["m-warn"].gen == "ok", 20.0, 0.1)
    assert eng.start("m-warn")["code"] == 0 and sim.kinds("mission.energy_warning")
    _ = Owner
