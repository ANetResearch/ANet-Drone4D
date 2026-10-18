"""相遇检测器 `proximity_encounter`（M23 §6.9.1，r6 §6.1）：两名脚本行人均可见（状态 1 或 2）、水平距离 < `dist_m`，
持续 ≥ `min_dur_s`；区间为满足条件的连续帧 [t_k0, t_k1]，持续 = t_k1 − t_k0。显著性 min(1, 持续 / 20 s)（§6.9.2）。
"""

from __future__ import annotations

import itertools

import numpy as np

from awr.sim.events.verify.base import Context, Detection, rnd, runs
from awr.sim.events.verify.registry import detector

SALIENCE_FULL_S = 20.0


@detector("proximity_encounter", defaults={"dist_m": 3.0, "min_dur_s": 5.0})
def proximity_encounter(ctx: Context, p: dict) -> list[Detection]:
    out = []
    need_ms = round(float(p["min_dur_s"]) * 1000)
    for i, j in itertools.combinations(ctx.scripted("person"), 2):
        d = np.hypot(*(ctx.xy[:, i] - ctx.xy[:, j]).T)
        m = ctx.vis[:, i] & ctx.vis[:, j] & (d < float(p["dist_m"]))
        for k0, k1 in runs(m):
            t0, t1 = ctx.t_ms(k0), ctx.t_ms(k1)
            if t1 - t0 < need_ms:
                continue
            km = (k0 + k1) // 2
            c = (ctx.xy[km, i] + ctx.xy[km, j]) / 2.0
            out.append(Detection("proximity_encounter", tuple(sorted((ctx.ids[i], ctx.ids[j]))), (t0, t1),
                                 min(1.0, (t1 - t0) / (SALIENCE_FULL_S * 1000.0)), {"min_dist_m": rnd(d[k0:k1 + 1].min())},
                                 (float(c[0]), float(c[1]))))
    return out
