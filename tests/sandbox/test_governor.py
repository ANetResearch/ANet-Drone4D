"""倍速治理（M17-FR-020、AC-006 的授予逻辑；AWR-04 §4.4.5；M17 PRD §6.6.5）。"""

from __future__ import annotations

from awr.sandbox.governor import grant, rate_cap, recheck, wire_limited_by

TIERS = [0.25, 0.5, 1, 2, 5, 10]


def test_cap_and_budget() -> None:
    assert round(rate_cap(0.15), 3) == 5.667 and rate_cap(0.01) == 10.0
    g = grant(10, 0.15, 0.0, 2.0, TIERS)          # 预算够，按单会话上限 5.667 → 档位 5
    assert g.granted == 5 and g.limited_by == "cap" and g.code == 501
    g2 = grant(10, 0.15, 0.15 * 5, 1.0, TIERS)    # 另一会话已占 0.75 核，B_sim 1.0 → 只剩 ×1
    assert g2.granted == 1 and g2.limited_by == "budget" and g2.code == 501
    assert 0.15 * 5 + 0.15 * g2.granted <= 1.0 * 1.1
    g3 = grant(2, 0.05, 0.0, 1.0, TIERS)
    assert g3.granted == 2 and g3.limited_by is None and g3.code == 0


def test_floor_manual_and_l2() -> None:
    assert grant(0.25, 0.15, 5.0, 0.1, TIERS).granted == 0.25          # 慢放从不受限
    assert grant(5, 0.15, 5.0, 0.1, TIERS).granted == 1                 # 不降到 min(请求, 1) 以下
    m = grant(5, 0.05, 0.0, 5.0, TIERS, manual=True)
    assert m.granted == 1 and m.limited_by == "manual" and m.code == 501
    assert grant(1, 0.05, 0.0, 5.0, TIERS, manual=True).code == 0
    l2 = grant(10, 0.05, 0.0, 5.0, TIERS, l2=True)
    assert l2.granted == 1 and l2.limited_by == "l2"


def test_recheck_and_wire() -> None:
    assert recheck(5, 4.9, TIERS) is None
    assert recheck(5, 3.1, TIERS) == 2
    assert recheck(10, 0.4, TIERS) == 1
    assert recheck(1, 0.2, TIERS) is None
    assert [wire_limited_by(x) for x in ("budget", "cap", "measured", "manual", "l2", None)] == \
        ["cpu_budget", "cost_cap", "cost_cap", "manual_control", "degraded", None]


def test_regrant_upward_when_first_measured_cost_arrives() -> None:
    """D2-FX-R2（D2-AC-07）：就绪后 10 s 内按先验 c0 授予（满载会话的实测值偏保守）；首个实测 c_i 更低时向上补授予，
    不超过申请值，并下发 `sim/speed`；实测降档（measured）后不回升。"""
    from brokerkit import create, make_broker, ready

    b, fx, clk = make_broker(n_max=2)
    sid = create(b, 1)["result"]["session"]["sid"]
    ready(b, sid)
    rec = b.sessions[sid]
    rec.clock_state, rec.played = "PLAYING", True
    rec.c_i = 0.658                                       # 实测表的满载先验：上限 0.85/0.658 → ×1
    r = b.rate(sid, 10)["result"]
    assert r["granted"] == 1 and r["limited_by"] == "cap"
    clk.adv(11.0)
    b.on_perf(sid, {"cost_core_per_rate": 0.06, "rtf_win": 1.0})
    assert rec.c_i_measured and rec.granted_rate == 10 and rec.requested_rate == 10
    assert (sid, "speed", {"rate": 10}) in fx.clocks
    rec2 = b.sessions[create(b, 2)["result"]["session"]["sid"]]
    ready(b, rec2.sid)
    rec2.clock_state, rec2.played = "PLAYING", True
    b._set_rate(rec2, 1.0, "measured", requested=5.0, why="callback")
    clk.adv(11.0)
    b.on_perf(rec2.sid, {"cost_core_per_rate": 0.01, "rtf_win": 1.0})
    assert rec2.granted_rate == 1.0                       # 实测回调降档后不回升


def test_perf_counts_fill_session_usage() -> None:
    """issue #7：sim-core 的 perf 带机巢数与活动任务数，会话用量（GET /sessions/{sid} 与 sandbox/session）随之更新。"""
    from brokerkit import create, make_broker, ready

    b, _fx, _clk = make_broker(n_max=2)
    sid = create(b, 1)["result"]["session"]["sid"]
    ready(b, sid)
    b.on_perf(sid, {"n_uav": 2, "n_target": 1, "n_nest": 1, "n_task_active": 2})
    rec = b.sessions[sid]
    w = b.wire(rec)["usage"]
    assert (w["vehicles"], w["targets"], w["nests"], w["tasks_active"]) == (2, 1, 1, 2)
