"""M13-AC-015：GNSS 输出（1000 架静止集合的水平误差 95% 分位、eph 公式、NO_FIX 为 null）；M13-FR-031、FR-034。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sim.sensors.enums import GnssFix
from awr.sim.sensors.gnss import gnss_table
from awr.sim.sensors.spec import rig_for_model

TB = gnss_table(rig_for_model("p600").by_name("gnss"))


def horiz_p95(b, fix: int) -> float:
    blk = b.S.blocks["sensors"]
    slots = np.arange(1000)
    blk["gn_fix"][slots] = fix
    errs = []
    for k in range(3):
        b.run(1.0)
        e = b.rt.gnss.error_enu(slots, b.S.agent_no[slots].astype(np.int64), b.tick)
        errs.append(np.hypot(e[:, 0], e[:, 1]))
        del k
    return float(np.percentile(np.concatenate(errs), 95))


def test_ensemble_95th_percentiles(bench_factory):
    b = bench_factory(capacity=1024, seed=21)
    for s in range(1000):
        b.spawn(s, s + 1, pos=(s * 1.0, 0.0, 30.0))
    b.slow_pose = b.slow_obs = False
    b.run(0.02)
    assert np.all(b.S.blocks["sensors"]["gn_fix"][:1000] == GnssFix.RTK_FIXED)  # warm start
    assert horiz_p95(b, int(GnssFix.RTK_FIXED)) == pytest.approx(0.0274, rel=0.10)
    # SINGLE 档（t_float 之前，置回 SINGLE 并刷新计时，只看误差尺度）
    b.S.blocks["sensors"]["gn_t_state_ns"][:1000] = b.tick * 4_000_000 + 10**12
    assert horiz_p95(b, int(GnssFix.SINGLE)) == pytest.approx(2.98, rel=0.10)


def test_eph_epv_formula_and_nofix_null(bench):
    for f in (1, 2, 3, 4):
        assert TB.eph[f] == pytest.approx(math.sqrt(2) * math.hypot(TB.sigma_h[f], TB.white_h[f]), abs=1e-12)
        assert TB.epv[f] == pytest.approx(math.hypot(TB.sigma_v[f], TB.white_v[f]), abs=1e-12)
    b = bench
    b.spawn(0, 1)
    b.run(0.02)
    s = b.rt.gnss.summary(0)
    assert s["gnss_fix"] == 4 and s["sats"] == 22 and s["eph_m"] == pytest.approx(round(TB.eph[4], 4)) and s["hdop"] == 0.8
    b.S.blocks["sensors"]["gn_fix"][0] = 0
    s = b.rt.gnss.summary(0)
    assert s["eph_m"] is None and s["epv_m"] is None and s["hdop"] is None
    e = b.rt.gnss.error_enu(np.array([0]), np.array([1]), b.tick)
    assert np.all(np.isnan(e))
