"""D3 扩展点 1（AWR-05 §12.3；M24 §9.4 第 1 条）：order 195–198 归 M24、199 归 M23，包前缀与预算登记；D2 组合根不登记
D3 stage，默认 pipeline 的 stage 集合与 Σbudget 不变。"""

from __future__ import annotations

import itertools

import pytest
from simlib import CoreHarness

from awr.sim.fleet.stages import budgets as B
from awr.sim.fleet.stages import registry as R


def _noop(S, ctx) -> None:
    return None


def test_d3_ranges_owners_and_budgets() -> None:
    assert B.ORDER_RANGES["M24"] == ((195, 198),) and B.ORDER_RANGES["M23"] == ((199, 199),)
    assert R.infer_owner("awr.phys.fast.capture") == "M24" and R.infer_owner("awr.sim.events.runtime.plugin") == "M23"
    assert R.infer_owner(None, 196) == "M24" and R.infer_owner(None, 199) == "M23"
    assert (B.BUDGET_CORE["mesh_contact"], B.BUDGET_CORE["pose_history"], B.BUDGET_CORE["event_clock"]) == (0.020, 0.006, 0.0005)
    assert set(B.MESH_CONTACT_WRITES) == {"p", "v", "omega", "thrust", "thr_sp", "ctrl_mode", "crash_sub", "mode_evt"}
    ranges = sorted(r for rs in B.ORDER_RANGES.values() for r in rs)
    assert all(a[1] < b[0] for a, b in itertools.pairwise(ranges))     # 区段互不重叠


def test_d3_stages_register_in_their_ranges_only() -> None:
    with R.isolated_registry() as reg:
        R.register_stage("mesh_contact", 5, 0, 195, owner="M24", writes=("d3phys.contact",))(_noop)
        R.register_stage("pose_history", 1, 0, 198, owner="M24")(_noop)
        R.register_stage("event_clock", 1, 0, 199, owner="M23")(_noop)
        assert {s.name for s in reg.stages} == {"mesh_contact", "pose_history", "event_clock"}
        with pytest.raises(ValueError):
            R.register_stage("x", 1, 0, 199, owner="M24", budget_core=0.001)(_noop)
        with pytest.raises(ValueError):
            R.register_stage("y", 1, 0, 196, owner="M23", budget_core=0.001)(_noop)


def test_d2_composition_root_registers_no_d3_stage() -> None:
    h = CoreHarness(n=1)
    try:
        names = {s.name for s in h.core.fleet.pipeline.stages}
        assert not names & {"mesh_contact", "pose_history", "event_clock"}
        assert all(s.order < 195 for s in h.core.fleet.pipeline.stages)
    finally:
        h.close()
