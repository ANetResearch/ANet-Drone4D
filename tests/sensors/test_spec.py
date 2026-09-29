"""M13-AC-001：传感器文件装配与校验（schema + 语义检查）；M13-FR-001、FR-051。"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest
import yaml

from awr.sim.sensors.spec import SensorSpecError, build_spec, load_rig, load_sensor_file, rig_for_model, vehicles_dir

P600 = vehicles_dir() / "p600" / "sensors"
FILES = ["camera", "thermal", "gnss", "imu", "mid360"]


def doc(name: str) -> dict:
    return yaml.safe_load((P600 / f"{name}.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FILES)
def test_p600_files_pass_schema(name):
    s = load_sensor_file(P600 / f"{name}.yaml")
    assert s.conf in "ABCD" and s.src


def test_x500_camera_same_as_p600():
    a = load_sensor_file(vehicles_dir() / "x500" / "sensors" / "camera.yaml")
    b = load_sensor_file(P600 / "camera.yaml")
    assert a.intr == b.intr and a.gimbal == b.gimbal and a.range_m == b.range_m


def test_camera_fov_from_intrinsics():
    s = load_sensor_file(P600 / "camera.yaml")
    assert math.degrees(s.hfov_rad) == pytest.approx(60.0, abs=1e-3)
    assert math.degrees(s.vfov_rad) == pytest.approx(42.10, abs=0.01)
    th = load_sensor_file(P600 / "thermal.yaml")
    assert math.degrees(th.hfov_rad) == pytest.approx(50.0, abs=0.01)
    lid = load_sensor_file(P600 / "mid360.yaml")
    assert lid.hfov_rad == pytest.approx(2 * math.pi) and math.degrees(lid.vfov_rad) == pytest.approx(59.4)


def test_asymmetric_principal_point_sums_both_sides():
    d = doc("camera")
    d["cx"] = 3500.0
    s = build_spec("camera", d, 0)
    assert s.hfov_rad == pytest.approx(math.atan(3500 / d["fx"]) + math.atan(2500 / d["fx"]))


def _reject(d: dict) -> SensorSpecError:
    with pytest.raises(SensorSpecError) as ei:
        build_spec("camera", d, 0, "test.yaml")
    e = ei.value
    assert e.code == 110 and e.detail == "SENSOR_SPEC_INVALID"
    assert e.to_json()["problems"]
    return e


def test_reject_missing_fx():
    d = doc("camera")
    del d["fx"]
    e = _reject(d)
    assert any("fx" in p for p in e.problems)


def test_reject_conf_outside_a_to_e():
    d = doc("camera")
    d["conf"] = "E"  # E 级只进 rejected[]（ADR-043）
    _reject(d)
    d["conf"] = "Z"
    e = _reject(d)
    assert any("conf" in p for p in e.problems)


def test_reject_inverted_gimbal_limits():
    d = doc("camera")
    d["gimbal"]["pitch_min_deg"], d["gimbal"]["pitch_max_deg"] = 30, -90
    e = _reject(d)
    assert any("GIMBAL_LIMITS_INVERTED" in p for p in e.problems)


def test_reject_rtk_fixed_sigma_mismatch():
    d = doc("gnss")
    d["rtk"]["fixed"]["sigma_h_m"] = 0.02
    with pytest.raises(SensorSpecError):
        build_spec("gnss", d, 3)


def test_lidar_imu_unit_stays_g():
    d = doc("mid360")
    assert d["imu"]["acc_unit"] == "g"
    bad = copy.deepcopy(d)
    bad["imu"]["acc_unit"] = "mps2"
    with pytest.raises(SensorSpecError):
        build_spec("mid360", bad, 2)
    assert set(d["mount_presets"]) == {"p600_prometheus_sim", "inverted_mapping"}


def test_rig_order_and_bits(tmp_path: Path):
    rig = rig_for_model("p600")
    assert [s.name for s in rig.specs] == ["camera", "thermal", "mid360", "gnss", "imu"]
    assert [s.sensor_no for s in rig.specs] == [0, 1, 2, 3, 4]
    assert rig.has_bits == 1 | 2 | 4 | 8 | 16 | 32
    # 目录里只有 gnss 与 camera 时，有视场的仍在前
    (tmp_path / "sensors").mkdir()
    for n in ("gnss", "camera"):
        (tmp_path / "sensors" / f"{n}.yaml").write_text((P600 / f"{n}.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    r2 = load_rig(tmp_path, {"gnss": "sensors/gnss.yaml", "camera": "sensors/camera.yaml"})
    assert [s.name for s in r2.specs] == ["camera", "gnss"]


def test_bad_rig_rejects_model(tmp_path: Path):
    (tmp_path / "sensors").mkdir()
    d = doc("camera")
    del d["fy"]
    (tmp_path / "sensors" / "camera.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    with pytest.raises(SensorSpecError):
        load_rig(tmp_path, {"camera": "sensors/camera.yaml"})
