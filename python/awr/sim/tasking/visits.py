"""区域巡查的访问栅格（M20-FR-013：10 m 格的最近访问时刻、每格重访间隔 p95；M20-AC-006）。"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from awr.tasking.geom import point_in_polygon, ring

__all__ = ["VisitGrid"]

CELL_M = 10.0
THETA_FOOT_RAD = math.radians(15.0)
MAX_GAPS = 8192


class VisitGrid:
    def __init__(self, poly: np.ndarray, cell_m: float = CELL_M) -> None:
        P = ring(poly)
        lo, hi = P.min(axis=0), P.max(axis=0)
        xs = np.arange(lo[0] + cell_m / 2, hi[0], cell_m)
        ys = np.arange(lo[1] + cell_m / 2, hi[1], cell_m)
        X, Y = np.meshgrid(xs, ys)
        xy = np.c_[X.ravel(), Y.ravel()]
        m = point_in_polygon(xy, P)
        self.xy = xy[m] if m.any() else P[:1].copy()
        self.last = np.full(len(self.xy), np.nan)
        self.gaps: list[float] = []

    def stamp(self, rt: Any, T: Any, t: float) -> None:
        tids = {T.tid, *T.children}
        for so in rt.sorties.values():
            if so.tid not in tids or so.state not in ("WORKING", "TRANSIT", "STANDBY"):
                continue
            p = rt.pos(so.uav)
            h = max(float(p[2] - rt.dtm(p[None, :2])[0]), 5.0)
            r = max(h * math.tan(THETA_FOOT_RAD), CELL_M / 2)
            d = np.hypot(self.xy[:, 0] - p[0], self.xy[:, 1] - p[1])
            hit = d <= r
            if not hit.any():
                continue
            prev = self.last[hit]
            g = t - prev[np.isfinite(prev)]
            g = g[g > 1.5]
            if g.size:
                self.gaps.extend(float(x) for x in g)
                if len(self.gaps) > MAX_GAPS:
                    del self.gaps[: len(self.gaps) - MAX_GAPS]
            self.last[hit] = t

    def coverage_pct(self) -> float:
        return 100.0 * float(np.isfinite(self.last).mean()) if len(self.last) else 0.0

    def revisit_p95(self) -> float | None:
        return float(np.percentile(self.gaps, 95)) if self.gaps else None
