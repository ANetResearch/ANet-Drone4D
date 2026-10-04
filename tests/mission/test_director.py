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


def _run_wall(sim: Sim, seconds: float, pred=None) -> bool:
    """按假墙钟推进（不假设仿真时间单调：循环剧本会把时钟重置到 0，harness.advance 的终点判定会一直追下去）。"""
    from awr.sim.fleet.pipeline import TICK_NS

    for _ in range(int(seconds * 1e9 / (5 * TICK_NS))):
        if pred is not None and pred():
            return True
        sim.W[0] += 5 * TICK_NS
        sim.core.iterate()
    return bool(pred()) if pred is not None else True


def test_loop_reset_rotates_weather(tmp_path, monkeypatch) -> None:
    """循环剧本（on_complete = reset，ADR-084）：结束后 LOOP_GAP_S【仿真】经 SimCore.request_reset 在步边界外重置
    （sim.reset 原因 scenario_loop、epoch + 1），重新加载时天气序列循环左移一位；人工 sim/reset 的轮次归零。"""
    import json

    from awr.sim.mission.director import LOOP_GAP_S
    from awr.sim.mission.scenario_loader import rotate_weather

    w = fake_world_query(tmp_path)
    d = _tiny_doc()
    d["scenario_id"] = "loop-tiny"
    d["missions"] = []
    d["env"] = {"preset": "clear"}
    d["events"] = [{"event_id": "wx-fog", "at_s": 2.0, "action": "env.preset", "args": {"name": "fog", "duration_s": 1}},
                   {"event_id": "wx-rain", "at_s": 4.0, "action": "env.preset", "args": {"name": "lightRain", "duration_s": 1}}]
    d["time_limit_s"] = 12
    d["on_complete"] = "reset"
    d["success"] = {"metric": "guard_events", "op": "==", "value": 0}
    (tmp_path / "loop-tiny.json").write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setenv("AWR_SCENARIOS_DIR", str(tmp_path))
    assert rotate_weather(d, 1)["env"]["preset"] == "fog"
    assert [e["args"]["name"] for e in rotate_weather(d, 2)["events"]] == ["clear", "fog"]
    assert rotate_weather(d, 3) is d and d["env"]["preset"] == "clear"  # 整轮回到原序列，入参不变
    sim = Sim(w, n=1)
    try:
        assert sim.rt.load_scenario("loop-tiny")["code"] == 0
        assert _run_wall(sim, 20.0, lambda: sim.rt.director.phase == "done")
        epoch0 = sim.core.epoch
        assert _run_wall(sim, LOOP_GAP_S + 2.0, lambda: sim.rt.loop_index == 1)
        resets = [e for e in sim.kinds("sim.reset") if e[2].get("reason") == "scenario_loop"]
        assert len(resets) == 1 and sim.core.epoch == epoch0 + 1 and sim.core.clock.t_ns < 2_000_000_000
        assert sim.rt.scenario.doc["env"]["preset"] == "fog"
        assert [e["args"]["name"] for e in sim.rt.scenario.doc["events"] if e["action"] == "env.preset"] == ["lightRain", "clear"]
        assert _run_wall(sim, 30.0, lambda: sim.rt.loop_index == 2)
        assert sim.rt.scenario.doc["env"]["preset"] == "lightRain"
        sim.core.reset(args={})  # 人工重置：轮次归零，天气回到剧本原序列
        assert sim.rt.loop_index == 0 and sim.rt.scenario.doc["env"]["preset"] == "clear"
    finally:
        sim.close()
