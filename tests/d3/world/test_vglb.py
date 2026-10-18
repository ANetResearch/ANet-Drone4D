"""V-GLB 规则组、`dataset.derivedFrom` 与世界清单署名（M22 §7.8、§6.6.6、FR-064、FR-065；M22-AC-006、007、009、028；
D3-AC-02 "V-GLB 全部通过、dsm_2m 与网格竖向射线抽样差 < 1 cm、dataset.license、citation、derivedFrom[]（含 ODbL）齐全"；
D3-AC-24 "world_manifest.json 含 CC-BY-4.0 原作者署名与 OSM ODbL 声明"）。

夹具为合成 GLB 世界（与 test_glb_build 同一场景：300 m 平地加 20 × 20 × 30 m 盒子），配方带 OSM 与 DEM 输入、CC-BY-4.0 来源；
每条规则以"已发布世界通过 + 逐项变异被同一规则拦下"验证。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PIL")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from awr.world.glb.manifest import check_world_manifest, notice_text, world_manifest
from awr.world.glb.raster import raster_top, trimesh_world
from awr.world.package.validate import (
    GLB_RULES,
    RULES,
    VGLB_RULE_IDS,
    coverage_mask,
    report_reason,
    validate_world,
    vertical_hits,
    vglb_results,
)

WID = "synth-vglb"
OSM_XML = (b'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="test" copyright="OpenStreetMap and contributors">\n'
           b' <node id="1" lat="43.7300" lon="23.9600"/>\n</osm>\n')
DEM_BYTES = bytes(1201 * 1201 * 2)            # 3″ 瓦片大小；构建只登记 sha256，不解析


def _sha(p: Path) -> str:
    from awr.world.glb.recipe import file_sha256

    return file_sha256(p)


def _write_fixture(tmp: Path, mp: pytest.MonkeyPatch, *, license_: str = "CC-BY-4.0", with_inputs: bool = True) -> object:
    import test_glb_build as g
    from glbfix import png, write_glb

    from awr.world.glb.recipe import load_recipe
    from awr.world.glb.register import register_manual, write_registration

    raw = tmp / "raw"
    for d in ("synth", "osm", "dem"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    V, F, UV = g._scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (200, 30, 30)))
    osm = raw / "osm" / "test.osm"
    osm.write_bytes(OSM_XML)
    dem = raw / "dem" / "N43E023.hgt"
    dem.write_bytes(DEM_BYTES)
    files = [{"name": f"{p.parent.name}/{p.name}", "bytes": p.stat().st_size, "sha256": _sha(p)} for p in (glb, osm, dem)]
    man = tmp / "raw-manifest.json"
    man.write_text(json.dumps({"files": files}))
    mp.setattr(g.recipe_mod, "RAW_DIR", raw)
    mp.setattr(g.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp / "recipes" / WID
    rd.mkdir(parents=True, exist_ok=True)
    inputs = (f"osm: {{path: osm/test.osm, sha256: {_sha(osm)}, extract: 'test extract 2026-10-11'}}\n"
              f"dem: {{path: dem/N43E023.hgt, sha256: {_sha(dem)}, model: srtm30m}}\n") if with_inputs else ""
    (rd / "recipe.yaml").write_text(
        f"schema: awr.glb.recipe.v1\nworld_id: {WID}\nname: Synthetic V-GLB test world\nname_zh: 合成校验世界\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {_sha(glb)}}}\n"
        f"  sketchfab: {{uid: 0123abcd, author: 'Test Author (https://example.org/a)', license: {license_}}}\n"
        f"{inputs}registration: registration.json\n"
        "build:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
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
def world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp = tmp_path_factory.mktemp("vglb")
    mp = pytest.MonkeyPatch()
    try:
        rec = _write_fixture(tmp, mp)
        res = _build(rec, tmp / "worlds")
        assert res.exit_code == 0, res.message
    finally:
        mp.undo()
    return tmp / "worlds" / WID


@pytest.fixture()
def copy(world: Path, tmp_path: Path) -> Path:
    d = tmp_path / WID
    shutil.copytree(world, d)
    return d


def _j(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _jw(p: Path, doc: dict) -> None:
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def _run(d: Path, rid: str, *, staging: bool = False):
    return validate_world(d, deep=True, rules={rid}, staging=staging)


def _fails(d: Path, rid: str, **kw) -> bool:
    rep = _run(d, rid, **kw)
    assert rid not in rep.skipped_rules, rep.skipped_rules
    return rid in rep.rule_ids("errors")


# ---------------------------------------------------------------- 竖向栅格化核


def _random_mesh(seed: int) -> tuple[np.ndarray, np.ndarray]:
    """起伏的高度场（共享边）叠加若干悬空三角形与竖直面：覆盖多层表面、共边采样与退化面。"""
    rng = np.random.default_rng(seed)
    n = 13
    x, y = np.meshgrid(np.linspace(0.3, 37.7, n), np.linspace(-0.4, 25.1, n))
    z = 2.0 * np.sin(x / 5.0) + 0.3 * y + rng.normal(0, 0.2, x.shape)
    V = np.stack([x.ravel(), y.ravel(), z.ravel()], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, b, c], [a, c, d]] if (i + j) % 2 else [[a, c, b], [a, d, c]]   # 两种绕向
    extra = rng.uniform([0, -1, 5], [38, 26, 9], (30, 3))
    base = len(V)
    V = np.vstack([V, extra, [[10.0, 10.0, 0.0], [12.0, 10.0, 0.0], [10.0, 10.0, 5.0]]])
    F += [[base + 3 * k, base + 3 * k + 1, base + 3 * k + 2] for k in range(10)]
    F += [[base + 30, base + 31, base + 32]]                                                  # 竖直面
    return V, np.asarray(F, np.int64)


def test_raster_kernel_numba_numpy_and_validator_agree() -> None:
    V, F = _random_mesh(5)
    org, cell, H, W = (0.0, -1.0), 2.0, 14, 20
    a_top, a_n = raster_top(V, F, org, cell, H, W, 4, use_numba=True)
    b_top, b_n = raster_top(V, F, org, cell, H, W, 4, use_numba=False)
    assert np.array_equal(a_top, b_top) and np.array_equal(a_n, b_n)
    r, c = np.divmod(np.arange(H * W), W)
    v_top, v_n = vertical_hits(V, F, org, cell, r, c)
    assert np.array_equal(v_top, a_top.ravel()) and np.array_equal(v_n, a_n.ravel().astype(np.int64))
    assert a_n.max() == 16 and (a_n == 0).any() and np.isfinite(a_top[a_n > 0]).all()


def test_raster_plane_is_exact_and_vertical_faces_miss() -> None:
    V = np.array([[0.0, 0.0, 1.0], [40.0, 0.0, 1.0 + 0.2 * 40], [0.0, 40.0, 1.0 - 0.1 * 40], [40.0, 40.0, 1.0 + 0.2 * 40 - 0.1 * 40]])
    F = np.array([[0, 1, 3], [0, 3, 2]])
    top, n = raster_top(V, F, (0.0, 0.0), 2.0, 20, 20, 4)
    assert (n == 16).all()
    xs = (np.arange(20 * 4) + 0.5) * 0.5
    zs = 1.0 + 0.2 * xs[None, :] - 0.1 * xs[:, None]                                      # 样本 (i, j) 的平面高度
    want = zs.reshape(20, 4, 20, 4).max(axis=(1, 3))
    assert np.abs(top - want).max() < 1e-9
    wall = np.array([[5.0, 5.0, 0.0], [9.0, 5.0, 0.0], [5.0, 5.0, 10.0]])
    t2, n2 = raster_top(wall, np.array([[0, 1, 2]]), (0.0, 0.0), 2.0, 8, 8, 4)
    assert n2.sum() == 0 and np.isneginf(t2).all()


def test_raster_is_independent_of_triangle_order() -> None:
    V, F = _random_mesh(9)
    a = raster_top(V, F, (0.0, -1.0), 2.0, 14, 20)
    perm = np.random.default_rng(1).permutation(len(F))
    b = raster_top(V, F[perm], (0.0, -1.0), 2.0, 14, 20)
    assert np.array_equal(a[0], b[0]) and np.array_equal(a[1], b[1])                # 三角形顺序无关（逐位）
    c = raster_top(V, F[:, [1, 2, 0]], (0.0, -1.0), 2.0, 14, 20)                    # 顶点轮换：舍入不同，差在 1e-9 m 以内
    fin = np.isfinite(a[0])
    assert np.array_equal(a[1], c[1]) and np.abs(a[0][fin] - c[0][fin]).max() < 1e-9


# ---------------------------------------------------------------- 已发布世界：V-GLB 全部通过


def test_rule_tables_keep_d2_unchanged() -> None:
    assert len(RULES) == 84 and set(GLB_RULES) == set(VGLB_RULE_IDS) and len(GLB_RULES) == 16
    assert not set(RULES) & set(GLB_RULES)


def test_published_world_passes_every_applicable_vglb_rule(world: Path) -> None:
    rep = validate_world(world, deep=True)
    assert rep.ok, rep.errors
    rows = {r["id"]: r for r in vglb_results(rep)}
    assert [r for r in rows] == list(VGLB_RULE_IDS)
    applicable = ("V-GLB-01", "V-GLB-02", "V-GLB-03", "V-GLB-04", "V-GLB-05", "V-GLB-07", "V-GLB-08", "V-GLB-09", "V-GLB-10", "V-GLB-11",
                  "V-GLB-12", "V-GLB-13", "V-GLB-14", "V-GLB-16")             # 07、13：quality 与 occupancy 阶段（W10）；05、14：navmesh（W09）
    assert all(rows[r]["status"] in ("pass", "warn") for r in applicable), {r: rows[r] for r in applicable}
    assert rows["V-GLB-04"]["measured"]["cells"] == 10_000 and rows["V-GLB-04"]["measured"]["max_abs_m"] < 1e-3
    for r in ("V-GLB-06",):                                                          # 锚点图层尚未生成
        assert rows[r]["status"] == "n/a" and rows[r]["reason"]
    assert rows["V-GLB-02"]["status"] == "warn"                                      # manual 配准且 landmark：提示须写明量测参照
    assert rows["V-GLB-15"]["status"] == "warn"                                      # 合成世界无调平证据


def test_qa_report_carries_the_glb_segment(world: Path) -> None:
    qa = _j(world / "qa/report.json")
    g = qa["glb"]
    assert g["registration"]["method"] == "manual" and g["registration"]["evidence"]
    assert [r["id"] for r in g["vglb"]] == list(VGLB_RULE_IDS)
    st = {r["id"]: r["status"] for r in g["vglb"]}
    assert st["V-GLB-04"] == "pass" and st["V-GLB-08"] == "pass" and st["V-GLB-02"] == "warn" and st["V-GLB-15"] == "warn"
    assert qa["status"] == "warn" and _j(world / "world.json")["qa"]["status"] == "warn"
    assert {"raycast", "dsm", "appearance"} <= {s["name"] for s in g["stages"]}
    from awr.world.package.schemas import schema_errors

    assert schema_errors(qa, "qa-report.schema.json") == []


def test_bounds_extent_and_point_cloud_take_the_mesh_box(world: Path) -> None:
    w = _j(world / "world.json")
    V, _F = trimesh_world(world / "geometry/mesh/raycast/index.json")
    assert np.allclose(w["bounds"]["min"], V.min(0), atol=1e-3) and np.allclose(w["bounds"]["max"], V.max(0), atol=1e-3)
    md = _j(world / "visual/pointcloud/metadata.json")["anet"]["tightBounds"]
    assert np.allclose(md["min"], w["bounds"]["min"], atol=2e-3) and np.allclose(md["max"], w["bounds"]["max"], atol=2e-3)
    assert w["stats"]["points"] == 60000


def test_dsm_comes_from_the_mesh(world: Path) -> None:
    sc = _j(world / "geometry/terrain/dsm_2m.json")
    dsm = np.fromfile(world / "geometry/terrain/dsm_2m.f32", "<f4").reshape(sc["height"], sc["width"])
    n = np.fromfile(world / "geometry/terrain/dsm_2m_n.u8", "u1").reshape(sc["height"], sc["width"])
    assert "vertical mesh hits" in sc["method"] and (n[:-1, :-1] == 16).all()                # 末行末列为 D1 尺寸规则的外延格
    ox, oy = sc["originXY"]
    r, c = int((-40.0 - oy) // 2), int((40.0 - ox) // 2)                           # 盒子顶面（world (30..50, −50..−30)，z = 30）
    assert abs(float(dsm[r, c]) - 30.0) < 1e-4
    assert abs(float(dsm[2, 2])) < 1e-4                                              # 平地 z = 0


# ---------------------------------------------------------------- 许可与署名（V-GLB-08，602）


def test_dataset_derived_from_and_notice(world: Path) -> None:
    d = _j(world / "world.json")["dataset"]
    df = {e["name"]: e for e in d["derivedFrom"]}
    osm = df["OpenStreetMap"]
    assert osm["license"] == "ODbL-1.0" and osm["attribution"] == "OpenStreetMap contributors" and len(osm["sha256"]) == 64
    assert osm["version"] == "test extract 2026-10-11" and osm["use"] == "registration,anchors,navgraph"
    dem = df["SRTM 1 arc-second"]
    assert dem["license"] == "public-domain" and dem["use"] == "levelling,hMsl" and dem["sha256"] == hashlib.sha256(DEM_BYTES).hexdigest()
    assert d["license"] == "CC-BY-4.0" and d["citation"] == "Test Author (https://example.org/a), CC-BY-4.0"
    assert d["url"].endswith("0123abcd") and "ODbL-1.0" in d["notice"] and d["notice"].startswith("Modified from the original")


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("license"),
    lambda d: d.update(license="unknown"),
    lambda d: d.pop("citation"),
    lambda d: d.update(citation="Test Author"),                                 # CC-BY 署名须写明许可
    lambda d: d.pop("notice"),
    lambda d: d.update(notice="Modified from the original."),                    # 缺 ODbL 声明
    lambda d: d.pop("url"),
    lambda d: d.update(sourceFiles=[]),
    lambda d: d.pop("derivedFrom"),
    lambda d: d["derivedFrom"][0].pop("attribution"),
    lambda d: d["derivedFrom"][0].update(sha256="0" * 12),
    lambda d: d["derivedFrom"][1].update(use=""),
], ids=["license", "license-unknown", "citation", "citation-no-licence", "notice", "notice-no-odbl", "url", "source-files",
        "derived-from", "attribution", "sha256", "use"])
def test_v_glb_08_rejects_each_missing_licence_field_with_602(copy: Path, mutate) -> None:
    w = _j(copy / "world.json")
    mutate(w["dataset"])
    _jw(copy / "world.json", w)
    rep = _run(copy, "V-GLB-08")
    assert "V-GLB-08" in rep.rule_ids("errors") and report_reason(rep) == 602
    assert next(r for r in vglb_results(rep) if r["id"] == "V-GLB-08")["reason_code"] == 602


def test_v_glb_08_requires_osm_and_dem_for_osm_based_registration(copy: Path) -> None:
    c = _j(copy / "coordinate.json")
    c["registration"]["method"] = "inherit-frame"
    _jw(copy / "coordinate.json", c)
    assert not _fails(copy, "V-GLB-08")
    w = _j(copy / "world.json")
    w["dataset"]["derivedFrom"] = [e for e in w["dataset"]["derivedFrom"] if e["name"] != "OpenStreetMap"]
    _jw(copy / "world.json", w)
    assert _fails(copy, "V-GLB-08")
    w["dataset"]["derivedFrom"] = []
    w["dataset"]["notice"] = "Modified from the original."
    _jw(copy / "world.json", w)
    rep = _run(copy, "V-GLB-08")
    assert sum("derivedFrom" in e["message"] for e in rep.errors) == 2               # 缺 OSM 与 DEM 两条


def test_v_glb_08_odbl_layers_need_licence_and_attribution(copy: Path) -> None:
    w = _j(copy / "world.json")
    fc = {"type": "FeatureCollection", "awr": {"schema": "awr.navgraph.v1", "world_id": WID, "frame": "world", "units": "m",
                                               "coordinate_sha256": w["coordinate"]["sha256"], "license": "ODbL-1.0",
                                               "attribution": "OpenStreetMap contributors", "source_sha256": "a" * 64}, "features": []}
    (copy / "geometry/nav").mkdir(parents=True, exist_ok=True)
    _jw(copy / "geometry/nav/graph.geojson", fc)
    w["layers"].append({"id": "nav.graph", "type": "navgraph", "role": "geometry", "format": "geojson/awr-navgraph@1",
                        "href": "geometry/nav/graph.geojson", "status": "ready", "note": "ODbL-1.0, OpenStreetMap contributors"})
    _jw(copy / "world.json", w)
    assert not _fails(copy, "V-GLB-08") and not _fails(copy, "V-GLB-10")
    fc["awr"].pop("attribution")
    _jw(copy / "geometry/nav/graph.geojson", fc)
    assert _fails(copy, "V-GLB-08")
    fc["awr"]["attribution"] = "OpenStreetMap contributors"
    fc["awr"]["coordinate_sha256"] = "b" * 64
    _jw(copy / "geometry/nav/graph.geojson", fc)
    assert _fails(copy, "V-GLB-10")


def test_build_without_licence_fails_with_602(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _write_fixture(tmp_path, monkeypatch, license_="''")
    res = _build(rec, tmp_path / "worlds")
    assert res.exit_code == 1 and res.message.startswith("602 WORLD_LICENSE_MISSING") and "V-GLB-08" in res.message
    assert not (tmp_path / "worlds" / WID / "world.json").exists()


def test_derived_from_checks_input_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awr.world.glb.pipeline import derived_from
    from awr.world.ingest.types import RawDataError

    rec = _write_fixture(tmp_path, monkeypatch)
    assert [e["name"] for e in derived_from(rec.doc)] == ["OpenStreetMap", "SRTM 1 arc-second"]
    (tmp_path / "raw" / "osm" / "test.osm").write_bytes(OSM_XML + b"\n")
    with pytest.raises(RawDataError):
        derived_from(rec.doc)


def test_derived_from_registers_qa_extras_once_with_contract_uses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from awr.world.glb.pipeline import derived_from

    rec = _write_fixture(tmp_path, monkeypatch)
    doc = dict(rec.doc)
    osm = dict(doc["osm"])
    doc["qa"] = {"osm_extra": [{**osm, "use": "levelling planes"}]}                  # 同一文件：只登记一次
    assert len(derived_from(doc)) == 2
    water = tmp_path / "raw" / "osm" / "water.osm"
    water.write_bytes(OSM_XML.replace(b"test", b"water"))
    man = _j(tmp_path / "raw-manifest.json")
    man["files"].append({"name": "osm/water.osm", "bytes": water.stat().st_size, "sha256": _sha(water)})
    _jw(tmp_path / "raw-manifest.json", man)
    doc["qa"] = {"osm_extra": [{"path": "osm/water.osm", "sha256": _sha(water), "use": "levelling planes"}]}
    df = derived_from(doc)
    assert [e["use"] for e in df] == ["registration,anchors,navgraph", "levelling", "levelling,hMsl"]
    assert df[1]["license"] == "ODbL-1.0" and df[1]["version"].startswith("water.osm")


def test_committed_recipes_declare_osm_and_dem() -> None:
    from awr.world.glb.recipe import RAW_DIR, load_recipe

    for wid in ("oryahovo-crop1", "smalltown-01"):
        doc = load_recipe(wid).doc
        assert doc["osm"]["path"] and len(doc["osm"]["sha256"]) == 64 and doc["osm"]["extract"]
        assert doc["dem"]["model"] == "srtm30m" and len(doc["dem"]["sha256"]) == 64
        if (RAW_DIR / doc["osm"]["path"]).exists() and (RAW_DIR / doc["dem"]["path"]).exists():
            from awr.world.glb.pipeline import derived_from

            df = derived_from(doc)
            assert {e["license"] for e in df} == {"ODbL-1.0", "public-domain"}


# ---------------------------------------------------------------- 世界清单（world_manifest.json）


def test_world_manifest_licence_and_attribution(world: Path) -> None:
    m = world_manifest(world)
    assert check_world_manifest(m) == []
    assert m["world_id"] == WID and m["contentVersion"] == _j(world / "world.json")["contentVersion"]
    assert m["coordinate_sha256"] == _sha(world / "coordinate.json") and m["frame"]["unitsToMeters"] == 1.0
    rc = next(L for L in m["layers"] if L["id"] == "mesh.raycast")
    assert {f["path"] for f in rc["files"]} >= {"geometry/mesh/raycast/index.json", "geometry/mesh/raycast/xyz.f32"}
    en, zh = m["license"]["attribution"]["en"], m["license"]["attribution"]["zh"]
    for t in (en, zh):
        assert "Test Author (https://example.org/a)" in t and "CC-BY-4.0" in t and "ODbL-1.0" in t and "OpenStreetMap contributors" in t
        assert "NASA/USGS SRTM" in t and "https://sketchfab.com/3d-models/0123abcd" in t
    assert "modified from the original asset" in en and "经过修改" in zh
    assert "build" in m and m["public"] is False


def test_public_world_manifest_has_no_build_or_internal_paths(world: Path) -> None:
    m = world_manifest(world, public=True)
    assert "build" not in m and m["public"] is True and check_world_manifest(m) == []
    assert str(world) not in json.dumps(m) and "/data/" not in json.dumps(m)


@pytest.mark.parametrize("mutate", [
    lambda m: m["license"].pop("citation"),
    lambda m: m["license"].update(derivedFrom=[]) or m["frame"].update(registration="inherit-frame"),
    lambda m: m["license"]["attribution"].update(zh=""),
    lambda m: m["license"].update(notice="Modified."),
    lambda m: m["license"]["derivedFrom"][0].update(sha256=""),
])
def test_check_world_manifest_reports_gaps(world: Path, mutate) -> None:
    m = world_manifest(world)
    mutate(m)
    assert check_world_manifest(m)


def test_notice_text_is_enterprise_copy(world: Path) -> None:
    w = _j(world / "world.json")
    for lang in ("en", "zh"):
        t = notice_text(w, lang)
        assert "!" not in t and "！" not in t and len(t.splitlines()) == 5


# ---------------------------------------------------------------- 逐条变异


def test_v_glb_01_scale_consistency(copy: Path) -> None:
    c = _j(copy / "coordinate.json")
    c["source"]["unitsToMeters"] = 1.001
    _jw(copy / "coordinate.json", c)
    assert _fails(copy, "V-GLB-01")


def test_v_glb_02_scale_evidence_and_method(copy: Path) -> None:
    c = _j(copy / "coordinate.json")
    c["source"]["evidence"] = []
    _jw(copy / "coordinate.json", c)
    assert _fails(copy, "V-GLB-02")
    c["source"]["evidence"] = ["x"]
    c["scaleStatus"] = "assumed"
    c["registration"]["method"] = "osm-road-correlation"
    _jw(copy / "coordinate.json", c)
    assert _fails(copy, "V-GLB-02")
    c["registration"]["method"] = "manual"
    _jw(copy / "coordinate.json", c)
    assert not _fails(copy, "V-GLB-02")
    qa = _j(copy / "qa/report.json")
    qa["glb"].pop("registration")
    _jw(copy / "qa/report.json", qa)
    assert _fails(copy, "V-GLB-02") and not _fails(copy, "V-GLB-02", staging=True)


def _patch_xyz(d: Path, k: int, delta: tuple[float, float, float]) -> None:
    p = d / "geometry/mesh/raycast/xyz.f32"
    a = np.fromfile(p, "<f4").reshape(-1, 3)
    a[k] += np.asarray(delta, np.float32)
    a.tofile(p)


def test_v_glb_03_vertices_inside_bounds(copy: Path) -> None:
    assert not _fails(copy, "V-GLB-03")
    _patch_xyz(copy, 0, (0.0, 0.0, 500.0))
    assert _fails(copy, "V-GLB-03")


def test_v_glb_04_dsm_matches_vertical_recompute(copy: Path) -> None:
    p = copy / "geometry/terrain/dsm_2m.f32"
    a = np.fromfile(p, "<f4")
    (a + np.float32(0.011)).astype("<f4").tofile(p)
    assert _fails(copy, "V-GLB-04")
    a.astype("<f4").tofile(p)
    assert not _fails(copy, "V-GLB-04")
    q = copy / "geometry/terrain/dsm_2m_n.u8"
    n = np.fromfile(q, "u1")
    n[n.size // 2] = 3
    n.tofile(q)
    assert _fails(copy, "V-GLB-04")


def _add_layer(d: Path, layer: dict, rel: str, doc: dict | bytes) -> None:
    p = d / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(doc, bytes):
        p.write_bytes(doc)
    else:
        _jw(p, doc)
    if layer:
        w = _j(d / "world.json")
        w["layers"].append(layer)
        _jw(d / "world.json", w)


def _navmesh(d: Path, **over) -> dict:
    w = _j(d / "world.json")
    w["layers"] = [x for x in w["layers"] if x.get("type") != "navmesh"]      # 以本夹具的清单替换构建生成的 NavMesh 图层（W09）
    _jw(d / "world.json", w)
    _add_layer(d, {}, "geometry/nav/person.bin", b"\x01" * 64)
    doc = {"schema": "awr.navmesh.v1", "profile": "person", "agent": {"radius_m": 0.3, "height_m": 1.8, "max_climb_m": 0.4, "max_slope_deg": 35.0},
           "cs_m": 0.3, "ch_m": 0.2, "tile_size_cells": 128, "axes": "recast-y-up-enu", "recast_version": "recast-navigation-js 0.39.0",
           "input": "raycast", "areas": [], "flags": {"walk": 1}, "seeds": ["n-1"],
           "islands": {"kept": 1, "dropped": 0, "walkable_area_m2": 8.0e4}, "offmesh": [],
           "bin": {"href": "person.bin", "bytes": 64, "sha256": "c" * 64}, "coordinate_sha256": w["coordinate"]["sha256"]}
    doc.update(over)
    _add_layer(d, {"id": "nav.person", "type": "navmesh", "role": "geometry", "format": "detour-navmesh@1",
                   "href": "geometry/nav/person.json", "status": "ready", "axes": "recast-y-up-enu"}, "geometry/nav/person.json", doc)
    return doc


def test_v_glb_05_navmesh_seed_island(copy: Path) -> None:
    doc = _navmesh(copy)
    assert not _fails(copy, "V-GLB-05")
    doc["islands"] = {"kept": 0, "dropped": 3, "walkable_area_m2": 0.0}
    _jw(copy / "geometry/nav/person.json", doc)
    assert _fails(copy, "V-GLB-05")


def test_v_glb_06_anchor_snap_warnings(copy: Path) -> None:
    w = _j(copy / "world.json")
    anchors = [{"id": f"j-{i}", "kind": "point", "category": "junction", "profile": "person", "xy": [0.0, 0.0], "z": 0.0,
                "snap_dist_m": {"person": 0.5, "vehicle": None}, "status": "auto"} for i in range(20)]
    doc = {"schema": "awr.anchors.v1", "schema_version": "1.0.0", "world_id": WID, "frame": "world", "units": "m",
           "coordinate_sha256": w["coordinate"]["sha256"], "license": "ODbL-1.0", "attribution": "OpenStreetMap contributors",
           "sources": [], "anchors": anchors}
    _add_layer(copy, {"id": "semantic.anchors", "type": "anchors", "role": "semantic", "format": "awr-anchors@1",
                      "href": "semantic/anchors.json", "status": "ready", "default": True, "note": "ODbL-1.0, OpenStreetMap contributors"},
               "semantic/anchors.json", doc)
    rep = _run(copy, "V-GLB-06")
    assert rep.warnings == [] and rep.errors == []
    anchors[0]["snap_dist_m"]["person"] = 4.2
    _jw(copy / "semantic/anchors.json", doc)
    rep = _run(copy, "V-GLB-06")
    assert len(rep.warnings) == 1 and rep.errors == []                               # 1/20 ≤ 5%：只有逐条告警
    anchors[1]["snap_dist_m"]["person"] = None
    _jw(copy / "semantic/anchors.json", doc)
    assert len(_run(copy, "V-GLB-06").warnings) == 3                                 # 2/20 > 5%：追加汇总告警
    assert not _fails(copy, "V-GLB-08")


def _quality(d: Path, ring: list[list[float]]) -> None:
    w = _j(d / "world.json")
    fc = {"type": "FeatureCollection", "awr": {"schema": "awr.quality.v1", "world_id": WID, "frame": "world", "units": "m",
                                               "coordinate_sha256": w["coordinate"]["sha256"], "cell_m": 2.0, "raster_href": None},
          "features": [{"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                        "properties": {"kind": "scan-footprint", "reasons_bits": 0, "area_m2": 0.0}}]}
    if (d / "semantic/quality.geojson").exists():
        _jw(d / "semantic/quality.geojson", fc)
        return
    _add_layer(d, {"id": "semantic.quality", "type": "quality", "role": "semantic", "format": "geojson/awr-quality@1",
                   "href": "semantic/quality.geojson", "status": "ready"}, "semantic/quality.geojson", fc)


def test_v_glb_07_coverage_polygon_iou(copy: Path) -> None:
    b = _j(copy / "world.json")["bounds"]
    (x0, y0), (x1, y1) = b["min"][:2], b["max"][:2]
    _quality(copy, [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]])
    rep = _run(copy, "V-GLB-07")
    assert rep.ok and rep.info["vglb"]["V-GLB-07"]["iou"] >= 0.98
    _quality(copy, [[x0, y0], [0.0, y0], [0.0, y1], [x0, y1], [x0, y0]])
    assert _fails(copy, "V-GLB-07")


def test_coverage_mask_closes_gaps_and_fills_holes() -> None:
    n = np.zeros((40, 40), np.uint8)
    n[5:35, 5:35] = 16
    n[10:12, 10:30] = 0                                                              # 两格宽缝：闭运算补上
    n[20:26, 20:26] = 0                                                              # 内部洞：填补
    n[0, 39] = 16                                                                    # 孤立格：最大连通分量之外
    m = coverage_mask(n)
    assert m[11, 15] and m[22, 22] and not m[0, 39] and m.sum() == 30 * 30


def test_v_glb_09_trimesh_integrity(copy: Path) -> None:
    rc = copy / "geometry/mesh/raycast"
    t = np.fromfile(rc / "tri.u32", "<u4")
    t[:-3].tofile(rc / "tri.u32")
    assert _fails(copy, "V-GLB-09")
    t2 = t.copy()
    t2[0] = 10 ** 6
    t2.tofile(rc / "tri.u32")
    assert _fails(copy, "V-GLB-09")
    t.tofile(rc / "tri.u32")
    idx = _j(rc / "index.json")
    idx["counts"]["vertices"] += 1
    _jw(rc / "index.json", idx)
    assert _fails(copy, "V-GLB-09")


def test_v_glb_10_sidecar_binding(copy: Path) -> None:
    p = copy / "visual/mesh/appearance/index.json"
    idx = _j(p)
    idx["coordinate_sha256"] = "0" * 64
    _jw(p, idx)
    assert _fails(copy, "V-GLB-10")


def _rewrite_glb_json(p: Path, edit) -> None:
    data = p.read_bytes()
    jl = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + jl])
    edit(js)
    jb = json.dumps(js, separators=(",", ":")).encode()
    jb += b" " * (-len(jb) % 4)
    rest = data[20 + jl:]
    body = struct.pack("<I4s", len(jb), b"JSON") + jb + rest
    p.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body)


def test_v_glb_11_appearance_materials_counts_and_images(copy: Path) -> None:
    ad = copy / "visual/mesh/appearance"
    idx = _j(ad / "index.json")
    idx["textures"][0]["sha256"] = "1" * 64
    _jw(ad / "index.json", idx)
    assert _fails(copy, "V-GLB-11")
    _rewrite_glb_json(ad / "appearance.glb", lambda js: js["materials"][0].update(doubleSided=False))
    rep = _run(copy, "V-GLB-11")
    assert any("double-sided" in e["message"] for e in rep.errors)


def test_v_glb_12_border_zone(copy: Path) -> None:
    z = _j(copy / "semantic/zones.geojson")
    b = next(f for f in z["features"] if f["properties"]["kind"] == "border")
    b["properties"]["max_z_m"] += 1.0
    _jw(copy / "semantic/zones.geojson", z)
    assert _fails(copy, "V-GLB-12")
    b["properties"]["max_z_m"] -= 1.0
    _jw(copy / "semantic/zones.geojson", z)
    assert not _fails(copy, "V-GLB-12")
    _quality(copy, [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0], [0.0, 0.0]])
    assert _fails(copy, "V-GLB-12")                                                 # 边界顶点落在覆盖多边形外


def _occupancy(d: Path, *, corrupt: bool = False) -> None:
    """按 `awr-occ@1`（M22 §7.3.2，键为 Morton 交织、砖块内 (z, y, x) 行主序）写一份与网格一致的占据层：顶点所在体素 occupied，
    其余显式砖块 free。"""
    import zstandard

    from awr.world.package.validate import OCC_IDX_DTYPE, _morton3

    V, F = trimesh_world(d / "geometry/mesh/raycast/index.json")
    v, B = 2.0, 16
    b = _j(d / "world.json")["bounds"]
    o = np.floor(np.asarray(b["min"]) / v) * v
    dims = np.ceil((np.asarray(b["max"]) - o) / v).astype(np.int64) + 1
    nb = -(-dims // B)
    vox = np.unique(np.floor((V[np.unique(F)] - o) / v).astype(np.int64), axis=0)
    grid = np.full(tuple(nb * B), 1, np.uint8)
    grid[vox[:, 0], vox[:, 1], vox[:, 2]] = 0 if corrupt else 2
    top = np.full((nb[1], nb[0]), nb[2] - 1, np.uint16)
    cctx = zstandard.ZstdCompressor(level=3)
    rows, blob = [], bytearray()
    keys = sorted((_morton3(bx, by, bz), bx, by, bz) for bx in range(nb[0]) for by in range(nb[1]) for bz in range(nb[2]))
    for key, bx, by, bz in keys:
        blk = grid[bx * B:(bx + 1) * B, by * B:(by + 1) * B, bz * B:(bz + 1) * B].transpose(2, 1, 0).copy()   # (z, y, x)
        raw = cctx.compress(blk.tobytes())
        rows.append((key, len(blob), len(raw), 0))
        blob += raw
    w = _j(d / "world.json")                                                         # 替换构建写出的占据层
    w["layers"] = [L for L in w["layers"] if L["id"] != "occupancy.voxels"]
    _jw(d / "world.json", w)
    od = d / "geometry/voxel/occ_2m"
    od.mkdir(parents=True, exist_ok=True)
    top.astype("<u2").tofile(od / "top.u16")
    np.array(rows, OCC_IDX_DTYPE).tofile(od / "bricks.idx")
    (od / "bricks.bin").write_bytes(bytes(blob))
    _add_layer(d, {"id": "occupancy.voxels", "type": "voxel", "role": "geometry", "format": "awr-occ@1", "href": "geometry/voxel/occ_2m/",
                   "status": "ready"}, "geometry/voxel/occ_2m/index.json",
               {"schema": "awr.occ.v1", "kind": "occupancy", "voxel_m": v, "brick": B, "origin_m": o.tolist(), "dims": dims.tolist(),
                "states": {"0": "unknown", "1": "free", "2": "occupied", "3": "below_surface"},
                "top": {"href": "top.u16", "width": int(nb[0]), "height": int(nb[1])},
                "bricks": {"index": "bricks.idx", "data": "bricks.bin", "codec": "zstd"}, "trunc_m": 20.0, "implicit": "free-in-cover"})


def test_v_glb_13_occupancy_surface_and_unknown(copy: Path) -> None:
    pytest.importorskip("zstandard")
    _occupancy(copy)
    rep = _run(copy, "V-GLB-13")
    assert rep.ok, rep.errors
    shutil.rmtree(copy / "geometry/voxel")
    w = _j(copy / "world.json")
    w["layers"] = [L for L in w["layers"] if L["id"] != "occupancy.voxels"]
    _jw(copy / "world.json", w)
    _occupancy(copy, corrupt=True)
    assert _fails(copy, "V-GLB-13")


def test_v_glb_14_recast_version_and_agent(copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import awr.world.ingest.manifest as im

    root = tmp_path / "root"
    (root / "tools/worldglb").mkdir(parents=True)
    (root / "tools/worldglb/RECAST_VERSION").write_text("recast-navigation-js 0.39.0\n")
    monkeypatch.setattr(im, "repo_root", lambda: root)
    doc = _navmesh(copy)
    assert not _fails(copy, "V-GLB-14")
    doc["agent"]["radius_m"] = 0.4
    _jw(copy / "geometry/nav/person.json", doc)
    assert _fails(copy, "V-GLB-14")
    doc["agent"]["radius_m"] = 0.3
    doc["recast_version"] = "recast-navigation-js 0.38.0"
    _jw(copy / "geometry/nav/person.json", doc)
    assert _fails(copy, "V-GLB-14")


def test_v_glb_15_levelling_evidence(copy: Path) -> None:
    qa = _j(copy / "qa/report.json")
    qa["glb"]["leveling"] = {"km_scale": False, "subset_delta_deg": 0.12, "ci95_deg": 0.2,
                             "planes": [{"slope_deg": 0.1}, {"slope_deg": 0.3}, {"slope_deg": 0.4}], "needs_control_points": False}
    _jw(copy / "qa/report.json", qa)
    assert _run(copy, "V-GLB-15").warnings == []
    qa["glb"]["leveling"].update(km_scale=True, delta_deg=0.41)                       # 公里级：两法差 ≤ 0.5°，但水平面坡度 ≤ 0.2° 只有 1 处
    _jw(copy / "qa/report.json", qa)
    assert len(_run(copy, "V-GLB-15").warnings) == 1
    qa["glb"]["leveling"].update(delta_deg=0.8, control_points={"n": 24, "rms_m": 1.4}, needs_control_points=True)
    _jw(copy / "qa/report.json", qa)
    msgs = [x["message"] for x in _run(copy, "V-GLB-15").warnings]
    assert len(msgs) == 2 and any("0.8" in m for m in msgs) and any("needs_control_points" in m for m in msgs)


def test_v_glb_16_colour_mode_and_profile(copy: Path) -> None:
    w = _j(copy / "world.json")
    w["render"]["defaultColorMode"] = "height"
    _jw(copy / "world.json", w)
    assert _fails(copy, "V-GLB-16")


def test_d2_world_skips_the_group(copy: Path) -> None:
    w = _j(copy / "world.json")
    w.pop("profile")
    _jw(copy / "world.json", w)
    rep = validate_world(copy, deep=True)
    assert not {r for r in rep.rule_ids("errors") | rep.rule_ids("warnings") if r.startswith("V-GLB")}
    assert rep.rules_checked == 84
