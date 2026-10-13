"""按构型分派的功率模型（M08-AC-207；M08-FR-214、FR-215；ADR-052、ADR-096）。

- 多旋翼兜底模型（与 D1 M09 同一公式 `P = P_hover·(T/T_hover)^1.5`）：P600 profile 2.0.0 悬停功率 401 W，按可用能量
  0.85 × 228 Wh 积分的悬停续航 1740 s ± 1%（用户规格 29 min）；
- `power_by_frame` 按 `frame_kind` 分组调用登记的模型并保持 slot 次序；同一构型重复登记失败；`leg_power_w` 按机型构型分派
  （爬升项 m·g·v_z/η）；
- 固定翼与 VTOL 没有登记功率模型时拒绝 spawn（110，detail NO_POWER_MODEL），登记后可以加入。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from simlib import CoreHarness, Rig

from awr.contracts.reasons import Reason
from awr.sim.core import power as PW
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R

ROOT = Path(__file__).resolve().parents[2]
F1_FIXTURE = ROOT / "packages" / "contracts" / "fixtures" / "d2" / "vehicle" / "awr_f1.params.yaml"


class ConstFW:
    frame_kind = 1

    def __init__(self, p: float) -> None:
        self.p = p

    def power_w(self, state, slots, ctx):
        return np.full(len(slots), self.p)

    def leg_power_w(self, profile, v_air_mps, v_climb_mps, turn_load, rho_kgm3):
        return self.p * turn_load


def test_p600_v2_hover_endurance() -> None:
    rig = Rig("p600_mid360", 1)
    try:
        rig.air([0], 20.0, 5.0)
        acc = 0.0
        m = 0
        for _ in range(200):
            rig.step(5)
            acc += float(PW.power_by_frame(rig.S, np.array([0]), rig.ctx)[0])
            m += 1
        p = acc / m
        assert p == pytest.approx(401.0, rel=0.01), p
        e_use_wh = 0.85 * 228.0
        assert e_use_wh * 3600.0 / p == pytest.approx(1740.0, rel=0.01)
    finally:
        rig.close()


def test_dispatch_groups_and_registry() -> None:
    rig = Rig("p600_mid360", 3)  # Rig 在自己的隔离登记表内运行；模型登记在其中
    try:
        R.register_power_model(1, ConstFW(201.4))
        with pytest.raises(ValueError):
            R.register_power_model(1, ConstFW(1.0))
        with pytest.raises(ValueError):
            R.register_power_model(7, ConstFW(1.0))
        rig.air([0, 1, 2], 20.0, 2.0)
        rig.S.frame_kind[1] = 1  # 模拟一架固定翼（只验证分组与次序）
        p = PW.power_by_frame(rig.S, np.array([2, 1, 0]), rig.ctx)
        assert p[1] == 201.4 and p[0] == pytest.approx(p[2], rel=0.05) and 300 < p[0] < 500
        rig.S.frame_kind[1] = 2  # VTOL 未登记：NaN
        assert np.isnan(PW.power_by_frame(rig.S, np.array([1]), rig.ctx)[0])
    finally:
        rig.close()
    T = ProfileTable()
    pr = T.get("p600_mid360")
    assert PW.leg_power_w(pr, 8.0, 0.0) == pytest.approx(401.0)
    assert PW.leg_power_w(pr, 8.0, 2.0) == pytest.approx(401.0 + 3.825 * 9.80665 * 2.0 / 0.5)


def test_fixed_wing_spawn_needs_power_model(tmp_path: Path) -> None:
    shutil.copytree(ROOT / "vehicles" / "p600", tmp_path / "p600", ignore=shutil.ignore_patterns("*.glb"))
    (tmp_path / "awr_f1").mkdir()
    shutil.copyfile(F1_FIXTURE, tmp_path / "awr_f1" / "params.yaml")
    T = ProfileTable(vehicles=tmp_path)
    for registered in (False, True):
        with R.isolated_registry() as reg:
            if registered:
                R.register_power_model(1, ConstFW(201.4))
            h = CoreHarness(n=1, reg=reg, profiles=T)
            try:
                h.n += 1
                adm = h.core.engine.handle({"v": 1, "cid": f"add-fw-{registered}", "op": "fleet/add", "uav": None,
                                            "args": {"model_id": "awr_f1", "pos_enu_m": [60.0, 0.0, 0.0]},
                                            "principal": h.principal(f"add-fw-{registered}")})
                if not registered:
                    assert adm["status"] == "rejected" and adm["code"] == int(Reason.PARAM_OUT_OF_RANGE)
                    assert adm["detail"]["why"] == "NO_POWER_MODEL"
                else:
                    assert adm["status"] == "accepted", adm
                    vid = adm["detail"]["id"]
                    assert vid.startswith("awr-f1-") and adm["detail"]["limits_profile"] == "awr_f1_std"
                    s = h.core.roster.resolve(vid).slot
                    assert int(h.S.frame_kind[s]) == 1 and int(h.S.fidelity[s]) == (1 | 16)  # L1 | FW
                    assert PW.power_by_frame(h.S, np.array([s]), h.core.ctx)[0] == 201.4
            finally:
                h.close()
