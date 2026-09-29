"""M09-AC-021（D1-AC-27）：批量风暴——批量 RTL 的第④步向量化、同一 tick 全部转移、每轮迭代每个事件 category 至多一次 put、
事件带合并键（前端 Toast 合并为 ≤ 3 条）。耗时阈值（第④步 ≤ 0.1 ms、转移 tick ≤ 4 ms、单步最大 ≤ 12 ms，1000 架）为 perf，
在独占性能锁下由验收阶段运行（`-m perf`）。"""

from __future__ import annotations

import time

import numpy as np
import pytest
from safelib import Harness, UnitRig

from awr.contracts.safety_codes import SAFETY_CODES
from awr.sim.safety.flight_fsm import Origin


def test_batch_transitions_one_tick_one_put() -> None:
    h = Harness(n=24, spacing=12.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        ids = h.takeoff_all(6.0)
        pub = h.core.events
        per_flush: list[dict[str, int]] = []
        orig = pub.flush

        def flush() -> int:
            per_flush.append({k: len(v) for k, v in pub._batches.items()})
            return orig()

        pub.flush = flush  # type: ignore[method-assign]
        tr0 = h.rt.fsm.transitions
        rep = h.cmd("rtl", {}, uav="*", cid="storm")
        assert len(rep["per_uav"]["accepted"]) == 24
        h.step(2)
        assert h.rt.fsm.transitions - tr0 >= 24
        assert max(p.get("sim", 0) for p in per_flush) >= 24  # 24 条 uav.state 在同一次 put
        # 自动安全动作风暴：全部机体同 tick 进入 ELAND，safety 事件同一次 put，合并键一致
        n0 = len(h.events)
        per_flush.clear()
        h.rt.fsm.propose(np.array([h.slot(u) for u in ids]), 10, 0, Origin.AUTO, "SAF.CTRL.TILT_ELAND")
        h.step(1)
        assert max(p.get("safety", 0) for p in per_flush) >= 24
        keys = {SAFETY_CODES[e["code"]].merge_key for e in h.events[n0:] if e.get("kind", "").startswith("safety.")}
        assert keys == {"safety:eland"}  # 前端按合并键合并为 1 条 Toast
    finally:
        h.close()


@pytest.mark.perf
def test_storm_1000_transitions_budget() -> None:
    """1000 次转移（含事件组装、合批与 Supervisor 命令）≤ 4 ms（M09-NFR-004）。只在性能锁下运行。"""
    r = UnitRig(n=1000, capacity=1024)
    for s in range(1000):
        r.set(s, 5, 1)
    r.tick(1)
    r.rt.fsm.propose(np.arange(1000), 8, 0, Origin.AUTO, "SAF.BAT.CRIT")
    t0 = time.perf_counter()
    r.tick(1)
    dt = time.perf_counter() - t0
    assert dt <= 0.004, dt
