"""剧本静态校验（M16-FR-024、FR-001、FR-002、FR-029；M16-AC-003、004、012（静态部分）、015、019；AWR-16 §12.6）。

G1 用例（≤ 20 s）：schema 与 V-SC 离线规则、catalog（V-SC-13）、curated zones（V-Z、不含 border、SCN-E003）、生成物与
提交一致且逐字节确定（M16-AC-019）、S1 与 ladder 的定稿参数、ladder 各阶段间距构造值（SCN-E002）、soak 的两组分离。
`needs_data` 部分用已构建的世界执行 M10 剧本加载器的 V-SC-02/05/09（世界 zones 换成合并后的 curated 文件）。
"""

from __future__ import annotations

import copy
import json
import math
import re
import warnings

import numpy as np
import pytest
from e2ehelp import SCENARIOS

from awr.datasets.scenarios import authoring as A
from awr.datasets.scenarios import geometry as GEO
from awr.datasets.scenarios.__main__ import committed_pins
from awr.datasets.scenarios.catalog import catalog_errors, load_catalog, scenario_files, schema_errors, zones_errors
from awr.datasets.urbanscene3d.cities import CITIES, CITY_IDS
from awr.sim.mission.scenario_loader import deep_merge, expand_vehicle_sets

ALL = scenario_files(SCENARIOS)
DOCS = {sid: json.loads(p.read_text(encoding="utf-8")) for sid, p in ALL.items()}
BUILTIN = ("s1-shenzhen-facade", "s2-shanghai-formation", "s3-newyork-sar", "s4-chicago-lakeshore",
           "s5-sanfrancisco-terrain", "s6-suzhou-corridor", "ladder-shenzhen", "soak-shenzhen",
           *(f"free-{w}" for w in CITY_IDS))
EMOJI = re.compile("[\U0001f000-\U0001faff" + "".join(f"{chr(a)}-{chr(b)}" for a, b in ((0x2600, 0x27BF), (0x25A0, 0x25FF), (0x2194, 0x21FF))) + chr(0xFE0F) + "]")


def merged(doc: dict, profile: str | None) -> dict:
    if not profile:
        return doc
    base = {k: v for k, v in doc.items() if k != "profiles"}
    return deep_merge(base, doc["profiles"][profile])


def all_variants():
    for sid, d in DOCS.items():
        yield sid, None
        for p in sorted(d.get("profiles") or {}):
            yield sid, p


# ---------------------------------------------------------------- 文件集合与生成物（M16-AC-019）
def test_builtin_set_complete() -> None:
    assert set(BUILTIN) <= set(ALL), sorted(set(BUILTIN) - set(ALL))
    for sid in ALL:
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", sid), sid
    assert sorted(p.name for p in (SCENARIOS / "zones").glob("*.zones.geojson")) == \
        sorted(f"{w}.zones.geojson" for w in CITY_IDS)


def test_authoring_deterministic_and_committed() -> None:
    """同输入两次生成逐字节一致；提交的文件等于生成物（剧本只能经 authoring 修改）。"""
    pins = committed_pins(SCENARIOS)
    a, b = A.build_all(pins), A.build_all(copy.deepcopy(pins))
    assert {k: A.dumps_canonical(v) for k, v in a.items()} == {k: A.dumps_canonical(v) for k, v in b.items()}
    diff = [rel for rel, doc in a.items() if (SCENARIOS / rel).read_bytes() != A.dumps_canonical(doc).encode("utf-8")]
    assert not diff, f"committed files differ from the generator: {diff}（python -m awr.datasets.scenarios generate）"


def test_canonical_encoder_rules() -> None:
    s = A.dumps_canonical({"a": [1, 2.5, None], "b": {"c": "x"}, "long": list(range(60))})
    assert s.endswith("\n") and '"a": [1, 2.5, null]' in s and json.loads(s)["long"][-1] == 59
    assert A._r(-0.0) == 0 and A._r(12.0) == 12 and A._r(1.23456) == 1.235


