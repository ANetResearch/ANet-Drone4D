"""M20-AC-017 至 AC-019 的功能部分、AC-023（统计口径）、AC-028/029 的触发部分：接力预检（581）、站位派遣、明确捕获的 G(t)、
能量接替与双机重叠 ≥ 15 s 的交接、间隙归类、on_detect = relay 与捕获机转岗、统计契约。

感知以替身注入（`FakePerception`：在站位 60 m 内且处于 WORKING 的机体对识别物明确捕获），可行窗口与站位用 M19 正式纯函数与 M20 作业。"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pytest
from ctlib import errors
from m20core import Core

TARGET_POS = (0.0, 900.0)


@dataclass
class _CS:
    captured: bool
    by: tuple
    since_ns: int | None
    t_last_ok_ns: int | None
    level: str = "R"


class FakePerception:
    def __init__(self, core: Core, radius_m: float = 60.0) -> None:
        self.core = core
        self.r = radius_m
        self.blind = False
        self.req: dict = {}
        self.cue: dict = {}
        self.last_ok: int | None = None

    def _capturing(self) -> list[str]:
        rt = self.core.rt
        out = []
        if self.blind:
            return out
        for uav, so in sorted(rt.sorties.items()):
            if so.state != "WORKING":
                continue
            _T, it = rt.ex.item_of(so)
            st = rt.relays.station_for(so, it) if it is not None and it.w.kind == "relay_station" else None
            p = rt.pos(uav)
            if (st is not None and math.dist(p[:2], st[:2]) <= self.r) or (it is not None and it.w.kind != "relay_station" and math.dist(p[:2], TARGET_POS) <= 450.0):
                out.append(uav)
        return out

    def capture_state(self, target: str, level_req: str = "R") -> _CS:
        by = tuple(u for u in self._capturing() if self.core.rt.sorties[u].wid and
                   self.core.rt.ex.item_of(self.core.rt.sorties[u])[1].w.kind == "relay_station")
        t = self.core.rt.t_ns
        if by:
            self.last_ok = t
        return _CS(bool(by), by, None, self.last_ok)

    def capture_detail(self, target: str) -> dict:
        c = self._capturing()
        return {"best_level": "R" if c else "none", "uav": c[0] if c else None}

    def track(self, target: str, t=None):
        return None

    def set_requirement(self, uav, target, level_req, n_req_px) -> None:
        self.req[uav] = level_req

    def set_cue(self, uav, on) -> None:
        self.cue[uav] = on


@pytest.fixture
def core(monkeypatch):
    c = Core()
    fp = FakePerception(c)
    monkeypatch.setattr(c.rt, "perception_rt", lambda: fp)
    c.fp = fp
    yield c
    c.close()


def _target(core: Core) -> str:
    r = core.cmd("target/add", {"target": {"class": "person", "pos": [TARGET_POS[0], TARGET_POS[1], None]}})
    assert r["status"] == "accepted", r
    core.advance(1.0)
    return r["detail"]["result"]["id"]


def test_relay_dispatch_capture_and_handover(core: Core) -> None:
    core.nest(n=3)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    rt = core.rt
    T = rt.tasks[tid]
    assert T.state == "RUNNING", (T.state, T.last_error)
    r = T.relay
    assert core.until(lambda: r.station is not None, 20.0)
    C = rt.circles(planning=False)
    assert all(math.hypot(r.station[0] - c[0], r.station[1] - c[1]) >= c[2] for c in C)
    assert core.until(lambda: r.on_station is not None and rt.sorties.get(r.on_station) is not None
                      and rt.sorties[r.on_station].state == "WORKING", 400.0, 2.0)
    assert core.until(lambda: r.first_capture is not None, 30.0)
    a = r.on_station
    assert core.fp.req.get(a) == "R"
    obj = core.query("tasking/task", {"tid": tid, "include_plan": True})["item"]
    assert errors("tasking/task.schema.json", obj) == []
    sts = obj["plan"]["geometry"]["stations"]
    assert sts[0]["role"] == "primary" and sts[0]["uav"] == a       # 站位几何按当前角色实时给出（图层的分配着色）
    rev0 = T.revision
    assert any(k == "relay.dispatch" and d.get("reason") == "initial" for k, d in rt.event_log)
    # 能量接替：在岗机低电 → 交接站位派 B → 双机同时捕获 ≥ 15 s 后 A 返航（SOC 取 0.45：t_end 约 6 min，按 ETA 提前派遣；
    # D2-FX-R2 起在岗机到 t_end 即离岗，不再越过 20% 储备等待）
    rt.set_soc(a, 0.45)
    assert core.until(lambda: r.handovers == 1, 600.0, 2.0)
    assert r.on_station != a and r.overlaps[0] >= 15.0
    assert T.revision > rev0                                        # 分配与站位变化使计划 revision 增长（17 §17.7.4）
    assert any(k == "relay.handover" and d.get("from_uav") == a for k, d in rt.event_log)
    assert core.until(lambda: a not in rt.sorties or rt.sorties[a].state in ("RETURNING", "RECOVERING"), 60.0)
    st = rt.relays.stats_wire(T)
    assert errors("tasking/relay_stats.schema.json", st) == []
    assert st["gaps"]["count"] == 0 and st["discovered"] == 0 and st["handovers"]["count"] == 1
    assert st["coverage"] > 0.9
    assert st["min_keepout_margin_m"] is None or st["min_keepout_margin_m"] >= 0.0


def test_gap_classified_and_closed(core: Core) -> None:
    core.nest(n=2)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    r = core.rt.tasks[tid].relay
    assert core.until(lambda: r.first_capture is not None, 400.0, 2.0)
    core.fp.blind = True
    core.advance(6.0)
    core.fp.blind = False
    assert core.until(lambda: r.gap_t0 is None and sum(r.gaps_n.values()) == 1, 20.0, 0.5)
    assert r.gaps_n["other"] == 1 and 5.0 <= r.gap_max <= 12.0      # 失捕 > 5 s 后重新选站（替身只在站位 60 m 内捕获）
    assert any(k == "relay.gap" for k, _d in core.rt.event_log)


def test_precheck_night_infeasible_581(core: Core, monkeypatch) -> None:
    core.nest(n=2)
    tgt = _target(core)
    monkeypatch.setattr(core.rt, "light", lambda: ("night", 10000.0))
    # P600 2.0.0 的对象形式映射带可选外挂照明器 nir_ill（机型能力含可选挂载，夜间 464.8 m 可行）；本用例验证"只有机内 NIR"的
    # 不可行路径：以不带外挂照明器的 GX40 规格代替（D2-INT）
    from awr.sim.perception.specs import gx40, gx40_nir
    from awr.sim.tasking import relay as RL

    monkeypatch.setattr(RL, "station_specs", lambda T, mid, band="day": [("eo", gx40_nir(False) if band == "night" else gx40("1080p"))])
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    T = core.rt.tasks[tid]
    assert T.state == "INFEASIBLE"
    assert T.last_error["code"] == 581
    assert T.last_error["why"] in ("night_sensor", "keepout_gt_range")      # 机内 NIR 上界约 195 m < 禁入半径 320 m
    assert T.last_error["detail"]["suggestions"]


def test_on_detect_relay_child_and_capture_vehicle_converts(core: Core) -> None:
    core.nest(n=3)
    tgt = _target(core)
    poly = [[-500, 300], [500, 300], [500, 600], [-500, 600]]
    tid = core.task({"type": "area_patrol", "params": {"polygon": poly, "revisit_s": 900}, "common": {"on_detect": "relay"}})
    rt = core.rt
    T = rt.tasks[tid]
    assert T.state == "RUNNING"
    assert core.until(lambda: any(rt.tasks[c].type == "relay_watch" for c in T.children), 600.0, 2.0)
    child = next(rt.tasks[c] for c in T.children if rt.tasks[c].type == "relay_watch")
    assert core.until(lambda: child.relay is not None, 10.0, 0.5)
    assert child.parent == tid and child.relay.target == tgt
    assert child.relay.first_uav is not None
    assert core.until(lambda: child.relay.on_station is not None, 120.0, 1.0)
    assert child.relay.on_station == child.relay.first_uav            # 捕获机转岗为首个在岗机（FR-060）
    # 巡查子区被重新分配
    assert core.until(lambda: all(it.uav != child.relay.on_station for it in T.items.values()), 30.0)


def test_search_after_lost_and_resume_in_place(core: Core) -> None:
    """M20-FR-057（P1）与 D2-FX-R2：明确捕获丢失 > 5 s 进入 SEARCH（task.warning{search}），先以航迹预测位置重新选站（≤ 3 次），
    仍未重获时飞外扩方形航线（禁入圆外）；整个过程在岗机不返航（第 1 轮搜索航段被拒三次即返航，新机到站立即重复，空转 18 h）；
    重新捕获后回到 TRACKING，间隙记 other。"""
    core.fp.r = 5000.0                     # 替身：WORKING 的在岗机不在盲区时总能捕获（搜索中也能重新捕获）
    core.nest(n=2)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    rt = core.rt
    T = rt.tasks[tid]
    r = T.relay
    assert core.until(lambda: r.first_capture is not None, 400.0, 2.0)
    a = r.on_station
    core.fp.blind = True
    assert core.until(lambda: r.state == "SEARCH", 20.0, 0.5)
    assert any(k == "task.warning" and d.get("code") == "search" for k, d in rt.event_log)
    assert core.until(lambda: r.reacq_n >= 1, 10.0, 0.5)                 # 先重新选站
    seen_search = []

    def searching() -> bool:
        so = rt.sorties.get(a)
        if so is not None and (so.action or {}).get("search") and so.call_op == "follow_path":
            seen_search.append(rt.t)
        return bool(seen_search)

    assert core.until(searching, 200.0, 1.0)                              # 选站 3 次未重获后外扩方形
    assert rt.sorties[a].state == "WORKING" and r.on_station == a         # 在岗机不返航
    core.advance(8.0)
    C = rt.circles(planning=False)
    p = rt.pos(a)
    assert all(math.hypot(p[0] - c[0], p[1] - c[1]) >= c[2] for c in C)
    core.fp.blind = False
    assert core.until(lambda: r.state == "TRACKING", 20.0, 0.5)
    assert r.on_station == a
    assert r.gaps_n["other"] >= 1
    st = rt.relays.stats_wire(T)
    assert st["gaps"]["count"] == sum(r.gaps_n.values())


def test_open_gap_counted_and_finalized(core: Core) -> None:
    """D2-FX-R2：进行中的间隙计入统计（第 1 轮 (a) 入夜后 18 h 无捕获，统计记间隙 0）；批处理结束 finalize 闭合并发 relay.gap。"""
    core.nest(n=2)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    rt = core.rt
    T = rt.tasks[tid]
    r = T.relay
    assert core.until(lambda: r.first_capture is not None, 400.0, 2.0)
    core.fp.blind = True
    core.advance(30.0)
    st = rt.relays.stats_wire(T)
    assert st["gaps"]["count"] == 1 and st["gaps"]["max_s"] >= 25.0
    assert st["coverage"] < 1.0
    assert not any(k == "relay.gap" for k, _d in rt.event_log)
    rt.relays.finalize(T)
    assert sum(r.gaps_n.values()) == 1 and r.gap_t0 is None
    assert any(k == "relay.gap" for k, _d in rt.event_log)
    assert rt.relays.stats_wire(T)["gaps"]["count"] == 1


def test_handover_slow_moves_relief_to_alternative(core: Core) -> None:
    """M20 §6.9.4：接替机到达交接站位 90 s 仍未与在岗机同时捕获 → 保持在岗机，以站位作业为接替机另选站位（视线复核、回避原
    交接点，D2-FX-R2：此前以旋转 ±30° 的点代替且不复核视线，第 1 轮 (a) 夜间换到的点不可达），记 handover_slow。"""
    core.nest(n=3)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    rt = core.rt
    T = rt.tasks[tid]
    r = T.relay
    assert core.until(lambda: r.first_capture is not None and r.on_station is not None, 400.0, 2.0)
    a = r.on_station
    orig = core.fp._capturing
    core.fp._capturing = lambda: [u for u in orig() if u == a]          # 接替机始终看不到
    rt.set_soc(a, 0.36)
    assert core.until(lambda: r.relief is not None and rt.sorties.get(r.relief) is not None
                      and rt.sorties[r.relief].state == "WORKING", 600.0, 2.0)
    b = r.relief
    _T, it_b = rt.ex.item_of(rt.sorties[b])
    p0 = tuple(it_b.w.pos)
    rt.set_soc(a, 0.9)          # 在岗机能量充足（否则到 t_end 即交接返航，M20 §6.9.4 "A 若此时到达自身 t_end 则先返航"）
    assert core.until(lambda: "handover_slow" in T.warnings, 200.0, 1.0)
    assert r.on_station == a and r.handovers == 0
    assert core.until(lambda: tuple(it_b.w.pos) != p0, 10.0, 0.5)      # 站位作业为接替机另选站位（回避原交接点）
    C = rt.circles(planning=True)
    assert all(math.hypot(it_b.w.pos[0] - c[0], it_b.w.pos[1] - c[1]) >= c[2] for c in C)


def test_monitoring_starts_with_relay_vehicle_capture(core: Core) -> None:
    """D2-FX-R2：接力机到站前其他机体（触发接力的巡查机）的零星捕获不开始监控与间隙计时（S7 诊断中 AWR-V1 看洞时的捕获使接力在
    P600 到站前就记了 4 次 keepout_priority 与 1 次 other）；接力机首次捕获后其他机体的捕获照常计入 G(t)。"""
    core.nest(n=2)
    tgt = _target(core)
    rt = core.rt
    orig = core.fp.capture_state
    seen = {"n": 0}

    def cs(target: str, level_req: str = "R") -> _CS:
        c = orig(target, level_req)
        if c.captured:
            return c
        seen["n"] += 1
        on = seen["n"] % 50 < 25                            # 巡查机时有时无（每 5 s 切换）
        return _CS(on, ("patrol-x",) if on else (), None, rt.t_ns if on else None)

    core.fp.capture_state = cs
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    r = rt.tasks[tid].relay
    core.advance(20.0)
    assert r.first_capture is None and r.gap_t0 is None and sum(r.gaps_n.values()) == 0
    assert core.until(lambda: r.first_capture is not None, 400.0, 1.0)
    a = rt.sorties.get(r.on_station or "")
    assert a is not None and a.state == "WORKING"
    assert sum(r.gaps_n.values()) == 0


def test_relief_offset_picks_candidate_with_line_of_sight(core: Core, monkeypatch) -> None:
    """D2-FX-R2：侧向错开的交接点逐个复核三射线视线（S7 诊断中错开点被楼挡住，接替机到站 60 s 无捕获，以重叠 0 交接）。"""
    import numpy as np

    core.nest(n=1)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    r = core.rt.tasks[tid].relay
    cands = [(10.0, 0.0, 60.0), (20.0, 0.0, 60.0), (30.0, 0.0, 70.0)]

    def los(A, B):
        A = np.asarray(A, float)
        return np.isclose(A[:, 0], 30.0)                 # 只有第 3 个候选三条射线可见

    monkeypatch.setattr(core.rt, "los", los)
    assert core.rt.relays._los_pick(r, cands, (*TARGET_POS, 0.0)) == cands[2]
    monkeypatch.setattr(core.rt, "los", lambda A, B: None)
    assert core.rt.relays._los_pick(r, cands, (*TARGET_POS, 0.0)) == cands[0]


def test_flicker_marks_station_bad(core: Core) -> None:
    """D2-FX-R2：视线闪断（180 s 内失捕 ≥ 5 次，每次很快重获）时当前站位记为坏站位并告警 flicker（此后的接替选站回避它）。"""
    core.nest(n=3)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    rt = core.rt
    T = rt.tasks[tid]
    r = T.relay
    assert core.until(lambda: r.first_capture is not None and r.on_station is not None, 400.0, 2.0)
    a = r.on_station
    for _ in range(6):
        core.fp.blind = True
        core.advance(1.0)
        core.fp.blind = False
        core.advance(4.0)
    assert "flicker" in T.warnings and r.bad and r.on_station == a
    assert any(k == "task.warning" and d.get("code") == "flicker" for k, d in rt.event_log)


def test_station_circles_moving_target_and_window_hysteresis(core: Core) -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-19 (b) 霾时段）：①运动识别物的接力站位禁入圆取守卫口径基础半径 + v·τ_pred + 跟随容差 + 2 m（小于规划口径的
    v·τ_route 外扩；夜间 3 km 霾中游走的人的窗口约 330–350 m，规划口径 345 m 使站位无解）；静止识别物在规划口径上加 5 m 余量；
    ②在岗站位的窗口核对带迟滞：选站留 WINDOW_MARGIN_M，核对只在越出上界 WINDOW_TOL_M 以上才判出窗。"""
    from awr.sim.tasking import relay as RL

    core.nest(n=2)
    r1 = core.cmd("target/add", {"target": {"class": "person", "pos": [0.0, 900.0, None],
                                            "motion": {"mode": "random_walk", "speed_mps": 1.2,
                                                       "region": [[-30.0, 870.0], [30.0, 870.0], [30.0, 930.0], [-30.0, 930.0]]}}})
    r2 = core.cmd("target/add", {"target": {"class": "person", "pos": [900.0, 0.0, None], "motion": {"mode": "static"}}})
    assert r1["status"] == "accepted" and r2["status"] == "accepted"
    core.advance(1.0)
    rt = core.rt
    plan = {k.id: k.r for k in rt.keepouts(planning=True)}
    sc = rt.relays.station_circles()
    mov = r1["detail"]["result"]["id"]
    sta = r2["detail"]["result"]["id"]
    seen = set()
    for i, k in enumerate(rt.keepouts(planning=False)):
        if k.target == mov:
            assert sc[i, 2] == pytest.approx(k.r + 1.2 * RL.GUARD_TAU_S + RL.FOLLOW_TOL_M + 2.0, abs=0.01)
            assert sc[i, 2] < plan[k.id]
        elif k.target == sta:
            assert sc[i, 2] == pytest.approx(plan[k.id] + RL.STATION_KEEP_M, abs=0.01)
        seen.add(k.target)
    assert {mov, sta} <= seen
    # 迟滞：窗口 [300, 350]（AGL 60），站位 352 m：选站口径不合格，核对口径（越界 ≤ 3 m）仍合格
    tid = core.task({"type": "relay_watch", "params": {"target": sta}})
    r = rt.tasks[tid].relay
    ws = [(60.0, 300.0, 350.0)]
    tp = rt.relays._track_pos(r)
    pos = (tp[0] - 352.0, tp[1], 60.0)
    assert not rt.relays.station_ok(r, pos, ws, RL.WINDOW_MARGIN_M)
    assert rt.relays.station_ok(r, pos, ws, -RL.WINDOW_TOL_M)
    assert not rt.relays.station_ok(r, (tp[0] - 356.0, tp[1], 60.0), ws, -RL.WINDOW_TOL_M)


