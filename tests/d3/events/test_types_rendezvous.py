"""交汇与交接事件类型 `rendezvous_handoff`（D3-AC-62 交汇与交接条款；M23-AC-037、039 的交汇与交接部分，M23-AC-020 的 OpenLABEL；
M23 §15.3、FR-042、058、071、074、077；M27-FR-071）。

1. 门 1：P0b 形式（给出 `roles`）缺少参与者与物体携带时一次报出全部 `E-TYPE`（611，`type_requirement`）；P0a 形式不检查；
   拾取与放下的目标须为物体（`E-CARRY-TARGET`），携带次序（`E-CARRY-ORDER`：放下者不是携带者、已被携带时再拾取、物体被携带时
   despawn）。
2. 构造帧数据：`meet`、`handover`、`disperse`、`board`、`alight` 关键事件与关系边（handover during meet、meet before disperse、
   meet before board、handover causes board）由验证器产出且与磁带一致；物品容器转移（直接交接与经地面交接）与 plan 携带日程逐帧
   一致；篡改磁带的容器或放下位置返回 `S-CARRY` 与 `S-HANDOVER`（strict 613，lenient 告警）；交汇不足 5 s 的交接只告警且计划交接
   报 `S-HANDOVER`；P0b 形式无交汇报 `S-KEY-MEET`，`roles` 只计参与者；真值通过 truth.v1、事件图通过 graph.v1；OpenLABEL 由事件图
   与真值导出，参考解析器 0 问题、元素与关系数与事件图一致（设 `AWR_OPENLABEL_SCHEMA` 为官方 OpenLABEL 1.0 JSON Schema 路径时另以
   官方 schema 校验）。
3. 合成世界（open3d）：带拾取、直接交接与上车的 P0b 事件 strict 编译通过；磁带中物体容器序列与 plan 一致、`S-CONTAINER` 通过；
   `event_graph.json` 写入真值层；两次编译 `content_sha256` 与 `truth_sha256` 相同；`export_event` 导出 OpenLABEL；拾取点距物体
   ≥ 2 m 时门 2 返回 `I-CARRY`（612）。
4. 示例事件 `crop-handoff-001`（P0a 形式）：关键事件 meet、board、disperse、enter_building 与关系边，OpenLABEL 导出可读。
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.sim.events.dsl import carry_schedule, gate1
from awr.sim.events.tape import NO_CONTAINER, RECORD
from awr.sim.events.verify import registry, verify_event
from awr.sim.events.verify.rendezvous import carry_labels, holds, planned_handovers, requirements

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "packages" / "contracts" / "event"
CROP = ROOT / "worlds" / "oryahovo-crop1"
CROP_EVENT = ROOT / "configs" / "events.d3" / "crop-handoff-001"
TICK = 10


def _schema(name: str):
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(json.loads((CONTRACTS / name).read_text()))


def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


# ---------------------------------------------------------------------- 1. 门 1
GATE1_ANCHORS = {a["id"]: a for a in [
    {"id": "a_west", "kind": "point", "profile": "person", "xy": [-20.0, 0.0]},
    {"id": "a_kiosk", "kind": "point", "profile": "person", "xy": [0.0, 0.0]},
    {"id": "a_plaza", "kind": "zone", "profile": "person", "polygon": _sq(20.0, 0.0, 5.0)},
    {"id": "a_east", "kind": "point", "profile": "person", "xy": [60.0, 0.0]},
]}


def _doc(typed: bool = True) -> dict:
    r = {"participant": ["participant"]} if typed else {}
    return {"schema": "drone4d.event.v1", "event_id": "rdv-gate1", "version": 1, "event_type": "rendezvous_handoff", "world": {"id": "w"},
            "seed": 1, "duration_s": 120,
            "actors": [
                {"id": "p_a", "class": "person", **({"roles": r["participant"]} if typed else {}),
                 "route": [{"t": 0, "at": "a_west", "do": "spawn"}, {"t": 20, "at": "a_kiosk", "do": "pickup", "target": "pkg"},
                           {"t": 40, "at": "a_plaza", "do": "drop", "target": "pkg", "dwell_s": 10}, {"t": 100, "at": "a_east"}]},
                {"id": "p_b", "class": "person", **({"roles": r["participant"]} if typed else {}),
                 "route": [{"t": 0, "at": "a_east", "do": "spawn"}, {"t": 40, "at": "a_plaza", "do": "pickup", "target": "pkg", "dwell_s": 10},
                           {"t": 100, "at": "a_west"}]},
                {"id": "pkg", "class": "object", "route": [{"t": 0, "at": "a_kiosk", "do": "spawn"}]},
            ]}


def _gate1(doc: dict) -> list:
    pytest.importorskip("jsonschema")
    _plan, errs, _warns = gate1(doc, GATE1_ANCHORS)
    return errs


def test_gate1_type_requirements_and_p0a_form() -> None:
    assert _gate1(_doc()) == [] and _gate1(_doc(typed=False)) == []
    sched = carry_schedule(_doc()["actors"])
    assert [(s.t_ms, s.kind, s.actor) for s in sched["pkg"]] == [(20_000, 1, "p_a"), (40_000, 0, "p_a"), (40_000, 1, "p_b")]
    doc = _doc()
    doc["actors"][1]["roles"] = ["distractor"]                      # 只剩 1 名参与者
    doc["actors"][2]["roles"] = ["participant"]                     # 物体作参与者
    for a in doc["actors"][:2]:
        a["route"] = [w for w in a["route"] if w.get("do") not in ("pickup", "drop")]
    errs = [e for e in _gate1(doc) if e.code == "E-TYPE"]
    assert {e.reason for e in errs} == {611} and all(e.detail["why"] == "type_requirement" for e in errs)
    assert sorted(e.detail["rule"] for e in errs) == ["object_carry", "participant_class", "participant_count"]
    assert requirements(doc, GATE1_ANCHORS) and requirements(_doc(typed=False), GATE1_ANCHORS) == []
    p0a = copy.deepcopy(doc)                                        # 去掉 roles：P0a 形式不检查必备配置
    for a in p0a["actors"]:
        a.pop("roles", None)
    assert not [e for e in _gate1(p0a) if e.code == "E-TYPE"]


def test_gate1_carry_target_and_order() -> None:
    doc = _doc()
    doc["actors"][1]["route"][1]["target"] = "p_a"                  # 拾取目标不是物体
    errs = _gate1(doc)
    assert [e.code for e in errs if e.code.startswith("E-CARRY")] == ["E-CARRY-TARGET"] and errs[0].path == "/actors/1/route/1/target"
    doc = _doc()
    doc["actors"][1]["route"][1]["t"] = 30                          # p_b 在 p_a 放下（40 s）之前拾取：物体仍被 p_a 携带
    errs = [e for e in _gate1(doc) if e.code == "E-CARRY-ORDER"]
    assert [e.detail["why"] for e in errs] == ["carried"] and errs[0].reason == 611 and errs[0].detail["holder"] == "p_a"
    assert errs[0].path == "/actors/1/route/1"
    doc = _doc()
    doc["actors"][2]["route"] = [{"t": 25, "at": "a_kiosk", "do": "spawn"}]           # 拾取（20 s）早于物体出现
    assert [e.detail["why"] for e in _gate1(doc) if e.code == "E-CARRY-ORDER"] == ["before_spawn", "not_holder"]
    doc = _doc()
    doc["actors"][2]["route"].append({"t": 70, "at": "a_kiosk", "do": "despawn"})
    assert [e.detail["why"] for e in _gate1(doc) if e.code == "E-CARRY-ORDER"] == ["despawn_carried"]


# ---------------------------------------------------------------------- 2. 构造帧数据
ANCHORS = {a["id"]: a for a in [
    {"id": "kiosk", "kind": "point", "profile": "person", "xy": [0.0, 0.0]},
    {"id": "plaza", "kind": "zone", "profile": "person", "polygon": _sq(20.5, 0.5, 2.0)},
    {"id": "lot", "kind": "zone", "profile": "vehicle", "polygon": [[25.0, 2.0], [50.0, 2.0], [50.0, 10.0], [25.0, 10.0]]},
]}
IDS = ["v1", "o1", "p1", "p2", "p3"]                     # 车辆在前，其余按 id（与 gen.actor_order 一致）
CLS = {"v1": "vehicle", "o1": "object", "p1": "person", "p2": "person", "p3": "person"}
TABLE = [{"idx": k, "id": i, "class": CLS[i], "scripted": True, "profile": {"vehicle": "vehicle", "object": None}.get(CLS[i], "person"),
          "size_mm": {"vehicle": [4500, 1800, 1500], "object": [600, 400, 400]}.get(CLS[i], [500, 300, 1750])} for k, i in enumerate(IDS)]
V, OBJ, P1, P2, P3 = range(5)


def _plan(*, typed: bool = True, pickup_b: float = 20.0, annotations=(), relations=()) -> dict:
    """p1 在 2 s 拾取 o1，20 s 在广场放下；p2 在 `pickup_b` s 拾取（20 s 为直接交接）、40 s 上 v1、50 s 下车；p3 为旁观者。"""
    routes = {
        "p1": [{"t_ms": 0, "at": "kiosk", "do": "spawn"}, {"t_ms": 2000, "at": "kiosk", "do": "pickup", "target": "o1"},
               {"t_ms": 20_000, "at": "plaza", "do": "drop", "target": "o1"}, {"t_ms": 35_000, "at": "kiosk", "do": "pass"}],
        "p2": [{"t_ms": 0, "at": "plaza", "do": "spawn"}, {"t_ms": round(pickup_b * 1000), "at": "plaza", "do": "pickup", "target": "o1"},
               {"t_ms": 40_000, "at": "lot", "do": "board", "target": "v1"}, {"t_ms": 50_000, "at": "lot", "do": "alight", "target": "v1"}],
        "p3": [{"t_ms": 0, "at": "kiosk", "do": "spawn"}], "o1": [{"t_ms": 0, "at": "kiosk", "do": "spawn"}],
        "v1": [{"t_ms": 0, "at": "lot", "do": "spawn"}]}
    roles = {"p1": ["participant"], "p2": ["participant"], "p3": ["distractor"]} if typed else {}
    return {"event_id": "synth-rdv", "version": 1, "event_type": "rendezvous_handoff", "tick_hz": 10, "duration_ms": 60_000,
            "actors": [{"id": i, "class": CLS[i], "route": routes[i], **({"roles": roles[i]} if i in roles else {})} for i in IDS],
            "annotations": list(annotations), "relations": list(relations)}


def _frames(*, pickup_b: float = 20.0, p2_far: bool = False, short_meet: bool = False) -> np.ndarray:
    """60 s、10 Hz（与 `_plan` 对应）。p1：kiosk 等待 0–6 s（2 s 拾取 o1）→ 东行 → 广场 (20, 0) 等待 12–25 s（20 s 放下）→ 南行离开；
    p2：广场 (21, 1) 等待至 30 s（`pickup_b` 拾取）→ 走到 (27, 4) 上 v1（40–50 s 在车内）→ 下车后 (47, 4) 等待；p3：kiosk 旁 (0, 1.5)
    停留 6 s 后离场；v1：lot 内等待 → 驶向 (47, 5) → 驶出。`short_meet`：p1 只在广场停留 18–21 s（交汇不足 5 s）。"""
    n_t = 60 * TICK + 1
    t = np.arange(n_t) / TICK
    f = np.zeros((n_t, len(IDS)), RECORD)
    f["container"] = NO_CONTAINER
    pos = np.zeros((n_t, len(IDS), 2))
    st = np.zeros((n_t, len(IDS)), np.uint8)

    def seg(i: int, t0: float, t1: float, a, b, s: int) -> None:
        m = (t >= t0 - 1e-9) & (t < t1 - 1e-9)
        u = ((t[m] - t0) / (t1 - t0))[:, None]
        pos[m, i] = np.asarray(a) + u * (np.asarray(b) - np.asarray(a))
        st[m, i] = s

    seg(P1, 0, 6, (0, 0), (0, 0), 2)
    if short_meet:
        seg(P1, 6, 18, (0, 0), (20, 0), 1)
        seg(P1, 18, 21, (20, 0), (20, 0), 2)
        seg(P1, 21, 31, (20, 0), (20, -20), 1)
    else:
        seg(P1, 6, 12, (0, 0), (20, 0), 1)
        seg(P1, 12, 25, (20, 0), (20, 0), 2)
        seg(P1, 25, 35, (20, 0), (20, -20), 1)
    seg(P1, 35 if not short_meet else 31, 61, (20, -20), (20, -20), 2)
    p2xy = (23.5, 1) if p2_far else (21, 1)
    seg(P2, 0, 30, p2xy, p2xy, 2)
    seg(P2, 30, 40, p2xy, (27, 4), 1)
    seg(P2, 50, 61, (47, 4), (47, 4), 2)
    seg(P3, 0, 6, (0, 1.5), (0, 1.5), 2)
    seg(P3, 6, 61, (0, 1.5), (0, 1.5), 5)
    seg(V, 0, 42, (27, 5), (27, 5), 2)
    seg(V, 42, 48, (27, 5), (47, 5), 1)
    seg(V, 48, 52, (47, 5), (47, 5), 2)
    seg(V, 52, 61, (47, 5), (80, 5), 1)
    m = (t >= 40 - 1e-9) & (t < 50 - 1e-9)                       # p2 在车内
    pos[m, P2], st[m, P2] = pos[m, V], 3
    f["container"][m, P2] = V
    # o1：kiosk 地面 → p1 携带 [2, 20) → p2 携带（经地面时先在 p1 放下点静止）；p2 在车内时随车
    seg(OBJ, 0, 2, (0, 0), (0, 0), 2)
    for k in range(n_t):
        tk = t[k]
        if tk < 2 - 1e-9:
            continue
        if tk < 20 - 1e-9:
            c = P1
        elif tk >= pickup_b - 1e-9:
            c = P2
        else:
            pos[k, OBJ], st[k, OBJ] = pos[int(20 * TICK), P1], 2
            continue
        pos[k, OBJ] = pos[k, c]
        st[k, OBJ] = 3
        f["container"][k, OBJ] = c if st[k, c] in (1, 2) else f["container"][k, c]
    f["x_mm"] = np.floor(pos[..., 0] * 1000 + 0.5).astype(np.int32)
    f["y_mm"] = np.floor(pos[..., 1] * 1000 + 0.5).astype(np.int32)
    f["state"] = st
    return f


def _verify(plan=None, frames=None, **kw):
    return verify_event(plan or _plan(), _frames() if frames is None else frames, TABLE, anchors=ANCHORS, **kw)


def _keys(g, typ: str) -> list[dict]:
    return [k for k in g.key_events if k["type"] == typ]


ANN = [{"id": "hand", "type": "object_handover", "participants": ["p1", "p2", "o1"], "window": [15, 25]},
       {"id": "enc", "type": "proximity_encounter", "participants": ["p1", "p2"], "window": [10, 30]}]


def test_registry_and_detector() -> None:
    registry.collect()
    assert "rendezvous_handoff" in registry.VALIDATORS and "rendezvous_handoff" in registry.REQUIREMENTS
    det = registry.DETECTORS["object_handover"]
    assert det.instant and det.defaults == {"dist_m": 2.0, "max_gap_s": 10.0}
    g = _verify(_plan(annotations=ANN))
    (h,) = [e for e in g.events if e["type"] == "object_handover"]
    assert h["id"] == "hand" and h["declared"] and h["participants"] == ["p1", "p2", "o1"] and h["interval_ms"] == [20_000, 20_000]
    assert h["evidence"] == {"object": "o1", "dist_m": 1.414, "gap_ms": 0} and h["salience"] == 1.0
    assert [c["verified"] for c in g.annotation_checks] == [True, True]


def test_key_events_relations_and_carry_consistent_with_tape() -> None:
    g = _verify(_plan(annotations=ANN, relations=[{"src": "enc", "rel": "before", "dst": "hand"}]), strict=True)
    # 关系 enc before hand 不成立（交接发生在相遇区间内）：只验证门 3 的关系谓词，换成 during 后通过
    assert [i.code for i in g.errors] == ["S-RELATION"]
    g = _verify(_plan(annotations=ANN, relations=[{"src": "hand", "rel": "during", "dst": "enc"}]), strict=True)
    assert g.errors == [] and g.stats["type_validator"] == "rendezvous_handoff"
    (meet,) = _keys(g, "meet")
    # p1 于 11.8 s 进入 p2 的 2 m 范围（x = 19.33 m），25.3 s 离开（y = −0.6 m 时 1.887 m，−0.8 m 时 2.06 m）
    assert meet["participants"] == ["p1", "p2"] and meet["interval_ms"] == [11_800, 25_300] and meet["declared"] is True
    assert meet["params"]["min_dist_m"] == 1.414 and meet["params"]["pairs"] == [["p1", "p2", 11_800, 25_300]]
    (hand,) = _keys(g, "handover")
    assert hand["interval_ms"] == [20_000, 20_000] and hand["ref"] == "hand" and hand["declared"] is True
    assert hand["params"] == {"giver": "p1", "receiver": "p2", "object": "o1", "dist_m": 1.414, "gap_ms": 0, "meet": meet["id"]}
    (disp,) = _keys(g, "disperse")
    # 分散：p1 自 25 s 以 2 m/s 南行，与 p2（21, 1）的距离首次 ≥ 10 m 于 29.5 s（√(1² + (1 + 2·4.5)²) = 10.05 m）
    assert disp["interval_ms"] == [29_500, 29_500] and disp["params"] == {"sep_m": 10.0, "after_s": 4.2, "meet": meet["id"]}
    (board,) = _keys(g, "board")
    (alight,) = _keys(g, "alight")
    assert board["participants"] == ["p2", "v1"] and board["interval_ms"] == [40_000, 40_000] and board["declared"] is False
    assert alight["interval_ms"] == [50_000, 50_000] and alight["params"] == {"vehicle": "v1"}
    assert [k["id"] for k in g.key_events] == ["k01-meet", "k02-handover", "k03-disperse", "k04-board", "k05-alight"]
    rel = {(r["src"], r["rel"], r["dst"]) for r in g.key_relations}
    assert rel == {("k02-handover", "during", "k01-meet"), ("k01-meet", "before", "k03-disperse"), ("k01-meet", "before", "k04-board"),
                   ("k01-meet", "before", "k05-alight"), ("k02-handover", "causes", "k04-board")}
    assert g.validator_params == {"meet": {"dist_m": 2.0, "min_dur_s": 5.0}, "disperse": {"sep_m": 10.0, "max_s": 120.0},
                                  "handover": {"dist_m": 2.0, "max_gap_s": 10.0}, "participants": "roles"}
    assert g.report_map["handover"] == ["handover"] and g.report_map["relation"] == ["key_relations"]
    # 物品容器转移与磁带一致：携带段 p1 [2, 19.9] s → p2 [20, 60] s（车内帧不中断 p2 的携带）
    rv = g.extra["rendezvous"]
    assert rv["carry"] == [{"object": "o1", "holder": "p1", "interval_ms": [2000, 19_900]}, {"object": "o1", "holder": "p2", "interval_ms": [20_000, 60_000]}]
    assert rv["carry_check"] == {"n_objects": 1, "n_steps": 3, "n_fail": 0}
    assert rv["planned_handovers"] == [{"giver": "p1", "receiver": "p2", "object": "o1", "interval_ms": [20_000, 20_000]}]
    assert g.stats["consistency_status"]["S-CONTAINER"] == "pass" and g.stats["consistency_status"]["S-OVERLAP"] == "pass"
    lab = carry_labels(_ctx_like(), OBJ)
    assert holds(lab) == [[P1, 20, 199], [P2, 200, 600]] and (lab[400:500] == -2).all()


def _ctx_like():
    from awr.sim.events.verify.base import Context

    return Context(_plan(), _frames(), TABLE, len(IDS), ANCHORS)


def test_via_ground_handover_and_rest_position() -> None:
    g = _verify(_plan(pickup_b=23.0), _frames(pickup_b=23.0))
    assert g.errors == []
    (hand,) = _keys(g, "handover")
    assert hand["interval_ms"] == [20_000, 23_000] and hand["params"]["gap_ms"] == 3000 and hand["ref"].startswith("e")
    assert planned_handovers(_plan(pickup_b=23.0), 10_000) == [{"giver": "p1", "receiver": "p2", "object": "o1", "interval_ms": [20_000, 23_000]}]
    f = _frames(pickup_b=23.0)
    f["x_mm"][210:230, OBJ] += 300                                   # 放在地面期间被移动
    g = _verify(_plan(pickup_b=23.0), f)
    (e,) = [i for i in g.errors if i.code == "S-CARRY"]
    assert e.reason == 613 and {r["why"] for r in e.detail["worst"]} == {"rest_position"}
    # 交接间隔超过 max_gap_s：不是交接（两次独立携带），plan 也不期望交接
    g = _verify(_plan(pickup_b=23.0), _frames(pickup_b=23.0), params={"object_handover": {"max_gap_s": 2.0}})
    assert _keys(g, "handover") == [] and g.errors == [] and g.validator_params["handover"]["max_gap_s"] == 2.0


def test_tampered_container_returns_613_and_lenient_warning() -> None:
    f = _frames()
    f["container"][200:211, OBJ] = P1                               # 磁带中交接晚 1.1 s（位置随 p1，S-CONTAINER 仍成立）
    for fld in ("x_mm", "y_mm"):
        f[fld][200:211, OBJ] = f[fld][200:211, P1]
    g = _verify(frames=f)
    codes = sorted(i.code for i in g.errors)
    assert codes == ["S-CARRY", "S-HANDOVER"] and {i.reason for i in g.errors} == {613}
    carry = next(i for i in g.errors if i.code == "S-CARRY")
    assert carry.detail["worst"][0] == {"object": "o1", "t_ms": 20_000, "why": "holder_mismatch", "expected": "p2", "tape": "p1"}
    hand = next(i for i in g.errors if i.code == "S-HANDOVER")
    assert hand.detail["why"] == "not_detected" and hand.detail["interval_ms"] == [20_000, 20_000]
    assert [k["interval_ms"] for k in _keys(g, "handover")] == [[21_100, 21_100]]       # 磁带上的交接仍按磁带产出
    g = _verify(frames=f, strict=False)
    assert g.errors == [] and {"S-CARRY", "S-HANDOVER"} <= {w.code for w in g.warnings}
    f = _frames()
    f["state"][300:310, OBJ], f["container"][300:310, OBJ] = 2, NO_CONTAINER              # 应由 p2 携带却在地面
    g = _verify(frames=f)
    assert {r["why"] for i in g.errors if i.code == "S-CARRY" for r in i.detail["worst"]} == {"not_carried"}


def test_handover_outside_meet_and_missing_meet() -> None:
    g = _verify(frames=_frames(short_meet=True))                 # p1 在广场只停留 3 s：交接成立但不在交汇内
    assert _keys(g, "meet") == [] and _keys(g, "handover") == []
    assert {"W-HANDOVER-OUTSIDE"} <= {w.code for w in g.warnings}
    assert sorted(i.code for i in g.errors) == ["S-HANDOVER", "S-KEY-MEET"]
    assert next(i for i in g.errors if i.code == "S-HANDOVER").detail["why"] == "outside_meet"
    g = _verify(_plan(typed=False), _frames(short_meet=True))     # P0a 形式：无交汇只告警；p3 与 p1 在 kiosk 旁 1.5 m 停留 6 s 计为交汇
    assert [k["participants"] for k in _keys(g, "meet")] == [["p1", "p3"]] and g.validator_params["participants"] == "persons"
    assert "S-KEY-MEET" not in {i.code for i in g.errors}
    g = _verify(_plan(typed=False), _frames())
    assert [k["participants"] for k in _keys(g, "meet")] == [["p1", "p3"], ["p1", "p2"]]
    g = _verify(frames=_frames(p2_far=True))                      # 两人相距 3.6 m：既无交汇也无交接
    assert _keys(g, "meet") == [] and not [e for e in g.events if e["type"] == "object_handover"]
    assert next(i for i in g.errors if i.code == "S-HANDOVER").detail["why"] == "not_detected"


def _truth_graph(g, plan):
    from awr.sim.events import gen

    plan = {**plan, "seed": 1, "world": {"id": "w"}}
    truth = gen.build_truth(plan, "0" * 64, **g.truth_kwargs(), perf=g.perf)
    return truth, gen.build_graph(truth, TABLE)


def test_truth_graph_and_openlabel_export() -> None:
    from awr.bench.export import openlabel
    from awr.sim.events import gen

    plan = _plan(annotations=ANN, relations=[{"src": "hand", "rel": "during", "dst": "enc"}])
    g = _verify(plan)
    truth, graph = _truth_graph(g, plan)
    _schema("truth.v1.schema.json").validate(truth)
    _schema("graph.v1.schema.json").validate(graph)
    assert truth["key_events"] == g.key_events and truth["validator_params"] == g.validator_params and truth["report_map"] == g.report_map
    t2, g2 = _truth_graph(_verify(plan), plan)
    assert gen.truth_sha256(t2) == gen.truth_sha256(truth) and json.dumps(g2) == json.dumps(graph)
    no_key = {k: v for k, v in truth.items() if k not in ("key_events", "key_relations", "validator_params", "report_map")}
    assert gen.truth_sha256(no_key) != gen.truth_sha256(truth)                       # 验证器输出进入 truth_sha256
    ent = [n["id"] for n in graph["nodes"] if n["kind"] == "entity"]
    ev = {n["id"]: n for n in graph["nodes"] if n["kind"] == "event"}
    assert ent == sorted(IDS) and {"k01-meet", "k02-handover", "hand", "enc"} <= set(ev)
    edges = {(e["src"], e["rel"], e["dst"]) for e in graph["edges"]}
    assert {("o1", "participates_in", "k02-handover"), ("hand", "during", "enc"), ("k02-handover", "during", "k01-meet")} <= edges
    header = {"n_ticks": 601, "actors": TABLE}
    doc = openlabel.to_openlabel(graph, truth, header, frames=_frames(), poses=True, stride=50, truth_sha256=gen.truth_sha256(truth))
    assert openlabel.check(doc, graph) == []
    ol = doc["openlabel"]
    assert len(ol["actions"]) + len(ol["events"]) == len(ev) and len(ol["relations"]) == len(graph["edges"]) and len(ol["objects"]) == 5
    by_name = {**{v["name"]: ("action", k, v) for k, v in ol["actions"].items()}, **{v["name"]: ("event", k, v) for k, v in ol["events"].items()}}
    kind, _uid, hv = by_name["k02-handover"]
    assert kind == "event" and hv["type"] == "handover" and hv["frame_intervals"] == [{"frame_start": 200, "frame_end": 200}]
    txt = {x["name"]: x["val"] for x in hv["event_data"]["text"]}
    assert txt["giver"] == "p1" and txt["receiver"] == "p2" and txt["object"] == "o1" and txt["layer"] == "key"
    kind, muid, mv = by_name["k01-meet"]
    assert kind == "action" and mv["frame_intervals"] == [{"frame_start": 118, "frame_end": 253}]
    dur = [r for r in ol["relations"].values() if r["name"] == "k02-handover during k01-meet"]
    assert dur and dur[0]["rdf_subjects"] == [{"type": "event", "uid": by_name["k02-handover"][1]}] and dur[0]["rdf_objects"] == [{"type": "action", "uid": muid}]
    assert ol["objects"]["1"]["name"] == "o1" and ol["objects"]["1"]["type"] == "object" and ol["metadata"]["schema_version"] == "1.0.0"
    assert set(ol["frames"]) == {str(k) for k in range(0, 601, 50)} and "1" not in ol["frames"]["100"]["objects"]   # 被携带时不可见
    cub = ol["frames"]["0"]["objects"]["2"]["object_data"]["cuboid"][0]["val"]
    assert cub[:3] == [0.0, 0.0, 0.875] and cub[6:] == [0.5, 0.3, 1.75]
    bad = copy.deepcopy(doc)
    bad["openlabel"]["relations"]["0"]["rdf_objects"] = [{"type": "event", "uid": "999"}]
    bad["openlabel"]["events"]["0"]["frame_intervals"] = [{"frame_start": 3, "frame_end": 9}]
    assert len(openlabel.check(bad, graph)) == 2
    _official_schema(doc)


def _official_schema(doc: dict) -> None:
    """官方 OpenLABEL 1.0 JSON Schema（draft-07，https://openlabel.asam.net/V1-0-0/schema/openlabel_json_schema.json）；仓库不收录
    （许可待 ADR-205 复核），设 `AWR_OPENLABEL_SCHEMA` 指向本地副本时校验。"""
    p = os.environ.get("AWR_OPENLABEL_SCHEMA")
    if not p:
        return
    jsonschema = pytest.importorskip("jsonschema")
    errs = list(jsonschema.Draft7Validator(json.loads(Path(p).read_text())).iter_errors(doc))
    assert errs == [], [e.message for e in errs[:3]]


# ---------------------------------------------------------------------- 3. 合成世界的编译
SYNTH_EVENT = {
    "schema": "drone4d.event.v1", "event_id": "synth-rdv", "version": 1, "event_type": "rendezvous_handoff", "world": {"id": "synth-glb"},
    "anchors_ref": "event:anchors.json", "seed": 7, "duration_s": 150,
    "actors": [
        {"id": "person_a", "class": "person", "roles": ["participant"],
         "route": [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": 50, "at": "ev_kiosk", "do": "pickup", "target": "pkg"},
                   {"t": 80, "at": "ev_plaza", "do": "drop", "target": "pkg", "dwell_s": 10}, {"t": 140, "at": "ev_kiosk", "do": "despawn"}]},
        {"id": "person_b", "class": "person", "roles": ["participant"],
         "route": [{"t": 0, "at": "ev_north", "do": "spawn"}, {"t": 80, "at": "ev_plaza", "do": "pickup", "target": "pkg", "dwell_s": 10},
                   {"t": 130, "at": "ev_curb", "do": "board", "target": "vehicle_c"}]},
        {"id": "pkg", "class": "object", "route": [{"t": 0, "at": "ev_kiosk", "do": "spawn"}]},
        {"id": "vehicle_c", "class": "vehicle", "route": [{"t": 0, "at": "ev_road_w", "do": "spawn"},
                                                          {"t": 100, "at": "ev_road", "do": "wait", "dwell_s": 35},
                                                          {"t": 150, "at": "ev_road_e", "do": "despawn"}]},
    ],
    "annotations": [{"id": "hand_ab", "type": "object_handover", "participants": ["person_a", "person_b", "pkg"], "window": [75, 90]},
                    {"id": "meet_ab", "type": "proximity_encounter", "participants": ["person_a", "person_b"], "window": [70, 95]},
                    {"id": "board_b", "type": "board_vehicle", "participants": ["person_b", "vehicle_c"], "window": [120, 140]}],
    "relations": [{"src": "hand_ab", "rel": "before", "dst": "board_b"}, {"src": "hand_ab", "rel": "during", "dst": "meet_ab"}],
}


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    pytest.importorskip("jsonschema")
    for d in (Path(__file__).resolve().parents[1] / "world", Path(__file__).resolve().parent):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("rdv")
    mp = pytest.MonkeyPatch()
    yield build_synth_world(tmp, mp), tmp
    mp.undo()


def _compile(synth, ev: dict, name: str, **kw):
    from test_compile import OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    w, tmp = synth
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", **kw))


def test_compile_handover_event_strict_and_openlabel(synth) -> None:
    from awr.bench.export import openlabel
    from awr.sim.events import gen
    from awr.sim.events import tape as tp

    r = _compile(synth, SYNTH_EVENT, "rdv")
    assert r.status == "compiled" and r.reason is None, json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    assert r.gates[2]["errors"] == [] and r.strict is True
    tdir = r.out / "truth" / "r1"
    truth = json.loads((tdir / "event_truth.json").read_text())
    graph = json.loads((tdir / "event_graph.json").read_text())
    _schema("truth.v1.schema.json").validate(truth)
    _schema("graph.v1.schema.json").validate(graph)
    assert all(c["verified"] for c in truth["annotation_checks"]) and all(c["verified"] for c in truth["relation_checks"])
    types = [k["type"] for k in truth["key_events"]]
    assert types.count("meet") == 1 and types.count("handover") == 1 and types.count("disperse") == 1 and types.count("board") == 1
    (h,) = [k for k in truth["key_events"] if k["type"] == "handover"]
    assert h["interval_ms"] == [80_000, 80_000] and h["params"]["giver"] == "person_a" and h["params"]["receiver"] == "person_b"
    assert h["declared"] and h["ref"] == "hand_ab" and h["params"]["dist_m"] < 2.0
    rel = {(r_["src"], r_["rel"], r_["dst"]) for r_ in truth["key_relations"]}
    meet_id = next(k["id"] for k in truth["key_events"] if k["type"] == "meet")
    board_id = next(k["id"] for k in truth["key_events"] if k["type"] == "board")
    assert {(h["id"], "during", meet_id), (h["id"], "causes", board_id), (meet_id, "before", board_id)} <= rel
    rep = json.loads((r.out / "compile_report.json").read_text())["gate3"]
    assert rep["stats"]["consistency_status"]["S-CONTAINER"] == "pass" and rep["extra"]["rendezvous"]["carry_check"]["n_fail"] == 0
    assert [c["holder"] for c in rep["extra"]["rendezvous"]["carry"]] == ["person_a", "person_b"]
    # 磁带中物体容器序列与 plan 一致：拾取 50 s、交接 80 s（同帧由 a 变为 b）、b 上车后随车（容器为车辆）
    t = tp.open_tape(r.out)
    f = np.concatenate([t.block_frames(i) for i in range(len(t.blocks))])
    idx = {a["id"]: a["idx"] for a in t.header["actors"]}
    o, a, b, v = idx["pkg"], idx["person_a"], idx["person_b"], idx["vehicle_c"]
    c = f["container"][:, o]
    assert (c[:500] == NO_CONTAINER).all() and (c[500:800] == a).all() and c[800] == b and f["state"][499, o] == 2
    kb = int(np.flatnonzero(f["state"][:, b] == 3)[0])
    assert kb == 1300 and (c[800:kb] == b).all() and (c[kb:1400] == v).all() and (f["state"][800:1400, o] == 3).all()
    for k in (600, 900, 1350):
        assert all(int(f[fld][k, o]) == int(f[fld][k, int(c[k])]) for fld in ("x_mm", "y_mm", "z_mm"))
    # 确定性
    r2 = _compile(synth, SYNTH_EVENT, "rdv-again")
    assert r2.tape["content_sha256"] == r.tape["content_sha256"] and r2.truth["truth_sha256"] == r.truth["truth_sha256"]
    assert r.truth["truth_sha256"] == gen.truth_sha256(truth)
    # OpenLABEL 导出
    p = openlabel.export_event(r.out, poses=True)
    assert p == tdir / "synth-rdv.openlabel.json"
    doc = json.loads(p.read_text())
    assert openlabel.check(doc, graph) == []
    assert openlabel.main([str(r.out), "--out", str(synth[1] / "rdv.openlabel.json")]) == 0
    assert json.loads((synth[1] / "rdv.openlabel.json").read_text())["openlabel"]["relations"] == doc["openlabel"]["relations"]
    _official_schema(doc)


def test_gate2_carry_distance_612(synth) -> None:
    ev = copy.deepcopy(SYNTH_EVENT)
    ev["actors"][1]["route"][1].update({"at": "ev_curb", "t": 110})   # b 在路缘拾取：距 a 的放下点约 60 m
    r = _compile(synth, ev, "rdv-far", preflight_only=True)
    codes = [e["code"] for g in r.gates for e in g["errors"]]
    assert r.status == "infeasible" and r.reason == 612 and "I-CARRY" in codes
    e = next(e for g in r.gates for e in g["errors"] if e["code"] == "I-CARRY")
    assert e["detail"]["object"] == "pkg" and e["detail"]["dist_m"] > 2.0


# ---------------------------------------------------------------------- 4. 示例事件 crop-handoff-001
@pytest.fixture(scope="module")
def crop(tmp_path_factory: pytest.TempPathFactory):
    if not (CROP / "world.json").exists():
        pytest.skip("needs worlds/oryahovo-crop1")
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    from awr.sim.events.compile import CompileOptions, compile_event

    tmp = tmp_path_factory.mktemp("crop-rdv")
    return compile_event(CROP_EVENT / "event.yaml", CROP, opts=CompileOptions(out=tmp / "v1", cache_dir=tmp / "cache"))


def test_crop_handoff_key_events_and_openlabel(crop) -> None:
    from awr.bench.export import openlabel

    r = crop
    assert r.status == "compiled" and r.gates[2]["errors"] == []
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text())
    _schema("truth.v1.schema.json").validate(truth)
    ks = {k["type"]: k for k in truth["key_events"]}
    assert set(ks) == {"meet", "board", "disperse", "enter_building"} and truth["validator_params"]["participants"] == "persons"
    assert ks["board"]["ref"] == "board_BC" and ks["enter_building"]["ref"] == "enter_AZ" and ks["meet"]["participants"] == ["person_A", "person_B"]
    assert all(k["interval_ms"][1] <= ks["disperse"]["interval_ms"][0] for k in truth["key_events"] if k["type"] == "meet")
    rel = {(x["rel"], x["dst"]) for x in truth["key_relations"]}
    assert ("before", ks["board"]["id"]) in rel and ("before", ks["enter_building"]["id"]) in rel and ("before", ks["disperse"]["id"]) in rel
    graph = json.loads((r.out / "truth" / "r1" / "event_graph.json").read_text())
    doc = json.loads(openlabel.export_event(r.out).read_text())
    assert openlabel.check(doc, graph) == [] and "frames" not in doc["openlabel"]
    _official_schema(doc)
