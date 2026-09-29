"""M13-AC-003：坐标帧链（Python 与 M02 frames、AWR-03 §5.1 第 7 条、golden 一致）；M13-FR-003。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

from awr.sim.sensors.frames import (
    R_FLU_CAM,
    R_FLU_OPT,
    R_to_quat_xyzw,
    gimbal_R,
    gimbal_R_s,
    mat3_mul_s,
    mount_R,
    quat_to_R,
    quat_to_R_s,
)
from awr.world.georef import frames as m02

GOLD = json.loads((Path(__file__).parent / "golden" / "frames.json").read_text())


def test_constants_match_baseline_and_m02():
    assert np.array_equal(R_FLU_CAM, np.array([[0, 0, -1], [-1, 0, 0], [0, 1, 0]], float))  # AWR-03 §5.1 第 7 条
    assert np.array_equal(R_FLU_OPT, m02.R_FLU_RDF)
    assert np.array_equal(R_FLU_CAM, m02.R_FLU_RUB)
    assert np.allclose(GOLD["R_FLU_CAM"], R_FLU_CAM.ravel(), atol=0) and np.allclose(GOLD["R_FLU_OPT"], R_FLU_OPT.ravel(), atol=0)


def test_gimbal_first_column_is_optical_axis():
    for g in GOLD["gimbal"]:
        a, e = math.radians(g["az_deg"]), math.radians(g["el_deg"])
        R = gimbal_R(a, e)
        assert np.allclose(R.ravel(), g["R"], atol=1e-12)
        assert np.allclose(R[:, 0], [math.cos(a) * math.cos(e), math.sin(a) * math.cos(e), math.sin(e)], atol=1e-12)
        assert np.allclose(gimbal_R_s(a, e), R.ravel(), atol=1e-15)


def test_mount_zyx_and_pitch_forward_is_down():
    for m in GOLD["mount"]:
        assert np.allclose(mount_R(m["rpy_deg"]).ravel(), m["R"], atol=1e-12)
    R = mount_R([0, 20, 0])
    assert R[2, 0] == pytest.approx(-math.sin(math.radians(20)))  # 光轴下俯 20°


def test_quaternion_round_trip_and_m02_parity():
    rng = np.random.default_rng(3)
    q = rng.normal(size=(500, 4))
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    q[q[:, 3] < 0] *= -1
    R = quat_to_R(q)
    assert np.allclose(R_to_quat_xyzw(R), q, atol=1e-12)
    for i in range(20):
        assert np.allclose(R[i], m02.quat_to_mat(q[i]), atol=1e-12)
        assert np.allclose(quat_to_R_s(*q[i]), R[i].ravel(), atol=1e-15)
    assert np.allclose(mat3_mul_s(R[0].ravel(), R[1].ravel()), (R[0] @ R[1]).ravel(), atol=1e-15)
