"""D2 新命令与准入第 ⑥ 步（M08-AC-209；M08-FR-218、FR-219；17 §17.7.6）。

- 多旋翼 `loiter` 映射为 ORBIT（速度 `min(v_cruise, √(3R))`），入圈后 3 s 内 ‖r − R‖ < 1 m；`duration_s` 到期 succeeded；
  两者都缺省时直到取消；半径越出 [5, 500] m 以 110 拒绝；中心高度为 null 时取 `alt_m`（agl）或当前高度；
- 固定翼 `loiter` 交构型运动提供者；登记在第 ⑥ 步、按构型过滤的检查（M21：盘旋半径下限、空速下限、VTOL 悬停上限）以其
  原因码拒绝并带下限；提供者经 `provider_done` 报告终态；
- `fleet/update`：空中改挂载或限速档 105（NOT_LANDED_LOCKED）；地面改限速档生效、label 进入 roster；
- 机体范围的插件命令（`gimbal`、`zoom`、`sensor/mode`）先做准入 ④⑤⑦ 再交 M13 处理函数（msg 带 `_slot`）；机体不存在 107；
  非机体插件命令（`target/add`）照常路由。
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from simlib import CoreHarness

from awr.contracts.reasons import Reason
from awr.sim.core import admission as A
from awr.sim.core import command as CMD
from awr.sim.fleet import kernels_l1 as K
from awr.sim.fleet.stages import registry as R


def test_multirotor_loiter_maps_to_orbit() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg, fleet_cfg={"clock_profile": "sandbox100"}, limits="p600_std")
        try:
            h.takeoff(12.0)
            s = h.slot()
            bad = h.cmd("loiter", {"center": [10.0, 0.0, None], "radius_m": 3.0})
            assert bad["status"] == "rejected" and bad["code"] == int(Reason.PARAM_OUT_OF_RANGE)
            assert bad["detail"]["range"] == [5.0, 500.0]
            adm = h.cmd("loiter", {"center": [20.0, 0.0, None], "radius_m": 15.0, "dir": "ccw"}, cid="lo-1")
            assert adm["status"] == "accepted", adm
            c = h.call("lo-1")
            assert c.op == "loiter" and c.args["speed_mps"] == pytest.approx(min(8.0, math.sqrt(45.0)))
            assert c.args["center"][2] == pytest.approx(h.pos()[2], abs=0.2)  # null → 当前高度
            assert h.until(lambda: int(h.S.ctrl_mode[s]) == K.M_ORBIT, 2.0)
            h.advance(25.0)
            rr = []
            for _ in range(30):
                h.advance(0.1)
                p = h.pos()
                rr.append(math.hypot(p[0] - 20.0, p[1]))
            assert max(abs(r - 15.0) for r in rr) < 1.0, rr
            assert h.call("lo-1").status == "running"  # 直到取消
            adm = h.cmd("loiter", {"center": [20.0, 0.0, None], "radius_m": 15.0, "alt_m": 30.0, "duration_s": 6.0},
                        cid="lo-2")
            assert adm["status"] == "accepted" and h.call("lo-1").status == "canceled"  # 新调用取代旧调用
            assert h.call("lo-2").args["center"][2] == pytest.approx(30.0, abs=1e-6)  # agl，平地 DSM 为 0
            assert h.until(lambda: h.call("lo-2").final, 60.0)
            assert h.call("lo-2").status == "succeeded"
            assert h.cmd("loiter", {"center": [0, 0, 0], "radius_m": 10.0, "turns": 1, "duration_s": 5})["code"] == \
                int(Reason.PARAM_OUT_OF_RANGE)
        finally:
            h.close()


class FakeFW:
    name = "fake_fw"
    ops = ()
    frames = frozenset({1, 2})

    def __init__(self) -> None:
        self.started: list = []

    def start(self, call, slots, args, apply_tick):
        self.started.append((call.cid, call.op, [int(x) for x in slots], dict(args)))
        return "ok"

    def cancel(self, cid, slots):
        pass

    def safety_action(self, action, slots, t_s, item):
        pass


def _fw_check(req: A.AdmitReq, ctx: A.AdmitCtx):
    if req.op == "loiter" and float(req.args.get("radius_m", 0)) < 96.5:
        return A.AdmitResult(code=int(Reason.PARAM_OUT_OF_RANGE),
                             detail={"why": "LOITER_RADIUS_MIN", "min_m": 96.5, "field": "args.radius_m"})
    if req.op == "hover" and ctx.S.frame_kind[req.slot] == 2:
        return A.AdmitResult(code=int(Reason.VTOL_HOVER_LIMIT), detail={"hover_max_s": 180})
    return None


def test_fixed_wing_dispatch_and_step6() -> None:
    fw = FakeFW()
    CMD.register_motion_provider(fw)
    try:
        with R.isolated_registry() as reg:
            R.register_admission_check(6, "m21.fw", _fw_check, owner="M21", frames={1, 2})
            h = CoreHarness(n=2, reg=reg, spacing=20.0)
            try:
                for vid in h.ids():
                    h.takeoff(15.0, vid)
                a, b = h.ids()
                sb = h.slot(b)
                h.S.frame_kind[sb] = 1  # 第二架当作固定翼（构型运动提供者接管）
                adm = h.cmd("loiter", {"center": [0.0, 0.0, None], "radius_m": 50.0}, uav=b)
                assert adm["status"] == "rejected" and adm["code"] == int(Reason.PARAM_OUT_OF_RANGE)
                assert adm["detail"]["why"] == "LOITER_RADIUS_MIN" and adm["detail"]["min_m"] == 96.5
                ok = h.cmd("loiter", {"center": [0.0, 0.0, None], "radius_m": 120.0}, uav=b, cid="fw-lo")
                assert ok["status"] == "accepted", ok
                h.advance(0.1)
                assert fw.started and fw.started[-1][:3] == ("fw-lo", "loiter", [sb])
                assert int(h.S.ctrl_mode[sb]) == K.M_TRAJ
                assert h.call("fw-lo").provider == "fake_fw"
                # 多旋翼不受第 ⑥ 步固定翼检查影响
                assert h.cmd("loiter", {"center": [0.0, 0.0, None], "radius_m": 20.0}, uav=a)["status"] == "accepted"
                assert h.core.engine.provider_done("fw-lo", sb, "succeeded", metrics={"turns": 1.0})
                assert h.call("fw-lo").status == "succeeded" and h.call("fw-lo").metrics["turns"] == 1.0
                h.S.frame_kind[sb] = 2
                r = h.cmd("hover", {}, uav=b)
                assert r["status"] == "rejected" and r["code"] == int(Reason.VTOL_HOVER_LIMIT)
            finally:
                h.close()
    finally:
        CMD.reset_motion_providers()


def test_fw_motion_without_provider_rejected() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            h.takeoff(10.0)
            h.S.frame_kind[h.slot()] = 1
            r = h.cmd("goto", {"pos": [5.0, 0.0, 10.0]})
            assert r["status"] == "rejected" and r["code"] == int(Reason.BACKEND_UNSUPPORTED)
            assert r["detail"]["why"] == "NO_FW_PROVIDER"
        finally:
            h.close()


def test_fleet_update_rules() -> None:
    with R.isolated_registry() as reg:
        h = CoreHarness(n=1, reg=reg)
        try:
            vid = h.ids()[0]

            def upd(patch: dict, cid: str) -> dict:
                h.n += 1
                return h.core.engine.handle({"v": 1, "cid": cid, "op": "fleet/update", "uav": vid,
                                             "args": {"vehicle": vid, "patch": patch}, "principal": h.principal(cid)})

            r = upd({"limits_profile": "p600_std", "label": "A-01"}, "fu-1")
            assert r["status"] == "accepted", r
            s = h.slot()
            assert h.core.T.limit_ids[int(h.S.limits_id[s])] == "p600_std"
            ent = next(e for e in h.core.roster.snapshot()["entries"] if e["id"] == vid)
            assert ent["label"] == "A-01"
            assert h.core.instance_items(vid)[0]["label"] == "A-01"
            assert upd({"bogus": 1}, "fu-2")["code"] == int(Reason.PARAM_OUT_OF_RANGE)
            assert upd({"limits_profile": "nope"}, "fu-3")["code"] == int(Reason.PARAM_OUT_OF_RANGE)
            h.takeoff(10.0)
            r = upd({"sensors": [{"name": "mid360", "enabled": False}]}, "fu-4")
            assert r["status"] == "rejected" and r["code"] == int(Reason.STATE) and r["detail"]["why"] == "NOT_LANDED_LOCKED"
            assert upd({"label": "A-02", "lights": {"nav": True}}, "fu-5")["status"] == "accepted"  # 标签与灯光随时可改
            assert h.core.vehicle_cfg[vid]["lights"] == {"nav": True}
        finally:
            h.close()


def test_vehicle_plugin_commands_routed() -> None:
    got: list = []

    def handler(op: str):
        def fn(msg, apply_tick, ctx):
            got.append((op, msg.get("uav"), msg.get("_slot"), dict(msg.get("args") or {})))
            return {"status": "accepted", "result": {"op": op}}
        return fn

    with R.isolated_registry() as reg:
        R.register_command_handler("gimbal", handler("gimbal"), owner="M13", need_seat=False)  # 同 M13：不带 vehicle 实参
        for op in ("zoom", "sensor/mode"):
            R.register_command_handler(op, handler(op), owner="M13", vehicle=True)
        assert reg.commands["gimbal"].vehicle  # VEHICLE_PLUGIN_OPS 恒按机体命令路由
        R.register_command_handler("target/add", handler("target/add"), owner="M18")
        h = CoreHarness(n=1, reg=reg)
        try:
            vid = h.ids()[0]
            for op, args in (("gimbal", {"mode": "angles", "pitch_rad": -0.5}), ("zoom", {"sensor": "eo", "zoom_x": 4}),
                             ("sensor/mode", {"sensor": "eo", "mode": "nir"})):
                r = h.cmd(op, args, cid=f"pc-{op}")
                assert r["status"] == "accepted", r
                assert h.call(f"pc-{op}").uav == vid and h.call(f"pc-{op}").slot == h.slot()
            assert [g[0] for g in got] == ["gimbal", "zoom", "sensor/mode"]
            assert all(g[1] == vid and g[2] == h.slot() for g in got)
            assert h.cmd("zoom", {"sensor": "eo"}, uav="nope-01")["code"] == int(Reason.NO_VEHICLE)
            # 不持席位的 operator：租约第 ⑤ 步 116
            r = h.cmd("gimbal", {"mode": "stow"}, pid="p-other")
            assert r["status"] == "rejected" and r["code"] == int(Reason.SEAT_TAKEN)
            r = h.cmd("target/add", {"target": {"class": "person", "pos": [0, 0, 0]}}, uav=None)
            assert r["status"] == "accepted" and got[-1][0] == "target/add"
            assert h.core.lease.lease_json(h.slot())["owner"] == "OPERATOR"
        finally:
            h.close()


def test_loiter_in_admission_matrix() -> None:
    from awr.sim.core import state_model as SM

    for fs in SM.FS:
        assert SM.admit("loiter", fs, 1, 0x02 | 0x04) == SM.admit("orbit", fs, 1, 0x02 | 0x04)
    assert np.isinf(np.inf)
