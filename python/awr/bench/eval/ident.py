"""识别表 I[k] = {(uav, actor)}（M27-FR-087）：L1 为已交付检测的真值映射满足 h_px_unocc ≥ 16 且 5 点可见比例 ≥ 0.5。

L1 的检测高度 `h_px` 已乘可见比例，识别谓词与 L2 的 `h_px_unocc` 同口径，取真值侧车记录的无遮挡投影高度。
"""

from __future__ import annotations

from collections import defaultdict

from awr.bench.eval.thresholds import TH, Thresholds

__all__ = ["build_ident"]


def build_ident(run, th: Thresholds = TH) -> dict[int, set[tuple[str, str]]]:
    ident: dict[int, set[tuple[str, str]]] = defaultdict(set)
    for r in run.l1:
        if r["h_unocc_px"] >= th.h_px and r["vis"] >= th.vis:
            ident[int(r["seq"])].add((r["uav"], r["actor"]))
    return ident
