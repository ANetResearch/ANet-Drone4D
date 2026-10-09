"""M3 / coverage evaluation: does a weather-blind scan plan still see the targets it was planned for?

One task = (world, weather, seed). A 500 m × 500 m area of interest is picked in the city; ground targets (standing
pedestrians) are placed on open ground inside it. For each perception level (D/R/I) four planners size the scan
(altitude, focal length, strip spacing, fleet split) with `plan_gsd_scan`, differing only in what they know:

* `blind`  — standard visibility (10 km) and no wind;
* `vis`    — the 4D field's slant extinction at the planned altitude, no wind;
* `wind`   — standard visibility, the field's wind at the planned altitude for the sortie energy budget;
* `aware`  — both.

Every plan is then scored against the ground truth with `evaluate_scan`: slant optical depth from the environment field,
DSM line of sight, and the true sortie endurance under the field's wind. Weather comes from the presets, plus a MOR sweep
(`mor_bg_m` patched on the clear preset) for the sensitivity figure.
"""

from __future__ import annotations

import math

import numpy as np

from common import PRESETS, WORLDS
from harness import PaperSim

SIGMA_STD = 3.912 / 10000.0
LEVELS = ("D", "R", "I")
POLICIES = ("blind", "vis", "wind", "aware")
M_KG, CDA, P_HOVER, RHO, E_USE = 3.5, 0.035, 515.0, 1.225, 0.85 * 222.0


def power_p600(v_air: np.ndarray, v_z: np.ndarray) -> np.ndarray:
    """P600 reference power: hover power scaled by thrust^1.5 (drag-tilted thrust) plus climb work at 70 %."""
    mg = M_KG * 9.81
    drag = 0.5 * RHO * CDA * np.asarray(v_air, np.float64) ** 2
    T = np.hypot(mg, drag)
    return P_HOVER * (T / mg) ** 1.5 + mg * np.maximum(np.asarray(v_z, np.float64), 0.0) / 0.7


def grid(worlds=WORLDS, presets=PRESETS, seeds=3, mor=(100, 150, 200, 300, 500, 800, 1200, 2000, 5000), mor_worlds=None,
         mor_seeds=3) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    if isinstance(presets, str):
        presets = presets.split(",")
    out = [{"world": w, "preset": p, "mor": None, "seed": s} for w in worlds for p in presets for s in range(int(seeds))]
    mw = worlds if mor_worlds is None else (mor_worlds.split(",") if isinstance(mor_worlds, str) else mor_worlds)
    out += [{"world": w, "preset": "clear", "mor": float(m), "seed": s} for w in mw for m in (mor or ()) for s in range(int(mor_seeds))]
    return out


def _aoi(sim: PaperSim, rng: np.random.Generator, side: float = 500.0) -> np.ndarray:
    g = sim.world.dsm_grid()
    a = np.asarray(g.a)
    lo = np.array([g.x0_m, g.y0_m]) + 120.0
    ext = np.array([a.shape[1], a.shape[0]]) * g.cell_m - 240.0
    side = float(min(side, 0.8 * ext.min()))
    hi = lo + ext - side
    c = rng.uniform(lo, hi)
    return np.array([c, c + [side, 0.0], c + [side, side], c + [0.0, side]])


def _targets(sim: PaperSim, aoi: np.ndarray, rng: np.random.Generator, n: int = 400) -> np.ndarray:
    w = sim.world
    out = []
    while sum(len(x) for x in out) < n:
        P = rng.uniform(aoi[0], aoi[2], (4 * n, 2))
        P = P[np.abs(w.height_dsm(P) - w.ground_dtm(P)) < 0.5]
        out.append(P)
    P = np.concatenate(out)[:n]
    return np.c_[P, w.ground_dtm(P) + 0.9]


def run(task: dict) -> list[dict]:
    sim = PaperSim(task["world"], preset=task["preset"], world_seed=int(task["seed"]))
    try:
        return _run(sim, task)
    finally:
        sim.close()


