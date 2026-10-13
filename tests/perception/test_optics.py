"""M19-AC-002：光学。p_eff 各输出档（4K 1.45、1080P 2.90、720P 4.35、SXGA 3.06、1.3M 3.26 µm）；p' 衍射约束只影响 4K（550 nm
F3.2 为 1.76 µm；NIR 850 nm 为 2.72 µm）；GSD 与 R·p'/f 误差 ≤ 1%；F 数两种插值律；契约夹具 `optics_cases.json`；均匀消光
与 M07 sigma_lambda 同一口径（Kim、水汽项）。"""

from __future__ import annotations

import json
import math
from dataclasses import replace

import pytest

from awr.contracts._paths import contracts_root
from awr.sim.perception import (
    f_number,
    gsd_m,
    gx40,
    gx40_nir,
    hfov_rad,
    lwir640,
    p_eff_um,
    p_prime_um,
    pixels_on_target,
    sigma_per_m,
    tau,
)


def test_p_eff_outputs():
    s = gx40()
    want = {"4k": 1.45, "1080p": 2.90, "720p": 4.35, "sxga": 1.45 * 2700 / 1280, "1p3m": 1.45 * 2880 / 1280}
    for out, p in want.items():
        assert p_eff_um(s, out) == pytest.approx(p, rel=1e-9)
    assert p_eff_um(s, "sxga") == pytest.approx(3.06, abs=0.01) and p_eff_um(s, "1p3m") == pytest.approx(3.26, abs=0.01)


def test_diffraction_only_bounds_4k():
    s = gx40()
    assert p_prime_um(s, 48.0, "4k") == pytest.approx(1.76, abs=1e-9)
    for out in ("1080p", "720p", "sxga", "1p3m"):
        assert p_prime_um(s, 48.0, out) == pytest.approx(p_eff_um(s, out))
    assert p_prime_um(s, 4.8, "4k") == pytest.approx(1.45)
    assert p_prime_um(gx40_nir(), 48.0, "4k") == pytest.approx(0.85 * 3.2)          # 850 nm：2.72 µm
    assert p_prime_um(lwir640(100.0), 100.0) == pytest.approx(12.0)                 # λF = 10 µm < 12 µm
    # 线性 F 数律下 4K 在 F# > 2.64（f > 31.8 mm）受衍射约束（d2-WP-08-to-M19 第 3 条）
    f_star = 4.8 + (1.45 / 0.55 - 1.7) / 1.5 * 43.2
    assert f_star == pytest.approx(31.8, abs=0.1)
    assert p_prime_um(s, f_star - 0.5, "4k") == pytest.approx(1.45) and p_prime_um(s, f_star + 0.5, "4k") > 1.45


def test_f_number_laws():
    s = gx40()
    assert f_number(s, 4.8) == 1.7 and f_number(s, 48.0) == 3.2 and f_number(s, 26.4) == pytest.approx(2.45)
    ln = replace(s, f_law="linear_ln_f")
    assert f_number(ln, math.sqrt(4.8 * 48.0)) == pytest.approx(2.45)


def test_gsd_and_pixels():
    s = gx40()
    for r, f in ((100.0, 4.8), (300.0, 4.8), (300.0, 48.0), (1000.0, 48.0)):
        p = p_prime_um(s, f)
        assert gsd_m(r, p, f) == pytest.approx(r * p * 1e-3 / f, rel=1e-12)
    assert gsd_m(100.0, 2.9, 4.8) == pytest.approx(0.0604, abs=0.0001)      # AWR-04 §6.1 示例 6.0 cm
    assert gsd_m(1000.0, 2.9, 48.0) == pytest.approx(0.0604, abs=0.0001)
    assert pixels_on_target(0.7246, 48.0, 857.0, 2.9) == pytest.approx(14.0, rel=0.01)


def test_contract_optics_fixture():
    cases = json.loads((contracts_root() / "fixtures" / "perception" / "optics_cases.json").read_text(encoding="utf-8"))["cases"]
    s = gx40()
    for c in cases:
        if c["sensor"] != "eo_gx40":
            continue
        res, f = c["resolution"], c["f_mm"]
        assert f_number(s, f) == pytest.approx(c["f_number"], abs=1e-6)
        assert p_prime_um(s, f, res) == pytest.approx(c["p_prime_um"], abs=1e-6)
        assert math.degrees(hfov_rad(s, f, res)) == pytest.approx(c["hfov_deg"], abs=1e-5)
        assert gsd_m(300.0, p_prime_um(s, f, res), f) == pytest.approx(c["gsd_m_at_300m"], rel=1e-6)


def test_uniform_extinction_matches_m07():
    from awr.environment.atmosphere.optics import kim_q, sigma_wv_per_m
    from awr.environment.weather.derive import K_V2

    for mor in (500.0, 3000.0, 10000.0, 50000.0):
        s550 = math.log(20.0) / mor
        assert sigma_per_m(mor, 550.0) == pytest.approx(s550)
        q = kim_q(K_V2 / s550 / 1000.0)
        assert sigma_per_m(mor, 850.0) == pytest.approx(s550 * (850.0 / 550.0) ** -q)
        assert sigma_per_m(mor, 10000.0, 10.0) == pytest.approx(s550 * (10000.0 / 550.0) ** -q + sigma_wv_per_m(10.0))
    # AWR-20 §6.4：rho_v = 10 g/m³ 时 LWIR MOR 10 km 0.157 /km、3 km 0.210 /km
    assert sigma_per_m(10000.0, 10000.0, 10.0) * 1000 == pytest.approx(0.157, abs=0.002)
    assert sigma_per_m(3000.0, 10000.0, 10.0) * 1000 == pytest.approx(0.210, abs=0.003)
    assert tau(10000.0, 550.0, 0.0) == 1.0 and tau(1e12, 550.0, 1000.0) == pytest.approx(1.0, abs=1e-6)
