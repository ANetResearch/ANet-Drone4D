"""热成像辐射核心与探测器噪声（D3-AC-61 辐射换算、构造场景 (a)(b)(c)、NETD 与固定图案噪声；M25 §15.6；AWR-20 §15.16.2）。

阈值逐条对照 AWR-05 §11 D3-AC-61：Planck 正反表往返 ≤ 0.01 K（200–400 K）；(a) 300 K、ε = 1 平板无噪声 ≤ 0.05 K；
(b) ε = 0.9 水平面晴空（T_sky = T_air − 25 K）与解析解差 ≤ 0.1 K；(c) 雾中 1 km 目标表观温差按 τ(d) 衰减，与解析解差 ≤ 5%；
均匀 300 K 场景逐像素时间标准差在标称 NETD 的 ±10% 内；固定图案噪声运行内恒定、换种子改变。
解析解一律以级数形式的波段 Planck 积分（float64）与牛顿反解求得，与查找表实现相互独立。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sensorsvc.thermal import noise as tn
from awr.sensorsvc.thermal import radiometry as rad

H, W = 48, 64
KW = {"seed": 20261018, "uav_no": 1, "sensor_no": 0}
T_SUB = (-0.030, -0.020, -0.010, 0.0)                                  # 4 个子帧时刻（s），t_end = 0


@pytest.fixture(scope="module")
def table() -> rad.PlanckTable:
    return rad.planck_table()


def _analytic_t_app(t_s: float, eps: float, n_z: float, d_m: float, env: rad.ThermalEnv) -> float:
    """构造场景解析解：同一辐射方程，B 取级数解析值，B⁻¹ 取牛顿反解。"""
    b = rad.band_radiance_exact
    w = min(1.0, max(0.0, n_z))
    l_refl = w * b(env.sky_k) + (1.0 - w) * b(env.t_air_k)
    l_obj = eps * b(t_s) + (1.0 - eps) * l_refl
    tau = math.exp(-env.beta_per_km * d_m * 1e-3)
    return float(rad.band_temperature_exact(tau * l_obj + (1.0 - tau) * b(env.t_air_k)))


def _render_clean(table: rad.PlanckTable, t_s, eps, n_z, d_m, env: rad.ThermalEnv) -> tuple[np.ndarray, np.ndarray]:
    """无噪档完整链路：4 个相同子帧 → 加权 → 模糊 → B⁻¹ → u16；返回 (表观温度, u16 解码温度)。"""
    l1 = rad.sensor_radiance(table, t_s, eps, n_z, d_m, env)
    t_app, ck = tn.detector(np.stack([l1] * 4), T_SUB, 0.0, table, tn.profile("clean"), frame_index=0, **KW)
    return t_app, tn.decode_ck(ck)


# ---------------------------------------------------------------- 波段 Planck 积分与查找表

def test_band_radiance_series_matches_quadrature_and_derivative() -> None:
    assert math.isclose(rad._A, 1.438776877e-2, rel_tol=1e-9)                     # 第二辐射常数 c₂（CODATA 2018）
    x, w = np.polynomial.legendre.leggauss(256)
    lam = 8e-6 + (x + 1.0) * 3e-6                                                   # 8–14 µm
    for t in (200.0, 250.0, 300.0, 350.0, 400.0):
        b_lam = 2 * rad._H * rad._C ** 2 / lam ** 5 / np.expm1(rad._H * rad._C / (lam * rad._K * t))
        quad = float((b_lam * w).sum() * 3e-6)
        assert float(rad.band_radiance_exact(t)) == pytest.approx(quad, rel=1e-12)
        num = (rad.band_radiance_exact(t + 1e-3) - rad.band_radiance_exact(t - 1e-3)) / 2e-3
        assert float(rad.band_dbdt_exact(t)) == pytest.approx(float(num), rel=1e-7)
    assert float(rad.band_radiance_exact(300.0)) == pytest.approx(54.93, abs=0.01)  # 8–14 µm、300 K 约 54.9 W·m⁻²·sr⁻¹


def test_planck_table_round_trip_within_10_mk(table: rad.PlanckTable) -> None:
    """D3-AC-61：正反变换往返误差 ≤ 0.01 K（200–400 K），并与解析正反变换一致。"""
    assert table.radiance_lut.dtype == np.float32 and table.radiance_lut.size == 20001
    assert np.all(np.diff(table.radiance_lut.astype(np.float64)) > 0)              # 严格单调
    t = np.concatenate([np.random.default_rng(61).uniform(200.0, 400.0, 1_000_000),
                        200.0 + np.arange(20001) * 0.01, [200.0, 400.0]])
    rt = table.temperature(table.radiance(t)).astype(np.float64)
    assert float(np.abs(rt - t).max()) <= 0.01
    assert float(np.abs(rt - t).max()) < 1e-3                                       # 实测约 2e−5 K（float32 舍入）
    exact_inv = table.temperature(rad.band_radiance_exact(t[::10])).astype(np.float64)   # 解析正变换 + 表反变换
    assert float(np.abs(exact_inv - t[::10]).max()) <= 0.01
    sub = t[:2000]
    tab_fwd = rad.band_temperature_exact(table.radiance(sub).astype(np.float64))    # 表正变换 + 解析反变换
    assert float(np.abs(tab_fwd - sub).max()) <= 0.01
    assert float(table.temperature(table.radiance(150.0))) == 200.0                # 越界饱和
    assert float(table.temperature(1e6)) == 400.0 and float(table.temperature(0.0)) == 200.0


def test_planck_table_is_reproducible_and_versioned(table: rad.PlanckTable) -> None:
    rad.planck_table.cache_clear()
    again = rad.planck_table()
    assert again.sha256 == table.sha256 and len(table.sha256) == 64
    assert again.radiance_lut.tobytes() == table.radiance_lut.tobytes()
    assert table.version == rad.PLANCK_TABLE_VERSION and table.band_um == (8.0, 14.0)
    assert rad.planck_table((8.0, 12.0)).sha256 != table.sha256                    # 波段进入摘要
    with pytest.raises(ValueError):
        rad.planck_table((14.0, 8.0))
    with pytest.raises(ValueError):
        table.radiance_lut[0] = 0.0                                                # 表只读


def test_dbdt_table_matches_exact(table: rad.PlanckTable) -> None:
    t = np.linspace(200.0, 400.0, 4001)
    assert np.allclose(table.dbdt(t), rad.band_dbdt_exact(t), rtol=2e-6)


# ---------------------------------------------------------------- 环境辐射与大气透过

def test_environment_coefficients_follow_spec() -> None:
    assert rad.sky_temperature_k(293.15, 0.0) == pytest.approx(268.15)             # 晴空 T_air − 25 K
    assert rad.sky_temperature_k(293.15, 1.0) == pytest.approx(288.15)             # 全云 T_air − 5 K
    assert rad.extinction_beta_per_km(0.5) == pytest.approx(0.05)
    assert rad.extinction_beta_per_km(0.5, 1.0) == pytest.approx(0.05 + 1.956)
    assert rad.extinction_beta_per_km(0.0, None, 4.0) == pytest.approx(0.02 + 0.25 * 4.0 ** 0.63)
    with pytest.raises(ValueError):
        rad.extinction_beta_per_km(0.5, 0.0)
    assert float(rad.transmittance(1000.0, 2.0)) == pytest.approx(math.exp(-2.0))
    assert np.allclose(rad.wet_emissivity([0.93, 0.98], 1.0), [0.95, 0.99])
    assert np.allclose(rad.wet_emissivity([0.93, 0.98], 0.2), [0.93, 0.98])
    env = rad.ThermalEnv(290.0, cloud=0.5, rh=0.4, fog_visibility_km=2.0, rain_mmh=1.0)
    assert env.sky_k == pytest.approx(290.0 - 25.0 * 0.6)
    assert env.beta_per_km == pytest.approx(0.02 + 0.024 + 0.978 + 0.25)


def test_range_from_depth_uses_ray_length() -> None:
    z = np.full((4, 6), 10.0)
    r = rad.range_from_depth(z, 3.0, 3.0, 3.0, 2.0)
    assert r[2, 3] == pytest.approx(10.0 * math.sqrt(1.0 + (0.5 / 3) ** 2 * 2))
    assert np.all(r >= 10.0) and r[0, 0] == r.max()


def test_sky_pixels_take_sky_radiance(table: rad.PlanckTable) -> None:
    env = rad.ThermalEnv(290.0, cloud=0.0, rh=0.5, fog_visibility_km=1.0)
    d = np.array([[np.inf, 0.0, np.nan, 50.0]])
    lv = rad.sensor_radiance(table, 300.0, 1.0, 1.0, d, env)
    t = table.temperature(lv)
    assert np.allclose(t[0, :3], 265.0, atol=1e-3) and abs(float(t[0, 3]) - 265.0) > 10.0


# ---------------------------------------------------------------- 构造场景 (a)(b)(c)

@pytest.mark.parametrize(("t_air", "d_m"), [(300.0, 200.0), (290.0, 0.5), (280.0, 30.0)])
def test_case_a_uniform_300k_blackbody_plate(table: rad.PlanckTable, t_air: float, d_m: float) -> None:
    """(a) 均匀 300 K、ε = 1 的平板，无噪声输出表观温度误差 ≤ 0.05 K（气温 = 300 K 时期望 300 K；其余对解析解）。"""
    env = rad.ThermalEnv(t_air, cloud=0.3, rh=0.6)
    n_z = np.random.default_rng(1).uniform(-1.0, 1.0, (H, W))                     # ε = 1：法向不影响结果
    t_app, dec = (a.astype(np.float64) for a in _render_clean(table, np.full((H, W), 300.0), 1.0, n_z, np.full((H, W), d_m), env))
    want = _analytic_t_app(300.0, 1.0, 0.0, d_m, env)
    if t_air == 300.0:
        assert want == pytest.approx(300.0, abs=1e-9)
    assert float(np.abs(t_app - want).max()) <= 0.05 and float(np.abs(dec - want).max()) <= 0.05
    assert float(np.abs(dec - want).max()) <= 0.006                                # 实际只有 0.005 K 量化误差


@pytest.mark.parametrize(("t_air", "t_s", "d_m"), [(293.15, 303.15, 0.0), (293.15, 308.0, 60.0), (273.15, 268.0, 120.0)])
def test_case_b_eps09_horizontal_surface_clear_sky(table: rad.PlanckTable, t_air: float, t_s: float, d_m: float) -> None:
    """(b) ε = 0.9 水平面（n_z = 1，w_sky = 1）在晴空（T_sky = T_air − 25 K）下，表观温度与解析解差 ≤ 0.1 K。"""
    env = rad.ThermalEnv(t_air, cloud=0.0, rh=0.5)
    assert env.sky_k == pytest.approx(t_air - 25.0)
    dd = np.full((H, W), d_m if d_m > 0 else 1e-6)
    t_app, dec = (a.astype(np.float64) for a in _render_clean(table, np.full((H, W), t_s), 0.9, np.ones((H, W)), dd, env))
    want = _analytic_t_app(t_s, 0.9, 1.0, float(dd[0, 0]), env)
    assert float(np.abs(t_app - want).max()) <= 0.1 and float(np.abs(dec - want).max()) <= 0.1
    assert float(np.abs(dec - want).max()) <= 0.006
    assert t_s - want > 1.0                                                         # 冷天空反射使表观温度明显低于表面温度


def test_reflection_weight_follows_normal(table: rad.PlanckTable) -> None:
    """竖直立面（n_z = 0）反射气温、朝下面（n_z < 0）同；水平面反射天空。"""
    env = rad.ThermalEnv(293.15, cloud=0.0, rh=0.5)
    nz = np.array([[1.0, 0.5, 0.0, -0.7]])
    t = table.temperature(rad.sensor_radiance(table, 303.15, 0.9, nz, np.full((1, 4), 1e-6), env))
    for j, n in enumerate((1.0, 0.5, 0.0, 0.0)):
        assert float(t[0, j]) == pytest.approx(_analytic_t_app(303.15, 0.9, n, 1e-6, env), abs=1e-3)
    assert t[0, 0] < t[0, 1] < t[0, 2] == pytest.approx(float(t[0, 3]), abs=1e-6)


@pytest.mark.parametrize("vis_km", [0.5, 1.0, 2.0])
def test_case_c_fog_contrast_attenuates_with_tau(table: rad.PlanckTable, vis_km: float) -> None:
    """(c) 雾中 1 km 外目标：表观温差按 τ(d) 衰减，与解析解差 ≤ 5%（辐亮度差严格为 τ 倍）。"""
    env = rad.ThermalEnv(288.15, cloud=0.8, rh=0.95, fog_visibility_km=vis_km)
    tau = math.exp(-env.beta_per_km)
    t_s = np.full((H, W), 288.15)
    t_s[:, W // 2:] = 318.15                                                         # 右半：+30 K 热目标（车辆发动机量级）
    eps = np.where(t_s > 300.0, 0.95, 0.93)
    d = np.full((H, W), 1000.0)
    lv = rad.sensor_radiance(table, t_s, eps, 1.0, d, env)
    l0 = rad.sensor_radiance(table, t_s, eps, 1.0, np.full((H, W), 1e-6), env)
    dl, dl0 = float(lv[0, -1] - lv[0, 0]), float(l0[0, -1] - l0[0, 0])
    assert dl == pytest.approx(tau * dl0, rel=1e-4)
    want = _analytic_t_app(318.15, 0.95, 1.0, 1000.0, env) - _analytic_t_app(288.15, 0.93, 1.0, 1000.0, env)
    t_app, dec = (a.astype(np.float64) for a in _render_clean(table, t_s, eps, 1.0, d, env))
    r, cl, ch = H // 2, W // 4, 3 * W // 4                                           # 远离模糊边缘
    for img, tol in ((t_app, 0.05), (dec, 0.05)):
        got = float(img[r, ch] - img[r, cl])
        assert abs(got - want) <= tol * abs(want), (vis_km, got, want)
    assert abs(float(t_app[r, ch] - t_app[r, cl]) - want) <= 1e-3 * abs(want)       # 无量化时实测 < 0.1%
    assert 0 < want < 30.0 * tau * 1.5                                               # 温差量级随 τ 衰减


def test_fog_contrast_decreases_with_distance(table: rad.PlanckTable) -> None:
    env = rad.ThermalEnv(288.15, rh=0.9, fog_visibility_km=1.0)
    ds = np.array([[100.0, 300.0, 600.0, 1000.0, 2000.0]])
    hot = table.temperature(rad.sensor_radiance(table, 318.15, 0.95, 1.0, ds, env))[0]
    bg = table.temperature(rad.sensor_radiance(table, 288.15, 0.93, 1.0, ds, env))[0]
    assert np.all(np.diff(hot - bg) < 0)


# ---------------------------------------------------------------- 探测器：子帧、模糊、量化

def test_profiles_follow_noise_table() -> None:
    r, h, c = tn.PROFILES["realistic"], tn.PROFILES["harsh"], tn.PROFILES["clean"]
    assert (r.netd_mk_300k, r.fpn_offset_k, r.fpn_gain_frac, r.optics_sigma_px, r.tau_bolometer_s) == (50.0, 0.10, 0.005, 0.6, 0.010)
    assert (h.netd_mk_300k, h.fpn_offset_k, h.fpn_gain_frac, h.optics_sigma_px, h.tau_bolometer_s) == (80.0, 0.25, 0.010, 0.8, 0.012)
    assert (c.netd_mk_300k, c.fpn_offset_k, c.fpn_gain_frac) == (0.0, 0.0, 0.0)
    with pytest.raises(ValueError):
        tn.profile("extreme")


def test_channels_do_not_overlap_camera_registry() -> None:
    assert tn.check_channels() == []
    lo_t, hi_t = tn.THERM_CHANNELS["therm_temporal"]
    lo_f, hi_f = tn.THERM_CHANNELS["therm_fpn"]
    assert hi_t < lo_f or hi_f < lo_t
    with pytest.raises(ValueError):
        tn.temporal_normals(1, 0, 0x100, 0, 2, 2)


def test_subframe_weights_and_blend() -> None:
    w = tn.subframe_weights(T_SUB, 0.0, 0.010)
    assert w.sum() == pytest.approx(1.0) and np.allclose(w[1:] / w[:-1], math.e)
    assert np.array_equal(tn.subframe_weights(T_SUB, 0.0, 0.0), [0, 0, 0, 1.0])
    with pytest.raises(ValueError):
        tn.subframe_weights([0.01], 0.0, 0.01)
    stack = np.stack([np.full((2, 3), v, np.float32) for v in (1.0, 2.0, 3.0, 4.0)])
    assert np.allclose(tn.blend_subframes(stack, w), float((w * [1, 2, 3, 4]).sum()))
    with pytest.raises(ValueError):
        tn.blend_subframes(stack, w[:3])


def test_optics_blur_preserves_level_and_spreads_points() -> None:
    flat = np.full((16, 20), 54.93, np.float32)
    assert np.allclose(tn.optics_blur(flat, 0.6), flat, rtol=1e-6)
    pt = np.zeros((15, 15), np.float32)
    pt[7, 7] = 1.0
    b = tn.optics_blur(pt, 0.6)
    assert float(b.sum()) == pytest.approx(1.0, abs=1e-6) and b[7, 7] < 1.0
    assert b[7, 9] > 0 and b[7, 10] == 0                                            # 半径 ceil(3sigma) = 2
    assert float(b[7, 8] / b[7, 7]) == pytest.approx(math.exp(-0.5 / 0.36), rel=1e-5)
    assert np.array_equal(tn.optics_blur(pt, 0.0), pt)


def test_quantize_ck_rounding_and_saturation() -> None:
    ck = tn.quantize_ck(np.array([300.0, 300.004, 300.005, 300.0149, -1.0, 700.0]))
    assert ck.dtype == np.uint16 and ck.tolist() == [30000, 30000, 30001, 30001, 0, 65535]
    assert tn.decode_ck(np.array([30000, 29315], np.uint16)).tolist() == pytest.approx([300.0, 293.15])


# ---------------------------------------------------------------- NETD 时间噪声

def test_netd_sigma_scales_with_dbdt(table: rad.PlanckTable) -> None:
    s = tn.netd_sigma_k(table, np.array([250.0, 300.0, 350.0]), 50.0)
    assert float(s[1]) == pytest.approx(0.050, rel=1e-6)
    ref = rad.band_dbdt_exact(300.0)
    assert s[0] == pytest.approx(0.05 * ref / rad.band_dbdt_exact(250.0), rel=1e-5) and s[0] > 0.05 > s[2]


@pytest.mark.parametrize(("name", "t_k"), [("realistic", 300.0), ("harsh", 300.0), ("realistic", 260.0), ("realistic", 330.0)])
def test_per_pixel_temporal_std_within_10pct_of_netd(table: rad.PlanckTable, name: str, t_k: float) -> None:
    """D3-AC-61 噪声：均匀场景逐像素时间标准差（含 u16 量化）在标称 NETD（按 dB/dT 换算到场景温度）的 ±10% 内。"""
    prof = tn.profile(name)
    h, w, n = 24, 24, 2000                                                          # 每像素 2000 帧：std 抽样误差约 1.6%
    t0 = np.full((h, w), t_k, np.float32)
    fpn = tn.fpn_maps(KW["seed"], KW["uav_no"], KW["sensor_no"], h, w, prof)
    frames = np.stack([tn.decode_ck(tn.quantize_ck(tn.apply_noise(t0, table, prof, frame_index=k, fpn=fpn, **KW)))
                       for k in range(n)]).astype(np.float64)
    std = frames.std(axis=0, ddof=1)
    nominal = float(tn.netd_sigma_k(table, t_k, prof.netd_mk_300k))
    if t_k == 300.0:
        assert nominal == pytest.approx(prof.netd_mk_300k * 1e-3, rel=1e-6)
    assert abs(float(std.mean()) / nominal - 1.0) <= 0.02                          # 像素平均：实测 < 1%
    assert np.all(np.abs(std / nominal - 1.0) <= 0.10)                              # 每个像素都在 ±10% 内
    dev = frames - frames.mean(axis=0)                                              # 去掉逐像素常量（FPN）
    lag1 = np.corrcoef(dev[:-1].ravel(), dev[1:].ravel())[0, 1]
    assert abs(lag1) < 0.02                                                         # 帧间独立


def test_full_frame_temporal_noise_matches_netd(table: rad.PlanckTable) -> None:
    """640 × 512 全幅：两帧差的空间标准差 / √2 = NETD（FPN 在差分中抵消）。"""
    prof = tn.profile("realistic")
    t0 = np.full((512, 640), 300.0, np.float32)
    a = tn.apply_noise(t0, table, prof, frame_index=10, **KW).astype(np.float64)
    b = tn.apply_noise(t0, table, prof, frame_index=11, **KW).astype(np.float64)
    assert float((b - a).std() / math.sqrt(2.0)) == pytest.approx(0.050, rel=0.02)


# ---------------------------------------------------------------- 固定图案噪声与计数器式随机数

def test_fpn_is_constant_within_run_and_changes_with_seed(table: rad.PlanckTable) -> None:
    prof = tn.profile("realistic")
    h, w = 512, 640
    off, gain = tn.fpn_maps(KW["seed"], KW["uav_no"], KW["sensor_no"], h, w, prof)
    assert off.dtype == np.float32 and float(off.std()) == pytest.approx(0.10, rel=0.02)
    assert float(gain.std()) == pytest.approx(0.005, rel=0.02) and abs(float(off.mean())) < 0.002
    again = tn.fpn_maps(KW["seed"], KW["uav_no"], KW["sensor_no"], h, w, prof)
    assert again[0].tobytes() == off.tobytes() and again[1].tobytes() == gain.tobytes()
    other = tn.fpn_maps(KW["seed"] + 1, KW["uav_no"], KW["sensor_no"], h, w, prof)
    assert abs(float(np.corrcoef(off.ravel(), other[0].ravel())[0, 1])) < 0.01      # 换种子：图案改变且不相关
    assert abs(float(np.corrcoef(gain.ravel(), other[1].ravel())[0, 1])) < 0.01
    for kw in ({**KW, "uav_no": 2}, {**KW, "sensor_no": 1}):
        assert not np.array_equal(tn.fpn_maps(kw["seed"], kw["uav_no"], kw["sensor_no"], 8, 8, prof)[0], off[:8, :8])

    # 运行内恒定：无时间噪声时各帧逐字节相同；有时间噪声时帧均值收敛到 FPN 图案
    p_fpn_only = tn.ThermalNoiseProfile("fpn", 0.0, prof.fpn_offset_k, prof.fpn_gain_frac, 0.6, 0.010)
    t0 = np.full((32, 32), 320.0, np.float32)
    f3 = tn.apply_noise(t0, table, p_fpn_only, frame_index=3, **KW)
    assert f3.tobytes() == tn.apply_noise(t0, table, p_fpn_only, frame_index=900, **KW).tobytes()
    o32, g32 = tn.fpn_maps(KW["seed"], KW["uav_no"], KW["sensor_no"], 32, 32, prof)
    assert np.allclose(f3 - t0, o32 + g32 * 20.0, atol=1e-4)                        # 增益残差作用于相对 300 K 的温差
    assert float((f3 - t0).std()) == pytest.approx(math.hypot(0.10, 0.005 * 20.0), rel=0.15)
    mean = np.mean([tn.apply_noise(t0, table, prof, frame_index=k, **KW) for k in range(200)], axis=0)
    assert float(np.corrcoef((mean - t0).ravel(), (o32 + g32 * 20.0).ravel())[0, 1]) > 0.95
    seeded = tn.apply_noise(t0, table, p_fpn_only, frame_index=3, **{**KW, "seed": KW["seed"] + 7})
    assert not np.array_equal(seeded, f3)


def test_noise_is_counter_based_and_stateless(table: rad.PlanckTable) -> None:
    """任一帧可单独重算、与计算顺序无关；帧号、机号、传感器号与种子都进入键。"""
    prof = tn.profile("harsh")
    t0 = np.full((H, W), 300.0, np.float32)
    seq = [tn.apply_noise(t0, table, prof, frame_index=k, **KW).tobytes() for k in range(5)]
    assert tn.apply_noise(t0, table, prof, frame_index=3, **KW).tobytes() == seq[3]
    assert len(set(seq)) == 5
    n0 = tn.temporal_normals(KW["seed"], 1, 0, 3, H, W)
    for args in ((KW["seed"], 2, 0, 3), (KW["seed"], 1, 1, 3), (KW["seed"] + 1, 1, 0, 3)):
        assert not np.array_equal(tn.temporal_normals(*args, H, W), n0)
    flat = tn.temporal_normals(KW["seed"], 1, 0, 3, 1, H * W)
    assert np.array_equal(flat.reshape(H, W), n0)                                    # 像素计数器为行主序下标
    fpn_n = tn.fpn_maps(KW["seed"], 1, 0, 8, 8, prof)[0] / np.float32(prof.fpn_offset_k)
    assert not np.allclose(fpn_n, tn.temporal_normals(KW["seed"], 1, 0, 0, 8, 8))    # FPN 与第 0 帧时间噪声通道不同、互不重复


def test_detector_chain_is_deterministic_and_clean_is_noise_free(table: rad.PlanckTable) -> None:
    env = rad.ThermalEnv(293.15, cloud=0.2, rh=0.5)
    rng = np.random.default_rng(5)
    t_s = rng.uniform(285.0, 320.0, (H, W))
    lv = rad.sensor_radiance(table, t_s, 0.95, rng.uniform(0, 1, (H, W)), np.full((H, W), 80.0), env)
    stack = np.stack([lv] * 4)
    a = tn.detector(stack, T_SUB, 0.0, table, tn.profile("realistic"), frame_index=4, **KW)
    b = tn.detector(stack, T_SUB, 0.0, table, tn.profile("realistic"), frame_index=4, **KW)
    assert a[1].dtype == np.uint16 and a[1].tobytes() == b[1].tobytes() and a[0].tobytes() == b[0].tobytes()
    clean, ck = tn.detector(lv, [0.0], 0.0, table, tn.profile("clean"), frame_index=4, **KW)
    assert np.array_equal(clean, table.temperature(tn.optics_blur(lv, 0.6)))
    assert float(np.abs(tn.decode_ck(ck) - clean).max()) <= 0.005 + 1e-5
