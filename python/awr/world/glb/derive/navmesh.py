"""行人与车辆 NavMesh（M22 §6.2 第 8 阶段、§6.6.11、§6.8、§7.3.3；M22-FR-032、FR-033；ADR-132、ADR-196）与 navsvc 客户端。

烘焙：求交网格（写出后的 `awr-trimesh@1`，与校验器同一份几何）→ 逐三角形 Detour 区域号（缺省 GROUND；`cls.u8` 语义：路面 PAVED、
植被 VEGETATION、立面 STEEP、屋顶与水面 NULL；`q.u8` 质量位 nodata、hole、water 置 NULL，stretched 只对非近水平面（n_z < 0.9）置 NULL
（W07：crop 上按边长判定的拉伸面 95% 为近水平的大三角形——道路、广场等平坦区的抽稀面，全部置 NULL 使行人主岛由 0.73 km² 碎成
0.10 km²、车辆主岛由 0.15 km² 碎成 0.006 km²）；距 OSM 道路折线 ≤ 5 m 的
非 NULL 三角形改为 ROAD_CORRIDOR）→ Node 烘焙工具 `tools/navsvc/src/bake.mjs`（recast-navigation 0.43.1 WASM，低层 API 分瓦片、
逐三角形 area id，worker_threads 并行，瓦片按 (ty, tx) 装配）→ 种子吸附（OSM 路口 `j-<node>` 与配方 `nav_seeds.json`，初次搜索
半宽 (2, 4, 2) m）与洪泛裁剪（含种子的岛保留，其余多边形 flags = 0；配方 `build.nav.<profile>.keep = largest-seeded` 时只保留
其中面积最大者）→ `geometry/nav/<profile>.{json,bin}`（`detour-navmesh@1`）。无任何种子来源（无 OSM 道路、无 `nav_seeds.json`）
时保留最大岛并登记种子 `auto-largest-island`；有候选种子但全部吸附失败时 `seeds[]` 为空，由 V-GLB-05 报错。

确定性（D3-AC-03、M22-NFR-004）：Python 侧全部为显式次序的 float64 运算后量化为 float32 / 整数毫米；WASM 为单线程 MVP 二进制
（不含 SIMD 与 relaxed SIMD），其 sha256 由 `tools/navsvc/WASM_SHA256` 锁定并写入图层清单；瓦片结果与线程数、调度次序无关。图层
清单只含确定性字段（耗时、线程数、RSS 与 Node 版本写入 `qa/report.json` 的 `glb.navmesh`），`bin.sha256` 随 `files[]` 进入
`contentVersion`。

查询：`NavSvc` 以子进程启动 `tools/navsvc/src/navsvc.mjs`（每个世界一个常驻进程），行分隔 JSON，坐标为世界帧整数毫米（`snap`、
`path`、`los`、`random_point_around`、`random_point`、`height`）；事件编译器的 Detour 后端（`awr.sim.events.nav`）与 geosvc 的 `nav_z` 经此调用。
"""

from __future__ import annotations

import itertools
import json
import math
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import lla_to_world
from awr.world.georef.types import Anchor
from awr.world.ingest.types import ConfigError
from awr.world.package.derivers import LayerSpec
from awr.world.package.jsonio import write_json

__all__ = ["AGENTS", "AREAS", "BAKE_JS", "CELLS", "FLAGS", "HREF_DIR", "NAVSVC_DIR", "NAVSVC_JS", "RECAST_VERSION_FILE", "Highways",
           "NavSvc", "NavSvcError", "NavmeshResult", "agent_for", "bake_profile", "corridor_mask", "junction_seeds", "lowest_hit",
           "nav_profiles", "navsvc_modules", "node_bin", "parse_highways", "recast_params", "recast_version", "to_mm", "triangle_areas", "wasm_sha256",
           "write_navmesh"]

ROOT = Path(__file__).resolve().parents[5]
NAVSVC_DIR = ROOT / "tools" / "navsvc"
BAKE_JS = NAVSVC_DIR / "src" / "bake.mjs"
NAVSVC_JS = NAVSVC_DIR / "src" / "navsvc.mjs"
RECAST_VERSION_FILE = ROOT / "tools" / "worldglb" / "RECAST_VERSION"
WASM_SHA256_FILE = NAVSVC_DIR / "WASM_SHA256"
HREF_DIR = "geometry/nav/"
SCHEMA = "awr.navmesh.v1"
SCHEMA_VERSION = "1.0.0"

