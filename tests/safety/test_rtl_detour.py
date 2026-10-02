"""ADR-054（needs_data）：绕行返航——M09 的 t_rtl 按实际返航路线计算，rtl 分发沿同一路线飞行。

深圳 381 m 塔（S1 几何，12 §7.2）：机体沿 S1 下段螺旋半径绕到塔的远侧（world z 62 m），此时直飞 home 的走廊上界为塔顶，
直飞方案要爬升到约 389 m。M09 选出的单绕行点路线：
1. 返航计划带绕行点，z_rtl 远低于塔顶，t_rtl 比直飞方案少一半以上；两段各自满足 `z_rtl ≥ 走廊上界 + 5 m`；
2. rtl 分发按该路线飞行：巡航段最高不超过 z_rtl + 1 m，全程离 DSM 净空 ≥ 2 m，先经过绕行点再到 home 上方，
   在 home 2 m 内触地；实际用时不超过 t_rtl 估算（12 §5.8.3 第 2 条：估算保守）；
3. 同一路线规则经 EnergyModel.rtl_route 提供给 M10 的能量预检。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from safelib import Harness

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.needs_data, pytest.mark.slow]

CENTER = np.array([-162.2, 77.3])   # S1 螺旋中心（塔体足迹形心）
RADIUS = 57.0
HOME = (-230.0, 20.0)                # S1 p600-01 的 home
Z = 62.0


@pytest.fixture(scope="module")
def shenzhen():
    if not (ROOT / "worlds/shenzhen/world.json").exists():
        pytest.skip("worlds/shenzhen 未构建")
    from awr.world.geometry import open_world_query

    return open_world_query(ROOT / "worlds/shenzhen")


def _ring(a_deg: float) -> list[float]:
    a = math.radians(a_deg)
    return [float(CENTER[0] + RADIUS * math.cos(a)), float(CENTER[1] + RADIUS * math.sin(a)), Z]


def test_detour_plan_matches_flight(shenzhen) -> None:
    wq = shenzhen
    h = Harness(world=wq, spawn=HOME, limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(5.0)
        s = h.slot()
        home = h.S.enu.home[s].copy()
        # 先爬升到螺旋高度，再沿螺旋半径（每段 45°，弦距塔心 ≥ 52 m）绕到塔的远侧（home 方位的对侧）
        rep, cid = h.goto([home[0], home[1], Z], timeout_s=60.0)
        assert h.call(cid).status == "succeeded", rep
        a_home = math.degrees(math.atan2(home[1] - CENTER[1], home[0] - CENTER[0]))
        for k in range(0, 5):
            rep, cid = h.goto(_ring(a_home + 45.0 * k), timeout_s=90.0)
            assert h.call(cid).status == "succeeded", (k, rep, h.call(cid).code if h.call(cid) else None)
        R = h.svc.params.rtl
        tol = R.h_top_tol_m
        p = h.pos()
        top_direct = float(wq.heightmap_top_along(p[:2], home[:2], tol_m=tol))
        assert top_direct > 300.0, top_direct                                           # 直飞要翻越塔顶
        h.rt.bat.refresh(np.array([s]))
        plan = h.svc.energy.rtl_plan(s)
        assert plan.via_enu_m is not None, plan
        via = np.asarray(plan.via_enu_m)
        z_direct = max(p[2], home[2] + R.alt_m, top_direct + R.top_margin_m)
        d = float(np.hypot(*(home[:2] - p[:2])))
        t_direct = (d / R.v_cruise_cap_mps + max(0.0, z_direct - p[2]) / R.v_up_est
                    + max(0.0, z_direct - home[2] - R.descend_alt_m) / R.v_dn_est + R.descend_alt_m / R.v_land_est)
        assert plan.z_rtl_m < 150.0 and plan.t_rtl_s < 0.5 * t_direct, (plan, t_direct)
        for a, b in ((p[:2], via), (via, home[:2])):
            assert plan.z_rtl_m >= float(wq.heightmap_top_along(a, b, tol_m=tol)) + R.top_margin_m - 1e-6
        # M10 能量预检使用同一路线规则（EnergyModel.rtl_route）
        r = h.svc.energy.rtl_route(p, home, s)
        assert r is not None and r[0] is not None and np.allclose(r[0], via) and r[1] == pytest.approx(plan.z_rtl_m)
        # 分发：rtl 命令沿绕行路线飞行
        t0 = h.core.clock.t_ns
        rep = h.cmd("rtl", {}, cid="rtl")
        assert rep["status"] == "accepted", rep
        c = h.call("rtl")
        min_clear, z_max, d_via = np.inf, -np.inf, np.inf
        while not c.final and h.core.clock.t_ns - t0 < 400e9:
            h.advance(0.2)
            q = h.pos()
            if h.fs()[0] == "RTL" and h.fs()[1] in ("CRUISE", "DESCEND"):
                min_clear = min(min_clear, float(q[2] - wq.height_dsm(q[None, :2])[0]))
                z_max = max(z_max, float(q[2]))
                d_via = min(d_via, float(np.hypot(*(q[:2] - via))))
        assert c.status == "succeeded", (c.status, c.code, h.state())
        t_fly = (h.core.clock.t_ns - t0) * 1e-9
        assert min_clear >= 2.0, min_clear
        assert z_max <= plan.z_rtl_m + 1.0, (z_max, plan.z_rtl_m)
        assert d_via <= 5.0, d_via                                                      # 经过绕行点
        q = h.pos()
        assert np.hypot(q[0] - home[0], q[1] - home[1]) <= 2.0
        assert t_fly <= plan.t_rtl_s + 10.0, (t_fly, plan.t_rtl_s)                     # 触地到 DISARMED 另有 2 s 判定
        assert "SAF.BAT.ENERGY_RTL" not in h.codes()
    finally:
        h.close()


def test_direct_when_unobstructed(shenzhen) -> None:
    """塔的 home 一侧：直飞不需要越障爬升，返航计划不带绕行点（与 12 §5.8.3 原规则一致）。"""
    h = Harness(world=shenzhen, spawn=HOME, limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(5.0)
        s = h.slot()
        rep, cid = h.goto([HOME[0], HOME[1], Z], timeout_s=60.0)
        assert h.call(cid).status == "succeeded", rep
        h.rt.bat.refresh(np.array([s]))
        plan = h.svc.energy.rtl_plan(s)
        assert plan.via_enu_m is None and plan.z_rtl_m == pytest.approx(Z, abs=1.0), plan
    finally:
        h.close()
