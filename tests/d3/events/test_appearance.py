"""外观库 `appearance_lib@1` 与门 1 可区分性（M23-AC-038、M23-FR-072；D3-AC-62 "目标与干扰者属性距离 ≥ 2（门 1 拒绝不合格配置）"
与"外观库许可登记齐全"；AWR-05 §5.2.8）。

- 外观库：同一规格两次生成逐字节一致且与入库文件一致；≥ 24 个人物外观与 ≥ 8 个车辆外观；库内两两属性距离 ≥ 2；同一颜色属性的
  调色板取值 CIEDE2000 ≥ 15；每个外观在其类别规则下有 ≥ 3 个合格干扰外观；自制资产，许可为项目许可且无第三方来源。
- 门 1：构造属性距离 1 的干扰者被拒绝（611，`appearance_distance`）；无法解析的外观引用返回 611（`appearance_unresolved`）；
  类别不符、与外观段互斥、步态档、难负样本（共享属性 ≥ 1）与车辆追踪同车型或同色规则；一次报出全部问题；经 `compile_event`
  端到端拒绝且只有门 1。
- 规范化与编译报告：引用外观的演员在 plan 与磁带规范头中的外观、尺寸与步态取自外观库；24 个人物外观两两属性距离统计
  （276 对）写入编译报告。
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")
from awr.sim.events import appearance as al
from awr.sim.events.compile import CompileOptions, compile_event
from awr.sim.events.dsl import gate1, normalize
from awr.sim.events.errors import EventError

ROOT = Path(__file__).resolve().parents[3]
CROP = ROOT / "worlds" / "oryahovo-crop1"


@pytest.fixture(scope="module")
def lib() -> al.Library:
    return al.load_library(al.LIB_PATH)


def _whys(issues) -> list[tuple[str, str]]:
    return sorted((i.path, i.detail.get("why")) for i in issues)


def _rule_partners(lib: al.Library, rule: dict, t: al.Appearance) -> list[al.Appearance]:
    return [lib.by_id[i] for i in lib.ids(t.cls) if i != t.id and not lib.pair_whys(rule, t, lib.by_id[i])]


# ------------------------------------------------------------------------------------------------- 外观库
def test_library_is_deterministic_and_up_to_date() -> None:
    spec = al.load_spec()
    a, b = al.lib_bytes(al.build_library(spec)), al.lib_bytes(al.build_library(copy.deepcopy(spec)))
    assert a == b == al.LIB_PATH.read_bytes()
    assert al.main(["build", "--check"]) == 0


def test_library_self_check_counts_and_license(lib: al.Library) -> None:
    doc = json.loads(al.LIB_PATH.read_text(encoding="utf-8"))
    assert al.library_issues(doc, al.load_spec()) == []
    assert len(lib.ids("person")) >= 24 and len(lib.ids("vehicle")) >= 8
    assert doc["license"]["kind"] == "original" and doc["license"]["third_party"] == []
    for e in doc["entries"]:
        assert e["license"] == {"kind": "original", "spdx": "LicenseRef-ANet-OSL", "holder": "Agent Network Research"}
        assert e["asset"]["kind"] == "procedural" and e["asset"]["generator"] in ("person-proxy@1", "vehicle-proxy@1")
        assert e["anim_clips"] and e["thermal_zones"] and e["description"]["zh"] and e["description"]["en"]
        assert abs(sum(x * x for x in e["embedding"]["values"]) - 1.0) < 1e-4
    # 人物七个属性：上下装颜色、图案、配饰、身高档、体型、步态；车辆四个属性：车型、颜色、车顶、标识
    assert lib.attr_names["person"] == ["top_color", "bottom_color", "top_pattern", "accessory", "height_class", "build", "gait"]
    assert lib.attr_names["vehicle"] == ["body_type", "body_color", "roof", "marking"]


def test_pairwise_distance_statistics(lib: al.Library) -> None:
    st = lib.stats("person")
    assert st["n"] == 24 and st["n_pairs"] == 276 and sum(st["distance_hist"].values()) == 276
    assert st["distance_min"] >= 2 and st["rule_partners_min"] >= 3
    vs = lib.stats("vehicle")
    assert vs["distance_min"] >= 2 and vs["rule_partners_min"] >= 3
    # 每个人物外观都能组成 1 名目标 + 3 名难负样本干扰者的 Re-ID 配置
    rule = lib.rules["reid_occlusion"]
    assert all(len(_rule_partners(lib, rule, lib.by_id[i])) >= 3 for i in lib.ids("person"))


def test_ciede2000_reference_pairs_and_palette() -> None:
    # Sharma, Wu, Dalal (2005) 测试数据第 1、7、17、25 对
    for lab1, lab2, de in [((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
                           ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
                           ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
                           ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644)]:
        assert al.ciede2000(lab1, lab2) == pytest.approx(de, abs=1e-4)
    lum, a, b = al.srgb_to_lab((255, 255, 255))
    assert abs(lum - 100.0) < 1e-3 and abs(a) < 1e-3 and abs(b) < 1e-3


def test_palette_rejects_close_colours() -> None:
    spec = al.load_spec()
    spec["palette"]["navy"]["rgb"] = [34, 34, 40]                              # 与黑色几乎相同
    doc = al.build_library(spec)
    assert any("CIEDE2000(black, navy)" in m for m in al.library_issues(doc))


# ------------------------------------------------------------------------------------------------- 门 1
def _reid(lib: al.Library, target: str, distractors: list[str]) -> dict:
    acts = [{"id": "tgt", "class": "person", "roles": ["target"], "appearance_ref": target,
             "route": [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 60, "at": "a1"}]}]
    acts += [{"id": f"d{k}", "class": "person", "roles": ["distractor"], "appearance_ref": r,
              "route": [{"t": 0, "at": "a0", "do": "spawn"}, {"t": 60, "at": "a1"}]} for k, r in enumerate(distractors)]
    return {"schema": "drone4d.event.v1", "event_id": "reid-ap-001", "event_type": "reid_occlusion", "world": {"id": "oryahovo-crop1"},
            "seed": 1, "duration_s": 60, "actors": acts}


ANCHORS = {"a0": {"id": "a0", "kind": "point", "profile": "person", "xy": [0.0, 0.0]},
           "a1": {"id": "a1", "kind": "point", "profile": "person", "xy": [50.0, 0.0]}}


def _good(lib: al.Library) -> tuple[str, list[str]]:
    t = lib.by_id["person-01"]
    return t.id, [p.id for p in _rule_partners(lib, lib.rules["reid_occlusion"], t)[:3]]


def test_compliant_reid_configuration_passes(lib: al.Library) -> None:
    t, ds = _good(lib)
    doc = _reid(lib, t, ds)
    assert al.gate1_issues(doc, lib) == []
    plan, errs, _w = gate1(doc, ANCHORS)
    assert errs == [] and plan is not None
    rep = al.appearance_report(plan, lib)
    assert len(rep["pairs"]) == 3 and all(p["ok"] and p["distance"] >= 2 and p["shared"] for p in rep["pairs"])
    assert rep["library"]["person"]["n_pairs"] == 276 and rep["actors"]["tgt"] == t


def _with_variant(tmp_path: Path, base: al.Appearance, attr: str) -> tuple[Path, str]:
    """外观库副本：加入与 `base` 只差一个属性的外观 `person-90`（属性距离 1）。"""
    doc = json.loads(al.LIB_PATH.read_text(encoding="utf-8"))
    names = [a["name"] for a in doc["classes"]["person"]["attributes"]]
    j = names.index(attr)
    vals = doc["classes"]["person"]["attributes"][j]["values"]
    e = copy.deepcopy(base.entry)
    e["id"] = "person-90"
    e["vector"][j] = (e["vector"][j] + 1) % len(vals)
    e["attrs"][attr] = vals[e["vector"][j]]
    doc["entries"].append(e)
    p = tmp_path / "appearance_lib.v1.json"
    p.write_bytes(al.lib_bytes(doc))
    return p, e["id"]


def test_distance_one_distractor_is_rejected(lib: al.Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t, ds = _good(lib)
    p, near = _with_variant(tmp_path, lib.by_id[t], "accessory")
    monkeypatch.setenv("AWR_APPEARANCE_LIB", str(p))
    fx = al.load_library()
    assert fx.distance(fx.by_id[t], fx.by_id[near]) == 1
    doc = _reid(fx, t, [*ds[:2], near])
    plan, errs, _w = gate1(doc, ANCHORS)
    assert plan is None
    bad = [e for e in errs if e.code == "E-APPEARANCE"]
    assert [(e.reason, e.path, e.detail["why"], e.detail["distance"]) for e in bad] == [(611, "/actors/3/appearance_ref", "appearance_distance", 1)]
    assert bad[0].detail["shared"] == [n for n in fx.attr_names["person"] if n != "accessory"]


def test_unresolved_and_misused_references_report_all_at_once(lib: al.Library) -> None:
    t, ds = _good(lib)
    doc = _reid(lib, t, ds)
    doc["actors"][1]["appearance_ref"] = "person-99"                           # 不在库中
    doc["actors"][2]["appearance_ref"] = "appearance_lib@2/" + ds[1]           # 其他库版本
    doc["actors"][3]["appearance"] = {"top_rgb": [1, 2, 3]}                    # 与外观引用互斥
    doc["actors"][3]["gait"] = {"speed_mps": [0.5, 2.5]}                       # 超出步态档
    doc["actors"].append({"id": "car", "class": "vehicle", "appearance_ref": "person-02",
                          "route": [{"t": 0, "at": "a0", "do": "spawn"}]})      # 类别不符
    doc["actors"].append({"id": "d9", "class": "person", "roles": ["distractor"], "route": [{"t": 0, "at": "a0", "do": "spawn"}]})
    got = _whys(al.gate1_issues(doc, lib))
    assert got == sorted([("/actors/1/appearance_ref", "appearance_unresolved"), ("/actors/2/appearance_ref", "appearance_unresolved"),
                          ("/actors/3/appearance", "appearance_override"), ("/actors/3/gait/speed_mps", "appearance_gait"),
                          ("/actors/4/appearance_ref", "appearance_class"), ("/actors/5/appearance_ref", "appearance_required")])
    assert all(i.reason == 611 and i.code == "E-APPEARANCE" for i in al.gate1_issues(doc, lib))
    with pytest.raises(EventError) as ei:
        al.resolve("person-99", lib=lib)
    assert ei.value.code == 611 and ei.value.detail["why"] == "appearance_unresolved"
    with pytest.raises(EventError) as ei:
        al.resolve("vehicle-01", "person", lib=lib)
    assert ei.value.detail["why"] == "appearance_class"


def test_hard_negative_rule_requires_a_shared_attribute(lib: al.Library) -> None:
    t = lib.by_id["person-01"]
    far = next(lib.by_id[i] for i in lib.ids("person") if i != t.id and not lib.shared(t, lib.by_id[i]))
    ok = [p.id for p in _rule_partners(lib, lib.rules["reid_occlusion"], t)[:2]]
    doc = _reid(lib, t.id, [*ok, far.id])
    assert _whys(al.gate1_issues(doc, lib)) == [("/actors/3/appearance_ref", "appearance_hard_negative")]


def test_vehicle_track_requires_same_type_or_colour(lib: al.Library) -> None:
    t = lib.by_id["vehicle-01"]
    rule = lib.rules["vehicle_track"]
    ok = _rule_partners(lib, rule, t)[:2]
    other = next(lib.by_id[i] for i in lib.ids("vehicle")
                 if i != t.id and not {"body_type", "body_color"} & set(lib.shared(t, lib.by_id[i])))
    acts = [{"id": "car_t", "class": "vehicle", "roles": ["target"], "appearance_ref": t.id, "route": [{"t": 0, "at": "a0", "do": "spawn"}]}]
    acts += [{"id": f"car_{k}", "class": "vehicle", "roles": ["distractor"], "appearance_ref": a.id, "route": [{"t": 0, "at": "a0", "do": "spawn"}]}
             for k, a in enumerate([*ok, other])]
    doc = {"event_type": "vehicle_track", "actors": acts}
    assert ("/actors/3/appearance_ref", "appearance_similarity") in _whys(al.gate1_issues(doc, lib))
    assert all(p.path == "/actors/3/appearance_ref" for p in al.gate1_issues(doc, lib))


def test_events_without_library_use_are_untouched(lib: al.Library) -> None:
    doc = {"event_type": "rendezvous_handoff", "actors": [{"id": "p", "class": "person", "route": []}]}
    assert al.gate1_issues(doc, lib) == []
    assert al.report_for({"event_type": "rendezvous_handoff", "actors": [{"id": "p", "appearance": {"asset": "person-proxy@1"}}]}) is None


def test_compile_rejects_at_gate1(lib: al.Library, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：`compile_event` 对属性距离 1 的干扰者与无法解析的引用返回 611，只执行门 1（不装载舞台）。"""
    if not (CROP / "world.json").exists():
        pytest.skip("[data] worlds/oryahovo-crop1 is not built")
    w = tmp_path / CROP.name
    (w / "qa").mkdir(parents=True)
    for rel in ("world.json", "coordinate.json", "qa/report.json"):
        shutil.copyfile(CROP / rel, w / rel)
    t, ds = _good(lib)
    p, near = _with_variant(tmp_path, lib.by_id[t], "gait")
    monkeypatch.setenv("AWR_APPEARANCE_LIB", str(p))
    doc = _reid(lib, t, [ds[0], near, "person-77"])
    doc["anchors_ref"] = "event:anchors.json"
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "anchors.json").write_text(json.dumps({"schema": "awr.anchors.v1", "extends": "world", "world_id": "oryahovo-crop1",
                                                 "anchors": list(ANCHORS.values())}), encoding="utf-8")
    (ev / "event.json").write_text(json.dumps(doc), encoding="utf-8")
    r = compile_event(ev / "event.json", w, opts=CompileOptions(out=tmp_path / "out", cache_dir=tmp_path / "cache"))
    assert r.status == "failed" and r.reason == 611 and len(r.gates) == 1
    ap = sorted((e["path"], e["detail"]["why"]) for e in r.gates[0]["errors"] if e["code"] == "E-APPEARANCE")
    assert ap == [("/actors/2/appearance_ref", "appearance_distance"), ("/actors/3/appearance_ref", "appearance_unresolved")]
    assert not (tmp_path / "out").exists()


