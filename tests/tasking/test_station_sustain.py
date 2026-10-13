"""M20-AC-018（站位选择）、M20-AC-002 与 AC-033 的评估部分（可持续性）、AC-017 的瓶颈码。"""

from __future__ import annotations

import math

import numpy as np
from m20lib import P600, V1, FakeWorld

from awr.tasking.keepout import signed_margin
from awr.tasking.station import N_LOS, fw_orbit_ok, select_station
from awr.tasking.sustain import evaluate

WIN_DAY = [[(40.0, 320.0, 850.0), (60.0, 320.0, 845.0), (80.0, 320.0, 835.0), (100.0, 320.0, 820.0), (120.0, 320.0, 800.0)]]


def test_station_constraints_and_determinism() -> None:
    w = FakeWorld(boxes=[(330.0, 400.0, -60.0, 60.0, 150.0)])     # 东侧一栋高楼挡住部分方位
    C = np.array([[0.0, 0.0, 320.0]])
    kw = dict(keepouts=C, height_dsm=w.height_dsm, ground_dtm=w.ground_dtm, los_fn=w.los_batch, nests=[((800.0, 0.0, 0.0), 3500.0)])
    r = select_station((0.0, 0.0, 0.0), WIN_DAY, **kw)
    assert r.ok
    p = np.asarray(r.primary)
    assert signed_margin(p[None, :2], C)[0] >= 0
    assert p[2] - w.height_dsm(p[None, :2])[0] >= 10.0
    assert r.n_cands <= 2400 and r.n_los <= 3 * N_LOS
    assert r.k_los >= 2 / 3
    assert r.handover is not None
    dh = math.hypot(r.handover[0] - p[0], r.handover[1] - p[1])
    assert 15.0 - 1e-6 <= dh <= 30.0 + 1e-6 or dh > 0
    r2 = select_station((0.0, 0.0, 0.0), WIN_DAY, **kw)
    assert r2.to_payload() == r.to_payload()
    # 不在建筑后方（视线被遮挡的方位被排除）
    assert not (p[0] >= 330.0 and abs(p[1]) < 60.0)


def test_station_infeasible_reasons_and_moving_offset() -> None:
    w = FakeWorld()
    r = select_station((0.0, 0.0, 0.0), [[]], keepouts=np.zeros((0, 3)), height_dsm=w.height_dsm, ground_dtm=w.ground_dtm)
    assert not r.ok and r.why == "night_sensor"
    r2 = select_station((0.0, 0.0, 0.0), WIN_DAY, keepouts=np.array([[0.0, 0.0, 2000.0]]), height_dsm=w.height_dsm,
                        ground_dtm=w.ground_dtm)
    assert not r2.ok and r2.why == "keepout_gt_range"
    r3 = select_station((0.0, 0.0, 0.0), WIN_DAY, keepouts=np.array([[0.0, 0.0, 320.0]]), height_dsm=w.height_dsm, ground_dtm=w.ground_dtm,
                        moving=True)
    assert r3.ok and r3.offset is not None and r3.offset["dist_m"] >= 320.0


def test_fixed_wing_orbit_full_circle() -> None:
    w = FakeWorld()
    ok, frac = fw_orbit_ok((0.0, 0.0, 0.0), 350.0, 80.0, keepouts=np.array([[0.0, 0.0, 320.0]]), height_dsm=w.height_dsm,
                           ground_dtm=w.ground_dtm, los_fn=w.los_batch)
    assert ok and frac == 1.0
    w2 = FakeWorld(boxes=[(300.0, 450.0, -100.0, 100.0, 200.0)])
    ok2, frac2 = fw_orbit_ok((0.0, 0.0, 0.0), 350.0, 80.0, keepouts=np.array([[0.0, 0.0, 320.0]]), height_dsm=w2.height_dsm,
                             ground_dtm=w2.ground_dtm, los_fn=w2.los_batch)
    assert not ok2 and frac2 < 1.0


def _nest(chargers: int = 4, spare: int = 3, crewed: bool = False) -> dict:
    return {"pos": [-800.0, 0.0, 0.0], "kind": "launcher" if crewed else "box", "crewed": crewed, "turnaround_s": 60.0, "launch_interval_s": 30.0,
            "energy": {"mode": "swap", "swap_s": 180.0, "packs": [{"cells": 6, "count": spare}]},
            "chargers": [{"model": "c1_xr", "count": chargers, "channels": 1, "i_max_a": 10.0, "cells_max": 6}], "link_range_m": 3500.0}


HOURS = [{"t_from_unix_ns": str(k * 3600 * 10**9), "t_to_unix_ns": str((k + 1) * 3600 * 10**9), "band": "day" if 6 <= k < 18 else "night"}
         for k in range(24)]


