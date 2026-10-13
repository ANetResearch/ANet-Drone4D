"""physical 声学准则与有效声学半径（M18-FR-012、FR-017；M18-M19 PRD §6.3.2、§6.3.3；AWR-04 §7.3；ISO 9613-2）。

逐 1/3 倍频程带（24 带，带表见 `sound.py`）：
    L_p,b = L_W,b(T) − 20·lg r − 11 − alpha_b·r/1000 − g_back,   g_back = gain_back_db·(1 − cos Δψ)/2
    可闻：任一带 L_p,b + A_b ≥ thr_b,   thr_b = max(L_th, L_bg,b + M_b) + 6·(1 − a) − adv
alpha_b 取 ISO 9613-2 表 2（20 °C、70% RH）的倍频程值 0.1、0.3、1.1、2.8、5.0、9.0、22.9、76.6 dB/km（63 Hz–8 kHz），按
lg alpha 对 log2 f 线性插值到 1/3 倍频程（两端线性外推）；`M_b` 纯音带 −4 dB、宽带 0 dB；`L_bg,b` 为 M07 给出的 A 计权带级。
有效半径：各带解等号（r ∈ [1, 5000] m 二分 40 次，单调递减），取最大后与 `radius_max` 取小。保守口径 a + 0.2、L_bg − 3 dB。
单带退化口径（只用 1 kHz 带、alpha = 5.0 dB/km、A = 0）见 `single_band_radius`（AWR-04 §7.3 全部算例）。判据不使用随机数（ADR-099）。

D2.1（M18-FR-029，AWR-04 §15.10.1）：识别物的类别频段修正 `hearing_band_adj_db[24]`（缺省全 0）加到 A 计权上：
`L_pw,b = L_p,b + A_b + adj_b`；`adj_db = None` 时与 D2 逐位一致（不做加法）。`acoustic_radii` 为按多个背向衰减（方位）向量化的
`acoustic_radius`（感知体表，M19-FR-033），逐方位结果与 `acoustic_radius` 相同。
"""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray

from .sound import A_W, BANDS_HZ

__all__ = ["ALPHA_DB_KM", "R_MAX_SOLVE", "acoustic_margin", "acoustic_radii", "acoustic_radius", "single_band_radius", "threshold_bands"]

_OCT_HZ = np.array([63.0, 125.0, 250.0, 500.0, 1000.0, 2000.0, 4000.0, 8000.0])
_OCT_ALPHA = np.array([0.1, 0.3, 1.1, 2.8, 5.0, 9.0, 22.9, 76.6])


def _alpha_bands() -> NDArray[np.float64]:
    x = np.log2(np.asarray(BANDS_HZ))
    xo, yo = np.log2(_OCT_HZ), np.log10(_OCT_ALPHA)
    y = np.interp(x, xo, yo)
    lo, hi = x < xo[0], x > xo[-1]
    y[lo] = yo[0] + (x[lo] - xo[0]) * (yo[1] - yo[0]) / (xo[1] - xo[0])
    y[hi] = yo[-1] + (x[hi] - xo[-1]) * (yo[-1] - yo[-2]) / (xo[-1] - xo[-2])
    return 10.0 ** y


ALPHA_DB_KM = _alpha_bands()
R_MIN_SOLVE = 1.0
R_MAX_SOLVE = 5000.0
N_B = len(A_W)


def threshold_bands(l_bg_bands: NDArray, masking_db: NDArray, hearing_th_dba: float, alertness: float, adv_db: float) -> NDArray:
    """thr_b（A 计权带级比较的门限）。"""
    return np.maximum(float(hearing_th_dba), np.asarray(l_bg_bands, float) + np.asarray(masking_db, float)) \
        + 6.0 * (1.0 - float(alertness)) - float(adv_db)


