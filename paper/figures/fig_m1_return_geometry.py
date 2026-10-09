"""Fig. M1: a fixed-altitude straight return crosses buildings; flying high to be safe wastes climb."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import style as S


def main() -> None:
    S.setup()
    d = S.load("m1geo_full")
    h = d[d.h_agl == 40]
    fig, axs = plt.subplots(1, 2, figsize=(S.FULL_W, 2.1), gridspec_kw={"width_ratios": [1.15, 1.0], "wspace": 0.32})

    # (a) per-world share of return lines crossing a building, three fixed return altitudes, start at 40 m AGL
    ax = axs[0]
    worlds = S.WORLD_ORDER
    y = np.arange(len(worlds))[::-1]
    series = [("hit_px4", "PX4 default (+30 m)", S.G1, "s"), ("hit_60", "Fixed +60 m", S.G3, "^"),
              ("hit_120", "Fixed +120 m", "white", "o")]
    for k, w in enumerate(worlds):
        vv = [h[h.world == w][c].mean() * 100 for c, *_ in series]
        ax.plot([min(vv), max(vv)], [y[k], y[k]], color=S.G4, lw=0.8, zorder=1)
    for col, lab, c, m in series:
        v = [h[h.world == w][col].mean() * 100 for w in worlds]
        ax.scatter(v, y, color=c, marker=m, s=18, label=lab, zorder=3, edgecolor=S.INK, linewidth=0.5)
    ax.set_yticks(y, [S.WORLD_LABEL[w] for w in worlds])
    ax.set_xlabel("Return lines inside building clearance (%)")
    ax.set_xlim(0, 85)
    ax.set_ylim(-0.6, len(worlds) - 0.4)
    ax.grid(axis="y", visible=False)
    ax.legend(loc="lower center", bbox_to_anchor=(0.4, 1.0), ncol=3, handletextpad=0.1, columnspacing=0.7, fontsize=6.5)
    S.panel(ax, "(a)", x=-0.36, y=1.03)

    # (b) climb before the return leg in the six real cities: geometry-aware vs fixed altitudes
    ax = axs[1]
    r = h[h.world != "synthcity"]
    S.ecdf(ax, r.climb_px4_m, color=S.G1, ls=":", label="PX4 (+30 m)")
    S.ecdf(ax, r.climb_120_m, color=S.G2, ls="--", label="+120 m")
    S.ecdf(ax, r.climb_aware_m, color=S.RED, lw=1.4, label="Geometry-\naware")
    ma, m120 = r.climb_aware_m.median(), r.climb_120_m.median()
    ax.annotate(f"median {ma:.0f} m", (ma, 0.5), xytext=(22, 0.40), fontsize=6.5, color=S.RED,
                arrowprops=dict(arrowstyle="-", color=S.RED, lw=0.5))
    ax.annotate(f"median {m120:.0f} m", (m120, 0.5), xytext=(112, 0.62), fontsize=6.5, color=S.G1, ha="center",
                arrowprops=dict(arrowstyle="-", color=S.G1, lw=0.5))
    ax.set_xlabel("Climb before the return leg (m)")
    ax.set_ylabel("CDF over return starts")
    ax.set_xlim(-3, 160)
    ax.set_ylim(0, 1.02)
    ax.legend(loc="lower right", bbox_to_anchor=(1.02, 0.02), handlelength=1.5)
    S.panel(ax, "(b)", x=-0.2, y=1.03)
    S.save(fig, "m1_return_geometry")


if __name__ == "__main__":
    main()
