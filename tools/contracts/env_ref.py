"""Environment reference implementation for golden generation (M07 §6.3; migrated from .cache/research/g06/env_ref.py).

Pure float64 functions; the only oracle of packages/contracts/env/golden/*.json. M07 ports these line by line to
python/awr/environment/* and apps/web/src/engine/environment/state/*.ts and checks both against the golden
(M07-FR-007, M07-AC-002). Changes relative to g06 (M07 §9.3): *_ms fields renamed *_mps; EnvScalars is a positional
vector in presets.json fields[] order; gust_along uses the frozen event direction; derive adds v_rain_mps,
v_snow_mps, wet_target and vmax_vis_mps; eval_env routes, windows and rates come from presets.json.

Every constant is read from presets.json (constants, windows, rates_per_s, defaults.config); the only literal
numbers are the ones M07 §6.4 marks as K (code constants covered by golden).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from awr.contracts.presets import FIELD_PATHS, NF, PRESETS

C = PRESETS["constants"]
K_MOR = C["k_mor"]  # ln 20: WMO MOR (5 % contrast), sigma = K_MOR / MOR
K_V2 = C["k_v2"]  # ln 50: Koschmieder 2 %, only for the spectral (Kim) conversion
H_HAZE = float(C["haze_scale_h_m"])
H_NS = int(C["anchor_grid_ms"]) * 1_000_000  # 20 ms env grid

FIELDS = PRESETS["fields"]
SPACE = tuple(f["space"] for f in FIELDS)
GROUP = tuple(f["group"] for f in FIELDS)
USER_AXIS = tuple(bool(f.get("user_axis")) for f in FIELDS)
WINDOWS = PRESETS["windows"]
RATES = PRESETS["rates_per_s"]
IDX = {p: i for i, p in enumerate(FIELD_PATHS)}
SPEED_REF, DIR, W_MEAN, SIGMA_REF = IDX["wind.speed_ref_mps"], IDX["wind.dir_from_deg"], IDX["wind.w_mean_mps"], IDX["wind.turb_sigma_u_ref_mps"]
GUST_AMP, GUST_RATE, GUST_LEN = IDX["wind.gust_amp_mps"], IDX["wind.gust_rate_hz"], IDX["wind.gust_length_m"]
COVER, CTYPE, BASE, TOP = IDX["cloud.cover"], IDX["cloud.type"], IDX["cloud.base_m"], IDX["cloud.top_m"]
RAIN, SNOW = IDX["precip.rain_mmh"], IDX["precip.snow_mmh"]
MOR_BG, FOG_TOP, DUST, ISA_DT, RH = (IDX["atmosphere.mor_bg_m"], IDX["atmosphere.fog_top_agl_m"], IDX["atmosphere.dust"],
                                     IDX["atmosphere.isa_dt_c"], IDX["atmosphere.rh"])
DEFAULT_PROFILE = PRESETS["defaults"]["config"]["wind"]["profile"]
FLAG_IN_FOG_LAYER, FLAG_BELOW_CLOUD_PRECIP = 16, 32  # rt/enums.json EnvFlags

assert len(FIELDS) == NF


def smoothstep(a: float, b: float, x: float) -> float:
    t = min(max((x - a) / (b - a), 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def clamp(x: float, a: float, b: float) -> float:
    return min(max(x, a), b)


# ---------------------------------------------------------------- conventions (M07 §6.3.1)
def from_to_uv(speed: float, dir_from_deg: float) -> tuple[float, float]:
    t = math.radians(dir_from_deg)
    return (-speed * math.sin(t), -speed * math.cos(t))


def uv_to_from(u: float, v: float, calm: float = 1e-6) -> tuple[float, float, bool]:
    s = math.hypot(u, v)
    if s < calm:
        return (0.0, 0.0, True)
    return (s, (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0, False)


def e(dir_from_deg: float) -> tuple[float, float]:
    t = math.radians(dir_from_deg)
    return (-math.sin(t), -math.cos(t))


def n(dir_from_deg: float) -> tuple[float, float]:
    ex, ey = e(dir_from_deg)
    return (-ey, ex)


def shortest_arc(a: float, b: float) -> float:
    return ((b - a + 540.0) % 360.0) - 180.0


def enu_to_three(u: float, v: float, w: float) -> tuple[float, float, float]:
    return (u, w, -v)


def three_to_enu(x: float, y: float, z: float) -> tuple[float, float, float]:
    return (x, -z, y)


def enu_to_ned(u: float, v: float, w: float) -> tuple[float, float, float]:
    return (v, u, -w)


# ---------------------------------------------------------------- scalars and presets
def get_path(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def default_vector() -> list[float]:
    return [float(get_path(PRESETS["defaults"]["scalars"], p)) for p in FIELD_PATHS]


def preset_snapshot(pid: str) -> dict[int, float]:
    """The 17 non-user-axis fields of one preset, keyed by index."""
    p = next(x for x in PRESETS["presets"] if x["id"] == pid)
    out = {}
    for i, path in enumerate(FIELD_PATHS):
        v = get_path(p["scalars"], path)
        if v is not None:
            out[i] = float(v)
    return out


def overlay(base: Sequence[float], pid: str) -> list[float]:
    out = [float(x) for x in base]
    for i, v in preset_snapshot(pid).items():
        out[i] = v
    return out


def preset_vector(pid: str, base: Sequence[float] | None = None) -> list[float]:
    return overlay(default_vector() if base is None else base, pid)


def route_for(from_preset: str | None, to_preset: str) -> list[str]:
    for r in PRESETS["routes"]:
        if r["from"] == from_preset and r["to"] == to_preset:
            return list(r["via"])
    return []


# ---------------------------------------------------------------- eval_env (M07 §6.3.2)
def interp(space: str, a: float, b: float, k: float) -> float:
    if space == "log":
        return math.exp(math.log(a) + (math.log(b) - math.log(a)) * k)
    if space == "arc":
        return (a + shortest_arc(a, b) * k) % 360.0
    return a + (b - a) * k


def precip_level(s: Sequence[float]) -> float:
    return s[RAIN] + 10.0 * s[SNOW]


@dataclass
class Keyframe:
    mode: str  # smooth | exp | step
    t0_ns: int
    t1_ns: int
    from_: list[float]
    to: list[float]
    via: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"mode": self.mode, "t0_ns": self.t0_ns, "t1_ns": self.t1_ns, "from": self.from_, "to": self.to, "via": self.via}


def exp_t1_ns(t0_ns: int) -> int:
    """exp mode: t1 = t0 + ceil(exp_settle / min(rates)) s (residual e^-12, snapped to `to` at t1)."""
    return t0_ns + math.ceil(C["exp_settle"] / min(RATES.values())) * 1_000_000_000


def eval_env(kf: Keyframe, t_ns: int, out: list[float] | None = None) -> list[float]:
    out = [0.0] * NF if out is None else out
    if kf.mode == "step" or t_ns >= kf.t1_ns:
        out[:] = kf.to
        return out
    if t_ns <= kf.t0_ns:
        out[:] = kf.from_
        return out
    if kf.mode == "exp":
        dt = (t_ns - kf.t0_ns) * 1e-9
        for i in range(NF):
            out[i] = interp(SPACE[i], kf.from_[i], kf.to[i], 1.0 - math.exp(-RATES[GROUP[i]] * dt))
        return out
    route = [kf.from_] + [overlay(kf.to, v) for v in kf.via] + [kf.to]
    nseg = len(route) - 1
    x = (t_ns - kf.t0_ns) / (kf.t1_ns - kf.t0_ns)
    seg = min(int(x * nseg), nseg - 1)
    xl = x * nseg - seg
    A, B = route[seg], route[seg + 1]
    W = WINDOWS["enter" if precip_level(B) > precip_level(A) else "leave"]
    for i in range(NF):
        w = W[GROUP[i]]
        out[i] = interp(SPACE[i], A[i], B[i], smoothstep(w[0], w[1], xl))
    return out


# ---------------------------------------------------------------- profile (M07 §6.3.6)
def profile(z_agl: float, kind: str = "log", z_ref: float = 10.0, z0: float = 0.5, d: float = 0.0, alpha: float = 0.25) -> float:
    if kind == "uniform":
        return 1.0 if z_agl > 0 else 0.0
    if kind == "power":
        return (max(z_agl, 0.0) / z_ref) ** alpha
    if z_agl <= d + z0:
        return 0.0  # WindNinja windProfile.cpp L73-76
    return math.log((z_agl - d) / z0) / math.log((z_ref - d) / z0)


def profile_cfg(z_agl: float, cfg: dict | None = None) -> float:
    c = DEFAULT_PROFILE if cfg is None else cfg
    return profile(z_agl, c["kind"], c["z_ref_m"], c["z0_m"], c["d_m"], c["alpha"])


# ---------------------------------------------------------------- derive (M07 §6.3.3)
DERIVED_KEYS = ("rain_eff_mmh", "snow_eff_mmh", "sigma_rain", "sigma_snow", "sigma_precip", "sigma_bg", "sigma_fog", "sigma_haze0",
                "sigma_ground", "mor_m", "rain_k", "snow_k", "sun_vis", "mp_lambda", "cloud_od", "v_rain_mps", "v_snow_mps",
                "wet_target", "vmax_vis_mps")


def derive(s: Sequence[float], prof: dict | None = None) -> dict[str, float]:
    g0, g1 = C["precip_gate_cover"]
    gate = smoothstep(g0, g1, s[COVER])
    R = s[RAIN] * gate
    S = s[SNOW] * gate
    d: dict[str, float] = {"rain_eff_mmh": R, "snow_eff_mmh": S}
    d["sigma_rain"] = C["rain_sigma_coef"] * R ** C["rain_sigma_exp"] if R > 0 else 0.0
    d["sigma_snow"] = C["snow_sigma_coef"] * S ** C["snow_sigma_exp"] if S > 0 else 0.0
    d["sigma_precip"] = d["sigma_rain"] + d["sigma_snow"]
    d["sigma_bg"] = K_MOR / clamp(s[MOR_BG], float(C["mor_bg_min_m"]), float(C["mor_bg_max_m"]))
    d["sigma_fog"] = C["fog_fraction"] * d["sigma_bg"] if s[FOG_TOP] > 0 else 0.0
    d["sigma_haze0"] = d["sigma_bg"] - d["sigma_fog"]
    d["sigma_ground"] = d["sigma_haze0"] + d["sigma_fog"] + d["sigma_precip"]
    d["mor_m"] = K_MOR / d["sigma_ground"]
    d["rain_k"] = clamp(math.log1p(R) / math.log(51.0), 0.0, 1.0)
    d["snow_k"] = clamp(math.log1p(10.0 * S) / math.log(51.0), 0.0, 1.0)
    d["sun_vis"] = (1.0 - 0.9 * s[COVER] ** 1.5) * (1.0 - 0.6 * s[DUST])
    d["mp_lambda"] = 4.1 * max(R, 0.1) ** -0.21
    d["cloud_od"] = clamp((s[TOP] - s[BASE]) * 0.022 * 0.35, 0.0, 6.0)
    d["v_rain_mps"] = 9.65 - 10.3 * math.exp(-0.6 * clamp(4.0 / d["mp_lambda"], 0.3, 5.0))
    d["v_snow_mps"] = 0.6 + 0.9 * d["snow_k"]
    d["wet_target"] = clamp(C["wet_gain"] * d["rain_k"], 0.0, 1.0)
    d["vmax_vis_mps"] = s[SPEED_REF] * profile_cfg(150.0, prof) + s[GUST_AMP] + 3.0 * s[SIGMA_REF] + abs(s[W_MEAN])
    return d


def puddle_target(d: dict[str, float]) -> float:
    return d["wet_target"] ** C["puddle_exp"]


def mor_bg_from_total(mor_total: float, rain_mmh: float, snow_mmh: float, cover: float) -> float:
    """Preset authoring: METAR-like total MOR -> background MOR (excluding precipitation)."""
    s = default_vector()
    s[COVER], s[TOP], s[BASE], s[RAIN], s[SNOW] = cover, 0.0, 0.0, rain_mmh, snow_mmh
    s[MOR_BG], s[FOG_TOP], s[DUST] = float(C["mor_bg_max_m"]), 0.0, 0.0
    d = derive(s)
    sbg = max(K_MOR / mor_total - d["sigma_precip"], K_MOR / C["mor_bg_max_m"])
    return K_MOR / sbg


# ---------------------------------------------------------------- optics (M07 §6.3.4, §6.3.12)
def flat_len(ro_z: float, rd_z: float, L: float, top: float) -> float:
    t0, t1 = 0.0, L
    if abs(rd_z) > 1e-5:
        tc = (top - ro_z) / rd_z
        if rd_z > 0:
            t1 = min(t1, tc)
        else:
            t0 = max(t0, tc)
    elif ro_z > top:
        return 0.0
    return max(t1 - t0, 0.0)


def optical_depth(z0_agl: float, rd_z: float, L: float, d: dict[str, float], fog_top: float, cloud_base: float, H: float = H_HAZE) -> float:
    a = d["sigma_haze0"] * math.exp(-z0_agl / H)
    k = rd_z * L / H
    od = a * L * ((1.0 - math.exp(-k)) / k if abs(k) > 1e-4 else 1.0)
    if d["sigma_fog"] > 0:
        od += d["sigma_fog"] * flat_len(z0_agl, rd_z, L, fog_top)
    if d["sigma_precip"] > 0:
        od += d["sigma_precip"] * flat_len(z0_agl, rd_z, L, cloud_base)
    return od


def sigma_at(z_agl: float, d: dict[str, float], fog_top: float, cloud_base: float) -> tuple[float, int]:
    s = d["sigma_haze0"] * math.exp(-z_agl / H_HAZE)
    flags = 0
    if z_agl < fog_top:
        s += d["sigma_fog"]
        flags |= FLAG_IN_FOG_LAYER
    if z_agl < cloud_base:
        s += d["sigma_precip"]
        if d["sigma_precip"] > 0:
            flags |= FLAG_BELOW_CLOUD_PRECIP
    return s, flags


def kim_q(v2_km: float) -> float:
    """Kim (2001) size-distribution exponent q(V2), V2 in km (M07 §6.3.12)."""
    if v2_km > 50:
        return 1.6
    if v2_km > 6:
        return 1.3
    if v2_km > 1:
        return 0.16 * v2_km + 0.34
    if v2_km > 0.5:
        return v2_km - 0.5
    return 0.0


def sigma_lambda(d: dict[str, float], lam_nm: float) -> float:
    """sigma at wavelength lam: sigma_bg (lam/550)^-q + sigma_precip, V2 = ln50 / sigma_bg in km."""
    v2_km = K_V2 / d["sigma_bg"] / 1000.0
    return d["sigma_bg"] * (lam_nm / 550.0) ** -kim_q(v2_km) + d["sigma_precip"]


def lidar_two_way(d: dict[str, float], lam_nm: float, r_m: float) -> float:
    """Two-way transmittance over a horizontal range r at ground level: exp(-2 sigma_lambda r)."""
    return math.exp(-2.0 * sigma_lambda(d, lam_nm) * r_m)


# ---------------------------------------------------------------- gust fronts (M07 §6.3.7)
@dataclass(frozen=True)
class GustEvent:
    kind: int
    id: int
    t_create_ns: int
    x0_m: float
    s0_m: float
    amp_mps: float
    lam_m: float
    dir_from_deg: float
    s_span_m: float

    def to_list(self) -> list:
        return [self.kind, self.id, self.t_create_ns, self.x0_m, self.s0_m, self.amp_mps, self.lam_m, self.dir_from_deg, self.s_span_m]


def gust_create(ev_id: int, t_ns: int, amp: float, d_m: float, dir_from_deg: float, S_t: float, speed_ref: float,
                bounds_min: Sequence[float], bounds_max: Sequence[float], f_adv: float) -> GustEvent:
    ex, ey = e(dir_from_deg)
    X = f_adv * S_t
    dots = [ex * x + ey * y for x in (bounds_min[0], bounds_max[0]) for y in (bounds_min[1], bounds_max[1])]
    s_min, s_max = min(dots), max(dots)
    margin = f_adv * speed_ref * C["gust_lead_s"] + C["gust_buffer_m"]
    s0 = s_min - margin
    return GustEvent(1, ev_id, t_ns, X, s0, amp, 2.0 * d_m, dir_from_deg, s_max - s0)


def gust(p_xy: Sequence[float], S_t: float, ev: GustEvent, f_adv: float) -> float:
    ex, ey = e(ev.dir_from_deg)
    xi = f_adv * S_t - ev.x0_m + ev.s0_m - (ex * p_xy[0] + ey * p_xy[1])
    if 0.0 <= xi <= ev.lam_m:
        return 0.5 * ev.amp_mps * (1.0 - math.cos(2.0 * math.pi * xi / ev.lam_m))
    return 0.0


def gust_expired(S_t: float, ev: GustEvent, f_adv: float) -> bool:
    return f_adv * S_t - ev.x0_m > ev.s_span_m + ev.lam_m


# ---------------------------------------------------------------- wind library sector slots (M07 §6.3.13; V0.3 runtime)
def sector_slots(theta_from: float, sectors_deg: Sequence[float], antisymmetric: bool) -> list[tuple[int, float, float, float]]:
    """-> [(file_index, weight, sign, a_deg)] x 2; sectors are stored for [0, 180) when antisymmetric."""
    full = []
    for i, a in enumerate(sectors_deg):
        full.append((i, float(a), 1.0))
        if antisymmetric:
            full.append((i, (a + 180.0) % 360.0, -1.0))
    full.sort(key=lambda x: x[1])
    dd = theta_from % 360.0
    angs = [x[1] for x in full]
    j = next((k for k, a in enumerate(angs) if a > dd), 0)
    lo, hi = full[j - 1], full[j]
    span = (hi[1] - lo[1]) % 360.0 or 360.0
    w = ((dd - lo[1]) % 360.0) / span
    return [(lo[0], 1.0 - w, lo[2], lo[1]), (hi[0], w, hi[2], hi[1])]


# ---------------------------------------------------------------- ISA (M07 §6.3.11)
def isa(h_msl_m: float, isa_dt_c: float) -> dict[str, float]:
    t_isa = 288.15 - 0.0065 * h_msl_m
    t = t_isa + isa_dt_c
    p = 101325.0 * (t_isa / 288.15) ** 5.25588
    return {"temperature_c": t - 273.15, "pressure_pa": p, "rho_kgm3": p / (287.053 * t)}


# ---------------------------------------------------------------- integral anchors (M07 §6.3.10)
@dataclass
class Anchors:
    t_ns: int = 0
    s_m: float = 0.0
    d_enu_m: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    fall_rain_m: float = 0.0
    fall_snow_m: float = 0.0
    wetness: float = 0.0
    puddle: float = 0.0

    def to_json(self) -> dict:
        return {"t_ns": self.t_ns, "s_m": self.s_m, "d_enu_m": list(self.d_enu_m), "fall_rain_m": self.fall_rain_m,
                "fall_snow_m": self.fall_snow_m, "wetness": self.wetness, "puddle": self.puddle}


def anchors_initial(kf: Keyframe, t_ns: int) -> Anchors:
    """Scenario start / reset: s = d = fall = 0, wetness = wet_target, puddle = wet_target^1.8 (steady state)."""
    d = derive(eval_env(kf, t_ns))
    return Anchors(t_ns=t_ns, wetness=d["wet_target"], puddle=puddle_target(d))


def rates(kf: Keyframe, t_ns: int) -> tuple[float, ...]:
    s = eval_env(kf, t_ns)
    d = derive(s)
    ex, ey = e(s[DIR])
    return (s[SPEED_REF], s[SPEED_REF] * ex, s[SPEED_REF] * ey, s[W_MEAN], d["v_rain_mps"], d["v_snow_mps"], d["wet_target"], puddle_target(d))


def advance(A: Anchors, kf: Keyframe, k0: int, k1: int) -> Anchors:
    h = H_NS * 1e-9
    r0 = rates(kf, k0 * H_NS)
    for k in range(k0, k1):
        r1 = rates(kf, (k + 1) * H_NS)
        A.s_m += 0.5 * h * (r0[0] + r1[0])
        A.d_enu_m = [A.d_enu_m[j] + 0.5 * h * (r0[1 + j] + r1[1 + j]) for j in range(3)]
        A.fall_rain_m += 0.5 * h * (r0[4] + r1[4])
        A.fall_snow_m += 0.5 * h * (r0[5] + r1[5])
        tw = C["wetness_tau_up_s"] if r0[6] > A.wetness else C["wetness_tau_down_s"]
        A.wetness += (r0[6] - A.wetness) * (1.0 - math.exp(-h / tw))
        tp = C["puddle_tau_up_s"] if r0[7] > A.puddle else C["puddle_tau_down_s"]
        A.puddle += (r0[7] - A.puddle) * (1.0 - math.exp(-h / tp))
        r0 = r1
    A.t_ns = k1 * H_NS
    return A
