"""M19-AC-015：航迹。静止人：CONFIRMED 后位置误差 p95 ≤ 2 m（R ≤ 500 m）；1.2 m/s 游走：p95 ≤ 4 m（云台跟随航迹）；30 s 无量测
DROPPED；状态 TENTATIVE → CONFIRMED（2 s 内 ≥ 3 次）→ COASTING（1 s 无更新）；多机量测融合；滤波器单元（恒速、协方差增长）。"""

from __future__ import annotations

import numpy as np
import pytest
from plib import FakeWorld, PBench, make_imaging

from awr.sim.perception import gx40
from awr.sim.perception.tracks import CONFIRMED, TrackTable

P0 = (0.0, 0.0, 60.0)


def _errors(b: PBench, tid: str, seconds: float, follow: bool) -> list[float]:
    trt = b.trt
    errs = []
    for _ in range(round(seconds / 0.2)):
        if follow:
            st = trt.state1(tid)
            b.platforms[0].imaging[0] = make_imaging(gx40(), P0, (st.pos[0], st.pos[1], 0.9), f_mm=24.0)
        b.run(0.2)
        tr = b.prt.track(tid, b.t)
        if tr is not None and tr.state == "CONFIRMED":
            truth = np.array(trt.state1(tid).pos)
            errs.append(float(np.linalg.norm(np.array(tr.pos)[:2] - truth[:2])))
    return errs


def test_static_person_p95_within_2m():
    b = PBench(1, FakeWorld())
    with b:
        tid = b.add({"class": "person", "pos": [450.0, 0.0, 0.0], "motion": {"mode": "static"}})
        b.step(1)
        b.platform(0, pos=P0, imaging=[make_imaging(gx40(), P0, (450.0, 0.0, 0.9), f_mm=48.0)])
        errs = _errors(b, tid, 30.0, False)
        assert len(errs) > 100 and float(np.percentile(errs, 95)) <= 2.0


def test_walking_person_p95_within_4m():
    b = PBench(1, FakeWorld())
    with b:
        tid = b.add({"class": "person", "pos": [300.0, 0.0, 0.0],
                     "motion": {"mode": "random_walk", "speed_mps": 1.2, "speed_max_mps": 1.5,
                                "region": [[200.0, -100.0], [400.0, -100.0], [400.0, 100.0], [200.0, 100.0]]}})
        b.step(1)
        b.platform(0, pos=P0, imaging=[make_imaging(gx40(), P0, (300.0, 0.0, 0.9), f_mm=24.0)])
        errs = _errors(b, tid, 60.0, True)
        assert len(errs) > 200 and float(np.percentile(errs, 95)) <= 4.0


def test_states_and_drop_after_30s():
    b = PBench(1, FakeWorld())
    with b:
        tid = b.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}})
        b.step(1)
        b.platform(0, pos=P0, imaging=[make_imaging(gx40(), P0, (300.0, 0.0, 0.9), f_mm=48.0)])
        b.run(0.25)
        assert b.prt.track(tid, b.t).state == "TENTATIVE"
        b.run(0.6)
        assert b.prt.track(tid, b.t).state == "CONFIRMED"
        b.platforms[0].imaging = []
        b.run(1.5)
        tr = b.prt.track(tid, b.t)
        assert tr.state == "COASTING" and tr.cov_diag[0] > 0.2
        b.run(29.0)
        assert b.prt.track(tid, b.t) is None


def test_filter_unit_constant_velocity_and_fusion():
    tt = TrackTable()
    rng = np.random.default_rng(1)
    for k in range(50):
        t = 0.2 * k
        z = np.array([1.0 * t, 2.0 * t, 0.0]) + rng.normal(0, 0.3, 3)
        tt.update(7, "person", t, z, 0.3)
        tt.update(7, "person", t, np.array([1.0 * t, 2.0 * t, 0.0]) + rng.normal(0, 0.3, 3), 0.3)
    tr = tt.get(7, 10.0)
    assert tr.state == "CONFIRMED" and tr.n_updates == 100
    assert tr.vel[0] == pytest.approx(1.0, abs=0.4) and tr.vel[1] == pytest.approx(2.0, abs=0.4)
    assert tr.pos[0] == pytest.approx(10.0, abs=0.8)
    later = tt.get(7, 15.0)
    assert later.cov_diag[0] > tr.cov_diag[0]
    tt.tick(11.5)
    assert tt.state_of(7) != CONFIRMED
