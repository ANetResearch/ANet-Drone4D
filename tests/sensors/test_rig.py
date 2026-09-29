"""M13-AC-002：传感器组与 roster `sensors[]`（sensor_no 在机体生命周期内稳定）；M13-FR-002。"""

from __future__ import annotations

from awr.sim.sensors.spec import rig_entries, rig_for_model


def test_roster_entries_only_fov_kinds():
    e = rig_entries(rig_for_model("p600"))
    assert e == [{"sensor_no": 0, "name": "camera", "kind": "camera"}, {"sensor_no": 1, "name": "thermal", "kind": "thermal"},
                 {"sensor_no": 2, "name": "mid360", "kind": "lidar"}]
    assert rig_entries(rig_for_model("x500")) == [{"sensor_no": 0, "name": "camera", "kind": "camera"}]


def test_scenario_subset_keeps_numbers():
    assert rig_entries(rig_for_model("p600"), ["thermal"]) == [{"sensor_no": 1, "name": "thermal", "kind": "thermal"}]


def test_sensor_no_stable_when_other_vehicles_come_and_go(bench):
    b = bench
    b.spawn(0, 10)
    b.spawn(1, 11, "x500")
    b.spawn(2, 12)
    b.run(0.1)
    rt = b.rt
    before = [(s.name, s.sensor_no) for s in rt.rig_of(2).specs]
    b.remove(1)
    b.run(0.02)
    assert b.S.blocks["sensors"]["init"][1] == 0
    b.spawn(3, 13)
    b.spawn(1, 14, "x500")
    b.run(0.1)
    after = [(s.name, s.sensor_no) for s in rt.rig_of(2).specs]
    assert before == after
    blk = b.S.blocks["sensors"]
    assert blk["init"][2] == 13 and blk["has"][2] == 63 and blk["init"][1] == 15 and blk["has"][1] == 1
    assert rt.stats["spawned"] == 5 and rt.stats["removed"] == 1


def test_configure_vehicle_subset_and_caps(bench):
    b = bench
    b.rt.configure_vehicle("a1", caps=["rgb.zoom"])
    b.rt.configure_vehicle("b1", caps=["thermal.imaging"])
    b.rt.configure_vehicle("c1", caps=["comm.relay"])
    b.rt.configure_vehicle("d1", sensors=["camera", "gnss"])
    for s, (no, vid) in enumerate([(1, "a1"), (2, "b1"), (3, "c1"), (4, "d1")]):
        b.spawn(s, no, vid=vid)
    b.run(0.02)
    blk = b.S.blocks["sensors"]
    assert list(blk["det_en"][:4]) == [1, 2, 0, 1]
    assert blk["has"][3] == 1 | 4 and blk["act"][3] == 1 | 4
    # 装配后再改：d1 加上热成像
    b.rt.configure_vehicle("d1", sensors=["camera", "thermal", "gnss"])
    assert blk["has"][3] == 1 | 2 | 4 and blk["det_en"][3] == 3
