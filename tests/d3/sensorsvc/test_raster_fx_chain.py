"""M25 光栅相机效果链与异步读回（docs/05 §5.4.3、M25 §6.5.7、§6.7；D3-AC-11）：渲染域着色遍（接触阴影、投影阴影图、湿润、消光、
雨线）与 CPU 参考实现、畸变重映射遍与 `proj.remap_*`、天空分类与 CPU 视线仰角掩码、`submit` 与同步 `render` 的一致性。
需要 moderngl 与 EGL（dmax 容器 d3-* 内运行；本机跳过）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from appfix import write_world

from awr.sensorsvc.camera import CameraModel, look_at_R
from awr.sensorsvc.camera import effects as fx
from awr.sensorsvc.camera.proj import build_remap, gimbal_R, ray_elevation, remap_bilinear, remap_nearest

DIST = (-0.08, 0.02, 0.0, 0.0, 0.0)
ACTORS = [
    {"idx": 0, "id": "p0", "class": "person", "scripted": True, "profile": "person", "size_mm": [500, 300, 1750],
     "appearance": {"asset": "person-proxy@1", "top_rgb": [42, 93, 176], "bottom_rgb": [40, 40, 40]}},
    {"idx": 1, "id": "v0", "class": "vehicle", "scripted": True, "profile": "vehicle", "size_mm": [4500, 1800, 1500],
     "appearance": {"asset": "vehicle-proxy@1", "body_rgb": [210, 210, 214]}},
]


@pytest.fixture(scope="module")
def cam(tmp_path_factory):
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                   # 无 EGL 设备
        pytest.skip(f"EGL unavailable: {e}")
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    root = write_world(tmp_path_factory.mktemp("fx"))
    c = RasterCamera(load_appearance(root, tile_m=64.0), ctx=ctx, readback_slots=4)
    c.set_actors(ACTORS)
    c.set_sun((135.0, 40.0))
    yield c
    c.release()


def _actors():
    from awr.sensorsvc.camera.raster import ActorFrame

    return ActorFrame(np.array([0, 1]), np.array([[-6.0, 4.0, 0.0], [4.0, -3.0, 0.0]]), np.array([0.3, 1.2]))


def _view():
    p = np.array([-25.0, -18.0, 22.0])
    return look_at_R(p, np.array([0.0, 0.0, 0.0])), p


def _params(cm, *, weather=None, shadow=True, frame=5):
    return fx.frame_params(cm, fx.AeState(), seed=3, uav_no=1, sensor_no=0, frame_index=frame, t_mid_s=2.0, weather=weather, shadow=shadow)


def test_shade_pass_matches_reference(cam) -> None:
    """接触阴影、湿润、雾与雨消光、雨线：GPU 着色遍与 `effects.shade_reference`（float64）在渲染域逐像素比较，≥ 99.9% 像素差
    ≤ 2e-3（雨滴哈希在单元边界与深度比较边界上允许个别像素不同）；阴影与雨确实改变了图像。"""
    with cam.ctx:
        cm = CameraModel.from_hfov(480, 320, 60.0, shadow="contact")
        R, p = _view()
        af = _actors()
        fp = _params(cm, weather=fx.Weather(visibility_m=2000.0, rain_mmh=8.0))
        assert fp.contact_shadow and not fp.shadow_map and fp.fog_sigma_m > 0 and fp.rain_mmh == 8.0 and fp.key_rain
        r = cam.render(cm, R, p, fx=fp, actors=af, debug=True)
    d = r.debug
    casters = fx.contact_casters(af.pos, cam.actor_dims[af.idx], cam.sun)
    ref = fx.shade_reference(d["lin_render"], d["z_render"], d["inst_render"], cm, R, p, fp, casters)
    diff = np.abs(d["shaded"] - ref).max(-1)
    assert (diff <= 2e-3).mean() >= 0.999, ((diff <= 2e-3).mean(), diff.max())
    changed = np.abs(d["shaded"] - d["lin_render"]).max(-1) > 1e-3
    assert changed.mean() > 0.5                                                   # 雾与湿润作用于几何像素


def test_contact_shadow_only_on_static_pixels_near_actor(cam) -> None:
    """接触阴影只压暗静态像素（实例 0），压暗区域位于演员脚底附近并沿背光方向延伸。"""
    with cam.ctx:
        cm = CameraModel.from_hfov(480, 320, 60.0, shadow="contact")
        R, p = _view()
        af = _actors()
        on = cam.render(cm, R, p, fx=_params(cm), actors=af, debug=True).debug
        off = cam.render(cm, R, p, fx=_params(cm, shadow=False), actors=af, debug=True).debug
    assert off.get("shaded") is None                                             # 晴天无阴影：不走着色遍
    ratio = on["shaded"][..., 1] / np.maximum(on["lin_render"][..., 1], 1e-6)
    dark = ratio < 0.97
    assert dark.any() and (on["inst_render"][dark] == 0).all()
    assert ratio.min() >= 1.0 - fx.CONTACT_K - 1e-3
    assert np.abs(on["shaded"][on["inst_render"] > 0] - on["lin_render"][on["inst_render"] > 0]).max() < 1e-6


def test_shadow_map_darkens_ground_away_from_sun(cam) -> None:
    """2048² 投影阴影图：`shadow = map` 的着色只会更暗（与接触阴影取最小）；新增的压暗像素在世界坐标中射向太阳的视线穿过某个
    演员的包围盒（几何阴影判定）。"""
    from awr.sensorsvc.camera.proj import R_FLU_OPT

    with cam.ctx:
        cmc = CameraModel.from_hfov(480, 320, 60.0, shadow="contact")
        cmm = CameraModel.from_hfov(480, 320, 60.0, shadow="map")
        R, p = _view()
        af = _actors()
        c = cam.render(cmc, R, p, fx=_params(cmc), actors=af, debug=True).debug
        m = cam.render(cmm, R, p, fx=_params(cmm), actors=af, debug=True).debug
    assert (m["shaded"] <= c["shaded"] + 1e-6).all()
    extra = (m["shaded"][..., 1] < c["shaded"][..., 1] * 0.97) & (m["inst_render"] == 0)
    assert extra.sum() > 20
    ii, jj = np.nonzero(extra)
    z = m["z_render"][ii, jj].astype(np.float64)
    r = np.stack([(jj + 0.5 - cmm.cx) / cmm.fx, (ii + 0.5 - cmm.cy) / cmm.fy, np.ones(len(ii))], 1) * z[:, None]
    X = r @ (R @ R_FLU_OPT).T + p
    ok = np.zeros(len(X), bool)
    for k, a in enumerate(af.idx):                                                # 射向太阳的视线穿过演员的有向包围盒（另加 0.2 m）
        c_, s_ = np.cos(af.yaw[k]), np.sin(af.yaw[k])
        Rz = np.array([[c_, s_, 0.0], [-s_, c_, 0.0], [0.0, 0.0, 1.0]])           # world → 演员体轴
        half = cam.actor_dims[a] / 2 + 0.2
        o = (X - (af.pos[k] + [0.0, 0.0, cam.actor_dims[a, 2] / 2])) @ Rz.T
        d = Rz @ cam.sun
        with np.errstate(divide="ignore", invalid="ignore"):
            t0, t1 = (-half - o) / d, (half - o) / d
        lo = np.nanmax(np.minimum(t0, t1), 1)
        hi = np.nanmin(np.maximum(t0, t1), 1)
        ok |= (hi >= np.maximum(lo, 0.0))
    assert ok.all(), ok.mean()


def test_distortion_resolve_matches_cpu_remap(cam) -> None:
    """畸变重映射遍：交付域线性 RGB 与 `remap_bilinear`（渲染域）差 ≤ 1e-4，平面深度、实例、语义、天空分类与 `remap_nearest`
    逐值相同；交付尺寸为相机尺寸、渲染域含裕量。"""
    with cam.ctx:
        cm = CameraModel.from_hfov(320, 240, 70.0, dist=DIST)
        R, p = _view()
        r = cam.render(cm, R, p, linear=True, actors=_actors(), debug=True)
    rt = build_remap(cm)
    d = r.debug
    assert r.remap is rt and d["z_render"].shape == (rt.render.height, rt.render.width) and r.rgb8.shape == (240, 320, 3)
    assert np.abs(r.rgb_lin - remap_bilinear(d["lin_render"], rt)).max() <= 1e-4
    assert np.array_equal(r.depth_m, remap_nearest(d["z_render"], rt))
    assert np.array_equal(r.instance, remap_nearest(d["inst_render"], rt))
    assert np.array_equal(r.qual, remap_nearest(d["qual_render"], rt))
    assert (r.instance > 0).any()


@pytest.mark.parametrize("dist", [(0.0, 0.0, 0.0, 0.0, 0.0), DIST])
def test_sky_class_matches_cpu_elevation_mask(cam, dist) -> None:
    """天空分类（质量位第 0 位）：`frame.void_below_horizon` 与 CPU `(depth ≤ 0) & (ray_elevation < 0)` 逐位相同（有畸变时在渲染域
    计算后最近邻映射）；水平视线下有地平线以上的天空、地面以外的无几何下视两类像素。"""
    from awr.sensorsvc.frame import void_below_horizon

    with cam.ctx:
        cm = CameraModel.from_hfov(480, 320, 90.0, dist=dist)
        R = gimbal_R(200.0, -2.0)
        p = np.array([0.0, 0.0, 6.0])
        r = cam.render(cm, R, p, debug=True)
    void = void_below_horizon(r, cm, R)
    if r.remap is None:
        want = (r.depth_m <= 0) & (ray_elevation(cm, R, cm.width, cm.height) < 0)
    else:
        rc = r.remap.render
        want = remap_nearest((r.debug["z_render"] <= 0) & (ray_elevation(rc, R, rc.width, rc.height) < 0), r.remap)
    assert np.array_equal(void, want)
    assert void.any() and ((r.depth_m <= 0) & ~void).any()


def test_submit_pipeline_matches_render(cam) -> None:
    """连续提交不同位姿的多帧、按序取结果，与逐帧同步 `render` 逐字节相同（读回槽位轮转、栅栏与视图不串帧）；
    视图只读，`release` 后槽位可复用。"""
    with cam.ctx:
        cm = CameraModel.from_hfov(320, 200, 60.0, dist=DIST, shadow="map")
        af = _actors()
        poses = [(look_at_R(q, np.zeros(3)), q) for q in (np.array([-25.0, -18.0, 22.0]), np.array([20.0, -30.0, 15.0]),
                                                         np.array([-10.0, 25.0, 30.0]))]
        wthr = fx.Weather(visibility_m=2000.0, rain_mmh=8.0)
        fps = [_params(cm, weather=wthr, frame=k) for k in range(3)]
        refs = [cam.render(cm, R, p, fx=f, actors=af) for (R, p), f in zip(poses, fps, strict=True)]
        for _ in range(3):
            jobs = [cam.submit(cm, R, p, fx=f, actors=af, timer=True) for (R, p), f in zip(poses, fps, strict=True)]
            for j, ref in zip(jobs, refs, strict=True):
                o = j.result()
                assert not o.rgb8.flags.writeable and o.gpu_ms is not None and o.gpu_ms > 0
                for k in ("rgb8", "depth_m", "instance", "semantic", "instance_unocc", "qual"):
                    assert np.array_equal(getattr(o, k), getattr(ref, k)), k
                j.release()
                j.release()                                                       # 幂等
    assert not np.array_equal(refs[0].rgb8, refs[1].rgb8)


@pytest.mark.parametrize("dist", [(0.0, 0.0, 0.0, 0.0, 0.0), DIST])
def test_gpu_visibility_stats_match_cpu(cam, dist) -> None:
    """GPU 可见度统计（原子归约，交付域）与 CPU `visibility_stats` 逐值相同（有、无畸变，全局快门与卷帘），由此构建的 `objects[]`
    与 CPU 归约相同；无演员时不读回统计。"""
    from awr.sensorsvc.camera.actors import objects_from_stats, visibility_objects, visibility_stats
    from awr.sensorsvc.camera.raster import RS_NODES

    with cam.ctx:
        cm = CameraModel.from_hfov(400, 260, 75.0, dist=dist, readout_s=0.02)
        R, p = _view()
        af = _actors()
        rs = [(R, p + np.array([0.05 * k, 0.0, 0.0])) for k in range(RS_NODES)]
        outs = [cam.render(cm, R, p, actors=af), cam.render(cm, R, p, actors=af, rs_nodes=rs)]
        none = cam.render(cm, R, p)
    for r in outs:
        assert r.vis is not None and r.vis.shape == (len(ACTORS) + 1, 266)
        cpu = visibility_stats(r.instance, r.instance_unocc, r.semantic, None, len(ACTORS) + 1)
        assert np.array_equal(r.vis, cpu)
        objs = objects_from_stats(r.vis, ACTORS, cm.width, cm.height)
        assert objs == visibility_objects(r.instance, r.instance_unocc, r.semantic, None, ACTORS) and len(objs) == 2
    assert none.vis is None
