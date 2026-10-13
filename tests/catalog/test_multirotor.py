"""多旋翼机型：P600 2.0.0 与参考四旋翼（CAT-AC-002、CAT-AC-003；AWR-20 §3.2、§3.3、§4.2、§4.3、§5.5）。

- P600 v2 与 §3.2、§3.3 逐项一致（派生值相对误差 ≤ 1%）：E_use 193.8 Wh、悬停 401.0 W、单桨推力 9.378 N、VH-1 0.488、TWR 2.05 与
  MTOW 时 1.91、8 m/s 风悬停倾角 7.43°、CdA/m 0.00915、续航 1740 s、挂载余量 0.275 kg；`p600_mid360@1.0.0` 可加载且等于 v1；
- AWR-Q1、AWR-Q9 的 L1 字段与 §4.2 一致，VH-1 至 VH-7 与 §4.2 表一致（≤ 1%）；悬停功率 112.2 W、835.1 W；
- 挂载与质量变化的能量修正（§5.5）：P600 + nir_ill_l 4.005 kg / 432.6 W / 26.9 min；Q1 + LWIR 13 mm 1.04 kg / 130.0 W / 30.2 min；
  Q9 + nir_ill_l 9.18 kg / 863.3 W / 31.0 min；
- 目录摘要（SB-21）的速度、续航、链路、机巢类型与 §4.3 一致。
"""

from __future__ import annotations

import pytest
from catlib import VEH, rel

from awr.sim.catalog import derive as D
from awr.sim.catalog.loader import Catalog
from awr.sim.catalog.view import model_summary
from awr.sim.fleet import profiles as PF

G = 9.80665


def _doc(pid: str) -> dict:
    return Catalog.load(VEH).docs[pid][0]


def test_p600_v2_against_awr20() -> None:
    d = _doc("p600_mid360")
    assert d["profile_version"] == "2.0.0"
    assert D.e_use_wh(d) == pytest.approx(193.8, rel=1e-3)
    assert D.multirotor_hover_power_w(d) == pytest.approx(401.0, rel=1e-3)
    dv = PF.derived_values(d)
    assert dv["hover_thrust"] == pytest.approx(0.488, rel=0.01) and dv["twr"] == pytest.approx(2.05, rel=0.01)
    assert pytest.approx(1.91, rel=0.01) == 4 * 19.23 / (4.1 * G)
    assert pytest.approx(9.378, rel=1e-3) == 3.825 * G / 4
    assert dv["tilt_at_8mps_deg"] == pytest.approx(7.43, rel=0.01)
    assert dv["cda_per_mass_m2kg"] == pytest.approx(0.00915, rel=0.01)
    assert dv["endurance_check_s"] == pytest.approx(1740, rel=0.01)
    assert D.num(d["mtow_kg"]) - D.num(d["mass_kg"]) == pytest.approx(0.275)
    T = PF.ProfileTable(vehicles=VEH)
    v1 = T.get("p600_mid360@1.0.0")
    assert (v1.version, v1.mass_kg) == ("1.0.0", 3.5) and D.num(v1.battery["p_hover_w"]) == 515.0
    s = model_summary(Catalog.load(VEH), "p600_mid360")
    assert s["speed"] == {"cruise_mps": 8.0, "max_mps": 12.0} and s["link_range_m"] == 3500.0
    assert s["endurance"]["hover_s"] == pytest.approx(1740, rel=0.01) and not s["reference"]
    assert s["default_sensors"] == ["mid360", "eo_gx40"]  # 映射顺序（P600 2.0.0 对象形式，D2-INT）


