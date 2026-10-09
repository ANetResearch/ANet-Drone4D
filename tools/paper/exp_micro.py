"""Micro-benchmarks for the cost of coupling (M4 and the evaluation micro-benchmarks).

Kinds (one task each, run with `--procs 1` on an otherwise idle host for the timing figures):

* `geo`      — geometry query service: DSM lookups, corridor top (pyramid-sampled vs exact) and batched line of sight,
               per call and per item over batch sizes.
* `env`      — environment field: wind / optics queries batched vs one point per call, slant optical depth.
* `rtl`      — return refresh at fleet size N: refreshing every drone vs the rotating share the battery_rtl stage
               refreshes per call, in core-seconds per simulated second.
* `stages`   — whole pipeline at fleet size N with every drone flying: per-stage cost (core-ms per simulated second)
               and the real-time factor.
* `coverage` — GSD scan planner runtime over area-of-interest size.
"""

from __future__ import annotations

import math
import time

import numpy as np

from harness import PaperSim

REPEAT = 7


def grid(kinds=("geo", "env", "rtl", "stages", "coverage"), world="shenzhen", fleet=(10, 50, 100, 250, 500, 1000),
         sides=(200, 400, 800, 1200, 1600, 2000), reps=3) -> list[dict]:
    if isinstance(kinds, str):
        kinds = kinds.split(",")
    out: list[dict] = []
    for k in kinds:
        if k in ("geo", "env"):
            out += [{"kind": k, "world": world, "rep": r} for r in range(int(reps))]
        elif k in ("rtl", "stages"):
            out += [{"kind": k, "world": world, "n": int(n), "rep": r} for n in fleet for r in range(int(reps))]
        elif k == "coverage":
            out += [{"kind": k, "world": world, "side": float(s), "rep": r} for s in sides for r in range(int(reps))]
    return out


def _time(fn, repeat: int = REPEAT) -> float:
    """Median wall time of `fn()` in seconds (one warm-up call)."""
    fn()
    ts = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return float(np.median(ts))


def _bounds(w) -> tuple[np.ndarray, np.ndarray]:
    g = w.dsm_grid()
    a = np.asarray(g.a)
    lo = np.array([g.x0_m, g.y0_m]) + 50.0
    hi = np.array([g.x0_m, g.y0_m]) + np.array([a.shape[1], a.shape[0]]) * g.cell_m - 50.0
    return lo, hi


def run(task: dict) -> list[dict]:
    return {"geo": _geo, "env": _env, "rtl": _rtl, "stages": _stages, "coverage": _coverage}[task["kind"]](task)


def _row(task: dict, op: str, batch: int, sec: float, **kw) -> dict:
    return {"kind": task["kind"], "world": task["world"], "rep": task.get("rep"), "op": op, "batch": int(batch),
            "t_call_us": sec * 1e6, "t_item_us": sec * 1e6 / max(batch, 1), **kw}


def _geo(task: dict) -> list[dict]:
    sim = PaperSim(task["world"])
    try:
        w = sim.world
        lo, hi = _bounds(w)
        rng = np.random.default_rng(task["rep"])
        rows = []
        for b in (1, 10, 100, 1000, 10000, 100000):
            P = rng.uniform(lo, hi, (b, 2))
            rows.append(_row(task, "height_dsm", b, _time(lambda P=P: w.height_dsm(P))))
        for b in (1, 10, 100, 1000):
            A = rng.uniform(lo, hi, (b, 2))
            B = np.clip(A + rng.uniform(-800, 800, (b, 2)), lo, hi)
            rows.append(_row(task, "top_along_sampled", b, _time(lambda A=A, B=B: w.heightmap_top_along(A, B))))
            rows.append(_row(task, "top_along_exact", b, _time(lambda A=A, B=B: w.heightmap_top_along(A, B, exact=True), 3)))
        for b in (1, 8, 64):
            A = np.c_[rng.uniform(lo, hi, (b, 2)), np.full(b, 80.0)]
            B = np.c_[A[:, :2] + rng.uniform(-150, 150, (b, 2)), np.full(b, 1.0)]
            rows.append(_row(task, "los_batch", b, _time(lambda A=A, B=B: w.los_batch(A, B))))
        return rows
    finally:
        sim.close()


def _env(task: dict) -> list[dict]:
    sim = PaperSim(task["world"], preset="rain")
    try:
        env, w = sim.env, sim.world
        t = int(sim.core.clock.t_ns)
        lo, hi = _bounds(w)
        rng = np.random.default_rng(task["rep"])
        rows = []
        for b in (1, 10, 100, 1000, 10000):
            X = np.c_[rng.uniform(lo, hi, (b, 2)), rng.uniform(20, 150, b)]
            for name, f in (("wind", 3), ("optics", 8), ("all", 0xFF)):
                rows.append(_row(task, f"query_{name}_batched", b, _time(lambda X=X, f=f: env.query(X, t, fields=f))))
            if b <= 1000:
                def loop(X=X):
                    for i in range(len(X)):
                        env.query(X[i:i + 1], t, fields=3)
                rows.append(_row(task, "query_wind_per_point", b, _time(loop, 3)))
            Y = X.copy()
            Y[:, 2] = 1.0
            rows.append(_row(task, "optical_depth", b, _time(lambda X=X, Y=Y: env.optical_depth(X, Y, t))))
        return rows
    finally:
        sim.close()


