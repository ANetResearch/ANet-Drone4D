"""Fig. ablation: remove one coupling at a time from the coupled policy (same sorties, same ground truth) and report
sorties completed, sorties unsafe (lost or launched beyond the wind rating) and energy per completed sortie."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np

import rtl_common as C
import style as S

ORDER = ["coupled", "no_geometry", "no_detour", "no_wind", "no_shared", "no_energy", "no_precheck"]


def main() -> None:
    S.setup()
    d = C.load()
    fig, axs = plt.subplots(1, 3, figsize=(S.FULL_W, 2.2), sharey=True, gridspec_kw={"wspace": 0.12})
    metrics = [("completed", "Sorties completed (%)", 100.0, None),
               ("unsafe", "Sorties unsafe (%)", 100.0, None),
               ("wh", "Wh per completed sortie", 1.0, "completed")]
    y = np.arange(len(ORDER))
    for ax, (col, xlabel, k, cond) in zip(axs, metrics):
        vals = []
        for p in ORDER:
            x = d[d.policy == p]
            v = x.loc[x.completed, "wh_used"] if col == "wh" else x[col].astype(float)
            m, lo, hi = S.boot_ci_cluster(v.values, S.task_key(x.loc[v.index]))
            vals.append((m * k, lo * k, hi * k))
        ref = vals[0][0]
        for i, (p, (m, lo, hi)) in enumerate(zip(ORDER, vals)):
            st = S.POLICY[p]
            ax.barh(i, m, height=0.66, color=S.RED if p == "coupled" else S.G4, edgecolor=S.INK, linewidth=0.4,
                    hatch="")
            ax.errorbar(m, i, xerr=[[m - lo], [hi - m]], color=S.INK, lw=0.7, capsize=1.6)
            fmt = "{:.1f}" if col != "wh" else "{:.0f}"
            ax.text(hi, i, "  " + fmt.format(m), va="center", ha="left",
                    fontsize=6)
        ax.axvline(ref, color=S.RED, lw=0.7, ls=":")
        ax.set_xlabel(xlabel, fontsize=7)
        ax.grid(axis="y", visible=False)
        lim = max(v[2] for v in vals)
        ax.set_xlim(0 if col != "wh" else 80, lim * 1.22 if col != "wh" else lim * 1.06)
    axs[0].set_yticks(y, [S.POLICY[p]["label"] for p in ORDER], fontsize=6.6)
    axs[0].invert_yaxis()
    for ax, t in zip(axs, "abc"):
        ax.tick_params(axis="y", length=0)
        S.panel(ax, f"({t})", x=-0.06 if t != "a" else -0.1, y=1.02)
    S.save(fig, "ablation")


if __name__ == "__main__":
    main()