@pytest.mark.parametrize(("pid", "row"), [
    ("awr_q1", {"wheelbase": 0.380, "arm": 0.1344, "tmax": 5.59, "wmax": 800, "kf": 8.734e-6, "tau": 0.025, "d": 0.239, "crd": 5.52e-5,
                "cda": 0.0095, "J": (0.00596, 0.00596, 0.01098), "vh1": 0.417, "vh2": 2.40, "twr_mtow": 2.17, "p_hover": 112.2,
                "hover_s": 2100, "speed": (10.0, 15.0), "link": 8000}),
    ("awr_q9", {"wheelbase": 0.900, "arm": 0.3182, "tmax": 44.13, "wmax": 450, "kf": 2.179e-4, "tau": 0.06, "d": 0.533, "crd": 8.49e-4,
                "cda": 0.090, "J": (0.3166, 0.3166, 0.5836), "vh1": 0.500, "vh2": 2.00, "twr_mtow": 1.89, "p_hover": 835.1,
                "hover_s": 1920, "speed": (12.0, 20.0), "link": 15000}),
])
def test_reference_quads(pid: str, row: dict) -> None:
    d = _doc(pid)
    v = D.num
    assert v(d["geometry"]["wheelbase_m"]) == row["wheelbase"] and v(d["geometry"]["arm_xy_m"]) == row["arm"]
    m = d["motor"]
    assert (v(m["t_max_n"]), v(m["omega_max_rad_s"]), v(m["tau_s"])) == (row["tmax"], row["wmax"], row["tau"])
    assert rel(v(m["k_f"]), row["tmax"] / row["wmax"] ** 2) < 0.01 and rel(v(m["k_f"]), row["kf"]) < 1e-3
    assert (v(d["prop"]["d_m"]), v(d["prop"]["c_rd"]), v(d["aero"]["cda_m2"])) == (row["d"], row["crd"], row["cda"])
    J = d["inertia_kgm2"]["value"]
    assert (J["xx"], J["yy"], J["zz"]) == row["J"]
    mass = v(d["mass_kg"])
    L = row["wheelbase"] / 2
    assert rel(J["xx"], 0.0217 * (mass / 2.064) * (L / 0.246) ** 2) < 0.01  # J_x500 缩放规则
    dv = PF.derived_values(d)
    assert rel(dv["hover_thrust"], row["vh1"]) < 0.01 and rel(dv["twr"], row["vh2"]) < 0.01
    assert rel(4 * row["tmax"] / (v(d["mtow_kg"]) * G), row["twr_mtow"]) < 0.01
    assert dv["tilt_at_8mps_deg"] == pytest.approx(7.85, abs=0.02) and dv["cda_per_mass_m2kg"] == pytest.approx(0.0100, rel=1e-3)
    assert all(c["pass"] for c in PF.run_checks(d))
    # c_rd 口径：ΣΩ_hover·c_rd = 0.120·m
    so = 4 * row["wmax"] * dv["hover_thrust"] ** 0.5
    assert rel(so * row["crd"] / mass, 0.120) < 0.01
    assert D.multirotor_hover_power_w(d) == pytest.approx(row["p_hover"], rel=1e-3)
    assert D.e_use_wh(d) * 3600 / row["p_hover"] == pytest.approx(row["hover_s"], rel=1e-3)
    s = model_summary(Catalog.load(VEH), pid)
    assert (s["speed"]["cruise_mps"], s["speed"]["max_mps"]) == row["speed"] and s["link_range_m"] == row["link"]
    assert s["reference"] and s["category"] == "multirotor" and s["nest_kinds"] == ["box", "vtol_pad"]
    lp = d["limits_profiles"][d["catalog"]["limits_default"]]["px4_params"]
    assert {"MPC_Z_VEL_MAX_UP", "MPC_Z_VEL_MAX_DN", "MPC_ACC_HOR", "MC_YAWRATE_MAX"} <= set(lp)


@pytest.mark.parametrize(("pid", "dm", "dp", "mass", "p", "minutes"), [
    ("p600_mid360", 0.18, 3.0, 4.005, 432.6, 26.9),
    ("awr_q1", 0.09, 1.5, 1.04, 130.0, 30.2),
    ("awr_q9", 0.18, 3.0, 9.18, 863.3, 31.0),
])
def test_mass_energy_correction(pid: str, dm: float, dp: float, mass: float, p: float, minutes: float) -> None:
    r = D.mass_energy_correction(_doc(pid), dm, dp)
    assert r["mass_kg"] == pytest.approx(mass, abs=1e-6)
    assert r["p_hover_w"] == pytest.approx(p, abs=0.1)
    assert r["hover_s"] / 60 == pytest.approx(minutes, abs=0.05)


def test_battery_capacity_change_mass() -> None:
    """电池容量变化按比能量不变改变电池质量（§5.5）：P600 12 Ah（+45.6 Wh）为 +0.24 kg。"""
    d = _doc("p600_mid360")
    r = D.mass_energy_correction(d, 0.0, 0.0, d_wh=45.6)
    assert r["dm_kg"] == pytest.approx(0.24, rel=1e-3)
