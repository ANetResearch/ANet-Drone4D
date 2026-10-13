"""感知量化与场剖面的独立参考实现（M19-FR-037；M19-AC-021、AC-022、AC-023；AWR-04 §15.10.3、§15.11.3；M18-M19 PRD §15.2.2、
§15.2.3）。纯 Python 循环，只用 math；不 import awr（带表、A 计权、ISO 9613-2 吸收与缺省谱形取 tests/targets 的独立参考
`ref_acoustic.py`，其本身也不 import awr）。

- 声学：`L_p,b = L_W,b − 20 lg r − 11 − alpha_b r/1000 − g`；合成 `L_pA = 10 lg Σ 10^((L_p,b + A_b)/10)`；
  `thr_b = max(L_th, L_bg,b + M_b) + 6(1 − a) − adv`；`m = max_b(L_p,b + A_b + adj_b − thr_b)`；`P = 1/(1 + e^(−m/2))`。
- 视觉：`θ = √A(e)/r`（e ≥ 45° 顶面否则侧面，识别物看无人机）；`θ_th = θ_0/(0.5 + a)`（低照 × 2）；`C_app = |C|·e^(−sigma r)`，
  `sigma = ln 20/MOR`；`P = x^E/(1 + x^E)`，`E = 2.7 + 0.7x`；夜间开灯 1、未开灯 r ≤ 30 m 为 1。
- 雷达：`SNR = snr_ref + 10 lg sigma − 40 lg(r/r_ref) − 2gamma r/1000`，`gamma = 1.0·RR^0.7`；`P = 1/(1 + e^(−m/1))`。
- 剖面：声学 L_pA；视觉 θ（'，侧面）；热 `N = d_hot/(r·IFOV)`、`ΔT_app = τ_LW·ε·ΔT·min(1, N²)`，`τ_LW = e^(−sigma_LW r)`，
  `sigma_LW = sigma_550·(10000/550)^(−q) + (0.03 + 0.012rho_v)/1000`（Kim q，V2 = ln 50/sigma_550 km）；雷达 SNR（参考 13.2 dB @ 180 m、1 m²）。
- 感知体：声学按方位二分求 `m = 0` 的半径；视觉按仰角取 `min(√A/θ_th, ln(|C|/0.05)/sigma)`；过渡宽度见 AWR-04 §15.11.4 第 2 条。
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

_T = Path(__file__).resolve().parents[1] / "targets"
if str(_T) not in sys.path:
    sys.path.insert(0, str(_T))

from ref_acoustic import A_W, NOMINAL, alpha, band_shape, tonal_bands  # noqa: E402

ARCMIN = 180.0 * 60.0 / math.pi
C_MIN = 0.05
LN20 = math.log(20.0)
LN50 = math.log(50.0)


def p_logistic(m: float, s: float) -> float:
    return 1.0 / (1.0 + math.exp(-m / s))


def p_visual(x: float) -> float:
    if x <= 0.0:
        return 0.0
    e = 2.7 + 0.7 * x
    return 1.0 / (1.0 + math.exp(-e * math.log(x)))


# ---------------------------------------------------------------- 签名（P600 与参考机型，AWR-04 §15.10.2）
def lw_bands(lwa: float, bpf: float, tonal: float = 12.0, f_pk: float = 1414.0) -> list[float]:
    return [lwa + s for s in band_shape(bpf, tonal, f_pk)]


def masking(bpf: float) -> list[float]:
    t = tonal_bands(bpf)
    return [-4.0 if b in t else 0.0 for b in range(24)]


# ---------------------------------------------------------------- 逐对
def acoustic_q(lw: list[float], r: float, g: float, l_bg: list[float], mask: list[float], l_th: float, a: float, adv: float,
               adj: list[float] | None = None) -> dict:
    r = max(r, 0.1)
    adj = adj or [0.0] * 24
    lp = [lw[b] - 20.0 * math.log10(r) - 11.0 - alpha(b) * r / 1000.0 - g for b in range(24)]
    thr = [max(l_th, l_bg[b] + mask[b]) + 6.0 * (1.0 - a) - adv for b in range(24)]
    m = [lp[b] + A_W[b] + adj[b] - thr[b] for b in range(24)]
    bs = max(range(24), key=lambda b: m[b])
    lpa = 10.0 * math.log10(sum(10.0 ** ((lp[b] + A_W[b]) / 10.0) for b in range(24)))
    return {"lp_dba": lpa, "lp_band_db": lp[bs] + A_W[bs] + adj[bs], "band_hz": NOMINAL[bs], "thr_dba": thr[bs], "margin_db": m[bs],
            "p": p_logistic(m[bs], 2.0)}


def acoustic_single(lwa: float, r: float, thr: float, alpha_km: float = 5.0) -> dict:
    """单带退化口径（1 kHz、alpha = 5.0 dB/km、A = 0）。"""
    lp = lwa - 20.0 * math.log10(r) - 11.0 - alpha_km * r / 1000.0
    return {"lp_dba": lp, "margin_db": lp - thr, "p": p_logistic(lp - thr, 2.0)}


def visual_q(area: tuple[float, float], c: tuple[float, float], r: float, e_rad: float, band: str, mor: float, theta0: float, a: float,
             acuity: float | None, *, lit: bool = False, night_m: float = 30.0, los: bool = True, in_fov: bool = True) -> dict:
    top, side = area
    d = math.sqrt(top if e_rad >= math.radians(45.0) else side)
    th = theta0 / ARCMIN / (0.5 + a) * (2.0 if band == "low" else 1.0)
    sig = LN20 / mor if math.isfinite(mor) and mor > 0 else 0.0
    cc = abs(c[0] if e_rad > 0.0 else c[1])
    c_app = cc * math.exp(-sig * r)
    theta = d / r
    gate = los and in_fov and c_app >= C_MIN
    if band == "night":
        ok = lit or r <= night_m
        p = 1.0 if (gate and ok) else 0.0
    else:
        ok = theta >= th
        p = p_visual(theta / th) if gate else 0.0
    return {"theta_arcmin": theta * ARCMIN, "d_vis_m": d, "n_eq_px": theta * ARCMIN / acuity if acuity else None, "theta_th_arcmin": th * ARCMIN,
            "ratio": theta / th, "c_app": c_app, "p": p, "ok": bool(gate and ok)}


def radar_q(snr_ref: float, r_ref: float, snr_req: float, rcs: float, r: float, rain: float = 0.0, *, in_fov: bool = True) -> dict:
    gamma = 1.0 * rain ** 0.7 if rain > 0 else 0.0
    snr = snr_ref + 10.0 * math.log10(rcs) - 40.0 * math.log10(r / r_ref) - 2.0 * gamma * r / 1000.0
    return {"snr_db": snr, "margin_db": snr - snr_req, "p": p_logistic(snr - snr_req, 1.0) if in_fov else 0.0}


# ---------------------------------------------------------------- 剖面
def kim_q(v2_km: float) -> float:
    if v2_km > 50:
        return 1.6
    if v2_km > 6:
        return 1.3
    if v2_km > 1:
        return 0.16 * v2_km + 0.34
    if v2_km > 0.5:
        return v2_km - 0.5
    return 0.0


def sigma_lwir(mor: float, rho_v: float) -> float:
    s = 0.0
    if math.isfinite(mor) and mor > 0:
        s550 = LN20 / mor
        s = s550 * (10000.0 / 550.0) ** -kim_q(LN50 / s550 / 1000.0)
    return s + (0.03 + 0.012 * max(0.0, rho_v)) / 1000.0


def prof_acoustic(lw: list[float], r: float) -> float:
    r = max(r, 0.1)
    return 10.0 * math.log10(sum(10.0 ** ((lw[b] - 20.0 * math.log10(r) - 11.0 - alpha(b) * r / 1000.0 + A_W[b]) / 10.0) for b in range(24)))


def prof_visual(side: float, r: float) -> float:
    return math.sqrt(side) / r * ARCMIN


def prof_thermal(d_hot: float, r: float, *, ifov: float = 12e-6 / 25e-3, eps: float = 0.9, dt: float = 20.0, netd: float = 0.05,
                 mor: float = math.inf, rho_v: float = 7.5) -> float | None:
    n = d_hot / (r * ifov)
    dta = math.exp(-sigma_lwir(mor, rho_v) * r) * eps * dt * min(1.0, n * n)
    return n if dta >= netd else None


def prof_radar(rcs: float, r: float, rain: float = 0.0) -> float:
    return radar_q(13.2, 180.0, 13.2, rcs, r, rain)["snr_db"]


# ---------------------------------------------------------------- 感知体
def acoustic_radius(lw: list[float], l_bg: list[float], mask: list[float], l_th: float, a: float, adv: float, g: float = 0.0) -> float:
    """各带 m_b(r) = 0 的解取最大（[1, 5000] m 二分）。"""
    best = 0.0
    thr = [max(l_th, l_bg[b] + mask[b]) + 6.0 * (1.0 - a) - adv for b in range(24)]
    for b in range(24):
        def f(r: float, b: int = b) -> float:
            return lw[b] + A_W[b] - 20.0 * math.log10(r) - 11.0 - alpha(b) * r / 1000.0 - g - thr[b]

        if f(1.0) < 0:
            continue
        if f(5000.0) >= 0:
            return 5000.0
        lo, hi = 1.0, 5000.0
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if f(mid) >= 0:
                lo = mid
            else:
                hi = mid
        best = max(best, 0.5 * (lo + hi))
    return best


def visual_radius_el(area: tuple[float, float], c: tuple[float, float], e_deg: float, band: str, mor: float, theta0: float, a: float, *,
                     lit: bool = False, night_m: float = 30.0) -> float:
    top, side = area
    th = theta0 / ARCMIN / (0.5 + a) * (2.0 if band == "low" else 1.0)
    r_ang = (math.inf if lit else night_m) if band == "night" else math.sqrt(top if e_deg >= 45.0 else side) / th
    cc = abs(c[0] if e_deg > 0 else c[1])
    sig = LN20 / mor if math.isfinite(mor) and mor > 0 else 0.0
    r_c = 0.0 if cc <= C_MIN else (math.inf if sig <= 0 else math.log(cc / C_MIN) / sig)
    return min(r_ang, r_c, 2000.0)
