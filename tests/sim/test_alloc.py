"""资源（M08-AC-037；M08-NFR-009 至 NFR-011）。全部为 perf 用例（执行 ADR-033 性能运行协议，并行阶段不跑）：

- 稳态每 1000 tick Python 堆净增长 ≤ 64 KB（tracemalloc，N = 200，悬停 + 导航混合）；
- gc gen2 手动回收停顿 p99 ≤ 2 ms（gc.freeze 之后）；
- N = 1000 RSS ≤ 400 MB。
S1 + 200 架 30 min 的 RSS 增长属 M16 soak。
"""

from __future__ import annotations

import gc
import resource
import time
import tracemalloc

import numpy as np
import pytest
from simlib import CoreHarness

from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import registry as R

pytestmark = pytest.mark.perf


def _run(h: CoreHarness, ticks: int) -> None:
    end = h.core.clock.tick + ticks
    while h.core.clock.tick < end:
        h.W[0] += 2 * TICK_NS
        h.core.iterate()


def test_heap_growth_per_1000_ticks() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=200, reg=reg, spacing=12.0)
        try:
            h.cmd("takeoff", {"alt_m": 10.0}, uav="*")
            _run(h, 5000)
            gc.collect()
            tracemalloc.start()
            _run(h, 1000)
            s0 = tracemalloc.take_snapshot()
            _run(h, 5000)
            s1 = tracemalloc.take_snapshot()
            tracemalloc.stop()
            grow = sum(st.size_diff for st in s1.compare_to(s0, "filename"))
            assert grow / 5.0 <= 64 * 1024, grow
        finally:
            h.close()


def test_gc_gen2_pause_p99() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=200, reg=reg, spacing=12.0)
        try:
            _run(h, 1000)
            gc.freeze()
            ts = []
            for _ in range(50):
                _run(h, 50)
                t0 = time.perf_counter()
                gc.collect(2)
                ts.append((time.perf_counter() - t0) * 1e3)
            assert float(np.percentile(ts, 99)) <= 2.0
        finally:
            h.close()


def test_rss_n1000() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1000, reg=reg, spacing=8.0, ready=False)
        try:
            _run(h, 500)
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
            assert rss_mb <= 400.0, rss_mb
        finally:
            h.close()
