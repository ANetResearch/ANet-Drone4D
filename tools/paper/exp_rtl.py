"""Closed-loop urban sortie campaign: pre-flight precheck and contingency return (overall performance and ablations).

One task = (world, weather preset, seed, policy). Up to `n` P600 drones are tasked from a depot to on-station points at
`h_op` m AGL spread over the city (500–1600 m away), to hold station for a per-drone time drawn from `t_station` = [lo, hi] s and come back. The ground truth is
always fully coupled (4D wind field acting on the dynamics, DSM contact, battery); a policy only changes what the decisions
can see (`DecisionAwareness`):

1. **Pre-flight precheck** (launch gate). `coupled`: the shared energy model with the wind field on a geometry-aware
   transit (cruise above `H_top + 5` of the leg), plus the profile wind rating; `blind`: the same energy model with no
   wind on a straight leg at `h_op`; `none`: always launch (PX4 has no mission energy precheck).
2. **Transit** with the production planner (`goto route=auto`), identical for all policies.
3. **Return**: commanded at the end of the station time, or triggered earlier by the onboard battery logic, and executed
   by the return planner under the policy's visibility switches (fixed-altitude PX4 default, wind/geometry-blind, coupled).

Per drone we record the outcome — `declined` (precheck refused), `home` (landed within 25 m of home), `collision` (DSM or
ground impact), `stranded` (landed elsewhere) or `timeout` — plus energy, reserve at touchdown and return statistics.
"""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import replace
from typing import Any

import numpy as np

from awr.sim.core.awareness import DecisionAwareness
from awr.sim.core.envwind import env_mean_wind
from awr.sim.core.interfaces import EstimatePath
from awr.sim.fleet.stages import registry as R
from awr.world.geometry.path import zone_check
from common import PRESETS, WORLDS
from harness import PaperSim

# policy -> (awareness switches, precheck mode)
_C = {"shared_energy": True}
POLICIES: dict[str, tuple[dict[str, Any], str]] = {
    "coupled": (_C, "coupled"),
    "px4": ({"rtl_policy": "fixed_alt"}, "none"),
    "px4_time": ({"geometry": False, "detour": False, "wind": False}, "blind"),
    "no_geometry": (_C | {"geometry": False, "detour": False}, "coupled"),
    "no_detour": (_C | {"detour": False}, "coupled"),
    "no_wind": (_C | {"wind": False}, "blind_geo"),
    "no_shared": ({}, "coupled"),
    "no_energy": (_C | {"energy_rtl": False}, "coupled"),
    "no_precheck": (_C, "none"),
    "coupled_4d": (_C, "coupled4d"),
}

HOME_R_M = 25.0


def grid(worlds=WORLDS, presets=PRESETS, seeds=10, policies=("coupled", "px4", "px4_time"), n=8, soc0=1.0, h_op=40.0,
         r_min=300.0, r_max=1100.0, t_station=(30.0, 300.0), t_max=2400.0, speed=3.0, rtl_margin=None, top_margin=None,
         front=None, front_dur=600.0) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    if isinstance(presets, str):
        presets = presets.split(",")
    if isinstance(policies, str):
        policies = policies.split(",")
    def vals(v):
        if v is None:
            return [None]
        return [float(x) for x in (str(v).split(",") if isinstance(v, str) else np.atleast_1d(v))]

    fronts = [None] if front is None else (front.split(",") if isinstance(front, str) else list(front))
    out = []
    for w in worlds:
        for p, fr in itertools.product(presets, fronts):
            for s in range(int(seeds)):
                for pol in policies:
                    for rm in vals(rtl_margin):
                        for tm in vals(top_margin):
                            t = {"world": w, "preset": p, "seed": s, "policy": pol, "n": n, "soc0": soc0, "h_op": h_op,
                                 "r_min": r_min, "r_max": r_max, "t_station": t_station, "t_max": t_max, "speed": speed}
                            if rm is not None:
                                t["rtl_margin"] = rm
                            if tm is not None:
                                t["top_margin"] = tm
                            if fr is not None:
                                t.update(front=fr, front_dur=float(front_dur))
                            out.append(t)
    return out


