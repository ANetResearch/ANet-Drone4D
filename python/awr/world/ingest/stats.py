"""统计（M03-FR-017、O-6）：nnMedianM（全量 KD 树、固定种子 20 万点、第 2 近邻中位数）、分位数、类别直方图。"""

from __future__ import annotations

import numpy as np
from scipy.spatial import cKDTree


def nn_median(E: np.ndarray, sample: int = 200_000, seed: int = 1) -> float:
    n = len(E)
    if n < 2:
        return 1.0
    tree = cKDTree(E, leafsize=32, balanced_tree=False, compact_nodes=False)
    rng = np.random.default_rng(seed)
    idx = rng.choice(n, min(sample, n), replace=False)
    d, _ = tree.query(E[idx], k=2, workers=1)
    return float(np.median(d[:, 1]))


def percentiles2(v: np.ndarray, lo: float = 1, hi: float = 99, nd: int = 2) -> tuple[float, float]:
    a, b = np.percentile(v, [lo, hi])
    return round(float(a), nd), round(float(b), nd)


def class_histogram(cls: np.ndarray, n_classes: int = 16) -> dict[str, int]:
    h = np.bincount(np.asarray(cls, np.uint8), minlength=n_classes)
    return {str(i): int(v) for i, v in enumerate(h) if v}