# Detour 区域与代价（M22 §6.8；与 tools/navsvc/src/core.mjs AREAS 一致）；0 为 Recast NULL_AREA（不可走）
NULL, ROAD_CORRIDOR, PAVED, GROUND, VEGETATION, STEEP, OFFMESH = 0, 1, 2, 3, 4, 5, 6
AREAS = ({"id": 1, "name": "ROAD_CORRIDOR", "cost": 1.0, "walkable": True}, {"id": 2, "name": "PAVED", "cost": 1.5, "walkable": True},
         {"id": 3, "name": "GROUND", "cost": 3.0, "walkable": True}, {"id": 4, "name": "VEGETATION", "cost": 8.0, "walkable": True},
         {"id": 5, "name": "STEEP", "cost": 6.0, "walkable": True}, {"id": 6, "name": "OFFMESH", "cost": 1.0, "walkable": True},
         {"id": 0, "name": "NULL", "cost": None, "walkable": False})
FLAGS = {"walk": 1, "drive": 2, "door": 4, "disabled": 8}
PROFILE_FLAG = {"person": FLAGS["walk"], "vehicle": FLAGS["drive"]}
# 代理参数（§6.8；V-GLB-14 以同一缺省与配方 build.nav.<profile>.agent 比对）
AGENTS = {"person": {"radius_m": 0.3, "height_m": 1.8, "max_climb_m": 0.4, "max_slope_deg": 35.0},
          "vehicle": {"radius_m": 1.2, "height_m": 2.0, "max_climb_m": 0.3, "max_slope_deg": 20.0}}
CELLS = {"person": (0.30, 0.20), "vehicle": (0.50, 0.20)}         # cs、ch 缺省（城镇级；town 0.50/0.25、hill 0.15/0.10 由配方给出）
TILE_CELLS = 128
DETAIL_SAMPLE_DIST = 3.0          # 格（× cs）
DETAIL_SAMPLE_MAX_ERROR = 0.4     # 格（× ch）
MAX_EDGE_M = 12.0
MAX_SIMPLIFICATION_ERROR = 1.3
MIN_REGION_CELLS = 8              # 边长（Recast 内部取平方）
MERGE_REGION_CELLS = 20
MAX_VERTS_PER_POLY = 6
CORRIDOR_HALF_M = 5.0             # OSM 道路走廊半宽（§6.8）
CORRIDOR_STEP_M = 0.2             # 折线加密步长：最近加密点距离与真距离之差 ≤ 0.1 m
SEED_EXT_MM = (2000, 4000, 2000)  # 初次吸附搜索半宽（Recast x、y 竖直、z）
NULL_QBITS = 1 | 2 | 4 | 32       # nodata、hole、stretched、water（§7.3.6）
Q_STRETCHED = 4
STRETCHED_FLAT_NZ = 0.9           # 近水平（坡度 ≤ 25.8°）的拉伸面仍可行走：平坦区抽稀产生的大三角形不是插值竖帘（W07 crop 实测）
# OSM highway 取值：行人走廊（walkable lines）与车辆道路
PERSON_WAYS = frozenset({"footway", "path", "pedestrian", "steps", "living_street", "residential", "service", "unclassified", "road",
                         "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link", "trunk", "trunk_link",
                         "track", "cycleway", "corridor", "bridleway"})
VEHICLE_WAYS = frozenset({"motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
                          "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service", "road"})
CLASS_AREA = {"road_surface": PAVED, "low_vegetation": VEGETATION, "medium_vegetation": VEGETATION, "high_vegetation": VEGETATION,
              "building_facade": STEEP, "building_roof": NULL, "water": NULL}
BAKE_TIMEOUT_S = 3600
# 无任何种子来源（配方无 OSM 道路、无 nav_seeds.json，如合成夹具）时保留最大岛并以此 id 登记；有候选种子但全部吸附失败时
# seeds[] 为空，由 V-GLB-05 报错（真实世界的 OSM 或配准问题不被掩盖）
AUTO_SEED = "auto-largest-island"
KEEP_MODES = ("seeded", "largest-seeded")        # 裁剪：保留全部含种子岛（§6.6.11 缺省）或只保留其中面积最大者


# ---------------------------------------------------------------- 版本与运行时


def recast_version() -> str:
    return RECAST_VERSION_FILE.read_text(encoding="utf-8").strip()


def wasm_sha256() -> str:
    return WASM_SHA256_FILE.read_text(encoding="utf-8").strip()