def _targets(sim: PaperSim, depot: np.ndarray, n: int, rng: np.random.Generator, r_min: float, r_max: float) -> np.ndarray:
    w = sim.world
    b = w.bounds_m
    lo, hi = b[0, :2] + 60.0, b[1, :2] - 60.0
    out: list[np.ndarray] = []
    for _ in range(4000):
        if len(out) >= n:
            break
        ang = rng.uniform(0, 2 * math.pi)
        r = rng.uniform(r_min, r_max)
        p = depot + r * np.array([math.cos(ang), math.sin(ang)])
        if np.any(p < lo) or np.any(p > hi):
            continue
        d = float(w.height_dsm(p[None])[0] - w.ground_dtm(p[None])[0])
        if d > 1.0:  # on-station point above open ground (street, square, park)
            continue
        z = float(w.ground_dtm(p[None])[0]) + 40.0
        if zone_check(w, np.array([[*depot, z], [*p, z]]), None, edge_budget=None)[0]:
            continue
        out.append(p)
    return np.asarray(out)


def _precheck(sim: PaperSim, mode: str, pad: np.ndarray, tgt: np.ndarray, h_op: float, soc: float, t_station: float,
              speed: float) -> tuple[bool, dict]:
    """Launch gate; returns (launch, detail)."""
    if mode == "none":
        return True, {}
    from awr.sim.core.estimate import vehicle_profile

    w = sim.world
    em = R.energy_model()
    import awr.sim.safety as M09

    prof = vehicle_profile(M09.SERVICE.rt.profiles.get(sim.profile_id))
    zp = float(w.ground_dtm(pad[None])[0])
    zt = float(w.ground_dtm(tgt[None])[0])
    z_op = zt + h_op
    if mode in ("coupled", "blind_geo"):
        top = float(np.asarray(w.heightmap_top_along(pad[None], tgt[None], exact=True)).reshape(-1)[0])
        z_cr = max(z_op, zp + 25.0, top + M09.SERVICE.params.rtl.top_margin_m)
    else:
        z_cr = max(z_op, zp + 25.0)
    env = sim.env if mode == "coupled" else None
    out = EstimatePath(np.array([[*pad, zp], [*pad, z_cr], [*tgt, z_cr], [*tgt, z_op]]), np.full(3, speed), t_station)
    back = EstimatePath(np.array([[*tgt, z_op], [*tgt, z_cr], [*pad, z_cr], [*pad, zp]]), np.full(3, speed), 0.0)
    e1 = em.estimate(prof, soc, out, env)
    e2 = em.estimate(prof, soc, back, env)
    wh = float(e1.energy_wh + e2.energy_wh)
    e_use = 0.85 * 222.0
    reserve = M09.SERVICE.params.battery.feasible_reserve
    ok_e = soc - wh / e_use >= reserve
    wmax = 0.0
    if mode == "coupled":
        P = np.array([[*pad, z_cr], [*(0.5 * (pad + tgt)), z_cr], [*tgt, z_cr], [*tgt, z_op]])
        wmax = float(np.max(np.hypot(*sim.wind_at(P)[:, :2].T)))
    wr = (M09.SERVICE.rt.profiles.get(sim.profile_id).doc or {}).get("wind_rating_mps")
    wr = wr.get("value") if isinstance(wr, dict) else wr
    rating = float(wr) if isinstance(wr, (int, float)) else math.inf
    ok_w = wmax <= rating
    return bool(ok_e and ok_w), {"pre_wh": wh, "pre_z_cruise": z_cr, "pre_wind_max": wmax, "pre_ok_energy": bool(ok_e),
                                 "pre_ok_wind": bool(ok_w)}


