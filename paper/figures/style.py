"""Shared figure style: vector PDF with embedded Type 42 fonts, sized for the sn-jnl single-column text block.

Palette: graphite greys for baselines and a single red for the coupled system ("ours"); every series also differs in
marker and line style so figures stay readable in greyscale and for colour-blind readers.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
OUT = ROOT / "figures" / "pdf"

FULL_W = 5.15  # inches: sn-jnl \textwidth = 372 pt
HALF_W = 2.5

RED = "#C8102E"
INK = "#1F1F1F"
G1, G2, G3, G4, G5 = "#3A3A3A", "#6B6B6B", "#9A9A9A", "#C4C4C4", "#E6E6E6"
BLUE = "#2F5D8A"  # rarely used secondary accent (reference lines, budgets)

POLICY = {
    "coupled": dict(label="Coupled (ours)", color=RED, marker="o", ls="-", hatch=""),
    "px4": dict(label="PX4 default", color=G1, marker="s", ls="--", hatch="///"),
    "px4_time": dict(label="Energy-timed, blind", color=G3, marker="^", ls=":", hatch="..."),
    "no_geometry": dict(label="w/o geometry", color=G1, marker="v", ls="--", hatch="\\\\"),
    "no_detour": dict(label="w/o detour", color=G2, marker="D", ls="-.", hatch="xx"),
    "no_wind": dict(label="w/o wind", color=G3, marker="P", ls=":", hatch=".."),
    "no_energy": dict(label="w/o energy return", color=G4, marker="X", ls="--", hatch="++"),
    "no_precheck": dict(label="w/o precheck", color=G2, marker="*", ls="-.", hatch="--"),
    "no_shared": dict(label="w/o shared energy model", color=G3, marker="h", ls="--", hatch="oo"),
    "blind": dict(label="Weather-blind", color=G1, marker="s", ls="--", hatch="///"),
    "vis": dict(label="Visibility only", color=G3, marker="^", ls=":", hatch="..."),
    "wind": dict(label="Wind only", color=G4, marker="v", ls="-.", hatch="xx"),
    "aware": dict(label="Coupled (ours)", color=RED, marker="o", ls="-", hatch=""),
}

WORLD_LABEL = {"shenzhen": "Shenzhen", "shanghai": "Shanghai", "newyork": "New York", "chicago": "Chicago",
               "sanfrancisco": "San Francisco", "suzhou": "Suzhou", "synthcity": "SynthCity"}
WORLD_ORDER = ["shenzhen", "shanghai", "suzhou", "newyork", "chicago", "sanfrancisco", "synthcity"]
PRESET_LABEL = {"clear": "Clear", "partlyCloudy": "Partly cloudy", "overcast": "Overcast", "lightRain": "Light rain",
                "rain": "Rain", "heavyRain": "Heavy rain", "thunderstorm": "Thunderstorm", "fog": "Fog", "haze": "Haze",
                "snow": "Snow", "blizzard": "Blizzard", "sandstorm": "Sandstorm"}
PRESET_ORDER = ["clear", "partlyCloudy", "overcast", "haze", "lightRain", "rain", "heavyRain", "snow", "fog",
                "sandstorm", "blizzard", "thunderstorm"]


def setup() -> None:
    mpl.rcParams.update({
        "pdf.fonttype": 42, "ps.fonttype": 42,
        "font.family": "sans-serif", "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
        "mathtext.fontset": "custom", "mathtext.rm": "Arial", "mathtext.it": "Arial:italic", "mathtext.bf": "Arial:bold",
        "font.size": 8, "axes.titlesize": 8, "axes.labelsize": 8, "xtick.labelsize": 7, "ytick.labelsize": 7,
        "legend.fontsize": 7, "legend.frameon": False, "legend.handlelength": 1.8, "legend.columnspacing": 1.0,
        "axes.edgecolor": INK, "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK, "text.color": INK,
        "axes.linewidth": 0.6, "xtick.major.width": 0.6, "ytick.major.width": 0.6, "xtick.major.size": 2.5,
        "ytick.major.size": 2.5, "xtick.minor.size": 1.5, "ytick.minor.size": 1.5,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "grid.color": G5, "grid.linewidth": 0.5, "axes.axisbelow": True,
        "lines.linewidth": 1.2, "lines.markersize": 3.5, "patch.linewidth": 0.6,
        "savefig.dpi": 300, "savefig.bbox": "tight", "savefig.pad_inches": 0.02,
        "figure.dpi": 150,
    })


def save(fig: plt.Figure, name: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f"{name}.pdf"
    fig.savefig(p)
    fig.savefig(OUT / f"{name}.png", dpi=200)
    plt.close(fig)
    return p


def load(name: str) -> pd.DataFrame:
    return pd.read_parquet(DATA / f"{name}.parquet")


def boot_ci(x: np.ndarray, stat=np.mean, n: int = 2000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float, float]:
    """Statistic with a percentile bootstrap (1 − alpha) confidence interval."""
    x = np.asarray(x, np.float64)
    x = x[np.isfinite(x)]
    if len(x) == 0:
        return np.nan, np.nan, np.nan
    rng = np.random.default_rng(seed)
    b = stat(x[rng.integers(0, len(x), (n, len(x)))], axis=1)
    return float(stat(x)), float(np.quantile(b, alpha / 2)), float(np.quantile(b, 1 - alpha / 2))


def boot_ci_cluster(x: np.ndarray, groups: np.ndarray, n: int = 2000, alpha: float = 0.05,
                    seed: int = 0) -> tuple[float, float, float]:
    """Mean with a cluster bootstrap interval: whole groups (tasks) are resampled, since the items of one task share its
    city, weather and seed and are not independent."""
    x = np.asarray(x, np.float64)
    ok = np.isfinite(x)
    x, g = x[ok], np.asarray(groups)[ok]
    if len(x) == 0:
        return np.nan, np.nan, np.nan
    _, gi = np.unique(g, return_inverse=True)
    s, c = np.bincount(gi, x), np.bincount(gi).astype(np.float64)
    rng = np.random.default_rng(seed)
    k = rng.integers(0, len(s), (n, len(s)))
    b = s[k].sum(1) / c[k].sum(1)
    return float(x.mean()), float(np.quantile(b, alpha / 2)), float(np.quantile(b, 1 - alpha / 2))


def task_key(d) -> np.ndarray:
    return (d.world.astype(str) + "|" + d.preset.astype(str) + "|" + d.seed.astype(str)).values


def panel(ax: plt.Axes, tag: str, x: float = -0.16, y: float = 1.04) -> None:
    ax.text(x, y, tag, transform=ax.transAxes, fontweight="bold", fontsize=8.5, va="bottom", ha="left")


def ecdf(ax: plt.Axes, x: np.ndarray, **kw) -> None:
    x = np.sort(np.asarray(x, np.float64)[np.isfinite(x)])
    if len(x):
        ax.step(x, np.arange(1, len(x) + 1) / len(x), where="post", **kw)
