"""M19-AC-009：声阵列与 MID-360。声阵列带 SNR 与自噪声抑制（多旋翼 25 dB、固定翼 35 dB）与 AWR-20 §6.6 参考检测距离对拍（狗
P600 113 m、Q9 67 m、F1 998 m；人 27、13、74 m；按缺省谐波 [0, −6, −10, −14] 的口径，±3%）；输出方位精度 sigma ≈ 5°（1000 次统计
4.5–5.5°）；遮挡只读缓存中的中心射线（不发射新射线，屏障 10 dB）；P1 谐波 R 级；MID-360 人量程 58.1 m、点数判据 D/R（42.9 m、
13.6 m）与解析值一致；量程按 rho_905·tau² 缩放。"""

from __future__ import annotations

import math

import numpy as np
import pytest
from plib import FakeEnv, FakeWorld, PBench, make_nonimg

from awr.sim.perception import mic8, mid360
from awr.sim.perception.nonimaging import acoustic_snr_bands, harmonic_bands, lidar_points, lidar_r_max
from awr.sim.targets.model import class_defaults
from awr.sim.targets.signature import signature_of
from awr.sim.targets.sound import band_spectrum


@pytest.fixture(scope="module")
def profiles():
    from awr.sim.fleet.profiles import ProfileTable

    return ProfileTable()


def _range(lw, ego, frame, bg):
    lo, hi = 1.0, 5000.0
    for _ in range(60):
        m = 0.5 * (lo + hi)
        if np.max(acoustic_snr_bands(mic8(), lw, m, ego, bg, frame=frame)) >= 6.0:
            lo = m
        else:
            hi = m
    return lo


@pytest.mark.parametrize(("cls", "model", "frame", "want"), [
    ("dog", "p600_mid360", "multirotor", 113.0), ("dog", "awr_q9", "multirotor", 67.0), ("dog", "awr_f1", "fixed_wing", 998.0),
    ("person", "p600_mid360", "multirotor", 27.0), ("person", "awr_q9", "multirotor", 13.0), ("person", "awr_f1", "fixed_wing", 74.0),
])
def test_acoustic_reference_ranges(profiles, cls, model, frame, want):
    s = dict(class_defaults()["classes"][cls]["sound"])
    s["harmonics_db"] = [0.0, -6.0, -10.0, -14.0]          # AWR-20 §6.6 表的口径
    bg = np.full(24, 30.0 - 10.0 * math.log10(24.0))
    r = _range(band_spectrum(s).lw_db, signature_of(model, profiles).lw_bands(), frame, bg)
    assert abs(r - want) <= max(0.03 * want, 0.5)            # 表值取整到米


def test_self_noise_suppression_and_barrier(profiles):
    lw = band_spectrum(dict(class_defaults()["classes"]["dog"]["sound"])).lw_db
    ego = signature_of("p600_mid360", profiles).lw_bands()
    bg = np.full(24, 10.0)
    mr = acoustic_snr_bands(mic8(), lw, 100.0, ego, bg, frame="multirotor")
    fw = acoustic_snr_bands(mic8(), lw, 100.0, ego, bg, frame="fixed_wing")
    assert np.nanmax(fw) > np.nanmax(mr)
    blocked = acoustic_snr_bands(mic8(), lw, 100.0, ego, bg, frame="multirotor", blocked=True)
    assert np.nanmax(mr) - np.nanmax(blocked) == pytest.approx(10.0)
    assert np.isneginf(mr[0]) and np.isneginf(mr[23])                 # 50 Hz 与 10 kHz 不在 100 Hz–8 kHz 内
    assert harmonic_bands(200.0)[6] and harmonic_bands(200.0)[9] and not harmonic_bands(200.0)[7]


