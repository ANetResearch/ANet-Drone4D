import copy
import hashlib
import json

import numpy as np
import pytest
from ctlib import errors
from tlib import Bench, FakeWorld, static_person

from awr.sim.activities.plans import ActivityPlan, compile_plan, load_plan
from awr.sim.targets.model import TargetError, merged_config, validate_config


@pytest.fixture
def activity(tmp_path, monkeypatch):
    monkeypatch.setenv("AWR_TARGET_ACTIVITY_DIR", str(tmp_path))
    load_plan.cache_clear()
    nodes = {
        str(i): {"pos": p, "label": f"Corner {i}", "destination": True, "purpose": "rest"}
        for i, p in enumerate(([0, 0, 0], [120, 0, 0], [120, 120, 0], [0, 120, 0]))
    }
    net = {
        "status": "verified",
        "world_id": "test",
        "world_content_version": "v1",
        "coordinate_sha256": "a" * 64,
        "radius_m": 0.5,
        "height_m": 2,
        "nodes": nodes,
        "edges": [
            {"from": str(i), "to": str((i + 1) % 4), "verified": True, "profile": "person", "bidirectional": True}
            for i in range(4)
        ],
    }
    doc = compile_plan(net, "walker-01", 7, "0")
    data = json.dumps(doc).encode()
    sha = hashlib.sha256(data).hexdigest()
    path = tmp_path / f"walker-01-{sha}.json"
    path.write_bytes(data)
    world = FakeWorld()
    world.world_id = "test"
    world.content_version = "v1"
    world.coordinate_sha256 = "a" * 64
    cfg = static_person(
        pos=[0, 0, 0],
        motion={"mode": "timeline"},
        reaction_policy="exogenous",
        sensitivity=[],
        activity={"plan_id": "walker-01", "sha256": sha},
    )
    return world, cfg, ActivityPlan(doc), path


def test_runtime_uses_absolute_clock_and_preserves_plan_on_visual_patch(activity):
    world, cfg, plan, _ = activity
    with Bench(1, world) as b:
        tid = b.add(cfg)
        b.run(5)
        expected = plan.sample(b.ctx.t_ns * 1e-9)[0]
        np.testing.assert_allclose(b.T(tid).ms.pos, expected, atol=0.16)
        d = b.rt.detail(b.T(tid), b.ctx.t_ns, b.rt.env(b.ctx), with_cfg=True)
        assert d["activity"]["seed"] == 7
        assert errors("rt/payloads/target_detail.schema.json", d) == []
        assert errors("target/target.schema.json", d["cfg"]) == []
        before = b.T(tid).ms.pos.copy()
        assert b.cmd("target/update", {"id": tid, "patch": {"label": "same person"}})["status"] == "accepted"
        np.testing.assert_array_equal(before, b.T(tid).ms.pos)
        assert b.cmd("target/update", {"id": tid, "patch": {"pos": [20, 0, 0]}})["status"] == "rejected"
        assert (
            b.cmd("target/update", {"id": tid, "patch": {"size_m": {"l": 4, "w": 2, "h": 2}}})["status"] == "rejected"
        )


def test_wrong_world_and_missing_or_corrupt_plan_are_rejected(activity):
    world, cfg, _, path = activity
    world.content_version = "different"
    with Bench(1, world) as b, pytest.raises(TargetError, match="ACTIVITY_NOT_ADMITTED"):
        b.rt.add(b.ctx, cfg)
    world.content_version = "v1"
    load_plan.cache_clear()
    path.write_bytes(b"{}")
    with Bench(1, world) as b, pytest.raises(TargetError, match="ACTIVITY_NOT_ADMITTED"):
        b.rt.add(b.ctx, cfg)
    path.unlink()
    load_plan.cache_clear()
    with Bench(1, world) as b, pytest.raises(TargetError, match="ACTIVITY_NOT_ADMITTED"):
        b.rt.add(b.ctx, cfg)


def test_activity_requires_readonly_motion_policy(activity):
    _, cfg, _, _ = activity
    for patch in (
        {"motion": {"mode": "random_walk"}},
        {"reaction_policy": "interactive"},
        {"activity": {"plan_id": "../escape", "sha256": "a" * 64}},
    ):
        bad = copy.deepcopy(cfg)
        bad.update(patch)
        with pytest.raises(TargetError):
            validate_config(merged_config(bad))


def test_exported_binding_reconstructs_same_pose_at_same_world_time(activity):
    world, cfg, _, _ = activity
    with Bench(1, world) as b:
        tid = b.add(cfg)
        b.run(127)
        exported = b.rt.cfg_dict(b.T(tid))
        expected = b.T(tid).ms.pos.copy()
        clock = b.ctx.t_ns
        assert exported["activity"] == cfg["activity"]
        assert exported["pos"] == cfg["pos"]
    load_plan.cache_clear()  # a new process must reload the immutable artifact
    with Bench(1, world) as b:
        tid = b.add(exported)
        b.ctx.t_ns = clock - 100_000_000
        b.step()
        np.testing.assert_array_equal(b.T(tid).ms.pos, expected)
