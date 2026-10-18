"""IMU 合成（M25 §6.9.1、§6.9.2）：位姿差分运动学、杆臂、200 Hz 绝对网格、噪声档与确定性。"""

from __future__ import annotations

import math

import numpy as np
import pytest

pytest.importorskip("scipy")

from awr.sensorsvc.imu import ACC_SAT, G, ImuModel, kinematics_from_poses, load_imu_model, synth_imu

NS = 1_000_000_000
I3 = np.eye(3)


def track(seconds: float = 4.0, hz: int = 250, yaw_rate: float = 0.0, acc_x: float = 0.0):
    t = np.arange(round(seconds * hz) + 1, dtype=np.int64) * (NS // hz)
    ts = t / NS
    pos = np.stack([0.5 * acc_x * ts**2, np.zeros_like(ts), np.full_like(ts, 30.0)], 1)
    yaw = yaw_rate * ts
    q = np.stack([np.zeros_like(ts), np.zeros_like(ts), np.sin(yaw / 2), np.cos(yaw / 2)], 1)
    return t, pos, q


def test_model_file_matches_builtin_defaults() -> None:
    assert load_imu_model() == ImuModel()


def test_hover_reads_plus_g_and_zero_rate() -> None:
    t, p, q = track()
    w, f = kinematics_from_poses(t, p, q)
    s = synth_imu(t, w, f, R_bi=I3, r_imu_b=np.zeros(3), model=ImuModel(), t_from_ns=0, t_to_ns=4 * NS, tier="clean")
    assert len(s.t_ns) == 800 and (np.diff(s.t_ns) == 5_000_000).all() and s.t_ns[0] == 0
    assert np.allclose(s.acc_mps2, [0.0, 0.0, G], atol=1e-9) and np.allclose(s.gyro_rps, 0.0, atol=1e-12)


def test_yaw_rate_forward_accel_and_mount_rotation() -> None:
    t, p, q = track(yaw_rate=0.5, acc_x=2.0)
    w, f = kinematics_from_poses(t, p, q)
    assert np.allclose(w[:, 2], 0.5, atol=1e-9) and np.allclose(w[:, :2], 0.0, atol=1e-12)
    s = synth_imu(t, w, f, R_bi=I3, r_imu_b=np.zeros(3), model=ImuModel(), t_from_ns=NS, t_to_ns=3 * NS, tier="clean")
    ts = s.t_ns / NS
    yaw = 0.5 * ts                                                # 世界 x 向 2 m/s² 在机体系的投影；滤波稳态后误差很小
    assert np.allclose(s.acc_mps2[:, 0], 2.0 * np.cos(yaw), atol=2e-3) and np.allclose(s.acc_mps2[:, 1], -2.0 * np.sin(yaw), atol=2e-3)
    R_inv = np.diag([1.0, -1.0, -1.0])                            # 倒装（绕 x 180°）：z 读数为 −g、yaw 速率取反
    s2 = synth_imu(t, w, f, R_bi=R_inv, r_imu_b=np.zeros(3), model=ImuModel(), t_from_ns=NS, t_to_ns=3 * NS, tier="clean")
    assert np.allclose(s2.acc_mps2[:, 2], -G, atol=1e-6) and np.allclose(s2.gyro_rps[:, 2], -0.5, atol=1e-9)


def test_lever_arm_centripetal() -> None:
    t, p, q = track(yaw_rate=2.0)
    w, f = kinematics_from_poses(t, p, q)
    s = synth_imu(t, w, f, R_bi=I3, r_imu_b=np.array([0.5, 0.0, 0.0]), model=ImuModel(), t_from_ns=NS, t_to_ns=2 * NS, tier="clean")
    assert np.allclose(s.acc_mps2[:, 0], -0.5 * 4.0, atol=1e-6)    # ω × (ω × r) = −ω² r


def test_noise_statistics_determinism_and_batching() -> None:
    t, p, q = track(seconds=60.0)
    w, f = kinematics_from_poses(t, p, q)
    m = ImuModel()
    kw = {"R_bi": I3, "r_imu_b": np.zeros(3), "model": m, "seed": 3, "uav_no": 1, "sensor_no": 0}
    a = synth_imu(t, w, f, t_from_ns=0, t_to_ns=60 * NS, tier="realistic", **kw)
    b = synth_imu(t, w, f, t_from_ns=0, t_to_ns=60 * NS, tier="realistic", **kw)
    assert np.array_equal(a.acc_mps2, b.acc_mps2) and np.array_equal(a.gyro_rps, b.gyro_rps)
    part = synth_imu(t, w, f, t_from_ns=30 * NS, t_to_ns=31 * NS, tier="realistic", **kw)       # 分段调用与整段逐位一致
    assert np.array_equal(part.acc_mps2, a.acc_mps2[6000:6200])
    dg = np.diff(a.gyro_rps[:, 0])                                # 差分去掉慢变偏置，剩白噪声 √2·sigma
    assert np.std(dg) / math.sqrt(2) == pytest.approx(m.gyro_nd * math.sqrt(200), rel=0.05)
    da = np.diff(a.acc_mps2[:, 2])
    assert np.std(da) / math.sqrt(2) == pytest.approx(m.acc_nd * math.sqrt(200), rel=0.05)
    h = synth_imu(t, w, f, t_from_ns=0, t_to_ns=60 * NS, tier="harsh", **kw)
    assert np.std(np.diff(h.gyro_rps[:, 0])) / np.std(dg) == pytest.approx(1.5, rel=1e-6)
    c = synth_imu(t, w, f, t_from_ns=0, t_to_ns=60 * NS, tier="realistic", **{**kw, "seed": 4})
    assert not np.allclose(a.gyro_rps, c.gyro_rps)
    assert (a.flags == 0).all()


def test_saturation_flag() -> None:
    t, p, q = track(seconds=1.0, acc_x=400.0)
    w, f = kinematics_from_poses(t, p, q)
    s = synth_imu(t, w, f, R_bi=I3, r_imu_b=np.zeros(3), model=ImuModel(), t_from_ns=0, t_to_ns=NS, tier="clean")
    assert s.flags[50:].all() and np.abs(s.acc_mps2).max() == pytest.approx(ACC_SAT)
