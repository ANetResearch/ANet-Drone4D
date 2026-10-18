"""PoseHistory 真值通道（M24 §6.3；契约 sensor/v2/pose_history.v1）：网格校验、环形容量与 634、释放水位、切片含外侧相邻样本、
存档消息编码与契约校验、VehicleStatus 派生与时钟档规则（637）。"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from awr.phys import PhysError
from awr.phys.adapter import CLOCK_PROFILES, StatusSignals, VehicleStatus, derive_status, steps_per_tick, validate_clock
from awr.phys.pose_history import PoseHistoryBuffer, PoseHistoryRangeError, unpack_pose_history

ROOT = Path(__file__).resolve().parents[3]
DT = 4_000_000


def _block(k: int, n_uav: int = 2, n: int = 25) -> tuple[np.ndarray, np.ndarray]:
    t = k * n * DT + DT * np.arange(1, n + 1, dtype=np.int64)
    s = np.zeros((n, n_uav, 16))
    s[:, :, 0] = t[:, None] / 1e9                        # x = t（便于核对）
    s[:, :, 1] = np.arange(n_uav)[None, :]
    s[:, :, 6] = 1.0                                     # qw
    s[:, :, 15] = 9.80665
    return t, s


def test_append_block_grid_slice_and_release() -> None:
    b = PoseHistoryBuffer(["uav-01", "uav-02"], 250, capacity_s=0.5)          # 125 个样本
    assert b.cap == 125 and b.dt_ns == DT
    for k in range(5):
        b.append_block(*_block(k))
    assert b.latest_t("uav-02") == 125 * DT
    sl = b.slice("uav-02", 10 * DT + 1, 12 * DT)                              # 含外侧相邻样本
    assert sl.t_ns.tolist() == [10 * DT, 11 * DT, 12 * DT] and np.all(sl.state[:, 1] == 1.0)
    assert sl.sample(0).f_b_mps2[2] == pytest.approx(9.80665)
    with pytest.raises(PoseHistoryRangeError):
        b.slice("uav-01", 0, 126 * DT)
    with pytest.raises(PhysError) as e:                                        # 未释放即写满
        b.append_block(*_block(5))
    assert e.value.code == 634
    b.release_before(50 * DT)
    assert b.slice("uav-01", 50 * DT, 51 * DT).t_ns[0] == 50 * DT
    with pytest.raises(PoseHistoryRangeError):
        b.slice("uav-01", 49 * DT, 51 * DT)
    b2 = PoseHistoryBuffer(["uav-01", "uav-02"], 250, capacity_s=0.5)
    b2.append_block(*_block(0))
    t, s = _block(2)
    with pytest.raises(PhysError) as e:                                        # 网格不连续
        b2.append_block(t, s)
    assert e.value.code == 635


def test_message_roundtrip_and_contract() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    b = PoseHistoryBuffer(["uav-01"], 250)
    t, s = _block(0, n_uav=1)
    b.append_block(t, s)
    b.set_gimbal("uav-01", 0, 0, 0.1, -0.5)
    b.set_gimbal("uav-01", 0, 40_000_000, 0.1, -0.5)                          # 未变化：不追加
    b.set_gimbal("uav-01", 0, 60_000_000, 0.2, -0.5)
    msg = b.drain_message("uav-01")
    assert b.drain_message("uav-01") is None
    hdr, arr = unpack_pose_history(msg)
    schema = json.loads((ROOT / "packages/contracts/sensor/v2/pose_history.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(hdr)
    assert hdr["n"] == 25 and np.array_equal(arr["t_ns"], t) and np.array_equal(arr["pos_enu_m"], s[:, 0, 0:3])
    assert arr["f_b_mps2"].dtype == np.float32 and arr["f_b_mps2"][0, 2] == np.float32(9.80665)
    assert hdr["gimbal"]["m"] == 2 and arr["gimbal.t_ns"].tolist() == [0, 60_000_000]
    t2, s2 = _block(1, n_uav=1)
    b.append_block(t2, s2)
    hdr2, arr2 = unpack_pose_history(b.drain_message("uav-01"))
    assert arr2["t_ns"][0] == 26 * DT and "gimbal" not in hdr2


def test_status_and_clock_rules() -> None:
    assert derive_status(StatusSignals(link_ok=False, crash_cause="world")) is VehicleStatus.LOST
    assert derive_status(StatusSignals(crash_cause="impact", in_air=True)) is VehicleStatus.CRASHED
    assert derive_status(StatusSignals(landed=True, in_air=True)) is VehicleStatus.GROUNDED
    assert derive_status(StatusSignals(in_air=True, cmd_mode="LAND")) is VehicleStatus.LANDING
    assert derive_status(StatusSignals(in_air=True)) is VehicleStatus.AIRBORNE
    assert steps_per_tick(100, 250) == 25
    validate_clock("realistic", CLOCK_PROFILES["d1_250"], 100, ("rgb", "lidar", "imu"))
    for tier, cp, tick, outs in (("realistic", CLOCK_PROFILES["sandbox100"], 100, ()), ("fast", CLOCK_PROFILES["d1_250_l1_125"], 100, ()),
                                 ("fast", CLOCK_PROFILES["d1_250_l1_125"], 96, ("lidar",)), ("fast", CLOCK_PROFILES["d1_250"], 102, ())):
        with pytest.raises(PhysError) as e:
            validate_clock(tier, cp, tick, outs)
        assert e.value.code == 637
    validate_clock("fast", CLOCK_PROFILES["d1_250_l1_125"], 96, ("rgb",))
