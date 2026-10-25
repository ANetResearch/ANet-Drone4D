"""Re-ID 遮挡事件类型 `reid_occlusion`（D3-AC-62 Re-ID 条款；M23-FR-071、073、077，M23-AC-037（Re-ID 部分）、039（Re-ID 部分）；
M23 §15.3 第 1 行）。

1. 门 1 必备配置：合格配置通过；缺少目标、干扰者不足、类别不符、遮挡设施不足一次报出全部（`E-TYPE` 611，`type_requirement`），
   与其他门 1 错误同批报告；遮挡设施的静态计数（建筑出入口对、上下车、树冠或廊下）；属性距离 1 的干扰者被门 1 拒绝
   （`E-APPEARANCE` 611，`appearance_distance`）。
2. 门 3 验证器（构造帧数据）：建筑、车内与廊下（视线 oracle）3 处 ≥ 5 s 遮挡各产出 1 个 `target_reacquire`，时刻与磁带一致；
   3 s 遮挡只计数；构造 2 处 6 s 遮挡产出 2 个 `target_reacquire`（M23-AC-039）；遮挡少于 2 处或均短于 5 s 时 strict 613、
   lenient 告警；可见间隙合并；与缺席、离场相邻的不可见段不计；关键事件清单、验证器参数与 report 映射。
3. 参考视点与视线：视点网格、身体点、车辆包围盒遮挡、分环求交与一次查全部视点结果相同。
4. 合成世界（open3d）：Re-ID 事件 strict 编译通过，编译报告中的 `target_reacquire` 与磁带状态一致；两处 3 s 遮挡 strict 613。
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("jsonschema")
from awr.sim.events import appearance as al
from awr.sim.events.dsl import gate1
from awr.sim.events.tape import NO_CONTAINER, RECORD
from awr.sim.events.verify import DETECTORS, VALIDATORS, registry, verify_event
from awr.sim.events.verify import reid as rv
from awr.sim.events.verify.base import Context

ROOT = Path(__file__).resolve().parents[3]
TICK = 10


@pytest.fixture(scope="module")
def lib() -> al.Library:
    return al.load_library(al.LIB_PATH)


def _partners(lib: al.Library, t: str, n: int = 3) -> list[str]:
    a = lib.by_id[t]
    rule = lib.rules["reid_occlusion"]
    return [i for i in lib.ids("person") if i != t and not lib.pair_whys(rule, a, lib.by_id[i])][:n]


# ---------------------------------------------------------------------------------------------------- 1. 门 1
ANCHORS = {a["id"]: a for a in [
    {"id": "a0", "kind": "point", "profile": "person", "xy": [0.0, 0.0]},
    {"id": "a1", "kind": "point", "profile": "person", "xy": [80.0, 0.0]},
    {"id": "door_a", "kind": "door", "profile": "person", "xy": [10.0, 4.0], "building": "b_a"},
    {"id": "door_b", "kind": "door", "profile": "person", "xy": [40.0, 4.0], "building": "b_b"},
    {"id": "curb", "kind": "curb", "profile": "person", "xy": [60.0, 3.0], "pair": "road"},
    {"id": "road", "kind": "point", "profile": "vehicle", "xy": [60.0, 6.0]},
    {"id": "arcade", "kind": "point", "category": "poi:arcade", "profile": "person", "xy": [25.0, 0.0]},
    {"id": "canopy", "kind": "zone", "category": "poi:tree_canopy", "profile": "person", "polygon": [[50, -5], [56, -5], [56, 5], [50, 5]]},
]}
TWO_DOORS = [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 20, "at": "door_a", "do": "enter_building"},
             {"t": 30, "at": "door_a", "do": "exit_building"}, {"t": 60, "at": "door_b", "do": "enter_building"},
             {"t": 70, "at": "door_b", "do": "exit_building"}, {"t": 110, "at": "a1"}]
CAR = {"id": "car", "class": "vehicle", "route": [{"t": 0, "at": "road", "do": "spawn"}, {"t": 1, "at": "road", "do": "wait"}]}


def _doc(lib: al.Library, route=TWO_DOORS, *, n_dis: int = 3, target: str = "person-01", extra=()) -> dict:
    acts = [{"id": "tgt", "class": "person", "roles": ["target"], "appearance_ref": target, "route": copy.deepcopy(list(route))}]
    acts += [{"id": f"d{k}", "class": "person", "roles": ["distractor"], "appearance_ref": r,
              "route": [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 110, "at": "a1"}]} for k, r in enumerate(_partners(lib, target, n_dis))]
    return {"schema": "drone4d.event.v1", "event_id": "reid-type-001", "event_type": "reid_occlusion", "world": {"id": "oryahovo-crop1"},
            "seed": 3, "duration_s": 120, "actors": acts + [copy.deepcopy(a) for a in extra]}


def _rules(errs) -> list[tuple[str, str]]:
    return sorted((e.path, e.detail["rule"]) for e in errs if e.code == "E-TYPE")


def test_registry_has_reid_requirement_and_validator() -> None:
    registry.collect()
    assert VALIDATORS["reid_occlusion"] is rv.reid_occlusion and registry.REQUIREMENTS["reid_occlusion"] is rv.requirements
    assert "target_reacquire" not in DETECTORS                    # 关键事件由类型验证器产出，不进入全量检测
    with pytest.raises(RuntimeError, match="duplicate"):
        registry.requirement("reid_occlusion")(lambda doc, anchors: [])
    assert registry.type_issues({"event_type": "rendezvous_handoff", "actors": []}, {}) == []


def test_compliant_configuration_passes(lib: al.Library) -> None:
    plan, errs, _w = gate1(_doc(lib), ANCHORS)
    assert errs == [] and plan is not None and plan["event_type"] == "reid_occlusion"
    assert [a["roles"] for a in plan["actors"]] == [["target"], ["distractor"], ["distractor"], ["distractor"]]


def test_missing_requirements_reported_together(lib: al.Library) -> None:
    """M23-AC-037（Re-ID）：缺少目标、干扰者不足与其他门 1 错误一次报出，全部 611。"""
    doc = _doc(lib, n_dis=2)
    doc["actors"][0]["roles"] = ["participant"]
    doc["actors"][1]["route"][1]["at"] = "nowhere"
    _plan, errs, _w = gate1(doc, ANCHORS)
    assert _rules(errs) == [("/actors", "distractor_count"), ("/actors", "target_count")]
    assert {e.code for e in errs} == {"E-TYPE", "E-ANCHOR-UNKNOWN"} and {e.reason for e in errs} == {611}
    t = next(e for e in errs if e.detail.get("rule") == "target_count")
    assert t.detail["why"] == "type_requirement" and t.detail["event_type"] == "reid_occlusion" and t.detail["found"] == 0
    d = next(e for e in errs if e.detail.get("rule") == "distractor_count")
    assert d.detail["found"] == 2 and d.detail["required"] == 3 and d.message_en.startswith("A Re-ID event requires at least 3")
    # 两名目标、车辆干扰者、遮挡设施不足（只有 1 处上下车）
    route = [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 50, "at": "curb", "do": "board", "target": "car"},
             {"t": 60, "at": "curb", "do": "alight", "target": "car"}, {"t": 110, "at": "a1"}]
    doc = _doc(lib, route, extra=[CAR])
    doc["actors"][-1]["roles"] = ["distractor"]
    _plan, errs, _w = gate1(doc, ANCHORS)
    assert ("/actors/0/route", "occlusion_facilities") in _rules(errs) and ("/actors/4/class", "distractor_class") in _rules(errs)
    fac = next(e for e in errs if e.detail.get("rule") == "occlusion_facilities").detail
    assert fac["found"] == 1 and fac["facilities"] == [{"kind": "vehicle", "waypoint": 1, "vehicle": "car"}]
    doc = _doc(lib)
    doc["actors"][1]["roles"] = ["target"]
    _plan, errs, _w = gate1(doc, ANCHORS)
    assert ("/actors", "target_count") in _rules(errs)
    assert next(e for e in errs if e.detail.get("rule") == "target_count").detail["actors"] == ["tgt", "d0"]


def test_target_must_be_person(lib: al.Library) -> None:
    doc = _doc(lib, extra=[CAR])
    doc["actors"][0]["roles"] = ["distractor"]
    doc["actors"][-1]["roles"] = ["target"]
    _plan, errs, _w = gate1(doc, ANCHORS)
    assert ("/actors/4/class", "target_class") in _rules(errs)
    assert ("/actors/4/route", "occlusion_facilities") in _rules(errs)    # 车辆路线没有遮挡设施


def test_facility_counting() -> None:
    f = rv.facilities
    assert [x["kind"] for x in f(TWO_DOORS, ANCHORS)] == ["building", "building"]
    r = [{"t": 0, "at": "arcade", "do": "spawn"}, {"t": 10, "at": "a1", "via": ["arcade"]},             # 首路点在廊下不计
         {"t": 20, "at": "canopy"}, {"t": 30, "at": "canopy", "do": "wait"},                           # 连续引用同一树冠计 1 处
         {"t": 40, "at": "curb", "do": "board", "target": "car"}]                                       # 上车后未下车不计
    assert f(r, ANCHORS) == [{"kind": "cover", "waypoint": 1, "anchor": "arcade"}, {"kind": "cover", "waypoint": 2, "anchor": "canopy"}]
    r = [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 10, "at": "door_a", "do": "enter_building"}]           # 进入后不出来不计
    assert f(r, ANCHORS) == []
    r = [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 10, "at": "a1", "via": ["arcade", "a0", "arcade"]},
         {"t": 50, "at": "curb", "do": "board", "target": "car"}, {"t": 60, "at": "curb", "do": "alight", "target": "car"}]
    assert [x["kind"] for x in f(r, ANCHORS)] == ["cover", "cover", "vehicle"]


def test_distance_one_distractor_rejected_at_gate1(lib: al.Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D3-AC-62：目标与干扰者属性距离 ≥ 2，门 1 拒绝不合格配置（类型必备配置满足时只报外观错误）。"""
    doc = json.loads(al.LIB_PATH.read_text(encoding="utf-8"))
    names = [a["name"] for a in doc["classes"]["person"]["attributes"]]
    j = names.index("accessory")
    vals = doc["classes"]["person"]["attributes"][j]["values"]
    e = copy.deepcopy(lib.by_id["person-01"].entry)
    e["id"] = "person-90"
    e["vector"][j] = (e["vector"][j] + 1) % len(vals)
    e["attrs"]["accessory"] = vals[e["vector"][j]]
    doc["entries"].append(e)
    p = tmp_path / "appearance_lib.v1.json"
    p.write_bytes(al.lib_bytes(doc))
    monkeypatch.setenv("AWR_APPEARANCE_LIB", str(p))
    fx = al.load_library()
    assert fx.distance(fx.by_id["person-01"], fx.by_id["person-90"]) == 1
    ev = _doc(lib)
    ev["actors"][3]["appearance_ref"] = "person-90"
    plan, errs, _w = gate1(ev, ANCHORS)
    assert plan is None and [(x.code, x.reason, x.path, x.detail["why"]) for x in errs] == [
        ("E-APPEARANCE", 611, "/actors/3/appearance_ref", "appearance_distance")]


