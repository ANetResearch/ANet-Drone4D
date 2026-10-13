"""M20-AC-012、AC-013（配对复核）、AC-014：默认贪心分配、可行性检查器、外部计划与 Allocator 桩（AWR-04 §8.4、§8.5；ADR-104）。"""

from __future__ import annotations

import itertools

import numpy as np
import pytest
from m20lib import F1, MODELS, P600, Q1, V1, nest, path_item, snap, station, veh

from awr.tasking.allocator import apply_hints, get_allocator
from awr.tasking.check import FeasibilityChecker
from awr.tasking.greedy import GreedyAllocator, bottleneck_assign, capable, estimate
from awr.tasking.keepout import circles
from awr.tasking.model import Keepout, Plan, Snapshot, TaskingError, WorkItem


def _fleet():
    na = nest("n-a", (0.0, 0.0, 0.0), "box")
    nb = nest("n-b", (1500.0, 0.0, 0.0), "vtol_pad")
    nc = nest("n-c", (0.0, 1500.0, 0.0), "launcher")
    vs = [veh("p600-01", P600, na), veh("p600-02", P600, na), veh("q1-01", Q1, na), veh("v1-01", V1, nb), veh("v1-02", V1, nb),
          veh("f1-01", F1, nc)]
    return [na, nb, nc], vs


def test_multi_nest_multi_model_constraints() -> None:
    nests, vs = _fleet()
    items = [station("t1.0", (300.0, 200.0, 60.0), persistent=True), station("t2.0", (1300.0, 100.0, 60.0), tid="t2", persistent=True),
             path_item("t3.0", [[0, 300, 60], [600, 300, 60]], tid="t3", group="t3"), path_item("t3.1", [[0, 400, 60], [600, 400, 60]], tid="t3", group="t3"),
             path_item("t3.2", [[0, 500, 60], [600, 500, 60]], tid="t3", group="t3")]
    s = snap(vs, nests, items)
    plan = GreedyAllocator().plan(s)
    assert not plan.infeasible, plan.infeasible
    by = {a.wid: a for a in plan.assignments}
    # 同组条带分给不同机体；持续性工作项占用整机；固定翼（有人值守发射回收站）不承担持续性工作项
    assert len({by[w].uav for w in ("t3.0", "t3.1", "t3.2")}) == 3
    pers = [by["t1.0"].uav, by["t2.0"].uav]
    assert len(set(pers)) == 2 and "f1-01" not in pers
    for a in plan.assignments:
        assert a.margin >= 0.0
        v = s.vehicle(a.uav)
        assert capable(MODELS[v.model_id], s.item(a.wid), v, s.nests[v.nest_id].crewed) is None
    # 近处的站位由 n-a 的机执行，远处的由 n-b 的 V1
    assert s.vehicle(by["t2.0"].uav).nest_id == "n-b"


def test_determinism_and_digest() -> None:
    nests, vs = _fleet()
    items = [station(f"t{k}.0", (100.0 * k, 50.0 * k, 60.0), tid=f"t{k}") for k in range(1, 6)]
    a = GreedyAllocator().plan(snap(vs, nests, items))
    b = GreedyAllocator().plan(snap(vs, nests, items))
    assert a.digest() == b.digest()
    assert Plan.from_payload(a.to_payload()).digest() == a.digest()
    assert Snapshot.from_payload(snap(vs, nests, items).to_payload()).to_payload()["items"][0]["wid"] == "t1.0"


def test_reasons_energy_keepout_range_capability() -> None:
    na = nest("n-a", (0.0, 0.0, 0.0), link_range_m=2000.0)
    vs = [veh("p600-01", P600, na, soc=0.3), veh("p600-02", P600, na, soc=0.95)]
    ko = [Keepout("k0", 500.0, 0.0, 60.0)]
    items = [station("a.0", (500.0, 0.0, 60.0), tid="a"),                         # 在禁入圆内
             station("b.0", (2600.0, 0.0, 60.0), tid="b"),                        # 超出链路距离
             station("c.0", (300.0, 300.0, 60.0), tid="c", workload=900.0),       # 低电机能量不足，满电机可行
             WorkItem("d.0", "d", "station", workload_s=60.0, pos=(10.0, 10.0, 60.0), needs={"role": "hover", "caps": ["radar.mmw"]})]
    plan = GreedyAllocator().plan(snap(vs, [na], items, ko))
    why = {x["wid"]: x["why"] for x in plan.infeasible}
    assert why == {"a.0": "keepout", "b.0": "range", "d.0": "capability"}
    assert plan.of("c.0").uav == "p600-02"


def test_divisible_split_when_no_single_vehicle_can_fly_it() -> None:
    na = nest("n-a", (0.0, 0.0, 0.0), link_range_m=50000.0)
    vs = [veh("p600-01", P600, na), veh("p600-02", P600, na)]
    long = path_item("t.0", [[0, 0, 60], [12000, 0, 60]], tid="t")
    long.divisible = True
    long.min_grain_s = 60.0
    plan = GreedyAllocator().plan(snap(vs, [na], [long]))
    assert {a.wid for a in plan.assignments} == {"t.0.a", "t.0.b"} or len(plan.assignments) >= 2


