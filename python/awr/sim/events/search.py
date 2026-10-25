"""目标搜索事件类型 `target_search`（M23 §15.2 `search` 段、§15.3 第 4 行与规则 2、4；M23-FR-076、083；AWR-05 §5.2.8；D3-AC-62）。

DSL（`search` 段，只对 `target_search`）：

```yaml
search:
  target: target_p                  # 目标演员（person、vehicle 或 object），其路线只能引用保留锚点 search_target
  prior:                            # 先验：高斯混合（各向同性）或栅格
    type: gmm
    components: [{anchor: ev_kiosk, sigma_m: 15, weight: 0.6}, {xy: [40, 30], sigma_m: 10, weight: 0.4}]
    # type: grid → grid{origin_xy, cell_m, nx, ny, weights[ny·nx]}（行主序，y 外 x 内），或 grid_ref（事件目录内的 JSON）
  area_polygon: [[-80, -60], [80, -60], [80, 80], [-80, 80]]
  motion: static                    # static 或 wander（wander_radius_m，缺省 10 m，只对人员）
  window_s: 60                      # 关键事件窗口长度
```

目标路线：首路点 `{t, at: search_target, do: spawn}`，可选末路点 `{t, at: search_target, do: despawn}`。实际位置在编译时由先验按
事件种子抽样（下述算法），只写入锚点表（`anchors.json`）、磁带与真值；`brief.json` 与任务契约 `bench/task.v1` 只含先验
（§15.3 规则 2），test 变体以秘密种子重新抽样。

抽样（确定性，流 19 `search` 通道 9–11、`search_wander` 通道 12–15，键 `(seed, 19, actor_no, goal_n, ch)`，actor_no 为目标的
演员表 idx）：

```text
格点 = 世界对齐的 cell_m 网格中心（x = (i + 0.5)·cell_m），限于 area_polygon 内且在目标 profile 的可走面上（人员、物品取 person，
       车辆取 vehicle）
密度 = gmm：Σ_k w_k / (2π sigma_k²) · exp(−d_k² / (2 sigma_k²))（d_k > 6 sigma_k 记 0）；grid：格点所在先验栅格单元的 weight / 单元面积
q_i  = floor(密度_i / max(密度) · 2^31)（整数权重，q_i = 0 的格点剔除）；表的 sha256 写入 plan.search.sample.table_sha256
r    = ((key(seed, 19, actor_no, 0, 9) >> 11) · Σq) >> 53          （整数运算）
格点 j = 首个满足 cumsum(q)_j > r 的格点
位置 = 格点中心 + ((key(…, 10) >> 11)·2^-53 − 0.5, (key(…, 11) >> 11)·2^-53 − 0.5)·cell_m；越出多边形或可走面时取格点中心；毫米定点化
```

抽样只用整数与 IEEE 基本运算（exp 只用于权重表，权重表经 2^31 量化并以 sha256 核对）。`motion = wander` 时，以通道 12、13 在
半径内拒绝抽样目标点（`goal_n = 16·g + 尝试序号`），通道 14 取停留时长 [5, 20] s，沿可走面路径以步态中值的 0.8 倍行进；ADR-196
的 Detour 后端提供 `walkable_batch` 时以其判定可走（否则按 NavGrid 栅格或逐点吸附距离 0 判定）。

关键事件（门 3，`verify/search.py`）：把目标在场区间（状态 1、2 的连续段）按 `window_s` 切分为 K = ⌈L / W⌉ 个窗口
`target_window_<k>`（参与者为目标，末窗口可短于 W），另产出单个 `target_present` 事件作为 EU 的真值（§15.3 规则 4）。
"""

from __future__ import annotations

import bisect
import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from awr.sim.events.anchors import anchor_xy, point_in_polygon
from awr.sim.sensors import cbrng

__all__ = ["CH_POS", "CH_WANDER", "DEFAULTS", "EVENT_TYPE", "SAMPLE_DEFAULTS", "SEARCH_ANCHOR", "Sampler", "brief", "density", "gate1_issues",
           "key_windows", "leak_scan", "materialize", "prepare", "resolve_prior", "search_spec", "support_fraction"]

EVENT_TYPE = "target_search"
SEARCH_ANCHOR = "search_target"                    # 目标路线引用的保留锚点 id；编译时替换为抽样位置
STREAM = 19
CH_POS = (9, 11)                                   # 格点选择、x 抖动、y 抖动
CH_WANDER = (12, 15)                               # 游走目标点 x、y，停留时长，保留
for _n, _r in (("search", CH_POS), ("search_wander", CH_WANDER)):
    cbrng.CHANNELS.setdefault((STREAM, _n), _r)
