"""M25 触发网格、PoseHistory 与帧记录（§6.2.2、§6.2.3、§6.2.5、§6.7；M25-FR-020 至 023）。"""

from __future__ import annotations

import json
import math
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from awr.sensorsvc.camera import CameraModel
from awr.sensorsvc.camera.proj import R_FLU_OPT
from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.poses import Mount, PoseHistory, q_world_opt, sensor_pose, slerp
from awr.sensorsvc.timing import check_exposure, deliver_tick, parse_rate, row_mid_ns, t_end_ns, t_mid_ns
from awr.sim.sensors.frames import quat_to_R

ROOT = Path(__file__).resolve().parents[3]


def test_rational_trigger_grid_and_delivery_ticks() -> None:
    r = parse_rate("15")
    ends = [t_end_ns(0, r, k) for k in (1, 2, 3)]
    assert ends == [66_666_666, 133_333_333, 200_000_000]
    assert [deliver_tick(0, t) for t in ends] == [1, 2, 2]                      # 100、200、200 ms 的 tick
    assert parse_rate("12.5") == Fraction(25, 2) and parse_rate(30.0) == 30
    assert t_end_ns(10, parse_rate("10"), 7) == 700_000_010
    tm = t_mid_ns(200_000_000, 8_000_000, 10_000_000)
    assert tm == 191_000_000
    assert row_mid_ns(tm, 10_000_000, 720, 0) < tm < row_mid_ns(tm, 10_000_000, 720, 719)
    assert row_mid_ns(tm, 0, 720, 0) == tm
    check_exposure(parse_rate("30"), 8_000_000, 25_000_000)
    with pytest.raises(SensorSvcError) as e:
        check_exposure(parse_rate("30"), 8_000_000, 30_000_000)
    assert e.value.code == 649 and e.value.name == "SENSOR_RIG_INVALID"


def _yaw_q(a: float) -> np.ndarray:
    return np.array([0.0, 0.0, math.sin(a / 2), math.cos(a / 2)])


def test_pose_history_interpolates_and_refuses_extrapolation() -> None:
    H = PoseHistory("u1")
    H.append(0, [0, 0, 10], _yaw_q(0.0))
    H.append(4_000_000, [4, 0, 10], _yaw_q(math.radians(40)))
    p, q = H.pose_at(1_000_000)
    assert np.allclose(p, [1, 0, 10])
    assert math.degrees(2 * math.atan2(q[2], q[3])) == pytest.approx(10.0)
    p, q = H.pose_at(4_000_000)
    assert np.allclose(p, [4, 0, 10])
    for t in (-1, 4_000_001):
        with pytest.raises(SensorSvcError) as e:
            H.pose_at(t)
        assert e.value.code == 646
    with pytest.raises(SensorSvcError):
        H.append(4_000_000, [0, 0, 0], _yaw_q(0))
    assert np.allclose(slerp(_yaw_q(0.1), -_yaw_q(0.3), 0.5), _yaw_q(0.2))     # 同半球


def test_sensor_pose_chain_mount_and_gimbal() -> None:
    H = PoseHistory("u1")
    H.append(0, [10, 20, 50], _yaw_q(math.radians(90)))                       # 机头朝北
    H.append(4_000_000, [10, 20, 50], _yaw_q(math.radians(90)))
    H.append_gimbal(0, 0, 0.0, math.radians(-90))                               # 云台正下视
    pos, R, g = sensor_pose(H, Mount((0.1, 0.0, -0.05), (0, 0, 0)), 0, 2_000_000)
    assert np.allclose(pos, [10, 20.1, 49.95])
    assert np.allclose(R @ [1, 0, 0], [0, 0, -1], atol=1e-12) and g[1] == pytest.approx(-math.pi / 2)
    pos, R, _ = sensor_pose(H, Mount((0, 0, 0), (0, 20, 0)), 5, 0)            # 无云台样本：挂载前倾 20°
    assert np.allclose(R @ [1, 0, 0], [0, math.cos(math.radians(20)), -math.sin(math.radians(20))], atol=1e-12)
    q = q_world_opt(R)
    assert np.allclose(quat_to_R(q), R @ R_FLU_OPT, atol=1e-12)


