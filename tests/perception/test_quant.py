"""M19-AC-021（D2-AC-52）：逐对感知量化读数（AWR-04 §15.10；`awr.sim.perception.quant`）。

- 显示用概率的标定：声学 m = 0 → 0.5、+4.4 dB → 0.9；视觉 x = 1 → 0.5；雷达 SNR = snr_req → 0.5；合成按独立近似；
- 单带锚点（P600 L_WA 88、1 kHz、alpha 5 dB/km、乡村夜间 30 dB(A)、a 0.5）145.7 m 处 P_ac = 0.50 ± 0.01；§15.10.2 算例（到达声压、角尺寸、
  对人与狗的等效像素）；§15.10.2 派生量（SL_A、D_vis、d_hot）五个机型与表一致；类别缺省（角分辨率）；
- 与独立参考实现 `ref_quant.py` 对拍：到达声压与余量 ≤ 0.1 dB、角尺寸与等效像素 ≤ 1%、对比度 ≤ 1%、雷达 SNR ≤ 0.1 dB（各 200 组随机）；
- 1000 组随机（识别物，无人机）对：physical 判据（M18 `_evaluate` 的命中）当且仅当 quant 的 `would_discover`（一致率 100%），
  geometric 同理（配置体内）；
- 线格式：REST 展开形式与 topic 紧凑列式 `soa1` 互逆且逐位相等；12 对、三模态加反向感知的帧 ≤ 1.5 KB；按斜距升序 ≤ 12 对、uav 过滤。
"""

from __future__ import annotations

import math
from pathlib import Path

import msgpack
import numpy as np
import pytest
import ref_quant as RQ
import yaml

from awr.sim.perception.quant import (
    FieldEnv,
    QuantTarget,
    QuantUav,
    ReverseQ,
    acoustic_pair,
    encode_soa,
    expand_soa,
    field_env,
    p_any,
    p_logistic,
    p_visual,
    quant_frame,
    quant_pairs,
    radar_pair,
    visual_pair,
)
from awr.sim.targets import TargetConfig
from awr.sim.targets.envview import TargetEnv, profile_bands
from awr.sim.targets.model import ACUITY_DEFAULT_ARCMIN, visual_acuity_of
from awr.sim.targets.signature import P600, signature_from_doc

ROOT = Path(__file__).resolve().parents[2]


def env_of(band: str = "night", bg: float = 30.0, mor: float = math.inf, profile: str = "rural", rain: float = 0.0) -> FieldEnv:
    return FieldEnv(TargetEnv(band, mor, profile_bands(profile, bg), bg, profile, 1e5 if band == "day" else 0.001, 0, False, rain))


def flat_env(band: str, bg: float, mor: float = math.inf) -> FieldEnv:
    """24 带等能量背景（ref_quant 用同一带级）。"""
    return FieldEnv(TargetEnv(band, mor, np.full(24, bg - 10.0 * math.log10(24.0)), bg, "rural", 1e5, 0, False, 0.0))


# ---------------------------------------------------------------- 标定
def test_probability_calibration():
    assert p_logistic(0.0, 2.0) == 0.5
    assert p_logistic(2.0 * math.log(9.0), 2.0) == pytest.approx(0.9, abs=1e-12)
    assert p_logistic(4.4, 2.0) == pytest.approx(0.9, abs=0.002)
    assert p_visual(1.0) == 0.5
    assert p_visual(1.75) == pytest.approx(0.9, abs=0.002)
    assert p_visual(0.49) == pytest.approx(0.1, abs=0.01)
    assert p_visual(0.0) == 0.0 and p_visual(-1.0) == 0.0 and p_visual(1e6) == 1.0
    assert p_any([0.5, 0.5]) == 0.75 and p_any([]) == 0.0
    rx = {"snr_ref_db": 13.2, "r_ref_m": 180.0, "snr_req_db": 13.2, "fov_az_deg": 60.0, "fov_el_deg": 20.0}
    assert radar_pair(rx, 1.0, 180.0, 0.0, in_fov=True).p == pytest.approx(0.5, abs=1e-12)
    assert radar_pair(rx, 1.0, 180.0, 0.0, in_fov=False).p == 0.0


