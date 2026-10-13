"""M19 pure functions: frozen signatures and stub reference implementations (WP-01, D2-MS1; AWR-04 §6, §9.2; M18-M19 PRD §6.6-§6.13, §7.1).

Purpose: M20 (tasking, relay station choice), M15 (calculator) and the fake data source develop against these signatures before
`awr.sim.perception` (M19, WP-06) exists. The signatures of perception_level, perception_level_batch, feasible_view, feasibility_window and
zoom_for_level, and the field names of SensorSpecP, SensorState, ViewGeom, EnvSample, LevelResult and FeasibleView are frozen here
(tests/contracts/test_d2_stubs.py compares them with the M19 module once it exists). M19 may add keyword-only parameters with defaults and
extra trailing dataclass fields, nothing else.

The formulas follow the PRD (Johnson N50 + TTPF, effective pixels N_eff = N k_los k_c k_light k_blur (1 - occlusion)). Stub simplifications,
replaced by M19 and M07: optical depth uses MOR with the Kim wavelength exponent and a LWIR water vapour term (no M07 field), physical-mode
keepouts use the configured radius (no acoustic or visual criterion), no line-of-sight rays (k_los is an input). Numbers are simulation
reference values. Pure Python + numpy, no awr.sim imports; usable from tools/ and tests only (awr.* must not import tools/).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import targets_stub as T
from numpy.typing import NDArray

ROOT = Path(__file__).resolve().parents[2]
LEVELS = json.loads((ROOT / "packages/contracts/sensor/perception_levels.json").read_text(encoding="utf-8"))
K_MOR = float(LEVELS["k_mor"])
LEVEL_NAMES = ("NONE", "D", "R", "I")
LEVEL_INDEX = {"none": 0, "NONE": 0, "D": 1, "R": 2, "I": 3}
N_P90 = {k: float(v["n_p90_px"]) for k, v in LEVELS["levels"].items()}          # D 3.5, R 14, I 22.4
N50_PX = {k: float(v["n50_px"]) for k, v in LEVELS["levels"].items()}           # D 2, R 8, I 12.8
LAMBDA_UM = {"eo": 0.55, "nir": 0.85, "lwir": 10.0}
BAND_LUX = {"day": 12543.0, "low": 3.0, "night": 0.001}                          # worst case of each band (AWR-04 §6.5)
Light = Literal["day", "low", "night"]


# ---------------------------------------------------------------- frozen data types
@dataclass(frozen=True)
class Illuminator:
    kind: Literal["builtin", "nir_ill_l"]
    p_w: float
    divergence_deg: float | None          # None = follow the current HFOV (GX40 built-in)
    r_ref_m: float
    p_ref_w: float
    theta_ref_deg: float
    rho_ref: float = 0.3
    r_spec_max_m: float | None = None


@dataclass(frozen=True)
class SensorSpecP:
    """Effective sensor spec for perception (merged model + mount file, AWR-20 §7.5)."""
    name: str                              # mount name, e.g. eo_gx40, lwir640
    kind: Literal["eo", "nir", "lwir"]
    pixel_um: float
    w_native_px: int
    h_native_px: int
    f_min_mm: float
    f_max_mm: float
    f_number_min: float                    # at f_min
    f_number_max: float                    # at f_max
    outputs: dict = field(default_factory=dict)   # name -> (w_out_px, crop_w_px)
    analysis: str = "native"
    f_law: Literal["linear_f", "linear_ln_f"] = "linear_f"
    netd_k: float = 0.05
    sigma_clutter_k: tuple[float, float] = (2.0, 1.0)  # day, night
    e_min_color_lx: float = 0.5
    e_min_mono_lx: float = 0.05
    illuminators: tuple[Illuminator, ...] = ()
    pitch_min_deg: float = -90.0
    pitch_max_deg: float = 30.0
    jitter_short_urad: float = 10.0
    jitter_long_urad: float = 30.0


@dataclass(frozen=True)
class SensorState:
    spec: SensorSpecP
    f_mm: float
    mode: Literal["auto", "day", "lowlight", "nir"] = "auto"
    resolution: str | None = None          # output name; None = spec.analysis
    illum_on: tuple[bool, ...] = ()        # per spec.illuminators; empty = all on in nir mode


@dataclass(frozen=True)
class ViewGeom:
    p_sensor: tuple[float, float, float]   # World ENU m
    k_los: float = 1.0                     # 0, 1/3, 2/3, 1 (filled by the LOS pipeline)
    tracking: bool = True                  # gimbal tracks this target
    v_rel_perp_mps: float = 0.0            # platform-target velocity across the line of sight
    in_fov: bool = True
    in_illum_cone: bool = True


@dataclass(frozen=True)
class EnvSample:
    illum_lx: float = 1.0e5
    band: Light = "day"
    mor_m: float = 20000.0
    rho_v_gm3: float = 7.5                 # water vapour density for the LWIR term
    k_cross: float = 1.0
    t_bg_c: float = 20.0
    eps_bg: float = 0.95
    t_sky_c: float = -20.0
    rain_mmph: float = 0.0


@dataclass(frozen=True)
class LevelResult:
    level: int                             # 0 NONE, 1 D, 2 R, 3 I
    n: float
    n_eff: float
    n_req: float
    p: float
    gsd_m: float
    range_m: float
    k: tuple[float, float, float, float, float]   # k_los, k_c, k_light, k_blur, occ
    limiting: str | None                   # fov, los, pixels, light, contrast, blur
    dt_app_k: float | None = None


@dataclass(frozen=True)
class FeasibleView:
    ok: NDArray                            # bool[n]
    ratio: NDArray                         # n_eff / n_req [n]
    f_mm: NDArray                          # [n]
    az_el: NDArray                         # [n, 2] rad (azimuth ENU, elevation of the line of sight)
    limiting: NDArray                      # object[n]


@dataclass(frozen=True)
class LevelBatch:
    level: NDArray
    n_eff: NDArray
    p: NDArray
    gsd_m: NDArray
    range_m: NDArray
    limiting: NDArray


# ---------------------------------------------------------------- reference sensors (AWR-04 §6.1; AWR-20 §6)
def gx40(analysis: str = "1080p") -> SensorSpecP:
    builtin = Illuminator("builtin", 0.8, None, 200.0, 0.8, 6.6, 0.3, 200.0)
    return SensorSpecP("eo_gx40", "eo", 1.45, 3840, 2160, 4.8, 48.0, 1.7, 3.2,
                       {"4k": (3840, 3840), "1080p": (1920, 3840), "720p": (1280, 3840), "sxga": (1280, 2700), "1p3m": (1280, 2880)},
                       analysis, illuminators=(builtin,))


def gx40_nir(with_ext: bool = False) -> SensorSpecP:
    s = gx40()
    ill = s.illuminators + ((Illuminator("nir_ill_l", 3.0, 5.5, 465.0, 3.0, 5.5, 0.3, None),) if with_ext else ())
    return SensorSpecP("eo_gx40", "nir", s.pixel_um, s.w_native_px, s.h_native_px, s.f_min_mm, s.f_max_mm, s.f_number_min, s.f_number_max,
                       s.outputs, s.analysis, illuminators=ill)


def lwir640(lens_mm: float = 100.0) -> SensorSpecP:
    return SensorSpecP("lwir640", "lwir", 12.0, 640, 512, lens_mm, lens_mm, 1.0, 1.0, {}, "native", netd_k=0.05,
                       sigma_clutter_k=(2.0, 1.0), jitter_short_urad=20.0, jitter_long_urad=20.0)


# ---------------------------------------------------------------- optics and closed forms
def f_number(sn: SensorSpecP, f_mm: float) -> float:
    if sn.f_max_mm <= sn.f_min_mm:
        return sn.f_number_min
    if sn.f_law == "linear_ln_f":
        u = (math.log(f_mm) - math.log(sn.f_min_mm)) / (math.log(sn.f_max_mm) - math.log(sn.f_min_mm))
    else:
        u = (f_mm - sn.f_min_mm) / (sn.f_max_mm - sn.f_min_mm)
    return sn.f_number_min + (sn.f_number_max - sn.f_number_min) * min(max(u, 0.0), 1.0)


def p_eff_um(sn: SensorSpecP, resolution: str | None = None) -> float:
    out = resolution or sn.analysis
    if out in sn.outputs:
        w_out, crop = sn.outputs[out]
        return sn.pixel_um * crop / w_out
    return sn.pixel_um


def p_prime_um(sn: SensorSpecP, f_mm: float, resolution: str | None = None) -> float:
    """Effective pixel p' = max(p_eff, lambda F#) (diffraction; LWIR keeps 12 um)."""
    return max(p_eff_um(sn, resolution), LAMBDA_UM[sn.kind] * f_number(sn, f_mm))


