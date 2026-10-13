"""M19-AC-008（P1）：雷达进阶。R 级须 SNR ≥ 20 dB 持续 2 s（微多普勒分类，class_hint 给出类别）；径向速度 < 0.3 m/s 的地面目标须
sigma ≥ 10·sigma0·A_cell，A_cell = R·Δθ_az·ΔR·sec psi_g，与解析值对拍（AWR-20 §6.5：R 150 m、psi_g 30° 时 3.63 m²、门限 0.36 m²：
静止的人可检、狗不可检）。"""

from __future__ import annotations

import math

import pytest
from plib import FakeWorld, PBench, look_R, make_nonimg

from awr.sim.perception import mmw77
from awr.sim.perception.nonimaging import a_cell_m2, clutter_ok


def test_a_cell_analytic():
    sp = mmw77()
    a = a_cell_m2(sp, 150.0, math.radians(30.0))
    assert a == pytest.approx(150.0 * math.radians(4.0) * 0.3 / math.cos(math.radians(30.0)))
    assert a == pytest.approx(3.63, abs=0.01)
    assert clutter_ok(sp, 0.5, 150.0, math.radians(30.0)) and not clutter_ok(sp, 0.1, 150.0, math.radians(30.0))


def _run(cls: str, xy: tuple[float, float], h: float, seconds: float, **over):
    b = PBench(1, FakeWorld())
    b.add({"class": cls, "pos": [xy[0], xy[1], 0.0], "motion": {"mode": "static"}, **over})
    b.step(1)
    p = (0.0, 0.0, h)
    b.platform(0, pos=p, nonimg=[make_nonimg("radar", mmw77(), p, R=look_R(p, (xy[0], xy[1], 0.0)))])   # 安装俯角对准识别物
    b.run(seconds)
    out = b.prt.radar_last["uav-01"]
    b.close()
    return out


def test_static_ground_clutter_gate():
    # 擦地角约 30°、R 140 m（A_cell 3.39 m²，门限 0.34 m²）：人 0.5 m² 可检；狗 0.1 m² 被杂波门限挡住（AWR-20 §6.5）
    h = 70.0
    x = math.sqrt(140.0 ** 2 - h ** 2)
    person = _run("person", (x, 0.0), h, 1.0)
    dog = _run("dog", (x, 0.0), h, 1.0)
    assert person["tracks"] and not person["clutter_gated"]
    assert dog["tracks"] == [] and dog["clutter_gated"]


def test_r_level_after_2s_dwell():
    out1 = _run("vehicle", (60.0, 0.0), 20.0, 1.5)
    assert out1["tracks"][0]["level"] == "D" and out1["tracks"][0]["snr_db"] >= 20.0
    out3 = _run("vehicle", (60.0, 0.0), 20.0, 3.0)
    t = out3["tracks"][0]
    assert t["level"] == "R" and t["class_hint"] == "vehicle"
