"""周期声源（M18-AC-007；M18-FR-008；AWR-04 §7.5）：四种波形的 A 计权合计 = lw_dba（±0.1 dB）；谐波落带与噪声带均分；
周期与占空在 10 Hz stage 中误差 ≤ 1 个 stage 周期；关闭相位 −40 dB；541 校验；emit 行为临时覆盖占空。"""

from __future__ import annotations

import numpy as np
import pytest
from tlib import Bench, FakeWorld, static_person

from awr.sim.targets.model import TargetError, validate_sound
from awr.sim.targets.sound import BANDS_HZ, OFF_DB, band_of, band_spectrum, gate_on, lw_bands_now, total_a

WAVES = [
    {"waveform": "tone", "f0_hz": 1000.0, "lw_dba": 60.0, "period_s": 1.0, "duty": 1.0},
    {"waveform": "harmonic", "f0_hz": 220.0, "harmonics_db": [0, -6, -10, -14], "lw_dba": 83.0, "period_s": 1.0, "duty": 1.0},
    {"waveform": "pulse_train", "f0_hz": 600.0, "harmonics_db": [0], "lw_dba": 100.0, "period_s": 6.0, "duty": 0.15},
    {"waveform": "noise_band", "f0_hz": 2000.0, "bandwidth_hz": 1500.0, "lw_dba": 75.0, "period_s": 2.0, "duty": 0.5},
]


@pytest.mark.parametrize("s", WAVES, ids=[w["waveform"] for w in WAVES])
def test_a_weighted_total_equals_lw(s) -> None:
    sb = band_spectrum(s)
    assert abs(total_a(sb.lw_db) - s["lw_dba"]) <= 0.1
    assert sb.any_in_band


def test_band_mapping() -> None:
    assert BANDS_HZ[band_of(1000.0)] == 1000.0 and BANDS_HZ[band_of(220.0)] == 200.0 and band_of(30.0) == -1
    sb = band_spectrum(WAVES[1]).lw_db
    top = np.argsort(sb)[::-1][:4]
    assert {BANDS_HZ[b] for b in top} == {200.0, 400.0, 630.0, 800.0}   # 220、440、660、880 Hz 落带
    nb = band_spectrum(WAVES[3]).lw_db
    covered = [b for b in range(24) if nb[b] > nb.max() - 30]
    assert covered and all(1000.0 <= BANDS_HZ[b] <= 3150.0 for b in covered)


def test_gate_and_off_phase() -> None:
    s = dict(WAVES[2], phase_s=0.0)
    assert gate_on(s, 0.0) and gate_on(s, 0.85) and not gate_on(s, 0.95) and gate_on(s, 6.05)
    on = lw_bands_now(s, 0.1)
    off = lw_bands_now(s, 3.0)
    assert np.allclose(on - off, OFF_DB)
    assert gate_on(dict(s, duty=1.0), 5.99) and not gate_on(dict(s, duty=0.0), 0.0)


@pytest.mark.parametrize(("patch", "field"), [
    ({"f0_hz": 5}, "sound.f0_hz"), ({"lw_dba": 150}, "sound.lw_dba"), ({"period_s": 4000}, "sound.period_s"), ({"duty": 1.2}, "sound.duty"),
    ({"harmonics_db": [0, -70]}, "sound.harmonics_db[1]"), ({"harmonics_db": [0] * 9}, "sound.harmonics_db"),
    ({"waveform": "noise_band"}, "sound.bandwidth_hz"), ({"phase_s": 8.0}, "sound.phase_s"), ({"waveform": "chirp"}, "sound.waveform"),
])
def test_validation_541(patch, field) -> None:
    with pytest.raises(TargetError) as ei:
        validate_sound({**WAVES[2], **patch})
    assert ei.value.code == 541 and ei.value.detail["field"] == field


def test_phase_in_stage_period_and_duty() -> None:
    """10 Hz stage 中开启相位的起止时刻与参数一致（误差 ≤ 1 个 stage 周期 0.1 s）。"""
    with Bench(0, FakeWorld()) as b:
        tid = b.add(static_person(sound={"waveform": "pulse_train", "f0_hz": 200, "lw_dba": 70, "period_s": 4.0, "duty": 0.25,
                                         "phase_s": 1.0}))
        on_t = []
        prev = None
        for _ in range(120):
            b.step(1)
            cur = bool(b.col("tgt_sound_on", tid))
            if cur and prev is False:
                on_t.append(b.t)
            prev = cur
        assert len(on_t) >= 2
        assert all(abs((t - 1.0) % 4.0) <= 0.1 + 1e-9 or abs(((t - 1.0) % 4.0) - 4.0) <= 0.1 + 1e-9 for t in on_t)
        assert abs(np.diff(on_t).mean() - 4.0) <= 0.1
        det = b.rt.detail(b.T(tid), b.ctx.t_ns, b.rt.env(b.ctx))
        assert det["sound"]["lw_dba_now"] in (70.0, 30.0)


def test_emit_override_raises_duty() -> None:
    """狗被发现后 emit：占空升至 0.6，持续 30 s 后恢复 0.15。"""
    with Bench(1, FakeWorld()) as b:
        tid = b.add({"class": "dog", "pos": [0, 0], "motion": {"mode": "static"}})
        b.S.fly(0, [10.0, 0.0, 20.0])
        b.run(3.0)
        T = b.T(tid)
        assert T.aware in (3, 4) and T.emit_over == {"duty": 0.6}
        on = 0
        for _ in range(60):
            b.step(1)
            on += int(b.col("tgt_sound_on", tid))
        assert abs(on / 60 - 0.6) <= 0.1
        b.S.land(0)
        b.run(31.0)
        assert T.emit_over is None