TARGET_CLASSES = ("person", "vehicle", "object")
WANDER_CLASSES = ("person",)
DEFAULTS = {"motion": "static", "wander_radius_m": 10.0, "window_s": 60.0}
SAMPLE_DEFAULTS = {"cell_m": 1.0, "support_min": 0.9, "cut_sigma": 6.0, "q_bits": 31, "max_cells": 4_000_000}
WANDER = {"dwell_s": (5.0, 20.0), "speed_frac": 0.8, "tries": 16, "max_goals": 64, "min_leg_m": 1.0}
INV53 = 1.0 / 9007199254740992.0


# ---------------------------------------------------------------------- 先验
def search_spec(doc_or_plan: dict) -> dict | None:
    """`search` 段补全缺省值（不解析锚点）；非目标搜索事件或缺少该段返回 None。"""
    s = doc_or_plan.get("search")
    if doc_or_plan.get("event_type") != EVENT_TYPE or not isinstance(s, dict):
        return None
    return {**DEFAULTS, **copy.deepcopy(s)}


def resolve_prior(prior: dict, anchors: dict[str, dict]) -> dict:
    """规范化先验：gmm 分量的锚点解析为 xy（保留锚点 id）、权重缺省 1；grid 保持原样（`grid_ref` 须已由 `prepare` 内联）。
    无法解析时抛 ValueError（门 1 先行报告）。"""
    if prior["type"] == "gmm":
        comps = []
        for c in prior.get("components", []):
            if "xy" in c:
                xy = [float(c["xy"][0]), float(c["xy"][1])]
            elif c.get("anchor") in anchors:
                xy = list(anchor_xy(anchors[c["anchor"]]))
            else:
                raise ValueError(f"prior component anchor cannot be resolved: {c.get('anchor')}")
            comps.append({**({"anchor": c["anchor"]} if "anchor" in c else {}), "xy": xy, "sigma_m": float(c["sigma_m"]),
                          "weight": float(c.get("weight", 1.0))})
        if not comps:
            raise ValueError("gmm prior has no components")
        return {"type": "gmm", "components": comps}
    g = prior.get("grid")
    if not isinstance(g, dict):
        raise ValueError("grid prior has no grid")
    return {"type": "grid", "grid": {"origin_xy": [float(g["origin_xy"][0]), float(g["origin_xy"][1])], "cell_m": float(g["cell_m"]),
                                     "nx": int(g["nx"]), "ny": int(g["ny"]), "weights": [float(w) for w in g["weights"]]}}


def density(prior: dict, x: np.ndarray, y: np.ndarray, cut_sigma: float = SAMPLE_DEFAULTS["cut_sigma"]) -> np.ndarray:
    """规范化先验在点 (x, y) 处的未归一化密度（每 m² 的质量；gmm 的分量权重未归一）。"""
    x, y = np.asarray(x, np.float64), np.asarray(y, np.float64)
    out = np.zeros(np.broadcast(x, y).shape)
    if prior["type"] == "gmm":
        for c in prior["components"]:
            s2 = c["sigma_m"] * c["sigma_m"]
            d2 = (x - c["xy"][0]) ** 2 + (y - c["xy"][1]) ** 2
            out += np.where(d2 <= (cut_sigma * cut_sigma) * s2, c["weight"] / (2.0 * math.pi * s2) * np.exp(-d2 / (2.0 * s2)), 0.0)
        return out
    g = prior["grid"]
    cm = g["cell_m"]
    ix = np.floor((x - g["origin_xy"][0]) / cm).astype(np.int64)
    iy = np.floor((y - g["origin_xy"][1]) / cm).astype(np.int64)
    ok = (ix >= 0) & (ix < g["nx"]) & (iy >= 0) & (iy < g["ny"])
    W = np.asarray(g["weights"], np.float64).reshape(g["ny"], g["nx"])
    out[ok] = W[iy[ok], ix[ok]] / (cm * cm)
    return out


def _prior_bbox(prior: dict, cut_sigma: float = 4.0) -> tuple[float, float, float, float]:
    if prior["type"] == "gmm":
        P = [(c["xy"][0], c["xy"][1], cut_sigma * c["sigma_m"]) for c in prior["components"]]
        return min(x - r for x, _y, r in P), min(y - r for _x, y, r in P), max(x + r for x, _y, r in P), max(y + r for _x, y, r in P)
    g = prior["grid"]
    x0, y0 = g["origin_xy"]
    return x0, y0, x0 + g["nx"] * g["cell_m"], y0 + g["ny"] * g["cell_m"]


