"""M13-AC-027 ②③：单次耗时微基准（N = 1000 合成 FleetState，兴趣集 80 架）与状态块内存（-m perf，只在独占锁下运行）。

`pytest tests/sensors/bench_stage.py -m perf`：阶梯 stage p99 ≤ 200 µs；S1（2 个跟踪云台）p99 ≤ 250 µs；S3 检测 tick
（N ≤ 50、16 对，LOS 用替身）p99 ≤ 1.3 ms；打包每片 p99 ≤ 0.2 ms；白噪声观测 p99 ≤ 0.3 ms；sensors 块 ≤ 1 MB。
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from awr.sim.sensors.block import block_bytes

pytestmark = pytest.mark.perf


def p99_us(fn, n: int = 2000, warm: int = 100) -> float:
    for _ in range(warm):
        fn()
    ts = np.empty(n)
    for i in range(n):
        t0 = time.perf_counter_ns()
        fn()
        ts[i] = (time.perf_counter_ns() - t0) / 1e3
    return float(np.percentile(ts, 99))


def ladder(bench_factory, n=1000, interest=80):
    b = bench_factory(capacity=1024)
    for s in range(n):
        b.spawn(s, s + 1, pos=(s % 40 * 5.0, s // 40 * 5.0, 30.0))
    b.interest(range(1, interest + 1))
    b.slow_pose = b.slow_obs = False
    b.run(0.1)
    return b


def stage_call(b):
    def f():
        b.tick += 5
        b.ctx.tick = b.tick - (b.tick - 2) % 5
        b.ctx.t_ns = b.ctx.tick * 4_000_000
        b.stage.fn(b.S, b.ctx)
    return f


def test_ladder_stage_p99(bench_factory):
    b = ladder(bench_factory)
    assert p99_us(stage_call(b)) <= 200.0


def test_s1_two_tracking_gimbals(bench_factory):
    b = ladder(bench_factory, n=2, interest=2)
    for s in range(2):
        b.rt.set_mode(s, "camera", "look_at_axis", {"center_enu_m": [50.0, 50.0, 0.0]})
    assert p99_us(stage_call(b)) <= 250.0


def test_pack_slice_and_noisy_obs(bench_factory):
    b = ladder(bench_factory)
    out = np.zeros(32, b.rt.packer.out.dtype)
    sl = b.rt.interest_slots(b.ctx)[:16]
    assert p99_us(lambda: b.rt.packer.pack(sl, out)) <= 200.0
    assert p99_us(lambda: b.rt.obs.run_slow(b.ctx), n=500) <= 300.0


def test_s3_detection_tick(bench_factory):
    b = bench_factory(capacity=64)
    for s in range(50):
        b.spawn(s, s + 1, pos=(s * 3.0, 0.0, 60.0))
    b.run(0.02)
    for s in range(50):
        b.rt.set_default_for(s, "lawnmower", {})
    for i in range(3):
        b.rt.spawn_target(f"t{i}", [i * 20.0, 0.0, 0.0], "person")
    b.run(1.5)
    tgt = b.rt.detector.targets
    snap = tgt.checkpoint()

    def f():
        tgt.restore(snap)
        b.rt.detector.tick(b.S, b.ctx)
    assert p99_us(f, n=300) <= 1300.0


def test_block_memory():
    assert block_bytes(1024) <= 1 << 20
