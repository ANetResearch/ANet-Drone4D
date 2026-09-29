#!/usr/bin/env python3
"""CdA 与转子阻力标定（g08 `calib_g08.py` 转正；V0.4 辨识工具，M08 §6.7.2 Propeller 行）。

在 FleetSim（L1，oracle 或 numba）中让机体在给定风速下悬停，二分 CdA 使稳态俯仰等于目标值（例如 x500 在 8 m/s 风中
−7.85°，SIH 黄金数据 inst2）；`--c-rd` 固定转子阻力系数。输出标定值与各风速下的倾角表（4/8/12/14 m/s）。
用法：python tools/vehicles/calib_cda.py --profile x500 --wind 8 --target-pitch-deg -7.85
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: F401  （把 python/ 加入 sys.path）

from awr.sim.backends.base import EntitySpec, Kind
from awr.sim.fleet import actions as ACT
from awr.sim.fleet.fleet import FleetConfig, FleetSim
from awr.sim.fleet.pipeline import StageCtx
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R


def hover_pitch_deg(profile_id: str, cda: float, wind_mps: float, *, settle_s: float = 20.0, kernel: str = "numpy") -> float:
    base = ProfileTable()
    p = replace(base.get(profile_id), cda_m2=cda, aero="composite")
    T = ProfileTable({profile_id: p}, base.limits)
    with R.isolated_registry() as reg:
        f = FleetSim(FleetConfig(kernel=kernel, path_capacity=16), profiles=T, reg=reg)
        f.build_pipeline()
        f.add(EntitySpec("c", Kind.UAV, profile_id, None, (0.0, 0.0, 0.0), math.pi / 2), slot=0, agent_no=0, entity_id="c")
        S = f.S
        S.lifecycle[0] = 4
        S.p[0, 2] = -10.0
        S.landed[0] = False
        S.in_air[0] = True
        S.thrust[0] = T.hover[0]
        ACT.set_offboard(S, [0], S.p[0].copy(), 0.0)
        S.set_wind_from_enu(np.array([[0.0, -wind_mps, 0.0]]), np.array([0]))
        ctx = StageCtx(profiles=T, paths=f.PB)
        f.step(ctx, int(settle_s * 250))
        w, x, y, z = S.q[0]
        return math.degrees(math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x)))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="x500")
    ap.add_argument("--wind", type=float, default=8.0)
    ap.add_argument("--target-pitch-deg", type=float, default=-7.85)
    ap.add_argument("--iters", type=int, default=16)
    a = ap.parse_args(argv)
    lo, hi = 0.0, 0.08
    for _ in range(a.iters):
        mid = 0.5 * (lo + hi)
        if hover_pitch_deg(a.profile, mid, a.wind) > a.target_pitch_deg:
            lo = mid
        else:
            hi = mid
    cda = 0.5 * (lo + hi)
    table = {w: round(hover_pitch_deg(a.profile, cda, w), 3) for w in (4.0, 8.0, 12.0, 14.0)}
    print(json.dumps({"profile": a.profile, "cda_m2": round(cda, 6), "pitch_deg": table}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
