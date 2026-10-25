"""quality 层、覆盖多边形与占据体素 unknown（M22 §6.6.7、§6.6.10、§7.3.2、§7.3.6；M22-FR-027、FR-031；
D3-AC-04 "quality 层与覆盖多边形存在；occupancy 中覆盖外体素为 unknown（抽样 100%）"）。

夹具为带洞的合成 GLB 世界：300 m × 300 m 平地（10 m 网格）去掉东南角 100 m × 100 m（覆盖外区域），内部挖去 20 m × 20 m 的洞
（开放边界环），东侧放一个 20 × 20 × 30 m 的封闭盒子（多层柱、盒内为表面之下），配方带一处 OSM 水面（ODbL）。
构建走完整 GlbPipeline（与 crop 同一代码），再以独立的解码与 V-GLB 规则核对：覆盖外体素逐个解码，100% 为 unknown。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PIL")
pytest.importorskip("zstandard")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "world"))

from awr.world.glb.derive.occupancy import (
    BELOW,
    FREE,
    OCCUPIED,
    UNKNOWN,
    dense_states,
    morton3,
    read_states,
    surface_voxels,
)
from awr.world.glb.derive.quality import (
    BIT_HOLE,
    BIT_LOW_TEXTURE,
    BIT_MULTILAYER,
    BIT_NODATA,
    BIT_WATER,
    border_ring,
    boundary_loops,
    mask_rings,
    raster_href,
    sample_top,
    simplify_ring,
)
from awr.world.glb.raster import HAVE_NUMBA, trimesh_world
from awr.world.package.streams import add_stream, read_stream, stream_entry
from awr.world.package.validate import _morton3, coverage_mask, rasterize_rings, validate_world, vglb_results

WID = "synth-quality"
SIZE = 300.0
STEP = 10.0
HOLE = (-60.0, -40.0)          # glTF x 与 z 区间（世界 E ∈ [-60, -40]、N ∈ [40, 60]）
CUT = 50.0                     # 去掉 x ≥ 50 且 z ≥ 50 的网格（世界 E ≥ 50、N ≤ −50）
WATER_EN = ((-120.0, -120.0), (-80.0, -80.0))     # 世界帧水面矩形（西南角、东北角）


# ---------------------------------------------------------------- 夹具


def _scene() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """glTF Y-up：带缺角与内洞的平地加一个外法线封闭盒子（顶、四壁、底）。"""
    n = int(SIZE / STEP) + 1
    xs = np.linspace(-SIZE / 2, SIZE / 2, n)
    x, z = np.meshgrid(xs, xs)
    V = np.stack([x.ravel(), np.zeros(x.size), z.ravel()], 1)
    UV = np.stack([(x.ravel() + SIZE / 2) / SIZE, (z.ravel() + SIZE / 2) / SIZE], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            x0, z0 = xs[j], xs[i]
            if x0 >= CUT and z0 >= CUT:
                continue
            if HOLE[0] <= x0 < HOLE[1] and HOLE[0] <= z0 < HOLE[1]:
                continue
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, c, b], [a, d, c]]
    bx = np.array([[x0 + 40.0, y0, z0] for y0 in (0.0, 30.0) for z0 in (-10.0, 10.0) for x0 in (-10.0, 10.0)])
    quads = [(4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5), (0, 2, 3, 1)]
    base = len(V)
    c0 = bx.mean(0)
    for q in quads:
        for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3])):
            p = bx[list(t)]
            nrm = np.cross(p[1] - p[0], p[2] - p[0])
            F.append([base + k for k in (t if nrm @ (p.mean(0) - c0) > 0 else t[::-1])])
    V = np.vstack([V, bx]).astype(np.float32)
    UV = np.vstack([UV, np.full((8, 2), 0.5)]).astype(np.float32)
    return V, np.asarray(F, np.uint32), UV


def _osm(anchor, T: np.ndarray) -> bytes:
    from awr.world.georef.frames import world_to_lla

    (e0, n0), (e1, n1) = WATER_EN
    P = np.array([[e0, n0, 0.0], [e1, n0, 0.0], [e1, n1, 0.0], [e0, n1, 0.0]])
    lat, lon, _h = world_to_lla(P, anchor)
    nodes = "".join(f' <node id="{i + 1}" lat="{la:.9f}" lon="{lo:.9f}"/>\n' for i, (la, lo) in enumerate(zip(lat, lon, strict=True)))
    return (b'<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="test" copyright="OpenStreetMap and contributors">\n'
            + nodes.encode() + b' <way id="10">\n  <nd ref="1"/>\n  <nd ref="2"/>\n  <nd ref="3"/>\n  <nd ref="4"/>\n  <nd ref="1"/>\n'
            b'  <tag k="natural" v="water"/>\n </way>\n</osm>\n')


def _write_fixture(tmp: Path, mp: pytest.MonkeyPatch, *, water: bool = True):
    import test_glb_build as g
    from glbfix import png, write_glb

    from awr.world.georef.types import Anchor
    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration

    raw = tmp / "raw"
    for d in ("synth", "osm"):
        (raw / d).mkdir(parents=True, exist_ok=True)
    V, F, UV = _scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=V, F=F, UV=UV, tex=png(8, 8, (90, 140, 60)))
    man = tmp / "raw-manifest.json"
    files = [{"name": "synth/synth.glb", "bytes": glb.stat().st_size, "sha256": file_sha256(glb)}]
    man.write_text(json.dumps({"files": files}))
    mp.setattr(g.recipe_mod, "RAW_DIR", raw)
    mp.setattr(g.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp / "recipes" / WID
    rd.mkdir(parents=True, exist_ok=True)
    head = (f"schema: awr.glb.recipe.v1\nworld_id: {WID}\nname: Synthetic quality test world\nname_zh: 合成质量层世界\nrole: event-main\n"
            f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(glb)}}}\n"
            "  sketchfab: {uid: 0123abcd, author: 'Test Author (https://example.org/a)', license: CC-BY-4.0}\n")
    tail = "registration: registration.json\nbuild:\n  tiling_m: 128\n  quality_cell_m: 2.0\n  voxel_m: 1.0\n  pointcloud: {target_points: 60000, seed: 7}\n"
    (rd / "recipe.yaml").write_text(head + tail)
    rec = load_recipe(WID, tmp / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture: unit scale by construction"])
    write_registration(rec, doc, accept_by="test")
    if water:
        a = doc["result"]["anchor"]
        anchor = Anchor(kind="synthetic", datum="WGS84", lat_deg=float(a["lat_deg"]), lon_deg=float(a["lon_deg"]), h_ellipsoid_m=0.0,
                        h_msl_m=float(a["h_msl_m"]))
        osm = raw / "osm" / "water.osm"
        osm.write_bytes(_osm(anchor, np.asarray(doc["result"]["T_world_source"])))
        files.append({"name": "osm/water.osm", "bytes": osm.stat().st_size, "sha256": file_sha256(osm)})
        man.write_text(json.dumps({"files": files}))
        (rd / "recipe.yaml").write_text(head + f"osm: {{path: osm/water.osm, sha256: {file_sha256(osm)}, extract: 'test water 2026-10-18'}}\n"
                                        + tail)
        rec = load_recipe(WID, tmp / "recipes")
    return rec, np.asarray(doc["result"]["T_world_source"], np.float64)


def _build(rec, worlds: Path):
    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world

    return build_world(GlbAdapter(WID, recipe=rec), worlds, ctx=StageContext(world_id=WID))


def _built(tmp: Path, *, water: bool = True) -> tuple[Path, np.ndarray]:
    mp = pytest.MonkeyPatch()
    try:
        rec, T = _write_fixture(tmp, mp, water=water)
        res = _build(rec, tmp / "worlds")
        assert res.exit_code == 0, res.message
    finally:
        mp.undo()
    return tmp / "worlds" / WID, T


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, np.ndarray]:
    return _built(tmp_path_factory.mktemp("quality"))


def _j(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def _to_world(T: np.ndarray, x: float, z: float, y: float = 0.0) -> np.ndarray:
    return (T @ np.array([x, y, z, 1.0]))[:3]


def _grid(d: Path, rel: str) -> tuple[dict, np.ndarray]:
    sc = _j(d / rel)
    dt = {"uint8": np.uint8, "float32": "<f4"}[sc["dtype"]]
    return sc, np.fromfile((d / rel).parent / sc["href"], dt).reshape(sc["height"], sc["width"])


def _cell(sc: dict, E: float, N: float) -> tuple[int, int]:
    return int((N - sc["originXY"][1]) // sc["cellM"]), int((E - sc["originXY"][0]) // sc["cellM"])


def _rings(geom: dict) -> list[np.ndarray]:
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [np.asarray(r, np.float64)[:, :2] for p in polys for r in p]


# ---------------------------------------------------------------- 图层存在与绑定


def test_quality_layers_exist_and_bind_to_the_world(built) -> None:
    d, _T = built
    w = _j(d / "world.json")
    L = {x["id"]: x for x in w["layers"]}
    assert L["quality.raster"]["format"] == "f32-grid@1" and L["quality.raster"]["href"] == raster_href(2.0) == "geometry/terrain/quality_2m.json"
    assert L["semantic.quality"]["format"] == "geojson/awr-quality@1" and L["semantic.quality"]["type"] == "quality"
    assert L["occupancy.voxels"]["format"] == "awr-occ@1" and L["occupancy.voxels"]["href"] == "geometry/voxel/occ_1m/"
    ids = [x["id"] for x in w["layers"]]
    assert ids.index("quality.raster") < ids.index("mesh.raycast") < ids.index("occupancy.voxels") < ids.index("semantic.quality")
    fc = _j(d / "semantic/quality.geojson")
    sha = w["coordinate"]["sha256"]
    assert fc["awr"]["schema"] == "awr.quality.v1" and fc["awr"]["coordinate_sha256"] == sha and fc["awr"]["world_id"] == WID
    assert fc["awr"]["raster_href"] == "geometry/terrain/quality_2m.json" and fc["awr"]["cell_m"] == 2.0
    sc, _Q = _grid(d, "geometry/terrain/quality_2m.json")
    _dsc, dsm = _grid(d, "geometry/terrain/dsm_2m.json")
    assert sc["kind"] == "quality" and sc["dtype"] == "uint8" and sc["valueFrame"] == "unitless" and sc["scale"] == 1.0
    assert sc["originXY"] == _dsc["originXY"] and (sc["height"], sc["width"]) == dsm.shape
    assert _j(d / "geometry/voxel/occ_1m/index.json")["coordinate_sha256"] == sha
    assert 0.0 < w["sensorHints"]["multiLayerColumnFrac"] < 0.05                  # 盒子：约 400 m² / 80,000 m²


def test_footprint_polygon_matches_the_coverage(built) -> None:
    d, T = built
    fc = _j(d / "semantic/quality.geojson")
    fp = [f for f in fc["features"] if f["properties"]["kind"] == "scan-footprint"]
    assert len(fp) == 1 and fp[0]["geometry"]["type"] == "Polygon"
    ring = np.asarray(fp[0]["geometry"]["coordinates"][0], np.float64)
    assert len(ring) - 1 <= 1024 and np.allclose(ring[0], ring[-1])
    nsc, n = _grid(d, "geometry/terrain/dsm_2m_n.json")
    cov = coverage_mask(n)
    poly = rasterize_rings([ring], tuple(nsc["originXY"]), nsc["cellM"], *n.shape)
    iou = (poly & cov).sum() / (poly | cov).sum()
    assert iou >= 0.98, iou
    area = fp[0]["properties"]["area_m2"]
    assert abs(area - (SIZE * SIZE - 100.0 * 100.0)) / (SIZE * SIZE - 100.0 * 100.0) < 0.01, area   # 缺角之外的平地（洞已填补）
    from awr.world.package.validate import _points_in_rings

    hole_c = _to_world(T, -50.0, -50.0)[:2]
    cut_c = _to_world(T, 100.0, 100.0)[:2]
    assert _points_in_rings(np.array([hole_c]), [ring])[0]                        # 洞在覆盖内（fill_holes）
    assert not _points_in_rings(np.array([cut_c]), [ring])[0]                     # 缺角在覆盖外


def test_hole_nodata_multilayer_and_water_bits(built) -> None:
    d, T = built
    sc, Q = _grid(d, "geometry/terrain/quality_2m.json")
    at = lambda x, z: int(Q[_cell(sc, *_to_world(T, x, z)[:2])])                 # noqa: E731
    assert at(-50.0, -50.0) & BIT_HOLE and at(-50.0, -50.0) & BIT_NODATA         # 洞：开放边界环内部且无竖向命中
    assert at(100.0, 100.0) == BIT_NODATA                                        # 覆盖外
    assert at(-5.0, 75.0) == 0                                                    # 有效地面
    assert at(40.0, 0.0) & BIT_MULTILAYER                                         # 盒顶 30 m 与盒底相隔 ≥ 2 m
    E, N = (np.mean(WATER_EN, 0))
    assert int(Q[_cell(sc, E, N)]) & BIT_WATER
    fc = _j(d / "semantic/quality.geojson")
    kinds = {f["properties"]["kind"]: f for f in fc["features"]}
    assert {"scan-footprint", "nodata", "hole", "multilayer", "water", "low-texture"} <= set(kinds)
    for f in fc["features"]:
        p = f["properties"]
        assert set(p) == {"kind", "reasons_bits", "area_m2"} and p["area_m2"] > 0
    hole = kinds["hole"]
    assert hole["properties"]["reasons_bits"] == BIT_HOLE and abs(hole["properties"]["area_m2"] - 400.0) <= 80.0
    from awr.world.package.validate import _points_in_rings

    assert _points_in_rings(np.array([_to_world(T, -50.0, -50.0)[:2]]), _rings(hole["geometry"]))[0]
    # 栅格与多边形一致：各位的格数 × 格面积 = 多边形 area_m2
    for name, bit in (("nodata", BIT_NODATA), ("hole", BIT_HOLE), ("water", BIT_WATER)):
        assert kinds[name]["properties"]["area_m2"] == pytest.approx(float(((Q & bit) > 0).sum()) * 4.0)


def test_water_polygons_are_odbl_and_licence_rule_passes(built) -> None:
    d, _T = built
    fc = _j(d / "semantic/quality.geojson")
    assert fc["awr"]["license"] == "ODbL-1.0" and fc["awr"]["attribution"] == "OpenStreetMap contributors"
    L = {x["id"]: x for x in _j(d / "world.json")["layers"]}
    assert "ODbL" in L["semantic.quality"]["note"]
    rows = {r["id"]: r for r in vglb_results(validate_world(d, deep=True, rules={"V-GLB-08"}))}
    assert rows["V-GLB-08"]["status"] == "pass"


def test_world_without_osm_has_no_odbl_quality_header(tmp_path: Path) -> None:
    d, _T = _built(tmp_path, water=False)
    fc = _j(d / "semantic/quality.geojson")
    assert "license" not in fc["awr"] and "water" not in {f["properties"]["kind"] for f in fc["features"]}
    assert "note" not in {x["id"]: x for x in _j(d / "world.json")["layers"]}["semantic.quality"]


def test_triangle_quality_streams(built) -> None:
    d, _T = built
    rc = d / "geometry/mesh/raycast/index.json"
    ap = d / "visual/mesh/appearance/index.json"
    assert stream_entry(rc, "q") == {"name": "q", "href": "q.u8", "dtype": "uint8", "components": 1}
    assert stream_entry(ap, "q") == {"name": "q", "href": "tri.q.u8", "dtype": "uint8", "components": 1}
    q = read_stream(rc, "q")
    src = read_stream(rc, "src").astype(np.int64)
    qa = read_stream(ap, "q")
    assert len(q) == _j(rc)["counts"]["triangles"] == len(qa) == _j(ap)["triangles"]
    assert np.array_equal(qa[src], q)                                             # 外观三角形序与求交网格序一一对应
    V, F = trimesh_world(rc)
    C = V[F].mean(1)
    box = (np.abs(C[:, 0] - 40.0) < 10.5) & (np.abs(C[:, 1]) < 10.5) & (C[:, 2] > 0.5)
    assert box.sum() >= 8 and np.all(q[box] & BIT_LOW_TEXTURE)                     # 盒子 UV 退化：无纹素
    ground = (C[:, 2] == 0.0) & (np.abs(C[:, 0] + 100.0) < 30.0) & (np.abs(C[:, 1]) < 30.0)
    assert ground.sum() > 10 and not np.any(q[ground])
    wet = (C[:, 2] == 0.0) & (C[:, 0] > -118) & (C[:, 0] < -82) & (C[:, 1] > -118) & (C[:, 1] < -82)
    assert wet.sum() > 0 and np.all(q[wet] & BIT_WATER)


# ---------------------------------------------------------------- 占据体素


def test_occupancy_outside_coverage_is_unknown_for_every_voxel(built) -> None:
    """D3-AC-04：覆盖外体素为 unknown（抽样 100%）—— 夹具上逐个解码覆盖外柱的全部体素。"""
    d, T = built
    ix = d / "geometry/voxel/occ_1m/index.json"
    doc = _j(ix)
    G = dense_states(ix)
    nz, ny, nx = G.shape
    assert [nx, ny, nz] == doc["dims"]
    v = doc["voxel_m"]
    o = np.asarray(doc["origin_m"])
    nsc, n = _grid(d, "geometry/terrain/dsm_2m_n.json")
    from scipy import ndimage

    cov = coverage_mask(n)
    near = ndimage.binary_dilation(cov, iterations=1)                              # 体素柱与覆盖格的重叠判据留 1 格余量
    cx = o[0] + (np.arange(nx) + 0.5) * v
    cy = o[1] + (np.arange(ny) + 0.5) * v
    c = np.floor((cx - nsc["originXY"][0]) / nsc["cellM"]).astype(int)
    r = np.floor((cy - nsc["originXY"][1]) / nsc["cellM"]).astype(int)
    inside = (r[:, None] >= 0) & (r[:, None] < n.shape[0]) & (c[None, :] >= 0) & (c[None, :] < n.shape[1])
    outside = ~(near[np.clip(r, 0, n.shape[0] - 1)][:, np.clip(c, 0, n.shape[1] - 1)] & inside)
    vox = G[:, outside]
    assert vox.size > 100_000                                                     # 缺角 100 m × 100 m × 全高
    assert int((vox != UNKNOWN).sum()) == 0                                       # 100% unknown
    # 洞内柱（离洞边 ≥ 2 m）：无表面、无竖向命中 → unknown；表面之下与洞不视为空地
    (e0, n0), (e1, n1) = np.sort([_to_world(T, HOLE[0], HOLE[0])[:2], _to_world(T, HOLE[1], HOLE[1])[:2]], axis=0)
    hx = (cx > e0 + 2.0) & (cx < e1 - 2.0)
    hy = (cy > n0 + 2.0) & (cy < n1 - 2.0)
    assert hx.sum() >= 15 and hy.sum() >= 15
    assert np.all(G[:, hy][:, :, hx] == UNKNOWN)
    # 抽样随机体素与按体素下标解码（read_states）一致
    rng = np.random.default_rng(4)
    pick = np.stack([rng.integers(0, nx, 2000), rng.integers(0, ny, 2000), rng.integers(0, nz, 2000)], 1)
    assert np.array_equal(read_states(ix, pick), G[pick[:, 2], pick[:, 1], pick[:, 0]])


def test_occupancy_surface_below_and_free(built) -> None:
    d, _T = built
    ix = d / "geometry/voxel/occ_1m/index.json"
    doc = _j(ix)
    G = dense_states(ix)
    o = np.asarray(doc["origin_m"])
    v = doc["voxel_m"]
    V, F = trimesh_world(d / "geometry/mesh/raycast/index.json")
    sv = surface_voxels(V, F, o, v, np.asarray(doc["dims"]))
    nx, ny = doc["dims"][:2]
    iz, iy, ixx = sv // (nx * ny), (sv // nx) % ny, sv % nx
    st = G[iz, iy, ixx]
    known = st != UNKNOWN
    assert known.mean() > 0.9 and np.all(st[known] == OCCUPIED)
    vx = np.floor((V[np.unique(F)] - o) / v).astype(int)                          # 顶点所在体素：V-GLB-13 的表面抽样口径
    sv2 = G[vx[:, 2], vx[:, 1], vx[:, 0]]
    assert np.all((sv2 == OCCUPIED) | (sv2 == UNKNOWN))

    def at(E: float, N: float, U: float) -> int:
        k = np.floor((np.array([E, N, U]) - o) / v).astype(int)
        return int(G[k[2], k[1], k[0]])

    assert at(40.0, 0.0, 15.0) == BELOW                                            # 盒内（表面之下）
    assert at(40.0, 0.0, 30.5) in (OCCUPIED, FREE) and at(-100.0, 100.0, 10.0) == FREE
    assert at(-100.0, 100.0, 0.0) == OCCUPIED
    counts = doc["stats"]["explicit_voxels"]
    assert counts["below_surface"] > 0 and counts["occupied"] > 0 and counts["unknown"] > 0


def test_vglb_rules_on_the_hole_world(built) -> None:
    d, _T = built
    rep = validate_world(d, deep=True)
    assert rep.ok, rep.errors
    rows = {r["id"]: r for r in vglb_results(rep)}
    for rid in ("V-GLB-07", "V-GLB-09", "V-GLB-10", "V-GLB-12", "V-GLB-13"):
        assert rows[rid]["status"] == "pass", rows[rid]
    assert rows["V-GLB-07"]["measured"]["iou"] >= 0.98
    # border = 覆盖多边形内缩 20 m（M22 §6.6.7），不再是包围盒内缩的矩形
    z = _j(d / "semantic/zones.geojson")
    b = next(f for f in z["features"] if f["properties"]["kind"] == "border")
    ring = np.asarray(b["geometry"]["coordinates"][0])
    assert 6 <= len(ring) - 1 <= 1024                                              # L 形
    wb = _j(d / "world.json")["bounds"]
    assert ring[:, 0].min() >= wb["min"][0] + 19.0 and ring[:, 0].max() <= wb["max"][0] - 19.0
    qa = _j(d / "qa/report.json")
    assert {s["name"] for s in qa["stages"]} >= {"quality", "occupancy"}
    st = {r["id"]: r["status"] for r in qa["glb"]["vglb"]}
    assert st["V-GLB-07"] == st["V-GLB-13"] == "pass"
    fresh = {r["id"]: r["status"] for r in vglb_results(rep)}
    assert {k: v for k, v in st.items() if k not in ("V-GLB-02", "V-GLB-15")} == \
        {k: v for k, v in fresh.items() if k not in ("V-GLB-02", "V-GLB-15")}      # 报告后补验不覆盖未执行的规则


def test_build_is_deterministic(built, tmp_path: Path) -> None:
    d1, _T = built
    d2, _T2 = _built(tmp_path)
    rels = ["geometry/terrain/quality_2m.json", "geometry/terrain/quality_2m.u8", "semantic/quality.geojson", "semantic/zones.geojson",
            "geometry/mesh/raycast/q.u8", "visual/mesh/appearance/tri.q.u8", "geometry/voxel/occ_1m/index.json",
            "geometry/voxel/occ_1m/top.u16", "geometry/voxel/occ_1m/bricks.idx", "geometry/voxel/occ_1m/bricks.bin"]
    for rel in rels:
        assert hashlib.sha256((d1 / rel).read_bytes()).hexdigest() == hashlib.sha256((d2 / rel).read_bytes()).hexdigest(), rel
    assert _j(d1 / "world.json")["contentVersion"] == _j(d2 / "world.json")["contentVersion"]


# ---------------------------------------------------------------- 单元


def test_sample_kernel_numba_and_numpy_agree() -> None:
    rng = np.random.default_rng(11)
    V = rng.uniform([0, 0, 0], [30, 20, 8], (60, 3))
    F = rng.integers(0, 60, (90, 3))
    F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    V = np.vstack([V, [[0, 0, 2], [30, 0, 2], [30, 20, 2], [0, 20, 2]]])          # 叠加一层平面：同采样多层
    F = np.vstack([F, [[60, 61, 62], [60, 62, 63]]])
    a = sample_top(V, F, (0.0, 0.0), 2.0, 10, 15, 2, use_numba=False)
    if HAVE_NUMBA:
        b = sample_top(V, F, (0.0, 0.0), 2.0, 10, 15, 2, use_numba=True)
        for x, y in zip(a, b, strict=True):
            assert np.array_equal(x, y)
    top, prim, low = a
    hit = prim >= 0
    assert hit.all()                                                              # 平面覆盖全部采样
    assert np.all(top[hit] >= 2.0 - 1e-12) and np.all(low[hit] <= 2.0 + 1e-12)
    from awr.world.glb.raster import raster_top

    t2, _n = raster_top(V, F, (0.0, 0.0), 2.0, 10, 15, 2, use_numba=False)
    assert np.array_equal(top.max(2), t2)                                         # 与 dsm 栅格化核同一采样位置


def test_mask_rings_reproduce_the_mask_exactly() -> None:
    rng = np.random.default_rng(5)
    for _ in range(20):
        m = rng.random((23, 31)) < 0.45
        m[3, 3], m[4, 4], m[3, 4], m[4, 3] = True, True, False, False             # 共角对角格
        rings = mask_rings(m, (10.0, -5.0), 0.5)
        back = rasterize_rings([P for P, _a, _c in rings], (10.0, -5.0), 0.5, *m.shape)
        assert np.array_equal(back, m)
        from scipy import ndimage

        n_comp = ndimage.label(m)[1]
        assert sum(1 for _P, a, _c in rings if a > 0) == n_comp                  # 每个 4 连通分量一个外环（逆时针）


def test_simplify_ring_respects_the_tolerance() -> None:
    t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    P = np.stack([50 * np.cos(t) + 0.3 * np.sin(17 * t), 30 * np.sin(t)], 1)
    S = simplify_ring(P, 1.0)
    assert 8 <= len(S) < len(P)
    from awr.world.glb.derive.quality import _seg_dist

    Q = np.vstack([S, S[:1]])
    d = np.min(np.stack([_seg_dist(P, Q[i], Q[i + 1]) for i in range(len(S))], 1), axis=1)
    assert d.max() <= 1.0 + 1e-9
    assert np.array_equal(simplify_ring(P, 1.0), S)                                # 确定性


def test_border_ring_is_inside_the_coverage() -> None:
    m = np.zeros((80, 100), bool)
    m[5:75, 5:95] = True
    m[40:75, 60:95] = False                                                       # L 形
    R = border_ring(m, (0.0, 0.0), 2.0, 20.0, 1.0)
    assert R is not None and 6 <= len(R) <= 1024
    from awr.world.package.validate import _points_in_rings

    foot = [P for P, a, _c in mask_rings(m, (0.0, 0.0), 2.0) if a > 0]
    assert _points_in_rings(R, foot).all()
    assert border_ring(m, (0.0, 0.0), 2.0, 500.0, 1.0) is None


def test_boundary_loops_keep_component_outlines_and_return_holes() -> None:
    n = 6
    x, y = np.meshgrid(np.arange(n, dtype=float), np.arange(n, dtype=float))
    V = np.stack([x.ravel(), y.ravel(), np.zeros(x.size)], 1)
    F = []
    for i in range(n - 1):
        for j in range(n - 1):
            if (i, j) == (2, 2):
                continue                                                          # 一个洞
            a, b, c, d = i * n + j, i * n + j + 1, (i + 1) * n + j + 1, (i + 1) * n + j
            F += [[a, b, c], [a, c, d]]
    F = np.asarray(F)
    float_ = np.array([[10, 10, 5], [12, 10, 5], [12, 12, 5]], float)             # 悬空构件：自身外边界不是洞
    V2 = np.vstack([V, float_])
    F2 = np.vstack([F, [[len(V), len(V) + 1, len(V) + 2]]])
    holes = boundary_loops(V2, F2)
    assert len(holes) == 1 and np.allclose(np.sort(holes[0][:, 0]), [2, 2, 3, 3])
    # 跨瓦片重复顶点（≤ 1 mm 偏差）按焊接合并，不产生假边界
    Vd = np.vstack([V, V + 4e-4])
    Fd = F.copy()
    Fd[::2] += len(V)
    assert len(boundary_loops(Vd, Fd)) == 1


def test_morton_key_matches_the_validator() -> None:
    rng = np.random.default_rng(2)
    P = rng.integers(0, 2 ** 12, (200, 3))
    k = morton3(P[:, 0], P[:, 1], P[:, 2])
    assert [int(x) for x in k] == [_morton3(int(a), int(b), int(c)) for a, b, c in P]


def test_add_stream_replaces_in_place(tmp_path: Path) -> None:
    ix = tmp_path / "index.json"
    ix.write_text(json.dumps({"schema": "awr.trimesh.v1", "streams": [{"name": "xyz", "href": "xyz.f32", "dtype": "float32", "components": 3}]}))
    add_stream(ix, "q", "q.u8", np.array([1, 2, 3]), dtype="uint8", count=3)
    add_stream(ix, "cls", "cls.u8", np.array([4, 5, 6]), dtype="uint8")
    add_stream(ix, "q", "q.u8", np.array([7, 8, 9]), dtype="uint8", count=3)
    doc = _j(ix)
    assert [s["name"] for s in doc["streams"]] == ["xyz", "q", "cls"] and doc["schema"] == "awr.trimesh.v1"
    assert read_stream(ix, "q").tolist() == [7, 8, 9] and read_stream(ix, "missing") is None
    with pytest.raises(ValueError):
        add_stream(ix, "q", "q.u8", np.array([1, 2]), dtype="uint8", count=3)
