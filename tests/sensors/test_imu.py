"""M13-AC-017：IMU（悬停比力 + 偏置、白噪声 sigma = ND·√200、偏置稳态 sigma）；M13-FR-033。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sim.sensors import cbrng
from awr.sim.sensors.imu import G0, imu_table, specific_force
from awr.sim.sensors.spec import rig_for_model

TB = imu_table(rig_for_model("p600").by_name("imu"))


def test_specific_force_hover_and_tilt():
    f = specific_force(np.array([[0, 0, 0, 1.0]]), np.zeros((1, 3)))
    assert np.allclose(f, [[0, 0, G0]], atol=1e-12)
    h = math.radians(30) / 2  # 绕 x 滚转 30°：重力在机体 y、z 上分解
    f = specific_force(np.array([[math.sin(h), 0, 0, math.cos(h)]]), np.zeros((1, 3)))
    assert np.allclose(f, [[0, G0 * math.sin(math.radians(30)), G0 * math.cos(math.radians(30))]], atol=1e-12)


def test_hover_sample_decomposes_exactly(bench):
    b = bench
    b.spawn(0, 5, pos=(0, 0, 20))
    b.run(0.2)
    acc, gyro, bias = b.rt.imu.sample(b.S, np.array([0]), np.array([5]), b.tick)
    nw = cbrng.normal(b.rt.seed, 3, np.array([5]), b.tick, np.arange(3, 9))[0]
    k = math.sqrt(200.0)
    assert np.max(np.abs(acc[0] - (np.array([0, 0, G0]) + bias[0, 3:] + TB.nd[3:] * k * nw[3:]))) <= 1e-9
    assert np.max(np.abs(gyro[0] - (bias[0, :3] + TB.nd[:3] * k * nw[:3]))) <= 1e-9


def test_white_noise_and_bias_statistics(bench_factory):
    b = bench_factory(capacity=4096, seed=3)
    n = 4000
    for s in range(n):
        b.spawn(s, s + 1, pos=(0, 0, 20))
    b.slow_pose = b.slow_obs = False
    b.run(0.02)
    slots = np.arange(n)
    ag = b.S.agent_no[slots].astype(np.int64)
    acc, gyro, bias = b.rt.imu.sample(b.S, slots[:2500], ag[:2500], 1000)
    acc2, gyro2, bias2 = b.rt.imu.sample(b.S, slots[2500:5000], ag[2500:5000], 1001)
    wa = np.concatenate([acc - bias[:, 3:], acc2 - bias2[:, 3:]]) - [0, 0, G0]
    wg = np.concatenate([gyro - bias[:, :3], gyro2 - bias2[:, :3]])
    assert wa.size >= 10_000
    assert wa.std() == pytest.approx(4.0e-3 * math.sqrt(200), rel=0.05)  # 0.0566 m/s²
    assert wg.std() == pytest.approx(3.39e-4 * math.sqrt(200), rel=0.05)  # 4.79e-3 rad/s
    b.run(30.0)
    z = b.S.blocks["sensors"]["im_zb"][:n]
    assert z.std(axis=0) == pytest.approx(np.ones(6), rel=0.03)
    b0 = b.S.blocks["sensors"]["im_b0"][:n].astype(np.float64)
    assert b0[:, 0].std() == pytest.approx(8.73e-3, rel=0.05) and b0[:, 3].std() == pytest.approx(0.196, rel=0.05)
    assert TB.sigma_b[0] == pytest.approx(3.88e-5 * math.sqrt(500)) and TB.sigma_b[3] == pytest.approx(6.0e-3 * math.sqrt(150))