def test_sustainability_yes_and_charger_bottleneck() -> None:
    pos = [{"id": "relay-1", "pos": [0.0, 0.0, 60.0], "role": "hover"}]
    vs = [{"id": f"p600-0{k}", "model_id": "p600_mid360", "nest_id": "n-a"} for k in range(1, 4)]
    feas = {"day": {"ok": True, "los": True}, "night": {"ok": True, "los": True}}
    s = evaluate(positions=pos, hours=HOURS, band_feasible=feas, models={"p600_mid360": P600}, vehicles=vs, nests={"n-a": _nest()})
    assert s["verdict"] == "yes", s["bottlenecks"]
    pp = s["per_position"][0]
    assert pp["n_ch"] == 4 and pp["n_pack"] == 5 and pp["n_air"] == 2
    s2 = evaluate(positions=pos, hours=HOURS, band_feasible=feas, models={"p600_mid360": P600}, vehicles=vs, nests={"n-a": _nest(chargers=3)})
    assert s2["verdict"] == "no" and [b["code"] for b in s2["bottlenecks"]] == ["charger"]
    s3 = evaluate(positions=pos, hours=HOURS, band_feasible=feas, models={"p600_mid360": P600}, vehicles=vs[:1], nests={"n-a": _nest()})
    assert "airframe" in [b["code"] for b in s3["bottlenecks"]]


def test_sustainability_night_infeasible_windows_and_crewed() -> None:
    pos = [{"id": "relay-1", "pos": [0.0, 0.0, 60.0], "role": "hover"}]
    vs = [{"id": f"p600-0{k}", "model_id": "p600_mid360", "nest_id": "n-a"} for k in range(1, 4)]
    feas = {"day": {"ok": True, "los": True}, "night": {"ok": False, "why": "light"}}
    s = evaluate(positions=pos, hours=HOURS, band_feasible=feas, models={"p600_mid360": P600}, vehicles=vs, nests={"n-a": _nest()})
    assert s["verdict"] == "no" and s["bottlenecks"][0]["code"] == "night_sensor"
    bad = [w for w in s["windows"] if not w["feasible"]]
    assert [w["band"] for w in bad] == ["night", "night"] and sum(1 for w in s["windows"] if w["feasible"]) == 1
    v1 = [{"id": "v1-01", "model_id": "awr_v1", "nest_id": "n-l"}]
    s2 = evaluate(positions=pos, hours=HOURS[:2], band_feasible={"night": {"ok": True}}, models={"awr_v1": V1}, vehicles=v1,
                  nests={"n-l": _nest(crewed=True)})
    assert "airframe" in [b["code"] for b in s2["bottlenecks"]]


def test_fixed_wing_group_uses_orbits_above_r_min() -> None:
    """M20-FR-059：固定翼组只取半径 ≥ R_loiter,min 的盘旋圈，整圈视线；结果给出盘旋半径；被建筑遮挡的圈排除。"""
    from awr.tasking.station import select_station

    # 一座塔（x 180–200 m）挡住半径 ≥ 180 m 的圈在该方位上的视线；小于 R_loiter,min 的半径不取
    w = FakeWorld(boxes=[(180.0, 200.0, -15.0, 15.0, 100.0)])
    win = [[(60.0, 100.0, 300.0)]]
    r = select_station((0.0, 0.0, 0.0), win, keepouts=np.array([[0.0, 0.0, 90.0]]), height_dsm=w.height_dsm, ground_dtm=w.ground_dtm,
                       los_fn=w.los_batch, fw=[True], r_min=[132.5])
    assert r.ok and r.offset is not None and 132.5 <= r.offset["loiter_radius_m"] < 180.0
    assert r.n_los <= 8 * 24
    # 窗口上界小于盘旋半径下限：fw_turn
    r2 = select_station((0.0, 0.0, 0.0), [[(60.0, 100.0, 120.0)]], keepouts=np.zeros((0, 3)), height_dsm=w.height_dsm, ground_dtm=w.ground_dtm,
                        fw=[True], r_min=[132.5])
    assert not r2.ok and r2.why == "fw_turn"


def test_mixed_fleet_checks_multirotor_candidates_when_orbits_fail() -> None:
    """D2-INT（S7 接力 581 los）：固定翼组得分靠前但整圈视线不成立（四周塔楼）时，混编组仍复核多旋翼候选，射线总数 ≤ 3·N_LOS。"""
    # 塔楼环绕：半径 ≥ 132.5 m 的每一圈都在某个方位被挡；多旋翼可取的西侧方位留空
    boxes = [(150.0, 170.0, -20.0, 20.0, 200.0), (-20.0, 20.0, 150.0, 170.0, 200.0), (-20.0, 20.0, -170.0, -150.0, 200.0)]
    w = FakeWorld(boxes=boxes)
    win = [[(60.0, 140.0, 400.0), (100.0, 140.0, 400.0)], [(60.0, 140.0, 400.0), (100.0, 140.0, 400.0)]]
    kw = dict(keepouts=np.array([[0.0, 0.0, 120.0]]), height_dsm=w.height_dsm, ground_dtm=w.ground_dtm, los_fn=w.los_batch,
              p_ratio=[0.2, 1.0], fw=[True, False], r_min=[132.5, 0.0])
    r = select_station((0.0, 0.0, 0.0), win, **kw)
    assert r.ok and r.group == 1 and r.k_los >= 2 / 3
    assert r.n_los <= 3 * N_LOS
    # 只有固定翼组：仍按整圈视线判 los
    r2 = select_station((0.0, 0.0, 0.0), win[:1], **{**kw, "p_ratio": [0.2], "fw": [True], "r_min": [132.5]})
    assert not r2.ok and r2.why == "los"


