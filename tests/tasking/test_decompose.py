"""M20-AC-003（分解部分）至 AC-008：七类任务的分解、禁入（583、自动绕行）、子区与等相位、区域值守合并与拆分。"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from m20lib import F1, P600, FakeWorld, nest, snap, veh

from awr.tasking.decompose import DecompCtx, decompose, g_req_m
from awr.tasking.keepout import circles, path_clear
from awr.tasking.model import Keepout, TaskingError


def _ctx(keepouts=(), world=None, models=None):
    na = nest("n-a", (0.0, 0.0, 0.0))
    vs = [veh(f"p600-0{k}", P600, na) for k in range(1, 4)]
    s = snap(vs, [na], [], list(keepouts))
    if models is not None:
        s.models = models
    w = world or FakeWorld()
    return DecompCtx(s, w.height_dsm, w.ground_dtm, "t1")


def test_g_req_person_r() -> None:
    assert g_req_m("person", "R") == pytest.approx(0.027)
    assert g_req_m("vehicle", "R") > g_req_m("person", "R")


def test_point_watch_station_and_keepout() -> None:
    out = decompose({"type": "point_watch", "params": {"point": [100.0, 50.0], "dwell_s": None}}, _ctx())
    (w,) = out["items"]
    assert w.kind == "station" and w.persistent and w.pos[2] == pytest.approx(60.0)
    with pytest.raises(TaskingError) as ei:
        decompose({"type": "point_watch", "params": {"point": [500.0, 0.0]}}, _ctx([Keepout("k", 500.0, 0.0, 50.0)]))
    assert ei.value.code == 583


def test_point_watch_fixed_wing_radius_floor() -> None:
    ctx = _ctx(models={"awr_f1": F1})
    ctx.snap.vehicles = [veh("f1-01", F1, nest("n-l", (0.0, 0.0, 0.0), "launcher"))]
    with pytest.raises(TaskingError) as ei:
        decompose({"type": "point_watch", "params": {"point": [0.0, 0.0], "loiter_radius_m": 50.0}}, ctx)
    assert ei.value.code == 110 and ei.value.detail["min_m"] == pytest.approx(96.5)


def test_polyline_waypoint_inside_keepout_and_detour() -> None:
    ko = [Keepout("k", 300.0, 0.0, 60.0)]
    with pytest.raises(TaskingError) as ei:
        decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [300, 10], [600, 0]]}}, _ctx(ko))
    assert ei.value.code == 583 and ei.value.detail["waypoints"] == [1]
    out = decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [600, 0]], "mode": "once"}}, _ctx(ko))
    C = circles(ko)
    for w in out["items"]:
        assert path_clear(w.path, C, 1.0)[0]
    with pytest.raises(TaskingError):
        decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [600, 0]], "mode": "once", "auto_detour": False}}, _ctx(ko))


def test_polyline_long_line_split_and_duration() -> None:
    out = decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [15000, 0]], "mode": "once"}}, _ctx())
    assert len(out["items"]) >= 2
    assert all(w.divisible and w.group == "t1" for w in out["items"])
    out2 = decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [300, 0], [300, 300]], "mode": "loop", "duration_s": 1200}},
                     _ctx())
    (w,) = out2["items"]
    assert w.persistent and w.closed


def test_area_patrol_subregions_and_look_in() -> None:
    poly = [[0, 0], [600, 0], [600, 400], [0, 400]]
    ko = [Keepout("p-01/0", 300.0, 200.0, 50.0, target="p-01")]
    out = decompose({"type": "area_patrol", "params": {"polygon": poly, "revisit_s": 300}}, _ctx(ko))
    items = out["items"]
    assert len(items) == out["preview"]["n_vehicles"] >= 1
    assert all(w.kind == "subregion" and w.persistent and w.group == "t1" for w in items)
    assert math.ceil(out["t_cov_s"] / 300 - 1e-9) == len(items)
    C = circles(ko)
    for w in items:
        assert path_clear(w.path, C, 1.0)[0]
    # 看洞点：洞边界外 15 m，作为某个子区周期起点的停留航点
    assert out["geometry"]["stations"]
    assert sum(len(w.dwell) for w in items) == len(out["geometry"]["stations"])


def test_perimeter_phases() -> None:
    poly = [[0, 0], [800, 0], [800, 600], [0, 600]]
    out = decompose({"type": "perimeter_patrol", "params": {"polygon": poly, "offset_m": 20.0, "revisit_s": 120}}, _ctx())
    L = out["ring_len_m"]
    n = math.ceil(L / (P600.v_cruise_mps * 120) - 1e-9)
    assert len(out["items"]) == n == out["preview"]["n_vehicles"]
    assert pytest.approx(2 * (840 + 640), rel=0.02) == L
    starts = [w.path[0] for w in out["items"]]
    assert len({(round(float(p[0]), 1), round(float(p[1]), 1)) for p in starts}) == n


def test_area_guard_merged_and_split() -> None:
    small = [[0, 0], [150, 0], [150, 120], [0, 120]]
    out = decompose({"type": "area_guard", "params": {"polygon": small, "revisit_s": 600}}, _ctx())
    assert out["mode"] == "merged" and out["items"][0].kind == "guard_cycle"
    big = [[0, 0], [900, 0], [900, 700], [0, 700]]
    out2 = decompose({"type": "area_guard", "params": {"polygon": big, "revisit_s": 300}}, _ctx())
    assert out2["mode"] == "split"
    assert [c["type"] for c in out2["children"]] == ["area_patrol", "perimeter_patrol"]
    assert all(c["common"]["on_detect"] == "relay" for c in out2["children"])


def test_scan_items_strip_groups() -> None:
    out = decompose({"type": "area_scan_gsd", "params": {"polygon": [[100, 100], [500, 100], [500, 400], [100, 400]], "gsd_cm": 3.0}}, _ctx())
    items = out["items"]
    assert len(items) == out["preview"]["n_vehicles"] >= 1
    assert all(w.kind == "strip_group" and w.group == "t1" and w.deadline_s is not None for w in items)
    assert out["preview"]["scan"]["strip_w_m"] == pytest.approx(57.6, rel=0.01)
    assert out["geometry"]["strips"]


def test_relay_requires_target() -> None:
    with pytest.raises(TaskingError):
        decompose({"type": "relay_watch", "params": {}}, _ctx())
    out = decompose({"type": "relay_watch", "params": {"target": "person-01"}}, _ctx())
    assert out["relay"]["target"] == "person-01" and out["items"] == []


def test_path_altitude_clears_buildings() -> None:
    w = FakeWorld(boxes=[(250, 350, -20, 20, 80.0)])
    out = decompose({"type": "polyline_patrol", "params": {"waypoints": [[0, 0], [600, 0]], "mode": "once", "alt_m": 40.0}}, _ctx(world=w))
    P = np.vstack([it.path for it in out["items"]])
    assert P[:, 2].min() >= 90.0 - 1e-6     # 80 m 楼顶 + 10 m 净空


# ---------------------------------------------------------------- D2-FX-R2（验收第 1 轮 D2-AC-16、35）
def _ctx_w(keepouts=(), world=None, *, border: float | None = None, classes: dict | None = None):
    c = _ctx(keepouts, world=world)
    w = world or FakeWorld()
    c.los_fn = w.los_batch
    if border is not None:
        c.border_fn = lambda xy, ins: (np.abs(np.asarray(xy, float)[:, :2]) <= border - ins).all(axis=1)
    c.target_classes = dict(classes or {})
    return c


def test_perimeter_crossing_keepouts_detours_instead_of_583() -> None:
    """M20-FR-015：巡边环与敏感体相交（顶点也在禁入圆内）时绕行，不再 583（第 1 轮 aoi_guard + 三个游走的人）；航线 1 m 采样全在圆外，
    并为相交的洞插入看洞停留（云台指向洞内识别物）。"""
    poly = [[25, -529.7], [425, -529.7], [425, -129.7], [25, -129.7]]
    ko = [Keepout("p1/0", 339.0, -335.7, 194.0, target="p1", moving=True), Keepout("p2/0", 259.0, -215.7, 194.0, target="p2", moving=True),
          Keepout("p3/0", 259.0, -475.7, 194.0, target="p3", moving=True)]
    out = decompose({"type": "perimeter_patrol", "params": {"polygon": poly, "n_vehicles": 2, "revisit_s": 600}}, _ctx_w(ko))
    assert len(out["items"]) == 2
    C = circles(ko)
    for w in out["items"]:
        loop = np.vstack([w.path, w.path[:1]])            # 循环航线含闭合段
        ok, mg = path_clear(loop, C, 1.0)
        assert ok, mg
    looks = out["geometry"]["stations"]
    assert looks and {s["target"] for s in looks} <= {"p1", "p2", "p3"}
    assert sum(len(w.dwell) for w in out["items"]) == len(looks)
    assert all(set(w.needs.get("looks") or {}) == {str(i) for i in w.dwell} for w in out["items"])


def test_ring_detour_respects_border() -> None:
    """绕行顶点在世界边界内缩带内（node_ok）；无可行绕行时 None（调用方 583）。"""
    from awr.tasking.decompose import ring_detour

    R = np.array([[-400.0, -400.0], [400.0, -400.0], [400.0, 400.0], [-400.0, 400.0]])
    C = np.array([[0.0, -400.0, 120.0]])
    free = ring_detour(R, C)
    assert free is not None and path_clear(np.vstack([free, free[:1]]), C, 1.0)[0]
    def ok(xy):
        return (np.abs(np.asarray(xy, float)[:, :2]) <= 470.0).all(axis=1)

    inner = ring_detour(R, C, node_ok=ok)
    assert inner is not None and (np.abs(inner) <= 470.0 + 1e-6).all()
    assert path_clear(np.vstack([inner, inner[:1]]), C, 1.0)[0]
    assert ring_detour(R, np.array([[0.0, 0.0, 2000.0]])) is None


def test_look_in_station_has_line_of_sight_and_class_filter() -> None:
    """看洞站位：洞边界外 0–60 m 环带、三射线全可见（楼挡住的一侧不选）、只给任务目标类别的洞（车辆的洞不插看洞）。"""
    poly = [[-300, -300], [300, -300], [300, 300], [-300, 300]]
    ko = [Keepout("p/0", 0.0, 0.0, 170.0, target="p"), Keepout("car/0", 200.0, 200.0, 60.0, target="car")]
    # 洞内东侧一排矮楼（挡住东侧低空站位的视线）、外圈东北南三侧高楼（其上空超过 H_cap）
    w = FakeWorld(boxes=[(60.0, 90.0, -120.0, 120.0, 30.0), (172.0, 260.0, -260.0, 260.0, 200.0), (-260.0, 260.0, 172.0, 260.0, 200.0),
                         (-260.0, 260.0, -260.0, -172.0, 200.0)])
    c = _ctx_w(ko, world=w, classes={"p": "person", "car": "vehicle"})
    out = decompose({"type": "area_patrol", "params": {"polygon": poly, "revisit_s": 900}}, c)
    looks = out["geometry"]["stations"]
    assert [s["target"] for s in looks] == ["p"]
    q = np.asarray(looks[0]["pos"], float)
    d = math.hypot(q[0], q[1])
    assert 170.0 + 15.0 - 1e-6 <= d <= 170.0 + 60.0 + 1e-6
    A = np.repeat(q[None, :], 3, axis=0)
    B = np.array([[0.0, 0.0, 0.95 * 1.75], [0.0, 0.0, 0.5 * 1.75], [0.0, 0.0, 0.05 * 1.75]])
    assert bool(w.los_batch(A, B).all())                           # 三射线全可见
    assert q[2] - float(w.height_dsm(q[None, :2])[0]) >= 10.0 and q[2] <= 120.0 + 1e-6
    items = out["items"]
    assert any((it.needs.get("looks") or {}) for it in items)


def test_guard_cycle_loop_closure_outside_keepouts() -> None:
    """值守周期（合并）：看洞 → 巡边 → 区域 → 禁入圆外回到首个看洞点（闭合段经可见图绕行，此前直线闭合可能穿越禁入圆）。"""
    poly = [[0, 0], [300, 0], [300, 240], [0, 240]]
    ko = [Keepout("p/0", 150.0, 120.0, 60.0, target="p")]
    out = decompose({"type": "area_guard", "params": {"polygon": poly, "revisit_s": 1200}}, _ctx_w(ko, classes={"p": "person"}))
    assert out["mode"] == "merged"
    (w,) = out["items"]
    loop = np.vstack([w.path, w.path[:1]])
    assert path_clear(loop, circles(ko), 1.0)[0]
    assert w.dwell and w.needs.get("looks")


def test_fixed_wing_ring_inset_from_border() -> None:
    """全部可用机型为固定翼：巡边环收进世界边界内缩带（30 m + 0.5·R_turn），转角处提前转弯不越界。"""
    na = nest("n-a", (0.0, 0.0, 0.0))
    s = snap([veh("f1-01", F1, na)], [na], [], [])
    w = FakeWorld()
    c = DecompCtx(s, w.height_dsm, w.ground_dtm, "t1")
    c.border_fn = lambda xy, ins: (np.abs(np.asarray(xy, float)[:, :2]) <= 500.0 - ins).all(axis=1)
    out = decompose({"type": "perimeter_patrol", "params": {"polygon": [[-490, -490], [490, -490], [490, 490], [-490, 490]], "n_vehicles": 1}}, c)
    P = out["items"][0].path
    assert (np.abs(P[:, :2]) <= 500.0 - 30.0 + 1e-6).all()


def test_fixed_wing_turn_radius_uses_work_altitude_wind() -> None:
    """D2-FX-R2：固定翼规划转弯半径按作业高度的风（快照 wind_hi_mps + 1 m/s），下限 80 m（与执行时 `Executor.fw_loiter_r` 同一口径；
    此前取机巢 10 m 风、下限 40 m，S7 看洞盘旋圈比规划大 15–45 m，被守卫反复撤离）。"""
    ctx = _ctx(models={"awr_f1": F1})
    ctx.snap.vehicles = [veh("f1-01", F1, nest("n-l", (0.0, 0.0, 0.0), "launcher"))]
    ctx.snap.wind_mps = 0.0
    r0 = ctx.r_turn()
    assert r0 >= 80.0 - 1e-9
    ctx.snap.wind_hi_mps = 8.0
    r_hi = ctx.r_turn()
    assert r_hi == pytest.approx(max(80.0, 1.2 * F1.r_loiter_min_m * ((15.0 + 9.0) / 25.0) ** 2))
    assert r_hi > r0
    from awr.tasking.model import Snapshot

    s2 = Snapshot.from_payload(ctx.snap.to_payload())
    assert s2.wind_hi_mps == pytest.approx(8.0)


def test_area_guard_drops_a_blocked_perimeter_instead_of_583() -> None:
    """D2-INT（suzhou T5 在七城模板中唯一 INFEASIBLE）：区域贴近世界边界，两个识别物的禁入圆把巡边环的两条边都盖住、绕行只能越出边界
    内缩带时，值守去掉巡边段并告警 `perimeter_blocked`，保留区域巡查与看洞；单独的巡边任务仍判 583。"""
    poly = [[-200, -200], [200, -200], [200, 200], [-200, 200]]
    ko = [Keepout("p1/0", 0.0, 110.0, 200.0, target="p1"), Keepout("p2/0", 0.0, -110.0, 200.0, target="p2")]
    c = _ctx_w(ko, border=330.0, classes={"p1": "person", "p2": "person"})
    out = decompose({"type": "area_guard", "params": {"polygon": poly, "revisit_s": 1200}}, c)
    assert "perimeter_blocked" in c.warnings
    if out["mode"] == "split":
        assert [ch["type"] for ch in out["children"]] == ["area_patrol"]
    else:
        (w,) = out["items"]
        assert w.kind == "guard_cycle" and len(w.path) >= 2
        assert out["geometry"]["stations"]                   # 看洞保留
    with pytest.raises(TaskingError) as ei:
        decompose({"type": "perimeter_patrol", "params": {"polygon": poly, "revisit_s": 600}},
                  _ctx_w(ko, border=330.0, classes={"p1": "person", "p2": "person"}))
    assert ei.value.code == 583 and ei.value.detail.get("why_detail") == "perimeter_blocked"


def test_safe_profile_vertical_transitions_and_dwell_remap() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：作业航线逐段（含循环闭合段）复核走廊 DSM 净空，两端较低者低于净空高度时在端点处插入垂直过渡点，
    停留（看洞）与看洞映射按新序号重排；此前看洞点以自身高度覆盖走廊高度、闭合段直连不复核，斜降段穿楼（细校验 102）。"""
    from awr.tasking.decompose import _safe_profile
    from awr.tasking.model import WorkItem

    w = FakeWorld(boxes=[(90.0, 110.0, -20.0, 20.0, 80.0)])
    c = _ctx_w([], world=w)
    P = np.array([[0.0, 0.0, 60.0], [200.0, 0.0, 60.0], [200.0, 100.0, 120.0]])
    wi = WorkItem("t.0", "t", "guard_cycle", path=P, closed=True, dwell={0: 15.0, 2: 15.0}, needs={"looks": {"0": "p", "2": "q"}})
    _safe_profile(wi, c)
    Q = np.asarray(wi.path)
    loop = np.vstack([Q, Q[:1]])
    for a, b in itertools.pairwise(loop):
        n = max(2, int(np.hypot(*(b[:2] - a[:2])) / 2.0) + 1)
        pts = np.array([a + (b - a) * t for t in np.linspace(0, 1, n)])
        assert np.all(pts[:, 2] >= w.height_dsm(pts[:, :2]) + 10.0 - 1e-6), (a, b)
    assert sorted(wi.dwell) == [int(k) for k in sorted(wi.needs["looks"], key=int)]
    assert Q[min(wi.dwell)][2] == pytest.approx(60.0)            # 看洞高度不变（垂直过渡在其前后）
    assert {wi.needs["looks"][str(i)] for i in wi.dwell} == {"p", "q"}


