"""Fig. weather fronts: sorties launched in clear air while the field holds a front that arrives over 10 min.
(a) outcome of every sortie per front for the PX4 default, the snapshot precheck and the space-time precheck;
(b) station time delivered by sorties that came home, as a share of the station time requested."""

from __future__ import annotations

import os

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

import style as S

RUN = os.environ.get("FRONT_RUN", "rtl_front2")
RATING = 13.8
FRONTS = ["rain", "heavyRain", "thunderstorm"]
POLS = [("px4", "PX4 default"), ("coupled", "Snapshot precheck"), ("coupled_4d", "Space-time precheck (ours)")]
OUTCOMES = [("done_safe", "Completed", S.G2, ""), ("done_over", "Completed beyond rating", "#E8A0AA", ""),
            ("aborted", "Returned early", S.G4, ""), ("declined", "Not launched", "white", "////"),
            ("lost", "Lost", S.RED, "")]


def load():
    d = S.load(RUN)
    d["completed"] = d.outcome.eq("home")
    d["aborted"] = d.outcome.eq("aborted")
    d["declined"] = d.outcome.eq("declined")
    d["lost"] = d.outcome.isin(("collision", "stranded", "timeout"))
    d["over"] = d.wind_exp_max.fillna(0.0) > RATING
    d["done_safe"] = d.completed & ~d.over
    d["done_over"] = d.completed & d.over
    d["unsafe"] = d.lost | d.over
    d["delivered"] = np.where(d.done_safe, d.t_station_s, 0.0)
    return d


def main() -> None:
    S.setup()
    d = load()
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.6), gridspec_kw={"width_ratios": [1.35, 1], "wspace": 0.5})
    y, ticks = [], []
    k = 0
    for fi, f in enumerate(FRONTS):
        for pi, (p, lab) in enumerate(POLS):
            yy = fi * 4.4 + pi * 1.05
            y.append(yy)
            ticks.append(lab.replace(" (ours)", ""))
            x = d[(d.front == f) & (d.policy == p)]
            left = 0.0
            for col, _, colr, h in OUTCOMES:
                v = x[col].mean() * 100
                a.barh(yy, v, left=left, height=0.85, color=colr, edgecolor=S.INK, linewidth=0.4, hatch=h)
                if v >= 9:
                    box = dict(boxstyle="square,pad=0.12", fc="white", ec="none") if h else None
                    a.text(left + v / 2, yy, f"{v:.0f}", ha="center", va="center", fontsize=6, bbox=box,
                           color="white" if colr in (S.RED, S.G2) else S.INK)
                left += v
            k += 1
        a.annotate(S.PRESET_LABEL[f], xy=(0, fi * 4.4 + 1.05), xycoords=("axes fraction", "data"), xytext=(-74, 0),
                   textcoords="offset points", ha="right", va="center", fontsize=6.2, fontweight="bold", rotation=90)
    a.set_yticks(y, ticks, fontsize=6.0)
    for t in a.get_yticklabels():
        if t.get_text().startswith("Space-time"):
            t.set_color(S.RED)
    a.tick_params(axis="y", length=0, pad=1)
    a.set_xlim(0, 100)
    a.set_xlabel("Sorties (%)")
    a.invert_yaxis()
    a.grid(axis="y", visible=False)
    hs = [Patch(facecolor=c, edgecolor=S.INK, linewidth=0.4, hatch=h, label=l) for _, l, c, h in OUTCOMES]
    a.legend(handles=hs, ncol=3, loc="lower left", bbox_to_anchor=(-0.05, 1.0), fontsize=6.0, handlelength=1.2,
             columnspacing=0.7, handletextpad=0.4)
    S.panel(a, "(a)", x=-0.62, y=1.14)

    w = 0.26
    cols = {"px4": S.G1, "coupled": S.G4, "coupled_4d": S.RED}
    for fi, f in enumerate(FRONTS):
        for pi, (p, lab) in enumerate(POLS):
            x = d[(d.front == f) & (d.policy == p)]
            share = x.delivered / x.t_station_req_s
            m, lo, hi = S.boot_ci_cluster(share.values, S.task_key(x))
            b.bar(fi + (pi - 1) * w, m * 100, w, color=cols[p], edgecolor=S.INK, linewidth=0.4,
                  label=lab.replace(" precheck", "") if fi == 0 else None)
            b.errorbar(fi + (pi - 1) * w, m * 100, yerr=[[(m - lo) * 100], [(hi - m) * 100]], color=S.INK, lw=0.6,
                       capsize=1.2)
            b.text(fi + (pi - 1) * w, hi * 100 + 2, f"{x.unsafe.mean() * 100:.0f}", ha="center", va="bottom",
                   fontsize=5.6, color=S.RED)
    b.set_xticks(range(len(FRONTS)), [S.PRESET_LABEL[f].replace(" ", "\n") for f in FRONTS], fontsize=6.6)
    b.set_ylabel("Station time delivered safely (%)")
    b.text(1.0, -0.2, "numbers above bars: sorties unsafe (%)", transform=b.transAxes, ha="right", va="top", fontsize=5.8,
           color=S.RED)
    b.set_ylim(0, 75)
    b.grid(axis="x", visible=False)
    b.legend(loc="upper right", bbox_to_anchor=(1.02, 1.0), fontsize=5.6, handlelength=1.0)
    S.panel(b, "(b)", x=-0.2, y=1.14)
    S.save(fig, "front")


if __name__ == "__main__":
    main()
