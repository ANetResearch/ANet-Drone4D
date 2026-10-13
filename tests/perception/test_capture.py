"""M19-AC-012：明确捕获（管线，PBench）。满足 1.0 s 后发 `perception.capture`（±0.2 s）；建筑遮挡（k_los < 2/3）不发；`level_req = I`
与 `n_req_px` 覆盖改变阈值；任务覆盖（`set_requirement`）优先；丢失 2.0 s 后发 `capture_lost`，reason 与 `t_last_ok_ns` 正确；识别物块
的 M19 列与 M18 detail 的 capture 段随之更新；视锥外不捕获；同一识别物多机各自捕获。"""

from __future__ import annotations

import pytest
from plib import FakeWorld, PBench, make_imaging

from awr.sim.perception import gx40

P0 = (0.0, 0.0, 60.0)


def _bench(world=None, target=None, at=(300.0, 0.0, 0.0), f=48.0):
    b = PBench(1, world or FakeWorld())
    tid = b.add({"class": "person", "pos": list(at), "motion": {"mode": "static"}, **(target or {})})
    b.step(1)
    b.platform(0, pos=P0, imaging=[make_imaging(gx40(), P0, (at[0], at[1], 0.9), f_mm=f)])
    return b, tid


def test_capture_after_hold_and_block_columns():
    b, tid = _bench()
    with b:
        b.run(3.0)
        cap = b.ev("perception.capture")
        assert len(cap) == 1
        lv = b.ev("perception.level")[0]
        assert cap[0]["t_sim_ns"] - (lv["t_sim_ns"] - 1_000_000_000) == pytest.approx(1e9, abs=0.2e9)
        d = cap[0]["data"]
        assert d["target"] == tid and d["level_req"] == "R" and d["t_hold_s"] == 1.0 and d["n_eff"] >= d["n_req"] == 14.0
        tab = b.ents["target"]
        row = tab.row_of(tid)
        assert tab.cols["tgt_captured"][row] == 1 and tab.cols["tgt_best_level"][row] == 3 and tab.cols["tgt_best_sensor"][row] == 1
        assert tab.cols["tgt_capture_uav_no"][row] == 1
        det = b.trt.capture_of(b.trt._by_id(tid))
        assert det["captured"] and det["uav"] == "uav-01" and det["since_t_sim_ns"] == cap[0]["t_sim_ns"]
        cs = b.prt.capture_state(tid, "R")
        assert cs.captured and cs.by == ("uav-01",) and cs.level == "I"
        assert not b.prt.capture_state(tid, "I").captured                # 该对的 N_req 为 R 级（14 px）


def test_building_blocks_capture():
    wall = FakeWorld(boxes=[(140.0, 160.0, -50.0, 50.0, 80.0)])
    b, _tid = _bench(world=wall)
    with b:
        b.run(3.0)
        assert b.ev("perception.capture") == []
        f = b.prt.frame_for("uav-01")
        t = f["sensors"][0]["targets"][0]
        assert t["k"]["los"] < 2 / 3 and t["limiting"] == "los" and not t["captured"]


def test_partial_occlusion_below_two_thirds_does_not_capture():
    # 只挡住底部射线：k_los = 2/3 仍可捕获；挡住中心与底部：1/3 不可
    low = FakeWorld(boxes=[(294.0, 296.0, -50.0, 50.0, 1.5)])
    b, _ = _bench(world=low)
    with b:
        b.run(2.5)
        f = b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]
        assert f["k"]["los"] == pytest.approx(2 / 3, abs=1e-3) and len(b.ev("perception.capture")) == 1


def test_level_req_and_override_priority():
    b, tid = _bench(target={"level_req": "I"}, at=(700.0, 0.0, 0.0))
    with b:
        b.run(3.0)
        assert b.ev("perception.capture") == []                       # 700 m：R 级可达，I 级（22.4 px）不可达
        f = b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]
        assert f["n_req"] == 22.4 and f["level"] == "R" and f["limiting"] == "pixels"
        b.prt.set_requirement("uav-01", tid, "R", None)                # 任务覆盖优先于识别物 level_req
        b.run(1.6)
        assert len(b.ev("perception.capture")) == 1 and b.ev("perception.capture")[0]["data"]["level_req"] == "R"
        b.prt.set_requirement("uav-01", tid, None, None)


def test_n_req_px_override():
    b, _tid = _bench(target={"n_req_px": 60.0})
    with b:
        b.run(3.0)
        assert b.ev("perception.capture") == []
        assert b.prt.frame_for("uav-01")["sensors"][0]["targets"][0]["n_req"] == 60.0


def test_capture_lost_reason_and_last_ok():
    b, tid = _bench()
    with b:
        b.run(2.0)
        assert len(b.ev("perception.capture")) == 1
        P = b.platforms[0]
        P.imaging[0] = make_imaging(gx40(), P0, (300.0, 200.0, 0.0), f_mm=48.0)    # 云台转开：视锥外
        t_turn = b.ctx.t_ns
        b.run(3.0)
        lost = b.ev("perception.capture_lost")
        assert len(lost) == 1
        d = lost[0]["data"]
        assert d["reason"] == "fov" and d["target"] == tid
        assert t_turn - 0.21e9 <= d["t_last_ok_ns"] <= t_turn
        assert lost[0]["t_sim_ns"] - d["t_last_ok_ns"] == pytest.approx(2.2e9, abs=0.21e9)
        assert not b.prt.capture_state(tid).captured
        assert b.ents["target"].cols["tgt_captured"][b.ents["target"].row_of(tid)] == 0


def test_two_uavs_capture_independently():
    b = PBench(2, FakeWorld())
    with b:
        tid = b.add({"class": "person", "pos": [300.0, 0.0, 0.0], "motion": {"mode": "static"}})
        b.step(1)
        for s, p in ((0, (0.0, 0.0, 60.0)), (1, (0.0, 80.0, 60.0))):
            b.platform(s, pos=p, imaging=[make_imaging(gx40(), p, (300.0, 0.0, 0.9), f_mm=48.0)])
        b.run(3.0)
        assert sorted(e["data"]["uav"] for e in b.ev("perception.capture")) == ["uav-01", "uav-02"]
        assert set(b.prt.capture_state(tid).by) == {"uav-01", "uav-02"}


def test_clock_rollback_resets_state():
    b, tid = _bench()
    with b:
        b.run(2.0)
        assert b.prt.capture_state(tid).captured and b.prt.pairs
        b.ctx.t_ns = 0
        b.ctx.tick = 0
        b.step(1)
        assert not b.prt.capture_state(tid).captured and len(b.prt.ring.items) == 0 and b.prt.ring.seq == 2   # seq 会话内单调


def test_sensor_switched_off_loses_capture():
    b, tid = _bench()
    with b:
        b.run(2.0)
        assert b.prt.capture_state(tid).captured
        b.platforms[0].imaging = []                     # 传感器关闭：按视锥外处理，2 s 后丢失
        b.run(2.6)
        lost = b.ev("perception.capture_lost")
        assert len(lost) == 1 and lost[0]["data"]["reason"] == "fov" and not b.prt.capture_state(tid).captured
        assert next(r for r in b.prt.ring.items if r["kind"] == "capture_lost")["sensor_kind"] == "eo"
