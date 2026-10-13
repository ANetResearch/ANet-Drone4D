"""M13-AC-207：D1 并存（Mock 检测器不观察 M18 识别物；P600 v2 在剧本中的 D1 槽位与编号；D1 组行为不变）；M13-FR-214、FR-215。"""

from __future__ import annotations

import numpy as np

from awr.sim.sensors.spec import rig_entries


def _m18_targets():
    return {"target": {"active": np.array([1, 1], np.uint8), "target_no": np.array([1, 2], np.uint16),
                       "tgt_pos": np.array([[10.0, 0.0, 0.0], [0.0, 15.0, 0.0]]), "tgt_size": np.ones((2, 3), np.float32),
                       "tgt_yaw": np.zeros(2)}}


def test_mock_detector_ignores_m18_targets(bench_d2_factory):
    b = bench_d2_factory(sandbox=False)
    b.ctx.entities = _m18_targets()
    b.spawn(0, 1, "p600_mid360", vid="p1", pos=(0.0, 0.0, 40.0))
    b.rt.set_mode(0, "camera", "nadir")
    b.run(3.0)
    assert not b.rt.detector.enabled() and b.events.of("sensor.detect") == []


def test_p600_v2_in_scenario_keeps_d1_slots(bench_d2_factory):
    b = bench_d2_factory(sandbox=False)
    b.rt.configure_vehicle("s1", sensors=["camera"])
    b.rt.configure_vehicle("s2", sensors=["eo", "gnss", "imu"])
    b.spawn(0, 1, "p600_mid360", vid="s0")
    b.spawn(1, 2, "p600_mid360", vid="s1")
    b.spawn(2, 3, "p600_mid360", vid="s2")
    b.run(0.1)
    rt = b.rt
    r0, r1, r2 = rt.rig_of(0), rt.rig_of(1), rt.rig_of(2)
    # 剧本缺省：D1 槽位 + default 槽位；camera 占云台列 0（FPV、视锥与孪生表路径名称 camera 可用）
    assert [s.name for s in r0.specs] == ["camera", "thermal", "mid360", "gnss", "imu", "eo"]
    assert r0.col_specs[0].name == "camera" and r0.col_specs[1].name == "thermal" and r0.col_of("eo") is None
    assert [s.name for s in r1.specs] == ["camera"] and r2.col_specs[0].name == "eo"
    # sensor_no 按映射顺序，不随子集变化（CAT-AC-017）
    assert [(s.name, s.sensor_no) for s in r0.specs][:5] == [("camera", 0), ("thermal", 1), ("mid360", 2), ("gnss", 3), ("imu", 4)]
    assert rig_entries(r2) == [{"sensor_no": 5, "name": "eo", "kind": "camera"}]
    pos, R = rt.sensor_world_pose(np.array([0, 1]), "camera")
    assert np.all(np.isfinite(pos)) and np.all(np.isfinite(R))
    # 剧本中的 camera 槽位仍带 S3 Mock 检测器能力
    assert int(b.S.blocks["sensors"]["det_en"][0]) & 1 and int(b.S.blocks["sensors"]["det_en"][1]) & 1


def test_d1_rig_has_zero_mount_mask(bench):
    # 纯 D1 映射的机型（x500；P600 自 2.0.0 起为对象形式，D2-INT）：挂载掩码 0、完整组
    b = bench
    b.spawn(0, 1, "x500")
    b.run(0.1)
    assert int(b.S.blocks["payload"]["mnt"][0]) == 0 and b.rt.rig_of(0).mask == 0
    assert np.all(b.S.blocks["payload"]["src"][0] == 0)
    # D1 机体的载荷消息（兴趣集内才生成）：camera 无光学变焦，f_mm 为空
    m = b.rt.payload.message(0, 0)
    assert [e["sensor"] for e in m["sensors"]] == ["camera"]
    assert m["sensors"][0]["f_mm"] is None and m["gimbal"]["src"] == "default"
