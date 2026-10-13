"""固定翼参数、派生值、检查与 fw_kin 运动学（CAT-AC-004；D2-AC-10；CAT-FR-005、FR-006；AWR-20 §5.1–§5.3；AWR-04 §5.3；ADR-096）。

解析部分：
- F1、V1 的 §5.2 全部派生值（AR、翼载、V_s、V_mp、V_md、(L/D)max、四个空速下的功率、巡航加最大爬升功率、续航、推力、R_min）≤ 1%；
- §5.3 盘旋指令空速与对地半径下限表（F1、V1，风 0–15 m/s）≤ 1%；盘旋倾斜修正（R 96.5 m、13.5 m/s：φ 10.9°、128.8 W、188.1 min）；
- 故意把 F1 的 `v_min` 改为 12.5 m/s 时 FW-2 失败，ProfileTable 以 353 拒绝；FW-3、FW-6、FW-7、FW-8 的负例；
运动学（sim-core 进程内，sandbox100，M21 插件 + 风场替身）：
- 弹射起飞（1 s 内加速至 V_min、4 m/s 爬升至 60 m AGL，TAKING_OFF → FLYING）；
- 无风盘旋半径与 `V²/(g·tan φ)` 误差 ≤ 2%；10 m/s 风下以 R_loiter,min 盘旋的对地航迹相对标称圆偏差 ≤ 10 m；小于下限的 loiter 110；
- `V < V_min` 的指令 110；`hover` 映射为盘旋（半径 max(R_loiter,min, r_loiter_default)）；10 m/s 侧风的风三角误差 ≤ 1%；
- 续航（功率模型在巡航与盘旋中的均值）与 §5.2 解析值误差 ≤ 5%；
- 风大于 v_cruise − 3 时发 `fw.wind_exceeds` 并改为逆风折线等待，风接近 v_max 时盘旋以 523 失败；
- 伞降回收（5 m/s 触地为正常触地、LANDED）、`rtl{land: false}` 在 home 盘旋、速度控制三通道与 `velocity_stop`、`fw/state` 查询。
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import yaml
from catlib import VEH, rel, vehicles_copy
from fwlib import FwRig, radius

from awr.contracts.enums import FlightState as FS
from awr.sim.catalog import checks as C
from awr.sim.catalog import derive as D
from awr.sim.catalog.loader import Catalog
from awr.sim.fleet.profiles import ProfileError, ProfileTable
from awr.sim.fleet.stages import registry as R

G = 9.80665


def _doc(pid: str) -> dict:
    return Catalog.load(VEH).docs[pid][0]


# ---------------------------------------------------------------- 解析（§5.2、§5.3）
@pytest.mark.parametrize(("pid", "exp"), [
    ("awr_f1", {"ar": 10.40, "wl": 90.5, "vs": 11.10, "vs_mtow": 11.99, "vmin_ratio": 1.126, "vs_bank": 12.26, "vmp": 9.45,
                "vmd": 12.43, "ld": 13.66, "p": (127.1, 127.1, 201.4, 597.8), "p_climb": 629, "end": (120.4, 190.6),
                "thrust": (5.54, 4.37), "rmin": 26.5, "rref": (15.0, 32.8)}),
    ("awr_v1", {"ar": 9.22, "wl": 109.6, "vs": 11.73, "vs_mtow": 12.62, "vmin_ratio": 1.109, "vs_bank": 12.61, "vmp": 10.06,
                "vmd": 13.24, "ld": 11.35, "p": (235.3, 255.9, 431.2, 1218.7), "p_climb": 939, "end": (112.4, 189.4),
                "thrust": (11.17, 8.47), "rmin": 39.7, "rref": (16.0, 45.2)}),
])
def test_fw_derived_values(pid: str, exp: dict) -> None:
    d = _doc(pid)
    fw = D.fw_aero(d)
    assert rel(fw.aspect_ratio, exp["ar"]) < 0.01 and rel(fw.wing_loading_n_m2, exp["wl"]) < 0.01
    assert rel(fw.v_stall(), exp["vs"]) < 0.01 and rel(fw.v_stall(fw.mtow_kg), exp["vs_mtow"]) < 0.01
    assert rel(fw.v_min_mps / fw.v_stall(fw.mtow_kg), exp["vmin_ratio"]) < 0.01
    assert rel(fw.v_stall() / math.sqrt(math.cos(math.radians(fw.phi_max_deg))), exp["vs_bank"]) < 0.01
    assert rel(fw.v_mp, exp["vmp"]) < 0.01 and rel(fw.v_md, exp["vmd"]) < 0.01 and rel(fw.ld_max, exp["ld"]) < 0.01
    for v, p in zip((fw.v_min_mps, fw.v_loiter_mps, fw.v_cruise_mps, fw.v_max_mps), exp["p"], strict=True):
        assert rel(fw.p_elec(v), p) < 0.01, (v, fw.p_elec(v), p)
    assert rel(fw.p_elec(fw.v_cruise_mps) + fw.mass_kg * G * fw.climb_max_mps / fw.eta_prop, exp["p_climb"]) < 0.01
    eu = D.e_use_wh(d)
    assert rel(eu * 60 / fw.p_elec(fw.v_cruise_mps), exp["end"][0]) < 0.01
    assert rel(eu * 60 / fw.p_elec(fw.v_loiter_mps), exp["end"][1]) < 0.01
    assert rel(fw.thrust_n(fw.v_cruise_mps), exp["thrust"][0]) < 0.01 and rel(fw.thrust_n(fw.v_loiter_mps), exp["thrust"][1]) < 0.01
    assert rel(fw.r_min(fw.v_loiter_mps), exp["rmin"]) < 0.01
    assert rel(exp["rref"][0] ** 2 / (G * math.tan(math.radians(fw.phi_max_deg))), exp["rref"][1]) < 0.01
    calc = D.fw_derived(d)
    for k, v in d["derived"].items():
        if k in calc:
            assert rel(calc[k], float(v)) < 0.01, (k, calc[k], v)


@pytest.mark.parametrize(("pid", "rows"), [
    ("awr_f1", [(0, 13.5, 31.8), (5, 13.5, 59.8), (10, 13.5, 96.5), (12, 15, 127.4), (14, 17, 167.9), (15, 18, 190.3)]),
    ("awr_v1", [(0, 15, 47.7), (5, 15, 84.8), (10, 15, 132.5), (12, 15, 154.5), (14, 17, 203.7)]),
])
def test_loiter_radius_table(pid: str, rows: list) -> None:
    fw = D.fw_aero(_doc(pid))
    for w, va, r in rows:
        assert fw.loiter_airspeed(w) == pytest.approx(va)
        assert rel(fw.r_loiter_min(w), r) < 0.01, (w, fw.r_loiter_min(w), r)
        assert fw.loiter_holdable(w)
    assert not fw.loiter_holdable(fw.v_cruise_mps - 2.0)
    assert fw.loiter_radius(10.0) == pytest.approx(fw.r_loiter_min(10.0)) and fw.loiter_radius(0.0) == fw.r_loiter_default_m


def test_loiter_bank_correction() -> None:
    fw = D.fw_aero(_doc("awr_f1"))
    phi = math.atan(13.5 ** 2 / (G * 96.5))
    assert math.degrees(phi) == pytest.approx(10.9, abs=0.05)
    p = fw.p_elec(13.5, n=1.0 / math.cos(phi))
    assert p == pytest.approx(128.8, abs=0.1)
    assert D.e_use_wh(_doc("awr_f1")) * 60 / p == pytest.approx(188.1, abs=0.1)


# ---------------------------------------------------------------- 检查（FW-1 至 FW-8）
@pytest.mark.parametrize(("path", "value", "rule"), [
    ("airframe.v_min_mps", 12.5, "FW-2"),
    ("airframe.v_loiter_mps", 19.0, "FW-3"),
    ("airframe.p_max_w", 500.0, "FW-6"),
    ("wind_rating_mps.value", 16.0, "FW-7"),
    ("airframe.r_loiter_default_m", 20.0, "FW-8"),
    ("airframe.phi_max_deg", 50.0, "FW-4"),
])
def test_fw_checks_negative(path: str, value: float, rule: str) -> None:
    d = yaml.safe_load(yaml.safe_dump(_doc("awr_f1")))
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur[k]
    cur[keys[-1]] = value
    bad = [r for r in C.fw_checks(d) if not r["pass"]]
    assert any(r["id"] == rule for r in bad), C.fw_checks(d)


def test_fw2_rejects_profile_load(tmp_path: Path) -> None:
    d = vehicles_copy(tmp_path)
    f = d / "awr_f1" / "params.yaml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    doc["airframe"]["v_min_mps"] = 12.5
    f.write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    with R.isolated_registry():
        C.register(R)
        with pytest.raises(ProfileError) as ei:
            ProfileTable(vehicles=d)
    assert ei.value.code == 353 and ei.value.profile_id == "awr_f1" and any("FW-2" in p for p in ei.value.problems)


# ---------------------------------------------------------------- 运动学
def test_catapult_takeoff_and_no_wind_loiter() -> None:
    with FwRig("awr_f1") as r:
        assert r.h.S.frame_kind[r.s] == 1 and int(r.h.S.fidelity[r.s]) & 16
        adm, c = r.cmd("takeoff", {}, cid="tk")
        assert adm["status"] == "accepted"
        r.h.advance(0.5)
        # D2-FX-R2：发射台上静止等 M09 预检（PREFLIGHT 1 s，静止判据）通过后才弹射（第 1 轮 D2-AC-16：受理即加速 → NOT_STILL）
        assert r.st.mode == "LAUNCH" and r.st.target.get("hold") and r.st.kin.va == 0.0
        assert r.fs() in (FS.PREFLIGHT, FS.READY, FS.TAKING_OFF)
        assert float(np.linalg.norm(r.h.S.enu.vel[r.s])) < 0.3
        assert r.run_until(lambda: not r.st.target.get("hold"), 3.0, dt=0.02)
        assert r.fs() == FS.TAKING_OFF
        t_go = r.h.t
        r.h.advance(1.1)
        assert r.st.kin.va == pytest.approx(13.5, abs=0.05)  # 1 s 内加速至 V_min
        assert r.run_until(lambda: c.final, 60.0)
        assert c.status == "succeeded" and r.pos()[2] == pytest.approx(60.0, abs=0.6)
        assert c.metrics["t_exec_s"] == pytest.approx(1.0 + 1.0 + 60.0 / 4.0, abs=1.0)
        assert t_go >= 1.0
        assert r.fs() == FS.FLYING
        adm, lo = r.cmd("loiter", {"center": [400.0, 0.0, None], "radius_m": 100.0, "turns": 2}, cid="lo")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: lo.final, 400.0)
        assert lo.status == "succeeded" and lo.metrics["turns"] == pytest.approx(2.0, abs=0.01)
        k = r.st.kin
        rr = radius(r.pos(), (400.0, 0.0))
        r_pred = k.va ** 2 / (G * math.tan(abs(k.phi)))
        assert rel(rr, r_pred) < 0.02 and rr == pytest.approx(100.0, abs=1.0)
        fw = r.h.core.vehicle_state(r.vid)["fw"]
        assert fw["mode"] == "FW" and fw["airspeed_mps"] == pytest.approx(13.5, abs=0.01) and abs(fw["bank_rad"]) > 0.1


def test_wind_loiter_tracking_and_radius_floor() -> None:
    with FwRig("awr_f1", wind_enu=(10.0, 0.0, 0.0)) as r:
        r.takeoff()
        adm, _ = r.cmd("loiter", {"center": [0.0, 300.0, None], "radius_m": 90.0}, cid="lo-bad")
        assert adm["status"] == "rejected" and adm["code"] == 110
        assert adm["detail"]["why"] == "LOITER_RADIUS_MIN" and adm["detail"]["min_m"] == pytest.approx(96.5, abs=0.1)
        adm, lo = r.cmd("loiter", {"center": [0.0, 300.0, None], "radius_m": 96.5}, cid="lo")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: r.st.loiter.get("est_t") is not None, 600.0)  # 入圈（逆风接近较慢）
        r.h.advance(30.0)
        dev = []
        for _ in range(300):  # 约 2.5 圈
            r.h.advance(0.5)
            dev.append(radius(r.pos(), (0.0, 300.0)) - r.st.loiter["r"])
        assert r.st.loiter["r"] == pytest.approx(96.5, abs=0.1) and r.st.loiter["va"] == pytest.approx(13.5)
        assert max(abs(x) for x in dev) <= 10.0, max(abs(x) for x in dev)
        assert not lo.final  # 无 turns、duration：直到取消


def test_airspeed_bounds_and_hover_maps_to_loiter() -> None:
    from awr.sim.fleet import kernels_l1 as K

    with FwRig("awr_f1") as r:
        r.takeoff()
        adm, _ = r.cmd("goto", {"pos": [500.0, 0.0, 60.0], "speed_mps": 10.0})
        assert adm["status"] == "rejected" and adm["code"] == 110 and adm["detail"]["why"] == "FW_AIRSPEED"
        assert adm["detail"]["min_mps"] == 13.5
        adm, h = r.cmd("hover", {}, cid="hv")
        assert adm["status"] == "accepted"
        r.h.advance(1.0)
        assert h.status == "succeeded"
        assert int(r.h.S.ctrl_mode[r.s]) == K.M_ORBIT and r.st.mode == "LOITER"
        assert r.st.loiter["r"] == pytest.approx(80.0)  # max(R_loiter,min(0) = 31.8, r_loiter_default 80)
        adm, _ = r.cmd("velocity", {"frame": "world"})
        assert adm["status"] == "rejected" and adm["detail"]["why"] == "FW_VELOCITY_FRAME"


def test_crosswind_wind_triangle() -> None:
    with FwRig("awr_f1", wind_enu=(10.0, 0.0, 0.0)) as r:
        r.takeoff()
        p0 = r.pos()
        adm, g = r.cmd("goto", {"pos": [float(p0[0]), float(p0[1]) + 5000.0, 60.0]}, cid="g")
        assert adm["status"] == "accepted"
        r.h.advance(60.0)
        vg = []
        xt = []
        for _ in range(40):
            r.h.advance(0.5)
            v = r.vel()
            vg.append(math.hypot(v[0], v[1]))
            xt.append(abs(r.pos()[0] - p0[0]))
        expect = math.sqrt(18.0 ** 2 - 10.0 ** 2)
        assert rel(float(np.mean(vg)), expect) < 0.01 and max(xt) < 5.0
        assert abs(math.degrees(r.st.kin.psi)) == pytest.approx(math.degrees(math.asin(10.0 / 18.0)), abs=0.5)  # 偏流角
        assert not g.final


def test_endurance_from_power_model() -> None:
    with FwRig("awr_f1") as r:
        r.takeoff()
        p0 = r.pos()
        r.cmd("goto", {"pos": [float(p0[0]) + 20000.0, float(p0[1]), 60.0]}, cid="g")
        r.h.advance(30.0)
        pw = []
        for _ in range(60):
            r.h.advance(0.5)
            pw.append(r.power_w())
        eu = D.e_use_wh(_doc("awr_f1"))
        assert rel(eu * 60 / float(np.mean(pw)), 120.4) < 0.05
        r.cmd("loiter", {"center": [float(r.pos()[0]), float(r.pos()[1]) + 100.0, None], "radius_m": 80.0}, cid="lo")
        r.h.advance(60.0)
        pw = []
        for _ in range(60):
            r.h.advance(0.5)
            pw.append(r.power_w())
        assert rel(eu * 60 / float(np.mean(pw)), 190.6) < 0.05


def test_wind_exceeds_zigzag_and_523() -> None:
    with FwRig("awr_f1") as r:
        r.takeoff()
        r.wind[:] = (16.0, 0.0, 0.0)  # 16 + 3 > v_cruise 18：不可保持盘旋
        adm, lo = r.cmd("loiter", {"center": [0.0, 0.0, None], "radius_m": 300.0}, cid="lo")
        assert adm["status"] == "accepted", adm
        r.h.advance(60.0)
        assert r.st.wind_exceeded
        assert any(e["kind"] == "fw.wind_exceeds" for e in r.h.core.events._ring)
        assert not lo.final and radius(r.pos(), (0.0, 0.0)) < 600.0  # 逆风折线等待：未被吹走
        r.wind[:] = (28.0, 0.0, 0.0)  # 接近 v_max：无可用逆风等待航线
        assert r.run_until(lambda: lo.final, 30.0)
        assert lo.status == "failed" and lo.code == 523


def test_parachute_landing_and_rtl_loiter() -> None:
    with FwRig("awr_f1", wind_enu=(3.0, 0.0, 0.0)) as r:
        r.takeoff()
        r.cmd("goto", {"pos": [600.0, 600.0, 60.0]}, cid="g")
        r.h.advance(60.0)
        adm, rt = r.cmd("rtl", {"land": False}, cid="rtl")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: rt.final, 200.0)
        assert rt.status == "succeeded" and radius(r.pos(), (0.0, 0.0)) < 120.0 and r.st.mode == "LOITER"
        adm, ld = r.cmd("land", {"at": "home"}, cid="ld")
        assert adm["status"] == "accepted"
        assert r.run_until(lambda: r.st.mode == "CHUTE", 120.0)
        assert r.vel()[2] == pytest.approx(-5.0, abs=0.01) and r.vel()[0] == pytest.approx(3.0, abs=0.01)  # 垂直 5 m/s、随风漂移
        assert r.fs() == FS.LANDING
        assert r.run_until(lambda: ld.final, 60.0)
        assert ld.status == "succeeded" and bool(r.h.S.landed[r.s])
        assert r.st.mode == "GROUND" and int(r.h.S.crash_sub[r.s]) == 0
        events = [e["kind"] for e in r.h.core.events._ring]
        assert "sim.contact.touchdown" in events and "sim.contact.collision" not in events
        # D2-FX-R2-tasking 第 2 次：开伞点取 home 上风的风漂移量处，落点在 home 附近（此前在 home 上方开伞，3 m/s 风漂移约 36 m，19 m/s 约 290 m）
        assert radius(r.pos(), (0.0, 0.0)) < 15.0, r.pos()


def test_fw_velocity_channels() -> None:
    with FwRig("awr_f1") as r:
        r.takeoff()
        adm, v = r.cmd("velocity", {"frame": "fw"}, cid="vel")
        assert adm["status"] == "accepted", adm
        mb = r.h.core.fleet.mailbox
        for seq in range(1, 101):
            mb.put(r.s, np.array([20.0, 0.0, 1.0]), -0.1, 2, seq, r.h.W[0], r.h.core.clock.paused_total_ns())
            r.h.advance(0.1)
        k = r.st.kin
        assert r.st.mode == "VEL" and k.va == pytest.approx(20.0, abs=0.3) and k.hdot == pytest.approx(1.0, abs=1e-6)
        assert k.psi_dot == pytest.approx(0.1, abs=0.01)  # ENU 偏航率 −0.1 = 自北顺时针 +0.1 rad/s
        adm, _ = r.cmd("velocity_stop", {}, cid="vs")
        assert adm["status"] == "accepted"
        r.h.advance(0.5)
        assert v.status == "succeeded" and r.st.mode == "LOITER"


def test_launch_heading_avoids_obstacles_and_wind_penetration() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：①弹射方向首选迎风，但弹射爬升轨迹（含该高度的风漂移）上有构筑物时改取偏离迎风最小的净空方向
    （第 1 版迎风弹射 1.7 s 撞上 20 m 外 9.8 m 的构筑物）；②强风（≥ v_cruise − 5）时航段空速抬高到风速 + 5 m/s，逆风仍可前进
    （此前 10 m/s 参考风在 150 m 高度约 19 m/s，按 18 m/s 飞航段 202 超时）。"""

    class _World:
        def __init__(self, boxes):
            self.boxes = boxes

        def height_dsm(self, xy):
            p = np.atleast_2d(np.asarray(xy, float))[:, :2]
            z = np.zeros(len(p))
            for x0, x1, y0, y1, h in self.boxes:
                m = (p[:, 0] >= x0) & (p[:, 0] <= x1) & (p[:, 1] >= y0) & (p[:, 1] <= y1)
                z[m] = np.maximum(z[m], h)
            return z

    with FwRig("awr_f1", wind_enu=(10.0, 0.0, 0.0)) as r:
        x0, y0 = (float(v) for v in r.pos()[:2])
        # 迎风（西）方向 20–40 m 处一排 15 m 的墙：迎风弹射爬不过
        r.h.core.ctx.world = _World([(x0 - 40.0, x0 - 20.0, y0 - 200.0, y0 + 200.0, 15.0)])
        adm, c = r.cmd("takeoff", {"alt_m": 30.0}, cid="tk")
        assert adm["status"] == "accepted"
        r.h.advance(0.5)
        psi = r.st.kin.psi
        up = math.atan2(-10.0, 0.0)
        dev = abs(math.degrees(math.atan2(math.sin(psi - up), math.cos(psi - up))))
        assert 10.0 <= dev <= 120.0, dev                                  # 不是正迎风，但仍偏向迎风一侧
        assert r.run_until(lambda: c.final, 120.0) and c.status == "succeeded", (c.status, c.code)
        assert int(r.h.S.crash_sub[r.s]) == 0 and r.pos()[2] > 25.0
        # 强风中航段：逆风 3 km 外的目标
        r.wind[:] = (19.0, 0.0, 0.0)
        p0 = r.pos()
        adm, _g = r.cmd("goto", {"pos": [float(p0[0]) - 600.0, float(p0[1]), 60.0]}, cid="g")
        assert adm["status"] == "accepted"
        r.h.advance(20.0)
        x1 = float(r.pos()[0])
        r.h.advance(20.0)
        assert float(r.pos()[0]) < x1 - 50.0                              # 逆风仍在前进（对地速度 ≥ 约 5 m/s）
        assert r.st.kin.va > 18.0


