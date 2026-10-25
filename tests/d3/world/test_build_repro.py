"""世界包构建可重复性与 crop 构建记录（D3-AC-02"两次构建 contentVersion 相同""内容文件 sha256 相同""单世界构建 ≤ 10 min"
"V-GLB 全部通过"；M22 §13 第 4 条；W07）。

1. 合成世界（带 OSM 道路与 DEM 输入，走完 normalize → … → navmesh → navgraph → package 全部阶段）：两次独立构建分别用缺省
   配置与"NavMesh 单线程烘焙 + 竖向栅格化与质量采样走 numpy 路径（不用 numba）"，`contentVersion`、`files[]` 与每个内容文件的
   sha256 逐一相同；非内容文件（`world.json`、`qa/report.json`）之外不允许任何字节差异。
2. 已发布的 crop（`worlds/oryahovo-crop1`，缺失时按 data 层跳过）：`contentVersion` 可由内容文件重算；`scaleStatus = landmark`
   且 `metric_ok()` 为真；全部适用的 V-GLB 规则通过（深度校验现场重跑）；调平 L1 与 L3/L4/L5 之一成立、尺度与 r2 差 ≤ 0.5%；
   构建各阶段耗时之和 ≤ 600 s（不含配准）；`dataset` 的许可、署名与 `derivedFrom[]`（OSM ODbL、SRTM、GLO-30）齐全。
3. 选择性用例（`AWR_D3_CROP_REBUILD=1`）：在临时目录（或 `AWR_D3_CROP_REBUILD_DIR`）从原始数据重建 crop，与已发布世界包逐文件
   比对 sha256，并计时（≤ 600 s）。本机约 5 min，跨机器（AVX2 与 AVX-512）对拍由 X01 在 dmax 上执行同一用例。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parents[3]
WORLDS = Path(os.environ.get("AWR_WORLDS_DIR") or ROOT / "worlds")
CROP_ID = "oryahovo-crop1"
CROP = WORLDS / CROP_ID
BUILD_MAX_S = 600.0                 # D3-AC-02：单世界构建（不含配准）≤ 10 min
SCALE_MAX_REL = 0.005
NON_CONTENT = ("world.json", "qa/report.json")


def _j(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _content(world: Path) -> dict[str, str]:
    from awr.world.package.manifest import scan_content_files

    return {f["path"]: f["sha256"] for f in scan_content_files(world)}


# ---------------------------------------------------------------- 1. 合成世界两次构建


WID = "synth-repro"


def _recipe(tmp: Path, mp: pytest.MonkeyPatch):
    """合成场景（300 m 平地加盒子）+ OSM 道路（行人与车辆 NavMesh 种子、车道图）+ 水面多边形 + DEM 输入的配方与已接受配准。"""
    import test_glb_build as gb
    from glbfix import png, write_glb
    from test_lanes import osm_xml

    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration

    raw = tmp / "raw"
    for d in ("synth", "osm", "dem"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    V, F, UV = gb._scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    nodes = {1: (-130, 0), 2: (0, 0), 3: (130, 0), 4: (0, -130), 5: (0, 130), 6: (-130, 100)}
    ways = [(10, [1, 2, 3], {"highway": "primary"}), (11, [4, 2, 5], {"highway": "residential"}), (12, [6, 1], {"highway": "footway"})]
    anchor = {"kind": "synthetic", "datum": "WGS84", "latDeg": 43.73, "lonDeg": 23.96, "hEllipsoidM": 100.0, "hMslM": 100.0}
    osm = osm_xml(raw / "osm" / "roads.osm", anchor=anchor, nodes=nodes, ways=ways)
    dem = raw / "dem" / "N43E023.hgt"
    dem.write_bytes(bytes(1201 * 1201 * 2))
    files = [{"name": f"{q.parent.name}/{q.name}", "bytes": q.stat().st_size, "sha256": file_sha256(q)} for q in (glb, osm, dem)]
    man = tmp / "raw-manifest.json"
    man.write_text(json.dumps({"files": files}))
    mp.setattr(gb.recipe_mod, "RAW_DIR", raw)
    mp.setattr(gb.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp / "recipes" / WID
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        f"schema: awr.glb.recipe.v1\nworld_id: {WID}\nname: Synthetic reproducibility world\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(glb)}}}\n"
        "  sketchfab: {uid: 0123abcd, author: 'Test Author (https://example.org/a)', license: CC-BY-4.0}\n"
        f"osm: {{path: osm/roads.osm, sha256: {file_sha256(osm)}, extract: 'synthetic roads 2026-10-19'}}\n"
        f"dem: {{path: dem/N43E023.hgt, sha256: {file_sha256(dem)}, model: srtm30m}}\n"
        "registration: registration.json\nbuild:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n"
        "  nav: {person: {keep: largest-seeded}}\n")
    rec = load_recipe(WID, tmp / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture: unit scale by construction"])
    write_registration(rec, doc, accept_by="test")
    return rec


def _build(rec, worlds: Path):
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    return build_world(GlbAdapter(WID, recipe=rec), worlds, ctx=StageContext(world_id=WID))


@pytest.fixture(scope="module")
def two_builds(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    pytest.importorskip("PIL")
    import awr.world.glb.derive.quality as quality_mod
    import awr.world.glb.raster as raster_mod

    tmp = tmp_path_factory.mktemp("repro")
    mp = pytest.MonkeyPatch()
    try:
        rec = _recipe(tmp, mp)
        r1 = _build(rec, tmp / "w1")
        assert r1.exit_code == 0, r1.message
        mp.setenv("AWR_NAV_THREADS", "1")                       # 第二次：单线程烘焙、numpy 路径
        mp.setattr(raster_mod, "HAVE_NUMBA", False)
        mp.setattr(quality_mod, "HAVE_NUMBA", False)
        r2 = _build(rec, tmp / "w2")
        assert r2.exit_code == 0, r2.message
        assert r1.content_version == r2.content_version
    finally:
        mp.undo()
    return tmp / "w1" / WID, tmp / "w2" / WID


def test_two_builds_have_identical_content_version_and_files(two_builds: tuple[Path, Path]) -> None:
    from awr.world.package.manifest import content_version, scan_content_files

    a, b = two_builds
    wa, wb = _j(a / "world.json"), _j(b / "world.json")
    assert wa["contentVersion"] == wb["contentVersion"] == content_version(scan_content_files(a))
    assert wa["files"] == wb["files"]
    ca, cb = _content(a), _content(b)
    assert ca == cb and len(ca) >= 20
    stages = {s["name"] for s in _j(a / "qa/report.json")["glb"]["stages"]}
    assert {"raycast", "dsm", "quality", "occupancy", "navmesh", "navgraph"} <= stages        # 全部派生阶段都参与了比对
    assert {"geometry/nav/person.bin", "geometry/nav/graph.geojson", "semantic/quality.geojson", "coordinate.json",
            "geometry/terrain/dsm_2m.f32"} <= set(ca)
    for p in NON_CONTENT:
        assert (a / p).is_file() and p not in ca


def test_non_content_files_differ_only_in_timings_and_dates(two_builds: tuple[Path, Path]) -> None:
    a, b = two_builds
    ra, rb = _j(a / "qa/report.json"), _j(b / "qa/report.json")
    assert ra["content_version"] == rb["content_version"] and ra["gates"] == rb["gates"] and ra["status"] == rb["status"]
    assert ra["glb"]["vglb"] == rb["glb"]["vglb"] and ra["glb"]["leveling"]["L1"] == rb["glb"]["leveling"]["L1"]
    na, nb = ra["glb"].get("navgraph") or {}, rb["glb"].get("navgraph") or {}
    assert na.get("content_sha256") and na.get("content_sha256") == nb.get("content_sha256")


# ---------------------------------------------------------------- 2. 已发布 crop 的构建记录


crop_only = pytest.mark.skipif(not (CROP / "world.json").is_file(),
                               reason="needs worlds/oryahovo-crop1 (python -m awr.world.glb build oryahovo-crop1)")


@crop_only
def test_crop_content_version_recomputes_and_metric_ok() -> None:
    from awr.world.geometry.status import metric_ok
    from awr.world.package.manifest import content_version, scan_content_files

    w = _j(CROP / "world.json")
    files = scan_content_files(CROP)
    assert w["contentVersion"] == content_version(files)
    assert {f["path"]: f["sha256"] for f in w["files"]} == {f["path"]: f["sha256"] for f in files}
    assert w["scaleStatus"] == "landmark" and w["profile"] == "d3-glb"
    ms = metric_ok(CROP)
    assert ms.ok, ms


@crop_only
def test_crop_vglb_all_applicable_rules_pass_on_a_fresh_deep_validation() -> None:
    from awr.world.package.validate import validate_world, vglb_results

    rep = validate_world(CROP, deep=True, staging=False)
    rows = {r["id"]: r for r in vglb_results(rep)}
    assert len(rows) == 16
    bad = {k: r for k, r in rows.items() if r["status"] not in ("pass", "n/a")}
    assert not bad, bad
    assert not rep.errors, rep.errors[:5]
    na = sorted(k for k, r in rows.items() if r["status"] == "n/a")
    assert set(na) <= {"V-GLB-06"}, na                                   # 锚点阶段尚未交付（M22 §6.6.11 anchors）
    stored = {r["id"]: r["status"] for r in _j(CROP / "qa/report.json")["glb"]["vglb"]}
    assert {k: r["status"] for k, r in rows.items()} == stored


@crop_only
def test_crop_levelling_scale_and_timing_record() -> None:
    rep = _j(CROP / "qa/report.json")
    g = rep["glb"]
    lv = g["leveling"]
    assert lv["km_scale"] and lv["L1"] and (lv["L3"] or lv["L4"] or lv.get("L5")) and not lv["needs_control_points"]
    if lv.get("L5"):
        xc = lv["cross_check"]
        assert xc["pass"] and xc["missing"] == [] and "dem:glo30" in xc["methods"]
        assert xc["max_pair_delta_deg"] <= 0.5 and max(xc["residual_tilt_deg"].values()) <= 0.5 and xc["consensus_tilt_deg"] <= 0.2
    gates = {x["id"]: x for x in rep["gates"]}
    assert all(gates[k]["pass"] for k in ("G-04", "G-05", "G-08", "G-11", "G-13", "G-14", "G-15"))
    sc = g["scale"]
    assert abs(sc["rel_diff"]) <= SCALE_MAX_REL and sc["fit"]["restarts_rel_std"] <= SCALE_MAX_REL
    total = sum(float(s["seconds"]) for s in g["stages"]) + float(lv.get("seconds") or 0.0)     # 各阶段加调平 QA（不含配准）
    assert 0 < total <= BUILD_MAX_S, total
    assert {"navmesh", "navgraph", "quality", "occupancy"} <= {s["name"] for s in g["stages"]}
    person = _j(CROP / "geometry/nav/person.json")                   # D3-AC-03：行人只保留含种子的最大岛（配方 build.nav.person.keep）
    assert person["keep"] == "largest-seeded" and g["navmesh"]["profiles"]["person"]["islands"]["kept"] == 1


@crop_only
def test_crop_dataset_licence_and_derived_from() -> None:
    d = _j(CROP / "world.json")["dataset"]
    assert d["license"] == "CC-BY-4.0" and "CC-BY-4.0" in d["citation"] and d["url"].startswith("https://sketchfab.com/")
    assert "ODbL" in d["notice"]
    names = {e["name"]: e for e in d["derivedFrom"]}
    assert {"OpenStreetMap", "SRTM 1 arc-second", "Copernicus DEM GLO-30"} <= set(names)
    assert names["OpenStreetMap"]["license"] == "ODbL-1.0" and names["OpenStreetMap"]["attribution"] == "OpenStreetMap contributors"
    assert "Terrain Tiles" in names["SRTM 1 arc-second"]["attribution"] and "levelling" in names["Copernicus DEM GLO-30"]["use"]
    assert all(len(e["sha256"]) == 64 for e in d["derivedFrom"])


# ---------------------------------------------------------------- 3. crop 从原始数据重建并逐文件比对（选择性）


@crop_only
@pytest.mark.skipif(os.environ.get("AWR_D3_CROP_REBUILD") != "1", reason="set AWR_D3_CROP_REBUILD=1 (full crop rebuild, about 5 min)")
def test_crop_rebuild_matches_published_world(tmp_path: Path) -> None:
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    out = Path(os.environ.get("AWR_D3_CROP_REBUILD_DIR") or tmp_path) / "worlds"
    t0 = time.perf_counter()
    res = build_world(GlbAdapter(CROP_ID), out, ctx=StageContext(world_id=CROP_ID))
    secs = time.perf_counter() - t0
    assert res.exit_code == 0, res.message
    w = _j(CROP / "world.json")
    got, want = _content(out / CROP_ID), _content(CROP)
    diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
    print(json.dumps({"contentVersion": res.content_version, "seconds": round(secs, 1), "files": len(got), "differ": diff[:20]}))
    assert res.content_version == w["contentVersion"] and not diff
    assert secs <= BUILD_MAX_S
