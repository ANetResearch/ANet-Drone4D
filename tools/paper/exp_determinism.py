"""Determinism: the same task run in separate processes must produce bit-identical fleet state.

One task = (world, preset, seed, rep). Sixteen drones take off in gusty weather and fly to random targets for
`t_s` simulated seconds; every second the SHA-256 of the fleet state (position, velocity, battery energy, flight
state) is recorded. Reps of the same (world, preset, seed) must agree hash for hash.
"""

from __future__ import annotations

import hashlib

import numpy as np

from harness import PaperSim


def grid(worlds=("shenzhen", "newyork", "synthcity"), presets=("clear", "thunderstorm", "fog"), seeds=2, reps=3,
         t_s=180.0, n=16) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    if isinstance(presets, str):
        presets = presets.split(",")
    return [{"world": w, "preset": p, "seed": s, "rep": r, "t_s": float(t_s), "n": int(n)}
            for w in worlds for p in presets for s in range(int(seeds)) for r in range(int(reps))]


def run(task: dict) -> list[dict]:
    sim = PaperSim(task["world"], preset=task["preset"], world_seed=int(task["seed"]))
    try:
        return _run(sim, task)
    finally:
        sim.close()


def _state_hash(sim: PaperSim, ids: list[str]) -> str:
    h = hashlib.sha256()
    for u in ids:
        s = sim.slot(u)
        h.update(np.ascontiguousarray(sim.S.enu.pos[s], np.float64).tobytes())
        h.update(np.ascontiguousarray(sim.S.enu.vel[s], np.float64).tobytes())
        h.update(np.float64(sim.energy_wh(u)).tobytes())
        h.update("/".join(sim.fs(u)).encode())
    return h.hexdigest()


def _run(sim: PaperSim, task: dict) -> list[dict]:
    rng = np.random.default_rng(97 * int(task["seed"]) + sum(map(ord, task["world"])))
    sim.env_set({"dir_from_deg": float(rng.uniform(0, 360))})
    pads = sim.flat_slots(int(task["n"]))
    ids = [sim.add((p[0], p[1]), soc=1.0) for p in pads]
    sim.ready()
    w = sim.world
    for u in ids:
        sim.cmd("takeoff", {"alt_m": 30.0}, u)
    sim.until(lambda: all(sim.fs(u)[0] not in ("DISARMED", "PREFLIGHT", "READY", "TAKING_OFF") for u in ids), 90.0, 0.5)
    depot = pads.mean(0)
    for k, u in enumerate(ids):
        a = rng.uniform(0, 2 * np.pi)
        q = depot + rng.uniform(150, 500) * np.array([np.cos(a), np.sin(a)])
        z = float(w.ground_dtm(q[None])[0]) + 60.0
        sim.cmd("goto", {"pos": [float(q[0]), float(q[1]), z], "route": "auto", "speed_mps": 3.0}, u, cid=f"g{k}")
    rows = []
    t0 = sim.t
    while sim.t - t0 < float(task["t_s"]):
        sim.advance(1.0)
        rows.append({"world": task["world"], "preset": task["preset"], "seed": task["seed"], "rep": task["rep"],
                     "t_s": round(sim.t, 3), "hash": _state_hash(sim, ids)})
    return rows
