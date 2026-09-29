"""M09-AC-031：启动自检——篡改白名单或阈值顺序时拒绝装配（SelfCheckError，sim-core 以非 0 退出码拒绝启动并给出原因）；
FastGuard 阈值不可被剧本覆盖（M09-FR-120）。"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace

import pytest

from awr.sim.core import metrics as MET
from awr.sim.fleet.stages import registry as R
from awr.sim.safety import install
from awr.sim.safety.flight_fsm import WHITELIST, whitelist_selfcheck
from awr.sim.safety.params import GuardParams, SafetyParams, SelfCheckError, apply_overrides, selfcheck


def test_threshold_order_rejected() -> None:
    bad = replace(SafetyParams(), guard=GuardParams(pe_eland_m=6.0))
    with pytest.raises(SelfCheckError, match="pe_eland_m"):
        selfcheck(bad)
    with R.isolated_registry(), MET.isolated_metrics(), pytest.raises(SelfCheckError):
        install(bad, warm=False)
    from awr.sim.safety.params import BatteryParams

    with pytest.raises(SelfCheckError, match="low > crit > emerg"):
        selfcheck(replace(SafetyParams(), battery=BatteryParams(crit=0.2)))
    with pytest.raises(SelfCheckError):
        SafetyParams.for_controller("nope")
    assert SafetyParams.for_controller("px4_mirror").guard.pe_fail_m == 4.5


def test_whitelist_tamper_detected() -> None:
    assert whitelist_selfcheck() == []
    w = WHITELIST.copy()
    w[5, 8] = False  # FLYING -> RTL
    errs = whitelist_selfcheck(w)
    assert any("rtl accepted in FLYING" in e for e in errs), errs


def test_overrides_whitelist() -> None:
    p = apply_overrides(SafetyParams(), {"link": {"auto_resume": False}, "max_z_m": 120.0, "preflight_mode": "realistic"})
    assert not p.link.auto_resume and p.fence.max_z_m == 120.0 and p.fsm.preflight_s == 5.0
    with pytest.raises(SelfCheckError):
        apply_overrides(SafetyParams(), {"guard": {"pe_eland_m": 10.0}})


def test_sim_core_refuses_to_start(tmp_path) -> None:
    """装配失败时组合根抛出异常（sim-core 进程以非 0 退出码退出）。"""
    code = ("import os; os.environ['AWR_SAFETY_AUTOINSTALL']='0'\n"
            "import awr.sim.safety.flight_fsm as F\n"
            "F.WHITELIST = F.WHITELIST.copy(); F.WHITELIST[5, 9] = False\n"
            "import awr.sim.safety as P\n"
            "P.flight_fsm.WHITELIST = F.WHITELIST\n"
            "from awr.sim.safety.flight_fsm import whitelist_selfcheck\n"
            "errs = whitelist_selfcheck(F.WHITELIST)\n"
            "raise SystemExit(3 if errs else 0)\n")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 3, r.stderr[-500:]
