"""D2 日历、太阳、照度、热背景、声学背景与按波长消光的纯函数（WP-09；D2-AC-15；AWR-04 §6.2、§6.3、§6.5、§7.3）。

- 太阳位置与 NOAA 参考值误差 ≤ 0.1°（12 组日期与地点）：以独立算法 PSA（Blanco-Muriel 等 2001，Solar Energy 70(5)）为参考，
  另以 NREL SPA 论文算例（Reda 与 Andreas 2004）与 Meeus 例 25.a、28.a 为绝对锚点；
- 照度分段、照度档阈值、k_light 锚点（AWR-04 §6.5：E ≈ 50 lx 时 h ≈ −2.5°，h ≈ −5.4° 时 k_light 0.5）；
- LWIR 水汽项随湿度单调、中纬度夏季 0.15–0.27 /km；MOR 由 5 km 降到 500 m 时各波长透过率单调下降；
- 声学背景：带级能量和等于总级，昼夜与晨昏过渡、降水 +5 至 +10 dB；
- golden `tests/environment/golden/d2_sky.json`（TS 对拍）与当前实现一致。
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from awr.environment.atmosphere.optics import optical_depth_lambda_arr, sigma_lambda, sigma_wv_per_m, water_vapour_gm3
from awr.environment.sky import acoustic as AC
from awr.environment.sky.calendar import (
    WORLD_TZ,
    Calendar,
    CalendarError,
    calendar_for_local,
    format_utc,
    parse_utc_ms,
    valid_tz,
)
from awr.environment.sky.illum import BAND_DAY, BAND_LOW, BAND_NIGHT, illum_band, illuminance_lx, illuminance_lx_arr
from awr.environment.sky.solar import refraction_deg, solar_position, solar_position_arr
from awr.environment.sky.thermal import SURFACE_CLASSES, k_cross_of, sky_temp_c, surface_temp_c
from awr.environment.weather.derive import derive
from awr.environment.weather.presets import MOR_BG, RH, presets

HERE = Path(__file__).resolve().parent


def _ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> float:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC).timestamp() * 1000.0


def psa(unix_ms: float, lat_deg: float, lon_deg: float) -> tuple[float, float]:
    """独立参考：PSA 算法（Blanco-Muriel 等 2001），-> (azimuth_deg 自北顺时针, elevation_deg 地心几何，未加视差)。"""
    jd = unix_ms / 86_400_000.0 + 2440587.5
    n = jd - 2451545.0
    hours = (unix_ms / 3_600_000.0) % 24.0
    omega = 2.1429 - 0.0010394594 * n
    mean_long = 4.8950630 + 0.017202791698 * n
    g = 6.2400600 + 0.0172019699 * n
    lam = mean_long + 0.03341607 * math.sin(g) + 0.00034894 * math.sin(2 * g) - 0.0001134 - 0.0000203 * math.sin(omega)
    eps = 0.4090928 - 6.2140e-9 * n + 0.0000396 * math.cos(omega)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam)) % (2 * math.pi)
    decl = math.asin(math.sin(eps) * math.sin(lam))
    gmst = 6.6974243242 + 0.0657098283 * n + hours
    lmst = math.radians(gmst * 15.0 + lon_deg)
    ha = lmst - ra
    lat = math.radians(lat_deg)
    zen = math.acos(math.cos(lat) * math.cos(ha) * math.cos(decl) + math.sin(decl) * math.sin(lat))
    az = math.atan2(-math.sin(ha), math.tan(decl) * math.cos(lat) - math.sin(lat) * math.cos(ha)) % (2 * math.pi)
    return math.degrees(az), 90.0 - math.degrees(zen)


# 12 组日期与地点：七个世界锚点与赤道、南北半球高纬；含分至日、晨昏与夜间
CASES_12 = [
    ("synthcity", 30.0, 120.0, (2026, 10, 5, 2, 0, 0)),
    ("synthcity", 30.0, 120.0, (2026, 10, 5, 9, 45, 0)),
    ("shanghai", 31.2281892, 121.5316942, (2026, 6, 21, 4, 0, 0)),
    ("shenzhen", 22.5431, 114.0579, (2026, 12, 21, 23, 30, 0)),
    ("suzhou", 31.2989, 120.5853, (2026, 3, 20, 10, 20, 0)),
    ("sanfrancisco", 37.7791216, -122.4212116, (2026, 10, 5, 20, 0, 0)),
    ("newyork", 40.7128, -74.006, (2026, 1, 15, 17, 30, 0)),
    ("chicago", 41.8781, -87.6298, (2026, 7, 4, 1, 10, 0)),
    ("equator", 0.0, 0.0, (2026, 3, 20, 12, 0, 0)),
    ("sydney", -33.8688, 151.2093, (2026, 9, 1, 6, 30, 0)),
    ("tromso", 69.6492, 18.9553, (2026, 6, 21, 23, 0, 0)),
    ("mcmurdo", -77.846, 166.676, (2026, 12, 1, 0, 0, 0)),
]


@pytest.mark.parametrize(("name", "lat", "lon", "ts"), CASES_12)
def test_sun_vs_reference_12_cases(name, lat, lon, ts):
    t = _ms(*ts)
    p = solar_position(t, lat, lon)
    az_r, el_r = psa(t, lat, lon)
    assert abs(p.el_deg - el_r) <= 0.1, (name, p.el_deg, el_r)
    if abs(p.el_deg) < 89.0:
        d_az = (p.az_deg - az_r + 180.0) % 360.0 - 180.0
        assert abs(d_az) * math.cos(math.radians(p.el_deg)) <= 0.1, (name, p.az_deg, az_r)


def test_sun_absolute_anchors():
    # NREL SPA 论文算例（Reda、Andreas 2004 表 A4.1）：2003-10-17 12:30:30 MST（UT−7），39.742476°N、105.1786°W；
    # 地形中心天顶角 50.11162°（含 820 mbar、11 °C 的折射）、方位 194.34024°；NOAA 折射按标准大气，差 ≈ 0.004°
    p = solar_position(_ms(2003, 10, 17, 19, 30, 30), 39.742476, -105.1786)
    assert abs(p.el_deg + refraction_deg(p.el_deg) - (90.0 - 50.11162)) <= 0.02
    assert abs(p.az_deg - 194.34024) <= 0.02
    # Meeus 例 25.a：1992-10-13 0h TD 视赤纬 −7.78507°；例 28.a：同时刻均时差 13.711 min（ΔT ≈ 59 s）
    q = solar_position(_ms(1992, 10, 13) - 59_000.0, 0.0, 0.0)
    assert abs(q.decl_deg - (-7.78507)) <= 0.005
    assert abs(q.eot_min - 13.711) <= 0.01


def test_solar_vectorised_matches_scalar():
    t = np.array([_ms(2026, 10, 5, h, 17) for h in range(24)])
    az, el = solar_position_arr(t, 30.0, 120.0)
    for i, ti in enumerate(t):
        p = solar_position(float(ti), 30.0, 120.0)
        assert abs(az[i] - p.az_deg) < 1e-9 and abs(el[i] - p.el_deg) < 1e-9


def test_illuminance_pieces_and_monotonic():
    assert illuminance_lx(90.0) == pytest.approx(1.2e5)
    assert illuminance_lx(10.0) == pytest.approx(1.2e5 * math.sin(math.radians(10.0)))
    assert illuminance_lx(9.999999) == pytest.approx(illuminance_lx(10.0), rel=1e-5)
    assert illuminance_lx(0.0) == pytest.approx(400.0)
    assert illuminance_lx(-6.0) == pytest.approx(3.0)
    assert illuminance_lx(-12.0) == pytest.approx(0.008)
    assert illuminance_lx(-18.0) == pytest.approx(0.001)
    assert illuminance_lx(-40.0) == pytest.approx(0.001)
    h = np.linspace(-30.0, 90.0, 2401)
    e = illuminance_lx_arr(h)
    assert np.all(np.diff(e) >= 0.0)
    assert np.allclose(e, [illuminance_lx(x) for x in h], rtol=1e-12)
    assert illuminance_lx(40.0, cover=1.0) == pytest.approx(0.3 * illuminance_lx(40.0))
    assert illuminance_lx(-30.0, moon_lx=0.1) == pytest.approx(0.101)
    # AWR-04 §6.5：被动 EO（彩色曲线 E_min 0.5 lx）的 k_light 在 E ≈ 50 lx（h ≈ −2.5°）开始下降，h ≈ −5.4° 时为 0.5
    k_color = lambda E: min(1.0, max(0.0, math.log10(E / 0.5) / 2))  # noqa: E731
    assert illuminance_lx(-2.55) == pytest.approx(50.0, rel=0.02)
    assert k_color(illuminance_lx(-5.37)) == pytest.approx(0.5, abs=0.01)


def test_bands():
    assert illum_band(6.0) == BAND_DAY and illum_band(5.999) == BAND_LOW
    assert illum_band(-6.0) == BAND_LOW and illum_band(-6.001) == BAND_NIGHT


def test_water_vapour_monotonic_in_humidity():
    rh = np.linspace(0.0, 1.0, 101)
    rv = np.array([water_vapour_gm3(x, 25.0) for x in rh])
    assert np.all(np.diff(rv) > 0)
    sw = np.array([sigma_wv_per_m(x) for x in rv])
    assert np.all(np.diff(sw) > 0)
    # 中纬度夏季 rho_v 10–20 g/m³ -> 0.15–0.27 /km（AWR-04 §6.3）
    assert sigma_wv_per_m(10.0) * 1000 == pytest.approx(0.15) and sigma_wv_per_m(20.0) * 1000 == pytest.approx(0.27)
    assert 10.0 < water_vapour_gm3(0.6, 25.0) < 20.0
    # LWIR 点消光随湿度单调（其余条件不变）
    d = derive(presets().preset_vector("clear"))
    lw = [sigma_lambda(d, 10000.0, water_vapour_gm3(x, 20.0)) for x in rh]
    assert np.all(np.diff(lw) > 0)
    assert sigma_lambda(d, 905.0, 99.0) == sigma_lambda(d, 905.0)  # 近红外不含水汽项


def test_transmittance_monotonic_in_mor():
    """D2-AC-15：MOR 由 5 km 降到 500 m 时（固定斜距、各波长）透过率单调下降（N_eff 经 k_c 单调下降的环境部分）。"""
    P = presets()
    mors = np.geomspace(5000.0, 500.0, 40)
    for lam in (550.0, 850.0, 905.0, 10000.0):
        taus = []
        for mor in mors:
            s = P.preset_vector("clear")
            s[MOR_BG] = mor
            s[RH] = 0.6
            d = derive(s)
            od = optical_depth_lambda_arr(np.array([60.0]), np.array([-0.3]), np.array([600.0]), d, 0.0, 1500.0, lam, 12.0)
            taus.append(math.exp(-float(od[0])))
        assert np.all(np.diff(taus) < 0), lam
    # 550 nm 与 D1 光学厚度逐位相同；LWIR 远比可见光透明
    s = P.preset_vector("haze")
    d = derive(s)
    from awr.environment.atmosphere.optics import optical_depth_arr

    a = optical_depth_lambda_arr(np.array([10.0]), np.array([0.1]), np.array([800.0]), d, 0.0, 1500.0, 550.0)
    b = optical_depth_arr(np.array([10.0]), np.array([0.1]), np.array([800.0]), d, 0.0, 1500.0)
    assert a[0] == b[0]
    lw = optical_depth_lambda_arr(np.array([10.0]), np.array([0.1]), np.array([800.0]), d, 0.0, 1500.0, 10000.0, 10.0)
    assert lw[0] < 0.5 * a[0]


def test_thermal_background():
    urban = SURFACE_CLASSES["urban"]
    assert surface_temp_c(20.0, 60.0, 0.0, 0.0, urban) > 30.0
    assert surface_temp_c(20.0, -20.0, 0.0, 0.0, urban) == pytest.approx(17.5)
    assert surface_temp_c(20.0, -20.0, 1.0, 0.0, urban) > surface_temp_c(20.0, -20.0, 0.0, 0.0, urban)
    assert sky_temp_c(15.0, 0.5, 0.0, 1500.0) == pytest.approx(-15.0)
    assert sky_temp_c(15.0, 0.5, 1.0, 1000.0) == pytest.approx(8.5)
    assert k_cross_of(3.0, -5.0, 15.0) == 0.5  # 日出后 1 h 内
    assert k_cross_of(3.0, 15.0, -5.0) == 0.5  # 日落前 1 h 内
    assert k_cross_of(30.0, 20.0, 40.0) == 1.0 and k_cross_of(-10.0, -20.0, -1.0) == 1.0


def test_acoustic_profiles():
    for prof in AC.PROFILES:
        day, night = AC.LEVELS_DBA[prof]
        assert AC.level_dba(prof, 30.0) == day and AC.level_dba(prof, -10.0) == night
        assert AC.level_dba(prof, 0.0) == pytest.approx((day + night) / 2)
        for tot in (30.0, 47.3, 60.0):
            assert AC.total_dba(AC.band_levels_dba(prof, tot)) == pytest.approx(tot, abs=1e-9)
        assert len(AC.REL_SHAPE_DB[prof]) == AC.N_BANDS == 24
    assert AC.level_dba("rural", -10.0) == 30.0  # AWR-04 §7.3 单带锚点的 L_bg
    assert AC.rain_boost_db(0.0) == 0.0
    assert 5.0 <= AC.rain_boost_db(0.5) <= 10.0 and AC.rain_boost_db(50.0) == pytest.approx(10.0)
    assert np.all(np.diff([AC.rain_boost_db(r) for r in np.linspace(0, 60, 200)]) >= 0)
    assert AC.a_weight_db(1000.0) == pytest.approx(0.0, abs=0.01) and AC.a_weight_db(100.0) == pytest.approx(-19.1, abs=0.1)


def test_calendar_parse_and_jump():
    t = parse_utc_ms("2026-10-05T15:00:00Z")
    assert t == int(_ms(2026, 10, 5, 15))
    assert parse_utc_ms("2026-10-05 15:00") == t
    assert parse_utc_ms("2026-10-05T23:00:00+08:00") == t
    assert parse_utc_ms("2026-10-05T23:00:00.250+0800") == t + 250
    for bad in ("2026-13-01T00:00:00Z", "2026-02-30T00:00:00Z", "1900-12-31T00:00:00Z", "tomorrow", "2026-10-05T15:00:00+19:00"):
        with pytest.raises(CalendarError):
            parse_utc_ms(bad)
    assert valid_tz("Asia/Shanghai") and not valid_tz("Mars/Olympus")
    with pytest.raises(CalendarError):
        Calendar.from_request("2026-10-05T15:00:00Z", "Mars/Olympus")
    cal = Calendar.from_request("2026-10-05T15:00:00Z", "Asia/Shanghai", jump=True, t_apply_ns=3_600_000_000_000)
    assert cal.t_cal_ms(3_600_000_000_000) == t  # jump：生效时刻即为该日历时刻
    assert cal.start_utc() == "2026-10-05T14:00:00Z"
    assert cal.to_wire(3_600_000_000_000)["t_cal_unix_ns"] == str(t * 1_000_000)
    nj = Calendar.from_request("2026-10-05T15:00:00Z", "Asia/Shanghai", jump=False, t_apply_ns=3_600_000_000_000)
    assert nj.start_utc() == "2026-10-05T15:00:00Z"
    assert format_utc(t + 5) == "2026-10-05T15:00:00.005Z"
    assert calendar_for_local(2026, 10, 5, 10, 0, WORLD_TZ["synthcity"]) == {"start_utc": "2026-10-05T02:00:00Z", "tz": "Asia/Shanghai"}
    assert set(WORLD_TZ) == {"synthcity", "shanghai", "shenzhen", "suzhou", "sanfrancisco", "newyork", "chicago"}


def test_golden_up_to_date():
    import sys

    sys.path.insert(0, str(HERE))
    from d2fix import fixture

    have = json.loads((HERE / "golden" / "d2_sky.json").read_text(encoding="utf-8"))
    want = json.loads(json.dumps(fixture()))
    assert have == want, "regenerate: python tests/environment/d2fix.py"
