"""可行观测与可行窗口（M19-FR-021；AWR-04 §9.2；M18-M19 PRD §6.13；签名冻结于 WP-01）。

```text
zoom_for_level(sn, R, d_c, n_target) = clamp(n_target·R·p'(f)/d_c, f_min, f_max)（p' 随 F# 变化，迭代 3 次）
feasible_view(vp[n,3], T, ts, sn, env, level_req)：逐候选视点在云台限位内指向识别物，f = zoom_for_level(…, 1.3·N_req)，
    k_los 缺省 1；ok = N_eff ≥ N_req 且俯仰 ∈ [pitch_min, pitch_max]；ratio = N_eff/N_req
feasibility_window(T, sn, light, mor, mode, model) → (d_lo, d_hi) | None
    d_lo(h) = 禁入体（M18 keepout_volumes，外扩 20 m + 3sigma_hold，保守口径，运动扇形按整圆）在站位高度 h 处的水平半径
    d_hi(h) = 自 d_lo 起 N_eff(d, h) ≥ N_req 连续成立的最大水平距离（1 m 网格；f = f_max，k_los = 1，β 最不利，
              运动识别物 v⊥ = speed_max 且云台跟踪）
    取 h ∈ {40, 60, 80, 100, 120} m 中窗口最宽者；d_hi − d_lo < 10 m 或不存在 → None
```
均匀消光按 `light` 与 `mor_m` 给出的环境样本（照度取该档最不利值，AWR-04 §6.5）；`feasibility_detail` 另给出不可行原因（REST
SB-67 与 M20 的 581）。`golden_rows()` 生成 AWR-04 §9.2 的黄金表（`tests/perception/golden/feasibility_table.json`）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from awr.sim.targets.geometry import critical_dim

from .johnson import n_req_for
from .level import PairArrays, angles, cfg_value, evaluate
from .optics import p_prime_um
from .params import BAND_LUX, LEVELS, LIMITING
from .specs import EnvSample, SensorSpecP, SensorState

__all__ = ["FeasibilityResult", "FeasibleView", "env_for", "feasibility_detail", "feasibility_window", "feasible_view", "golden_rows",
           "keepout_horizontal_radius", "zoom_for_level"]

Light = Literal["day", "low", "night"]
_FZ = LEVELS["feasibility"]
D_CAP_M = 20000.0


@dataclass(frozen=True)
class FeasibleView:
    ok: NDArray                            # bool[n]
    ratio: NDArray                         # N_eff / N_req [n]
    f_mm: NDArray                          # [n]
    az_el: NDArray                         # [n, 2] rad（视线方位 ENU、仰角）
    limiting: NDArray                      # object[n]


@dataclass(frozen=True)
class FeasibilityResult:
    d_lo_m: float | None
    d_hi_m: float | None
    agl_m: float | None
    why: str | None                        # None 可行；light、contrast、blur、pixels、keepout_gt_range、narrow、no_sensor

    @property
    def feasible(self) -> bool:
        return self.why is None and self.d_hi_m is not None

    def window(self) -> tuple[float, float] | None:
        return (float(self.d_lo_m), float(self.d_hi_m)) if self.feasible else None  # type: ignore[arg-type]


def zoom_for_level(sn: SensorSpecP, r_m: float, d_c_m: float, n_target: float) -> float:
    """在斜距 r 处使关键维度 d_c 上有 n_target 个像素的焦距（mm），截断到镜头范围（p' 随 F# 变化，迭代 3 次）。"""
    f = sn.f_max_mm
    for _ in range(3):
        f = min(max(float(n_target) * float(r_m) * p_prime_um(sn, f) * 1e-3 / max(float(d_c_m), 1e-9), sn.f_min_mm), sn.f_max_mm)
    return f


def env_for(light: Light, mor_m: float, **kw: Any) -> EnvSample:
    """可行性评估的环境样本：该照度档最不利的照度、给定 MOR、k_cross = 1。"""
    return EnvSample(illum_lx=BAND_LUX[light], band=light, mor_m=float(mor_m), k_cross=1.0, **kw)


def _mode_of(sn: SensorSpecP) -> str:
    return "nir" if sn.kind == "nir" else "auto"