# ---------------------------------------------------------------------------------------------------- 2. 门 3（构造帧数据）
IDS = ["car", "tgt", "d0", "d1", "d2"]                     # 车辆在前（gen.actor_order）
TABLE = [{"idx": k, "id": i, "class": "vehicle" if i == "car" else "person", "scripted": True,
          "profile": "vehicle" if i == "car" else "person", "size_mm": [4500, 1800, 1500] if i == "car" else [500, 300, 1750]}
         for k, i in enumerate(IDS)]
DOORS = {"door1": {"id": "door1", "kind": "door", "profile": "person", "xy": [8.0, 0.0], "building": "b1"},
         "door2": {"id": "door2", "kind": "door", "profile": "person", "xy": [50.0, 0.0], "building": "b2"}}
CAR_XY = (14.0, 1.5)
ARCADE_X = (30.0, 36.95)                                   # 廊下：起点 x ∈ [30, 36.95]、|y| < 1 的射线全部被挡


def _plan() -> dict:
    roles = {"tgt": ["target"], "d0": ["distractor"], "d1": ["distractor"], "d2": ["distractor"]}
    return {"event_id": "synth-reid", "version": 1, "event_type": "reid_occlusion", "tick_hz": 10, "duration_ms": 60_000,
            "actors": [{"id": i, "class": "vehicle" if i == "car" else "person", "route": [{"at": "a0", "do": "spawn"}],
                        **({"roles": roles[i]} if i in roles else {})} for i in IDS], "annotations": [], "relations": []}