def support_fraction(prior: dict, polygon: list[list[float]], n_max: int = 1_000_000) -> float:
    """先验质量落在多边形内的比例（门 1：先验支撑集与 area_polygon 相交 ≥ 90%）。gmm 在各分量 ±4 sigma 包围盒上以 ≤ sigma_min / 4 的
    格距数值积分（格点数 ≤ n_max）；grid 以每个先验单元 4 × 4 个子点计面积比例。"""
    P = np.asarray(polygon, np.float64)
    if prior["type"] == "grid":
        g = prior["grid"]
        W = np.asarray(g["weights"], np.float64).reshape(g["ny"], g["nx"])
        tot = float(W.sum())
        if tot <= 0:
            return 0.0
        sub = (np.arange(4) + 0.5) / 4.0
        iy, ix, sy, sx = np.meshgrid(np.arange(g["ny"]), np.arange(g["nx"]), sub, sub, indexing="ij")
        x = g["origin_xy"][0] + (ix + sx) * g["cell_m"]
        y = g["origin_xy"][1] + (iy + sy) * g["cell_m"]
        frac = point_in_polygon(P, x.ravel(), y.ravel()).reshape(x.shape).mean(axis=(2, 3))
        return float((W * frac).sum() / tot)
    x0, y0, x1, y1 = _prior_bbox(prior)
    h = max(0.05, min(c["sigma_m"] for c in prior["components"]) / 4.0)
    while ((x1 - x0) / h + 1) * ((y1 - y0) / h + 1) > n_max:
        h *= 1.5
    X, Y = np.meshgrid(np.arange(x0 + h / 2, x1, h), np.arange(y0 + h / 2, y1, h))
    D = density(prior, X.ravel(), Y.ravel(), cut_sigma=8.0)
    tot = float(D.sum())
    return 0.0 if tot <= 0 else float(D[point_in_polygon(P, X.ravel(), Y.ravel())].sum() / tot)


def polygon_area(P: list[list[float]]) -> float:
    A = np.asarray(P, np.float64)
    return float(abs(np.sum(A[:, 0] * np.roll(A[:, 1], -1) - np.roll(A[:, 0], -1) * A[:, 1])) / 2.0)


# ---------------------------------------------------------------------- 抽样
def walkable_mask(nav: Any, xy: np.ndarray) -> np.ndarray:
    """(N,) bool：点是否在 nav 的可走面（主岛）上。优先 `walkable_batch`（Detour 后端，ADR-196），其次 NavGrid 栅格，
    再次逐点 `walkable` 或吸附距离为 0。"""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    fn = getattr(nav, "walkable_batch", None)
    if fn is not None:
        return np.asarray(fn(xy), bool)
    if all(hasattr(nav, k) for k in ("walk", "x0", "y0", "nx", "ny")):
        from awr.sim.events.nav import CELL_M

        ix = np.floor((xy[:, 0] - nav.x0) / CELL_M).astype(np.int64)
        iy = np.floor((xy[:, 1] - nav.y0) / CELL_M).astype(np.int64)
        ok = (ix >= 0) & (ix < nav.nx) & (iy >= 0) & (iy < nav.ny)
        out = np.zeros(len(xy), bool)
        out[ok] = np.asarray(nav.walk, bool)[iy[ok], ix[ok]]
        return out
    if hasattr(nav, "walkable"):
        return np.array([bool(nav.walkable(float(x), float(y))) for x, y in xy], bool)
    return np.array([nav.snap(SEARCH_ANCHOR, float(x), float(y)).dist_m <= 1e-9 for x, y in xy], bool)


def _u53(seed: int, actor_no: int, goal_n: int, ch: int) -> int:
    return int(cbrng.key(int(seed), STREAM, int(actor_no), int(goal_n), int(ch))) >> 11


def _mm(v: float) -> float:
    return math.floor(float(v) * 1000.0 + 0.5) / 1000.0


