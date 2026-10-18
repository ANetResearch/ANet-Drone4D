"""M25 EGL 光栅相机（§6.4.2、§6.4.3；M25-FR-030 至 033、039）：需要 moderngl 与 EGL（dmax 容器 d3-* 内运行；本机跳过）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appfix import wall_enu, write_world

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


@pytest.fixture(scope="module")
def wall_cam(tmp_path_factory):
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:
        pytest.skip(f"EGL unavailable: {e}")
    from awr.sensorsvc.camera.raster import RasterCamera

    root = write_world(tmp_path_factory.mktemp("wall"), scene=wall_enu(), world_id="wall-fixture")
    c = RasterCamera(load_appearance(root, tile_m=64.0), ctx=ctx)
    yield c
    c.release()


def _rs(cm, pose, T: float):
    """读出窗口 [−T/2, T/2]（相对 t_mid）内 9 个节点与行时刻函数。"""
    from awr.sensorsvc.camera.raster import RS_NODES

    nodes = [pose(-T / 2 + k * T / (RS_NODES - 1)) for k in range(RS_NODES)]
    t_row = lambda i: (np.asarray(i, np.float64) - (cm.height - 1) / 2) * T / cm.height  # noqa: E731
    return nodes, t_row


def test_rolling_shutter_translation_depth_per_row(wall_cam) -> None:
    """沿光轴 50 m/s 平移、读出 20 ms：每行平面深度 = 40 − x(t_row(i))（M25 §15.4）；全局快门各行相同。"""
    cm = CameraModel.from_hfov(640, 480, 60.0, readout_s=0.020)
    v, x0 = 50.0, 0.0
    pose = lambda t: (gimbal_R(0.0, 0.0), np.array([x0 + v * t, 0.0, 30.0]))  # noqa: E731
    nodes, t_row = _rs(cm, pose, 0.020)
    r = wall_cam.render(cm, *pose(0.0), rs_nodes=nodes)
    gs = wall_cam.render(cm, *pose(0.0))
    rows = np.arange(40, 440)
    want = 40.0 - (x0 + v * t_row(rows))
    got = r.depth_m[rows, 320]
    assert np.abs(got - want).max() < 2e-3, np.abs(got - want).max()
    assert np.abs(gs.depth_m[rows, 320] - 40.0).max() < 1e-3 and abs(float(got[0] - got[-1])) > 0.8


def test_rolling_shutter_yaw_skews_vertical_edge(wall_cam) -> None:
    """90°/s 偏航、读出 20 ms：墙左缘（y = +10）在每行的列位置与逐行真值投影一致（≤ 1 px，量化 0.5 px 内另加插值误差）。"""
    from awr.sensorsvc.camera.proj import project_points

    cm = CameraModel.from_hfov(640, 480, 60.0, readout_s=0.020)
    w = np.radians(90.0)
    pose = lambda t: (gimbal_R(np.degrees(w * t), 0.0), np.array([0.0, 0.0, 30.0]))  # noqa: E731
    nodes, t_row = _rs(cm, pose, 0.020)
    r = wall_cam.render(cm, *pose(0.0), rs_nodes=nodes)
    zs = np.linspace(0.0, 60.0, 6001)
    edge = np.stack([np.full_like(zs, 40.0), np.full_like(zs, 10.0), zs], 1)
    errs, cols = [], []
    for i in range(60, 420, 6):
        uv, _ = project_points(cm, *pose(float(t_row(i))), edge)
        k = int(np.argmin(np.abs(uv[:, 1] - (i + 0.5))))
        u = uv[k, 0]
        hit = np.flatnonzero(r.depth_m[i] > 0)
        assert hit.size
        errs.append(abs(hit[0] - (u - 0.5)))
        cols.append(hit[0])
    assert max(errs) <= 1.0, max(errs)
    assert cols[-1] - cols[0] > 10                       # 左转（yaw 增大）时后读出的下方行看到的边更靠右：竖直边明显倾斜


ACTORS = [
    {"idx": 0, "id": "p0", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
     "appearance": {"asset": "person-proxy@1", "top_rgb": [42, 93, 176], "bottom_rgb": [40, 40, 40]}},
    {"idx": 1, "id": "v0", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
     "appearance": {"asset": "vehicle-proxy@1", "body_rgb": [210, 210, 214]}},
    {"idx": 2, "id": "p1", "class": "person", "scripted": False, "profile": "person", "size_mm": [500, 300, 1700],
     "appearance": {"asset": "person-proxy@1", "tint_rgb": [180, 60, 40]}},
]


def test_actors_instances_unoccluded_pass_and_visibility(cam) -> None:
    """M25 §6.4.4、§6.6.1：实例号 idx + 1、语义 32/33、深度偏置不改平面深度；盒后演员只出现在无遮挡通道，遮挡物计入静态类别。"""
    with cam.ctx:                                            # 本模块另有 wall_cam 上下文：切回 cam 的 EGL 上下文
        _actors_case(cam)


def _actors_case(cam) -> None:
    from awr.sensorsvc.camera.actors import visibility_objects
    from awr.sensorsvc.camera.raster import ActorFrame

    cam.set_actors(ACTORS)
    cm = CameraModel.from_hfov(640, 480, 30.0)
    p = np.array([0.0, 0.0, 1.5])
    af = ActorFrame(np.array([0, 1, 2]), np.array([[20.0, 0.0, 0.0], [30.0, 6.0, 0.0], [60.0, 3.0, 0.0]]), np.array([0.0, 1.57, 0.0]))
    r = cam.render(cm, gimbal_R(0.0, 0.0), p, actors=af)
    assert r.actors_drawn == 3 and r.instance_unocc is not None
    m0 = r.instance == 1
    assert m0.any() and (r.semantic[m0] == 32).all() and (r.semantic[r.instance == 2] == 33).all()
    assert not (r.instance == 3).any() and (r.instance_unocc == 3).any()                       # 盒后的人：只在无遮挡通道
    d = r.depth_m[m0]
    assert d.min() > 19.6 and d.max() < 20.3                                                   # 平面深度无偏置
    rgb = r.rgb8[m0].astype(int)
    assert np.median(rgb[:, 2]) > np.median(rgb[:, 0])                                         # 蓝色上装可辨
    objs = {o["inst"]: o for o in visibility_objects(r.instance, r.instance_unocc, r.semantic, None, ACTORS)}
    assert set(objs) == {1, 2, 3}
    o1, o3 = objs[1], objs[3]
    assert o1["visible_ratio"] == 1.0 and o1["h_px_unocc"] == pytest.approx(1.75 * cm.fy / 20.0, abs=4)
    assert o3["px_visible"] == 0 and o3["bbox_xywh"] == [0, 0, 0, 0] and o3["occluder_sem_px"] == {"0": o3["px_unoccluded"]}
    assert objs[2]["class"] == 33 and o1["flags"] == []
    rs = cam.render(cm, gimbal_R(0.0, 0.0), p, actors=af, rs_nodes=[(gimbal_R(0.0, 0.0), p)] * 9)
    assert np.array_equal(rs.instance, r.instance) and np.array_equal(rs.instance_unocc, r.instance_unocc)
    none = cam.render(cm, gimbal_R(0.0, 0.0), p)
    assert none.instance_unocc is None and not (none.instance > 0).any()


def test_actor_behind_camera_is_clipped(cam) -> None:
    """相机后方与跨越相机平面的演员不得被深度偏置翻到前方（M25 §6.4.4 深度偏置只作用于 z > 0）。"""
    from awr.sensorsvc.camera.raster import ActorFrame

    with cam.ctx:
        cam.set_actors(ACTORS)
        cm = CameraModel.from_hfov(640, 480, 60.0)
        p = np.array([0.0, 0.0, 1.5])
        af = ActorFrame(np.array([0, 1]), np.array([[-5.0, 0.0, 0.0], [0.5, 0.0, 0.0]]), np.array([0.0, 0.0]))
        r = cam.render(cm, gimbal_R(0.0, 0.0), p, actors=af)
        assert not (r.instance == 1).any()                                     # 完全在后方
        ys, xs = np.nonzero(r.instance == 2)                                   # 跨越相机平面的车：只出现在画面下部近处
        assert len(ys) == 0 or (r.depth_m[ys, xs] < 3.0).all()
