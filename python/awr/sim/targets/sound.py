"""周期声源：四种波形到 24 个 1/3 倍频程带的映射、A 计权归一与开关门控（M18-FR-008；M18-M19 PRD §6.5.2；AWR-04 §7.5）。

带表与 M07 声学背景、M21 机型谱形同一张（50 Hz–10 kHz，带 b 的精确中心 `f_b = 1000·10^((b − 13)/10)`，边界 `f_b·10^(±0.05)`，
AWR-20 §4.4）；A 计权取 AWR-20 §4.4 的 IEC 61672 表（0.1 dB 取整，与 M21 谱形归一化同一口径）。

谱形 s[b]（dB，未计权）：
- tone：f0 所在带 0 dB，其余 −60 dB；
- harmonic、pulse_train：第 k 次谐波 k·f0（k ≤ len(harmonics_db) ≤ 8，且 ≤ 10 kHz）落入的带累加 `harmonics_db[k−1]`（能量相加），
  其余带 −60 dB；
- noise_band：[f0 − bw/2, f0 + bw/2] 覆盖的带按重叠频宽比例均分能量，其余带 −60 dB。
归一：`L_W[b] = s[b] + (lw_dba − 10·lg Σ_b 10^((s[b] + A[b])/10))`，A 计权合计恰为 `lw_dba`。
门控：`on = ((t − phase_s) mod period_s) < duty·period_s`；关闭相位 `L_W[b] −= 40`（AWR-04 §7.5）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = ["A_W", "BANDS_HZ", "EXACT_HZ", "N_BANDS", "OFF_DB", "SoundBands", "band_of", "band_spectrum", "effective_sound", "gate_on",
           "lw_bands_now", "sound_phase", "total_a"]

N_BANDS = 24
BANDS_HZ: tuple[float, ...] = (50.0, 63.0, 80.0, 100.0, 125.0, 160.0, 200.0, 250.0, 315.0, 400.0, 500.0, 630.0, 800.0, 1000.0, 1250.0,
                               1600.0, 2000.0, 2500.0, 3150.0, 4000.0, 5000.0, 6300.0, 8000.0, 10000.0)
EXACT_HZ = np.array([1000.0 * 10.0 ** ((b - 13) / 10.0) for b in range(N_BANDS)])
EDGE_LO = EXACT_HZ * 10.0 ** -0.05
EDGE_HI = EXACT_HZ * 10.0 ** 0.05
A_W = np.array([-30.2, -26.2, -22.5, -19.1, -16.1, -13.4, -10.9, -8.6, -6.6, -4.8, -3.2, -1.9, -0.8, 0.0, 0.6, 1.0, 1.2, 1.3, 1.2, 1.0,
                0.5, -0.1, -1.1, -2.5])
FLOOR_DB = -60.0
OFF_DB = 40.0


def band_of(f_hz: float) -> int:
    """频率所在带（−1 为不在 24 带内）。"""
    if not (EDGE_LO[0] <= f_hz < EDGE_HI[-1]):
        return -1
    b = int(np.searchsorted(EDGE_HI, f_hz, side="right"))
    return b if b < N_BANDS else -1


def total_a(lw_bands: NDArray[np.float64]) -> float:
    """A 计权总声功率级（dB(A)）。"""
    return float(10.0 * np.log10(np.sum(10.0 ** ((np.asarray(lw_bands, float) + A_W) / 10.0))))


@dataclass(frozen=True)
class SoundBands:
    lw_db: NDArray[np.float64]      # [24] 开启相位的未计权带声功率级 L_W[b]
    any_in_band: bool               # 至少一个分量落在 24 带内（否则谱形退化为 −60 dB 平谱，541 NO_BAND_IN_RANGE）


def band_spectrum(s: dict) -> SoundBands:
    lin = np.full(N_BANDS, 10.0 ** (FLOOR_DB / 10.0))
    wf = s.get("waveform")
    f0 = float(s["f0_hz"])
    hit = False
    if wf == "tone":
        b = band_of(f0)
        if b >= 0:
            lin[b] = 1.0
            hit = True
    elif wf in ("harmonic", "pulse_train"):
        h = s.get("harmonics_db")
        h = [0.0, -6.0, -10.0, -14.0] if h is None else list(h)
        acc = np.zeros(N_BANDS)
        for k, rel in enumerate(h[:8], start=1):
            fk = k * f0
            if fk > 10000.0 * 10.0 ** 0.05:
                break
            b = band_of(fk)
            if b >= 0:
                acc[b] += 10.0 ** (float(rel) / 10.0)
                hit = True
        lin = np.where(acc > 0.0, acc, lin)
    elif wf == "noise_band":
        bw = float(s.get("bandwidth_hz") or 0.0)
        lo, hi = f0 - 0.5 * bw, f0 + 0.5 * bw
        ov = np.clip(np.minimum(hi, EDGE_HI) - np.maximum(lo, EDGE_LO), 0.0, None)
        tot = float(ov.sum())
        if tot > 0.0:
            lin = np.where(ov > 0.0, ov / tot, lin)
            hit = True
    rel_db = 10.0 * np.log10(lin)
    c = float(s["lw_dba"]) - 10.0 * math.log10(float(np.sum(10.0 ** ((rel_db + A_W) / 10.0))))
    return SoundBands(rel_db + c, hit)


def effective_sound(s: dict | None, override: dict | None) -> dict | None:
    """emit 行为的临时覆盖（`duty`、`lw_dba`、`period_s`）合并到声源参数；无声源时以覆盖为准的缺省声源不生成（返回 None）。"""
    if s is None:
        return None
    if not override:
        return s
    out = dict(s)
    for k in ("duty", "lw_dba", "period_s"):
        if override.get(k) is not None:
            out[k] = float(override[k])
    if float(out.get("phase_s", 0.0)) >= float(out["period_s"]):
        out["phase_s"] = 0.0
    return out


def sound_phase(s: dict, t_s: float) -> float:
    """当前相位（s，[0, period)）。"""
    per = float(s["period_s"])
    return float((t_s - float(s.get("phase_s", 0.0))) % per)


def gate_on(s: dict, t_s: float) -> bool:
    duty = float(s["duty"])
    if duty >= 1.0:
        return True
    if duty <= 0.0:
        return False
    return sound_phase(s, t_s) < duty * float(s["period_s"])


def lw_bands_now(s: dict | None, t_s: float, bands: SoundBands | None = None) -> NDArray[np.float64] | None:
    """t 时刻的 L_W[b]（关闭相位 −40 dB）；无声源为 None。"""
    if s is None:
        return None
    sb = bands or band_spectrum(s)
    return sb.lw_db if gate_on(s, t_s) else sb.lw_db - OFF_DB
