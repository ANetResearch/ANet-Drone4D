"""M13-AC-008：相机几何库（in_fov 与逐点投影一致、平地足迹解析解、pixel_of 与 golden/TS 一致）；M13-FR-014。"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from awr.sim.sensors.camera import CameraGeom, project, world_pose
from awr.sim.sensors.frames import gimbal_R
from awr.sim.sensors.spec import rig_for_model

GOLD = json.loads((Path(__file__).parent / "golden" / "intrinsics.json").read_text())
CAM = rig_for_model("p600").by_name("camera")


def random_pose(rng):
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    from awr.sim.sensors.frames import quat_to_R

    return rng.uniform(-100, 100, 3), quat_to_R(q[None])[0]


def test_in_fov_agrees_with_pointwise_projection():
    rng = np.random.default_rng(1)
    pos, R = random_pose(rng)
    fwd = R[:, 0]
    pts = pos + fwd * rng.uniform(-50, 400, (100_000, 1)) + rng.normal(size=(100_000, 3)) * 150
    got = CameraGeom.in_fov(pts, pos, R, CAM, near_m=1.0, far_m=300.0)
    it = CAM.intr
    exp = np.zeros(len(pts), bool)
    for i in range(0, len(pts), 997):  # 逐点（math）与向量化判定一致
        d = R.T @ (pts[i] - pos)
        if d[0] <= 0:
            continue
        u = it.fx * (-d[1]) / d[0] + it.cx
        v = it.fy * (-d[2]) / d[0] + it.cy
        exp[i] = 1.0 <= d[0] <= 300.0 and 0 <= u <= it.w and 0 <= v <= it.h
        assert got[i] == exp[i]
    u, v, z = project(pts, pos, R, CAM)
    with np.errstate(invalid="ignore"):
        ref = (z >= 1) & (z <= 300) & (u >= 0) & (u <= it.w) & (v >= 0) & (v <= it.h)
    assert np.array_equal(got, ref) and 0.02 < got.mean() < 0.5
    px = CameraGeom.pixel_of(pts, pos, R, CAM)
    assert np.array_equal(np.isfinite(px[:, 0]), (z > 0) & (u >= 0) & (u <= it.w) & (v >= 0) & (v <= it.h))


def test_frustum_planes_contain_in_fov_points():
    rng = np.random.default_rng(2)
    pos, R = random_pose(rng)
    pts = pos + R[:, 0] * rng.uniform(0, 400, (20000, 1)) + rng.normal(size=(20000, 3)) * 120
    pl = CameraGeom.frustum_planes(pos, R, CAM, 1.0, 300.0)
    inside = np.all(pts @ pl[:, :3].T + pl[:, 3] >= -1e-9, axis=1)
    assert np.array_equal(inside, CameraGeom.in_fov(pts, pos, R, CAM, 1.0, 300.0))


def test_footprint_flat_dtm_analytic():
    h = 60.0
    pos = np.array([10.0, -5.0, h + 2.0])
    R = gimbal_R(0.0, -math.pi / 2)  # 天底
    grid = SimpleNamespace(a=np.full((400, 400), 2.0, np.float32), x0_m=-400.0, y0_m=-400.0, cell_m=2.0)
    fp = CameraGeom.footprint_on_dtm(pos, R, CAM, grid)
    it = CAM.intr
    ex = np.array([[pos[0] + h * (it.cy - v) / it.fy, pos[1] - h * (u - it.cx) / it.fx, 2.0]
                   for u, v in ((0, 0), (it.w, 0), (it.w, it.h), (0, it.h))])
    assert np.max(np.abs(fp - ex)) <= 1e-6
    W = np.linalg.norm(fp[0] - fp[1])
    assert pytest.approx(2 * h * math.tan(CAM.hfov_rad / 2), rel=1e-9) == W  # 足迹宽 W = 2h·tan(HFOV/2)
    up = gimbal_R(0.0, 0.2)
    assert np.all(np.isnan(CameraGeom.footprint_on_dtm(pos, up, CAM, grid)[:2]))


def test_footprint_accepts_callable_ground():
    pos = np.array([0.0, 0.0, 50.0])
    fp = CameraGeom.footprint_on_dtm(pos, gimbal_R(0.0, -math.pi / 2), CAM, lambda xy: np.zeros(len(xy)))
    assert np.allclose(fp[:, 2], 0.0) and np.all(np.isfinite(fp))


def test_pixel_of_matches_golden():
    for case in GOLD["cases"]:
        s = case["sensor"]

        class S:
            intr = SimpleNamespace(**{k: s[k] for k in ("w", "h", "fx", "fy", "cx", "cy")})

        pts = np.asarray(case["pixels"]["pts_flu"]).reshape(-1, 3)
        u, v, _ = project(pts, np.zeros(3), np.eye(3), S)
        assert np.max(np.abs(np.stack([u, v], 1).ravel() - case["pixels"]["uv"])) <= 1e-6


def test_world_pose_mount_and_gimbal():
    pq = np.array([[1.0, 2.0, 30.0, 0.0, 0.0, math.sin(0.3), math.cos(0.3)]])
    pos, R = world_pose(pq, CAM, np.array([0.2]), np.array([-0.5]))
    from awr.sim.sensors.frames import quat_to_R

    Rb = quat_to_R(pq[:, 3:])[0]
    assert np.allclose(pos[0], pq[0, :3] + Rb @ CAM.mount_t)
    assert np.allclose(R[0], Rb @ CAM.mount_R @ gimbal_R(0.2, -0.5))
