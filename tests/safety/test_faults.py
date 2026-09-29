"""M09-AC-025、AC-027（D1-ext）：六类故障注入路径与时刻、非 Mock 后端 109、agent 调用 115、参数越界 110、清除与审计记录；
FCU 链路（link_drop）机上 3 s HOLD。"""

from __future__ import annotations

import numpy as np
import pytest
from safelib import Harness

from awr.contracts.reasons import Reason
from awr.sim.safety.faults import FaultError, validate

pytestmark = pytest.mark.ext


def _inject(h: Harness, uav: str, kind: str, params: dict | None = None, **kw) -> dict:
    return h.svc.fault_query({"op": "inject", "uav": uav, "kind": kind, "params": params or {}, **kw}, None)


def test_validation_and_access() -> None:
    assert validate("thrust_loss", {}, None, None)["frac"] == 0.45
    for kind, params, code in (("thrust_loss", {"frac": 0.0}, 110), ("motor_fail", {"motor": 4}, 110),
                               ("battery_drain", {"rate_pct_s": 1.0, "set_soc": 0.5}, 110), ("link_drop", {"side": "gcs"}, 110),
                               ("state_drop", {"x": 1}, 300), ("nope", {}, 110)):
        with pytest.raises(FaultError) as e:
            validate(kind, params, None, None)
        assert e.value.code == code, kind
    with pytest.raises(FaultError) as e:
        validate("gnss_denied", {}, -1.0, None)
    assert e.value.code == int(Reason.PARAM_OUT_OF_RANGE)


def test_x500_faults() -> None:
    h = Harness(n=4, profile="x500", spacing=25.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b, c, d = h.takeoff_all(25.0)
        fi = h.rt.faults
        with pytest.raises(FaultError) as e:
            fi.inject(h.slot(a), "thrust_loss", {}, backend="px4_sih")
        assert e.value.code == int(Reason.BACKEND_UNSUPPORTED)
        with pytest.raises(FaultError) as e:
            fi.inject(h.slot(a), "thrust_loss", {}, principal={"role": "agent"})
        assert e.value.code == int(Reason.ROLE_FORBIDDEN)
        r = _inject(h, a, "thrust_loss", {"frac": 0.45})
        assert r["code"] == 0 and r["fault_id"] and r["apply_tick"] == h.core.clock.tick + 1
        assert _inject(h, b, "motor_fail", {"motor": 0})["code"] == 0
        assert _inject(h, c, "state_drop")["code"] == 0
        assert _inject(h, d, "link_drop")["code"] == 0
        t0 = h.core.clock.t_ns
        assert h.until(lambda: h.fs(c)[0] == "FAILSAFE", 0.3, step_s=0.004)
        assert (h.core.clock.t_ns - t0) * 1e-9 <= 0.15 and "SAF.EST.TIMEOUT" in h.codes(c)
        assert h.until(lambda: h.fs(b)[0] in ("DISARMED", "CRASHED"), 1.5, step_s=0.004), h.state(b)
        # L1 近似（M08 融合核的不可控滚转）：倾角超限或倾角误差持续 0.5 s 均走 kill 链（g08；r24 原型 0.40/0.46 s）
        assert {"SAF.CTRL.TILT_ELAND", "SAF.CTRL.TILT_KILL", "SAF.CTRL.TILT_ERR_KILL"} & set(h.codes(b))
        assert h.until(lambda: h.fs(a)[0] == "ELAND", 3.0), h.state(a)
        assert "SAF.CTRL.THROTTLE_SAT" in h.codes(a)
        assert not bool(h.S.blocks["safety"]["flag_fcu"][h.slot(d)])
        assert h.until(lambda: h.fs(d) == ("HOLD", "LINK_LOSS"), 3.5), h.state(d)
        assert h.until(lambda: h.fs(b)[0] == "CRASHED", 15.0), h.state(b)
        assert h.fs(b)[1] == "IMPACT"
        # 注入与清除记录（审计与输入日志的本地副本）；机体坠毁后故障自动清除
        kinds = [x["kind"] for x in fi.log if x["op"] == "inject"]
        assert kinds == ["thrust_loss", "motor_fail", "state_drop", "link_drop"]
        assert "SAF.FAULT.INJECTED" in h.codes(a) and "SAF.FAULT.CLEARED" in h.codes(b)
        fid = [x["fault_id"] for x in fi.log if x["op"] == "inject"][3]
        assert h.svc.fault_query({"op": "clear", "fault_id": fid}, None)["code"] == 0
        h.advance(0.1)
        assert bool(h.S.blocks["safety"]["flag_fcu"][h.slot(d)])
        assert h.svc.fault_query({"op": "clear", "fault_id": fid}, None)["code"] == int(Reason.NOT_FOUND)
    finally:
        h.close()


def test_gnss_and_battery_drain() -> None:
    h = Harness(n=2, spacing=25.0)
    try:
        h.ready()
        h.gcs_age_ms = 0
        a, b = h.takeoff_all(15.0)
        assert _inject(h, a, "gnss_denied")["code"] == 0
        assert _inject(h, b, "battery_drain", {"set_soc": 0.06})["code"] == 0
        h.advance(0.3)
        assert h.fs(a) == ("HOLD", "LOC_LOST") and not bool(h.S.blocks["safety"]["flag_loc_ok"][h.slot(a)])
        assert h.until(lambda: h.fs(b)[0] == "RTL", 0.5) and "SAF.BAT.CRIT" in h.codes(b)
        rows = h.rt.faults.row_faults(h.slot(a))
        assert rows and rows[0]["kind"] == "gnss_denied"
        t0 = h.core.clock.t_ns
        assert h.until(lambda: h.fs(a)[0] == "LANDING", 31.0)
        assert abs((h.core.clock.t_ns - t0) * 1e-9 - 29.7) <= 0.5
        assert "SAF.EST.LOC_LOST" in h.codes(a)
        # 地面：定位不可用时 takeoff 返回 113（M08 第④步）
        h.rt.faults.inject(h.slot(b), "gnss_denied")
        h.advance(0.05)
        assert h.fs(b)[0] in ("RTL", "LANDING")
    finally:
        h.close()


def test_battery_drain_rate() -> None:
    h = Harness()
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(10.0)
        s = h.slot()
        h.rt.bat.set_soc(np.array([s]), 0.17)
        assert _inject(h, "p600-01", "battery_drain", {"rate_pct_s": 2.0})["code"] == 0
        assert h.until(lambda: h.fs()[0] == "LANDING", 10.0), h.state()
        codes = h.codes()
        assert codes.index("SAF.BAT.LOW") < codes.index("SAF.BAT.EMERG")
        assert "SAF.BAT.CRIT" in codes or "SAF.BAT.ENERGY_RTL" in codes
    finally:
        h.close()