def navsvc_modules() -> Path:
    """navsvc 实际装载的 `node_modules` 目录：按 Node ESM 裸模块解析的次序，自 `tools/navsvc/src` 起逐级向上查找含
    `@recast-navigation/core` 的 `node_modules`（K06：dmax 编译镜像把依赖装在 `/node_modules`，代码以卷挂载在任意目录时同样可解析；
    `tools/navsvc/src/recast.mjs` 以 `import.meta.resolve` 取同一目录做版本与 WASM 核对）。均不存在时返回
    `tools/navsvc/node_modules`（调用方据 `is_dir()` 判定未安装）。"""
    for d in (NAVSVC_DIR / "src", *(NAVSVC_DIR / "src").parents):
        m = d / "node_modules"
        if (m / "@recast-navigation" / "core").is_dir():
            return m
    return NAVSVC_DIR / "node_modules"


def node_bin(node: str | None = None) -> str:
    """Node 可执行文件：参数、`AWR_NODE` 或 PATH 中的 `node`；navsvc 依赖未安装时报配置错误（`npm ci --prefix tools/navsvc`）。"""
    exe = node or os.environ.get("AWR_NODE") or shutil.which("node")
    if not exe:
        raise ConfigError("node not found (set AWR_NODE or install Node 22); required by the navmesh stage and navsvc")
    if not (navsvc_modules() / "@recast-navigation" / "core").is_dir():
        raise ConfigError(f"recast-navigation not installed in {NAVSVC_DIR} (run: npm ci --prefix tools/navsvc)")
    return exe


def to_mm(p: Sequence[float]) -> list[int]:
    """世界帧米 → 整数毫米（四舍五入，0.5 向 +∞；与 navsvc 的 Math.round 同一规则）。"""
    return [math.floor(float(v) * 1000.0 + 0.5) for v in p]


# ---------------------------------------------------------------- OSM 道路


@dataclass
class Highways:
    """OSM highway 折线（世界帧 (E, N)）与路口（≥ 2 条道路共享的节点）。"""

    ways: list[tuple[int, str, np.ndarray]] = field(default_factory=list)     # (way id, highway 值, (k, 2) E N)
    nodes: dict[int, np.ndarray] = field(default_factory=dict)                 # node id → (E, N)
    node_ways: dict[int, set[int]] = field(default_factory=dict)               # node id → 引用它的 way id

    def lines(self, kinds: frozenset[str]) -> list[np.ndarray]:
        return [xy for _w, k, xy in self.ways if k in kinds and len(xy) >= 2]

    def junctions(self, kinds: frozenset[str]) -> list[tuple[int, np.ndarray]]:
        """`kinds` 道路中被 ≥ 2 条不同 way 引用的节点，按节点 id 升序。"""
        ok = {w for w, k, _xy in self.ways if k in kinds}
        return [(n, self.nodes[n]) for n in sorted(self.node_ways) if len(self.node_ways[n] & ok) >= 2 and n in self.nodes]


def parse_highways(paths: str | Path | Sequence[str | Path], anchor: Anchor) -> Highways:
    """读取 OSM XML 的 `highway=*` way（按 way id 去重、升序），节点经纬度经 M02 `lla_to_world`（h = 0）转世界帧 (E, N)。"""
    plist = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
    lla: dict[int, tuple[float, float]] = {}
    ways: dict[int, tuple[list[int], str]] = {}
    for p in plist:
        root = ET.fromstring(p.read_bytes())
        for nd in root.iter("node"):
            lla[int(nd.get("id"))] = (float(nd.get("lat")), float(nd.get("lon")))
        for w in root.iter("way"):
            tags = {t.get("k"): t.get("v") for t in w.iter("tag")}
            if tags.get("highway"):
                ways[int(w.get("id"))] = ([int(x.get("ref")) for x in w.iter("nd")], str(tags["highway"]))
    used = sorted({n for ids, _k in ways.values() for n in ids if n in lla})
    out = Highways()
    if not used:
        return out
    ll = np.array([lla[n] for n in used], np.float64)
    en = lla_to_world(ll[:, 0], ll[:, 1], np.zeros(len(ll)), anchor)[:, :2]
    out.nodes = {n: en[i] for i, n in enumerate(used)}
    for wid in sorted(ways):
        ids, kind = ways[wid]
        ids = [n for n in ids if n in out.nodes]
        if len(ids) < 2:
            continue
        out.ways.append((wid, kind, np.array([out.nodes[n] for n in ids], np.float64)))
        for n in ids:
            out.node_ways.setdefault(n, set()).add(wid)
    return out


