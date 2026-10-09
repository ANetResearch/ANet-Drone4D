"""Micro-benchmark figures (one process pinned to one core on an otherwise idle server, median of 7 timed calls, 3 reps).

* m4_coupling_cost  — F4: CPU cost of keeping every drone's return plan current, naive vs the runtime's schedule.
* micro_queries     — geometry and environment queries: per-item latency vs batch size.
* micro_stages      — whole pipeline at fleet size N: per-stage cost and real-time factor.
* micro_coverage    — GSD scan planner runtime vs area of interest.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import style as S

STAGE_GROUPS = {
    "Dynamics and control": ["l1", "refgen", "attitude", "rate", "mixer", "motor", "integrate", "kinematic"],
    "Environment": ["env"],
    "Sensors": ["sensors"],
    "Safety and energy": ["guard", "fsm", "battery", "battery_rtl", "mission_guard", "fleet_guard", "contact", "collide",
                          "faults", "cmd_watch"],
    "Missions": ["mission", "mission_engine", "coverage", "director"],
    "State publication": ["tap", "clock", "ingest"],
}
STAGE_COL = [S.G1, S.RED, S.G3, S.G2, S.G4, S.G5]


def med(d, by, col):
    return d.groupby(by)[col].median()


def m4(d) -> None:
    r = d[d.kind == "rtl"]
    fig, ax = plt.subplots(figsize=(S.HALF_W + 0.6, 2.1))
    series = [("core_ms_per_s_all_tick", "Every drone, every tick", S.G1, "s", "--"),
              ("exact_tick", "Every drone, every tick, exact corridor", S.G3, "D", ":"),
              ("core_ms_per_s_all_5hz", "Every drone at 5 Hz", S.G2, "^", "-."),
              ("core_ms_per_s_rot", "Rotating fifth at 5 Hz (ours)", S.RED, "o", "-")]
    r = r.assign(exact_tick=r.t_all_exact_ms * 250.0)
    for col, lab, c, mk, ls in series:
        m = med(r, "n", col) / 1e3
        ax.plot(m.index, m.values, color=c, marker=mk, ls=ls, label=lab)
    ax.axhline(1.0, color=S.INK, lw=0.6)
    ax.text(12, 1.15, "one core in real time", fontsize=6)
    ax.axhline(0.40, color=S.BLUE, lw=0.6, ls=":")
    ax.text(150, 0.25, "whole-pipeline budget", fontsize=6, color=S.BLUE)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Drones")
    ax.set_ylabel("Return refresh (core-s per sim-s)")
    ax.legend(fontsize=5.6, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    S.save(fig, "m4_coupling_cost")


def queries(d) -> None:
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.1), gridspec_kw={"wspace": 0.32})
    g = d[d.kind == "geo"]
    for op, lab, c, mk, ls in [("height_dsm", "Surface height", S.G2, "o", "-"),
                               ("top_along_sampled", "Corridor top, pyramid", S.RED, "s", "-"),
                               ("top_along_exact", "Corridor top, exact", S.G3, "D", ":"),
                               ("los_batch", "Line of sight", S.G1, "^", "--")]:
        m = med(g[g.op == op], "batch", "t_item_us")
        a.plot(m.index, m.values, color=c, marker=mk, ls=ls, label=lab)
    a.set_xscale("log")
    a.set_yscale("log")
    a.set_xlabel("Batch size")
    a.set_ylabel(r"Time per item ($\mu$s)")
    a.legend(fontsize=5.8, loc="lower left")
    S.panel(a, "(a)", x=-0.25)
    e = d[d.kind == "env"]
    for op, lab, c, mk, ls in [("query_wind_per_point", "Wind, one point per call", S.G3, "D", ":"),
                               ("query_wind_batched", "Wind, batched", S.RED, "o", "-"),
                               ("query_all_batched", "All fields, batched", S.G2, "s", "-."),
                               ("optical_depth", "Slant optical depth", S.G1, "^", "--")]:
        m = med(e[e.op == op], "batch", "t_item_us")
        b.plot(m.index, m.values, color=c, marker=mk, ls=ls, label=lab)
    b.set_xscale("log")
    b.set_yscale("log")
    b.set_xlabel("Batch size")
    b.set_ylabel(r"Time per item ($\mu$s)")
    b.legend(fontsize=5.8, loc="lower left")
    S.panel(b, "(b)", x=-0.25)
    S.save(fig, "micro_queries")


def stages(d) -> None:
    s = d[d.kind == "stages"]
    cols = [c for c in s.columns if c.startswith("st_")]
    s = s.assign(wall_ms_per_s=1e3 * s.wall_s / s.sim_s)
    m = s.groupby("n")[cols + ["rtf", "stage_total_ms_per_s", "wall_ms_per_s"]].median()
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.1), gridspec_kw={"wspace": 0.35, "width_ratios": [1.5, 1]})
    x = np.arange(len(m))
    bottom = np.zeros(len(m))
    used = set()
    for (g, names), c in zip(STAGE_GROUPS.items(), STAGE_COL):
        cs = [f"st_{n}" for n in names if f"st_{n}" in m]
        used |= set(cs)
        v = m[cs].sum(1).values if cs else np.zeros(len(m))
        a.bar(x, v, 0.65, bottom=bottom, color=c, edgecolor=S.INK, linewidth=0.3, label=g)
        bottom += v
    rest = [c for c in cols if c not in used]
    if rest:
        v = m[rest].sum(1).values
        a.bar(x, v, 0.65, bottom=bottom, color="white", edgecolor=S.INK, linewidth=0.3, label="Other")
        bottom += v
    a.scatter(x, m.wall_ms_per_s.values, marker="_", s=120, color=S.INK, lw=1.2, zorder=4, label="Wall clock")
    a.axhline(400, color=S.INK, lw=0.7, ls=":")
    a.text(-0.4, 410, "budget 400", fontsize=5.8, va="bottom", color=S.G1)
    a.set_xticks(x, [str(n) for n in m.index])
    a.set_xlabel("Drones")
    a.set_ylabel("Pipeline cost (core-ms per sim-s)")
    a.set_ylim(0, 430)
    a.legend(fontsize=5.6, ncol=1, loc="upper left", bbox_to_anchor=(0.0, 0.9))
    a.grid(axis="x", visible=False)
    S.panel(a, "(a)", x=-0.2)
    b.plot(m.index, m.rtf.values, color=S.RED, marker="o")
    b.axhline(1.0, color=S.INK, lw=0.6)
    b.set_xscale("log")
    b.set_yscale("log")
    b.set_xlabel("Drones")
    b.set_ylabel("Real-time factor")
    S.panel(b, "(b)", x=-0.3)
    S.save(fig, "micro_stages")


def coverage(d) -> None:
    c = d[d.kind == "coverage"]
    fig, ax = plt.subplots(figsize=(S.HALF_W, 1.9))
    for lv, lab, col, mk, ls in [("D", "Detect", S.G3, "o", ":"), ("R", "Recognise", S.RED, "s", "-"),
                                 ("I", "Identify", S.G1, "^", "--")]:
        m = med(c[c.level == lv], "area_km2", "t_plan_ms")
        ax.plot(m.index, m.values / 1e3, color=col, marker=mk, ls=ls, label=lab)
    ax.set_xlabel(r"Area of interest (km$^2$)")
    ax.set_ylabel("Planning time (s)")
    ax.legend(fontsize=5.8)
    S.save(fig, "micro_coverage")


def main() -> None:
    S.setup()
    d = S.load("micro_full")
    m4(d)
    queries(d)
    stages(S.load("micro_stages2"))
    coverage(d)


if __name__ == "__main__":
    main()
