"""M03-AC-028：构建性能（perf 标记，经 ADR-033 性能运行协议执行：`make perf-world`）。

单城 build ≤ 60 s（门禁）；建树 5M 点 ≤ 7.0 s；tile 阶段 ≤ 12 s；峰值 RSS ≤ 4 GB。
"""

from __future__ import annotations

import time

import pytest
from m03_common import RAW, needs_raw

pytestmark = [pytest.mark.perf, pytest.mark.needs_data, needs_raw]


def test_single_city_build_budget(tmp_path):
    from awr.world.ingest.types import StageContext
    from awr.world.ingest.urbanscene3d import UrbanScene3DAdapter
    from awr.world.package.build import build_world

    t = time.perf_counter()
    res = build_world(UrbanScene3DAdapter("shenzhen", RAW), tmp_path, ctx=StageContext(quiet=True))
    total = time.perf_counter() - t
    assert res.exit_code == 0 and total <= 60.0
    st = {s["name"]: s for s in res.stages}
    assert st["tile"]["seconds"] <= 12.0
    assert max(s.get("peak_rss_mb", 0) for s in res.stages) <= 4096


def test_octree_build_budget():
    import numpy as np

    from awr.world.pointcloud.octree import build_octree, world_cube

    rng = np.random.default_rng(1)
    P = rng.random((5_000_000, 3)) * [2000, 2000, 400]
    cm, size = world_cube(P)
    ts = []
    for _ in range(3):
        t = time.perf_counter()
        build_octree(P, cm, size)
        ts.append(time.perf_counter() - t)
    assert sorted(ts)[1] <= 7.0
