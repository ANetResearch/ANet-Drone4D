"""识别物 stage 预算（M18-AC-022；M18-NFR-001、NFR-002；perf 标记：按 ADR-033 运行协议在 MS5 执行，并行阶段不运行）。

30 个识别物 × 12 机（geometric，含 2 个 physical）在 sandbox100 下 targets stage ≤ 3 ms/仿真秒、单次 ≤ 0.3 ms；有效半径缓存 ≤ 256 条。

计时口径（D3-AC-01 回归门第 3 条，封板任务 G04）：stage 成本以本线程 CPU 时间（`time.thread_time_ns`）计，主机负载下的
抢占与排队等待不计入，同一主机上多次运行的差异约 1%；墙钟时间只记录（`*_wall_*`）。全部测量值经 `record_property` 写入
用例报告，回归门的 `perf` 步骤据此与 D2 基线（`tests/d3/regress_gate/baseline.json` 的 `perf` 节，dmax CPU 池无负载窗口
录制）比较，差 ≤ 5%。台架、步数与预算阈值与 D2 相同。
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from tlib import Bench, FakeWorld

REGION = [[-300.0, -300.0], [300.0, -300.0], [300.0, 300.0], [-300.0, 300.0]]
STAGES_PER_SIM_S = 10            # sandbox100：targets stage 10 Hz（Bench.step 每次推进 0.1 s【仿真】）


def measure_stage(steps: int = 600, warmup: int = 50) -> dict[str, float]:
    """M18-AC-022 台架：30 个识别物 × 12 机，预热 `warmup` 次后逐次计时 `steps` 次 targets stage。返回
    `stage_cpu_ms_per_sim_s`（CPU 均值 × 10 Hz）、`step_cpu_p99_ms`、对应的墙钟值与有效半径缓存条数 `eff_cache`。"""
    with Bench(12, FakeWorld()) as b:
        for k in range(30):
            cls = ("person", "dog", "vehicle")[k % 3]
            extra = {"motion": {"mode": "random_walk", "region": REGION}} if cls != "vehicle" else {}
            if k < 2:
                extra["criterion_mode"] = "physical"
            b.add({"class": cls, "pos": [k * 8.0 - 120.0, (k % 5) * 20.0 - 40.0], **extra})
        for i in range(12):
            b.S.fly(i, [i * 50.0 - 300.0, 1000.0, 60.0])
        b.step(warmup)
        cpu = np.empty(steps)
        wall = np.empty(steps)
        for k in range(steps):
            c0, w0 = time.thread_time_ns(), time.perf_counter_ns()
            b.step(1)
            cpu[k] = time.thread_time_ns() - c0
            wall[k] = time.perf_counter_ns() - w0
        eff = len(b.rt.eff.d)
    return {"stage_cpu_ms_per_sim_s": round(float(cpu.mean()) * STAGES_PER_SIM_S / 1e6, 4),
            "step_cpu_p99_ms": round(float(np.percentile(cpu, 99)) / 1e6, 4),
            "stage_wall_ms_per_sim_s": round(float(wall.mean()) * STAGES_PER_SIM_S / 1e6, 4),
            "step_wall_p99_ms": round(float(np.percentile(wall, 99)) / 1e6, 4),
            "eff_cache": float(eff)}


def test_measure_stage_reports_all_metrics() -> None:
    """台架自检（常规运行，不计时判定）：指标齐全、CPU 时间不超过墙钟（同一线程），有效半径缓存不超过 256 条。"""
    m = measure_stage(steps=20, warmup=5)
    assert set(m) == {"stage_cpu_ms_per_sim_s", "step_cpu_p99_ms", "stage_wall_ms_per_sim_s", "step_wall_p99_ms", "eff_cache"}
    assert 0.0 < m["stage_cpu_ms_per_sim_s"] <= m["stage_wall_ms_per_sim_s"] * 1.01 + 1e-3
    assert m["eff_cache"] <= 256


@pytest.mark.perf
def test_stage_budget_30x12(record_property) -> None:
    m = measure_stage()
    for k, v in m.items():
        record_property(k, v)
    assert m["stage_cpu_ms_per_sim_s"] <= 3.0, f"{m['stage_cpu_ms_per_sim_s']:.2f} ms/sim-s (CPU)"
    assert m["step_cpu_p99_ms"] <= 0.3, f"p99 {m['step_cpu_p99_ms']:.3f} ms (CPU)"
    assert m["eff_cache"] <= 256
