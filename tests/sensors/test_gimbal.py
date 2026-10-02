"""M13-AC-006：云台 5 模式、限位、限速、映射表与生成器缺省（M13-FR-011、FR-012；M13 §6.5.2）。"""

from __future__ import annotations

import math

import numpy as np
import pytest

from awr.sim.sensors.enums import GimbalMode
from awr.sim.sensors.gimbal import generator_default, mode_from_item

D = math.radians


def ang(b, s, k=0):
    blk = b.S.blocks["sensors"]
    return float(blk["g_az"][s, k]), float(blk["g_el"][s, k])


def test_default_fixed_minus_15(bench):
    bench.spawn(0, 1)
    bench.run(0.1)
    az, el = ang(bench, 0)
    assert az == 0.0 and el == pytest.approx(D(-15))
    assert not bench.S.blocks["sensors"]["g_dyn"][0].any()


def test_look_at_static_target_converges_in_half_second(bench):
    b = bench
    b.spawn(0, 1, pos=(0, 0, 60), yaw_deg=10)
    b.run(0.02)
    tgt = np.array([40.0, 30.0, 0.0])
    b.rt.set_mode(0, "camera", GimbalMode.LOOK_AT, {"p_enu_m": tgt})
    b.run(0.5)
    az, el = ang(b, 0)
    pm = np.array([0, 0, 60.0]) + np.array([[math.cos(D(10)), -math.sin(D(10)), 0], [math.sin(D(10)), math.cos(D(10)), 0], [0, 0, 1]]) @ np.array([0.12, 0, -0.08])
    d = tgt - pm
    az_e = math.atan2(d[1], d[0]) - D(10)
    el_e = math.atan2(d[2], math.hypot(d[0], d[1]))
    assert abs(math.degrees(az - az_e)) <= 0.1 and abs(math.degrees(el - el_e)) <= 0.1
    assert b.S.blocks["sensors"]["g_dyn"][0, 0]  # LOOK_AT 常驻动态集


def test_rate_limit_90_deg_per_s(bench):
    b = bench
    b.spawn(0, 1)
    b.run(0.02)
    b.rt.set_mode(0, "camera", "fixed", {"az_rad": D(120), "el_rad": D(-15)})
    prev = ang(b, 0)[0]
    rates = []
    for _ in range(75):
        b.run(0.02)
        a = ang(b, 0)[0]
        rates.append(abs(a - prev) / 0.02)
        prev = a
    assert max(rates) <= D(90) * (1 + 1e-9)
    assert ang(b, 0)[0] == pytest.approx(D(120))  # 1.33 s 后到位并退出动态集
    assert not b.S.blocks["sensors"]["g_dyn"][0, 0]


def test_limits_clamp_and_flag(bench):
    b = bench
    b.spawn(0, 1, pos=(0, 0, 10))
    b.run(0.02)
    b.rt.set_mode(0, "camera", "look_at", {"p_enu_m": [-100.0, 1.0, 10.0]})  # 正后方 ~179°
    b.run(3.0)
    az, _ = ang(b, 0)
    assert az == pytest.approx(D(150)) and b.S.blocks["sensors"]["g_lim"][0, 0]
    b.rt.set_mode(0, "camera", "fixed", {"az_rad": 0.0, "el_rad": D(60)})  # 超过 +30°
    b.run(2.0)
    assert ang(b, 0)[1] == pytest.approx(D(30))


def test_nadir_singularity_keeps_azimuth(bench):
    b = bench
    b.spawn(0, 1, pos=(0, 0, 60))
    b.run(0.02)
    b.rt.set_mode(0, "camera", "fixed", {"az_rad": D(40), "el_rad": D(-15)})
    b.run(1.0)
    b.rt.set_mode(0, "camera", "look_at", {"p_enu_m": [0.12, 0.0, 0.0]})  # 挂载点正下方
    b.run(1.5)
    az, el = ang(b, 0)
    assert az == pytest.approx(D(40)) and el == pytest.approx(D(-90), abs=1e-3)


