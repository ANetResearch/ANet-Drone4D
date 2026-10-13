"""M20-AC-008、AC-021 的功能部分：5 Hz 禁入守卫——识别物逼近在岗机时预测 5 s 内进入"禁入体 + v_t·τ_pred"即撤离，宁可间隙也不进入，
撤离后恢复；进入配置体 0、被发现 0（M20-FR-044、FR-056）。"""

from __future__ import annotations

import math

from m20core import Core

from awr.tasking.keepout import signed_margin


def test_target_approach_triggers_evade_and_never_enters() -> None:
    core = Core()
    try:
        core.nest(pos=(0.0, -400.0), n=1)
        r = core.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 1300.0, None],
                                               "motion": {"mode": "path", "path": [[0.0, 1300.0, None], [0.0, 300.0, None]], "speed_mps": 2.5,
                                                          "loop": "once"}}})
        assert r["status"] == "accepted", r
        core.advance(1.0)
        tid = core.task({"type": "point_watch", "params": {"point": [0.0, 600.0], "dwell_s": None}})
        rt = core.rt
        it = next(iter(rt.tasks[tid].items.values()))
        min_margin = math.inf
        evaded = False
        for _ in range(int(400 / 0.5)):
            core.advance(0.5)
            for uav, so in rt.sorties.items():
                if not so.airborne:
                    continue
                C = rt.circles(planning=False)
                if len(C):
                    min_margin = min(min_margin, float(signed_margin(rt.pos(uav)[None, :2], C)[0]))
            if any(k == "uav.keepout_guard" and d.get("action") == "evade" for k, d in rt.event_log):
                evaded = True
            if evaded and core.t > 380:
                break
        assert evaded, "禁入守卫未触发撤离"
        assert min_margin >= 0.0, min_margin
        tgt = core.trt._by_id(core.trt.ids()[0])
        assert int(getattr(tgt, "disc_n", 0)) == 0
        assert it.uav is None or it.uav in rt.sorties
    finally:
        core.close()


def test_multirotor_evade_climbs_first_when_low_and_debounces() -> None:
    """D2-FX-R2：多旋翼撤离走廊在当前高度 DSM 净空不足时先原地爬升再水平撤离（刚起飞 10 m 高度向楼群直飞撤离，细校验 102 每 0.2 s
    一次）；撤离点未变时 5 Hz 上不重发（第 2 轮诊断 542 次 206 superseded）。"""
    import numpy as np

    core = Core()
    try:
        core.nest(n=1)
        core.task({"type": "point_watch", "params": {"point": [300.0, 0.0], "dwell_s": None}})
        rt = core.rt
        assert core.until(lambda: any(s.state == "TRANSIT" for s in rt.sorties.values()), 120.0, 0.1)
        so = next(iter(rt.sorties.values()))
        p = rt.pos(so.uav)
        assert p[2] < 13.0, p
        sent: list = []
        orig = rt.ex.submit

        def spy(s, op, args):
            sent.append((op, dict(args)))
            return orig(s, op, args)

        rt.ex.submit = spy
        C = np.array([[float(p[0]) + 40.0, float(p[1]), 60.0]])
        rt.guard.evade(so, C, 0, p, None)
        op, args = sent[-1]
        assert so.state == "EVADING" and op == "goto" and so.evade_climb is not None
        assert math.hypot(args["pos"][0] - p[0], args["pos"][1] - p[1]) < 1e-6 and args["pos"][2] >= 15.0 - 1e-6
        n = len(sent)
        rt.guard.evade(so, C, 0, rt.pos(so.uav), None)          # 同一撤离点：不重发
        assert len(sent) == n
        rt.guard.evaded(so, True)                                # 爬升完成：水平撤离
        op, args = sent[-1]
        assert op == "goto" and so.evade_climb is None and math.hypot(args["pos"][0] - p[0], args["pos"][1] - p[1]) > 25.0
        assert args["pos"][2] >= 15.0 - 1e-6
    finally:
        core.close()