def _cfg_arrays(t: Any, n: int, R: NDArray, d: NDArray, occ: float, v_perp: float, k_los: NDArray | float) -> PairArrays:
    th = cfg_value(t, "thermal", {}) or {}
    return PairArrays(
        R, d, np.full(n, float(th.get("hot_fraction", 1.0))), np.full(n, float(cfg_value(t, "contrast0", 0.3))),
        np.full(n, float(cfg_value(t, "camouflage", 0.0) or 0.0)), np.full(n, float(cfg_value(t, "reflect_850", 0.3))),
        np.full(n, th.get("mode", "relative") == "absolute"), np.full(n, float(th.get("delta_t_k", 0.0) or 0.0)),
        np.full(n, float(th.get("t_surface_c", 30.0) or 30.0)), np.full(n, float(th.get("emissivity", 0.95) or 0.95)), np.full(n, float(occ)),
        np.broadcast_to(np.asarray(k_los, float), (n,)).copy(), np.ones(n, bool), np.full(n, float(v_perp)), np.ones(n, bool), np.ones(n, bool))


def _size(t: Any) -> NDArray:
    s = cfg_value(t, "size_m", {"l": 0.5, "w": 0.5, "h": 0.5})
    return np.array([float(s["l"]), float(s["w"]), float(s["h"])]) if isinstance(s, dict) else np.asarray(s, float)


def _moving(t: Any, ts: Any | None = None) -> bool:
    m = (cfg_value(t, "motion", {}) or {}).get("mode", "static") != "static"
    return bool(m or (ts is not None and (bool(getattr(ts, "moving", False)) or float(getattr(ts, "speed_mps", 0.0)) > 0.1)))


def feasible_view(vp: NDArray, t: Any, ts: Any, sn: SensorSpecP, env: EnvSample, level_req: str, *, n_req_px: float | None = None,
                  k_los: NDArray | None = None) -> FeasibleView:
    """逐候选视点的可行观测（M20 站位选择与跟随复核；签名冻结于 WP-01）。"""
    vp = np.atleast_2d(np.asarray(vp, float)).reshape(-1, 3)
    n = len(vp)
    n_req = n_req_for(level_req, n_req_px if n_req_px is not None else cfg_value(t, "n_req_px", None))
    pos = np.asarray(ts.pos, float).reshape(3)
    R, eps, beta = angles(vp, np.broadcast_to(pos, (n, 3)), np.full(n, float(ts.yaw_rad)))
    moving = _moving(t, ts)
    ov = cfg_value(t, "critical_dim_m", None)
    d = np.asarray(critical_dim(np.broadcast_to(_size(t), (n, 3)), eps, beta, worst=moving, override_m=None if ov is None else np.full(n, ov)),
                   float).reshape(n)
    margin = float(_FZ["zoom_margin"])
    f = np.array([zoom_for_level(sn, float(R[j]), float(d[j]), n_req * margin) for j in range(n)])
    dv = pos[None, :] - vp
    az = np.arctan2(dv[:, 1], dv[:, 0])
    el = np.arctan2(dv[:, 2], np.hypot(dv[:, 0], dv[:, 1]))
    pitch_ok = (el >= math.radians(sn.pitch_min_deg) - 1e-9) & (el <= math.radians(sn.pitch_max_deg) + 1e-9)
    kl = np.ones(n) if k_los is None else np.asarray(k_los, float).reshape(n)
    v_perp = float(getattr(ts, "speed_mps", 0.0)) if moving else 0.0
    a = _cfg_arrays(t, n, R, d, float(cfg_value(t, "occlusion", 0.0) or 0.0), v_perp, kl)
    r = evaluate(SensorState(sn, sn.f_max_mm, _mode_of(sn)), a, env, n_req, f_mm=f)  # type: ignore[arg-type]
    lim = np.array([None if c < 0 else LIMITING[int(c)] for c in r["lim"]], dtype=object)
    lim = np.where(pitch_ok, lim, "fov")
    ok = pitch_ok & (r["n_eff"] >= n_req)
    return FeasibleView(ok, r["n_eff"] / n_req, f, np.stack([az, el], axis=1), lim)


