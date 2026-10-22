import copy

import numpy as np
import pytest
from lifelib import ring_population

from awr.sim.activities.life import VERSION, LifeWorld


def test_whole_population_updates_and_moves_without_observation():
    world = LifeWorld(ring_population(48))
    world.advance(120)
    audit = world.audit()
    assert all(a["updates"] == 1200 for a in audit["actors"].values())
    assert all(a["distance_m"] > 100 and a["visits"] >= 2 for a in audit["actors"].values())
    assert audit["min_clearance_m"] > 4
    # Work does not depend on which target is sampled or visible.
    before = world.audit()
    for _ in range(100):
        world.sample("a000")
    assert before == world.audit()


def test_replay_clock_chunking_and_reset_are_identical():
    recipe = ring_population(3)
    one, chunked = LifeWorld(recipe), LifeWorld(recipe)
    one.advance(150)
    for t in np.arange(0.13, 150, 0.13):
        chunked.advance(float(t))
    chunked.advance(150)
    assert one.audit() == chunked.audit()
    for key in one.actors:
        np.testing.assert_array_equal(one.sample(key)[0], chunked.sample(key)[0])
    chunked.advance(0)
    chunked.advance(150)
    assert one.audit() == chunked.audit()


def test_portal_preserves_identity_and_location_and_resumes():
    world = LifeWorld(ring_population(1))
    hidden = []
    for t in np.arange(0, 90, 0.1):
        world.advance(float(t))
        p, _, _, s = world.sample("a000")
        if s["hidden"]:
            hidden.append(p)
    assert len(hidden) >= 45
    assert all(np.array_equal(p, [40, 0, 0]) for p in hidden)
    assert world.actors["a000"].visits >= 2
    assert any(e["kind"] == "exit" for e in world.events)


def test_unverified_portal_and_unreachable_itinerary_fail_closed():
    recipe = ring_population(1)
    bad = copy.deepcopy(recipe)
    bad["facilities"]["a000-1"]["portal_verified"] = False
    with pytest.raises(ValueError, match="portal"):
        LifeWorld(bad)
    bad = copy.deepcopy(recipe)
    del bad["edges"]["a000-0"]
    with pytest.raises(ValueError, match="route"):
        LifeWorld(bad)


def test_red_light_waits_then_all_actors_resume():
    recipe = ring_population(5)
    for e in recipe["edges"].values():
        e["signal"] = {"period_s": 30, "green_s": [10, 30]}
    world = LifeWorld(recipe)
    world.advance(9)
    assert all(a.distance == 0 and a.updates == 90 for a in world.actors.values())
    world.advance(25)
    assert all(a.distance > 10 for a in world.actors.values())
    assert all(a.reason_seconds["red signal"] >= 9 for a in world.actors.values())


def test_follower_yields_without_freezing_the_leader():
    recipe = ring_population(1)
    recipe["nodes"]["mid"] = {"pos": [12, 0, 0]}
    recipe["edges"]["a000-0"]["to"] = "mid"
    recipe["edges"]["a000-0"]["points"] = [[0, 0, 0], [12, 0, 0]]
    recipe["edges"]["mid"] = {
        **recipe["edges"]["a000-0"],
        "from": "mid",
        "to": "a000-1",
        "points": [[12, 0, 0], [40, 0, 0]],
    }
    follower = recipe["actors"][0]
    follower.update(id="a-follower", speed_mps=2.0)
    leader = copy.deepcopy(follower)
    leader.update(id="z-leader", start="mid", speed_mps=1.0)
    recipe["actors"].append(leader)
    world = LifeWorld(recipe)
    world.advance(150)
    assert world.actors["z-leader"].distance > 110
    assert world.actors["a-follower"].distance > 110
    assert world.min_clearance_m >= 0.05 - 1e-8
    assert max(a.max_unplanned_wait_s for a in world.actors.values()) < 20


@pytest.mark.parametrize("reservation", ["exclusive", "compatible", "uncontrolled"])
def test_crossing_reservation_is_safe_and_both_flows_progress(reservation):
    nodes = {k: {"pos": p} for k, p in {"w": [-15, 0, 0], "e": [15, 0, 0], "s": [0, -15, 0], "n": [0, 15, 0]}.items()}
    facilities = {k: {"node": k, "label": k, "profiles": ["vehicle"], "capacity": 1, "indoor": False} for k in nodes}
    edges = {
        a + b: {
            "from": a,
            "to": b,
            "points": [nodes[a]["pos"], nodes[b]["pos"]],
            "profiles": ["vehicle"],
            "verified": True,
            "clearance_m": 3,
            "speed_mps": 5,
            "junction": "crossing",
        }
        for a, b in [("w", "e"), ("e", "w"), ("s", "n"), ("n", "s")]
    }
    actors = [
        {
            "id": a,
            "start": a,
            "profile": "vehicle",
            "radius_m": 2.5,
            "speed_mps": 4,
            "itinerary": [
                {"facility": b, "dwell_s": 3, "purpose": "service"},
                {"facility": a, "dwell_s": 3, "purpose": "return"},
            ],
        }
        for a, b in [("w", "e"), ("s", "n")]
    ]
    for e in edges.values():
        if reservation == "uncontrolled":
            e.pop("junction")
        if reservation == "compatible":
            e["compatible_group"] = "same signal phase"
    world = LifeWorld(
        {
            "version": VERSION,
            "status": "verified",
            "seed": 3,
            "nodes": nodes,
            "edges": edges,
            "facilities": facilities,
            "actors": actors,
        }
    )
    world.advance(180)
    assert all(a.visits >= 5 and a.updates == 1800 for a in world.actors.values())
    assert world.min_clearance_m > 0.05
    assert max(a.max_unplanned_wait_s for a in world.actors.values()) < 20


def test_vehicle_does_not_stop_at_an_artificial_straight_lane_split():
    doc = ring_population(1)
    actor = doc["actors"][0]
    actor.update(profile="vehicle", **{"class": "vehicle"})
    for f in doc["facilities"].values():
        f["profiles"] = ["vehicle"]
    for e in doc["edges"].values():
        e["profiles"] = ["vehicle"]
    split = copy.deepcopy(doc)
    split["nodes"]["split"] = {"pos": [10, 0, 0]}
    edge = split["edges"]["a000-0"]
    edge.update(to="split", points=[[0, 0, 0], [10, 0, 0]])
    split["edges"]["continuation"] = {**edge, "from": "split", "to": "a000-1", "points": [[10, 0, 0], [40, 0, 0]]}
    whole, parts = LifeWorld(doc), LifeWorld(split)
    whole.advance(10)
    parts.advance(10)
    assert abs(whole.actors["a000"].distance - parts.actors["a000"].distance) < 0.16
    assert parts.actors["a000"].speed > 1.4