def _frames(tl=None) -> np.ndarray:
    """60 s、10 Hz。目标缺省时间线：0–8 s 东行 → 8–14 s 在 door1 内（6 s）→ 14–20 s 东行 → 20–26 s 在车内（6 s）→ 26–28 s 走到
    (28, 0) → 28–40 s 以 1 m/s 东行穿过廊下（30.0–36.9 s 被挡，7 s）→ 40–50 s 东行 → 50–53 s 在 door2 内（3 s）→ 东行。
    `tl` 为 [(t0, t1, a, b, state)]，替换目标时间线；state 3 时位置与车辆相同、容器为车辆。"""
    n_t = 60 * TICK + 1
    t = np.arange(n_t) / TICK
    f = np.zeros((n_t, len(IDS)), RECORD)
    f["container"] = NO_CONTAINER
    pos = np.zeros((n_t, len(IDS), 2))
    st = np.zeros((n_t, len(IDS)), np.uint8)
    car, tgt = 0, 1
    tl = tl if tl is not None else [(0, 8, (0, 0), (8, 0), 1), (8, 14, (8, 0), (8, 0), 4), (14, 20, (8, 0), (14, 0), 1),
                                    (20, 26, CAR_XY, CAR_XY, 3), (26, 28, (14, 0), (28, 0), 1), (28, 40, (28, 0), (40, 0), 1),
                                    (40, 50, (40, 0), (50, 0), 1), (50, 53, (50, 0), (50, 0), 4), (53, 61, (50, 0), (57, 0), 1)]
    for t0, t1, a, b, s in tl:
        m = (t >= t0 - 1e-9) & (t < t1 - 1e-9)
        u = ((t[m] - t0) / (t1 - t0))[:, None]
        pos[m, tgt] = np.asarray(a, float) + u * (np.asarray(b, float) - np.asarray(a, float))
        st[m, tgt] = s
        if s == 3:
            f["container"][m, tgt] = car
    pos[:, car], st[:, car] = CAR_XY, 2
    for k, x in enumerate((0.0, 5.0, 10.0)):
        pos[:, 2 + k], st[:, 2 + k] = (x, 10.0), 2
    f["x_mm"] = np.floor(pos[..., 0] * 1000 + 0.5).astype(np.int32)
    f["y_mm"] = np.floor(pos[..., 1] * 1000 + 0.5).astype(np.int32)
    f["state"] = st
    return f


