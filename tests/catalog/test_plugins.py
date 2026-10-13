"""M21 sim-core 插件的登记与运行时接口（CAT-FR-005、FR-014、FR-017；M08-FR-213 至 FR-215、FR-219 的宿主接口；AWR-04 §5.3）。

- `install()` 对当前登记表幂等：机型检查、`catalog/*` 命令与查询、`fleet/update` 钩子、`fw_kin` stage（order 086，按档 50 Hz）、
  构型运动提供者（frames {1, 2}，重复装配替换上一次的 M21 提供者、保留其他提供者）、功率模型 1 与 2、准入第 ⑥ 步、`fw/state`；
- 查询 `catalog/nest_compat`、`catalog/capabilities`、`catalog/model`（预置与克隆）在 sim-core 内可用；
- 安全动作：固定翼 HOLD 为盘旋、ELAND 立即开伞（ELAND 态）、KILL 后失控坠落（硬着陆）、RTL 返航伞降；
- 功率模型：地面 0，固定翼巡航按 P_elec，`leg_power_w` 与解析值一致，VTOL 在 `v_air < v_min` 时按悬停功率。
"""

from __future__ import annotations

import numpy as np
import pytest
from catlib import install
from fwlib import FwRig

from awr.contracts.enums import FlightState as FS
from awr.sim.core import command as CMD
from awr.sim.core import power as PW
from awr.sim.fleet.profiles import ProfileTable
from awr.sim.fleet.stages import registry as R


def test_install_registrations_idempotent() -> None:
    with R.isolated_registry() as reg:
        a = install()
        b = install()
        assert a[0] is b[0] and a[1] is b[1]
        assert {c.check_id for c in reg.profile_checks} == {"M21.FW", "M21.VT", "M21.SG", "M21.MT"}
        assert {"catalog/clone", "catalog/clone_update", "catalog/clone_delete"} <= set(reg.commands)
        assert {"catalog/models", "catalog/model", "catalog/nest_compat", "catalog/capabilities", "fw/state"} <= set(reg.queries)
        st = next(s for s in reg.stages if s.name == "fw_kin")
        assert (st.order, st.owner, st.hz) == (86, "M21", 50.0)
        assert set(reg.power) == {1, 2} and any(c.name == "m21.fw" and c.frames == frozenset({1, 2}) for c in reg.admission)
        assert CMD.fw_provider(1) is a[1] and CMD.fw_provider(2) is a[1]
    with R.isolated_registry():
        c = install()
        assert c[1] is not a[1] and CMD.fw_provider(1) is c[1]  # 新登记表：替换进程级提供者


def test_power_models() -> None:
    with R.isolated_registry() as reg:
        install()
        T = ProfileTable()
        f1, v1 = T.get("awr_f1"), T.get("awr_v1")
        assert PW.leg_power_w(f1, 18.0) == pytest.approx(201.4, rel=0.01)
        assert PW.leg_power_w(f1, 13.5) == pytest.approx(127.1, rel=0.01)
        assert PW.leg_power_w(v1, 20.0) == pytest.approx(431.2, rel=0.01)
        assert PW.leg_power_w(v1, 0.0) == pytest.approx(1300.0)
        assert PW.leg_power_w(f1, 18.0, 4.0) == pytest.approx(629.3, rel=0.01)
        m = reg.power[1]
        assert m.hover_or_loiter_power_w(f1, 10.0) == pytest.approx(127.1, rel=0.01)  # 10 m/s 风：盘旋空速仍为 13.5
        assert m.hover_or_loiter_power_w(f1, 12.0) == pytest.approx(PW.leg_power_w(f1, 15.0), rel=1e-9)


def test_catalog_queries_in_core() -> None:
    with FwRig("awr_f1") as r:
        ctx = r.h.core.ctx
        q = r.h.core.reg.queries
        rep = q["catalog/nest_compat"].fn({"args": {"model_id": "awr_f1", "nest": {"kind": "box", "chargers": []}}}, ctx)
        assert rep["code"] == 0 and rep["item"]["why"] == "kind"
        rep = q["catalog/capabilities"].fn({"args": {"model_id": "awr_f1"}}, ctx)
        assert "flight.loiter" in rep["items"] and "thermal.imaging" in rep["items"]
        rep = q["catalog/model"].fn({"args": {"model_id": "awr_v1"}}, ctx)
        assert rep["code"] == 0 and rep["item"]["fw"]["mission"]["cruise_s"] == pytest.approx(6127, rel=0.01)
        assert q["catalog/model"].fn({"args": {"model_id": "x500"}}, ctx)["code"] == 520
        assert q["catalog/models"].fn({"args": {}}, ctx)["items"] == []
        assert r.power_w() == 0.0  # 地面上锁