class Sampler:
    """先验 × 任务区 × 可走面上的离散抽样表（模块说明的算法）。同一 (先验, 多边形, 可走面, cell_m) 的表与 `table_sha256` 相同；
    `sample(seed, actor_no)` 只依赖键，不同种子之间互不影响（test 变体的秘密种子重新抽样）。"""

    def __init__(self, prior: dict, polygon: list[list[float]], nav: Any, *, cell_m: float = SAMPLE_DEFAULTS["cell_m"],
                 q_bits: int = SAMPLE_DEFAULTS["q_bits"], cut_sigma: float = SAMPLE_DEFAULTS["cut_sigma"],
                 max_cells: int = SAMPLE_DEFAULTS["max_cells"]) -> None:
        self.prior, self.cell_m, self.nav = prior, float(cell_m), nav
        self.polygon = np.asarray(polygon, np.float64)
        c = self.cell_m
        x0, y0 = self.polygon.min(0)
        x1, y1 = self.polygon.max(0)
        i0, i1 = math.floor(x0 / c), math.ceil(x1 / c)
        j0, j1 = math.floor(y0 / c), math.ceil(y1 / c)
        if (i1 - i0) * (j1 - j0) > max_cells:
            raise ValueError(f"search area too large for cell_m = {c:g}: {(i1 - i0) * (j1 - j0)} cells")
        gi, gj = np.meshgrid(np.arange(i0, i1), np.arange(j0, j1))            # y 外 x 内（行主序）
        gi, gj = gi.ravel(), gj.ravel()
        cx, cy = (gi + 0.5) * c, (gj + 0.5) * c
        m = point_in_polygon(self.polygon, cx, cy)
        gi, gj, cx, cy = gi[m], gj[m], cx[m], cy[m]
        if len(cx):
            m = walkable_mask(nav, np.stack([cx, cy], 1))
            gi, gj, cx, cy = gi[m], gj[m], cx[m], cy[m]
        d = density(prior, cx, cy, cut_sigma=cut_sigma) if len(cx) else np.zeros(0)
        dmax = float(d.max()) if len(d) else 0.0
        q = np.floor(d / dmax * float(1 << q_bits)).astype(np.int64) if dmax > 0 else np.zeros(len(d), np.int64)
        keep = q > 0
        self.ij = np.stack([gi[keep], gj[keep]], 1).astype(np.int64)
        self.centers = np.stack([cx[keep], cy[keep]], 1)
        self.q = q[keep]
        self.cum = np.cumsum(self.q) if len(self.q) else np.zeros(0, np.int64)
        self.total = int(self.cum[-1]) if len(self.cum) else 0
        h = hashlib.sha256(f"awr-search-table@1|{c!r}|{q_bits}|{len(self.q)}|".encode())
        h.update(self.ij.astype("<i8").tobytes())
        h.update(self.q.astype("<i8").tobytes())
        self.table_sha256 = h.hexdigest()
        self._cum_list = self.cum.tolist()

    @property
    def n_cells(self) -> int:
        return len(self.q)

    def probabilities(self) -> np.ndarray:
        """各格点被选中的精确概率 q_i / Σq（测试与诊断用）。"""
        return self.q / float(self.total) if self.total else np.zeros(0)

    def cell_index(self, seed: int, actor_no: int) -> int:
        if not self.total:
            raise ValueError("the search prior has no mass on the walkable part of the search area")
        r = (_u53(seed, actor_no, 0, CH_POS[0]) * self.total) >> 53
        return bisect.bisect_right(self._cum_list, r)

    def sample(self, seed: int, actor_no: int) -> tuple[float, float]:
        """实际位置（毫米定点化的 world 帧 m）。"""
        j = self.cell_index(seed, actor_no)
        cx, cy = float(self.centers[j, 0]), float(self.centers[j, 1])
        x = cx + (_u53(seed, actor_no, 0, CH_POS[0] + 1) * INV53 - 0.5) * self.cell_m
        y = cy + (_u53(seed, actor_no, 0, CH_POS[0] + 2) * INV53 - 0.5) * self.cell_m
        x, y = _mm(x), _mm(y)
        if not (point_in_polygon(self.polygon, np.array([x]), np.array([y]))[0] and walkable_mask(self.nav, np.array([[x, y]]))[0]):
            x, y = _mm(cx), _mm(cy)
        return x, y

    def describe(self) -> dict:
        return {"stream": STREAM, "channels": list(CH_POS), "cell_m": self.cell_m, "n_cells": self.n_cells, "table_sha256": self.table_sha256}


# ---------------------------------------------------------------------- 门 1
def _profile(cls: str) -> str:
    return "vehicle" if cls == "vehicle" else "person"


