"""plan-pool 客户端（M10-FR-030、FR-037；M10-AC-011、AC-009 的生效语义；ARCH-AC-006）。

进程模式（spawn）用例启动真实子进程；世界为 None 的作业（path_valid 无世界时直接 ok）避免 worker 装载世界。
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from awr.sim.planning.jobs import PlanRequest
from awr.sim.planning.pool import PlanPoolClient


def _req(jid: str, key: str = "k", prio: int = 1, budget: int = 100, **payload) -> PlanRequest:
    pl = {"polyline": np.array([[0, 0, 10.0], [1, 0, 10.0]])} | payload
    return PlanRequest(jid, "path_valid", None, (), pl, {}, prio, 0, budget, key)  # type: ignore[arg-type]


def _drain_until(pool: PlanPoolClient, pred, timeout_s: float = 30.0) -> None:
    t_end = time.monotonic() + timeout_s
    tick = 0
    while time.monotonic() < t_end:
        tick += 1
        pool.drain(tick)
        if pred():
            return
        time.sleep(0.01)
    raise AssertionError("timeout waiting for plan results")


def test_inline_applies_at_next_drain_and_supersedes() -> None:
    pool = PlanPoolClient(None, mode="inline")
    got: list = []
    pool.submit(_req("a", key="slot:1"), lambda r, t: got.append((r.job_id, r.status, t)))
    assert got == []                              # 结果在下一次 drain（步边界）生效
    pool.drain(7)
    assert got == [("a", "ok", 7)]
    pool.submit(_req("b", key="slot:2"), lambda r, t: got.append((r.job_id, t)))
    pool.submit(_req("c", key="slot:2"), lambda r, t: got.append((r.job_id, t)))   # 同键新作业取代旧作业
    pool.drain(8)
    assert got[1:] == [("c", 8)]
    assert pool.stats.superseded == 1


def test_inline_result_logged_to_inputlog() -> None:
    class Log:
        def __init__(self) -> None:
            self.items: list = []

        def append(self, kind, tick, payload) -> None:
            self.items.append((kind, tick, payload))

    lg = Log()
    pool = PlanPoolClient(None, mode="inline", inputlog=lg)
    pool.submit(_req("x"), lambda r, t: None)
    pool.drain(42)
    assert lg.items[0][0] == "plan_result" and lg.items[0][1] == 42 and len(lg.items[0][2]["sha256"]) == 64


@pytest.mark.slow
def test_process_pool_crash_retry_and_second_crash(tmp_path) -> None:
    """kill worker：首次崩溃后重建进程池并重交成功；同一作业连续两次崩溃以 214 结束。"""
    os.environ["AWR_PLAN_TEST_HOOKS"] = "1"
    try:
        pool = PlanPoolClient(None, mode="process")
        got: dict = {}
        pool.submit(_req("warm"), lambda r, t: got.__setitem__("warm", r))
        _drain_until(pool, lambda: "warm" in got, 60.0)
        t0 = time.monotonic()
        pool.submit(_req("once", key="c1", _test_crash_once=str(tmp_path / "mark")),
                    lambda r, t: got.__setitem__("once", r))
        _drain_until(pool, lambda: "once" in got, 60.0)
        assert got["once"].status == "ok" and pool.stats.crashes == 1 and pool.stats.rebuilds == 1
        assert time.monotonic() - t0 < 30.0
        pool.submit(_req("twice", key="c2", _test_crash=True), lambda r, t: got.__setitem__("twice", r))
        _drain_until(pool, lambda: "twice" in got, 60.0)
        assert got["twice"].status == "error" and got["twice"].code == 214
        pool.submit(_req("after", key="c3"), lambda r, t: got.__setitem__("after", r))
        _drain_until(pool, lambda: "after" in got, 60.0)
        assert got["after"].ok
        pool.close()
    finally:
        os.environ.pop("AWR_PLAN_TEST_HOOKS", None)


def test_timeout_marks_failed_and_discards_late_result() -> None:
    os.environ["AWR_PLAN_TEST_HOOKS"] = "1"
    try:
        pool = PlanPoolClient(None, mode="thread")
        got: list = []
        pool.submit(_req("slow", budget=10, _test_sleep_s=0.3), lambda r, t: got.append(r))
        _drain_until(pool, lambda: bool(got), 10.0)
        assert got[0].status == "timeout" and got[0].code == 125 and got[0].detail == "PLAN_TIMEOUT"
        time.sleep(0.4)
        pool.drain(99)
        assert len(got) == 1                          # 超时后到达的结果被丢弃
        pool.close()
    finally:
        os.environ.pop("AWR_PLAN_TEST_HOOKS", None)


def test_priority_order_in_thread_mode() -> None:
    os.environ["AWR_PLAN_TEST_HOOKS"] = "1"
    try:
        pool = PlanPoolClient(None, mode="thread")
        order: list = []
        pool.submit(_req("block", key="b", prio=3, budget=5000, _test_sleep_s=0.2), lambda r, t: order.append(r.job_id))
        pool.submit(_req("bg", key="bg", prio=3), lambda r, t: order.append(r.job_id))
        pool.submit(_req("ui", key="ui", prio=0), lambda r, t: order.append(r.job_id))
        _drain_until(pool, lambda: len(order) == 3, 10.0)
        assert order == ["block", "ui", "bg"]
        pool.close()
    finally:
        os.environ.pop("AWR_PLAN_TEST_HOOKS", None)


def test_worker_cpus_avoids_parent_pinning() -> None:
    """worker 避开 sim-core 所钉的核（FX2-R3，ADR-070）：父进程钉单核时取其余核；父进程未钉核时保持继承。"""
    from awr.sim.planning.worker import worker_cpus

    assert worker_cpus((1,), 8) == {0, 2, 3, 4, 5, 6, 7}
    assert worker_cpus(tuple(range(8)), 8) is None
    assert worker_cpus(None, 8) is None
    assert worker_cpus((0,), 1) is None


def test_init_worker_exits_when_parent_already_gone() -> None:
    """孤儿竞态（D1 验收第 2 轮 4.1b）：父进程在 worker 设置 PDEATHSIG 之前已死亡时，worker 的父进程 pid 已变化，init_worker
    立即退出，而不是常驻。这里以"给出一个不是父进程的 pid"模拟。"""
    import subprocess
    import sys

    code = ("import os\nfrom awr.sim.planning.worker import init_worker\n"
            "init_worker(None, None, None, os.getppid() + 1, None)\nprint('alive')\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "alive" not in r.stdout, (r.returncode, r.stdout, r.stderr)
    code_ok = ("import os\nfrom awr.sim.planning.worker import init_worker\n"
               "init_worker(None, None, None, os.getppid(), None)\nprint('alive')\n")
    r = subprocess.run([sys.executable, "-c", code_ok], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "alive" in r.stdout, (r.returncode, r.stdout, r.stderr)


@pytest.mark.slow
@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"), reason="needs sched_setaffinity")
def test_process_worker_not_on_parent_cpu() -> None:
    """进程模式：父进程钉在单核时，worker 的亲和性不含该核。"""
    if (os.cpu_count() or 1) < 2:
        pytest.skip("single cpu")
    import subprocess
    import sys

    code = (
        "import os, time\n"
        "os.sched_setaffinity(0, {0})\n"
        "import numpy as np\n"
        "from awr.sim.planning.jobs import PlanRequest\n"
        "from awr.sim.planning.pool import PlanPoolClient\n"
        "pool = PlanPoolClient(None, mode='process')\n"
        "pl = {'polyline': np.array([[0, 0, 10.0], [1, 0, 10.0]])}\n"
        "got = []\n"
        "pool.submit(PlanRequest('a', 'path_valid', None, (), pl, {}, 1, 0, 100, 'k'), lambda r, t: got.append(r))\n"
        "t_end = time.monotonic() + 60\n"
        "while not got and time.monotonic() < t_end:\n"
        "    pool.drain(1); time.sleep(0.01)\n"
        "pids = [p.pid for p in pool._exec._processes.values()]\n"
        "print(sorted(os.sched_getaffinity(pids[0])))\n"
        "pool.close()\n"
    )
    import ast

    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    cpus = ast.literal_eval(r.stdout.strip().splitlines()[-1])
    assert cpus and 0 not in cpus, cpus