def test_safety_actions_fixed_wing() -> None:
    from awr.sim.core.supervisor_queue import SupervisorQueue

    with FwRig("awr_f1") as r:
        r.takeoff()
        S = r.h.S
        q = SupervisorQueue()
        q.hold(np.array([r.s], np.int32))
        r.h.core.supq.items.extend(q.take())
        r.h.advance(0.5)
        assert r.st.mode == "LOITER" and r.st.loiter["r"] == pytest.approx(80.0)
        assert r.power_w() == pytest.approx(127.1, rel=0.05)
        q.eland(np.array([r.s], np.int32))
        r.h.core.supq.items.extend(q.take())
        r.h.advance(0.5)
        assert r.st.mode == "CHUTE" and r.fs() == FS.ELAND
        assert r.power_w() == pytest.approx(20.0)  # 伞降：航电
        assert r.run_until(lambda: bool(S.landed[r.s]), 30.0)
        assert int(S.crash_sub[r.s]) == 0


def test_kill_falls_and_crashes() -> None:
    from awr.sim.core.supervisor_queue import SupervisorQueue

    with FwRig("awr_f1") as r:
        r.takeoff()
        q = SupervisorQueue()
        q.kill(np.array([r.s], np.int32))
        r.h.core.supq.items.extend(q.take())
        r.h.advance(0.5)
        assert r.st.mode == "FALL"
        assert r.run_until(lambda: bool(r.h.S.landed[r.s]), 30.0, dt=0.1)
        assert int(r.h.S.crash_sub[r.s]) != 0 and r.fs() == FS.CRASHED
        assert "sim.contact.collision" in [e["kind"] for e in r.h.core.events._ring]


def test_safety_rtl_lands_by_parachute() -> None:
    from awr.sim.core.supervisor_queue import SupervisorQueue

    with FwRig("awr_f1") as r:
        r.takeoff()
        r.cmd("goto", {"pos": [800.0, 0.0, 60.0]}, cid="g")
        r.h.advance(40.0)
        q = SupervisorQueue()
        q.rtl(np.array([r.s], np.int32))
        r.h.core.supq.items.extend(q.take())
        r.h.advance(1.0)
        assert r.fs() == FS.RTL
        assert r.run_until(lambda: bool(r.h.S.landed[r.s]), 200.0)
        p = r.pos()
        assert float(np.hypot(p[0], p[1])) < 10.0  # 无风：在 home 正上方开伞


def test_state_block_restore_after_restart() -> None:
    """sim-core 重启接续：`fw` 状态块（参与 checkpoint）恢复空速、姿态与盘旋参数；运行时对象重建后继续同一盘旋。"""
    with FwRig("awr_f1") as r:
        r.takeoff()
        adm, _ = r.cmd("loiter", {"center": [300.0, 0.0, None], "radius_m": 120.0}, cid="lo")
        assert adm["status"] == "accepted"
        r.h.advance(60.0)
        lo = dict(r.st.loiter)
        va, phi = r.st.kin.va, r.st.kin.phi
        b = r.h.S.blocks["fw"]
        assert int(b["fw_valid"][r.s]) == 1 and b["fw_loi"][r.s][3] == pytest.approx(lo["r"])
        r.rt.slots.clear()  # 新进程的运行时对象（状态只剩 FleetState 与 fw 块）
        r.h.advance(0.1)
        st = r.st
        assert st.mode == "LOITER" and st.loiter["r"] == pytest.approx(lo["r"]) and st.loiter["cx"] == pytest.approx(300.0)
        assert st.kin.va == pytest.approx(va, abs=0.05) and st.kin.phi == pytest.approx(phi, abs=0.05)
        r.h.advance(30.0)
        assert abs(float(np.hypot(r.pos()[0] - 300.0, r.pos()[1])) - lo["r"]) < 5.0
