"""Fig. M3: a scan plan sized for clear air silently stops seeing its targets when visibility drops."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import style as S

LEVEL = {"D": ("Detection", "o", S.G3, ":"), "R": ("Recognition", "s", S.G1, "--"), "I": ("Identification", "^", S.G2, "-.")}


def rel_capture(d):
    """Capture rate divided by the same world/seed/level capture in clear air (the occlusion ceiling)."""
    base = d[(d.preset == "clear") & d.mor_set.isna()].groupby(["world", "seed", "level", "policy"]).capture_rate.mean()
    key = list(zip(d.world, d.seed, d.level, d.policy))
    return d.capture_rate.to_numpy() / np.maximum(base.reindex(key).to_numpy(), 1e-9)


def main() -> None:
    S.setup()
    d = S.load("m3perc_full")
    d["rel"] = rel_capture(d)
    fig, axs = plt.subplots(1, 2, figsize=(S.FULL_W, 2.1), gridspec_kw={"width_ratios": [1.0, 0.9], "wspace": 0.75})

    # (a) MOR sweep, weather-blind plans: share of targets reaching the required level
    ax = axs[0]
    m = d[d.mor_set.notna() & (d.policy == "blind")]
    ceil = d[(d.preset == "clear") & d.mor_set.isna() & (d.policy == "blind")].capture_rate.mean() * 100
    ax.axhline(ceil, color=S.G4, lw=0.8, ls="-", zorder=1)
    ax.text(5200, ceil + 2, "clear-air ceiling (occlusion)", fontsize=6.3, color=S.G2, ha="right", va="bottom")
    for lv, (lab, mk, c, ls) in LEVEL.items():
        g = m[m.level == lv].groupby("mor_set").capture_rate
        mu = g.mean() * 100
        ax.plot(mu.index, mu, color=c, marker=mk, ls=ls, label=lab, markeredgecolor=S.INK, markeredgewidth=0.3)
    ax.set_xscale("log")
    ax.set_xticks([100, 200, 500, 1000, 2000, 5000], ["100", "200", "500", "1k", "2k", "5k"])
    ax.set_xlabel("Meteorological optical range (m)")
    ax.set_ylabel("Targets at required level (%)")
    ax.set_ylim(-3, 105)
    ax.legend(loc="lower right", bbox_to_anchor=(1.02, 0.04))
    S.panel(ax, "(a)", x=-0.27, y=1.03)

    # (b) weather presets: capture relative to the clear-air ceiling, weather-blind plans
    ax = axs[1]
    p = d[d.mor_set.isna() & (d.policy == "blind")]
    order = [x for x in S.PRESET_ORDER if x in set(p.preset)]
    mor = p.groupby("preset").mor_50_m.median()
    order = sorted(order, key=lambda x: -mor[x])
    y = np.arange(len(order))[::-1]
    for j, (lv, (lab, mk, c, _)) in enumerate(LEVEL.items()):
        clear = p[(p.preset == "clear") & (p.level == lv)].capture_rate.mean()
        v = [p[(p.preset == q) & (p.level == lv)].capture_rate.mean() / clear * 100 for q in order]
        ax.scatter(v, y + (1 - j) * 0.22, marker=mk, color=c, s=14, edgecolor=S.INK, linewidth=0.3, label=lab, zorder=3)
    ax.set_yticks(y, [f"{S.PRESET_LABEL[q]} ({mor[q] / 1000:.1f} km)" if mor[q] < 10000 else
                      S.PRESET_LABEL[q] for q in order], fontsize=6.5)
    ax.set_xlabel("Captured vs. clear air (%)")
    ax.set_xlim(-4, 108)
    ax.grid(axis="y", visible=False)
    for k in range(len(order)):
        ax.axhline(y[k], color=S.G5, lw=0.4, zorder=0)
    S.panel(ax, "(b)", x=-0.62, y=1.03)
    S.save(fig, "m3_perception_gap")


if __name__ == "__main__":
    main()
