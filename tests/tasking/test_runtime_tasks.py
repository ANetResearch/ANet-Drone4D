"""M20-AC-003 至 AC-005、AC-008、AC-009（端到端功能部分）、AC-012（分配生效）、AC-014、AC-015（返航）、AC-016（手动接管）：
在 sim-core（sandbox100、平地）中经命令与查询驱动任务全流程。"""

from __future__ import annotations

import numpy as np
import pytest
from ctlib import errors, registry
from m20core import Core

from awr.tasking.keepout import path_clear


def _preview_errors(obj) -> list[str]:
    from jsonschema import Draft202012Validator

    v = Draft202012Validator({"$ref": "https://schemas.anet-drone.dev/tasking/1/task.schema.json#/$defs/preview"}, registry=registry())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:160]}" for e in v.iter_errors(obj)]


@pytest.fixture
def core():
    c = Core()
    yield c
    c.close()


def test_point_watch_cycle_and_status_contracts(core: Core) -> None:
    _nid, vids = core.nest(n=2)
    tid = core.task({"type": "point_watch", "label": "定点", "params": {"point": [150.0, 80.0], "dwell_s": 20.0}})
    rt = core.rt
    T = rt.tasks[tid]
    assert T.state == "RUNNING"
    assert core.until(lambda: T.state == "DONE", 200.0)
    it = next(iter(T.items.values()))
    assert it.state == "DONE" and it.uav in vids
    assert it.dwell_s >= 19.0
    so = rt.sorties[it.uav]
    assert so.state == "STANDBY"
    st = rt.task_status(T)
    assert errors("rt/payloads/task_status.schema.json", st) == []
    obj = core.query("tasking/task", {"tid": tid, "include_plan": True})["item"]
    assert errors("tasking/task.schema.json", obj) == []
    # 待命 120 s 未分配返航，着陆后交机巢检查
    assert core.until(lambda: it.uav not in rt.sorties, 400.0)
    assert rt.nests["n-1"].air[it.uav].state in ("turnaround", "waiting_pack", "swapping", "ready")
    kinds = [k for k, _d in rt.event_log]
    for k in ("task.created", "task.allocated", "nest.launch", "nest.recover"):
        assert k in kinds
    msgs = {k for k, _m in core.sink}
    assert any(k.endswith("/tasks") for k in msgs) and any(k.endswith("/nests") for k in msgs)


def test_scan_preview_and_two_vehicles_distinct(core: Core) -> None:
    core.nest(n=3)
    spec = {"type": "area_scan_gsd", "params": {"polygon": [[200, -150], [500, -150], [500, 150], [200, 150]], "gsd_cm": 3.0, "t_max_s": 240}}
    q = core.query("tasking/preview", spec)
    assert q["code"] == 0 and q["state"] == "done", q
    pv = q["item"]
    assert _preview_errors(pv) == []
    assert pv["n_vehicles"] >= 2 and pv["scan"]["strip_w_m"] == pytest.approx(57.6, rel=0.01)
    assert pv["why_n"]["rule"] == "makespan" and len(pv["assignments"]) == pv["n_vehicles"]
    assert len({a["uav"] for a in pv["assignments"]}) == pv["n_vehicles"]
    tid = core.task({**spec, "preview_id": pv["preview_id"]})
    T = core.rt.tasks[tid]
    assert "PREVIEW_RECOMPUTED" not in T.warnings
    core.advance(20.0)
    uavs = [it.uav for it in T.items.values()]
    assert None not in uavs and len(set(uavs)) == len(uavs)
    assert core.until(lambda: T.state == "DONE", 900.0, 5.0)


def test_polyline_once_detours_keepout(core: Core) -> None:
    core.nest(n=1)
    tgt = core.trt
    r = core.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 800.0, None]}})     # 人的禁入圆半径约 320 m
    assert r["status"] == "accepted", r
    core.advance(1.0)
    tid = core.task({"type": "polyline_patrol", "params": {"waypoints": [[-600, 800], [600, 800]], "mode": "once", "alt_m": 60}})
    T = core.rt.tasks[tid]
    C = core.rt.circles(planning=False)
    assert len(C) >= 1
    for it in T.items.values():
        assert path_clear(it.w.path, C, 1.0)[0]
    assert core.until(lambda: T.state == "DONE", 900.0, 2.0)
    # 全程（含转场）在禁入体外、未进入配置体、未被发现
    rec = tgt._by_id(tgt.ids()[0])
    assert int(getattr(rec, "disc_n", 0)) == 0
    r = core.cmd("task/create", {"type": "polyline_patrol", "params": {"waypoints": [[-600, 800], [10, 805], [600, 800]]}})
    assert r["status"] == "rejected" and r["code"] == 583 and r["detail"]["waypoints"] == [1]