class _EnvAt:
    """The shared environment field seen `dt_s` seconds ahead (the field's keyframe transition defines future weather)."""

    def __init__(self, env: Any, dt_s: float):
        self._env, self._s_t = env, int(env._s_t) + int(round(dt_s * 1e9))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._env, name)


def _precheck_4d(sim: PaperSim, pad: np.ndarray, tgt: np.ndarray, h_op: float, soc: float, t_station: float,
                 speed: float, t_min: float = 30.0) -> tuple[bool, dict]:
    """Space-time launch gate: wind and energy of each leg evaluated at the time it will be flown. If the requested
    station time would put the return into wind beyond the rating or below the reserve, the station time is shortened
    (down to `t_min`); otherwise the sortie is declined."""
    from awr.sim.core.estimate import vehicle_profile

    import awr.sim.safety as M09

    w, em, env = sim.world, R.energy_model(), sim.env
    prof = vehicle_profile(M09.SERVICE.rt.profiles.get(sim.profile_id))
    zp = float(w.ground_dtm(pad[None])[0])
    z_op = float(w.ground_dtm(tgt[None])[0]) + h_op
    top = float(np.asarray(w.heightmap_top_along(pad[None], tgt[None], exact=True)).reshape(-1)[0])
    z_cr = max(z_op, zp + 25.0, top + M09.SERVICE.params.rtl.top_margin_m)
    wr = (M09.SERVICE.rt.profiles.get(sim.profile_id).doc or {}).get("wind_rating_mps")
    wr = wr.get("value") if isinstance(wr, dict) else wr
    rating = float(wr) if isinstance(wr, (int, float)) else math.inf
    reserve = M09.SERVICE.params.battery.feasible_reserve
    out = EstimatePath(np.array([[*pad, zp], [*pad, z_cr], [*tgt, z_cr], [*tgt, z_op]]), np.full(3, speed), 0.0)
    back = EstimatePath(np.array([[*tgt, z_op], [*tgt, z_cr], [*pad, z_cr], [*pad, zp]]), np.full(3, speed), 0.0)
    P_out = np.array([[*pad, z_cr], [*(0.5 * (pad + tgt)), z_cr], [*tgt, z_cr]])
    P_back = np.array([[*tgt, z_cr], [*(0.5 * (pad + tgt)), z_cr], [*pad, z_cr]])
    e1 = em.estimate(prof, soc, out, env)

    def wind_max(P: np.ndarray, t0: float, t1: float) -> float:
        ts = np.linspace(t0, t1, 4)
        return max(float(np.max(np.hypot(*np.asarray(env_mean_wind(env, P, int(env._s_t + t * 1e9)))[:, :2].T)))
                   for t in ts)

    w_out = wind_max(P_out, 0.0, e1.eta_s)
    detail = {"pre_wh": float("nan"), "pre_z_cruise": z_cr, "pre_wind_max": w_out, "pre_ok_energy": False,
              "pre_ok_wind": w_out <= rating, "station_s": 0.0}
    if w_out > rating:
        return False, detail
    for ts in np.unique(np.r_[np.arange(t_station, t_min, -15.0), t_min])[::-1]:
        t_back = e1.eta_s + float(ts)
        e1d = em.estimate(prof, soc, EstimatePath(out.points_enu_m, out.speeds_mps, float(ts)), _EnvAt(env, e1.eta_s))
        e2 = em.estimate(prof, soc, back, _EnvAt(env, t_back))
        wh = float(e1d.energy_wh + e2.energy_wh)
        w_back = wind_max(P_back, t_back, t_back + e2.eta_s)
        ok_e, ok_w = soc - wh / (0.85 * 222.0) >= reserve, w_back <= rating
        if ok_e and ok_w:
            detail.update(pre_wh=wh, pre_wind_max=max(w_out, w_back), pre_ok_energy=True, pre_ok_wind=True,
                          station_s=float(ts))
            return True, detail
        detail.update(pre_wh=wh, pre_wind_max=max(w_out, w_back), pre_ok_energy=bool(ok_e), pre_ok_wind=bool(ok_w))
    return False, detail