class _Arcade:
    """起点 x ∈ ARCADE_X、|y| < 1 的射线在 1 m 处命中，其余不命中（视线与天空可见度共用）。"""

    def __init__(self) -> None:
        self.n = 0

    def raycast_batch(self, o, d, tmax):
        o = np.asarray(o)
        self.n += len(o)
        hit = (o[:, 0] >= ARCADE_X[0]) & (o[:, 0] <= ARCADE_X[1]) & (np.abs(o[:, 1]) < 1.0)
        return np.where(hit, 1.0, np.inf).astype(np.float32), np.zeros(len(o), np.uint32), np.zeros(len(o), np.uint32)


class _Open:
    def raycast_batch(self, o, d, tmax):
        n = len(o)
        return np.full(n, np.inf, np.float32), np.zeros(n, np.uint32), np.zeros(n, np.uint32)


def _verify(frames=None, mesh=None, strict=True):
    return verify_event(_plan(), _frames() if frames is None else frames, TABLE, anchors=DOORS, mesh=mesh, strict=strict)


def _reacq(g) -> list[list[int]]:
    return [k["interval_ms"] for k in g.extra["key_events"] if k["type"] == "target_reacquire"]


def _tape_reacquire(frames: np.ndarray, i: int = 1, min_ms: int = 5000) -> list[int]:
    """只按磁带状态独立复算：在车内或建筑内（3、4）的连续段，前后均为移动或等待，持续 ≥ min_ms 时重新可见首帧的时刻。"""
    s = frames["state"][:, i]
    out, k = [], 1
    while k < len(s):
        if s[k] in (3, 4) and s[k - 1] in (1, 2):
            j = k
            while j < len(s) and s[j] in (3, 4):
                j += 1
            if j < len(s) and s[j] in (1, 2) and (j - k) * 100 >= min_ms:
                out.append(j * 100)
            k = j
        k += 1
    return out


