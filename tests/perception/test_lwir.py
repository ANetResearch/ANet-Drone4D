"""M19-AC-006：LWIR。relative 与 absolute 两种热模式、k_cross 0.5 时段、杂波日夜差异；关键维度乘 √max(hot_fraction, 0.25)；数字
变焦不改变 N（N 按原生 12 µm 像元，型号的 digital_zoom 只影响显示）；调色板只有白热与黑热两种灰阶（契约与型号）。"""

from __future__ import annotations

import math

import pytest
from pf import at, view

from awr.sim.perception import EnvSample, SensorState, ViewGeom, lwir640, perception_level
from awr.sim.perception.specs import load_model_doc

LW = SensorState(lwir640(100.0), 100.0)


def _lv(env: EnvSample, r: float = 300.0, **over):
    return perception_level(LW, view("person", **over), 0, ViewGeom(at(r, 10.0)), env, 14.0)


def test_relative_mode_and_cross_over():
    a = _lv(EnvSample(band="night", mor_m=1e9))
    tau = math.exp(-(0.03 + 0.012 * 7.5) / 1000 * a.range_m)
    assert a.dt_app_k == pytest.approx(tau * 0.97 * 8.0, rel=1e-6)
    b = _lv(EnvSample(band="night", mor_m=1e9, k_cross=0.5))
    assert b.dt_app_k == pytest.approx(0.5 * a.dt_app_k) and b.k[1] < a.k[1]


def test_absolute_mode():
    th = {"mode": "absolute", "t_surface_c": 32.0, "emissivity": 0.97, "hot_fraction": 1.0}
    env = EnvSample(band="night", mor_m=1e9, t_bg_c=10.0, eps_bg=0.95, t_sky_c=-20.0)
    r = _lv(env, thermal=th)
    tau = math.exp(-(0.03 + 0.012 * 7.5) / 1000 * r.range_m)
    K = 273.15
    want = tau * (0.97 * (32 + K) + 0.03 * (-20 + K) - 0.95 * (10 + K) - 0.05 * (-20 + K))
    assert r.dt_app_k == pytest.approx(want, rel=1e-6)
    hot = _lv(EnvSample(band="night", mor_m=1e9, t_bg_c=31.0, eps_bg=0.97), thermal=th)
    assert abs(hot.dt_app_k) < abs(r.dt_app_k)            # 背景温度接近目标：热对比下降（热交叉）


def test_clutter_day_night():
    day = _lv(EnvSample(band="day", mor_m=1e9))
    night = _lv(EnvSample(band="night", mor_m=1e9))
    assert day.k[1] < night.k[1]                           # 杂波 2.0 K（日）对 1.0 K（夜）
    assert day.k[2] == night.k[2] == 1.0                   # LWIR 不受照度影响


def test_hot_fraction_scaling_and_native_pixels():
    full = _lv(EnvSample(band="night", mor_m=1e9))
    part = _lv(EnvSample(band="night", mor_m=1e9), thermal={"mode": "relative", "delta_t_k": 8.0, "emissivity": 0.97, "hot_fraction": 0.09})
    assert part.n == pytest.approx(full.n * math.sqrt(0.25))   # 0.09 < 0.25 取 0.25
    sn = lwir640(100.0)
    assert sn.digital_zoom_max == 4.0 and sn.outputs == {}
    assert full.gsd_m == pytest.approx(full.range_m * 12e-3 / 100.0)


def test_palettes_only_two_grey_scales():
    assert load_model_doc("lwir640")["palettes"] == ["white_hot", "black_hot"]
    import json

    from awr.contracts._paths import contracts_root

    s = json.loads((contracts_root() / "sensor" / "lwir.schema.json").read_text(encoding="utf-8"))
    assert "white_hot" in json.dumps(s) and "rainbow" not in json.dumps(s) and "iron" not in json.dumps(s)
