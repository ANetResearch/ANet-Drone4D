"""M09-AC-016：能量感知 RTL——低电量外飞：触发 ENERGY_RTL，在 home 2 m 内触地，最低 soc ≥ 0.05，无 EMERG；初始 soc 0.06 在
空中：CRIT 后 EMERG 就地 LANDING。起飞预检要求 soc ≥ 0.30，故以满电起飞后在空中直接置 soc（测试夹具，AC 的"初始 soc"）。"""

from __future__ import annotations

import numpy as np
from safelib import Harness


def test_energy_rtl_lands_home() -> None:
    h = Harness(limits="px4_default")
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(10.0)
        s = h.slot()
        home = h.S.enu.home[s].copy()
        _rep, cid = h.goto([home[0] + 200.0, home[1], 10.0], wait=True, timeout_s=90.0)
        assert h.call(cid).status == "succeeded"
        bb = h.S.blocks["battery"]
        h.rt.bat.set_soc(np.array([s]), 0.125)
        bb["soc_min"][s] = 0.125
        assert h.until(lambda: h.fs()[0] == "RTL", 3.0)
        assert "SAF.BAT.ENERGY_RTL" in h.codes()
        ev = next(e for e in h.events if e.get("code") == "SAF.BAT.ENERGY_RTL")
        assert ev["value"] < ev["threshold"]  # t_rem < 1.3·t_rtl
        assert h.until(lambda: h.fs()[0] == "DISARMED", 150.0), h.state()
        p = h.pos()
        assert np.hypot(p[0] - home[0], p[1] - home[1]) <= 2.0
        assert float(bb["soc_min"][s]) >= 0.05
        assert "SAF.BAT.EMERG" not in h.codes()
        assert h.svc.energy_rtl_count() == 1
        assert "RTL/CLIMB" in h.states() and "RTL/CRUISE" in h.states() and "RTL/FINAL" in h.states()
    finally:
        h.close()


def test_crit_then_emerg_landing() -> None:
    h = Harness()
    try:
        h.ready()
        h.gcs_age_ms = 0
        h.takeoff(15.0)
        s = h.slot()
        h.rt.bat.set_soc(np.array([s]), 0.06)
        assert h.until(lambda: h.fs()[0] == "RTL", 1.0)
        assert "SAF.BAT.CRIT" in h.codes()
        assert h.until(lambda: h.fs()[0] == "LANDING", 40.0), h.state()
        assert "SAF.BAT.EMERG" in h.codes()
        assert bool(h.S.blocks["safety"]["fs_auto"][s]) and bool(h.S.blocks["safety"]["flag_failsafe"][s])
    finally:
        h.close()
