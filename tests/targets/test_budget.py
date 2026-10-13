"""识别物 stage 预算（M18-AC-022；M18-NFR-001、NFR-002；perf 标记：按 ADR-033 运行协议在 MS5 执行，并行阶段不运行）。

30 个识别物 × 12 机（geometric，含 2 个 physical）在 sandbox100 下 targets stage ≤ 3 ms/仿真秒、单次 ≤ 0.3 ms；有效半径缓存 ≤ 256 条。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from tlib import Bench, FakeWorld

REGION = [[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]]


@pytest.mark.perf
def test_stage_budget_30x12() -> None:
    with Bench(12, FakeWorld()) as b:
        for k in range(30):
            cls = ("person", "dog", "vehicle")[k % 3]
            extra = {"motion": {"mode": "random_walk", "region": REGION}} if cls != "vehicle" else {}
            if k < 2:
                extra["criterion_mode"] = "physical"
            b.add({"class": cls, "pos": [k * 8.0 - 120.0, (k % 5) * 20.0 - 40.0], **extra})
        for i in range(12):
            b.S.fly(i, [i * 50.0 - 300.0, 1000.0, 60.0])
        b.step(50)
        ts = []
        for _ in range(600):
            t0 = time.perf_counter()
            b.step(1)
            ts.append(time.perf_counter() - t0)
        ts = np.array(ts)
        assert ts.mean() * 10 * 1e3 <= 3.0, f"{ts.mean() * 1e4:.2f} ms/sim-s"
        assert np.percentile(ts, 99) * 1e3 <= 0.3
        assert len(b.rt.eff.d) <= 256
