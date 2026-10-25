"""门 3 语义验证（D3-AC-06 后三句；M23-AC-015、016、017 的 P0a 部分；M23 §6.9、§6.10.1）。

1. 构造帧数据（不需要世界包）：四类状态转移、相遇与 zone 检测器的判据与区间；声明匹配、strict 613 与 lenient 告警；
   未声明的显著事件以 `declared: false` 写入、显著性不足者只计数；关系 before、after、during、overlaps 正反用例各 2 条，
   causes 时序前提违反返回 613；一致性检查注入重叠、离网、z 偏移 5 cm、容器指向非车辆；天空可见度与未声明遮挡；不完整区域；
   注册表自动收集与重复登记。
2. 合成世界（open3d）：编译产物 `truth/r1/event_truth.json` 通过 schema，未声明的上车与进出建筑为 `declared: false`；
   声明不成立时 strict 返回 613、不发布产物、CLI 退出码 4，lenient 编译通过并标记 `strict = false`；`truth_sha256` 可复算。
3. 示例事件 `crop-handoff-001`（crop 世界包）：strict 编译通过，相遇、上车、进入建筑、车辆经过 4 条声明全部验证成立，关系成立；
   在其磁带上改写声明：错误窗口 strict 613、删去上车声明后上车以 `declared: false` 写入、`causes` 违反返回 613。
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.sim.events.tape import NO_CONTAINER, RECORD
from awr.sim.events.verify import CHECKS, DETECTORS, load_quality, registry, verify_event

ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"
CROP_EVENT = ROOT / "configs" / "events.d3" / "crop-handoff-001"
TICK = 10                                              # 每秒 tick 数


# ---------------------------------------------------------------------- 1. 构造帧数据
def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


ANCHORS = {a["id"]: a for a in [
    {"id": "door1", "kind": "door", "profile": "person", "xy": [30.0, 0.0], "building": "b1"},
    {"id": "door2", "kind": "door", "profile": "person", "xy": [-30.0, 0.0], "building": "b2"},
    {"id": "plaza", "kind": "zone", "profile": "person", "polygon": _sq(20.0, 0.0, 2.0)},
    {"id": "lot", "kind": "zone", "profile": "vehicle", "polygon": [[25.0, 2.0], [50.0, 2.0], [50.0, 10.0], [25.0, 10.0]]},
    {"id": "street", "kind": "zone", "profile": "vehicle", "polygon": _sq(60.0, 5.0, 3.0)},
]}
IDS = ["v1", "p1", "p2", "p3"]                         # 演员 idx 次序（车辆在前，与 gen.actor_order 一致）


def _plan(annotations=(), relations=(), event_type="rendezvous_handoff") -> dict:
    route = {"p1": [{"at": "plaza", "do": "spawn"}, {"at": "plaza", "do": "wait"}, {"at": "door1", "do": "enter_building"},
                    {"at": "door1", "do": "exit_building"}], "p2": [{"at": "plaza", "do": "spawn"}], "p3": [{"at": "plaza", "do": "spawn"}],
             "v1": [{"at": "lot", "do": "spawn"}]}
    return {"event_id": "synth-verify", "version": 1, "event_type": event_type, "tick_hz": 10, "duration_ms": 60_000,
            "actors": [{"id": i, "class": "vehicle" if i[0] == "v" else "person", "route": route[i]} for i in IDS],
            "annotations": list(annotations), "relations": list(relations)}


TABLE = [{"idx": k, "id": i, "class": "vehicle" if i[0] == "v" else "person", "scripted": True,
          "profile": "vehicle" if i[0] == "v" else "person"} for k, i in enumerate(IDS)]


def _frames() -> np.ndarray:
    """60 s、10 Hz。p1：广场等待 → 东行 → 广场等待（与 p2 相遇）→ 进 door1 → 出门东行；p2：等待 → 走到路缘上车 → 下车；
    p3：在 p1 旁 2 m 停留 5.5 s 后离场（相遇显著性 0.275 < 0.3）；v1：lot 内等待 → 驶向停靠点等待 → 驶出经过 street。"""
    n_t = 60 * TICK + 1
    t = np.arange(n_t) / TICK
    f = np.zeros((n_t, len(IDS)), RECORD)
    f["container"] = NO_CONTAINER
    pos = np.zeros((n_t, len(IDS), 2))
    st = np.zeros((n_t, len(IDS)), np.uint8)

    def seg(i: int, t0: float, t1: float, a, b, s: int) -> None:
        m = (t >= t0) & (t < t1)
        u = ((t[m] - t0) / (t1 - t0))[:, None]
        pos[m, i] = np.asarray(a) + u * (np.asarray(b) - np.asarray(a))
        st[m, i] = s

    v, p1, p2, p3 = range(4)
    seg(p1, 0, 10, (0, 0), (0, 0), 2)
    seg(p1, 10, 20, (0, 0), (20, 0), 1)                 # 2 m/s，经过 x ∈ [6, 16] 的遮挡廊（13–18 s）
    seg(p1, 20, 30, (20, 0), (20, 0), 2)
    seg(p1, 30, 35, (20, 0), (30, 0), 1)
    seg(p1, 35, 45, (30, 0), (30, 0), 4)                # 进入建筑（door1）
    seg(p1, 45, 50, (30, 0), (40, 0), 1)
    seg(p1, 50, 61, (40, 0), (40, 0), 2)
    seg(p2, 0, 30, (21, 1), (21, 1), 2)
    seg(p2, 30, 40, (21, 1), (27, 4), 1)
    seg(p2, 50, 61, (47, 4), (47, 4), 2)                # 下车点
    seg(p3, 0, 5.55, (0, 2), (0, 2), 2)
    seg(p3, 5.55, 61, (0, 2), (0, 2), 5)
    seg(v, 0, 42, (27, 5), (27, 5), 2)
    seg(v, 42, 48, (27, 5), (47, 5), 1)
    seg(v, 48, 52, (47, 5), (47, 5), 2)
    seg(v, 52, 61, (47, 5), (80, 5), 1)
    m = (t >= 40) & (t < 50)                            # p2 在车内：位置与车辆相同
    pos[m, p2], st[m, p2] = pos[m, v], 3
    f["container"][m, p2] = v
    f["x_mm"] = np.floor(pos[..., 0] * 1000 + 0.5).astype(np.int32)
    f["y_mm"] = np.floor(pos[..., 1] * 1000 + 0.5).astype(np.int32)
    f["state"] = st
    return f


def _ann(id_, type_, who, win, **params) -> dict:
    a = {"id": id_, "type": type_, "participants": list(who), "window": list(win)}
    if params:
        a["params"] = params
    return a


ANNS = [_ann("enc", "proximity_encounter", ["p1", "p2"], [15, 35]),
        _ann("board", "board_vehicle", ["p2", "v1"], [38, 42]),
        _ann("alight", "alight_vehicle", ["p2", "v1"], [48, 52]),
        _ann("enter", "enter_building", ["p1"], [30, 40], anchor="door1"),
        _ann("exit", "exit_building", ["p1", "door1"], [44, 46]),        # 参与者中的锚点 id 归入 params.anchor
        _ann("lot_pass", "zone_pass", ["v1"], [0, 60], zone="lot", buffer_m=0),
        _ann("street_pass", "zone_pass", ["v1"], [50, 60], zone="street", buffer_m=0)]


def _verify(anns=ANNS, rels=(), frames=None, **kw):
    return verify_event(_plan(anns, rels), _frames() if frames is None else frames, TABLE, anchors=ANCHORS, **kw)


def _codes(issues) -> list[str]:
    return sorted(i.code for i in issues)


def _ev(g, eid: str) -> dict:
    return next(e for e in g.events if e["id"] == eid)


def test_registry_collects_p0a_detectors_and_checks() -> None:
    registry.collect()
    assert set(DETECTORS) >= {"proximity_encounter", "board_vehicle", "alight_vehicle", "enter_building", "exit_building", "zone_enter",
                              "zone_exit", "zone_pass", "aerial_occlusion"}
    assert set(CHECKS) == {"S-OFFMESH", "S-ZGROUND", "S-ARRIVAL", "S-OVERLAP", "S-CONTAINER"}
    with pytest.raises(RuntimeError, match="duplicate"):
        registry.detector("board_vehicle")(lambda ctx, p: [])
    registry.collect()                                  # 幂等
    assert DETECTORS["board_vehicle"].instant and DETECTORS["enter_building"].anchor_param == "anchor"


def test_detectors_and_declared_matches() -> None:
    g = _verify()
    assert g.errors == [] and all(c["verified"] for c in g.annotation_checks)
    enc = _ev(g, "enc")
    assert enc["declared"] and enc["participants"] == ["p1", "p2"] and enc["evidence"]["min_dist_m"] == pytest.approx(1.188, abs=1e-3)
    a, b = enc["interval_ms"]
    assert 18_000 < a < 20_000 and 32_000 < b < 33_000 and enc["salience"] == pytest.approx(min(1.0, (b - a) / 20_000), abs=1e-3)
    assert _ev(g, "board")["interval_ms"] == [40_000, 40_000] and _ev(g, "board")["participants"] == ["p2", "v1"]
    assert _ev(g, "alight")["interval_ms"] == [50_000, 50_000] and _ev(g, "alight")["salience"] == 1.0
    assert _ev(g, "enter")["evidence"] == {"door": "door1", "door_dist_m": 0.0} and _ev(g, "enter")["interval_ms"] == [35_000, 35_000]
    assert _ev(g, "exit")["interval_ms"] == [45_000, 45_000]
    lp = _ev(g, "lot_pass")
    assert lp["interval_ms"][0] == 0 and 51_000 < lp["interval_ms"][1] < 53_000 and lp["evidence"] == {"zone": "lot"}
    sp = _ev(g, "street_pass")
    assert 54_000 < sp["interval_ms"][0] < sp["interval_ms"][1] <= 60_000 and sp["salience"] == 0.5
    ck = {c["id"]: c for c in g.annotation_checks}
    assert ck["board"]["computed_ms"] == [[40_000, 40_000]] and ck["board"]["window_ms"] == [38_000, 42_000] and ck["board"]["n_candidates"] == 1
    assert g.detector_params["annotation_overrides"]["exit"]["anchor"] == "door1"
    assert g.detector_params["proximity_encounter"] == {"dist_m": 3.0, "min_dur_s": 5.0} and g.detector_params["salience_min"] == 0.3


def test_encounter_threshold_and_door_anchor() -> None:
    g = _verify([_ann("enc", "proximity_encounter", ["p1", "p2"], [15, 35], dist_m=1.0)], strict=True)
    assert _codes(g.errors) == ["S-MISMATCH"] and g.errors[0].reason == 613             # 最近 1.19 m > 1 m
    g = _verify([_ann("enc", "proximity_encounter", ["p1", "p2"], [15, 35], min_dur_s=20)])
    assert _codes(g.errors) == ["S-MISMATCH"]                                          # 约 12 s < 20 s
    g = _verify([_ann("enter", "enter_building", ["p1"], [30, 40], anchor="door2")])
    assert _codes(g.errors) == ["S-MISMATCH"] and g.errors[0].detail["n_candidates"] == 0   # door2 距 60 m
    g = _verify([_ann("enter", "enter_building", ["p1"], [30, 40], door_m=0.0)])
    assert _codes(g.errors) == ["S-MISMATCH"]


def test_strict_613_and_lenient_warning() -> None:
    hidden = [*ANNS, _ann("hidden", "board_vehicle", ["p1", "v1"], [0, 60])]               # p1 从未上车
    g = _verify(hidden, strict=True)
    (e,) = g.errors
    assert e.code == "S-MISMATCH" and e.reason == 613 and e.path == "/annotations/7" and e.detail["annotation"] == "hidden"
    assert next(c for c in g.annotation_checks if c["id"] == "hidden") == {"id": "hidden", "type": "board_vehicle", "window_ms": [0, 60_000],
                                                                           "verified": False, "computed_ms": [], "n_candidates": 0}
    g = _verify(hidden, strict=False)
    assert g.errors == [] and "S-MISMATCH" in _codes(g.warnings) and g.summary(1, None)["declared_failed"] == 1
    assert not g.strict
    g = _verify([_ann("late", "board_vehicle", ["p2", "v1"], [41, 45])])               # 有候选、窗口外
    assert g.errors[0].detail["n_candidates"] == 1 and g.errors[0].detail["candidates_ms"] == [[40_000, 40_000]]


def test_undeclared_salient_events_written_with_declared_false() -> None:
    g = _verify([_ann("enc", "proximity_encounter", ["p1", "p2"], [15, 35])])
    und = [e for e in g.events if not e["declared"]]
    kinds = {(e["type"], tuple(e["participants"])) for e in und}
    assert kinds == {("board_vehicle", ("p2", "v1")), ("alight_vehicle", ("p2", "v1")), ("enter_building", ("p1",)), ("exit_building", ("p1",))}
    assert [e["id"] for e in und] == ["e001", "e002", "e003", "e004"] and all(e["salience"] == 1.0 and "annotation" not in e for e in und)
    assert [e["interval_ms"][0] for e in und] == sorted(e["interval_ms"][0] for e in und)
    assert _codes(g.warnings) == ["W-UNDECLARED"] * 4 and g.errors == []
    # p1 与 p3 的 5.5 s 相遇显著性 0.275 < 0.3：不写入真值，只计数
    assert not any(e["type"] == "proximity_encounter" and "p3" in e["participants"] for e in g.events)
    assert g.stats["undeclared_low_salience"] == {"proximity_encounter": 1}
    # 作者声明的显著性覆盖计算值；zone 类只对注释引用的 zone 检测（显著性 0.5 ≥ 0.3，未声明的进入、离开与经过写入）
    g = _verify([{**_ann("enc", "proximity_encounter", ["p1", "p2"], [15, 35]), "salience": 0.9}, ANNS[5]])
    assert _ev(g, "enc")["salience"] == 0.9
    zs = sorted((e["type"], e["participants"][0], e["interval_ms"][0]) for e in g.events if e["type"].startswith("zone_") and not e["declared"])
    assert ("zone_enter", "v1", 0) in zs and ("zone_exit", "v1", 52_900) in zs and ("zone_exit", "p2", 40_000) in zs   # p2 上车即离开 lot
    assert ("zone_enter", "p2", 50_000) in zs and not any(t == "zone_pass" and a == "v1" for t, a, _ in zs)     # v1 的经过已声明
    assert all(e["evidence"] == {"zone": "lot"} for e in g.events if e["type"].startswith("zone_"))


REL_POS = {"before": [("enc", "board"), ("board", "alight")], "after": [("alight", "board"), ("exit", "enter")],
           "during": [("board", "lot_pass"), ("enc", "lot_pass")], "overlaps": [("enc", "lot_pass"), ("lot_pass", "enc")],
           "causes": [("enter", "exit")], "enables": [("board", "alight")]}
REL_NEG = {"before": [("alight", "board"), ("exit", "enter")], "after": [("board", "alight"), ("enter", "exit")],
           "during": [("lot_pass", "board"), ("street_pass", "lot_pass")], "overlaps": [("enc", "street_pass"), ("board", "enc")],
           "causes": [("exit", "enter")]}


def test_relations_positive_and_negative() -> None:
    rels = [{"src": s, "rel": r, "dst": d} for r, pairs in REL_POS.items() for s, d in pairs]
    g = _verify(ANNS, rels)
    assert g.errors == [] and [c["verified"] for c in g.relation_checks] == [True] * len(rels)
    for rel, pairs in REL_NEG.items():
        for s, d in pairs:
            g = _verify(ANNS, [{"src": s, "rel": rel, "dst": d}], strict=True)
            assert _codes(g.errors) == ["S-RELATION"] and g.errors[0].reason == 613, (s, rel, d)
            assert g.relation_checks == [{"src": s, "rel": rel, "dst": d, "verified": False}]
    g = _verify(ANNS, [{"src": "exit", "rel": "causes", "dst": "enter"}], strict=False)
    assert g.errors == [] and _codes(g.warnings).count("S-RELATION") == 1
    # 未验证的声明使引用它的关系不成立
    g = _verify([*ANNS, _ann("hidden", "board_vehicle", ["p1", "v1"], [0, 60])], [{"src": "enc", "rel": "before", "dst": "hidden"}])
    assert _codes(g.errors) == ["S-MISMATCH", "S-RELATION"]


class _Ground:
    def ground_z_batch(self, xy, zh):
        return np.zeros(len(xy)), np.ones(len(xy), bool)


def _stage(hole: bool = False):
    from types import SimpleNamespace

    from awr.sim.events.nav import NavGrid

    walk = np.ones((200, 300), bool)
    if hole:
        walk[99:102, 107:113] = False                   # x ∈ [7, 13)、y ∈ [−1, 2) 的不可走格（p1 沿 y = 0 穿过，距可走格 1 m）
    nav = NavGrid("person", -100.0, -100.0, 300, 200, np.zeros((200, 300)), walk)
    return SimpleNamespace(nav={"person": nav, "vehicle": NavGrid("vehicle", -100.0, -100.0, 300, 200, np.zeros((200, 300)),
                                                                  np.ones((200, 300), bool))}, ground=_Ground())


ARR = [{"actor": "p1", "waypoint": 1, "at": "plaza", "do": "wait", "t_plan_ms": 20_000, "t_arrive_ms": 20_000, "late_ms": 0,
        "xy_plan_m": [20.0, 0.0], "xy_mm": [20_000, 0], "err_mm": 0.0}]


def test_consistency_checks_and_injections() -> None:
    g = _verify(stage=_stage(), arrivals=ARR)
    st = g.stats["consistency_status"]
    assert st == {"S-ARRIVAL": "pass", "S-CONTAINER": "pass", "S-OFFMESH": "pass", "S-OVERLAP": "pass", "S-ZGROUND": "pass"} and g.errors == []
    assert g.consistency["S-OVERLAP"]["scripted_scripted_min_m"] > 0.5 and g.consistency["S-CONTAINER"]["n_frames"] == 100
    # 重叠：p3 移到 p1 旁 0.3 m
    f = _frames()
    f["y_mm"][:55, 3] = 300
    g = _verify(frames=f)
    assert _codes(g.errors) == ["S-OVERLAP"] and g.errors[0].reason == 613 and g.errors[0].detail["worst"][0]["dist_m"] == 0.3
    # 离网：p1 经过不可走格
    g = _verify(stage=_stage(hole=True))
    assert _codes(g.errors) == ["S-OFFMESH"] and g.consistency["S-OFFMESH"]["worst_m"] >= 0.5
    # z 偏移 5 cm
    f = _frames()
    f["z_mm"][100:110, 1] += 50
    g = _verify(frames=f, stage=_stage())
    assert _codes(g.errors) == ["S-ZGROUND"] and g.consistency["S-ZGROUND"]["n_fail"] == 10 and g.consistency["S-ZGROUND"]["worst_m"] == 0.05
    # 容器指向非车辆（p1）
    f = _frames()
    f["container"][400:500, 2] = 1
    g = _verify(frames=f)
    (e,) = [i for i in g.errors if i.code == "S-CONTAINER"]
    assert {r["why"] for r in e.detail["worst"]} == {"not_vehicle"} and e.reason == 613
    assert _codes(g.errors) == ["S-CONTAINER", "S-MISMATCH", "S-MISMATCH"]          # 上下车的容器变为 p1，声明随之不成立
    # 位置与容器不同、非状态 3 而容器有值
    f = _frames()
    f["x_mm"][420, 2] += 1
    f["container"][10, 1] = 0
    g = _verify(frames=f)
    assert {r["why"] for r in g.errors[0].detail["worst"]} == {"position_differs", "container_without_state3"}
    # 到达延迟
    g = _verify(arrivals=[{**ARR[0], "late_ms": 300, "t_arrive_ms": 20_300}])
    assert _codes(g.errors) == ["S-ARRIVAL"] and g.consistency["S-ARRIVAL"]["worst_late_ms"] == 300
    g = _verify(frames=f, strict=False)
    assert g.errors == [] and "S-CONTAINER" in _codes(g.warnings)


def test_background_overlap_is_warning_until_crowd_avoidance() -> None:
    f = _frames()
    bg = np.zeros((len(f), 1), RECORD)
    bg["container"] = NO_CONTAINER
    bg["state"] = 1
    bg["x_mm"], bg["y_mm"] = f["x_mm"][:, 1:2] + 200, f["y_mm"][:, 1:2]                 # 紧随 p1 0.2 m
    F = np.concatenate([f, bg], 1)
    table = [*TABLE, {"idx": 4, "id": "bg0_0", "class": "person", "scripted": False, "profile": "person"}]
    g = verify_event(_plan(ANNS), F, table, anchors=ANCHORS)
    assert g.errors == [] and "W-BG-OVERLAP" in _codes(g.warnings) and g.consistency["S-OVERLAP"]["bg_scripted_min_m"] == 0.2
    g = verify_event(_plan(ANNS), F, table, anchors=ANCHORS, params={"S-OVERLAP": {"bg_overlap_error": True}})
    assert _codes(g.errors) == ["S-OVERLAP"]


class _Arcade:
    """x ∈ [6, 16] 的廊下：起点在其中的射线全部命中（天空可见度 0），其余不命中。"""

    def __init__(self) -> None:
        self.n = 0

    def raycast_batch(self, o, d, tmax):
        o = np.asarray(o)
        self.n += len(o)
        hit = (o[:, 0] >= 6.0) & (o[:, 0] <= 16.0) & (np.abs(o[:, 1]) < 1.0)
        return np.where(hit, 3.0, np.inf).astype(np.float32), np.zeros(len(o), np.uint32), np.zeros(len(o), np.uint32)


def test_aerial_occlusion_undeclared_and_skyvis() -> None:
    from awr.sim.events.verify.occlusion import sky_dirs

    D = sky_dirs(64, 25.0)
    assert np.allclose(np.linalg.norm(D, axis=1), 1.0) and D[:, 2].min() >= np.sin(np.radians(25.0))
    assert np.allclose(np.diff(np.sort(D[:, 2])), (1 - np.sin(np.radians(25.0))) / 64)        # 等立体角分层
    mesh = _Arcade()
    g = _verify(ANNS, mesh=mesh)
    (occ,) = [e for e in g.events if e["type"] == "aerial_occlusion"]
    assert occ["participants"] == ["p1"] and occ["interval_ms"] == [13_000, 18_000] and not occ["declared"]
    assert occ["salience"] == 0.5 and occ["evidence"] == {"sky_vis_min": 0.0} and occ["id"].startswith("e")
    sv = g.skyvis_1hz
    assert set(sv) == {"p1", "p2", "p3"} and len(sv["p1"]) == 61
    assert sv["p1"][12] == 1.0 and sv["p1"][13] == 0.0 and sv["p1"][36] is None and sv["p2"][45] is None and sv["p3"][10] is None
    assert g.perf["skyvis_rays"] == mesh.n
    g = _verify([*ANNS, _ann("occ", "aerial_occlusion", ["p1"], [10, 20])], mesh=_Arcade())
    assert _ev(g, "occ")["declared"] and g.errors == []
    g = _verify([_ann("occ", "aerial_occlusion", ["p1"], [10, 20])])                             # 无网格：未验证
    assert _codes(g.errors) == ["S-MISMATCH"] and g.errors[0].detail["why"] == "resource_unavailable" and g.skyvis_1hz == {}
    assert "aerial_occlusion" in g.stats["skipped_detectors"]


def test_incomplete_region_from_quality_layer(tmp_path: Path) -> None:
    (tmp_path / "semantic").mkdir()
    fc = {"type": "FeatureCollection", "awr": {"schema": "awr.quality.v1"}, "features": [
        {"type": "Feature", "properties": {"kind": "hole", "reasons_bits": 2}, "geometry": {"type": "Polygon", "coordinates": [_sq(30.0, 0.0, 3.0)]}},
        {"type": "Feature", "properties": {"kind": "low-texture"}, "geometry": {"type": "Polygon", "coordinates": [_sq(40.0, 5.0, 50.0)]}},
        {"type": "Feature", "properties": {"kind": "nodata"}, "geometry": {"type": "MultiPolygon", "coordinates": [[_sq(-50.0, -50.0, 1.0)]]}}]}
    (tmp_path / "semantic" / "quality.geojson").write_text(json.dumps(fc))
    q = load_quality(tmp_path)
    assert len(q) == 2 and load_quality(tmp_path / "none") == [] and load_quality(None) == []
    g = _verify(quality=q)
    inc = {e["id"] for e in g.events if e["in_incomplete_region"]}
    assert inc == {"enter", "exit"} and g.summary(1, "x")["incomplete_region"] == 2


def test_type_validator_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = {}

    def fn(ctx, g3) -> None:
        seen["n"] = len(g3.events)
        g3.extra["key_events"] = [e["id"] for e in g3.events if e["declared"]]

    monkeypatch.setitem(registry.VALIDATORS, "synth_type", fn)
    g = verify_event(_plan(ANNS, event_type="synth_type"), _frames(), TABLE, anchors=ANCHORS)
    assert seen["n"] == len(g.events) and g.extra["key_events"][0] == "lot_pass" and g.stats["type_validator"] == "synth_type"


def test_truth_document_schema_and_hash() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from awr.sim.events import gen

    plan = _plan(ANNS, [{"src": "enc", "rel": "before", "dst": "board"}])
    g = verify_event(plan, _frames(), TABLE, anchors=ANCHORS, stage=_stage(), arrivals=ARR, mesh=_Arcade())
    plan.update({"seed": 1, "world": {"id": "w"}})
    truth = gen.build_truth(plan, "0" * 64, **g.truth_kwargs(), perf=g.perf)
    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())).validate(truth)
    g2 = verify_event(plan, _frames(), TABLE, anchors=ANCHORS, stage=_stage(), arrivals=ARR, mesh=_Arcade())
    t2 = gen.build_truth(plan, "0" * 64, **g2.truth_kwargs(), perf={"wall_ms": -1})
    assert gen.truth_sha256(truth) == gen.truth_sha256(t2)                                    # 与 perf 无关、可复算


# ---------------------------------------------------------------------- 2. 合成世界的编译
@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    for d in (Path(__file__).resolve().parents[1] / "world", Path(__file__).resolve().parent):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("verify")
    mp = pytest.MonkeyPatch()
    yield build_synth_world(tmp, mp), tmp
    mp.undo()


def _compile_synth(synth, ev: dict, name: str, **kw):
    from test_compile import OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    w, tmp = synth
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", **kw))


def test_compile_writes_truth_with_undeclared_events(synth) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from test_compile import EVENT

    from awr.sim.events import gen

    r = _compile_synth(synth, EVENT, "plain")
    assert r.status == "compiled" and r.gates[2]["gate"] == 3 and r.gates[2]["status"] == "warn" and r.gates[2]["errors"] == []
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())).validate(truth)
    assert truth["tape_content_sha256"] == r.tape["content_sha256"] and truth["strict"] is True and truth["truth_rev"] == 1
    got = {(e["type"], tuple(e["participants"]), tuple(e["interval_ms"])) for e in truth["events"] if not e["declared"]}
    assert got >= {("board_vehicle", ("person_b", "vehicle_c"), (80_000, 80_000)), ("enter_building", ("person_c",), (40_000, 40_000)),
                   ("exit_building", ("person_c",), (70_000, 70_000))}
    assert all(not e["declared"] for e in truth["events"]) and {w["code"] for w in r.gates[2]["warnings"]} >= {"W-UNDECLARED"}
    assert r.truth["truth_sha256"] == gen.truth_sha256(truth) and r.truth["undeclared"] == len(truth["events"])
    assert set(truth["skyvis_1hz"]) == {"person_a", "person_b", "person_c"} and truth["perf"]["skyvis_rays"] > 0
    assert {k for k, v in truth["consistency"].items() if v["worst"] and k != "S-OVERLAP"} == set()
    rep = json.loads((r.out / "compile_report.json").read_text())
    assert rep["truth_summary"] == r.truth and rep["gate3"]["stats"]["consistency_status"]["S-OFFMESH"] == "pass"
    r2 = _compile_synth(synth, EVENT, "plain-again")
    assert r2.truth["truth_sha256"] == r.truth["truth_sha256"]


def test_compile_strict_613_and_lenient(synth) -> None:
    from test_compile import EVENT

    from awr.sim.events import __main__ as cli

    ev = copy.deepcopy(EVENT)
    ev["annotations"] = [{"id": "board_bc", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [70, 90]},
                         {"id": "hidden_a", "type": "board_vehicle", "participants": ["person_a", "vehicle_c"], "window": [0, 150]}]
    ev["relations"] = [{"src": "board_bc", "rel": "before", "dst": "hidden_a"}]
    r = _compile_synth(synth, ev, "hidden")
    assert r.status == "failed" and r.reason == 613 and r.out is None and not (synth[1] / "hidden" / "v1").exists()
    assert not any(p.name.startswith("v1.tmp") for p in (synth[1] / "hidden").iterdir())
    assert [e["code"] for e in r.gates[2]["errors"]] == ["S-MISMATCH", "S-RELATION"] and r.gates[2]["status"] == "fail"
    assert r.as_dict()["reason"] == {"code": 613, "name": "EVENT_SEMANTIC_MISMATCH"} and cli.exit_code(r.status, r.gates) == 4
    assert r.truth["truth_sha256"] is None and r.truth["declared_verified"] == 1 and r.truth["declared_failed"] == 1
    r = _compile_synth(synth, ev, "hidden-lenient", strict=False)
    assert r.status == "compiled" and r.gates[2]["errors"] == [] and {"S-MISMATCH", "S-RELATION"} <= {w["code"] for w in r.gates[2]["warnings"]}
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    assert truth["strict"] is False and r.as_dict()["strict"] is False
    assert [c["verified"] for c in truth["annotation_checks"]] == [True, False] and truth["relation_checks"][0]["verified"] is False


# ---------------------------------------------------------------------- 3. 示例事件 crop-handoff-001
@pytest.fixture(scope="module")
def crop(tmp_path_factory: pytest.TempPathFactory):
    if not (CROP / "world.json").exists():
        pytest.skip("needs worlds/oryahovo-crop1")
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("crop")
    r = compile_event(CROP_EVENT / "event.yaml", CROP, opts=CompileOptions(out=tmp / "v1", cache_dir=tmp / "cache"))
    return r, tmp


def test_crop_handoff_four_key_events_verified(crop) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    r, _tmp = crop
    assert r.status == "compiled" and r.reason is None, json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    assert r.gates[2]["errors"] == []
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())).validate(truth)
    ck = {c["id"]: c for c in truth["annotation_checks"]}
    assert {c["type"] for c in ck.values()} == {"proximity_encounter", "board_vehicle", "enter_building", "zone_pass"}
    assert all(c["verified"] for c in ck.values()) and set(ck) == {"encounter_AB", "board_BC", "enter_AZ", "cross_C"}
    assert all(c["verified"] for c in truth["relation_checks"]) and len(truth["relation_checks"]) == 2
    ev = {e["id"]: e for e in truth["events"]}
    assert ev["board_BC"]["interval_ms"] == [255_000, 255_000] and ev["enter_AZ"]["evidence"]["door"] == "ev_building_z_door"
    assert ev["encounter_AB"]["evidence"]["min_dist_m"] < 3.0 and ev["cross_C"]["evidence"] == {"zone": "ev_plaza"}
    assert truth["strict"] is True and r.truth["declared_failed"] == 0
    st = json.loads((r.out / "compile_report.json").read_text())["gate3"]["stats"]["consistency_status"]
    assert all(st[k] == "pass" for k in ("S-OFFMESH", "S-ZGROUND", "S-ARRIVAL", "S-CONTAINER")) and st["S-OVERLAP"] in ("pass", "warn")


def _crop_verify(crop, mutate, strict: bool = True):
    from awr.sim.events import tape as tp

    r, _tmp = crop
    t = tp.open_tape(r.out)
    frames = np.concatenate([t.block_frames(i) for i in range(len(t.blocks))])
    plan = json.loads((r.out / "plan.json").read_text())
    anchors = {a["id"]: a for a in json.loads((r.out / "anchors.json").read_text())["anchors"]}
    mutate(plan)
    return verify_event(plan, frames, t.header["actors"], anchors=anchors, n_scripted=t.n_scripted, strict=strict)


def test_crop_variants_mismatch_undeclared_and_causes(crop) -> None:
    def hidden(plan: dict) -> None:                     # r6 hidden_A 的形式：声明窗口内不存在该事件
        plan["annotations"][0]["window"] = [100, 150]

    g = _crop_verify(crop, hidden)
    assert [i.code for i in g.errors] == ["S-MISMATCH", "S-RELATION"] and {i.reason for i in g.errors} == {613}
    g = _crop_verify(crop, hidden, strict=False)
    assert g.errors == [] and {"S-MISMATCH", "S-RELATION"} <= {w.code for w in g.warnings}

    def drop_board(plan: dict) -> None:
        plan["annotations"] = [a for a in plan["annotations"] if a["id"] != "board_BC"]
        plan["relations"] = []

    g = _crop_verify(crop, drop_board)
    (b,) = [e for e in g.events if e["type"] == "board_vehicle"]
    assert b["declared"] is False and b["id"].startswith("e") and "annotation" not in b and b["participants"] == ["person_B", "vehicle_C"] and b["salience"] == 1.0
    assert g.errors == [] and "W-UNDECLARED" in {w.code for w in g.warnings}

    def causes(plan: dict) -> None:
        plan["relations"] = [{"src": "enter_AZ", "rel": "causes", "dst": "board_BC"}]       # 290 s 晚于 255 s

    g = _crop_verify(crop, causes)
    assert [i.code for i in g.errors] == ["S-RELATION"] and g.errors[0].reason == 613
