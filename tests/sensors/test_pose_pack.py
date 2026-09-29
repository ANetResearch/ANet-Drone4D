"""M13-AC-007：SensorPose48 打包（只发布兴趣集并标记集；10 Hz；位置与姿态精度；flags）；M13-FR-013。"""

from __future__ import annotations

import math

import msgpack
import numpy as np

from awr.contracts.layouts import SENSOR_POSE48
from awr.sim.sensors.frames import R_to_quat_xyzw, gimbal_R, quat_to_R
from awr.sim.sensors.spec import rig_for_model


def rows_of(payloads):
    return [(m["t_sim_ns"], np.frombuffer(m["rows"], SENSOR_POSE48)) for m in (msgpack.unpackb(p, raw=False) for p in payloads)]


def test_only_interest_set_camera_and_thermal(bench):
    b = bench
    for s in range(20):
        b.spawn(s, 100 + s, pos=(s * 10.0, 0, 50), yaw_deg=s * 7.0)
    b.spawn(20, 200, "x500", pos=(0, 50, 30))
    out = []
    b.rt.packer.sink = out.append
    b.interest([103, 107, 200])
    b.run(1.0)
    got = rows_of(out)
    agents = {int(a) for _t, r in got for a in r["agent_no"]}
    assert agents == {103, 107, 200}
    ts = sorted({t for t, _ in got})
    # 首轮在首次装配后立即开始，其后对齐 100 ms 网格：10 Hz【仿真】±1 帧
    assert len(ts) == 10 and np.allclose(np.diff(ts[1:]), 1e8, atol=4e6) and all(t % 100_000_000 == 0 for t in ts[1:])
    kinds = {(int(a), int(k), int(n)) for _t, r in got for a, k, n in zip(r["agent_no"], r["kind"], r["sensor_no"], strict=True)}
    assert kinds == {(103, 0, 0), (103, 2, 1), (107, 0, 0), (107, 2, 1), (200, 0, 0)}  # D1 无 LiDAR 列


def test_pose_accuracy_and_flags(bench):
    b = bench
    yaw = 33.0
    b.spawn(0, 7, pos=(123.4, -56.7, 80.0), yaw_deg=yaw)
    out = []
    b.rt.packer.sink = out.append
    b.interest([7])
    b.run(0.02)
    b.rt.set_mode(0, "camera", "fixed", {"az_rad": 0.4, "el_rad": -0.7})
    b.run(1.5)
    _t, r = rows_of(out)[-1]
    cam = rig_for_model("p600").by_name("camera")
    h = math.radians(yaw) / 2
    Rb = quat_to_R(np.array([[0, 0, math.sin(h), math.cos(h)]]))[0]
    pos = np.array([123.4, -56.7, 80.0]) + Rb @ cam.mount_t
    q = R_to_quat_xyzw(Rb @ cam.mount_R @ gimbal_R(0.4, -0.7))
    row = r[r["sensor_no"] == 0][0]
    assert np.max(np.abs(row["pos"] - pos)) <= 1e-3
    qq = row["q"] if np.dot(row["q"], q) >= 0 else -row["q"]
    assert np.max(np.abs(qq - q)) <= 1e-6
    assert row["flags"] == 3 and row["hfov_rad"] == np.float32(cam.hfov_rad) and row["range_m"] == 300.0
    b.rt.set_active(0, "camera", False)
    b.run(0.2)
    _t, r = rows_of(out)[-1]
    assert r[r["sensor_no"] == 0][0]["flags"] == 0 and r[r["sensor_no"] == 1][0]["flags"] == 3


def test_no_publish_without_interest_or_when_paused(bench):
    b = bench
    b.spawn(0, 1)
    out = []
    b.rt.packer.sink = out.append
    b.run(0.5)
    assert out == []
    b.interest([1])
    b.run(0.25)
    n = len(out)
    assert n >= 2
    for _ in range(50):  # 时间不前进：不开始新一轮
        b.rt.packer.run_slow(b.ctx)
    assert len(out) == n


def test_slices_of_16(bench_factory):
    b = bench_factory(capacity=128)
    for s in range(80):
        b.spawn(s, s + 1)
    out = []
    b.rt.packer.sink = out.append
    b.interest(range(1, 81))
    b.slow_pose = False
    b.run(0.02)
    n = 0
    while b.rt.packer.run_slow(b.ctx) is not False:
        n += 1
        if n > 10:
            break
    assert n == 5 and [len(np.frombuffer(msgpack.unpackb(p)["rows"], SENSOR_POSE48)) for p in out] == [32] * 5
