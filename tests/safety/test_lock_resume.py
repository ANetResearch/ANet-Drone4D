"""M09-AC-012：SafetyStop 加锁 → HOLD/SAFETY_STOP 且 locked；非安全命令 114；非席位 resume 116；agent 发 safety_stop 115；
电量 RTL 下 resume 101（detail BATTERY）；席位持有者 resume 后 FLYING/HOVER 并解锁。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason

AGENT = {"principal_id": "agent-1", "role": "agent", "entry": "scenario", "seat": False}


def test_lock_and_resume() -> None:
    h = Harness(n=2)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b = h.takeoff_all(10.0)
        s = h.slot(a)
        sb = h.S.blocks["safety"]
        assert h.cmd("safety_stop", {}, uav=a, cid="ss")["status"] == "accepted"
        h.advance(0.1)
        assert h.fs(a) == ("HOLD", "SAFETY_STOP") and bool(sb["locked"][s])
        assert h.core.lease.suspended(s)
        r = h.cmd("goto", {"pos": [0.0, 30.0, 10.0], "route": "direct"}, uav=a)
        assert r["code"] == int(Reason.LOCKED), r
        # 非席位持有者 resume：116（入口第⑤步）
        r = h.cmd("resume", {}, uav=a, pid="p-other")
        assert r["code"] == int(Reason.SEAT_TAKEN), r
        # agent 发 safety_stop：115（入口与第④步防御性检查）
        r = h.core.engine.submit_internal({"cid": "ag-ss", "op": "safety_stop", "uav": b, "args": {}}, AGENT)
        assert r["code"] == int(Reason.ROLE_FORBIDDEN), r
        # 席位持有者 resume：解锁并回到 FLYING/HOVER
        r = h.cmd("resume", {}, uav=a, cid="res")
        assert r["status"] == "accepted", r
        h.advance(0.1)
        assert h.fs(a) == ("FLYING", "HOVER") and not sb["locked"][s] and not h.core.lease.suspended(s)
        assert h.codes(a)[-2:] == ["SAF.OP.SAFETY_STOP", "SAF.OP.RESUME"] or "SAF.OP.RESUME" in h.codes(a)
        # 电量原因的自动 RTL 不可 resume：101 BATTERY
        h.rt.bat.set_soc(np.array([h.slot(b)]), 0.065)
        assert h.until(lambda: h.fs(b)[0] == "RTL", 1.0)
        assert "SAF.BAT.CRIT" in h.codes(b)
        r = h.cmd("resume", {}, uav=b)
        assert r["code"] == int(Reason.SAFETY_ACTIVE) and r["detail"]["why"] == "BATTERY", r
        # 自动 RTL 下的 hover（安全类）同样被拒：101（矩阵 RTL_auto 列）
        assert h.cmd("hover", {}, uav=b)["code"] == int(Reason.SAFETY_ACTIVE)
    finally:
        h.close()


def test_escalate_chain_unit() -> None:
    """M09-AC-024（ext，M09 侧）：FLYING → HOLD/ESCALATE → ELAND；间隔 < 2 s 拒绝；第 3 级默认关闭（105）。
    经 CommandEngine 的端到端路径（确认令牌、apply 时调用 apply_operator）见 test_ext_wiring.py::test_escalate_end_to_end。"""
    from safelib import UnitRig

    from awr.contracts.enums import FlightState as FS
    from awr.sim.core.admission import AdmitCtx, AdmitReq

    r = UnitRig(n=1)
    r.set(0, int(FS.FLYING), 1)
    adm = r.rt.admission

    def req() -> AdmitReq:
        return AdmitReq("c", "escalate", 0, "u00", {"confirm_token": "t"}, {"role": "operator"})

    actx = AdmitCtx(0, 0, r.S, None, r.T)

    assert adm.admit_state(req(), actx) is None
    r.svc.apply_operator(0, type("C", (), {"op": "escalate", "args": {}})(), r.ctx.t_ns + 4_000_000)
    r.tick(1)
    assert r.fs(0) == (int(FS.HOLD), 2) and [a for a, _ in r.rt.act_.pushed][-1] == "HOLD"
    assert adm.admit_state(req(), actx).detail["why"] == "ESCALATE_INTERVAL"
    r.ctx.clock.wall += 2_100_000_000
    assert adm.admit_state(req(), actx) is None
    r.svc.apply_operator(0, type("C", (), {"op": "escalate", "args": {}})(), r.ctx.t_ns + 4_000_000)
    r.tick(1)
    assert r.fs(0) == (int(FS.ELAND), 0) and [a for a, _ in r.rt.act_.pushed][-1] == "ELAND"
    r.ctx.clock.wall += 2_100_000_000
    res = adm.admit_state(req(), actx)
    assert res.code == 105 and res.detail["why"] == "ESCALATION_LEVEL"