def hfov_rad(sn: SensorSpecP, f_mm: float, resolution: str | None = None) -> float:
    out = resolution or sn.analysis
    crop = sn.outputs[out][1] if out in sn.outputs else sn.w_native_px
    return 2.0 * math.atan(crop * sn.pixel_um * 1e-3 / (2.0 * f_mm))


def vfov_rad(sn: SensorSpecP, f_mm: float) -> float:
    return 2.0 * math.atan(sn.h_native_px * sn.pixel_um * 1e-3 / (2.0 * f_mm))


def gsd_m(r_m: float, p_um: float, f_mm: float) -> float:
    return r_m * p_um * 1e-3 / f_mm


def pixels_on_target(d_c_m: float, f_mm: float, r_m: float, p_um: float) -> float:
    return d_c_m * f_mm / (max(r_m, 1e-9) * p_um * 1e-3)


def ttpf(n: float | NDArray, n50_px: float) -> float | NDArray:
    """Target transfer probability function P(N) = x^E / (1 + x^E), x = N / N50, E = 2.7 + 0.7 x."""
    x = np.maximum(np.asarray(n, dtype=float), 0.0) / n50_px
    e = LEVELS["ttpf"]["e0"] + LEVELS["ttpf"]["e1"] * x
    xe = np.power(x, e)
    out = xe / (1.0 + xe)
    return float(out) if np.ndim(out) == 0 else out


