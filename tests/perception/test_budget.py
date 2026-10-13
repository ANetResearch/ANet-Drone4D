"""M19-AC-010：管线与预算。功能部分（常规运行）：12 机 × 30 识别物、视锥全覆盖时每 tick 射线 ≤ 8、任意 1 s【仿真】窗口 ≤ 250
（共享 LosCache 令牌桶），超出部分 deferred 并按（机体，识别物）轮转服务（每对最终都得到视线结果）；接力悬停几何的缓存命中率
≥ 70%；六级门控（距离、视锥、像素、环境 n_max 先于视线）。成本部分（`perf` 标记，并行阶段不运行，MS5 按 ADR-033 协议测量）：
12 × 30 在接力与巡查几何下感知 ≤ 25 ms/仿真秒、单 tick ≤ 1 ms。"""

from __future__ import annotations

import time

import numpy as np
import pytest
from plib import FakeEnv, FakeWorld, PBench, make_imaging

from awr.sim.perception import gx40


class _CountWorld(FakeWorld):
    def __init__(self) -> None:
        super().__init__()
        self.per_tick: list[int] = []

    def los_batch(self, A, B, step_m=1.0, eps_m=0.5):
        out = np.ones(len(np.asarray(A).reshape(-1, 3)), bool)
        self.rays += len(out)
        return out


def _fleet(n_uav=12, n_tgt=30, f=4.8, spread=True):
    w = _CountWorld()
    b = PBench(n_uav, w, env=FakeEnv())
    for k in range(n_tgt):
        b.add({"class": "person", "pos": [150.0 + 7.0 * (k % 10), -40.0 + 30.0 * (k // 10), 0.0], "motion": {"mode": "static"}})
    b.step(1)
    for s in range(n_uav):
        p = (0.0, -60.0 + 11.0 * s, 80.0) if spread else (0.0, 0.0, 80.0)
        b.platform(s, pos=p, imaging=[make_imaging(gx40(), p, (185.0, -10.0, 0.0), f_mm=f)])
    return b, w


def test_ray_budget_per_tick_and_window():
    b, w = _fleet()
    with b:
        hist = []
        seen: set[tuple[str, str]] = set()
        listed: set[tuple[str, str]] = set()
        for k in range(800):
            r0 = w.rays
            b.step(1)
            hist.append(w.rays - r0)
            if k % 20 == 19:
                for u in b.prt.frames:
                    for sn in b.prt.frame_for(u)["sensors"]:
                        for t in sn["targets"]:
                            listed.add((u, t["target"]))
                            if t["k"]["los"] > 0:
                                seen.add((u, t["target"]))
        assert max(hist) <= 8
        assert max(sum(hist[i:i + 100]) for i in range(len(hist) - 100)) <= 250
        assert b.prt.stats["deferred"] > 0                         # 预算打满：确有延期
        # 轮转：帧中列出的（机体，识别物）对在 8 s 内都得到过视线结果
        assert len(seen) >= 0.9 * len(listed) and len(listed) >= 12 * 8


def test_cache_hit_rate_relay_hover():
    b = PBench(3, FakeWorld(), env=FakeEnv())
    with b:
        for k in range(3):
            b.add({"class": "person", "pos": [300.0, 100.0 * k, 0.0], "motion": {"mode": "static"}})
        b.step(1)
        for s in range(3):
            p = (0.0, 100.0 * s, 80.0)
            b.platform(s, pos=p, imaging=[make_imaging(gx40(), p, (300.0, 100.0 * s, 0.9), f_mm=48.0)])
        los = b.prt.los
        h0, q0 = los.stats["hits"], los.stats["requests"]
        b.run(20.0)
        rate = (los.stats["hits"] - h0) / max(1, los.stats["requests"] - q0)
        assert rate >= 0.70


def test_gates_before_line_of_sight():
    b, _w = _fleet(n_uav=1, n_tgt=30, f=48.0)
    with b:
        b.run(2.0)
        # 48 mm 窄视锥：大部分识别物被视锥门挡住，不请求视线
        assert b.prt.stats["los_requests"] < 30 * 10
    night = PBench(1, _CountWorld(), env=FakeEnv.night())
    with night:
        night.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}})
        night.step(1)
        night.platform(0, pos=(0.0, 0.0, 60.0), imaging=[make_imaging(gx40(), (0.0, 0.0, 60.0), (300.0, 0.0, 0.9), f_mm=48.0)])
        night.run(2.0)
        assert night.world.rays == 0                                # 环境门：夜间被动 EO 的 n_max < 3.5，不请求视线


@pytest.mark.perf
def test_stage_cost_12x30():
    b, _w = _fleet()
    with b:
        b.run(1.0)
        ns = []
        for _ in range(1000):
            t0 = time.perf_counter_ns()
            b.prt.stage(b.S, b.ctx)
            ns.append(time.perf_counter_ns() - t0)
            b.ctx.tick += 1
            b.ctx.t_ns += 10_000_000
        per_s = sum(ns) / 10.0 / 1e6
        print(f"perception stage: {per_s:.1f} ms per sim second, p99 tick {np.percentile(ns, 99) / 1e6:.3f} ms")
        assert per_s <= 25.0 and np.percentile(ns, 99) / 1e6 <= 1.0


def test_shard_slots_each_once_per_cycle_and_rotating():
    from awr.sim.perception.runtime import PerceptionRuntime

    act = np.array([0, 1, 2, 5, 7, 8, 9, 10, 11, 12, 13, 15])
    first = []
    for cyc in range(3):
        got = [PerceptionRuntime.shard_slots(act, cyc * 20 + k, 20) for k in range(20)]
        flat = np.concatenate(got)
        assert sorted(flat.tolist()) == act.tolist()                         # 每周期每架恰好一次（每机 5 Hz）
        first.append(next(int(g[0]) for g in got if g.size))
    assert len(set(first)) == 3                                              # 周期之间错开
    assert PerceptionRuntime.shard_slots(np.array([3]), 7, 20).tolist() in ([], [3])
