"""D3-AC-03 NavMesh 与锚点（W12；ADR-196 决策 (4)、(5)；M22 §6.6.11、§6.8；M23 §6.5、§6.6.2）。

两部分：

1. 事件编译器的 Detour 后端（`awr.sim.events.nav.DetourNav`、`build_stage_nav`）：合成求交网格（与 test_navmesh_layer 同一夹具：80 m 缓坡
   平地、中央 10 m × 10 m × 3 m 箱体、东侧被裁剪的高台岛），验证后端选择与退回、吸附（宽竖直窗口与层复查）、寻路（完整走廊、首末点、
   受限圆角与栅格后端同一算法）、视线、导航面高度、可走判定、离面距离、舞台 1 m 栅格视图与确定性。
2. crop 世界包（`worlds/oryahovo-crop1`，缺省时跳过）上的 D3-AC-03 统计，阈值逐条对照 §11 原文：
   - 行人 NavMesh 在种子洪泛裁剪后随机 1,000 对路径 100% 可达（navsvc `random_point` 按面积均匀抽点，种子 20261019 + i）；
   - 路径落在 OSM 道路走廊（行人道路折线 5 m 缓冲，与烘焙的 ROAD_CORRIDOR 同一口径）的长度比例 ≥ 70%；
   - 经过植被的长度比例 ≤ 5%：植被取 OSM 植被面（natural = wood、scrub、heath、grassland、wetland；landuse = forest、meadow、grass、
     orchard、vineyard、farmland 等）与求交网格语义流（低、中、高植被，世界包有 `cls` 流时）；crop 的点云与网格无植被类别，以 OSM 为准；
   - 车辆 profile NavMesh 存在且示例事件 crop-handoff-001 的车辆段全部可达（门 2 预检，Detour 后端）；
   - 点状锚点（OSM 路口、OSM POI 与出入口节点、示例事件的点状与门锚点；建筑多边形不计；限扫描覆盖范围内）`navSnap` 中位距离 ≤ 1 m，
     > 3 m 的比例 ≤ 5%；吸附按 M22 §6.6.11（水平搜索半宽 15 m，`point` 类 > 3 m 时沿 OSM 道路 ±15 m、步长 0.5 m 重定位，接受 ≤ 1 m）；
   - 两台机器烘焙的 NavMesh sha256 相同：本机部分为图层清单、bin 文件与 navsvc `hello` 三处 sha256 一致及两个 navsvc 进程应答逐字节
     相同；跨机器对拍由 X01 执行，给出 `AWR_NAVMESH_PEER_SHA256="person=<sha>,vehicle=<sha>"` 时在此比对。

统计结果写入 `AWR_AC03_OUT`（给出时，JSON）供 P0a 报告引用。
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import logging
import math
import os
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_navmesh_layer as tl  # 合成夹具（网格、OSM、烘焙）

from awr.sim.events import nav as nv
from awr.world.glb.derive import navmesh as nm
from awr.world.ingest.types import ConfigError

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not (nm.NAVSVC_DIR / "node_modules").is_dir(),
                                reason="dep: node 22 and tools/navsvc/node_modules (npm ci --prefix tools/navsvc)")

ROOT = Path(__file__).resolve().parents[3]
CROP = Path(os.environ.get("AWR_D3_CROP_WORLD", ROOT / "worlds" / "oryahovo-crop1"))
EVENT_DIR = ROOT / "configs" / "events.d3" / "crop-handoff-001"
PAIR_SEED = 20261019
N_PAIRS = 1000
STEP_M = 0.5                                     # 路径长度加权的采样步长
VEG_NATURAL = frozenset({"wood", "scrub", "heath", "grassland", "wetland"})
VEG_LANDUSE = frozenset({"forest", "meadow", "grass", "orchard", "vineyard", "farmland", "allotments", "plant_nursery", "village_green"})
VEG_CLASSES = frozenset({"low_vegetation", "medium_vegetation", "high_vegetation"})
POI_KEYS = ("amenity", "shop", "tourism", "office", "craft", "historic", "entrance")
SNAP_EXT_M, RELOC_SPAN_M, RELOC_STEP_M, RELOC_ACCEPT_M = 15.0, 15.0, 0.5, 1.0      # M22 §6.6.11


# ====================================================================== 合成夹具
def _top(P: np.ndarray) -> np.ndarray:
    """合成网格自上而下首个命中：平地 z = 0.02 x（|x|、|y| ≤ 40）、箱顶 3.0、高台 10.0，其余 NaN。"""
    P = np.asarray(P, np.float64).reshape(-1, 2)
    x, y = P[:, 0], P[:, 1]
    z = np.where((np.abs(x) <= 40) & (np.abs(y) <= 40), 0.02 * x, np.nan)
    z = np.where((x >= -5) & (x <= 5) & (y >= 10) & (y <= 20), 3.0, z)
    return np.where((x >= 60) & (x <= 80) & (y >= 0) & (y <= 20), 10.0, z)


class _Q:
    """MeshWorldQuery 的最小替身：`walkable_surface`（Detour 竖直初值）、`raycast_batch`（栅格后端高度场）、`attach_nav`。"""

    _zhi = 20.0

    def __init__(self) -> None:
        self.nav_surface: dict = {}

    def walkable_surface(self, P, z_top):
        z = _top(P)
        return z, np.where(np.isfinite(z), 0, -1)

    def raycast_batch(self, o, d, tmax):
        z = _top(np.asarray(o)[:, :2])
        t = np.asarray(o)[:, 2] - z
        return np.where(np.isfinite(t), t, np.inf), None, None

    def attach_nav(self, profile, fn) -> None:
        self.nav_surface[profile] = fn


BBOX = (-40.0, -40.0, 40.0, 40.0)
Z_TOL = 0.5          # Detour 多边形与细节网格高度相对求交网格的偏差（体素顶面，ch = 0.2 m；本夹具实测 ≤ 0.4 m）


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    tmp = tmp_path_factory.mktemp("navw")
    st, recipe = tl._stage(tmp)
    r = tl._write(st, recipe)
    (st / "world.json").write_text(json.dumps({"id": tl.WID, "layers": [L.to_json() for L in r.layers]}))
    empty = tmp / "nolayers"
    empty.mkdir()
    (empty / "world.json").write_text(json.dumps({"id": tl.WID, "layers": []}))
    q = _Q()
    navs = nv.build_stage_nav(q, BBOX, [(0.0, 0.0)], world_dir=st, backend="detour")
    return SimpleNamespace(world=st, empty=empty, docs=r.docs, q=q, navs=navs, p=navs["person"], v=navs["vehicle"])


def test_backend_selection_and_fallback(synth, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """ADR-196 (5)：有图层取 Detour（sha256 = 图层 bin.sha256，provenance 取 navsvc），无图层或 `grid` 取栅格；`detour` 不可用即报错。"""
    p, v = synth.p, synth.v
    assert isinstance(p, nv.DetourNav) and isinstance(v, nv.DetourNav) and p.svc is v.svc
    assert p.sha256 == synth.docs["person"]["bin"]["sha256"] and v.sha256 == synth.docs["vehicle"]["bin"]["sha256"] and p.sha256 != v.sha256
    assert p.provenance() == {"recast_navigation": nm.recast_version(), "wasm_sha256": nm.wasm_sha256(), "node": p.svc.info["node"]}
    assert p.kind == "detour" and p.nav_format == "detour-navmesh@1" and np.array_equal(synth.q.nav_surface["person"](np.array([[-20.0, -20.0]])), p.nav_z_batch(np.array([[-20.0, -20.0]])))
    g = nv.build_stage_nav(_Q(), BBOX, [(0.0, 0.0)], world_dir=synth.world, backend="grid")
    assert all(isinstance(n, nv.NavGrid) for n in g.values()) and g["person"].provenance() == nv.GRID_PROVENANCE
    assert g["person"].kind == "grid" and g["person"].provenance()["recast_navigation"] == "none (awr-navgrid@1)"
    assert all(isinstance(n, nv.NavGrid) for n in nv.build_stage_nav(_Q(), BBOX, world_dir=synth.empty).values())
    assert all(isinstance(n, nv.NavGrid) for n in nv.build_stage_nav(_Q(), BBOX).values())               # 无世界目录（夹具）
    with pytest.raises(ConfigError, match="detour-navmesh@1"):
        nv.build_stage_nav(_Q(), BBOX, world_dir=synth.empty, backend="detour")
    with pytest.raises(ConfigError, match="must be one of"):
        nv.build_stage_nav(_Q(), BBOX, world_dir=synth.world, backend="recast")
    monkeypatch.setenv("AWR_EVENT_NAV", "grid")
    assert isinstance(nv.build_stage_nav(_Q(), BBOX, world_dir=synth.world)["person"], nv.NavGrid)
    monkeypatch.setenv("AWR_EVENT_NAV", "auto")
    monkeypatch.setenv("AWR_NODE", str(synth.empty / "no-such-node"))
    with caplog.at_level(logging.WARNING, logger="awr.sim.events.nav"):
        f = nv.build_stage_nav(_Q(), BBOX, world_dir=synth.world)
    assert isinstance(f["person"], nv.NavGrid) and "falls back to awr-navgrid@1" in caplog.text
    with pytest.raises(ConfigError, match="navsvc unavailable"):
        nv.build_stage_nav(_Q(), BBOX, world_dir=synth.world, backend="detour")


def test_snap_wide_window_and_level_recheck(synth) -> None:
    p = synth.p
    s = p.snap("open", -20.0, -20.0)
    assert s.status == "ok" and s.dist_m <= 0.001 and abs(s.p[2] - (-0.4)) < Z_TOL and s.cell > 0
    s = p.snap("roof", 0.0, 15.0)                         # 箱顶不可走（未含种子的岛被裁剪）：吸附到箱体旁的地面，水平最近
    assert s.status == "warn" and 5.0 < s.dist_m < 6.0 and abs(s.p[2] - 0.02 * s.p[0]) < Z_TOL and abs(s.p[0]) <= 5.6
    assert p.snap("roof", 0.0, 15.0) == s                 # 同一请求同一应答
    far = p.snap("far", 200.0, 200.0)
    assert far.status == "error" and far.why == "none" and math.isinf(far.dist_m)
    s = p.snap("edge", 0.0, 21.5, max_m=1.0)              # 箱北侧 1.5 m：可走（代理半径 0.3 m 外）
    assert s.status == "ok" and s.dist_m <= 0.001
    v = synth.v.snap("veh", 0.0, 21.5)                   # 车辆代理半径 1.2 m：同一点离箱体更近、仍可走或就近
    assert v.status == "ok" and v.dist_m < 1.0


def test_path_route_and_corner_rounding(synth) -> None:
    p = synth.p
    a, b = (0.0, 5.0), (0.0, 25.0)
    P = p.path(a, b)
    assert P is not None and P.shape[1] == 2 and np.array_equal(P[0], a) and np.array_equal(P[-1], b)
    inside = (P[:, 0] > -5.0) & (P[:, 0] < 5.0) & (P[:, 1] > 10.0) & (P[:, 1] < 20.0)
    assert not inside.any() and nv.rounded_length(P) > 25.0             # 绕开箱体
    raw = p.path_raw(a, b)
    assert raw["complete"] and len(raw["points"]) >= 4 and len(P) >= len(raw["points"])
    # Detour 拉直点贴在障碍角上：圆角弦切入障碍一侧，视线不通，拐点保留（§6.6.2 只在 a→b 可见时圆角）
    assert {tuple(np.round(x, 3)) for x in np.asarray(raw["points"])[1:-1, :2] / 1000.0} <= {tuple(np.round(x, 3)) for x in P}
    assert p.path((0.0, 0.0), (70.0, 10.0)) is None                    # 高台岛已被裁剪：不完整走廊返回 None
    R = p.route([(-30.0, -30.0), (30.0, -30.0), (30.0, 30.0)])
    assert R is not None and np.array_equal(R[0], [-30.0, -30.0]) and np.array_equal(R[-1], [30.0, 30.0])
    assert p.route([(-30.0, -30.0), (70.0, 10.0)]) is None
    assert p.los((-30.0, -30.0), (30.0, -30.0)) and not p.los((0.0, 5.0), (0.0, 25.0))
    assert p.los_many([((-30.0, -30.0), (30.0, -30.0)), ((0.0, 5.0), (0.0, 25.0))]) == [True, False]
    # 受限圆角：Detour 与栅格后端同一算法（视线恒真时逐位相同）
    Q = np.array([[0.0, 0.0], [10.0, 0.0], [10.0, 7.0], [3.0, 9.0]])
    g = nv.NavGrid("person", -50.0, -50.0, 100, 100, np.zeros((100, 100)), np.ones((100, 100), bool))
    assert np.array_equal(nv.round_corners_with(Q, lambda pairs: [True] * len(pairs)), g.round_corners(Q))
    Q3 = np.c_[Q, [0.0, 1.0, 2.0, 3.0]]
    assert np.array_equal(nv.round_corners_with(Q3, lambda pairs: [True] * len(pairs)), g.round_corners(Q))
    assert np.array_equal(nv.round_corners_with(Q, lambda pairs: [False] * len(pairs)), Q)


def test_heights_walkability_offmesh_and_stage_raster(synth) -> None:
    p = synth.p
    xy = np.array([[-20.0, -20.0], [20.0, 30.0], [0.0, 15.0], [200.0, 0.0], [-20.0, -20.0]])
    nz = p.nav_z_batch(xy)
    assert abs(nz[0] - (-0.4)) < Z_TOL and abs(nz[1] - 0.4) < Z_TOL and np.isnan(nz[2]) and np.isnan(nz[3]) and nz[4] == nz[0]
    z = p.z_at_batch(xy)                                                   # 无导航面时退回网格首个命中
    assert z[2] == 3.0 and np.isnan(z[3]) and z[0] == nz[0] and p.z_at(-20.0, -20.0) == nz[0]
    assert p.walkable_batch(xy).tolist() == [True, True, False, False, True] and p.walkable(-20.0, -20.0)
    d = p.offmesh_dist_batch(np.array([[-20.0, -20.0], [0.0, 10.5], [0.0, 15.0], [200.0, 0.0]]))
    assert d[0] == 0.0 and 0.5 < d[1] < 1.5 and math.isinf(d[2]) and math.isinf(d[3])
    assert p.walkable_batch(np.zeros((0, 2))).shape == (0,) and p.offmesh_dist_batch(np.zeros((0, 2))).shape == (0,)
    W = p.walk
    assert W.shape == (p.ny, p.nx) == (80, 80) and W is p.walk
    assert not W[p.cell_of(0.5, 15.5) // p.nx, p.cell_of(0.5, 15.5) % p.nx] and W.mean() > 0.8
    c = p.cell_of(-19.5, -19.5)
    assert p.center(c) == (-19.5, -19.5) and W.ravel()[c] and abs(p.z.ravel()[c] - 0.02 * -19.5) < Z_TOL
    assert p.cell_of(100.0, 0.0) == -1
    from awr.sim.events.gen import zone_cells

    cells = zone_cells(p, [[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0]], {})
    assert 300 < len(cells) <= 400 and W.ravel()[cells].all()


def test_queries_are_deterministic_across_processes(synth) -> None:
    """同一 NavMesh、同一调用序列：两个舞台（两个 navsvc 进程）给出逐位相同的吸附、路径、高度与栅格视图。"""
    outs = []
    for _ in range(2):
        n = nv.build_stage_nav(_Q(), BBOX, [(0.0, 0.0)], world_dir=synth.world, backend="detour")["person"]
        rng = np.random.default_rng(5)
        pts = rng.uniform(-35.0, 35.0, (40, 2))
        paths = [n.path(a, b) for a, b in itertools.pairwise(pts)]
        outs.append((repr([n.snap("a", *map(float, x)) for x in pts]), [None if q is None else q.tobytes() for q in paths],
                     n.z_at_batch(pts).tobytes(), n.walk.tobytes()))
    assert outs[0] == outs[1]


# ====================================================================== crop：D3-AC-03 统计
need_crop = pytest.mark.skipif(not (CROP / "geometry" / "nav" / "person.json").is_file(), reason=f"dep: crop world package at {CROP}")


def _osm_files() -> list[Path]:
    from awr.world.glb.recipe import load_recipe, verify_raw
    from awr.world.ingest.types import RawDataError

    doc = load_recipe("oryahovo-crop1").doc
    ents = doc.get("osm") or []
    ents = [ents] if isinstance(ents, dict) else list(ents)
    ents += list((doc.get("qa") or {}).get("osm_extra") or [])
    try:
        return [verify_raw(e["path"], e.get("sha256")) for e in ents]
    except (RawDataError, OSError) as e:                                     # 原始 OSM 不在本机
        pytest.skip(f"dep: raw OSM for crop ({e})")


def _veg_polygons(files: list[Path], anchor) -> list[tuple[str, np.ndarray]]:
    """OSM 植被面（世界帧 (E, N) 闭合环）：闭合 way 与 multipolygon 关系中本地可得的闭合 outer 成员（关系成员不全时不拼环）。"""
    from awr.world.georef.frames import lla_to_world

    lla: dict[int, tuple[float, float]] = {}
    rings: list[tuple[str, list[int]]] = []
    for f in files:
        root = ET.fromstring(f.read_bytes())
        for nd in root.iter("node"):
            lla[int(nd.get("id"))] = (float(nd.get("lat")), float(nd.get("lon")))
        way_nodes = {int(w.get("id")): [int(x.get("ref")) for x in w.iter("nd")] for w in root.iter("way")}
        for w in root.iter("way"):
            t = {x.get("k"): x.get("v") for x in w.iter("tag")}
            if t.get("natural") in VEG_NATURAL or t.get("landuse") in VEG_LANDUSE:
                rings.append((t.get("natural") or t.get("landuse"), way_nodes[int(w.get("id"))]))
        for r in root.iter("relation"):
            t = {x.get("k"): x.get("v") for x in r.iter("tag")}
            if t.get("natural") in VEG_NATURAL or t.get("landuse") in VEG_LANDUSE:
                rings += [(t.get("natural") or t.get("landuse"), way_nodes[int(m.get("ref"))]) for m in r.iter("member")
                          if m.get("type") == "way" and m.get("role") == "outer" and int(m.get("ref")) in way_nodes]
    out = []
    for kind, ids in rings:
        if len(ids) >= 4 and ids[0] == ids[-1] and all(i in lla for i in ids):
            ll = np.array([lla[i] for i in ids], np.float64)
            out.append((kind, lla_to_world(ll[:, 0], ll[:, 1], np.zeros(len(ll)), anchor)[:, :2]))
    return out


def _densify(lines: list[np.ndarray], step: float) -> np.ndarray:
    pts = []
    for L in lines:
        for a, b in itertools.pairwise(L):
            k = max(1, math.ceil(float(np.hypot(*(b - a))) / step))
            pts.append(a + (b - a) * (np.arange(k, dtype=np.float64)[:, None] / k))
        pts.append(L[-1:])
    return np.concatenate(pts)


def _record(key: str, stats: dict) -> None:
    out = os.environ.get("AWR_AC03_OUT")
    if not out:
        return
    p = Path(out)
    doc = json.loads(p.read_text()) if p.exists() else {}
    doc[key] = stats
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=1))


@pytest.fixture(scope="module")
def crop(tmp_path_factory: pytest.TempPathFactory) -> SimpleNamespace:
    if not (CROP / "geometry" / "nav" / "person.json").is_file():
        pytest.skip(f"dep: crop world package at {CROP}")
    pytest.importorskip("open3d")
    from awr.sim.events.anchors import point_in_polygon
    from awr.world.georef.types import Anchor
    from awr.world.mesh.query import open_mesh_world_query

    files = _osm_files()
    anchor = Anchor.from_coordinate(json.loads((CROP / "coordinate.json").read_text(encoding="utf-8")))
    hw = nm.parse_highways(files[:1], anchor)
    fp = next(f for f in json.loads((CROP / "semantic" / "quality.geojson").read_text(encoding="utf-8"))["features"]
              if f["properties"]["kind"] == "scan-footprint")
    cover = np.asarray(fp["geometry"]["coordinates"][0], np.float64)[:, :2]
    q = open_mesh_world_query(CROP, tmp_path_factory.mktemp("cache"))
    lo, hi = cover.min(0), cover.max(0)
    navs = nv.build_stage_nav(q, (lo[0], lo[1], hi[0], hi[1]), world_dir=CROP, backend="detour")
    return SimpleNamespace(q=q, navs=navs, hw=hw, files=files, anchor=anchor, cover=cover, pip=point_in_polygon,
                           layers=nv.navmesh_profiles(CROP))


@need_crop
def test_crop_random_pairs_reachable_corridor_and_vegetation(crop) -> None:
    """随机 1,000 对 100% 可达；OSM 道路走廊长度比例 ≥ 70%；经过植被 ≤ 5%（§11 D3-AC-03）。"""
    nav = crop.navs["person"]
    items = [{"op": "random_point", "profile": "person", "seed": (PAIR_SEED + i) & 0xFFFFFFFF} for i in range(2 * N_PAIRS)]
    pts = [r["p"] for r in nav.svc.batch(items)]
    assert all(r is not None for r in pts)
    res = [nav.svc.call("path", profile="person", a=pts[2 * i], b=pts[2 * i + 1]) for i in range(N_PAIRS)]
    complete = sum(1 for r in res if r.get("found") and r.get("complete"))
    mids, ws, pid = [], [], []
    for j, r in enumerate(res):
        P = np.asarray(r["points"], np.float64).reshape(-1, 3) / 1000.0
        for a, b in itertools.pairwise(P):
            seg = float(np.hypot(*(b[:2] - a[:2])))
            k = max(1, math.ceil(seg / STEP_M))
            t = (np.arange(k, dtype=np.float64) + 0.5) / k
            mids.append(a[:2] + (b[:2] - a[:2]) * t[:, None])
            ws.append(np.full(k, seg / k))
            pid.append(np.full(k, j))
    M, w, pid_a = np.concatenate(mids), np.concatenate(ws), np.concatenate(pid)
    cor = nm.corridor_mask(M, crop.hw.lines(nm.PERSON_WAYS), nm.CORRIDOR_HALF_M)
    polys = _veg_polygons(crop.files, crop.anchor)
    U, inv = np.unique(np.floor(M / 0.1 + 0.5).astype(np.int64), axis=0, return_inverse=True)      # 路径大量重叠：0.1 m 量化去重
    U = U * 0.1
    vu = np.zeros(len(U), bool)
    for _k, ring in polys:
        lo, hi = ring.min(0), ring.max(0)
        m = np.flatnonzero((U[:, 0] >= lo[0]) & (U[:, 0] <= hi[0]) & (U[:, 1] >= lo[1]) & (U[:, 1] <= hi[1]) & ~vu)
        if len(m):
            vu[m[crop.pip(ring, U[m, 0], U[m, 1])]] = True
    veg = vu[np.asarray(inv).reshape(-1)]
    sem = getattr(crop.q, "semantic", None)
    veg_sem = 0
    if sem is not None:                                                     # 网格语义流（cls）：自上而下首个命中三角形的类别
        keys = [c["key"] for c in json.loads((CROP / "semantic" / "anet-classes@1.json").read_text())["classes"]]
        _z, prim = crop.q.walkable_surface(M, np.inf)
        cls = np.where(prim >= 0, sem[np.maximum(prim, 0)], 255)
        vm = np.isin(cls, [i for i, k in enumerate(keys) if k in VEG_CLASSES])
        veg_sem = int(vm.sum())
        veg |= vm
    L = float(w.sum())
    per = np.bincount(pid_a, w * cor, minlength=N_PAIRS) / np.maximum(np.bincount(pid_a, w, minlength=N_PAIRS), 1e-12)
    stats = {"pairs": N_PAIRS, "seed0": PAIR_SEED, "complete": complete, "rounds_max": max(r.get("rounds", 0) for r in res),
             "length_km": round(L / 1000.0, 3), "corridor_frac": round(float(w[cor].sum() / L), 4),
             "corridor_frac_per_path_p50": round(float(np.median(per)), 4), "vegetation_frac": round(float(w[veg].sum() / L), 4),
             "vegetation_sources": {"osm_polygons": len(polys), "mesh_semantic": sem is not None, "mesh_semantic_samples": veg_sem},
             "navmesh_sha256": nav.sha256}
    _record("random_pairs", stats)
    print(json.dumps(stats, ensure_ascii=False))
    assert complete == N_PAIRS, stats
    assert stats["corridor_frac"] >= 0.70, stats
    assert stats["vegetation_frac"] <= 0.05, stats


@pytest.fixture(scope="module")
def anchor_snaps(crop) -> SimpleNamespace:
    """点状锚点候选（覆盖范围内）与 M22 §6.6.11 吸附结果。"""
    from scipy.spatial import cKDTree

    from awr.world.georef.frames import lla_to_world

    cands: list[tuple[str, str, str, str, np.ndarray]] = [("junction", "point", "person", f"j-{n}", p)
                                                          for n, p in crop.hw.junctions(nm.PERSON_WAYS)]
    poi = []
    for nd in ET.fromstring(crop.files[0].read_bytes()).iter("node"):
        t = {x.get("k"): x.get("v") for x in nd.iter("tag")}
        if any(k in t for k in POI_KEYS):
            poi.append((int(nd.get("id")), float(nd.get("lat")), float(nd.get("lon"))))
    if poi:
        ll = np.array([(a, b) for _i, a, b in poi], np.float64)
        en = lla_to_world(ll[:, 0], ll[:, 1], np.zeros(len(ll)), crop.anchor)[:, :2]
        cands += [("poi", "point", "person", f"n-{i}", en[k]) for k, (i, _a, _b) in enumerate(poi)]
    for a in json.loads((EVENT_DIR / "anchors.json").read_text(encoding="utf-8"))["anchors"]:
        if a["kind"] != "zone":                                               # 建筑与区域多边形不计
            cands.append(("event", a["kind"], a.get("profile", "person"), a["id"], np.asarray(a["xy"], np.float64)))
    xy = np.array([c[4] for c in cands])
    inside = crop.pip(crop.cover, xy[:, 0], xy[:, 1])
    D = _densify(crop.hw.lines(nm.PERSON_WAYS), RELOC_STEP_M)
    tree = cKDTree(D)
    rows = []
    for (src, kind, prof, aid, p), ok in zip(cands, inside, strict=True):
        if not ok:
            continue
        nav = crop.navs[prof]
        s = nav.snap(aid, float(p[0]), float(p[1]), max_m=SNAP_EXT_M)
        d, moved = s.dist_m, 0.0
        if kind == "point" and d > 3.0:                                       # 沿 OSM 道路重定位（就近优先，接受 ≤ 1 m）
            idx = sorted(tree.query_ball_point(p, RELOC_SPAN_M), key=lambda j: (float(np.hypot(*(D[j] - p))), j))
            for j in idx:
                s2 = nav.snap(aid, float(D[j, 0]), float(D[j, 1]), max_m=SNAP_EXT_M)
                if s2.dist_m <= RELOC_ACCEPT_M:
                    d, moved = s2.dist_m, float(np.hypot(*(D[j] - p)))
                    break
        rows.append({"source": src, "id": aid, "profile": prof, "raw_m": s.dist_m, "dist_m": d, "relocated_m": moved})
    dist = np.array([r["dist_m"] for r in rows])
    by = {k: {"n": sum(1 for r in rows if r["source"] == k),
              "median_m": float(np.median([r["dist_m"] for r in rows if r["source"] == k])),
              "far_frac": float(np.mean([r["dist_m"] > 3.0 for r in rows if r["source"] == k]))} for k in ("junction", "poi", "event")}
    no_nav = [r["id"] for r in rows if math.isinf(r["dist_m"])]
    st = {"n": len(rows), "candidates": len(cands), "median_m": float(np.median(dist)), "far_frac": float(np.mean(dist > 3.0)),
          "relocated": sum(1 for r in rows if r["relocated_m"] > 0), "no_navmesh_within_15m": len(no_nav), "by_source": by,
          "far": [r for r in rows if r["dist_m"] > 3.0]}
    kept = dist[np.isfinite(dist)]
    st["kept_island_only"] = {"n": len(kept), "median_m": float(np.median(kept)), "far_frac": float(np.mean(kept > 3.0))}
    _record("anchor_snap", st)
    print(json.dumps({k: v for k, v in st.items() if k != "far"}, ensure_ascii=False))
    return SimpleNamespace(rows=rows, stats=st)


@need_crop
def test_crop_anchor_snap_median(anchor_snaps) -> None:
    """点状锚点 navSnap 中位距离 ≤ 1 m（§11 D3-AC-03）；示例事件锚点全部 ≤ 3 m。"""
    st = anchor_snaps.stats
    assert st["n"] >= 200 and st["median_m"] <= 1.0, st
    assert all(r["dist_m"] <= 3.0 for r in anchor_snaps.rows if r["source"] == "event")
    assert st["kept_island_only"]["far_frac"] <= 0.05, st["kept_island_only"]


@need_crop
@pytest.mark.xfail(strict=True, reason="D3-AC-03 未过项（待用户决定）：crop 7d3429a04895 行人 NavMesh 按 keep = largest-seeded 只保留主岛（W07），"
                                     "> 3 m 比例 22/231 ≈ 9.5%（只计保留岛 6/215 ≈ 2.8%）；保留全部含种子岛时为 6/231 ≈ 2.6% 但随机 1,000 对只有 829 对可达，"
                                     "两条款在本世界互相冲突；唯一有依据的 off-mesh 连接（OSM way 200320976 上 3 m 断口，并入西侧 55,923 m² 岛）"
                                     "实验烘焙后为 13/231 ≈ 5.6%，仍未达标；决定并重建 crop 后删除本标记")
def test_crop_anchor_snap_far_fraction(anchor_snaps) -> None:
    """点状锚点 navSnap > 3 m 的比例 ≤ 5%（允许沿 OSM 道路自动重定位）（§11 D3-AC-03，全量口径）。

    未过项的构成（W12 复核，2026-10-19 实测）：保留岛上 6 个（5 个 POI 位于建筑或遮蔽结构内，吸附 3.5–4.4 m，15 m 内无 OSM 道路可供重定位；
    1 个路口距扫描覆盖边缘 2.1 m）；被裁剪岛上 16 个：西侧 55,923 m² 岛 9 个（与主岛之间只有上述 3 m 断口），其余 7 个位于扫描边缘的山坡孤岛，
    沿 OSM 道路与主岛之间为 35 m 以上的无网格或不可走地形，不以 off-mesh 连接虚构连通。
    """
    assert anchor_snaps.stats["far_frac"] <= 0.05, {k: v for k, v in anchor_snaps.stats.items() if k != "far"}


@need_crop
def test_crop_vehicle_navmesh_and_example_event_vehicle_legs(crop, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """车辆 profile NavMesh 存在（保留多边形与面积 > 0）且示例事件的车辆段可达：车辆路点在车辆 NavMesh 上吸附 ≤ 3 m、相邻路点间
    Detour 走廊完整；门 2 预检（Detour 后端；车辆按事件给出的方式行驶——车道图或运动学）车辆段全部连通且可行。"""
    import yaml

    from awr.sim.events.compile import CompileOptions, compile_event

    vd = crop.layers["vehicle"]
    assert vd["polys"]["kept"] > 0 and vd["islands"]["walkable_area_m2"] > 1.0e5 and crop.navs["vehicle"].sha256 == vd["bin"]["sha256"]
    ev = yaml.safe_load((EVENT_DIR / "event.yaml").read_text(encoding="utf-8"))
    anchors = {a["id"]: a for a in json.loads((EVENT_DIR / "anchors.json").read_text(encoding="utf-8"))["anchors"]}
    vnav = crop.navs["vehicle"]
    legs = []
    for act in (a for a in ev["actors"] if a["class"] == "vehicle"):
        snaps = [vnav.snap(w["at"], *map(float, anchors[w["at"]]["xy"])) for w in act["route"]]
        assert all(s.status == "ok" for s in snaps), snaps
        for s0, s1 in itertools.pairwise(snaps):
            P = vnav.path(s0.p, s1.p)
            assert P is not None, (act["id"], s0.anchor, s1.anchor)
            legs.append({"actor": act["id"], "from": s0.anchor, "to": s1.anchor, "len_m": round(nv.rounded_length(P), 3)})
    assert legs
    monkeypatch.setenv("AWR_EVENT_NAV", "detour")
    r = compile_event(EVENT_DIR / "event.yaml", CROP, opts=CompileOptions(preflight_only=True, cache_dir=tmp_path / "cache"))
    veh = [s for s in r.segments if s["actor"] in {a["actor"] for a in legs}]
    assert veh and all(math.isfinite(s["len_m"]) and s["why"] != "disconnected" and s["feasible"] for s in veh), veh
    assert all(math.isfinite(s["len_m"]) for s in r.segments)               # 行人段全部连通（可行性另由排时决定）
    _record("example_event", {"status": r.status, "vehicle_navmesh_legs": legs, "segments": r.segments, "retime": r.retime})


@need_crop
def test_crop_compile_records_detour_navmesh(crop, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Detour 后端编译（ADR-196 (4)）：磁带头 navmesh_sha256 = 图层 bin.sha256，provenance 记录 recast-navigation、WASM 与 Node 版本；
    门 3 `S-OFFMESH` 以 Detour 离面距离实际判定；背景演员经舞台栅格视图抽样、沿 Detour 路径行走。"""
    import yaml

    from awr.sim.events import tape as tp
    from awr.sim.events.compile import CompileOptions, compile_event

    ev = yaml.safe_load((EVENT_DIR / "event.yaml").read_text(encoding="utf-8"))
    small = {k: copy.deepcopy(ev[k]) for k in ("schema", "world", "anchors_ref", "seed", "environment")}
    small.update(event_id="w12-detour-smoke", version=1, duration_s=120,
                 actors=[{"id": "walker", "class": "person", "route": [{"t": 0, "at": "ev_market_exit", "do": "spawn"},
                                                                       {"t": 110, "at": "ev_north_street"}]},
                         {"id": "car", "class": "vehicle", "route": [{"t": 0, "at": "ev_road_east", "do": "spawn"},
                                                                     {"t": 100, "at": "ev_plaza_curb_road", "do": "despawn"}]}],
                 background=[{"class": "person", "count": 6, "zone": "ev_town_center", "pause_s": [0, 10]}])
    small["environment"]["keyframes"] = [k for k in small["environment"]["keyframes"] if k["t"] <= 110]
    d = tmp_path / "ev"
    d.mkdir()
    (d / "anchors.json").write_text((EVENT_DIR / "anchors.json").read_text(encoding="utf-8"))
    (d / "event.json").write_text(json.dumps(small))
    monkeypatch.setenv("AWR_EVENT_NAV", "detour")
    r = compile_event(d / "event.json", CROP, opts=CompileOptions(out=d / "v1", cache_dir=tmp_path / "cache"))
    assert r.status == "compiled", json.dumps(r.as_dict(), ensure_ascii=False)[:3000]
    man = json.loads((r.out / "tape.json").read_text(encoding="utf-8"))
    hd = tp.open_tape(r.out).header
    assert hd["world"]["navmesh_sha256"] == {p: crop.layers[p]["bin"]["sha256"] for p in ("person", "vehicle")}
    pv = man["provenance"]
    assert pv["generator_version"].endswith("-detour") and pv["recast_navigation"] == nm.recast_version()
    assert pv["wasm_sha256"] == nm.wasm_sha256() and pv["node"].startswith("v22.")
    truth = json.loads(next((r.out / "truth").rglob("event_truth.json")).read_text(encoding="utf-8"))
    off = truth["consistency"]["S-OFFMESH"]
    assert off["available"] is True and off["n_frames"] > 0 and off["n_fail"] == 0
    f = np.concatenate([tp.open_tape(r.out).block_frames(i) for i in range(len(tp.open_tape(r.out).blocks))])
    assert (f["state"][:, 2:] == 1).mean() > 0.3                            # 背景演员在走


