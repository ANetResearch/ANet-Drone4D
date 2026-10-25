"""目标搜索事件类型 `target_search`（D3-AC-62 目标搜索条款；M23-AC-041、M23-AC-037 的目标搜索部分；M23 §15.2、§15.3）。

1. 抽样（不需要世界包，构造 NavGrid）：同一事件种子两次抽样位置相同、不同种子互不相同；位置只落在任务区内的可走面上；
   1,000 个 test 变体种子的位置服从先验（卡方检验 p ≥ 0.01：单分量高斯的径向分箱对 Rayleigh 解析概率、双分量高斯加不可走区
   对连续先验的数值积分、栅格先验对单元权重）；流 19 通道登记不重叠。
2. 门 1 必备配置：合规配置通过；各类缺陷一次报出全部 `E-TYPE`（611，`detail.why = type_requirement`）。
3. 门 3 验证器（构造帧数据）：60 s 窗口关键事件计数与区间、`target_present` 真值事件；静止目标位移、抽样位置不符、离开任务区
   在 strict 下返回 613、lenient 下为告警。
4. 泄漏扫描函数的正反用例。
5. 合成世界（open3d）端到端：strict 编译通过；`brief.json` 通过 brief.v1 schema 且只含先验；`brief.json` 与 `bench/task.v1`
   对磁带中目标全部位置的泄漏扫描 0 命中；实际位置可由先验与事件种子复现，同一种子两次编译 `content_sha256` 相同、换种子则位置
   与哈希改变；真值 `key_events` 的窗口数等于 ⌈在场时长 / 60 s⌉；游走与离场变体。
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from awr.sim.events import dsl
from awr.sim.events import search as sr
from awr.sim.events.nav import NavGrid
from awr.sim.events.tape import NO_CONTAINER, RECORD
from awr.sim.events.verify import verify_event
from awr.sim.sensors import cbrng

ROOT = Path(__file__).resolve().parents[3]
N_VARIANTS = 1000


def _sq(cx: float, cy: float, r: float) -> list[list[float]]:
    return [[cx - r, cy - r], [cx + r, cy - r], [cx + r, cy + r], [cx - r, cy + r]]


def _nav(hole: tuple[float, float, float, float] | None = None, profile: str = "person") -> NavGrid:
    """200 m × 200 m 平地（−100..100），可选不可走矩形 (x0, y0, x1, y1)。"""
    n = 200
    walk = np.ones((n, n), bool)
    if hole is not None:
        x0, y0, x1, y1 = hole
        cx = -100.0 + np.arange(n) + 0.5
        walk[np.ix_((cx >= y0) & (cx < y1), (cx >= x0) & (cx < x1))] = False
    return NavGrid(profile, -100.0, -100.0, n, n, np.zeros((n, n)), walk)


def _chi2(observed, expected) -> float:
    stats = pytest.importorskip("scipy.stats")
    obs, exp = np.asarray(observed, np.float64), np.asarray(expected, np.float64)
    return float(stats.chisquare(obs, exp * obs.sum() / exp.sum()).pvalue)


# ---------------------------------------------------------------------- 1. 抽样
def test_channels_registered_without_overlap() -> None:
    assert cbrng.CHANNELS[(19, "search")] == (9, 11) and cbrng.CHANNELS[(19, "search_wander")] == (12, 15)
    assert cbrng.check_channels() == []


GMM2 = {"type": "gmm", "components": [{"xy": [-20.0, 10.0], "sigma_m": 15.0, "weight": 0.6},
                                       {"xy": [30.0, -5.0], "sigma_m": 8.0, "weight": 0.4}]}
HOLE = (24.0, -12.0, 40.0, 4.0)                        # 第二个分量附近的建筑（不可走）
AREA = _sq(0.0, 0.0, 80.0)


def test_same_seed_same_position_and_walkable_support() -> None:
    nav = _nav(HOLE)
    a, b = sr.Sampler(GMM2, AREA, nav), sr.Sampler(GMM2, AREA, nav)
    assert a.table_sha256 == b.table_sha256 and a.n_cells == b.n_cells > 10_000
    assert a.sample(123, 4) == b.sample(123, 4) == a.sample(123, 4)                    # 同一种子两次抽样位置相同
    pts = np.array([a.sample(s, 4) for s in range(300)])
    assert len({tuple(p) for p in pts}) == 300                                          # 不同种子互不相同
    assert a.sample(123, 5) != a.sample(123, 4)                                         # 键含演员表 idx
    assert np.allclose(np.round(pts * 1000), pts * 1000, rtol=0, atol=1e-6)            # 毫米定点
    x0, y0, x1, y1 = HOLE
    assert not ((pts[:, 0] >= x0) & (pts[:, 0] < x1) & (pts[:, 1] >= y0) & (pts[:, 1] < y1)).any()
    assert (np.abs(pts) <= 80.0).all() and sr.walkable_mask(nav, pts).all()
    c = sr.Sampler(GMM2, AREA, _nav())                                                  # 可走面不同 → 抽样表不同
    assert c.table_sha256 != a.table_sha256
    with pytest.raises(ValueError, match="no mass"):
        sr.Sampler(GMM2, _sq(500.0, 500.0, 5.0), nav).sample(1, 0)


def test_variants_follow_gaussian_prior_radially() -> None:
    """单分量 sigma = 10 m 完全落在任务区与可走面内：1,000 个种子的径向距离按 Rayleigh 分布的 10 个等概率分箱，卡方 p ≥ 0.01。"""
    pr = {"type": "gmm", "components": [{"xy": [3.0, -7.0], "sigma_m": 10.0, "weight": 1.0}]}
    smp = sr.Sampler(pr, AREA, _nav())
    pts = np.array([smp.sample(s, 0) for s in range(N_VARIANTS)])
    r = np.hypot(pts[:, 0] - 3.0, pts[:, 1] + 7.0)
    edges = 10.0 * np.sqrt(-2.0 * np.log(1.0 - np.arange(1, 10) / 10.0))
    obs = np.bincount(np.searchsorted(edges, r), minlength=10)
    p = _chi2(obs, np.full(10, 0.1))
    assert p >= 0.01, (p, obs.tolist())


def _bins_two_component(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """6 个分箱：最近分量（按马氏距离）× 径向带（≤ sigma、sigma–2 sigma、> 2 sigma）。"""
    d = [np.hypot(x - c["xy"][0], y - c["xy"][1]) / c["sigma_m"] for c in GMM2["components"]]
    k = (d[1] < d[0]).astype(int)
    dm = np.where(k == 1, d[1], d[0])
    return 3 * k + np.searchsorted([1.0, 2.0], dm)


def test_variants_follow_truncated_mixture_prior() -> None:
    """双分量高斯 + 不可走区：期望分箱概率由连续先验在可走任务区上 0.2 m 格距数值积分得到，卡方 p ≥ 0.01。"""
    nav = _nav(HOLE)
    smp = sr.Sampler(GMM2, AREA, nav)
    pts = np.array([smp.sample(s, 2) for s in range(N_VARIANTS)])
    obs = np.bincount(_bins_two_component(pts[:, 0], pts[:, 1]), minlength=6)
    h = 0.2
    X, Y = np.meshgrid(np.arange(-80 + h / 2, 80, h), np.arange(-80 + h / 2, 80, h))
    X, Y = X.ravel(), Y.ravel()
    D = sr.density(GMM2, X, Y) * sr.walkable_mask(nav, np.stack([X, Y], 1))
    exp = np.bincount(_bins_two_component(X, Y), weights=D, minlength=6)
    p = _chi2(obs, exp)
    assert p >= 0.01, (p, obs.tolist(), (exp / exp.sum()).round(4).tolist())
    # 离散表本身：按格点精确概率分箱，与抽样的格点下标一致
    cells = np.array([smp.cell_index(s, 2) for s in range(N_VARIANTS)])
    pc = smp.probabilities()
    b = _bins_two_component(smp.centers[:, 0], smp.centers[:, 1])
    assert _chi2(np.bincount(b[cells], minlength=6), np.bincount(b, weights=pc, minlength=6)) >= 0.01


def test_variants_follow_grid_prior() -> None:
    """4 × 4 栅格先验（20 m 单元）：1,000 个种子落入各单元的次数对单元权重，卡方 p ≥ 0.01。"""
    w = [1, 2, 3, 4, 0, 1, 0, 2, 5, 1, 1, 1, 2, 3, 4, 6]
    pr = {"type": "grid", "grid": {"origin_xy": [-40.0, -40.0], "cell_m": 20.0, "nx": 4, "ny": 4, "weights": [float(v) for v in w]}}
    smp = sr.Sampler(pr, _sq(0.0, 0.0, 40.0), _nav())
    pts = np.array([smp.sample(s, 1) for s in range(N_VARIANTS)])
    ix = np.floor((pts[:, 0] + 40.0) / 20.0).astype(int)
    iy = np.floor((pts[:, 1] + 40.0) / 20.0).astype(int)
    obs = np.bincount(iy * 4 + ix, minlength=16)
    nz = np.asarray(w) > 0
    assert (obs[~nz] == 0).all()
    p = _chi2(obs[nz], np.asarray(w, float)[nz])
    assert p >= 0.01, (p, obs.tolist())
    assert sr.support_fraction(pr, _sq(0.0, 0.0, 40.0)) == pytest.approx(1.0)
    assert sr.support_fraction(pr, [[-40.0, -40.0], [0.0, -40.0], [0.0, 40.0], [-40.0, 40.0]]) == pytest.approx(
        sum(w[r * 4 + c] for r in range(4) for c in range(2)) / sum(w))


def test_support_fraction_gmm() -> None:
    one = {"type": "gmm", "components": [{"xy": [0.0, 0.0], "sigma_m": 10.0, "weight": 1.0}]}
    assert sr.support_fraction(one, _sq(0.0, 0.0, 200.0)) == pytest.approx(1.0, abs=1e-6)
    half = [[0.0, -200.0], [200.0, -200.0], [200.0, 200.0], [0.0, 200.0]]
    assert sr.support_fraction(one, half) == pytest.approx(0.5, abs=5e-3)
    r = 15.0                                           # 圆内质量 1 − exp(−r² / 2 sigma²)（64 边形近似圆）
    circ = [[r * math.cos(2 * math.pi * k / 64), r * math.sin(2 * math.pi * k / 64)] for k in range(64)]
    assert sr.support_fraction(one, circ) == pytest.approx(1 - math.exp(-r * r / 200.0), abs=0.01)


# ---------------------------------------------------------------------- 2. 门 1 必备配置
ANCHORS = {a["id"]: a for a in [
    {"id": "ev_kiosk", "kind": "point", "profile": "person", "xy": [-20.0, 20.0]},
    {"id": "ev_west", "kind": "point", "profile": "person", "xy": [-60.0, -40.0]},
    {"id": "ev_east", "kind": "point", "profile": "person", "xy": [60.0, -40.0]},
]}


def _doc(**kw) -> dict:
    d = {"schema": "drone4d.event.v1", "event_id": "synth-search", "world": {"id": "synth-glb"}, "seed": 7, "duration_s": 150,
         "event_type": "target_search",
         "actors": [{"id": "target_p", "class": "person", "roles": ["target"], "route": [{"t": 0, "at": sr.SEARCH_ANCHOR, "do": "spawn"}]},
                    {"id": "walker", "class": "person", "route": [{"t": 0, "at": "ev_west", "do": "spawn"}, {"t": 120, "at": "ev_east"}]}],
         "search": {"target": "target_p", "prior": {"type": "gmm", "components": [{"anchor": "ev_kiosk", "sigma_m": 15, "weight": 0.6},
                                                                                {"xy": [10, -20], "sigma_m": 10, "weight": 0.4}]},
                    "area_polygon": _sq(0.0, 0.0, 80.0)},
         "brief": {"public_anchors": ["ev_kiosk"], "targets": [{"actor": "target_p", "description_en": "Person in a red jacket"}]}}
    d.update(kw)
    return d


def _gate1(doc: dict, anchors: dict | None = None):
    doc, an = sr.prepare(doc, dict(anchors or ANCHORS))
    return dsl.gate1(doc, an)


def _rules(errs) -> list[str]:
    return sorted(e.detail.get("rule", e.code) for e in errs)


def test_gate1_accepts_compliant_configuration() -> None:
    plan, errs, _w = _gate1(_doc())
    assert errs == [] and plan is not None and plan["event_type"] == "target_search"
    doc = _doc()
    doc["search"]["prior"] = {"type": "grid", "grid": {"origin_xy": [-80, -80], "cell_m": 40, "nx": 4, "ny": 4, "weights": [1] * 16}}
    doc["search"].update(motion="wander", wander_radius_m=6, window_s=30)
    assert _gate1(doc)[1] == []
    assert sr.prepare(_doc(event_type="rendezvous_handoff"), ANCHORS)[1] is ANCHORS              # 非目标搜索事件不加占位


def test_gate1_reports_all_type_errors_at_once() -> None:
    doc = _doc(duration_s=20)
    doc["actors"][0]["class"] = "dog"                                                          # target_class
    doc["actors"][0]["route"].append({"t": 10, "at": "ev_east"})                               # target_route
    doc["actors"][1]["route"][1]["via"] = [sr.SEARCH_ANCHOR]                                   # reserved_anchor（其他演员）
    doc["actors"][1]["route"][1]["t"] = 15
    doc["brief"]["public_anchors"].append(sr.SEARCH_ANCHOR)                                    # reserved_anchor（简报）
    doc["search"]["prior"]["components"][0]["xy"] = [0, 0]                                     # prior_component（xy 与锚点并存）
    doc["search"]["motion"] = "wander"                                                         # wander_class（狗）
    doc["search"]["window_s"] = 60                                                             # window（> 20 s）
    plan, errs, _w = _gate1(doc)
    assert plan is None and all(e.code == "E-TYPE" and e.reason == 611 and e.detail["why"] == "type_requirement" for e in errs
                                if e.code == "E-TYPE")
    assert _rules([e for e in errs if e.code == "E-TYPE"]) == ["prior_component", "reserved_anchor", "reserved_anchor", "target_class",
                                                               "target_route", "wander_class", "window"]


def test_gate1_prior_and_target_rules() -> None:
    d = _doc()
    d["search"]["prior"]["components"] = [{"xy": [300, 300], "sigma_m": 20}]                 # 先验质量几乎全在任务区外
    errs = _gate1(d)[1]
    assert _rules(errs) == ["prior_support"] and errs[0].detail["fraction"] < 0.9
    d = _doc()
    d["search"]["prior"]["components"] = [{"xy": [70, 0], "sigma_m": 15}]                    # 约 25% 在外
    assert _rules(_gate1(d)[1]) == ["prior_support"]
    d["search"]["prior"]["components"] = [{"xy": [50, 0], "sigma_m": 15}]                     # 97.7% 在内
    assert _gate1(d)[1] == []
    d = _doc()
    d["search"]["prior"] = {"type": "grid", "grid": {"origin_xy": [0, 0], "cell_m": 10, "nx": 2, "ny": 2, "weights": [1, 1, 1]}}
    assert _rules(_gate1(d)[1]) == ["prior_grid"]
    d["search"]["prior"] = {"type": "grid", "grid_ref": "missing.json"}
    assert _rules(_gate1(d)[1]) == ["prior_grid"]
    d = _doc()
    d["search"]["prior"]["components"][0]["anchor"] = "no_such_anchor"
    d["search"]["target"] = "ghost"
    assert _rules(_gate1(d)[1]) == ["prior_component", "reserved_anchor", "target_unknown"]   # 目标未知时 target_p 不得用保留锚点
    d = _doc()
    del d["search"]
    assert _rules(_gate1(d)[1]) == ["E-ANCHOR-UNKNOWN", "search_missing"]                    # 无 search 段时不加保留锚点占位
    d = _doc()
    d["actors"][0]["class"] = "vehicle"
    d["search"]["motion"] = "wander"
    assert _rules(_gate1(d)[1]) == ["wander_class"]
    clash = {**ANCHORS, sr.SEARCH_ANCHOR: {"id": sr.SEARCH_ANCHOR, "kind": "point", "xy": [0.0, 0.0]}}
    assert _rules(_gate1(_doc(), clash)[1]) == ["reserved_anchor"]


def test_grid_ref_is_inlined_from_the_event_directory(tmp_path: Path) -> None:
    g = {"origin_xy": [-80, -80], "cell_m": 40, "nx": 4, "ny": 4, "weights": [1] * 16}
    (tmp_path / "prior.json").write_text(json.dumps(g))
    d = _doc()
    d["search"]["prior"] = {"type": "grid", "grid_ref": "prior.json"}
    d2, an = sr.prepare(d, ANCHORS, tmp_path)
    assert d2["search"]["prior"] == {"type": "grid", "grid": g} and d["search"]["prior"]["grid_ref"] == "prior.json"
    assert dsl.gate1(d2, an)[1] == []
    d["search"]["prior"]["grid_ref"] = "../escape.json"                                       # 事件目录之外不解析
    (tmp_path.parent / "escape.json").write_text(json.dumps(g))
    assert "grid" not in sr.prepare(d, ANCHORS, tmp_path)[0]["search"]["prior"]


# ---------------------------------------------------------------------- 3. 门 3 验证器（构造帧）
def test_key_windows() -> None:
    W = 60_000
    assert sr.key_windows((0, 360_000), W) == [(k * W, (k + 1) * W) for k in range(6)]
    assert sr.key_windows((0, 330_000), W)[-1] == (300_000, 330_000) and len(sr.key_windows((0, 330_000), W)) == 6
    assert sr.key_windows((5_000, 5_000), W) == [(5_000, 5_000)]
    assert len(sr.key_windows((0, 60_000), W)) == 1 and len(sr.key_windows((0, 60_100), W)) == 2
    assert len(sr.key_windows((10_000, 129_900), W)) == 2


ACT = [{"id": "target_p", "class": "person", "route": [{"at": sr.SEARCH_ANCHOR, "do": "spawn"}]},
       {"id": "walker", "class": "person", "route": [{"at": "ev_west", "do": "spawn"}]}]
TABLE = [{"idx": k, "id": a["id"], "class": "person", "scripted": True, "profile": "person"} for k, a in enumerate(ACT)]
POS = (12.345, -6.789)


def _splan(motion: str = "static", **kw) -> dict:
    s = {"target": "target_p", "prior": GMM2, "area_polygon": AREA, "motion": motion, "window_ms": 60_000,
         "sample": {"stream": 19, "channels": [9, 11], "cell_m": 1.0, "actor_no": 0, "profile": "person"}}
    if motion == "wander":
        s["wander_radius_m"] = 5.0
    s.update(kw)
    return {"event_id": "synth-search", "version": 1, "event_type": "target_search", "seed": 7, "tick_hz": 10, "duration_ms": 150_000,
            "actors": copy.deepcopy(ACT), "annotations": [], "relations": [], "search": s}


def _sframes(t_on: float = 10.0, t_off: float = 130.0, drift_m: float = 0.0) -> np.ndarray:
    n_t = 1501
    t = np.arange(n_t) / 10.0
    f = np.zeros((n_t, 2), RECORD)
    f["container"] = NO_CONTAINER
    on = (t >= t_on) & (t < t_off)
    f["state"][:, 0] = np.where(on, 2, np.where(t < t_on, 0, 5))
    f["x_mm"][:, 0] = round(POS[0] * 1000) + np.where(t >= 60.0, round(drift_m * 1000), 0)
    f["y_mm"][:, 0] = round(POS[1] * 1000)
    f["state"][:, 1] = 2
    f["x_mm"][:, 1], f["y_mm"][:, 1] = -60_000, -40_000
    return f


def _sanchors(xy=POS) -> dict:
    return {**ANCHORS, sr.SEARCH_ANCHOR: {"id": sr.SEARCH_ANCHOR, "kind": "point", "profile": "person", "xy": list(xy)}}


def _sverify(plan=None, frames=None, anchors=None, **kw):
    return verify_event(plan or _splan(), _sframes() if frames is None else frames, TABLE, anchors=anchors or _sanchors(), **kw)


def test_validator_windows_and_target_present() -> None:
    g = _sverify()
    assert g.errors == [] and g.stats["type_validator"] == "target_search"
    pres = [e for e in g.events if e["type"] == "target_present"]
    assert len(pres) == 1 and pres[0]["interval_ms"] == [10_000, 129_900] and pres[0]["participants"] == ["target_p"]
    keys = g.extra["key_events"]
    assert [k["id"] for k in keys] == ["target_window_1", "target_window_2"]                   # ⌈119.9 s / 60 s⌉ = 2
    assert [k["interval_ms"] for k in keys] == [[10_000, 70_000], [70_000, 129_900]]
    assert all(k["type"] == "target_window" and k["window_s"] == 60.0 and k["ref"] == "target_present" for k in keys)
    rep = g.extra["target_search"]
    assert rep["n_windows"] == 2 and rep["actual_xy"] == list(POS) and all(c.get("ok", True) for c in rep["checks"].values())
    assert rep["checks"]["sample_replay"] == {"available": False}
    assert g.extra["report_map"] == {"found": ["target_present"]} and g.detector_params["target_search"]["window_ms"] == 60_000
    tk = g.truth_kwargs()
    assert tk.get("key_events", keys) == keys
    # 窗口长度 30 s → ⌈119.9 / 30⌉ = 4；在场 [0, 150 s] → 3 个 60 s 窗口
    assert len(_sverify(plan=_splan(window_ms=30_000)).extra["key_events"]) == 4
    g = _sverify(frames=_sframes(0.0, 200.0))
    assert [k["interval_ms"] for k in g.extra["key_events"]] == [[0, 60_000], [60_000, 120_000], [120_000, 150_000]]


def test_validator_flags_inconsistencies_strict_and_lenient() -> None:
    g = _sverify(frames=_sframes(drift_m=0.5))
    assert [(e.code, e.reason, e.detail["why"]) for e in g.errors] == [("S-TYPE", 613, "static_motion")]
    g = _sverify(frames=_sframes(drift_m=0.5), strict=False)
    assert g.errors == [] and [w.detail["why"] for w in g.warnings if w.code == "S-TYPE"] == ["static_motion"]
    g = _sverify(anchors=_sanchors((12.0, -6.789)))
    assert [e.detail["why"] for e in g.errors] == ["sample_position", "static_motion"]
    g = _sverify(plan=_splan(area_polygon=_sq(0.0, 0.0, 5.0)))
    assert [e.detail["why"] for e in g.errors] == ["outside_area"]
    g = _sverify(plan=_splan("wander"), frames=_sframes(drift_m=4.0))
    assert g.errors == [] and g.extra["target_search"]["checks"]["wander_radius"]["ok"]
    g = _sverify(plan=_splan("wander"), frames=_sframes(drift_m=7.0))
    assert [e.detail["why"] for e in g.errors] == ["wander_radius"]
    g = _sverify(frames=_sframes(200.0, 300.0))
    assert [e.detail["why"] for e in g.errors] == ["target_absent"]
    p = _splan()
    del p["search"]
    assert [e.detail["why"] for e in _sverify(plan=p).errors] == ["search_missing"]


# ---------------------------------------------------------------------- 4. 泄漏扫描
def test_leak_scan_positive_and_negative() -> None:
    P = np.array([[12.345, -6.789], [1.5, 2.5]])
    clean = {"prior": GMM2, "area": AREA, "txt": "Person near the kiosk", "n": [12.345]}
    assert sr.leak_scan(clean, P) == []
    dirty = {"a": {"pos": [12.3452, -6.7889, 0.0]}, "b": [[0, 0], [12345, -6789]], "c": "last seen at 12.35, -6.79", "d": [9, 1.5, 2.5]}
    hits = sr.leak_scan(dirty, P)
    assert sorted(h["path"] for h in hits) == ["/a/pos/0", "/b/1/0", "/c", "/d/1"]


# ---------------------------------------------------------------------- 5. 合成世界端到端
@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory):
    pytest.importorskip("open3d")
    pytest.importorskip("zstandard")
    for d in (Path(__file__).resolve().parents[1] / "world", Path(__file__).resolve().parent):
        if str(d) not in sys.path:
            sys.path.insert(0, str(d))
    from synthworld import build_synth_world

    tmp = tmp_path_factory.mktemp("search")
    mp = pytest.MonkeyPatch()
    yield build_synth_world(tmp, mp), tmp
    mp.undo()


def _event(seed: int = 7, **search) -> dict:
    ev = _doc(seed=seed, anchors_ref="event:anchors.json")
    ev["search"]["area_polygon"] = _sq(0.0, 0.0, 100.0)
    ev["search"]["prior"]["components"][1] = {"xy": [40.0, -25.0], "sigma_m": 12, "weight": 0.4}     # 盒子 (30..50, −50..−30) 附近
    ev["search"].update(search)
    ev["background"] = [{"class": "person", "count": 6, "zone": "ev_town", "pause_s": [0, 5]}]
    return ev


def _compile(synth, ev: dict, name: str, **kw):
    from test_compile import OVERLAY

    from awr.sim.events.compile import CompileOptions, compile_event

    w, tmp = synth
    d = tmp / name
    d.mkdir(exist_ok=True)
    (d / "anchors.json").write_text(json.dumps(OVERLAY))
    (d / "event.json").write_text(json.dumps(ev))
    return compile_event(d / "event.json", w, opts=CompileOptions(out=d / "v1", cache_dir=tmp / "cache", **kw))


def _target_track(out: Path, target: str = "target_p") -> tuple[np.ndarray, np.ndarray]:
    from awr.sim.events import tape as tp

    t = tp.TapeReader(out / "tape.bin")
    f = np.concatenate([t.block_frames(i) for i in range(len(t.blocks))])
    i = [a["id"] for a in t.header["actors"]].index(target)
    return np.stack([f["x_mm"][:, i], f["y_mm"][:, i]], -1) / 1000.0, f["state"][:, i]


@pytest.fixture(scope="module")
def compiled(synth):
    r = _compile(synth, _event(), "s7")
    assert r.status == "compiled", [e for g in r.gates for e in g["errors"]]
    return r


def _load(out: Path, name: str) -> dict:
    return json.loads((out / name).read_text(encoding="utf-8"))


def test_compile_brief_contains_prior_only(compiled) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from awr.agentsdk.task import task_from_event
    from awr.contracts._paths import schema_path

    out = compiled.out
    assert {p.name for p in out.iterdir()} >= {"brief.json", "plan.json", "anchors.json", "tape.bin", "truth"}
    brief = _load(out, "brief.json")
    jsonschema.validate(brief, json.loads(schema_path("event/brief.v1.schema.json").read_text()))
    assert brief["event_type"] == "target_search" and [a["id"] for a in brief["public_anchors"]] == ["ev_kiosk"]
    s = brief["search"]
    assert s["target_class"] == "person" and s["window_s"] == 60.0 and s["motion"] == "static"
    assert s["prior"]["components"][0] == {"xy": [-20.0, 20.0], "sigma_m": 15.0, "weight": 0.6}               # 锚点已解析为 xy
    task = task_from_event(out)
    jsonschema.validate(task, json.loads(schema_path("bench/task.v1.schema.json").read_text()))
    assert task["prior"] == {"kind": "region_weights", "ref": "brief.json#/search/prior"} and task["report_types"] == ["found"]
    assert "target_window" in task["scored_event_types"]
    xy, st = _target_track(out)
    present = np.isin(st, (1, 2))
    actual = _load(out, "anchors.json")
    an = {a["id"]: a for a in actual["anchors"]}[sr.SEARCH_ANCHOR]
    assert np.allclose(xy[present][0], an["xy"], atol=1e-3)
    # 泄漏扫描：简报与任务契约对目标全部位置 0 命中；反例（把实际位置写入简报）被检出
    assert sr.leak_scan(brief, xy[present]) == [] and sr.leak_scan(task, xy[present]) == []
    text = json.dumps(brief) + json.dumps(task)
    assert sr.SEARCH_ANCHOR not in text and "target_present" not in text
    bad = copy.deepcopy(brief)
    bad["public_anchors"].append({"id": "x", "pos_enu_m": [*an["xy"], 0.0]})
    assert len(sr.leak_scan(bad, xy[present])) == 1


def test_compile_actual_position_is_seeded_sample(synth, compiled) -> None:
    from awr.sim.events.verify.base import dist_to_polygon

    out = compiled.out
    plan = _load(out, "plan.json")
    an = {a["id"]: a for a in _load(out, "anchors.json")["anchors"]}
    xy = an[sr.SEARCH_ANCHOR]["xy"]
    s = plan["search"]
    from awr.sim.events import tape as tp

    assert s["sample"]["actor_no"] == [a["id"] for a in tp.TapeReader(out / "tape.bin").header["actors"]].index("target_p")
    assert dist_to_polygon(np.asarray(s["area_polygon"]), np.array([xy[0]]), np.array([xy[1]]))[0] == 0.0
    assert not (30.0 < xy[0] < 50.0 and -50.0 < xy[1] < -30.0)                                               # 不在盒子（建筑）内
    rep = _load(out, "compile_report.json")
    ts = rep["gate3"]["extra"]["target_search"]
    assert ts["checks"]["sample_replay"]["ok"] and ts["checks"]["sample_replay"]["table_sha256"] == s["sample"]["table_sha256"]
    assert ts["checks"]["static_motion"]["ok"] and ts["actual_xy"] == xy
    # 同一种子再编译：位置与磁带哈希相同；换种子（test 变体的秘密种子）：位置与哈希改变
    r2 = _compile(synth, _event(), "s7-again")
    assert r2.tape["content_sha256"] == compiled.tape["content_sha256"]
    assert {a["id"]: a for a in _load(r2.out, "anchors.json")["anchors"]}[sr.SEARCH_ANCHOR]["xy"] == xy
    r3 = _compile(synth, _event(seed=8), "s8")
    assert r3.status == "compiled" and r3.tape["content_sha256"] != compiled.tape["content_sha256"]
    xy3 = {a["id"]: a for a in _load(r3.out, "anchors.json")["anchors"]}[sr.SEARCH_ANCHOR]["xy"]
    assert xy3 != xy and _load(r3.out, "brief.json") == _load(out, "brief.json")                                # 简报与种子无关


def test_compile_truth_key_windows(compiled) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    from awr.contracts._paths import schema_path

    truth = _load(compiled.out, "truth/r1/event_truth.json")
    jsonschema.validate(truth, json.loads(schema_path("event/truth.v1.schema.json").read_text()))
    _xy, st = _target_track(compiled.out)
    k = np.flatnonzero(np.isin(st, (1, 2)))
    L = (k[-1] - k[0]) * 100
    pres = [e for e in truth["events"] if e["type"] == "target_present"]
    assert len(pres) == 1 and pres[0]["interval_ms"] == [int(k[0]) * 100, int(k[-1]) * 100]
    keys = truth.get("key_events")
    if keys is None:                                                                                        # 真值尚未接入 key_events 时取编译报告
        keys = _load(compiled.out, "compile_report.json")["gate3"]["extra"]["key_events"]
    assert len(keys) == max(1, math.ceil(L / 60_000)) == 3 and all(x["type"] == "target_window" for x in keys)
    assert truth["detector_params"]["target_search"]["window_ms"] == 60_000


def test_compile_wander_and_despawn_variants(synth) -> None:
    ev = _event(seed=11, motion="wander", wander_radius_m=8)
    r = _compile(synth, ev, "wander")
    assert r.status == "compiled", [e for g in r.gates for e in g["errors"]]
    xy, st = _target_track(r.out)
    c = np.asarray({a["id"]: a for a in _load(r.out, "anchors.json")["anchors"]}[sr.SEARCH_ANCHOR]["xy"])
    pres = np.isin(st, (1, 2))
    d = np.hypot(*(xy[pres] - c).T)
    assert d.max() > 1.0 and d.max() <= 9.0 and (st[pres] == 1).any()                                     # 确有游走且不超出半径
    assert sr.leak_scan(_load(r.out, "brief.json"), xy[pres]) == []
    ev = _event(seed=12)
    ev["actors"][0]["route"].append({"t": 100, "at": sr.SEARCH_ANCHOR, "do": "despawn"})
    r = _compile(synth, ev, "despawn")
    assert r.status == "compiled", [e for g in r.gates for e in g["errors"]]
    keys = _load(r.out, "compile_report.json")["gate3"]["extra"]["key_events"]
    _xy, st = _target_track(r.out)
    k = np.flatnonzero(np.isin(st, (1, 2)))
    assert len(keys) == math.ceil((k[-1] - k[0]) * 100 / 60_000) == 2 and (st[k[-1] + 1:] == 5).all()


def test_compile_rejects_bad_search_at_gate1(synth) -> None:
    ev = _event()
    ev["search"]["prior"]["components"] = [{"xy": [400, 400], "sigma_m": 10}]
    r = _compile(synth, ev, "bad")
    assert r.status == "failed" and r.reason == 611 and len(r.gates) == 1
    assert [e["detail"]["rule"] for e in r.gates[0]["errors"]] == ["prior_support"]