def test_three_occlusions_produce_target_reacquire_consistent_with_tape() -> None:
    fr = _frames()
    mesh = _Arcade()
    g = _verify(fr, mesh)
    assert g.errors == [] and not [w for w in g.warnings if w.code.startswith("W-TYPE")]
    assert _reacq(g) == [[14_000, 14_000], [26_000, 26_000], [37_000, 37_000]]
    rep = g.extra["reid_occlusion"]
    assert rep["target"] == "tgt" and rep["n_valid"] == 3 and rep["n_short"] == 1
    occ = rep["occlusions"]
    assert [(o["occluded_ms"], o["causes"], o["valid"]) for o in occ] == [
        ([8_000, 14_000], ["building"], True), ([20_000, 26_000], ["vehicle"], True), ([30_000, 37_000], ["los"], True),
        ([50_000, 53_000], ["building"], False)]
    assert occ[1]["containers"] == ["car"] and occ[2]["ticks"] == {"inside": 0, "los": 70}
    assert occ[2]["last_seen_xy"] == [29.9, 0.0] and occ[2]["reacquire_xy"] == [37.0, 0.0]
    # 与磁带一致：状态类遮挡与独立复算相同；视线类遮挡的首末帧与廊下几何相同；重新可见首帧可见、前一帧不可见
    tape = _tape_reacquire(fr)
    assert tape == [14_000, 26_000] and [r[0] for r in _reacq(g) if r[0] in tape] == tape
    x = fr["x_mm"][:, 1] / 1000.0
    blocked = np.flatnonzero((x >= ARCADE_X[0]) & (x <= ARCADE_X[1]) & np.isin(fr["state"][:, 1], (1, 2)))
    assert occ[2]["occluded_ms"] == [int(blocked[0]) * 100, (int(blocked[-1]) + 1) * 100]
    for t_ms, _ in _reacq(g):
        k = t_ms // 100
        assert fr["state"][k, 1] in (1, 2) and (fr["state"][k - 1, 1] in (3, 4) or ARCADE_X[0] <= x[k - 1] <= ARCADE_X[1])
    # 关键事件清单：目标的进出建筑与上下车（引用真值事件 id），按时刻排序
    ke = g.extra["key_events"]
    assert [(k["id"], k["type"], k["interval_ms"][0]) for k in ke] == [
        ("enter_building-1", "enter_building", 8_000), ("exit_building-1", "exit_building", 14_000),
        ("target_reacquire-1", "target_reacquire", 14_000), ("board-1", "board", 20_000), ("alight-1", "alight", 26_000),
        ("target_reacquire-2", "target_reacquire", 26_000), ("target_reacquire-3", "target_reacquire", 37_000),
        ("enter_building-2", "enter_building", 50_000), ("exit_building-2", "exit_building", 53_000)]
    ev = {e["id"]: e for e in g.events}
    for k in ke:
        if k["type"] != "target_reacquire":
            src = ev[k["ref"]]
            assert src["interval_ms"] == k["interval_ms"] and src["participants"] == k["participants"] and k["declared"] is False
    assert ke[3]["participants"] == ["tgt", "car"] and ke[0]["params"]["door"] == "door1"
    r1 = ke[2]["params"]
    assert r1["occluded_ms"] == [8_000, 14_000] and r1["duration_ms"] == 6_000 and [ev[e]["type"] for e in r1["events"]] == [
        "enter_building", "exit_building"]
    assert all(k["participants"] == ["tgt"] for k in ke if k["type"] == "target_reacquire")
    # 验证器参数、关键事件与 report 映射经 truth_kwargs 进入真值（truth_sha256）
    assert g.extra["validator_params"] == rv.DEFAULTS and "reid_occlusion" not in g.detector_params
    tk = g.truth_kwargs()
    assert tk["key_events"] == ke and tk["validator_params"] == rv.DEFAULTS and tk["report_map"] == rv.REPORT_MAP
    assert g.extra["report_map"]["reacquire"] == ["target_reacquire"] and g.extra["report_map"]["board"] == ["board"]
    assert g.stats["type_validator"] == "reid_occlusion" and g.perf["reid_los_rays"] > 0 and rep["oracle"]["ticks_blocked"] == 70
    assert rep["oracle"]["rings"][0]["r_m"] == [0.0, 20.0] and rep["oracle"]["rings"][0]["seen"] == rep["oracle"]["ticks_present"] - 70
    g2 = _verify(fr, _Arcade())
    assert g2.extra == g.extra and g2.events == g.events                  # 确定性


def test_two_six_second_occlusions_produce_two_reacquire_without_mesh() -> None:
    """M23-AC-039（Re-ID）：2 处 6 s 遮挡（建筑、车内）产出 2 个 target_reacquire；无网格时视线部分不可用并告警。"""
    g = _verify(mesh=None)
    assert g.errors == [] and _reacq(g) == [[14_000, 14_000], [26_000, 26_000]]
    assert [w.code for w in g.warnings if w.code.startswith("W-TYPE")] == ["W-TYPE-ORACLE"]
    assert g.extra["reid_occlusion"]["oracle"]["los_available"] is False and g.extra["reid_occlusion"]["n_valid"] == 2