# ---------------------------------------------------------------- schema 与 V-SC 离线规则（M16-AC-015）
@pytest.mark.parametrize("sid,profile", list(all_variants()))
def test_schema_every_profile(sid: str, profile: str | None) -> None:
    d = merged(DOCS[sid], profile)
    assert not schema_errors(d, "scenario/scenario.schema.json")
    assert d["scenario_id"] == sid and d["world_id"] == A.world_of(sid)
    vehicles, missions = expand_vehicle_sets(d)
    ids = [v["vehicle_id"] for v in vehicles]
    assert len(ids) == len(set(ids)) and len(ids) <= 1000                                     # V-SC-04
    assert {m["mission_id"] for m in missions}.__len__() == len(missions)                      # V-SC-06
    assert set().union(*[set(m["vehicle_ids"]) for m in missions] or [set()]) <= set(ids)
    zmax = CITIES[d["world_id"]].border_max_z_m
    for m in missions:                                                                        # V-SC-07
        p = m["params"]
        for z in [p.get("z_m"), *(p.get("z_range_m") or [])]:
            assert z is None or z <= zmax, (m["mission_id"], z, zmax)
    tl = d.get("time_limit_s", 1800)
    for ev in d.get("events") or []:                                                          # V-SC-10
        assert ev.get("at_s") is None or ev["at_s"] <= tl
        if ev["action"] == "cmd":
            assert ev["args"]["op"] in ("rtl", "land", "hover")
    if d["scenario_id"].startswith("ladder"):
        assert d["record"] is False                                                          # 16 §12.2


def test_names_and_determinism_fields() -> None:
    names = {}
    for sid, d in DOCS.items():
        for k in ("name", "name_zh", "description"):
            assert not EMOJI.search(d.get(k, "")), (sid, k)                                   # EMOJI-01、GLYPH-01
        assert d["name"] not in names, (sid, names.get(d["name"]))
        names[d["name"]] = sid
        assert d.get("seed", 7) == 7                                                          # ADR-049：种子固定
        assert d["gcs_loss_policy"] in ("ignore", "hold_rtl")                                 # 必须显式声明


def test_catalog_v_sc_13() -> None:
    cat = load_catalog(SCENARIOS)
    assert catalog_errors(cat, SCENARIOS) == []
    assert cat["worlds"]["shenzhen"]["default"] == "s1-shenzhen-facade" and cat["worlds"]["shenzhen"]["gate"] is True
    assert set(cat["worlds"]) == set(CITY_IDS)
    for w in CITY_IDS:
        assert cat["worlds"][w]["default"] == CITIES[w].default_scenario
    bad = copy.deepcopy(cat)
    bad["worlds"]["shanghai"]["default"] = "s1-shenzhen-facade"
    bad["ui_profiles"]["ladder-shenzhen"].append("n7")
    bad["worlds"]["newyork"]["gate"] = True
    errs = catalog_errors(bad, SCENARIOS)
    assert any("world_id shenzhen" in e for e in errs) and any("'n7'" in e for e in errs)
    assert any("more than one gate" in e for e in errs)


# ---------------------------------------------------------------- 定稿参数
def test_s1_parameters_match_awr12_7_2() -> None:
    d = DOCS["s1-shenzhen-facade"]
    v = {x["vehicle_id"]: x for x in d["vehicles"]}
    assert v["p600-01"]["home_enu_m"] == [-230, 20, None] and v["p600-02"]["home_enu_m"] == [-230, 40, None]
    assert v["p600-02"]["sensors"] == ["camera", "mid360"] and all(x["speed_profile"] == "px4_default" for x in v.values())
    m = {x["mission_id"]: x["params"] for x in d["missions"]}
    for mid, zr in (("m-lower", [252, 50]), ("m-upper", [391, 248])):
        p = m[mid]
        assert p["center_enu_m"] == [-162.2, 77.3] and p["radius_m"] == 57 and p["standoff_m"] == 30
        assert p["z_range_m"] == zr and p["dz_per_rev_m"] == 18.47 and p["speed_mps"] == 6.0 and p["direction"] == "ccw"
        assert zr[0] > zr[1]                                                                  # 两机均自上而下
        assert p["facade_z_range_m"] == [45, 374.1]                                           # 12 §7.1.3 立面网格
    assert d["gcs_loss_policy"] == "ignore" and d["profiles"]["ci"] == {"rate": 10, "record": False}  # D1-AC-15
    assert d["env"]["patch"]["wind"] == {"speed_ref_mps": 6.0, "dir_from_deg": 135, "turb_sigma_u_ref_mps": 1.0}
    gust = next(e for e in d["events"] if e["action"] == "env.gust")
    assert gust["at_s"] == 420 and gust["args"] == {"amp_mps": 6.0, "length_m": 120}
    leaves = {x["metric"] for x in d["success"]["all"]}
    assert {"missions_done", "min_separation_m", "guard_events", "pos_err_max_m", "energy_rtl_count"} <= leaves
    # profile 合并：wx-fog 只改预设（风继承 6 m/s），wx-rain / wx-storm 改为安全终止谓词
    fog, storm = merged(d, "wx-fog"), merged(d, "wx-storm")
    assert fog["env"]["preset"] == "fog" and fog["env"]["patch"]["wind"]["speed_ref_mps"] == 6.0
    assert storm["env"]["patch"]["wind"] == {"speed_ref_mps": 14.0, "dir_from_deg": 135, "turb_sigma_u_ref_mps": 5.0}
    assert storm["energy_precheck"] == "warn" and len(storm["success"]["all"]) == 2
    assert merged(d, "demo")["on_complete"] == "continue"


