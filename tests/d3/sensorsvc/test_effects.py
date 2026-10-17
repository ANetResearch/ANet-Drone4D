"""相机效果链参考实现（M25 §6.5、§6.17.2；AWR-20 §15.6.5）：确定性、ISP 往返、噪声统计、自动曝光、L2-depth 与云台读数。"""

from __future__ import annotations

import itertools
from dataclasses import replace

import numpy as np
import pytest

from awr.sensorsvc.camera import effects as fx
from awr.sensorsvc.camera.model import CameraModel
from awr.sensorsvc.camera.raster import srgb8_to_linear


def _cm(profile: str = "realistic", **isp) -> CameraModel:
    cm = fx.with_profile(CameraModel.from_hfov(96, 64, 84.0), fx.profile(profile))
    return replace(cm, isp=replace(cm.isp, **isp)) if isp else cm


KW = {"seed": 7, "uav_no": 1, "sensor_no": 0, "frame_index": 3}


def test_channels_do_not_overlap_and_profiles_follow_the_table() -> None:
    for (a, (alo, ahi)), (b, (blo, bhi)) in itertools.combinations(fx.CHANNELS_V2.items(), 2):
        assert ahi < blo or bhi < alo, (a, b)
    r, h = fx.PROFILES["realistic"], fx.PROFILES["harsh"]
    assert (r.noise.read_sigma_dn, r.noise.shot_k_dn, r.isp.ae_error_amp, r.depth.d_max_m, r.gimbal_sigma_deg) == (2.0, 0.2, 0.2, 60.0, 0.05)
    assert (h.noise.read_sigma_dn, h.noise.shot_k_dn, h.isp.ae_error_amp, h.depth.a, h.depth.d_max_m) == (4.0, 0.4, 0.4, 2.0, 40.0)
    with pytest.raises(ValueError):
        fx.profile("extreme")


def test_pixel_normals_are_keyed_and_standard() -> None:
    a = fx.pixel_normals(7, 1, 3, 0x1000, 200_000, 3)
    assert a.dtype == np.float32 and np.array_equal(a, fx.pixel_normals(7, 1, 3, 0x1000, 200_000, 3))
    assert abs(float(a.mean())) < 0.01 and abs(float(a.std()) - 1.0) < 0.01
    assert not np.array_equal(a[:10], fx.pixel_normals(7, 1, 4, 0x1000, 10, 3))          # 帧序号进入键
    assert not np.array_equal(a[:10], fx.pixel_normals(7, 2, 3, 0x1000, 10, 3))          # 机号进入键
    assert np.array_equal(a[:10], fx.pixel_normals(7, 1, 3, 0x1000, 10, 3))              # 前缀与总像素数无关


def test_clean_isp_round_trip_is_identity() -> None:
    """clean 档、无渐晕、增益 1：sRGB8 → 线性 → 效果链 → sRGB8 逐值相同（M25 §6.5.5 恒等性）。"""
    img = np.random.default_rng(0).integers(0, 256, (64, 96, 3), dtype=np.uint8)
    cm = _cm("clean", vignette_k=0.0)
    out = fx.apply_rgb(srgb8_to_linear(img).astype(np.float32), cm, fx.AeState(), t_mid_s=0.0, **KW)
    assert np.abs(out.astype(int) - img.astype(int)).max() == 0


def test_noise_sigma_matches_poisson_gaussian_model() -> None:
    cm = _cm("realistic", vignette_k=0.0, ae="fixed", ae_error_amp=0.0)
    lin = np.full((64, 96, 3), 0.2, np.float32)
    out = fx.apply_rgb(lin, cm, fx.AeState(), t_mid_s=0.0, **KW)
    dn = srgb8_to_linear(out) * 255.0
    want = np.sqrt(0.2 * 51.0 + 2.0 ** 2)                                                # x = 51 DN：sigma ≈ 3.77 DN
    assert abs(float(dn.mean()) - 51.0) < 0.3 and float(dn.std()) == pytest.approx(want, rel=0.12)
    again = fx.apply_rgb(lin, cm, fx.AeState(), t_mid_s=0.0, **KW)
    assert np.array_equal(out, again)


def test_vignette_darkens_corners() -> None:
    cm = _cm("clean")
    out = fx.apply_rgb(np.full((64, 96, 3), 0.5, np.float32), cm, fx.AeState(), t_mid_s=0.0, **KW)
    assert out[32, 48, 0] > out[0, 0, 0] and out[32, 48, 0] >= 187                     # 中心约等于 sRGB(0.5)


def test_auto_exposure_converges_and_error_is_bounded() -> None:
    cm = _cm("realistic", vignette_k=0.0)
    cm = replace(cm, noise=replace(cm.noise, read_sigma_dn=0.0, shot_k_dn=0.0))
    st = fx.AeState()
    lin = np.full((64, 96, 3), 0.09, np.float32)                                          # 中灰目标 0.18 的一半：增益趋于 2
    for k in range(40):
        fx.apply_rgb(lin, cm, st, t_mid_s=k * 0.1, **{**KW, "frame_index": k + 1})
    assert st.gain_prev == pytest.approx(2.0, rel=0.05)
    errs = np.asarray(st.history[-20:]) / st.gain_prev
    assert errs.min() >= 0.8 - 1e-6 and errs.max() <= 1.2 + 1e-6 and errs.max() - errs.min() > 0.01


def test_depth_sensor_range_and_noise() -> None:
    d = np.array([[0.1, 0.3, 10.0, 59.0, 61.0, 0.0]], np.float32)
    clean = fx.depth_sensor(d, fx.PROFILES["clean"].depth, **KW)
    assert clean.tolist() == [[0.0, pytest.approx(0.3), 10.0, 59.0, 61.0, 0.0]]
    real = fx.depth_sensor(np.full((200, 200), 10.0, np.float32), fx.PROFILES["realistic"].depth, **KW)
    assert float(real.std()) == pytest.approx(0.005 * 10 + 0.0005 * 100, rel=0.05) and abs(float(real.mean()) - 10.0) < 0.002
    r = fx.depth_sensor(d, fx.PROFILES["realistic"].depth, **KW)
    assert r[0, 0] == 0 and r[0, 4] == 0 and r[0, 5] == 0 and r[0, 2] > 0                 # 越界与无几何为 0
    assert (fx.depth_sensor(d, fx.PROFILES["harsh"].depth, **KW)[0, 3]) == 0              # harsh 量程 40 m


def test_gimbal_reading_noise() -> None:
    g = (10.0, -25.0, 0.0)
    assert fx.gimbal_reading(g, 0.0, **KW) == g
    r = fx.gimbal_reading(g, 0.05, **KW)
    assert r != g and max(abs(a - b) for a, b in zip(r, g, strict=True)) < 0.3
    assert r == fx.gimbal_reading(g, 0.05, **KW)