def test_camera_frame_record_validates_against_frame_v1() -> None:
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    import jsonschema

    from awr.sensorsvc.camera.raster import RenderOut
    from awr.sensorsvc.frame import FrameContext, camera_frame

    cm = CameraModel.from_hfov(64, 48, 84.0)
    rgb = np.zeros((48, 64, 3), np.uint8)
    rgb[..., 0] = 200
    depth = np.full((48, 64), 120.0, np.float32)
    depth[:10] = 0.0
    out = RenderOut(rgb, depth, np.zeros((48, 64), np.uint16), np.where(depth > 0, 0, 255).astype(np.uint8), 1.0, 1.0, 3)
    ctx = FrameContext("run-1", "sess-1", "8eff39605772", "NVIDIA A40", "580.95.05")
    R = np.eye(3)
    rec, blocks = camera_frame(ctx, uav="u1", sensor_id="cam0", seq=7, t_mid_ns=696_000_000, exposure_ns=8_000_000, readout_ns=0, cm=cm,
                               mount=Mount(), gimbal_reading=(0.0, -0.1, 0.0), gimbal_true=(0.0, -0.1, 0.0), pos_enu_m=np.array([1.0, 2.0, 30.0]),
                               R_ws=R, out=out, outputs={"rgb", "depth", "gt_depth"}, ref_prefix="u1/cam0/7/")
    schema = json.loads((ROOT / "packages/contracts/sensor/v2/frame.v1.schema.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(rec)
    assert rec["frame_id"] == "run-1/u1/cam0/7" and blocks[rec["agent_view"]["rgb"]["ref"]][:2] == b"\xff\xd8"
    import hashlib

    assert hashlib.sha256(blocks["u1/cam0/7/rgb.jpg"]).hexdigest() == rec["agent_view"]["rgb"]["sha256"]
    assert np.frombuffer(blocks["u1/cam0/7/depth.f32"], "<f4").reshape(48, 64)[20, 20] == 120.0
    import zstandard

    mask = np.frombuffer(zstandard.ZstdDecompressor().decompress(blocks["u1/cam0/7/invalid_mask.u8.zst"]), np.uint8).reshape(48, 64)
    assert mask[20].sum() == 0                                                  # 有几何：无标记
    assert set(rec["agent_view"]) >= {"K", "dist", "gimbal_reading"} and "T_world_sensor" not in rec["agent_view"]


def test_camera_frame_objects_from_unoccluded_pass() -> None:
    """有演员时 `instance_unocc` 写出真实掩膜、`objects[]` 按 §6.6.1 归约且满足 frame.v1。"""
    pytest.importorskip("PIL")
    pytest.importorskip("zstandard")
    import jsonschema
    import zstandard

    from awr.sensorsvc.camera.raster import RenderOut
    from awr.sensorsvc.frame import FrameContext, camera_frame

    cm = CameraModel.from_hfov(64, 48, 84.0)
    inst = np.zeros((48, 64), np.uint16)
    unocc = np.zeros((48, 64), np.uint16)
    unocc[20:30, 30:34] = 1
    inst[20:25, 30:34] = 1
    sem = np.zeros((48, 64), np.uint8)
    sem[20:25, 30:34] = 32
    out = RenderOut(np.zeros((48, 64, 3), np.uint8), np.full((48, 64), 50.0, np.float32), inst, sem, 1.0, 1.0, 3, instance_unocc=unocc,
                    actors_drawn=1)
    acts = [{"idx": 0, "id": "person_A", "class": "person"}]
    rec, blocks = camera_frame(FrameContext("run-1", "sess-1", "v", "gpu", "drv", dynamic_source_id="ab" * 32), uav="u1", sensor_id="cam0",
                               seq=1, t_mid_ns=0, exposure_ns=1_000_000, readout_ns=0, cm=cm, mount=Mount(), gimbal_reading=(0.0, 0.0, 0.0),
                               gimbal_true=(0.0, 0.0, 0.0), pos_enu_m=np.zeros(3), R_ws=np.eye(3), out=out, outputs={"rgb"}, actors=acts)
    jsonschema.Draft202012Validator(json.loads((ROOT / "packages/contracts/sensor/v2/frame.v1.schema.json").read_text())).validate(rec)
    (o,) = rec["ground_truth"]["objects"]
    assert (o["actor"], o["px_unoccluded"], o["px_visible"], o["h_px_unocc"], o["occluder_sem_px"]) == ("person_A", 40, 20, 10, {"0": 20})
    ref = rec["ground_truth"]["instance_unocc"]["ref"]
    assert np.array_equal(np.frombuffer(zstandard.ZstdDecompressor().decompress(blocks[ref]), "<u2").reshape(48, 64), unocc)
