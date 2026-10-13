"""M18-AC-023（D2-AC-52）：D2.1 识别物字段（M18-FR-028、FR-029、FR-031；AWR-04 §15.10.1）。

- `visual_acuity_arcmin`（0.3–20'）、`hearing_band_adj_db[24]`（−20 至 +20 dB）、`radar_rx`（五个字段，P1）、`rf_rx`（P2，只入 schema）
  的字段级校验（110）与 null 清除；类别缺省在使用处解析（不写入导出的配置，契约 1.2.0 合入前导出与 D2 逐字段相同）；
- 缺省 `hearing_band_adj_db` 时声学判据逐位不变（`acoustic_margin`、`acoustic_radius` 与不传修正时完全相等）；修正 +x dB 时余量
  增加 x dB、有效半径增大；`visual_acuity_arcmin` 不改变判据；
- radar 敏感体未配置 `radar_rx` 时只给 warning，配置后不再给 warning；共享视线缓存的只读 `peek` 不消耗令牌。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets import TargetConfig, effective_radius
from awr.sim.targets.acoustic import acoustic_margin, acoustic_radius, threshold_bands
from awr.sim.targets.envview import day_night
from awr.sim.targets.los_cache import LosCache
from awr.sim.targets.model import TargetError, merged_config, validate_config, visual_acuity_of
from awr.sim.targets.signature import P600


def _v(**over) -> list[str]:
    return validate_config(merged_config({"class": "person", **over}))


def test_new_fields_validate():
    assert _v(visual_acuity_arcmin=2.5) == []
    assert _v(hearing_band_adj_db=[0.0] * 24) == []
    assert _v(radar_rx={"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 60.0, "fov_el_deg": 20.0}) == []
    assert _v(rf_rx={"sens_dbm": -90.0, "f_mhz": 2400.0, "bw_mhz": 20.0}) == []
    bad = [
        ({"visual_acuity_arcmin": 0.1}, "visual_acuity_arcmin", "BELOW_MIN"),
        ({"visual_acuity_arcmin": 25.0}, "visual_acuity_arcmin", "ABOVE_MAX"),
        ({"hearing_band_adj_db": [0.0] * 23}, "hearing_band_adj_db", "BAD_LIST"),
        ({"hearing_band_adj_db": [0.0] * 23 + [25.0]}, "hearing_band_adj_db[23]", "ABOVE_MAX"),
        ({"radar_rx": {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 60.0}}, "radar_rx.fov_el_deg", "REQUIRED"),
        ({"radar_rx": {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 0.0, "fov_el_deg": 10.0}}, "radar_rx.fov_az_deg",
         "BELOW_MIN"),
        ({"radar_rx": {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 30.0, "fov_el_deg": 10.0, "x": 1}}, "radar_rx.x",
         "UNKNOWN_FIELD"),
        ({"rf_rx": {"sens_dbm": 5.0}}, "rf_rx.sens_dbm", "ABOVE_MAX"),
    ]
    for over, field, why in bad:
        with pytest.raises(TargetError) as ei:
            _v(**over)
        assert ei.value.code == 110 and ei.value.detail["field"] == field and ei.value.detail["why"] == why, (over, ei.value.detail)


def test_defaults_resolved_at_use_not_exported():
    with Bench(1, FakeWorld()) as b:
        tid = b.add(static_person())
        cfg = b.rt.cfg_dict(b.T(tid))
        for k in ("visual_acuity_arcmin", "hearing_band_adj_db", "radar_rx", "rf_rx"):
            assert k not in cfg
        assert visual_acuity_of(b.T(tid).cfg.raw) == 1.0
        r = b.cmd("target/update", {"id": tid, "patch": {"visual_acuity_arcmin": 3.0, "hearing_band_adj_db": [1.0] * 24}})
        assert r["status"] == "accepted", r
        assert b.T(tid).cfg.raw["visual_acuity_arcmin"] == 3.0
        r = b.cmd("target/update", {"id": tid, "patch": {"hearing_band_adj_db": None}})
        assert r["status"] == "accepted" and b.T(tid).cfg.raw.get("hearing_band_adj_db") is None


def test_band_adjustment_default_is_bitwise_d2():
    rng = np.random.default_rng(23)
    _d_env, n_env = day_night("rural", math.inf)
    for _ in range(50):
        lw = np.array([P600.lw_bands()] * 3)
        r = rng.uniform(5, 800, 3)
        g = rng.uniform(0, 10, 3)
        thr = threshold_bands(n_env.l_bg_bands, P600.masking_db(), 20.0, float(rng.uniform(0, 1)), 0.0)
        a = acoustic_margin(lw, r, g, thr)
        b = acoustic_margin(lw, r, g, thr, None)
        assert np.array_equal(a, b)
        assert acoustic_radius(P600.lw_bands(), thr) == acoustic_radius(P600.lw_bands(), thr, adj_db=None)
        up = acoustic_margin(lw, r, g, thr, np.full(24, 7.0))
        assert np.allclose(up, a + 7.0)
    c0 = TargetConfig.from_dict({"class": "person"})
    c1 = TargetConfig.from_dict({"class": "person", "hearing_band_adj_db": [6.0] * 24})
    r0 = effective_radius(c0, 1, P600, n_env, conservative=False, truncate=False)
    r1 = effective_radius(c1, 1, P600, n_env, conservative=False, truncate=False)
    assert r1 > r0 * 1.2


def test_acuity_does_not_change_criterion():
    with Bench(1, FakeWorld()) as b:
        a = b.add(static_person(pos=[0.0, 0.0, None], criterion_mode="physical"))
        c = b.add(static_person(pos=[0.0, 0.0, None], criterion_mode="physical", visual_acuity_arcmin=15.0))
        b.S.fly(0, (120.0, 0.0, 30.0))
        b.run(5.0)
        ea = [e for e in b.events.of("target.discovered") if e["data"]["target"] == a]
        ec = [e for e in b.events.of("target.discovered") if e["data"]["target"] == c]
        assert ea and len(ea) == len(ec) and ea[0]["t_sim_ns"] == ec[0]["t_sim_ns"]


def test_radar_body_warning_only_without_rx():
    body = {"kind": "radar", "shape": "sphere", "radius_max_m": 300}
    assert _v(sensitivity=[body]) == ["SENSITIVITY_KIND_STORED_ONLY"]
    rx = {"snr_ref_db": 13.2, "r_ref_m": 300.0, "snr_req_db": 13.2, "fov_az_deg": 60.0, "fov_el_deg": 20.0}
    assert _v(sensitivity=[body], radar_rx=rx) == []
    assert _v(sensitivity=[{"kind": "rf", "shape": "sphere", "radius_max_m": 300}], radar_rx=rx) == ["SENSITIVITY_KIND_STORED_ONLY"]


def test_los_peek_is_read_only():
    w = FakeWorld()
    c = LosCache(w)
    a, b = np.array([100.0, 0.0, 50.0]), np.array([0.0, 0.0, 0.0])
    assert c.peek(a, b, "top", 0) is None
    r = c.get(a, b, 1, "M18", 0, "top", h_m=1.75)
    tok = c.total.tokens
    st = dict(c.stats)
    hit = c.peek(a, b, "top", 500_000_000, h_m=1.75)
    assert hit is not None and hit[0].k_los == r.k_los and not hit[0].stale and hit[1] == pytest.approx(0.5)
    assert c.total.tokens == tok and c.stats == st
    old = c.peek(a, b, "top", 1_500_000_000)
    assert old is not None and old[0].stale
    assert c.peek(a, b, "top", 2_500_000_000) is None
    assert c.peek(a, b, "center", 0) is None                             # 中心射线未算过