def _brute(s: Snapshot) -> float | None:
    """持续性站位（每机至多一项）的穷举最优代价和（与贪心同一代价式，T_ref 按工作项自适应）。"""
    g = GreedyAllocator()
    C = circles(s.keepouts)
    cands = g._candidates(s)
    cost = {}
    for j in s.items:
        ests = [(c, estimate(s, c, j, C)) for c in cands if capable(c.m, j, c.v, False) is None]
        ests = [(c, e) for c, e in ests if e.ok]
        if not ests:
            return None
        t_ref = max(60.0, float(np.median([e.eta_s for _c, e in ests])))
        p_max = max(c.m.p_role(j.role) for c, _e in ests)
        for c, e in ests:
            cost[(j.wid, c.v.id)] = (0.4 * e.eta_s / t_ref + 0.3 * (1 - min(e.margin, 1.0)) + 0.2 * (1 - e.match)
                                     + 0.1 * c.m.p_role(j.role) / p_max)
    best = None
    ids = [c.v.id for c in cands]
    for perm in itertools.permutations(ids, len(s.items)):
        tot = 0.0
        for j, u in zip(s.items, perm, strict=True):
            c = cost.get((j.wid, u))
            if c is None:
                break
            tot += c
        else:
            best = tot if best is None else min(best, tot)
    return best


def test_greedy_within_1_3_of_optimum_on_small_instances() -> None:
    rng = np.random.default_rng(11)
    ratios = []
    for _ in range(200):
        na = nest("n-a", (0.0, 0.0, 0.0))
        nb = nest("n-b", (float(rng.uniform(500, 1500)), float(rng.uniform(-500, 500)), 0.0))
        nv = int(rng.integers(2, 6))
        vs = [veh(f"u{k}", [P600, Q1, V1][k % 3], na if k % 2 == 0 else nb, soc=float(rng.uniform(0.5, 1.0))) for k in range(nv)]
        ni = int(rng.integers(1, min(4, nv) + 1))
        items = [station(f"t{k}.0", (float(rng.uniform(-200, 1600)), float(rng.uniform(-600, 600)), 60.0), tid=f"t{k}", persistent=True)
                 for k in range(ni)]
        s = snap(vs, [na, nb], items)
        opt = _brute(s)
        if opt is None or opt <= 0:
            continue
        plan = GreedyAllocator().plan(s)
        if plan.infeasible:
            continue
        ratios.append(sum(a.cost for a in plan.assignments) / opt)
    assert len(ratios) >= 150
    assert max(ratios) <= 1.3, max(ratios)


def test_bottleneck_assignment_beats_sum_assignment_on_max() -> None:
    C = np.array([[1.0, 2.0, 100.0], [2.0, 50.0, 3.0], [60.0, 1.0, 2.0]])
    b = bottleneck_assign(C)
    from awr.swarm.allocation.hungarian import hungarian

    h = hungarian(C)
    assert C[np.arange(3), b].max() <= C[np.arange(3), h].max()


def test_external_checker_and_stubs() -> None:
    nests, vs = _fleet()
    items = [station("t1.0", (300.0, 200.0, 60.0), persistent=True),
             path_item("t3.0", [[0, 300, 60], [600, 300, 60]], tid="t3", group="t3"),
             path_item("t3.1", [[0, 400, 60], [600, 400, 60]], tid="t3", group="t3")]
    s = snap(vs, nests, items)
    verdicts, plan = FeasibilityChecker().check(s, [{"wid": "t1.0", "uav": "f1-01"}, {"wid": "t3.0", "uav": "p600-01"},
                                                    {"wid": "t3.1", "uav": "p600-01"}, {"wid": "zz", "uav": "p600-02"}])
    v = {x.wid: x for x in verdicts}
    assert v["t1.0"].code == 580 and v["t1.0"].why == "nest"       # 持续性工作项不进入有人值守机巢
    assert v["t3.0"].ok
    assert v["t3.1"].code == 584 and v["t3.1"].why == "group"
    assert v["zz"].code == 105
    assert [a.wid for a in plan.assignments] == ["t3.0"]
    for name in ("anet_cnp", "llm_agent"):
        with pytest.raises(TaskingError) as ei:
            get_allocator(name).plan(s, 200)
        assert ei.value.code == 584 and ei.value.why == "allocator_unavailable"
    ext = get_allocator("external", {"fallback": "greedy"}).plan(s, 200)
    assert ext.allocator == "external>greedy" and ext.assignments


def test_hints_only_narrow_candidates() -> None:
    nests, vs = _fleet()
    s = snap(vs, nests, [station("t1.0", (300.0, 200.0, 60.0))])
    s2 = apply_hints(s, [{"wid": "t1.0", "prefer": ["awr_v1"], "forbid": ["p600_mid360"]}])
    plan = GreedyAllocator().plan(s2)
    assert s2.vehicle(plan.assignments[0].uav).model_id == "awr_v1"


def test_relay_station_inside_planning_circle_is_assignable() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-35）：运动识别物的接力站位按站位口径（小于规划口径）选出，可能落在规划圆内；分配时包含站位的
    规划圆收缩到站位以内 1 m（与执行器转场同一口径），不判 keepout。普通站位在圆内仍判 keepout。"""
    na = nest("n-a", (0.0, 0.0, 0.0), "box")
    vs = [veh("p600-01", P600, na)]
    ko = [Keepout("p/0", 600.0, 0.0, 200.0, target="p", moving=True)]
    relay = WorkItem("t1.r1", "t1", "relay_station", persistent=True, workload_s=600.0, pos=(420.0, 0.0, 60.0))
    plan = GreedyAllocator().plan(snap(vs, [na], [relay], ko))
    assert [a.wid for a in plan.assignments] == ["t1.r1"], plan.infeasible
    plan2 = GreedyAllocator().plan(snap(vs, [na], [station("t2.0", (420.0, 0.0, 60.0), tid="t2", persistent=True)], ko))
    assert not plan2.assignments and plan2.infeasible
