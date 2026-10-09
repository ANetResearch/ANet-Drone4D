"""Fig. cross-device access: the same 60 s scripted camera flight over Shenzhen on four clients, with the adaptive
budget (APH + CAS) vs fixed point budgets (1M is the Potree default). Reads the per-run probe snapshots collected by
tools/paper/web/stream_bench.mjs into paper/data/stream_full.csv (see collect())."""

from __future__ import annotations

import glob
import json
import sys

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import style as S

DEVICES = [("bmax_a100", "A100\n(LAN)"), ("mac_m3", "M3\n(WAN)"), ("bmax_swiftshader", "Software\n(CPU)")]
VARIANTS = [("cas", "Adaptive (ours)", S.RED), ("fixed100k", "Fixed 100k", S.G5), ("fixed1m", "Fixed 1M (Potree default)", S.G3),
            ("fixed3m", "Fixed 3M", S.G1)]


def collect(dirs: list[str]) -> None:
    rows = []
    for d in dirs:
        for f in sorted(glob.glob(f"{d}/*.json")):
            r = json.load(open(f))
            s = r["snap"]
            iv = np.asarray(s["frame"]["interval"], np.float64)
            iv = iv[np.isfinite(iv) & (iv > 0)]
            dr = np.asarray(s["pc"]["drawn_ring"], np.float64)
            er = np.asarray(s["pc"]["achievedErr"], np.float64)
            tgt = float(s["meta"]["targetMs"])
            rows.append({"device": r["device"], "variant": r["variant"], "rep": r["rep"], "renderer": r["gl"]["renderer"],
                         "tier": s["meta"]["tier"], "target_ms": tgt, "frames": len(iv),
                         "fps": 1000.0 / np.mean(iv) if len(iv) else np.nan,
                         "p50_ms": np.percentile(iv, 50), "p95_ms": np.percentile(iv, 95), "p99_ms": np.percentile(iv, 99),
                         "over_target": float(np.mean(iv > 1.5 * tgt)), "drawn_med": np.median(dr), "drawn_max": dr.max(),
                         "err_med_px": np.median(er), "err_p95_px": np.percentile(er, 95),
                         "B_final": s["cas"]["B"], "downloaded_mb": s["pc"]["downloadedBytes"] / 1e6,
                         "tti_ms": s["load"]["tti"], "first_pixel_ms": s["load"]["ttfpFirstPixel"]})
    pd.DataFrame(rows).to_csv(S.DATA / "stream_full.csv", index=False)


def main() -> None:
    S.setup()
    d = pd.read_csv(S.DATA / "stream_full.csv")
    m = d.groupby(["device", "variant"]).median(numeric_only=True)
    fig, axs = plt.subplots(1, 3, figsize=(S.FULL_W, 2.3), gridspec_kw={"wspace": 0.42})
    w = 0.2
    for ax, (col, lab, log) in zip(axs, [("p95_ms", "95th-pct frame time (ms)", True),
                                         ("err_med_px", "Screen-space error (px)", False),
                                         ("drawn_med", "Points drawn (millions)", False)]):
        for di, (dev, _) in enumerate(DEVICES):
            for k, (v, vl, c) in enumerate(VARIANTS):
                if (dev, v) not in m.index:
                    continue
                y = m.loc[(dev, v), col] / (1e6 if col == "drawn_med" else 1.0)
                ax.bar(di + (k - 1.5) * w, y, w, color=c, edgecolor=S.INK, linewidth=0.4,
                       label=vl if di == 0 else None)
        if col == "p95_ms":
            for di, (dev, _) in enumerate(DEVICES):
                t = d[d.device == dev].target_ms.median()
                ax.plot([di - 2 * w, di + 2 * w], [t, t], color=S.INK, lw=0.7, ls=":")
            ax.set_ylim(0, 160)
        ax.set_xticks(range(len(DEVICES)), [x[1] for x in DEVICES], fontsize=6)
        ax.set_ylabel(lab, fontsize=7)
        ax.grid(axis="x", visible=False)
    axs[0].legend(ncol=4, loc="lower left", bbox_to_anchor=(0.0, 1.06), fontsize=6.0, columnspacing=0.8)
    for ax, t in zip(axs, "abc"):
        S.panel(ax, f"({t})", x=-0.32, y=1.0)
    S.save(fig, "stream")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "collect":
        collect(sys.argv[2:])
    else:
        main()