def corridor_mask(xy: np.ndarray, lines: Sequence[np.ndarray], half_width_m: float = CORRIDOR_HALF_M,
                  step_m: float = CORRIDOR_STEP_M) -> np.ndarray:
    """点 `xy` (n, 2) 到折线集合的水平距离 ≤ 半宽：折线按 ≤ step 加密后以 KD 树求最近点距离（误差 ≤ step / 2）。"""
    from scipy.spatial import cKDTree

    pts = []
    for L in lines:
        for a, b in itertools.pairwise(L):
            k = max(1, math.ceil(float(np.hypot(*(b - a))) / step_m))
            t = np.arange(k, dtype=np.float64)[:, None] / k
            pts.append(a + (b - a) * t)
        pts.append(L[-1:])
    if not pts or len(xy) == 0:
        return np.zeros(len(xy), bool)
    tree = cKDTree(np.concatenate(pts))
    d, _ = tree.query(xy, k=1, distance_upper_bound=half_width_m + 1e-9)
    return d <= half_width_m


def triangle_areas(V: np.ndarray, F: np.ndarray, *, corridor_lines: Sequence[np.ndarray] = (), tri_cls: np.ndarray | None = None,
                   class_keys: Sequence[str] | None = None, tri_q: np.ndarray | None = None) -> tuple[np.ndarray, dict]:
    """逐三角形 Detour 区域号（§6.6.11 伪代码；坡度由 Recast markWalkableTriangles 另行判定）。返回 (uint8 区域号, 各区域三角形数)。"""
    area = np.full(len(F), GROUND, np.uint8)
    if tri_cls is not None and class_keys is not None:
        lut = np.full(256, GROUND, np.uint8)
        for i, k in enumerate(class_keys):
            lut[i] = CLASS_AREA.get(k, GROUND)
        area = lut[np.asarray(tri_cls, np.uint8)]
    if tri_q is not None:
        qq = np.asarray(tri_q, np.uint8)
        null = (qq & (NULL_QBITS & ~Q_STRETCHED)) != 0
        st = np.flatnonzero(((qq & Q_STRETCHED) != 0) & ~null)
        if st.size:
            P = np.asarray(V, np.float64)[np.asarray(F, np.int64)[st]]
            n = np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0])
            nz = np.abs(n[:, 2]) / np.maximum(np.linalg.norm(n, axis=1), 1e-300)
            null[st[nz < STRETCHED_FLAT_NZ]] = True
        area[null] = NULL
    if corridor_lines:
        cen = (V[F[:, 0], :2] + V[F[:, 1], :2] + V[F[:, 2], :2]) / 3.0
        area[(area != NULL) & corridor_mask(cen, corridor_lines)] = ROAD_CORRIDOR
    names = {a["id"]: a["name"] for a in AREAS}
    hist = np.bincount(area, minlength=7)
    return area, {names[i]: int(hist[i]) for i in range(7) if hist[i]}


