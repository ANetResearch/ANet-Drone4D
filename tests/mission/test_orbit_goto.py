"""运动提供者：orbit（M10-FR-013；M10-AC-007）、goto_route（M10-FR-014；M10-AC-008）、follow_path（FR-012）。

FleetSim L1 实跑（合成小世界）；调用生命周期与完成判据由 M08 CommandEngine 判定。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from harness import Sim

from awr.world.geometry.fake import fake_world_query


@pytest.fixture
def sim(tmp_path):
    s = Sim(fake_world_query(tmp_path), n=1)
    s.advance(1.0)
    vid = s.ids[0]
    assert s.cmd("takeoff", {"alt_m": 30}, vid, "to")["status"] == "accepted"
    assert s.until(lambda: s.results.get("to", ("",))[0] == "succeeded", 60.0)
    yield s
    s.close()


@pytest.mark.parametrize("R,v,turns", [(20.0, 4.0, 1.0), (60.0, 6.0, 0.5)])
def test_orbit_turns_radius_speed(sim: Sim, R: float, v: float, turns: float) -> None:
    vid = sim.ids[0]
    c = np.array([-150.0, -60.0, 45.0])
    assert sim.cmd("orbit", {"center": c.tolist(), "radius_m": R, "speed_mps": v, "turns": turns, "cw": False}, vid,
                   "orb")["status"] == "accepted"
    S = sim.core.fleet.S
    s = sim.slot(vid)
    B = sim.rt.tracker.B
    rerr, verr = [], []
    ok = sim.until(lambda: B["kind"][s] == 2 and B["orb"][s, 4] >= 0.99 * v / R, 120.0, 0.25)
    assert ok
    for _ in range(int(2 * math.pi * R * turns / v * 0.6 / 0.5)):
        sim.advance(0.5)
        if B["kind"][s] != 2 or B["orb"][s, 5] == 0.0:
            break
        p = S.enu.pos[s]
        rerr.append(abs(np.hypot(p[0] - c[0], p[1] - c[1]) - R))
        verr.append(abs(np.linalg.norm(S.enu.vel[s][:2]) - v))
    assert rerr and max(rerr) <= 1.0 and max(verr) <= 0.5
    assert sim.until(lambda: "orb" in sim.results, 300.0)
    st = sim.results["orb"]
    assert st[0] == "succeeded", st
    assert B["kind"][s] == 0
    assert math.degrees(abs(sim.rt.tracker.stats["handovers"] and 0.0)) == 0.0
    ev = [e for e in sim.kinds("path.changed")]
    assert ev and ev[-1][2]["vehicle_id"] == vid


def test_orbit_accumulated_angle_error_under_5deg(sim: Sim) -> None:
    vid = sim.ids[0]
    s = sim.slot(vid)
    B = sim.rt.tracker.B
    c = [-150.0, -60.0, 45.0]
    sim.cmd("orbit", {"center": c, "radius_m": 15.0, "speed_mps": 3.0, "turns": 2.0, "cw": True}, vid, "orb2")
    acc = []

    def watch() -> bool:
        if B["kind"][s] == 2:
            acc.append(float(B["orb"][s, 7]))
        return "orb2" in sim.results

    assert sim.until(watch, 200.0, 0.1)
    assert sim.results["orb2"][0] == "succeeded"
    assert abs(math.degrees(max(acc) - 2 * 2 * math.pi)) <= 5.0


def test_goto_route_auto_direct_and_safe_transit(sim: Sim) -> None:
    vid = sim.ids[0]
    goal = [-100.0, -20.0, 30.0]
    sim.cmd("goto", {"pos": goal, "route": "auto", "speed_mps": 5.0}, vid, "g1")
    assert sim.until(lambda: "g1" in sim.results, 60.0)
    assert sim.results["g1"][0] == "succeeded" and np.linalg.norm(sim.pos(vid) - goal) < 3.0
    # 目标越过塔（中心 (−40, −20)，高 100 m）：safe_transit 剖面，全程高于 Height_map
    goal2 = [30.0, -20.0, 40.0]
    sim.cmd("goto", {"pos": goal2, "route": "safe_transit", "speed_mps": 5.0}, vid, "g2")
    zmax = [0.0]

    def track() -> bool:
        zmax[0] = max(zmax[0], float(sim.pos(vid)[2]))
        return "g2" in sim.results

    assert sim.until(track, 200.0, 0.5)
    assert sim.results["g2"][0] == "succeeded", sim.results["g2"]
    assert np.linalg.norm(sim.pos(vid) - goal2) < 3.0 and zmax[0] >= 110.0
    ready = [e for e in sim.kinds("plan.ready") if e[2].get("kind") == "safe_transit"]
    assert ready and ready[-1][2]["planner"] == "profile"


def test_goto_route_goal_in_obstacle_rejected(sim: Sim) -> None:
    vid = sim.ids[0]
    sim.cmd("goto", {"pos": [-40.0, -20.0, 50.0], "route": "safe_transit"}, vid, "g3")     # 塔体内
    assert sim.until(lambda: "g3" in sim.results, 30.0)
    assert sim.results["g3"][:2] == ("failed", 102)


def test_operator_follow_path_planned_then_executed(sim: Sim) -> None:
    vid = sim.ids[0]
    wp = [[-150.0, -60.0, 40.0], [-60.0, -60.0, 40.0]]
    sim.cmd("follow_path", {"waypoints": wp, "speed_mps": 5.0}, vid, "fp")
    assert sim.until(lambda: "fp" in sim.results, 120.0)
    # 规划结果生效（plan.ready）之前调用停在 accepted（fine_pending），之后才 running
    t_ready = next(e[0] for e in sim.kinds("plan.ready") if e[2].get("kind") == "follow_path")
    t_run = next(e[0] for e in sim.kinds("cmd.running") if e[2].get("cid") == "fp")
    assert t_ready <= t_run
    assert sim.results["fp"][0] == "succeeded" and np.linalg.norm(sim.pos(vid) - wp[-1]) < 1.0
    bad = [[-150.0, -60.0, 40.0], [-20.0, -20.0, 40.0]]                                   # 穿塔：细校验 102
    sim.cmd("follow_path", {"waypoints": bad}, vid, "fp2")
    assert sim.until(lambda: "fp2" in sim.results, 30.0)
    assert sim.results["fp2"][:2] == ("failed", 102)
