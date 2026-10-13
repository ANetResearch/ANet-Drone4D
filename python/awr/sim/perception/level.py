"""统一感知判据（M19-FR-003、FR-004、FR-005、FR-006、FR-021；ADR-100、ADR-101；AWR-04 §6.2；M18-M19 PRD §6.7）。

```text
N = d_c·f/(R·p')，GSD = R·p'/f；d_c 按视线方向（LWIR 乘 √max(hot_fraction, 0.25)）
N_eff = N·k_los·k_c·k_light·k_blur·(1 − occlusion)
EO/NIR：k_c = clamp((|C0|·(1 − camo)·τ − 0.05)/0.20)；LWIR：k_c = clamp((SNR_T − 1)/3)，SNR_T = |ΔT_app|/√(NETD² + sigma_clutter²)
k_light：被动 clamp(lg(E/E_min)/2)（auto 取彩色与黑白的较大值）；NIR 主动 clamp((R_nir − R)/(0.2·R_nir))（照明锥内）；LWIR 1
k_blur = GSD/√(GSD² + b²)，b = v_res·t_exp + R·sigma_jit，v_res = 0.1·|v⊥|（云台跟踪）或 |v⊥|
level：I ≥ 22.4、R ≥ 14、D ≥ 3.5 px；limiting：fov、pixels（N < N_req）或 argmin(k_los·(1 − occ), k_light, k_c, k_blur)
```

`perception_level`（单对）与 `perception_level_batch`（按对向量化）共用同一个数组核 `evaluate`；sim-core 管线直接调用
`evaluate`（路径透过率取 M07 `optical_depth`），纯函数在未给出透过率时按 `env.mor_m` 均匀消光。签名冻结于 WP-01。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from awr.sim.targets.geometry import critical_dim

from .johnson import level_of, level_req_of, ttpf
from .optics import lambda_um, p_eff_um, p_prime_um, sigma_per_m
from .params import LEVELS, LIMITING, N50_PX
from .specs import EnvSample, SensorState, ViewGeom

__all__ = ["LevelBatch", "LevelResult", "PairArrays", "ViewGeomBatch", "angles", "cfg_value", "evaluate", "pair_arrays", "perception_level",
           "perception_level_batch", "r_nir_fixed_point"]

_C = LEVELS["contrast"]
_LW = LEVELS["lwir"]
_LI = LEVELS["light"]
_TRACK = float(LEVELS["blur"]["track_residual"])
K0 = 273.15


@dataclass(frozen=True)
class LevelResult:
    level: int                             # 0 NONE、1 D、2 R、3 I
    n: float
    n_eff: float
    n_req: float
    p: float
    gsd_m: float
    range_m: float
    k: tuple[float, float, float, float, float]   # k_los, k_c, k_light, k_blur, occ
    limiting: str | None                   # fov、los、pixels、light、contrast、blur
    dt_app_k: float | None = None


@dataclass(frozen=True)
class LevelBatch:
    level: NDArray
    n_eff: NDArray
    p: NDArray
    gsd_m: NDArray
    range_m: NDArray
    limiting: NDArray                      # object[n]：名称或 None
    # ---- 追加（M19）
    n: NDArray | None = None
    k: NDArray | None = None               # [n, 5]
    dt_app_k: NDArray | None = None
    n_max: NDArray | None = None           # 视线前的上界 N·k_c·k_light·k_blur·(1 − occ)


@dataclass(frozen=True)
class ViewGeomBatch:
    """按对的几何（数组形式的 ViewGeom）。"""

    p_sensor: NDArray                      # [n, 3]
    k_los: NDArray | float = 1.0
    tracking: NDArray | bool = True
    v_rel_perp_mps: NDArray | float = 0.0
    in_fov: NDArray | bool = True
    in_illum_cone: NDArray | bool = True
    worst_aspect: NDArray | bool | None = None
    tau_path: NDArray | None = None
    tau_850: NDArray | None = None


def cfg_value(cfg: Any, key: str, default: Any = None) -> Any:
    """识别物配置字段（awr.sim.targets.TargetConfig、桩 TargetConfig 或 dict）。"""
    raw = cfg if isinstance(cfg, dict) else getattr(cfg, "raw", None)
    if isinstance(raw, dict):
        v = raw.get(key, default)
        return default if v is None and default is not None else v
    return getattr(cfg, key, default)


@dataclass
class PairArrays:
    """数组核的输入（按对）。"""

    R: NDArray
    d_c: NDArray            # 关键维度（未乘 LWIR 热斑缩放）
    hot: NDArray            # hot_fraction
    c0: NDArray
    camo: NDArray
    rho850: NDArray
    th_abs: NDArray         # bool：thermal.mode = absolute
    dt_rel: NDArray
    t_surf_c: NDArray
    emis: NDArray
    occ: NDArray
    k_los: NDArray
    tracking: NDArray
    v_perp: NDArray
    in_fov: NDArray
    in_cone: NDArray
    tau_path: NDArray | None = None
    tau_850: NDArray | None = None
    off_axis: NDArray | None = None   # 识别物偏离视轴的角度（rad）；给出时按每个照明器的锥角判定（θ/2），否则用 in_cone


def _arr(x: Any, n: int, dtype: Any = float) -> NDArray:
    a = np.asarray(x, dtype=dtype)
    return np.broadcast_to(a, (n,)).copy() if a.ndim == 0 else a.reshape(n)


def angles(p_sensor: NDArray, p_target: NDArray, yaw_t: NDArray) -> tuple[NDArray, NDArray, NDArray]:
    """(R, ε, β)：斜距、自识别物看传感器的俯视角（传感器高于识别物为正）、传感器方位相对识别物航向的夹角。"""
    d = np.asarray(p_sensor, float).reshape(-1, 3) - np.asarray(p_target, float).reshape(-1, 3)
    R = np.linalg.norm(d, axis=1)
    h = np.hypot(d[:, 0], d[:, 1])
    eps = np.arctan2(d[:, 2], h)
    beta = np.arctan2(d[:, 1], d[:, 0]) - np.asarray(yaw_t, float).reshape(-1)
    return R, eps, beta


def _thermal(cfgs: list[Any]) -> tuple[NDArray, NDArray, NDArray, NDArray, NDArray]:
    th = [cfg_value(c, "thermal", {}) or {} for c in cfgs]
    return (np.array([float(t.get("hot_fraction", 1.0)) for t in th]), np.array([t.get("mode", "relative") == "absolute" for t in th]),
            np.array([float(t.get("delta_t_k", 0.0) or 0.0) for t in th]), np.array([float(t.get("t_surface_c", 30.0) or 30.0) for t in th]),
            np.array([float(t.get("emissivity", 0.95) or 0.95) for t in th]))


def pair_arrays(cfgs: list[Any], R: NDArray, d_c: NDArray, occ: NDArray, geom: ViewGeomBatch) -> PairArrays:
    n = len(cfgs)
    hot, th_abs, dt, ts, em = _thermal(cfgs)
    return PairArrays(
        _arr(R, n), _arr(d_c, n), hot, np.array([float(cfg_value(c, "contrast0", 0.3)) for c in cfgs]),
        np.array([float(cfg_value(c, "camouflage", 0.0) or 0.0) for c in cfgs]), np.array([float(cfg_value(c, "reflect_850", 0.3)) for c in cfgs]),
        th_abs, dt, ts, em, _arr(occ, n), _arr(geom.k_los, n), _arr(geom.tracking, n, bool), _arr(geom.v_rel_perp_mps, n),
        _arr(geom.in_fov, n, bool), _arr(geom.in_illum_cone, n, bool),
        None if geom.tau_path is None else _arr(geom.tau_path, n), None if geom.tau_850 is None else _arr(geom.tau_850, n))


def r_nir_fixed_point(r0: NDArray, sigma_per_m: NDArray) -> NDArray:
    """照明器有效距离 `R_nir = R0·tau_850(R_nir)` 的解（AWR-20 §6.3 的不动点，与 M13 `IllumSpec.range_m` 同一口径）。
    `g(x) = x − R0·exp(−sigma·x)` 单调递增，用二分（50 次，误差 < R0·2^-50）求根：浓雾下 sigma·R_nir > 1 时简单迭代会振荡。"""
    r0 = np.asarray(r0, float)
    s = np.broadcast_to(np.asarray(sigma_per_m, float), r0.shape)
    lo = np.zeros_like(r0)
    hi = r0.copy()
    for _ in range(50):
        mid = 0.5 * (lo + hi)
        pos = mid - r0 * np.exp(-s * mid) >= 0.0
        hi = np.where(pos, mid, hi)
        lo = np.where(pos, lo, mid)
    return 0.5 * (lo + hi)


def _k_passive(e_lx: float, e_min: float) -> float:
    return min(max(math.log10(max(float(e_lx), 1e-12) / e_min) / float(_LI["decades"]), 0.0), 1.0)


def _p_prime_arr(sn: SensorState, f: NDArray) -> NDArray:
    spec = sn.spec
    if spec.f_max_mm <= spec.f_min_mm:
        return np.full(f.shape, p_prime_um(spec, float(spec.f_min_mm), sn.resolution))
    if spec.f_law == "linear_ln_f":
        u = (np.log(np.maximum(f, 1e-9)) - math.log(spec.f_min_mm)) / (math.log(spec.f_max_mm) - math.log(spec.f_min_mm))
    else:
        u = (f - spec.f_min_mm) / (spec.f_max_mm - spec.f_min_mm)
    fn = spec.f_number_min + (spec.f_number_max - spec.f_number_min) * np.clip(u, 0.0, 1.0)
    return np.maximum(p_eff_um(spec, sn.resolution), lambda_um(spec.kind) * fn)


def evaluate(sn: SensorState, a: PairArrays, env: EnvSample, n_req: NDArray | float, *, f_mm: NDArray | None = None) -> dict[str, NDArray]:
    """数组核：返回 level、n、n_eff、n_max、p、gsd、k_c、k_light、k_blur、lim（-1 无，0–5 为 LIMITING 下标）、dt_app、tau。
    `f_mm` 给出时按对的焦距（feasible_view 的逐候选变焦），否则为 `sn.f_mm`。"""
    spec = sn.spec
    n = len(a.R)
    if f_mm is None:
        f = np.full(n, float(sn.f_mm))
        pp = np.full(n, p_prime_um(spec, float(sn.f_mm), sn.resolution))
    else:
        f = np.broadcast_to(np.asarray(f_mm, float), (n,)).copy()
        pp = _p_prime_arr(sn, f)
    lwir = spec.kind == "lwir"
    R = np.maximum(a.R, 1e-6)
    d = a.d_c * np.sqrt(np.maximum(a.hot, 0.25)) if lwir else a.d_c
    N = d * f / (R * pp * 1e-3)
    G = R * pp * 1e-3 / f
    if a.tau_path is not None:
        ta = a.tau_path
    else:
        ta = np.exp(-sigma_per_m(env.mor_m, lambda_um(spec.kind) * 1000.0, env.rho_v_gm3) * R)
    dt_app = None
    E = float(env.illum_lx)
    if lwir:
        ts, tb = env.t_sky_c + K0, env.t_bg_c + K0
        absolute = ta * (a.emis * (a.t_surf_c + K0) + (1.0 - a.emis) * ts - env.eps_bg * tb - (1.0 - env.eps_bg) * ts)
        dt_app = np.where(a.th_abs, absolute, ta * a.emis * a.dt_rel * env.k_cross)
        clutter = spec.sigma_clutter_k[0] if env.band == "day" else spec.sigma_clutter_k[1]
        snr = np.abs(dt_app) / math.sqrt(spec.netd_k ** 2 + clutter ** 2)
        k_c = np.clip((snr - float(_LW["snr_min"])) / float(_LW["snr_span"]), 0.0, 1.0)
        k_light = np.ones(n)
        t_exp, jit = spec.t_int_s, spec.jitter_short_urad
    else:
        C = np.abs(a.c0) * (1.0 - a.camo) * ta
        k_c = np.clip((C - float(_C["c_min"])) / (float(_C["c_full"]) - float(_C["c_min"])), 0.0, 1.0)
        kc, km = _k_passive(E, spec.e_min_color_lx), _k_passive(E, spec.e_min_mono_lx)
        if spec.kind == "nir":
            t850 = a.tau_850 if a.tau_850 is not None else np.exp(-sigma_per_m(env.mor_m, 850.0, env.rho_v_gm3) * R)
            with np.errstate(divide="ignore", invalid="ignore"):
                sig850 = np.where(R > 0, -np.log(np.maximum(t850, 1e-300)) / R, 0.0)    # 等效均匀消光（路径透过率 → 1/m）
            active = np.zeros(n)
            o = spec.outputs.get(sn.resolution or spec.analysis) if spec.outputs else None
            crop_mm = (o[1] if o is not None else spec.w_native_px) * spec.pixel_um * 1e-3
            hf = 2.0 * np.arctan(crop_mm / (2.0 * f))
            for j, ill in enumerate(spec.illuminators):
                if sn.illum_on and (j >= len(sn.illum_on) or not sn.illum_on[j]):
                    continue
                th_deg = np.degrees(hf) if ill.divergence_deg is None else np.full(n, float(ill.divergence_deg))
                r0 = (ill.r_ref_m * (ill.theta_ref_deg / np.maximum(th_deg, 1e-9)) * math.sqrt(ill.p_w / ill.p_ref_w)
                      * np.sqrt(np.maximum(a.rho850, 0.0) / ill.rho_ref))
                rn = r_nir_fixed_point(r0, sig850)
                with np.errstate(divide="ignore", invalid="ignore"):
                    k = np.where(rn > 0, np.clip((rn - R) / (float(_LI["nir_edge_frac"]) * rn), 0.0, 1.0), 0.0)
                cone = a.in_cone if a.off_axis is None else a.in_cone & (a.off_axis <= 0.5 * np.radians(th_deg) + 1e-9)
                active = np.maximum(active, np.where(cone, k, 0.0))
            k_light = np.maximum(active, km)
            t_exp = spec.t_exp_low_s
        else:
            kl = {"day": kc, "lowlight": km, "nir": km}.get(sn.mode, max(kc, km))
            k_light = np.full(n, kl)
            t_exp = spec.t_exp_day_s if spec.switch_lx <= E else spec.t_exp_low_s
        jit = spec.jitter_short_urad if t_exp <= spec.jitter_fast_max_s else spec.jitter_long_urad
    v_res = np.where(a.tracking, _TRACK * np.abs(a.v_perp), np.abs(a.v_perp))
    b = v_res * t_exp + R * jit * 1e-6
    k_blur = np.where(G > 0, G / np.sqrt(G * G + b * b), 0.0)
    n_max = N * k_c * k_light * k_blur * (1.0 - a.occ)
    n_eff = np.where(a.in_fov, n_max * a.k_los, 0.0)
    nr = np.broadcast_to(np.asarray(n_req, float), (n,))
    p = np.zeros(n)
    for v in np.unique(nr):
        m = nr == v
        p[m] = ttpf(n_eff[m], N50_PX[level_req_of(float(v))])
    stack = np.stack([a.k_los * (1.0 - a.occ), k_light, k_c, k_blur], axis=1) if n else np.zeros((0, 4))
    arg = np.argmin(stack, axis=1) if n else np.zeros(0, int)
    lim = np.array([1, 3, 4, 5], np.int8)[arg] if n else np.zeros(0, np.int8)
    lim = np.where(nr > N, 2, lim)
    lim = np.where(n_eff >= nr, -1, lim)
    lim = np.where(a.in_fov, lim, 0).astype(np.int8)
    return {"level": level_of(n_eff) if n else np.zeros(0, np.int8), "n": N, "n_eff": n_eff, "n_max": n_max, "p": p, "gsd": G,
            "k_c": k_c, "k_light": k_light, "k_blur": k_blur, "lim": lim, "dt_app": dt_app, "tau": ta}


def _lim_name(code: int) -> str | None:
    return None if code < 0 else LIMITING[int(code)]


def _target_inputs(t: Any, idx: NDArray, P: NDArray, worst: NDArray | bool | None) -> tuple[list[Any], NDArray, NDArray, NDArray]:
    idx = np.asarray(idx, int).reshape(-1)
    pos = np.asarray(t.pos, float).reshape(-1, 3)[idx]
    yaw = np.asarray(t.yaw_rad, float).reshape(-1)[idx]
    size = np.asarray(t.size_m, float).reshape(-1, 3)[idx]
    vel = np.asarray(t.vel, float).reshape(-1, 3)[idx]
    cfgs = [t.cfg[int(i)] for i in idx]
    R, eps, beta = angles(P, pos, yaw)
    moving = np.array([bool(np.hypot(vel[j, 0], vel[j, 1]) > 0.1 or (cfg_value(c, "motion", {}) or {}).get("mode", "static") != "static")
                       for j, c in enumerate(cfgs)], bool)
    w = moving if worst is None else np.broadcast_to(np.asarray(worst, bool), moving.shape)
    d = np.empty(len(idx))
    for j, c in enumerate(cfgs):
        ov = cfg_value(c, "critical_dim_m", None)
        d[j] = float(critical_dim(size[j], eps[j], beta[j], worst=bool(w[j]), override_m=ov))
    occ = np.asarray(t.occlusion, float).reshape(-1)[idx]
    return cfgs, R, d, occ


def perception_level(sn: SensorState, t: Any, i: int, geom: ViewGeom, env: EnvSample, n_req: float) -> LevelResult:
    """一对（成像传感器，识别物）的统一判据；纯函数（M18-M19 PRD §6.7；签名冻结于 WP-01）。"""
    P = np.asarray(geom.p_sensor, float).reshape(1, 3)
    cfgs, R, d, occ = _target_inputs(t, np.array([int(i)]), P, geom.worst_aspect)
    gb = ViewGeomBatch(P, geom.k_los, geom.tracking, geom.v_rel_perp_mps, geom.in_fov, geom.in_illum_cone, geom.worst_aspect,
                       None if geom.tau_path is None else np.array([geom.tau_path]), None if geom.tau_850 is None else np.array([geom.tau_850]))
    r = evaluate(sn, pair_arrays(cfgs, R, d, occ, gb), env, float(n_req))
    dt = None if r["dt_app"] is None else float(r["dt_app"][0])
    return LevelResult(int(r["level"][0]), float(r["n"][0]), float(r["n_eff"][0]), float(n_req), float(r["p"][0]), float(r["gsd"][0]),
                       float(R[0]), (float(geom.k_los), float(r["k_c"][0]), float(r["k_light"][0]), float(r["k_blur"][0]), float(occ[0])),
                       _lim_name(int(r["lim"][0])), dt)


def perception_level_batch(sn: SensorState, t: Any, idx: NDArray, geom: Any, env: EnvSample, n_req: NDArray) -> LevelBatch:
    """按对向量化；`geom` 为 ViewGeomBatch 或每对一个 ViewGeom 的序列（桩的写法）。"""
    idx = np.asarray(idx, int).reshape(-1)
    n = len(idx)
    if not isinstance(geom, ViewGeomBatch):
        gl = list(geom)
        geom = ViewGeomBatch(np.array([g.p_sensor for g in gl], float).reshape(n, 3), np.array([g.k_los for g in gl], float),
                             np.array([g.tracking for g in gl], bool), np.array([g.v_rel_perp_mps for g in gl], float),
                             np.array([g.in_fov for g in gl], bool), np.array([g.in_illum_cone for g in gl], bool),
                             None if all(g.worst_aspect is None for g in gl) else np.array([bool(g.worst_aspect) for g in gl]),
                             None if any(g.tau_path is None for g in gl) else np.array([g.tau_path for g in gl], float),
                             None if any(g.tau_850 is None for g in gl) else np.array([g.tau_850 for g in gl], float))
    P = np.asarray(geom.p_sensor, float).reshape(n, 3)
    cfgs, R, d, occ = _target_inputs(t, idx, P, geom.worst_aspect)
    a = pair_arrays(cfgs, R, d, occ, geom)
    r = evaluate(sn, a, env, np.broadcast_to(np.asarray(n_req, float), (n,)))
    k = np.stack([a.k_los, r["k_c"], r["k_light"], r["k_blur"], a.occ], axis=1) if n else np.zeros((0, 5))
    lim = np.array([_lim_name(int(c)) for c in r["lim"]], dtype=object)
    return LevelBatch(np.asarray(r["level"], np.int8), r["n_eff"], r["p"], r["gsd"], R, lim, r["n"], k, r["dt_app"], r["n_max"])
