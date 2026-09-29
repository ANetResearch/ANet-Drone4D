"""可观测性字段（M08-AC-040；M08-NFR-020；M08 §7.6；AWR-17 §6.5、§9.2）。

- `state/sim-core/perf` 每秒【墙钟】一条，含 §7.6 全部字段（`stage_ms_per_s` 的键为 §5.2 stage 名）；
- StateRing 头部 `step_p50_us`、`step_p99_us`、`step_max_us`、`catchup_saturated`、`rtf_milli` 每秒更新；
- 时钟组（`t_sim_ns`、`clock_state`、`rate_milli`、`epoch`、`segment`）在 `clock_seq` 下一致读，且与 SimClock 一致
  （FR-003：每轮迭代先写心跳再推进，因此头部反映本轮开始时的时钟；不推进的一轮之后两者相等）。
"""

from __future__ import annotations

import msgpack
import pytest
from simlib import CoreHarness

from awr.contracts import bus_keys
from awr.contracts.enums import TimeState
from awr.sim.fleet.pipeline import TICK_NS
from awr.sim.fleet.stages import budgets as B
from awr.sim.fleet.stages import registry as R

FIELDS = {"stage_ms_per_s", "n_active", "n_l0", "kernel", "cpu_pct", "rtf_limited", "rss_mb", "env_query_us_p99",
          "publish_us_p99", "admission_us_p99", "estimate_us_p99", "gc_gen2_ms_max"}


@pytest.fixture
def h():
    with R.isolated_registry() as reg:
        R.register_stage("env", 5, 0, 20, owner="M07")(lambda S, ctx: None)
        x = CoreHarness(n=3, reg=reg)
        try:
            yield x
        finally:
            x.close()


def test_perf_topic_1hz_with_all_fields(h: CoreHarness) -> None:
    got: list[dict] = []
    h.bus.subscribe(bus_keys.STATE_PERF, lambda k, raw: got.append(msgpack.unpackb(raw, raw=False)))
    h.takeoff(5.0)
    h.cmd("hover", {})
    n0 = len(got)
    w0 = h.W[0]
    for _ in range(3 * 250):
        h.W[0] += TICK_NS
        h.core.iterate()
    n = len(got) - n0
    assert 2 <= n <= 4, n  # 3 s【墙钟】约 3 条
    m = got[-1]
    assert set(m) >= FIELDS, FIELDS - set(m)
    assert m["n_active"] == 3 and m["kernel"] in ("numba", "numpy") and m["n_l0"] == 0
    assert set(m["stage_ms_per_s"]) <= set(B.BUDGET_CORE) and {"l1", "tap", "ingest", "env"} & set(m["stage_ms_per_s"])
    assert m["env_query_us_p99"] is not None and m["publish_us_p99"] is not None
    assert m["admission_us_p99"] is None or m["admission_us_p99"] >= 0
    assert h.W[0] - w0 == 3 * 250 * TICK_NS


def test_ring_header_step_stats_and_clock_group(h: CoreHarness) -> None:
    hdr0 = h.ring.header()
    for _ in range(300):
        h.W[0] += TICK_NS
        h.core.iterate()
    h.core.iterate()  # 墙钟不前进：不推进，只写心跳
    hdr = h.ring.header()
    assert hdr.step_seq > hdr0.step_seq
    assert hdr.step_p99_us >= hdr.step_p50_us >= 0 and hdr.step_max_us >= hdr.step_p99_us
    assert hdr.rtf_milli > 0 and hdr.step_budget_us > 0
    assert hdr.catchup_saturated == h.core.clock.catchup_saturated
    assert hdr.t_sim_ns == h.core.clock.t_ns and hdr.clock_state == int(TimeState.PLAYING)
    assert hdr.rate_milli == 1000 and hdr.epoch == h.core.epoch and hdr.segment == h.core.segment
    assert h.clock("speed", {"rate": 2.0})["code"] == 0
    h.W[0] += TICK_NS
    h.core.iterate()
    h.core.iterate()
    hdr = h.ring.header()
    assert hdr.rate_milli == 2000 and hdr.t_sim_ns == h.core.clock.t_ns
    assert h.clock("pause")["code"] == 0
    h.W[0] += TICK_NS
    h.core.iterate()
    h.core.iterate()
    assert h.ring.header().clock_state == int(TimeState.PAUSED)
