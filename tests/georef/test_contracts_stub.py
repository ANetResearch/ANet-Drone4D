"""LiDAR frame and IMU contract stubs (M02-AC-015; M02-FR-013, FR-014; AWR-16 §14.3)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from awr.contracts._paths import contracts_root
from awr.world.georef.lidar import contract as L


def rules(fs):
    return {(f.rule, f.severity, f.code) for f in fs}


def test_schema_compiles_and_positive_fixture_passes():
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(json.loads((contracts_root() / "sensor" / "lidar_frame.schema.json").read_text()))
    f = L.make_fixture("valid")
    assert L.validate_frame(f, dev_type=9, prev_seq=122) == []
    b = L.encode_payload(f)
    assert len(b) == L.POINT_BYTES * f.n
    g = L.decode_payload(f.header, b)
    for k in ("x", "y", "z", "offset_time", "reflectivity", "tag", "line"):
        assert np.array_equal(getattr(g, k), getattr(f, k)), k


@pytest.mark.parametrize(("kind", "rule", "code"), [("line4", "L-01", None), ("offset_backwards", "L-02", None),
                                                    ("unsynced_no_offset", "L-04", 490), ("too_many_points", "L-07", None)])
def test_negative_fixtures_rejected(kind, rule, code):
    fs = L.validate_frame(L.make_fixture(kind), dev_type=9)
    assert (rule, "error", code) in rules(fs), fs


def test_model_dev_type_and_frame_seq():
    fs = L.validate_frame(L.make_fixture("wrong_model"), dev_type=9, prev_seq=100)
    assert ("L-06", "error", None) in rules(fs) and ("L-09", "warn", None) in rules(fs)
    assert L.validate_frame(L.make_fixture("wrong_model"), dev_type=35) == []


def test_time_grid_and_unsynced_with_estimate():
    f = L.make_fixture("valid")
    f.header["raw_timebase_ns"] += 3_000_000
    assert ("L-03", "warn", None) in rules(L.validate_frame(f))
    g = L.make_fixture("valid")
    g.header.update(sync_type="none", timescale="host_mono", offset_est_ns=12_345)
    assert rules(L.validate_frame(g)) == {("L-04", "warn", None)}


def test_imu_units_and_driver_extrinsic():
    rest_g = np.tile([0.01, -0.02, 1.0], (200, 1))
    assert L.check_imu_static(rest_g, "g") == []
    assert np.allclose(L.imu_acc_to_mps2([0, 0, 1.0], "g"), [0, 0, 9.80665])
    bad = L.check_imu_static(rest_g, "m/s^2")                    # g values declared as m/s^2
    assert bad and bad[0].code == 491
    assert L.check_imu_static(rest_g * 9.80665, "m/s^2") == []
    assert L.check_driver_extrinsic({"roll": 0, "pitch": 0, "yaw": 0, "x": 0, "y": 0, "z": 0}) == []
    assert L.check_driver_extrinsic([0, 1, 0, 0, 0, 0])[0].code == 492
    assert L.check_driver_extrinsic(np.eye(4)) == []


def test_lio_tags_pointcloud2_and_custommsg():
    assert L.lio_valid_mask(np.array([0x00, 0x10, 0x20, 0x30, 0x01, 0x11], np.uint8)).tolist() == [True, True, False, False,
                                                                                                    True, True]
    assert 200 <= L.pointcloud2_time_resolution_ns(1.7e18) <= 256
    msg = {"timebase": 1_790_000_000_100_000_000, "points": [
        {"x": 1.0, "y": 2.0, "z": 3.0, "offset_time": 10, "reflectivity": 50, "tag": 0x10, "line": 1},
        {"x": 1.5, "y": 2.5, "z": 3.5, "offset_time": 20, "reflectivity": 160, "tag": 0x00, "line": 2}]}
    f = L.custommsg_to_frame(msg, sensor_id="p600-01/mid360", frame_id="uav01/mid360", t_sim_ns=5_000_000_000,
                             sync_type="ptp", timescale="tai", frame_seq=7, offset_est_ns=0)
    assert f.header["raw_timebase_ns"] == msg["timebase"] and f.header["timebase_ns"] == 5_000_000_000
    assert L.validate_frame(f, dev_type=9) == []


def test_v05_interface_drafts():
    """M02-AC-018 (P2): V0.5 signatures and value types exist; AWLT tile header round-trips."""
    import math

    from awr.world.georef.localization.service import InitialGuess, LioBackend, LocalizationService, LocState
    from awr.world.georef.release.tiles import HEADER, TileHeader, decode_tile, encode_tile
    from awr.world.georef.time import SessionTimebase, StreamClock, SyncType, Timescale

    s = LocState()
    j = s.to_json()
    assert j["status"] == "NO_MAP" and j["fitness_m2"] is None and math.isnan(s.fitness_m2) and j["last_ok_ns"] == -1
    g = InitialGuess.with_defaults("pad", np.eye(4))
    assert g.sigma_pos_m == 0.1
    with pytest.raises(ValueError):
        InitialGuess("manual", np.eye(4), 5.0, 0.1)
    for name in ("load_release", "set_initial_guess", "on_odom", "on_scan", "tick"):
        assert hasattr(LocalizationService, name)
    assert hasattr(LioBackend, "run")
    tb = SessionTimebase(0, {"lidar": StreamClock("lidar", SyncType.PTP, Timescale.TAI)})
    assert tb.streams["lidar"].timescale == Timescale.TAI
    r = np.random.default_rng(0)
    h = TileHeader((12, -3), (768.0, -192.0, -5.0), 0.0625, 37, True)     # f32-exact quantum for the equality check
    xyz = r.integers(0, 65535, (37, 3)).astype(np.uint16)
    nrm = r.integers(1, 65535, 37).astype(np.uint16)
    inten = r.integers(0, 255, 37).astype(np.uint8)
    b = encode_tile(h, xyz, nrm, inten)
    assert len(b) % 8 == 0 and HEADER.size == 48
    h2, x2, n2, i2 = decode_tile(b)
    assert h2 == h and np.array_equal(x2, xyz) and np.array_equal(n2, nrm) and np.array_equal(i2, inten)