# ------------------------------------------------------------------------------------------------- 规范化与编译报告
def test_normalize_takes_appearance_size_and_gait_from_library(lib: al.Library) -> None:
    t, ds = _good(lib)
    doc = _reid(lib, t, ds)
    doc["actors"][1]["gait"] = {"accel_mps2": 0.8}
    doc["actors"].append({"id": "car", "class": "vehicle", "appearance_ref": "appearance_lib@1/vehicle-04",
                          "route": [{"t": 0, "at": "a0", "do": "spawn"}]})
    plan = normalize(doc)
    a, d1, car = plan["actors"][0], plan["actors"][1], plan["actors"][-1]
    e, v = lib.by_id[t].entry, lib.by_id["vehicle-04"].entry
    assert a["appearance"]["asset"] == f"appearance_lib@1/{t}" and a["roles"] == ["target"]
    assert a["appearance"]["top_rgb"] == e["asset"]["render"]["top_rgb"] and a["appearance"]["bottom_rgb"] == e["asset"]["render"]["bottom_rgb"]
    assert a["size_mm"] == [round(x * 1000) for x in e["size_m"]] and a["appearance"]["height_mm"] == a["size_mm"][2]
    assert a["gait"]["speed_mps"] == e["gait"]["speed_mps"] and a["gait"]["median_mps"] == e["gait"]["median_mps"]
    assert d1["gait"]["accel_mps2"] == 0.8 and d1["gait"]["speed_mps"] == lib.by_id[ds[0]].entry["gait"]["speed_mps"]
    assert car["appearance"] == {"asset": "appearance_lib@1/vehicle-04", "body_rgb": v["asset"]["render"]["body_rgb"]}
    assert car["size_mm"] == [round(x * 1000) for x in v["size_m"]] and "roles" not in car


