"""M09-AC-010：预检（arm 与带 auto_arm 的 takeoff）——soc 0.29、nofly 内、运动中、多项同时失败 → 103，detail 分别为
SOC_LOW、IN_NOFLY、NOT_STILL，多项时全部列出；预检窗口到期时重新检查。"""

from __future__ import annotations

import numpy as np
from safelib import Harness

from awr.contracts.reasons import Reason


def test_preflight_items(tiny_world) -> None:
    h = Harness(n=1, world=tiny_world, spawn=(70.0, -50.0))  # L 形禁飞区内出生（spawn_default 不做围栏检查）
    try:
        code, det = h.core.admit_add({"profile_id": "p600_mid360", "home_enu_m": [-20.0, -100.0, None],
                                      "vehicle_id": "p600-02"})
        assert code == 0, det
        code, det = h.core.admit_add({"profile_id": "p600_mid360", "home_enu_m": [-20.0, -110.0, None],
                                      "vehicle_id": "p600-03"})
        assert code == 0, det
        h.ready()
        # 1：nofly 内
        r = h.cmd("arm", {}, uav="p600-01")
        assert r["code"] == int(Reason.PREFLIGHT_FAILED) and r["detail"]["items"] == ["IN_NOFLY"], r
        # 2：soc 0.29
        s2 = h.slot("p600-02")
        h.rt.bat.set_soc(np.array([s2]), 0.29)
        r = h.cmd("takeoff", {"alt_m": 5}, uav="p600-02")
        assert r["code"] == int(Reason.PREFLIGHT_FAILED) and r["detail"]["items"] == ["SOC_LOW"], r
        assert "remedy" in r["detail"]
        # 3：运动中
        s3 = h.slot("p600-03")
        h.S.v[s3] = (0.5, 0.0, 0.0)
        h.S.touch()
        r = h.cmd("arm", {}, uav="p600-03")
        assert r["code"] == int(Reason.PREFLIGHT_FAILED) and r["detail"]["items"] == ["NOT_STILL"], r
        # 多项同时失败：全部列出
        s1 = h.slot("p600-01")
        h.rt.bat.set_soc(np.array([s1]), 0.2)
        r = h.cmd("arm", {}, uav="p600-01")
        assert set(r["detail"]["items"]) == {"IN_NOFLY", "SOC_LOW"}, r
        # 通过的机体：窗口到期时重新检查（期间电量跌破 0.30 → DISARMED/NOT_READY，调用 failed 103）
        h.S.v[s3] = 0.0
        h.S.touch()
        h.rt.bat.set_soc(np.array([s2]), 0.8)
        r = h.cmd("arm", {}, uav="p600-02", cid="arm2")
        assert r["status"] == "accepted", r
        h.advance(0.2)
        assert h.fs("p600-02")[0] == "PREFLIGHT"
        h.rt.bat.set_soc(np.array([s2]), 0.25)
        assert h.until(lambda: h.fs("p600-02")[0] == "DISARMED", 1.5)
        assert h.fs("p600-02") == ("DISARMED", "NOT_READY")
        h.advance(0.1)
        c = h.call("arm2")
        assert (c.status, c.code) == ("failed", int(Reason.PREFLIGHT_FAILED))
        assert "SAF.FSM.PREFLIGHT_FAILED" in h.codes("p600-02")
    finally:
        h.close()
