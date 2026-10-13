"""沙盒场景模板 T1–T6、世界锚点与共享展示剧本 S7（WP-13；AWR-04 §10.4、ADR-094、ADR-112；D2-AC-24、D2-AC-34 的模板部分）。

G1（离线，≤ 10 s）：42 个模板与 S7 的 schema、生成器确定性与提交一致、几何与引用约束、ADR-094 的 S7 组成、M17 模板目录读取。
needs_data：锚点在世界几何上复核（`tools/sandbox/anchors.py --check` 的同一判据）；synthcity 的 T1–T6 以批处理运行 60 s 无错误
（slow）；七个世界全部 42 个模板只在 `AWR_E2E_FULL=1` 时运行（harness 用例）。已知的跨模块缺陷（AWR-V1 在 `follow_path` 中
`POS_ERR_FAILSAFE`／`TILT_ERR_KILL`，d2-WP-13-to-M20-M21 第 1 条）按 xfail 记录，不掩盖其他错误。
S7 的装配与可持续性评估在 P600 2.0.0 的传感器映射合入（含 `nir_ill` 槽位，d2-WP-10-to-M08）前跳过并给出原因。
"""

from __future__ import annotations

import json
import os
import sys
from functools import cache
from pathlib import Path

import pytest
from awrproc import ROOT, world_ready
from e2ehelp import FULL

sys.path.insert(0, str(ROOT / "tools" / "sandbox"))
import gen_templates as G
import sbcommon as C

from awr.datasets.scenarios.catalog import schema_errors

TEMPLATES = sorted(C.SANDBOX.glob("*/t*.json"))
CATALOG = json.loads((ROOT / "vehicles" / "catalog.json").read_text(encoding="utf-8"))
MODEL_CAT = {m["id"]: m.get("category") for m in CATALOG.get("models") or CATALOG.get("items") or []}
NEST_ACCEPTS = {"box": {"multirotor"}, "vtol_pad": {"vtol", "multirotor"}, "launcher": {"fixed_wing"}}
LIMITS = {"vehicles": 12, "targets": 30, "nests": 4, "tasks": 6}
V1_KNOWN = ("POS_ERR_FAILSAFE", "TILT_ERR_KILL", "IMPACT", "preempted by safety", "follow_path 204", "goto 204")


def _doc(world: str, tid: str) -> dict:
    return json.loads((C.SANDBOX / world / f"{tid}.json").read_text(encoding="utf-8"))