MAPPING = [
    ({"pitch_rad": -0.3}, GimbalMode.FIXED, {"az_rad": 0.0, "el_rad": -0.3}),
    ({"pitch_rad": -math.pi / 2}, GimbalMode.FIXED, {"az_rad": 0.0, "el_rad": -math.pi / 2}),
]


def test_set_gimbal_mapping_rows(bench):
    b = bench
    b.spawn(0, 1)
    b.run(0.02)
    blk = b.S.blocks["sensors"]
    b.rt.set_gimbal(0, pitch_rad=-0.3)
    assert blk["g_mode"][0, 0] == GimbalMode.FIXED and blk["g_pel"][0, 0] == -0.3 and blk["g_paz"][0, 0] == 0.0
    mc = {"center_enu_m": [5.0, 6.0, 7.0], "target_enu_m": [1.0, 2.0, 3.0]}
    b.rt.set_gimbal(0, look_at="axis", mission_ctx=mc)
    assert blk["g_mode"][0, 0] == GimbalMode.LOOK_AT_AXIS and list(blk["g_tgt"][0, 0, :2]) == [5.0, 6.0] and math.isnan(blk["g_tgt"][0, 0, 2])
    b.rt.set_gimbal(0, look_at="center", mission_ctx=mc)  # 无世界时地面高取 center z
    assert blk["g_mode"][0, 0] == GimbalMode.LOOK_AT and list(blk["g_tgt"][0, 0]) == [5.0, 6.0, 7.0]
    b.rt.set_gimbal(0, look_at="target", mission_ctx=mc)
    assert list(blk["g_tgt"][0, 0]) == [1.0, 2.0, 3.0]
    b.rt.set_gimbal(0, look_at="target", mission_ctx={"center_enu_m": [5.0, 6.0, 7.0]})  # 缺 target 退回 center
    assert list(blk["g_tgt"][0, 0]) == [5.0, 6.0, 7.0]
    assert blk["g_mode"][0, 1] == GimbalMode.LOOK_AT  # 不指定传感器时相机与热成像同指


@pytest.mark.parametrize(("gen", "params", "mode", "args"), [
    ("lawnmower", {}, GimbalMode.NADIR, {}),
    ("expanding_square", {}, GimbalMode.NADIR, {}),
    ("terrain_follow", {}, GimbalMode.NADIR, {}),
    ("orbit", {"center_enu_m": [3.0, 4.0, 9.0]}, GimbalMode.LOOK_AT, {"p_enu_m": (3.0, 4.0, 9.0)}),
    ("helix_scan", {"center_enu_m": [3.0, 4.0, 0.0]}, GimbalMode.LOOK_AT_AXIS, {"center_enu_m": (3.0, 4.0, float("nan"))}),
    ("corridor", {"gimbal_tilt_deg": 30, "look": "right"}, GimbalMode.FIXED, {"az_rad": -math.pi / 2, "el_rad": -D(30)}),
    ("formation", {}, GimbalMode.FIXED, {"az_rad": 0.0, "el_rad": D(-15)}),
])
def test_generator_defaults(gen, params, mode, args):
    m, a = generator_default(gen, params)
    assert m == mode
    for k, v in args.items():
        assert np.allclose(np.asarray(a[k], float), np.asarray(v, float), equal_nan=True)


def test_nadir_default_for_coverage_generators(bench):
    b = bench
    b.spawn(0, 1, yaw_deg=30)
    b.run(0.02)
    b.rt.set_default_for(0, "expanding_square", {})
    b.run(1.5)
    az, el = ang(b, 0)
    assert az == 0.0 and el == pytest.approx(D(-90))


