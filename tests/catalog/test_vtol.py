"""VTOL（AWR-V1）：任务剖面、模式切换与悬停上限（CAT-AC-005；D2-AC-10 的 VTOL 部分；AWR-20 §5.4；AWR-04 §5.3）。

- 标准任务剖面：垂直段能量 74.0 Wh、巡航段 102.1 min、剖面总时长 105.5 min（≤ 1%）；纯巡航 112.4 min、盘旋 189.4 min；
- MC 模式垂直起飞（L1 多旋翼），长航段 goto 经 TRANSITION_FW（15 s，空速线性升至 v_transition）切到 FW 并到点盘旋；
  FW 模式的悬停类指令经 TRANSITION_MC（10 s）回到 MC 执行；`fw/state` 的 mode 依次为 MC、TRANSITION_FW、FW、TRANSITION_MC；
- MC 空中悬停累计达 180 s：发 `vtol.hover_limit` 并降落；之后的悬停类指令（hover）以 524 VTOL_HOVER_LIMIT 拒绝并建议 loiter；
- VT-1 至 VT-4 的负例（MTOW 推重比、hover_max_s 越界、J_zz 不最大、任务剖面派生值不符）。
"""

from __future__ import annotations

import pytest
import yaml
from catlib import VEH, rel
from fwlib import FwRig, radius

from awr.sim.catalog import checks as C
from awr.sim.catalog import derive as D
from awr.sim.catalog.loader import Catalog
from awr.sim.fixedwing.energy import endurance_s
from awr.sim.fixedwing.params import params_from_doc
from awr.sim.fixedwing.vtol import hover_class, mission_profile


def _doc() -> dict:
    return Catalog.load(VEH).docs["awr_v1"][0]


def test_mission_profile() -> None:
    d = _doc()
    vp = D.vtol_profile(d)
    assert vp["e_vert_wh"] == pytest.approx(74.0, abs=0.05)
    assert rel(vp["cruise_s"] / 60, 102.1) < 0.01 and rel(vp["total_s"] / 60, 105.5) < 0.01
    p = params_from_doc(d, "awr_v1", 2)
    mp = mission_profile(p)
    assert mp["cruise_s"] == pytest.approx(vp["cruise_s"], rel=1e-9)
    assert rel(endurance_s(p, "cruise") / 60, 112.4) < 0.01 and rel(endurance_s(p, "loiter") / 60, 189.4) < 0.01
    assert rel(endurance_s(p, "mission") / 60, 102.1) < 0.01
    assert hover_class("goto", {"pos": [200.0, 0.0, 40.0]}, (0.0, 0.0), p) and not hover_class("goto", {"pos": [400.0, 0.0, 40.0]},
                                                                                              (0.0, 0.0), p)
    assert hover_class("land", {}, (0.0, 0.0), p) and not hover_class("loiter", {}, (0.0, 0.0), p)


@pytest.mark.parametrize(("path", "value", "rule"), [
    ("mtow_kg.value", 12.5, "VT-1"),
    ("vtol.hover_max_s", 30.0, "VT-2"),
    ("vtol.v_transition_mps", 15.0, "VT-2"),
    ("inertia_kgm2.value.zz", 1.5, "VT-3"),
    ("derived.endurance_mission_cruise_s", 6000.0, "VT-4"),
])
def test_vt_checks_negative(path: str, value: float, rule: str) -> None:
    d = yaml.safe_load(yaml.safe_dump(_doc()))
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value
    assert any(r["id"] == rule and not r["pass"] for r in C.vt_checks(d)), C.vt_checks(d)
    assert all(r["pass"] for r in C.vt_checks(_doc()))


def test_transitions_and_modes() -> None:
    with FwRig("awr_v1") as r:
        assert r.h.S.frame_kind[r.s] == 2 and not int(r.h.S.fidelity[r.s]) & 16
        r.takeoff(40.0)
        assert r.h.core.vehicle_state(r.vid)["fw"]["mode"] == "MC"
        adm, g = r.cmd("goto", {"pos": [1500.0, 0.0, 40.0]}, cid="g")
        assert adm["status"] == "accepted", adm
        assert r.run_until(lambda: r.st.mode == "TR_FW", 2.0, dt=0.02)
        t0 = r.h.t
        assert r.h.core.vehicle_state(r.vid)["fw"]["mode"] == "TRANSITION_FW"
        assert r.run_until(lambda: r.st.fw_mode == "FW", 30.0, dt=0.02)
        assert r.h.t - t0 == pytest.approx(15.0, abs=0.1)
        assert int(r.h.S.fidelity[r.s]) & 16 and r.st.kin.va >= 14.0 - 1e-6
        assert r.run_until(lambda: g.final, 200.0)
        assert g.status == "succeeded" and radius(r.pos(), (1500.0, 0.0)) < 150.0 and r.st.mode == "LOITER"
        assert r.h.core.vehicle_state(r.vid)["fw"]["mode"] == "FW"
        adm, h = r.cmd("hover", {}, cid="hv")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: r.st.mode == "TR_MC", 2.0, dt=0.02)
        t1 = r.h.t
        assert r.run_until(lambda: not int(r.h.S.fidelity[r.s]) & 16, 20.0, dt=0.02)
        assert r.h.t - t1 == pytest.approx(10.0, abs=0.1)
        assert r.run_until(lambda: h.final, 30.0)
        assert h.status == "succeeded" and r.h.core.vehicle_state(r.vid)["fw"]["mode"] == "MC"


def test_hover_limit_and_524() -> None:
    with FwRig("awr_v1") as r:
        r.takeoff(20.0)
        used = r.st.hover_used_s
        assert used > 0.0  # 垂直起飞计入悬停
        adm, _ = r.cmd("hover", {}, cid="hv")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: r.st.hover_limit_sent, 200.0)
        ev = [e for e in r.h.core.events._ring if e["kind"] == "vtol.hover_limit"]
        assert len(ev) == 1 and ev[0]["data"]["hover_used_s"] == pytest.approx(180.0, abs=0.05)  # 一个 stage 周期内
        r.h.advance(1.0)
        assert r.fs() in (9, 12, 1)  # LANDING（无固定翼航段：降落）
        adm, _ = r.cmd("hover", {}, cid="hv2")
        assert adm["status"] == "rejected" and adm["code"] == 524
        assert adm["detail"]["suggest"] == "loiter" and adm["detail"]["hover_max_s"] == 180.0
        assert r.run_until(lambda: bool(r.h.S.landed[r.s]) and r.st.hover_used_s == 0.0, 120.0)  # 落地上锁后清零
