"""共享视线缓存与射线预算（M18-FR-023；M18-M19 PRD §6.9；M19-NFR-002 的 M18 侧）：

- 令牌桶：任意一次 stage（tick）≤ 8 条、任意 1 s【仿真】窗口 ≤ 250 条（饱和请求下统计）；M18、M20 子配额各 25 条/s；
- 缓存：（5 m，2 m）三维格、1 s 有效；已算位复用（顶部位被 M18 算过后，M19 的全射线请求只补算中心与底部）；
- 预算不足：≤ 2 s 的旧值标 stale，否则 deferred；确定性（同一调用序列结果相同）；无世界几何时一律可见；遮挡与部分遮挡的 k_los。
"""

from __future__ import annotations

import numpy as np
import pytest
from tlib import FakeWorld

from awr.sim.targets import LosCache, shared_cache

TICK = 10_000_000  # sandbox100


def _pairs(n: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    r = np.random.default_rng(seed)
    A = np.c_[r.uniform(-800, 800, (n, 2)), r.uniform(40, 120, n)]
    B = np.c_[r.uniform(-800, 800, (n, 2)), np.zeros(n)]
    return A, B


def test_budget_per_tick_and_window() -> None:
    c = LosCache(FakeWorld())
    per_tick, rays_t = [], []
    for k in range(1, 501):            # 5 s @ 100 Hz，每 tick 都请求 10 对新的（不命中缓存）
        before = c.stats["rays"]
        A, B = _pairs(10, k)
        c.get_batch(A, B, np.full(10, 1.7), "M19", k * TICK, "all")
        per_tick.append(c.stats["rays"] - before)
        rays_t.append(k)
    per_tick = np.array(per_tick)
    assert per_tick.max() <= 8
    win = np.convolve(per_tick, np.ones(100, int), "valid")
    assert win.max() <= 250
    assert c.stats["deferred"] > 0


def test_sub_quotas() -> None:
    c = LosCache(FakeWorld())
    for k in range(1, 1001):           # 10 s：M18 只请求顶部射线
        A, B = _pairs(4, 1000 + k)
        c.get_batch(A, B, np.full(4, 1.7), "M18", k * TICK, "top")
        A, B = _pairs(4, 5000 + k)
        c.get_batch(A, B, np.full(4, 1.7), "M20", k * TICK, "all")
    assert c.rays_by_owner["M18"] <= 25 * 10 + 3
    assert c.rays_by_owner["M20"] <= 25 * 10 + 3


def test_cache_reuse_stale_deferred() -> None:
    w = FakeWorld(boxes=[(40.0, 45.0, -20.0, 20.0, 1.0)])   # 低矮墙：挡住底部射线，不挡顶部与中心
    c = LosCache(w, burst=8)
    a, b = np.array([0.0, 0.0, 1.0]), np.array([80.0, 0.0, 0.0])
    r1 = c.get(a, b, 1, "M18", TICK, "top", h_m=2.0)
    assert r1.k_los == 1.0 and not r1.deferred
    rays0 = c.stats["rays"]
    r3 = c.get(a, b, 3, "M19", 2 * TICK, "all", h_m=2.0)
    assert c.stats["rays"] - rays0 == 2                      # 只补算中心与底部
    assert r3.k_los == pytest.approx(1 / 3) or r3.k_los == pytest.approx(2 / 3)
    hit = c.get(a + 1.0, b + 0.5, 3, "M19", 3 * TICK, "all", h_m=2.0)   # 同一格：命中
    assert c.stats["hits"] >= 1 and hit.k_los == r3.k_los
    # 预算耗尽：1.5 s 后旧值 stale；2.5 s 后 deferred
    c.total.tokens = 0.0
    c.total.t_ns = int(1.5e9)
    st = c.get(a, b, 3, "M19", int(1.5e9), "all", h_m=2.0)
    assert st.stale and st.k_los == r3.k_los
    c.total.tokens = 0.0
    c.total.t_ns = int(2.5e9)
    df = c.get(a, b, 3, "M19", int(2.5e9), "all", h_m=2.0)
    assert df.deferred and df.k_los == 0.0


def test_no_world_and_determinism() -> None:
    c = LosCache(None)
    assert c.get(np.zeros(3), np.ones(3), 3, "M19", TICK, "all").k_los == 1.0
    out = []
    for _ in range(2):
        c = LosCache(FakeWorld(boxes=[(10, 20, -100, 100, 30)]))
        seq = []
        for k in range(1, 300):
            A, B = _pairs(6, k)
            seq.append([(r.k_los, r.stale, r.deferred) for r in c.get_batch(A, B, np.full(6, 1.5), "M19", k * TICK, "all")])
        out.append(seq)
    assert out[0] == out[1]


def test_shared_cache_fallback() -> None:
    assert isinstance(shared_cache(), LosCache)
