"""physical 声学判据的独立参考实现（M18-FR-012、M18-AC-011；纯 Python 循环，只用 math；不 import awr）。

- 带表：24 个 1/3 倍频程带，名义中心 50 Hz–10 kHz，精确中心 `1000·10^((b − 13)/10)`，边界 ±1/20 decade；
- A 计权：AWR-20 §4.4 表（0.1 dB）；
- 机型谱形：AWR-20 §4.4 `default_band_shape`（逐项按文中伪代码重写）；
- 大气吸收：ISO 9613-2 表 2 倍频程值（20 °C、70% RH），lg alpha 对 log2 f 分段线性插值，两端用端段斜率外推；
- 判据：`L_p,b = L_W,b − 20 lg r − 11 − alpha_b r/1000 − g`，`thr_b = max(L_th, L_bg,b + M_b) + 6(1 − a) − adv`，余量 = max_b(L_p,b + A_b − thr_b)。
"""

from __future__ import annotations

import math

NOMINAL = [50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000]
A_W = [-30.2, -26.2, -22.5, -19.1, -16.1, -13.4, -10.9, -8.6, -6.6, -4.8, -3.2, -1.9, -0.8, 0.0, 0.6, 1.0, 1.2, 1.3, 1.2, 1.0, 0.5, -0.1, -1.1,
       -2.5]
OCT = [(63, 0.1), (125, 0.3), (250, 1.1), (500, 2.8), (1000, 5.0), (2000, 9.0), (4000, 22.9), (8000, 76.6)]


def exact(b: int) -> float:
    return 1000.0 * 10.0 ** ((b - 13) / 10.0)


def band_index(f: float) -> int:
    for b in range(24):
        lo, hi = exact(b) * 10 ** -0.05, exact(b) * 10 ** 0.05
        if lo <= f < hi:
            return b
    return -1


def alpha(b: int) -> float:
    x = math.log2(NOMINAL[b])
    pts = [(math.log2(f), math.log10(a)) for f, a in OCT]
    if x <= pts[0][0]:
        (x0, y0), (x1, y1) = pts[0], pts[1]
    elif x >= pts[-1][0]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
    else:
        k = next(i for i in range(len(pts) - 1) if pts[i][0] <= x <= pts[i + 1][0])
        (x0, y0), (x1, y1) = pts[k], pts[k + 1]
    return 10.0 ** (y0 + (x - x0) * (y1 - y0) / (x1 - x0))


def band_shape(bpf: float, tonal: float, f_pk: float) -> list[float]:
    B = [-((math.log2(exact(b) / f_pk)) ** 2) for b in range(24)]
    lin = [10.0 ** (x / 10.0) for x in B]
    for h in range(1, 6):
        b = band_index(h * bpf)
        if b >= 0:
            lin[b] += 10.0 ** ((B[b] + tonal - 3.0 * (h - 1)) / 10.0)
    S = [10.0 * math.log10(v) for v in lin]
    c = -10.0 * math.log10(sum(10.0 ** ((S[b] + A_W[b]) / 10.0) for b in range(24)))
    return [s + c for s in S]


def tonal_bands(bpf: float) -> set[int]:
    return {band_index(h * bpf) for h in range(1, 6)} - {-1}


def margin_db(lwa: float, bpf: float, tonal: float, f_pk: float, r: float, g: float, l_bg_bands: list[float], hearing: float, a: float,
              adv: float) -> float:
    shape = band_shape(bpf, tonal, f_pk)
    tb = tonal_bands(bpf)
    best = -1e9
    for b in range(24):
        lw = lwa + shape[b]
        lp = lw - 20.0 * math.log10(r) - 11.0 - alpha(b) * r / 1000.0 - g
        m = -4.0 if b in tb else 0.0
        thr = max(hearing, l_bg_bands[b] + m) + 6.0 * (1.0 - a) - adv
        best = max(best, lp + A_W[b] - thr)
    return best
