"""M1 geometry probe: how often does a fixed-altitude return line cross a building, and what does a fixed high altitude cost?

For each world we sample `n` (position, home) pairs: home on open ground (DSM within 1 m of DTM), position 200–1500 m away
at `h_agl` above its local ground and outside any building column. For the straight return line we compare

* PX4 default: z = max(z_now, z_home + 30) (RTL_RETURN_ALT = 30 m, multicopter default);
* fixed high: z = z_home + 60 / 120 (operators raising RTL_RETURN_ALT to be safe);
* geometry-aware: z = max(z_now, z_home + 30, H_top + 5) with H_top the DSM maximum along the line.

A row is one sample; `top` is H_top, `hit_*` flags whether the line at that altitude clears the DSM by less than 0 m.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from common import WORLDS

WORLDS_DIR = Path(__file__).resolve().parents[2] / "worlds"


def grid(worlds=WORLDS, h_agl=(30, 40, 60, 80, 100), n=2000, chunks=5) -> list[dict]:
    if isinstance(worlds, str):
        worlds = worlds.split(",")
    return [{"world": w, "h_agl": float(h), "n": int(n), "chunk": c} for w in worlds for h in h_agl for c in range(int(chunks))]


def _wq(world: str):
    from awr.world.geometry.query import open_world_query

    return open_world_query(WORLDS_DIR / world, WORLDS_DIR / ".geo-cache")


def run(task: dict) -> list[dict]:
    wq = _wq(task["world"])
    rng = np.random.default_rng([sum(map(ord, task["world"])), int(task["h_agl"]), int(task["chunk"])])
    g = wq.dsm_grid()
    a = np.asarray(g.a)
    lo = np.array([g.x0_m, g.y0_m]) + 40.0
    hi = lo + np.array([a.shape[1], a.shape[0]]) * g.cell_m - 80.0
    h = float(task["h_agl"])
    rows: list[dict] = []
    tries = 0
    while len(rows) < task["n"] and tries < 200:
        tries += 1
        m = 4096
        H = rng.uniform(lo, hi, (m, 2))
        open_h = np.abs(wq.height_dsm(H) - wq.ground_dtm(H)) < 1.0
        H = H[open_h]
        ang = rng.uniform(0, 2 * np.pi, len(H))
        r = rng.uniform(200.0, 1500.0, len(H))
        P = H + r[:, None] * np.c_[np.cos(ang), np.sin(ang)]
        ok = np.all((P > lo) & (P < hi), axis=1)
        H, P, r = H[ok], P[ok], r[ok]
        zg = wq.ground_dtm(P)
        z_now = zg + h
        ok = wq.height_dsm(P) < z_now - 2.0
        H, P, r, z_now = H[ok], P[ok], r[ok], z_now[ok]
        if not len(H):
            continue
        k = min(len(H), task["n"] - len(rows))
        H, P, r, z_now = H[:k], P[:k], r[:k], z_now[:k]
        z_home = wq.ground_dtm(H)
        top = np.asarray(wq.heightmap_top_along(P, H, exact=True), np.float64)
        z_px4 = np.maximum(z_now, z_home + 30.0)
        z_aw = np.maximum(z_px4, top + 5.0)
        for i in range(k):
            rows.append({
                "world": task["world"], "h_agl": h, "dist_m": float(r[i]), "z_now": float(z_now[i]), "z_home": float(z_home[i]),
                "top": float(top[i]), "z_px4": float(z_px4[i]), "z_aware": float(z_aw[i]),
                "hit_px4": bool(top[i] > z_px4[i]), "hit_60": bool(top[i] > z_home[i] + 60.0),
                "hit_120": bool(top[i] > z_home[i] + 120.0),
                "climb_aware_m": float(z_aw[i] - z_now[i]), "climb_px4_m": float(z_px4[i] - z_now[i]),
                "climb_120_m": float(max(z_home[i] + 120.0, z_now[i]) - z_now[i]),
            })
    return rows
