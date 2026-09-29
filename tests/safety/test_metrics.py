"""M09-AC-036：剧本度量——FR-124 的 6 个度量在注册表中可读（名称与参数同 16 §12.3）；在合成场景中与手算值一致；重复登记失败。"""

from __future__ import annotations

import numpy as np
import pytest
from safelib import UnitRig

from awr.contracts.enums import FlightState
from awr.sim.core import metrics as MET
from awr.sim.fleet.stages import registry as R
from awr.sim.safety import METRICS, install

FS = FlightState


def test_registered_and_duplicate_fails() -> None:
    with R.isolated_registry(), MET.isolated_metrics():
        svc = install(warm=False)
        assert sorted(m.name for m in MET.list_metrics()) == sorted(METRICS)
        assert all(m.owner == "M09" for m in MET.list_metrics())
        assert MET.metric("guard_events") == 0.0 and MET.metric("min_separation_m") == np.inf
        with pytest.raises(ValueError):
            MET.register_metric("guard_events", svc.guard_events, owner="M09")
        with pytest.raises(ValueError):
            install(warm=False)  # 状态块、stage、钩子重复登记即失败


def test_values_on_synthetic_state() -> None:
    r = UnitRig(n=3)
    svc = r.svc
    sb, bb = r.sb, r.bb
    r.set(0, int(FS.FLYING), 1)
    r.set(1, int(FS.RTL), 0, auto=True)
    r.set(2, int(FS.HOLD), 3)
    sb["pe_max_m"][:3] = [0.4, 1.7, 0.2]
    sb["pe_gust_max_m"][:3] = [0.3, 0.9, 0.0]
    bb["soc_min"][:3] = [0.8, 0.31, 0.55]
    bb["energy_rtl_n"][:3] = [0, 1, 0]
    r.rt.fg.pair_min = {(0, 2): 12.5, (1, 2): 8.0}
    r.rt.fg.min_sep_seen = 8.0
    r.rt.sink.counts.update({"warn": 2, "action": 1, "critical": 1})
    assert svc.pos_err_max_m() == pytest.approx(1.7)
    assert svc.pos_err_max_m(window="gust") == pytest.approx(0.9)
    assert svc.pos_err_max_m(vehicle_ids=["u00", "u02"]) == pytest.approx(0.4)
    assert svc.battery_soc_min() == pytest.approx(0.31) and svc.battery_soc_min(["u02"]) == pytest.approx(0.55)
    assert svc.energy_rtl_count() == 1 and svc.energy_rtl_count(["u00"]) == 0
    assert svc.min_separation_m() == 8.0 and svc.min_separation_m(["u00"]) == 12.5
    assert svc.guard_events() == 2 and svc.guard_events("warn") == 4 and svc.guard_events("critical") == 1
    assert svc.flight_state("u01") == int(FS.RTL) and svc.flight_state_name("u02") == "HOLD"