def _fleet(sim: PaperSim, n: int, rng: np.random.Generator) -> list[str]:
    for sp in (6.0, 8.0, 10.0, 14.0):
        pads = sim.flat_slots(n, spacing=sp)
        if len(pads) >= n:
            break
    ids = [sim.add((float(p[0]), float(p[1]))) for p in pads[:n]]
    sim.ready(60.0)
    for k, u in enumerate(ids):
        sim.cmd("takeoff", {"alt_m": 30.0 + 12.0 * (k % 4)}, u)
    sim.until(lambda: sum(sim.fs(u)[0] == "FLYING" for u in ids) >= 0.95 * len(ids), 120.0, 1.0)
    w = sim.world
    for k, u in enumerate(ids):
        p = sim.pos(u)
        q = p[:2] + rng.uniform(-300, 300, 2)
        z = float(w.ground_dtm(q[None])[0]) + 30.0 + 12.0 * (k % 4)
        sim.cmd("goto", {"pos": [float(q[0]), float(q[1]), z], "route": "auto", "speed_mps": 8.0}, u)
    sim.advance(5.0)
    return ids


def _rtl(task: dict) -> list[dict]:
    import awr.sim.safety as M09

    sim = PaperSim(task["world"])
    try:
        rng = np.random.default_rng(task["rep"])
        ids = _fleet(sim, int(task["n"]), rng)
        bat = M09.SERVICE.rt.bat
        act = np.asarray([sim.slot(u) for u in ids], np.int64)
        n = act.size
        R = bat._rtl_params()
        k_rot = max(1, int(round(n * R.refresh_hz * bat.dt_rtl_s)))
        t_all = _time(lambda: bat.refresh(act))
        t_rot = _time(lambda: bat.refresh(act[:k_rot]))
        t_all_exact = _time(lambda: bat.refresh(act, exact=True), 3)
        calls_per_s = 1.0 / bat.dt_rtl_s
        return [{"kind": "rtl", "world": task["world"], "rep": task["rep"], "n": n, "k_rot": k_rot, "refresh_hz": R.refresh_hz,
                 "t_all_ms": t_all * 1e3, "t_rot_ms": t_rot * 1e3, "t_all_exact_ms": t_all_exact * 1e3,
                 "core_ms_per_s_rot": t_rot * 1e3 * calls_per_s,
                 "core_ms_per_s_all_5hz": t_all * 1e3 * calls_per_s,
                 "core_ms_per_s_all_tick": t_all * 1e3 * 250.0,
                 "n_flying": int(sum(sim.fs(u)[0] == "FLYING" for u in ids))}]
    finally:
        sim.close()


def _stages(task: dict) -> list[dict]:
    sim = PaperSim(task["world"], preset="rain")
    try:
        rng = np.random.default_rng(task["rep"])
        ids = _fleet(sim, int(task["n"]), rng)
        pipe = sim.core.fleet.pipeline
        pipe.take_stage_ns()
        # the core's 1 Hz perf report drains the same counters; accumulate everything drained during the window
        st: dict[str, int] = {}
        take = pipe.take_stage_ns

        def take_acc() -> dict[str, int]:
            out = take()
            for k, v in out.items():
                st[k] = st.get(k, 0) + v
            return out

        pipe.take_stage_ns = take_acc
        T = 20.0
        t0, w0 = sim.t, time.perf_counter()
        sim.advance(T)
        wall = time.perf_counter() - w0
        dt = sim.t - t0
        take_acc()
        pipe.take_stage_ns = take
        row = {"kind": "stages", "world": task["world"], "rep": task["rep"], "n": len(ids),
               "n_flying": int(sum(sim.fs(u)[0] in ("FLYING", "RTL") for u in ids)), "sim_s": dt, "wall_s": wall,
               "rtf": dt / wall if wall > 0 else math.nan, "stage_total_ms_per_s": sum(st.values()) * 1e-6 / dt}
        row.update({f"st_{k}": v * 1e-6 / dt for k, v in st.items()})
        return [row]
    finally:
        sim.close()


def _coverage(task: dict) -> list[dict]:
    from awr.sim.perception.levels import EoSensor
    from awr.swarm.coverage.gsd import ScanRequest, plan_gsd_scan, sortie_endurance_s

    from exp_m3perc import E_USE, power_p600

    sim = PaperSim(task["world"])
    try:
        w = sim.world
        lo, hi = _bounds(w)
        side = float(task["side"])
        rng = np.random.default_rng(task["rep"])
        c = rng.uniform(lo, np.maximum(lo + 1, hi - side))
        aoi = np.array([c, c + [side, 0], c + [side, side], c + [0, side]])
        homes = np.array([[c[0] - 50, c[1] - 50, float(w.ground_dtm(np.array([[c[0] - 50, c[1] - 50]]))[0])]])
        ts = sortie_endurance_s(power_p600, E_USE, 8.0, (5.0, 0.0))
        out = []
        for lvl in ("D", "R", "I"):
            req = ScanRequest(aoi=aoi, level=lvl)
            plan = None

            def go(req=req):
                nonlocal plan
                plan = plan_gsd_scan(req, EoSensor(), w.height_dsm, w.ground_dtm, sigma_ext_per_m=3.912 / 2000, t_sortie_s=ts,
                                     homes=homes)

            sec = _time(go, 3)
            out.append({"kind": "coverage", "world": task["world"], "rep": task["rep"], "side_m": side, "area_km2": side * side / 1e6,
                        "level": lvl, "t_plan_ms": sec * 1e3, "n_drones": plan.n_drones, "feasible": plan.feasible,
                        "h_agl_m": plan.h_agl_m, "length_m": plan.length_m, "n_cells_seq": len(plan.seq)})
        return out
    finally:
        sim.close()