def test_look_points_avoid_other_tasks_and_face_nest() -> None:
    """D2-FX-R2-tasking 第 2 次：①分解时看洞点与其他任务的看洞点、站位水平间距 ≥ LOOK_SEP_M（区域巡查与巡边曾对同一个洞取同一点，两架在同一点
    上下相撞）；②区域值守的看洞点取朝向机巢的一侧，周期内按离机巢由近到远排列（D2-AC-34 首次捕获）。"""
    from awr.tasking.decompose import LOOK_SEP_M

    poly = [[-300, -300], [300, -300], [300, 300], [-300, 300]]
    ko = [Keepout("p/0", 0.0, 0.0, 120.0, target="p")]
    c = _ctx_w(ko, classes={"p": "person"})
    out = decompose({"type": "area_patrol", "params": {"polygon": poly, "revisit_s": 900}}, c)
    q0 = np.asarray(out["geometry"]["stations"][0]["pos"][:2], float)
    c2 = _ctx_w(ko, classes={"p": "person"})
    c2.avoid_pts = [tuple(q0)]
    out2 = decompose({"type": "perimeter_patrol", "params": {"polygon": poly, "revisit_s": 900, "target_class": "person"}}, c2)
    for st in out2["geometry"]["stations"]:
        assert math.hypot(st["pos"][0] - q0[0], st["pos"][1] - q0[1]) >= LOOK_SEP_M - 1e-6
    # 区域值守：机巢在 (0, 0)（_ctx_w 的机巢），识别物在东侧 250 m：看洞点在识别物的西侧（朝机巢）
    poly3 = [[100, -200], [500, -200], [500, 200], [100, 200]]
    ko3 = [Keepout("p/0", 300.0, 0.0, 120.0, target="p")]
    out3 = decompose({"type": "area_guard", "params": {"polygon": poly3, "revisit_s": 1800}}, _ctx_w(ko3, classes={"p": "person"}))
    st = out3["geometry"]["stations"][0]["pos"]
    assert st[0] < 300.0 - 100.0