def _override_params(task: dict) -> None:
    """Sensitivity knobs: `rtl_margin` (trigger factor on t_rtl) and `top_margin` (m above the corridor height)."""
    if "rtl_margin" not in task and "top_margin" not in task:
        return
    import awr.sim.safety as M09

    svc = M09.SERVICE
    P = svc.params
    P = replace(P, battery=replace(P.battery, rtl_margin=float(task.get("rtl_margin", P.battery.rtl_margin))),
                rtl=replace(P.rtl, top_margin_m=float(task.get("top_margin", P.rtl.top_margin_m))))
    svc.params = P
    svc.rt.params = P


def run(task: dict) -> list[dict]:
    sw, _mode = POLICIES[task["policy"]]
    aw = DecisionAwareness(**{"precheck_env": True, **sw})
    t_wall0 = time.perf_counter()
    sim = PaperSim(task["world"], awareness=aw, preset=task["preset"], world_seed=int(task["seed"]))
    try:
        return _run(sim, task, t_wall0)
    finally:
        sim.close()


def _run(sim: PaperSim, task: dict, t_wall0: float) -> list[dict]:
    mode = POLICIES[task["policy"]][1]
    rng = np.random.default_rng(1000 * int(task["seed"]) + sum(map(ord, task["world"])))
    wind_dir = float(rng.uniform(0, 360))
    sim.env_set({"dir_from_deg": wind_dir})
    w = sim.world
    pads = sim.flat_slots(int(task["n"]))
    depot = pads.mean(0)
    h_op, speed = float(task["h_op"]), float(task["speed"])
    ts_lo, ts_hi = (task["t_station"], task["t_station"]) if np.isscalar(task["t_station"]) else task["t_station"]
    z_probe = float(w.ground_dtm(depot[None])[0]) + h_op
    wind_hop = float(np.hypot(*sim.wind_at([depot[0], depot[1], z_probe])[0, :2]))
    mor_hop = float(sim.mor_at([depot[0], depot[1], z_probe])[0])
    tg = _targets(sim, depot, len(pads), rng, float(task["r_min"]), float(task["r_max"]))
    n = len(tg)
    t_st = rng.uniform(float(ts_lo), float(ts_hi), n)
    ids = [sim.add((pads[k, 0], pads[k, 1]), soc=float(task["soc0"])) for k in range(n)]
    sim.ready()
    _override_params(task)
    if task.get("front"):
        # weather front: from now the weather moves to preset `front` over `front_dur` s; the field's keyframe holds the
        # whole transition, so the future weather is known to anything that queries the field at a future time
        r_f = sim.cmd("env/preset", {"name": task["front"], "duration_s": float(task["front_dur"])}, uav=None)
        if r_f.get("status") not in ("accepted", "succeeded"):
            raise RuntimeError(f"env/preset {task['front']}: {r_f}")
        sim.advance(0.5)
    pre: dict[str, tuple[bool, dict]] = {}
    t_req = t_st.copy()
    for k, u in enumerate(ids):
        if mode == "coupled4d":
            pre[u] = _precheck_4d(sim, pads[k], tg[k], h_op, float(task["soc0"]), float(t_st[k]), speed)
            if pre[u][0]:
                t_st[k] = pre[u][1]["station_s"]
        else:
            pre[u] = _precheck(sim, mode, pads[k], tg[k], h_op, float(task["soc0"]), float(t_st[k]), speed)
    fly = [u for u in ids if pre[u][0]]
    for u in fly:
        sim.cmd("takeoff", {"alt_m": 25.0}, u)
    sim.until(lambda: all(sim.fs(u)[0] not in ("DISARMED", "PREFLIGHT", "READY", "TAKING_OFF") for u in fly), 90.0, 0.5)
    goto_ok, tgt3 = {}, {}
    for k, u in enumerate(ids):
        z = float(w.ground_dtm(tg[k][None])[0]) + h_op
        tgt3[u] = np.array([tg[k][0], tg[k][1], z])
        if u in fly:
            r = sim.cmd("goto", {"pos": [float(tg[k][0]), float(tg[k][1]), z], "route": "auto", "speed_mps": speed}, u,
                        cid=f"go-{u}")
            goto_ok[u] = r.get("status") in ("accepted", "succeeded")
    home = {u: sim.S.enu.home[sim.slot(u)].copy() for u in ids}
    rec: dict[str, dict] = {u: {"t_rtl": None, "soc_rtl": None, "wh_rtl0": None, "pos_rtl": None, "path": 0.0, "z_max": -1e9,
                                "last": None, "outcome": None if u in fly else "declined", "t_end": None, "arrived": False,
                                "rtl_reason": None, "t_arrive": None, "rtl_cmd": False, "t_resume": sim.t}
                            for u in ids}
    t_end = float(task["t_max"])
    coll_seen: set[str] = set()
    n_ev = 0
    while sim.t < t_end and fly:
        sim.advance(0.5)
        for e in sim.events[n_ev:]:
            if e[1] == "sim.contact.collision" and e[2] in rec and e[2] not in coll_seen:
                coll_seen.add(e[2])
                r = rec[e[2]]
                if r["outcome"] is None:
                    r["outcome"], r["t_end"] = "collision", e[0]
                    r["collision_kind"] = e[3].get("kind")
                    r["collision_phase"] = "return" if r["t_rtl"] is not None else ("station" if r["arrived"] else "transit")
        n_ev = len(sim.events)
        air = [u for u in fly if rec[u]["outcome"] is None and sim.fs(u)[0] not in ("LANDED", "DISARMED")]
        if air:
            wx = np.hypot(*sim.wind_at(np.array([sim.pos(u) for u in air]))[:, :2].T)
            for u, v in zip(air, wx):
                rec[u]["wind_exp"] = max(rec[u].get("wind_exp", 0.0), float(v))
        alive = 0
        for u in fly:
            r = rec[u]
            if r["outcome"] is not None:
                continue
            alive += 1
            fs = sim.fs(u)[0]
            p = sim.pos(u)
            if r["t_rtl"] is None and not r["arrived"] and float(np.linalg.norm(p - tgt3[u])) < 15.0:
                r["arrived"], r["t_arrive"] = True, sim.t
            # a separation hold (SAF.SEP.AVOIDING) cancels the active command and leaves the drone hovering: resume it,
            # identically for every policy
            if fs == "FLYING" and sim.fs(u)[1] == "HOVER" and sim.t - r.get("t_resume", 0.0) > 3.0:
                if r["t_rtl"] is None and not r["arrived"]:
                    q = tgt3[u]
                    n_r = r.get("n_resume", 0) + 1
                    res = sim.cmd("goto", {"pos": [float(q[0]), float(q[1]), float(q[2])], "route": "auto", "speed_mps": speed}, u,
                            cid=f"go-{u}-r{n_r}")
                    r["t_resume"], r["n_resume"] = sim.t, n_r
                    if res.get("status") == "rejected":
                        sim.cmd("rtl", {}, u, cid=f"rtl-{u}-x")
                        r["rtl_cmd"] = True
                elif r["t_rtl"] is not None or r["rtl_cmd"]:
                    sim.cmd("rtl", {}, u, cid=f"rtl-{u}-r{r.get('n_resume', 0) + 1}")
                    r["t_resume"], r["n_resume"] = sim.t, r.get("n_resume", 0) + 1
            if r["arrived"] and r["t_rtl"] is None and not r["rtl_cmd"] and sim.t - r["t_arrive"] >= t_st[ids.index(u)] \
                    and fs == "FLYING":
                sim.cmd("rtl", {}, u, cid=f"rtl-{u}")
                r["rtl_cmd"] = True
            if fs in ("RTL", "LANDING", "ELAND") and r["t_rtl"] is None:
                r["t_rtl"], r["soc_rtl"], r["wh_rtl0"], r["pos_rtl"] = sim.t, sim.soc(u), sim.energy_wh(u), p.tolist()
                r["rtl_reason"] = next((e[3].get("reason") for e in sim.events if e[1] == "uav.state" and e[2] == u
                                        and str(e[3].get("to", "")).split("/")[0] in ("RTL", "LANDING", "ELAND")
                                        and str(e[3].get("from", "")).split("/")[0] not in ("RTL", "LANDING", "ELAND")), None)
                r["last"] = p
            if r["t_rtl"] is not None:
                if r["last"] is not None:
                    r["path"] += float(np.linalg.norm(p - r["last"]))
                r["last"] = p
                r["z_max"] = max(r["z_max"], float(p[2]))
            if fs in ("LANDED", "DISARMED") and (r["t_rtl"] is not None or sim.t > 120.0):
                d = float(np.hypot(*(p[:2] - home[u][:2])))
                r["outcome"] = "home" if d <= HOME_R_M and r["arrived"] else ("aborted" if d <= HOME_R_M else "stranded")
                r["t_end"], r["d_home_end"] = sim.t, d
        if alive == 0:
            break
    rows = []
    for k, u in enumerate(ids):
        r = rec[u]
        p = sim.pos(u)
        out = r["outcome"] or "timeout"
        prt = np.asarray(r["pos_rtl"]) if r["pos_rtl"] is not None else None
        hz = float("nan")
        if prt is not None:
            hz = float(np.asarray(w.heightmap_top_along(prt[None, :2], home[u][None, :2], exact=True)).reshape(-1)[0])
        rows.append({
            "world": task["world"], "preset": task["preset"], "seed": task["seed"], "policy": task["policy"], "uav": u, "k": k,
            "rtl_margin": task.get("rtl_margin"), "top_margin": task.get("top_margin"), "front": task.get("front"),
            "precheck": mode, "t_station_s": float(t_st[k]), "t_station_req_s": float(t_req[k]), "launched": u in fly, **pre[u][1],
            "wind_dir_from_deg": wind_dir, "wind_hop_mps": wind_hop, "mor_hop_m": mor_hop,
            "goto_ok": goto_ok.get(u), "n_resume": r.get("n_resume", 0), "goto_result": (sim.results.get(f"go-{u}") or (None,))[0],
            "arrived": r["arrived"], "t_arrive_s": r["t_arrive"], "outcome": out, "rtl_cmd": r["rtl_cmd"],
            "d_target_home_m": float(np.hypot(*(tgt3[u][:2] - home[u][:2]))),
            "rtl_reason": r["rtl_reason"], "t_rtl_s": r["t_rtl"], "soc_rtl": r["soc_rtl"], "t_end_s": r["t_end"],
            "soc_end": sim.soc(u), "wh_used": sim.energy_wh(u) - (1.0 - float(task["soc0"])) * 0.85 * 222.0,
            "wh_return": (sim.energy_wh(u) - r["wh_rtl0"]) if r["wh_rtl0"] is not None else None,
            "d_home_rtl_m": float(np.hypot(*(prt[:2] - home[u][:2]))) if prt is not None else None,
            "z_rtl_start_m": float(prt[2]) if prt is not None else None, "z_home_m": float(home[u][2]),
            "h_top_path_m": hz, "path_rtl_m": r["path"], "z_max_rtl_m": r["z_max"] if r["t_rtl"] is not None else None,
            "d_home_end_m": float(np.hypot(*(p[:2] - home[u][:2]))), "collision_kind": r.get("collision_kind"),
            "collision_phase": r.get("collision_phase"), "wind_exp_max": r.get("wind_exp"), "t_sim_s": sim.t, "wall_s": time.perf_counter() - t_wall0,
        })
    return rows