def test_item_gimbal_dicts_from_m10():
    assert mode_from_item({"mode": "nadir"}) == (GimbalMode.NADIR, {})
    assert mode_from_item({"mode": "look_at", "p_enu_m": [1, 2, 3]})[1]["p_enu_m"] == (1.0, 2.0, 3.0)
    assert mode_from_item({"mode": "fixed", "az_rad": 1.0, "el_rad": -0.5}) == (GimbalMode.FIXED, {"az_rad": 1.0, "el_rad": -0.5})


def test_scalar_and_numpy_paths_agree(bench_factory):
    res = []
    for n in (4, 12):  # 2n 个云台：8 以内走标量路径，24 走 numpy 路径
        b = bench_factory()
        rng = np.random.default_rng(5)
        for s in range(n):
            b.spawn(s, s + 1, pos=rng.uniform(-50, 50, 3) + np.array([0.0, 0.0, 60.0]), yaw_deg=float(rng.uniform(-180, 180)))
        b.run(0.02)
        for s in range(n):
            b.rt.set_mode(s, None, "look_at", {"p_enu_m": [10.0 * s, -5.0, 0.0]})
        b.run(0.6)
        blk = b.S.blocks["sensors"]
        res.append((blk["g_az"][:4].copy(), blk["g_el"][:4].copy(), dict(b.rt.gimbal.stats)))
    assert res[0][2]["scalar"] > 0 and res[1][2]["numpy"] > 0
    assert np.allclose(res[0][0], res[1][0], atol=1e-12) and np.allclose(res[0][1], res[1][1], atol=1e-12)


@pytest.mark.parametrize("variant", ["vec", "numba", "scan"])
def test_vector_path_matches_numpy_path(bench_factory, variant):
    """大机群路径与逐对取参数的 `_step_numpy` 逐位相同：`GimbalBank._step_vec`（按 rig 分组取参数，FX2-R2）与融合核
    `_step_nb`（kernels_gimbal，ADR-070）。同一初始状态各走 40 步（含 LOOK_AT、NADIR、FORWARD、FIXED 与越限钳位）。"""
    from awr.sim.sensors import gimbal as GM
    from awr.sim.sensors import kernels_gimbal as KG

    if variant in ("numba", "scan") and not KG.HAVE_NUMBA:
        pytest.skip("numba unavailable")
    out = []
    for path in ("numpy", variant):
        b = bench_factory()
        b.rt.gimbal.use_numba = path in ("numba", "scan")
        b.rt.gimbal.scan = path == "scan"
        rng = np.random.default_rng(9)
        for s in range(20):
            b.spawn(s, s + 1, pos=rng.uniform(-50, 50, 3) + np.array([0.0, 0.0, 60.0]), yaw_deg=float(rng.uniform(-180, 180)))
        b.run(0.02)
        modes = ["look_at", "nadir", "forward", "fixed", "look_at_axis"]
        for s in range(20):
            m = modes[s % 5]
            args = ({"p_enu_m": [10.0 * s, -5.0, 0.0]} if m == "look_at" else
                    {"center_enu_m": [4.0 * s, 7.0]} if m == "look_at_axis" else
                    ({"az_rad": 3.5, "el_rad": -2.0} if m == "fixed" else {}))
            b.rt.set_mode(s, None, m, args)
        if path == "numpy":
            orig = GM.GimbalBank._step_vec

            def via_numpy(self, S, rows, S_, K_, ri, dt):
                PQ = S.enu.pose_enu_flu(rows)
                self._step_numpy([(int(s), int(k)) for s, k in zip(S_, K_, strict=True)], PQ,
                                 {int(s): i for i, s in enumerate(rows)}, dt)

            GM.GimbalBank._step_vec = via_numpy
            try:
                b.run(0.8)
            finally:
                GM.GimbalBank._step_vec = orig
        else:
            b.run(0.8)
        blk = b.S.blocks["sensors"]
        out.append({k: blk[k].copy() for k in ("g_az", "g_el", "g_lim", "g_dyn")} | {"limited": b.rt.gimbal.stats["limited"]})
    for k in out[0]:
        assert np.array_equal(out[0][k], out[1][k]), k
