"""剧本开局屏障（ADR-068 第 4 条，ADR-073 第 7 条；M10 §6.4.1、M08 SimClock 内部保持）。

plan-pool 为 thread 模式（结果由后台线程在墙钟上完成）：设置期内每次提交即置 SimClock 内部保持，结果全部进入 inbox
后放行，由下一次 mission stage 统一生效。主循环每轮推进 1 个 tick（×1 的节奏）与每轮 50 个 tick（×10 的追帧批次）两种
驱动下，开局作业的生效 tick、任务开始与轨迹进入 TRANSIT 的仿真时刻逐 tick 相同；设置期在加载后 1 s【仿真】结束。"""

from __future__ import annotations

import time

import pytest
from harness import Sim
from test_scenario_loader import _tiny_doc

from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.mission import runtime as RT
from awr.world.geometry.fake import fake_world_query


def _run(world, ticks_per_iter: int) -> tuple[list, list, list]:
    sim = Sim(world, n=2)
    try:
        while not sim.rt.bound:  # 首个 mission stage 绑定 M10（创建 plan-pool）
            sim.W[0] += TICK_NS
            sim.core.iterate()
        sim.rt.load_scenario("s1-shenzhen-facade", doc=_tiny_doc())
        assert sim.rt.setup_active and sim.core.clock.per_tick
        held_iters = 0
        end = sim.core.clock.t_ns + 3_000_000_000
        deadline = time.monotonic() + 60.0
        while sim.core.clock.t_ns < end and time.monotonic() < deadline:
            sim.W[0] += ticks_per_iter * TICK_NS
            t0 = sim.core.clock.tick
            sim.core.iterate()
            if sim.core.clock.held and sim.core.clock.tick == t0:
                held_iters += 1
            time.sleep(0.004)  # 每轮约 4 ms 墙钟（plan-pool 线程在墙钟上完成作业）
        assert not sim.rt.setup_active and not sim.core.clock.per_tick  # 1 s【仿真】后设置期结束
        applied = [(round(e[0] / 0.004), e[1], e[2].get("mid")) for e in sim.events if e[1] in ("plan.warm", "plan.ready")]
        missions = [(round(e[0] / 0.004), e[2].get("mid"), e[2].get("to") or e[2].get("state")) for e in sim.kinds("mission.state")]
        tracks = [(round(e[0] / 0.004), e[2].get("vehicle_id"), (e[2].get("fields") or e[2]).get("to"))
                  for e in sim.kinds("track.state")]
        assert held_iters > 0  # 确有保持（作业在墙钟上完成）
        return applied, missions, tracks
    finally:
        sim.close()


@pytest.mark.ext
def test_start_barrier_makes_start_ticks_independent_of_batching(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """规划作业在墙钟上各需 40 ms：没有屏障时，每轮 1 tick 的驱动下结果在约 10 个 tick 后生效，每轮 50 tick 时在数百个
    tick 后生效（`AWR_SCENARIO_BARRIER=0` 时两者的任务与轨迹事件不同）；有屏障时逐 tick 相同。"""
    from awr.sim.planning import worker as WK

    slow = WK.worker_main

    def worker_main(req):
        time.sleep(0.04)
        return slow(req)

    monkeypatch.setattr(WK, "worker_main", worker_main)
    monkeypatch.setenv("AWR_PLAN_POOL", "thread")
    w = fake_world_query(tmp_path)
    a = _run(w, 1)
    b = _run(w, 50)
    assert a[0] and a[1] and a[2]  # 预热与 generator 结果的生效 tick、任务状态与轨迹状态
    assert a == b


def test_setup_window_constant() -> None:
    assert RT.SETUP_WINDOW_NS == 1_000_000_000 and RT.SETUP_HOLD_TIMEOUT_S == 10.0
