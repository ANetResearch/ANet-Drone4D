"""D2.2-FX-world：`tools/sim/_engine_check.py` `observe()` 的纯函数回归（不起真实引擎，注入假时钟与假状态源）。

复现背景：原实现一旦全部任务离开 `PLANNING` 就立即退出观察循环，挡不住 `relay_watch` 这类任务先进入
`RUNNING`/`READY`、几个 tick 之后才因站位搜索失败转入 `INFEASIBLE`（终态）的情况——shenzhen、newyork 的
`relay_watch` 因此从未被这道闸挡住，固化进了对外展示的录制内容（见 `docs/impl/D2.2-R-world-报告.md` 第 3 节、
`tools/sim/_engine_check.py` 模块说明）。这里用假时钟直接复现“旧逻辑会提前退出、新逻辑不会”的时间线，不依赖真实
引擎，运行在毫秒级。"""

from __future__ import annotations

from tools.sim._engine_check import TERMINAL_BAD, observe


class FakeClock:
    """假的墙钟：`sleep(dt)` 直接前进 `now()`，测试里不会真的等待。"""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


def test_observe_waits_the_full_window_even_if_states_leave_planning_immediately() -> None:
    """旧逻辑：全部任务一离开 PLANNING 就退出，不会等满 `seconds`。新逻辑必须真的等满。"""
    clock = FakeClock()

    def get_states() -> dict[str, str]:
        return {"t-1": "RUNNING"}  # 从第一次采样起就已经离开 PLANNING

    observe(get_states, seconds=2.0, sleep=clock.sleep, now=clock.now)
    assert clock.now() >= 2.0


def test_observe_catches_infeasible_that_appears_after_leaving_planning() -> None:
    """relay_watch 的真实时间线复现：0 s 仍在 PLANNING；0.2 s 离开 PLANNING 转 RUNNING（旧逻辑在这一刻就退出，把
    t-2 误判为可行）；1.0 s 站位作业回调到达，t-2 才转为 INFEASIBLE（终态）。新逻辑必须等到 1.0 s 之后的采样才会
    看到，且不能因为在 0.2 s 已经“看起来都不是 PLANNING”就提前返回。"""
    clock = FakeClock()

    def get_states() -> dict[str, str]:
        t = clock.now()
        if t < 0.2:
            return {"t-1": "RUNNING", "t-2": "PLANNING"}
        if t < 1.0:
            return {"t-1": "RUNNING", "t-2": "RUNNING"}
        return {"t-1": "RUNNING", "t-2": "INFEASIBLE"}

    states = observe(get_states, seconds=2.0, sleep=clock.sleep, now=clock.now)
    assert states["t-2"] == "INFEASIBLE"
    assert states["t-1"] == "RUNNING"
    assert clock.now() >= 2.0


def test_observe_preserves_terminal_state_even_if_a_later_sample_disagrees() -> None:
    """终态在真实引擎里不会恢复（`python/awr/sim/tasking/book.py` 把 INFEASIBLE 列为终态），但 `observe()` 自身
    对此做了防御：窗口内任意一次采样见过终态就保留，不被末次采样覆盖掉（防的是采样恰好落在两次 tick 回调之间）。"""
    clock = FakeClock()
    seen = {"n": 0}

    def get_states() -> dict[str, str]:
        seen["n"] += 1
        if seen["n"] == 2:
            return {"t-1": "FAILED"}
        return {"t-1": "RUNNING"}

    states = observe(get_states, seconds=1.0, sleep=clock.sleep, now=clock.now)
    assert states["t-1"] == "FAILED"


def test_observe_handles_engine_not_ready_without_crashing() -> None:
    """引擎尚未就绪时 `get_states()` 可能返回 `{}`（`_engine_check.main()` 里 `tasking_runtime()` 返回 None 的情况）；
    不应因此崩溃，窗口结束时按最后一次非空采样给出结果。"""
    clock = FakeClock()

    def get_states() -> dict[str, str]:
        return {} if clock.now() < 0.5 else {"t-1": "DONE"}

    states = observe(get_states, seconds=1.0, sleep=clock.sleep, now=clock.now)
    assert states == {"t-1": "DONE"}


def test_terminal_bad_matches_the_infeasible_filter_used_by_main() -> None:
    assert set(TERMINAL_BAD) == {"INFEASIBLE", "FAILED", "CANCELED"}