def lowest_hit(V: np.ndarray, F: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """竖直线与网格的最低交点高度（无交点为 NaN）：种子吸附的高度初值（路口处取地面而非树冠，§6.6.11 吸附半宽竖直 4 m）。"""
    A, B, C = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    lo = np.minimum(np.minimum(A[:, :2], B[:, :2]), C[:, :2])
    hi = np.maximum(np.maximum(A[:, :2], B[:, :2]), C[:, :2])
    out = np.full(len(xy), np.nan)
    for i, (x, y) in enumerate(np.asarray(xy, np.float64)):
        m = np.flatnonzero((lo[:, 0] <= x) & (hi[:, 0] >= x) & (lo[:, 1] <= y) & (hi[:, 1] >= y))
        if len(m) == 0:
            continue
        a, b, c = A[m], B[m], C[m]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(d) > 1e-12
        if not ok.any():
            continue
        a, b, c, d = a[ok], b[ok], c[ok], d[ok]
        l1 = ((b[:, 1] - c[:, 1]) * (x - c[:, 0]) + (c[:, 0] - b[:, 0]) * (y - c[:, 1])) / d
        l2 = ((c[:, 1] - a[:, 1]) * (x - c[:, 0]) + (a[:, 0] - c[:, 0]) * (y - c[:, 1])) / d
        l3 = 1.0 - l1 - l2
        inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
        if inside.any():
            z = l1 * a[:, 2] + l2 * b[:, 2] + l3 * c[:, 2]
            out[i] = float(z[inside].min())
    return out


def junction_seeds(hw: Highways, kinds: frozenset[str], V: np.ndarray, F: np.ndarray) -> list[dict]:
    """OSM 路口种子 `j-<node>`：网格覆盖内（竖直线有交点）者，高度取最低交点。"""
    js = hw.junctions(kinds)
    if not js:
        return []
    xy = np.array([p for _n, p in js], np.float64)
    z = lowest_hit(V, F, xy)
    return [{"id": f"j-{n}", "p_mm": to_mm((xy[i, 0], xy[i, 1], z[i]))} for i, (n, _p) in enumerate(js) if np.isfinite(z[i])]


# ---------------------------------------------------------------- 参数


def _cells(x: float, c: float, up: bool) -> int:
    """米 → 格数（ceil 或 floor），先消去浮点除法的末位误差（1.8 / 0.2 = 9.000000000000002 取 9）。"""
    q = x / c
    r = round(q)
    if abs(q - r) < 1e-9:
        q = float(r)
    return int(math.ceil(q) if up else math.floor(q))


def nav_profiles(recipe_doc: Mapping[str, Any]) -> list[str]:
    """烘焙的 profile：行人总是生成；车辆在配方给出 `build.nav.vehicle`（未置 `enabled: false`）或角色为 `event-main` 时生成
    （M22-FR-033：crop 必交、town 缺省不生成）；`build.profile = fast` 跳过车辆（FR-019）。"""
    build = recipe_doc.get("build") or {}
    nav = build.get("nav") or {}
    out = ["person"] if (nav.get("person") or {}).get("enabled", True) else []
    v = nav.get("vehicle")
    want_v = (v is not None and v.get("enabled", True)) or (v is None and recipe_doc.get("role") == "event-main")
    if want_v and build.get("profile", "full") != "fast":
        out.append("vehicle")
    return out


def agent_for(profile: str, recipe_doc: Mapping[str, Any]) -> dict[str, float]:
    """代理参数：§6.8 缺省叠加配方 `build.nav.<profile>.agent`（V-GLB-14 同一口径）。"""
    nav = ((recipe_doc.get("build") or {}).get("nav") or {}).get(profile) or {}
    return {k: float(v) for k, v in {**AGENTS[profile], **(nav.get("agent") or {})}.items()}


def recast_params(profile: str, recipe_doc: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    """Recast 配置（格单位整数在此换算，Node 侧原样使用）与代理参数。`fast` 档栅格放大一倍（FR-019）。"""
    build = recipe_doc.get("build") or {}
    nav = (build.get("nav") or {}).get(profile) or {}
    cs0, ch0 = CELLS[profile]
    cs, ch = float(nav.get("cs", cs0)), float(nav.get("ch", ch0))
    if build.get("profile", "full") == "fast":
        cs, ch = 2 * cs, 2 * ch
    a = agent_for(profile, recipe_doc)
    rc = {"cs": cs, "ch": ch, "tileSize": int(nav.get("tile_size_cells", TILE_CELLS)), "walkableSlopeAngle": a["max_slope_deg"],
          "walkableHeight": _cells(a["height_m"], ch, True), "walkableClimb": _cells(a["max_climb_m"], ch, False),
          "walkableRadius": _cells(a["radius_m"], cs, True), "maxEdgeLen": round(MAX_EDGE_M / cs),
          "maxSimplificationError": MAX_SIMPLIFICATION_ERROR, "minRegionArea": MIN_REGION_CELLS, "mergeRegionArea": MERGE_REGION_CELLS,
          "maxVertsPerPoly": MAX_VERTS_PER_POLY, "detailSampleDist": float(nav.get("detail_sample_dist", DETAIL_SAMPLE_DIST)),
          "detailSampleMaxError": float(nav.get("detail_sample_max_error", DETAIL_SAMPLE_MAX_ERROR))}
    return rc, a


def _recipe_json(recipe_dir: Path | None, name: str) -> dict:
    p = None if recipe_dir is None else Path(recipe_dir) / name
    return json.loads(p.read_text(encoding="utf-8")) if p is not None and p.is_file() else {}


# ---------------------------------------------------------------- 烘焙


def bake_profile(work: Path, out_bin: Path, *, profile: str, verts_recast: np.ndarray, tris: np.ndarray, areas: np.ndarray,
                 recast: Mapping[str, Any], seeds: Sequence[Mapping[str, Any]], offmesh: Sequence[Mapping[str, Any]] = (),
                 keep: str = "seeded", threads: int = 1, node: str | None = None) -> dict:
    """写出 Node 烘焙输入（`work/`）并运行 `bake.mjs`；返回其统计 JSON（失败抛 RuntimeError）。"""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    np.ascontiguousarray(verts_recast, "<f4").tofile(work / "verts.f32")
    np.ascontiguousarray(tris, "<u4").tofile(work / "tris.u32")
    np.ascontiguousarray(areas, "u1").tofile(work / "areas.u8")
    cfg = {"profile": profile, "recast": dict(recast), "poly_flag": PROFILE_FLAG[profile], "seeds": list(seeds),
           "seed_ext_mm": list(SEED_EXT_MM), "offmesh": list(offmesh), "keep": keep}
    (work / "config.json").write_text(json.dumps(cfg, sort_keys=True), encoding="utf-8")
    out_bin.parent.mkdir(parents=True, exist_ok=True)
    cmd = [node_bin(node), str(BAKE_JS), "--in", str(work), "--out", str(out_bin), "--threads", str(max(1, int(threads)))]
    r = subprocess.run(cmd, capture_output=True, timeout=BAKE_TIMEOUT_S, check=False)
    lines = [ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    try:
        res = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        res = {}
    if r.returncode != 0 or not res.get("ok"):
        raise RuntimeError(f"navmesh bake ({profile}) failed: {res.get('error') or r.stderr.decode('utf-8', 'replace')[-2000:]}")
    return res


@dataclass
class NavmeshResult:
    layers: list[LayerSpec]
    docs: dict[str, dict]           # profile → 图层清单
    qa: dict[str, Any]              # qa/report.json `glb.navmesh`（非内容）


def _threads(threads: int | None) -> int:
    if threads is not None:
        return max(1, int(threads))
    env = os.environ.get("AWR_NAV_THREADS")
    return max(1, int(env)) if env else max(1, min(16, os.cpu_count() or 1))


def _stream(index_json: Path, name: str, count: int) -> np.ndarray | None:
    """求交网格目录中由后续阶段登记的逐三角形流（`cls`、`q`），未登记或长度不符时为 None。"""
    try:
        doc = json.loads(index_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    st = {s.get("name"): s for s in doc.get("streams", [])}
    if name not in st:
        return None
    a = np.fromfile(index_json.parent / st[name]["href"], np.uint8)
    return a if len(a) == count else None


def write_navmesh(stage: Path, world_id: str, coordinate: Mapping[str, Any], coordinate_sha256: str, recipe: Any, *,
                  mesh: tuple[np.ndarray, np.ndarray] | None = None, tri_q: np.ndarray | None = None, tri_cls: np.ndarray | None = None,
                  class_keys: Sequence[str] | None = None, threads: int | None = None, profiles: Sequence[str] | None = None,
                  node: str | None = None, verify: Any = None, keep_inputs: Path | None = None) -> NavmeshResult:
    """navmesh 阶段：每个 profile 写 `geometry/nav/<profile>.{json,bin}`，返回图层（`nav.person`、`nav.vehicle`）与 qa 段。

    `recipe` 为 `awr.world.glb.recipe.Recipe`（`doc` 与 `dir`）；`mesh` 缺省读取 `stage` 的求交网格；`tri_q`、`tri_cls` 缺省读取
    求交网格目录登记的 `q`、`cls` 流；类别名缺省取 `semantic/anet-classes@1.json`。OSM 输入为配方 `osm`（入库清单校验）。
    `keep_inputs` 给出时 Node 烘焙输入保留在 `<keep_inputs>/<profile>/`（跨机器复烘对拍用），否则写在 staging 的隐藏目录并在烘焙后删除。
    """
    from awr.world.glb.raster import trimesh_world
    from awr.world.glb.recipe import verify_raw

    stage = Path(stage)
    verify = verify or verify_raw
    doc = recipe.doc
    rc_index = stage / "geometry" / "mesh" / "raycast" / "index.json"
    V, F = mesh if mesh is not None else trimesh_world(rc_index)
    V = np.asarray(V, np.float64)
    F = np.asarray(F, np.int64)
    if tri_q is None:
        tri_q = _stream(rc_index, "q", len(F))
    if tri_cls is None:
        tri_cls = _stream(rc_index, "cls", len(F))
    if tri_cls is not None and class_keys is None:
        ct = stage / "semantic" / "anet-classes@1.json"
        class_keys = [c["key"] for c in json.loads(ct.read_text(encoding="utf-8"))["classes"]] if ct.is_file() else None
    osm_entries = doc.get("osm") or []
    osm_entries = [osm_entries] if isinstance(osm_entries, dict) else list(osm_entries)
    hw = Highways()
    if osm_entries:
        hw = parse_highways([verify(e["path"], e.get("sha256")) for e in osm_entries], Anchor.from_coordinate(dict(coordinate)))
    recipe_dir = getattr(recipe, "dir", None)
    extra_seeds = _recipe_json(recipe_dir, "nav_seeds.json").get("seeds", [])
    links = _recipe_json(recipe_dir, "offmesh.json").get("links", [])
    verts = np.column_stack([V[:, 0], V[:, 2], -V[:, 1]]).astype("<f4")     # Recast (E, U, −N)
    nthreads = _threads(threads)
    out_dir = stage / HREF_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    layers, docs, qa = [], {}, {"recast_version": recast_version(), "wasm_sha256": wasm_sha256(), "input": "raycast",
                                "triangles": len(F), "profiles": {}}
    for prof in (profiles if profiles is not None else nav_profiles(doc)):
        t0 = time.perf_counter()
        kinds = PERSON_WAYS if prof == "person" else VEHICLE_WAYS
        lines = hw.lines(kinds)
        areas, hist = triangle_areas(V, F, corridor_lines=lines, tri_cls=tri_cls, class_keys=class_keys, tri_q=tri_q)
        rcp, agent = recast_params(prof, doc)
        seeds = junction_seeds(hw, kinds, V, F)
        manual = [s for s in extra_seeds if s.get("profile", "person") == prof]
        if manual:
            xy = np.array([s["xy"] for s in manual], np.float64)
            z = lowest_hit(V, F, xy)
            seeds += [{"id": str(s["id"]), "p_mm": to_mm((xy[i, 0], xy[i, 1], z[i] if np.isfinite(z[i]) else 0.0))}
                      for i, s in enumerate(manual)]
        om = [{"id": str(L["id"]), "a_mm": to_mm(L["a"]), "b_mm": to_mm(L["b"]), "radius_mm": to_mm([L.get("radius_m", 0.5)])[0],
               "bidir": bool(L.get("bidir", True))} for L in links if L.get("profile", "person") == prof]
        t1 = time.perf_counter()
        bin_rel = f"{prof}.bin"
        keep = str((((doc.get("build") or {}).get("nav") or {}).get(prof) or {}).get("keep", "seeded"))
        if keep not in KEEP_MODES:
            raise ConfigError(f"recipe build.nav.{prof}.keep {keep!r} not in {KEEP_MODES}")
        work = Path(keep_inputs) / prof if keep_inputs is not None else stage / f".navbake-{prof}"
        res = bake_profile(work, out_dir / bin_rel, profile=prof, verts_recast=verts, tris=F.astype(np.uint32),
                           areas=areas, recast=rcp, seeds=seeds, offmesh=om, keep=keep, threads=nthreads, node=node)
        if keep_inputs is None:
            shutil.rmtree(work, ignore_errors=True)
        isl = res["islands"]
        snapped = [s for s in res["seeds"] if s.get("found") and s.get("kept", True)]
        nd = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "world_id": world_id, "coordinate_sha256": coordinate_sha256,
              "profile": prof, "agent": agent, "cs_m": rcp["cs"], "ch_m": rcp["ch"], "tile_size_cells": rcp["tileSize"],
              "axes": "recast-y-up-enu", "recast_version": recast_version(), "wasm_sha256": wasm_sha256(), "input": "raycast",
              "recast": rcp, "areas": [dict(a) for a in AREAS], "flags": dict(FLAGS), "poly_flag": PROFILE_FLAG[prof],
              "corridor": {"half_width_m": CORRIDOR_HALF_M, "osm_highways": sorted(kinds), "lines": len(lines)},
              "area_triangles": hist, "seeds": [s["id"] for s in snapped] or ([AUTO_SEED] if not seeds and res.get("seed_fallback") else []),
              "seed_candidates": len(seeds), "seed_snapped": sum(1 for s in res["seeds"] if s.get("found")), "keep": keep,
              "seed_ext_m": [v / 1000 for v in SEED_EXT_MM], "seed_fallback": res.get("seed_fallback"),
              "islands": {"kept": isl["kept"], "dropped": isl["dropped"], "total": isl["total"],
                          "walkable_area_m2": isl["walkable_area_m2"], "seed_area_m2": isl["walkable_area_m2"],
                          "walkable_area_before_m2": isl["walkable_area_before_m2"], "largest_m2": isl["largest_m2"],
                          "kept_areas_m2": isl.get("kept_areas_m2", [])},
              "offmesh": [{"id": o["id"], "a": [v / 1000 for v in o["a_mm"]], "b": [v / 1000 for v in o["b_mm"]],
                           "radius_m": o["radius_mm"] / 1000, "bidir": o["bidir"]} for o in om],
              "tiles": {"x": res["grid"]["tiles_x"], "y": res["grid"]["tiles_y"], "built": res["tiles"]["built"],
                        "tile_m": round(float(res["grid"]["tile_m"]), 6)},
              "polys": {"total": res["polys"], "kept": res["polys_kept"]},
              "bin": {"href": bin_rel, "bytes": res["bin"]["bytes"], "sha256": res["bin"]["sha256"]}}
        write_json(out_dir / f"{prof}.json", nd)
        docs[prof] = nd
        dists = [s["dist_mm"] / 1000 for s in snapped]
        qa["profiles"][prof] = {"islands": nd["islands"], "polys": nd["polys"], "tiles": nd["tiles"], "area_triangles": hist,
                                "seeds": {"candidates": len(seeds), "snapped": len(snapped),
                                          "dist_p50_m": round(float(np.median(dists)), 3) if dists else None},
                                "seed_fallback": res.get("seed_fallback"), "bin_sha256": res["bin"]["sha256"],
                                "seconds": {"prepare": round(t1 - t0, 3), "bake": round(time.perf_counter() - t1, 3)},
                                "runtime": res.get("runtime", {})}
        layers.append(LayerSpec(f"nav.{prof}", "navmesh", "geometry", "detour-navmesh@1", f"{HREF_DIR}{prof}.json",
                                files=[f"{HREF_DIR}{prof}.json", f"{HREF_DIR}{bin_rel}"],
                                extra={"axes": "recast-y-up-enu", "bytes": res["bin"]["bytes"],
                                       "stats": {"polys": res["polys_kept"], "islandsKept": isl["kept"],
                                                 "walkableAreaM2": isl["walkable_area_m2"]}}))
    return NavmeshResult(layers, docs, qa)


# ---------------------------------------------------------------- navsvc 客户端


class NavSvcError(RuntimeError):
    """navsvc 进程不可用或应答 `ok = false`（`code`：BAD_REQUEST、UNKNOWN_OP、NAV_UNAVAILABLE）。"""

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class NavSvc:
    """navsvc 子进程客户端：行分隔 JSON，请求与应答一一对应（同序、同 id）。

    `world` 给出世界目录（读取全部 navmesh 图层），`profiles` 直接指定 `{profile: <profile>.json}`。`call_raw` 返回应答原始字节
    （确定性对拍用）；`snap`、`path`、`los`、`random_point_around`、`height` 接受世界帧米并在边界量化为整数毫米。
    """

    def __init__(self, world: str | Path | None = None, profiles: Mapping[str, str | Path] | None = None, *, node: str | None = None) -> None:
        cmd = [node_bin(node), str(NAVSVC_JS)]
        if world is not None:
            cmd += ["--world", str(world)]
        for k, v in (profiles or {}).items():
            cmd += ["--profile", f"{k}={v}"]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._id = 0
        self.info = self.call("hello")

    def __enter__(self) -> NavSvc:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b'{"op":"quit"}\n')
                self.proc.stdin.flush()
                self.proc.stdin.close()               # 标准输入保持打开时 Node 事件循环不退出（quit 之后仍需 EOF）
                self.proc.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()
                self.proc.wait()
        for s in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if s is not None:
                s.close()

    def call_raw(self, op: str, **fields: Any) -> bytes:
        self._id += 1
        req = json.dumps({"id": self._id, "op": op, **fields}, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            self.proc.stdin.write(req)
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except OSError as e:
            raise NavSvcError(f"navsvc pipe error: {e}") from e
        if not line:
            err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.poll() is not None else ""
            raise NavSvcError(f"navsvc exited: {err.strip()[-2000:]}")
        return line

    def call(self, op: str, **fields: Any) -> dict:
        r = json.loads(self.call_raw(op, **fields))
        if not r.get("ok"):
            e = r.get("error") or {}
            raise NavSvcError(str(e.get("message", "navsvc error")), e.get("code"))
        return r

    @staticmethod
    def _ext(ext: Sequence[float] | None) -> dict:
        return {} if ext is None else {"ext": to_mm(ext)}

    def snap(self, profile: str, p: Sequence[float], ext: Sequence[float] | None = None) -> dict:
        return self.call("snap", profile=profile, p=to_mm(p), **self._ext(ext))

    def path(self, profile: str, a: Sequence[float], b: Sequence[float], ext: Sequence[float] | None = None) -> dict:
        return self.call("path", profile=profile, a=to_mm(a), b=to_mm(b), **self._ext(ext))

    def los(self, profile: str, a: Sequence[float], b: Sequence[float]) -> dict:
        return self.call("los", profile=profile, a=to_mm(a), b=to_mm(b))

    def random_point_around(self, profile: str, p: Sequence[float], radius_m: float, seed: int) -> dict:
        return self.call("random_point_around", profile=profile, p=to_mm(p), r_mm=to_mm([radius_m])[0], seed=int(seed))

    def random_point(self, profile: str, seed: int) -> dict:
        return self.call("random_point", profile=profile, seed=int(seed))

    def height(self, profile: str, p: Sequence[float]) -> dict:
        return self.call("height", profile=profile, p=to_mm(p))