def test_limits_quota_and_unknown(core: Core) -> None:
    core.nest(n=1)
    for k in range(6):
        core.ok("task/create", {"type": "point_watch", "params": {"point": [100.0 * k, 50.0], "dwell_s": 5.0}})
    r = core.cmd("task/create", {"type": "point_watch", "params": {"point": [0.0, 0.0]}})
    assert r["status"] == "rejected" and r["code"] == 505
    big = [[0, 0], [2000, 0], [2000, 2000], [0, 2000]]
    r = core.cmd("task/create", {"type": "area_scan_gsd", "params": {"polygon": big, "gsd_cm": 3}})
    assert r["status"] == "rejected" and r["code"] in (585, 505)
    q = core.query("tasking/preview", {"type": "area_scan_gsd", "params": {"polygon": big, "gsd_cm": 3}})
    assert q["code"] == 585 and q["detail"]["item"] == "area_m2"
    r = core.cmd("task/create", {"type": "point_watch", "params": {"point": [0, 0], "bogus": 1}})
    assert r["status"] == "rejected" and r["code"] == 110
    r = core.cmd("task/start", {"tid": "t-99"})
    assert r["status"] == "rejected" and r["code"] == 305
    r = core.cmd("task/create", {"type": "point_watch", "params": {"point": [0, 0]}, "common": {"allocator": "llm_agent"}})
    assert r["code"] in (584, 505)


def test_manual_takeover_reallocates_and_release_to_standby(core: Core) -> None:
    _nid, _vids = core.nest(n=2)
    tid = core.task({"type": "point_watch", "params": {"point": [120.0, 60.0], "dwell_s": None}})
    T = core.rt.tasks[tid]
    it = next(iter(T.items.values()))
    assert core.until(lambda: it.uav is not None and core.rt.sorties[it.uav].state == "WORKING", 200.0)
    first = it.uav
    p = core.rt.pos(first)
    r = core.h.cmd("goto", {"pos": [float(p[0]) + 5.0, float(p[1]), float(p[2])]}, uav=first)   # 操作员导航命令隐式取得 OPERATOR 租约
    assert r["status"] == "accepted", r
    assert core.until(lambda: core.rt.sorties[first].state == "PREEMPTED", 3.0, 0.2)
    assert it.reason == "manual" or it.uav not in (None, first)
    assert any(k == "task.reallocated" and d.get("reason") == "manual_takeover" for k, d in core.rt.event_log)
    assert core.until(lambda: it.uav not in (None, first), 2.5, 0.1)          # M20-FR-046：2 s【仿真】内重新分配（含 1 Hz 判定粒度）
    other = it.uav
    assert other != first
    core.h.core.lease.release(core.h.slot(first), "p-op", return_to="none", uav=first, t_ns=core.h.core.clock.t_ns)
    assert core.until(lambda: core.rt.sorties.get(first) is None or core.rt.sorties[first].state in ("STANDBY", "RETURNING"), 10.0, 0.5)


def test_external_plan_stale_violation_and_accept(core: Core) -> None:
    core.nest(n=2)
    tid = core.task({"type": "point_watch", "params": {"point": [100.0, 0.0], "dwell_s": 10.0}, "common": {"allocator": "external"}})
    snap = core.query("tasking/snapshot")["item"]
    assert errors("tasking/snapshot.schema.json", snap) == []
    wid = f"{tid}.0"
    assert [w["wid"] for w in snap["work_items"]] == [wid]
    r = core.cmd("tasking/plan", {"based_on_revision": snap["revision"] - 1, "assignments": [{"wid": wid, "uav": "p600-01"}]})
    assert r["status"] == "rejected" and r["code"] == 105 and r["detail"]["why"] == "STALE_SNAPSHOT"
    snap = core.query("tasking/snapshot")["item"]
    r = core.cmd("tasking/plan", {"based_on_revision": snap["revision"], "assignments": [{"wid": wid, "uav": "nope-01"}]})
    assert r["status"] == "rejected" and r["code"] == 584 and r["detail"]["violations"][0]["code"] == 105
    snap = core.query("tasking/snapshot")["item"]
    r = core.cmd("tasking/plan", {"based_on_revision": snap["revision"], "assignments": [{"wid": wid, "uav": "p600-02"}]})
    assert r["status"] == "accepted", r
    res = r["detail"]["result"]
    assert res["applied"] and res["plan_id"].startswith("pl-")
    assert core.rt.tasks[tid].items[wid].uav == "p600-02"