def test_concurrent_path_tasks_separate_work_layers() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：航线类任务与范围重叠的其他任务航线作业高度相差 < LAYER_SEP_M 时整体降低 LAYER_SEP_M（区域
    巡查与巡边曾在同一高度沿交叉航线反复相遇，M09 第 3 次让行即 OSCILLATION 保持 HOLD，两机悬停到能量 RTL）；范围不重叠时不变。"""
    from awr.tasking.decompose import LAYER_SEP_M, layers_conflict, path_layer

    poly = [[-300, -300], [300, -300], [300, 300], [-300, 300]]
    spec = {"type": "perimeter_patrol", "params": {"polygon": poly, "revisit_s": 900}}
    out0 = decompose(spec, _ctx_w([]))
    base = [path_layer(w.path) for w in out0["items"] if w.path is not None and len(w.path) >= 2]
    other = [(-250.0, -250.0, 250.0, 250.0, base[0][4] + 3.0)]        # 范围重叠、高度只差 3 m 的另一任务航线
    assert any(layers_conflict(a, other[0]) for a in base)
    c = _ctx_w([])
    c.avoid_layers = other
    out1 = decompose(spec, c)
    mine = [path_layer(w.path) for w in out1["items"] if w.path is not None and len(w.path) >= 2]
    assert not any(layers_conflict(a, b) for a in mine for b in other)
    assert all(m[4] <= b[4] - LAYER_SEP_M + 1e-6 for m, b in zip(mine, base, strict=True))
    c2 = _ctx_w([])
    c2.avoid_layers = [(1000.0, 1000.0, 1200.0, 1200.0, base[0][4])]   # 范围不重叠
    out2 = decompose(spec, c2)
    assert [path_layer(w.path)[4] for w in out2["items"] if w.path is not None and len(w.path) >= 2] == [b[4] for b in base]
