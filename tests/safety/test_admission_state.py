"""M09-AC-009：准入第④步——与 12 §5.2 准入矩阵一致（M08 查表 + M09 登记检查）；原因码优先级；批量命令逐机结论；apply 时复核
（`matrix_verdict`）。1000 架批量耗时（p99 ≤ 0.1 ms）为 perf，不在此运行。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason
from awr.sim.safety.flight_fsm import Origin

AGENT = {"principal_id": "agent-1", "role": "agent", "entry": "scenario", "seat": False}


def test_state_admission_codes() -> None:
    h = Harness(n=3, spacing=15.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b, c = h.takeoff_all(8.0)
        assert h.cmd("takeoff", {"alt_m": 5}, uav=a)["code"] == int(Reason.STATE)          # FLYING：takeoff '-'
        assert h.cmd("arm", {}, uav=a)["code"] == int(Reason.STATE)
        assert h.cmd("rtl", {}, uav=b, cid="rtl-b")["status"] == "accepted"
        h.advance(0.1)
        assert h.cmd("rtl", {}, uav=b)["code"] == int(Reason.DUPLICATE)                    # RTL：rtl '='
        # ELAND（锁存）：导航与 hover 均 101
        h.rt.fsm.propose(np.array([h.slot(c)]), 10, 0, Origin.AUTO, "SAF.CTRL.TILT_ELAND")
        h.advance(0.05)
        assert h.fs(c)[0] == "ELAND"
        for op, args in (("goto", {"pos": [0.0, 0.0, 10.0], "route": "direct"}), ("hover", {}), ("land", {}), ("rtl", {})):
            assert h.cmd(op, args, uav=c)["code"] == int(Reason.SAFETY_ACTIVE), op
        # agent 的 kill：第④步防御性拒绝 115（入口第①步本应已拒）
        r = h.core.engine.submit_internal({"cid": "ag-k", "op": "kill", "uav": a, "args": {"confirm_token": "x"}}, AGENT)
        assert r["code"] == int(Reason.ROLE_FORBIDDEN)
        # HOLD/LINK_LOSS 下 resume：链路未恢复 → 101 LINK_NOT_RESTORED
        h.gcs_age_ms = None
        assert h.until(lambda: h.fs(a) == ("HOLD", "LINK_LOSS"), 4.0)
        r = h.cmd("resume", {}, uav=a)
        assert r["code"] == int(Reason.SAFETY_ACTIVE) and r["detail"]["why"] == "LINK_NOT_RESTORED", r
        # apply 时复核：加锁机体的非安全命令、原因未解除的 resume
        sl = np.array([h.slot(a), h.slot(b)], np.int32)
        v = h.svc.matrix_verdict(sl, "resume")
        assert int(v[0]) == int(Reason.SAFETY_ACTIVE)
        h.S.blocks["safety"]["locked"][h.slot(b)] = True
        assert list(h.svc.matrix_verdict(sl, "goto")) == [0, int(Reason.LOCKED)]
        assert list(h.svc.matrix_verdict(sl, "land")) == [0, 0]
        h.S.blocks["safety"]["locked"][h.slot(b)] = False
    finally:
        h.close()


def test_batch_rtl_per_uav() -> None:
    h = Harness(n=6, spacing=15.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        ids = h.takeoff_all(6.0)
        h.rt.fsm.propose(np.array([h.slot(ids[0])]), 10, 0, Origin.AUTO, "SAF.CTRL.TILT_ELAND")
        h.advance(0.05)
        rep = h.cmd("rtl", {}, uav="*", cid="b-rtl")
        assert rep["status"] == "accepted"
        assert sorted(rep["per_uav"]["accepted"]) == sorted(ids[1:])
        assert rep["per_uav"]["rejected"] == [[ids[0], int(Reason.SAFETY_ACTIVE)]]
        h.advance(0.1)
        assert all(h.fs(u)[0] == "RTL" for u in ids[1:])
        # 批量 RTL 使用缓存的 z_rtl：同一 tick 不做逐机 H_top 查询
        assert all(bool(h.S.blocks["battery"]["rtl_valid"][h.slot(u)]) for u in ids[1:])
    finally:
        h.close()
