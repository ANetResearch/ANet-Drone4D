"""SlowTasks.reset_sim_clock（m23 修复，缺省不启用）：仿真周期任务在时钟归零后立即重新到期，墙钟周期任务不受影响。"""

from __future__ import annotations

from awr.sim.runtime.slow import SlowTask, SlowTasks


def test_reset_sim_clock_rearms_sim_period_tasks() -> None:
    sl = SlowTasks(lambda: 0)
    sim = sl.add(SlowTask("sim", lambda _b: None, period_sim_ns=1_000_000_000))
    wall = sl.add(SlowTask("wall", lambda _b: None, period_wall_ns=1_000_000_000))
    sim.last_sim, wall.last_wall = 50_000_000_000, 7
    assert not sl._due(sim, 0, 0)              # 归零后仍等待上一纪元的时刻（缺省行为）
    sl.reset_sim_clock()
    assert sl._due(sim, 0, 0) and sim.due_since == -1
    assert wall.last_wall == 7 and wall.last_sim == -(1 << 62)