def gate1_issues(doc: dict, anchors: dict[str, dict]) -> list:
    """门 1 必备配置（M23-FR-071，§15.3 第 4 行第 2 列；`E-TYPE` 611，`detail.rule` 区分条目，一次报出全部）：

    `search_missing`（缺少 search 段）、`target_unknown`、`target_class`（person、vehicle、object）、`target_route`（目标路线只引用
    `search_target`：首路点 spawn，可选末路点 despawn）、`reserved_anchor`（锚点表已有同名锚点、其他演员或简报引用保留锚点）、
    `prior_component`（gmm 分量须给出 xy 或已知锚点之一）、`prior_grid`（grid 或 grid_ref 可解析、权重个数为 nx·ny、非负且和 > 0）、
    `area_polygon`（面积 > 0）、`prior_support`（先验质量落在任务区内 ≥ 90%）、`wander_class`（游走只对人员）、`window`（window_s
    ≤ 事件时长）。"""
    from awr.sim.events.verify.registry import type_issue

    def iss(path: str, zh: str, en: str, rule: str, **d):
        return type_issue(path, zh, en, event_type=EVENT_TYPE, rule=rule, **d)

    s = search_spec(doc)
    if s is None:
        return [iss("/search", "目标搜索事件须给出 search 段", "A target search event requires the search section", "search_missing")]
    out = []
    acts = doc.get("actors", [])
    idx = {a["id"]: i for i, a in enumerate(acts)}
    ti = idx.get(s["target"])
    if ti is None:
        out.append(iss("/search/target", f"搜索目标 {s['target']} 不是脚本演员", f"Search target {s['target']} is not a scripted actor",
                       "target_unknown", actor=s["target"]))
    else:
        a = acts[ti]
        if a["class"] not in TARGET_CLASSES:
            out.append(iss(f"/actors/{ti}/class", f"搜索目标须为人员、车辆或物品，{a['id']} 为 {a['class']}",
                           f"The search target must be a person, vehicle or object, {a['id']} is a {a['class']}", "target_class", actor=a["id"]))
        r = a["route"]
        ok = all(w["at"] == SEARCH_ANCHOR and not w.get("via") for w in r) and r[0].get("do", "pass") == "spawn" and \
            len(r) <= 2 and (len(r) == 1 or r[1].get("do", "pass") == "despawn")
        if not ok:
            out.append(iss(f"/actors/{ti}/route", f"搜索目标的路线须为 spawn（可选 despawn），且只引用保留锚点 {SEARCH_ANCHOR}",
                           f"The search target route must be spawn (optionally despawn) at the reserved anchor {SEARCH_ANCHOR} only",
                           "target_route", actor=a["id"]))
        if s["motion"] == "wander" and a["class"] not in WANDER_CLASSES:
            out.append(iss("/search/motion", f"游走只对人员目标，{a['id']} 为 {a['class']}", f"Wandering applies to person targets only, "
                           f"{a['id']} is a {a['class']}", "wander_class", actor=a["id"]))
    an = anchors.get(SEARCH_ANCHOR)
    if an is not None and not an.get("search_placeholder"):
        out.append(iss("/search", f"锚点表中已有保留锚点 {SEARCH_ANCHOR}", f"The anchor table already defines the reserved anchor {SEARCH_ANCHOR}",
                       "reserved_anchor", anchor=SEARCH_ANCHOR))
    for i, a in enumerate(acts):
        if i != ti and any(w["at"] == SEARCH_ANCHOR or SEARCH_ANCHOR in w.get("via", []) for w in a["route"]):
            out.append(iss(f"/actors/{i}/route", f"只有搜索目标可以引用保留锚点 {SEARCH_ANCHOR}",
                           f"Only the search target may reference the reserved anchor {SEARCH_ANCHOR}", "reserved_anchor", actor=a["id"]))
    for j, ref in enumerate((doc.get("brief") or {}).get("public_anchors", [])):
        if ref == SEARCH_ANCHOR:
            out.append(iss(f"/brief/public_anchors/{j}", "简报不得公开保留锚点（实际位置）", "The brief must not publish the reserved anchor "
                           "(actual position)", "reserved_anchor", anchor=SEARCH_ANCHOR))
    pr = s["prior"]
    prior_ok = True
    if pr["type"] == "gmm":
        comps = pr.get("components") or []
        if not comps:
            prior_ok = False
            out.append(iss("/search/prior/components", "高斯混合先验至少需要 1 个分量", "A gmm prior requires at least one component",
                           "prior_component"))
        for k, c in enumerate(comps):
            if ("xy" in c) == ("anchor" in c) or ("anchor" in c and c["anchor"] not in anchors):
                prior_ok = False
                out.append(iss(f"/search/prior/components/{k}", "先验分量须给出 xy 或已知锚点之一", "A prior component requires exactly one of xy "
                               "or a known anchor", "prior_component", anchor=c.get("anchor")))
    else:
        g = pr.get("grid")
        if not isinstance(g, dict):
            prior_ok = False
            out.append(iss("/search/prior", "栅格先验须给出 grid，或可解析的 grid_ref", "A grid prior requires grid or a resolvable grid_ref",
                           "prior_grid", grid_ref=pr.get("grid_ref")))
        else:
            w = np.asarray(g.get("weights", []), np.float64)
            if len(w) != int(g["nx"]) * int(g["ny"]) or (w < 0).any() or not w.sum() > 0:
                prior_ok = False
                out.append(iss("/search/prior/grid/weights", "栅格权重个数须为 nx·ny、非负且和大于 0",
                               "Grid weights must number nx·ny, be non-negative and sum above 0", "prior_grid", n=len(w),
                               expected=int(g["nx"]) * int(g["ny"])))
    area = s["area_polygon"]
    if polygon_area(area) <= 0:
        prior_ok = False
        out.append(iss("/search/area_polygon", "任务区多边形面积须大于 0", "The search area polygon must have a positive area", "area_polygon"))
    if prior_ok:
        frac = support_fraction(resolve_prior(pr, anchors), area)
        if frac < SAMPLE_DEFAULTS["support_min"]:
            out.append(iss("/search/prior", f"先验质量只有 {frac:.1%} 落在任务区内，至少需要 90%",
                           f"Only {frac:.1%} of the prior mass lies inside the search area, at least 90% is required", "prior_support",
                           fraction=round(frac, 4), required=SAMPLE_DEFAULTS["support_min"]))
    if float(s["window_s"]) > float(doc["duration_s"]):
        out.append(iss("/search/window_s", "关键事件窗口长于事件时长", "The key-event window is longer than the event", "window",
                       window_s=float(s["window_s"]), duration_s=float(doc["duration_s"])))
    return out