def acoustic_margin(lw_uav: NDArray, r_m: NDArray, g_back_db: NDArray, thr: NDArray, adj_db: NDArray | None = None) -> NDArray:
    """各机的最大带余量 `max_b(L_p,b + A_b + adj_b − thr_b)`（dB，≥ 0 即可闻）。lw_uav [n, 24]（未计权），r_m [n]，g_back_db [n]，
    thr [n, 24] 或 [24]；adj_db [24] 为识别物的类别频段修正（None 为 0，D2 口径）。"""
    r = np.maximum(np.asarray(r_m, float), 0.1)[:, None]
    lp = np.asarray(lw_uav, float) - 20.0 * np.log10(r) - 11.0 - ALPHA_DB_KM[None, :] * r / 1000.0 - np.asarray(g_back_db, float)[:, None]
    w = A_W if adj_db is None else A_W + np.asarray(adj_db, float)
    return np.max(lp + w[None, :] - np.asarray(thr, float), axis=1)


def acoustic_radius(lw_uav: NDArray, thr: NDArray, *, g_back_db: float = 0.0, iters: int = 40, adj_db: NDArray | None = None) -> float:
    """有效声学半径（m，未截断到 radius_max；各带解等号取最大，[1, 5000] 区间二分）。无带可闻时为 0。"""
    lw = np.asarray(lw_uav, float)
    th = np.asarray(thr, float)
    w = A_W if adj_db is None else A_W + np.asarray(adj_db, float)

    def f(r: NDArray) -> NDArray:
        return lw + w - 20.0 * np.log10(r) - 11.0 - ALPHA_DB_KM * r / 1000.0 - g_back_db - th

    lo = np.full(lw.shape, R_MIN_SOLVE)
    hi = np.full(lw.shape, R_MAX_SOLVE)
    ok_lo = f(lo) >= 0.0
    ok_hi = f(hi) >= 0.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        pos = f(mid) >= 0.0
        lo = np.where(pos, mid, lo)
        hi = np.where(pos, hi, mid)
    r = np.where(ok_hi, R_MAX_SOLVE, np.where(ok_lo, 0.5 * (lo + hi), 0.0))
    return float(np.max(r))


def acoustic_radii(lw_uav: NDArray, thr: NDArray, g_back_db: NDArray, *, iters: int = 40, adj_db: NDArray | None = None) -> NDArray:
    """`acoustic_radius` 对一组背向衰减 g_back_db [k] 的向量化版本（同一二分序列，逐项结果与标量版相同）。返回 [k]。"""
    lw = np.asarray(lw_uav, float)[None, :]
    th = np.asarray(thr, float)[None, :]
    g = np.asarray(g_back_db, float).reshape(-1, 1)
    w = (A_W if adj_db is None else A_W + np.asarray(adj_db, float))[None, :]
    k = g.shape[0]

    def f(r: NDArray) -> NDArray:
        return lw + w - 20.0 * np.log10(r) - 11.0 - ALPHA_DB_KM[None, :] * r / 1000.0 - g - th

    lo = np.full((k, N_B), R_MIN_SOLVE)
    hi = np.full((k, N_B), R_MAX_SOLVE)
    ok_lo = f(lo) >= 0.0
    ok_hi = f(hi) >= 0.0
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        pos = f(mid) >= 0.0
        lo = np.where(pos, mid, lo)
        hi = np.where(pos, hi, mid)
    r = np.where(ok_hi, R_MAX_SOLVE, np.where(ok_lo, 0.5 * (lo + hi), 0.0))
    return np.max(r, axis=1)


def single_band_radius(lwa_dba: float, alertness: float, hearing_th_dba: float, l_bg_dba: float, adv_db: float = 0.0, *,
                       alpha_db_km: float = 5.0, conservative: bool = False) -> float:
    """单带退化口径（1 kHz 带、alpha = 5.0 dB/km、A = 0，AWR-04 §7.3 算例）：解 `L_WA − 20 lg r − 11 − alpha r/1000 = thr`。"""
    a = min(1.2, alertness + 0.2) if conservative else alertness
    bg = l_bg_dba - 3.0 if conservative else l_bg_dba
    thr = max(hearing_th_dba, bg) + 6.0 * (1.0 - a) - adv_db
    lo, hi = R_MIN_SOLVE, R_MAX_SOLVE

    def f(r: float) -> float:
        return lwa_dba - 20.0 * math.log10(r) - 11.0 - alpha_db_km * r / 1000.0 - thr

    if f(lo) < 0.0:
        return 0.0
    if f(hi) >= 0.0:
        return hi
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if f(mid) >= 0.0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)
