"""M25 EGL 光栅相机（§6.4.2、§6.4.3；M25-FR-030 至 033、039）：需要 moderngl 与 EGL（dmax 容器 d3-* 内运行；本机跳过）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appfix import write_world

from awr.sensorsvc.camera import CameraModel, look_at_R
from awr.sensorsvc.camera.proj import gimbal_R
from awr.sensorsvc.camera.raster import linear_to_srgb8
from awr.sensorsvc.camera.scene import load_appearance


@pytest.fixture(scope="module")
def cam(tmp_path_factory):
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                   # 无 EGL 设备
        pytest.skip(f"EGL unavailable: {e}")
    from awr.sensorsvc.camera.raster import RasterCamera

    root = write_world(tmp_path_factory.mktemp("app"))
    c = RasterCamera(load_appearance(root, tile_m=64.0), ctx=ctx)
    yield c
    c.release()


def test_nadir_depth_colour_and_ids(cam) -> None:
    cm = CameraModel.from_hfov(640, 480, 60.0)
    p = np.array([20.0, 0.0, 100.0])
    r = cam.render(cm, look_at_R(p, p - [0, 0, 1]), p)
    assert r.depth_m[240, 320] == pytest.approx(100.0, abs=1e-3)
    assert (r.depth_m > 0).all() and (r.semantic == 0).all() and (r.instance == 0).all()
    rgb = r.rgb8
    assert np.abs(rgb[100:380, 100:540].astype(int) - [200, 30, 30]).max() <= 2         # sRGB 往返
    box = r.depth_m < 99.0                                                              # 盒顶 30 m：深度 70 m
    assert box.any() and np.median(r.depth_m[box]) == pytest.approx(70.0, abs=1e-3)
    _, xs = np.nonzero(box)
    assert xs.mean() > 320                                                              # 东在右


def test_horizontal_view_sky_ground_and_box_face(cam) -> None:
    cm = CameraModel.from_hfov(640, 480, 60.0)
    p = np.array([0.0, 0.0, 10.0])
    r = cam.render(cm, gimbal_R(0.0, 0.0), p)
    assert r.depth_m[240, 320] == pytest.approx(40.0, abs=1e-3)                        # 盒子西面 x = 40
    assert r.depth_m[0, 320] == pytest.approx(40.0, abs=1e-3)                          # 上缘仍落在 30 m 高的盒面上
    corner = (slice(0, 40), slice(0, 60))                                               # 左上角：视线越过盒子，无几何
    assert (r.depth_m[corner] == 0).all() and (r.semantic[corner] == 255).all()
    v = 479.5 - cm.cy
    assert r.depth_m[479, 100] == pytest.approx(10.0 * cm.fy / v, rel=1e-3)           # 地面平面深度
    far = r.depth_m[250:260, 0]
    assert ((far == 0) | (far > 50)).all()
    sky = r.rgb8[0, 0].astype(int)
    assert sky[2] > sky[0]                                                              # 天空偏蓝
    lin = cam.render(cm, gimbal_R(0.0, 0.0), p, linear=True)
    assert np.abs(linear_to_srgb8(lin.rgb_lin).astype(int) - lin.rgb8).max() <= 1        # GPU 编码遍与 CPU 参照一致


def test_reversed_z_keeps_far_detail(cam) -> None:
    """近 0.3 m、远 3000 m：1 km 外盒面的平面深度误差在 1 cm 内；视线从盒顶下方 5 cm 掠过时仍命中盒面而非地面。"""
    cm = CameraModel.from_hfov(320, 240, 10.0)
    p = np.array([-1000.0, 0.0, 29.95])
    r = cam.render(cm, gimbal_R(0.0, 0.0), p)
    hit = r.depth_m[120, 160]
    assert hit == pytest.approx(1040.0, abs=1e-2)


@pytest.mark.parametrize("profile", ["clean", "realistic", "harsh"])
def test_gpu_effects_match_reference(cam, profile: str) -> None:
    """GPU 效果链遍与 numpy 参考实现（M25 §6.5、§6.17.2）：RGB 差 ≤ 1 DN 且 ≥ 99% 像素逐值相同；L2-depth 有效掩码相同、
    数值差 ≤ 1e-4·sigma(d) + 1e-5 m；测光小图与参考的 1/8 降采样逐值相同。"""
    from dataclasses import replace

    from awr.sensorsvc.camera import effects as fx

    prof = fx.profile(profile)
    cm = fx.with_profile(CameraModel.from_hfov(640, 480, 60.0), prof)
    p = np.array([0.0, 0.0, 35.0])
    R = gimbal_R(0.0, -35.0)
    st = fx.AeState()
    fp = fx.frame_params(cm, st, seed=11, uav_no=2, sensor_no=0, frame_index=9, t_mid_s=4.2,
                         depth=replace(prof.depth, d_max_m=80.0))
    gpu = cam.render(cm, R, p, linear=True, fx=fp)
    rgb, dob = fx.apply_fx_reference(gpu.rgb_lin, gpu.depth_m, cm, fp)
    diff = np.abs(gpu.rgb8.astype(int) - rgb.astype(int))
    assert diff.max() <= 1 and (diff == 0).mean() >= 0.99, (profile, diff.max(), (diff == 0).mean())
    assert np.array_equal(gpu.depth_obs > 0, dob > 0) and (dob > 0).mean() > 0.2
    d = gpu.depth_m
    tol = 1e-4 * prof.depth.a * (0.005 * d + 0.0005 * d * d) + 1e-5
    assert (np.abs(gpu.depth_obs - dob) <= tol).all()
    if fp.meter:
        assert np.array_equal(gpu.meter_sub, gpu.rgb_lin[::8, ::8])
