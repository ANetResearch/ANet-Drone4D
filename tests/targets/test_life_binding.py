import copy

import numpy as np
import pytest
from ctlib import errors
from tlib import Bench, FakeWorld, static_person

from awr.sim.activities.bindings import load_recipe
from awr.sim.activities.compiler import persist_recipe
from awr.sim.activities.life import VERSION
from awr.sim.targets.model import TargetError


@pytest.fixture
def life(tmp_path, monkeypatch):
    monkeypatch.setenv("AWR_TARGET_LIFE_DIR", str(tmp_path))
    load_recipe.cache_clear()
    points = [[0, 0, 0], [30, 0, 0], [30, 20, 0], [0, 20, 0]]
    nodes = {str(i): {"pos": p} for i, p in enumerate(points)}
    edges = {
        str(i): {
            "from": str(i),
            "to": str((i + 1) % 4),
            "points": [p, points[(i + 1) % 4]],
            "profiles": ["person"],
            "clearance_m": 0.5,
            "speed_mps": 1.5,
            "verified": True,
        }
        for i, p in enumerate(points)
    }
    facilities = {k: {"node": k, "label": k, "profiles": ["person"], "capacity": 2, "indoor": False} for k in nodes}
    doc = {
        "version": VERSION,
        "status": "verified",
        "seed": 71,
        "world_id": "test",
        "world_content_version": "v1",
        "coordinate_sha256": "a" * 64,
        "nodes": nodes,
        "edges": edges,
        "facilities": facilities,
        "actors": [
            {
                "id": "resident-01",
                "class": "person",
                "start": "0",
                "profile": "person",
                "radius_m": 0.5,
                "height_m": 2,
                "speed_mps": 1.2,
                "itinerary": [
                    {"facility": "1", "purpose": "shop", "dwell_s": 5},
                    {"facility": "3", "purpose": "home", "dwell_s": 5},
                ],
            }
        ],
    }
    sha = persist_recipe(doc, tmp_path)
    world = FakeWorld()
    world.world_id = "test"
    world.content_version = "v1"
    world.coordinate_sha256 = "a" * 64
    cfg = static_person(
        id="resident-01",
        pos=[0, 0, 0],
        motion={"mode": "timeline"},
        reaction_policy="exogenous",
        sensitivity=[],
        life={"recipe_id": "life", "sha256": sha, "actor_id": "resident-01"},
    )
    return world, cfg, doc


def test_shared_runtime_pose_detail_metadata_and_replay(life):
    world, cfg, _doc = life
    with Bench(1, world) as b:
        tid = b.add(cfg)
        b.run(120)
        target = b.T(tid)
        expected = target.ms.pos.copy()
        clock = b.ctx.t_ns
        detail = b.rt.detail(target, clock, b.rt.env(b.ctx), with_cfg=True)
        assert errors("rt/payloads/target_detail.schema.json", detail) == []
        assert detail["activity"]["updates"] > 1000 and detail["activity"]["visits"] >= 2
        before = target.ms.pos.copy()
        assert (
            b.cmd("target/update", {"id": tid, "patch": {"metadata": {"role": "same resident"}}})["status"]
            == "accepted"
        )
        np.testing.assert_array_equal(target.ms.pos, before)
        exported = b.rt.cfg_dict(target)
        assert errors("target/target.schema.json", exported) == []
        for patch in ({"pos": [20, 0, 0]}, {"enabled": False}, {"life": None}, {"motion": {"mode": "static"}}):
            assert b.cmd("target/update", {"id": tid, "patch": patch})["status"] == "rejected"
        assert b.cmd("target/remove", {"id": tid})["status"] == "rejected"
    load_recipe.cache_clear()
    with Bench(1, world) as b:
        tid = b.add(exported)
        b.ctx.t_ns = clock - 100_000_000
        b.step()
        np.testing.assert_array_equal(b.T(tid).ms.pos, expected)


def test_life_binding_rejects_wrong_world_oversize_and_mixed_motion(life):
    world, cfg, _ = life
    world.content_version = "different"
    with Bench(1, world) as b, pytest.raises(TargetError):
        b.rt.add(b.ctx, cfg)
    world.content_version = "v1"
    with Bench(1, world) as b:
        bad = copy.deepcopy(cfg)
        bad["size_m"] = {"l": 10, "w": 10, "h": 2}
        with pytest.raises(TargetError):
            b.rt.add(b.ctx, bad)
        b.add(cfg)
        with pytest.raises(TargetError):
            b.rt.add(b.ctx, static_person(id="untracked-obstacle", pos=[5, 5, 0]))


def test_shared_population_import_is_atomic(life, tmp_path):
    world, cfg, doc = life
    second = copy.deepcopy(doc["actors"][0])
    second.update(id="resident-02", start="2")
    doc["actors"].append(second)
    sha = persist_recipe(doc, tmp_path)
    one = copy.deepcopy(cfg)
    one["life"]["sha256"] = sha
    two = copy.deepcopy(one)
    two.update(id="resident-02", pos=[30, 20, 0])
    two["life"]["actor_id"] = "resident-02"
    with Bench(1, world) as b:
        with pytest.raises(TargetError):
            b.rt.add(b.ctx, one)
        assert b.rt.import_targets(b.ctx, [one]) and b.rt.ids() == []
        bad = copy.deepcopy(two)
        bad["size_m"] = {"l": 10, "w": 10, "h": 2}
        assert b.rt.import_targets(b.ctx, [one, bad]) and b.rt.ids() == []
        assert b.rt.import_targets(b.ctx, [one, two]) == []
        b.run(20)
        assert all(t.ms.speed > 0 for t in b.rt.ordered())
    with Bench(1, world, limit=1) as b:
        assert b.rt.import_targets(b.ctx, [one, two]) and b.rt.ids() == []
