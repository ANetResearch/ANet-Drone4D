"""M09-AC-015：电量模型——P600 悬停 soc 1 → 0 用时 1320 s ± 2%（VH-6，可用能量口径 188.7 Wh）；x500（battery = null）soc 恒为 1、
battery_pct = 255、不触发任何电量判据；LOW 只发 1 次；电压按 OCV 模型、只供显示。"""

from __future__ import annotations

import numpy as np
import pytest
from safelib import UnitRig

from awr.contracts.enums import FlightState
from awr.sim.safety.battery import ocv_v

FS = FlightState


def _hover(r: UnitRig, slot: int) -> None:
    r.set(slot, int(FS.FLYING), 0)
    r.S.thrust[slot] = r.T.hover[r.S.profile_id[slot]]


def test_p600_hover_endurance() -> None:
    r = UnitRig(n=1)
    _hover(r, 0)
    bb = r.bb
    assert bool(bb["has_bat"][0]) and bb["e_use_wh"][0] == pytest.approx(0.85 * 222.0)
    t = 0.0
    while bb["soc"][0] > 0.0 and t < 2000.0:
        r.ctx.tick += 25
        r.ctx.t_ns = r.ctx.tick * 4_000_000
        r.rt.begin(r.ctx)
        r.rt.bat.step(r.ctx)
        t += 0.1
    assert abs(t - 1320.0) <= 0.02 * 1320.0, t
    assert bb["battery_pct"][0] == 0
    assert bb["p_avg_w"][0] == pytest.approx(515.0, rel=1e-3)
    low = [e for e in r.rt.sink.pending if e.code == r.rt.code("SAF.BAT.LOW")]
    assert len(low) == 1  # 只在下降方向触发一次


def test_x500_no_battery() -> None:
    r = UnitRig(n=1, profile="x500")
    _hover(r, 0)
    for _ in range(100):
        r.ctx.tick += 25
        r.ctx.t_ns = r.ctx.tick * 4_000_000
        r.rt.begin(r.ctx)
        r.rt.bat.step(r.ctx)
    bb = r.bb
    assert not bb["has_bat"][0] and float(bb["soc"][0]) == 1.0 and int(bb["battery_pct"][0]) == 255
    assert r.rt.sink.pending == [] and not r.rt.fsm.C.any()


def test_ocv_monotonic() -> None:
    soc = np.linspace(0, 1, 11)
    v = ocv_v(soc, np.full(11, 6.0))
    assert np.all(np.diff(v) > 0) and 18.0 < v[-1] < 26.0