def n_req_for(level_req: str, n_req_px: float | None = None) -> float:
    return float(n_req_px) if n_req_px is not None else N_P90[level_req]


def level_of(n_eff: float) -> int:
    return 3 if n_eff >= N_P90["I"] else 2 if n_eff >= N_P90["R"] else 1 if n_eff >= N_P90["D"] else 0


def kim_q(mor_m: float) -> float:
    v = mor_m / 1000.0
    if v > 50:
        return 1.6
    if v > 6:
        return 1.3
    if v > 1:
        return 0.16 * v + 0.34
    if v > 0.5:
        return v - 0.5
    return 0.0


def sigma_per_m(mor_m: float, lambda_um: float, rho_v_gm3: float = 7.5) -> float:
    """Extinction (1/m): sigma_550 = ln 20 / MOR (ADR-023), Kim exponent for other wavelengths, LWIR water vapour 0.03 + 0.012 rho_v per km."""
    s = K_MOR / mor_m * (lambda_um / 0.55) ** (-kim_q(mor_m))
    if lambda_um >= 8.0:
        s += (0.03 + 0.012 * rho_v_gm3) / 1000.0
    return s


def tau(mor_m: float, lambda_um: float, r_m: float, rho_v_gm3: float = 7.5) -> float:
    return math.exp(-sigma_per_m(mor_m, lambda_um, rho_v_gm3) * r_m)


def k_light_passive(e_lx: float, e_min_lx: float) -> float:
    return min(max(math.log10(max(e_lx, 1e-12) / e_min_lx) / LEVELS["light"]["decades"], 0.0), 1.0)


def r_nir_m(ill: Illuminator, theta_rad: float, rho_850: float, tau_850: float) -> float:
    theta_deg = math.degrees(theta_rad) if ill.divergence_deg is None else ill.divergence_deg
    r = ill.r_ref_m * (ill.theta_ref_deg / theta_deg) * math.sqrt(ill.p_w / ill.p_ref_w) * math.sqrt(rho_850 / ill.rho_ref) * tau_850
    return r


