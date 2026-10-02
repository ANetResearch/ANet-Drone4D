"""慢任务公平轮转与预算借贷（M08-FR-004；ADR-057；M07-to-M08 第 5 条、M14-to-M08 第 2 条）。

以注入的假 `perf_ns` 驱动 SlowTasks：任务函数按设定耗时推进假时钟，断言不可分片任务在 p99 超过每轮预算、且其他任务
持续占满预算时仍在有界轮数内启动，借贷在后续轮次扣还，空队列不计积分。
"""

from __future__ import annotations

import itertools
import math

import pytest

from awr.sim.runtime.slow import DEBT_CAP_US, MAX_DEFER_ROUNDS, MIN_BUDGET_US, STARVE_WALL_NS, SlowTask, SlowTasks


class Clock:
    def __init__(self) -> None:
        self.ns = 0

    def __call__(self) -> int:
        return self.ns

    def spend(self, us: float) -> None:
        self.ns += int(us * 1000)


def _mk(cap_us: float = 1000.0) -> tuple[Clock, SlowTasks]:
    c = Clock()
    return c, SlowTasks(c, cap_us=cap_us)


def _hog(c: Clock, name: str, us: float) -> SlowTask:
    """可分片任务：每次把剩余预算吃满（至多 us）。"""
    def fn(left: float) -> None:
        c.spend(min(us, max(left, 1.0)))
        return None
    return SlowTask(name, fn)


def test_atomic_over_budget_is_not_starved() -> None:
    """p99 = 2500 µs > 每轮最大预算 1000 µs：旧规则（剩余 ≥ p99 才启动）永不再启动；新规则每次请求都在有界轮数内执行。"""
    c, sl = _mk()
    q: list[int] = []
    served: list[tuple[int, int]] = []  # (请求序号, 等待轮数)
    rnd = [0]

    def est(left: float) -> object:
        if not q:
            return False
        k = q.pop(0)
        c.spend(2500.0)
        served.append((k, rnd[0] - enq[k]))
        return None

    enq: dict[int, int] = {}
    sl.add(_hog(c, "state_ext", 400.0))
    sl.add(SlowTask("estimate", est, atomic=True, pending=lambda: bool(q)))
    sl.add(_hog(c, "geo", 400.0))
    for r in range(400):
        rnd[0] = r
        if r % 20 == 0:
            enq[len(enq)] = r
            q.append(len(enq) - 1)
        sl.run(1000.0, now_wall=r * 4_000_000, now_sim=r * 4_000_000)
    assert len(served) == 20, served
    bound = math.ceil(1000.0 / MIN_BUDGET_US)
    assert max(w for _k, w in served) <= bound, served
    est_t = sl.get("estimate")
    assert est_t is not None and est_t.defer_max <= MAX_DEFER_ROUNDS


def test_atomic_under_full_load_minimum_budget() -> None:
    """每轮只剩下限 100 µs 时（主循环已用满 2.7 ms），p99 1000 µs 的查询至多顺延 ⌈1000/100⌉ 轮后启动。"""
    c, sl = _mk()
    q = [1]
    waited: list[int] = []

    def query(left: float) -> object:
        if not q:
            return False
        q.pop()
        c.spend(1000.0)
        return None

    t = sl.add(SlowTask("query", query, atomic=True, pending=lambda: bool(q)))
    t.us.extend([1000.0] * 10)
    t.runs = 10
    for r in range(40):
        before = len(q)
        sl.run(0.0, now_wall=r, now_sim=r)          # budget 取下限 100 µs
        if before and not q:
            waited.append(r)
            q.append(1)
    assert waited, "query never ran"
    gaps = [b - a for a, b in itertools.pairwise(waited)]
    # 两次执行之间：扣还借贷（900 µs 超支按每轮 0 µs 可扣，只能以积分方式抵），积分每轮 +100 µs
    assert all(g <= math.ceil(1000.0 / MIN_BUDGET_US) + 1 for g in gaps), gaps


def test_debt_is_repaid_but_budget_keeps_floor() -> None:
    c, sl = _mk()
    q = [1]
    got: list[float] = []

    def big(left: float) -> object:
        if not q:
            return False
        q.pop()
        c.spend(3000.0)
        return None

    def probe(left: float) -> None:
        got.append(left)
        return None

    sl.add(SlowTask("estimate", big, atomic=True, pending=lambda: bool(q)))
    sl.add(SlowTask("probe", probe))
    sl.run(1000.0, now_wall=0, now_sim=0)
    assert 0 < sl.debt_us <= DEBT_CAP_US
    debt0 = sl.debt_us
    for r in range(1, 10):
        sl.run(1000.0, now_wall=r, now_sim=r)
    assert sl.debt_us < debt0
    assert min(got[1:]) >= MIN_BUDGET_US - 1e-6       # 扣还期间每轮仍保留下限
    for r in range(10, 40):
        sl.run(1000.0, now_wall=r, now_sim=r)
    assert sl.debt_us == 0.0
    assert got[-1] == pytest.approx(1000.0)


def test_empty_queue_accrues_no_credit() -> None:
    c, sl = _mk()
    q: list[int] = []

    def est(left: float) -> object:
        return False

    t = sl.add(SlowTask("estimate", est, atomic=True, pending=lambda: bool(q)))
    t.us.extend([5000.0] * 4)
    t.runs = 4
    sl.add(_hog(c, "hog", 2000.0))
    for r in range(20):
        sl.run(1000.0, now_wall=r, now_sim=r)
    assert t.credit_us == 0.0 and t.deferred_rounds == 0 and sl.backlog == 0