def test_too_few_or_too_short_occlusions_fail_strict_613() -> None:
    walk = [(0, 8, (0, 0), (8, 0), 1)]
    short2 = [*walk, (8, 12.9, (8, 0), (8, 0), 4), (12.9, 20, (8, 0), (14, 0), 1), (20, 24.9, CAR_XY, CAR_XY, 3),
              (24.9, 61, (14, 0), (40, 0), 1)]
    one = [*walk, (8, 14, (8, 0), (8, 0), 4), (14, 61, (8, 0), (40, 0), 1)]
    for tl, n_valid in ((short2, 0), (one, 1)):
        fr = _frames(tl)
        g = _verify(fr, _Open())
        assert [e.code for e in g.errors] == ["S-TYPE"] and g.errors[0].reason == 613, tl
        d = g.errors[0].detail
        assert d["why"] == "occlusion_count" and d["n_valid"] == n_valid and d["min_occlusions"] == 2 and d["min_dur_s"] == 5.0
        assert g.errors[0].path == "/actors/1/route"
        gl = _verify(fr, _Open(), strict=False)
        assert gl.errors == [] and [w.reason for w in gl.warnings if w.code == "S-TYPE"] == [None]
    assert [o["duration_ms"] for o in _verify(_frames(short2), _Open()).extra["reid_occlusion"]["occlusions"]] == [4_900, 4_900]
    # 参数覆盖（verify_event 的 params，键为事件类型）：min_dur_s = 8 s 时缺省时间线的 6、6、7 s 遮挡均不足
    g = verify_event(_plan(), _frames(), TABLE, anchors=DOORS, mesh=_Arcade(), params={"reid_occlusion": {"min_dur_s": 8.0}})
    assert [e.code for e in g.errors] == ["S-TYPE"] and g.errors[0].detail["n_valid"] == 0 and g.extra["validator_params"]["min_dur_s"] == 8.0


def test_merge_gap_and_boundaries() -> None:
    # 出建筑 0.5 s 后上车：可见间隙 < 1 s，合并为 1 处 12.5 s 遮挡（建筑与车内）；间隙 1 s 时为 2 处
    for gap, want in ((0.5, [[8_000, 20_500]]), (1.0, [[8_000, 14_000], [15_000, 21_000]])):
        tl = [(0, 8, (0, 0), (8, 0), 1), (8, 14, (8, 0), (8, 0), 4), (14, 14 + gap, (8, 0), (8, 0), 1),
              (14 + gap, 20 + gap, CAR_XY, CAR_XY, 3), (20 + gap, 61, (14, 0), (40, 0), 1)]
        occ = _verify(_frames(tl), _Open(), strict=False).extra["reid_occlusion"]["occlusions"]
        assert [o["occluded_ms"] for o in occ] == want, gap
    # 出生前缺席（0）后直接可见、事件末尾仍在建筑内：均不计为遮挡
    tl = [(2, 10, (0, 0), (8, 0), 1), (10, 16, (8, 0), (8, 0), 4), (16, 30, (8, 0), (20, 0), 1), (30, 61, (20, 0), (20, 0), 4)]
    rep = _verify(_frames(tl), _Open(), strict=False).extra["reid_occlusion"]
    assert [o["occluded_ms"] for o in rep["occlusions"]] == [[10_000, 16_000]] and rep["n_valid"] == 1


def test_missing_target_fails_strict() -> None:
    plan = _plan()
    plan["actors"][1].pop("roles")
    g = verify_event(plan, _frames(), TABLE, anchors=DOORS)
    assert [(e.code, e.reason, e.detail["why"]) for e in g.errors] == [("S-TYPE", 613, "target_missing")]
    assert g.extra["key_events"] == []


