"""逐对感知量化读数（M19-FR-030、FR-031；AWR-04 §15.10，ADR-124；M18-M19 PRD §15.2.1；17 §17.20.6 `awr.target.quant.v1`）。

对一个（识别物 T，飞行器 U）对给出：到达声压（24 带、A 计权合成）、主导带、该带阈值、余量与声学概率；角尺寸、可见尺寸、等效像素、
检测阈、比值、表观对比度、照度档、灯光、角向覆盖与视觉概率；雷达 SNR、余量与概率（P1，识别物配置 `radar_rx` 且挂 radar 敏感体）；
合成概率、按当前判据模式是否满足判据、驻留与反向感知（该机成像传感器对 T 的最佳感知）。

判据不变（ADR-099）：概率只用于读数、感知体透明度与说明。以"判据边界 = 0.5"标定：

| 模态 | 显示用概率 | 边界 |
|---|---|---|
| 声学 | `P = 1/(1 + exp(−m/s))`，`m = max_b(L_p,b + A_b + adj_b − thr_b)`，s = 2.0 dB | m = 0 → 0.5 |
| 视觉 | 门控（视线、角向覆盖、`|C|·τ_550 ≥ 0.05`、夜间灯光规则）通过时 `P = x^E/(1 + x^E)`，`x = θ/θ_th`，`E = 2.7 + 0.7·x`；夜间开灯为 1、未开灯 r ≤ night_visible_m 为 1 否则 0；门控不通过为 0 | x = 1 → 0.5 |
| 雷达 | 视场内 `P = 1/(1 + exp(−(SNR − snr_req)/1.0))`，否则 0 | SNR = snr_req → 0.5 |

合成 `P_any = 1 − Π_m (1 − P_m)`（只对识别物启用了该模态敏感体的模态求积）。physical 模式下 `would_discover` 为"参与者（armed 或 in_air）
∧ 某模态：位于该模态配置体内 ∧ 门控通过 ∧ P_m ≥ 0.5"，布尔值直接按 M18 判据的同一表达式计算（`acoustic_margin` 同序运算、
`dvis/r ≥ θ_th`、`|C|·τ ≥ 0.05`、`k_los > 0.5`），因此与 M18 判定逐项一致（M19-AC-021）；geometric 模式为"参与者 ∧ 位于任一判定
敏感体内"，概率标为物理参考。

几何口径与 M18 判据相同：斜距与方位取识别物脚点，视觉仰角取识别物顶部（z0 + 0.95·h）看无人机；声学背向衰减
`g_back = gain_back_db·(1 − cos Δψ)/2`，Δψ 相对（航向 + 敏感体方位偏置）。视线为顶部射线：先读共享 `LosCache` 的缓存值
（`peek`，不消耗令牌，`los_age_s` 为年龄），未命中时为 None（`QuantContext.los_fn` 可按需请求，见 field.py 的服务）。

线格式两种（同值）：REST（SB-73）与 `expand` 为 17 §17.20.6 的 `pairs[{...}]`；topic `target/{id}/quant` 为紧凑列式 `soa1`
（每列一个数组、全列相同的值折叠为标量、数值按 `QUANT_SCALE` 定标为整数），由前端 `decodeTargetQuant` 还原为同一结构
（帧 ≤ 1.5 KB，M19-NFR-008）。数值的取整步长与定标一致，因此解码值与 REST 逐位相等。
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray

from awr.sim.targets.acoustic import ALPHA_DB_KM, threshold_bands
from awr.sim.targets.envview import TargetEnv
from awr.sim.targets.geometry import body_volumes, inside_points, wrap_pi
from awr.sim.targets.model import band_adj_of, radar_rx_of, visual_acuity_of
from awr.sim.targets.radar import gamma_rain_db_km, radar_in_fov, radar_snr_db
from awr.sim.targets.signature import VehicleSignature
from awr.sim.targets.sound import A_W, BANDS_HZ
from awr.sim.targets.visual import C_MIN, theta_th_rad

__all__ = [
    "ARCMIN_PER_RAD", "MAX_PAIRS", "QUANT_SCALE", "S_AC_DB", "S_RD_DB", "AcousticQ", "FieldEnv", "PairQuant", "QuantTarget", "QuantUav",
    "RadarQ", "ReverseQ", "VisualQ", "acoustic_batch", "acoustic_pair", "encode_pairs", "encode_soa", "expand_soa", "field_env", "p_any", "p_logistic", "p_visual", "quant_frame",
    "quant_pairs", "radar_pair", "visual_batch", "visual_pair",
]

S_AC_DB = 2.0                     # 声学概率尺度（ISO 9613-2 自由场估计精度约 ±3 dB；+4.4 dB 时 P = 0.9）
S_RD_DB = 1.0                     # 雷达概率尺度（Swerling 0 的陡峭过渡）
ARCMIN_PER_RAD = 180.0 * 60.0 / math.pi
MAX_PAIRS = 12                    # 每识别物 ≤ 12 对（斜距升序；AWR-04 §4.4.3 机群上限）
ELEV45 = math.radians(45.0)


# ---------------------------------------------------------------- 环境
@dataclass(frozen=True)
class FieldEnv:
    """量化与场所需的环境：M18 的 `TargetEnv`（照度档、MOR、24 带背景、剖面、雨强）加 LWIR 水汽密度（M07 `rho_v_gm3`）。"""

    t: TargetEnv
    rho_v_gm3: float = 7.5

    @property
    def band(self) -> str:
        return self.t.band

    @property
    def mor_m(self) -> float:
        return self.t.mor_m

    @property
    def sigma550(self) -> float:
        return self.t.sigma550

    @property
    def l_bg_dba(self) -> float:
        return float(self.t.l_bg_dba)

    @property
    def l_bg_bands(self) -> NDArray:
        return self.t.l_bg_bands

    @property
    def rain_mmph(self) -> float:
        return float(getattr(self.t, "rain_mmph", 0.0) or 0.0)

    def key(self) -> tuple:
        return (*self.t.key(), self.t.profile, round(self.rain_mmph, 1), round(self.rho_v_gm3, 1))


def field_env(tenv: TargetEnv, sample: Any = None) -> FieldEnv:
    """M18 环境样本 + M19 `EnvSample`（雨强、水汽）→ FieldEnv。"""
    if sample is None:
        return FieldEnv(tenv)
    rain = float(getattr(sample, "rain_mmph", 0.0) or 0.0)
    t = tenv if abs(rain - float(getattr(tenv, "rain_mmph", 0.0) or 0.0)) < 1e-9 else replace(tenv, rain_mmph=rain)
    return FieldEnv(t, float(getattr(sample, "rho_v_gm3", 7.5) or 7.5))


# ---------------------------------------------------------------- 概率
def p_logistic(m: float, s: float) -> float:
    z = -float(m) / float(s)
    if z > 700.0:
        return 0.0
    return 1.0 / (1.0 + math.exp(z))


def p_visual(x: float) -> float:
    """TTPF 同形：`x^E/(1 + x^E)`，`E = 2.7 + 0.7·x`（P(1) = 0.5，与 AWR-04 §6.2 同形）。"""
    x = float(x)
    if not x > 0.0:
        return 0.0
    if x > 50.0:
        return 1.0
    e = 2.7 + 0.7 * x
    v = x ** e
    return v / (1.0 + v)


def p_any(ps: Sequence[float]) -> float:
    q = 1.0
    for p in ps:
        q *= 1.0 - float(p)
    return 1.0 - q


# ---------------------------------------------------------------- 纯函数（逐模态）
@dataclass(frozen=True)
class AcousticQ:
    lp_dba: float            # 合成到达声压 L_pA（dB(A)）
    lp_band_db: float        # 主导带加权带级 L_p,b* + A_b* + adj_b*（dB）
    band_hz: float           # 主导带标称中心频率
    thr_dba: float           # 主导带阈值 thr_b*（dB(A)）
    l_bg_dba: float          # 背景噪声总级（dB(A)）
    margin_db: float         # max_b(L_pw,b − thr_b) = lp_band_db − thr_dba
    p: float
    in_body: bool


@dataclass(frozen=True)
class VisualQ:
    theta_arcmin: float
    d_vis_m: float
    n_eq_px: float | None
    theta_th_arcmin: float
    ratio: float
    c_app: float
    light_band: str
    lights_on: bool
    in_fov: bool
    p: float
    in_body: bool


@dataclass(frozen=True)
class RadarQ:
    snr_db: float
    snr_req_db: float
    margin_db: float
    p: float
    in_body: bool


@dataclass(frozen=True)
class ReverseQ:
    sensor: str
    level: str
    n_eff: float | None
    n_req: float | None
    p: float | None
    limiting: str | None


@dataclass(frozen=True)
class PairQuant:
    uav: str
    model_id: str
    range_m: float
    elev_deg: float
    az_rel_deg: float
    los: bool | None
    los_age_s: float | None
    acoustic: AcousticQ | None
    visual: VisualQ | None
    radar: RadarQ | None
    p_any: float
    would_discover: bool
    dwell_s: float
    t_dwell_s: float
    reverse: ReverseQ | None


def acoustic_pair(lw_bands: NDArray, r_m: float, g_back_db: float, l_bg_bands: NDArray, masking_db: NDArray, hearing_th_dba: float,
                  alertness: float, adv_db: float, adj_db: NDArray | None = None, *, l_bg_dba: float | None = None, in_body: bool = False,
                  alpha: NDArray = ALPHA_DB_KM, a_w: NDArray = A_W, bands_hz: Sequence[float] = BANDS_HZ) -> AcousticQ:
    """声学逐对读数（AWR-04 §15.10.3 第 1 行）。逐带运算的次序与 M18 `acoustic_margin` 相同，余量逐位一致。

    `alpha`、`a_w`、`bands_hz` 可替换为单带（单带退化口径：1 kHz、alpha = 5.0 dB/km、A = 0），用于 AWR-04 §7.3 的锚点。"""
    lw = np.asarray(lw_bands, float).reshape(-1)
    r = max(float(r_m), 0.1)
    al = np.asarray(alpha, float).reshape(-1)
    aw = np.asarray(a_w, float).reshape(-1)
    lp = lw - 20.0 * np.log10(r) - 11.0 - al * r / 1000.0 - float(g_back_db)
    w = aw if adj_db is None else aw + np.asarray(adj_db, float)
    thr = threshold_bands(l_bg_bands, masking_db, hearing_th_dba, alertness, adv_db)
    m_b = lp + w - np.asarray(thr, float)
    b = int(np.argmax(m_b))
    margin = float(m_b[b])
    lpa = float(10.0 * np.log10(np.sum(10.0 ** ((lp + aw) / 10.0))))
    bg = float(l_bg_dba) if l_bg_dba is not None else float(10.0 * np.log10(np.sum(10.0 ** (np.asarray(l_bg_bands, float) / 10.0))))
    return AcousticQ(lpa, float(lp[b] + w[b]), float(bands_hz[b]), float(np.asarray(thr, float).reshape(-1)[b]), bg, margin,
                     p_logistic(margin, S_AC_DB), bool(in_body))


def visual_pair(sig: VehicleSignature, r_m: float, elev_rad: float, env: FieldEnv | TargetEnv, theta0_arcmin: float, alertness: float,
                acuity_arcmin: float | None, *, los: bool | None, in_fov: bool, in_body: bool = False) -> tuple[VisualQ, bool]:
    """视觉逐对读数（AWR-04 §15.10.3 第 2 行）与"门控通过且 θ ≥ θ_th"（与 M18 `visual_ok` 同一表达式）。返回 (读数, ok)。"""
    te = env.t if isinstance(env, FieldEnv) else env
    r3 = max(float(r_m), 1e-3)
    e = float(elev_rad)
    top, side, _front = sig.area_m2
    dvis = float(np.sqrt(top if e >= ELEV45 else side))
    th = theta_th_rad(theta0_arcmin, alertness, te.band)
    c = abs(sig.contrast_sky if e > 0.0 else sig.contrast_ground)
    tau = float(te.tau550(r3))
    c_app = c * tau
    ok_c = c_app >= C_MIN
    lit = bool(sig.lights_nav or sig.lights_strobe)
    theta = dvis / r3
    night = te.band == "night"
    ok_ang = (lit or r3 <= sig.night_visible_m) if night else theta >= th
    gate = bool(los) and bool(in_fov) and ok_c
    p = (1.0 if (gate and ok_ang) else 0.0) if night else (p_visual(theta / th) if gate else 0.0)
    theta_am = theta * ARCMIN_PER_RAD
    n_eq = theta_am / float(acuity_arcmin) if acuity_arcmin else None
    q = VisualQ(theta_am, dvis, n_eq, th * ARCMIN_PER_RAD, theta / th, c_app, te.band, lit, bool(in_fov), p, bool(in_body))
    return q, bool(gate and ok_ang)


def radar_pair(rx: dict, rcs_m2: float, r_m: float, rain_mmph: float, *, in_fov: bool, in_body: bool = False) -> RadarQ:
    """雷达逐对读数（P1，AWR-04 §15.10.3 第 3 行）。"""
    snr = float(radar_snr_db(rx, rcs_m2, r_m, gamma_rain_db_km(rain_mmph)))
    req = float(rx["snr_req_db"])
    m = snr - req
    return RadarQ(snr, req, m, p_logistic(m, S_RD_DB) if in_fov else 0.0, bool(in_body))


# ---------------------------------------------------------------- 逐对组装
@dataclass(frozen=True)
class QuantUav:
    """参与读数的机体：id、机型、位置（ENU）、是否判据参与者（armed 或 in_air）、签名（含灯光）。"""

    id: str
    model_id: str
    pos: NDArray
    participant: bool
    sig: VehicleSignature


@dataclass(frozen=True)
class QuantTarget:
    """识别物的只读状态（M18 `TargetRuntime.quant_view` 同形）。"""

    id: str
    cfg: Any                  # TargetConfig
    pos: NDArray              # 脚点
    yaw_rad: float
    h_m: float
    alertness: float
    dwell: dict
    t_dwell_s: float


# los_fn(a, b, h, need) → (可见, 年龄 s)：先读缓存；need 为真（视线决定读数或判据）且未命中时才请求射线（共享预算）
LosFn = Callable[[NDArray, NDArray, float, bool], "tuple[bool | None, float | None]"]


def _bodies(raw: dict, kind: str) -> list[tuple[int, dict]]:
    return [(i, b) for i, b in enumerate(raw.get("sensitivity") or []) if b.get("enabled", True) and b.get("kind") == kind]


def _inside(raw: dict, pos: NDArray, yaw: float, P: NDArray, kind: str, *, unbounded: bool = False) -> tuple[NDArray, list[NDArray]]:
    """P [n, 3] 是否在某一 kind 的启用体内（any）与逐体掩码；`unbounded` 时半径取 1e7（角向覆盖与高度范围）。"""
    bs = _bodies(raw, kind)
    if not bs:
        return np.zeros(len(P), bool), []
    radius_of = [1e7] * len(raw.get("sensitivity") or []) if unbounded else None
    vols = body_volumes(list(raw.get("sensitivity") or []), pos, yaw, kinds=(kind,), radius_of=radius_of)
    masks = [inside_points(v, P) for v in vols]
    return np.any(masks, axis=0), masks


def _p_visual_vec(x: NDArray) -> NDArray:
    x = np.asarray(x, float)
    out = np.zeros_like(x)
    m = (x > 0.0) & (x <= 50.0)
    xm = x[m]
    v = xm ** (2.7 + 0.7 * xm)
    out[m] = v / (1.0 + v)
    out[x > 50.0] = 1.0
    return out


def acoustic_batch(lw: NDArray, r_m: NDArray, g_back_db: NDArray, l_bg_bands: NDArray, masking: NDArray, hearing_th_dba: float, alertness: float,
                   adv_db: float, adj_db: NDArray | None, l_bg_dba: float, in_body: NDArray) -> list[AcousticQ]:
    """`acoustic_pair` 的批量版本（lw、masking 为 [k, 24]）；余量与 M18 `acoustic_margin` 同一表达式与次序。"""
    r = np.maximum(np.asarray(r_m, float), 0.1)[:, None]
    lp = np.asarray(lw, float) - 20.0 * np.log10(r) - 11.0 - ALPHA_DB_KM[None, :] * r / 1000.0 - np.asarray(g_back_db, float)[:, None]
    w = A_W if adj_db is None else A_W + np.asarray(adj_db, float)
    thr = threshold_bands(l_bg_bands, masking, hearing_th_dba, alertness, adv_db)
    m_b = lp + w[None, :] - np.asarray(thr, float)
    b = np.argmax(m_b, axis=1)
    k = np.arange(len(b))
    margin = m_b[k, b]
    lpa = 10.0 * np.log10(np.sum(10.0 ** ((lp + A_W[None, :]) / 10.0), axis=1))
    band = (lp + w[None, :])[k, b]
    thr_b = np.asarray(thr, float)[k, b]
    return [AcousticQ(float(lpa[i]), float(band[i]), float(BANDS_HZ[int(b[i])]), float(thr_b[i]), float(l_bg_dba), float(margin[i]),
                      p_logistic(float(margin[i]), S_AC_DB), bool(in_body[i])) for i in range(len(b))]


def _visual_core(sigs: Sequence[VehicleSignature], r_m: NDArray, elev_rad: NDArray, env: FieldEnv, theta0_arcmin: float, alertness: float) -> dict:
    """视觉逐对量的批量中间值（与视线无关的部分）。"""
    te = env.t
    r3 = np.maximum(np.asarray(r_m, float), 1e-3)
    e = np.asarray(elev_rad, float)
    top = np.array([g.area_m2[0] for g in sigs])
    side = np.array([g.area_m2[1] for g in sigs])
    csky = np.array([g.contrast_sky for g in sigs])
    cgnd = np.array([g.contrast_ground for g in sigs])
    lit = np.array([bool(g.lights_nav or g.lights_strobe) for g in sigs], bool)
    nv = np.array([g.night_visible_m for g in sigs])
    dvis = np.sqrt(np.where(e >= ELEV45, top, side))
    th = theta_th_rad(theta0_arcmin, alertness, te.band)
    c = np.abs(np.where(e > 0.0, csky, cgnd))
    c_app = c * np.asarray(te.tau550(r3), float)
    theta = dvis / r3
    night = te.band == "night"
    ratio = theta / th
    return {"r3": r3, "dvis": dvis, "th": th, "c_app": c_app, "ok_c": c_app >= C_MIN, "theta": theta, "night": night, "lit": lit,
            "ok_ang": (lit | (r3 <= nv)) if night else theta >= th, "ratio": ratio, "pv": None if night else _p_visual_vec(ratio), "band": te.band}


def _visual_final(core: dict, los: NDArray, in_fov: NDArray, in_body: NDArray, acuity_arcmin: float | None) -> tuple[list[VisualQ], NDArray]:
    gate = np.asarray(los, bool) & np.asarray(in_fov, bool) & core["ok_c"]
    ok = gate & core["ok_ang"]
    p = np.where(ok, 1.0, 0.0) if core["night"] else np.where(gate, core["pv"], 0.0)
    th_am = core["th"] * ARCMIN_PER_RAD
    theta, dvis, ratio, c_app, lit, band = core["theta"], core["dvis"], core["ratio"], core["c_app"], core["lit"], core["band"]
    out = []
    for i in range(len(theta)):
        t_am = float(theta[i]) * ARCMIN_PER_RAD
        out.append(VisualQ(t_am, float(dvis[i]), t_am / float(acuity_arcmin) if acuity_arcmin else None, th_am, float(ratio[i]), float(c_app[i]),
                           band, bool(lit[i]), bool(in_fov[i]), float(p[i]), bool(in_body[i])))
    return out, ok


def visual_batch(sigs: Sequence[VehicleSignature], r_m: NDArray, elev_rad: NDArray, env: FieldEnv, theta0_arcmin: float, alertness: float,
                 acuity_arcmin: float | None, los: NDArray, in_fov: NDArray, in_body: NDArray) -> tuple[list[VisualQ], NDArray]:
    """`visual_pair` 的批量版本（los 为布尔数组，未知按不可见）。返回 (读数, ok)。"""
    return _visual_final(_visual_core(sigs, r_m, elev_rad, env, theta0_arcmin, alertness), los, in_fov, in_body, acuity_arcmin)


def quant_pairs(t: QuantTarget, uavs: Sequence[QuantUav], env: FieldEnv, *, los_fn: LosFn | None = None,
                reverse_fn: Callable[[str, str], ReverseQ | None] | None = None, uav_filter: str | None = None,
                limit: int = MAX_PAIRS) -> list[PairQuant]:
    """识别物 t 对机体集合的逐对读数（M19-FR-030），按斜距升序 ≤ limit 对（按模态批量计算）。`los_fn(a, b, h, need)` → (可见, 年龄 s)，
    缺省视为未知。"""
    raw = t.cfg.raw
    mode = raw.get("criterion_mode", "geometric")
    us = [u for u in uavs if uav_filter is None or u.id == uav_filter]
    if not us:
        return []
    pos = np.asarray(t.pos, float)
    P = np.array([np.asarray(u.pos, float) for u in us]).reshape(-1, 3)
    d = P - pos[None, :]
    r3 = np.linalg.norm(d, axis=1)
    order = np.argsort(r3, kind="stable")[:limit]
    us = [us[i] for i in order]
    P, d, r3 = P[order], d[order], r3[order]
    n = len(us)
    h = float(t.h_m)
    top_z = pos[2] + 0.95 * h
    rh = np.hypot(d[:, 0], d[:, 1])
    elev = np.arctan2(P[:, 2] - top_z, rh)
    az = np.arctan2(d[:, 1], d[:, 0])
    az_rel = np.degrees(np.asarray(wrap_pi(az - t.yaw_rad), float))
    a = float(t.alertness)
    rx = radar_rx_of(raw)
    ac_bodies = _bodies(raw, "acoustic")
    has_vis = bool(_bodies(raw, "visual"))
    has_rd = bool(_bodies(raw, "radar")) and rx is not None
    in_ac, ac_masks = _inside(raw, pos, t.yaw_rad, P, "acoustic")
    in_vis, _ = _inside(raw, pos, t.yaw_rad, P, "visual")
    fov_vis, _ = _inside(raw, pos, t.yaw_rad, P, "visual", unbounded=True)
    in_rd, _ = _inside(raw, pos, t.yaw_rad, P, "radar") if has_rd else (np.zeros(n, bool), [])
    sigs = [u.sig for u in us]
    would = np.zeros(n, bool)
    physical = mode == "physical"
    # 声学：每个声学体批量计算，逐对取"位于其中的体优先，同类余量最大"
    acs: list[AcousticQ | None] = [None] * n
    if ac_bodies:
        cache: dict[int, tuple[NDArray, NDArray]] = {}
        for g in sigs:
            if id(g) not in cache:
                cache[id(g)] = (g.lw_bands(), g.masking_db())
        lw = np.array([cache[id(g)][0] for g in sigs])
        mk = np.array([cache[id(g)][1] for g in sigs])
        adj = band_adj_of(raw)
        for q, (_i, b) in enumerate(ac_bodies):
            dpsi = np.asarray(wrap_pi(az - (t.yaw_rad + math.radians(float(b.get("azimuth_offset_deg", 0.0))))), float)
            gb = float(b.get("gain_back_db", 0.0)) * (1.0 - np.cos(dpsi)) / 2.0
            cand = acoustic_batch(lw, r3, gb, env.l_bg_bands, mk, float(raw.get("hearing_th_dba", 20.0)), a, float(raw.get("acoustic_adv_db", 0.0)),
                                  adj, env.l_bg_dba, ac_masks[q])
            for k in range(n):
                best = acs[k]
                if best is None or (cand[k].in_body, cand[k].margin_db) > (best.in_body, best.margin_db):
                    acs[k] = cand[k]
        if physical:
            would |= np.array([x is not None and x.in_body and x.margin_db >= 0.0 for x in acs], bool)
    # 视觉：先按"视线可见"预判是否需要视线，再按实际视线计算
    viss: list[VisualQ | None] = [None] * n
    los_l: list[bool | None] = [None] * n
    age_l: list[float | None] = [None] * n
    if has_vis:
        acuity = visual_acuity_of(raw)
        theta0 = float(raw.get("visual_theta0_arcmin", 1.0))
        core = _visual_core(sigs, r3, elev, env, theta0, a)
        if los_fn is not None:
            g0 = fov_vis & core["ok_c"]
            need_v = in_vis | (g0 & core["ok_ang"]) | (False if core["night"] else (g0 & (core["pv"] > 1e-3)))
            for k in range(n):
                los_l[k], age_l[k] = los_fn(P[k], pos, h, bool(need_v[k]))
        vq, ok = _visual_final(core, np.array([x is True for x in los_l], bool), fov_vis, in_vis, acuity)
        viss = list(vq)
        if physical:
            would |= in_vis & ok
    rds: list[RadarQ | None] = [None] * n
    if has_rd and rx is not None:
        axes = [t.yaw_rad + math.radians(float(b.get("azimuth_offset_deg", 0.0))) for _i, b in _bodies(raw, "radar")]
        fov = np.any([radar_in_fov(rx, d, ax) for ax in axes], axis=0)
        for k in range(n):
            rds[k] = radar_pair(rx, sigs[k].rcs_m2, float(r3[k]), env.rain_mmph, in_fov=bool(fov[k]), in_body=bool(in_rd[k]))
        if physical:
            would |= in_rd & fov & np.array([x.margin_db >= 0.0 for x in rds if x is not None], bool)
    if not physical:
        would = in_ac | in_vis | in_rd
    out: list[PairQuant] = []
    for k, u in enumerate(us):
        ac, vis, rd = acs[k], viss[k], rds[k]
        ps = [m.p for m in (ac, vis, rd) if m is not None]
        dw = int(t.dwell.get(u.id, 0))
        rev = reverse_fn(u.id, t.id) if reverse_fn is not None else None
        out.append(PairQuant(u.id, u.model_id, float(r3[k]), math.degrees(float(elev[k])), float(az_rel[k]), los_l[k], age_l[k], ac, vis, rd,
                             p_any(ps) if ps else 0.0, bool(would[k] and u.participant), max(0, dw - 1) * 0.1, float(t.t_dwell_s), rev))
    return out


# ---------------------------------------------------------------- 线格式
# 紧凑列式 soa1 的定标（整数 = round(值 × scale)）；REST 与 expand 的值按同一步长取整（值 = 整数 / scale），二者逐位相等。
QUANT_SCALE: dict[str, float] = {
    "range_m": 10, "elev_deg": 10, "az_rel_deg": 10, "los_age_s": 10, "p_any": 1000, "dwell_s": 10, "t_dwell_s": 100,
    "acoustic.lp_dba": 10, "acoustic.lp_band_db": 10, "acoustic.band_hz": 1, "acoustic.thr_dba": 10, "acoustic.l_bg_dba": 10,
    "acoustic.margin_db": 10, "acoustic.p": 1000,
    "visual.theta_arcmin": 100, "visual.d_vis_m": 1000, "visual.n_eq_px": 100, "visual.theta_th_arcmin": 100, "visual.ratio": 1000,
    "visual.c_app": 1000, "visual.p": 1000,
    "radar.snr_db": 10, "radar.snr_req_db": 10, "radar.margin_db": 10, "radar.p": 1000,
    "reverse.n_eff": 10, "reverse.n_req": 10, "reverse.p": 1000,
}
SUB = ("acoustic", "visual", "radar", "reverse")
_FIELDS = {
    "acoustic": ("lp_dba", "lp_band_db", "band_hz", "thr_dba", "l_bg_dba", "margin_db", "p", "in_body"),
    "visual": ("theta_arcmin", "d_vis_m", "n_eq_px", "theta_th_arcmin", "ratio", "c_app", "light_band", "lights_on", "in_fov", "p", "in_body"),
    "radar": ("snr_db", "snr_req_db", "margin_db", "p", "in_body"),
    "reverse": ("sensor", "level", "n_eff", "n_req", "p", "limiting"),
}
_TOP = ("uav", "model_id", "range_m", "elev_deg", "az_rel_deg", "los", "los_age_s")
_TAIL = ("p_any", "would_discover", "dwell_s", "t_dwell_s")
I32 = 2 ** 31 - 1


def _q(v: Any, scale: float | None) -> Any:
    """取整到定标步长的整数（None 与非有限值为 None）。"""
    if v is None or scale is None:
        return v
    x = float(v)
    if not math.isfinite(x):
        return None
    return max(-I32, min(I32, round(x * scale)))


def _deq(v: Any, scale: float | None) -> Any:
    if v is None or scale is None or isinstance(v, bool):
        return v
    s = float(scale)
    return int(v) if s == 1.0 else int(v) / s


_INF = float("inf")


def _val_s(v: Any, s: float | None) -> Any:
    """REST 值：按定标步长取整后的浮点（定标 1 的字段为 int）；布尔、字符串与 None 原样（与 `_deq(_q(v, s), s)` 相同，热路径）。"""
    if s is None or v is None or v is True or v is False or type(v) is str:
        return v
    x = v * s
    if x != x or x == _INF or x == -_INF:
        return None
    k = round(x)
    if k > I32 or k < -I32:
        k = I32 if k > 0 else -I32
    return k if s == 1 else k / s


def _val(v: Any, key: str) -> Any:
    return _val_s(v, QUANT_SCALE.get(key))


_SUB_KEYS = {sub: tuple((f, QUANT_SCALE.get(f"{sub}.{f}")) for f in fs) for sub, fs in _FIELDS.items()}
_TOP_KEYS = tuple((k, QUANT_SCALE.get(k)) for k in _TOP)
_TAIL_KEYS = tuple((k, QUANT_SCALE.get(k)) for k in _TAIL)


def pair_dict(q: PairQuant) -> dict:
    """17 §17.20.6 的单对对象（REST、expand）。"""
    d: dict[str, Any] = {k: _val_s(getattr(q, k), sc) for k, sc in _TOP_KEYS}
    for sub in SUB:
        m = getattr(q, sub)
        if sub == "radar" and m is None:      # 识别物没有雷达接收（整帧一致）：省略
            continue
        d[sub] = None if m is None else {f: _val_s(getattr(m, f), sc) for f, sc in _SUB_KEYS[sub]}
    for k, sc in _TAIL_KEYS:
        d[k] = _val_s(getattr(q, k), sc)
    return d


def quant_frame(target: str, mode: str, t_ns: int, pairs: Sequence[PairQuant]) -> dict:
    """`awr.target.quant.v1`（展开形式：REST SB-73 与 `expand_soa` 的结果）。"""
    return {"t_sim_ns": int(t_ns), "target": str(target), "criterion_mode": str(mode), "pairs": [pair_dict(q) for q in pairs]}


def _col(vals: list) -> Any:
    """一列：全列相同（且 ≥ 2 项）折叠为标量。"""
    if len(vals) >= 2 and all(v == vals[0] and type(v) is type(vals[0]) for v in vals[1:]):
        return vals[0]
    return vals


def encode_soa(frame: dict) -> dict:
    """展开形式 → 紧凑列式 `soa1`（topic 载荷）。子对象整体缺失（识别物没有该模态的敏感体）时省略该键；个别对缺失
    （如反向感知）时该对各列为 None。"""
    pairs = frame["pairs"]
    n = len(pairs)
    out: dict[str, Any] = {"t_sim_ns": frame["t_sim_ns"], "target": frame["target"], "criterion_mode": frame["criterion_mode"],
                           "enc": "soa1", "n": n}
    for k in (*_TOP, *_TAIL):
        out[k] = _col([_q(p.get(k), QUANT_SCALE.get(k)) if not isinstance(p.get(k), (bool, str)) else p.get(k) for p in pairs])
    for sub in SUB:
        if not any(sub in p for p in pairs):
            continue
        if not any(isinstance(p.get(sub), dict) for p in pairs):
            out[sub] = None
            continue
        cols: dict[str, Any] = {}
        for f in _FIELDS[sub]:
            key = f"{sub}.{f}"
            vals = []
            for p in pairs:
                m = p.get(sub)
                v = m.get(f) if isinstance(m, dict) else None
                vals.append(v if isinstance(v, (bool, str)) or v is None else _q(v, QUANT_SCALE.get(key)))
            cols[f] = _col(vals)
        if any(not isinstance(p.get(sub), dict) for p in pairs):
            cols["_has"] = [isinstance(p.get(sub), dict) for p in pairs]
        out[sub] = cols
    return out


def _qv(v: Any, scale: float | None) -> Any:
    """`encode_soa` 对展开值的取值规则直接作用于原始值：布尔、字符串与 None 原样，其余 `_q`（展开值本身为 `_q` 的整数除以定标，
    再次 `_q` 得到同一整数，因此与先 `pair_dict` 再 `encode_soa` 逐值相同）。"""
    if v is None or v is True or v is False or type(v) is str:
        return v
    if isinstance(v, (bool, str)) or scale is None:
        return v
    x = float(v)
    if x != x or x == _INF or x == -_INF:
        return None
    k = round(x * scale)
    return I32 if k > I32 else (-I32 if k < -I32 else k)       # 同 `_q`：max(−I32, min(I32, round(x·scale)))


def encode_pairs(target: str, mode: str, t_ns: int, pairs: Sequence[PairQuant]) -> dict:
    """`encode_soa(quant_frame(target, mode, t_ns, pairs))` 的直接形式（D2-FX-R2-sim 第 2 次：场与读数服务 10 ms/仿真秒中约 1/3 在
    逐值展开再编码；键次序、列折叠、`_has` 与缺省模态的规则相同，msgpack 字节一致）。"""
    n = len(pairs)
    out: dict[str, Any] = {"t_sim_ns": int(t_ns), "target": str(target), "criterion_mode": str(mode), "enc": "soa1", "n": n}
    for k, sc in (*_TOP_KEYS, *_TAIL_KEYS):
        if sc is None:
            out[k] = _col([getattr(q, k) for q in pairs])   # 无定标：原样（_qv 对 None、布尔、字符串与无定标值均原样返回）
        else:
            out[k] = _col([_qv(getattr(q, k), sc) for q in pairs])
    for sub in SUB:
        ms = [getattr(q, sub) for q in pairs]
        if sub == "radar" and all(m is None for m in ms):
            continue                                       # 展开形式省略该键（pair_dict），编码时同样省略
        if n == 0:
            continue
        if all(m is None for m in ms):
            out[sub] = None
            continue
        cols: dict[str, Any] = {}
        for f, sc in _SUB_KEYS[sub]:
            if sc is None:
                cols[f] = _col([None if m is None else getattr(m, f) for m in ms])
            else:
                cols[f] = _col([None if m is None else _qv(getattr(m, f), sc) for m in ms])
        if any(m is None for m in ms):
            cols["_has"] = [m is not None for m in ms]
        out[sub] = cols
    return out


def expand_soa(m: dict) -> dict:
    """`soa1` → 展开形式（与 TS `decodeTargetQuant` 同一规则；测试与工具用）。"""
    if m.get("enc") != "soa1":
        return m
    n = int(m["n"])

    def at(col: Any, i: int) -> Any:
        return col[i] if isinstance(col, list) else col

    pairs = []
    for i in range(n):
        p: dict[str, Any] = {}
        for k in _TOP:
            p[k] = _deq(at(m.get(k), i), QUANT_SCALE.get(k)) if k not in ("uav", "model_id", "los") else at(m.get(k), i)
        for sub in SUB:
            if sub not in m:
                continue
            cols = m[sub]
            if cols is None:
                p[sub] = None
                continue
            has = cols.get("_has")
            if has is not None and not at(has, i):
                p[sub] = None
                continue
            p[sub] = {f: (_deq(at(cols.get(f), i), QUANT_SCALE.get(f"{sub}.{f}"))
                          if not isinstance(at(cols.get(f), i), (bool, str)) else at(cols.get(f), i)) for f in _FIELDS[sub]}
        for k in _TAIL:
            v = at(m.get(k), i)
            p[k] = v if isinstance(v, bool) else _deq(v, QUANT_SCALE.get(k))
        pairs.append(p)
    return {"t_sim_ns": m["t_sim_ns"], "target": m["target"], "criterion_mode": m["criterion_mode"], "pairs": pairs}
