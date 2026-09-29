"""M09-AC-008（needs_data）：深圳楼群间起点的 RTL——全程净空 ≥ 2 m；`z_rtl ≥ H_top + 5 m`；在 home 2 m 内触地；CLIMB 结束时以
当前位置到 home 的线段重算 H_top（FR-011）。起点自动选取：出生点周围 120 m 处、与 home 之间走廊上界最高（≤ 60 m，
控制用例时长）的开阔地。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from safelib import Harness

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.needs_data


@pytest.fixture(scope="module")
def shenzhen():
    if not (ROOT / "worlds/shenzhen/world.json").exists():
        pytest.skip("worlds/shenzhen 未构建")
    from awr.world.geometry import open_world_query

    return open_world_query(ROOT / "worlds/shenzhen")


def test_rtl_between_buildings(shenzhen) -> None:
    wq = shenzhen
    home = np.array([87.1, 12.5])
    best = None
    off = np.array([[dx, dy] for dx in (-4, 0, 4) for dy in (-4, 0, 4)], np.float64)
    for R in (120.0, 160.0):
        for ang in np.radians(np.arange(0, 360, 20)):
            d = home + R * np.array([np.cos(ang), np.sin(ang)])
            z = wq.height_dsm(d[None] + off)
            flat = float(z.max() - z.min()) < 0.5  # 3×3 邻域开阔（同 M08 `_flat_spot` 判据）
            top = float(wq.heightmap_top_along(home, d))
            if flat and top <= 60.0 and (best is None or top > best[1]):
                best = (d, top)
    assert best is not None and best[1] > 10.0, best
    dest, top = best
    h = Harness(world=wq, spawn=tuple(home), limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(5.0)
        z_home = float(h.S.enu.home[h.slot()][2])
        z_cruise = max(top + 2.0, z_home + 20.0)  # 起点低于返航高度：RTL 需要先 CLIMB
        rep, cid = h.goto([h.pos()[0], h.pos()[1], z_cruise], timeout_s=90.0)
        assert h.call(cid).status == "succeeded", h.call(cid).code
        rep, cid = h.goto([dest[0], dest[1], z_cruise], timeout_s=120.0)
        assert h.call(cid).status == "succeeded", (rep, h.call(cid).code)
        s = h.slot()
        plan = h.svc.energy.rtl_plan(s)
        # 与 M09 同一走廊容差（RtlParams.h_top_tol_m，INT-1）：采样上界不低于精确值
        h_top = float(wq.heightmap_top_along(h.pos()[:2], h.S.enu.home[s][:2], tol_m=h.svc.params.rtl.h_top_tol_m))
        h_exact = float(wq.heightmap_top_along(h.pos()[:2], h.S.enu.home[s][:2], exact=True))
        assert plan.z_rtl_m >= h_top + 5.0 - 1e-6 and h_top >= h_exact - 1e-3
        rep = h.cmd("rtl", {}, cid="rtl")
        assert rep["status"] == "accepted", rep
        c = h.call("rtl")
        min_clear = np.inf
        while not c.final and h.core.clock.t_ns < 400e9:
            h.advance(0.2)
            if h.fs()[0] == "RTL" and h.fs()[1] in ("CRUISE", "DESCEND"):
                p = h.pos()
                min_clear = min(min_clear, float(p[2] - wq.height_dsm(p[None, :2])[0]))
        assert c.status == "succeeded", (c.status, c.code, h.state())
        assert min_clear >= 2.0, min_clear
        p = h.pos()
        assert np.hypot(p[0] - home[0], p[1] - home[1]) <= 2.0
    finally:
        h.close()
