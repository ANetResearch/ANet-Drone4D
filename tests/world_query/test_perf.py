"""M04-AC-012、AC-021：进程内查询性能预算（M04 §5.1；perf 标记，经 ADR-033 性能运行协议执行：`make bench-geo` 或
`pytest -m perf tests/world_query`）。"""

from __future__ import annotations

import time

import numpy as np
import pytest
from geo_m04_common import CITIES, needs_worlds

pytestmark = [pytest.mark.perf, pytest.mark.needs_data, needs_worlds]


def _t(fn, rep=50):
    ts = []
    for _ in range(rep):
        s = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - s) * 1e3)
    return float(np.median(ts)), float(np.percentile(ts, 99))


@pytest.mark.parametrize("city", CITIES)
def test_budgets(city_wq, city):
    wq = city_wq(city)
    b = wq.bounds_m
    rng = np.random.default_rng(1)
    xy = np.c_[rng.uniform(b[0, 0] + 50, b[1, 0] - 50, 10_000), rng.uniform(b[0, 1] + 50, b[1, 1] - 50, 10_000)]
    for _ in range(3):
        wq.height_dsm(xy)                     # 预热页缓存
    assert _t(lambda: wq.height_dsm(xy[:1000]), 200)[0] <= 0.15
    assert _t(lambda: wq.ground_dtm(xy[:1000]), 200)[0] <= 0.25
    xyz = np.c_[xy[:1000], rng.uniform(20, 200, 1000)]
    assert _t(lambda: wq.clearance(xyz, 0.0), 200)[0] <= 0.15
    assert _t(lambda: wq.clearance(xyz[:64], 10.0), 100)[0] <= 1.0
    o = np.r_[xy[0], 300.0]
    d = np.array([0.3, 0.4, -0.866])
    d /= np.linalg.norm(d)
    assert _t(lambda: wq.ray_hit(o, d, 5000.0), 100)[1] <= 2.0
    P = np.c_[np.linspace(b[0, 0] + 60, b[1, 0] - 60, 1000), np.linspace(b[0, 1] + 60, b[1, 1] - 60, 1000),
              np.full(1000, float(wq.qa["dsm_max_m"]) + 20)]
    seg = np.linalg.norm(np.diff(P, axis=0), axis=1)
    P = P[: int(np.searchsorted(np.cumsum(seg), 19_000)) + 1]
    assert _t(lambda: wq.path_coarse_check(P, goal_clear_m=0.0), 20)[0] <= 2.0
    assert _t(lambda: wq.path_valid(P), 5)[0] <= 80.0


def test_hot_open_time():
    from geo_m04_common import WORLDS

    from awr.world.geometry import open_world_query

    ts = []
    for _ in range(10):
        s = time.perf_counter()
        open_world_query(WORLDS / "sanfrancisco", WORLDS / ".geo-cache", allow_derive=False)
        ts.append(time.perf_counter() - s)
    assert np.percentile(ts, 95) <= 0.3


_MEM_PROBE = r"""
import re, sys
from pathlib import Path
import numpy as np
from awr.world.geometry import open_world_query
import awr.world.geometry.path, awr.world.geometry.probe

def rss():
    return int(re.search(r"VmRSS:\s+(\d+)", Path("/proc/self/status").read_text()).group(1)) / 1024

r0 = rss()
wq = open_world_query(Path(sys.argv[1]), Path(sys.argv[2]), allow_derive=False)
r_open = rss() - r0
b = wq.bounds_m
rng = np.random.default_rng(0)
xy = np.c_[rng.uniform(b[0, 0], b[1, 0], 200000), rng.uniform(b[0, 1], b[1, 1], 200000)]
wq.height_dsm(xy); wq.ground_dtm(xy); wq.agl(np.c_[xy, np.full(len(xy), 50.0)])
wq.clearance(np.c_[xy[:20000], np.full(20000, 50.0)], 5.0)
for i in range(300):
    wq.ray_hit(np.array([*xy[i], 300.0]), np.array([0.6, 0.0, -0.8]), 5000.0)
    wq.path_coarse_check(np.array([[*xy[i], 120.0], [*xy[i + 1], 120.0]]), goal_clear_m=0.0)
print(f"{r_open:.1f} {rss() - r0:.1f}")
"""


def test_memory():
    """M04-AC-021（sim-core 口径）：打开旧金山并执行 20 万点高度、AGL、2 万点净空、300 次 ray_hit 与粗校验后 RSS 增量
    ≤ 200 MB（派生数组经 memmap 共享页缓存；plan-pool 的 inflated_1m 不在 sim-core 触及）。"""
    import subprocess
    import sys

    from geo_m04_common import WORLDS

    out = subprocess.run([sys.executable, "-c", _MEM_PROBE, str(WORLDS / "sanfrancisco"), str(WORLDS / ".geo-cache")],
                         capture_output=True, text=True, check=True).stdout.split()
    r_open, r_used = float(out[0]), float(out[1])
    assert r_open <= 50.0 and r_used * 2**20 <= 200e6, (r_open, r_used)
