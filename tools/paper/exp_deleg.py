"""Capability-based task delegation (contract net) vs distance-only delegation.

One task = (world, weather, seed): a fleet of 12 drones parked at random depots on open ground, half with a 4.8-48 mm
zoom camera and half with a fixed 4.8 mm wide lens, a state of charge drawn from 12-90 % (drones between sorties), and 30 observation requests, each a
standing person on open ground to be seen at a random perception level (detect, recognise, identify).

Every candidate quotes a request against the shared models: the flight out at a corridor-safe cruise altitude, a 60 s
observation hover and the flight home, integrated with the field wind (energy and feasibility), and the perception level
reached from the best of eight viewpoints around the target (pixels on target, line of sight, slant optical depth).
The policies differ only in what the award sees:

* `distance`      — nearest capable drone; it flies to the viewpoint on its approach line;
* `no_perception` — contract-net score with feasibility, time, energy and weather risk, but a perception quote that
                    ignores line of sight and the atmosphere (approach viewpoint, clear-air pixels);
* `coupled`       — the full contract-net score of the decision layer.

The award is then scored against the fully coupled ground truth: the request succeeds if the drone has the energy to
fly it and come home, the route stays within the wind rating, and the target reaches the required level at the
viewpoint the drone flies to.
"""

from __future__ import annotations

import math

import numpy as np

from common import PRESETS, WORLDS
from exp_m3perc import E_USE, power_p600
from harness import PaperSim

POLICIES = ("distance", "no_perception", "coupled")
N_DRONES, N_REQ = 12, 30
V_G, V_UP, T_OBS, STANDOFF, H_OBS = 5.0, 2.0, 60.0, 35.0, 45.0
RATING, RESERVE, EMERG = 13.8, 0.20, 0.05
LEVEL_IDX = {"D": 1, "R": 2, "I": 3}


def grid(worlds=WORLDS, presets=PRESETS, seeds=3) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    if isinstance(presets, str):
        presets = presets.split(",")
    return [{"world": w, "preset": p, "seed": s} for w in worlds for p in presets for s in range(int(seeds))]


def run(task: dict) -> list[dict]:
    sim = PaperSim(task["world"], preset=task["preset"], world_seed=int(task["seed"]))
    try:
        return _run(sim, task)
    finally:
        sim.close()


def _open_ground(w, rng, lo, hi, n) -> np.ndarray:
    out = []
    while sum(len(x) for x in out) < n:
        P = rng.uniform(lo, hi, (8 * n, 2))
        out.append(P[np.abs(w.height_dsm(P) - w.ground_dtm(P)) < 0.5])
    return np.concatenate(out)[:n]