def _s7() -> dict:
    return json.loads(C.S7_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 文件集合、schema 与生成器
def test_template_set_complete() -> None:
    got = {(p.parent.name, p.stem) for p in TEMPLATES}
    want = {(w, t) for w in C.WORLDS for t in C.TEMPLATE_IDS}
    assert got == want, sorted(got ^ want)
    assert sorted(p.stem for p in C.ANCHORS.glob("*.json")) == sorted(C.WORLDS)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_template_schema(path: Path) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    assert not schema_errors(d, "sandbox/template.schema.json")
    assert d["template_id"] == path.stem and d["world_id"] == path.parent.name
    assert d["recommended"] is (path.stem == C.DEFAULT_TEMPLATE)
    assert d["calendar_default"]["tz"] == C.TZ[path.parent.name]
    assert d["name_zh"].startswith(path.stem[:2].upper())


def test_s7_schema() -> None:
    d = _s7()
    assert not schema_errors(d, "sandbox/snapshot.schema.json")
    assert d["world_id"] == "synthcity" and d["template_id"] == C.S7_ID


def test_generator_deterministic_and_committed() -> None:
    a, b = G.build_all(), G.build_all()
    assert {str(k): C.dumps_canonical(v) for k, v in a.items()} == {str(k): C.dumps_canonical(v) for k, v in b.items()}
    diff = [str(p.relative_to(ROOT)) for p, d in a.items() if p.read_text(encoding="utf-8") != C.dumps_canonical(d)]
    assert not diff, f"committed files differ from the generator: {diff}（python tools/sandbox/gen_templates.py）"


def test_no_glyphs_or_emoji_in_texts() -> None:
    import re

    bad = re.compile("[" + "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x1F000, 0x1FAFF), (0x2600, 0x27BF))) + chr(0xFE0F) + "]")
    for p in [*TEMPLATES, C.S7_PATH, *C.ANCHORS.glob("*.json")]:
        assert not bad.search(p.read_text(encoding="utf-8")), p


# ---------------------------------------------------------------- 几何与引用（离线）
def _check_config(d: dict, where: str) -> None:
    """实体数量上限、id 唯一、引用存在、机巢与机型兼容、多边形逆时针且 ≤ 1 km²、游走区在世界内。"""
    for k, lim in LIMITS.items():
        assert len(d[k]) <= lim, (where, k)
    nests = {n["id"]: n for n in d["nests"]}
    tgts = {t["id"] for t in d["targets"]}
    assert len(nests) == len(d["nests"]) and len(tgts) == len(d["targets"]), where
    assert len({v["id"] for v in d["vehicles"]}) == len(d["vehicles"]), where
    slots: dict[str, int] = {}
    for v in d["vehicles"]:
        assert v["model_id"] in MODEL_CAT, (where, v["model_id"])
        if v.get("nest_id"):
            n = nests[v["nest_id"]]
            assert MODEL_CAT[v["model_id"]] in NEST_ACCEPTS[n["kind"]], (where, v["id"], n["kind"])
            slots[n["id"]] = slots.get(n["id"], 0) + 1
        else:
            assert v.get("home_enu_m") is not None, (where, v["id"])
    for nid, k in slots.items():
        assert k <= nests[nid]["slots"], (where, nid)
    for t in d["targets"]:
        reg = (t.get("motion") or {}).get("region")
        if reg:
            assert C.signed_area(reg) > 0 and 100.0 <= C.area(reg) <= 1e6, (where, t["id"])
            assert C.inside(t["pos"], reg), (where, t["id"])
    for task in d["tasks"]:
        p = task["params"]
        if "polygon" in p:
            assert C.signed_area(p["polygon"]) > 0 and C.area(p["polygon"]) <= 1e6, (where, task["type"])
            for n in d["nests"]:
                assert not C.inside(n["pos"], p["polygon"]), (where, n["id"])
        look = (p.get("look") or {}).get("target") or p.get("target")
        if look:
            assert look in tgts, (where, look)
        for nid in (task.get("common") or {}).get("allowed_nests") or []:
            assert nid in nests, (where, nid)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_template_geometry_and_references(path: Path) -> None:
    d = json.loads(path.read_text(encoding="utf-8"))
    _check_config(d, str(path.relative_to(ROOT)))
    a = json.loads((C.ANCHORS / f"{d['world_id']}.json").read_text(encoding="utf-8"))
    (x0, y0), (x1, y1) = a["bounds_m"]
    pts = [n["pos"] for n in d["nests"]] + [t["pos"] for t in d["targets"]] + \
          [q for task in d["tasks"] for q in (task["params"].get("polygon") or task["params"].get("waypoints") or [task["params"].get("point")])]
    assert all(x0 <= float(q[0]) <= x1 and y0 <= float(q[1]) <= y1 for q in pts if q is not None)


def test_template_contents_per_table() -> None:
    """AWR-04 §10.4 的模板表：机巢种类与数量、机型、识别物与任务类型。"""
    for w in C.WORLDS:
        t1, t2, t3, t4, t5, t6 = (_doc(w, t) for t in C.TEMPLATE_IDS)
        assert [n["kind"] for n in t1["nests"]] == ["box"] and [v["model_id"] for v in t1["vehicles"]] == ["p600_mid360"] * 2
        assert [t["motion"]["mode"] for t in t1["targets"]] == ["static"] and t1["tasks"][0]["type"] == "point_watch"
        assert len(t2["vehicles"]) == 1 and len(t2["tasks"][0]["params"]["waypoints"]) == 4
        assert t2["tasks"][0]["params"]["mode"] == "loop"
        assert len(t3["nests"]) == 2 and sorted(v["model_id"] for v in t3["vehicles"]) == ["awr_q1", "p600_mid360", "p600_mid360"]
        assert [t["motion"]["mode"] for t in t3["targets"]] == ["random_walk"] * 3 and t3["tasks"][0]["type"] == "area_patrol"
        assert [v["model_id"] for v in t4["vehicles"]] == ["p600_mid360"] * 4 and t4["tasks"][0]["type"] == "area_scan_gsd"
        assert sorted(n["kind"] for n in t5["nests"]) == ["box", "vtol_pad"]
        assert {v["model_id"] for v in t5["vehicles"]} == {"p600_mid360", "awr_v1"} and len(t5["targets"]) == 2
        assert t5["tasks"][0]["type"] == "area_guard" and t5["tasks"][0]["common"]["on_detect"] == "relay"
        assert [n["slots"] for n in t6["nests"]] == [1] and len(t6["vehicles"]) == 1 and len(t6["targets"]) == 1
        assert t6["tasks"][0]["type"] == "point_watch" and t6["tasks"][0]["params"]["dwell_s"] is None


def test_s7_composition_adr094() -> None:
    """ADR-094：机巢 A 机巢箱 6 架 P600（均挂 nir_ill_l）、8 个 6S 通道、14 块电池；机巢 B VTOL 起降台 2 架 AWR-V1、2 个 12S 通道、
    4 块电池；3 个识别物（静止的人、游走的人、沿路车辆）；区域值守 on_detect = relay，巡查只用 AWR-V1（值守周期）。"""
    d = _s7()
    _check_config(d, "s7")
    na, nb = (next(n for n in d["nests"] if n["id"] == k) for k in ("nest-a", "nest-b"))
    va = [v for v in d["vehicles"] if v["nest_id"] == "nest-a"]
    vb = [v for v in d["vehicles"] if v["nest_id"] == "nest-b"]
    assert na["kind"] == "box" and len(va) == 6 and all(v["model_id"] == "p600_mid360" for v in va)
    assert all(v["sensors"] == [{"name": "nir_ill", "enabled": True}] for v in va)
    assert sum(c["count"] * c["channels"] for c in na["chargers"] if c["cells_max"] >= 6) == 8
    assert sum(p["count"] for p in na["energy"]["packs"] if p["cells"] == 6) + len(va) == 14
    assert nb["kind"] == "vtol_pad" and len(vb) == 2 and all(v["model_id"] == "awr_v1" for v in vb)
    assert sum(c["count"] * c["channels"] for c in nb["chargers"] if c["cells_max"] >= 12) == 2
    assert sum(p["count"] for p in nb["energy"]["packs"] if p["cells"] == 12) + len(vb) == 4
    kinds = sorted((t["class"], t["motion"]["mode"]) for t in d["targets"])
    assert kinds == [("person", "random_walk"), ("person", "static"), ("vehicle", "path")]
    (task,) = d["tasks"]
    assert task["type"] == "area_guard" and task["common"]["on_detect"] == "relay"
    assert task["common"]["allowed_models"] == ["awr_v1"] and task["common"]["allowed_nests"] == ["nest-b"]
    assert task["params"]["revisit_s"] == G.S7_REVISIT_S and d["env"]["preset"] == "clear"


def test_m17_template_catalog_reads_the_files() -> None:
    """M17 池的模板目录（`TemplateCatalog`）直接读取本目录：每个世界 6 个，T5 推荐，组装出的 session.json 含全部配置段。"""
    from awr.sandbox.templates import TemplateCatalog, build_session_config

    cat = TemplateCatalog(C.SANDBOX)
    items = cat.items(None, list(C.WORLDS))
    assert len(items) == 42 and sum(1 for it in items if it["recommended"]) == 7
    for w in C.WORLDS:
        assert cat.ids(w) == sorted(C.TEMPLATE_IDS)
    t = cat.get("sanfrancisco", C.T5)
    cfg = build_session_config("sanfrancisco", template_id=C.T5, template=t, snapshot=None, calendar=None, acoustic_bg=None,
                               seed=3, limits={"vehicles_max": 12, "targets_max": 30, "nests_max": 4, "tasks_active_max": 6},
                               tz=C.TZ["sanfrancisco"], now=1791158400.0)
    assert cfg["calendar"] == {"start_utc": "2026-10-04T17:00:00Z", "tz": "America/Los_Angeles"}   # 当地"当日" 10:00
    # D2-FX-R2-tasking 第 2 次（D2-AC-35）：T5 按 D2-AC-19 (a) 的接力资源配置，6 架 P600 + 1 架 AWR-V1
    assert len(cfg["nests"]) == 2 and len(cfg["vehicles"]) == 7 and cfg["tasks"][0]["type"] == "area_guard"


def test_anchor_files_shape() -> None:
    for w in C.WORLDS:
        a = json.loads((C.ANCHORS / f"{w}.json").read_text(encoding="utf-8"))
        assert a["schema"] == "awr.sandbox.anchors.v1" and a["world_id"] == w
        assert a["checks"]["aoi_guard_area_km2"] <= 1.0 and a["checks"]["aoi_scan_area_km2"] <= 1.0
        assert a["checks"]["watch_los"] is True and a["checks"]["target_sky_los_8"] >= 6
        assert all(len(a[k]) == 3 for k in ("nest_box", "nest_pad", "nest_watch"))


# ---------------------------------------------------------------- 世界几何（needs_data）
@pytest.mark.needs_data
@pytest.mark.parametrize("world", C.WORLDS)
def test_anchors_hold_on_world_geometry(world: str) -> None:
    if not world_ready(world):
        pytest.skip(f"worlds/{world} not built")
    import anchors as A

    a = json.loads((C.ANCHORS / f"{world}.json").read_text(encoding="utf-8"))
    assert A.problems(a, A.WorldLayers(world)) == []


# ---------------------------------------------------------------- 60 s 运行（needs_data，slow）
@cache
def _runs(worlds: tuple[str, ...]) -> dict[tuple[str, str], dict]:
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    import check_templates as K

    out = Path(tempfile.mkdtemp(prefix="awr-tpl-"))
    jobs = [(w, t) for w in worlds for t in C.TEMPLATE_IDS]
    with ThreadPoolExecutor(max_workers=int(os.environ.get("AWR_TPL_JOBS", "3"))) as ex:
        res = list(ex.map(lambda j: K.run_one(j[0], j[1], 60.0, out), jobs))
    return {(r["world"], r["template"]): r for r in res}


def _judge_run(world: str, tid: str) -> None:
    if not world_ready(world):
        pytest.skip(f"worlds/{world} not built")
    worlds = tuple(w for w in C.WORLDS if world_ready(w)) if FULL else ("synthcity",)
    r = _runs(worlds)[(world, tid)]
    if r["ok"]:
        return
    v1 = {v["id"] for v in _doc(world, tid)["vehicles"] if v["model_id"] == "awr_v1"}

    def known(p: str) -> str | None:
        if v1 and any(i in p for i in v1) and any(k in p for k in V1_KNOWN):
            return "AWR-V1 path following, d2-WP-13-to-M20-M21 item 1"
        if p.startswith("cmd.failed ") and p.endswith(" follow_path 102"):
            return "M20 transit route fails the M10 fine check (PATH_OBSTACLE), d2-WP-13-to-M20-M21 item 2"
        if "relay_watch -> INFEASIBLE (581 los)" in p:
            return ("relay station search with the D1 camera of P600 1.x (R level within about 205 m): no line-of-sight station "
                    "in the narrow window, d2-WP-10-to-M08 item 1 / d2-WP-13-to-M20-M21 item 4")
        return None

    why = [known(p) for p in r["problems"]]
    if all(why):
        pytest.xfail(f"known cross-module defects ({'; '.join(sorted(set(why)))}): {r['problems'][:2]}")   # type: ignore[arg-type]
    pytest.fail(f"{world}/{tid}: {[p for p, w in zip(r['problems'], why, strict=True) if not w]}")


@pytest.mark.needs_data
@pytest.mark.slow
@pytest.mark.parametrize("tid", C.TEMPLATE_IDS)
def test_synthcity_templates_run_60s(tid: str) -> None:
    _judge_run("synthcity", tid)


@pytest.mark.needs_data
@pytest.mark.slow
@pytest.mark.skipif(not FULL, reason="all 42 templates only with AWR_E2E_FULL=1 (harness case e2e.sandbox-templates)")
@pytest.mark.parametrize("world", [w for w in C.WORLDS if w != "synthcity"])
@pytest.mark.parametrize("tid", C.TEMPLATE_IDS)
def test_all_templates_run_60s(world: str, tid: str) -> None:
    _judge_run(world, tid)


# ---------------------------------------------------------------- S7 装配与可持续性（needs_data）
def _p600_has_nir_ill() -> bool:
    import yaml

    d = yaml.safe_load((ROOT / "vehicles" / "p600" / "params.yaml").read_text(encoding="utf-8"))
    return "nir_ill" in (d.get("sensors") or {})


@pytest.mark.needs_data
@pytest.mark.slow
def test_s7_loads_and_is_sustainable() -> None:
    """S7 在 sim-core 中装配无问题（8 架、3 个识别物、2 个机巢），区域值守 RUNNING，可持续性评估为"是"（M20-AC-031 的配置前提）；
    `tools/sandbox/s7_check.py` 在独立进程中运行（插件登记表与其他用例隔离），日历取今日北京时间 08:00。"""
    import subprocess

    if not world_ready("synthcity"):
        pytest.skip("worlds/synthcity not built")
    if not _p600_has_nir_ill():
        pytest.skip("P600 2.0.0 sensor mapping without the nir_ill slot (d2-WP-10-to-M08 item 1); S7 mounts nir_ill_l on all P600")
    p = subprocess.run([sys.executable, str(ROOT / "tools" / "sandbox" / "s7_check.py"), "--until", "30"], cwd=ROOT,
                       capture_output=True, text=True, timeout=600)
    rep = json.loads(p.stdout.strip().splitlines()[-1])
    assert rep["ok"], rep
