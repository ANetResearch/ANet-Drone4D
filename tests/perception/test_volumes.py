"""M19-AC-023（D2-AC-53）：识别物感知体表（AWR-04 §15.11.4 第 2 条；M18-M19 PRD §15.2.3；`awr.sim.perception.field.volume_table`）。

- 各方向半径与 M18 `effective_radius`（同参数、不截断）一致 ≤ 1 m：声学在敏感体方位偏置方向、视觉在 15° 仰角（侧面、对天）；
  背向衰减使背后方位半径减小；视觉顶面（≥ 45°）与对地（< 0°）；夜间规则；与独立参考 `ref_quant.py` ≤ 1 m；
- 表模型概率 P(r_eff) = 0.5、P(r_eff − w) ≈ 0.9、P(r_eff + w) ≈ 0.1；雷达（P1）视场内为 SNR = snr_req 的距离、视场外 0；
- 线格式折叠（1、6、8、48 项）与展开互逆；感知体只取启用的视觉、声学（与配置 radar_rx 时的雷达）敏感体。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
import ref_quant as RQ

from awr.sim.perception import field as F
from awr.sim.perception.quant import FieldEnv
from awr.sim.targets import TargetConfig, effective_radius
from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.radar import radar_radius
from awr.sim.targets.signature import P600


def env_of(band: str = "night", bg: float = 30.0, mor: float = math.inf) -> FieldEnv:
    return FieldEnv(TargetEnv(band, mor, profile_bands("rural", bg), bg, "rural", 1.0, 0, False, 0.0))


def flat(band: str, bg: float, mor: float = math.inf) -> FieldEnv:
    return FieldEnv(TargetEnv(band, mor, np.full(24, bg - 10.0 * math.log10(24.0)), bg, "rural", 1.0, 0, False, 0.0))


def _cfg(**over) -> TargetConfig:
    return TargetConfig.from_dict({"class": "person", "motion": {"mode": "static"}, **over}, "person-01")


@pytest.mark.parametrize("gb,off", [(0.0, 0.0), (6.0, 30.0), (12.0, -60.0)])
@pytest.mark.parametrize("bg", [30.0, 45.0, 60.0])
def test_acoustic_table_matches_effective_radius(gb, off, bg):
    body = {"kind": "acoustic", "shape": "hemisphere", "radius_max_m": 300, "gain_back_db": gb, "azimuth_offset_deg": off}
    c = _cfg(sensitivity=[{"kind": "visual", "shape": "sector", "radius_max_m": 300}, body])
    env = env_of("night", bg)
    vt = F.volume_table(c.raw, c.raw["sensitivity"][1], P600, env, 0.5)
    want = effective_radius(c, 1, P600, env.t, conservative=False, alertness=0.5, truncate=False)
    # 方位格 a_i 相对航向；在 a_i = off 方向 g_back = 0（off 取 45° 的倍数时与格点重合；否则用最近格与解析式比较）
    az = np.radians(np.asarray(F.AZ_DEG))
    g = gb * (1.0 - np.cos(az - math.radians(off))) / 2.0
    for i in range(8):
        lw, mask = list(P600.lw_bands()), list(P600.masking_db())
        ref = RQ.acoustic_radius(lw, list(env.l_bg_bands), mask, 20.0, 0.5, 0.0, g=float(g[i]))
        assert vt.r_eff_m[i, 0] == pytest.approx(min(ref, 2000.0), abs=1.0)
        assert np.all(vt.r_eff_m[i] == vt.r_eff_m[i, 0])                # 与仰角无关
    if off % 45.0 == 0.0 or gb == 0.0:
        assert float(np.max(vt.r_eff_m)) == pytest.approx(min(want, 2000.0), abs=1.0)   # 偏置方向（g_back = 0）即 M18 有效半径
    else:
        assert float(np.max(vt.r_eff_m)) <= min(want, 2000.0) + 1e-6
    if gb > 0:
        back = int(np.argmax(np.cos(az - math.radians(off) - math.pi)))
        assert vt.r_eff_m[back, 0] < 0.9 * np.max(vt.r_eff_m)
    # 过渡宽度：P(r_eff) = 0.5，r_eff − w 处 0.9
    r0 = float(min(want, 2000.0))
    assert F.volume_p(np.array([r0]), np.array([r0]), vt.w_m)[0] == pytest.approx(0.5)
    assert F.volume_p(np.array([r0 - vt.w_m]), np.array([r0]), vt.w_m)[0] == pytest.approx(0.9, abs=1e-6)
    assert F.volume_p(np.array([r0 + vt.w_m]), np.array([r0]), vt.w_m)[0] == pytest.approx(0.1, abs=1e-6)


def test_acoustic_w_matches_logistic_slope():
    """w = 2.197·s/|dm/dr|（r_eff 处的局部线性化）：逐对读数的声学 P 在 r_eff 处为 0.5，在 r_eff ∓ w 处约为 0.9 / 0.1（m(r) 对 r 下凸，
    向内的 P 略高，容差 0.06）。"""
    from awr.sim.perception.quant import acoustic_pair

    c = _cfg()
    env = env_of("night", 30.0)
    vt = F.volume_table(c.raw, c.raw["sensitivity"][1], P600, env, 0.5)
    r0 = float(vt.r_eff_m[0, 0])
    for r, want in ((r0, 0.5), (r0 - vt.w_m, 0.9), (r0 + vt.w_m, 0.1)):
        q = acoustic_pair(P600.lw_bands(), r, 0.0, env.l_bg_bands, P600.masking_db(), 20.0, 0.5, 0.0)
        assert q.p == pytest.approx(want, abs=0.06)


@pytest.mark.parametrize("band,mor", [("day", math.inf), ("day", 5000.0), ("low", 20000.0), ("night", 8000.0)])
def test_visual_table_matches_effective_radius(band, mor):
    c = _cfg()
    env = env_of(band, mor=mor)
    vt = F.volume_table(c.raw, c.raw["sensitivity"][0], P600, env, 0.5)
    j15 = F.EL_DEG.index(15.0)
    want = effective_radius(c, 0, P600, env.t, conservative=False, alertness=0.5, truncate=False)
    assert vt.r_eff_m[0, j15] == pytest.approx(min(want, 2000.0), abs=1.0)
    for j, e in enumerate(F.EL_DEG):
        ref = RQ.visual_radius_el((0.22, 0.19), (-0.8, 0.3), e, band, mor, 1.0, 0.5)
        assert vt.r_eff_m[0, j] == pytest.approx(ref, abs=1.0)
        assert np.all(vt.r_eff_m[:, j] == vt.r_eff_m[0, j])              # 与方位无关
    assert vt.w_m == pytest.approx(0.73 * vt.r_eff_m[0, j15], rel=1e-9)


def test_visual_lights_at_night():
    c = _cfg()
    vt = F.volume_table(c.raw, c.raw["sensitivity"][0], P600, env_of("night"), 0.5)
    assert np.all(vt.r_eff_m == 30.0)
    vt2 = F.volume_table(c.raw, c.raw["sensitivity"][0], P600.with_lights(True, True), env_of("night"), 0.5)
    assert np.all(vt2.r_eff_m == 2000.0)                                  # 开灯：只受对比度与 2000 m 上限


def test_alertness_and_band_adjustment_change_tables():
    c = _cfg()
    env = env_of("night", 30.0)
    lo = F.volume_table(c.raw, c.raw["sensitivity"][1], P600, env, 0.2).r_eff_m[0, 0]
    hi = F.volume_table(c.raw, c.raw["sensitivity"][1], P600, env, 0.9).r_eff_m[0, 0]
    assert hi > lo
    adj = _cfg(hearing_band_adj_db=[6.0] * 24)
    up = F.volume_table(adj.raw, adj.raw["sensitivity"][1], P600, env, 0.5).r_eff_m[0, 0]
    base = F.volume_table(c.raw, c.raw["sensitivity"][1], P600, env, 0.5).r_eff_m[0, 0]
    assert up > base + 20.0
    assert up == pytest.approx(effective_radius(adj, 1, P600, env.t, conservative=False, alertness=0.5, truncate=False), abs=1.0)


def test_radar_table_p1():
    rx = {"snr_ref_db": 13.2, "r_ref_m": 400.0, "snr_req_db": 13.2, "fov_az_deg": 50.0, "fov_el_deg": 20.0}
    c = _cfg(radar_rx=rx, sensitivity=[{"kind": "radar", "shape": "sphere", "radius_max_m": 800, "azimuth_offset_deg": 90.0}])
    vt = F.volume_table(c.raw, c.raw["sensitivity"][0], P600, env_of("day"), 0.5)
    r = radar_radius(rx, P600.rcs_m2, 0.0)
    assert r == pytest.approx(400.0 * (0.05 ** 0.25), rel=1e-9)
    j = [F.EL_DEG.index(e) for e in (-15.0, 15.0)]
    for i, az in enumerate(F.AZ_DEG):
        inside = abs(((az - 90.0 + 180.0) % 360.0) - 180.0) <= 50.0
        for jj, e in enumerate(F.EL_DEG):
            want = r if (inside and jj in j) else 0.0
            assert vt.r_eff_m[i, jj] == pytest.approx(want), (az, e)
    assert vt.w_m == pytest.approx(math.log(9.0) * 1.0 / (17.372 / r), rel=1e-3)
    assert effective_radius(c, 0, P600, env_of("day").t, conservative=False, alertness=0.5, truncate=False) == pytest.approx(r, abs=1e-6)


@pytest.mark.parametrize("shape", ["uniform", "el", "az", "full"])
def test_table_compaction_roundtrip(shape):
    rng = np.random.default_rng(3)
    if shape == "uniform":
        t = np.full((8, 6), 153.3)
    elif shape == "el":
        t = np.repeat(rng.uniform(1, 2000, 6)[None, :], 8, axis=0)
    elif shape == "az":
        t = np.repeat(rng.uniform(1, 2000, 8)[:, None], 6, axis=1)
    else:
        t = rng.uniform(0, 2000, (8, 6))
    q = F._q_table(t)
    assert len(q) == {"uniform": 1, "el": 6, "az": 8, "full": 48}[shape]
    assert np.allclose(F.expand_table(q), np.round(t * 10) / 10)