def _run(sim: PaperSim, task: dict) -> list[dict]:
    from awr.agent.runtime.scoring import DEFAULT_WEIGHTS, EnvAtTarget, Limits, ScoreNorm, risk_of, score
    from awr.sim.perception.levels import EoSensor, Target, perception_level

    rng = np.random.default_rng([int(task["seed"]), sum(map(ord, task["world"])), 7])
    sim.env_set({"dir_from_deg": float(rng.uniform(0, 360))})
    w, env = sim.world, sim.env
    t_ns = int(sim.core.clock.t_ns)
    g = w.dsm_grid()
    a = np.asarray(g.a)
    lo = np.array([g.x0_m, g.y0_m]) + 150.0
    hi = np.array([g.x0_m, g.y0_m]) + np.array([a.shape[1], a.shape[0]]) * g.cell_m - 150.0
    span = hi - lo
    c0 = lo + 0.5 * span
    half = np.minimum(0.5 * span, 900.0)
    lo, hi = c0 - half, c0 + half

    homes = _open_ground(w, rng, lo, hi, N_DRONES)
    homes = np.c_[homes, w.ground_dtm(homes)]
    soc = rng.uniform(0.12, 0.9, N_DRONES)
    f_max = np.where(np.arange(N_DRONES) % 2 == 0, 48.0, 4.8)
    targets = _open_ground(w, rng, lo, hi, N_REQ)
    targets = np.c_[targets, w.ground_dtm(targets) + 0.9]
    levels = rng.choice(list(LEVEL_IDX), N_REQ)
    tgt = Target()
    norm = ScoreNorm(eta_s=600.0 / V_G, energy_wh=0.10 * E_USE)
    lim = Limits()

    def los(A, B):
        return np.concatenate([np.asarray(w.los_batch(A[i:i + 64], B[i:i + 64]), bool).reshape(-1)
                               for i in range(0, len(A), 64)])

    def tau(A, B):
        return np.exp(-np.concatenate([np.asarray(env.optical_depth(A[i:i + 256], B[i:i + 256], t_ns), np.float64)
                                       for i in range(0, len(A), 256)]))

    def wind(P):
        return np.concatenate([np.asarray(sim.wind_at(P[i:i + 256]), np.float64) for i in range(0, len(P), 256)])

    def leg_wh(A, B, z_c, see_wind: bool) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Energy (Wh), time (s) and max horizontal wind of climb A->z_c, cruise to B at z_c, descent to B (per pair)."""
        d = B[:, :2] - A[:, :2]
        L = np.linalg.norm(d, axis=1)
        u = d / np.maximum(L, 1e-6)[:, None]
        S = np.linspace(0.1, 0.9, 5)
        P = (A[:, None, :2] + S[None, :, None] * d[:, None, :]).reshape(-1, 2)
        Wv = wind(np.c_[P, np.repeat(z_c, len(S))])[:, :2].reshape(len(A), len(S), 2) if see_wind else \
            np.zeros((len(A), len(S), 2))
        v_air = np.linalg.norm(V_G * u[:, None, :] - Wv, axis=2)
        p_cr = power_p600(v_air, 0.0).mean(1)
        t_cr = L / V_G
        up = np.maximum(z_c - A[:, 2], 0.0)
        dn = np.maximum(z_c - B[:, 2], 0.0)
        p_up = power_p600(0.0, V_UP)
        p_h = power_p600(0.0, 0.0)
        e = (p_cr * t_cr + p_up * up / V_UP + p_h * dn / V_UP) / 3600.0
        return e, t_cr + (up + dn) / V_UP, np.linalg.norm(Wv, axis=2).max(1)

    rows = []
    ang = np.linspace(0.0, 2 * math.pi, 8, endpoint=False)
    for r in range(N_REQ):
        T = targets[r]
        lvl = str(levels[r])
        # viewpoints: 8 around the target, plus each drone's approach viewpoint
        vp8 = np.c_[T[0] + STANDOFF * np.cos(ang), T[1] + STANDOFF * np.sin(ang), np.zeros(8)]
        u_app = homes[:, :2] - T[:2]
        u_app /= np.maximum(np.linalg.norm(u_app, axis=1), 1e-6)[:, None]
        vpa = np.c_[T[:2] + STANDOFF * u_app, np.zeros(N_DRONES)]
        V = np.r_[vp8, vpa]
        V[:, 2] = np.maximum(T[2] - 0.9 + H_OBS, w.height_dsm(V[:, :2]) + 10.0)
        Tt = np.repeat(T[None], len(V), 0)
        k_los = los(V, Tt).astype(float)
        tv = tau(V, Tt)
        R = np.linalg.norm(V - Tt, axis=1)
        el = np.arcsin(np.clip((V[:, 2] - Tt[:, 2]) / R, -1, 1))
        # truth perception for every (drone lens, viewpoint)
        ok_true = np.zeros((N_DRONES, len(V)), bool)
        p_clear = np.zeros((N_DRONES, len(V)))
        p_true = np.zeros((N_DRONES, len(V)))
        for i in range(N_DRONES):
            pt = perception_level(tgt, EoSensor(f_max_mm=f_max[i]), R, el, f_max[i], tau=tv, k_los=k_los)
            pc = perception_level(tgt, EoSensor(f_max_mm=f_max[i]), R, el, f_max[i])
            ok_true[i] = pt["level"] >= LEVEL_IDX[lvl]
            p_true[i] = pt["p_" + lvl]
            p_clear[i] = pc["p_" + lvl]
        best_vp = np.argmax(p_true[:, :8], axis=1)
        env_t = sim.wind_at(T[None])[0]
        risk = risk_of(EnvAtTarget(wind_mps=float(np.hypot(env_t[0], env_t[1])),
                                   rain_mmh=0.0, mor_m=float(sim.mor_at(T[None])[0])), lim)
        quotes = {}
        for mode, vidx in (("best", best_vp), ("approach", 8 + np.arange(N_DRONES))):
            Vd = V[vidx]
            z_c = np.maximum.reduce([homes[:, 2] + 25.0, Vd[:, 2],
                                     np.asarray(w.heightmap_top_along(homes[:, :2], Vd[:, :2]), np.float64) + 5.0,
                                     np.asarray(w.heightmap_top_along(Vd[:, :2], homes[:, :2]), np.float64) + 5.0])
            e_out, t_out, w_out = leg_wh(homes, Vd, z_c, True)
            e_bk, t_bk, w_bk = leg_wh(Vd, homes, z_c, True)
            v_hover = np.linalg.norm(wind(Vd)[:, :2], axis=1)
            e_obs = power_p600(v_hover, 0.0) * T_OBS / 3600.0
            E = e_out + e_obs + e_bk
            quotes[mode] = dict(vidx=vidx, E=E, eta=t_out, wind=np.maximum.reduce([w_out, w_bk, v_hover]))
        dist = np.linalg.norm(homes[:, :2] - T[:2], axis=1)
        for pol in POLICIES:
            if pol == "distance":
                i = int(np.argmin(dist))
                q = quotes["approach"]
            else:
                q = quotes["best" if pol == "coupled" else "approach"]
                feas = (soc - q["E"] / E_USE >= RESERVE) & (q["wind"] <= RATING)
                conf = p_true[np.arange(N_DRONES), q["vidx"]] if pol == "coupled" else \
                    p_clear[np.arange(N_DRONES), q["vidx"]]
                U = np.array([score({"feasible": bool(feas[k]), "conf_expected": float(conf[k]),
                                     "eta_s": float(q["eta"][k]), "energy_wh": float(q["E"][k]), "load": 0.0},
                                    norm, DEFAULT_WEIGHTS, risk) for k in range(N_DRONES)])
                if not np.isfinite(U).any():
                    rows.append(_row(task, r, lvl, pol, None, None, None, None, None, None, declined=True))
                    continue
                i = int(np.argmax(np.where(np.isfinite(U), U, -1e18)))
            v = int(q["vidx"][i])
            stranded = bool(q["E"][i] > (soc[i] - EMERG) * E_USE)
            reserve = bool(q["E"][i] > (soc[i] - RESERVE) * E_USE)
            over = bool(q["wind"][i] > RATING)
            seen = bool(ok_true[i, v])
            rows.append(_row(task, r, lvl, pol, i, f_max[i], float(q["E"][i]), float(q["eta"][i]), float(dist[i]),
                             float(soc[i]), stranded=stranded, reserve_violation=reserve, over_rating=over, seen=seen,
                             los_ok=bool(k_los[v] > 0), tau=float(tv[v]), success=bool(seen and not stranded and not over)))
    return rows


def _row(task, r, lvl, pol, i, f, E, eta, dist, soc, **kw) -> dict:
    out = {"world": task["world"], "preset": task["preset"], "seed": task["seed"], "req": r, "level": lvl, "policy": pol,
           "drone": i, "f_max_mm": f, "wh": E, "eta_s": eta, "dist_m": dist, "soc": soc, "declined": False,
           "stranded": False, "reserve_violation": False, "over_rating": False, "seen": False, "los_ok": None,
           "tau": None, "success": False}
    out.update(kw)
    return out