def test_session_export_and_reload() -> None:
    a = Core()
    try:
        a.nest(pos=(30.0, 40.0), n=1)
        a.ok("task/create", {"type": "point_watch", "params": {"point": [100.0, 0.0], "dwell_s": None}})
        nests = a.reg.snapshot_exporters["nests"](a.h.core.session)
        tasks = a.reg.snapshot_exporters["tasks"](a.h.core.session)
        assert errors("tasking/nest.schema.json", nests[0]) == []
        assert tasks and tasks[0]["type"] == "point_watch"
    finally:
        a.close()
    b = Core()
    try:
        probs = b.h.core.session.apply({"nests": nests, "vehicles": [{"id": "p600-07", "model_id": "p600_mid360", "nest_id": nests[0]["id"]}],
                                        "tasks": tasks})
        assert probs == [], probs
        b.advance(3.0)
        assert list(b.rt.nests) == [nests[0]["id"]]
        T = next(iter(b.rt.tasks.values()))
        assert T.state in ("RUNNING", "READY")
        assert np.allclose(b.rt.pos("p600-07")[:2], np.array([30.0, 40.0]), atol=8.0)
    finally:
        b.close()


def test_route_recheck_reroutes_around_new_keepout(core: Core) -> None:
    """M20-FR-045：执行中的航线每 10 s 按识别物（含 60 s 预测位置）复核，违规即从当前位置重新绕行；转场全程在禁入体外。"""
    _nid, _vids = core.nest(n=1)
    tid = core.task({"type": "point_watch", "params": {"point": [0.0, 1800.0], "dwell_s": 30.0}})
    rt = core.rt
    T = rt.tasks[tid]
    it = next(iter(T.items.values()))
    assert core.until(lambda: it.uav in rt.sorties and rt.sorties[it.uav].state == "TRANSIT", 120.0, 1.0)
    so = rt.sorties[it.uav]
    assert core.until(lambda: rt.pos(it.uav)[1] > 150.0, 120.0, 1.0)
    r = core.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 1000.0, None]}})   # 敏感范围落在原直线航线上
    assert r["status"] == "accepted", r
    n0 = rt.ex.stats["reroutes"]
    assert core.until(lambda: rt.ex.stats["reroutes"] > n0, 15.0, 0.5)
    C = rt.circles(planning=False)
    ok, _mg = path_clear(so.path_cur[:, :2], C)
    assert ok
    assert any(k == "uav.keepout_guard" and d.get("action") == "reroute" for k, d in rt.event_log)


# ---------------------------------------------------------------- D2-FX-R2（验收第 1 轮 D2-AC-35：同时降落与原地降落）
def test_simultaneous_returns_land_in_turn(core: Core) -> None:
    """两架同时返航到同一机巢：回收按起降位时间线错开 ≥ launch_interval_s（M20-FR-003），后到者在机巢上方等待；不在机巢外原地降落。"""
    _nid, _vids = core.nest(n=2)
    t1 = core.task({"type": "point_watch", "params": {"point": [120.0, 60.0], "dwell_s": None}})
    t2 = core.task({"type": "point_watch", "params": {"point": [-120.0, 60.0], "dwell_s": None}})
    rt = core.rt
    assert core.until(lambda: len([s for s in rt.sorties.values() if s.state == "WORKING"]) == 2, 300.0)
    core.ok("task/cancel", {"tid": t1})
    core.ok("task/cancel", {"tid": t2})
    t_land: dict[str, float] = {}

    def watch() -> bool:
        for u, s in rt.sorties.items():
            if s.state == "RECOVERING" and u not in t_land:
                t_land[u] = rt.t
                p = rt.pos(u)
                assert np.hypot(p[0], p[1]) < 30.0, p            # 只在机巢上方降落
        return len(t_land) == 2

    assert core.until(watch, 300.0, 0.5)
    a, b = sorted(t_land.values())
    assert b - a >= 30.0 - 1.5


