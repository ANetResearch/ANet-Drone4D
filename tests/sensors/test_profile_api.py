"""M13-AC-009：`GET /api/fleet/profiles/{id}` 的 `sensors{}`（M08 ProfileTable.describe 经 register_sensor_describer）；M13-FR-015。"""

from __future__ import annotations

from awr.sim.sensors.describe import describe, dir_for_profile


def test_describe_fields_p600():
    d = describe("p600_mid360", {})
    assert [r["name"] for r in d["rig"]] == ["camera", "thermal", "mid360", "gnss", "imu"]
    cam = d["camera"]
    assert cam["intrinsics"] == {"w": 6000, "h": 4000, "fx": 5196.152, "fy": 5196.152, "cx": 3000.0, "cy": 2000.0, "dist": []}
    assert abs(cam["hfov_deg"] - 60.0) < 1e-3 and abs(cam["vfov_deg"] - 42.103) < 1e-3
    assert cam["gimbal"]["pitch_min_deg"] == -90 and cam["gimbal"]["default_pitch_deg"] == -15
    assert cam["mount"]["xyz_m"] == [0.12, 0.0, -0.08] and cam["conf"] == "D" and cam["src"]
    assert d["thermal"]["netd_k"] == 0.05 and d["mid360"]["mount"]["preset"] == "p600_prometheus_sim"
    assert d["gnss"]["gnss"]["rtk"] is True
    assert d["twin"] == [{"item": "Camera", "status": "present", "conf": "D"}, {"item": "LiDAR", "status": "present", "conf": "C"},
                         {"item": "RTK", "status": "present", "conf": "D"}]


def test_describe_x500_and_variant():
    assert dir_for_profile("x500_sih") == dir_for_profile("x500")
    d = describe("x500_sih", {})
    assert [r["name"] for r in d["rig"]] == ["camera"]
    assert [r["status"] for r in d["twin"]] == ["present", "missing", "missing"]
    assert describe("nope", {}) == {}


def test_profile_table_merges_sensors(bench):
    """装配插件后 M08 describe() 返回的 sensors{} 来自 M13（每进程登记一次）。"""
    item = bench.T.describe("p600_mid360")
    assert set(item["sensors"]) >= {"camera", "thermal", "mid360", "gnss", "imu", "rig", "twin"}


def test_plugin_hooks_for_m08(bench):
    """roster 传感器组与 estimate conf_expected 的回调（M08 钩子上线后登记）；bench 保证插件在隔离登记表中首次导入。"""
    import math

    from awr.sim.sensors.plugin import _conf_expected, _rig_entries_for_profile

    assert [e["name"] for e in _rig_entries_for_profile("p600_mid360")] == ["camera", "thermal", "mid360"]
    assert _rig_entries_for_profile("x500_sih", ["camera"]) == [{"sensor_no": 0, "name": "camera", "kind": "camera"}]
    v = _conf_expected("p600_mid360", [0, 0, 60], [0, 0, 0], "thermal.imaging")
    assert v is not None and abs(v - 0.95 * math.exp(-(60 / 150) ** 2)) < 1e-12
    assert _conf_expected("x500", [0, 0, 60], [0, 0, 0], "thermal.imaging") is None