def test_compile_report_carries_library_statistics(tmp_path_factory: pytest.TempPathFactory) -> None:
    """完整编译（合成世界）：引用外观的演员进入磁带规范头，编译报告含 24 个人物外观两两属性距离统计。"""
    pytest.importorskip("open3d")
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    import sys

    sys.path.insert(0, str(ROOT / "tests" / "d3" / "events"))
    sys.path.insert(0, str(ROOT / "tests" / "d3" / "world"))
    import test_compile as tc
    from synthworld import build_synth_world

    from awr.sim.events import tape as tp

    tmp = tmp_path_factory.mktemp("apw")
    mp = pytest.MonkeyPatch()
    try:
        w = build_synth_world(tmp, mp)
        ev = copy.deepcopy(tc.EVENT)
        ev["event_id"] = "synth-appearance"
        ev["actors"][0].pop("appearance")
        ev["actors"][0]["appearance_ref"] = "person-02"
        ev["actors"][3]["appearance_ref"] = "vehicle-01"
        r = tc._compile((w, tmp), tmp, ev, name="ap")
    finally:
        mp.undo()
    assert r.status == "compiled", json.dumps(r.as_dict(), ensure_ascii=False)[:2000]
    rep = json.loads((r.out / "compile_report.json").read_text(encoding="utf-8"))["appearance"]
    assert rep["lib_id"] == "appearance_lib@1" and rep["library"]["person"]["n_pairs"] == 276
    assert rep["actors"] == {"person_a": "person-02", "vehicle_c": "vehicle-01"}
    hd = tp.open_tape(r.out).header
    row = next(a for a in hd["actors"] if a["id"] == "person_a")
    lib = al.load_library(al.LIB_PATH)
    assert row["appearance"]["asset"] == "appearance_lib@1/person-02"
    assert row["appearance"]["top_rgb"] == lib.by_id["person-02"].entry["asset"]["render"]["top_rgb"]
    car = next(a for a in hd["actors"] if a["id"] == "vehicle_c")
    assert car["size_mm"] == [round(x * 1000) for x in lib.by_id["vehicle-01"].entry["size_m"]]