def keepout_horizontal_radius(vols: list[Any], agl_m: float, base_z: float = 0.0) -> float:
    """禁入体在识别物基准以上 agl_m 处的水平半径（球与半球按三维距离在该高度截取）。"""
    z = base_z + float(agl_m)
    r = 0.0
    for v in vols:
        if v.shape in ("sphere", "hemisphere"):
            dz = z - float(v.center[2])
            if v.shape == "hemisphere" and dz < 0:
                continue
            r = max(r, math.sqrt(max(v.r_m ** 2 - dz ** 2, 0.0)))
        elif v.z_min_m <= z <= v.z_max_m:
            r = max(r, float(v.r_m))
    return r


def _keepouts(t: Any, ts: Any, mode: str, margin: float, model: Any, light: str, mor_m: float) -> list[Any]:
    from awr.sim.targets import keepout_volumes
    from awr.sim.targets.signature import VehicleSignature, signature_of

    if mode != "physical":
        return list(keepout_volumes(t, ts, mode, margin, conservative=True, moving_as_circle=True))
    sig = model if isinstance(model, VehicleSignature) else replace(signature_of(str(getattr(model, "model_id", "p600_mid360")), None),
                                                                     hold_sigma_h_m=float(getattr(model, "hold_sigma_h_m", 0.02)))
    from awr.sim.targets.envview import day_night

    d_env, n_env = day_night("rural", float(mor_m))
    env = d_env if light == "day" else n_env if light == "night" else None
    return list(keepout_volumes(t, ts, mode, margin, conservative=True, moving_as_circle=True, models=[sig], env=env))


def _target_state(t: Any) -> Any:
    from awr.sim.targets import TargetState1

    moving = _moving(t)
    sp = float((cfg_value(t, "motion", {}) or {}).get("speed_max_mps", 0.0) or 0.0) if moving else 0.0
    return TargetState1(str(getattr(t, "id", "t")), 0, (0.0, 0.0, 0.0), 0.0, (0.0, 0.0, 0.0), sp, moving, "UNAWARE",
                        float(cfg_value(t, "alertness", 0.5) or 0.5))


def feasibility_detail(t: Any, sn: SensorSpecP | None, light: Light, mor_m: float, mode: str, model: Any, *, level_req: str = "R",
                       agl_m: tuple[float, ...] = tuple(_FZ["agl_m"]), env: EnvSample | None = None) -> FeasibilityResult:
    """`feasibility_window` 的明细：窗口、所取站位高度与不可行原因。"""
    if sn is None:
        return FeasibilityResult(None, None, None, "no_sensor")
    ts = _target_state(t)
    margin = float(_FZ["margin_base_m"]) + float(_FZ["margin_sigma_k"]) * float(getattr(model, "hold_sigma_h_m", 0.02))
    vols = _keepouts(t, ts, mode, margin, model, light, mor_m)
    env = env or env_for(light, mor_m)
    n_req = n_req_for(level_req, cfg_value(t, "n_req_px", None))
    sz = _size(t)
    ov = cfg_value(t, "critical_dim_m", None)
    pp = p_prime_um(sn, sn.f_max_mm)
    d_cmax = float(ov) if ov is not None else math.sqrt(sz[0] * sz[1] + min(sz[1] * sz[2], sz[0] * sz[2]))
    d_max = min(D_CAP_M, d_cmax * sn.f_max_mm / (n_req * pp * 1e-3))
    grid = np.arange(0.0, math.floor(d_max) + 2.0)
    n = len(grid)
    st = SensorState(sn, sn.f_max_mm, _mode_of(sn))  # type: ignore[arg-type]
    best: tuple[float, float, float] | None = None
    why_votes: list[str] = []
    for h in agl_m:
        d_lo = keepout_horizontal_radius(vols, float(h))
        P = np.stack([grid, np.zeros(n), np.full(n, float(h))], axis=1)
        R, eps, beta = angles(P, np.zeros((n, 3)), np.zeros(n))
        d = np.asarray(critical_dim(np.broadcast_to(sz, (n, 3)), eps, beta, worst=True, override_m=None if ov is None else np.full(n, ov)),
                       float).reshape(n)
        a = _cfg_arrays(t, n, R, d, float(cfg_value(t, "occlusion", 0.0) or 0.0), ts.speed_mps, 1.0)
        r = evaluate(st, a, env, n_req)
        ok = r["n_eff"] >= n_req
        s = math.ceil(d_lo - 1e-9)
        if s >= n or not ok[s]:
            if s < n:
                code = int(r["lim"][s])
                why_votes.append("keepout_gt_range" if bool(ok[:s].any()) else (LIMITING[code] if code >= 0 else "pixels"))
            else:
                why_votes.append("keepout_gt_range" if bool(ok.any()) else "pixels")
            continue
        bad = np.flatnonzero(~ok[s:])
        d_hi = float(s + (bad[0] - 1 if bad.size else n - 1 - s))
        if d_hi - d_lo < float(_FZ["w_min_m"]):
            why_votes.append("narrow")
            continue
        if best is None or d_hi - d_lo > best[1] - best[0]:
            best = (round(d_lo, 1), d_hi, float(h))
    if best is not None:
        return FeasibilityResult(best[0], best[1], best[2], None)
    why = next((w for w in ("narrow", "keepout_gt_range", "light", "contrast", "blur", "los", "pixels") if w in why_votes), "pixels")
    return FeasibilityResult(None, None, None, why)