LADDER_TABLE = {   # M16 §6.4.8：N → (每层机数, 每层列数, L0 起点)
    10: ([3, 3, 2, 2], 2, (-393, 2)), 50: ([13, 13, 12, 12], 4, (-417, -22)), 100: ([25] * 4, 5, (-429, -34)),
    200: ([50] * 4, 8, (-465, -58)), 500: ([125] * 4, 12, (-513, -106)), 1000: ([250] * 4, 16, (-561, -166)),
}


@pytest.mark.parametrize("n", sorted(LADDER_TABLE))
def test_ladder_layout_table(n: int) -> None:
    d = merged(DOCS["ladder-shenzhen"], f"n{n}")
    counts, cols, (x0, y0) = LADDER_TABLE[n]
    sets = d["vehicle_sets"]
    assert [s["count"] for s in sets] == counts and all(s["layout"]["cols"] == cols for s in sets)
    offs = [(0, 0), (12, 0), (0, 12), (12, 12)]
    for L, s in enumerate(sets):
        assert s["layout"]["origin_enu_m"][:2] == [x0 + offs[L][0], y0 + offs[L][1]]
        assert s["mission"]["start"]["at_s"] == 15 - 5 * L                                    # 高层先飞
        assert s["mission"]["params"]["agl_m"] == 60 + 15 * L and s["mission"]["params"]["radius_m"] == 3
    v, _ = expand_vehicle_sets(d)
    assert len(v) == n and v[0]["vehicle_id"] == "sim-0001" and v[-1]["vehicle_id"] == f"sim-{n:04d}"
    assert d["events"][0]["args"]["label"] == "ladder.steady" and d["events"][0]["when"]["value"] == 45


@pytest.mark.parametrize("n", sorted(LADDER_TABLE))
def test_ladder_constructed_separation(n: int) -> None:
    """SCN-E002：M16 §6.4.8 的阶段构造值（地面起飞的理想口径）：离地后任意阶段 ≥ 16.2 m，错时爬升 19.2 m。"""
    r = GEO.ladder_min_separation(merged(DOCS["ladder-shenzhen"], f"n{n}")["vehicle_sets"])
    assert r["ground"] == pytest.approx(12.0)
    assert r["climb"] == pytest.approx(19.2, abs=0.05)
    assert r["entry"] >= 16.15 and r["orbit"] >= 16.15 and r["any_phase"] >= 16.15


def test_ladder_x500_profile() -> None:
    d = merged(DOCS["ladder-shenzhen"], "x500")
    assert {s["profile_id"] for s in d["vehicle_sets"]} == {"x500"} and sum(s["count"] for s in d["vehicle_sets"]) == 200


def test_free_scenarios() -> None:
    for w in CITY_IDS:
        d = DOCS[f"free-{w}"]
        h = [v["home_enu_m"] for v in d["vehicles"]]
        assert len(h) == 2 and math.dist(h[0][:2], h[1][:2]) == 6.0 and not d.get("missions")
        assert d["on_complete"] == "continue" and d["success"] == {"all": [{"metric": "guard_events", "op": "==", "value": 0}]}
        assert [h[0][0] + 3, h[0][1]] == list(CITIES[w].free_pad)
        assert h[0][2] == (0 if w == "suzhou" else None)                                    # 苏州无点区显式 z = 0


def test_soak_composition_and_separation() -> None:
    """§7.3.4：S1 两机、任务与事件原样保留，另加 ladder n200（x500、200 圈）；两组最小水平距离约 52 m。"""
    soak, s1 = DOCS["soak-shenzhen"], DOCS["s1-shenzhen-facade"]
    assert soak["vehicles"] == s1["vehicles"] and soak["missions"] == s1["missions"] and soak["events"] == s1["events"]
    assert {s["profile_id"] for s in soak["vehicle_sets"]} == {"x500"}
    assert all(s["mission"]["params"]["turns"] == 200 for s in soak["vehicle_sets"])
    assert soak["time_limit_s"] == 2400 and soak["on_complete"] == "continue"
    v, _ = expand_vehicle_sets(soak)
    lad = np.array([x["home_enu_m"][:2] for x in v if x["vehicle_id"].startswith("sim-")], float)
    s1pl = GEO.plan_polylines(s1)
    s1pts = np.vstack([p.reshape(-1, 2) for k in ("homes", "missions", "returns") for p in s1pl[k]])
    d = np.linalg.norm(lad[:, None, :] - s1pts[None, :, :], axis=2).min() - 3.0              # 环绕半径 3 m
    assert d >= 50.0, d


