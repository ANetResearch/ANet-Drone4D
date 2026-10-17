"""M25 相机核心（§6.2.1、§6.4.1、§6.4.2；M25-FR-006、031、032）：坐标约定、reversed-Z 投影、瓦片模型视图与外观场景分块。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appfix import scene_enu, write_world

from awr.sensorsvc.camera import CameraModel, look_at_R, proj_reversed_z, project_points, world_to_opt
from awr.sensorsvc.camera.proj import gimbal_R, mv_tile, ray_elevation
from awr.sensorsvc.camera.scene import load_appearance


def test_reversed_z_projection_matches_pinhole_pixels() -> None:
    cm = CameraModel(1280, 720, 700.0, 705.0, 641.3, 359.2, near_m=0.3, far_m=3000.0)
    rng = np.random.default_rng(1)
    R = gimbal_R(30.0, -25.0)
    p = np.array([10.0, -5.0, 80.0])
    X = p + (R @ np.array([1.0, 0.0, 0.0])) * rng.uniform(5, 500, (200, 1)) + rng.normal(0, 30, (200, 3))
    uv, z = project_points(cm, R, p, X)
    M = world_to_opt(R, p)
    clip = (np.c_[X, np.ones(len(X))] @ M.T) @ proj_reversed_z(cm).T
    ndc = clip[:, :3] / clip[:, 3:]
    ok = z > cm.near_m
    assert np.allclose((ndc[ok, 0] + 1) * cm.width / 2, uv[ok, 0], atol=1e-6)
    assert np.allclose((ndc[ok, 1] + 1) * cm.height / 2, uv[ok, 1], atol=1e-6)      # 行 0 = 图像顶部
    P = proj_reversed_z(cm)
    for zz, want in ((cm.near_m, 1.0), (cm.far_m, 0.0)):
        c = P @ np.array([0.0, 0.0, zz, 1.0])
        assert c[2] / c[3] == pytest.approx(want, abs=1e-9)


def test_tile_model_view_equals_world_view() -> None:
    R = gimbal_R(-60.0, -40.0)
    p = np.array([1234.5, -987.6, 120.0])
    o = np.array([1280.0, -768.0, 0.0])
    X = np.array([[1300.0, -900.0, 12.0], [1250.5, -700.25, 3.0]])
    a = (np.c_[X - o, np.ones(2)] @ mv_tile(R, p, o).T)[:, :3]
    b = (np.c_[X, np.ones(2)] @ world_to_opt(R, p).T)[:, :3]
    assert np.allclose(a, b, atol=1e-9)


def test_camera_frame_conventions() -> None:
    assert np.allclose(gimbal_R(0, 0), np.eye(3))
    assert np.allclose(gimbal_R(90, 0) @ [1, 0, 0], [0, 1, 0], atol=1e-12)          # yaw 90°：光轴指北
    assert np.allclose(gimbal_R(0, -90) @ [1, 0, 0], [0, 0, -1], atol=1e-12)        # pitch −90°：正下视
    cm = CameraModel.from_hfov(640, 480, 90.0)
    assert cm.fx == pytest.approx(320.0) and cm.hfov_deg == pytest.approx(90.0)
    p = np.array([0.0, 0.0, 100.0])
    R = look_at_R(p, np.zeros(3))
    assert np.allclose(R.T @ R, np.eye(3)) and np.linalg.det(R) == pytest.approx(1.0)
    uv, z = project_points(cm, R, p, np.array([[0.0, 0.0, 0.0], [0.0, 20.0, 0.0], [20.0, 0.0, 0.0]]))
    assert uv[0] == pytest.approx([320.0, 240.0]) and z[0] == pytest.approx(100.0)
    assert uv[1, 1] < 240 and uv[2, 0] > 320                                         # 正下视：北在上、东在右
    R2 = look_at_R(np.array([0.0, 0.0, 10.0]), np.array([100.0, 0.0, 10.0]))
    el = ray_elevation(cm, R2, 640, 480)
    assert el[0, 320] > 0 > el[-1, 320] and abs(el[240, 320]) < 0.01
    with pytest.raises(ValueError):
        CameraModel(640, 480, 500, 500, 320, 240, near_m=10, far_m=5)


def test_appearance_scene_tiles_reproduce_world_triangles(tmp_path: Path) -> None:
    write_world(tmp_path)
    V, F, _ = scene_enu()
    sc = load_appearance(tmp_path, tile_m=64.0)
    assert sc.triangles == len(F) and len(sc.textures) == 1
    assert len({b.tile for b in sc.batches}) == 16                                  # ±100 m 按 64 m 瓦片：4 × 4
    seen = np.zeros(len(F), int)
    for b in sc.batches:
        assert np.all(np.abs(b.pos[:, :2]) <= 64.0 / 2 + 64.0)                        # 局部坐标相对瓦片中心
        got = (b.pos.astype(np.float64) + b.origin)[b.idx]
        assert np.abs(got - V[F[b.tri_ids]]).max() < 1e-4
        seen[b.tri_ids] += 1
    assert (seen == 1).all()
    assert np.allclose(sc.bounds_min, [-100, -100, 0]) and np.allclose(sc.bounds_max, [100, 100, 30])
    assert math.isclose(float(sc.batches[0].origin[2]), 0.0)
