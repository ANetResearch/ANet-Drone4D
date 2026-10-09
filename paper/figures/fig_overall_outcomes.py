"""Fig. overall outcomes: (a) what happened to every sortie, per weather group, coupled vs PX4 default;
(b) how high returns climb above the depot, with the collision rate of each policy."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

import rtl_common as C
import style as S

OUTCOMES = [("completed", "Completed", S.G2, ""), ("aborted", "Returned early", S.G4, ""),
            ("declined", "Not launched", "white", "////"), ("lost", "Lost", S.RED, "")]


def main() -> None:
    S.setup()
    d = C.load()
    d["wg"] = d.preset.map(C.weather_group)
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.35), gridspec_kw={"width_ratios": [1.25, 1], "wspace": 0.42})

    groups = list(C.WEATHER_GROUPS)
    pols = ["px4", "coupled"]
    y, ticks = [], []
    for gi, g in enumerate(groups):
        for pi, p in enumerate(pols):
            y.append(gi * 2.7 + pi * 1.05)
            ticks.append(("PX4 default" if p == "px4" else "Coupled") )
    k = 0
    for gi, g in enumerate(groups):
        for p in pols:
            x = d[(d.wg == g) & (d.policy == p)]
            left = 0.0
            for col, _, colr, h in OUTCOMES:
                v = x[col].mean() * 100
                a.barh(y[k], v, left=left, height=0.85, color=colr, edgecolor=S.INK, linewidth=0.4, hatch=h)
                if v >= 9:
                    box = dict(boxstyle="square,pad=0.12", fc="white", ec="none") if h else None
                    a.text(left + v / 2, y[k], f"{v:.0f}", ha="center", va="center", fontsize=6, bbox=box,
                           color="white" if colr in (S.RED, S.G2) else S.INK)
                left += v
            ov = x.over_rating.mean() * 100
            if ov >= 0.5:
                a.text(101.5, y[k], f"{ov:.0f}%\u2191", ha="left", va="center", fontsize=6, color=S.RED)
            k += 1
        a.text(-2, gi * 2.7 + 0.52, g, ha="right", va="center", fontsize=7, fontweight="bold",
               transform=a.get_yaxis_transform() if False else a.transData)
    a.set_yticks(y, ticks, fontsize=6.3)
    a.tick_params(axis="y", length=0, pad=1)
    a.set_xlim(0, 100)
    a.set_xlabel("Sorties (%)")
    a.invert_yaxis()
    a.grid(axis="y", visible=False)
    for gi, g in enumerate(groups):
        a.annotate(g, xy=(0, gi * 2.7 + 0.52), xycoords=("axes fraction", "data"), xytext=(-48, 0),
                   textcoords="offset points", ha="right", va="center", fontsize=6.6, fontweight="bold", rotation=90)
    for t in a.texts[:]:
        if t.get_text() in groups and t.get_position()[0] == -2:
            t.remove()
    hs = [Patch(facecolor=c, edgecolor=S.INK, linewidth=0.4, hatch=h, label=l) for _, l, c, h in OUTCOMES]
    a.legend(handles=hs, ncol=4, loc="lower left", bbox_to_anchor=(-0.02, 1.0), fontsize=6.2, handlelength=1.2,
             columnspacing=0.7, handletextpad=0.4)
    a.text(1.0, -0.2, "\u2191 launched beyond the wind rating", transform=a.transAxes, ha="right", va="top",
           fontsize=5.8, color=S.RED)
    S.panel(a, "(a)", x=-0.42, y=1.08)

    r = d[d.rtl_cmd.astype(bool) & d.z_max_rtl_m.notna()].copy()
    r["climb"] = r.z_max_rtl_m - r.z_home_m
    for p in ["px4", "no_detour", "coupled"]:
        x = r[r.policy == p]
        coll = d[d.policy == p].collision.mean() * 100
        st = S.POLICY[p]
        lab = {"px4": "PX4 default", "no_detour": "w/o detour", "coupled": "Coupled (ours)"}[p]
        S.ecdf(b, x.climb.values, color=st["color"], ls=st["ls"], lw=1.3, label=f"{lab}  {coll:.1f}%")
    b.set_xlim(0, 260)
    b.set_ylim(0, 1.0)
    b.set_xlabel("Return climb above depot (m)")
    b.set_ylabel("CDF of returns")
    b.legend(loc="lower right", bbox_to_anchor=(1.0, 0.04), fontsize=6.0, handlelength=1.8,
             title="collision rate", title_fontsize=6.0)
    S.panel(b, "(b)", x=-0.2, y=1.08)
    S.save(fig, "overall_outcomes")


if __name__ == "__main__":
    main()
