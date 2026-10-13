"""降级阶梯（M17-FR-021、FR-022、AC-004 的阶梯部分；AWR-04 §4.4.6）：L2 进入与 30 s 解除、L3 的 RTF 持续、共享展示 RTF、
L4 内存，以及超额分排序（超额会话先于无连接的空闲会话）。"""

from __future__ import annotations

from sbharness import make_sb

from awr.sandbox.ladder import Ladder, LadderInput, SessionLoad, reclaim_score


def _s(sid: str, rtf: float = 1.0, **kw) -> SessionLoad:
    d = {"c_i": 0.15, "granted": 1.0, "anon_mb": 140.0, "anon_expected_mb": 140.0, "ws": 1, "idle": False,
         "in_grant_window": False}
    d.update(kw)
    return SessionLoad(sid, rtf=rtf, **d)


def test_l2_throttle_enter_and_exit_hold() -> None:
    lad = Ladder(make_sb().ladder)
    t = 0.0
    assert lad.tick(LadderInput(now=t, cpu_stat={"nr_periods": 0, "nr_throttled": 0})) == 0
    for _ in range(5):
        t += 1
        lv = lad.tick(LadderInput(now=t, cpu_stat={"nr_periods": int(t * 10), "nr_throttled": int(t * 2)}))
    assert lv == 2
    periods, thr = int(t * 10), int(t * 2)
    for _ in range(15):     # 节流停止：窗口（10 s）滚出前与解除计时（30 s）内仍为 L2
        t += 1
        periods += 10
        lv = lad.tick(LadderInput(now=t, cpu_stat={"nr_periods": periods, "nr_throttled": thr}))
    assert lv == 2
    for _ in range(40):
        t += 1
        periods += 10
        lv = lad.tick(LadderInput(now=t, cpu_stat={"nr_periods": periods, "nr_throttled": thr}))
    assert lv == 0
    assert Ladder(make_sb().ladder).tick(LadderInput(now=0, psi_cpu=25.0)) == 2
    assert Ladder(make_sb().ladder).tick(LadderInput(now=0, budget_exhausted=True)) == 2


def test_l2_from_sessions_parent_throttle() -> None:
    """D2-FX-R2：会话父组 sandbox/sb/ 的 cpu.max 先于沙盒组生效，其限流同样触发 L2（沙盒组本身不再被限流）。"""
    lad = Ladder(make_sb().ladder)
    lv = 0
    for k in range(6):
        t = float(k)
        lv = lad.tick(LadderInput(now=t, cpu_stat={"nr_periods": k * 10, "nr_throttled": 0},
                                  cpu_stat_sb={"nr_periods": k * 10, "nr_throttled": k * 3}))
    assert lv == 2 and lad.last["throttle"] is not None and lad.last["throttle"] > 0.05


def test_l3_rtf_hold_and_grant_window() -> None:
    lad = Ladder(make_sb().ladder)
    lv = 0
    for t in range(0, 31):
        lv = lad.tick(LadderInput(now=float(t), sessions=[_s("a", 0.9, in_grant_window=t < 5)]))
        if t < 30:
            assert lv < 3, t
    assert lv < 3                    # 授予窗口内的 5 s 不计入持续时长
    for t in range(31, 37):
        lv = lad.tick(LadderInput(now=float(t), sessions=[_s("a", 0.9)]))
    assert lv == 3
    show = Ladder(make_sb().ladder)
    for t in range(0, 11):
        lv = show.tick(LadderInput(now=float(t), show_rtf=0.95))
    assert lv == 3


def test_l4_and_l1() -> None:
    assert Ladder(make_sb().ladder).tick(LadderInput(now=0, mem_mb=1200.0)) == 4
    lad = Ladder(make_sb().ladder)
    for t in range(11):
        lv = lad.tick(LadderInput(now=float(t), psi_mem=15.0))
    assert lv == 4
    assert Ladder(make_sb().ladder).tick(LadderInput(now=0, full=True)) == 1


def test_reclaim_score_order() -> None:
    hog = _s("hog", 0.8, granted=5.0)
    idle = _s("idle", 1.0, ws=0, idle=True)
    normal = _s("ok", 1.0)
    order = sorted([idle, normal, hog], key=lambda x: reclaim_score(x, 3, 1.0, 3), reverse=True)
    assert [x.sid for x in order] == ["hog", "ok", "idle"]
    mem = _s("mem", 1.0, anon_mb=300.0)
    assert reclaim_score(mem, 4, 1.0, 3) > reclaim_score(mem, 3, 1.0, 3) > reclaim_score(normal, 3, 1.0, 3)
