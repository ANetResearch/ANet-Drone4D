"""NavMesh 图层与 navsvc（M22 §6.6.11、§6.8、§7.3.3，FR-032、FR-033；ADR-196；D3-AC-03 "两台机器烘焙的 NavMesh sha256 相同"
的单机部分、D3-AC-26 "浏览器与 navsvc 共用 WASM" 的版本锁部分）。

夹具为合成求交网格：80 m × 80 m 缓坡平地（1 m 格）、中央 10 m × 10 m × 3 m 实心箱体、东侧 20 m 外抬高 10 m 的平台（独立岛）；
OSM 为两条在原点相交的道路（一个路口 `j-1`）。验证：区域号与走廊、Recast 格参数换算、图层清单字段（V-GLB-05、V-GLB-14）、
线程数与重复烘焙的字节确定性、种子裁剪、navsvc 查询与应答逐字节确定。跨机器（本机 AVX2 与 dmax AVX-512）对拍记录在 W09 执行记录，
正式统计（随机 1,000 对、走廊、植被、吸附分位数）由 tests/d3/world/test_navmesh.py（W12）在 crop 上判定。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.world.georef.frames import world_to_lla
from awr.world.georef.types import Anchor
from awr.world.glb.derive import navmesh as nm
from awr.world.glb.derive.raycast import write_raycast
from awr.world.package.validate import validate_world, vglb_results

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not (nm.NAVSVC_DIR / "node_modules").is_dir(),
                                reason="dep: node 22 and tools/navsvc/node_modules (npm ci --prefix tools/navsvc)")

WID = "synth-nav"
ANCHOR = {"kind": "synthetic", "datum": "WGS84", "latDeg": 43.73, "lonDeg": 23.96, "hEllipsoidM": 0.0, "hMslM": 0.0}
COORD = {"anchor": ANCHOR}


def _mesh() -> tuple[np.ndarray, np.ndarray]:
    n = 80
    xs = np.arange(n + 1, dtype=np.float64) - n / 2
    X, Y = np.meshgrid(xs, xs)
    V = [np.c_[X.ravel(), Y.ravel(), 0.02 * X.ravel()]]
    F = []
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            F += [[a, a + 1, a + n + 2], [a, a + n + 2, a + n + 1]]
    k = (n + 1) ** 2
    box = np.array([[x, y, z] for z in (-0.1, 3.0) for x, y in ((-5, 10), (5, 10), (5, 20), (-5, 20))], np.float64)
    V.append(box)
    for q in ((4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)):
        F += [[k + q[0], k + q[1], k + q[2]], [k + q[0], k + q[2], k + q[3]]]
    k += 8
    V.append(np.array([[60, 0, 10], [80, 0, 10], [80, 20, 10], [60, 20, 10]], np.float64))
    F += [[k, k + 1, k + 2], [k, k + 2, k + 3]]
    return np.vstack(V), np.asarray(F, np.int64)


def _osm(path: Path) -> Path:
    """两条 OSM 道路：东西向 residential（N = 0）与南北向 footway（E = 0），共享节点 1（路口）。"""
    a = Anchor.from_coordinate(COORD)
    pts = {1: (0.0, 0.0), 2: (-35.0, 0.0), 3: (35.0, 0.0), 4: (0.0, -35.0), 5: (0.0, 35.0)}
    lines = []
    for nid, (e, n) in pts.items():
        lat, lon, _h = world_to_lla(np.array([e, n, 0.0]), a)
        lines.append(f' <node id="{nid}" lat="{float(lat):.10f}" lon="{float(lon):.10f}"/>')
    lines.append(' <way id="10"><nd ref="2"/><nd ref="1"/><nd ref="3"/><tag k="highway" v="residential"/></way>')
    lines.append(' <way id="11"><nd ref="4"/><nd ref="1"/><nd ref="5"/><tag k="highway" v="footway"/></way>')
    lines.append(' <way id="12"><nd ref="4"/><nd ref="5"/><tag k="building" v="yes"/></way>')
    path.write_text('<?xml version="1.0"?>\n<osm version="0.6">\n' + "\n".join(lines) + "\n</osm>\n", encoding="utf-8")
    return path


def _stage(tmp: Path, name: str = "w") -> tuple[Path, SimpleNamespace]:
    st = tmp / name
    V, F = _mesh()
    write_raycast(st, WID, "0" * 64, V, F, tile_m=512.0)
    osm = _osm(tmp / "roads.osm")
    rdir = tmp / "recipe"
    rdir.mkdir(exist_ok=True)
    recipe = SimpleNamespace(world_id=WID, dir=rdir, doc={"world_id": WID, "role": "event-main",
                                                          "osm": {"path": str(osm), "sha256": hashlib.sha256(osm.read_bytes()).hexdigest()}})
    return st, recipe


def _verify(path: str, sha: str | None = None) -> Path:
    p = Path(path)
    assert sha is None or hashlib.sha256(p.read_bytes()).hexdigest() == sha
    return p


def _write(st: Path, recipe: SimpleNamespace, **kw) -> nm.NavmeshResult:
    return nm.write_navmesh(st, WID, COORD, "0" * 64, recipe, verify=_verify, **kw)


def test_version_pins_match_package_lock_and_wasm() -> None:
    v = nm.recast_version()
    assert v == "0.43.1"
    pkg = json.loads((nm.NAVSVC_DIR / "package.json").read_text())
    lock = json.loads((nm.NAVSVC_DIR / "package-lock.json").read_text())
    for name in ("core", "generators", "wasm"):
        assert pkg["dependencies"][f"@recast-navigation/{name}"] == v
        assert lock["packages"][f"node_modules/@recast-navigation/{name}"]["version"] == v
    wasm = nm.NAVSVC_DIR / "node_modules/@recast-navigation/wasm/dist/recast-navigation.wasm.wasm"
    assert hashlib.sha256(wasm.read_bytes()).hexdigest() == nm.wasm_sha256()


def test_recast_params_cells_and_profiles() -> None:
    rc, a = nm.recast_params("person", {})
    assert a == nm.AGENTS["person"]
    assert (rc["cs"], rc["ch"], rc["walkableHeight"], rc["walkableClimb"], rc["walkableRadius"], rc["maxEdgeLen"]) == (0.3, 0.2, 9, 2, 1, 40)
    rc, a = nm.recast_params("vehicle", {})
    assert (rc["cs"], rc["walkableHeight"], rc["walkableClimb"], rc["walkableRadius"], rc["walkableSlopeAngle"]) == (0.5, 10, 1, 3, 20.0)
    doc = {"build": {"profile": "fast", "nav": {"person": {"cs": 0.5, "ch": 0.25, "agent": {"radius_m": 0.4}}}}}
    rc, a = nm.recast_params("person", doc)
    assert (rc["cs"], rc["ch"], rc["walkableRadius"], a["radius_m"]) == (1.0, 0.5, 1, 0.4)
    assert nm.nav_profiles({"role": "event-main"}) == ["person", "vehicle"]
    assert nm.nav_profiles({"role": "lidar-explore"}) == ["person"]
    assert nm.nav_profiles({"role": "event-main", "build": {"profile": "fast"}}) == ["person"]
    assert nm.nav_profiles({"build": {"nav": {"vehicle": {"cs": 0.5}}}}) == ["person", "vehicle"]
    assert nm.nav_profiles({"role": "event-main", "build": {"nav": {"vehicle": {"enabled": False}}}}) == ["person"]
    assert nm.to_mm([0.0005, -0.0005, 1.2344]) == [1, 0, 1234]


def test_highways_junction_and_corridor(tmp_path: Path) -> None:
    hw = nm.parse_highways(_osm(tmp_path / "r.osm"), Anchor.from_coordinate(COORD))
    assert [w for w, _k, _xy in hw.ways] == [10, 11]
    assert [n for n, _p in hw.junctions(nm.PERSON_WAYS)] == [1]
    assert hw.junctions(nm.VEHICLE_WAYS) == []                     # footway 不是车辆道路
    assert np.abs(hw.nodes[1]).max() < 1e-3 and abs(hw.nodes[3][0] - 35.0) < 1e-3
    m = nm.corridor_mask(np.array([[20.0, 4.9], [20.0, 5.2], [-4.95, -20.0], [-5.3, -20.0]]), hw.lines(nm.PERSON_WAYS))
    assert m.tolist() == [True, False, True, False]
    assert len(hw.lines(nm.VEHICLE_WAYS)) == 1


def test_triangle_areas_semantics_quality_and_corridor() -> None:
    V = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [100, 0, 0], [101, 0, 0], [100, 1, 0]], np.float64)
    F = np.array([[0, 1, 2], [3, 4, 5], [0, 1, 2], [3, 4, 5], [3, 4, 5]], np.int64)
    keys = ["unclassified", "ground", "low_vegetation", "medium_vegetation", "high_vegetation", "building_roof", "building_facade",
            "low_object", "water", "road_surface"]
    cls = np.array([1, 9, 5, 4, 6], np.uint8)
    q = np.array([0, 0, 0, 0, 4], np.uint8)                        # 第 5 个三角形为拉伸面
    lines = [np.array([[-10.0, 0.0], [10.0, 0.0]])]
    a, hist = nm.triangle_areas(V, F, corridor_lines=lines, tri_cls=cls, class_keys=keys, tri_q=q)
    assert a.tolist() == [nm.ROAD_CORRIDOR, nm.PAVED, nm.NULL, nm.VEGETATION, nm.NULL]
    assert hist == {"NULL": 2, "ROAD_CORRIDOR": 1, "PAVED": 1, "VEGETATION": 1}
    a, _ = nm.triangle_areas(V, F)
    assert set(a.tolist()) == {nm.GROUND}
    z = nm.lowest_hit(np.array([[0, 0, 5], [4, 0, 5], [0, 4, 5], [0, 0, 1], [4, 0, 1], [0, 4, 1.0]]),
                      np.array([[0, 1, 2], [3, 4, 5]]), np.array([[1.0, 1.0], [9.0, 9.0]]))
    assert z[0] == pytest.approx(1.0) and np.isnan(z[1])


def test_write_navmesh_layer_fields_and_determinism(tmp_path: Path) -> None:
    st, recipe = _stage(tmp_path)
    r1 = _write(st, recipe, threads=1)
    d = r1.docs["person"]
    assert [L.id for L in r1.layers] == ["nav.person", "nav.vehicle"]
    L = r1.layers[0].to_json()
    assert (L["type"], L["format"], L["href"], L["axes"]) == ("navmesh", "detour-navmesh@1", "geometry/nav/person.json", "recast-y-up-enu")
    assert d["schema"] == "awr.navmesh.v1" and d["profile"] == "person" and d["recast_version"] == nm.recast_version()
    assert d["wasm_sha256"] == nm.wasm_sha256() and d["axes"] == "recast-y-up-enu" and d["input"] == "raycast"
    assert d["agent"] == nm.AGENTS["person"] and (d["cs_m"], d["ch_m"], d["tile_size_cells"]) == (0.3, 0.2, 128)
    assert d["seeds"] == ["j-1"] and d["seed_candidates"] == 1
    isl = d["islands"]
    assert isl["kept"] == 1 and isl["dropped"] >= 2 and isl["seed_area_m2"] == isl["walkable_area_m2"]
    assert 5800 < isl["walkable_area_m2"] < 6400 < isl["walkable_area_before_m2"]
    assert d["area_triangles"]["ROAD_CORRIDOR"] > 0 and d["area_triangles"]["GROUND"] > 0
    b = st / "geometry/nav" / d["bin"]["href"]
    assert b.stat().st_size == d["bin"]["bytes"] and hashlib.sha256(b.read_bytes()).hexdigest() == d["bin"]["sha256"]
    assert not list(st.glob(".navbake-*"))
    v = r1.docs["vehicle"]
    assert v["poly_flag"] == nm.FLAGS["drive"] and v["agent"] == nm.AGENTS["vehicle"] and v["seeds"] == [nm.AUTO_SEED]
    assert v["seed_fallback"] == "largest-island" and v["seed_candidates"] == 0     # 夹具无车辆路口
    shas = {p: r1.docs[p]["bin"]["sha256"] for p in r1.docs}
    js = {p: (st / "geometry/nav" / f"{p}.json").read_bytes() for p in r1.docs}
    st2, recipe2 = _stage(tmp_path, "w2")
    r3 = _write(st2, recipe2, threads=3)
    assert {p: r3.docs[p]["bin"]["sha256"] for p in r3.docs} == shas
    assert {p: (st2 / "geometry/nav" / f"{p}.json").read_bytes() for p in r3.docs} == js
    assert r3.qa["profiles"]["person"]["runtime"]["threads"] == 3


def test_vglb_05_and_14_on_the_layer(tmp_path: Path) -> None:
    st, recipe = _stage(tmp_path)
    r = _write(st, recipe, profiles=["person"])
    (st / "world.json").write_text(json.dumps({"id": WID, "profile": "d3-glb", "layers": [L.to_json() for L in r.layers]}))
    def rows() -> dict[str, str]:
        rep = validate_world(st, rules={"V-GLB-05", "V-GLB-14"}, staging=True)
        return {x["id"]: x["status"] for x in vglb_results(rep) if x["id"] in ("V-GLB-05", "V-GLB-14")}

    assert rows() == {"V-GLB-05": "pass", "V-GLB-14": "pass"}
    p = st / "geometry/nav/person.json"
    doc = json.loads(p.read_text())
    doc["seeds"], doc["recast_version"] = [], "0.42.0"
    p.write_text(json.dumps(doc))
    assert rows() == {"V-GLB-05": "fail", "V-GLB-14": "fail"}


def test_unsnapped_seeds_are_not_masked(tmp_path: Path) -> None:
    """有候选种子但全部吸附失败：保留最大岛但 seeds[] 为空（V-GLB-05 报错）；无任何种子来源时登记 auto-largest-island。"""
    st, recipe = _stage(tmp_path)
    (recipe.dir / "nav_seeds.json").write_text(json.dumps({"seeds": [{"id": "m-far", "xy": [500.0, 500.0], "profile": "person"}]}))
    recipe.doc.pop("osm")
    r = _write(st, recipe, profiles=["person"])
    d = r.docs["person"]
    assert d["seeds"] == [] and d["seed_candidates"] == 1 and d["seed_fallback"] == "largest-island"
    (recipe.dir / "nav_seeds.json").unlink()
    d = _write(st, recipe, profiles=["person"]).docs["person"]
    assert d["seeds"] == [nm.AUTO_SEED] and d["seed_candidates"] == 0


def test_keep_largest_seeded_island(tmp_path: Path) -> None:
    st, recipe = _stage(tmp_path)
    (recipe.dir / "nav_seeds.json").write_text(json.dumps({"seeds": [{"id": "m-platform", "xy": [70.0, 10.0], "profile": "person"}]}))
    both = _write(st, recipe, profiles=["person"]).docs["person"]
    assert both["islands"]["kept"] == 2 and sorted(both["seeds"]) == ["j-1", "m-platform"]
    recipe.doc["build"] = {"nav": {"person": {"keep": "largest-seeded"}}}
    one = _write(st, recipe, profiles=["person"]).docs["person"]
    assert one["islands"]["kept"] == 1 and one["seeds"] == ["j-1"] and one["keep"] == "largest-seeded"
    assert one["islands"]["walkable_area_m2"] < both["islands"]["walkable_area_m2"]


def test_navsvc_queries_and_byte_identical_replies(tmp_path: Path) -> None:
    st, recipe = _stage(tmp_path)
    r = _write(st, recipe)
    (st / "world.json").write_text(json.dumps({"id": WID, "layers": [L.to_json() for L in r.layers]}))
    seq = [("snap", {"profile": "person", "p": [1234, -5678, 100]}),
           ("path", {"profile": "person", "a": [-30000, -30000, 0], "b": [0, 30000, 0]}),
           ("los", {"profile": "person", "a": [0, 0, 0], "b": [0, 30000, 0]}),
           ("random_point_around", {"profile": "person", "p": [0, 0, 0], "r_mm": 5000, "seed": 3}),
           ("random_point", {"profile": "person", "seed": 9}),
           ("height", {"profile": "person", "p": [10000, -10000, 500]}),
           ("path", {"profile": "person", "a": [0, 0, 0], "b": [70000, 10000, 10000]}),
           ("snap", {"profile": "vehicle", "p": [0, 0, 0]})]
    raws = []
    for _ in range(2):
        with nm.NavSvc(world=st) as ns:
            assert ns.info["recast_version"] == nm.recast_version() and ns.info["wasm_sha256"] == nm.wasm_sha256()
            assert ns.info["profiles"] == {p: r.docs[p]["bin"]["sha256"] for p in r.docs}
            raws.append(b"".join(ns.call_raw(op, **kw) for op, kw in seq))
    assert raws[0] == raws[1]
    with nm.NavSvc(profiles={"person": st / "geometry/nav/person.json"}) as ns:
        s = ns.snap("person", (1.234, -5.678, 0.1))
        assert s["found"] and s["dist_mm"] == 0 and s["p"][:2] == [1234, -5678]
        p = ns.path("person", (-30, -30, 0), (0, 30, 0))
        assert p["complete"] and len(p["points"]) >= 3 and p["length_mm"] > 67000      # 绕开箱体
        assert ns.los("person", (0, 0, 0), (0, 30, 0))["clear"] is False
        assert ns.los("person", (-30, -30, 0), (30, -30, 0))["clear"] is True
        a, b = ns.random_point_around("person", (0, 0, 0), 5.0, 3), ns.random_point_around("person", (0, 0, 0), 5.0, 3)
        assert a["found"] and a["p"] == b["p"] and a["ref"] == b["ref"]
        assert np.hypot(*a["p"][:2]) < 15000                       # Detour 在与圆相交的多边形内取点，可略超出半径
        h = ns.height("person", (10, -10, 0.5))
        assert h["found"] and abs(h["z_mm"] - 200) < 600
        assert ns.path("person", (0, 0, 0), (70, 10, 10))["found"] is False             # 平台已被裁剪
        with pytest.raises(nm.NavSvcError) as e:
            ns.snap("vehicle", (0, 0, 0))
        assert e.value.code == "NAV_UNAVAILABLE"
        with pytest.raises(nm.NavSvcError) as e:
            ns.call("snap", profile="person", p=[0.5, 0, 0])
        assert e.value.code == "BAD_REQUEST"
