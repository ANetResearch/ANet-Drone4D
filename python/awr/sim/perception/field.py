"""飞行器辐射场剖面、识别物感知体表、确定性采样图案与按订阅生成的场服务（M19-FR-032 至 FR-036；AWR-04 §15.11，ADR-125；
M18-M19 PRD §15.2.2、§15.2.3；17 §17.20.6 `awr.field.profile.v1`、`awr.field.volumes.v1`）。

物理量全部在服务端计算（P-02），前端只按确定性图案插值与绘制（`engine/perception/field.ts` 解码与插值，M06 `engine/field` 绘制）。

**辐射场剖面**（每架飞行器，按所选指标）：共享径向网格 `r_k = r_min·(R/r_min)^(k/31)`（r_min = 2 m，R 为本帧各机 r_max 的最大值按
√2 阶梯上取整，≤ 2000 m）上的 `v_r[32]`，仰角因子 `f_el[6]`（e = −75、−45、−15、15、45、75°，e 为观察者看飞行器的仰角，按最近
的仰角格取值），每个仰角格的截止距离 `cut_el_m[6]`（0 为不截止）；场值 `v(r, e) = v_r(r) ⊕ f_el(e)`（add：dB 相加；mul：相乘），
r 超过 cut 时不绘制。径向插值：add 量对 ln r 线性，mul 量对 (ln r, ln v) 线性。

| 指标 | v_r | f_el | cut | 不绘制 |
|---|---|---|---|---|
| acoustic（dB(A)） | `L_pA(r) = 10·lg Σ_b 10^((L_W,b(T) − 20·lg r − 11 − alpha_b·r/1000 + A_b)/10)` | 0 | 0 | `L_pA < max(20, L_bg − 10)` |
| visual（arcmin） | `θ = D_side/r`（arcmin） | `√(A(e)/A_side)`，`A(e)` 为顶面（|e| ≥ 45°）或侧面 | 对比度 `ln(|C(e)|/0.05)/sigma_550`（C 为对天 e > 0 或对地）；夜间未开灯 night_visible_m，开灯 0 | `θ < 0.5'` |
| thermal（px） | 参考热像仪（`lwir640` 25 mm，IFOV 0.48 mrad）`N_th = d_hot/(r·IFOV)` | 同视觉 | 0 | `τ_LW·ε·ΔT·min(1, N_th²) < NETD` |
| radar（dB） | 参考雷达（`mmw77`，13.2 dB @ 180 m、1 m²）`SNR = 13.2 + 10·lg sigma − 40·lg(r/180) − 2·gamma_rain·r/1000` | 0 | 0 | `SNR < −10 dB` |

另给每机的参考壳半径 `shell_m`：声学为缺省"人"（听阈 20 dB(A)、警觉度 0.5）在当前背景下的可闻半径（M18 `acoustic_radius`），视觉为
缺省"人"的可见半径（M18 `visual_radius`），热为 N = 3.5（D 级）半径，雷达为 SNR = 13.2 dB（D 级）半径；帧级 `iso[]` 为虚线参考壳的
场值（声学 `L_bg`、视觉 1'、热 14 px）。

**感知体表**（每个识别物、每个启用的敏感体、会话中的每个机型）：方位格 `a_i = i·45°`（相对识别物航向）× 仰角格 `EL_DEG`（识别物看
飞行器的仰角）的有效半径 `r_eff[8, 6]` 与过渡宽度 `w_m`（P 从 0.9 到 0.1 的径向距离之半，取参考方向）。与 M18 `effective_radius`
同参数（不截断到 radius_max，≤ 2000 m）：声学按方位计 `g_back`；视觉按仰角取顶面（e ≥ 45°，M18 判据口径）或侧面与对天（e > 0）或对地
对比度；雷达（P1）在接收视场内为 SNR = snr_req 的距离、视场外为 0。前端 `P = 1/(1 + exp((r − r_eff(dir))/(w/2.197)))`，P(r_eff) = 0.5。

**线格式**（非关键帧省略 `r_m`、`el_deg`、`bands`、`iso`、`ref`、`shell_ref`，沿用上一关键帧）：剖面 `v_r` 为 u16（帧级 `v_enc`：dB 量 `lin` 为 `round((v − v_off)·10)`；线性量 `log` 为 `round((lg v + 8)·4000)`，
相对分辨率 0.06%，使 ≤ 2% 的插值一致性在 1' 量级也成立；0xFFFF 不绘制，末尾的 0xFFFF 省略），`f_el` 为 i16（×100，全相同时为 1 项），
`cut_el_m` 为 u16（m，全相同时为 1 项）；同帧中数组完全相同的机体以 `same: <前一项下标>` 引用（与被引用项机型相同时省略 `model_id`）。感知体表 `r_eff_dm` 为 u16 分米，每机型一组，
按冗余折叠为 1（均匀）、6（只随仰角）、8（只随方位）或 48 项（方位主序 `i·6 + j`）。剖面 rev：与上次发出的值相比 dB 量最大差
> 0.5 dB 或线性量最大相对差 > 2%（或可绘制范围变化）时 rev + 1 并发出数组，否则只发 `{id, rev}`；每 5 s 与网格或订阅变化时关键帧。
感知体表：变更时只发 rev 变化的识别物，每 30 s、机型集合或识别物集合变化与新订阅时关键帧。

**确定性图案**（`pattern`，与前端 M06 同式）：第 i 点 `h1 = H2(i+1)`、`h2 = H3(i+1)`、`h3 = H5(i+1)`；方向 `z = 1 − 2·h2`、
`φ = 2π·h3 + j·2.39996`（j 为锚点序号）；半径 `r = r_min·(r_max/r_min)^h1`。显式采样（SB-78，P1）按此图案给出 World ENU 点与
场值（飞行器锚点：精确场值，仰角取所在仰角格；识别物锚点：表模型（方位线性、仰角最近格、过渡宽度按 r_eff 比例缩放）的
显示用概率，配置体外的点剔除）。

**服务**（`FieldService`，sim-core 慢任务 10 Hz【墙钟】检查）：只在有订阅时生成与编码（兴趣下推 `streams.field` = 指标码，
`streams.quant` = 识别物 target_no，`streams.fsub` = 订阅代数，新订阅时立即关键帧）；剖面 1 Hz【墙钟】、感知体表 2 s、逐对读数 2 Hz【仿真】
（墙钟间隔 ≥ 0.25 s）；总线键 `state/sim-core/field`（items 以指标名与 `volumes` 为键）与 `state/sim-core/target_quant`（items 以识别物
id 为键），每个载荷只编码一次（`use_single_float`），网关按键拆为 `field/{kind}`、`field/volumes`、`target/{id}/quant`。
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from awr.sim.targets.acoustic import ALPHA_DB_KM, acoustic_radii, acoustic_radius, threshold_bands
from awr.sim.targets.geometry import body_volumes, inside_points, wrap_pi
from awr.sim.targets.model import band_adj_of, radar_rx_of
from awr.sim.targets.radar import gamma_rain_db_km, radar_radius
from awr.sim.targets.signature import VehicleSignature
from awr.sim.targets.sound import A_W
from awr.sim.targets.visual import C_MIN, theta_th_rad, visual_radius

from .quant import (
    ARCMIN_PER_RAD,
    S_AC_DB,
    S_RD_DB,
    FieldEnv,
    QuantTarget,
    QuantUav,
    ReverseQ,
    encode_pairs,
    field_env,
    quant_frame,
    quant_pairs,
)

__all__ = [
    "AZ_DEG", "EL_DEG", "FIELD_KINDS", "KIND_CODE", "N_R", "R_CAP_M", "FieldError", "FieldService", "Profile", "VolumeTable", "decode_profile_uav",
    "default_shell_m", "field_profile", "field_value", "frame_grid", "halton", "pattern", "profile_frame", "table_r_eff", "volume_frame", "volume_p",
    "volume_table",
]

FIELD_KINDS = ("acoustic", "visual", "thermal", "radar")
KIND_CODE = {"acoustic": 1, "visual": 2, "thermal": 3, "radar": 4, "volumes": 5}
CODE_KIND = {v: k for k, v in KIND_CODE.items()}
EL_DEG = (-75.0, -45.0, -15.0, 15.0, 45.0, 75.0)
AZ_DEG = (0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0)
N_R = 32
R_MIN_UAV = 2.0
R_MIN_BODY = 1.0
R_CAP_M = 2000.0
LADDER = tuple(sorted(R_CAP_M / 2.0 ** (k / 2.0) for k in range(0, 15)))
UNIT = {"acoustic": "dba", "visual": "arcmin", "thermal": "px", "radar": "db"}
COMBINE = {"acoustic": "add", "visual": "mul", "thermal": "mul", "radar": "add"}
BANDS = {"acoustic": (20.0, 30.0, 40.0, 50.0, 60.0), "visual": (0.5, 1.0, 2.0, 5.0, 10.0), "thermal": (0.0, 1.0, 2.0, 3.5, 14.0),
         "radar": (-10.0, 0.0, 6.0, 13.2, 20.0)}
V_OFF = {"acoustic": 0.0, "visual": 0.0, "thermal": 0.0, "radar": -10.0}
V_ENC = {"acoustic": "lin", "visual": "log", "thermal": "log", "radar": "lin"}
V_SCALE = 10.0
LOG_SCALE = 4000.0
LOG_OFF = 8.0
F_SCALE = 100.0
NONE_U16 = 0xFFFF
ACOUSTIC_FLOOR_DBA = 20.0
VISUAL_FLOOR_ARCMIN = 0.5
RADAR_FLOOR_DB = -10.0
GOLDEN_ANGLE = 2.39996
LN9 = math.log(9.0)
W_VISUAL_REL = 0.73          # TTPF 在 x = 1.75 与 0.49 处的半宽（r_eff 的倍数）
# 参考传感器（AWR-04 §15.11.3）
THERMAL_LENS_MM = 25.0
REV_DB = 0.5
REV_REL = 0.02
PROFILE_PERIOD_NS = 1_000_000_000
PROFILE_KEYFRAME_NS = 5_000_000_000
VOLUME_PERIOD_NS = 2_000_000_000
VOLUME_KEYFRAME_NS = 30_000_000_000
QUANT_SIM_NS = 500_000_000
QUANT_WALL_MIN_NS = 250_000_000
SAMPLES_MAX = 4096
DELTA_OMIT = frozenset(("r_m", "el_deg", "bands", "iso", "ref", "shell_ref"))
VOL_MODELS_MAX = 8


class FieldError(ValueError):
    """场查询参数非法（564 FIELD_QUERY_INVALID，17 §17.20.7）或识别物不存在（543）。"""

    def __init__(self, code: int, field_: str, why: str = "INVALID", **extra: Any) -> None:
        self.code = int(code)
        self.detail: dict[str, Any] = {"field": field_, "why": why, **{k: v for k, v in extra.items() if v is not None}}
        super().__init__(f"{code} {field_} {why}")


# ---------------------------------------------------------------- 参考传感器
def _thermal_ref() -> tuple[float, float, float]:
    """(IFOV rad, NETD K, 镜头 mm)：`lwir640` 25 mm。"""
    from .specs import lwir640

    sn = lwir640(THERMAL_LENS_MM)
    p_um = float(sn.pixel_um)
    f_mm = float(THERMAL_LENS_MM)
    netd = float(getattr(sn, "netd_k", 0.05) or 0.05)
    return p_um * 1e-6 / (f_mm * 1e-3), netd, f_mm


def _radar_ref() -> Any:
    from .specs import mmw77

    return mmw77()


# ---------------------------------------------------------------- 剖面（纯函数）
def l_pa(lw_bands: NDArray, r_m: NDArray) -> NDArray:
    """合成 A 计权到达声压 L_pA(r)（dB(A)，自由场、无方向性）。"""
    r = np.maximum(np.asarray(r_m, float).reshape(-1), 0.1)[:, None]
    lp = np.asarray(lw_bands, float)[None, :] - 20.0 * np.log10(r) - 11.0 - ALPHA_DB_KM[None, :] * r / 1000.0
    return 10.0 * np.log10(np.sum(10.0 ** ((lp + A_W[None, :]) / 10.0), axis=1))


def _solve_dec(f: Callable[[float], float], target: float, lo: float, hi: float, iters: int = 50) -> float:
    """单调递减 f 的 f(r) = target（[lo, hi]；f(hi) ≥ target 时为 hi，f(lo) < target 时为 lo）。"""
    if f(hi) >= target:
        return hi
    if f(lo) < target:
        return lo
    a, b = math.log(lo), math.log(hi)
    for _ in range(iters):
        m = 0.5 * (a + b)
        if f(math.exp(m)) >= target:
            a = m
        else:
            b = m
    return math.exp(0.5 * (a + b))


def _acoustic_floor(env: FieldEnv) -> float:
    return max(ACOUSTIC_FLOOR_DBA, env.l_bg_dba - 10.0)


def _thermal_dt_app(sig: VehicleSignature, env: FieldEnv, r: NDArray, ifov: float) -> NDArray:
    from .optics import tau

    r = np.maximum(np.asarray(r, float), 0.1)
    n = sig.d_hot_m / (r * ifov)
    return np.asarray(tau(env.mor_m, 10000.0, r, env.rho_v_gm3), float) * sig.ir_emissivity * sig.ir_delta_t_k * np.minimum(1.0, n * n)


def _radar_snr(sig: VehicleSignature, env: FieldEnv, r: NDArray) -> NDArray:
    rd = _radar_ref()
    r = np.maximum(np.asarray(r, float), 0.1)
    gamma = float(rd.rain_k) * env.rain_mmph ** float(rd.rain_alpha) if env.rain_mmph > 0 else 0.0
    return (float(rd.snr_d_db) + 10.0 * np.log10(max(sig.rcs_m2, 1e-9) / float(rd.sigma_ref_m2)) - 40.0 * np.log10(r / float(rd.r_ref_m))
            - 2.0 * gamma * r / 1000.0)


def _values(kind: str, sig: VehicleSignature, env: FieldEnv, r: NDArray, thrust_ratio: float = 1.0) -> NDArray:
    """侧面（f_el = 1 或 0 dB）的场值 v(r)，不绘制处为 NaN。"""
    r = np.asarray(r, float).reshape(-1)
    if kind == "acoustic":
        v = l_pa(sig.lw_bands(thrust_ratio), r)
        return np.where(v >= _acoustic_floor(env), v, np.nan)
    if kind == "visual":
        v = math.sqrt(sig.area_m2[1]) / np.maximum(r, 1e-3) * ARCMIN_PER_RAD
        return np.where(v >= VISUAL_FLOOR_ARCMIN, v, np.nan)
    if kind == "thermal":
        ifov, netd, _f = _thermal_ref()
        v = sig.d_hot_m / (np.maximum(r, 0.1) * ifov)
        return np.where(_thermal_dt_app(sig, env, r, ifov) >= netd, v, np.nan)
    if kind == "radar":
        v = _radar_snr(sig, env, r)
        return np.where(v >= RADAR_FLOOR_DB, v, np.nan)
    raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)


def r_max_of(kind: str, sig: VehicleSignature, env: FieldEnv, thrust_ratio: float = 1.0) -> float:
    """可绘制的最大距离（≤ 2000 m）。"""
    lo, hi = R_MIN_UAV, R_CAP_M
    if kind == "acoustic":
        lw = sig.lw_bands(thrust_ratio)
        return _solve_dec(lambda r: float(l_pa(lw, np.array([r]))[0]), _acoustic_floor(env), lo, hi)
    if kind == "visual":
        return float(min(hi, max(lo, math.sqrt(sig.area_m2[1]) * ARCMIN_PER_RAD / VISUAL_FLOOR_ARCMIN)))
    if kind == "thermal":
        ifov, netd, _f = _thermal_ref()
        return _solve_dec(lambda r: float(_thermal_dt_app(sig, env, np.array([r]), ifov)[0]), netd, lo, hi)
    if kind == "radar":
        return _solve_dec(lambda r: float(_radar_snr(sig, env, np.array([r]))[0]), RADAR_FLOOR_DB, lo, hi)
    raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)


def _f_el(kind: str, sig: VehicleSignature) -> NDArray:
    if COMBINE[kind] == "add":
        return np.zeros(len(EL_DEG))
    top, side, _ = sig.area_m2
    return np.array([math.sqrt(top / side) if abs(e) >= 45.0 else 1.0 for e in EL_DEG])


def _cut_el(kind: str, sig: VehicleSignature, env: FieldEnv) -> NDArray:
    """每个仰角格的截止距离（m，0 为不截止；只有视觉）。"""
    out = np.zeros(len(EL_DEG))
    if kind != "visual":
        return out
    if env.band == "night":
        return out if sig.lights_on else np.full(len(EL_DEG), float(sig.night_visible_m))
    s = env.sigma550
    for j, e in enumerate(EL_DEG):
        c = abs(sig.contrast_sky if e > 0.0 else sig.contrast_ground)
        if c <= C_MIN:
            out[j] = 1.0
        elif s > 0.0:
            out[j] = math.log(c / C_MIN) / s
    return out


def default_shell_m(kind: str, sig: VehicleSignature, env: FieldEnv, thrust_ratio: float = 1.0) -> float:
    """参考壳半径（m）：声学、视觉为缺省"人"（M18 同一函数），热 N = 3.5，雷达 SNR = 13.2 dB。"""
    if kind == "acoustic":
        thr = threshold_bands(env.l_bg_bands, sig.masking_db(), 20.0, 0.5, 0.0)
        return float(min(R_CAP_M, acoustic_radius(sig.lw_bands(thrust_ratio), thr)))
    if kind == "visual":
        return float(visual_radius(sig, env.t, 1.0, 0.5, cap_m=R_CAP_M))
    if kind == "thermal":
        ifov, _netd, _f = _thermal_ref()
        return float(min(sig.d_hot_m / (3.5 * ifov), r_max_of(kind, sig, env)))
    if kind == "radar":
        rd = _radar_ref()
        return _solve_dec(lambda r: float(_radar_snr(sig, env, np.array([r]))[0]), float(rd.snr_d_db), 0.5, R_CAP_M)
    raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)


@dataclass(frozen=True)
class Profile:
    """一架飞行器的剖面（共享网格上的值；未量化）。"""

    kind: str
    unit: str
    combine: str
    r_m: NDArray          # [32]
    v_r: NDArray          # [32]，NaN 为不绘制
    f_el: NDArray         # [6]
    cut_el_m: NDArray     # [6]，0 为不截止
    r_max_m: float
    shell_m: float
    rev: int = 0


def frame_grid(r_max_m: float, r_min_m: float = R_MIN_UAV) -> NDArray:
    """本帧共享径向网格：R = 不小于 r_max 的最小阶梯值（√2 阶梯，≤ 2000 m）。"""
    R = next((x for x in LADDER if x >= r_max_m - 1e-9), R_CAP_M)
    R = max(R, r_min_m * 2.0)
    return r_min_m * (R / r_min_m) ** (np.arange(N_R) / (N_R - 1))


def field_profile(kind: str, sig: VehicleSignature, env: FieldEnv, *, thrust_ratio: float = 1.0, r_grid: NDArray | None = None) -> Profile:
    """M19-FR-032：剖面（缺省网格为本机 r_max 的阶梯网格）。"""
    if kind not in FIELD_KINDS:
        raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)
    rmax = r_max_of(kind, sig, env, thrust_ratio)
    r = frame_grid(rmax) if r_grid is None else np.asarray(r_grid, float)
    cut = _cut_el(kind, sig, env)
    return Profile(kind, UNIT[kind], COMBINE[kind], r, _values(kind, sig, env, r, thrust_ratio), _f_el(kind, sig), cut, rmax,
                   default_shell_m(kind, sig, env, thrust_ratio))


def field_value(kind: str, sig: VehicleSignature, env: FieldEnv, r_m: NDArray, el_deg: NDArray, *, thrust_ratio: float = 1.0) -> NDArray:
    """精确场值（显式采样与对拍）：径向按公式，仰角取最近的仰角格（与前端同一仰角模型），超出截止距离为 NaN。"""
    r = np.asarray(r_m, float).reshape(-1)
    e = np.asarray(el_deg, float).reshape(-1)
    j = np.argmin(np.abs(e[:, None] - np.asarray(EL_DEG)[None, :]), axis=1)
    v = _values(kind, sig, env, r, thrust_ratio)
    f = _f_el(kind, sig)[j]
    cut = _cut_el(kind, sig, env)[j]
    v = v + f if COMBINE[kind] == "add" else v * f
    return np.where((cut > 0.0) & (r > cut), np.nan, v)


# ---------------------------------------------------------------- 剖面线格式
def _q_one(x: float, enc: str, v_off: float) -> int:
    if not math.isfinite(x) or (enc == "log" and x <= 0.0):
        return NONE_U16
    y = (math.log10(x) + LOG_OFF) * LOG_SCALE if enc == "log" else (x - v_off) * V_SCALE
    return int(min(NONE_U16 - 1, max(0, round(y))))


def _dq_one(q: int, enc: str, v_off: float) -> float:
    if int(q) == NONE_U16:
        return math.nan
    return 10.0 ** (int(q) / LOG_SCALE - LOG_OFF) if enc == "log" else int(q) / V_SCALE + v_off


def _q_vr(v: NDArray, enc: str, v_off: float) -> list[int]:
    out = [_q_one(float(x), enc, v_off) for x in np.asarray(v, float)]
    while out and out[-1] == NONE_U16:
        out.pop()
    return out


def _collapse(a: list[int]) -> list[int]:
    return a[:1] if a and all(x == a[0] for x in a) else a


def _q_fel(f: NDArray) -> list[int]:
    return _collapse([int(max(-32768, min(32767, round(float(x) * F_SCALE)))) for x in f])


def _q_cut(c: NDArray, r_top: float) -> list[int]:
    return _collapse([0 if (x <= 0.0 or x >= r_top) else int(min(NONE_U16, max(1, round(float(x))))) for x in c])


def decode_profile_uav(item: dict, enc: str, v_off: float) -> tuple[NDArray, NDArray, NDArray]:
    """线格式一项 → (v_r[32]，NaN 为不绘制；f_el[6]；cut_el_m[6]，0 为不截止)（与 TS `decodeFieldProfile` 同一规则）。"""
    vr = np.full(N_R, np.nan)
    for k, x in enumerate(item.get("v_r") or []):
        if k < N_R:
            vr[k] = _dq_one(int(x), enc, v_off)
    fe = list(item.get("f_el") or [0])
    fe = fe * len(EL_DEG) if len(fe) == 1 else fe
    cu = list(item.get("cut_el_m") or [0])
    cu = cu * len(EL_DEG) if len(cu) == 1 else cu
    return vr, np.array(fe, float) / F_SCALE, np.array(cu, float)


def _changed(kind: str, a: tuple[NDArray, NDArray, NDArray], b: tuple[NDArray, NDArray, NDArray]) -> bool:
    va, vb = a[0], b[0]
    if not np.array_equal(np.isnan(va), np.isnan(vb)):
        return True
    m = ~np.isnan(va)
    if COMBINE[kind] == "add":
        if m.any() and float(np.max(np.abs(va[m] - vb[m]))) > REV_DB:
            return True
        if float(np.max(np.abs(a[1] - b[1]))) > REV_DB:
            return True
    else:
        if m.any() and float(np.max(np.abs(va[m] - vb[m]) / np.maximum(np.abs(vb[m]), 1e-9))) > REV_REL:
            return True
        if float(np.max(np.abs(a[1] - b[1]) / np.maximum(np.abs(b[1]), 1e-9))) > REV_REL:
            return True
    ca, cb = a[2], b[2]
    if not np.array_equal(ca > 0, cb > 0):
        return True
    mc = ca > 0
    return bool(mc.any() and float(np.max(np.abs(ca[mc] - cb[mc]) / np.maximum(cb[mc], 1.0))) > REV_REL)


def profile_frame(kind: str, t_ns: int, r_m: NDArray, env: FieldEnv, items: list[dict]) -> dict:
    """`awr.field.profile.v1`。items 为各机的线格式项（`{id, model_id, rev, v_r, f_el, cut_el_m, shell_m}` 或 `{id, rev}`）。"""
    iso = {"acoustic": [{"id": "bg", "v": _c(round(env.l_bg_dba, 1))}], "visual": [{"id": "ref", "v": 1}], "thermal": [{"id": "r", "v": 14}],
           "radar": []}[kind]
    ref: dict[str, Any] = {}
    if kind == "thermal":
        ref = {"sensor": "lwir640", "lens_mm": THERMAL_LENS_MM}
    elif kind == "radar":
        ref = {"sensor": "mmw77"}
    return {"t_sim_ns": int(t_ns), "kind": kind, "unit": UNIT[kind], "combine": COMBINE[kind], "r_m": [float(x) for x in r_m],
            "el_deg": [_c(x) for x in EL_DEG], "bands": [_c(x) for x in BANDS[kind]], "v_enc": V_ENC[kind], "v_off": _c(V_OFF[kind]), "iso": iso, "shell_ref": "person" if kind in ("acoustic", "visual")
            else "D", "ref": ref, "uav": items}


# ---------------------------------------------------------------- 感知体表（纯函数）
@dataclass(frozen=True)
class VolumeTable:
    r_eff_m: NDArray      # [8, 6]（方位相对识别物航向 × 仰角）
    w_m: float            # 参考方向的过渡宽度


def volume_table(raw: dict, body: dict, sig: VehicleSignature, env: FieldEnv, alertness: float) -> VolumeTable:
    """M19-FR-033：一个（识别物，敏感体，机型）的有效半径表（与 M18 `effective_radius` 同参数，不截断到 radius_max，≤ 2000 m）。"""
    kind = body.get("kind")
    a = float(alertness)
    n_az, n_el = len(AZ_DEG), len(EL_DEG)
    if kind == "acoustic":
        lw = sig.lw_bands()
        adj = band_adj_of(raw)
        thr = threshold_bands(env.l_bg_bands, sig.masking_db(), float(raw.get("hearing_th_dba", 20.0)), a, float(raw.get("acoustic_adv_db", 0.0)))
        off = math.radians(float(body.get("azimuth_offset_deg", 0.0)))
        gb = float(body.get("gain_back_db", 0.0))
        az = np.radians(np.asarray(AZ_DEG))
        g = gb * (1.0 - np.cos(az - off)) / 2.0
        r_az = np.minimum(acoustic_radii(lw, thr, g, adj_db=adj), R_CAP_M)
        r0 = float(min(acoustic_radius(lw, thr, adj_db=adj), R_CAP_M))
        w = 0.0
        if r0 > 0.0:
            wgt = A_W if adj is None else A_W + adj
            lp = lw - 20.0 * math.log10(r0) - 11.0 - ALPHA_DB_KM * r0 / 1000.0 + wgt - thr
            b = int(np.argmax(lp))
            w = LN9 * S_AC_DB / (8.686 / r0 + float(ALPHA_DB_KM[b]) / 1000.0)
        return VolumeTable(np.repeat(r_az[:, None], n_el, axis=1), float(w))
    if kind == "visual":
        th = theta_th_rad(float(raw.get("visual_theta0_arcmin", 1.0)), a, env.band)
        top, side, _ = sig.area_m2
        s = env.sigma550
        r_el = np.zeros(n_el)
        for j, e in enumerate(EL_DEG):
            if env.band == "night":
                r_ang = math.inf if sig.lights_on else float(sig.night_visible_m)
            else:
                r_ang = math.sqrt(top if e >= 45.0 else side) / th
            c = abs(sig.contrast_sky if e > 0.0 else sig.contrast_ground)
            r_c = 0.0 if c <= C_MIN else (math.inf if s <= 0.0 else math.log(c / C_MIN) / s)
            r_el[j] = min(r_ang, r_c, R_CAP_M)
        ref = r_el[EL_DEG.index(15.0)]
        return VolumeTable(np.repeat(r_el[None, :], n_az, axis=0), float(W_VISUAL_REL * ref))
    if kind == "radar":
        rx = radar_rx_of(raw)
        if rx is None:
            return VolumeTable(np.zeros((n_az, n_el)), 0.0)
        gamma = gamma_rain_db_km(env.rain_mmph)
        r = float(min(radar_radius(rx, sig.rcs_m2, gamma), R_CAP_M))
        off = math.radians(float(body.get("azimuth_offset_deg", 0.0)))
        fa, fe = math.radians(float(rx["fov_az_deg"])), float(rx["fov_el_deg"])
        tab = np.zeros((n_az, n_el))
        for i, az in enumerate(AZ_DEG):
            if abs(float(wrap_pi(math.radians(az) - off))) <= fa + 1e-9:
                for j, e in enumerate(EL_DEG):
                    if abs(e) <= fe + 1e-9:
                        tab[i, j] = r
        w = LN9 * S_RD_DB / (17.372 / r + 2.0 * gamma / 1000.0) if r > 0.0 else 0.0
        return VolumeTable(tab, float(w))
    return VolumeTable(np.zeros((n_az, n_el)), 0.0)


def volume_p(r_m: NDArray, r_eff_m: NDArray, w_m: float | NDArray) -> NDArray:
    """表模型的显示用概率 `1/(1 + exp((r − r_eff)/(w/2.197)))`（r_eff = 0 时为 0）。"""
    r = np.asarray(r_m, float)
    re = np.asarray(r_eff_m, float)
    s = np.maximum(np.asarray(w_m, float), 1e-6) / LN9
    z = np.clip((r - re) / s, -700.0, 700.0)
    return np.where(re > 0.0, 1.0 / (1.0 + np.exp(z)), 0.0)


def table_r_eff(tab: NDArray, az_rel_rad: NDArray, el_rad: NDArray) -> NDArray:
    """前端表模型（`engine/perception/field.ts` `volumeREff`）：方位在 45° 格间线性（环绕），仰角取最近格。"""
    a = (np.degrees(np.asarray(az_rel_rad, float)) % 360.0) / 45.0
    i0 = np.floor(a).astype(int) % 8
    i1 = (i0 + 1) % 8
    t = a - np.floor(a)
    j = np.argmin(np.abs(np.degrees(np.asarray(el_rad, float)).reshape(-1, 1) - np.asarray(EL_DEG)[None, :]), axis=1)
    tab = np.asarray(tab, float)
    return tab[i0, j] * (1.0 - t) + tab[i1, j] * t


def _q_table(tab: NDArray) -> list[int]:
    dm = np.clip(np.round(np.asarray(tab, float) * 10.0), 0, NONE_U16).astype(int)
    if np.all(dm == dm.flat[0]):
        return [int(dm.flat[0])]
    if np.all(dm == dm[0:1, :]):
        return [int(x) for x in dm[0]]                      # 只随仰角（6）
    if np.all(dm == dm[:, 0:1]):
        return [int(x) for x in dm[:, 0]]                   # 只随方位（8）
    return [int(x) for x in dm.reshape(-1)]                 # 方位主序（48）


def expand_table(q: Sequence[int]) -> NDArray:
    """`r_eff_dm` 一组 → r_eff [8, 6]（m）。"""
    a = np.asarray(list(q), float) / 10.0
    n_az, n_el = len(AZ_DEG), len(EL_DEG)
    if a.size == 1:
        return np.full((n_az, n_el), a[0])
    if a.size == n_el:
        return np.repeat(a[None, :], n_az, axis=0)
    if a.size == n_az:
        return np.repeat(a[:, None], n_el, axis=1)
    return a.reshape(n_az, n_el)


def volume_frame(t_ns: int, keyframe: bool, models: Sequence[str], items: list[dict]) -> dict:
    return {"t_sim_ns": int(t_ns), "keyframe": bool(keyframe), "models": list(models), "az_deg": list(AZ_DEG), "el_deg": list(EL_DEG),
            "tgt": items}


# ---------------------------------------------------------------- 确定性图案
def halton(i: NDArray | int, base: int) -> NDArray:
    """Halton 序列 H_base(i)（i ≥ 1）。"""
    i = np.asarray(i, np.int64).reshape(-1).copy()
    f = np.ones(i.shape)
    out = np.zeros(i.shape)
    while np.any(i > 0):
        f = f / base
        out = out + f * (i % base)
        i = i // base
    return out


def pattern(n: int, j: int, r_min: float, r_max: float) -> tuple[NDArray, NDArray]:
    """锚点 j 的前 n 个点：(单位方向 [n, 3]，半径 [n])（AWR-04 §15.11.2）。"""
    idx = np.arange(1, n + 1)
    h1, h2, h3 = halton(idx, 2), halton(idx, 3), halton(idx, 5)
    z = 1.0 - 2.0 * h2
    phi = 2.0 * math.pi * h3 + j * GOLDEN_ANGLE
    s = np.sqrt(np.maximum(0.0, 1.0 - z * z))
    u = np.stack([s * np.cos(phi), s * np.sin(phi), z], axis=1)
    r = r_min * (r_max / r_min) ** h1
    return u, r


# ---------------------------------------------------------------- 服务
@dataclass
class _KindState:
    grid_R: float = 0.0
    sent: dict[str, tuple] = field(default_factory=dict)       # uav id -> (v_r, f_el, cut) 解码值（上次发出）
    wire: dict[str, dict] = field(default_factory=dict)        # uav id -> 线格式项（上次发出的数组）
    rev: dict[str, int] = field(default_factory=dict)
    model: dict[str, str] = field(default_factory=dict)
    last_ns: int = -1
    last_key_ns: int = -1
    frame: dict | None = None


@dataclass
class _VolState:
    models: tuple[str, ...] = ()
    tids: tuple[str, ...] = ()
    sent: dict[str, list] = field(default_factory=dict)        # tid -> 线格式 bodies（上次发出）
    rev: dict[str, int] = field(default_factory=dict)
    tsig: dict[str, tuple] = field(default_factory=dict)       # tid -> 输入签名（未变则跳过）
    last_ns: int = -1
    last_key_ns: int = -1
    frame: dict | None = None


class FieldService:
    """按订阅生成剖面、感知体表与逐对读数（M19-FR-034）；REST 查询（M19-FR-035）共用同一缓存。"""

    def __init__(self, prt: Any, *, clock: Callable[[], int] | None = None) -> None:
        from .wire import Publisher

        self.prt = prt
        self.S: Any = None
        self.pub = Publisher()
        self.clock = clock or time.monotonic_ns
        self.local_kinds: set[str] = set()
        self.local_volumes = False
        self.local_quant: set[str] = set()
        self.kinds: dict[str, _KindState] = {}
        self.vol = _VolState()
        self.quant_last: dict[str, tuple[int, int]] = {}
        self.vcache: OrderedDict[tuple, Any] = OrderedDict()
        self.pcache: OrderedDict[tuple, Any] = OrderedDict()
        self.vcache_cap = 512
        self._fsub: Any = None
        self._prev_kinds: set[str] = set()
        self._prev_vol = False
        self._last_q: dict[str, tuple] = {}
        self._wanted: tuple | None = None
        self.stats = {"profiles": 0, "volumes": 0, "quant": 0, "vcache_hit": 0, "vcache_miss": 0, "bytes": 0, "ticks": 0}

    @property
    def last_quant(self) -> dict[str, dict]:
        """各识别物最近一次发布的逐对读数（展开形式，按需由读数生成）。"""
        return {tid: quant_frame(*qp) for tid, qp in self._last_q.items()}

    # ------------------------------------------------------------ 输入
    def note_stage(self, S: Any) -> None:
        self.S = S

    @property
    def trt(self) -> Any:
        return self.prt.trt

    def reset(self) -> None:
        self.kinds.clear()
        self.vol = _VolState()
        self.quant_last.clear()
        self._last_q.clear()

    def wanted(self, ctx: Any) -> tuple[set[str], bool, set[str]]:
        """(剖面指标, 感知体表, 逐对读数的识别物 id)：兴趣下推 `streams.field`、`streams.quant` 与本地订阅（测试、工具）的并集。
        D2-FX-R2-sim 第 2 次：按（兴趣下推的两组编码、本地订阅、M18 的 rev 与实体表版本）记忆（每 0.1 s 调用一次，此前每次遍历全部
        识别物）；返回值只读。"""
        st = getattr(ctx, "streams", None) or {}
        trt = self.trt
        fa, qa = st.get("field"), st.get("quant")
        key = (None if fa is None else np.asarray(fa).tobytes(), None if qa is None else np.asarray(qa).tobytes(), "field" in st,
               "quant" in st, frozenset(self.local_kinds), bool(self.local_volumes), frozenset(self.local_quant), id(trt),
               getattr(trt, "rev", None), getattr(getattr(trt, "tab", None), "version", None), id(getattr(trt, "tab", None)))
        hit = self._wanted
        if hit is not None and hit[0] == key:
            return hit[1]
        out = self._wanted_now(st)
        self._wanted = (key, out)
        return out

    def _wanted_now(self, st: dict) -> tuple[set[str], bool, set[str]]:
        codes = {int(x) for x in np.asarray(st.get("field", []), np.int64).reshape(-1).tolist()} if "field" in st else set()
        kinds = {CODE_KIND[c] for c in codes if c in CODE_KIND and CODE_KIND[c] != "volumes"} | set(self.local_kinds)
        vols = KIND_CODE["volumes"] in codes or self.local_volumes
        tids = set(self.local_quant)
        if "quant" in st and self.trt is not None:
            nos = {int(x) for x in np.asarray(st.get("quant", []), np.int64).reshape(-1).tolist()}
            if nos:
                tids |= {T.id for T in self.trt.ordered() if int(T.target_no) in nos}
        return kinds, bool(vols), tids

    def env(self, ctx: Any) -> FieldEnv:
        t_ns = int(getattr(ctx, "t_ns", 0))
        tenv = self.trt.env(ctx) if self.trt is not None else None
        if tenv is None:
            from awr.sim.targets.envview import fallback_env

            tenv = fallback_env(t_ns)
        es = None
        e = getattr(ctx, "env", None)
        fn = getattr(e, "sky_sample", None)
        if callable(fn):
            try:
                es = fn(t_ns)
            except Exception:
                es = None
        return field_env(tenv, es)

    def uavs(self, ctx: Any) -> list[QuantUav]:
        trt = self.trt
        if trt is None or self.S is None:
            return []
        return [QuantUav(f.id, f.model_id, f.pos, f.participant, f.sig) for f in trt.fleet(self.S, ctx)]

    # ------------------------------------------------------------ 剖面
    def profiles(self, kind: str, ctx: Any, *, keyframe: bool, uavs: list[QuantUav] | None = None, env: FieldEnv | None = None) -> dict:
        """生成一帧剖面并更新 rev 状态（REST 与 topic 共用）。"""
        if kind not in FIELD_KINDS:
            raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)
        us = self.uavs(ctx) if uavs is None else uavs
        en = self.env(ctx) if env is None else env
        st = self.kinds.setdefault(kind, _KindState())
        ekey = en.key()
        stat = {(u.model_id, u.sig.lights_on): self._static(kind, u.sig, en, ekey) for u in us}
        rmax = max((x[0] for x in stat.values()), default=R_MIN_UAV * 2.0)
        grid = frame_grid(rmax)
        R = float(grid[-1])
        if abs(R - st.grid_R) > 1e-6:
            st.grid_R = R
            st.sent.clear()
            st.wire.clear()
            keyframe = True
        items: list[dict] = []
        seen_arrays: dict[tuple, int] = {}
        live = {u.id for u in us}
        for u in us:
            wire, dec = self._wire(kind, u.sig, en, ekey, grid, stat[(u.model_id, u.sig.lights_on)])
            prev = st.sent.get(u.id)
            changed = prev is None or _changed(kind, dec, prev)
            if changed:
                st.rev[u.id] = st.rev.get(u.id, -1) + 1
                st.sent[u.id] = dec
                st.wire[u.id] = wire
            rev = st.rev[u.id]
            st.model[u.id] = u.model_id
            if changed or keyframe:
                arr = st.wire[u.id]
                key = (tuple(arr["v_r"]), tuple(arr["f_el"]), tuple(arr["cut_el_m"]), arr["shell_m"])
                it: dict[str, Any] = {"id": u.id, "rev": rev}
                if key in seen_arrays:
                    j = seen_arrays[key]
                    it["same"] = j
                    if items[j].get("model_id") != u.model_id:      # 引用项与本项机型相同时省略
                        it["model_id"] = u.model_id
                else:
                    seen_arrays[key] = len(items)
                    it["model_id"] = u.model_id
                    it.update(arr)
                items.append(it)
            else:
                items.append({"id": u.id, "rev": rev})
        for gone in [k for k in st.sent if k not in live]:
            st.sent.pop(gone, None)
            st.wire.pop(gone, None)
            st.model.pop(gone, None)
        frame = profile_frame(kind, int(getattr(ctx, "t_ns", 0)), grid, en, items)
        frame["keyframe"] = bool(keyframe)
        st.frame = frame
        if not keyframe:      # 非关键帧不重复网格与图例（网格变化必为关键帧）
            frame = {k: v for k, v in frame.items() if k not in DELTA_OMIT}
        self.stats["profiles"] += 1
        return frame

    def _static(self, kind: str, sig: VehicleSignature, en: FieldEnv, ekey: tuple) -> tuple[float, NDArray, NDArray, float]:
        """(r_max, f_el, cut_el_m, shell_m)：按（指标，机型，灯光，环境键）缓存（环境键按照度档、背景整 dB、MOR 100 m 格变化）。"""
        k = ("s", kind, sig.model_id, sig.lights_on, ekey)
        hit = self.pcache.get(k)
        if hit is None:
            hit = (r_max_of(kind, sig, en), _f_el(kind, sig), _cut_el(kind, sig, en), default_shell_m(kind, sig, en))
            self._pput(k, hit)
        return hit

    def _wire(self, kind: str, sig: VehicleSignature, en: FieldEnv, ekey: tuple, grid: NDArray, stat: tuple) -> tuple[dict, tuple]:
        """一架的线格式数组与其解码值（按（指标，机型，灯光，环境键，网格）缓存）。"""
        R = float(grid[-1])
        k = ("w", kind, sig.model_id, sig.lights_on, ekey, round(R, 3))
        hit = self.pcache.get(k)
        if hit is None:
            _rmax, fe, cu, shell = stat
            v = _values(kind, sig, en, grid)
            wire = {"v_r": _q_vr(v, V_ENC[kind], V_OFF[kind]), "f_el": _q_fel(fe), "cut_el_m": _q_cut(cu, R), "shell_m": round(shell, 1)}
            hit = (wire, decode_profile_uav(wire, V_ENC[kind], V_OFF[kind]))
            self._pput(k, hit)
        return hit

    def _pput(self, k: tuple, v: Any) -> None:
        self.pcache[k] = v
        while len(self.pcache) > 256:
            self.pcache.popitem(last=False)

    def profile_full(self, kind: str, ctx: Any) -> dict:
        """REST SB-74：当前剖面（全部机体、展开数组、不引用）。有订阅时为最近一次发出的状态。"""
        st = self.kinds.get(kind)
        if kind not in self._prev_kinds or st is None or st.frame is None or not st.wire:
            self.profiles(kind, ctx, keyframe=True)
            st = self.kinds[kind]
        fr = st.frame
        assert fr is not None
        items = []
        for it in fr["uav"]:
            arr = st.wire.get(it["id"])
            if arr is None:
                continue
            items.append({"id": it["id"], "model_id": st.model.get(it["id"], ""), "rev": st.rev.get(it["id"], 0), **arr})
        out = dict(fr)
        out["uav"] = items
        out["keyframe"] = True
        return out

    # ------------------------------------------------------------ 感知体表
    def _vkey(self, raw: dict, body: dict, sig: VehicleSignature, env: FieldEnv, a: float) -> tuple:
        adj = raw.get("hearing_band_adj_db")
        rx = radar_rx_of(raw)
        return (body.get("kind"), round(float(body.get("gain_back_db", 0.0)), 3), round(float(body.get("azimuth_offset_deg", 0.0)), 3),
                round(float(raw.get("visual_theta0_arcmin", 1.0)), 3), round(float(raw.get("hearing_th_dba", 20.0)), 3),
                round(float(raw.get("acoustic_adv_db", 0.0)), 3), tuple(adj) if isinstance(adj, list) else None,
                tuple(sorted(rx.items())) if rx and body.get("kind") == "radar" else None, sig.model_id, sig.lights_on, env.key(), round(a, 4))

    def table(self, raw: dict, body: dict, sig: VehicleSignature, env: FieldEnv, a: float) -> VolumeTable:
        k = self._vkey(raw, body, sig, env, a)
        hit = self.vcache.get(k)
        if hit is not None:
            self.vcache.move_to_end(k)
            self.stats["vcache_hit"] += 1
            return hit
        self.stats["vcache_miss"] += 1
        v = volume_table(raw, body, sig, env, a)
        self.vcache[k] = v
        while len(self.vcache) > self.vcache_cap:
            self.vcache.popitem(last=False)
        return v

    def qtable(self, raw: dict, body: dict, sig: VehicleSignature, env: FieldEnv, a: float) -> tuple[list[int], float]:
        """线格式的一组表（折叠后的分米整数）与过渡宽度（0.1 m）；与 `table` 同一缓存键。"""
        k = ("q", *self._vkey(raw, body, sig, env, a))
        hit = self.vcache.get(k)
        if hit is None:
            vt = self.table(raw, body, sig, env, a)
            hit = (_q_table(vt.r_eff_m), round(vt.w_m, 1))
            self.vcache[k] = hit       # type: ignore[assignment]
        return hit                     # type: ignore[return-value]

    def _models(self, ctx: Any, us: list[QuantUav]) -> list[tuple[str, VehicleSignature]]:
        seen: dict[str, VehicleSignature] = {}
        for u in us:
            if u.model_id not in seen:
                seen[u.model_id] = u.sig
        if not seen:
            from awr.sim.targets.signature import P600

            seen[P600.model_id] = P600
        return sorted(seen.items())[:VOL_MODELS_MAX]

    def volumes(self, ctx: Any, *, keyframe: bool, uavs: list[QuantUav] | None = None, env: FieldEnv | None = None) -> dict:
        trt = self.trt
        us = self.uavs(ctx) if uavs is None else uavs
        en = self.env(ctx) if env is None else env
        models = self._models(ctx, us)
        mids = tuple(m for m, _s in models)
        Ts = [T for T in (trt.ordered() if trt is not None else []) if T.cfg.raw.get("enabled", True)]
        tids = tuple(T.id for T in Ts)
        st = self.vol
        if mids != st.models or tids != st.tids:
            keyframe = True
            st.models, st.tids = mids, tids
            st.sent = {k: v for k, v in st.sent.items() if k in tids}
            st.tsig = {k: v for k, v in st.tsig.items() if k in tids}
        items: list[dict] = []
        ekey = en.key()
        msig = tuple((m, g.lights_on) for m, g in models)
        for T in Ts:
            raw = T.cfg.raw
            # 识别物的输入签名（配置版本、警觉度、环境、机型与灯光）未变时表不变：跳过重算与比较
            tsig = (int(T.rev), round(float(T.alert), 4), ekey, msig)
            if not keyframe and st.tsig.get(T.id) == tsig and T.id in st.sent:
                continue
            st.tsig[T.id] = tsig
            kinds = ("visual", "acoustic", "radar") if radar_rx_of(raw) else ("visual", "acoustic")
            bodies = []
            for i, b in enumerate(raw.get("sensitivity") or []):
                if not b.get("enabled", True) or b.get("kind") not in kinds:
                    continue
                tabs, ws = [], []
                for _m, sig in models:
                    q, w = self.qtable(raw, b, sig, en, float(T.alert))
                    tabs.append(q)
                    ws.append(w)
                bodies.append({"idx": i, "kind": b["kind"], "shape": b["shape"], "mode": raw.get("criterion_mode", "geometric"),
                               "r_eff_dm": tabs, "w_m": ws})
            prev = st.sent.get(T.id)
            changed = prev is None or not _bodies_close(prev, bodies)
            if changed:
                st.rev[T.id] = st.rev.get(T.id, -1) + 1
                st.sent[T.id] = bodies
            if changed or keyframe:
                items.append({"id": T.id, "rev": st.rev[T.id], "bodies": st.sent[T.id]})
        frame = volume_frame(int(getattr(ctx, "t_ns", 0)), keyframe, mids, items)
        st.frame = frame
        self.stats["volumes"] += 1
        return frame

    def volumes_full(self, ctx: Any) -> dict:
        """REST SB-75：当前感知体表（关键帧）。"""
        fr = self.volumes(ctx, keyframe=True)
        return fr

    # ------------------------------------------------------------ 逐对读数
    def _reverse(self, uav: str, tid: str) -> ReverseQ | None:
        fn = getattr(self.prt, "_frame", None)          # 帧在读取时展开（runtime._frame）
        f = fn(uav) if callable(fn) else getattr(self.prt, "frames", {}).get(uav)
        if not f:
            return None
        best = None
        for s in f.get("sensors") or []:
            for it in s.get("targets") or []:
                if it.get("target") != tid:
                    continue
                lv = {"none": 0, "D": 1, "R": 2, "I": 3}.get(str(it.get("level")), 0)
                key = (lv, float(it.get("n_eff") or 0.0))
                if best is None or key > best[0]:
                    best = (key, ReverseQ(str(s.get("sensor")), str(it.get("level") or "none"), _f(it.get("n_eff")), _f(it.get("n_req")),
                                          _f(it.get("p")), it.get("limiting")))
        return best[1] if best else None

    def _los(self, t_ns: int) -> Callable[[NDArray, NDArray, float, bool], tuple[bool | None, float | None]]:
        los = getattr(self.trt, "los", None)

        def fn(a: NDArray, b: NDArray, h: float, need: bool) -> tuple[bool | None, float | None]:
            if los is None:
                return None, None
            hit = los.peek(a, b, "top", t_ns, h_m=h)
            if hit is not None:
                return bool(hit[0].k_los > 0.5), float(hit[1])
            if not need:
                return None, None
            r = los.get(a, b, 1, "M19", t_ns, "top", h_m=h)
            if r.deferred:
                return None, None
            return bool(r.k_los > 0.5), 0.0

        return fn

    def quant(self, tid: str, ctx: Any, *, uav: str | None = None, uavs: list[QuantUav] | None = None, env: FieldEnv | None = None) -> dict:
        """逐对读数（展开形式，REST SB-73 与 topic 编码前的同一结构）。"""
        return quant_frame(*self._quant_pairs(tid, ctx, uav=uav, uavs=uavs, env=env))

    def _quant_pairs(self, tid: str, ctx: Any, *, uav: str | None = None, uavs: list[QuantUav] | None = None,
                     env: FieldEnv | None = None) -> tuple[str, str, int, list]:
        """(识别物 id, 判据模式, t_ns, 逐对读数)：`quant` 与 topic 编码（`encode_pairs`）共用。"""
        trt = self.trt
        v = trt.quant_view(tid) if trt is not None else None
        if v is None:
            raise FieldError(543, "target", "TARGET_NOT_FOUND", value=tid)
        us = self.uavs(ctx) if uavs is None else uavs
        en = self.env(ctx) if env is None else env
        t_ns = int(getattr(ctx, "t_ns", 0))
        qt = QuantTarget(v.id, v.cfg, v.pos, v.yaw_rad, v.h_m, v.alertness, v.dwell, v.t_dwell_s)
        pairs = quant_pairs(qt, us, en, los_fn=self._los(t_ns), reverse_fn=self._reverse, uav_filter=uav)
        self.stats["quant"] += 1
        return v.id, v.cfg.raw.get("criterion_mode", "geometric"), t_ns, pairs

    # ------------------------------------------------------------ 显式采样（SB-78，P1）
    def samples(self, kind: str, anchor: str, n: int, ctx: Any, *, model_id: str | None = None, j: int | None = None) -> dict:
        if kind not in FIELD_KINDS:
            raise FieldError(564, "kind", "KIND_UNKNOWN", value=kind)
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= SAMPLES_MAX:
            raise FieldError(564, "n", "OUT_OF_RANGE", value=n, limit=SAMPLES_MAX)
        en = self.env(ctx)
        us = self.uavs(ctx)
        parts = str(anchor).split(":")
        if parts[0] == "uav" and len(parts) == 2:
            idx = next((k for k, u in enumerate(us) if u.id == parts[1]), None)
            if idx is None:
                raise FieldError(564, "anchor", "ANCHOR_UNKNOWN", value=anchor)
            u = us[idx]
            jj = idx if j is None else int(j)
            rmax = r_max_of(kind, u.sig, en)
            dirs, rr = pattern(n, jj, R_MIN_UAV, max(rmax, R_MIN_UAV * 1.01))
            el = -np.degrees(np.arcsin(np.clip(dirs[:, 2], -1.0, 1.0)))      # 观察者（采样点）看飞行器的仰角
            val = field_value(kind, u.sig, en, rr, el)
            P = np.asarray(u.pos, float)[None, :] + dirs * rr[:, None]
            pts = [[*map(lambda x: round(float(x), 3), P[i]), (None if not math.isfinite(val[i]) else round(float(val[i]), 4))] for i in range(n)]
            return {"kind": kind, "anchor": anchor, "j": jj, "r_min_m": R_MIN_UAV, "r_max_m": round(rmax, 3), "points": pts}
        if parts[0] == "target" and len(parts) == 3:
            trt = self.trt
            v = trt.quant_view(parts[1]) if trt is not None else None
            if v is None:
                raise FieldError(564, "anchor", "ANCHOR_UNKNOWN", value=anchor)
            try:
                bi = int(parts[2])
                body = v.cfg.raw["sensitivity"][bi]
            except (ValueError, IndexError, KeyError):
                raise FieldError(564, "anchor", "BODY_UNKNOWN", value=anchor) from None
            models = dict(self._models(ctx, us))
            mid = model_id or next(iter(models))
            sig = models.get(mid)
            if sig is None:
                raise FieldError(564, "model_id", "MODEL_NOT_IN_SESSION", value=mid)
            vt = self.table(v.cfg.raw, body, sig, en, v.alertness)
            tids = [T.id for T in trt.ordered() if T.cfg.raw.get("enabled", True)]       # 与感知体表帧的识别物顺序相同
            jj = (tids.index(v.id) * 4 + bi) if j is None else int(j)
            rmax = max(float(np.max(vt.r_eff_m)) + 2.0 * vt.w_m, float(body.get("radius_max_m", 1.0)), R_MIN_BODY * 2.0)
            rmax = min(rmax, float(body.get("radius_max_m", rmax)))
            dirs, rr = pattern(n, jj, R_MIN_BODY, max(rmax, R_MIN_BODY * 1.01))
            P = np.asarray(v.pos, float)[None, :] + dirs * rr[:, None]
            vol = body_volumes([body], v.pos, v.yaw_rad, kinds=(str(body["kind"]),))
            keep = inside_points(vol[0], P) if vol else np.zeros(n, bool)
            az = np.asarray(wrap_pi(np.arctan2(dirs[:, 1], dirs[:, 0]) - v.yaw_rad), float)
            el = np.arcsin(np.clip(dirs[:, 2], -1.0, 1.0))
            re = table_r_eff(vt.r_eff_m, az, el)
            ref = float(np.max(vt.r_eff_m))
            pv = volume_p(rr, re, vt.w_m * re / ref if ref > 0 else vt.w_m)
            pts = [[*map(lambda x: round(float(x), 3), P[i]), round(float(pv[i]), 4)] for i in range(n) if keep[i]]
            return {"kind": kind, "anchor": anchor, "j": jj, "model_id": mid, "r_min_m": R_MIN_BODY, "r_max_m": round(rmax, 3), "points": pts}
        raise FieldError(564, "anchor", "BAD_ANCHOR", value=anchor)

    # ------------------------------------------------------------ 慢任务：按订阅生成并发布
    def tick(self, ctx: Any) -> None:
        st0 = getattr(ctx, "streams", None) or {}
        if not (len(st0.get("field", ())) or len(st0.get("quant", ())) or self.local_kinds or self.local_volumes or self.local_quant):
            self._prev_kinds, self._prev_vol = set(), False      # 无订阅：不绑定、不生成（M19-NFR-007）
            return None
        if not self.prt.bind(ctx):
            return None
        kinds, vols, tids = self.wanted(ctx)
        self.stats["ticks"] += 1
        if not kinds and not vols and not tids:
            self._prev_kinds, self._prev_vol = set(), False
            return None
        now = int(self.clock())
        t_ns = int(getattr(ctx, "t_ns", 0))
        st = getattr(ctx, "streams", None) or {}
        fsub = tuple(np.asarray(st.get("fsub", []), np.int64).reshape(-1).tolist()) if "fsub" in st else None
        resub = fsub is not None and fsub != self._fsub
        self._fsub = fsub
        us: list[QuantUav] | None = None
        en: FieldEnv | None = None
        field_items: dict[str, bytes] = {}
        import msgpack

        for kind in sorted(kinds):
            ks = self.kinds.get(kind)
            new = kind not in self._prev_kinds or resub or ks is None
            if not new and now - ks.last_ns < PROFILE_PERIOD_NS:  # type: ignore[union-attr]
                continue
            us = self.uavs(ctx) if us is None else us
            en = self.env(ctx) if en is None else en
            key = new or ks is None or now - ks.last_key_ns >= PROFILE_KEYFRAME_NS
            fr = self.profiles(kind, ctx, keyframe=bool(key), uavs=us, env=en)
            ks = self.kinds[kind]
            ks.last_ns = now
            if fr["keyframe"]:
                ks.last_key_ns = now
            field_items[kind] = msgpack.packb(fr, use_bin_type=True, use_single_float=True)
        if vols:
            vs = self.vol
            new = not self._prev_vol or resub or vs.frame is None
            if new or now - vs.last_ns >= VOLUME_PERIOD_NS:
                us = self.uavs(ctx) if us is None else us
                en = self.env(ctx) if en is None else en
                key = new or now - vs.last_key_ns >= VOLUME_KEYFRAME_NS
                fr = self.volumes(ctx, keyframe=bool(key), uavs=us, env=en)
                vs.last_ns = now
                if fr["keyframe"]:
                    vs.last_key_ns = now
                if fr["keyframe"] or fr["tgt"]:
                    field_items["volumes"] = msgpack.packb(fr, use_bin_type=True, use_single_float=True)
        self._prev_kinds, self._prev_vol = set(kinds), vols
        if field_items:
            self.stats["bytes"] += sum(len(b) for b in field_items.values())
            self.pub.put(ctx, _key("field"), t_ns, field_items)
        q_items: dict[str, bytes] = {}
        for tid in sorted(tids):
            last = self.quant_last.get(tid)
            due = last is None or resub or (t_ns - last[1] >= QUANT_SIM_NS and now - last[0] >= QUANT_WALL_MIN_NS)
            if not due:
                continue
            us = self.uavs(ctx) if us is None else us
            en = self.env(ctx) if en is None else en
            try:
                qp = self._quant_pairs(tid, ctx, uavs=us, env=en)
            except FieldError:
                continue
            self.quant_last[tid] = (now, t_ns)
            self._last_q[tid] = qp                    # 展开形式在读取 last_quant 时生成（只有 REST 与测试读取）
            q_items[tid] = msgpack.packb(encode_pairs(*qp), use_bin_type=True, use_single_float=True)
        for gone in [k for k in self.quant_last if k not in tids]:
            self.quant_last.pop(gone, None)
        if q_items:
            self.stats["bytes"] += sum(len(b) for b in q_items.values())
            self.pub.put(ctx, _key("target_quant"), t_ns, q_items)
        return None


def _bodies_close(a: list, b: list) -> bool:
    """两组线格式 bodies 是否在 rev 阈值内（半径差 ≤ max(1 m, 2%)、过渡宽度差 ≤ 2%，结构相同）。"""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b, strict=True):
        if (x["idx"], x["kind"], x["shape"], x["mode"], len(x["r_eff_dm"])) != (y["idx"], y["kind"], y["shape"], y["mode"], len(y["r_eff_dm"])):
            return False
        for ta, tb in zip(x["r_eff_dm"], y["r_eff_dm"], strict=True):
            ea, eb = expand_table(ta), expand_table(tb)
            if float(np.max(np.abs(ea - eb) - np.maximum(1.0, REV_REL * np.maximum(ea, eb)))) > 0.0:
                return False
        for wa, wb in zip(x["w_m"], y["w_m"], strict=True):
            if abs(wa - wb) > REV_REL * max(abs(wa), abs(wb), 1.0):
                return False
    return True


def _c(x: float) -> float | int:
    """整数值写 int（紧凑数值）。"""
    return int(x) if float(x).is_integer() else float(x)


def _f(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) else None


def _key(name: str) -> str:
    """总线键：契约 1.2.0 的 `STATE_FIELD`、`STATE_TARGET_QUANT`（M00 合入前按 sim-core 状态键的同级位置构造）。"""
    from awr.contracts import bus_keys

    const = {"field": "STATE_FIELD", "target_quant": "STATE_TARGET_QUANT"}[name]
    got = getattr(bus_keys, const, None)
    if isinstance(got, str):
        return got
    return bus_keys.STATE_PERCEPTION.rsplit("/", 1)[0] + "/" + name