def prepare(doc: dict, anchors: dict[str, dict], base_dir: Path | None = None) -> tuple[dict, dict[str, dict]]:
    """门 1 之前（compile 调用）：内联 `prior.grid_ref`（相对事件目录的 JSON，失败时保留原样由门 1 报 `prior_grid`），并在锚点表中
    加入保留锚点的占位（zone，多边形为任务区，使门 1 的锚点检查通过、事件舞台覆盖整个任务区）。非目标搜索事件原样返回。"""
    s = doc.get("search")
    if doc.get("event_type") != EVENT_TYPE or not isinstance(s, dict):
        return doc, anchors
    pr = s.get("prior") or {}
    if pr.get("type") == "grid" and "grid" not in pr and isinstance(pr.get("grid_ref"), str) and base_dir is not None:
        p = (Path(base_dir) / pr["grid_ref"]).resolve()
        try:
            if Path(base_dir).resolve() in p.parents:
                g = json.loads(p.read_text(encoding="utf-8"))
                doc = copy.deepcopy(doc)
                doc["search"]["prior"] = {"type": "grid", "grid": g}
        except (OSError, ValueError):
            pass
    if SEARCH_ANCHOR not in anchors and isinstance(s.get("area_polygon"), list) and len(s["area_polygon"]) >= 3:
        cls = next((a["class"] for a in doc.get("actors", []) if a.get("id") == s.get("target")), "person")
        anchors = {**anchors, SEARCH_ANCHOR: {"id": SEARCH_ANCHOR, "kind": "zone", "profile": _profile(cls),
                                              "polygon": [[float(x), float(y)] for x, y in s["area_polygon"]], "search_placeholder": True}}
        anchors = {k: anchors[k] for k in sorted(anchors)}
    return doc, anchors