def zoom_for_level(sn: SensorSpecP, r_m: float, d_c_m: float, n_target: float) -> float:
    """Focal length (mm) that puts n_target pixels on d_c at slant range r, clamped to the lens range (p' depends on f: two passes)."""
    f = sn.f_max_mm
    for _ in range(3):
        f = min(max(n_target * r_m * p_prime_um(sn, f) * 1e-3 / max(d_c_m, 1e-9), sn.f_min_mm), sn.f_max_mm)
    return f


# ---------------------------------------------------------------- perception_level (PRD §6.7)
def _angles(p_sensor: NDArray, p_target: NDArray, yaw_t: float) -> tuple[float, float, float]:
    d = np.asarray(p_sensor, float) - np.asarray(p_target, float)
    r = float(np.linalg.norm(d))
    h = math.hypot(d[0], d[1])
    eps = math.atan2(d[2], h) if r > 0 else 0.0          # look-down angle seen from the target (observer above > 0)
    beta = math.atan2(d[1], d[0]) - yaw_t
    return r, eps, beta


def perception_level(sn: SensorState, t: T.TargetView, i: int, geom: ViewGeom, env: EnvSample, n_req: float) -> LevelResult:
    """Unified criterion for one (imaging sensor, target) pair; pure function (M19 §6.7)."""
    spec = sn.spec
    cfg = t.cfg[i]
    r, eps, beta = _angles(np.asarray(geom.p_sensor), t.pos[i], float(t.yaw_rad[i]))
    if not geom.in_fov:
        return LevelResult(0, 0.0, 0.0, n_req, 0.0, 0.0, r, (geom.k_los, 0.0, 0.0, 0.0, float(t.occlusion[i])), "fov", None)
    moving = bool(np.linalg.norm(t.vel[i][:2]) > 0.1)
    d = T.critical_dim(t.size_m[i], eps, beta, worst=moving, override_m=cfg.critical_dim_m)
    if spec.kind == "lwir":
        d *= math.sqrt(max(cfg.thermal.get("hot_fraction", 1.0), 0.25))
    p = p_prime_um(spec, sn.f_mm, sn.resolution)
    n = pixels_on_target(d, sn.f_mm, r, p)
    g = gsd_m(r, p, sn.f_mm)
    lam = LAMBDA_UM[spec.kind]
    ta = tau(env.mor_m, lam, r, env.rho_v_gm3)
    dt_app = None
    if spec.kind == "lwir":
        th = cfg.thermal
        eps_t = th.get("emissivity", 0.95)
        if th.get("mode", "relative") == "absolute":
            tt, tb, ts = th.get("t_surface_c", 30.0) + 273.15, env.t_bg_c + 273.15, env.t_sky_c + 273.15
            dt_app = ta * (eps_t * tt + (1 - eps_t) * ts - env.eps_bg * tb - (1 - env.eps_bg) * ts)
        else:
            dt_app = ta * eps_t * th.get("delta_t_k", 0.0) * env.k_cross
        clutter = spec.sigma_clutter_k[0] if env.band == "day" else spec.sigma_clutter_k[1]
        snr = abs(dt_app) / math.sqrt(spec.netd_k ** 2 + clutter ** 2)
        k_c = min(max((snr - LEVELS["lwir"]["snr_min"]) / LEVELS["lwir"]["snr_span"], 0.0), 1.0)
        k_light = 1.0
    else:
        c = abs(cfg.contrast0) * (1.0 - cfg.camouflage) * ta
        k_c = min(max((c - LEVELS["contrast"]["c_min"]) / (LEVELS["contrast"]["c_full"] - LEVELS["contrast"]["c_min"]), 0.0), 1.0)
        passive = {"day": k_light_passive(env.illum_lx, spec.e_min_color_lx),
                   "lowlight": k_light_passive(env.illum_lx, spec.e_min_mono_lx)}
        passive["auto"] = max(passive["day"], passive["lowlight"])
        k_light = passive.get(sn.mode, passive["auto"])
        if spec.kind == "nir":
            active = 0.0
            t850 = tau(env.mor_m, 0.85, r)
            for j, ill in enumerate(spec.illuminators):
                if sn.illum_on and not sn.illum_on[j]:
                    continue
                if not geom.in_illum_cone:
                    continue
                rn = r_nir_m(ill, hfov_rad(spec, sn.f_mm, sn.resolution), cfg.reflect_850, t850)
                active = max(active, min(max((rn - r) / (LEVELS["light"]["nir_edge_frac"] * rn), 0.0), 1.0))
            k_light = max(active, passive["lowlight"])
    bl = LEVELS["blur"]
    if spec.kind == "lwir":
        t_exp, jit = bl["t_exp_lwir_s"], spec.jitter_short_urad * 1e-6
    else:
        t_exp = bl["t_exp_day_s"] if (env.illum_lx >= bl["switch_lx"] and spec.kind != "nir") else bl["t_exp_low_s"]
        jit = (spec.jitter_short_urad if t_exp <= bl["t_short_max_s"] else spec.jitter_long_urad) * 1e-6
    v_res = bl["track_residual"] * abs(geom.v_rel_perp_mps) if geom.tracking else abs(geom.v_rel_perp_mps)
    b = v_res * t_exp + r * jit
    k_blur = g / math.sqrt(g * g + b * b) if g > 0 else 0.0
    occ = float(t.occlusion[i])
    n_max = n * k_c * k_light * k_blur * (1.0 - occ)
    n_eff = n_max * geom.k_los
    level = level_of(n_eff)
    lvl_req = next((k for k, v in N_P90.items() if abs(v - n_req) < 1e-9), cfg.level_req)
    pr = ttpf(n_eff, N50_PX[lvl_req])
    if n_eff >= n_req:
        lim = None
    elif n < n_req:
        lim = "pixels"
    else:
        lim = min([(geom.k_los * (1.0 - occ), "los"), (k_light, "light"), (k_c, "contrast"), (k_blur, "blur")], key=lambda x: x[0])[1]
    return LevelResult(level, n, n_eff, n_req, float(pr), g, r, (geom.k_los, k_c, k_light, k_blur, occ), lim, dt_app)