def _ac_bench(n_targets=1, xy=(60.0, 0.0), cls="dog", lw_bands=None, world=None):
    b = PBench(1, world or FakeWorld(), env=FakeEnv(bg_dba=30.0))
    for k in range(n_targets):
        b.add({"class": cls, "pos": [xy[0], xy[1] + 3.0 * k, 0.0], "motion": {"mode": "static"},
               "sound": {"waveform": "harmonic", "f0_hz": 600.0, "lw_dba": 100.0, "period_s": 1.0, "duty": 1.0,
                                                                         "harmonics_db": [0.0, -6.0, -10.0, -14.0]}})
    b.step(1)
    p = (0.0, 0.0, 30.0)
    b.platform(0, pos=p, nonimg=[make_nonimg("acoustic", mic8(), p, name="mic")], lw_bands=lw_bands if lw_bands is not None else np.full(24, 60.0))
    return b


def test_acoustic_pipeline_bearing_sigma_and_harmonic_r_level():
    b = _ac_bench()
    with b:
        errs = []
        for _ in range(400):
            b.run(0.5)
            out = b.prt.acoustic_last["uav-01"]
            assert out["bearings"] and "target" not in out["bearings"][0]
            errs.append(out["bearings"][0]["az_rad"] - 0.0)
        sd = math.degrees(float(np.std(errs)))
        assert 4.5 <= sd <= 5.5
        bt = b.prt.acoustic_last["uav-01"]["bearings"][0]
        assert bt["level"] == "R" and bt["harmonic_f0_hz"] == 600.0 and all(x["snr_db"] >= 0 for x in bt["bands"])
        assert b.prt.acoustic_last["uav-01"]["ego_noise_dba"] is not None
        assert any(e["data"]["sensor_kind"] == "acoustic" for e in b.ev("perception.detect"))
        assert b.prt.los.rays_by_owner.get("M19", 0) == 0                 # 声阵列不发射射线


def test_bearing_sigma_over_1000_draws():
    b = _ac_bench(n_targets=5)
    with b:
        errs = []
        for _ in range(200):
            b.run(0.5)
            for bt in b.prt.acoustic_last["uav-01"]["bearings"]:
                k = bt["bearing_id"]
                errs.append(bt["az_rad"] - math.atan2(3.0 * k, 60.0))
        assert len(errs) >= 1000
        assert 4.5 <= math.degrees(float(np.std(errs))) <= 5.5


def test_mid360_closed_forms():
    sp = mid360()
    assert lidar_r_max(sp, 0.4) == pytest.approx(58.1, abs=0.1)
    assert lidar_r_max(sp, 0.1) == pytest.approx(40.0) and lidar_r_max(sp, 100.0) == 100.0
    assert math.sqrt(3500 * 0.525 * 10 / 10) == pytest.approx(42.9, abs=0.1)
    assert lidar_points(sp, 0.525, 42.87) == pytest.approx(10.0, abs=0.05)
    assert lidar_points(sp, 0.525, 13.55) == pytest.approx(100.0, abs=0.5)


def test_mid360_pipeline_levels():
    b = PBench(1, FakeWorld())
    with b:
        b.add({"class": "person", "pos": [10.0, 0.0, 0.0], "motion": {"mode": "static"}})
        b.add({"class": "person", "pos": [30.0, 5.0, 0.0], "motion": {"mode": "static"}})
        b.add({"class": "person", "pos": [70.0, 0.0, 0.0], "motion": {"mode": "static"}})
        b.step(1)
        p = (0.0, 0.0, 2.0)
        b.platform(0, pos=p, nonimg=[make_nonimg("lidar", mid360(), p, name="mid360")])
        b.run(1.0)
        rows = {r["target"]: r for r in b.prt.lidar_last["uav-01"]["n_points_target"]}
        assert rows["person-01"]["level"] == "R" and rows["person-02"]["level"] == "D"
        assert "person-03" not in rows                                      # 70 m > 58.1 m 量程
        assert rows["person-01"]["n_1s"] > 100 and 10 <= rows["person-02"]["n_1s"] < 100