def _run(sim: PaperSim, task: dict) -> list[dict]:
    from awr.sim.perception.levels import EoSensor
    from awr.swarm.coverage.gsd import ScanRequest, evaluate_scan, plan_gsd_scan, sortie_endurance_s

    rng = np.random.default_rng([int(task["seed"]), sum(map(ord, task["world"]))])
    sim.env_set({"dir_from_deg": float(rng.uniform(0, 360))})
    if task.get("mor"):
        sim.env_patch({"atmosphere": {"mor_bg_m": float(task["mor"])}})
    w, env = sim.world, sim.env
    t_ns = int(sim.core.clock.t_ns)
    aoi = _aoi(sim, rng)
    T = _targets(sim, aoi, rng)
    c = aoi.mean(0)
    pads = sim.flat_slots(1, near=(float(c[0]), float(c[1])))
    homes = np.c_[pads, w.ground_dtm(pads)]
    z_g = float(np.median(w.ground_dtm(T[:, :2])))
    sensor = EoSensor()

    def od(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return np.asarray(env.optical_depth(a, b, t_ns), np.float64)

    def los(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return np.concatenate([np.asarray(w.los_batch(a[i:i + 64], b[i:i + 64]), bool).reshape(-1)
                               for i in range(0, len(a), 64)]) if len(a) else np.zeros(0, bool)

    def wind_xy(h: float) -> np.ndarray:
        return sim.wind_at([c[0], c[1], z_g + h])[0, :2]

    def sigma_eff(h: float) -> float:
        """Path-mean extinction of the strip-edge slant (15° off nadir) from altitude h down to a standing target."""
        ang = np.linspace(0.0, 2.0 * math.pi, 4, endpoint=False)
        off = (h - 0.9) * math.tan(math.radians(15.0))
        p0 = np.repeat([[c[0], c[1], z_g + h]], 4, axis=0)
        p1 = np.c_[c[0] + off * np.cos(ang), c[1] + off * np.sin(ang), np.full(4, z_g + 0.9)]
        L = np.linalg.norm(p1 - p0, axis=1)
        return max(float(np.max(od(p0, p1) / L)), 1e-6)

    mor_50 = float(sim.mor_at([c[0], c[1], z_g + 50.0])[0])
    rows = []
    for lvl in LEVELS:
        req = ScanRequest(aoi=aoi, level=lvl)
        for pol in POLICIES:
            use_vis, use_wind = pol in ("vis", "aware"), pol in ("wind", "aware")
            h_guess = 60.0
            plan = None
            sig_seen = 0.0
            for _ in range(6):  # fixed point: sigma and wind depend on the altitude the plan picks
                sig_seen = max(sig_seen, sigma_eff(h_guess)) if use_vis else SIGMA_STD
                sig = sig_seen
                ts = sortie_endurance_s(power_p600, E_USE, req.speed_mps, wind_xy(h_guess) if use_wind else (0.0, 0.0))
                plan = plan_gsd_scan(req, sensor, w.height_dsm, w.ground_dtm, sigma_ext_per_m=sig, t_sortie_s=ts, homes=homes)
                if not plan.feasible and plan.why == "height" or abs(plan.h_agl_m - h_guess) < 1.0:
                    break
                h_guess = float(plan.h_agl_m)
            h_true = float(plan.h_agl_m) if math.isfinite(plan.h_agl_m) else h_guess
            ts_true = sortie_endurance_s(power_p600, E_USE, req.speed_mps, wind_xy(h_true))
            ev = evaluate_scan(plan, req, sensor, T, optical_depth=od, los=los, t_sortie_true_s=ts_true)
            s = plan.summary()
            rows.append({
                "world": task["world"], "preset": task["preset"], "mor_set": task.get("mor"), "seed": task["seed"],
                "level": lvl, "policy": pol, "aoi_side_m": float(aoi[1, 0] - aoi[0, 0]), "mor_50_m": mor_50, "wind_h_mps": float(np.hypot(*wind_xy(h_true))),
                "sigma_plan": sig, "sigma_true": sigma_eff(h_true), "t_sortie_plan_s": ts, "t_sortie_true_s": ts_true,
                **{f"plan_{k}": v for k, v in s.items()}, **ev["summary"],
                "silent_fail": bool(plan.feasible and ev["summary"]["capture_rate"] < req.p_req),
            })
    return rows