# ---------------------------------------------------------------------- 物化（门 1 之后、门 2 之前）
def _wander(plan: dict, a: dict, stage: Any, nav: Any, center: tuple[float, float], s: dict, actor_no: int, poly: np.ndarray) -> list[dict]:
    """游走路线（模块说明）：返回替换后的路线与新增锚点（写入 stage.anchors）。"""
    from awr.sim.events.gen import t_min
    from awr.sim.events.nav import rounded_length

    seed, R = int(plan["seed"]), float(s["wander_radius_m"])
    r0 = a["route"][0]
    t_end = a["route"][1]["t_ms"] if len(a["route"]) > 1 else plan["duration_ms"]
    v = float(a["gait"]["median_mps"]) * WANDER["speed_frac"]
    acc = float(a["gait"]["accel_mps2"])
    lo, hi = WANDER["dwell_s"]

    def dwell_ms(g: int) -> int:
        return math.floor((lo + (hi - lo) * _u53(seed, actor_no, 16 * g, CH_WANDER[0] + 2) * INV53) * 10.0) * 100

    cur, cur_id = center, SEARCH_ANCHOR
    route = [{**r0, "at": SEARCH_ANCHOR, "do": "spawn", "dwell_ms": dwell_ms(0), "via": []}]
    t = r0["t_ms"] + route[0]["dwell_ms"]
    for g in range(1, WANDER["max_goals"] + 1):
        goal = None
        for k in range(WANDER["tries"]):
            gx = center[0] + (2.0 * _u53(seed, actor_no, 16 * g + k, CH_WANDER[0]) * INV53 - 1.0) * R
            gy = center[1] + (2.0 * _u53(seed, actor_no, 16 * g + k, CH_WANDER[0] + 1) * INV53 - 1.0) * R
            gx, gy = _mm(gx), _mm(gy)
            if math.hypot(gx - center[0], gy - center[1]) > R or math.hypot(gx - cur[0], gy - cur[1]) < WANDER["min_leg_m"]:
                continue
            if not (point_in_polygon(poly, np.array([gx]), np.array([gy]))[0] and walkable_mask(nav, np.array([[gx, gy]]))[0]):
                continue
            path = nav.route([(cur[0], cur[1]), (gx, gy)])
            if path is None:
                continue
            goal = (gx, gy, rounded_length(path))
            break
        if goal is None:
            continue
        T_ms = math.ceil(t_min(goal[2], acc, v) * 10.0) * 100
        dw = dwell_ms(g)
        if t + T_ms + 100 > t_end:
            break
        gid = f"{SEARCH_ANCHOR}~{g}"
        stage.anchors[gid] = {"id": gid, "kind": "point", "profile": _profile(a["class"]), "xy": [goal[0], goal[1]]}
        route.append({**r0, "t_ms": t + T_ms, "at": gid, "do": "pass", "dwell_ms": dw, "via": [], "target": None})
        cur, cur_id, t = (goal[0], goal[1]), gid, t + T_ms + dw
    if len(a["route"]) > 1:                                        # 作者给出 despawn：在最后一个目标点停留至 despawn 时刻后离场
        last = route[-1]
        if len(route) == 1:
            last["dwell_ms"] = max(0, t_end - last["t_ms"])
            route.append({**a["route"][1], "at": cur_id, "via": []})
        else:
            last["do"], last["dwell_ms"] = "despawn", max(0, t_end - last["t_ms"])
    return route


def materialize(plan: dict, doc: dict, stage: Any) -> dict:
    """门 2 之前（compile 调用，舞台已装载）：按事件种子抽样目标实际位置，以点锚点替换保留锚点的占位（`stage.anchors` 原位更新，
    随后的 `anchors_sha256` 与磁带输入哈希因此覆盖实际位置），游走时生成路线；`plan.search` 写入规范化的 search 段（先验锚点
    已解析）与抽样诊断（表 sha256、格点数，不含实际位置）。返回新的 plan；非目标搜索事件原样返回。"""
    s = search_spec(doc)
    if s is None:
        return plan
    from awr.sim.events.gen import actor_order

    anchors = stage.anchors
    prior = resolve_prior(s["prior"], anchors)
    plan = copy.deepcopy(plan)
    order = actor_order(plan)
    actor_no = next(i for i, a in enumerate(order) if a["id"] == s["target"])
    a = next(x for x in plan["actors"] if x["id"] == s["target"])
    prof = _profile(a["class"])
    nav = stage.nav[prof]
    smp = Sampler(prior, s["area_polygon"], nav, cell_m=SAMPLE_DEFAULTS["cell_m"])
    x, y = smp.sample(int(plan["seed"]), actor_no)
    anchors[SEARCH_ANCHOR] = {"id": SEARCH_ANCHOR, "kind": "point", "profile": prof, "xy": [x, y]}
    if s["motion"] == "wander":
        a["route"] = _wander(plan, a, stage, nav, (x, y), s, actor_no, np.asarray(s["area_polygon"], np.float64))
    items = sorted(anchors.items())                                         # 锚点表保持按 id 排序（原位）
    anchors.clear()
    anchors.update(items)
    plan["search"] = {"target": s["target"], "prior": prior, "area_polygon": [[float(p), float(q)] for p, q in s["area_polygon"]],
                      "motion": s["motion"], **({"wander_radius_m": float(s["wander_radius_m"])} if s["motion"] == "wander" else {}),
                      "window_ms": math.floor(float(s["window_s"]) * 1000.0 + 0.5), "sample": {**smp.describe(), "actor_no": actor_no,
                                                                                               "profile": prof}}
    return plan


