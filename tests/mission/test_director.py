"""剧本导演（M10-FR-063、FR-064；M10-AC-016；AWR-16 §12.3）。"""

from __future__ import annotations

import copy

import pytest
from harness import Sim
from test_scenario_loader import _tiny_doc

from awr.sim.core import metrics as MET
from awr.sim.mission.director import compile_tsir, eval_predicate, metric_key
from awr.world.geometry.fake import fake_world_query


def test_predicate_eval_and_missing_metric_is_false() -> None:
    vals = {"a": 1.0, "b": 0.0}
    get = lambda leaf: vals.get(leaf["metric"])  # noqa: E731
    trace: list = []
    p = {"all": [{"metric": "a", "op": ">=", "value": 1}, {"not": {"metric": "b", "op": "==", "value": True}},
                 {"any": [{"metric": "missing", "op": "<", "value": 5}, {"metric": "a", "op": "!=", "value": 2}]}]}
    assert eval_predicate(p, get, trace)
    assert any(t["value"] is None and not t["ok"] for t in trace)
    assert not eval_predicate({"metric": "missing", "op": "<", "value": 1e9}, get)


def test_tsir_compile() -> None:
    p = {"all": [{"metric": "missions_done", "op": "==", "value": True},
                 {"metric": "pos_err_max_m", "args": {"window": "gust"}, "op": "<", "value": 3.0},
                 {"metric": "guard_events", "op": "!=", "value": 1}]}
    t = compile_tsir(p)
    assert t["op"] == 1 and len(t["args"]) == 3
    assert t["args"][0] == {"op": 12, "thresh": {"metric": "missions_done", "op": 3, "value": 1.0}}
    assert t["args"][1]["thresh"]["metric"] == "pos_err_max_m{window=gust}" and t["args"][1]["thresh"]["op"] == 1
    assert t["args"][2]["op"] == 3 and t["args"][2]["args"][0]["thresh"]["op"] == 3
    assert compile_tsir({"any": [{"metric": "x", "op": ">", "value": 1}]})["op"] == 12
    assert metric_key({"metric": "facade_coverage", "args": {"mission_ids": ["b", "a"]}}) == "facade_coverage{mission_ids=[b,a]}"


def test_timed_and_conditional_events_fire_on_time(tmp_path) -> None:
    w = fake_world_query(tmp_path)
    d = _tiny_doc()
    d["missions"] = []
    d["events"] = [{"event_id": "mark-3", "at_s": 3.0, "action": "mark", "args": {"label": "t3"}},
                   {"event_id": "rtl-when", "when": {"metric": "probe_a", "op": ">", "value": 0.5}, "delay_s": 1.0,
                    "action": "mark", "args": {"label": "cond"}}]
    d["success"] = {"metric": "elapsed_s", "op": ">=", "value": 0}
    d["events"][1]["when"]["metric"] = "elapsed_s"
    d["events"][1]["when"]["value"] = 5.0
    sim = Sim(w, n=1)
    try:
        if "elapsed_s" not in {m.name for m in MET.list_metrics()}:          # INT-1：SimCore 已登记 M08 度量
            MET.register_metric("elapsed_s", lambda **kw: sim.core.clock.t_ns * 1e-9, owner="M08")
        r = sim.rt.load_scenario("s1-shenzhen-facade", doc=d)
        assert r["code"] == 0
        sim.until(lambda: sim.rt.director.phase == "running", 10.0, 0.05)
        sim.advance(8.0)
        marks = {e[2]["event_id"]: e[0] for e in sim.kinds("scenario.mark")}
        assert marks["mark-3"] == pytest.approx(3.0, abs=0.0081)          # ≤ 1 个 L1 tick
        assert 6.0 <= marks["rtl-when"] <= 6.2                              # when 10 Hz + delay 1 s
        ev = [e for e in sim.kinds("scenario.event")]
        assert len(ev) == 2
    finally:
        sim.close()


def test_scenario_setup_replaces_skeleton_vehicles(tmp_path) -> None:
    w = fake_world_query(tmp_path)
    d = _tiny_doc()
    d["missions"] = []
    d["events"] = []
    sim = Sim(w, n=1)
    try:
        assert sim.ids == ["x500-01"]
        sim.rt.load_scenario("s1-shenzhen-facade", doc=copy.deepcopy(d))
        assert sim.until(lambda: sim.rt.director.phase == "running", 10.0, 0.1)
        assert sorted(sim.ids) == ["p600-01", "p600-02"]
        assert sim.pos("p600-02")[:2].tolist() == [-150.0, -80.0]
    finally:
        sim.close()
