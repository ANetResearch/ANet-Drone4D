"""M25 Worker 渲染进程组 `awr.sensorsvc.procgroup`（ADR-214 决策 (4)）：调度单元的固定分派、spawn 进程的请求与回复、多通道并发、
处理器异常与构造失败（648）、跨进程时刻可比与关闭。不需要 GPU（处理器为本文件中的替身）。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from awr.sensorsvc.errors import SensorSvcError
from awr.sensorsvc.procgroup import (
    RENDER_PROCS_MAX,
    RenderGroup,
    Unit,
    assign_units,
    load_target,
    proc_units,
    shared_clock,
)

HERE = Path(__file__).resolve()


class EchoProc:
    """渲染进程替身：记录收到的请求序列（逐会话），`("boom",)` 抛异常，`("clock",)` 返回本进程的 perf_counter。"""

    def __init__(self, arg: dict, index: int) -> None:
        if arg.get("fail"):
            raise RuntimeError("world load failed")
        self.arg, self.index = arg, index
        self.seen: dict[int, list[int]] = {}

    def info(self) -> dict:
        return {"tag": self.arg.get("tag")}

    def handle(self, msg: tuple):
        op = msg[0]
        if op == "boom":
            raise ValueError("bad request")
        if op == "clock":
            return time.perf_counter()
        if op == "tick":
            _, s, seq = msg
            time.sleep(0.002)
            self.seen.setdefault(int(s), []).append(int(seq))
            return (self.index, os.getpid(), int(s), int(seq))
        return ("echo", self.index, msg)

    def close(self) -> dict:
        return {"index": self.index, "seen": self.seen}


TARGET = f"{HERE}:EchoProc"


# ---------------------------------------------------------------------------------------------------------------- 分派
def test_assign_single_session_spreads_units() -> None:
    a = assign_units([3], 3)
    assert a == {Unit(0, 0): 0, Unit(0, 1): 1, Unit(0, 2): 2}                     # 1 个会话：每机一个渲染进程
    assert proc_units(a, 3) == [{0: [0]}, {0: [1]}, {0: [2]}]
    assert assign_units([3], 1) == {Unit(0, i): 0 for i in range(3)}              # 单进程 Worker：全部在进程 0
    assert proc_units(assign_units([5], 2), 2) == [{0: [0, 2, 4]}, {0: [1, 3]}]   # 会话内轮转
    assert proc_units(assign_units([2], 3), 3) == [{0: [0]}, {0: [1]}, {}]        # 单元少于进程：多余进程无单元


def test_assign_by_session_when_sessions_cover_procs() -> None:
    a = assign_units([3, 3, 3], 3)
    assert all(a[Unit(s, i)] == s for s in range(3) for i in range(3))           # 按会话分派：一个会话的全部单元在同一进程
    assert proc_units(a, 3) == [{0: [0, 1, 2]}, {1: [0, 1, 2]}, {2: [0, 1, 2]}]
    b = assign_units([3, 3, 3, 3], 3)
    assert {b[Unit(3, i)] for i in range(3)} == {0}                               # 会话 3 → 进程 0（k mod R）
    c = assign_units([3, 3], 3)                                                   # 会话少于进程：进程 p 属于会话 p mod S
    assert proc_units(c, 3) == [{0: [0, 2]}, {1: [0, 1, 2]}, {0: [1]}]
    assert assign_units([3, 3], 3) == c and assign_units([], 3) == {}             # 确定性


def test_assign_rejects_bad_sizes() -> None:
    for R in (0, RENDER_PROCS_MAX + 1):
        with pytest.raises(SensorSvcError) as e:
            assign_units([3], R)
        assert e.value.code == 640
    with pytest.raises(SensorSvcError):
        assign_units([3, 0], 2)


def test_shared_clock_and_load_target() -> None:
    assert shared_clock()                                                         # Linux：CLOCK_MONOTONIC
    assert load_target(TARGET).__name__ == "EchoProc"
    assert load_target("awr.sensorsvc.procgroup:assign_units") is assign_units
    with pytest.raises(ValueError):
        load_target("no_colon")


# ---------------------------------------------------------------------------------------------------------------- 进程
def test_group_requests_scatter_and_close() -> None:
    with RenderGroup(TARGET, [{"tag": "a"}, {"tag": "b"}]) as g:
        assert g.size == 2 and len(set(g.pids)) == 2 and os.getpid() not in g.pids
        assert [i["tag"] for i in g.infos] == ["a", "b"] and [i["pid"] for i in g.infos] == g.pids and [i["index"] for i in g.infos] == [0, 1]
        assert g.request(1, ("hello", 7)) == ("echo", 1, ("hello", 7))
        rep = g.scatter({0: ("tick", 0, 5), 1: ("tick", 0, 5)})
        assert rep == {0: (0, g.pids[0], 0, 5), 1: (1, g.pids[1], 0, 5)}
        a = time.perf_counter()
        t = g.request(0, ("clock",))
        b = time.perf_counter()
        assert a <= t <= b                                                        # 跨进程时刻可比（系统范围单调时钟）
        out = g.close()
    assert [o["index"] for o in out] == [0, 1] and out[0]["seen"] == {0: [5]}
    assert g.close() == []                                                         # 重复关闭无副作用


def test_group_channels_serve_sessions_concurrently() -> None:
    """两个会话控制线程各用一条通道驱动同一组进程；每个进程按到达顺序串行处理，各会话的 tick 序列完整且有序。"""
    g = RenderGroup(TARGET, [{}, {}], channels=2)
    try:
        def drive(s: int) -> None:
            for seq in range(20):
                g.scatter({0: ("tick", s, seq), 1: ("tick", s, seq)}, ch=s)

        ths = [threading.Thread(target=drive, args=(s,)) for s in range(2)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        out = g.close()
    finally:
        g.terminate()
    for o in out:
        assert o["seen"] == {0: list(range(20)), 1: list(range(20))}


def test_handler_error_is_worker_lost_and_proc_survives() -> None:
    with RenderGroup(TARGET, [{}]) as g:
        with pytest.raises(SensorSvcError) as e:
            g.request(0, ("boom",))
        assert e.value.code == 648 and "bad request" in e.value.detail
        assert g.request(0, ("tick", 0, 1))[3] == 1                               # 进程继续服务


def test_construction_failure_and_lost_proc() -> None:
    with pytest.raises(SensorSvcError) as e:
        RenderGroup(TARGET, [{}, {"fail": True}])
    assert e.value.code == 648 and "world load failed" in e.value.detail
    g = RenderGroup(TARGET, [{}])
    try:
        g.procs[0].kill()
        g.procs[0].join(10)
        with pytest.raises(SensorSvcError) as e2:
            g.request(0, ("tick", 0, 1), timeout_s=5.0)
        assert e2.value.code == 648
    finally:
        g.terminate()
    with pytest.raises(SensorSvcError):
        RenderGroup(TARGET, [])


def test_dispatch_mode_and_tick_owner() -> None:
    """可见光（自动曝光以同机上一帧为输入）按机粘滞；只有热成像与 LiDAR 的会话按帧序号分派。"""
    from awr.sensorsvc.procgroup import dispatch_mode, tick_owner

    assert dispatch_mode(["camera"]) == "sensor" and dispatch_mode(["camera", "thermal", "lidar"]) == "sensor"
    assert dispatch_mode(["thermal"]) == "tick" and dispatch_mode(["thermal", "lidar"]) == "tick" and dispatch_mode([]) == "sensor"
    assert [tick_owner(s, 6) for s in range(8)] == [0, 1, 2, 3, 4, 5, 0, 1]
    with pytest.raises(SensorSvcError):
        tick_owner(3, 0)


def test_render_procs_capped_by_concurrent_sessions() -> None:
    """§4.7 内存预算（K13）：`sensor` 调度下并发会话 ≥ 3 时渲染进程数 ≤ 3，即每会话一个进程（K03 三并发同构）；会话更少或 `tick`
    调度（单会话热成像持续测量）时按请求；请求越界为 640。"""
    from awr.sensorsvc.procgroup import SESSIONS_CAP_PROCS, WORKER_MEM_BUDGET_MIB, render_procs_for

    assert WORKER_MEM_BUDGET_MIB == 16384 and SESSIONS_CAP_PROCS == 3
    assert [render_procs_for(6, s) for s in (1, 2, 3, 4, 6)] == [6, 6, 3, 3, 3]
    assert render_procs_for(2, 3) == 2 and render_procs_for(1, 5) == 1
    assert render_procs_for(6, 1, "tick") == 6 and render_procs_for(6, 3, "tick") == 6
    per = proc_units(assign_units([3, 3, 3], render_procs_for(6, 3)), 3)          # 三个标准轨会话：各占一个进程
    assert per == [{0: [0, 1, 2]}, {1: [0, 1, 2]}, {2: [0, 1, 2]}]
    for bad in ((0, 1), (RENDER_PROCS_MAX + 1, 1)):
        with pytest.raises(SensorSvcError):
            render_procs_for(*bad)
    with pytest.raises(SensorSvcError):
        render_procs_for(3, 1, "frame")