def test_zigzag_hold_stays_bounded_in_strong_wind() -> None:
    """D2-FX-R2-tasking 第 2 次（D2-AC-16）：19 m/s 风中（> v_cruise − 3）盘旋改为横风折线等待；chi_d 是对地航迹角，名义取横风、
    偏角按顺风向位置/速度闭环，并按横风对地速度提前折返。此前把名义"航向角" acos(w/v_max) 当作航迹角下发，
    稳态偏到上风 150 m、横风向外冲到 ±300 m。"""
    with FwRig("awr_f1") as r:
        r.takeoff()
        r.wind[:] = (19.0, 0.0, 0.0)
        c0 = [float(v) for v in r.pos()[:2]]
        adm, lo = r.cmd("loiter", {"center": [c0[0], c0[1], None], "radius_m": 300.0}, cid="lo")
        assert adm["status"] == "accepted", adm
        r.h.advance(90.0)
        assert r.st.wind_exceeded
        along, dist = [], []
        for _ in range(120):
            r.h.advance(5.0)
            p = r.pos()
            along.append(float(p[0]) - c0[0])          # 风向 +x：上风为 −x，along 取顺风向偏移
            dist.append(radius(p, c0))
        assert not lo.final
        assert max(dist) < 260.0, max(dist)
        assert abs(float(np.mean(along))) < 40.0, float(np.mean(along))
