import copy

import numpy as np
import pytest

from awr.sim.activities.plans import ActivityPlan, compile_plan, digest, shortest_path


def network():
    nodes = {
        str(i): {"pos": p, "label": f"Corner {i}", "destination": True, "purpose": "rest"}
        for i, p in enumerate(([0, 0, 0], [120, 0, 0], [120, 120, 0], [0, 120, 0]))
    }
    return {
        "status": "verified",
        "world_id": "test",
        "world_content_version": "v1",
        "coordinate_sha256": "a" * 64,
        "radius_m": 0.45,
        "height_m": 1.9,
        "nodes": nodes,
        "edges": [
            {"from": str(i), "to": str((i + 1) % 4), "verified": True, "profile": "person", "bidirectional": True}
            for i in range(4)
        ],
    }


def test_seed_is_per_actor_reproducible_and_sensitive():
    n = network()
    a = compile_plan(n, "resident-1", 42, "0")
    compile_plan(n, "unrelated", 42, "2")
    assert digest(a) == digest(compile_plan(n, "resident-1", 42, "0"))
    assert digest(a) != digest(compile_plan(n, "resident-1", 43, "0"))
    assert len(a["visits"]) >= 3 and a["distance_m"] > 700
    assert all(v["node"] != w["node"] for v, w in zip(a["visits"], a["visits"][1:], strict=False))


def test_rejects_unreachable_unverified_and_wrong_profile():
    n = network()
    n["edges"] = []
    with pytest.raises(ValueError, match="No confirmed"):
        shortest_path(n, "0", "1")
    with pytest.raises(ValueError, match="reachable"):
        compile_plan(n, "a", 1, "0")
    n = network()
    for e in n["edges"]:
        e["profile"] = "vehicle"
    with pytest.raises(ValueError, match="No confirmed"):
        shortest_path(n, "0", "1")
    n["status"] = "unverified"
    with pytest.raises(ValueError, match="Unverified"):
        compile_plan(n, "a", 1, "0")


def test_absolute_time_and_continuity_no_loop_or_teleport():
    p = ActivityPlan(compile_plan(network(), "a", 1, "0"))
    times = np.arange(0, 1900, 0.1)
    positions = p.positions(times)
    assert np.max(np.linalg.norm(np.diff(positions, axis=0), axis=1)) <= 0.150001
    for t in (0, 12.5, 100, 700, 1500, 1900):
        a = p.sample(t)
        p.sample(30)  # out-of-order observer / checkpoint does not advance a private clock
        np.testing.assert_array_equal(a[0], p.sample(t)[0])
        np.testing.assert_allclose(a[0], p.positions([t])[0])
    for s in p.segments:
        np.testing.assert_allclose(p.sample(s["t1"] - 1e-6)[0], p.sample(s["t1"])[0], atol=1e-5)
    assert p.sample(p.doc["duration_s"] + 100)[3]["phase"] == "complete"
    np.testing.assert_array_equal(p.sample(1e5)[0], p.segments[-1]["q"])


def test_plan_integrity_rejects_discontinuities():
    doc = compile_plan(network(), "a", 1, "0")
    bad = copy.deepcopy(doc)
    bad["segments"][1]["p"][0] += 10
    with pytest.raises(ValueError, match="Discontinuous"):
        ActivityPlan(bad)


def test_ground_profile_follows_surface_in_both_directions():
    n = network()
    for edge in n["edges"]:
        edge["ground_profile"] = [[0, 0], [0.25, -0.15], [0.75, -0.15], [1, 0]]
    doc = compile_plan(n, "a", 1, "0")
    plan = ActivityPlan(doc)
    walk = next(s for s in plan.segments if s["phase"] == "walk")
    t = (walk["t0"] + walk["t1"]) / 2
    pos, _, velocity, _ = plan.sample(t)
    assert pos[2] == pytest.approx(-0.15)
    assert velocity[2] == pytest.approx(0)
    np.testing.assert_allclose(pos, plan.positions([t])[0])
    for s in plan.segments:
        np.testing.assert_allclose(plan.sample(s["t1"] - 1e-7)[0], plan.sample(s["t1"])[0], atol=1e-6)
    bad = copy.deepcopy(doc)
    next(s for s in bad["segments"] if s["phase"] == "walk")["ground_profile"][0][1] = 5
    with pytest.raises(ValueError, match="ground profile"):
        ActivityPlan(bad)
    bad = copy.deepcopy(doc)
    bad["segments"][1]["t0"] += 1
    with pytest.raises(ValueError, match="Discontinuous"):
        ActivityPlan(bad)
