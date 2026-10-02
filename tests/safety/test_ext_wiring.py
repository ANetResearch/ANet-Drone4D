"""M09 ext 接线（FX-SIM2；M09-to-M08 第 4、6 条，M08-to-M09 第 1 条；D1-AC-11b 功能部分）：

- escalate 经 CommandEngine 端到端：缺确认令牌 112；FLYING → HOLD/ESCALATE → ELAND；2 s 内再次升级 105（M09 第④步）；
- `fault/inject`、`fault/clear` 命令路由：accepted → running → succeeded（result 带 fault_id），写输入日志，席位与角色检查；
- checkpoint 扩展段：`capture_ext` 带 M09 段，`restore_ext` 恢复故障登记与事件计数。
"""

from __future__ import annotations

from safelib import UAV, Harness

from awr.contracts.reasons import Reason


def test_escalate_end_to_end() -> None:
    h = Harness(n=1)
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(10.0)
        r = h.cmd("escalate", {})
        assert r["code"] == int(Reason.CONFIRM_REQUIRED), r
        r = h.cmd("escalate", {"confirm_token": "t-1"}, cid="esc-1")
        assert r["status"] == "accepted", r
        h.advance(0.1)
        c = h.call("esc-1")
        assert c.final and c.status == "succeeded", (c.status, c.code)
        assert h.fs() == ("HOLD", "ESCALATE"), h.fs()
        r = h.cmd("escalate", {"confirm_token": "t-2"})
        assert r["code"] == int(Reason.STATE) and r["detail"]["why"] == "ESCALATE_INTERVAL", r
        h.W[0] += 2_100_000_000
        r = h.cmd("escalate", {"confirm_token": "t-3"}, cid="esc-3")
        assert r["status"] == "accepted", r
        h.advance(0.1)
        assert h.fs()[0] == "ELAND", h.fs()
        assert "SAF.OP.ESCALATE" in h.codes()
    finally:
        h.close()


def test_fault_inject_command_route() -> None:
    h = Harness(n=1)
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(10.0)
        r = h.cmd("fault/inject", {"kind": "thrust_loss", "params": {"frac": 0.2}, "duration_s": 2.0}, cid="f-1")
        assert r["status"] == "accepted", r
        fid = r["detail"]["result"]["fault_id"]
        assert fid and r["detail"]["result"]["apply_tick"] >= h.core.clock.tick
        h.advance(0.1)
        c = h.call("f-1")
        assert c.final and c.status == "succeeded" and c.effect["status"] == "OK"
        assert fid in h.rt.faults.faults
        # 非席位持有者 116；viewer 115；未知机体 107
        assert h.cmd("fault/inject", {"kind": "thrust_loss"}, pid="p-other")["code"] == int(Reason.SEAT_TAKEN)
        assert h.cmd("fault/inject", {"kind": "thrust_loss"}, pid="p-v", role="viewer", seat=False)["code"] == \
            int(Reason.ROLE_FORBIDDEN)
        assert h.cmd("fault/inject", {"kind": "thrust_loss"}, uav="nope")["code"] == int(Reason.NO_VEHICLE)
        # 内部 principal（剧本导演）：不要求席位
        r = h.core.engine.submit_internal({"cid": "scn-f", "op": "fault/inject", "uav": UAV,
                                           "args": {"kind": "gnss_denied", "duration_s": 1.0}},
                                          {"principal_id": "scenario", "role": "scenario", "entry": "scenario", "seat": False})
        assert r["status"] == "accepted", r
        r = h.cmd("fault/clear", {"fault_id": fid}, cid="f-clr")
        assert r["status"] == "accepted", r
        h.advance(0.1)
        assert h.call("f-clr").status == "succeeded"
    finally:
        h.close()


def test_checkpoint_extension_segments() -> None:
    from awr.sim.runtime.ckpt import capture, capture_ext, restore_ext

    h = Harness(n=1)
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(10.0)
        r = h.cmd("fault/inject", {"kind": "thrust_loss", "params": {"frac": 0.1}, "duration_s": 30.0})
        assert r["status"] == "accepted", r
        h.advance(0.2)
        ext = capture_ext(h.core)
        assert ext.get("safety"), sorted(ext)
        _arrays, meta = capture(h.core)
        assert "safety" in meta["ext"]
        faults0 = sorted(h.rt.faults.faults)
        h.rt.faults.faults.clear()
        h.rt.sink.counts.clear()
        done = restore_ext(h.core, ext)
        assert "safety" in done and sorted(h.rt.faults.faults) == faults0
    finally:
        h.close()
