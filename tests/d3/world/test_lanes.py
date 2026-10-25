"""OSM 道路图与车道图（M22 §7.3.4，FR-034、FR-074、FR-075；M22-AC-033；D3-AC-33 "车辆沿 OSM 车道图运动，路段 100% 落在
道路走廊内"的车道图部分与"车辆生成确定性"的车道图输入部分）。

合成夹具（世界帧 m）：东西向 primary（双向 4 车道、限速 50、`turn:lanes:forward = left|through;right`），东端越出覆盖（E > 150）
被裁剪；南北向 residential（无车道标签，缺省每向 1 车道）与 primary 相交于 n-3，北段在 n-7 与单行 secondary（2 车道，n-4 → n-9 →
n-7）相接，北端 n-8 只接 track（车辆断头）；service 支路 n-2 → n-10 断头；footway 经 n-13 与 primary 共点（车辆度 2 节点）；
residential 北行方向在 n-15 有停车让行标志；转向限制：residential 北行在 n-3 禁止左转进入 primary。

验证：标签换算；切分与覆盖裁剪；车道偏移（右侧通行）；道路走廊 100%（从文件独立复算）与越出走廊的标记；路口全部有转向连通
与冲突区、让行与停止线、转向限制与 `turn:lanes`；断头掉头；强连通与 `lane_route` 随机 500 对可达且首尾相接；字节确定性与输入
次序无关、`content_sha256` 复算；NavMesh 吸附与 GLB 构建端到端（V-GLB-08、V-GLB-10）；crop 实数据（有数据时）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.world.georef.frames import world_to_lla
from awr.world.georef.types import Anchor
from awr.world.glb.derive import lanes as L
from awr.world.glb.derive import navmesh as nm

ROOT = Path(__file__).resolve().parents[3]
ANCHOR = {"kind": "synthetic", "datum": "WGS84", "latDeg": 43.73, "lonDeg": 23.96, "hEllipsoidM": 0.0, "hMslM": 0.0}
COORD_SHA = "c" * 64
SOURCES = [{"name": "osm/roads.osm", "sha256": "a" * 64}]
NODES = {1: (-140, 0), 2: (-60, 0), 13: (-30, 0), 3: (0, 0), 4: (60, 0), 5: (175, 0), 6: (0, -120), 15: (0, -20), 7: (0, 60),
         8: (0, 120), 9: (60, 60), 10: (-60, -80), 11: (-30, -20), 12: (-30, 20), 14: (40, 120)}
WAYS = [(100, [1, 2, 13, 3, 4, 5], {"highway": "primary", "lanes": "4", "maxspeed": "50", "turn:lanes:forward": "left|through;right"}),
        (101, [6, 15, 3, 7, 8], {"highway": "residential"}),
        (102, [4, 9, 7], {"highway": "secondary", "oneway": "yes", "lanes": "2"}),
        (104, [2, 10], {"highway": "service"}),
        (105, [11, 13, 12], {"highway": "footway"}),
        (106, [8, 14], {"highway": "track"}),
        (107, [6, 10], {"building": "yes"})]
NODE_TAGS = {15: {"highway": "stop", "direction": "forward"}}
RESTRICTION = '<relation id="900"><member type="way" ref="101" role="from"/><member type="node" ref="3" role="via"/>' \
              '<member type="way" ref="100" role="to"/><tag k="type" v="restriction"/><tag k="restriction" v="no_left_turn"/></relation>'


def osm_xml(path: Path, anchor: dict = ANCHOR, nodes: dict = NODES, ways: list = WAYS, reverse: bool = False) -> Path:
    a = Anchor.from_coordinate({"anchor": anchor})
    nl = []
    for nid, (e, n) in nodes.items():
        lat, lon, _h = world_to_lla(np.array([float(e), float(n), 0.0]), a)
        tags = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in NODE_TAGS.get(nid, {}).items())
        nl.append(f' <node id="{nid}" lat="{float(lat):.10f}" lon="{float(lon):.10f}">{tags}</node>')
    wl = []
    for wid, ids, tags in ways:
        nd = "".join(f'<nd ref="{i}"/>' for i in ids)
        tg = "".join(f'<tag k="{k}" v="{v}"/>' for k, v in tags.items())
        wl.append(f' <way id="{wid}">{nd}{tg}</way>')
    body = nl + wl + [" " + RESTRICTION]
    if reverse:
        body = body[::-1]
    path.write_text('<?xml version="1.0"?>\n<osm version="0.6">\n' + "\n".join(body) + "\n</osm>\n", encoding="utf-8")
    return path


def _cover() -> np.ndarray:
    m = np.ones((160, 160), bool)          # origin (-160, -160)，2 m 格
    m[:, 155:] = False                     # E > 150 不在覆盖内
    return m


def _zfn(xy: np.ndarray) -> np.ndarray:
    return 0.01 * xy[:, 0] + 1.0


def _write(tmp: Path, name: str = "w", **kw) -> L.LanesResult:
    osm = kw.pop("osm", None) or osm_xml(tmp / f"{name}.osm")
    roads = L.read_osm(osm, Anchor.from_coordinate({"anchor": ANCHOR}))
    return L.write_lanes(tmp / name, "synth-lanes", COORD_SHA, roads, sources=SOURCES, inside=L.cover_fn(_cover(), (-160.0, -160.0), 2.0),
                         z_fn=_zfn, **kw)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[L.LanesResult, Path, L.LaneGraph]:
    tmp = tmp_path_factory.mktemp("lanes")
    r = _write(tmp)
    p = tmp / "w" / L.HREF
    return r, p, L.LaneGraph.load(p)


def _feat(doc: dict, kind: str) -> dict[str, dict]:
    return {f["id"]: f for f in doc["features"] if f["properties"]["feature"] == kind}


def test_tag_parsing() -> None:
    assert L.parse_maxspeed("50") == 13.889
    assert L.parse_maxspeed("30 mph") == round(30 * 1.609344 / 3.6, 3)
    assert L.parse_maxspeed("BG:urban") == 13.889 and L.parse_maxspeed("RO:rural") == 25.0
    assert L.parse_maxspeed("50;30") == 13.889
    assert L.parse_maxspeed("none") is None and L.parse_maxspeed(None) is None and L.parse_maxspeed("signals") is None
    assert L._oneway({"oneway": "yes"}) == 1 and L._oneway({"oneway": "-1"}) == -1 and L._oneway({"oneway": "no"}) == 0
    assert L._oneway({"junction": "roundabout"}) == 1 and L._oneway({"highway": "motorway"}) == 1 and L._oneway({}) == 0
    assert L._turns("left|through;right", 2) == [["left"], ["right", "through"]]
    assert L._turns("|right", 2) == [["none"], ["right"]] and L._turns("left|right", 3) is None


def test_edges_split_at_junctions_and_clipped_to_cover(built) -> None:
    r, _p, _g = built
    ed = _feat(r.doc, "edge")
    assert {k for k in ed if k.startswith("e-100-")} == {"e-100-0", "e-100-1", "e-100-2", "e-100-3", "e-100-4-0"}
    assert {k for k in ed if k.startswith("e-101-")} == {"e-101-0", "e-101-1", "e-101-2"}       # 15 只被引用一次，不切分
    cut = ed["e-100-4-0"]["properties"]
    assert cut["from_node"] == "n-4" and cut["to_node"] == "c-100-4-0b"
    end = ed["e-100-4-0"]["geometry"]["coordinates"][-1]
    assert 148.0 <= end[0] <= 150.0 and abs(end[1]) < 0.01
    for f in ed.values():
        p = f["properties"]
        assert len(p["z_m"]) == len(f["geometry"]["coordinates"])
        assert p["osm_id"].startswith("way/") and p["kind"] in ("road", "service", "footway", "path", "steps", "cycleway")
        assert set(p) >= {"edge_id", "from_node", "to_node", "kind", "agents", "width_m", "speed_mps", "oneway", "lanes", "osm_id", "z_m"}
    z = ed["e-100-3"]["properties"]["z_m"]
    assert z == [1.0, 1.6]                                              # z_fn = 0.01 E + 1
    assert ed["e-105-0"]["properties"]["agents"] == ["person"] and ed["e-106-0"]["properties"]["kind"] == "path"
    pr = ed["e-100-3"]["properties"]
    assert (pr["lanes"], pr["lanes_forward"], pr["lanes_backward"], pr["width_m"], pr["speed_mps"]) == (4, 2, 2, 12.8, 13.889)
    rs = ed["e-101-0"]["properties"]
    assert (rs["lanes"], rs["width_m"], rs["speed_mps"], rs["speed_source"]) == (2, 6.4, 8.3, "default")
    assert ed["e-102-0"]["properties"]["oneway"] is True and ed["e-102-0"]["properties"]["lanes_backward"] == 0
    assert "e-107-0" not in ed                                          # 非道路 way 不进入道路图


def test_lane_offsets_right_hand_traffic(built) -> None:
    _r, _p, g = built
    def n_mid(lane: str) -> float:
        xy = g.segs[lane].xy
        return float(xy[len(xy) // 2, 1])
    # primary（东西向，4 车道，车道宽 3.2 m）：东行在南侧，0 号为最左（靠中线）
    assert n_mid("l-100-3-f0") == pytest.approx(-1.6) and n_mid("l-100-3-f1") == pytest.approx(-4.8)
    assert n_mid("l-100-3-b0") == pytest.approx(1.6) and n_mid("l-100-3-b1") == pytest.approx(4.8)
    # residential 北行在东侧
    assert float(g.segs["l-101-0-f0"].xy[1, 0]) == pytest.approx(1.6)
    # 单行 secondary（2 车道）对称于中线
    xs = sorted(float(g.segs[k].xy[1, 0]) for k in ("l-102-0-f0", "l-102-0-f1"))
    assert xs == pytest.approx([58.4, 61.6])
    # 车道在路口节点处退让：primary 在 n-3（东西 4 车道 + 南北 2 车道）处退让 ≥ 相邻道路半宽 + 1 m
    assert float(g.segs["l-100-3-f0"].xy[0, 0]) >= 3.2 + 1.0
    for k in g.lanes:
        p = g.segs[k].props
        assert p["speed_mps"] > 0 and "stop_line" in p and p["width_m"] > 0 and len(p["z_m"]) == len(g.segs[k].xy)


def test_all_lanes_inside_road_corridor_recomputed_from_file(built) -> None:
    r, p, g = built
    st = r.qa["stats"]
    assert st["in_corridor_frac"] == 1.0 and st["in_corridor_samples_frac"] == 1.0 and st["max_corridor_excess_m"] == 0.0
    assert st["carriageway_samples_frac"] >= 0.99
    assert r.qa["corridor_warnings"] == 0
    doc = json.loads(p.read_text())
    lines = [(np.asarray(f["geometry"]["coordinates"]), max(f["properties"]["width_m"] / 2, 5.0), f["properties"]["width_m"] / 2 + 0.5)
             for f in doc["features"] if f["properties"]["feature"] == "edge" and "vehicle" in f["properties"]["agents"]]
    cor = L.Corridor(lines)
    for k, s in g.segs.items():
        ec, _ek = cor.excess(L._samples(s.xy))
        assert (ec <= 0).all(), k
        assert s.props["in_corridor"] is True
    off = np.array([[-50.0, 9.0], [-40.0, 9.0]])                       # primary 中线以北 9 m（半宽 6.4 m、走廊半宽 6.4 m）
    assert (cor.excess(L._samples(off))[0] > 0).all()


def test_lanes_outside_the_corridor_are_flagged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orig = L._offset
    monkeypatch.setattr(L, "_offset", lambda P, o: orig(P, o + 12.0))     # 全部车道整体右移 12 m
    r = _write(tmp_path)
    lanes = _feat(r.doc, "lane")
    assert lanes and not any(f["properties"]["in_corridor"] for f in lanes.values())
    assert all(f["properties"]["corridor_excess_m"] > 0 for f in lanes.values())
    assert r.qa["stats"]["in_corridor_frac"] < 1.0 and r.qa["warnings_total"] >= len(lanes)
    assert r.qa["corridor_warnings"] >= len(lanes) and any("outside the road corridor" in w for w in r.qa["warnings"])


def _pip(pt: np.ndarray, ring: np.ndarray) -> bool:
    x, y = float(pt[0]), float(pt[1])
    inside = False
    for (x1, y1), (x2, y2) in itertools.pairwise(ring):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def test_junctions_turn_connectivity_conflict_zones_and_right_of_way(built) -> None:
    r, _p, g = built
    st = r.qa["stats"]
    assert set(g.junctions) == {"j-2", "j-3", "j-4", "j-7"}
    assert st["junctions_covered"] == st["junctions"] == 4 == st["conflict_zones"]
    for jid, j in g.junctions.items():
        assert j["connectors"] and j["conflict_zone"] == f"z-{jid[2:]}"
        z = g.conflict_zones(jid)[0]
        assert _pip(j["xy"], z["ring"]) and z["area_m2"] > 0
        assert g.conflict_zones(j["node"]) == [z]
    con = {k: s.props for k, s in g.segs.items() if s.kind == "connector"}
    for k, p in con.items():                                             # 冲突对称
        for o in p["conflicts"]:
            assert k in con[o]["conflicts"]
    j3 = g.junctions["j-3"]
    assert j3["rule"] == "priority" and set(j3["major_edges"]) == {"e-100-2", "e-100-3"}
    at3 = {k: con[k] for k in j3["connectors"]}
    assert {p["priority"] for p in at3.values() if p["from_lane"].startswith("l-100-")} == {"major"}
    assert {p["priority"] for p in at3.values() if p["from_lane"].startswith("l-101-")} == {"minor"}
    # 转向限制：residential 北行（e-101-0 forward）在 n-3 不得左转进入 primary 西行（e-100-2 backward）
    assert not [p for p in at3.values() if p["from_lane"] == "l-101-0-f0" and p["to_lane"].startswith("l-100-2-b")]
    assert {p["turn"] for p in at3.values() if p["from_lane"] == "l-101-0-f0"} == {"through", "right"}
    # turn:lanes（东行 left|through;right）：左转只自 0 号车道，直行与右转只自 1 号车道
    east = [p for p in at3.values() if p["from_lane"].startswith("l-100-2-f")]
    assert {p["from_lane"] for p in east if p["turn"] == "left"} == {"l-100-2-f0"}
    assert {p["from_lane"] for p in east if p["turn"] in ("through", "right")} == {"l-100-2-f1"}
    assert {p["to_lane"] for p in east if p["turn"] == "through"} == {"l-100-3-f0", "l-100-3-f1"}
    # 东行左转与西行直行交叉
    lt = next(k for k, p in at3.items() if p["from_lane"] == "l-100-2-f0" and p["turn"] == "left")
    wt = [k for k, p in at3.items() if p["from_lane"].startswith("l-100-3-b") and p["turn"] == "through"]
    assert wt and set(wt) <= set(con[lt]["conflicts"])
    # n-7：单行 secondary 驶入为主路，residential 为次路；同级以上无停止线
    assert g.junctions["j-7"]["major_edges"] == ["e-102-0"]
    # 停止线：次路驶入车道全部有停止线，主路驶入没有；n-15 的停车标志使北行驶入 n-3 的规则为 stop_sign
    for k in g.lanes:
        p = g.segs[k].props
        if p["junction_rule"] in ("minor", "equal"):
            assert g.stop_line(k) is not None
        if p["junction_rule"] == "major":
            assert g.stop_line(k) is None
    sl = g.stop_line("l-101-0-f0")
    assert sl["rule"] == "stop_sign" and sl["s_m"] == pytest.approx(g.segs["l-101-0-f0"].length)
    assert np.allclose(sl["xy"].mean(0), g.segs["l-101-0-f0"].xy[-1], atol=1e-3)
    assert g.stop_line("l-104-0-b0")["rule"] == "yield_major"          # service 驶入 primary
    assert st["stop_lines_missing"] == 0 and st["stop_lines"] == st["stop_lines_required"]


def test_pass_through_and_dead_end_connectors(built) -> None:
    _r, _p, g = built
    con = {k: s.props for k, s in g.segs.items() if s.kind == "connector"}
    at13 = [p for p in con.values() if p["junction"] == "n-13"]
    assert at13 and {p["turn"] for p in at13} == {"through"} and "j-13" not in g.junctions
    assert {(p["from_lane"], p["to_lane"]) for p in at13} >= {("l-100-1-f0", "l-100-2-f0"), ("l-100-2-b1", "l-100-1-b1")}
    # 单行 secondary 在 n-9 的直角转折是 way 内部顶点（不切分）：车道斜接偏移后仍在走廊内，转角处偏移距离为 √2 倍
    for lane, corner in (("l-102-0-f1", [61.6, 61.6]), ("l-102-0-f0", [58.4, 58.4])):
        xy = g.segs[lane].xy
        k = int(np.argmin(np.abs(xy[:, 0] - corner[0]) + np.abs(xy[:, 1] - corner[1])))
        assert np.allclose(xy[k], corner, atol=1e-6), lane
    # turn:lanes 与路口几何不符（n-2 东行 0 号车道只允许左转，而 n-2 无左转出口）：按缺省直行补齐并告警
    assert ("l-100-0-f0", "l-100-1-f0") in {(p["from_lane"], p["to_lane"]) for p in con.values() if p["junction"] == "n-2"}
    assert any("l-100-0-f0: no connection by turn:lanes" in w for w in built[0].qa["warnings"])
    for node, a, b in (("n-10", "l-104-0-f0", "l-104-0-b0"), ("n-8", "l-101-2-f0", "l-101-2-b0")):
        u = [p for p in con.values() if p["junction"] == node]
        assert [(p["from_lane"], p["to_lane"], p["turn"]) for p in u] == [(a, b, "uturn")], node
    for node, a, b in (("c-100-4-0b", "l-100-4-0-f", "l-100-4-0-b"), ("n-1", "l-100-0-b", "l-100-0-f")):   # 4 车道断头：每条驶入车道可掉头
        u = {(p["from_lane"], p["to_lane"]) for p in con.values() if p["junction"] == node}
        assert u == {(a + "0", b + "0"), (a + "1", b + "0"), (a + "0", b + "1")}, node


def test_routing_strongly_connected_and_500_random_pairs(built) -> None:
    r, _p, g = built
    assert r.qa["stats"]["routable_length_frac"] == 1.0
    ids = g.routable()
    assert len(ids) == len(g.segs)
    rng = np.random.default_rng(20261019)
    lanes = g.lanes
    for _ in range(500):
        a, b = (lanes[i] for i in rng.integers(0, len(lanes), 2))
        rt = g.lane_route(a, b)
        assert rt is not None and rt.segments[0] == a and rt.segments[-1] == b
        for x, y in itertools.pairwise(rt.segments):
            assert y in g.segs[x].succ
            assert np.array_equal(g.segs[x].xy[-1], g.segs[y].xy[0])           # 首尾相接（逐位）
        assert rt.time_s == pytest.approx(sum(g.segs[k].time_s for k in rt.segments))
    # 单行：secondary 只能 n-4 → n-7 方向通行
    rt = g.lane_route("n-7", "n-4")
    assert rt is not None and not [k for k in rt.segments if k.startswith("l-102-")]
    assert any(k.startswith("l-102-") for k in g.lane_route("n-4", "n-7").segments)
    assert g.lane_route("n-6", "n-8").segments[0] == "l-101-0-f0"
    # 世界帧点：投影到最近车道；同一车道上回到后方需绕行
    rt = g.lane_route((30.0, -1.6), (50.0, -1.6))
    assert rt.segments == ["l-100-3-f0"] and rt.length_m == pytest.approx(20.0, abs=1e-6)
    back = g.lane_route((50.0, -1.6), (30.0, -1.6))
    assert back is not None and len(back.segments) > 3 and back.segments[0] == back.segments[-1] == "l-100-3-f0"
    xyz, s = g.route_polyline(back)
    assert s[-1] == pytest.approx(back.length_m, rel=1e-9) and xyz.shape[1] == 3
    assert np.allclose(xyz[0, :2], [50.0, -1.6], atol=1e-3) and np.allclose(xyz[-1, :2], [30.0, -1.6], atol=1e-3)


def test_bytes_deterministic_and_independent_of_input_order(built, tmp_path: Path) -> None:
    r, p, _g = built
    a = p.read_bytes()
    r2 = _write(tmp_path, "again")
    assert (tmp_path / "again" / L.HREF).read_bytes() == a
    r3 = _write(tmp_path, "rev", osm=osm_xml(tmp_path / "rev.osm", ways=WAYS[::-1], reverse=True,
                                              nodes=dict(sorted(NODES.items(), reverse=True))))
    assert (tmp_path / "rev" / L.HREF).read_bytes() == a
    doc = json.loads(a)
    h = doc["awr"]
    assert h["content_sha256"] == L.content_sha256(doc) == r.qa["content_sha256"] == r2.qa["content_sha256"] == r3.qa["content_sha256"]
    assert r.qa["file_sha256"] == hashlib.sha256(a).hexdigest()
    assert (h["schema"], h["frame"], h["units"], h["coordinate_sha256"], h["license"], h["attribution"], h["source_sha256"]) == \
        ("awr.navgraph.v1", "world", "m", COORD_SHA, "ODbL-1.0", "OpenStreetMap contributors", "a" * 64)
    doc["features"][0]["properties"]["width_m"] += 0.001
    assert L.content_sha256(doc) != h["content_sha256"]
    lay = r.layer.to_json()
    assert (lay["id"], lay["type"], lay["format"], lay["href"]) == ("nav.graph", "navgraph", "geojson/awr-navgraph@1", "geometry/nav/graph.geojson")
    assert "ODbL" in lay["note"] and r.layer.files == [L.HREF]


HAVE_NODE = shutil.which("node") is not None and nm.navsvc_modules().is_dir()
need_node = pytest.mark.skipif(not HAVE_NODE, reason="dep: node 22 and tools/navsvc/node_modules (npm ci --prefix tools/navsvc)")


def _plane(half: float = 160.0, step: float = 4.0) -> tuple[np.ndarray, np.ndarray]:
    xs = np.arange(-half, half + step / 2, step)
    n = len(xs)
    X, Y = np.meshgrid(xs, xs)
    V = np.c_[X.ravel(), Y.ravel(), 0.01 * X.ravel() + 1.0]
    F = []
    for j in range(n - 1):
        for i in range(n - 1):
            a = j * n + i
            F += [[a, a + 1, a + n + 1], [a, a + n + 1, a + n]]
    return V, np.asarray(F, np.int64)


@need_node
def test_lanes_snap_to_vehicle_navmesh(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from awr.world.glb.derive.raycast import write_raycast

    st = tmp_path / "st"
    V, F = _plane()
    write_raycast(st, "synth-lanes", COORD_SHA, V, F, tile_m=512.0)
    recipe = SimpleNamespace(world_id="synth-lanes", dir=tmp_path, doc={"world_id": "synth-lanes", "build": {"nav": {"vehicle": {}}}})
    res = nm.write_navmesh(st, "synth-lanes", {"anchor": ANCHOR}, COORD_SHA, recipe, profiles=["vehicle"], threads=2)
    assert res.docs["vehicle"]["islands"]["kept"] >= 1
    with nm.NavSvc(profiles={"vehicle": st / nm.HREF_DIR / "vehicle.json"}) as nav:
        r = _write(tmp_path, "nav", nav=nav)
        _write(tmp_path, "nav2", nav=nav)
    assert (tmp_path / "nav" / L.HREF).read_bytes() == (tmp_path / "nav2" / L.HREF).read_bytes()
    assert r.qa["stats"]["nav_snapped_frac"] >= 0.95
    g = L.LaneGraph(r.doc)
    for k, s in g.segs.items():
        assert s.props["nav"]["z_source"] in ("navmesh", "navmesh+interp"), k
        assert np.abs(s.z - _zfn(s.xy)).max() < 0.5, k                     # NavMesh 高度贴近平面（Recast 体素高 0.2 m 与细节网格误差）
    assert r.doc["awr"]["lanes"]["nav_snap"] == {"profile": "vehicle", "ok_m": 1.0}


@need_node
def test_glb_build_publishes_navgraph_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GLB 构建端到端：配方带 OSM 时发布 `nav.graph`，V-GLB-08（ODbL 署名）与 V-GLB-10（坐标 sha256）通过，qa 有 `glb.navgraph`。"""
    pytest.importorskip("PIL")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_glb_build as gb
    from glbfix import png, write_glb

    from awr.world.glb.pipeline import GlbAdapter
    from awr.world.glb.recipe import file_sha256, load_recipe
    from awr.world.glb.register import register_manual, write_registration
    from awr.world.ingest.types import StageContext
    from awr.world.package.build import build_world
    from awr.world.package.validate import validate_world, vglb_results

    raw = tmp_path / "raw"
    (raw / "synth").mkdir(parents=True)
    (raw / "osm").mkdir()
    Vg, Fg, UV = gb._scene()
    glb = write_glb(raw / "synth" / "synth.glb", V=Vg, F=Fg, UV=UV, tex=png(8, 8, (200, 30, 30)))
    nodes = {1: (-130, 0), 2: (0, 0), 3: (130, 0), 4: (0, -130), 5: (0, 130), 6: (-130, 100)}
    ways = [(10, [1, 2, 3], {"highway": "primary"}), (11, [4, 2, 5], {"highway": "residential"}), (12, [6, 1], {"highway": "service"})]
    anchor = {"kind": "synthetic", "datum": "WGS84", "latDeg": 43.73, "lonDeg": 23.96, "hEllipsoidM": 100.0, "hMslM": 100.0}
    osm = osm_xml(raw / "osm" / "roads.osm", anchor=anchor, nodes=nodes, ways=ways)
    man = tmp_path / "raw-manifest.json"
    man.write_text(json.dumps({"files": [{"name": "synth/synth.glb", "bytes": glb.stat().st_size, "sha256": file_sha256(glb)},
                                         {"name": "osm/roads.osm", "bytes": osm.stat().st_size, "sha256": file_sha256(osm)}]}))
    monkeypatch.setattr(gb.recipe_mod, "RAW_DIR", raw)
    monkeypatch.setattr(gb.recipe_mod, "RAW_MANIFEST", man)
    rd = tmp_path / "recipes" / "synth-glb"
    rd.mkdir(parents=True)
    (rd / "recipe.yaml").write_text(
        "schema: awr.glb.recipe.v1\nworld_id: synth-glb\nname: Synthetic GLB test world\nrole: event-main\n"
        f"source:\n  glb: {{path: synth/synth.glb, sha256: {file_sha256(glb)}}}\n"
        "  sketchfab: {uid: '', author: test, license: CC0-1.0}\n"
        f"osm: {{path: osm/roads.osm, sha256: {file_sha256(osm)}, extract: 'synthetic roads 2026-10-19', use: 'anchors,navgraph'}}\n"
        "registration: registration.json\nbuild:\n  tiling_m: 128\n  pointcloud: {target_points: 60000, seed: 7}\n")
    rec = load_recipe("synth-glb", tmp_path / "recipes")
    doc = register_manual(rec, {"s": 1.0, "yaw_deg": 0.0, "g": (0.0, 0.0), "t_osm_m": (0.0, 0.0), "osm_ref": (43.73, 23.96)},
                          h_msl_m=100.0, evidence=["synthetic fixture"])
    write_registration(rec, doc, accept_by="test")
    assert build_world(GlbAdapter("synth-glb", recipe=rec), tmp_path / "worlds", ctx=StageContext(world_id="synth-glb")).exit_code == 0
    w = tmp_path / "worlds" / "synth-glb"
    world = json.loads((w / "world.json").read_text())
    lay = next(x for x in world["layers"] if x["id"] == "nav.graph")
    assert (lay["type"], lay["format"], lay["href"]) == ("navgraph", "geojson/awr-navgraph@1", L.HREF) and "ODbL" in lay["note"]
    assert (w / L.HREF).is_file() and L.HREF in json.dumps(world.get("files", []))
    gdoc = json.loads((w / L.HREF).read_text())
    assert gdoc["awr"]["coordinate_sha256"] == world["coordinate"]["sha256"] and gdoc["awr"]["content_sha256"] == L.content_sha256(gdoc)
    rep = validate_world(w, rules={"V-GLB-08", "V-GLB-10"}, staging=False)
    rows = {x["id"]: x["status"] for x in vglb_results(rep)}
    assert rows["V-GLB-08"] == "pass" and rows["V-GLB-10"] == "pass", rep.errors
    qa = json.loads((w / "qa" / "report.json").read_text())
    ng = qa["glb"]["navgraph"]
    assert ng["content_sha256"] == gdoc["awr"]["content_sha256"] and ng["stats"]["in_corridor_frac"] == 1.0
    assert ng["stats"]["junctions"] == ng["stats"]["junctions_covered"] == 1 and ng["stats"]["routable_length_frac"] == 1.0
    assert "navgraph" in [s["name"] for s in qa["glb"]["stages"]]
    assert ng["stats"]["nav_snapped_frac"] > 0.5