# ---------------------------------------------------------------------------------------------------- 3. 参考视点与视线
def test_viewpoints_and_body_points() -> None:
    V = rv.viewpoints(3.0, 4.0, 1.0, -1.0, 20.0, 10.0, [40.0, 70.0, 100.0])
    xy = {(float(x), float(y)) for x, y in V[:, :2]}
    assert all(math.hypot(x - 3, y - 4) <= 20 for x, y in xy) and len(V) == 3 * len(xy) and (0.0, 0.0) in xy
    assert sorted(set(V[:, 2])) == [41.0, 71.0, 101.0] and (V[:, 0] % 10 == 0).all()
    ring = rv.viewpoints(3.0, 4.0, 1.0, 20.0, 60.0, 10.0, [40.0])
    assert not {(float(x), float(y)) for x, y in ring[:, :2]} & xy and np.array_equal(V, rv.viewpoints(3.0, 4.0, 1.0, -1.0, 20.0, 10.0,
                                                                                                         [40.0, 70.0, 100.0]))
    B = rv.body_points(1.0, 2.0, 0.5, math.pi / 2, rv.DEFAULTS["body_m"])                # 航向 +y：横向沿 x
    assert np.allclose(B, [[1, 2, 0.8], [1, 2, 1.4], [1, 2, 2.1], [1.2, 2, 1.5], [0.8, 2, 1.5]])


def test_vehicle_box_blocks_line_of_sight() -> None:
    box = np.array([[1.5, 0.0, 0.75, 2.25, 0.9, 0.75, math.pi / 2]])        # 长轴沿 y：x ∈ [0.6, 2.4]，z ∈ [0, 1.5]
    o = np.array([[3.0, 0.0, 1.0], [3.0, 0.0, 1.0], [3.0, 0.0, 1.0], [1.5, 0.0, 1.0]])
    d = np.array([[-3.0, 0.0, 0.0], [-3.0, 0.0, 3.0], [0.0, 3.0, 0.0], [0.0, 0.0, 5.0]])
    assert rv._box_hits(o, d, box).tolist() == [True, False, False, True]   # 穿过、越过上沿、侧向离开、起点在盒内
    assert rv._box_hits(o[:1], np.array([[-0.5, 0.0, 0.0]]), box).tolist() == [False]         # 线段未到达盒体
    # 只有一个低视点 (0, 0, 1) 时，目标与视点之间的车辆挡住全部身体点
    plan = _plan()
    fr = _frames([(0, 61, (3, 0), (3, 0), 2)])
    fr["x_mm"][:, 0], fr["y_mm"][:, 0], fr["yaw_cdeg"][:, 0] = 1500, 0, 9000
    ctx = Context(plan, fr, TABLE, 5, DOORS, mesh=_Open())
    p = {**rv.DEFAULTS, "stage_m": 6.0, "rings_m": [], "alts_m": [1.0]}
    los, info = rv.los_visible(ctx, 1, p)
    assert not los.any() and info["viewpoints"] == 601 and info["ticks_blocked"] == 601
    fr["x_mm"][:, 0] = 20_000                                                 # 车辆移开（> vehicle_m 之外不参与求交）
    los, _ = rv.los_visible(Context(plan, fr, TABLE, 5, DOORS, mesh=_Open()), 1, p)
    assert los.all()


def test_rings_equal_single_pass() -> None:
    class _Canopy:
        """z = 4 m、x ≥ 20 的大面积水平板（解析求交）。"""

        def raycast_batch(self, o, d, tmax):
            o, d = np.asarray(o, float), np.asarray(d, float)
            u = d / np.linalg.norm(d, axis=1, keepdims=True)
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (4.0 - o[:, 2]) / u[:, 2]
            x, y = o[:, 0] + t * u[:, 0], o[:, 1] + t * u[:, 1]
            hit = (t > 0) & (x >= 20) & (x <= 2000) & (np.abs(y) <= 2000)
            return np.where(hit, t, np.inf).astype(np.float32), np.zeros(len(o), np.uint32), np.zeros(len(o), np.uint32)

    fr = _frames([(0, 61, (0, 0), (60, 0), 1)])
    plan = _plan()
    p = {**rv.DEFAULTS, "stage_m": 60.0, "rings_m": [20.0, 40.0]}                    # 缩小舞台以控制纯 numpy 求交耗时
    a, ia = rv.los_visible(Context(plan, fr, TABLE, 5, DOORS, mesh=_Canopy()), 1, p)
    b, ib = rv.los_visible(Context(plan, fr, TABLE, 5, DOORS, mesh=_Canopy()), 1, {**p, "rings_m": []})
    assert np.array_equal(a, b) and ia["rays"] < ib["rays"]
    x = fr["x_mm"][:, 1] / 1000.0
    assert a[x < 15].all() and not a[x > 30].any() and a[(x >= 15) & (x <= 30)].sum() > 0   # 板外可见；板下 > 5.6 m 全部视点被挡


