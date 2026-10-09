"""Fig. sensitivity: the coupled policy under its own decision parameters and under visibility.
(a) clearance margin above the corridor top; (b) return-trigger margin on the energy-equivalent return time;
(c) the coupled scan planner as the meteorological optical range falls: capture and the drones it asks for."""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import numpy as np

import style as S

os.environ.setdefault("RTL_RUN", "rtl_sens")
import rtl_common as C  # noqa: E402

LEVEL = {"D": ("Detect", "o", S.G3, ":"), "R": ("Recognise", "s", S.RED, "-"), "I": ("Identify", "^", S.G1, "--")}


def _line(ax, x, d, col, **kw):
    m = np.array([d[d.key == v][col].astype(float).mean() for v in x]) * 100
    ax.plot(x, m, **kw)


def main() -> None:
    S.setup()
    d = C.load()
    d = d[d.preset.isin(["clear", "rain"])]
    fig, axs = plt.subplots(1, 3, figsize=(S.FULL_W, 2.0), gridspec_kw={"wspace": 0.62})
    a, b, c = axs
    x = d[d.rtl_margin == 1.3].assign(key=lambda z: z.top_margin)
    tm = sorted(x.key.unique())
    _line(a, tm, x, "completed", color=S.G1, marker="o", ls="-", label="Completed")
    _line(a, tm, x, "declined", color=S.RED, marker="s", ls="--", label="Not launched")
    _line(a, tm, x, "aborted", color=S.G3, marker="^", ls=":", label="Returned early")
    a.set_xlabel("Clearance margin (m)")
    a.set_ylabel("Sorties (%)")
    a.set_ylim(0, 70)
    a.axvline(5.0, color=S.INK, lw=0.6, ls=":")
    a.legend(loc="center left", fontsize=5.8, bbox_to_anchor=(0, 0.66))
    S.panel(a, "(a)", x=-0.3, y=1.04)

    y = d[d.top_margin == 5.0].assign(key=lambda z: z.rtl_margin)
    rm = sorted(y.key.unique())
    _line(b, rm, y, "completed", color=S.G1, marker="o", ls="-", label="Completed")
    _line(b, rm, y, "declined", color=S.RED, marker="s", ls="--", label="Not launched")
    _line(b, rm, y, "aborted", color=S.G3, marker="^", ls=":", label="Returned early")
    b.axvline(1.3, color=S.INK, lw=0.6, ls=":")
    b.set_xlabel(r"Trigger margin ($t_{\mathrm{rem}}/t_{\mathrm{rtl}}$)")
    b.set_ylabel("Sorties (%)")
    b.set_ylim(0, 70)
    b.legend(loc="center left", fontsize=5.8, bbox_to_anchor=(0, 0.62))
    S.panel(b, "(b)", x=-0.3, y=1.04)

    m = S.load("m3perc_full")
    m = m[m.mor_set.notna() & (m.policy == "aware")]
    for lv, (lab, mk, col, ls) in LEVEL.items():
        z = m[m.level == lv].groupby("mor_set")
        c.plot(z.capture_rate.mean().index, z.capture_rate.mean().values * 100, marker=mk, color=col, ls=ls, lw=1.1,
               label=lab)
    c.set_xscale("log")
    c.set_xlabel("MOR (m)")
    c.set_ylabel("Targets captured (%)")
    c.set_ylim(0, 100)
    c3 = c.twinx()
    z = m[m.level == "R"].groupby("mor_set").plan_n_drones.mean()
    c3.bar(z.index, z.values, width=z.index * 0.25, color=S.G5, edgecolor=S.G3, linewidth=0.4, zorder=0)
    c3.set_ylabel("Drones, recognise", color=S.G2)
    c3.tick_params(axis="y", colors=S.G2)
    c3.spines["right"].set_visible(True)
    c3.grid(False)
    c3.set_ylim(0, max(6.0, float(z.max()) * 1.6))
    c.set_zorder(c3.get_zorder() + 1)
    c.patch.set_visible(False)
    c.legend(loc="center right", fontsize=5.8, bbox_to_anchor=(1.0, 0.58))
    S.panel(c, "(c)", x=-0.3, y=1.04)
    S.save(fig, "sensitivity")


if __name__ == "__main__":
    main()