def test_returning_rejected_retries_instead_of_landing_in_place(core: Core) -> None:
    """返航航线被拒（安全动作进行中 101）：稍后重试返航，不在原地 land（第 1 轮两架让行悬停的 P600 原地降落 → POS_ERR_ELAND）。"""
    _nid, _vids = core.nest(n=1)
    tid = core.task({"type": "point_watch", "params": {"point": [250.0, 0.0], "dwell_s": None}})
    rt = core.rt
    assert core.until(lambda: any(s.state == "WORKING" for s in rt.sorties.values()), 300.0)
    so = next(iter(rt.sorties.values()))
    core.ok("task/cancel", {"tid": tid})
    assert so.state == "RETURNING"
    so.ret_climb = False
    so.call_op = "goto"
    rt.ex.on_result(so, "rejected", 101, None)
    assert so.state == "RETURNING" and so.retry_t is not None and so.call_op == "goto"
    assert core.until(lambda: so.uav not in rt.sorties, 300.0)
    assert any(k == "nest.recover" for k, _d in rt.event_log)


def test_return_route_short_of_home_holds_and_retries(core: Core) -> None:
    """返航航线止于机巢外（机巢落在运动识别物的规划禁入圆内）：原地等待后重试返航，只在机巢上方降落（D2-FX-R2：S7 中车辆驶近
    机巢箱 A，P600 降落在机巢外 55 m 处）。"""
    core.nest(n=1)
    tid = core.task({"type": "point_watch", "params": {"point": [250.0, 0.0], "dwell_s": None}})
    rt = core.rt
    assert core.until(lambda: any(s.state == "WORKING" for s in rt.sorties.values()), 300.0)
    so = next(iter(rt.sorties.values()))
    core.ok("task/cancel", {"tid": tid})
    assert so.state == "RETURNING"
    so.ret_climb = False
    so.call_op = "goto"
    rt.ex.on_result(so, "succeeded", 0, None)            # 航线"完成"时仍在机巢外 200 m 以上
    assert so.state == "RETURNING" and (so.action or {}).get("op") == "hold_home" and so.retry_t is not None
    landed_at: list = []

    def watch() -> bool:
        if so.state == "RECOVERING" and not landed_at:
            landed_at.append(tuple(rt.pos(so.uav)))
        return so.uav not in rt.sorties

    assert core.until(watch, 400.0, 0.5)
    assert landed_at and np.hypot(landed_at[0][0], landed_at[0][1]) < 30.0, landed_at


def test_return_blocked_home_forces_landing_after_wait(core: Core) -> None:
    """机巢长时间被禁入圆占据（S7 中车辆停在机巢箱 A 附近）：等待超过 HOME_WAIT_MAX_S 后保持高度直飞机巢上方降落、守卫让步，
    不一直等到 M09 能量 RTL。"""
    from awr.sim.tasking.executor import HOME_WAIT_MAX_S

    core.nest(n=1)
    tid = core.task({"type": "point_watch", "params": {"point": [250.0, 0.0], "dwell_s": None}})
    rt = core.rt
    assert core.until(lambda: any(s.state == "WORKING" for s in rt.sorties.values()), 300.0)
    so = next(iter(rt.sorties.values()))
    core.ok("task/cancel", {"tid": tid})
    so.ret_climb = False
    so.call_op = "goto"
    rt.ex.on_result(so, "succeeded", 0, None)
    assert (so.action or {}).get("op") == "hold_home" and not so.force_home
    so.home_wait_t0 = rt.t - HOME_WAIT_MAX_S - 1.0
    so.call_op = "goto"
    rt.ex.on_result(so, "succeeded", 0, None)
    assert so.force_home and so.call_op == "goto"
    assert any(k == "task.warning" and d.get("code") == "home_blocked_forced" for k, d in rt.event_log)
    assert core.until(lambda: so.uav not in rt.sorties, 400.0, 0.5)
