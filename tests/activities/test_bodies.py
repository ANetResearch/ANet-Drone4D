import math
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sim.activities.bodies import PopulationBodies, centers, clearance, discs


def vehicle(length=12.2, width=3.1, pos=(0, 0, 0), yaw=0):
    spec = {
        "class": "vehicle",
        "radius_m": math.hypot(length, width) / 2 + 0.025,
        "body_m": {"length": length, "width": width},
    }
    return SimpleNamespace(spec=spec, pos=np.array(pos, dtype=float), yaw=yaw)


def test_sphere_row_covers_entire_rectangle_including_corners():
    a = vehicle()
    offsets, radius = discs(a.spec)
    grid = np.array([[x, y, 0] for x in np.linspace(-6.1, 6.1, 101) for y in np.linspace(-1.55, 1.55, 31)])
    assert (
        np.max(np.min(np.linalg.norm(grid[:, None] - centers(a.pos, a.yaw, offsets)[None, :], axis=2), axis=1)) < radius
    )


def test_long_parallel_buses_can_pass_and_keep_real_body_gap():
    a, b = vehicle(), vehicle(pos=(0, 4.5, 0), yaw=math.pi)
    assert clearance(a.spec, a.pos, a.yaw, b.spec, b.pos, b.yaw) > 1
    bodies = PopulationBodies([a, b])
    gaps = bodies.gaps([[20, 0, 0], [-20, 4.5, 0]], [0, math.pi])
    assert gaps[0, 1] > 1
    assert np.isinf(gaps[0, 0])


def test_swept_translation_detects_crossing_between_endpoints():
    a, b = vehicle(pos=(-20, 0, 0)), vehicle(pos=(0, -20, 0), yaw=math.pi / 2)
    bodies = PopulationBodies([a, b])
    assert bodies.gaps([[20, 0, 0], [0, 20, 0]], [0, math.pi / 2])[0, 1] < 0
    assert bodies.gaps([[20, 0, 0], [0, 20, 0]], [0, math.pi / 2], swept=False)[0, 1] > 0


def test_rotation_sweep_is_conservative_against_dense_reference():
    a, b = vehicle(), vehicle(length=1, width=1, pos=(4, 4, 0))
    bodies = PopulationBodies([a, b])
    swept = bodies.gaps([a.pos, b.pos], [math.pi / 2, 0])[0, 1]
    sampled = min(clearance(a.spec, a.pos, yaw, b.spec, b.pos, 0) for yaw in np.linspace(0, math.pi / 2, 300))
    assert swept <= sampled + 1e-8
    assert sampled < 0


def test_bad_body_cannot_shrink_broad_radius():
    a = vehicle()
    a.spec["radius_m"] = 1
    with pytest.raises(ValueError):
        discs(a.spec)


def test_vehicle_requires_matching_lane_certificate_not_only_width():
    from lifelib import ring_population

    from awr.sim.activities.life import LifeWorld

    doc = ring_population(1)
    actor = doc["actors"][0]
    actor.update(vehicle(4, 2).spec, profile="vehicle", asset_id="test-car")
    for f in doc["facilities"].values():
        f["profiles"] = ["vehicle"]
    for e in doc["edges"].values():
        e.update(profiles=["vehicle"], clearance_m=10)
    with pytest.raises(ValueError, match="No admitted"):
        LifeWorld(doc)
    for e in doc["edges"].values():
        e["vehicle_bodies"] = {"test-car": actor["body_m"]}
    LifeWorld(doc)


def test_blocked_vehicle_rotation_is_checked_before_yaw_commit():
    from lifelib import ring_population

    from awr.sim.activities.life import LifeWorld

    doc = ring_population(1)
    actor = doc["actors"][0]
    actor.update(vehicle(4, 2).spec, profile="vehicle", asset_id="test-car", yaw_rad=math.pi / 2)
    for f in doc["facilities"].values():
        f["profiles"] = ["vehicle"]
    for e in doc["edges"].values():
        e.update(profiles=["vehicle"], clearance_m=10, vehicle_bodies={"test-car": actor["body_m"]})
    doc["nodes"]["obstacle"] = {"pos": [2.4, 0, 0]}
    doc["actors"].append(
        {
            "id": "obstacle",
            "class": "object",
            "profile": "object",
            "start": "obstacle",
            "radius_m": 0.3,
            "speed_mps": 1,
            "stationary": True,
        }
    )
    world = LifeWorld(doc)
    for t in np.arange(0.1, 5, 0.1):
        world.advance(float(t))
        a, b = world.actors["a000"], world.actors["obstacle"]
        assert clearance(a.spec, a.pos, a.yaw, b.spec, b.pos, b.yaw) >= 0.05 - 1e-8
    assert world.actors["a000"].yaw > 0.1