def test_yaw_goto_steps_large_turns() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：带机头朝向的 goto 每次相对当前航向最多转 YAW_STEP_MAX_DEG（M09 快速守卫在悬停中航向误差
    > 90° 即 ELAND），余量在 goto 完成后续转；其他指令取代分步转向。"""
    from types import SimpleNamespace

    from awr.sim.tasking.book import SortieRT
    from awr.sim.tasking.executor import YAW_STEP_MAX_DEG, Executor

    sent: list[dict] = []
    st = {"yaw": 0.0}
    rt = SimpleNamespace(t=100.0, fs=lambda vid: "FLYING", yaw=lambda vid: st["yaw"], pos=lambda vid: [1.0, 2.0, 50.0],
                         cmd=SimpleNamespace(submit_internal=lambda req, pr: sent.append(req) or {"status": "accepted"}), results=[])
    ex = Executor(rt)
    so = SortieRT("p600-01", 0, "nest-a", "s1", state="WORKING")
    ex.yaw_goto(so, [1.0, 2.0, 50.0], math.radians(170.0))
    assert math.isclose(math.degrees(sent[-1]["args"]["yaw_rad"]), YAW_STEP_MAX_DEG) and so.yaw_goal == math.radians(170.0)
    st["yaw"] = sent[-1]["args"]["yaw_rad"]
    assert ex._yaw_continue(so) and math.isclose(math.degrees(sent[-1]["args"]["yaw_rad"]), 2 * YAW_STEP_MAX_DEG)
    st["yaw"] = sent[-1]["args"]["yaw_rad"]
    assert ex._yaw_continue(so) and math.isclose(math.degrees(sent[-1]["args"]["yaw_rad"]), 170.0)
    st["yaw"] = math.radians(168.0)
    assert not ex._yaw_continue(so) and so.yaw_goal is None
    ex.yaw_goto(so, [1.0, 2.0, 50.0], math.radians(-60.0))
    assert so.yaw_goal is not None
    ex.submit(so, "hover", {})
    assert so.yaw_goal is None


def test_yield_after_separation_hold_and_evade_pad() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：①间隔让行解除后两机仍在近处时，编号大者先原地升高 YIELD_STEP_M 让对方先走，对方水平离开
    YIELD_R_M 后才续飞（此前两机同时续飞、三轮后在 1.5 m 处相撞）；②反复撤离时转场与返航的规划圆外扩。"""
    from types import SimpleNamespace

    import numpy as np

    from awr.sim.tasking.book import SortieRT
    from awr.sim.tasking.executor import EVADE_PAD_M, YIELD_R_M, YIELD_STEP_M, Executor

    sent: list[dict] = []
    pos = {"p600-02": [0.0, 0.0, 100.0], "p600-04": [2.0, 0.0, 100.0]}
    a = SortieRT("p600-02", 0, "nest-a", "s1", state="WORKING")
    b = SortieRT("p600-04", 1, "nest-a", "s2", state="WORKING")
    rt = SimpleNamespace(t=100.0, fs=lambda vid: "FLYING", pos=lambda vid: np.asarray(pos[vid], float), sorties={"p600-02": a, "p600-04": b},
                         model=lambda mid: SimpleNamespace(frame="multirotor"), model_id=lambda vid: "p600_mid360",
                         dtm=lambda xy: np.zeros(len(xy)), dsm=lambda xy: np.zeros(len(xy)), yaw=lambda vid: 0.0,
                         cmd=SimpleNamespace(submit_internal=lambda req, pr: sent.append(req) or {"status": "accepted"}), results=[])
    ex = Executor(rt)
    assert ex._yield(a, 100.0) and not sent              # 编号小者不避让，但避让方仍停在其前方：等对方让开
    b.action = {"op": "path", "P": [[0.0, 0.0, 100.0]]}
    assert ex._yield(b, 100.0) and b.action["op"] == "yield"
    assert sent[-1]["uav"] == "p600-04" and sent[-1]["args"]["pos"][2] == 100.0 + YIELD_STEP_M
    pos["p600-04"][2] += YIELD_STEP_M                    # 升高后高差够了，但对方仍在水平近处：继续等
    assert ex._yield(b, 103.0)
    assert not ex._yield(a, 103.0) and a.yield_t0 is None    # 避让方已让开：先行方续飞
    pos["p600-02"][0] = -(YIELD_R_M + 1.0)               # 对方离开
    assert not ex._yield(b, 106.0) and b.action["op"] == "path" and b.yield_t0 is None
    # 对方已在 30 m 外，但其前方航线与本机续飞航线在前方相交：仍等待；对方航线离开后续飞
    pos["p600-02"] = [-30.0, 40.0, 100.0]
    a.path_cur = np.array([[-30.0, 40.0, 100.0], [30.0, 40.0, 100.0]])
    b.action = {"op": "path", "P": np.array([[2.0, 0.0, 100.0], [2.0, 80.0, 100.0]])}
    assert ex._yield(b, 110.0)
    a.path_cur = np.array([[-30.0, 40.0, 100.0], [-90.0, 40.0, 100.0]])
    assert not ex._yield(b, 113.0)
    # 撤离外扩
    b.evade_ts = [10.0, 50.0, 90.0]
    assert ex.evade_pad("p600-04") == 2 * EVADE_PAD_M
    b.evade_ts = [90.0]
    assert ex.evade_pad("p600-04") == 0.0


def test_sidestep_before_vertical_climb_through_other_vehicle() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：原地垂直升降的竖直走廊内有他机（巡线两机停在同一终点上方 70 m 与 80 m）时，先在当前高度
    水平让开 VERT_SIDESTEP_M；走廊无他机时直接升降。"""
    from types import SimpleNamespace

    import numpy as np

    from awr.sim.tasking.book import SortieRT
    from awr.sim.tasking.executor import VERT_CLEAR_M, VERT_SIDESTEP_M, Executor

    pos = {"p600-01": [0.0, 0.0, 80.0], "p600-02": [1.0, 0.0, 70.0]}
    a = SortieRT("p600-01", 0, "nest-a", "s1", state="STANDBY")
    b = SortieRT("p600-02", 1, "nest-a", "s2", state="RETURNING")
    rt = SimpleNamespace(t=0.0, pos=lambda vid: np.asarray(pos[vid], float), sorties={"p600-01": a, "p600-02": b}, world=None,
                         dsm=lambda xy: np.zeros(len(xy)), dtm=lambda xy: np.zeros(len(xy)))
    ex = Executor(rt)
    g = ex._sidestep(b, 110.0)
    assert g is not None and math.isclose(g[2], 70.0)
    assert math.isclose(math.hypot(g[0] - 1.0, g[1]), VERT_SIDESTEP_M) and math.hypot(g[0], g[1]) >= VERT_CLEAR_M
    assert ex._sidestep(b, 75.0) is None                 # 升到 75 m：走廊（70–75 ± 3 m）内没有 80 m 的他机
    pos["p600-01"] = [20.0, 0.0, 80.0]
    assert ex._sidestep(b, 110.0) is None

