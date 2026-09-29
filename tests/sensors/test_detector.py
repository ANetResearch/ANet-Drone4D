"""M13-AC-021、AC-022：检测器公式、频率无关、S3 检测链（caps 过滤、复检节流）；M13-FR-041 至 FR-045。"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from m13_s3lib import T1, run_s3, setup_s3

from awr.sim.sensors import cbrng
from awr.sim.sensors.detector import bayes_miss, expected_pd, pd_1s, pd_tick
from awr.sim.sensors.spec import rig_for_model

CAM = rig_for_model("p600").by_name("camera").detector
TH = rig_for_model("p600").by_name("thermal").detector


def test_expected_pd_is_the_detector_formula():
    rng = np.random.default_rng(8)
    A = rng.uniform(-300, 300, (1000, 3))
    B = rng.uniform(-300, 300, (1000, 3))
    r = np.linalg.norm(B - A, axis=1)
    for spec in (CAM, TH):
        got = expected_pd(spec, A, B)
        ref = spec.p0 * np.exp(-(r / spec.r_fp_m) ** 2)
        assert np.max(np.abs(got - ref)) <= 1e-12
        assert np.array_equal(got, pd_1s(spec.p0, spec.r_fp_m, r))
    assert expected_pd(CAM, [0, 0, 60], [0, 0, 0])[0] == pytest.approx(0.8 * math.exp(-(60 / 90) ** 2))  # 0.513
    assert expected_pd(TH, [0, 0, 60], [0, 0, 0])[0] == pytest.approx(0.95 * math.exp(-(60 / 150) ** 2))  # 0.81


class Env:
    def optical_depth(self, p0, p1, t, wavelength_nm=550.0):
        return np.linalg.norm(np.asarray(p1) - np.asarray(p0), axis=1) * math.log(20) / 3000.0  # MOR 3 km


def test_vis_enters_through_optical_depth():
    v = expected_pd(TH, [0, 0, 60], [0, 0, 0], Env())[0]
    assert v == pytest.approx(0.95 * math.exp(-(60 / 150) ** 2) * math.exp(-60 * math.log(20) / 3000.0))


def test_first_detection_time_independent_of_rate():
    pd = 0.5
    n = 20000
    trials = np.arange(n)
    means = []
    for dt in (0.2, 0.1):
        p = pd_tick(pd, dt)
        first = np.full(n, -1)
        k = 0
        while (first < 0).any() and k < 2000:
            u = cbrng.uniform(99, 4, trials, k, np.array([256]))[:, 0]
            hit = (u < p) & (first < 0)
            first[hit] = k
            k += 1
        means.append(float(((first + 0.5) * dt).mean()))
    assert abs(means[0] - means[1]) / means[1] <= 0.05
    assert means[1] == pytest.approx(1 / math.log(2), rel=0.03)


def test_bayes_miss():
    assert bayes_miss(0.5, 0.8) == pytest.approx(0.5 * 0.2 / (1 - 0.4))
    assert np.allclose(bayes_miss(np.array([0.1, 0.9]), 0.0), [0.1, 0.9])


def test_s3_chain_caps_and_repeats(bench):
    b = bench
    setup_s3(b)
    ev = run_s3(b, 40.0)
    assert ev, "no detection"
    first = ev[0]["data"]
    assert first["uav"] == "a1" and first["capability"] == "rgb.zoom" and first["state"] == "suspect" and first["conf"] == 0.42
    assert first["sensor"] == "camera" and first["artifact"] is None and first["repeat"] is False
    conf = [e for e in ev if e["data"]["state"] == "confirmed"]
    assert conf and conf[0]["data"]["uav"] == "b1" and conf[0]["data"]["conf"] == 0.9 and conf[0]["data"]["repeat"] is False
    assert conf[0]["data"]["capability"] == "thermal.imaging" and conf[0]["data"]["artifact"]["kind"] == "thermal_frame"
    rep = [e for e in conf[1:]]
    assert len(rep) >= 3 and all(e["data"]["repeat"] is True for e in rep)
    ts = [e["t_sim_ns"] for e in conf]
    assert all(t1 - t0 >= 2_000_000_000 for t0, t1 in itertools.pairwise(ts))
    assert {e["data"]["uav"] for e in ev} == {"a1", "b1"}  # c1（comm.relay）不参与
    assert all(e["data"]["capability"] == "thermal.imaging" for e in ev if e["data"]["uav"] == "b1")  # b1 的相机不检出
    p = ev[0]["data"]["pos_enu_m"]
    assert math.dist(p, T1) < 10.0 and ev[0]["data"]["range_m"] == pytest.approx(math.dist(b.S.enu.pos[0], T1), abs=1.0)
    assert b.rt.detector.targets.items()[0]["state"] == "confirmed"


def test_b1_camera_never_detects_unseen_target(bench):
    b = bench
    setup_s3(b)
    b.set_pose(0, (900.0, -900.0, 60.0))  # a1 远离
    move_pose = (T1[0], T1[1] + 1.0, 60.0)
    b.set_pose(1, move_pose)
    b.rt.set_default_for(1, "expanding_square", {})
    b.run(20.0)
    ev = b.events.of("sensor.detect")
    assert ev and all(e["data"]["uav"] == "b1" and e["data"]["sensor"] == "thermal" for e in ev)
    assert ev[0]["data"]["state"] == "confirmed"  # UNSEEN -> CONFIRMED（热成像直接确认）


def test_out_of_fov_and_forward_gimbal_rarely_detects(bench):
    b = bench
    b.rt.configure_vehicle("a1", caps=["rgb.zoom"])
    b.spawn(0, 1, pos=(T1[0], T1[1], 60.0), vid="a1")  # 缺省 FIXED(0, -15°)：正下方目标不在视场
    b.rt.spawn_target("t1", T1.tolist(), "person")
    b.run(10.0)
    assert b.events.of("sensor.detect") == []


def test_pairs_capped_at_16_with_rotation(bench_factory):
    b = bench_factory()
    for s in range(12):
        b.spawn(s, s + 1, pos=(s * 2.0, 0.0, 60.0))
    b.run(0.02)
    for s in range(12):
        b.rt.set_default_for(s, "lawnmower", {})
    for i in range(3):
        b.rt.spawn_target(f"t{i}", [i * 5.0, 0.0, 0.0], "person")
    b.run(0.6)
    st = b.rt.detector.stats
    assert st["rotated"] > 0 and st["pairs"] <= 16 * st["ticks"]
