"""agent-runtime 重启恢复（M14-to-M16 第 2 条；M14 A15 `test_a15_restore_interrupted` 的多进程版；D1-ext，P1）。

S3 以 ci profile 运行（sim-core + api + agent-runtime）；T-0001 授标后对 agent-runtime 发 kill -9：
supervisor 重启该进程，新进程从协调者证据链重建任务表，非终态任务置 input-required（reason interrupted，483）；
sim-core 与 api 不受影响（pid 不变）。多进程版依赖 M08 的租约修复与 M13 检测器（M14-to-M08 第 1、2 条），未满足时失败即暴露。
"""

from __future__ import annotations

import os
import signal
import time

import pytest
from awrproc import Backend, world_ready

pytestmark = [pytest.mark.chaos, pytest.mark.perf, pytest.mark.ext, pytest.mark.needs_data]

PROCS = "sim-core,api,agent-runtime"


def _data(e: dict) -> dict:
    return dict(e.get("data") or {})


def test_agent_runtime_restart_restores_tasks() -> None:
    if not world_ready("newyork"):
        pytest.skip("worlds/newyork not built（make worlds）")
    b = Backend(world="newyork", scenario="s3-newyork-sar", scenario_profile="ci", only=PROCS)
    b.start()
    try:
        b.wait_event(lambda e: e.get("type") == "agent.task.awarded", 300.0)
        pid0, sim0, api0 = b.pid_of("agent-runtime"), b.pid_of("sim-core"), b.pid_of("api")
        os.kill(pid0, signal.SIGKILL)
        t0 = time.monotonic()
        restarted = None
        while time.monotonic() - t0 < 30.0:
            cur = {p["name"]: p for p in b.procs()}.get("agent-runtime") or {}
            if int(cur.get("pid") or 0) not in (0, pid0) and cur.get("state") == "RUNNING":
                restarted = time.monotonic() - t0
                break
            time.sleep(0.2)
        assert restarted is not None, "agent-runtime not restarted within 30 s"
        ev = b.wait_event(lambda e: e.get("type") == "agent.task.state" and _data(e).get("to") == "input-required"
                          and int(_data(e).get("reason_code") or 0) == 483, 60.0)
        assert _data(ev).get("task_id") == "T-0001" and _data(ev).get("reason") == "interrupted", ev
        assert b.pid_of("sim-core") == sim0 and b.pid_of("api") == api0
    finally:
        b.stop()