def perception_level_batch(sn: SensorState, t: T.TargetView, idx: NDArray, geom: list[ViewGeom], env: EnvSample, n_req: NDArray) -> LevelBatch:
    """Vectorised form (stub: loops; M19 vectorises). geom is one ViewGeom per pair."""
    rs = [perception_level(sn, t, int(i), g, env, float(nr)) for i, g, nr in zip(idx, geom, n_req, strict=True)]
    return LevelBatch(np.array([x.level for x in rs]), np.array([x.n_eff for x in rs]), np.array([x.p for x in rs]),
                      np.array([x.gsd_m for x in rs]), np.array([x.range_m for x in rs]), np.array([x.limiting for x in rs], dtype=object))


# ---------------------------------------------------------------- feasible_view and feasibility_window (PRD §6.13)
def feasible_view(vp: NDArray, t: T.TargetConfig, ts: T.TargetState1, sn: SensorSpecP, env: EnvSample, level_req: str, *,
                  n_req_px: float | None = None, k_los: NDArray | None = None) -> FeasibleView:
    """For each candidate viewpoint: point the gimbal at the target, zoom for 1.3 x n_req, evaluate N_eff (k_los default 1)."""
    vp = np.atleast_2d(np.asarray(vp, float))
    n = len(vp)
    n_req = n_req_for(level_req, n_req_px if n_req_px is not None else t.n_req_px)
    view = T.view_of([t], [ts])
    ok = np.zeros(n, bool)
    ratio = np.zeros(n)
    fm = np.zeros(n)
    azel = np.zeros((n, 2))
    lim = np.empty(n, dtype=object)
    margin = LEVELS["feasibility"]["zoom_margin"]
    for j in range(n):
        r, eps, beta = _angles(vp[j], ts.pos, ts.yaw_rad)
        d = T.critical_dim(t.size_m, eps, beta, worst=ts.moving, override_m=t.critical_dim_m)
        f = zoom_for_level(sn, r, d, n_req * margin)
        dvec = np.asarray(ts.pos) - vp[j]
        az, el = math.atan2(dvec[1], dvec[0]), math.atan2(dvec[2], math.hypot(dvec[0], dvec[1]))
        azel[j] = (az, el)
        pitch_ok = math.radians(sn.pitch_min_deg) - 1e-9 <= el <= math.radians(sn.pitch_max_deg) + 1e-9
        kl = 1.0 if k_los is None else float(np.asarray(k_los)[j])
        v_perp = ts.speed_mps if ts.moving else 0.0
        res = perception_level(SensorState(sn, f, "nir" if sn.kind == "nir" else "auto"), view, 0,
                               ViewGeom(tuple(vp[j]), kl, True, v_perp), env, n_req)
        fm[j], ratio[j] = f, res.n_eff / n_req
        ok[j] = pitch_ok and res.n_eff >= n_req
        lim[j] = res.limiting if pitch_ok else "fov"
    return FeasibleView(ok, ratio, fm, azel, lim)