# ---------------------------------------------------------------- curated zones（M16-AC-003）
@pytest.mark.parametrize("wid", CITY_IDS)
def test_curated_zones_valid(wid: str) -> None:
    fc = json.loads((SCENARIOS / "zones" / f"{wid}.zones.geojson").read_text(encoding="utf-8"))
    assert zones_errors(fc, wid) == []
    assert [f["id"] for f in fc["features"]] == [z.zone_id for z in CITIES[wid].zones]
    for f, z in zip(fc["features"], CITIES[wid].zones, strict=True):
        ring = np.asarray(f["geometry"]["coordinates"][0])
        assert len(ring) == 33 and np.allclose(np.linalg.norm(ring - z.center, axis=1), z.radius_m, atol=0.01)
    if wid == "suzhou":
        assert fc["features"] == []


@pytest.mark.parametrize("sid", sorted(ALL))
def test_zones_do_not_intersect_plans(sid: str) -> None:
    """SCN-E003：curated 区域与剧本的出生点、任务航线、返航线不相交（10 m 缓冲）。"""
    d = DOCS[sid]
    fc = json.loads((SCENARIOS / "zones" / f"{d['world_id']}.zones.geojson").read_text(encoding="utf-8"))
    pl = GEO.plan_polylines(d)
    for f in fc["features"]:
        for kind in ("homes", "missions", "returns"):
            for line in pl[kind]:
                dist = GEO.polyline_zone_distance(line, f)
                assert dist >= 10.0, f"{sid}: {kind} within {dist:.1f} m of {f['id']}"


def test_zone_geometry_helpers() -> None:
    z = A.circle_zone("nofly-x", "nofly", (0.0, 0.0), 50.0)
    assert GEO.polyline_zone_distance(np.array([[0.0, 0.0]]), z) == 0.0
    assert GEO.polyline_zone_distance(np.array([[-100.0, 60.0], [100.0, 60.0]]), z) == pytest.approx(10.0, abs=0.2)
    assert GEO.polyline_intersects_zone(np.array([[-100.0, 0.0], [100.0, 0.0]]), z)
    sq = GEO.expanding_square((0.0, 0.0), 55.0, 4)
    assert np.allclose(sq[1], [55, 0]) and np.allclose(sq[2], [55, 55]) and np.allclose(sq[3], [-55, 55])


# ---------------------------------------------------------------- 需要世界：加载器 V-SC-02/05/09
@pytest.mark.needs_data
@pytest.mark.parametrize("sid,profile", list(all_variants()))
def test_loader_rules_on_built_worlds(sid: str, profile: str | None, world_query, profile_table) -> None:
    from awr.datasets.scenarios.catalog import validate_scenario

    d = DOCS[sid]
    sc = validate_scenario(d, profile=profile, world=world_query(d["world_id"]), profiles=profile_table)
    assert all(w.startswith("V-SC-11") for w in sc.warnings), sc.warnings


@pytest.mark.needs_data
def test_ladder_site_and_rooftop_homes(world_query) -> None:
    """RK-M16-10：ladder 占地内 HAG ≤ 35 m（direct 转场的前提）。出生点多在屋顶（DSM），按"从出生高度爬升"复算阶段
    构造值：低于 16.2 m 时告警（设计口径假设地面起飞，见实现报告的遗留问题），低于 10 m（FleetGuard CONFLICT）时记录。"""
    wq = world_query("shenzhen")
    d = merged(DOCS["ladder-shenzhen"], "n1000")
    v, _ = expand_vehicle_sets(d)
    H = np.array([x["home_enu_m"][:2] for x in v], float)
    hag = np.asarray(wq.height_dsm(H)) - np.asarray(wq.ground_dtm(H))
    assert float(hag.max()) <= 35.0
    r = GEO.ladder_min_separation(d["vehicle_sets"], home_z=hag)
    assert r["entry"] >= 16.15 and r["orbit"] >= 16.15
    if r["climb"] < 16.15:
        warnings.warn(f"ladder rooftop-aware climb separation {r['climb']:.1f} m < 16.2 m "
                      f"({int((hag > 5).sum())} of {len(hag)} homes on roofs); see M16 report open issue", stacklevel=1)