def test_single_band_anchor_145_7_m():
    """AWR-04 §7.3 单带算例：P600 对人（乡村夜间 30 dB(A)、a 0.5）可闻半径 145.7 m，P_ac(145.7) = 0.50 ± 0.01。"""
    q = acoustic_pair(np.array([88.0]), 145.7, 0.0, np.array([30.0]), np.array([0.0]), 20.0, 0.5, 0.0, alpha=np.array([5.0]), a_w=np.array([0.0]),
                      bands_hz=[1000.0], l_bg_dba=30.0)
    assert q.thr_dba == pytest.approx(33.0)
    assert q.margin_db == pytest.approx(0.0, abs=0.01)
    assert q.p == pytest.approx(0.5, abs=0.01)
    assert q.lp_dba == pytest.approx(33.0, abs=0.01)
    # 算例（AWR-04 §15.10.2）：P600 到达声压 50 m 42.8、100 m 36.5、300 m 26.0 dB(A)（单带口径）
    for r, want in ((50.0, 42.8), (100.0, 36.5), (300.0, 26.0)):
        q = acoustic_pair(np.array([88.0]), r, 0.0, np.array([30.0]), np.array([0.0]), 20.0, 0.5, 0.0, alpha=np.array([5.0]), a_w=np.array([0.0]),
                          bands_hz=[1000.0])
        assert q.lp_dba == pytest.approx(want, abs=0.05)


