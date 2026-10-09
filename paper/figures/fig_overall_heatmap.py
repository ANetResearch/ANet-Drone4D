"""Fig. overall heatmap: sorties lost per city and weather preset, PX4 default vs the coupled policy."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

import rtl_common as C
import style as S


def grid(d, pol: str, col: str) -> np.ndarray:
    g = d[d.policy == pol].groupby(["world", "preset"])[col].mean().mul(100)
    return np.array([[g.get((w, p), np.nan) for p in S.PRESET_ORDER] for w in S.WORLD_ORDER])


def main() -> None:
    S.setup()
    d = C.load()
    cmap_bad = LinearSegmentedColormap.from_list("bad", ["#FFFFFF", "#F2C4CB", S.RED, "#6E0A1A"])
    cmap_ok = LinearSegmentedColormap.from_list("ok", ["#FFFFFF", S.G4, S.G2, S.G1])
    panels = [("px4", "unsafe", cmap_bad, "PX4 default: sorties lost or launched beyond rating (%)"),
              ("coupled", "unsafe", cmap_bad, "Coupled: sorties lost or launched beyond rating (%)"),
              ("coupled", "completed", cmap_ok, "Coupled: sorties completed (%)")]
    fig, axs = plt.subplots(3, 1, figsize=(S.FULL_W, 4.6), gridspec_kw={"hspace": 0.28})
    for k, (ax, (pol, col, cm, title)) in enumerate(zip(axs, panels)):
        a = grid(d, pol, col)
        im = ax.imshow(a, cmap=cm, vmin=0, vmax=100, aspect="auto")
        for i in range(a.shape[0]):
            for j in range(a.shape[1]):
                v = a[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:.0f}", ha="center", va="center", fontsize=5.6,
                            color="white" if v > 55 else S.INK)
        if k == len(panels) - 1:
            ax.set_xticks(range(len(S.PRESET_ORDER)), [S.PRESET_LABEL[p] for p in S.PRESET_ORDER], rotation=30,
                          ha="right", rotation_mode="anchor", fontsize=6.3)
        else:
            ax.set_xticks(range(len(S.PRESET_ORDER)), [])
        ax.set_yticks(range(len(S.WORLD_ORDER)), [S.WORLD_LABEL[w] for w in S.WORLD_ORDER], fontsize=6.3)
        ax.tick_params(length=0)
        ax.grid(False)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.set_title(title, loc="left", fontsize=7.2, pad=3)
        cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
        cb.ax.tick_params(labelsize=6, length=1.5)
        cb.outline.set_linewidth(0.4)
    S.save(fig, "overall_heatmap")


if __name__ == "__main__":
    main()
