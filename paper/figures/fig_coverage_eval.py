"""Fig. coverage evaluation: GSD scan plans in the three low-visibility presets, weather-blind vs coupled.
(a) share of ground targets captured at the required perception level; (b) what the coupled plan pays for it
(drones per scan and makespan, relative to the weather-blind plan of the same task)."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import style as S

PRESETS = ["fog", "blizzard", "sandstorm"]
LEVELS = [("D", "Detect"), ("R", "Recognise"), ("I", "Identify")]
SHORT = {"D": "D", "R": "R", "I": "I"}


def main() -> None:
    S.setup()
    d = S.load("m3perc_full")
    d = d[d.mor_set.isna() & d.aoi_side_m.isna() & d.policy.isin(["aware", "blind"])]
    ceil = d[d.preset == "clear"].groupby("level").capture_rate.mean()
    fig, (a, b) = plt.subplots(1, 2, figsize=(S.FULL_W, 2.2), gridspec_kw={"width_ratios": [1.6, 1], "wspace": 0.3})
    w = 0.36
    xt, xl = [], []
    for pi, p in enumerate(PRESETS):
        for li, (lv, ln) in enumerate(LEVELS):
            x0 = pi * 3.6 + li * 1.0
            for k, pol in enumerate(["blind", "aware"]):
                s = d[(d.preset == p) & (d.level == lv) & (d.policy == pol)].capture_rate
                m, lo, hi = S.boot_ci(s.values)
                a.bar(x0 + (k - 0.5) * w, m * 100, w, color=S.RED if pol == "aware" else S.G4, edgecolor=S.INK,
                      linewidth=0.4, label=(S.POLICY[pol]["label"] if pi == 0 and li == 0 else None))
                a.errorbar(x0 + (k - 0.5) * w, m * 100, yerr=[[(m - lo) * 100], [(hi - m) * 100]], color=S.INK,
                           lw=0.6, capsize=1.2)
            a.plot([x0 - w, x0 + w], [ceil[lv] * 100] * 2, color=S.INK, lw=0.7, ls=":")
            xt.append(x0)
            xl.append(SHORT[lv])
        a.text(pi * 3.6 + 1.0, -10, S.PRESET_LABEL[p], ha="center", va="top", fontsize=7, fontweight="bold")
    a.set_xticks(xt, xl, fontsize=6.4)
    a.tick_params(axis="x", length=0, pad=1.5)
    a.set_ylim(0, 105)
    a.set_ylabel("Targets captured (%)")
    a.grid(axis="x", visible=False)
    a.plot([], [], color=S.INK, lw=0.7, ls=":", label="Clear-air ceiling")
    a.legend(ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0), fontsize=6.2)
    S.panel(a, "(a)", x=-0.12, y=1.1)

    rows = []
    for lv, ln in LEVELS:
        x = d[d.preset.isin(PRESETS) & (d.level == lv)]
        k = ["world", "preset", "seed"]
        aw = x[x.policy == "aware"].set_index(k)
        bl = x[x.policy == "blind"].set_index(k).reindex(aw.index)
        ok = aw.plan_feasible.values & bl.plan_feasible.values
        rows.append((ln, (aw.plan_n_drones.values[ok] / bl.plan_n_drones.values[ok]),
                     (aw.plan_makespan_s.values[ok] / bl.plan_makespan_s.values[ok])))
    y = np.arange(len(rows))
    for j, (lab, col, mk) in enumerate([("Drones per scan", S.G2, "o"), ("Makespan", S.G4, "s")]):
        for i, r in enumerate(rows):
            m, lo, hi = S.boot_ci(r[1 + j])
            b.barh(i + (j - 0.5) * 0.36, m, 0.36, color=col, edgecolor=S.INK, linewidth=0.4,
                   label=lab if i == 0 else None)
            b.errorbar(m, i + (j - 0.5) * 0.36, xerr=[[m - lo], [hi - m]], color=S.INK, lw=0.6, capsize=1.2)
            b.text(hi + 0.03, i + (j - 0.5) * 0.36, f"{m:.2f}\u00d7", va="center", fontsize=5.8)
    b.axvline(1.0, color=S.INK, lw=0.6)
    b.set_yticks(y, [r[0] for r in rows], fontsize=6.6)
    b.invert_yaxis()
    b.set_xlim(0.8, 1.9)
    b.set_xlabel("Coupled / weather-blind")
    b.grid(axis="y", visible=False)
    b.tick_params(axis="y", length=0)
    b.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=1, fontsize=6.2)
    S.panel(b, "(b)", x=-0.22, y=1.1)
    S.save(fig, "coverage_eval")


if __name__ == "__main__":
    main()
