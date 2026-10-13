"""tap 空载优化与性能自报（M08-AC-212；M08-FR-223、FR-224；AWR-04 §4.4.5 第 1 条）。

- `state/sim-core/perf` 追加字段齐全（clock_profile、cpu_win_core、rtf_win、rate_set、cost_core_per_rate、n_uav、n_target、
  ray_rate_hz、rss_anon_mb、clock_lock）；`cost_core_per_rate` 为 `cpu_win_core / rtf_win` 的指数平均（系数 0.3），只在
  `rtf_win ≥ 0.2` 的窗口更新；
- sandbox100 下 tap 50 Hz；读者游标表连续 2 s【墙钟】无活动读者时降为 5 Hz，活动读者出现后下一次 tap 恢复全速；活动机体为 0
  且上一帧也为 0、roster 未变时不发布；d1_250 不启用（行为不变）。
"""

from __future__ import annotations

import pytest
from simlib import CoreHarness

from awr.sim.fleet.stages import registry as R
from awr.sim.runtime.perfwin import PerfWindow, rss_anon_mb


def test_perf_window_ema() -> None:
    pw = PerfWindow()
    pw.sample(0.0, 0, 0)
    assert pw.cost_core_per_rate is None
    pw.sample(0.12, 1_000_000_000, 1_000_000_000)  # 0.12 核、×1
    assert pw.cpu_win_core == pytest.approx(0.12) and pw.rtf_win == pytest.approx(1.0)
    assert pw.cost_core_per_rate == pytest.approx(0.12)
    pw.sample(0.12 + 0.6, 2_000_000_000, 6_000_000_000)  # 0.6 核、×5 → 0.12/倍速
    assert pw.cost_core_per_rate == pytest.approx(0.12)
    pw.sample(0.72 + 0.5, 3_000_000_000, 7_000_000_000)  # 0.5 核、×1 → 0.5
    assert pw.cost_core_per_rate == pytest.approx(0.12 + 0.3 * (0.5 - 0.12))
    c = pw.cost_core_per_rate
    pw.sample(1.22 + 0.05, 4_000_000_000, 7_100_000_000)  # rtf 0.1 < 0.2：不更新
    assert pw.cost_core_per_rate == c and pw.rtf_win == pytest.approx(0.1)
    pw.sample(1.27, 5_000_000_000, 0)  # 仿真时钟回退（重置）：rtf 记 0
    assert pw.rtf_win == 0.0
    v = rss_anon_mb()
    assert v is None or v > 0


def test_perf_fields_present() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=2, reg=reg, fleet_cfg={"clock_profile": "sandbox100"})
        try:
            h.core.perf_msg()
            h.advance(2.0)
            m = h.core.perf_msg()
            for k in ("clock_profile", "cpu_win_core", "rtf_win", "rate_set", "cost_core_per_rate", "n_uav", "n_target",
                      "ray_rate_hz", "rss_anon_mb", "clock_lock", "budget_total_core"):
                assert k in m, k
            assert m["clock_profile"] == "sandbox100" and m["n_uav"] == 2 and m["n_target"] == 0
            assert m["rate_set"] == 1.0 and m["clock_lock"] is None and m["budget_total_core"] <= 0.16
            assert m["rtf_win"] > 0
        finally:
            h.close()


def _tap_harness(n: int, profile: str) -> tuple[CoreHarness, list[int]]:
    h = CoreHarness(n=n, reg=None, fleet_cfg={"clock_profile": profile}, ready=n > 0)
    M = [10_000_000_000]
    h.core.fleet.tap.mono_ns = lambda: M[0]
    return h, M


def _run(h: CoreHarness, M: list[int], seconds: float, step_ns: int = 10_000_000) -> int:
    tap = h.core.fleet.tap
    p0 = tap.published
    end = h.W[0] + int(seconds * 1e9)
    while h.W[0] < end:
        h.W[0] += step_ns
        M[0] += step_ns
        h.core.iterate()
    return tap.published - p0


def test_tap_idle_rate_and_reader_resume() -> None:
    with R.isolated_registry():
        h, M = _tap_harness(1, "sandbox100")
        try:
            ring = h.ring
            cur = ring._cur
            cur["mode"][:] = 0
            n_full = _run(h, M, 1.0)
            assert 40 <= n_full <= 55  # 50 Hz（前 2 s 内尚未降频）
            _run(h, M, 1.5)  # 累计 > 2 s 无活动读者
            assert h.core.fleet.tap.idle
            n_idle = _run(h, M, 2.0)
            assert 8 <= n_idle <= 12, n_idle  # 5 Hz
            cur["mode"][0] = 1
            cur["heartbeat_ns"][0] = M[0]
            p0 = h.core.fleet.tap.published
            h.W[0] += 20_000_000
            M[0] += 20_000_000
            h.core.iterate()
            assert h.core.fleet.tap.published > p0 and not h.core.fleet.tap.idle  # 下一次 tap 即恢复
            cur["heartbeat_ns"][0] = M[0] + 10_000_000_000  # 读者持续活跃
            n_back = _run(h, M, 1.0)
            assert 40 <= n_back <= 55
        finally:
            h.close()


def test_tap_skips_empty_frames_and_d1_unchanged() -> None:
    with R.isolated_registry():
        h, M = _tap_harness(0, "sandbox100")
        try:
            h.ring._cur["mode"][:] = 1
            h.ring._cur["heartbeat_ns"][:] = M[0] + 100_000_000_000
            assert _run(h, M, 1.0) <= 1  # N = 0：只发一次空帧
            assert h.core.fleet.tap.skipped_empty >= 40
        finally:
            h.close()
    with R.isolated_registry():
        h, M = _tap_harness(1, "d1_250")
        try:
            h.ring._cur["mode"][:] = 0
            n = _run(h, M, 3.0, step_ns=4_000_000)
            assert n >= 3 * 120 and not h.core.fleet.tap.idle  # d1_250：125 Hz，不降频
        finally:
            h.close()
