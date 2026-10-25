"""热成像渲染（`therm512`）与参数表 `thermal_table_v1`（D3-AC-61 动态实体条款与 GPU 管线；D3-AC-11 热成像全输出；M25 §15.6）。

阈值逐条对照 docs/05 §11.2 D3-AC-61：动态实体（人员；车辆发动机运转与熄火 600 s 后）无噪声档表观温度符合 `thermal_table_v1`
与 DSL 参数，误差 ≤ 0.2 K；构造场景 (a) 均匀 300 K、ε = 1 平板 ≤ 0.05 K，(b) ε = 0.9 水平面晴空（T_sky = T_air − 25 K）≤ 0.1 K，
(c) 雾中 1 km 目标表观温差按 τ(d) 衰减 ≤ 5%（GPU 管线复核，CPU 参考见 test_thermal_core.py）；同镜像同卡两次逐字节一致；
GPU 着色器与 CPU 参考（`radiometry.sensor_radiance`、`noise.detector`）的一致性：子帧辐亮度相对差 ≤ 1e-5，`temp_ck` 差 ≤ 1 LSB
且 ≥ 99% 像素逐值相同；`thermal_bake` 两次逐字节一致、Warp 与 numpy 参照一致；u16 + zstd 交付字节即存档字节。

期望值一律由参数表经独立的解析式求得（`radiometry.band_radiance_exact` / `band_temperature_exact`，float64），不读取渲染中间量。
GPU 用例需要 moderngl 与 EGL（d3-sensorsvc 镜像，dmax 单卡绑定），`thermal_bake` 的 Warp 用例需要 warp-lang；本机只运行 CPU 用例。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sensorsvc"))
from appfix import BOX, scene_enu, write_world

from awr.sensorsvc.thermal import noise as tn
from awr.sensorsvc.thermal import radiometry as rad
from awr.sensorsvc.thermal import render as tr

T_AIR = 288.15
NIGHT = (135.0, -10.0)                                  # 太阳在地平线下：无日照项
DAY = (90.0, 30.0)                                      # 太阳在正东、高度 30°
DYN_TOL_K = 0.2                                         # D3-AC-61 动态实体
PLATE_TOL_K, CLEAR_TOL_K, FOG_TOL = 0.05, 0.1, 0.05
PERSON = {"id": "p1", "class": "person", "size_mm": [500, 450, 1750]}
VEHICLE = {"id": "v1", "class": "vehicle", "size_mm": [4500, 1800, 1500]}
GROUND_CLS, BOX_CLS = 1, 5                               # d3-semantic@1：ground、building_roof


def _table() -> tr.ThermalTable:
    return tr.load_thermal_table()


def _exact_app(t_s, eps, n_z, d_m, env: rad.ThermalEnv) -> float:
    """解析表观温度（float64）：`B⁻¹(τ·(ε·B(T_s) + (1 − ε)·L_refl) + (1 − τ)·B(T_air))`。"""
    B = rad.band_radiance_exact
    w = min(1.0, max(0.0, n_z))
    l_refl = w * B(env.sky_k) + (1.0 - w) * B(env.t_air_k)
    tau = math.exp(-env.beta_per_km * d_m * 1e-3)
    return float(rad.band_temperature_exact(tau * (eps * B(t_s) + (1.0 - eps) * l_refl) + (1.0 - tau) * B(env.t_air_k)))


# ---------------------------------------------------------------- 参数表（CPU）

def test_table_loads_versioned_and_digest_ignores_formatting(tmp_path) -> None:
    import yaml

    t = _table()
    assert t.version == "thermal_table_v1" and len(t.sha256) == 64
    doc = yaml.safe_load(tr.DEFAULT_TABLE.read_text(encoding="utf-8"))
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(doc, sort_keys=False, default_flow_style=False), encoding="utf-8")       # 换排版、去注释
    assert tr.load_thermal_table(p).sha256 == t.sha256
    doc2 = copy.deepcopy(doc)
    doc2["static"]["road"]["eps"] = 0.94
    assert tr.thermal_table_from_doc(doc2).sha256 != t.sha256                                        # 改值即改摘要
    for bad in ({"static": {**doc["static"], "x": {"codes": [9]}}}, {"actors": {**doc["actors"], "dog": {"model": "blend", "k": 0.6}}},
                {"sky": {**doc["sky"], "extra": 1.0}}, {"extra_top": 1}):
        with pytest.raises(ValueError):
            tr.thermal_table_from_doc({**copy.deepcopy(doc), **bad})


def test_table_rows_match_prd_and_cover_all_codes() -> None:
    """M25 §15.6 参数表：各类别的 ΔT 与发射率逐项相同；每个 d3-semantic@1 静态码都有行（未列出者取 default）。"""
    t = _table()
    want = {9: (1.0, 12.0, -1.0, 1.0, 0.95), 1: (0.5, 8.0, -2.0, 0.8, 0.93), 6: (0.5, 6.0, -1.0, 0.5, 0.92), 5: (1.0, 15.0, -4.0, 1.5, 0.90),
            3: (-1.5, 3.0, -2.0, 0.5, 0.98), 8: (-2.0, 0.0, 1.0, 0.0, 0.96), 0: (0.0, 4.0, -1.5, 0.5, 0.93), 14: (0.0, 4.0, -1.5, 0.5, 0.93)}
    for code, row in want.items():
        r = t.row_of(code)
        assert (r.dT_base, r.dT_sun, r.dT_night, r.dT_wet, r.eps) == row, code
    assert t.row_of(6).sun_incidence and not t.row_of(5).sun_incidence
    assert (t.code_row >= 0).all()


@pytest.mark.parametrize("cloud,rh,fog,rain", [(0.0, 0.5, None, 0.0), (0.4, 0.9, 1.0, 0.0), (1.0, 0.2, 0.5, 4.0), (0.7, 1.0, None, 12.0)])
def test_table_sky_and_extinction_match_radiometry(cloud, rh, fog, rain) -> None:
    """参数表的天空与消光式与 `radiometry` 的暂定常数逐值相同（同一式的两份实现不得漂移）。"""
    t = _table()
    env = tr.thermal_env(t, T_AIR, cloud=cloud, rh=rh, fog_visibility_km=fog, rain_mmh=rain)
    assert env.sky_k == pytest.approx(rad.sky_temperature_k(T_AIR, cloud), abs=1e-12)
    assert t.beta_per_km(rh, fog, rain) == pytest.approx(rad.extinction_beta_per_km(rh, fog, rain), rel=1e-15)
    assert t.wet["rain_mmh"] == rad.WET_RAIN_MMH
    assert np.allclose(t.wet_eps([0.9, 0.98], rain), rad.wet_emissivity([0.9, 0.98], rain))


def test_static_surface_formula() -> None:
    t = _table()
    cls = np.array([9, 1, 6, 5, 3, 8, 0], np.uint8)
    vis = np.array([255, 255, 255, 0, 255, 255, 128], np.uint8)
    cos = np.full(7, 0.5)
    day = tr.thermal_env(t, T_AIR, cloud=0.3)
    dT, eps = tr.static_surface(t, cls, vis, cos, day, DAY)
    sh = math.sin(math.radians(30.0))
    want = [1.0 + 12 * sh, 0.5 + 8 * sh, 0.5 + 6 * sh * 0.5, 1.0, -1.5 + 3 * sh, -2.0, 4 * sh * 128 / 255]
    assert np.allclose(dT, want, atol=1e-6)
    assert np.allclose(eps, [0.95, 0.93, 0.92, 0.90, 0.98, 0.96, 0.93])
    night = tr.thermal_env(t, T_AIR, cloud=0.25, rain_mmh=2.0)
    dT, eps = tr.static_surface(t, cls, vis, cos, night, NIGHT)
    want = [1.0 - 0.75 - 1.0, 0.5 - 1.5 - 0.8, 0.5 - 0.75 - 0.5, 1.0 - 3.0 - 1.5, -1.5 - 1.5 - 0.5, -2.0 + 0.75, -1.125 - 0.5]
    assert np.allclose(dT, want, atol=1e-6)
    assert np.allclose(eps, [0.97, 0.95, 0.94, 0.92, 0.99, 0.98, 0.95])          # 降雨 +0.02，上限 0.99


# ---------------------------------------------------------------- 动态实体（CPU）

def test_person_and_dog_temperatures_and_dsl_override() -> None:
    t = _table()
    env = tr.thermal_env(t, T_AIR, cloud=0.5)
    p = tr.actor_thermal(PERSON, t, env, NIGHT)
    assert p.zone_t_k[0] == pytest.approx(T_AIR + 0.55 * (307.15 - T_AIR), abs=1e-12)
    assert p.zone_t_k[1] == pytest.approx(306.15, abs=1e-12) and p.zone_eps[:2] == (0.97, 0.98)
    d = tr.actor_thermal({"id": "d", "class": "dog", "size_mm": [900, 300, 600]}, t, env, NIGHT)
    assert d.zone_t_k[0] == pytest.approx(T_AIR + 0.6 * (311.15 - T_AIR), abs=1e-12)
    o = tr.actor_thermal({**PERSON, "thermal": {"surface_temp_c": 20.0, "emissivity": 0.95}}, t, env, NIGHT)
    assert o.zone_t_k[0] == pytest.approx(293.15) and o.zone_t_k[1] == pytest.approx(306.15) and set(o.zone_eps) == {0.95}
    m = tr.actor_thermal({"id": "m", "class": "machine", "size_mm": [2000, 2000, 2000], "thermal": {"profile": "vehicle"}}, t, env, NIGHT,
                         engine_off=None)
    assert m.zone_t_k[2] == pytest.approx(T_AIR + 45.0) and m.engine_x_m == pytest.approx(0.4)
    wet = tr.actor_thermal(PERSON, t, tr.thermal_env(t, T_AIR, rain_mmh=1.0), NIGHT)
    assert wet.zone_eps[:2] == pytest.approx((0.99, 0.99))
    with pytest.raises(ValueError):
        tr.actor_thermal({**PERSON, "thermal": {"temp": 1}}, t, env, NIGHT)


def test_vehicle_engine_running_and_600s_after_off() -> None:
    t = _table()
    env = tr.thermal_env(t, T_AIR, cloud=0.0)
    run = tr.actor_thermal(VEHICLE, t, env, DAY, engine_off=None)
    body = T_AIR + 2.0 + 8.0 * math.sin(math.radians(30.0))
    assert run.zone_t_k == pytest.approx((body, body, T_AIR + 45.0, T_AIR + 80.0))
    assert run.zone_eps == (0.90,) * 4
    assert (run.engine_x_m, run.exhaust_x_m, run.exhaust_z_m) == pytest.approx((0.9, -2.025, 0.525))
    off = tr.actor_thermal(VEHICLE, t, env, DAY, engine_off=600.0)
    e = math.exp(-1.0)
    assert off.zone_t_k[2] == pytest.approx(body + (T_AIR + 45.0 - body) * e, abs=1e-9)
    assert off.zone_t_k[3] == pytest.approx(body + (T_AIR + 80.0 - body) * e, abs=1e-9)
    cold = tr.actor_thermal(VEHICLE, t, env, DAY)
    assert cold.zone_t_k == pytest.approx((body,) * 4)
    # engine_on：行驶中或停车后 120 s 内；熄火时长 = 停车时长 − 120 s
    assert tr.engine_off_s(100.0, 100.0, t) is None and tr.engine_off_s(219.0, 100.0, t) is None
    assert tr.engine_off_s(820.0, 100.0, t) == pytest.approx(600.0) and tr.engine_off_s(5.0, None, t) == math.inf
    ts, v = [0.0, 1.0, 2.0, 3.0, 4.0], [3.0, 3.0, 0.1, 0.0, 0.0]
    assert tr.last_moving_time(ts, v, 1.5) == 1.5 and tr.last_moving_time(ts, v, 3.5) == 1.0
    assert tr.last_moving_time(ts, [0.0] * 5, 3.0) is None


def test_actor_zones_follow_proxy_layout() -> None:
    from awr.sensorsvc.camera.actors import proxy_mesh

    for a in (PERSON, VEHICLE, {"id": "d", "class": "dog", "size_mm": [900, 300, 600]}, {"id": "o", "class": "object", "size_mm": [500] * 3}):
        P, _N, C = proxy_mesh(a)
        z = tr.actor_zones(a)
        assert z.shape == (len(P),)
        if a["class"] == "person":                                       # 皮肤分区 = 颈与头（代理网格中肤色顶点）
            skin = np.all(np.abs(C - C[-1]) < 1e-6, axis=1)
            assert np.array_equal(z == 1, skin) and skin.sum() == 168
        if a["class"] == "vehicle":                                      # 车身盒体 = 车身颜色、z 在 [0.18h, 0.62h]
            assert (z == 2).sum() == 48 and np.allclose(P[z == 2][:, 2].min(), 0.18 * 1.5, atol=1e-6)
        if a["class"] == "object":
            assert (z == 2).all()


# ---------------------------------------------------------------- thermal_bake、编码与真值（CPU）

def _fixture_tris() -> np.ndarray:
    V, F, _UV = scene_enu()
    return V[F.astype(np.int64)].astype(np.float64)


def test_bake_numpy_shadow_geometry_and_deterministic_bytes(tmp_path) -> None:
    T = _fixture_tris()
    a = tr.thermal_bake(T, DAY, backend="numpy")
    b = tr.thermal_bake(T, DAY, backend="numpy")
    assert a.npz == b.npz and a.sha256 == b.sha256 == hashlib.sha256(a.npz).hexdigest()
    c = T.mean(1)
    (x0, y0, _), (x1, y1, z1) = BOX
    ground = np.abs(c[:, 2]) < 1e-9
    shadow_len = z1 / math.tan(math.radians(30.0))                       # 太阳在正东：盒影向西 52 m
    in_shadow = ground & (c[:, 0] > x0 - shadow_len + 0.5) & (c[:, 0] < x1) & (c[:, 1] > y0 + 0.5) & (c[:, 1] < y1 - 0.5)
    far = ground & ((c[:, 0] < x0 - shadow_len - 1.0) | (c[:, 0] > x1 + 1.0) | (np.abs(c[:, 1]) > y1 + 1.0))
    assert in_shadow.any() and (a.sun_vis[in_shadow] == 0).all() and (a.sun_vis[far] == 255).all()
    top = np.abs(c[:, 2] - z1) < 1e-9
    west = (np.abs(c[:, 0] - x0) < 1e-9) & ~ground & ~top
    east = (np.abs(c[:, 0] - x1) < 1e-9) & ~ground & ~top
    assert (a.sun_vis[top] == 255).all() and (a.sun_vis[east] == 255).all() and (a.sun_vis[west] == 0).all()
    r = tr.ThermalStatic.read(a.write(tmp_path / "thermal_static.npz"))
    assert np.array_equal(r.sun_vis, a.sun_vis) and r.sha256 == a.sha256 and r.sun_az_el_deg == DAY and r.backend == "numpy"
    assert (tr.thermal_bake(T, NIGHT, backend="numpy").sun_vis == 0).all()


def test_temp_ck_codec_roundtrip() -> None:
    rng = np.random.default_rng(3)
    a = (28800 + rng.integers(-300, 3000, (512, 640))).astype(np.uint16)
    b = tr.encode_temp_ck(a)
    assert b == tr.encode_temp_ck(a.copy()) and np.array_equal(tr.decode_temp_ck(b, 640, 512), a)
    assert len(b) < a.nbytes
    with pytest.raises(ValueError):
        tr.decode_temp_ck(b, 640, 511)


def test_objects_dT_app_ring() -> None:
    """dT_app_k = 实体可见像素中位数 − 3 px 环带中位数；环带不含任何演员像素；被静态几何遮挡的部分不计入可见像素。"""
    H, W = 64, 80
    t = np.full((H, W), 290.0, np.float32)
    t[:, 40:] = 291.0                                                    # 环带外的背景差异不影响结果
    inst = np.zeros((H, W), np.uint16)
    inst[20:30, 10:20] = 1
    t[20:30, 10:20] = 298.5
    inst[20:30, 20:23] = 2                                               # 紧邻的另一名演员（不入环带）
    t[20:30, 20:23] = 330.0
    unocc = inst.copy()
    unocc[30:33, 10:20] = 1                                              # 演员 1 的下沿被静态几何遮挡（该处为背景温度）
    sem = np.where(inst > 0, 32, 1).astype(np.uint8)
    actors = [{"id": "a", "class": "person"}, {"id": "b", "class": "person"}]
    objs = tr.thermal_objects(t, inst, unocc, sem, actors)
    o = {x["actor"]: x for x in objs}
    assert o["a"]["dT_app_k"] == pytest.approx(8.5, abs=1e-6) and o["a"]["visible_ratio"] == pytest.approx(100 / 130, abs=1e-6)
    assert o["b"]["dT_app_k"] == pytest.approx(40.0, abs=1e-6)


def test_thermal_frame_record_delivered_bytes_are_archived() -> None:
    pytest.importorskip("zstandard")
    from awr.sensorsvc.frame import FrameContext
    from awr.sensorsvc.poses import Mount

    m = tr.THERM512
    H, W = m.height, m.width
    ck = np.full((H, W), 28815, np.uint16)
    inst = np.zeros((H, W), np.uint16)
    inst[100:140, 200:220] = 1
    out = tr.ThermalOut(ck, ck.astype(np.float32) / 100, np.full((H, W), 50.0, np.float32), inst, np.ones((H, W), np.uint8), inst.copy(),
                        np.array([0.1, 0.2, 0.3, 0.4]), np.array([0.97, 0.98, 0.99, 1.0]), 1.0, 1.0, 1, 1)
    ctx = FrameContext("run1", "s1", "cv1", "A40", "580")
    t = _table()
    man = tr.thermal_manifest(t, rad.planck_table(), m)
    rec, blocks = tr.thermal_frame(ctx, uav="u1", sensor_id="therm0", seq=7, t_mid_ns=1_000_000_000, model=m, mount=Mount(), gimbal_reading=(0, 0, 0),
                                   gimbal_true=(0, 0, 0), pos_enu_m=np.zeros(3), R_ws=np.eye(3), out=out, manifest=man, actors=[PERSON])
    tc = rec["agent_view"]["temp_ck"]
    assert rec["kind"] == "thermal" and tc["codec"] == "radiometric_u16" and tc["sha256"] == hashlib.sha256(blocks[tc["ref"]]).hexdigest()
    assert np.array_equal(tr.decode_temp_ck(blocks[tc["ref"]], W, H), ck)
    assert rec["internal"]["thermal"]["thermal_table"] == {"version": "thermal_table_v1", "sha256": t.sha256}
    assert rec["internal"]["thermal"]["planck_table"]["version"] == rad.PLANCK_TABLE_VERSION
    assert rec["ground_truth"]["objects"][0]["actor"] == "p1" and "dT_app_k" in rec["ground_truth"]["objects"][0]
    assert rec["ground_truth"]["timing"]["t_truth_ns"] == [1_000_000_000]
    json.dumps(rec)
    with pytest.raises(ValueError):
        tr.thermal_frame(ctx, uav="u1", sensor_id="therm0", seq=7, t_mid_ns=0, model=tr.ThermalModel(codec="agc_jpeg"), mount=Mount(),
                         gimbal_reading=(0, 0, 0), gimbal_true=(0, 0, 0), pos_enu_m=np.zeros(3), R_ws=np.eye(3), out=out, manifest=man)


def test_model_profiles_and_subframes() -> None:
    m = tr.THERM512
    assert m.camera().hfov_deg == pytest.approx(32.9, abs=0.05) and math.degrees(2 * math.atan(256 / m.fy)) == pytest.approx(26.6, abs=0.05)
    p = m.noise_profile()
    assert (p.netd_mk_300k, p.fpn_offset_k, p.fpn_gain_frac, p.optics_sigma_px, p.tau_bolometer_s) == (50.0, 0.10, 0.005, 0.6, 0.010)
    assert tr.ThermalModel(noise="harsh", netd_mk_300k=60.0).noise_profile().netd_mk_300k == 60.0
    ts = tr.subframe_times_s(2.0, m)
    assert np.allclose(ts, [1.97, 1.98, 1.99, 2.0])
    w = tn.subframe_weights(ts, 2.0, p.tau_bolometer_s)
    assert np.allclose(w, np.exp([-3, -2, -1, 0]) / np.exp([-3, -2, -1, 0]).sum())


# ---------------------------------------------------------------- GPU（dmax）

def _with_cls(root: Path, cls: np.ndarray) -> Path:
    d = root / "visual/mesh/appearance"
    (d / "tri.cls.u8").write_bytes(np.asarray(cls, np.uint8).tobytes())
    doc = json.loads((d / "index.json").read_text(encoding="utf-8"))
    doc["streams"] = [{"name": "tri.cls", "href": "tri.cls.u8", "dtype": "uint8", "components": 1}]
    (d / "index.json").write_text(json.dumps(doc), encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def gl_world(tmp_path_factory):
    moderngl = pytest.importorskip("moderngl")
    pytest.importorskip("PIL")
    try:
        ctx = moderngl.create_standalone_context(backend="egl", require=450)
    except Exception as e:                                              # 无 EGL 设备
        pytest.skip(f"[gpu] EGL unavailable: {e}")
    from awr.sensorsvc.camera.raster import RasterCamera
    from awr.sensorsvc.camera.scene import load_appearance

    V, F, _UV = scene_enu()
    cen = V[F.astype(np.int64)].mean(1)
    cls = np.where(cen[:, 2] > 1e-6, BOX_CLS, GROUND_CLS).astype(np.uint8)
    root = _with_cls(write_world(tmp_path_factory.mktemp("therm")), cls)
    scene = load_appearance(root, tile_m=64.0)
    raster = RasterCamera(scene, ctx=ctx, textures=False)
    yield root, scene, raster
    raster.release()


def _cam(gl_world, table=None, static=None, sun=NIGHT) -> tr.ThermalCamera:
    root, scene, raster = gl_world
    return tr.ThermalCamera(scene, table=table or _table(), tri_cls=tr.static_classes(root, scene.triangles), static=static, sun_az_el_deg=sun,
                            raster=raster)


def _custom_table(**rows) -> tr.ThermalTable:
    import yaml

    doc = yaml.safe_load(tr.DEFAULT_TABLE.read_text(encoding="utf-8"))
    for k, v in rows.items():
        doc["static"][k].update(v)
    return tr.thermal_table_from_doc(doc)


def _nadir(p):
    from awr.sensorsvc.camera import look_at_R

    p = np.asarray(p, np.float64)
    return look_at_R(p, p - [0.0, 0.0, 1.0]), p


def _render1(cam, R, p, env, model, **kw):
    t_end = 10.0
    ts = tr.subframe_times_s(t_end, model)
    return cam.render(model, [(R, p)] * len(ts), ts, t_end, env, **kw)


def test_gpu_plate_300k_eps1_and_clear_sky_eps09(gl_world) -> None:
    """(a) 均匀 300 K、ε = 1 平板（无噪声档）误差 ≤ 0.05 K；(b) ε = 0.9 水平面晴空（T_sky = T_air − 25 K）与解析解差 ≤ 0.1 K。"""
    clean = tr.ThermalModel(noise="clean")
    zero = {"dT_base": 0.0, "dT_night": 0.0, "dT_sun": 0.0}
    cam = _cam(gl_world, _custom_table(ground={**zero, "eps": 1.0}, roof={**zero, "eps": 1.0}))
    env = tr.thermal_env(cam.table, 300.0, cloud=1.0)
    R, p = _nadir([-60.0, 60.0, 80.0])                                   # 只看地面（盒子在视场外）
    o = _render1(cam, R, p, env, clean, depth=True)
    assert (o.semantic == GROUND_CLS).all() and (o.depth_m > 0).all()
    err = np.abs(o.temp_ck.astype(np.float64) / 100 - 300.0)
    assert err.max() <= PLATE_TOL_K, err.max()
    assert np.abs(o.t_app_k - 300.0).max() < 1e-3
    cam.release()
    cam = _cam(gl_world, _custom_table(ground={**zero, "eps": 0.9}))
    env = tr.thermal_env(cam.table, T_AIR, cloud=0.0, rh=0.5)
    assert env.sky_k == pytest.approx(T_AIR - 25.0)
    o = _render1(cam, R, p, env, clean, depth=True)
    rows, cols = np.mgrid[0:512:64, 0:640:64]
    from awr.sensorsvc.thermal.radiometry import range_from_depth

    rng = range_from_depth(o.depth_m, clean.fx, clean.fy, clean.cx, clean.cy)
    for i, j in zip(rows.ravel(), cols.ravel(), strict=True):
        want = _exact_app(T_AIR, 0.9, 1.0, rng[i, j], env)
        assert abs(o.temp_ck[i, j] / 100 - want) <= CLEAR_TOL_K and abs(o.t_app_k[i, j] - want) < 2e-3, (i, j, want)
    cam.release()


def test_gpu_fog_1km_contrast_follows_tau(gl_world) -> None:
    """(c) 雾中 1 km 外的目标（盒子西立面，ΔT = +10 K，ε = 1）：入瞳辐亮度对比 L − B(T_air) 按 τ(d) 衰减，与解析解差 ≤ 5%；
    表观温差与解析解差 ≤ 5%。"""
    from awr.sensorsvc.camera.proj import gimbal_R

    clean = tr.ThermalModel(noise="clean", optics_sigma_px=0.0)
    cam = _cam(gl_world, _custom_table(roof={"dT_base": 10.0, "dT_night": 0.0, "eps": 1.0}))
    B = rad.band_radiance_exact
    p = np.array([BOX[0][0] - 1000.0, 0.0, 15.0])
    for vis_km in (0.5, 1.0, 2.0):
        env = tr.thermal_env(cam.table, T_AIR, cloud=1.0, rh=0.5, fog_visibility_km=vis_km)
        o = _render1(cam, gimbal_R(0.0, 0.0), p, env, clean, depth=True)
        c = o.temp_ck[250:262, 314:326].astype(np.float64) / 100            # 立面中部（约 22 × 32 px）
        assert np.allclose(o.depth_m[250:262, 314:326], 1000.0, atol=1e-2)
        tau = math.exp(-env.beta_per_km * 1.0)
        got = (B(float(np.median(c))) - B(T_AIR)) / (B(T_AIR + 10.0) - B(T_AIR))
        assert abs(got / tau - 1.0) <= FOG_TOL, (vis_km, got, tau)
        want = _exact_app(T_AIR + 10.0, 1.0, 0.0, 1000.0, env) - T_AIR
        assert abs((float(np.median(c)) - T_AIR) / want - 1.0) <= FOG_TOL
    cam.release()


def _px(model, R, p, X) -> tuple[int, int, float]:
    from awr.sensorsvc.camera.proj import project_points

    uv, z = project_points(model.camera(), R, p, np.asarray([X], np.float64))
    return math.floor(uv[0, 1]), math.floor(uv[0, 0]), float(z[0])


def _at(base: np.ndarray, x: float, y: float, z: float) -> np.ndarray:
    return np.asarray(base, np.float64) + np.array([x, y, z])


def _win(a: np.ndarray, i: int, j: int, r: int = 2) -> np.ndarray:
    return a[i - r:i + r + 1, j - r:j + r + 1]


@pytest.mark.parametrize("case", ["person", "person_dsl", "vehicle_running", "vehicle_off_600s"])
def test_gpu_dynamic_entities_match_table(gl_world, case: str) -> None:
    """动态实体（无噪声档）：人员着装与皮肤、车辆车身与发动机区（正下视）及排气区（正后方平视）的表观温度与参数表 + 解析辐射式
    之差 ≤ 0.2 K（D3-AC-61）。演员位于地面 (−60, 60)，夜间、云量 0.5。"""
    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import ActorFrame

    clean = tr.ThermalModel(noise="clean")
    cam = _cam(gl_world)
    t = cam.table
    env = tr.thermal_env(t, T_AIR, cloud=0.5)
    base = np.array([-60.0, 60.0, 0.0])
    actor = {"person": PERSON, "person_dsl": {**PERSON, "thermal": {"surface_temp_c": 20.0, "emissivity": 0.95}}}.get(case, VEHICLE)
    off = {"vehicle_running": None, "vehicle_off_600s": tr.engine_off_s(720.0 + 1.0, 1.0, t)}.get(case, math.inf)
    if case == "vehicle_off_600s":
        assert off == pytest.approx(600.0)
    th = tr.actor_thermal(actor, t, env, NIGHT, engine_off=off)
    cam.set_actors([actor])
    frame = ActorFrame(np.array([0]), base[None], np.array([0.0]))
    ln, _w, h = (s / 1000.0 for s in actor["size_mm"])
    checks = []                                                          # (像素, 期望表观温度)
    if actor["class"] == "person":
        R, p = _nadir(_at(base, 0.0, 0.0, 6.0))
        for X, zone in ((_at(base, 0.0, 0.0, h), 1), (_at(base, 0.0, 0.14, 0.82 * h), 0)):        # 头顶（皮肤）、肩部（着装）
            i, j, d = _px(clean, R, p, X)
            checks.append((R, p, i, j, _exact_app(th.zone_t_k[zone], th.zone_eps[zone], 1.0, d, env), zone))
    else:
        R, p = _nadir(_at(base, 0.0, 0.0, 10.0))
        for X, zone in ((_at(base, 0.35 * ln, 0.0, 0.62 * h), 2), (_at(base, -0.07 * ln, 0.0, h), 0)):   # 前机盖（发动机）、车顶（车身）
            i, j, d = _px(clean, R, p, X)
            checks.append((R, p, i, j, _exact_app(th.zone_t_k[zone], th.zone_eps[zone], 1.0, d, env), zone))
        Rr, pr = gimbal_R(0.0, 0.0), _at(base, -ln / 2 - 4.0, 0.0, 0.265 * h)                  # 正后方平视：尾部下沿（排气区）
        X = _at(base, -ln / 2, 0.0, 0.265 * h)
        i, j, d = _px(clean, Rr, pr, X)
        checks.append((Rr, pr, i, j, _exact_app(th.zone_t_k[3], th.zone_eps[3], 0.0, math.hypot(d, 0.0), env), 3))
    rep = []
    for R, p, i, j, want, zone in checks:
        o = _render1(cam, R, p, env, clean, actor_frames=[frame] * clean.lag_subframes, actor_th=[th])
        assert (_win(o.instance, i, j) == 1).all() and (_win(o.semantic, i, j) == 32 + (actor["class"] == "vehicle")).all()
        got = float(np.median(_win(o.temp_ck, i, j))) / 100
        rep.append((zone, round(got, 3), round(want, 3)))
        assert abs(got - want) <= DYN_TOL_K, (case, zone, got, want)
        assert abs(float(np.median(_win(o.t_app_k, i, j))) - want) <= 0.01
        objs = tr.thermal_objects(o.t_app_k, o.instance, o.instance_unocc, o.semantic, [actor])
        assert len(objs) == 1 and objs[0]["dT_app_k"] > 1.0
        from awr.sensorsvc.camera.actors import visibility_stats

        assert o.vis is not None and np.array_equal(o.vis, visibility_stats(o.instance, o.instance_unocc, o.semantic, None, 2))   # GPU 归约
        assert tr.thermal_objects(o.t_app_k, o.instance, o.instance_unocc, o.semantic, [actor], stats=o.vis) == objs
    print(case, rep)
    cam.release()


@pytest.mark.parametrize("profile", ["clean", "realistic", "harsh"])
def test_gpu_matches_cpu_reference(gl_world, profile: str) -> None:
    """GPU 管线与 CPU 参考：运动机体（4 子帧，8 m/s 平移叠加偏航）、运动演员、昼间带日照可见度；逐子帧入瞳辐亮度与
    `radiometry.sensor_radiance` 相对差 ≤ 1e-5，无噪表观温度差 ≤ 1e-3 K，`temp_ck` 与 `noise.detector` 差 ≤ 1 LSB 且 ≥ 99% 相同（量化舍入边界处的 float32 差）。"""
    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import ActorFrame

    _root, scene, _ = gl_world
    static = tr.thermal_bake(scene, DAY, backend="numpy")
    cam = _cam(gl_world, static=static, sun=DAY)
    t = cam.table
    model = tr.ThermalModel(noise=profile)
    env = tr.thermal_env(t, T_AIR, cloud=0.2, rh=0.6, fog_visibility_km=3.0)
    t_end = 12.0
    ts = tr.subframe_times_s(t_end, model)
    poses = [(gimbal_R(20.0 + 90.0 * (s - t_end), -20.0), np.array([-60.0 + 8.0 * (s - t_end), -20.0, 30.0])) for s in ts]
    cam.set_actors([PERSON, VEHICLE])
    frames = [ActorFrame(np.array([0, 1]), np.array([[0.0, -10.0, 0.0], [8.0 + 6.0 * (s - t_end), 5.0, 0.0]]), np.array([0.3, 1.2])) for s in ts]
    th = [tr.actor_thermal(PERSON, t, env, DAY), tr.actor_thermal(VEHICLE, t, env, DAY, engine_off=None)]
    keys = tr.NoiseKeys(seed=1234, uav_no=2, sensor_no=0, frame_index=57)
    o = cam.render(model, poses, ts, t_end, env, actor_frames=frames, actor_th=th, noise=keys, debug=True)
    assert len(o.weights) == 4 and o.actors_drawn == 2
    planck = cam.planck
    prof = model.noise_profile()
    L_ref = []
    for j in range(4):
        te, z = o.debug["te"][j], o.debug["z"][j]
        rng = rad.range_from_depth(z, model.fx, model.fy, model.cx, model.cy)
        L = rad.sensor_radiance(planck, te[..., 0], te[..., 1], te[..., 2], rng, env)
        assert np.abs(o.debug["L"][j] / L - 1.0).max() <= 1e-5
        L_ref.append(L)
    sky = o.semantic == 255
    assert sky.any() and (~sky).any() and (o.instance == 1).any() and (o.instance == 2).any()
    lv = tn.optics_blur(tn.blend_subframes(np.stack(o.debug["L"]), o.weights), prof.optics_sigma_px)
    assert np.abs(planck.temperature(lv) - o.t_app_k).max() <= 1e-3
    _t, ck = tn.detector(np.stack(o.debug["L"]), ts, t_end, planck, prof, seed=keys.seed, uav_no=keys.uav_no, sensor_no=keys.sensor_no,
                         frame_index=keys.frame_index)
    d = np.abs(ck.astype(np.int32) - o.temp_ck.astype(np.int32))
    assert d.max() <= 1 and (d == 0).mean() >= 0.99, (d.max(), (d == 0).mean())
    print(profile, "temp_ck equal fraction", float((d == 0).mean()))
    if profile != "clean":                                               # 噪声确实施加（与无噪图差的标准差约为 NETD 量级）
        noise_k = o.temp_ck.astype(np.float64) / 100 - o.t_app_k
        assert 0.02 < float(np.std(noise_k[~sky])) < 0.6
    cam.release()


def test_gpu_two_runs_byte_identical_and_static_shortcut(gl_world) -> None:
    """同进程同卡两次运行逐字节一致（temp_ck、无噪温度、实例、语义与 u16 + zstd 交付字节）；静止机体只绘制一遍。"""
    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import ActorFrame

    cam = _cam(gl_world)
    t = cam.table
    model = tr.THERM512
    env = tr.thermal_env(t, T_AIR, cloud=0.5)
    ts = tr.subframe_times_s(3.0, model)
    poses = [(gimbal_R(10.0 * k, -50.0), np.array([-20.0 + k, 0.0, 40.0])) for k in range(4)]
    cam.set_actors([PERSON])
    fr = [ActorFrame(np.array([0]), np.array([[0.0, -5.0, 0.0]]), np.array([0.0]))] * 4
    th = [tr.actor_thermal(PERSON, t, env, NIGHT)]
    keys = tr.NoiseKeys(7, 1, 0, 3)
    a = cam.render(model, poses, ts, 3.0, env, actor_frames=fr, actor_th=th, noise=keys, depth=True)
    b = cam.render(model, poses, ts, 3.0, env, actor_frames=fr, actor_th=th, noise=keys, depth=True)
    for k in ("temp_ck", "t_app_k", "instance", "semantic", "instance_unocc", "depth_m"):
        assert getattr(a, k).tobytes() == getattr(b, k).tobytes(), k
    assert tr.encode_temp_ck(a.temp_ck) == tr.encode_temp_ck(b.temp_ck)
    c = cam.render(model, [poses[0]] * 4, ts, 3.0, env, actor_frames=fr, actor_th=th, noise=keys)
    assert len(c.weights) == 1 and c.t_sub_s.tolist() == [ts[-1]]
    other = cam.render(model, poses, ts, 3.0, env, actor_frames=fr, actor_th=th, noise=tr.NoiseKeys(8, 1, 0, 3))
    assert other.temp_ck.tobytes() != a.temp_ck.tobytes()
    cam.release()


def test_gpu_bake_warp_matches_numpy_and_is_deterministic(gl_world) -> None:
    pytest.importorskip("warp")
    _root, scene, _ = gl_world
    """同卡两次逐字节一致；与 numpy 参照逐三角形相同（太阳方位 100°）；方位 90° 时盒子南北面与太阳方向共面（掠射，射线擦过
    棱边，两种求交实现可不同），比较时排除 |n·sun| < 1e-6 的三角形。"""
    tris = tr.scene_triangles(scene)
    for sun in ((100.0, 30.0), DAY):
        a = tr.thermal_bake(scene, sun, backend="warp")
        b = tr.thermal_bake(scene, sun, backend="warp")
        n = tr.thermal_bake(scene, sun, backend="numpy")
        assert a.npz == b.npz and a.backend.startswith("warp-")
        ok = tr.triangle_sun_cos(tris, sun) >= 1e-6
        assert np.array_equal(a.sun_vis[ok], n.sun_vis[ok]), sun
        if sun == (100.0, 30.0):
            assert ok.all()


def test_gpu_throughput_reference(gl_world) -> None:
    """开发参考（不计入 D3-AC-11 正式数字）：夹具世界 therm512 4 子帧运动机体 + 1 名演员 + realistic 噪声，渲染与读回帧率；
    正式 10 min 吞吐由 camera_throughput.py 在独占卡上采集（S20）。"""
    import time

    from awr.sensorsvc.camera.proj import gimbal_R
    from awr.sensorsvc.camera.raster import ActorFrame

    cam = _cam(gl_world)
    t = cam.table
    model = tr.THERM512
    env = tr.thermal_env(t, T_AIR, cloud=0.5)
    cam.set_actors([VEHICLE])
    th = [tr.actor_thermal(VEHICLE, t, env, NIGHT, engine_off=None)]
    n = 120
    t0 = time.perf_counter()
    enc = 0.0
    for k in range(n):
        ts = tr.subframe_times_s(0.1 * k, model)
        poses = [(gimbal_R(s * 20.0, -45.0), np.array([-40.0 + 5.0 * s, 0.0, 50.0])) for s in ts]
        fr = [ActorFrame(np.array([0]), np.array([[2.0 * s, 0.0, 0.0]]), np.array([0.0])) for s in ts]
        o = cam.render(model, poses, ts, 0.1 * k, env, actor_frames=fr, actor_th=th, noise=tr.NoiseKeys(1, 0, 0, k))
        e0 = time.perf_counter()
        tr.encode_temp_ck(o.temp_ck)
        enc += time.perf_counter() - e0
    dt = time.perf_counter() - t0
    print(f"therm512 fixture: {n / (dt - enc):.0f} frame/s render+readback, zstd {enc / n * 1e3:.2f} ms/frame")
    cam.release()


def test_thermal_frame_aux_pool_is_byte_identical() -> None:
    """辅助线程池组装（`temp_ck` 编码与 sha256、真值 zstd 并行）与同步组装的记录与载荷块逐字节相同、块顺序相同。"""
    pytest.importorskip("zstandard")
    from concurrent.futures import ThreadPoolExecutor

    from awr.sensorsvc.frame import FrameContext
    from awr.sensorsvc.poses import Mount

    m = tr.THERM512
    H, W = m.height, m.width
    rng = np.random.default_rng(9)
    ck = (28800 + rng.integers(-300, 3000, (H, W))).astype(np.uint16)
    inst = np.zeros((H, W), np.uint16)
    inst[100:140, 200:220] = 1
    out = tr.ThermalOut(ck, ck.astype(np.float32) / 100, None, inst, np.ones((H, W), np.uint8), inst.copy(),
                        np.array([0.1, 0.2, 0.3, 0.4]), np.array([0.97, 0.98, 0.99, 1.0]), 1.0, 1.0, 1, 1)
    ctx = FrameContext("run1", "s1", "cv1", "A40", "580")
    man = tr.thermal_manifest(_table(), rad.planck_table(), m)
    kw = dict(uav="u1", sensor_id="therm0", seq=7, t_mid_ns=1_000_000_000, model=m, mount=Mount(), gimbal_reading=(0, 0, 0),
              gimbal_true=(0, 0, 0), pos_enu_m=np.zeros(3), R_ws=np.eye(3), out=out, manifest=man, actors=[PERSON])
    rec0, blk0 = tr.thermal_frame(ctx, **kw)
    with ThreadPoolExecutor(4) as aux:
        rec1, blk1 = tr.thermal_frame(ctx, aux=aux, **kw)
    assert json.dumps(rec0) == json.dumps(rec1) and list(blk0) == list(blk1) and all(blk0[k] == blk1[k] for k in blk0)
    assert rec1["internal"]["encoder"] == tr.TEMP_CK_ENCODER == "zstd-1/u16le"