def test_angular_size_examples():
    """§15.10.2 算例：P600 侧向角尺寸 100 m 15.0'、300 m 5.0'、1000 m 1.5'；对人等效像素同值，对狗 3.7、1.2、0.4 px。"""
    env = env_of("day")
    for r, theta, dog in ((100.0, 15.0, 3.7), (300.0, 5.0, 1.2), (1000.0, 1.5, 0.4)):
        q, _ = visual_pair(P600, r, math.radians(10.0), env, 1.0, 0.5, 1.0, los=True, in_fov=True)
        assert q.theta_arcmin == pytest.approx(theta, abs=0.05)
        assert q.n_eq_px == pytest.approx(theta, abs=0.05)
        qd, _ = visual_pair(P600, r, math.radians(10.0), env, 1.0, 0.7, 4.0, los=True, in_fov=True)
        assert qd.n_eq_px == pytest.approx(dog, abs=0.05)
    # 顶面截面（仰角 ≥ 45°）、对地对比度（仰角 < 0）、低照阈值 × 2、夜间灯光规则
    q, _ = visual_pair(P600, 100.0, math.radians(60.0), env, 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.d_vis_m == pytest.approx(math.sqrt(0.22))
    q, _ = visual_pair(P600, 100.0, math.radians(-10.0), env_of("day", mor=5000.0), 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.c_app == pytest.approx(0.3 * math.exp(-math.log(20.0) / 5000.0 * 100.0))
    q, _ = visual_pair(P600, 100.0, 0.2, env_of("low"), 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.theta_th_arcmin == pytest.approx(2.0)
    q, ok = visual_pair(P600, 25.0, 0.2, env_of("night"), 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.p == 1.0 and ok
    q, ok = visual_pair(P600, 40.0, 0.2, env_of("night"), 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.p == 0.0 and not ok
    q, ok = visual_pair(P600.with_lights(True, False), 400.0, 0.2, env_of("night"), 1.0, 0.5, 1.0, los=True, in_fov=True)
    assert q.p == 1.0 and ok and q.lights_on
    q, ok = visual_pair(P600, 100.0, 0.2, env, 1.0, 0.5, 1.0, los=False, in_fov=True)
    assert q.p == 0.0 and not ok


# ---------------------------------------------------------------- 派生量与类别缺省
TABLE = {  # AWR-04 §15.10.2：SL_A（悬停或巡航）、D_vis 顶/侧/前、d_hot、RCS
    "p600": ("p600_mid360", 77.0, (0.469, 0.436, 0.436), 0.169, 0.05),
    "awr_q1": ("awr_q1", 71.0, (0.265, 0.173, 0.173), 0.067, 0.01),
    "awr_q9": ("awr_q9", 83.0, (0.671, 0.500, 0.500), 0.194, 0.10),
    "awr_f1": ("awr_f1", 69.0, (0.894, 0.500, 0.346), 0.158, 0.10),
    "awr_v1": ("awr_v1", 81.0, (1.000, 0.548, 0.424), 0.190, 0.20),
}


@pytest.mark.parametrize("d", sorted(TABLE))
def test_derived_signature_values(d):
    mid, sl_a, dvis, d_hot, rcs = TABLE[d]
    doc = yaml.safe_load((ROOT / "vehicles" / d / "params.yaml").read_text(encoding="utf-8"))
    sig = signature_from_doc(mid, doc)
    assert sig.sl_a_1m_dba() == pytest.approx(sl_a, abs=0.05)
    for got, want in zip(sig.d_vis_faces(), dvis, strict=True):
        assert got == pytest.approx(want, abs=0.0015)
    assert sig.d_hot_m == pytest.approx(d_hot, abs=0.0015)
    assert sig.rcs_m2 == pytest.approx(rcs)


def test_class_default_acuity():
    for cls, v in (("person", 1.0), ("uav", 2.5), ("dog", 4.0), ("vehicle", 1.0)):
        assert ACUITY_DEFAULT_ARCMIN[cls] == v
        assert visual_acuity_of(TargetConfig.from_dict({"class": cls}).raw) == v
    for cls in ("machine", "object"):
        assert visual_acuity_of(TargetConfig.from_dict({"class": cls}).raw) is None
    assert visual_acuity_of(TargetConfig.from_dict({"class": "person", "visual_acuity_arcmin": 2.0}).raw) == 2.0


# ---------------------------------------------------------------- 与参考实现对拍
def test_acoustic_vs_reference_200():
    rng = np.random.default_rng(52)
    for _ in range(200):
        lwa = float(rng.uniform(70, 100))
        bpf = float(rng.choice([110.0, 120.0, 150.0, 200.0, 220.0]))
        lw = RQ.lw_bands(lwa, bpf)
        mask = RQ.masking(bpf)
        bg = float(rng.uniform(20, 65))
        l_bg = [bg - 10.0 * math.log10(24.0)] * 24
        r = float(10 ** rng.uniform(0.5, 3.3))
        g = float(rng.uniform(0, 12))
        a = float(rng.uniform(0, 1))
        adv = float(rng.choice([0.0, 5.0]))
        lth = float(rng.choice([15.0, 20.0]))
        adj = list(rng.uniform(-6, 6, 24)) if rng.random() < 0.3 else None
        want = RQ.acoustic_q(lw, r, g, l_bg, mask, lth, a, adv, adj)
        got = acoustic_pair(np.array(lw), r, g, np.array(l_bg), np.array(mask), lth, a, adv, None if adj is None else np.array(adj))
        assert got.lp_dba == pytest.approx(want["lp_dba"], abs=0.1)
        assert got.margin_db == pytest.approx(want["margin_db"], abs=0.1)
        assert got.thr_dba == pytest.approx(want["thr_dba"], abs=0.1)
        assert got.p == pytest.approx(want["p"], abs=0.01)


def test_visual_and_radar_vs_reference_200():
    rng = np.random.default_rng(53)
    for _ in range(200):
        top, side = float(rng.uniform(0.05, 1.0)), float(rng.uniform(0.03, 0.4))
        csky, cgnd = float(rng.uniform(-0.9, -0.2)), float(rng.uniform(0.1, 0.5))
        band = str(rng.choice(["day", "low", "night"]))
        mor = float(10 ** rng.uniform(2.5, 4.5))
        r = float(10 ** rng.uniform(0.7, 3.2))
        e = float(rng.uniform(-1.2, 1.4))
        a = float(rng.uniform(0, 1))
        th0 = float(rng.uniform(0.5, 3))
        acu = float(rng.choice([1.0, 2.5, 4.0]))
        lit = bool(rng.integers(0, 2))
        sig = signature_from_doc("x", {"visual": {"area_m2": {"top": top, "side": side}, "contrast_sky": csky, "contrast_ground": cgnd}})
        sig = sig.with_lights(lit, False)
        q, ok = visual_pair(sig, r, e, env_of(band, mor=mor), th0, a, acu, los=True, in_fov=True)
        w = RQ.visual_q((top, side), (csky, cgnd), r, e, band, mor, th0, a, acu, lit=lit)
        for k in ("theta_arcmin", "n_eq_px", "c_app", "theta_th_arcmin", "ratio"):
            assert getattr(q, k) == pytest.approx(w[k], rel=0.01)
        assert q.p == pytest.approx(w["p"], abs=0.01)
        assert ok == w["ok"]
        rx = {"snr_ref_db": float(rng.uniform(0, 30)), "r_ref_m": float(rng.uniform(50, 1000)), "snr_req_db": float(rng.uniform(5, 20)),
              "fov_az_deg": 60.0, "fov_el_deg": 20.0}
        rcs, rain = float(rng.uniform(0.005, 1.0)), float(rng.choice([0.0, 5.0, 20.0]))
        rq = radar_pair(rx, rcs, r, rain, in_fov=True)
        wr = RQ.radar_q(rx["snr_ref_db"], rx["r_ref_m"], rx["snr_req_db"], rcs, r, rain)
        assert rq.snr_db == pytest.approx(wr["snr_db"], abs=0.1)
        assert rq.p == pytest.approx(wr["p"], abs=0.01)


# ---------------------------------------------------------------- 判据一致性（1000 组）
def _random_target(rng: np.random.Generator, mode: str) -> dict:
    cls = str(rng.choice(["person", "dog", "vehicle", "uav"]))
    d = {"class": cls, "pos": [float(rng.uniform(-50, 50)), float(rng.uniform(-50, 50)), None], "motion": {"mode": "static"},
         "criterion_mode": mode, "alertness": float(rng.uniform(0, 1)), "yaw_rad": float(rng.uniform(-3.1, 3.1))}
    bodies = [{"kind": "visual", "shape": str(rng.choice(["sector", "sphere", "hemisphere", "cylinder"])), "radius_max_m": float(rng.uniform(80, 900)),
               "half_angle_deg": float(rng.uniform(20, 179)), "azimuth_offset_deg": float(rng.uniform(-90, 90)), "z_max_m": 300.0, "z_min_m": -20.0}]
    if rng.random() < 0.8:
        bodies.append({"kind": "acoustic", "shape": "hemisphere", "radius_max_m": float(rng.uniform(80, 600)), "half_angle_deg": 180.0,
                       "gain_back_db": float(rng.choice([0.0, 6.0, 12.0])), "azimuth_offset_deg": float(rng.uniform(-90, 90))})
    if rng.random() < 0.3:
        d["radar_rx"] = {"snr_ref_db": 13.2, "r_ref_m": float(rng.uniform(150, 600)), "snr_req_db": 13.2, "fov_az_deg": float(rng.uniform(20, 90)),
                         "fov_el_deg": float(rng.uniform(10, 60))}
        bodies.append({"kind": "radar", "shape": "sphere", "radius_max_m": float(rng.uniform(100, 900))})
    if rng.random() < 0.3:
        d["hearing_band_adj_db"] = [float(x) for x in rng.uniform(-8, 8, 24)]
    if rng.random() < 0.3:
        d["visual_acuity_arcmin"] = float(rng.uniform(0.5, 6))
    d["sensitivity"] = bodies
    return d


@pytest.mark.parametrize("mode", ["physical", "geometric"])
def test_would_discover_matches_m18_criterion_1000_pairs(mode):
    from plib import FakeEnv, FakeWorld
    from tlib import Bench

    from awr.sim.targets.los_cache import LosCache

    rng = np.random.default_rng(1000 if mode == "physical" else 1001)
    world = FakeWorld(boxes=[(20.0, 60.0, -30.0, 30.0, 40.0), (-120.0, -80.0, 40.0, 90.0, 25.0)])
    n_uav = 5
    agree = total = hits_n = 0
    with Bench(n_uav, world, env=FakeEnv(band="day")) as b:
        rt = b.rt
        rt.los = LosCache(world, limit_per_s=1e9, burst=10 ** 9, quotas={})
        for trial in range(50):
            band = ("day", "low", "night")[trial % 3]
            b.ctx.env = FakeEnv(band=band, bg_dba=float(rng.uniform(25, 60)), mor_m=float(10 ** rng.uniform(3, 4.5)),
                                illum_lx=1e5 if band == "day" else (10.0 if band == "low" else 0.001))
            for tid in list(rt.ids()):
                b.cmd("target/remove", {"id": tid})
            tids = [b.add(_random_target(rng, mode)) for _ in range(4)]
            b.step(1)
            for i in range(n_uav):
                p = (float(rng.uniform(-700, 700)), float(rng.uniform(-700, 700)), float(rng.uniform(5, 250)))
                if rng.random() < 0.15:
                    b.S.pos[i] = p
                    b.S.land(i)
                else:
                    b.S.fly(i, p)
            if rng.random() < 0.3:
                rt.lights_fn = lambda vid: (vid.endswith("1"), False)
            else:
                rt.lights_fn = None
            rt._env = None
            env_t = rt.env(b.ctx)
            ids, P, _models, sigs = rt.participants(b.S, b.ctx)
            Ts = rt.ordered()
            IN, rows = rt._inside_all(Ts, P)
            t_ns = b.ctx.t_ns
            fleet = rt.fleet(b.S, b.ctx)
            uavs = [QuantUav(f.id, f.model_id, f.pos, f.participant, f.sig) for f in fleet]
            fenv = field_env(env_t)
            for k, T in enumerate(Ts):
                s0, s1, meta = rows.get(k, (0, 0, ()))
                hits, _deferred = rt._evaluate(T, ids, P, sigs, env_t, t_ns, IN[s0:s1], meta)
                v = rt.quant_view(T.id)
                qt = QuantTarget(v.id, v.cfg, v.pos, v.yaw_rad, v.h_m, v.alertness, v.dwell, v.t_dwell_s)

                def los_fn(a, bb, h, need, _t=t_ns):
                    hit = rt.los.peek(a, bb, "top", _t, h_m=h)
                    return (None, None) if hit is None else (bool(hit[0].k_los > 0.5), hit[1])

                pairs = quant_pairs(qt, uavs, fenv, los_fn=los_fn, limit=12)
                for q in pairs:
                    total += 1
                    want = q.uav in hits
                    hits_n += want
                    agree += want == q.would_discover
                    if q.would_discover and mode == "physical":
                        assert any(m is not None and m.in_body and m.p >= 0.5 for m in (q.acoustic, q.visual, q.radar)), q
            assert set(tids) == set(rt.ids())
    assert total == 1000
    assert agree == total, (agree, total)
    assert hits_n > 30            # 判据为真的对足够多（不是全部为假）


# ---------------------------------------------------------------- 组装与线格式
def _qt(**over) -> QuantTarget:
    d = {"class": "person", "motion": {"mode": "static"}, "criterion_mode": "physical", **over}
    cfg = TargetConfig.from_dict(d, "person-01")
    return QuantTarget("person-01", cfg, np.zeros(3), 0.0, 1.75, 0.5, {"uav-03": 12}, 2.1)


def _uavs(n: int, models=("p600_mid360", "awr_q9", "awr_f1")) -> list[QuantUav]:
    from awr.sim.targets.signature import signature_of

    rng = np.random.default_rng(7)
    out = []
    for i in range(n):
        p = np.array([float(rng.uniform(-400, 400)), float(rng.uniform(-400, 400)), float(rng.uniform(10, 150))])
        out.append(QuantUav(f"uav-{i + 1:02d}", models[i % len(models)], p, True, signature_of(models[i % len(models)])))
    return out


def test_pairs_sorted_limited_and_filtered():
    us = _uavs(15)
    pairs = quant_pairs(_qt(), us, env_of("day"), limit=12)
    assert len(pairs) == 12
    rs = [q.range_m for q in pairs]
    assert rs == sorted(rs)
    one = quant_pairs(_qt(), us, env_of("day"), uav_filter="uav-03")
    assert [q.uav for q in one] == ["uav-03"] and one[0].dwell_s == pytest.approx(1.1) and one[0].t_dwell_s == 2.1
    # machine：无视觉、无声学敏感体 → 两个模态为 None，P_any = 0
    m = quant_pairs(_qt(**{"class": "machine"}), us[:2], env_of("day"))
    assert all(q.acoustic is None and q.visual is None and q.p_any == 0.0 and not q.would_discover for q in m)


def test_soa_roundtrip_and_frame_size():
    us = _uavs(12)
    rev = {u.id: ReverseQ("eo", "R", 15.2, 14.0, 0.92, None) for u in us[::2]}
    rx = {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 60.0, "fov_el_deg": 30.0}
    qt = _qt(radar_rx=rx, sensitivity=[{"kind": "visual", "shape": "sector", "radius_max_m": 300}, {"kind": "acoustic", "shape": "hemisphere",
             "radius_max_m": 300}, {"kind": "radar", "shape": "sphere", "radius_max_m": 500}])
    pairs = quant_pairs(qt, us, env_of("day", mor=8000.0), los_fn=lambda a, b, h, need: (True, 0.4), reverse_fn=lambda u, t: rev.get(u))
    fr = quant_frame("person-01", "physical", 123, pairs)
    assert all("radar" in p and "reverse" in p for p in fr["pairs"])
    soa = encode_soa(fr)
    raw = msgpack.packb(soa, use_bin_type=True, use_single_float=True)
    assert len(raw) <= 1536, len(raw)                                   # M19-NFR-008：读数帧 ≤ 1.5 KB
    back = expand_soa(msgpack.unpackb(raw, raw=False))
    assert back == fr                                                    # 解码值与 REST 逐位相等
    # 无雷达接收、无反向感知时省略或为 null
    fr2 = quant_frame("person-01", "geometric", 1, quant_pairs(_qt(), us[:3], env_of("day")))
    assert all("radar" not in p and p["reverse"] is None for p in fr2["pairs"])
    assert expand_soa(msgpack.unpackb(msgpack.packb(encode_soa(fr2), use_single_float=True), raw=False)) == fr2


def test_batch_equals_scalar():
    """逐对组装用的批量计算与公开的单对纯函数逐项相等（同一公式）。"""
    from awr.sim.perception.quant import acoustic_batch, visual_batch
    from awr.sim.targets.signature import signature_of

    rng = np.random.default_rng(77)
    sigs = [signature_of(m) for m in ("p600_mid360", "awr_q9", "awr_f1", "awr_v1")] * 5
    sigs = [g.with_lights(bool(rng.integers(0, 2)), False) for g in sigs]
    k = len(sigs)
    r = 10 ** rng.uniform(0.3, 3.3, k)
    g = rng.uniform(0, 12, k)
    e = rng.uniform(-1.2, 1.4, k)
    inb = rng.random(k) > 0.5
    for band in ("day", "low", "night"):
        env = env_of(band, bg=float(rng.uniform(25, 60)), mor=float(10 ** rng.uniform(3, 4.5)))
        adj = rng.uniform(-5, 5, 24)
        b = acoustic_batch(np.array([s.lw_bands() for s in sigs]), r, g, env.l_bg_bands, np.array([s.masking_db() for s in sigs]), 20.0, 0.4, 0.0,
                           adj, env.l_bg_dba, inb)
        for i, s in enumerate(sigs):
            one = acoustic_pair(s.lw_bands(), float(r[i]), float(g[i]), env.l_bg_bands, s.masking_db(), 20.0, 0.4, 0.0, adj, l_bg_dba=env.l_bg_dba,
                                in_body=bool(inb[i]))
            for f in ("lp_dba", "lp_band_db", "thr_dba", "margin_db", "p"):
                assert getattr(b[i], f) == pytest.approx(getattr(one, f), abs=1e-9)
            assert b[i].band_hz == one.band_hz and b[i].in_body == one.in_body
        los = rng.random(k) > 0.2
        fov = rng.random(k) > 0.2
        vb, okb = visual_batch(sigs, r, e, env, 1.3, 0.4, 2.5, los, fov, inb)
        for i, s in enumerate(sigs):
            one, ok = visual_pair(s, float(r[i]), float(e[i]), env, 1.3, 0.4, 2.5, los=bool(los[i]), in_fov=bool(fov[i]), in_body=bool(inb[i]))
            assert bool(okb[i]) == ok
            for f in ("theta_arcmin", "d_vis_m", "n_eq_px", "theta_th_arcmin", "ratio", "c_app", "p"):
                assert getattr(vb[i], f) == pytest.approx(getattr(one, f), rel=1e-12, abs=1e-12)
            assert (vb[i].light_band, vb[i].lights_on, vb[i].in_fov, vb[i].in_body) == (one.light_band, one.lights_on, one.in_fov, one.in_body)
