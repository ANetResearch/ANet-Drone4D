"""剧本加载器（M10-FR-062；M10-AC-015；AWR-16 §12.6 V-SC-01 至 V-SC-11）：每条规则注入一个缺陷 → 121。"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from awr.sim.fleet.profiles import ProfileTable
from awr.sim.mission.scenario_loader import ScenarioError, deep_merge, expand_vehicle_sets, load_scenario
from awr.world.geometry.fake import fake_world_query

ROOT = Path(__file__).resolve().parents[2]
S1 = json.loads((ROOT / "packages/contracts/fixtures/scenario/s1-shenzhen-facade.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return fake_world_query(tmp_path_factory.mktemp("tiny"))


def _tiny_doc() -> dict:
    d = copy.deepcopy(S1)
    d["world_id"] = "tiny"
    d["zones"] = {"active": ["border", "nofly-l"]}
    d["vehicles"][0]["home_enu_m"] = [-150.0, -100.0, None]
    d["vehicles"][1]["home_enu_m"] = [-150.0, -80.0, None]
    for m in d["missions"]:
        m["params"]["center_enu_m"] = [-40.0, -20.0]
        m["params"]["radius_m"] = 45
        m["params"]["z_range_m"] = [90, 30]
    return d


def test_s1_fixture_ci_profile(world) -> None:
    d = _tiny_doc()
    sc = load_scenario("s1-shenzhen-facade", profile="ci", world=world, profiles=ProfileTable(), doc=d)
    assert len(sc.vehicles) == 2 and len(sc.missions) == 2 and sc.doc["rate"] == 10 and sc.doc["record"] is False
    assert sc.zones_active == ["border", "nofly-l"] and len(sc.sha256) == 64


def _mut(fn):
    d = _tiny_doc()
    fn(d)
    return d


DEFECTS = {
    "V-SC-01": lambda d: d.pop("gcs_loss_policy"),
    "V-SC-02": lambda d: d.update(world_id="shenzhen"),
    "V-SC-03": lambda d: d["vehicles"][0].update(speed_profile="warp_drive"),
    "V-SC-04": lambda d: d["vehicles"][1].update(vehicle_id="p600-01"),
    "V-SC-05": lambda d: d["vehicles"][0].update(home_enu_m=[80.0, -50.0, None]),         # nofly-l 内
    "V-SC-06": lambda d: d["missions"][0].update(vehicle_ids=["ghost-01"]),
    "V-SC-07": lambda d: d["missions"][0]["params"].update(z_range_m=[400, 30]),
    "V-SC-08": lambda d: d["env"].update(preset="volcano"),
    "V-SC-09": lambda d: d["zones"].update(active=["border", "nofly-mars"]),
    "V-SC-10": lambda d: d["events"].append({"event_id": "x", "at_s": 5000, "action": "mark", "args": {"label": "late"}}),
}


@pytest.mark.parametrize("rule", sorted(DEFECTS))
def test_each_rule_rejects_with_121(world, rule: str) -> None:
    d = _mut(DEFECTS[rule])
    with pytest.raises(ScenarioError) as e:
        load_scenario("s1-shenzhen-facade", world=world, profiles=ProfileTable(), doc=d)
    assert e.value.rule == rule and e.value.code == 121


def test_v_sc_11_is_a_warning(world) -> None:
    """16 §12.6：V-SC-11 为告警（S1 的 ci profile 正是 record = false 且机体 marked）。"""
    d = _tiny_doc()
    d["record"] = False
    sc = load_scenario("x", world=world, profiles=ProfileTable(), doc=d)
    assert any(w.startswith("V-SC-11") for w in sc.warnings)


def test_v_sc_10_grammar(world) -> None:
    for bad in ({"metric": "elapsed_s", "op": "~", "value": 1}, {"metric": "happiness", "op": ">", "value": 1},
                {"metric": "landed_all", "op": ">", "value": True}, {"all": []}):
        d = _tiny_doc()
        d["success"] = bad
        with pytest.raises(ScenarioError) as e:
            load_scenario("x", world=world, profiles=ProfileTable(), doc=d)
        assert e.value.rule in ("V-SC-01", "V-SC-10")
    d = _tiny_doc()
    p = {"metric": "elapsed_s", "op": "<", "value": 1}
    for _ in range(17):
        p = {"not": p}
    d["success"] = p
    with pytest.raises(ScenarioError) as e:
        load_scenario("x", world=world, profiles=ProfileTable(), doc=d)
    assert e.value.rule == "V-SC-10"


def test_start_after_cycle(world) -> None:
    d = _tiny_doc()
    d["missions"][0]["start"] = {"after": "m-upper"}
    d["missions"][1]["start"] = {"after": "m-lower"}
    with pytest.raises(ScenarioError) as e:
        load_scenario("x", world=world, profiles=ProfileTable(), doc=d)
    assert e.value.rule == "V-SC-06"


def test_deep_merge_replaces_arrays() -> None:
    a = {"x": {"y": 1, "z": [1, 2]}, "k": 1}
    assert deep_merge(a, {"x": {"z": [3]}, "k": 2}) == {"x": {"y": 1, "z": [3]}, "k": 2}


def test_vehicle_sets_expansion() -> None:
    d = {"vehicle_sets": [{"set_id": "ladder", "id_prefix": "sim", "count": 5, "id_digits": 4,
                           "layout": {"kind": "grid", "origin_enu_m": [0, 0, None], "spacing_m": 24, "cols": 2},
                           "mission": {"generator": "orbit", "params": {"radius_m": 3, "speed_mps": 2, "turns": 20,
                                                                         "agl_m": 60}, "center": "home",
                                       "start": {"at_s": 5}}}]}
    v, m = expand_vehicle_sets(d)
    assert [x["vehicle_id"] for x in v] == ["sim-0001", "sim-0002", "sim-0003", "sim-0004", "sim-0005"]
    assert np.allclose([x["home_enu_m"][:2] for x in v], [[0, 0], [24, 0], [0, 24], [24, 24], [0, 48]])
    assert m[0]["vehicle_ids"] == [x["vehicle_id"] for x in v] and m[0]["params"]["center_enu_m"] == "home"
    assert m[0]["start"] == {"at_s": 5}