def env_for(light: Light, mor_m: float) -> EnvSample:
    return EnvSample(illum_lx=BAND_LUX[light], band=light, mor_m=mor_m, k_cross=1.0)


def keepout_horizontal_radius(vols: list[T.Volume], agl_m: float) -> float:
    """Horizontal radius of the keepouts at a height above the target base (spheres and hemispheres cut at that height)."""
    r = 0.0
    for v in vols:
        if v.shape in ("sphere", "hemisphere"):
            dz = agl_m - (v.z_min_m if v.shape == "hemisphere" else v.center[2])
            r = max(r, math.sqrt(max(v.r_m ** 2 - dz ** 2, 0.0)))
        elif v.z_min_m <= agl_m <= v.z_max_m:
            r = max(r, v.r_m)
    return r


def feasibility_window(t: T.TargetConfig, sn: SensorSpecP, light: Light, mor_m: float, mode: str, model: T.VehicleSignature, *,
                       level_req: str = "R", agl_m: tuple[float, ...] = (40, 60, 80, 100, 120)) -> tuple[float, float] | None:
    """Feasible horizontal distance window (d_lo, d_hi) in metres or None (AWR-04 §9.2): d_lo from the keepouts with the margin
    20 m + 3 sigma_hold, d_hi = largest distance with N_eff >= N_req at maximum zoom (k_los = 1, worst aspect, moving target at speed_max)."""
    fz = LEVELS["feasibility"]
    margin = fz["margin_base_m"] + fz["margin_sigma_k"] * model.hold_sigma_h_m
    moving = t.motion.get("mode", "static") != "static"
    ts = T.TargetState1(t.id or "t", 0, (0.0, 0.0, 0.0), 0.0, (0.0, 0.0, 0.0), float(t.motion.get("speed_max_mps", 0.0)) if moving else 0.0,
                        moving, "UNAWARE", t.alertness)
    vols = T.keepout_volumes(t, ts, mode, margin, conservative=True, moving_as_circle=True)
    env = env_for(light, mor_m)
    n_req = n_req_for(level_req, t.n_req_px)
    view = T.view_of([t], [ts])
    best: tuple[float, float] | None = None
    for h in agl_m:
        d_lo = keepout_horizontal_radius(vols, h)

        def ok(d: float, h: float = h) -> bool:
            p = (d, 0.0, h)                            # d_hi is evaluated at maximum zoom
            res = perception_level(SensorState(sn, sn.f_max_mm, "nir" if sn.kind == "nir" else "auto"), view, 0,
                                   ViewGeom(p, 1.0, True, ts.speed_mps), env, n_req)
            return res.n_eff >= n_req

        if not ok(0.0):
            continue
        lo, hi = 0.0, 20000.0
        if ok(hi):
            d_hi = hi
        else:
            for _ in range(60):
                mid = 0.5 * (lo + hi)
                lo, hi = (mid, hi) if ok(mid) else (lo, mid)
            d_hi = math.floor(lo)
            while d_hi > 0 and not ok(d_hi):        # 1 m monotonicity check
                d_hi -= 1.0
        if d_hi - d_lo >= fz["w_min_m"] and (best is None or d_hi - d_lo > best[1] - best[0]):
            best = (round(d_lo, 1), float(d_hi))
    return best


SIGNATURES = ("perception_level", "perception_level_batch", "feasible_view", "feasibility_window", "zoom_for_level")
DATACLASSES = ("SensorSpecP", "SensorState", "ViewGeom", "EnvSample", "LevelResult", "FeasibleView")
