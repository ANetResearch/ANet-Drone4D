"""M19-AC-007：雷达基础与引导。SNR 符合 R⁻⁴（人 0.5 m² 151 m、车 10 m² 320 m，±1%）；视场外不检；雨 10 mm/h 时检测距离按
gamma_rain 缩短（AWR-20 §6.5：人 151 → 约 139 m）；航迹不含识别物 id；D 级检测在 ≤ 2 s 内把 cue 开启机的云台转向识别物（M13
`cue_point`）；cue 关闭或已明确捕获时不引导；Swerling 1 起伏取计数器 RNG（同键同值）。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from plib import FakeEnv, FakeWorld, PBench, make_imaging, make_nonimg

from awr.sim.perception import gx40, mmw77
from awr.sim.perception.nonimaging import radar_range_m, radar_snr_db


def test_r4_law_and_anchors():
    sp = mmw77()
    assert radar_range_m(sp, 0.5) == pytest.approx(151.4, rel=0.01)
    assert radar_range_m(sp, 10.0) == pytest.approx(320.1, rel=0.01)
    assert radar_range_m(sp, 1.0) == pytest.approx(180.0)
    r = np.array([90.0, 180.0, 360.0])
    snr = radar_snr_db(sp, 1.0, r)
    assert np.allclose(np.diff(snr), -40 * math.log10(2.0))
    rain = radar_range_m(sp, 0.5, 10.0)
    assert 135.0 <= rain <= 142.0 and rain < radar_range_m(sp, 0.5)


class _M13:
    def __init__(self) -> None:
        self.cues: list[tuple[int, list[float]]] = []

    def cue_point(self, slot, p, gimbal_id=None):
        self.cues.append((int(slot), [float(x) for x in p]))
        return True

    def cue(self, slot, az, el, rng, src=None):
        self.cues.append((int(slot), [az, el]))
        return True


def _bench(target_xy=(120.0, 0.0), cls="person", cue=1, env=None, imaging=True):
    b = PBench(1, FakeWorld(), env=env)
    tid = b.add({"class": cls, "pos": [target_xy[0], target_xy[1], 0.0], "motion": {"mode": "static"}})
    b.step(1)
    p = (0.0, 0.0, 30.0)
    img = [make_imaging(gx40(), p, (0.0, -300.0, 0.0), f_mm=48.0, src="default")] if imaging else []
    b.platform(0, pos=p, imaging=img, nonimg=[make_nonimg("radar", mmw77(), p, name="radar")], cue=cue)
    m = _M13()
    b.prt.adapter.runtime = lambda: m  # type: ignore[method-assign]
    return b, tid, m


def test_detect_tracks_without_truth_id_and_cue():
    b, tid, m = _bench()
    with b:
        b.run(2.0)
        out = b.prt.radar_last["uav-01"]
        assert out["tracks"] and all("target" not in t for t in out["tracks"])
        t = out["tracks"][0]
        assert t["level"] == "D" and t["snr_db"] >= 13.2 and abs(t["range_m"] - math.hypot(120.0, 29.125)) < 1.0
        assert abs(t["az_rad"]) < math.radians(5.0)
        det = b.ev("perception.detect")
        assert det and det[0]["data"]["sensor_kind"] == "radar" and "target" not in det[0]["data"]
        assert m.cues and m.cues[0][0] == 0
        first_cue_t = b.prt.cue_t[1]
        assert first_cue_t <= det[0]["t_sim_ns"] * 1e-9 + 2.0
        assert np.linalg.norm(np.array(m.cues[0][1][:2]) - np.array([120.0, 0.0])) < 5.0
        ring = [r for r in b.prt.ring.items if r["kind"] == "detect"]
        assert ring and ring[0]["target"] is None and ring[0]["track_id"] == t["track_id"]
        assert b.prt.track(tid) is not None


def test_out_of_fov_and_range_not_detected():
    b, _tid, _m = _bench(target_xy=(0.0, 150.0))        # 方位 90°：视场 ±60° 外
    with b:
        b.run(1.5)
        assert b.prt.radar_last["uav-01"]["tracks"] == []
    b2, _t2, _m2 = _bench(target_xy=(200.0, 0.0))        # 人 151 m 之外
    with b2:
        b2.run(1.5)
        assert b2.prt.radar_last["uav-01"]["tracks"] == []


def test_rain_shortens_detection():
    b, _tid, _m = _bench(target_xy=(140.0, 0.0), env=FakeEnv(rain_mmph=10.0))
    with b:
        b.run(1.5)
        assert b.prt.radar_last["uav-01"]["tracks"] == []      # 雨 10 mm/h：约 139 m
    b2, _t, _m2 = _bench(target_xy=(140.0, 0.0), env=FakeEnv())
    with b2:
        b2.run(1.5)
        assert b2.prt.radar_last["uav-01"]["tracks"]


def test_cue_off_and_captured_do_not_cue():
    b, _tid, m = _bench(cue=2)
    with b:
        b.run(2.0)
        assert m.cues == [] and b.prt.radar_last["uav-01"]["tracks"]
    b2, _t, m2 = _bench(cue=0)                                  # 未设：缺省 off（M20 set_cue 打开）
    with b2:
        b2.run(1.0)
        assert m2.cues == []
        b2.prt.set_cue("uav-01", True)
        b2.run(1.0)
        assert m2.cues


def test_swerling1_draws_are_keyed():
    b, _tid, _m = _bench(cls="uav", target_xy=(30.0, 0.0))     # uav 模板 swerling1，0.01 m²
    with b:
        b.run(1.0)
        a = b.prt._uniform(b.ctx, 1, 5, 0, 7)
        assert a == b.prt._uniform(b.ctx, 1, 5, 0, 7) and 0.0 < a < 1.0
