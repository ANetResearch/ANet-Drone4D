"""Fig. M2: wind breaks decisions through feasibility and model disagreement, not through energy magnitude.

Closed-loop 800 m legs in SynthCity under a reference-wind sweep. (a) share of legs completed when the return trigger
uses its own time-based headwind model vs the planner's shared energy model; (b) electrical power drawn in flight
relative to calm air, and the error of a wind-blind energy estimate of the same flown trajectory.
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import style as S

RATING = 13.8


def main() -> None:
    S.setup()
    d = S.load("energy_wind_shared")
    # legs on the two headings that cross the neighbouring pads are interrupted by separation holds in every run;
    # they say nothing about wind and are left out
    d = d[~d.heading_deg.round().isin([90, 270])].copy()
    d["ok"] = (d.result == "succeeded").astype(float)
    fig, axs = plt.subplots(1, 2, figsize=(S.FULL_W, 2.0), gridspec_kw={"wspace": 0.34})
    ws = np.sort(d.wind_ref.unique())

    ax = axs[0]
    for sh, lab, c, m, ls in [(False, "Separate return-time model", S.G1, "s", "--"),
                              (True, "Shared energy model (ours)", S.RED, "o", "-")]:
        g = d[d.shared == sh]
        st = np.array([S.boot_ci(g[g.wind_ref == w].ok.values) for w in ws]) * 100
        ax.fill_between(ws, st[:, 1], st[:, 2], color=c, alpha=0.15, lw=0)
        ax.plot(ws, st[:, 0], color=c, marker=m, ls=ls, label=lab)
    ax.axvspan(RATING, ws.max() + 1, color=S.G5, zorder=0)
    ax.text(RATING + 1.2, 60, "beyond wind rating", fontsize=6.5, color=S.G1, va="center", ha="center", rotation=90)
    ax.set_xlim(-0.5, ws.max() + 0.5)
    ax.set_ylim(-3, 105)
    ax.set_xlabel("Reference wind speed (m/s)")
    ax.set_ylabel("Legs completed (%)")
    ax.legend(loc="lower left", bbox_to_anchor=(0.0, 0.07), handlelength=1.6, fontsize=6.3)
    S.panel(ax, "(a)", x=-0.2, y=1.03)

    ax = axs[1]
    g = d[d.shared & (d.dur_s > 30)].copy()
    g["p"] = g.true_wh / g.dur_s
    p0 = g[g.wind_ref == 0].p.median()
    g["dp"] = (g.p / p0 - 1) * 100
    g["eb"] = (g.est_blind_wh / g.true_wh - 1) * 100
    for col, lab, c, m, ls in [("dp", "Power drawn vs calm air", S.G1, "s", "--"),
                               ("eb", "Wind-blind estimate error", S.G3, "^", ":")]:
        st = np.array([S.boot_ci(g[g.wind_ref == w][col].values, stat=np.median) for w in ws])
        ax.fill_between(ws, st[:, 1], st[:, 2], color=c, alpha=0.2, lw=0)
        ax.plot(ws, st[:, 0], color=c, marker=m, ls=ls, label=lab)
    ax.axvspan(RATING, ws.max() + 1, color=S.G5, zorder=0)
    ax.axhline(0, color=S.INK, lw=0.5)
    ax.set_xlim(-0.5, ws.max() + 0.5)
    ax.set_ylim(-4, 14)
    ax.set_xlabel("Reference wind speed (m/s)")
    ax.set_ylabel("Relative change (%)")
    ax.legend(loc="upper left", fontsize=6.3, handlelength=1.6)
    S.panel(ax, "(b)", x=-0.2, y=1.03)
    S.save(fig, "m2_wind_feasibility")


if __name__ == "__main__":
    main()
