"""M2 energy mismatch (closed loop): wind-blind vs wind-aware energy estimates against energy drawn in flight.

One task = (world, preset, wind speed override or None, seed). Eight P600 drones take off from flat pads, climb to a
cruise altitude above everything on their leg, fly a straight 1 km leg on headings 0°, 45°, …, 315° and fly back. For
each leg we record the flown trajectory (t, position, velocity at 2 Hz) and the battery energy actually drawn (the
simulated ground truth: full wind field with gusts and turbulence acting on the dynamics). The same trajectory is
re-integrated by the shared energy model twice: without wind (what a weather-blind planner sees) and with the 4D wind
field's mean wind (what the coupled precheck sees).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from awr.sim.core.awareness import DecisionAwareness
from awr.sim.fleet.stages import registry as R
from common import PRESETS
from harness import PaperSim

LEG_M = 800.0
H_CRUISE = 60.0
SPEED = 8.0


def grid(worlds=("shenzhen", "newyork", "chicago", "synthcity"), presets=PRESETS, seeds=2, speeds=None,
         speed_world="synthcity", speed_seeds=3, shared=(False,)) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    if isinstance(presets, str):
        presets = presets.split(",")
    out = [{"world": w, "preset": p, "wind_ref": None, "seed": s} for w in worlds for p in presets for s in range(int(seeds))]
    sp = speeds if speeds is not None else (0, 2, 4, 6, 8, 10, 12, 14, 16)
    if isinstance(shared, str):
        shared = tuple(x.strip().lower() in ("1", "true") for x in shared.split(","))
    out += [{"world": speed_world, "preset": "clear", "wind_ref": float(v), "seed": s, **({"shared": True} if sh else {})}
            for v in sp for s in range(int(speed_seeds)) for sh in shared]
    return out


def run(task: dict) -> list[dict]:
    aw = DecisionAwareness(precheck_env=True, shared_energy=bool(task.get("shared", False)))
    sim = PaperSim(task["world"], awareness=aw, preset=task["preset"],
                   world_seed=int(task["seed"]))
    try:
        return _run(sim, task)
    finally:
        sim.close()


def _run(sim: PaperSim, task: dict) -> list[dict]:
    rng = np.random.default_rng(7919 * int(task["seed"]) + sum(map(ord, task["world"] + task["preset"])))
    wind_dir = float(rng.uniform(0, 360))
    wind: dict[str, Any] = {"dir_from_deg": wind_dir}
    if task.get("wind_ref") is not None:
        wind["speed_ref_mps"] = float(task["wind_ref"])
    sim.env_set(wind)
    w = sim.world
    pads = sim.flat_slots(8, spacing=12.0)
    n = len(pads)
    heads = np.arange(n) * (2 * math.pi / 8)
    ids = [sim.add((pads[k, 0], pads[k, 1]), soc=1.0) for k in range(n)]
    sim.ready()
    lo, hi = w.bounds_m[0, :2] + 40, w.bounds_m[1, :2] - 40
    legs = {}
    for k, u in enumerate(ids):
        a = pads[k]
        b = np.clip(a + LEG_M * np.array([math.cos(heads[k]), math.sin(heads[k])]), lo, hi)
        zg = float(w.ground_dtm(a[None])[0])
        legs[u] = (a, b, zg + H_CRUISE)
        sim.cmd("takeoff", {"alt_m": 25.0}, u)
    sim.until(lambda: all(sim.fs(u) == ("FLYING", "HOVER") for u in ids), 120.0, 0.5)
    em = R.energy_model()
    prof = sim.profile_id
    rows = []
    for phase in ("out", "back"):
        rec = {u: [] for u in ids}
        wh0 = {u: sim.energy_wh(u) for u in ids}
        acc = {}
        for u in ids:
            a, b, z = legs[u]
            tgt = b if phase == "out" else a
            zt = float(w.ground_dtm(tgt[None])[0]) + H_CRUISE
            r = sim.cmd("goto", {"pos": [float(tgt[0]), float(tgt[1]), zt], "route": "auto", "speed_mps": SPEED}, u,
                        cid=f"{phase}-{u}")
            acc[u] = r.get("status") in ("accepted", "succeeded")
        done = {u: not acc[u] for u in ids}
        wh1: dict[str, float] = {}
        t_end = sim.t + 900.0
        while sim.t < t_end and not all(done.values()):
            sim.advance(0.5)
            for u in ids:
                if done[u]:
                    continue
                s = sim.slot(u)
                p = sim.S.enu.pos[s].copy()
                v = sim.S.enu.vel[s].copy()
                rec[u].append([sim.t, *p, *v])
                res = sim.results.get(f"{phase}-{u}")
                if res is not None or sim.fs(u)[0] not in ("FLYING",):
                    done[u] = True
                    wh1[u] = sim.energy_wh(u)
        for k, u in enumerate(ids):
            X = np.asarray(rec[u])
            if not acc[u] or len(X) < 4:
                continue
            true_wh = wh1.get(u, sim.energy_wh(u)) - wh0[u]
            est0 = float(em.path_wh(prof, X, None))
            est1 = float(em.path_wh(prof, X, sim.env))
            a, b, z = legs[u]
            hd = math.atan2(*(((b - a) if phase == "out" else (a - b))[::-1]))
            wm = np.asarray(sim.env.query(np.array([[*(0.5 * (a + b)), z]]), int(sim.core.clock.t_ns), fields=3).wind_mean_mps)[0]
            along = float(wm[0] * math.cos(hd) + wm[1] * math.sin(hd))
            rows.append({"world": task["world"], "preset": task["preset"], "wind_ref": task.get("wind_ref"), "seed": task["seed"],
                         "shared": bool(task.get("shared", False)), "uav": u, "phase": phase, "heading_deg": math.degrees(hd) % 360, "wind_dir_from_deg": wind_dir,
                         "wind_speed_mps": float(np.hypot(wm[0], wm[1])), "tailwind_mps": along, "z_m": z,
                         "dur_s": float(X[-1, 0] - X[0, 0]), "dist_m": float(np.linalg.norm(b - a)),
                         "path_m": float(np.linalg.norm(np.diff(X[:, 1:4], axis=0), axis=1).sum()),
                         "result": (sim.results.get(f"{phase}-{u}") or (None,))[0], "fs_end": sim.fs(u)[0],
                         "rtl_reason": next((e[3].get("reason") for e in reversed(sim.events) if e[1] == "uav.state"
                                             and e[2] == u and str(e[3].get("to", "")).startswith(("RTL", "HOLD", "ELAND"))),
                                            None),
                         "v_air_max_mps": float(np.max(np.hypot(X[:, 4] - wm[0], X[:, 5] - wm[1]))),
                         "true_wh": true_wh, "est_blind_wh": est0, "est_coupled_wh": est1})
    return rows
