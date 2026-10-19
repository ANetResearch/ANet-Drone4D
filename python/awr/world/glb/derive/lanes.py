"""OSM 道路图与车道图（M22 §6.2 第 8 阶段 navgraph、§6.6.11 伪代码末行、§7.3.4；M22-FR-034、FR-074、FR-075；ADR-132 v1.3、
ADR-181；D3-AC-33 "路段 100% 落在道路走廊内"）与车道图查询库（`LaneGraph`：`lane_route`、`conflict_zones`、`stop_line`）。

构建（`build_lane_graph`）：
1. 读取 OSM XML（`read_osm`）：`highway=*` way（按 way id 去重、升序）、`highway=stop|give_way` 节点与 `type=restriction`
   关系（只支持 via 节点）；节点经纬度经 M02 `lla_to_world`（h = 0）转世界帧 (E, N) 后立即量化到毫米（跨 ISA 的三角函数末位
   差异不进入后续几何）。
2. 道路边（FR-034）：way 在路口节点（被 ≥ 2 次引用的节点，含闭合 way 的首尾）处切分为边 `e-<way>-<seq>`；按覆盖掩膜
   （扫描覆盖，DSM 格）以 1 m 步长裁剪，越出覆盖的部分删除，裁剪端点为合成节点 `c-<way>-<seq>-<k>a|b`，一条边被裁成多段时
   边 id 加后缀 `-<k>`；`oneway=-1` 的边按行驶方向反转几何。属性 `kind`、`agents`、`width_m`、`speed_mps`、`oneway`、
   `lanes`、`osm_id`、逐顶点 `z_m`（`z_fn`，构建流水线取 `dsm_2m` 即最高命中面）。
3. 车道（FR-074，只增）：车辆道路（与 NavMesh 车辆走廊同一 `VEHICLE_WAYS`，排除 `access|vehicle|motor_vehicle = no`）按
   `lanes`、`lanes:forward|backward`、`oneway` 生成车道中心线，右侧通行；车道宽 = 路宽 / 车道数（缺省车道宽 3.2 m，缺失标签时
   单行 1 车道、双向每向 1 车道；双向 `lanes = 1` 记 `shared`）；车道序号 0 为行驶方向最左（与 `turn:lanes` 的列举次序一致）。
   车道在节点处按路口几何退让（`_setback`），中心线截取后按 ≤ 5 m 加密再做斜接偏移。
4. 连接段（路口转向连通）：同一节点的驶入车道组 × 驶出车道组（掉头只在断头处），转向按航向点积与叉积分为 through、left、
   right、uturn（不用三角函数）；车道对应规则见 `_lane_pairs`（有 `turn:lanes` 时按标签）；OSM 转向限制删去被禁止的连接；
   几何为三次 Bézier（控制距离随转角在弦长的 1/3 至 2/3 之间），越出道路走廊时依次收缩控制距离（0.5、0.25、0）。
5. 路口（度 ≥ 3 的车辆节点）：`j-<node>` 点要素、冲突区 `z-<node>`（该路口全部连接段采样点的凸包）、连接段两两冲突（汇入
   同一车道或几何相交；同一来源车道不冲突）；让行：道路等级最高者为主路（`major`），其余为次路（`minor`），全部同级为
   `equal`（先到先行，M23 §15.4）；次路与同级路口的驶入车道以及 OSM 停车让行标志所在的驶入车道在车道末端设停止线
   `s-<lane>`。
6. 限速：`maxspeed`（km/h、mph、`<国家>:urban|rural|living_street|motorway`）或按道路等级缺省（M23 §15.4：居住区 8.3 m/s、
   次干道 11.1 m/s、主干道 13.9 m/s）；连接段取两端车道的较小值并给出最小转弯半径（弯道限速由 M23 生成器按 `√(a_lat·R)` 计算）。
7. 道路走廊校验（D3-AC-33）：走廊 = 全部车辆道路边中心线的缓冲区并集，半宽 `max(width_m / 2, 5 m)`（与 NavMesh
   ROAD_CORRIDOR 同一 5 m 口径，§6.8）；车道与连接段按 ≤ 0.5 m 采样逐点求到线段的精确距离，全部采样点在走廊内的段记
   `in_corridor = true`，否则标记并告警（FR-074）。另报严格的车行道口径（≤ `width_m / 2 + 0.5 m`）供参考。
8. 吸附到车辆 NavMesh（可选，`nav` 为 `NavSvc`）：车道与连接段顶点经 navsvc `snap`（batch）求最近多边形，水平距离 ≤ 1 m
   时取 NavMesh 高度，否则取 `z_fn`；吸附统计写入车道属性与 qa。
9. 可路由性：车道与连接段构成有向图，最大强连通分量内的段记 `routable = true`（`lane_route` 随机对抽样取自该分量）。

确定性：全部几何为显式次序的 float64 加减乘除与开方（不用数组三角函数、`np.linalg.norm` 或浮点求和归约），写出前统一量化到
毫米；要素次序由 way id、节点 id 与车道序号决定，与 OSM 文件中的元素次序无关。文件头 `content_sha256` 为
`{"awr": 文件头（不含该字段）, "features": [...]}` 的规范 JSON（键排序、紧凑分隔）的 sha256；文件字节 sha256 随 `files[]`
进入 `contentVersion`。图层为 ODbL 派生层：文件头写 `license = ODbL-1.0`、`attribution = OpenStreetMap contributors`，
图层 `note` 声明 ODbL（V-GLB-08）。
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from awr.world.georef.frames import lla_to_world
from awr.world.georef.types import Anchor
from awr.world.glb.derive.navmesh import CORRIDOR_HALF_M, VEHICLE_WAYS, to_mm
from awr.world.package.derivers import LayerSpec

__all__ = ["FORMAT", "HREF", "LAYER_ID", "Corridor", "LaneBuild", "LaneGraph", "LaneRoute", "LanesResult", "OsmRoads", "build_lane_graph",
           "content_sha256", "cover_fn", "parse_maxspeed", "read_osm", "write_lanes"]

HREF = "geometry/nav/graph.geojson"
FORMAT = "geojson/awr-navgraph@1"
LAYER_ID = "nav.graph"
SCHEMA = "awr.navgraph.v1"
SCHEMA_VERSION = "1.1.0"          # 1.0.0 道路边（FR-034）；1.1.0 增加车道扩展（FR-074，只增）
ODBL = {"license": "ODbL-1.0", "attribution": "OpenStreetMap contributors"}
NOTE = "Road and lane graph derived from OpenStreetMap (ODbL-1.0, OpenStreetMap contributors)."

LANE_WIDTH_M = 3.2                # 缺省车道宽（FR-074）
LANE_STEP_M = 5.0                 # 车道中心线加密步长（高度与 NavMesh 吸附的采样点）
CONNECTOR_STEP_M = 1.0            # 连接段采样步长
SAMPLE_M = 0.5                    # 走廊校验采样步长
CLIP_STEP_M = 1.0                 # 覆盖裁剪步长
CORRIDOR_SEG_M = 2.0              # 走廊线段加密上限（球查询半径 = 最大半宽 + 半段长）
CARRIAGEWAY_TOL_M = 0.5           # 严格车行道口径的容差
MIN_SETBACK_M = 1.0
JUNCTION_MARGIN_M = 1.0
SETBACK_MAX_FRAC = 0.45           # 每端退让不超过边长的 45%
MITER_MAX = 2.0
NAV_EXT_M = (3.0, 10.0, 3.0)      # navsvc snap 搜索半宽（Recast x、竖直、z）
NAV_SNAP_OK_M = 1.0               # 吸附水平距离 ≤ 1 m 时取 NavMesh 高度
NAV_BATCH = 2000
COS_THROUGH = 0.8660254037844387  # cos 30°：航向差 ≤ 30° 为直行
COS_UTURN = -0.9659258262890683   # cos 165°：航向差 ≥ 165° 为掉头
TAN_CAP = 1.7320508075688772      # tan 60°：退让公式中半转角正切的上限
BEZIER_SHRINK = (1.0, 0.5, 0.25, 0.0)

# highway 取值 → 道路图 kind（§7.3.4）；未列出的取值（construction、proposed、platform 等）不进入道路图
KIND_OF = {**{h: "road" for h in ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary",
                                  "secondary_link", "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "road")},
           "service": "service", "footway": "footway", "pedestrian": "footway", "corridor": "footway", "path": "path", "track": "path",
           "bridleway": "path", "steps": "steps", "cycleway": "cycleway"}
VEHICLE_ONLY = frozenset({"motorway", "motorway_link"})
# 道路等级（让行：等级高者为主路）；匝道同主线
RANK = {"motorway": 8, "motorway_link": 8, "trunk": 7, "trunk_link": 7, "primary": 6, "primary_link": 6, "secondary": 5,
        "secondary_link": 5, "tertiary": 4, "tertiary_link": 4, "unclassified": 3, "residential": 3, "road": 3, "living_street": 2,
        "service": 1}
# 缺省限速（m/s；M23 §15.4：居住区 8.3、次干道 11.1、主干道 13.9）
SPEED_MPS = {"motorway": 27.8, "motorway_link": 16.7, "trunk": 19.4, "trunk_link": 13.9, "primary": 13.9, "primary_link": 11.1,
             "secondary": 11.1, "secondary_link": 11.1, "tertiary": 11.1, "tertiary_link": 8.3, "unclassified": 8.3, "residential": 8.3,
             "road": 8.3, "living_street": 2.8, "service": 5.6}
PERSON_SPEED_MPS = 1.4
PERSON_WIDTH_M = {"footway": 2.0, "path": 1.5, "steps": 2.0, "cycleway": 2.0}
ZONE_KMH = {"urban": 50.0, "rural": 90.0, "living_street": 20.0, "motorway": 130.0, "trunk": 90.0, "walk": 5.0}
TURN_TOKENS = {"through": frozenset({"through", "none", "", "merge_to_left", "merge_to_right"}),
               "left": frozenset({"left", "slight_left", "sharp_left"}),
               "right": frozenset({"right", "slight_right", "sharp_right"}),
               "uturn": frozenset({"reverse"})}
NO_VEHICLE = frozenset({"no"})


# ---------------------------------------------------------------- 标签解析


def parse_maxspeed(v: str | None) -> float | None:
    """`maxspeed` → m/s（保留 3 位小数）；取第一个值（`50;30` 取 50）；无法解析（`none`、`signals`、`variable`）为 None。"""
    if not v:
        return None
    s = str(v).split(";")[0].strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(km/h|kmh|kph|mph)?", s)
    if m:
        kmh = float(m.group(1)) * (1.609344 if m.group(2) == "mph" else 1.0)
    else:
        zone = s.split(":", 1)[1] if ":" in s else s
        if zone not in ZONE_KMH:
            return None
        kmh = ZONE_KMH[zone]
    return round(kmh / 3.6, 3) if kmh > 0 else None


def _num(v: str | None) -> float | None:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(m)?\s*", str(v)) if v is not None else None
    return float(m.group(1)) if m else None


def _int(v: str | None) -> int | None:
    m = re.fullmatch(r"\s*(\d+)\s*", str(v)) if v is not None else None
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _oneway(tags: Mapping[str, str]) -> int:
    """1 顺 way 方向单行，-1 逆向单行，0 双向；`junction=roundabout|circular` 与高速公路隐含单行。"""
    v = str(tags.get("oneway", "")).strip().lower()
    if v in ("yes", "true", "1"):
        return 1
    if v in ("-1", "reverse"):
        return -1
    if v in ("no", "false", "0"):
        return 0
    return 1 if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in ("motorway", "motorway_link") else 0


def _turns(v: str | None, n: int) -> list[list[str]] | None:
    """`turn:lanes` → 每车道（自左至右）的转向词表；车道数不符时为 None。"""
    if v is None:
        return None
    out = [sorted({t.strip().lower() or "none" for t in p.split(";")}) for p in str(v).split("|")]
    return out if len(out) == n else None


# ---------------------------------------------------------------- OSM 读取


@dataclass
class OsmRoads:
    """OSM 道路要素（世界帧 (E, N)，毫米量化）。"""

    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)
    ways: list[tuple[int, dict[str, str], list[int]]] = field(default_factory=list)       # (way id, 标签, 节点序列)，way id 升序
    node_tags: dict[int, dict[str, str]] = field(default_factory=dict)                   # 带 highway 标签的节点
    restrictions: list[dict[str, Any]] = field(default_factory=list)                    # {id, kind, from, via, to}
    unsupported_restrictions: int = 0


def read_osm(paths: str | Path | Sequence[str | Path], anchor: Anchor) -> OsmRoads:
    """读取 OSM XML 的道路 way、标志节点与转向限制（多个文件按 id 合并，后者覆盖前者）。"""
    plist = [Path(paths)] if isinstance(paths, (str, Path)) else [Path(p) for p in paths]
    lla: dict[int, tuple[float, float]] = {}
    ntags: dict[int, dict[str, str]] = {}
    ways: dict[int, tuple[dict[str, str], list[int]]] = {}
    rels: dict[int, dict[str, Any]] = {}
    bad: set[int] = set()
    for p in plist:
        root = ET.fromstring(p.read_bytes())
        for nd in root.iter("node"):
            nid = int(nd.get("id"))
            lla[nid] = (float(nd.get("lat")), float(nd.get("lon")))
            t = {x.get("k"): x.get("v") for x in nd.iter("tag")}
            if t.get("highway"):
                ntags[nid] = t
        for w in root.iter("way"):
            t = {x.get("k"): x.get("v") for x in w.iter("tag")}
            if t.get("highway") in KIND_OF and t.get("area") != "yes":
                ways[int(w.get("id"))] = (t, [int(x.get("ref")) for x in w.iter("nd")])
        for r in root.iter("relation"):
            t = {x.get("k"): x.get("v") for x in r.iter("tag")}
            if t.get("type") != "restriction":
                continue
            kind = t.get("restriction") or t.get("restriction:motorcar") or t.get("restriction:motor_vehicle")
            mem: dict[str, list[tuple[str, int]]] = {}
            for m in r.iter("member"):
                mem.setdefault(str(m.get("role")), []).append((str(m.get("type")), int(m.get("ref"))))
            rid = int(r.get("id"))
            fr, via, to = mem.get("from", []), mem.get("via", []), mem.get("to", [])
            ok = (kind and (kind.startswith("no_") or kind.startswith("only_")) and len(fr) == 1 and len(via) == 1 and len(to) == 1
                  and fr[0][0] == "way" and to[0][0] == "way" and via[0][0] == "node")
            if ok:
                rels[rid] = {"id": f"relation/{rid}", "kind": kind, "from": fr[0][1], "via": via[0][1], "to": to[0][1]}
                bad.discard(rid)
            else:
                rels.pop(rid, None)
                bad.add(rid)
    used = sorted({n for _t, ids in ways.values() for n in ids if n in lla} | {n for n in ntags if n in lla})
    out = OsmRoads(node_tags={n: ntags[n] for n in sorted(ntags) if n in lla}, unsupported_restrictions=len(bad))
    if used:
        ll = np.array([lla[n] for n in used], np.float64)
        en = np.round(lla_to_world(ll[:, 0], ll[:, 1], np.zeros(len(ll)), anchor)[:, :2], 3) + 0.0
        out.nodes = {n: (float(en[i, 0]), float(en[i, 1])) for i, n in enumerate(used)}
    for wid in sorted(ways):
        t, ids = ways[wid]
        ids = [n for n in ids if n in out.nodes]
        ids = [n for i, n in enumerate(ids) if i == 0 or n != ids[i - 1]]
        if len(ids) >= 2:
            out.ways.append((wid, t, ids))
    out.restrictions = [rels[k] for k in sorted(rels)]
    return out


def cover_fn(mask: np.ndarray, origin_xy: Sequence[float], cell_m: float) -> Callable[[np.ndarray], np.ndarray]:
    """覆盖掩膜（行主序、第 0 行在南）→ 点 (n, 2) 是否在覆盖内。"""
    m = np.asarray(mask, bool)
    H, W = m.shape
    ox, oy, c = float(origin_xy[0]), float(origin_xy[1]), float(cell_m)

    def inside(xy: np.ndarray) -> np.ndarray:
        xy = np.asarray(xy, np.float64).reshape(-1, 2)
        ci = np.floor((xy[:, 0] - ox) / c).astype(np.int64)
        ri = np.floor((xy[:, 1] - oy) / c).astype(np.int64)
        ok = (ri >= 0) & (ri < H) & (ci >= 0) & (ci < W)
        out = np.zeros(len(xy), bool)
        out[ok] = m[ri[ok], ci[ok]]
        return out

    return inside


# ---------------------------------------------------------------- 折线几何（显式次序的 float64 运算）


def _seglen(P: np.ndarray) -> np.ndarray:
    d = P[1:] - P[:-1]
    return np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])


def _cum(P: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(_seglen(P))])


def _unit(v: np.ndarray) -> np.ndarray:
    n = math.sqrt(float(v[0]) * float(v[0]) + float(v[1]) * float(v[1]))
    return np.array([float(v[0]) / n, float(v[1]) / n]) if n > 0 else np.array([0.0, 0.0])


def _dedupe(P: np.ndarray, eps: float = 1e-3) -> np.ndarray:
    keep = [0]
    for i in range(1, len(P)):
        d = P[i] - P[keep[-1]]
        if math.sqrt(float(d[0]) * float(d[0]) + float(d[1]) * float(d[1])) > eps:
            keep.append(i)
    if len(keep) == 1 and len(P) > 1:
        keep.append(len(P) - 1)
    return P[keep]


def _point_at(P: np.ndarray, cum: np.ndarray, s: float) -> np.ndarray:
    i = int(np.searchsorted(cum, s, side="right")) - 1
    i = min(max(i, 0), len(P) - 2)
    L = cum[i + 1] - cum[i]
    t = 0.0 if L <= 0 else (s - cum[i]) / L
    return P[i] + (P[i + 1] - P[i]) * t


def _cut(P: np.ndarray, s0: float, s1: float) -> np.ndarray:
    """折线在弧长 [s0, s1] 内的部分（端点插值）。"""
    cum = _cum(P)
    a, b = _point_at(P, cum, s0), _point_at(P, cum, s1)
    mid = P[(cum > s0) & (cum < s1)]
    return _dedupe(np.vstack([a[None], mid, b[None]]))


def _densify(P: np.ndarray, step: float) -> np.ndarray:
    out = [P[:1]]
    for a, b, L in zip(P[:-1], P[1:], _seglen(P), strict=True):
        k = max(1, math.ceil(float(L) / step))
        t = np.arange(1, k + 1, dtype=np.float64)[:, None] / k
        out.append(a + (b - a) * t)
    return np.concatenate(out)


def _offset(P: np.ndarray, o: float) -> np.ndarray:
    """向行驶方向右侧偏移 o（斜接，斜接比上限 2）。"""
    if o == 0.0:
        return P.copy()
    d = P[1:] - P[:-1]
    L = np.sqrt(d[:, 0] * d[:, 0] + d[:, 1] * d[:, 1])
    t = d / L[:, None]
    r = np.column_stack([t[:, 1], -t[:, 0]])
    n = np.empty_like(P)
    n[0], n[-1] = r[0], r[-1]
    if len(P) > 2:
        m = r[:-1] + r[1:]
        ml = np.sqrt(m[:, 0] * m[:, 0] + m[:, 1] * m[:, 1])
        ok = ml > 1e-9
        mu = np.where(ok[:, None], m / np.where(ok, ml, 1.0)[:, None], r[1:])
        cosh = mu[:, 0] * r[1:, 0] + mu[:, 1] * r[1:, 1]
        sc = np.minimum(1.0 / np.maximum(cosh, 1e-9), MITER_MAX)
        n[1:-1] = mu * sc[:, None]
    return P + n * o


def _bezier(p0: np.ndarray, h0: np.ndarray, p3: np.ndarray, h1: np.ndarray, d: float, step: float) -> np.ndarray:
    p1, p2 = p0 + h0 * d, p3 - h1 * d
    chord = p3 - p0
    est = math.sqrt(float(chord[0]) ** 2 + float(chord[1]) ** 2) + 2.0 * d
    k = max(2, math.ceil(est / step))
    t = np.arange(k + 1, dtype=np.float64)[:, None] / k
    u = 1.0 - t
    P = u * u * u * p0 + 3.0 * u * u * t * p1 + 3.0 * u * t * t * p2 + t * t * t * p3
    P[0], P[-1] = p0, p3
    return _dedupe(P)


def _radius_min(P: np.ndarray) -> float | None:
    """相邻三点外接圆半径的最小值（直线为 None）。"""
    if len(P) < 3:
        return None
    a, b, c = P[:-2], P[1:-1], P[2:]
    ab, bc, ca = b - a, c - b, a - c
    la = np.sqrt(ab[:, 0] ** 2 + ab[:, 1] ** 2)
    lb = np.sqrt(bc[:, 0] ** 2 + bc[:, 1] ** 2)
    lc = np.sqrt(ca[:, 0] ** 2 + ca[:, 1] ** 2)
    cr = np.abs(ab[:, 0] * bc[:, 1] - ab[:, 1] * bc[:, 0])
    ok = cr > 1e-9
    if not ok.any():
        return None
    return float((la[ok] * lb[ok] * lc[ok] / (2.0 * cr[ok])).min())


def _hull(P: np.ndarray) -> np.ndarray:
    """二维凸包（Andrew 单调链，逆时针，不闭合）；输入先量化到毫米。"""
    pts = sorted({(float(x), float(y)) for x, y in np.round(P, 3)})
    if len(pts) < 3:
        return np.asarray(pts, np.float64)

    def cross(o: tuple, a: tuple, b: tuple) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return np.asarray(lower[:-1] + upper[:-1], np.float64)


def _poly_area(R: np.ndarray) -> float:
    x, y = R[:, 0], R[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)) if len(R) >= 3 else 0.0


def _cross_polylines(A: np.ndarray, B: np.ndarray) -> bool:
    """两条折线是否严格相交（不计端点接触与共线重叠）。"""
    a0, a1 = A[:-1][:, None, :], A[1:][:, None, :]
    b0, b1 = B[:-1][None, :, :], B[1:][None, :, :]

    def orient(p, q, r):
        return (q[..., 0] - p[..., 0]) * (r[..., 1] - p[..., 1]) - (q[..., 1] - p[..., 1]) * (r[..., 0] - p[..., 0])

    d1, d2 = orient(a0, a1, b0), orient(a0, a1, b1)
    d3, d4 = orient(b0, b1, a0), orient(b0, b1, a1)
    eps = 1e-9
    sa = ((d1 > eps) & (d2 < -eps)) | ((d1 < -eps) & (d2 > eps))
    sb = ((d3 > eps) & (d4 < -eps)) | ((d3 < -eps) & (d4 > eps))
    return bool((sa & sb).any())


# ---------------------------------------------------------------- 道路走廊


class Corridor:
    """车辆道路边中心线的缓冲区并集：`lines[i] = (xy (k, 2), 走廊半宽, 车行道半宽)`；点到线段距离精确计算。"""

    def __init__(self, lines: Sequence[tuple[np.ndarray, float, float]]) -> None:
        from scipy.spatial import cKDTree

        A, B, hc, hk = [], [], [], []
        for xy, half_c, half_k in lines:
            P = _densify(np.asarray(xy, np.float64), CORRIDOR_SEG_M) if len(xy) >= 2 else np.asarray(xy, np.float64)
            if len(P) < 2:
                continue
            A.append(P[:-1])
            B.append(P[1:])
            hc.append(np.full(len(P) - 1, float(half_c)))
            hk.append(np.full(len(P) - 1, float(half_k)))
        self.n = len(A)
        if not A:
            return
        self.A, self.B = np.concatenate(A), np.concatenate(B)
        self.hc, self.hk = np.concatenate(hc), np.concatenate(hk)
        self.tree = cKDTree((self.A + self.B) / 2.0)
        self.r = float(self.hc.max()) + CORRIDOR_SEG_M / 2.0 + 1e-6

    def excess(self, pts: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """逐点超出量（m）：(到走廊边界，到车行道边界)；≤ 0 为在内；半径内无候选线段为 +inf。"""
        pts = np.asarray(pts, np.float64).reshape(-1, 2)
        ec = np.full(len(pts), np.inf)
        ek = np.full(len(pts), np.inf)
        if self.n == 0 or len(pts) == 0:
            return ec, ek
        lists = self.tree.query_ball_point(pts, self.r)
        cnt = np.fromiter((len(x) for x in lists), np.int64, len(lists))
        if cnt.sum() == 0:
            dn, i = self.tree.query(pts)
            return dn - self.hc[i], dn - self.hk[i]
        idx = np.concatenate([np.asarray(x, np.int64) for x in lists if len(x)])
        pi = np.repeat(np.arange(len(pts)), cnt)
        a, ab = self.A[idx], self.B[idx] - self.A[idx]
        p = pts[pi]
        den = ab[:, 0] * ab[:, 0] + ab[:, 1] * ab[:, 1]
        t = np.clip(((p[:, 0] - a[:, 0]) * ab[:, 0] + (p[:, 1] - a[:, 1]) * ab[:, 1]) / np.where(den > 0, den, 1.0), 0.0, 1.0)
        q = a + ab * t[:, None]
        dq = p - q
        d = np.sqrt(dq[:, 0] * dq[:, 0] + dq[:, 1] * dq[:, 1])
        np.minimum.at(ec, pi, d - self.hc[idx])
        np.minimum.at(ek, pi, d - self.hk[idx])
        far = ~np.isfinite(ec)
        if far.any():                                     # 半径内无候选：按最近线段中点估计（必为正）
            dn, i = self.tree.query(pts[far])
            ec[far], ek[far] = dn - self.hc[i], dn - self.hk[i]
        return ec, ek


def _samples(P: np.ndarray) -> np.ndarray:
    return _densify(P, SAMPLE_M) if len(P) >= 2 else P


# ---------------------------------------------------------------- 构建


@dataclass
class _Edge:
    id: str
    way: int
    nodes: tuple[str, str]
    osm_nodes: list[int]            # 该边覆盖的 OSM 节点（含端点；裁剪后为覆盖内的原始节点）
    xy: np.ndarray
    tags: dict[str, str]
    highway: str
    kind: str
    agents: list[str]
    width: float
    speed: float
    speed_src: str
    oneway: bool
    lanes: int
    nf: int
    nb: int
    shared: bool
    rank: int
    vehicle: bool
    turns_f: list[list[str]] | None = None
    turns_b: list[list[str]] | None = None
    reversed_: bool = False
    length: float = 0.0


@dataclass
class _Lane:
    id: str
    edge: _Edge
    dir: str                        # forward | backward（相对边几何）
    index: int                      # 0 为行驶方向最左
    n: int
    from_node: str
    to_node: str
    xy: np.ndarray
    width: float
    turns: list[str] | None
    kind: str = "lane"
    z: np.ndarray | None = None
    succ: list[str] = field(default_factory=list)
    stop_line: str | None = None
    rule: str | None = None
    in_corridor: bool = True
    excess: float = 0.0
    carriage_ok: int = 0
    samples: int = 0
    nav: dict | None = None


@dataclass
class _Conn:
    id: str
    node: str
    src: _Lane
    dst: _Lane
    turn: str
    xy: np.ndarray
    shrink: float
    kind: str = "connector"
    z: np.ndarray | None = None
    conflicts: list[str] = field(default_factory=list)
    priority: str = "none"
    in_corridor: bool = True
    excess: float = 0.0
    carriage_ok: int = 0
    samples: int = 0
    nav: dict | None = None


@dataclass
class LaneBuild:
    doc: dict                       # FeatureCollection
    stats: dict
    warnings: list[str]


def _nid(n: int) -> str:
    return f"n-{n}"


def _edges(roads: OsmRoads, inside: Callable[[np.ndarray], np.ndarray] | None, warnings: list[str]) -> list[_Edge]:
    """way → 道路边（在路口节点处切分、按覆盖裁剪、标签换算）。"""
    ref: dict[int, int] = {}
    for _w, _t, ids in roads.ways:
        for n in ids:
            ref[n] = ref.get(n, 0) + 1
    out: list[_Edge] = []
    for wid, tags, ids in roads.ways:
        cut = [0] + [i for i in range(1, len(ids) - 1) if ref[ids[i]] >= 2] + [len(ids) - 1]
        for seq, (i0, i1) in enumerate(itertools.pairwise(cut)):
            seg = ids[i0:i1 + 1]
            P = np.array([roads.nodes[n] for n in seg], np.float64)
            if len(_dedupe(P)) < 2:
                continue
            out.extend(_make_edge(wid, seq, part, tags, warnings) for part in _clip(P, seg, inside, wid, seq))
    return out


def _clip(P: np.ndarray, seg: list[int], inside: Callable[[np.ndarray], np.ndarray] | None, wid: int,
          seq: int) -> list[tuple[str, np.ndarray, str, str, list[int]]]:
    """覆盖裁剪：返回 [(边 id, xy, 起点 id, 终点 id, OSM 节点)]；完全在覆盖内时原样返回。"""
    base = f"e-{wid}-{seq}"
    if inside is None:
        return [(base, P, _nid(seg[0]), _nid(seg[-1]), list(seg))]
    pts, orig = [P[:1]], [0]
    for i, (a, b, L) in enumerate(zip(P[:-1], P[1:], _seglen(P), strict=True)):
        k = max(1, math.ceil(float(L) / CLIP_STEP_M))
        t = np.arange(1, k + 1, dtype=np.float64)[:, None] / k
        pts.append(a + (b - a) * t)
        orig += [-1] * (k - 1) + [i + 1]
    D = np.concatenate(pts)
    orig_a = np.asarray(orig)
    ins = inside(D)
    if ins.all():
        return [(base, P, _nid(seg[0]), _nid(seg[-1]), list(seg))]
    runs, i = [], 0
    while i < len(D):
        if not ins[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(D) and ins[j + 1]:
            j += 1
        runs.append((i, j))
        i = j + 1
    out = []
    kept = [(a, b) for a, b in runs if b > a and float(_cum(D[a:b + 1])[-1]) >= 1.0]
    for r, (a, b) in enumerate(kept):
        mids = [k for k in range(a + 1, b) if orig_a[k] >= 0]
        Q = np.vstack([D[a][None], D[mids], D[b][None]]) if mids else np.vstack([D[a], D[b]])
        n0 = _nid(seg[orig_a[a]]) if orig_a[a] >= 0 else f"c-{wid}-{seq}-{r}a"
        n1 = _nid(seg[orig_a[b]]) if orig_a[b] >= 0 else f"c-{wid}-{seq}-{r}b"
        osm = [seg[orig_a[k]] for k in range(a, b + 1) if orig_a[k] >= 0]
        eid = base if len(kept) == 1 and orig_a[a] == 0 and orig_a[b] == len(seg) - 1 else f"{base}-{r}"
        out.append((eid, _dedupe(Q), n0, n1, osm))
    return out


def _make_edge(wid: int, seq: int, part: tuple, tags: dict[str, str], warnings: list[str]) -> _Edge:
    eid, P, n0, n1, osm = part
    hw = tags["highway"]
    kind = KIND_OF[hw]
    veh = hw in VEHICLE_WAYS and not ({tags.get(k) for k in ("access", "vehicle", "motor_vehicle")} & NO_VEHICLE)
    agents = ["vehicle"] if hw in VEHICLE_ONLY else (["person", "vehicle"] if veh else ["person"])
    ow = _oneway(tags) if veh else 0
    rev = ow == -1
    if rev:
        P, n0, n1, osm = P[::-1].copy(), n1, n0, osm[::-1]
    sp = parse_maxspeed(tags.get("maxspeed"))
    speed, src = (sp, "maxspeed") if sp is not None else ((SPEED_MPS.get(hw, 8.3), "default") if veh else (PERSON_SPEED_MPS, "default"))
    tl, lf, lb = _int(tags.get("lanes")), _int(tags.get("lanes:forward")), _int(tags.get("lanes:backward"))
    shared = False
    if not veh:
        nf = nb = 0
        lanes = tl or 1
    elif ow:
        nf, nb = (tl or lf or (2 if hw in ("motorway", "trunk") else 1)), 0
        if rev and lb:
            nf = lb
        lanes = nf
    else:
        if lf and lb:
            nf, nb = lf, lb
        elif tl and tl >= 2:
            nf = lf or (tl - lb if lb and tl > lb else (tl + 1) // 2)
            nb = max(1, tl - nf)
        elif tl == 1:
            nf, nb, shared = 1, 1, True
        else:
            nf, nb = lf or 1, lb or 1
        lanes = tl or (nf + nb)
    w = _num(tags.get("width"))
    width = w if w and w > 0 else ((max(lanes, 1) * LANE_WIDTH_M) if veh else PERSON_WIDTH_M.get(kind, 2.0))
    e = _Edge(eid, wid, (n0, n1), osm, P, tags, hw, kind, agents, float(width), float(speed), src, bool(ow), int(lanes), int(nf), int(nb),
              shared, RANK.get(hw, 0), veh, reversed_=rev, length=float(_cum(P)[-1]))
    if veh:
        if ow:
            e.turns_f = _turns(tags.get("turn:lanes") or tags.get("turn:lanes:forward" if not rev else "turn:lanes:backward"), nf)
        else:
            tf, tb = tags.get("turn:lanes:forward"), tags.get("turn:lanes:backward")
            e.turns_f, e.turns_b = (_turns(tb, nf), _turns(tf, nb)) if rev else (_turns(tf, nf), _turns(tb, nb))
        for k in ("turn:lanes", "turn:lanes:forward", "turn:lanes:backward"):
            if tags.get(k) is not None and e.turns_f is None and e.turns_b is None:
                warnings.append(f"way/{wid}: {k} lane count differs from lanes; ignored")
                break
    return e


def _head_away(e: _Edge, at_start: bool) -> np.ndarray:
    """边在端点处背离节点的单位航向（取距端点 min(5 m, L / 2) 处的点）。"""
    P = e.xy if at_start else e.xy[::-1]
    cum = _cum(P)
    q = _point_at(P, cum, min(5.0, float(cum[-1]) / 2.0))
    return _unit(q - P[0])


def _setbacks(vedges: list[_Edge], ends: dict[str, list[tuple[_Edge, bool]]]) -> dict[tuple[str, bool], float]:
    """每个车辆边端点的车道退让距离（m）：断头为半宽 + 0.5 m；度 2 按半转角正切；路口取相邻道路半宽与转角的较大者 + 1 m。"""
    out: dict[tuple[str, bool], float] = {}
    for lst in ends.values():
        for e, st in lst:
            hw = e.width / 2.0
            others = [(o, os_) for o, os_ in lst if not (o is e and os_ == st)]
            if not others:
                s = hw + 0.5
            else:
                u = _head_away(e, st)
                tmax, hmax = 0.0, hw
                for o, os_ in others:
                    w = _head_away(o, os_)
                    c = -(float(u[0]) * float(w[0]) + float(u[1]) * float(w[1]))          # 转角余弦（直行为 1）
                    c = min(1.0, max(-1.0, c))
                    t = TAN_CAP if c <= -0.5 else math.sqrt((1.0 - c) / (1.0 + c))
                    tmax = max(tmax, min(t, TAN_CAP))
                    hmax = max(hmax, o.width / 2.0)
                s = hmax * tmax + 0.5 if len(others) == 1 else hmax * max(1.0, tmax) + JUNCTION_MARGIN_M
            s = max(MIN_SETBACK_M, s)
            out[(e.id, st)] = min(s, SETBACK_MAX_FRAC * e.length)
    return out


def _lanes_of(e: _Edge, sb: dict[tuple[str, bool], float]) -> list[_Lane]:
    s0, s1 = sb[(e.id, True)], sb[(e.id, False)]
    C = _densify(_cut(e.xy, s0, e.length - s1), LANE_STEP_M)
    lw = e.width / (e.nf + e.nb)
    out = []
    for d, n, P, a, b, turns in (("forward", e.nf, C, e.nodes[0], e.nodes[1], e.turns_f),
                                 ("backward", e.nb, C[::-1].copy(), e.nodes[1], e.nodes[0], e.turns_b)):
        for i in range(n):
            o = e.width / 2.0 - (n - i - 0.5) * lw          # 右侧偏移：本方向车道占车行道右侧 n 条，0 为最左
            out.append(_Lane(f"l-{e.id[2:]}-{d[0]}{i}", e, d, i, n, a, b, _offset(P, o), lw, None if turns is None else turns[i]))
    return out


def _lane_pairs(nin: int, nout: int, turn: str, tok: list[list[str]] | None, deg: int) -> list[tuple[int, int]]:
    """驶入组 × 驶出组的车道对（序号 0 为最左）：直行 i → min(i, n_out − 1) 并补齐多出的驶出车道；左转最左 → 最左；右转最右
    → 最右；断头掉头为每条驶入车道 → 最左并自最左补齐其余驶出车道。有 `turn:lanes` 且有匹配车道时按标签选来源车道（度 ≤ 2 的节点
    一律按直行连通）；仍无连接的车道由 `_repair` 补齐。"""
    t = "through" if deg <= 2 and turn != "uturn" else turn
    src = None
    if tok is not None and deg > 2:
        src = [i for i, ts in enumerate(tok) if set(ts) & TURN_TOKENS[t]] or None
    if t == "through":
        s = src or list(range(nin))
        pairs = [(i, min(k, nout - 1)) for k, i in enumerate(s)]
        pairs += [(s[-1], j) for j in range(len(s), nout)]
        return sorted(set(pairs))
    if t == "uturn":                                    # 断头掉头：每条驶入车道都可掉头进入最左车道，其余驶出车道自最左车道补齐
        return sorted({(i, 0) for i in range(nin)} | {(0, j) for j in range(1, nout)})
    if t == "left":
        return [(src[0] if src else 0, 0)]
    return [(src[-1] if src else nin - 1, nout - 1)]


def build_lane_graph(roads: OsmRoads, *, inside: Callable[[np.ndarray], np.ndarray] | None = None,
                     z_fn: Callable[[np.ndarray], np.ndarray] | None = None, z_guess_fn: Callable[[np.ndarray], np.ndarray] | None = None,
                     nav: Any = None, nav_profile: str = "vehicle") -> LaneBuild:
    """道路图与车道图（不含文件头）：返回 FeatureCollection（`awr` 头由 `write_lanes` 补写）、确定性统计与告警。"""
    warnings: list[str] = []
    z_fn = z_fn or (lambda xy: np.zeros(len(xy)))
    edges = _edges(roads, inside, warnings)
    vedges = [e for e in edges if e.vehicle]
    ends: dict[str, list[tuple[_Edge, bool]]] = {}
    for e in vedges:
        ends.setdefault(e.nodes[0], []).append((e, True))
        ends.setdefault(e.nodes[1], []).append((e, False))
    deg = {v: len(lst) for v, lst in ends.items()}
    sb = _setbacks(vedges, ends)
    lanes: list[_Lane] = [ln for e in vedges for ln in _lanes_of(e, sb)]
    corridor = Corridor([(e.xy, max(e.width / 2.0, CORRIDOR_HALF_M), e.width / 2.0 + CARRIAGEWAY_TOL_M) for e in vedges])
    conns, junctions, zones, stops = _junctions(lanes, deg, corridor, roads, warnings)
    _stop_lines(lanes, deg, roads, stops)
    succ: dict[str, list[str]] = {}
    for c in conns:
        succ.setdefault(c.src.id, []).append(c.id)
    for ln in lanes:
        ln.succ = succ.get(ln.id, [])
    # 走廊校验（车道；连接段在生成时已校验）
    for s in lanes:
        S = _samples(s.xy)
        ec, ek = corridor.excess(S)
        s.in_corridor, s.excess = bool((ec <= 0).all()), float(max(0.0, ec.max()))
        s.carriage_ok, s.samples = int((ek <= 0).sum()), len(S)
    warnings.extend(f"{s.id}: {s.excess:.2f} m outside the road corridor (M22-FR-074)" for s in [*lanes, *conns] if not s.in_corridor)
    _drape(lanes, conns, z_fn, z_guess_fn, nav, nav_profile)
    routable = _scc(lanes, conns)
    feats = _features(edges, lanes, conns, junctions, zones, stops, z_fn, routable)
    stats = _stats(edges, vedges, lanes, conns, junctions, zones, stops, routable, roads, nav is not None)
    return LaneBuild({"type": "FeatureCollection", "features": feats}, stats, warnings)


def _junctions(lanes: list[_Lane], deg: dict[str, int], corridor: Corridor, roads: OsmRoads, warnings: list[str]) -> tuple[list[_Conn], dict, dict, dict]:
    """连接段、路口与冲突区（节点按 id 字典序处理，确定性）。"""
    inc: dict[str, dict[tuple[str, str], list[_Lane]]] = {}
    out: dict[str, dict[tuple[str, str], list[_Lane]]] = {}
    for ln in lanes:
        inc.setdefault(ln.to_node, {}).setdefault((ln.edge.id, ln.dir), []).append(ln)
        out.setdefault(ln.from_node, {}).setdefault((ln.edge.id, ln.dir), []).append(ln)
    restr: dict[str, list[dict]] = {}
    for r in roads.restrictions:
        restr.setdefault(_nid(r["via"]), []).append(r)
    conns: list[_Conn] = []
    junctions: dict[str, dict] = {}
    zones: dict[str, dict] = {}
    stops: dict[str, dict] = {}
    for v in sorted(set(inc) | set(out)):
        appr = sorted(inc.get(v, {}).items())
        exits = sorted(out.get(v, {}).items())
        dv = deg.get(v, 0)
        ranks = sorted({g[0].edge.rank for _k, g in appr} | {g[0].edge.rank for _k, g in exits})
        R = ranks[-1] if ranks else 0
        here: list[_Conn] = []
        cands: list[tuple[float, list[_Lane], list[_Lane], str]] = []     # 允许的 (航向点积, 驶入组, 驶出组, 转向)
        nway: dict[int, int] = {}
        for _k, gb in exits:
            nway[gb[0].edge.way] = nway.get(gb[0].edge.way, 0) + 1
        for (eid_a, dir_a), ga in appr:
            ga = sorted(ga, key=lambda x: x.index)
            ha = _unit(ga[0].xy[-1] - ga[0].xy[-2])
            for (eid_b, dir_b), gb in exits:
                gb = sorted(gb, key=lambda x: x.index)
                uturn = eid_a == eid_b and dir_a != dir_b
                if uturn and dv != 1:
                    continue
                hb = _unit(gb[0].xy[1] - gb[0].xy[0])
                c = float(ha[0]) * float(hb[0]) + float(ha[1]) * float(hb[1])
                x = float(ha[0]) * float(hb[1]) - float(ha[1]) * float(hb[0])
                turn = "uturn" if uturn or c < COS_UTURN else ("through" if c >= COS_THROUGH else ("left" if x > 0 else "right"))
                if not _allowed(restr.get(v, []), ga[0].edge, gb[0].edge, turn, nway):
                    continue
                cands.append((c, ga, gb, turn))
                for i, j in _lane_pairs(len(ga), len(gb), turn, None if dv <= 2 else _tok(ga), dv):
                    here.append(_connector(v, ga[i], gb[j], turn, corridor))
        _repair(v, here, cands, corridor, warnings)
        here.sort(key=lambda c: (c.src.id, c.dst.id))
        for k, c in enumerate(here):
            c.id = f"t-{v}-{k}"
        if dv >= 3:
            same = len(ranks) == 1
            for c in here:
                c.priority = "equal" if same else ("major" if c.src.edge.rank == R else "minor")
            for _k, ga in appr:
                for ln in ga:
                    ln.rule = "equal" if same else ("major" if ln.edge.rank == R else "minor")
            for a in range(len(here)):
                for b in range(a + 1, len(here)):
                    ca, cb = here[a], here[b]
                    if ca.src is cb.src:
                        continue
                    if ca.dst is cb.dst or _cross_polylines(ca.xy, cb.xy):
                        ca.conflicts.append(cb.id)
                        cb.conflicts.append(ca.id)
            pts = np.concatenate([c.xy for c in here]) if here else np.zeros((0, 2))
            H = _hull(pts) if len(pts) else pts
            jid = f"j-{v[2:]}" if v.startswith("n-") else f"j-{v}"
            zid = f"z-{v[2:]}" if v.startswith("n-") else f"z-{v}"
            if len(H) >= 3 and abs(_poly_area(H)) > 0:
                zones[zid] = {"id": zid, "junction": jid, "ring": H, "connectors": [c.id for c in here], "area_m2": abs(_poly_area(H))}
            else:
                warnings.append(f"{jid}: no conflict zone (degenerate connector hull)")
            node_xy = np.asarray(roads.nodes[int(v[2:])], np.float64) if v.startswith("n-") else (pts.mean(0) if len(pts) else np.zeros(2))
            junctions[jid] = {"id": jid, "node": v, "xy": node_xy, "degree": dv, "rule": "equal" if same else "priority",
                              "major_edges": sorted({g[0].edge.id for _k, g in appr + exits if g[0].edge.rank == R}) if not same else [],
                              "approaches": [f"{k[0]}:{k[1]}" for k, _g in appr], "connectors": [c.id for c in here],
                              "conflict_zone": zid if zid in zones else None}
            if not here:
                warnings.append(f"{jid}: junction without connectors")
        conns += here
    return conns, junctions, zones, stops


def _repair(v: str, here: list[_Conn], cands: list[tuple[float, list[_Lane], list[_Lane], str]], corridor: Corridor,
            warnings: list[str]) -> None:
    """补齐连通：按 `turn:lanes` 或缺省规则没有任何连接的驶入车道，接到航向最接近直行的允许驶出组（序号 min(i, n_out − 1)）；
    没有任何来源的驶出车道，自航向最接近直行的允许驶入组接入（序号 min(j, n_in − 1)）。掉头只在没有其他选择时使用。"""
    order = sorted(cands, key=lambda x: (x[3] == "uturn", -x[0], x[1][0].id, x[2][0].id))
    has_out = {c.src.id for c in here}
    has_in = {c.dst.id for c in here}
    for ga in {id(g[1]): g[1] for g in order}.values():
        for ln in ga:
            if ln.id in has_out:
                continue
            m = next((x for x in order if x[1] is ga), None)
            if m is not None:
                here.append(_connector(v, ln, m[2][min(ln.index, len(m[2]) - 1)], m[3], corridor))
                has_out.add(ln.id)
                has_in.add(here[-1].dst.id)
                warnings.append(f"{ln.id}: no connection by turn:lanes at {v}; connected {m[3]} by default")
    for gb in {id(g[2]): g[2] for g in order}.values():
        for ln in gb:
            if ln.id in has_in:
                continue
            m = next((x for x in order if x[2] is gb), None)
            if m is not None:
                here.append(_connector(v, m[1][min(ln.index, len(m[1]) - 1)], ln, m[3], corridor))
                has_in.add(ln.id)


def _tok(ga: list[_Lane]) -> list[list[str]] | None:
    t = [ln.turns for ln in ga]
    return None if any(x is None for x in t) else t


RESTRICTION_TURN = {"left_turn": "left", "right_turn": "right", "straight_on": "through", "u_turn": "uturn"}


def _allowed(rs: list[dict], ea: _Edge, eb: _Edge, turn: str, nway: Mapping[int, int]) -> bool:
    """OSM 转向限制（via 节点）：`no_*` 禁止 from → to，`only_*` 只允许 from → to。to way 在 via 节点处有两条驶出边（way 未在
    路口切开）时以限制的转向类别区分（`no_left_turn` 只禁左转连接）。"""
    for r in rs:
        if r["from"] != ea.way:
            continue
        hit = r["to"] == eb.way and (nway.get(eb.way, 0) <= 1 or RESTRICTION_TURN.get(r["kind"].split("_", 1)[1]) in (None, turn))
        if r["kind"].startswith("no_") and hit:
            return False
        if r["kind"].startswith("only_") and not hit:
            return False
    return True


def _connector(v: str, a: _Lane, b: _Lane, turn: str, corridor: Corridor) -> _Conn:
    p0, p3 = a.xy[-1], b.xy[0]
    h0, h1 = _unit(a.xy[-1] - a.xy[-2]), _unit(b.xy[1] - b.xy[0])
    ch = p3 - p0
    chord = math.sqrt(float(ch[0]) * float(ch[0]) + float(ch[1]) * float(ch[1]))
    c = float(h0[0]) * float(h1[0]) + float(h0[1]) * float(h1[1])
    k = 1.0 / 3.0 + (1.0 / 3.0) * (1.0 - c) / 2.0          # 直行 1/3、直角 1/2、掉头 2/3（相对弦长）
    best = None
    for f in BEZIER_SHRINK:
        P = _bezier(p0, h0, p3, h1, k * chord * f, CONNECTOR_STEP_M) if f > 0 else _dedupe(np.vstack([p0, p3]))
        ec, ek = corridor.excess(_samples(P))
        cand = (P, f, float(max(0.0, ec.max())), int((ek <= 0).sum()), len(ec))
        if best is None or cand[2] < best[2]:
            best = cand
        if (ec <= 0).all():
            best = cand
            break
    P, f, ex, okk, ns = best
    cn = _Conn("", v, a, b, turn, P, f)
    cn.in_corridor, cn.excess, cn.carriage_ok, cn.samples = ex <= 0, ex, okk, ns
    return cn


def _stop_lines(lanes: list[_Lane], deg: dict[str, int], roads: OsmRoads, stops: dict[str, dict]) -> None:
    """停止线：路口（度 ≥ 3）次路与同级的驶入车道，以及 OSM `highway=stop|give_way` 所在方向的驶入车道，位于车道末端。"""
    sign: dict[tuple[str, str], str] = {}
    for n, t in roads.node_tags.items():
        kind = {"stop": "stop_sign", "give_way": "give_way"}.get(str(t.get("highway")))
        if kind is None:
            continue
        for ln in lanes:
            e = ln.edge
            if n not in e.osm_nodes or ln.index != 0:
                continue
            want = str(t.get("direction", "")).lower()
            fwd_geo = "forward" if not e.reversed_ else "backward"          # OSM way 方向在边几何中的方向
            if want in ("forward", "backward"):
                d = fwd_geo if want == "forward" else ("backward" if fwd_geo == "forward" else "forward")
            else:
                p = np.asarray(roads.nodes[n])
                cum = _cum(e.xy)
                dist = np.sqrt(((e.xy - p) ** 2).sum(1))
                s = float(cum[int(np.argmin(dist))])
                d = "forward" if e.length - s <= s else "backward"           # 标志指向较近的端点
            sign[(e.id, d)] = kind
    for ln in lanes:
        kind = sign.get((ln.edge.id, ln.dir))
        rule = kind or ({"minor": "yield_major", "equal": "first_come"}.get(ln.rule or "") if deg.get(ln.to_node, 0) >= 3 else None)
        if rule is None:
            continue
        p = ln.xy[-1]
        h = _unit(ln.xy[-1] - ln.xy[-2])
        r = np.array([h[1], -h[0]])
        sid = f"s-{ln.id[2:]}"
        stops[sid] = {"id": sid, "lane": ln.id, "rule": rule, "xy": np.vstack([p - r * ln.width / 2.0, p + r * ln.width / 2.0]),
                      "s_m": float(_cum(ln.xy)[-1])}
        ln.stop_line = sid


def _drape(lanes: list[_Lane], conns: list[_Conn], z_fn: Callable, z_guess_fn: Callable | None, nav: Any, profile: str) -> None:
    """高度：NavMesh 吸附（水平距离 ≤ 1 m 的顶点取 NavMesh 高度）；同一段内未吸附的顶点按弧长在吸附顶点之间线性插值（两端外推取
    端值）；整段无吸附顶点时取 `z_fn`（最高命中面，与 `walkable_height` 无导航面时的过渡口径一致）。无 `nav` 时全部取 `z_fn`。"""
    segs = [*lanes, *conns]
    allp = np.concatenate([s.xy for s in segs]) if segs else np.zeros((0, 2))
    z0 = np.asarray(z_fn(allp), np.float64) if len(allp) else np.zeros(0)
    zn = np.full(len(allp), np.nan)
    dist = np.full(len(allp), np.nan)
    if nav is not None and len(allp):
        zg = np.asarray((z_guess_fn or z_fn)(allp), np.float64)
        zg = np.where(np.isfinite(zg), zg, np.where(np.isfinite(z0), z0, 0.0))
        ext = to_mm(NAV_EXT_M)
        for a in range(0, len(allp), NAV_BATCH):
            items = [{"op": "snap", "profile": profile, "p": to_mm((allp[i, 0], allp[i, 1], zg[i])), "ext": ext}
                     for i in range(a, min(a + NAV_BATCH, len(allp)))]
            res = nav.call("batch", items=items)["results"]
            for k, r in enumerate(res):
                if r.get("ok") and r.get("found"):
                    dist[a + k] = r["dist_mm"] / 1000.0
                    if r["dist_mm"] <= NAV_SNAP_OK_M * 1000:
                        zn[a + k] = r["p"][2] / 1000.0
    o = 0
    for s in segs:
        n = len(s.xy)
        zs, ok = zn[o:o + n], np.isfinite(zn[o:o + n])
        if nav is None or not ok.any():
            s.z, src = z0[o:o + n].copy(), "dsm"
        else:
            cum = _cum(s.xy)
            s.z = np.where(ok, zs, np.interp(cum, cum[ok], zs[ok]))
            src = "navmesh" if ok.all() else "navmesh+interp"
        if nav is not None:
            d = dist[o:o + n]
            f = np.isfinite(d)
            s.nav = {"vertices": n, "snapped": int(ok.sum()), "z_source": src,
                     "max_dist_m": round(float(d[f].max()), 3) if f.any() else None}
        o += n


def _scc(lanes: list[_Lane], conns: list[_Conn]) -> set[str]:
    """车道与连接段有向图的最大强连通分量（按总长；并列取最小 id 所在分量）。"""
    succ: dict[str, list[str]] = {ln.id: list(ln.succ) for ln in lanes}
    for c in conns:
        succ[c.id] = [c.dst.id]
    length = {s.id: float(_cum(s.xy)[-1]) for s in [*lanes, *conns]}
    comps = _tarjan(succ)
    if not comps:
        return set()
    comps.sort(key=lambda cc: (-round(sum(length[x] for x in cc), 6), cc[0]))
    return set(comps[0])


def _tarjan(succ: Mapping[str, Sequence[str]]) -> list[list[str]]:
    """迭代 Tarjan 强连通分量（节点与后继按 id 排序，确定性）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    comps: list[list[str]] = []
    counter = 0
    for root in sorted(succ):
        if root in index:
            continue
        work = [(root, iter(sorted(succ.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(succ.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                cc = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    cc.append(w)
                    if w == v:
                        break
                comps.append(sorted(cc))
    return comps


# ---------------------------------------------------------------- 要素与统计


def _xy(P: np.ndarray) -> list[list[float]]:
    return (np.round(np.asarray(P, np.float64), 3) + 0.0).tolist()


def _zl(z: np.ndarray) -> list[float | None]:
    r = np.round(np.asarray(z, np.float64), 3) + 0.0
    return [float(v) if math.isfinite(v) else None for v in r.tolist()]


def _r(v: float, nd: int = 3) -> float:
    return float(np.round(float(v), nd)) + 0.0


def _features(edges: list[_Edge], lanes: list[_Lane], conns: list[_Conn], junctions: dict, zones: dict, stops: dict, z_fn: Callable,
              routable: set[str]) -> list[dict]:
    feats: list[dict] = []
    of_edge: dict[str, list[str]] = {}
    for ln in lanes:
        of_edge.setdefault(ln.edge.id, []).append(ln.id)
    for e in edges:
        z = np.asarray(z_fn(e.xy), np.float64)
        p = {"feature": "edge", "edge_id": e.id, "from_node": e.nodes[0], "to_node": e.nodes[1], "kind": e.kind, "agents": e.agents,
             "width_m": _r(e.width), "speed_mps": _r(e.speed), "oneway": e.oneway, "lanes": e.lanes, "osm_id": f"way/{e.way}", "z_m": _zl(z),
             "highway": e.highway, "length_m": _r(e.length), "speed_source": e.speed_src}
        if e.vehicle:
            p.update({"lanes_forward": e.nf, "lanes_backward": e.nb, "shared": e.shared, "rank": e.rank,
                      "lane_ids": of_edge.get(e.id, [])})
        feats.append({"type": "Feature", "id": e.id, "geometry": {"type": "LineString", "coordinates": _xy(e.xy)}, "properties": p})
    for s in lanes:
        e = s.edge
        p = {"feature": "lane", "lane_id": s.id, "edge_id": e.id, "osm_id": f"way/{e.way}", "dir": s.dir, "index": s.index, "lanes": s.n,
             "from_node": s.from_node, "to_node": s.to_node, "width_m": _r(s.width), "speed_mps": _r(e.speed), "length_m": _r(_cum(s.xy)[-1]),
             "z_m": _zl(s.z), "turns": s.turns, "shared": e.shared, "successors": s.succ, "stop_line": s.stop_line, "junction_rule": s.rule,
             "in_corridor": s.in_corridor, "corridor_excess_m": _r(s.excess), "routable": s.id in routable}
        if s.nav is not None:
            p["nav"] = s.nav
        feats.append({"type": "Feature", "id": s.id, "geometry": {"type": "LineString", "coordinates": _xy(s.xy)}, "properties": p})
    for c in conns:
        rad = _radius_min(c.xy)
        p = {"feature": "connector", "lane_id": c.id, "junction": c.node, "from_lane": c.src.id, "to_lane": c.dst.id, "turn": c.turn,
             "length_m": _r(_cum(c.xy)[-1]), "speed_mps": _r(min(c.src.edge.speed, c.dst.edge.speed)),
             "radius_min_m": None if rad is None else _r(rad), "z_m": _zl(c.z), "priority": c.priority, "conflicts": sorted(c.conflicts),
             "control_scale": c.shrink, "in_corridor": c.in_corridor, "corridor_excess_m": _r(c.excess), "routable": c.id in routable}
        if c.nav is not None:
            p["nav"] = c.nav
        feats.append({"type": "Feature", "id": c.id, "geometry": {"type": "LineString", "coordinates": _xy(c.xy)}, "properties": p})
    for jid in sorted(junctions):
        j = junctions[jid]
        feats.append({"type": "Feature", "id": jid, "geometry": {"type": "Point", "coordinates": _xy(j["xy"])},
                      "properties": {"feature": "junction", **{k: v for k, v in j.items() if k not in ("id", "xy")}}})
    for zid in sorted(zones):
        z = zones[zid]
        R = np.vstack([z["ring"], z["ring"][:1]])
        feats.append({"type": "Feature", "id": zid, "geometry": {"type": "Polygon", "coordinates": [_xy(R)]},
                      "properties": {"feature": "conflict_zone", "junction": z["junction"], "connectors": z["connectors"],
                                     "area_m2": _r(z["area_m2"], 1)}})
    for sid in sorted(stops):
        s = stops[sid]
        feats.append({"type": "Feature", "id": sid, "geometry": {"type": "LineString", "coordinates": _xy(s["xy"])},
                      "properties": {"feature": "stop_line", "lane": s["lane"], "rule": s["rule"], "s_m": _r(s["s_m"])}})
    return feats


def _stats(edges, vedges, lanes, conns, junctions, zones, stops, routable, roads, snapped: bool) -> dict:
    segs = [*lanes, *conns]
    L = {s.id: float(_cum(s.xy)[-1]) for s in segs}
    tot = sum(L[k] for k in sorted(L))
    nsamp = sum(s.samples for s in segs)
    need_stop = [ln for ln in lanes if ln.rule in ("minor", "equal")]
    st = {"edges": len(edges), "vehicle_edges": len(vedges), "edge_length_km": _r(sum(e.length for e in edges) / 1000.0),
          "lanes": len(lanes), "connectors": len(conns), "lane_length_km": _r(sum(L[s.id] for s in lanes) / 1000.0),
          "junctions": len(junctions), "conflict_zones": len(zones), "stop_lines": len(stops),
          "junctions_covered": sum(1 for j in junctions.values() if j["connectors"] and j["conflict_zone"]),
          "in_corridor_frac": _r(sum(1 for s in segs if s.in_corridor) / len(segs), 6) if segs else 1.0,
          "in_corridor_samples_frac": _r(sum(s.samples for s in segs if s.in_corridor) / nsamp, 6) if nsamp else 1.0,
          "carriageway_samples_frac": _r(sum(s.carriage_ok for s in segs) / nsamp, 6) if nsamp else 1.0,
          "max_corridor_excess_m": _r(max((s.excess for s in segs), default=0.0)),
          "routable_segments": len(routable), "routable_length_frac": _r(sum(L[k] for k in sorted(routable)) / tot, 6) if tot else 0.0,
          "speed_from_maxspeed": sum(1 for e in vedges if e.speed_src == "maxspeed"),
          "stop_lines_required": len(need_stop), "stop_lines_missing": sum(1 for ln in need_stop if ln.stop_line is None),
          "restrictions": len(roads.restrictions), "restrictions_unsupported": roads.unsupported_restrictions}
    if snapped:
        v = sum(s.nav["vertices"] for s in segs)
        st["nav_snapped_frac"] = _r(sum(s.nav["snapped"] for s in segs) / v, 6) if v else 0.0
        st["nav_z_dsm_segments"] = sum(1 for s in segs if s.nav["z_source"] == "dsm")
    return st


# ---------------------------------------------------------------- 写出与图层


def content_sha256(doc: Mapping[str, Any]) -> str:
    """`{"awr": 文件头（不含 content_sha256）, "features": [...]}` 的规范 JSON sha256（键排序、紧凑分隔、UTF-8）。"""
    head = {k: v for k, v in dict(doc.get("awr") or {}).items() if k != "content_sha256"}
    body = json.dumps({"awr": head, "features": doc.get("features", [])}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass
class LanesResult:
    layer: LayerSpec
    doc: dict
    qa: dict


def write_lanes(stage: Path, world_id: str, coordinate_sha256: str, roads: OsmRoads, *, sources: Sequence[Mapping[str, str]],
                inside: Callable[[np.ndarray], np.ndarray] | None = None, z_fn: Callable[[np.ndarray], np.ndarray] | None = None,
                z_guess_fn: Callable[[np.ndarray], np.ndarray] | None = None, nav: Any = None) -> LanesResult:
    """navgraph 阶段：写 `geometry/nav/graph.geojson`（`geojson/awr-navgraph@1`），返回图层 `nav.graph` 与 qa 段
    （qa/report.json `glb.lanes`）。`sources` 为 OSM 输入 `[{name, sha256}]`（配方 `osm` 条目，入库清单已校验）。"""
    b = build_lane_graph(roads, inside=inside, z_fn=z_fn, z_guess_fn=z_guess_fn, nav=nav)
    src = [{"name": str(s["name"]), "sha256": str(s["sha256"])} for s in sources]
    head = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "world_id": world_id, "frame": "world", "units": "m",
            "coordinate_sha256": coordinate_sha256, **ODBL, "source_sha256": src[0]["sha256"] if src else None, "sources": src,
            "lanes": {"ext": "awr.lanes.v1", "drive": "right", "lane_width_default_m": LANE_WIDTH_M, "lane_index": "0 = leftmost in travel direction",
                      "corridor": {"half_width_m": f"max(width_m / 2, {CORRIDOR_HALF_M})", "lines": "vehicle edges", "sample_m": SAMPLE_M},
                      "carriageway_tol_m": CARRIAGEWAY_TOL_M, "nav_snap": None if nav is None else {"profile": "vehicle", "ok_m": NAV_SNAP_OK_M},
                      "speed_defaults_mps": dict(sorted(SPEED_MPS.items()))},
            "stats": b.stats}
    doc = {"type": "FeatureCollection", "awr": head, "features": b.doc["features"]}
    head["content_sha256"] = content_sha256(doc)
    p = Path(stage) / HREF
    p.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(doc, separators=(",", ":"), ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    p.write_bytes(data)
    s = b.stats
    layer = LayerSpec(LAYER_ID, "navgraph", "geometry", FORMAT, HREF, files=[HREF],
                      extra={"bytes": len(data), "note": NOTE,
                             "stats": {"edges": s["edges"], "lanes": s["lanes"], "connectors": s["connectors"], "junctions": s["junctions"],
                                       "inCorridorFrac": s["in_corridor_frac"]}})
    qa = {"content_sha256": head["content_sha256"], "file_sha256": hashlib.sha256(data).hexdigest(), "stats": s,
          "warnings": b.warnings[:50], "warnings_total": len(b.warnings),
          "corridor_warnings": sum(1 for w in b.warnings if "outside the road corridor" in w)}
    return LanesResult(layer, doc, qa)


# ---------------------------------------------------------------- 查询库（M22-FR-075）


@dataclass
class LaneSeg:
    """车道或连接段（世界帧；`s` 为累计弧长）。"""

    id: str
    kind: str                       # lane | connector
    xy: np.ndarray
    z: np.ndarray
    s: np.ndarray
    length: float
    speed: float
    succ: list[str]
    props: dict

    @property
    def time_s(self) -> float:
        return self.length / self.speed if self.speed > 0 else math.inf


@dataclass
class LaneRoute:
    """`lane_route` 结果：段序列（车道与连接段交替）、首段起点弧长与末段终点弧长、长度与按限速的行驶时间。"""

    segments: list[str]
    s_start: float
    s_end: float
    length_m: float
    time_s: float


class LaneGraph:
    """`geojson/awr-navgraph@1` 车道扩展的只读查询（确定性 CPU 路径；M23 车辆生成器的路线解析与弧长参数化）。"""

    def __init__(self, doc: Mapping[str, Any]) -> None:
        self.head = dict(doc.get("awr") or {})
        self.segs: dict[str, LaneSeg] = {}
        self.edges: dict[str, dict] = {}
        self.junctions: dict[str, dict] = {}
        self.zones: dict[str, dict] = {}
        self.stops: dict[str, dict] = {}
        for f in doc.get("features", []):
            p = f["properties"]
            k = p.get("feature", "edge")
            if k in ("lane", "connector"):
                xy = np.asarray(f["geometry"]["coordinates"], np.float64)
                z = np.asarray([np.nan if v is None else v for v in p["z_m"]], np.float64)
                succ = list(p["successors"]) if k == "lane" else [p["to_lane"]]
                cum = _cum(xy)                                       # 长度取文件几何（毫米坐标）的弧长，与 route_polyline 一致
                self.segs[f["id"]] = LaneSeg(f["id"], k, xy, z, cum, float(cum[-1]), float(p["speed_mps"]), succ, p)
            elif k == "edge":
                self.edges[f["id"]] = {**p, "xy": np.asarray(f["geometry"]["coordinates"], np.float64)}
            elif k == "junction":
                self.junctions[f["id"]] = {**p, "xy": np.asarray(f["geometry"]["coordinates"], np.float64)}
            elif k == "conflict_zone":
                self.zones[f["id"]] = {**p, "ring": np.asarray(f["geometry"]["coordinates"][0], np.float64)}
            elif k == "stop_line":
                self.stops[f["id"]] = {**p, "xy": np.asarray(f["geometry"]["coordinates"], np.float64)}
        self._kd = None

    @classmethod
    def load(cls, path: str | Path) -> LaneGraph:
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def lanes(self) -> list[str]:
        return sorted(k for k, s in self.segs.items() if s.kind == "lane")

    def routable(self) -> list[str]:
        return sorted(k for k, s in self.segs.items() if s.props.get("routable"))

    def stop_line(self, lane: str) -> dict | None:
        sid = self.segs[lane].props.get("stop_line")
        return None if sid is None else self.stops[sid]

    def conflict_zones(self, junction: str) -> list[dict]:
        """路口的冲突区（`j-<node>` 或节点 id `n-<node>`）。"""
        j = self.junctions.get(junction) or next((x for x in self.junctions.values() if x["node"] == junction), None)
        if j is None or j.get("conflict_zone") is None:
            return []
        return [self.zones[j["conflict_zone"]]]

    def nearest_lane(self, xy: Sequence[float]) -> tuple[str, float, float]:
        """最近的车道（不含连接段）：(lane id, 投影弧长, 水平距离)。"""
        from scipy.spatial import cKDTree

        if self._kd is None:
            ids, A, B, S = [], [], [], []
            for k in self.lanes:
                s = self.segs[k]
                ids += [k] * (len(s.xy) - 1)
                A.append(s.xy[:-1])
                B.append(s.xy[1:])
                S.append(s.s[:-1])
            self._kd = (np.asarray(ids), np.concatenate(A), np.concatenate(B), np.concatenate(S), cKDTree(np.concatenate([np.concatenate(A),
                                                                                                                           np.concatenate(B)])))
        ids, A, B, S, tree = self._kd
        p = np.asarray(xy, np.float64)[:2]
        n = len(A)
        _d, near = tree.query(p, k=min(32, 2 * n))
        cand = sorted({int(i) % n for i in np.atleast_1d(near)})
        best = None
        for i in cand:
            ab = B[i] - A[i]
            den = float(ab[0] * ab[0] + ab[1] * ab[1])
            t = 0.0 if den <= 0 else min(1.0, max(0.0, float((p[0] - A[i, 0]) * ab[0] + (p[1] - A[i, 1]) * ab[1]) / den))
            q = A[i] + ab * t
            d = math.sqrt(float((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2))
            key = (round(d, 9), str(ids[i]), i)
            if best is None or key < best[0]:
                best = (key, str(ids[i]), float(S[i]) + t * math.sqrt(den))
        return best[1], best[2], best[0][0]

    def _ends(self, x: Any, start: bool) -> tuple[list[str], float | None]:
        if isinstance(x, str):
            if x in self.segs:
                return [x], (0.0 if start else self.segs[x].length)
            key = "from_node" if start else "to_node"
            ids = sorted(k for k in self.lanes if self.segs[k].props[key] == x)
            if not ids:
                raise KeyError(f"no lane {'leaves' if start else 'enters'} {x}")
            return ids, None
        lane, s, _d = self.nearest_lane(x)
        return [lane], s

    def _search(self, seeds: Mapping[str, float], targets: set[str]) -> list[str] | None:
        """Dijkstra（代价为行驶时间）；弹出次序 (代价, id) 与严格小于才更新前驱，结果确定。"""
        dist = dict(seeds)
        prev: dict[str, str | None] = {k: None for k in seeds}
        heap = sorted((d, k) for k, d in seeds.items())
        done: set[str] = set()
        while heap:
            d, k = heapq.heappop(heap)
            if k in done:
                continue
            done.add(k)
            if k in targets:
                path = [k]
                while prev[path[-1]] is not None:
                    path.append(prev[path[-1]])
                return path[::-1]
            for w in self.segs[k].succ:
                nd = d + self.segs[w].time_s
                if nd < dist.get(w, math.inf):
                    dist[w], prev[w] = nd, k
                    heapq.heappush(heap, (nd, w))
        return None

    def lane_route(self, a: Any, b: Any) -> LaneRoute | None:
        """最短路（代价为长度除以限速）：`a`、`b` 为段 id、节点 id（`n-<osm>`；从该节点驶出 / 驶入的车道）或世界帧 (E, N)
        （投影到最近车道）。不可达为 None。"""
        src, s0 = self._ends(a, True)
        dst, s1 = self._ends(b, False)
        if len(src) == 1 and src == dst and s0 is not None and s1 is not None:
            ln = self.segs[src[0]]
            if s1 >= s0:
                return LaneRoute([src[0]], s0, s1, s1 - s0, (s1 - s0) / ln.speed)
            tail = self._search({w: ln.time_s + self.segs[w].time_s for w in ln.succ}, {src[0]})   # 同一车道上回到后方：绕行一圈
            path = None if tail is None else [src[0], *tail]
        else:
            path = self._search({k: self.segs[k].time_s for k in src}, set(dst))
        if path is None:
            return None
        first, last = self.segs[path[0]], self.segs[path[-1]]
        a0 = 0.0 if s0 is None else s0
        a1 = last.length if s1 is None else s1
        L = sum(self.segs[k].length for k in path) - a0 - (last.length - a1)
        T = sum(self.segs[k].time_s for k in path) - a0 / first.speed - (last.length - a1) / last.speed
        return LaneRoute(path, a0, a1, L, T)

    def route_polyline(self, r: LaneRoute) -> tuple[np.ndarray, np.ndarray]:
        """路线的连续折线：(xyz (n, 3), 累计弧长 (n,))；相邻段首尾重合点只保留一个。"""
        pts, zs = [], []
        for i, k in enumerate(r.segments):
            s = self.segs[k]
            a = r.s_start if i == 0 else 0.0
            b = r.s_end if i == len(r.segments) - 1 else s.length
            m = (s.s > a) & (s.s < b)
            xy = np.vstack([_point_at(s.xy, s.s, a)[None], s.xy[m], _point_at(s.xy, s.s, b)[None]])
            z = np.concatenate([[np.interp(a, s.s, s.z)], s.z[m], [np.interp(b, s.s, s.z)]])
            if pts:
                xy, z = xy[1:], z[1:]
            pts.append(xy)
            zs.append(z)
        P = np.concatenate(pts)
        return np.column_stack([P, np.concatenate(zs)]), _cum(P)