# ---------------------------------------------------------------------------------------------------- 4. 合成世界编译
@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    for d in (Path(__file__).resolve().parents[1] / "world", Path(__file__).resolve().parent):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("reid")
    mp = pytest.MonkeyPatch()
    try:
        yield build_synth_world(tmp, mp), tmp
    finally:
        mp.undo()


def _synth_event(lib: al.Library, inside_s: float) -> dict:
    """合成世界（盒子建筑 x ∈ [30, 50]、y ∈ [−50, −30]，门 ev_door）：目标两次进出建筑，各停留 inside_s。"""
    t = "person-04"                                                          # 步态 0.9–1.8 m/s
    ds = _partners(lib, t)
    a, b = 60.0, 60.0 + inside_s
    route = [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": a, "at": "ev_door", "do": "enter_building"},
             {"t": b, "at": "ev_door", "do": "exit_building"}, {"t": b + 30, "at": "ev_south"},
             {"t": b + 60, "at": "ev_door", "do": "enter_building"}, {"t": b + 60 + inside_s, "at": "ev_door", "do": "exit_building"},
             {"t": b + 110 + inside_s, "at": "ev_north"}]
    acts = [{"id": "tgt", "class": "person", "roles": ["target"], "appearance_ref": t, "route": route}]
    acts += [{"id": f"d{k}", "class": "person", "roles": ["distractor"], "appearance_ref": r,
              "route": [{"t": 0, "at": at, "do": "spawn"}, {"t": 10, "at": at, "do": "wait"}]}
             for k, (r, at) in enumerate(zip(ds, ("ev_plaza", "ev_kiosk", "ev_east"), strict=True))]
    return {"schema": "drone4d.event.v1", "event_id": "synth-reid", "version": 1, "event_type": "reid_occlusion", "world": {"id": "synth-glb"},
            "anchors_ref": "event:anchors.json", "seed": 5, "duration_s": 240, "actors": acts}


def _compile(synth, ev: dict, name: str):
    from test_compile import OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    w, tmp = synth
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache"))


def test_compile_reid_event_reacquire_matches_tape(synth, lib: al.Library) -> None:
    import jsonschema

    from awr.sim.events import tape as tp

    r = _compile(synth, _synth_event(lib, 10.0), "ok")
    assert r.status == "compiled", json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    assert [e["code"] for e in r.gates[2]["errors"]] == []
    rep = json.loads((r.out / "compile_report.json").read_text(encoding="utf-8"))
    ex = rep["gate3"]["extra"]
    assert rep["gate3"]["stats"]["type_validator"] == "reid_occlusion" and ex["reid_occlusion"]["oracle"]["los_available"] is True
    rd = tp.open_tape(r.out)
    fr = np.concatenate([rd.block_frames(b, background=False) for b in range(len(rd.blocks))])
    i = next(a["idx"] for a in rd.actors if a["id"] == "tgt")
    want = _tape_reacquire(fr, i)
    assert len(want) == 2 and [k["interval_ms"][0] for k in ex["key_events"] if k["type"] == "target_reacquire"] == want
    occ = ex["reid_occlusion"]["occlusions"]
    assert [o["causes"] for o in occ if o["valid"]] == [["building"], ["building"]] and all(o["duration_ms"] >= 9_900 for o in occ)
    truth = json.loads((r.out / "truth" / "r1" / "event_truth.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/event/truth.v1.schema.json").read_text())).validate(truth)
    assert truth["validator_params"] == rv.DEFAULTS and truth["key_events"] == ex["key_events"] and truth["report_map"] == rv.REPORT_MAP
    exits = sorted(e["interval_ms"][0] for e in truth["events"] if e["type"] == "exit_building" and e["participants"] == ["tgt"])
    assert exits == want


def test_compile_short_occlusions_fail_613(synth, lib: al.Library) -> None:
    r = _compile(synth, _synth_event(lib, 3.0), "short")
    assert r.status == "failed" and r.reason == 613 and len(r.gates) == 3 and r.gates[0]["errors"] == []
    (e,) = [x for x in r.gates[2]["errors"] if x["code"] == "S-TYPE"]
    assert e["detail"]["why"] == "occlusion_count" and e["detail"]["n_valid"] == 0 and len(e["detail"]["occlusions_ms"]) == 2
