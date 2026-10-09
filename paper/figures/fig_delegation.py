"""Fig. delegation: contract-net awards on the shared models vs distance-only awards.
(a) observation requests completed per weather group; (b) what goes wrong with the awarded drone."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

import style as S

GROUPS = {"Clear air": ["clear", "partlyCloudy", "overcast", "haze"],
          "Precipitation": ["lightRain", "rain", "heavyRain", "snow"],
          "Low visibility": ["fog", "blizzard", "sandstorm"],
          "Thunderstorm": ["thunderstorm"]}
POL = [("distance", "Distance only", S.G4), ("no_perception", "Contract net, blind quote", S.G2),
       ("coupled", "Contract net (ours)", S.RED)]
CAUSES = [("not_seen", "Target not resolved", S.G4, ""), ("energy", "Reserve broken", S.G2, ""),
          ("over_rating", "Beyond wind rating", S.RED, ""), ("declined", "Declined", "white", "////")]


def main() -> None:
    S.setup()
    d = S.load("deleg_full")
    d["group"] = d.preset.map({p: g for g, ps in GROUPS.items() for p in ps})
    d["energy"] = d.reserve_violation & ~d.over_rating
    d["not_seen"] = ~d.seen & ~d.declined & ~d.energy & ~d.over_rating
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.25), gridspec_kw={"width_ratios": [1.45, 1], "wspace": 0.35})
    w = 0.26
    for gi, g in enumerate(GROUPS):
        for k, (p, lab, col) in enumerate(POL):
            x = d[(d.group == g) & (d.policy == p)]
            m, lo, hi = S.boot_ci_cluster(x.success.astype(float).values, S.task_key(x))
            a.bar(gi + (k - 1) * w, m * 100, w, color=col, edgecolor=S.INK, linewidth=0.4, label=lab if gi == 0 else None)
            a.errorbar(gi + (k - 1) * w, m * 100, yerr=[[(m - lo) * 100], [(hi - m) * 100]], color=S.INK, lw=0.6,
                       capsize=1.2)
    a.set_xticks(range(len(GROUPS)), [g.replace(" ", "\n") for g in GROUPS], fontsize=6.6)
    a.text(3, 3, "none\ncompleted", ha="center", va="bottom", fontsize=5.6, color=S.G1)
    a.set_ylabel("Requests completed (%)")
    a.set_ylim(0, 105)
    a.grid(axis="x", visible=False)
    a.legend(ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=6.0, columnspacing=0.8)
    S.panel(a, "(a)", x=-0.13, y=1.2)

    for k, (p, lab, _) in enumerate(POL):
        x = d[d.policy == p]
        left = 0.0
        for col, _, colr, h in CAUSES:
            v = x[col].mean() * 100
            b.barh(k, v, left=left, height=0.6, color=colr, edgecolor=S.INK, linewidth=0.4, hatch=h)
            if v >= 3.5:
                box = dict(boxstyle="square,pad=0.1", fc="white", ec="none") if h else None
                b.text(left + v / 2, k, f"{v:.0f}", ha="center", va="center", fontsize=5.8, bbox=box,
                       color="white" if colr in (S.RED, S.G2) else S.INK)
            left += v
    b.set_yticks(range(len(POL)), [p[1].replace(", ", ",\n") for p in POL], fontsize=6.2)
    b.tick_params(axis="y", length=0)
    b.invert_yaxis()
    b.set_xlabel("Requests (%)")
    b.grid(axis="y", visible=False)
    hs = [Patch(facecolor=c, edgecolor=S.INK, linewidth=0.4, hatch=h, label=l) for _, l, c, h in CAUSES]
    b.legend(handles=hs, ncol=2, loc="lower left", bbox_to_anchor=(-0.05, 1.0), fontsize=5.8, handlelength=1.1,
             columnspacing=0.6)
    S.panel(b, "(b)", x=-0.45, y=1.2)
    S.save(fig, "delegation")


if __name__ == "__main__":
    main()