def test_deferred_task_becomes_head_of_next_round() -> None:
    c, sl = _mk()
    q = [1]
    order: list[str] = []

    def est(left: float) -> object:
        if not q:
            return False
        order.append("estimate")
        q.pop()
        c.spend(800.0)
        return None

    def hog(left: float) -> None:
        order.append("hog")
        c.spend(max(left, 1.0))
        return None

    sl.add(SlowTask("hog", hog))
    t = sl.add(SlowTask("estimate", est, atomic=True, pending=lambda: bool(q)))
    t.us.extend([800.0] * 4)
    t.runs = 4
    sl.run(1000.0, now_wall=0, now_sim=0)            # hog 吃满，estimate 顺延
    assert sl.backlog == 1 and order == ["hog"]
    sl.run(1000.0, now_wall=1, now_sim=1)            # 下一轮从 estimate 开始
    assert order[1] == "estimate"


def test_wall_clock_bound_and_batch_drain() -> None:
    """追帧时一轮很长：顺延满 20 ms【墙钟】即强制启动，并在同一轮把排队的请求（≤ ATOMIC_BATCH）一并处理。"""
    from awr.sim.runtime.slow import ATOMIC_BATCH, STARVE_WALL_NS

    c, sl = _mk()
    q = [1, 2, 3]
    done: list[tuple[int, int]] = []

    def est(left: float) -> object:
        if not q:
            return False
        done.append((q.pop(0), rnd[0]))
        c.spend(900.0)
        return None

    rnd = [0]
    t = sl.add(SlowTask("estimate", est, atomic=True, pending=lambda: bool(q)))
    t.us.extend([900.0] * 4)
    t.runs = 4
    sl.add(_hog(c, "hog", 5000.0))
    wall = 0
    for r in range(6):
        rnd[0] = r
        sl.ptr = 1                                  # hog 先吃满预算（最坏情形），estimate 只能靠墙钟上界
        sl.run(1000.0, now_wall=wall, now_sim=r)
        wall += 50_000_000 if r == 0 else 1_000_000  # 第一轮之后隔了 50 ms【墙钟】（一轮追帧）
        if not q:
            break
    assert [k for k, _ in done] == [1, 2, 3], done
    assert len({r for _, r in done}) == 1 and ATOMIC_BATCH >= 3 and STARVE_WALL_NS == 20_000_000


def test_heavy_skip_runs_only_starving_tasks() -> None:
    """`heavy_skip`（sim-core 主循环，ADR-070）：预算不足 MIN_BUDGET_US 时本轮只执行已饿死的任务；不带该参数时仍保底
    MIN_BUDGET_US（既有调用方语义不变）。"""
    t = [0]
    st = SlowTasks(lambda: t[0])
    calls: list[str] = []

    def task(name: str):
        def fn(b: float):
            calls.append(name)
            t[0] += 500_000  # 每次 500 µs：不可分片任务的启动门槛 need = 500 µs
            return None
        return fn

    st.add(SlowTask("plain", task("plain")))
    st.add(SlowTask("atomic", task("atomic"), atomic=True, pending=lambda: True))
    ran = st.run(2000.0, now_wall=0, now_sim=0)  # 预热：两者各执行，atomic 的 p99 = 500 µs
    assert "plain" in ran and "atomic" in ran
    calls.clear()
    assert st.run(50.0, now_wall=1, now_sim=0, heavy_skip=True) == [] and calls == []
    assert "plain" in st.run(50.0, now_wall=2, now_sim=0)  # 不带 heavy_skip：保底 100 µs，普通任务照常执行
    calls.clear()
    st.run(0.0, now_wall=3, now_sim=0, heavy_skip=True)  # atomic 开始顺延
    assert "atomic" not in calls
    ran = st.run(0.0, now_wall=3 + STARVE_WALL_NS + 10, now_sim=0, heavy_skip=True)  # 顺延满 STARVE_WALL_NS：重轮也执行
    assert "atomic" in ran and "plain" not in ran


def test_pair_ticks_wakes_at_even_deadline() -> None:
    """成对推进（`SimCore.pair_ticks`，ADR-070）：×1 下刚执行完偶数 tick 时建议的休眠到其后的偶数 tick 到期（两 tick），
    下一轮推进奇、偶两个 tick；关闭时按下一 tick 到期前 150 µs 醒来。"""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from simlib import CoreHarness

    from awr.sim.fleet.pipeline import TICK_NS
    from awr.sim.fleet.stages import registry as R

    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg, seat=False, ready=False)
        try:
            core = h.core
            core.pair_ticks = True
            clk = core.clock
            while clk.tick % 2 != 0:  # 对齐到偶数 tick
                h.W[0] += TICK_NS
                core.iterate()
            w = core.iterate()
            nd = clk.next_deadline_ns()
            assert abs(w * 1e9 - (nd + TICK_NS - h.W[0])) < 1_000
            tick0 = clk.tick
            h.W[0] = nd + TICK_NS
            core.iterate()
            assert clk.tick == tick0 + 2 and clk.tick % 2 == 0
            core.pair_ticks = False
            w = core.iterate()
            assert abs(w * 1e9 - (clk.next_deadline_ns() - 150_000 - h.W[0])) < 1_000
        finally:
            h.close()
