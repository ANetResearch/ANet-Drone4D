"""M19-AC-022（D2-AC-53）：辐射场剖面（AWR-04 §15.11.3、§15.11.4；M18-M19 PRD §15.2.2；`awr.sim.perception.field`）。

- 四种剖面与独立参考实现 `ref_quant.py` 逐点一致（dB 量 ≤ 0.1 dB，线性量 ≤ 0.5%），不绘制处一致；
- 声学场值随距离单调不增，低于 max(20, L_bg − 10) dB(A) 不绘制；视觉截止（对比度、夜间未开灯 30 m、开灯不截止）与顶面因子；
  热的 NETD 截止；§15.11.3 算例（P600：声学可闻壳对人 145.7 m 为单带口径，此处检 24 带口径的缺省人壳与背景噪声壳；视觉对人
  可见壳 1499 m；热 D 级壳约 100 m；雷达 D 级壳约 85 m）；
- 线格式：u16 / i16 定标、末尾省略、折叠与解码互逆（≤ 0.05 dB 或 0.05%）；rev 只在超过阈值（0.5 dB / 2%）时增加；
- 确定性图案：Halton 序列与 AWR-04 §15.11.2 一致（方向为单位向量，半径对数均匀落在 [r_min, r_max]）；精确场值与插值对拍的
  Python 侧（前端插值的同式实现）≤ 0.5 dB 或 2%。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import ref_quant as RQ

from awr.sim.perception import field as F
from awr.sim.perception.quant import FieldEnv
from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.signature import P600, signature_of


def env_of(band: str = "night", bg: float = 30.0, mor: float = math.inf, rain: float = 0.0, rho_v: float = 7.5) -> FieldEnv:
    return FieldEnv(TargetEnv(band, mor, profile_bands("rural", bg), bg, "rural", 1.0, 0, False, rain), rho_v)


MODELS = ("p600_mid360", "awr_q1", "awr_q9", "awr_f1", "awr_v1")


def _sig(mid: str):
    from awr.sim.fleet.profiles import ProfileTable

    return signature_of(mid, ProfileTable())


@pytest.mark.parametrize("mid", MODELS)
def test_profiles_vs_reference(mid):
    sig = _sig(mid)
    for env in (env_of("night", 30.0), env_of("day", 50.0, mor=6000.0, rain=10.0, rho_v=15.0)):
        for kind in F.FIELD_KINDS:
            p = F.field_profile(kind, sig, env)
            assert p.r_m.size == F.N_R and p.r_m[0] == pytest.approx(2.0)
            for r, v in zip(p.r_m, p.v_r, strict=True):
                if kind == "acoustic":
                    w = RQ.prof_acoustic(list(sig.lw_bands()), r)
                    w = w if w >= max(20.0, env.l_bg_dba - 10.0) else None
                elif kind == "visual":
                    w = RQ.prof_visual(sig.area_m2[1], r)
                    w = w if w >= 0.5 else None
                elif kind == "thermal":
                    w = RQ.prof_thermal(sig.d_hot_m, r, eps=sig.ir_emissivity, dt=sig.ir_delta_t_k, mor=env.mor_m, rho_v=env.rho_v_gm3)
                else:
                    w = RQ.prof_radar(sig.rcs_m2, r, env.rain_mmph)
                    w = w if w >= -10.0 else None
                if w is None:
                    assert math.isnan(v), (kind, r, v)
                elif F.COMBINE[kind] == "add":
                    assert v == pytest.approx(w, abs=0.1), (kind, r)
                else:
                    assert v == pytest.approx(w, rel=0.005), (kind, r)


def test_acoustic_monotone_and_floor():
    for mid in MODELS:
        sig = _sig(mid)
        for bg in (30.0, 50.0, 60.0):
            p = F.field_profile("acoustic", sig, env_of("day", bg))
            v = p.v_r[~np.isnan(p.v_r)]
            assert np.all(np.diff(v) <= 1e-9)
            assert np.all(v >= max(20.0, bg - 10.0) - 1e-9)
            assert np.all(p.f_el == 0.0) and np.all(p.cut_el_m == 0.0)


def test_visual_cut_and_faces():
    e = env_of("day", mor=5000.0)
    p = F.field_profile("visual", P600, e)
    sky = math.log(0.8 / 0.05) / (math.log(20.0) / 5000.0)
    gnd = math.log(0.3 / 0.05) / (math.log(20.0) / 5000.0)
    assert list(p.cut_el_m) == pytest.approx([gnd, gnd, gnd, sky, sky, sky])
    assert p.f_el[0] == pytest.approx(math.sqrt(0.22 / 0.19)) and p.f_el[2] == 1.0 and p.f_el[3] == 1.0 and p.f_el[5] == p.f_el[0]
    n = F.field_profile("visual", P600, env_of("night"))
    assert np.all(n.cut_el_m == 30.0)
    lit = F.field_profile("visual", P600.with_lights(True, False), env_of("night"))
    assert np.all(lit.cut_el_m == 0.0)
    clear = F.field_profile("visual", P600, env_of("day", mor=math.inf))
    assert np.all(clear.cut_el_m == 0.0)


def test_shell_examples_awr04_15_11_3():
    """算例：视觉对人可见壳 1499 m（日间、无限 MOR）；热 D 级壳约 100 m；雷达 D 级壳约 85 m；声学缺省人壳（24 带口径）与背景噪声壳。"""
    assert F.default_shell_m("visual", P600, env_of("day")) == pytest.approx(1499.0, abs=1.0)
    assert F.default_shell_m("thermal", P600, env_of("night")) == pytest.approx(100.5, abs=1.0)
    assert F.default_shell_m("radar", P600, env_of("night")) == pytest.approx(85.0, abs=0.5)
    ac = F.default_shell_m("acoustic", P600, env_of("night", 30.0))
    from awr.sim.targets.acoustic import acoustic_radius, threshold_bands

    want = acoustic_radius(P600.lw_bands(), threshold_bands(env_of("night", 30.0).l_bg_bands, P600.masking_db(), 20.0, 0.5, 0.0))
    assert ac == pytest.approx(want, abs=1e-6)
    assert 120.0 < ac < 200.0
    # 背景噪声壳：L_pA(r) = L_bg（剖面插值求得，与 ref 二分 ≤ 1 m）
    p = F.field_profile("acoustic", P600, env_of("night", 30.0))
    r_bg = _iso(p, 30.0)
    lo, hi = 2.0, 2000.0
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if RQ.prof_acoustic(list(P600.lw_bands()), mid) >= 30.0 else (lo, mid)
    assert r_bg == pytest.approx(lo, rel=0.01)


def _interp(p: F.Profile, r: float, el: float, kind: str) -> float:
    """前端插值的同式实现（engine/perception/field.ts `profileValue`）：add 量对 ln r 线性，mul 量对 (ln r, ln v) 线性；仰角取最近格。"""
    lr = np.log(p.r_m)
    x = math.log(r)
    if x < lr[0] or x > lr[-1]:
        return math.nan
    k = min(int(np.searchsorted(lr, x, side="right")) - 1, len(lr) - 2)
    t = (x - lr[k]) / (lr[k + 1] - lr[k])
    a, b = p.v_r[k], p.v_r[k + 1]
    if math.isnan(a) or math.isnan(b):
        return math.nan
    v = a + t * (b - a) if F.COMBINE[kind] == "add" else math.exp(math.log(a) + t * (math.log(b) - math.log(a)))
    j = int(np.argmin(np.abs(np.asarray(F.EL_DEG) - el)))
    v = v + p.f_el[j] if F.COMBINE[kind] == "add" else v * p.f_el[j]
    c = p.cut_el_m[j]
    return math.nan if (c > 0 and r > c) else v


def _iso(p: F.Profile, value: float) -> float:
    v = p.v_r
    for k in range(len(v) - 1):
        if not math.isnan(v[k]) and not math.isnan(v[k + 1]) and v[k] >= value >= v[k + 1]:
            t = (v[k] - value) / (v[k] - v[k + 1])
            return float(math.exp(math.log(p.r_m[k]) + t * (math.log(p.r_m[k + 1]) - math.log(p.r_m[k]))))
    return math.nan


@pytest.mark.parametrize("kind", F.FIELD_KINDS)
def test_interpolation_vs_exact_samples(kind):
    """前端插值（量化后的线格式解码 + 同式插值）与精确采样 ≤ 0.5 dB 或 2%（M19-NFR-009 的 Python 侧）。"""
    env = env_of("day", 45.0, mor=8000.0)
    for mid in MODELS:
        sig = _sig(mid)
        p = F.field_profile(kind, sig, env)
        grid = F.frame_grid(p.r_max_m)
        wire = {"v_r": F._q_vr(F._values(kind, sig, env, grid), F.V_ENC[kind], F.V_OFF[kind]), "f_el": F._q_fel(p.f_el),
                "cut_el_m": F._q_cut(p.cut_el_m, grid[-1])}
        vr, fe, cu = F.decode_profile_uav(wire, F.V_ENC[kind], F.V_OFF[kind])
        q = F.Profile(kind, p.unit, p.combine, grid, vr, fe, cu, p.r_max_m, p.shell_m)
        dirs, rr = F.pattern(512, 3, 2.0, p.r_max_m)
        el = -np.degrees(np.arcsin(dirs[:, 2]))
        exact = F.field_value(kind, sig, env, rr, el)
        for r, e, x in zip(rr, el, exact, strict=True):
            got = _interp(q, float(r), float(e), kind)
            if math.isnan(x) or math.isnan(got):
                continue
            if F.COMBINE[kind] == "add":
                assert abs(got - x) <= 0.5, (kind, mid, r, got, x)
            else:
                assert abs(got - x) <= 0.02 * abs(x), (kind, mid, r, got, x)


def test_wire_quantization_roundtrip():
    v = np.array([71.234, 65.0, np.nan, 40.0, np.nan, np.nan])
    q = F._q_vr(v, "lin", 0.0)
    assert q == [712, 650, 0xFFFF, 400]                                 # 末尾的 0xFFFF 省略
    assert F._q_fel(np.zeros(6)) == [0] and F._q_cut(np.full(6, 30.0), 2000.0) == [30]
    assert F._q_cut(np.array([5000.0, 0, 0, 0, 0, 0]), 2000.0) == [0]  # 超过网格上限视为不截止
    vr, fe, cu = F.decode_profile_uav({"v_r": q, "f_el": [0], "cut_el_m": [30]}, "lin", 0.0)
    assert vr[0] == pytest.approx(71.2) and math.isnan(vr[2]) and np.all(np.isnan(vr[4:])) and np.all(fe == 0) and np.all(cu == 30)
    vr, _, _ = F.decode_profile_uav({"v_r": [0, 100, 232]}, "lin", -10.0)
    assert list(vr[:3]) == pytest.approx([-10.0, 0.0, 13.2])
    # 线性量的对数定标：相对误差 ≤ 0.06%，覆盖 0.01–10⁸
    x = np.array([0.0123, 0.5, 1.0, 1.153, 942.0, 2.5e7])
    back, _, _ = F.decode_profile_uav({"v_r": F._q_vr(x, "log", 0.0)}, "log", 0.0)
    assert np.all(np.abs(back[:6] - x) <= 6e-4 * x)


def test_rev_threshold():
    a = (np.array([50.0, 40.0, np.nan]), np.zeros(6), np.zeros(6))
    assert not F._changed("acoustic", (a[0] + 0.4, a[1], a[2]), a)
    assert F._changed("acoustic", (a[0] + 0.6, a[1], a[2]), a)
    assert F._changed("acoustic", (np.array([50.0, np.nan, np.nan]), a[1], a[2]), a)
    b = (np.array([10.0, 5.0]), np.ones(6), np.zeros(6))
    assert not F._changed("visual", (b[0] * 1.015, b[1], b[2]), b)
    assert F._changed("visual", (b[0] * 1.03, b[1], b[2]), b)
    assert F._changed("visual", (b[0], b[1], np.full(6, 30.0)), b)


def test_halton_pattern():
    assert list(F.halton(np.arange(1, 6), 2)) == pytest.approx([0.5, 0.25, 0.75, 0.125, 0.625])
    assert list(F.halton(np.arange(1, 4), 3)) == pytest.approx([1 / 3, 2 / 3, 1 / 9])
    assert list(F.halton(np.arange(1, 3), 5)) == pytest.approx([0.2, 0.4])
    u, r = F.pattern(512, 0, 2.0, 500.0)
    assert np.allclose(np.linalg.norm(u, axis=1), 1.0)
    assert r.min() >= 2.0 and r.max() <= 500.0
    # 对数均匀：ln r 的分位与 h1 一致；第 1 点 h1 = 0.5 → r = √(2·500)
    assert r[0] == pytest.approx(math.sqrt(2.0 * 500.0))
    u2, _ = F.pattern(512, 1, 2.0, 500.0)
    assert not np.allclose(u, u2)                                         # 锚点按黄金角错开
    assert np.allclose(u2[:, 2], u[:, 2])                                 # z 只取决于 h2
