"""M13-AC-004：后端可替换（FakeBackend 替换 Camera 位姿实现后 SensorPose48 字节不变）与 SensorModel 接口；M13-FR-004。"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors.backends.base import FakeBackend, SensorBackend
from awr.sim.sensors.models import SensorModel, models_for


def run_once(b, source=None):
    for s in range(6):
        b.spawn(s, s + 1, pos=(s * 3.0, -s * 2.0, 40.0 + s), yaw_deg=s * 25.0)
    out = []
    b.rt.packer.sink = out.append
    if source is not None:
        b.rt.packer.pose_source = source
    b.interest(range(1, 7))
    b.run(0.02)
    b.rt.set_mode(2, "camera", "look_at", {"p_enu_m": [30.0, 10.0, 0.0]})
    b.run(0.6)
    return out


def test_fake_backend_same_bytes(bench_factory):
    a = run_once(bench_factory())
    fb = FakeBackend()
    b = run_once(bench_factory(), fb.pose)
    assert a == b and len(a) >= 6


def test_protocols(bench):
    fb = FakeBackend()
    assert isinstance(fb, SensorBackend)
    rig = bench.rt  # models_for 只需要 runtime
    from awr.sim.sensors.spec import rig_for_model

    ms = models_for(rig, rig_for_model("p600").specs)
    assert [m.kind.name for m in ms] == ["CAMERA", "THERMAL", "LIDAR", "GNSS", "IMU"]
    assert all(isinstance(m, SensorModel) for m in ms)
    assert ms[0].describe()["caps"]["pose"] is True and ms[2].caps.raycast == "none"
    fb.open(None, {0: [rig_for_model("p600").by_name("camera")]})
    assert fb.request(np.array([0]), np.array([[0, 0, 10, 0, 0, 0, 1.0]]), 0) == 1 and len(fb.poll()) == 1
