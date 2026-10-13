"""建议半径与有效体（M18-AC-015；M18-FR-017；ADR-099；17 §17.6.5 SB-46）：

- P600 对人（声学体）：日间、夜间、保守值与独立参考实现（逐带，`ref_acoustic.py`）一致（±1 m）；单带退化口径的锚点见 test_acoustic；
- 不受 radius_max 截断（上限 2000 m）；采纳值 `ceil(conservative_max_m / 10)·10`；只对视觉与声学体；
- 有效半径缓存：detail 2 Hz 连续 10 min【仿真】命中率 ≥ 90%；
- 回复形状通过契约 `target.schema.json#/$defs/suggested_radius`。
"""

from __future__ import annotations

import math

import pytest
import ref_acoustic as REF
from ctlib import registry, validator
from jsonschema import Draft202012Validator
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets import P600, TargetConfig, suggested_radius
from awr.sim.targets.effective import EffCache
from awr.sim.targets.envview import day_night, profile_bands


def _ref_radius(total_bg: float, a: float, hearing: float = 20.0, adv: float = 0.0) -> float:
    bg = list(profile_bands("rural", total_bg))
    lo, hi = 1.0, 5000.0
    if REF.margin_db(88.0, 150.0, 12.0, 1414.0, hi, 0.0, bg, hearing, a, adv) >= 0:
        return hi
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if REF.margin_db(88.0, 150.0, 12.0, 1414.0, mid, 0.0, bg, hearing, a, adv) >= 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def test_person_acoustic_matches_reference() -> None:
    c = TargetConfig.from_dict({"class": "person", "pos": [0, 0], "alertness": 0.5}, "p")
    _d_env, n_env = day_night("rural", math.inf)
    s = suggested_radius(c, P600, n_env).to_wire()
    ac = next(v for v in s["volumes"] if v["kind"] == "acoustic")
    assert abs(ac["day_m"] - _ref_radius(40.0, 0.5)) <= 1.0
    assert abs(ac["night_m"] - _ref_radius(30.0, 0.5)) <= 1.0
    assert abs(ac["now_m"] - ac["night_m"]) < 1e-6
    cons = max(_ref_radius(37.0, 0.7), _ref_radius(27.0, 0.7))
    assert abs(ac["conservative_max_m"] - min(cons, 2000.0)) <= 1.0
    assert ac["radius_suggested_m"] == math.ceil(ac["conservative_max_m"] / 10.0) * 10.0
    assert s["basis"]["conservative"] is True and s["basis"]["alertness_used"] == pytest.approx(0.7)
    assert {v["kind"] for v in s["volumes"]} == {"visual", "acoustic"}


def test_not_truncated_by_radius_max_and_schema() -> None:
    c = TargetConfig.from_dict({"class": "dog", "pos": [0, 0], "sensitivity": [{"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 10}]},
                               "d")
    s = suggested_radius(c, P600, day_night("rural", math.inf)[1]).to_wire()
    v = s["volumes"][0]
    assert v["radius_cfg_m"] == 10.0 and v["night_m"] > 100.0 and v["conservative_max_m"] <= 2000.0
    assert list(Draft202012Validator(validator("target/target.schema.json").schema["$defs"]["suggested_radius"],
                                     registry=registry()).iter_errors(s)) == []


def test_sb46_via_query_and_cache_hit_rate() -> None:
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person(criterion_mode="physical"))
        r = b.query("target/suggested_radius", {"id": tid, "model_id": "p600_mid360", "light": "night", "bg": "rural"})
        assert r["code"] == 0 and r["item"]["basis"]["light"] == "night"
        assert b.query("target/suggested_radius", {"id": "nope"})["code"] == 543
        b.rt.eff = EffCache()
        b.S.fly(0, [3000.0, 0.0, 50.0])
        for _ in range(1200):          # 10 min，2 Hz
            b.step(5)
            b.rt.publish_detail(b.ctx)
        assert b.rt.eff.hit_rate >= 0.9, b.rt.eff.hit_rate
        det = b.rt.last_detail["items"][tid]
        assert set(det["sensitivity"][0]["radius_eff_m"]) == {"p600_mid360"}