CROP = ROOT / "worlds" / "oryahovo-crop1"
CROP_OSM = ROOT / "data" / "raw" / "d3-glb" / "osm" / "oryahovo_map.osm"


@pytest.mark.skipif(not (CROP / "world.json").is_file() or not CROP_OSM.is_file(), reason="data: worlds/oryahovo-crop1 and the crop OSM extract")
def test_crop_lane_graph_corridor_junctions_and_500_routes(tmp_path: Path) -> None:
    """M22-AC-033 / D3-AC-33（车道图部分）：crop 车道段 100% 在道路走廊内；全部路口有转向连通与冲突区；限速与停止线字段齐全；
    `lane_route` 随机 500 对可达；两次构建 `content_sha256` 相同。已发布的车道图图层（W07 重建后）按同一判据复验。"""
    from awr.world.package.validate import coverage_mask
    from awr.world.terrain.grids import Grid, read_grid

    world = json.loads((CROP / "world.json").read_text())
    pub = next((x for x in world["layers"] if x.get("format") == L.FORMAT), None)
    if pub is not None:
        doc = json.loads((CROP / pub["href"]).read_text())
        assert doc["awr"]["content_sha256"] == L.content_sha256(doc)
    else:
        coord = json.loads((CROP / "coordinate.json").read_text())
        sc, a = read_grid(CROP / "geometry" / "terrain" / "dsm_2m.json")
        dsm = Grid(a, tuple(sc["originXY"]), sc["cellM"])
        scn, cnt = read_grid(CROP / "geometry" / "terrain" / "dsm_2m_n.json")
        roads = L.read_osm(CROP_OSM, Anchor.from_coordinate(coord))
        kw = {"sources": [{"name": "osm/oryahovo_map.osm", "sha256": hashlib.sha256(CROP_OSM.read_bytes()).hexdigest()}],
              "inside": L.cover_fn(coverage_mask(cnt, 2), scn["originXY"], scn["cellM"]), "z_fn": lambda xy: dsm.bilinear(xy[:, 0], xy[:, 1])}
        r1 = L.write_lanes(tmp_path / "a", "oryahovo-crop1", world["coordinate"]["sha256"], roads, **kw)
        r2 = L.write_lanes(tmp_path / "b", "oryahovo-crop1", world["coordinate"]["sha256"], roads, **kw)
        assert r1.qa["file_sha256"] == r2.qa["file_sha256"] and r1.qa["content_sha256"] == r2.qa["content_sha256"]
        doc = r1.doc
    st = doc["awr"]["stats"]
    assert st["in_corridor_frac"] == 1.0 and st["in_corridor_samples_frac"] == 1.0
    assert st["junctions"] > 50 and st["junctions_covered"] == st["junctions"] == st["conflict_zones"]
    assert st["stop_lines_missing"] == 0 and st["routable_length_frac"] >= 0.95
    g = L.LaneGraph(doc)
    for k, s in g.segs.items():
        assert s.props["in_corridor"] and s.speed > 0, k
        if s.kind == "lane":
            assert "stop_line" in s.props and "junction_rule" in s.props
    ids = sorted(k for k in g.routable() if g.segs[k].kind == "lane")
    rng = np.random.default_rng(33)
    miss = 0
    for _ in range(500):
        a, b = (ids[i] for i in rng.integers(0, len(ids), 2))
        miss += g.lane_route(a, b) is None
    assert miss == 0
