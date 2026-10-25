"""车辆追踪事件类型 `vehicle_track` 与车道图车辆接入（D3-AC-33、D3-AC-62 车辆追踪条款；M23-AC-031、M23-AC-040；M23 §15.2 至 §15.4）。

合成路网（世界帧 m，`OsmRoads` 经 M22 `build_lane_graph` 生成车道图）：东西向 primary（主路）与南北向 residential（次路）交于 (0, 0)，
右侧通行、每向 1 车道。舞台为轻量替身（平地导航面、平地贴地），不需要世界包。

1. 门 1：`drive` 只对车辆、IDM 参数、`stop` 与 `park` 需要 `drive`、`park` 须为末路点（`E-DRIVE` 611）；schema 拒绝 s0 < 2 m；
   `vehicle_track` 必备配置（目标 1 辆、干扰 ≥ 2 辆、均为车辆、全部车辆有 `drive`；`E-TYPE` 611）。
2. 车道图车流（`gen.lane_traffic`）：路点投影与路线；`wait` 停留到计划时刻 + dwell、`stop` 只停 dwell、首路点停留、`despawn` 前停留、
   `park` 熄火；迟到 `I-LATE`、锚点离车道过远 `I-LANES`、车道图不可达 `I-DISCONNECTED`、无车道图 `I-LANES`；背景车流只取 zone 内车道且
   与脚本车辆同一次推进；车道口径的到达行；同一 plan 两次生成摘要相同。
3. 门 2 至门 3（`preflight` → `generate_frames` → `verify_event`）：磁带车辆轨迹与车流逐毫米相同；`S-OFFMESH` 车道口径与
   `S-ARRIVAL` 车道口径通过；`vehicle_track` 验证器产出 depart、turn{intersection}、stop、park、enter_zone、exit_zone 并与生成决策
   一致，审计（D3-AC-33：走廊、车距、无穿越、路口让行）通过；磁带篡改（偏出车道）、生成决策与磁带关键事件不符、plan 与磁带
   不对应时 strict 613、lenient 告警；无舞台时告警 `W-TYPE-LANES`（审计各判据的检出见 `test_vehicle.py`）。
4. 合成 GLB 世界包（open3d，含写入的 nav.graph 图层）端到端 strict 编译：真值关键事件、`report.lanes`、磁带头 `navgraph_sha256`、
   契约 schema；两次编译 `content_sha256` 相同。
5. crop 实数据（有数据时）：`crop-handoff-001` 改用车道图后 strict 编译通过、4 条声明与 2 条关系全部成立；车辆 100% 在道路走廊内、
   沿车道横向偏差 ≤ 2 mm；与 UAV 起飞位最近距离 ≥ 15 m（运行规格的出生位约定）。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("jsonschema")
pytest.importorskip("scipy")
from awr.sim.events import dsl, gen
from awr.sim.events import vehicle as V
from awr.sim.events.verify import VALIDATORS, registry, verify_event
from awr.sim.events.verify import vehicle_track as vt
from awr.world.glb.derive import lanes as L

ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"
CROP_EVENT = ROOT / "configs" / "events.d3" / "crop-handoff-001"
UAV_PADS = [(76.0, -78.0), (76.0, -82.0), (76.0, -86.0)]       # configs/bench.d3/crop-handoff-001.*.run_spec.json


def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


def _roads() -> L.OsmRoads:
    return L.OsmRoads(nodes={1: (-140.0, 0.0), 2: (0.0, 0.0), 3: (140.0, 0.0), 4: (0.0, -140.0), 5: (0.0, 140.0)},
                      ways=[(100, {"highway": "primary"}, [1, 2, 3]), (101, {"highway": "residential"}, [4, 2, 5])], node_tags={})


@pytest.fixture(scope="module")
def graph() -> L.LaneGraph:
    return L.LaneGraph(L.build_lane_graph(_roads(), z_fn=lambda xy: np.zeros(len(xy))).doc)


def _pt(aid: str, x: float, y: float) -> dict:
    return {"id": aid, "kind": "point", "profile": "vehicle", "xy": [x, y]}


ANCHORS = {a["id"]: a for a in [
    _pt("w_in", -130.0, -1.6), _pt("w_stop", -70.0, -1.6), _pt("n_park", 1.6, 110.0),           # 东行：途中停车后左转北行泊车
    _pt("n_in", -1.6, 130.0), _pt("w_out", -130.0, 1.6),                                          # 南行右转西行
    _pt("e_in", 130.0, 1.6),                                                                      # 西行直行
    _pt("s_in", 1.6, -130.0), _pt("s_wait", 1.6, -50.0), _pt("e_out", 130.0, -1.6),              # 北行：等候到计划时刻后右转东行
    _pt("far", 60.0, 60.0),                                                                       # 离车道 > 15 m
    {"id": "jz", "kind": "zone", "profile": "vehicle", "polygon": _sq(0.0, 0.0, 12.0)},
    {"id": "all", "kind": "zone", "profile": "vehicle", "polygon": _sq(0.0, 0.0, 140.0)},
]}


def _veh(aid: str, route: list[dict], *, roles: list[str] | None = None, ref: str | None = None, drive: dict | None = None) -> dict:
    a = {"id": aid, "class": "vehicle", "drive": {"profile": "urban"} if drive is None else drive, "route": route}
    if roles:
        a["roles"] = roles
    if ref:
        a["appearance_ref"] = ref
    return a


def _doc(**kw) -> dict:
    acts = [
        _veh("car_t", [{"t": 0, "at": "w_in", "do": "spawn"}, {"t": 20, "at": "w_stop", "do": "stop", "dwell_s": 6},
                       {"t": 75, "at": "n_park", "do": "park"}], roles=["target"], ref="vehicle-01"),
        _veh("car_d1", [{"t": 2, "at": "n_in", "do": "spawn"}, {"t": 60, "at": "w_out", "do": "despawn"}], roles=["distractor"], ref="vehicle-03"),
        _veh("car_d2", [{"t": 0, "at": "e_in", "do": "spawn"}, {"t": 60, "at": "w_out", "do": "despawn"}], roles=["distractor"], ref="vehicle-13"),
        _veh("car_d3", [{"t": 1, "at": "s_in", "do": "spawn"}, {"t": 40, "at": "s_wait", "do": "wait", "dwell_s": 5},
                        {"t": 90, "at": "e_out", "do": "despawn"}], ref="vehicle-16"),
    ]
    d = {"schema": "drone4d.event.v1", "event_id": "synth-vehicle", "version": 1, "event_type": "vehicle_track", "world": {"id": "synth-glb"},
         "anchors_ref": "event:anchors.json", "seed": 19, "duration_s": 120, "actors": acts,
         "background": [{"class": "vehicle", "count": 4, "zone": "all"}],
         "annotations": [{"id": "pass_j", "type": "zone_pass", "participants": ["car_t"], "window": [20, 75], "params": {"zone": "jz", "buffer_m": 0}}]}
    d.update(kw)
    return d


class _Flat:
    """平地导航面替身：高度 0（车道图车辆的 z_hint 由此取得）。"""

    def z_at_batch(self, xy: np.ndarray) -> np.ndarray:
        return np.zeros(len(np.asarray(xy).reshape(-1, 2)))


class _Ground:
    def ground_z_batch(self, xy: np.ndarray, z: np.ndarray):
        return np.zeros(len(xy)), np.ones(len(xy), bool)


def _stage(g: L.LaneGraph | None) -> gen.Stage:
    st = gen.Stage({"person": _Flat(), "vehicle": _Flat()}, _Ground(), copy.deepcopy(ANCHORS), lanes=g)
    if g is None:
        st.cache["lanes_loaded"] = True
    return st


def _plan(doc: dict | None = None) -> dict:
    plan, errs, _w = dsl.gate1(doc or _doc(), ANCHORS)
    assert errs == [], [e.as_dict() for e in errs]
    return plan


def _codes(xs) -> list[str]:
    return sorted(x.code for x in xs)


# ---------------------------------------------------------------------------------------------------- 1. 门 1
def test_gate1_drive_rules_and_schema() -> None:
    plan = _plan()
    assert plan["actors"][0]["drive"] == {"profile": "urban"} and all("drive" in a for a in plan["actors"])
    bad = _doc()
    bad["actors"].append({"id": "walker", "class": "person", "drive": {"profile": "urban"}, "route": [{"t": 0, "at": "w_in", "do": "spawn"}]})
    del bad["actors"][3]["drive"]                                                   # car_d3：wait 合法，但车辆追踪须有 drive
    bad["actors"][1]["route"].insert(1, {"t": 30, "at": "w_out", "do": "park"})     # park 不是末路点
    bad["actors"][2]["drive"] = {"profile": "urban", "idm": {"T_s": 0.0}}           # schema：exclusiveMinimum
    _p, errs, _w = dsl.gate1(bad, ANCHORS)
    assert _codes(errs) == ["E-SCHEMA"]                                             # 结构性错误先返回
    bad["actors"][2]["drive"] = {"profile": "urban"}
    _p, errs, _w = dsl.gate1(bad, ANCHORS)
    whys = sorted((e.code, e.detail.get("why"), e.detail.get("rule")) for e in errs)
    assert ("E-DRIVE", "class", None) in whys and ("E-DRIVE", "park_not_last", None) in whys
    assert ("E-TYPE", "type_requirement", "drive_required") in whys and all(e.reason == 611 for e in errs)
    nod = _doc()
    del nod["actors"][0]["drive"]
    _p, errs, _w = dsl.gate1(nod, ANCHORS)
    assert ("E-DRIVE", "needs_drive") in {(e.code, e.detail.get("why")) for e in errs}            # stop、park 需要 drive
    s0 = _doc()
    s0["actors"][0]["drive"] = {"idm": {"s0_m": 1.5}}
    _p, errs, _w = dsl.gate1(s0, ANCHORS)
    assert _codes(errs) == ["E-SCHEMA"] and "/actors/0/drive/idm/s0_m" in {e.path for e in errs}


def test_gate1_vehicle_track_requirements() -> None:
    registry.collect()
    assert "vehicle_track" in VALIDATORS and "vehicle_track" in registry.REQUIREMENTS
    assert registry.type_issues(_doc(), ANCHORS) == []
    d = _doc()
    d["actors"][0]["roles"] = ["distractor"]
    d["actors"][2]["roles"] = []
    d["actors"].append({"id": "p", "class": "person", "roles": ["target"], "route": [{"t": 0, "at": "w_in", "do": "spawn"}]})
    rules = sorted(i.detail["rule"] for i in registry.type_issues(d, ANCHORS))
    assert rules == ["target_class"]                                               # 干扰仍有 car_d1 与 car_t 两辆
    d["actors"][0]["roles"] = []
    del d["actors"][1]["drive"]
    rules = sorted(i.detail["rule"] for i in registry.type_issues(d, ANCHORS))
    assert rules == ["distractor_count", "drive_required", "target_class"]
    assert all(i.code == "E-TYPE" and i.reason == 611 and i.detail["why"] == "type_requirement" for i in registry.type_issues(d, ANCHORS))


# ---------------------------------------------------------------------------------------------------- 2. 车道图车流
def test_lane_traffic_routes_holds_park_and_arrivals(graph) -> None:
    plan = _plan()
    st = _stage(graph)
    lt = gen.lane_traffic(plan, st)
    assert lt.errors == [] and gen.lane_traffic(plan, st) is lt                    # 同一舞台按 plan 缓存
    sim, lg = lt.sim, lt.sim.log
    assert lt.scripted == ["car_d1", "car_d2", "car_d3", "car_t"] and len(sim.ids) == 4 + len(lt.table_idx)
    assert lt.audit["ok"], lt.audit["counts"]
    st_ = {s["actor"]: s for s in lg["stops"]}
    # stop：停稳后只停 dwell（不等计划时刻）；wait：停到计划时刻 40 s + 5 s
    assert lt.holds["car_t"] == [6000] and st_["car_t"]["t_ms"] < 20_000
    t_w = st_["car_d3"]["t_ms"]
    assert t_w < 40_000 and lt.holds["car_d3"] == [45_000 - t_w] and lt.iterations >= 2
    c = lt.index["car_d3"]
    mv = np.flatnonzero(np.nan_to_num(sim.v[:, c]) > 0)
    assert int(mv[mv * 100 > t_w][0]) * 100 == 45_100                              # 45.0 s 解除停留，下一帧起有速度
    # park：终点熄火
    (pk,) = [p for p in lg["parks"] if p["actor"] == "car_t"]
    ct = lt.index["car_t"]
    assert (sim.flags[pk["t_ms"] // 100:, ct] & V.FLAG_ENGINE == 0).all() and (sim.state[pk["t_ms"] // 100:, ct] == V.STATE_WAITING).all()
    # 到达行（车道口径）
    rows = {(r["actor"], r["waypoint"]): r for rr in lt.arrivals.values() for r in rr}
    assert all(r["lane"] and r["err_mm"] <= r["tol_mm"] and r["late_ms"] <= r["tol_ms"] for r in rows.values())
    assert rows[("car_t", 0)]["t_arrive_ms"] == 0 and rows[("car_t", 0)]["err_mm"] < 1.0
    assert rows[("car_d1", 1)]["t_arrive_ms"] == int(np.flatnonzero(sim.state[:, lt.index["car_d1"]] == V.STATE_GONE)[0]) * 100
    # 停靠窗口（门 2 上车检查）与路点投影
    (w0,) = lt.windows["car_d3"]
    assert w0[0] == t_w / 1000.0 and w0[1] == 45.0
    assert lt.windows["car_t"][-1][1] == float("inf") and all(s.dist_m < 0.01 for s in lt.wp_snap["car_t"])
    # 背景：起终点车道在 zone 内，idx 为演员表 idx（脚本 4 名之后）
    assert sorted(lt.table_idx) == [4, 5, 6, 7] and all(sim.ids[c].startswith("bg0_") for c in lt.table_idx.values())
    lt2 = gen.lane_traffic(plan, _stage(graph))
    assert lt2 is not lt and lt2.sim.digest() == sim.digest()                      # 确定性
    rep = lt.report()
    assert rep["navgraph_sha256"] == gen._navgraph_sha256(graph) and rep["audit"]["ok"] and rep["log"]["safety_stops"] == 0


def test_lane_traffic_gate2_issues(graph) -> None:
    d = _doc()
    d["actors"][3]["route"][1]["t"] = 8                                            # 8 s 内驶不完 80 m：迟到
    lt = gen.lane_traffic(_plan(d), _stage(graph))
    (late,) = [e for e in lt.errors if e.code == "I-LATE"]
    assert late.reason == 612 and late.detail["actor"] == "car_d3" and late.detail["t_arrive_s"] > 11.0
    d = _doc()
    d["actors"][2]["route"][1]["at"] = "far"
    lt = gen.lane_traffic(_plan(d), _stage(graph))
    assert [(e.code, e.detail["why"]) for e in lt.errors] == [("I-LANES", "unsnapped")] and lt.sim is None
    one = L.LaneGraph(L.build_lane_graph(L.OsmRoads(nodes={1: (-140.0, 0.0), 2: (140.0, 0.0), 3: (-140.0, 40.0), 4: (140.0, 40.0)},
                                                    ways=[(1, {"highway": "primary", "oneway": "yes"}, [1, 2]),
                                                          (2, {"highway": "primary", "oneway": "yes"}, [4, 3])], node_tags={}),
                                         z_fn=lambda xy: np.zeros(len(xy))).doc)
    anc = {"a": _pt("a", -100.0, 0.0), "b": _pt("b", 100.0, 40.0)}
    plan = {"seed": 1, "duration_ms": 30_000, "background": [], "actors": [
        {"id": "v", "class": "vehicle", "profile": "vehicle", "drive": {}, "size_mm": [4500, 1800, 1500],
         "route": [{"t_ms": 0, "at": "a", "do": "spawn", "dwell_ms": 0, "via": [], "tol_ms": 3000},
                   {"t_ms": 20_000, "at": "b", "do": "despawn", "dwell_ms": 0, "via": [], "tol_ms": 3000}]}]}
    st = gen.Stage({}, None, anc, lanes=one)
    assert [(e.code, e.reason) for e in gen.lane_traffic(plan, st).errors] == [("I-DISCONNECTED", 612)]
    lt = gen.lane_traffic(_plan(), _stage(None))
    assert {(e.code, e.detail["why"]) for e in lt.errors} == {("I-LANES", "no_lane_graph")} and len(lt.errors) == 4
    nobg = _doc(event_type="rendezvous_handoff", actors=[{"id": "p", "class": "person", "route": [{"t": 0, "at": "jz", "do": "spawn"}]}],
                annotations=[])
    nobg["background"] = []
    assert gen.lane_traffic(_plan(nobg), _stage(graph)) is None                    # 没有车道图车辆：不改动


def test_despawn_dwell_and_first_waypoint_dwell(graph) -> None:
    d = _doc()
    d["actors"][1]["route"] = [{"t": 2, "at": "n_in", "do": "spawn", "dwell_s": 4}, {"t": 50, "at": "w_out", "do": "despawn", "dwell_s": 3}]
    lt = gen.lane_traffic(_plan(d), _stage(graph))
    assert lt.errors == []
    c = lt.index["car_d1"]
    sim = lt.sim
    vis = np.flatnonzero(np.isin(sim.state[:, c], (1, 2)))
    assert vis[0] == 20 and not np.any(np.nan_to_num(sim.v[20:60, c]) > 0)          # 出现于 2 s，停到 2 + 4 s
    stops = [s for s in sim.log["stops"] if s["actor"] == "car_d1"]
    assert len(stops) == 2 and lt.holds["car_d1"][1] >= 3000
    gone = int(np.flatnonzero(sim.state[:, c] == V.STATE_GONE)[0]) * 100
    assert gone >= stops[1]["t_ms"] + lt.holds["car_d1"][1] and gone >= 53_000     # 终点停到计划时刻 50 s + 3 s 后消失


# ---------------------------------------------------------------------------------------------------- 3. 门 2 至门 3
@pytest.fixture(scope="module")
def built(graph):
    plan = _plan()
    st = _stage(graph)
    pf = gen.preflight(plan, st)
    assert pf["errors"] == [], [e.as_dict() for e in pf["errors"]]
    fr, table, report = gen.generate_frames(plan, st, pf)
    return plan, st, pf, fr, table, report


def _verify(built, frames=None, *, stage="same", strict=True, params=None):
    plan, st, pf, fr, table, _rep = built
    stg = st if stage == "same" else stage
    return verify_event(plan, fr if frames is None else frames, table, anchors=ANCHORS, n_scripted=4, stage=stg, arrivals=gen.arrivals(plan, pf),
                        strict=strict, params=params)


def test_frames_follow_traffic_and_report(built) -> None:
    plan, st, pf, fr, table, report = built
    lt = pf["lanes"]
    for v, c in lt.index.items():
        i = next(a["idx"] for a in table if a["id"] == v)
        assert np.array_equal(fr["x_mm"][:, i], np.floor(lt.sim.xy[:, c, 0] * 1000.0 + 0.5).astype(np.int32))
        assert np.array_equal(fr["state"][:, i], lt.sim.state[:, c])
    assert [a["id"] for a in table[:4]] == ["car_d1", "car_d2", "car_d3", "car_t"] and [a["profile"] for a in table] == ["vehicle"] * 8
    k = int(np.flatnonzero(fr["state"][:, 3] == 1)[0])
    assert abs(fr["yaw_cdeg"][0, 3]) <= 100 and abs(fr["yaw_cdeg"][k, 3]) <= 100        # 东行：航向 ≈ 0°
    assert report["lanes"]["audit"]["ok"] and report["lanes"]["digest"] == lt.sim.digest()
    assert {r["actor"] for r in report["arrivals"]} == {"car_t", "car_d1", "car_d2", "car_d3"}
    assert gen.navgraph_ref(plan, st) == {"navgraph_sha256": lt.navgraph_sha256}
    assert pf["boards"] == [] and pf["legs"]["car_t"][0].why == "lane" and pf["retime"]["actors"]["car_t"][0] == 0.0


def test_gate3_vehicle_track_key_events(built) -> None:
    g = _verify(built)
    assert g.errors == [], [e.as_dict() for e in g.errors]
    st = g.stats["consistency_status"]
    assert st["S-OFFMESH"] == "pass" and st["S-ARRIVAL"] == "pass" and st["S-ZGROUND"] == "pass" and g.stats["type_validator"] == "vehicle_track"
    keys = g.truth_kwargs()["key_events"]
    by = {}
    for k in keys:
        by.setdefault((k["type"], k["participants"][0]), []).append(k)
    assert {t for t, _a in by} == {"depart", "turn", "stop", "park", "enter_zone", "exit_zone"}
    (tl,) = by[("turn", "car_t")]
    (tr,) = by[("turn", "car_d1")]
    assert (tl["params"]["turn"], tr["params"]["turn"]) == ("left", "right") and tl["params"]["intersection"] == "j-2"
    assert 80.0 < tl["params"]["heading_change_deg"] < 100.0 and ("turn", "car_d2") not in by          # 直行不产出 turn
    (sp,) = by[("stop", "car_t")]
    assert sp["params"]["cause"] == "dwell" and sp["interval_ms"][1] - sp["interval_ms"][0] >= 5_900
    (pk,) = by[("park", "car_t")]
    lt = built[2]["lanes"]
    assert pk["interval_ms"] == [lt.sim.log["parks"][0]["t_ms"]] * 2
    assert [k["type"] for k in by[("stop", "car_d3")]] == ["stop"] and by[("stop", "car_d3")][0]["interval_ms"][1] >= 44_900
    ez = by[("enter_zone", "car_t")][0]
    assert ez["ref"] and ez["params"]["zone"] == "jz" and ez["declared"] in (True, False)
    assert all(k["id"] == f"{k['type']}-{n}" for t in {k["type"] for k in keys} for n, k in enumerate([x for x in keys if x["type"] == t], 1))
    tv = g.extra["vehicle_track"]
    assert tv["target"] == "car_t" and tv["audit"]["ok"] and tv["key_events_ok"] and tv["tape_mismatch"] == [] and tv["key_mismatch"] == []
    assert tv["audit"]["stats"]["corridor_samples_frac"] == 1.0 and tv["audit"]["stats"]["min_gap_m"] >= V.MIN_GAP_M
    assert g.truth_kwargs()["report_map"] == vt.REPORT_MAP and g.truth_kwargs()["validator_params"]["audit"] == V.AUDIT_DEFAULTS
    truth = gen.build_truth(built[0], "0" * 64, **g.truth_kwargs())
    import jsonschema

    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())).validate(truth)


def test_gate3_detects_tampering_and_mismatch(built) -> None:
    plan, st, pf, fr, table, _rep = built
    f2 = fr.copy()
    ks = np.flatnonzero(f2["state"][:, 3] == 1)[:5]
    f2["y_mm"][ks, 3] += 2500                                                        # 目标横向偏出车道中心线 2.5 m（> 半宽 1.6 m）
    g = _verify(built, f2)
    whys = {e.detail.get("why") for e in g.errors if e.code == "S-TYPE"}
    assert {"tape_mismatch", "audit"} <= whys and {e.reason for e in g.errors} == {613}
    assert "S-OFFMESH" in _codes(g.errors) and g.extra["vehicle_track"]["audit"]["counts"]["off_lane"] == 1
    g = _verify(built, f2, strict=False)
    assert g.errors == [] and "S-TYPE" in _codes(g.warnings)
    lt = pf["lanes"]
    saved = copy.deepcopy(lt.sim.log["parks"])
    try:
        lt.sim.log["parks"][0]["t_ms"] += 1000                                       # 生成决策与磁带关键事件不符
        g = _verify(built)
        (km,) = [e for e in g.errors if e.detail.get("why") == "key_mismatch"]
        assert km.detail["mismatches"][0]["type"] == "park"
    finally:
        lt.sim.log["parks"] = saved
    g = _verify(built, stage=None)
    assert g.errors == [] and "W-TYPE-LANES" in _codes(g.warnings) and g.extra["vehicle_track"]["available"] is False
    plan2 = copy.deepcopy(plan)
    plan2["actors"][3]["route"][1]["dwell_ms"] = 7000                                # plan 与磁带不再对应：重算的车流与磁带不一致
    g = verify_event(plan2, fr, table, anchors=ANCHORS, n_scripted=4, stage=st, strict=True)
    assert "tape_mismatch" in {e.detail.get("why") for e in g.errors}


# ---------------------------------------------------------------------------------------------------- 4. 合成世界包端到端
OVERLAY_ANCHORS = [{**a, **({"seed": True} if a["id"] == "w_in" else {})} for a in ANCHORS.values()]


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    for d in (Path(__file__).resolve().parents[1] / "world", Path(__file__).resolve().parent):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("veh")
    mp = pytest.MonkeyPatch()
    w = build_synth_world(tmp, mp)
    res = L.write_lanes(w, "synth-glb", "0" * 64, _roads(), sources=[{"name": "synthetic", "sha256": "1" * 64}],
                        z_fn=lambda xy: np.zeros(len(xy)))
    wj = json.loads((w / "world.json").read_text())
    wj["layers"].append({"id": res.layer.id, "type": "navgraph", "role": "geometry", "format": L.FORMAT, "href": L.HREF, "status": "ready"})
    (w / "world.json").write_text(json.dumps(wj, indent=1))
    yield w, tmp, res.doc["awr"]["content_sha256"]
    mp.undo()


def _compile(synth, ev: dict, name: str, **kw):
    from awr.sim.events.compile import CompileOptions, compile_event

    w, tmp, _sha = synth
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps({"schema": "awr.anchors.v1", "extends": "world", "world_id": "synth-glb", "anchors": OVERLAY_ANCHORS}))
    (d / "event.json").write_text(json.dumps(ev))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", **kw))


def test_synth_world_compile_strict(synth) -> None:
    import jsonschema

    r = _compile(synth, _doc(), "vt")
    assert r.status == "compiled" and r.reason is None, json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    man = json.loads((r.out / "tape.json").read_text())
    from awr.contracts._paths import schema_path
    from awr.sim.schemas import contracts_registry

    for name, doc in (("truth.v1", truth), ("tape_manifest.v1", man), ("tape.v1", man["header"])):
        sch = json.loads(schema_path(f"event/{name}.schema.json").read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(sch, registry=contracts_registry()).validate(doc)
    assert {k["type"] for k in truth["key_events"]} >= {"depart", "turn", "stop", "park", "enter_zone", "exit_zone"}
    assert truth["report_map"] == vt.REPORT_MAP and truth["validator_params"]["match_ms"] == 100
    assert man["header"]["world"]["navgraph_sha256"] == man["report"]["lanes"]["navgraph_sha256"] == synth[2]
    assert man["report"]["lanes"]["audit"]["ok"] and man["report"]["lanes"]["log"]["safety_stops"] == 0
    rep = json.loads((r.out / "compile_report.json").read_text())
    st = rep["gate3"]["stats"]["consistency_status"]
    assert st["S-OFFMESH"] == st["S-ARRIVAL"] == st["S-ZGROUND"] == "pass"
    tv = rep["gate3"]["extra"]["vehicle_track"]
    assert tv["key_events_ok"] and tv["reacquire"]["oracle"]["los_available"] is True
    r2 = _compile(synth, _doc(), "vt-again")
    assert r2.tape["content_sha256"] == r.tape["content_sha256"] and r2.truth["truth_sha256"] == r.truth["truth_sha256"]
    assert json.loads((r2.out / "tape.json").read_text())["report"]["lanes"]["digest"] == man["report"]["lanes"]["digest"]
    late = _doc()
    late["actors"][3]["route"][1]["t"] = 8
    r3 = _compile(synth, late, "vt-late", preflight_only=True)
    assert r3.status == "infeasible" and r3.reason == 612 and "I-LATE" in {e["code"] for e in r3.gates[1]["errors"]}


# ---------------------------------------------------------------------------------------------------- 5. crop 实数据
@pytest.fixture(scope="module")
def crop(tmp_path_factory: pytest.TempPathFactory):
    if not (CROP / "world.json").exists() or gen.load_lane_graph(CROP) is None:
        pytest.skip("needs worlds/oryahovo-crop1 with the nav.graph layer")
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("crop-veh")
    return compile_event(CROP_EVENT / "event.yaml", CROP, opts=CompileOptions(out=tmp / "v1", cache_dir=tmp / "cache"))


def test_crop_handoff_on_lane_graph(crop) -> None:
    from awr.sim.events import tape as tp

    r = crop
    assert r.status == "compiled" and r.reason is None, json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    ck = {c["id"]: c["verified"] for c in truth["annotation_checks"]}
    assert ck == {"encounter_AB": True, "board_BC": True, "enter_AZ": True, "cross_C": True}
    assert len(truth["relation_checks"]) == 2 and all(c["verified"] for c in truth["relation_checks"])
    man = json.loads((r.out / "tape.json").read_text())
    lanes = man["report"]["lanes"]
    assert [v["id"] for v in lanes["vehicles"]] == ["vehicle_C"] and lanes["audit"]["ok"]
    au = lanes["audit"]["stats"]
    assert au["corridor_samples_frac"] == 1.0 and au["lat_max_m"] <= 0.002 and au["max_corridor_excess_m"] == 0.0
    assert man["header"]["world"]["navgraph_sha256"] == gen.load_lane_graph(CROP).head["content_sha256"]
    rep = json.loads((r.out / "compile_report.json").read_text())
    st = rep["gate3"]["stats"]["consistency_status"]
    assert all(st[k] == "pass" for k in ("S-OFFMESH", "S-ZGROUND", "S-ARRIVAL", "S-CONTAINER"))
    board = {b["actor"]: b for b in rep["boards"]}["person_B"]
    assert board["feasible"] and board["t_board_s"] == 255.0
    t = tp.open_tape(r.out)
    f = np.concatenate([t.block_frames(i) for i in range(len(t.blocks))])
    i = [a["id"] for a in t.header["actors"]].index("vehicle_C")
    vis = np.isin(f["state"][:, i], (1, 2))
    xy = np.stack([f["x_mm"][vis, i], f["y_mm"][vis, i]], 1) / 1000.0
    assert float(np.linalg.norm(xy[:, None] - np.asarray(UAV_PADS)[None], axis=-1).min()) >= 15.0
