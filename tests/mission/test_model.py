"""任务数据模型（M10-FR-001；M10-AC-001）：msgpack 与 JSON 往返无损；字段名带单位后缀。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from awr.sim.mission.model import (
    ActionSpec,
    MissionConstraints,
    MissionItemSpec,
    MissionSpec,
    StartSpec,
    TrajectorySpec,
    from_json,
    from_msgpack,
    to_json,
    to_msgpack,
)

ROOT = Path(__file__).resolve().parents[2]


def _spec() -> MissionSpec:
    return MissionSpec(mission_id="m-lower", generator="helix_scan", vehicle_ids=["p600-01"],
                       params={"center_enu_m": [-162.2, 77.3], "radius_m": 57, "z_range_m": [252, 50]},
                       constraints=MissionConstraints(alt_max_m=400.0, prefer_low=True), start=StartSpec(on="ready"),
                       priority=1)


@pytest.mark.parametrize("m", [
    _spec(),
    MissionItemSpec(seq=3, kind="leg", primitive="follow_path", geometry={"polyline_enu_m": [[0, 0, 1], [1, 2, 3]]},
                    speed_mps=6.0, actions=[ActionSpec(kind="camera.trigger", args={"every_m": 23.1})]),
    TrajectorySpec(traj_id=7, vehicle_id="p600-01", ctrl_pts=[[0, 0, 0]] * 4, ts_s=0.5, limits={"v_max_mps": 6.0}),
])
def test_roundtrip(m) -> None:
    assert from_msgpack(type(m), to_msgpack(m)) == m
    assert from_json(type(m), to_json(m)) == m


def test_defaults_and_ranges() -> None:
    c = MissionConstraints()
    assert (c.alt_min_agl_m, c.clearance_m, c.energy_reserve, c.min_sep_m, c.layer_dz_m) == (20, 5, 0.2, 10, 4)
    assert c.transit_planner == "safe_transit"
    with pytest.raises(ValidationError):
        MissionConstraints(min_sep_m=3.0)
    with pytest.raises(ValidationError):
        StartSpec(at_s=1.0, on="ready")
    with pytest.raises(ValidationError):
        TrajectorySpec(traj_id=1, vehicle_id="x", ctrl_pts=[[0, 0, 0]] * 3)
    it = MissionItemSpec(seq=0)
    assert it.acceptance_m(8.0) == 2.0 and it.acceptance_m(2.0) == 1.0
    with pytest.raises(ValidationError):
        MissionSpec(mission_id="x", generator="warp", params={}, vehicle_ids=["a"])


def test_unit_suffixes() -> None:
    """数值字段名都带 units.json 登记的后缀（无量纲量与枚举除外）。"""
    units = json.loads((ROOT / "packages/contracts/rt/units.json").read_text(encoding="utf-8"))
    sfx = sorted(units["suffixes"], key=len, reverse=True)
    plain = {"seq", "revision", "priority", "traj_id", "order", "photos", "energy_reserve", "prefer_low", "vehicle_ids",
             "ctrl_pts", "t_fwd_s", "fixed_rad"}
    for cls in (MissionConstraints, MissionItemSpec, TrajectorySpec):
        for name, f in cls.model_fields.items():
            ann = str(f.annotation)
            if ann in ("<class 'float'>", "float | None") and name not in plain:
                assert any(name.endswith(s) for s in sfx), name
            assert not re.search(r"_(sec|secs|msec|kmh|meters|degrees)$", name), name