# ---------------------------------------------------------------------- 关键事件
def key_windows(interval_ms: tuple[int, int], window_ms: int) -> list[tuple[int, int]]:
    """在场区间 [a, b]（毫秒，含两端的帧时刻）按 W 切分：K = max(1, ⌈(b − a) / W⌉)，第 k 个窗口 [a + (k−1)W, min(a + kW, b)]。"""
    a, b = int(interval_ms[0]), int(interval_ms[1])
    W = int(window_ms)
    K = max(1, -(-(b - a) // W))
    return [(a + k * W, min(a + (k + 1) * W, b)) for k in range(K)]


# ---------------------------------------------------------------------- 简报与任务契约（只含先验）
def brief(plan: dict, anchors: dict[str, dict], truth_rev: int = 1) -> dict | None:
    """`brief.json`（`drone4d.event_brief.v1`）：公开锚点（排除保留锚点）、目标外观描述、任务区与 `search`（先验、任务区、运动
    方式、窗口长度、目标类别）；不含实际位置、演员路线与时刻（§15.3 规则 2、M23-FR-083）。非目标搜索事件返回 None。"""
    s = plan.get("search")
    if plan.get("event_type") != EVENT_TYPE or not s:
        return None
    b = plan.get("brief") or {}
    acts = {a["id"]: a for a in plan["actors"]}
    pub = []
    for aid in b.get("public_anchors", []):
        an = anchors.get(aid)
        if an is None or aid == SEARCH_ANCHOR or aid.startswith(SEARCH_ANCHOR + "~"):
            continue
        x, y = anchor_xy(an)
        pub.append({"id": aid, **({"kind": an["kind"]} if "kind" in an else {}), "pos_enu_m": [x, y, float(an.get("z", 0.0))]})
    targets = [dict(t) for t in b.get("targets", [])]
    tgt = acts[s["target"]]
    if not any(t.get("actor") == s["target"] for t in targets):
        desc = {}
        if tgt.get("appearance", {}).get("asset", "").startswith("appearance_lib@1/"):
            from awr.sim.events.appearance import load_library

            e = load_library().get(tgt["appearance"]["asset"])
            d = (e.entry.get("description") or {}) if e is not None else {}
            desc = {f"description_{k}": d[k] for k in ("zh", "en") if k in d}
        targets.append({"actor": s["target"], **desc})
    area = [list(p) for p in (b.get("task_area") or s["area_polygon"])]
    prior = {"type": s["prior"]["type"],
             **({"components": [{"xy": c["xy"], "sigma_m": c["sigma_m"], "weight": c["weight"]} for c in s["prior"]["components"]]}
                if s["prior"]["type"] == "gmm" else {"grid": s["prior"]["grid"]})}
    return {"schema": "drone4d.event_brief.v1", "event_id": plan["event_id"], "event_version": int(plan["version"]), "truth_rev": int(truth_rev),
            "event_type": EVENT_TYPE, "public_anchors": pub, "targets": targets, "task_area": area,
            "search": {"target_class": tgt["class"], "prior": prior, "area_polygon": s["area_polygon"], "motion": s["motion"],
                       **({"wander_radius_m": s["wander_radius_m"]} if "wander_radius_m" in s else {}),
                       "window_s": s["window_ms"] / 1000.0}}


def leak_scan(obj: Any, positions_xy: np.ndarray, *, tol_m: float = 0.001, path: str = "") -> list[dict]:
    """实际位置泄漏扫描（§15.3 规则 2）：`obj`（JSON 值）中任一 ≥ 2 个数的数组的相邻两数 (a, b) 与 `positions_xy` 中某一点的
    (x, y) 两坐标均相差 ≤ tol_m（或均为其毫米整数值），或字符串中出现某一点坐标的 2 位、3 位小数文本（"x, y" 或 "x,y"），
    计为 1 处命中。返回 [{path, value}]。"""
    P = np.asarray(positions_xy, np.float64).reshape(-1, 2)
    hits: list[dict] = []
    if isinstance(obj, dict):
        for k in sorted(obj):
            hits += leak_scan(obj[k], P, tol_m=tol_m, path=f"{path}/{k}")
        return hits
    if isinstance(obj, (list, tuple)):
        nums = [v for v in obj if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if len(nums) == len(obj) and len(nums) >= 2 and len(P):
            for j in range(len(nums) - 1):
                a, b = float(nums[j]), float(nums[j + 1])
                m = (np.abs(P[:, 0] - a) <= tol_m) & (np.abs(P[:, 1] - b) <= tol_m)
                m |= (np.abs(P[:, 0] * 1000.0 - a) <= 0.5) & (np.abs(P[:, 1] * 1000.0 - b) <= 0.5)
                if m.any():
                    hits.append({"path": f"{path}/{j}", "value": [a, b]})
        for j, v in enumerate(obj):
            if isinstance(v, (dict, list, tuple, str)):
                hits += leak_scan(v, P, tol_m=tol_m, path=f"{path}/{j}")
        return hits
    if isinstance(obj, str) and len(P):
        for x, y in P:
            for nd in (2, 3):
                for sep in (", ", ","):
                    t = f"{x:.{nd}f}{sep}{y:.{nd}f}"
                    if t in obj:
                        hits.append({"path": path, "value": t})
    return hits
