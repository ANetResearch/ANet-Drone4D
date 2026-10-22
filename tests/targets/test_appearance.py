"""Appearance must survive persistence without changing identity, motion or perception."""
import copy

import numpy as np
import pytest
from ctlib import errors
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.model import TargetError, appearance_catalog, merged_config, validate_config


def binding(key="human-1"):
    asset = appearance_catalog()[key]
    return {"asset_id": key, "geometry_sha256": asset["geometry_sha256"]}


@pytest.mark.parametrize("appearance", [
    {"asset_id": "../../outside", "geometry_sha256": "a" * 64},
    {"asset_id": "human-1", "geometry_sha256": "a" * 64},
    {**binding(), "url": "https://invalid.example/mesh"},
    binding("sedan"),
])
def test_rejects_unknown_mismatched_or_unpinned_assets(appearance):
    with pytest.raises(TargetError) as exc:
        validate_config(merged_config({"class": "person", "appearance": appearance}))
    assert exc.value.code == 110


def test_visual_patch_preserves_identity_motion_and_can_be_removed():
    with Bench(1, FakeWorld()) as bench:
        tid = bench.add(static_person(pos=[11.0, 12.0, 0.0]))
        target = bench.T(tid)
        identity = (target.id, target.target_no)
        motion = target.ms
        old = copy.deepcopy(target.cfg.raw)
        rep = bench.cmd("target/update", {"id": tid, "patch": {"appearance": binding()}})
        assert rep["status"] == "accepted", rep
        assert (target.id, target.target_no) == identity
        assert target.ms is motion
        np.testing.assert_array_equal(target.ms.pos, [11, 12, 0])
        cfg = bench.rt.cfg_dict(target)
        assert cfg["appearance"] == binding()
        assert errors("target/target.schema.json", cfg) == []
        assert {k: v for k, v in target.cfg.raw.items() if k != "appearance"} == old
        assert bench.cmd("target/update", {"id": tid, "patch": {"appearance": None}})["status"] == "accepted"
        assert "appearance" not in target.cfg.raw


def test_visual_change_does_not_restart_a_moving_target():
    with Bench(1, FakeWorld()) as bench:
        motion = {"mode": "path", "path": [[0, 0, 0], [100, 0, 0]], "speed_mps": 1.2, "loop": "once"}
        a = bench.add(static_person(motion=motion))
        b = bench.add(static_person(motion=motion))
        bench.run(3)
        before = bench.T(a).ms.pos.copy()
        assert before[0] > 0
        assert bench.cmd("target/update", {"id": a, "patch": {"appearance": binding()}})["status"] == "accepted"
        bench.run(2)
        np.testing.assert_allclose(bench.T(a).ms.pos, bench.T(b).ms.pos, atol=0.03)
        assert bench.T(a).ms.pos[0] > before[0]


def test_legacy_config_unchanged_and_roundtrip_valid():
    cfg = merged_config({"class": "person"})
    assert "appearance" not in cfg
    assert validate_config(cfg) == []
    cfg["appearance"] = binding()
    restored = merged_config(copy.deepcopy(cfg))
    assert restored == cfg
    assert validate_config(restored) == []