def feasibility_window(t: Any, sn: SensorSpecP, light: Light, mor_m: float, mode: str, model: Any, *, level_req: str = "R",
                       agl_m: tuple[float, ...] = (40, 60, 80, 100, 120)) -> tuple[float, float] | None:
    """以水平距离表示的站位可行窗口 `(d_lo, d_hi)`（m）或 None（AWR-04 §9.2；签名冻结于 WP-01）。"""
    return feasibility_detail(t, sn, light, mor_m, mode, model, level_req=level_req, agl_m=tuple(agl_m)).window()


# ---------------------------------------------------------------- 黄金表（AWR-04 §9.2 重生成，附录 B.3 L-05）
GOLDEN_SENSORS = {
    "eo_1080p_48mm": ("day", lambda: _s("eo")),
    "nir_builtin": ("night", lambda: _s("nir")),
    "nir_ill_l": ("night", lambda: _s("nir_ext")),
    "lwir_75mm": ("night", lambda: _s("lwir75")),
    "lwir_100mm": ("night", lambda: _s("lwir100")),
}
GOLDEN_CLASSES = {
    "person": {"class": "person"},
    "dog": {"class": "dog"},
    "vehicle": {"class": "vehicle", "motion": {"mode": "path", "speed_mps": 10.0, "speed_max_mps": 15.0}},
}


def _s(name: str) -> SensorSpecP:
    from .specs import gx40, gx40_nir, lwir640

    return {"eo": lambda: gx40("1080p"), "nir": lambda: gx40_nir(False), "nir_ext": lambda: gx40_nir(True),
            "lwir75": lambda: lwir640(75.0), "lwir100": lambda: lwir640(100.0)}[name]()


def golden_rows(models: dict[str, Any] | None = None, *, mors: tuple[float, ...] = (10000.0, 3000.0)) -> list[dict]:
    """AWR-04 §9.2 可行窗口表（geometric 三类 × 五种传感器 × 两档 MOR；physical 行按 `models` 中的签名）。"""
    from awr.sim.targets import TargetConfig
    from awr.sim.targets.signature import P600

    rows: list[dict] = []
    combos: list[tuple[str, str, Any]] = [(c, "geometric", P600) for c in GOLDEN_CLASSES]
    for key, (cls, sig) in (models or {}).items():
        combos.append((cls, f"physical:{key}", sig))
    for cls, mode_key, sig in combos:
        t = TargetConfig.from_dict({**GOLDEN_CLASSES[cls], "pos": [0.0, 0.0, 0.0]}, f"{cls}-01")
        mode = mode_key.split(":", 1)[0]
        for sname, (light, mk) in GOLDEN_SENSORS.items():
            sn = mk()
            for mor in mors:
                r = feasibility_detail(t, sn, light, mor, mode, sig)  # type: ignore[arg-type]
                rows.append({"class": cls, "mode": mode_key, "model_id": getattr(sig, "model_id", "p600_mid360"), "sensor": sname, "light": light,
                             "mor_m": mor, "level_req": "R", "d_lo_m": r.d_lo_m, "d_hi_m": r.d_hi_m, "agl_m": r.agl_m, "why": r.why})
    return rows