def test_station_job_failure_retries_instead_of_581(core: Core, monkeypatch) -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-45 真实栈诊断）：站位作业本身失败（规划服务超时、出错）不判 581，稍后重选；连续失败
    JOB_FAIL_MAX 次后才按结果判定。"""
    import awr.sim.tasking.jobs as J

    orig = J.run_kind
    n = {"fail": 2}

    def rk(kind, payload, world=None):
        if kind == "tasking.station" and n["fail"] > 0:
            n["fail"] -= 1
            return {"ok": False, "code": 125, "why": "plan_failed"}
        return orig(kind, payload, world)

    monkeypatch.setattr(J, "run_kind", rk)
    core.nest(n=3)
    tgt = _target(core)
    tid = core.task({"type": "relay_watch", "params": {"target": tgt}})
    T = core.rt.tasks[tid]
    assert core.until(lambda: T.relay.station is not None, 60.0)
    assert T.state == "RUNNING" and n["fail"] == 0, (T.state, T.last_error)


def test_relief_station_not_through_or_at_incumbent() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-19 (b)）：接替机的目标点离在岗机 < HANDOVER_DH_M，或从接替机当前位置直飞过去要穿过在岗机时判为
    受阻（站位改为错开）；在岗机在侧方时不受阻。"""
    from types import SimpleNamespace

    import numpy as np

    from awr.sim.tasking.book import SortieRT
    from awr.sim.tasking.relay import HANDOVER_DH_M, RelayController

    pos = {"a": [200.0, 0.0, 120.0], "b": [0.0, 0.0, 60.0]}
    rc = RelayController.__new__(RelayController)
    rc.rt = SimpleNamespace(pos=lambda vid: np.asarray(pos[vid], float))
    A, B = SortieRT("a", 0, None, "s1"), SortieRT("b", 1, None, "s2")
    assert rc._relief_blocked(B, A, (219.0, 0.0))                       # 在岗机在接替机与目标点之间
    assert rc._relief_blocked(B, A, (200.0, HANDOVER_DH_M - 1.0))      # 目标点离在岗机太近
    assert not rc._relief_blocked(B, A, (200.0, 40.0))                  # 侧方错开