@need_crop
def test_crop_navmesh_sha_and_byte_identical_replies(crop) -> None:
    """本机：图层清单、bin 文件与 navsvc hello 的 sha256 一致；两个 navsvc 进程对同一请求序列（200 条随机路径）应答逐字节相同。"""
    hello = crop.navs["person"].svc.info["profiles"]
    for prof in ("person", "vehicle"):
        doc = crop.layers[prof]
        b = (CROP / nm.HREF_DIR / doc["bin"]["href"]).read_bytes()
        assert hashlib.sha256(b).hexdigest() == doc["bin"]["sha256"] == hello[prof] and doc["wasm_sha256"] == nm.wasm_sha256()
    seq = [("random_point", {"profile": "person", "seed": PAIR_SEED + i}) for i in range(400)]
    raws = []
    for _ in range(2):
        with nm.NavSvc(world=CROP) as ns:
            pts = [json.loads(ns.call_raw(op, **kw))["p"] for op, kw in seq]
            raws.append(b"".join(ns.call_raw("path", profile="person", a=pts[2 * i], b=pts[2 * i + 1]) for i in range(200)))
    assert raws[0] == raws[1]


@need_crop
@pytest.mark.skipif(not os.environ.get("AWR_NAVMESH_PEER_SHA256"), reason="cross-machine: set AWR_NAVMESH_PEER_SHA256 from the X01 peer bake")
def test_crop_navmesh_sha_matches_peer_machine(crop) -> None:
    """两台机器烘焙的 NavMesh sha256 相同（§11 D3-AC-03）：对端（dmax AVX-512）烘焙结果由 X01 给出。"""
    peer = dict(kv.split("=", 1) for kv in os.environ["AWR_NAVMESH_PEER_SHA256"].split(","))
    assert {p: crop.layers[p]["bin"]["sha256"] for p in peer} == peer