# ---------------------------------------------------------------- D2-FX-R2（验收第 1 轮 D2-AC-19）
def test_station_nest_clearance_avoid_and_border() -> None:
    """站位与机巢的水平距离 ≥ nest_clear_m（第 1 轮 (b) 站位在机巢上方 25 m，返航机与在岗机相撞）；回避点 25 m 内不选；世界边界外不选。"""
    w = FakeWorld()
    C = np.array([[0.0, 0.0, 320.0]])
    nests = [((-400.0, 0.0, 0.0), 3500.0)]
    kw = dict(keepouts=C, height_dsm=w.height_dsm, ground_dtm=w.ground_dtm, los_fn=w.los_batch, nests=nests)
    r0 = select_station((0.0, 0.0, 0.0), WIN_DAY, **kw)
    assert r0.ok
    r1 = select_station((0.0, 0.0, 0.0), WIN_DAY, nest_clear_m=60.0, **kw)
    assert r1.ok and math.hypot(r1.primary[0] + 400.0, r1.primary[1]) >= 60.0
    r2 = select_station((0.0, 0.0, 0.0), WIN_DAY, nest_clear_m=60.0, avoid=[tuple(r1.primary[:2])], **kw)
    assert r2.ok and math.hypot(r2.primary[0] - r1.primary[0], r2.primary[1] - r1.primary[1]) >= 25.0
    r3 = select_station((0.0, 0.0, 0.0), WIN_DAY, border_fn=lambda xy: np.asarray(xy)[:, 0] >= 0.0, **kw)
    assert r3.ok and r3.primary[0] >= 0.0


def test_moving_target_region_grid_los() -> None:
    """随机游走：按跟随几何复核活动区域 3×3 网格的视线（识别物走到楼后时，跟随站位仍看得见），可见比例计入评分。"""
    # 区域东半边紧挨一排矮楼：从西侧、低仰角看过去时被挡；北侧高处的站位整片可见
    w = FakeWorld(boxes=[(20.0, 26.0, -40.0, 40.0, 25.0)])
    C = np.array([[0.0, 0.0, 200.0]])
    win = [[(40.0, 200.0, 600.0), (120.0, 200.0, 600.0)]]
    grid = [[x, y] for x in (-30.0, 0.0, 30.0) for y in (-30.0, 0.0, 30.0)]
    r = select_station((0.0, 0.0, 0.0), win, keepouts=C, height_dsm=w.height_dsm, ground_dtm=w.ground_dtm, los_fn=w.los_batch,
                       moving=True, move_r_m=20.0, move_q=grid)
    assert r.ok and r.offset is not None
    off = np.array([r.primary[0], r.primary[1]])
    vis = []
    for q in grid:
        a = np.array([[q[0] + off[0], q[1] + off[1], r.primary[2]]])
        b = np.array([[q[0], q[1], 0.5 * 1.75]])
        vis.append(bool(w.los_batch(a, b)[0]))
    assert sum(vis) >= 8


def test_region_station_sees_current_position() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-19 (c)）：随机游走识别物的固定区域站位须看见识别物当前（预测）位置（see_pos，有这样的候选时
    只在其中选）；此前按区域可见比例选出的交接站位恰好看不到识别物，在岗机到 t_end 以重叠 0 交接，间隙 28 s。"""
    # 区域西侧一道 15 m 的墙：从西侧低仰角看，区域西半部被挡；东侧站位整片可见
    w = FakeWorld(boxes=[(-60.0, -54.0, -80.0, 80.0, 15.0)])
    C = np.array([[0.0, 0.0, 200.0]])
    win = [[(120.0, 200.0, 600.0)]]
    grid = [[x, y] for x in np.linspace(-30.0, 30.0, 6) for y in np.linspace(-30.0, 30.0, 6)]
    kw = dict(keepouts=C, height_dsm=w.height_dsm, ground_dtm=w.ground_dtm, los_fn=w.los_batch, region_q=grid,
              origin=(-450.0, 0.0, 120.0))
    for sp in ((20.0, 0.0), (-25.0, 10.0)):
        r = select_station((0.0, 0.0, 0.0), win, see_pos=list(sp), **kw)
        assert r.ok
        a = np.repeat(np.asarray(r.primary, float)[None], 3, axis=0)
        b = np.array([[sp[0], sp[1], f * 1.75] for f in (0.95, 0.5, 0.05)])
        assert int(w.los_batch(a, b).sum()) >= 2, (sp, r.primary)
