"""M13-AC-031：传感器开关（STANDBY：ACTIVE = 0、不参与检测；有状态噪声过程与对照运行逐位一致）；M13-FR-016。"""

from __future__ import annotations

import msgpack
import numpy as np
from m13_s3lib import T1

from awr.contracts.layouts import SENSOR_POSE48


def scenario(b, off: bool):
    b.rt.configure_vehicle("a1", caps=["rgb.zoom"])
    b.spawn(0, 1, pos=(T1[0], T1[1], 60.0), vid="a1")
    b.spawn(1, 2, pos=(0.0, 0.0, 30.0))
    b.run(0.02)
    b.rt.set_default_for(0, "lawnmower", {})
    b.rt.spawn_target("t1", T1.tolist(), "person")
    out = []
    b.rt.packer.sink = out.append
    b.interest([1])
    if off:
        b.rt.set_active(0, "camera", False)
    b.run(8.0)
    return out


def test_standby_camera(bench_factory):
    b_on = bench_factory(seed=5)
    on = scenario(b_on, False)
    b_off = bench_factory(seed=5)
    off = scenario(b_off, True)
    last = np.frombuffer(msgpack.unpackb(off[-1])["rows"], SENSOR_POSE48)
    assert last[last["sensor_no"] == 0]["flags"][0] == 0
    assert b_on.events.of("sensor.detect") and not b_off.events.of("sensor.detect")
    s_on, s_off = b_on.S.blocks["sensors"], b_off.S.blocks["sensors"]
    for k in ("gn_z", "im_zb", "im_b0", "gn_fix"):
        assert np.array_equal(s_on[k], s_off[k]), k  # 噪声过程照常推进
    assert s_off["state"][0, 0] == 4 and s_off["act"][0] == 63 & ~1
    b_off.rt.set_active(0, "camera", True)
    b_off.run(0.2)
    last = np.frombuffer(msgpack.unpackb(b_off.rt.packer.last_payload)["rows"], SENSOR_POSE48)
    assert last[last["sensor_no"] == 0]["flags"][0] == 3 and s_off["state"][0, 0] == 2
    del on


def test_unknown_sensor_rejected(bench):
    import pytest

    from awr.sim.sensors.gimbal import SensorError

    bench.spawn(0, 1, "x500")
    bench.run(0.02)
    with pytest.raises(SensorError) as e:
        bench.rt.set_active(0, "thermal", False)
    assert e.value.detail == "SENSOR_UNKNOWN"
