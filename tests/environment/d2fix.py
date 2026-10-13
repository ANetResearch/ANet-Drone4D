"""D2 环境纯函数的 TS 对拍夹具（WP-09；AWR-04 §6.3、§6.5、§7.3；M07-AC-002 的 D2 延伸）。

`python tests/environment/d2fix.py` 重新生成 `tests/environment/golden/d2_sky.json`；`test_sky.py::test_golden_up_to_date`
断言夹具与当前实现一致，`apps/web/tests/environment/sky.test.ts` 用同一夹具对拍 `engine/environment/sky/*.ts` 与 optics.ts。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python"))

from awr.environment.atmosphere.optics import kim_factor, sigma_lambda, sigma_wv_per_m, water_vapour_gm3  # noqa: E402
from awr.environment.sky import acoustic as AC  # noqa: E402
from awr.environment.sky.calendar import parse_utc_ms  # noqa: E402
from awr.environment.sky.illum import illum_band, illuminance_lx  # noqa: E402
from awr.environment.sky.solar import refraction_deg, solar_position  # noqa: E402
from awr.environment.sky.thermal import (  # noqa: E402
    SURFACE_CLASSES,
    air_temp_c,
    k_cross_of,
    sky_temp_c,
    surface_temp_c,
)
from awr.environment.weather.derive import derive  # noqa: E402
from awr.environment.weather.presets import MOR_BG, RH, presets  # noqa: E402

OUT = Path(__file__).resolve().parent / "golden" / "d2_sky.json"

# 世界锚点与几个极端纬度（极昼、极夜、赤道、南半球）
PLACES = [("synthcity", 30.0, 120.0), ("shanghai", 31.2281892, 121.5316942), ("sanfrancisco", 37.7791216, -122.4212116),
          ("newyork", 40.7128, -74.006), ("equator", 0.0, 0.0), ("sydney", -33.8688, 151.2093), ("tromso", 69.6492, 18.9553),
          ("mcmurdo", -77.846, 166.676)]
TIMES = ["2026-03-20T00:00:00Z", "2026-03-20T04:00:00Z", "2026-06-21T03:30:00Z", "2026-06-21T12:00:00Z", "2026-09-23T09:15:00Z",
         "2026-10-05T10:42:00Z", "2026-10-05T22:10:00Z", "2026-12-21T16:00:00Z", "2030-01-01T00:00:00Z", "1999-08-11T11:03:00Z"]


def _ms(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000.0


def cases() -> list[dict]:
    out: list[dict] = []

    def add(case: dict) -> None:
        out.append(case)

    for name, lat, lon in PLACES:
        for t in TIMES:
            p = solar_position(_ms(t), lat, lon)
            add({"fn": "solar_position", "args": {"unix_ms": _ms(t), "lat_deg": lat, "lon_deg": lon, "place": name},
                        "out": {"az_deg": p.az_deg, "el_deg": p.el_deg, "decl_deg": p.decl_deg, "eot_min": p.eot_min, "tst_min": p.tst_min}})
    for h in (-89.0, -30.0, -18.0, -15.0, -12.0, -9.0, -6.0, -5.37, -2.55, -1.0, -0.575, 0.0, 2.0, 5.0, 10.0, 30.0, 60.0, 89.9):
        add({"fn": "refraction_deg", "args": {"el_deg": h}, "out": {"v": refraction_deg(h)}})
        for c in (0.0, 0.5, 1.0):
            add({"fn": "illuminance_lx", "args": {"el_deg": h, "cover": c, "moon_lx": 0.0}, "out": {"v": illuminance_lx(h, c)}})
        add({"fn": "illuminance_lx", "args": {"el_deg": h, "cover": 0.2, "moon_lx": 0.1}, "out": {"v": illuminance_lx(h, 0.2, 0.1)}})
        add({"fn": "illum_band", "args": {"el_deg": h}, "out": {"v": illum_band(h)}})
    for tst in (0.0, 180.0, 540.0, 900.0, 1200.0, 1439.0):
        for c in (0.0, 0.7):
            add({"fn": "air_temp_c", "args": {"t_mean": 14.9, "tst_min": tst, "cover": c}, "out": {"v": air_temp_c(14.9, tst, c)}})
    for cls in SURFACE_CLASSES:
        for h, c, w in ((45.0, 0.0, 0.0), (10.0, 0.5, 0.3), (-3.0, 0.0, 0.0), (-20.0, 0.9, 1.0)):
            add({"fn": "surface_temp_c", "args": {"t_air": 18.0, "el_deg": h, "cover": c, "wetness": w, "cls": cls},
                        "out": {"v": surface_temp_c(18.0, h, c, w, SURFACE_CLASSES[cls])}})
    for rh, c, base in ((0.2, 0.0, 1500.0), (0.8, 0.5, 600.0), (0.5, 1.0, 3000.0)):
        add({"fn": "sky_temp_c", "args": {"t_air": 15.0, "rh": rh, "cover": c, "cloud_base_m": base},
                    "out": {"v": sky_temp_c(15.0, rh, c, base)}})
    for a, b, c in ((5.0, -3.0, 18.0), (5.0, 12.0, -3.0), (-2.0, -9.0, 6.0), (40.0, 30.0, 45.0), (-0.833, -10.0, 8.0)):
        add({"fn": "k_cross_of", "args": {"el_now": a, "el_before": b, "el_after": c}, "out": {"v": k_cross_of(a, b, c)}})
    for f in AC.BANDS_HZ:
        add({"fn": "a_weight_db", "args": {"f_hz": f}, "out": {"v": AC.a_weight_db(f)}})
    for prof in AC.PROFILES:
        for h, r in ((30.0, 0.0), (0.0, 0.0), (-10.0, 0.0), (-3.0, 0.2), (20.0, 12.0)):
            lv = AC.level_dba(prof, h, r)
            add({"fn": "level_dba", "args": {"profile": prof, "el_deg": h, "rain_eff_mmh": r}, "out": {"v": lv}})
            add({"fn": "band_levels_dba", "args": {"profile": prof, "total": lv}, "out": {"v": [float(x) for x in AC.band_levels_dba(prof, lv)]}})
    for rh, t in ((0.0, 15.0), (0.5, 14.9), (0.9, 30.0), (0.3, -10.0)):
        rv = water_vapour_gm3(rh, t)
        add({"fn": "water_vapour_gm3", "args": {"rh": rh, "t_c": t}, "out": {"v": rv}})
        add({"fn": "sigma_wv_per_m", "args": {"rho_v_gm3": rv}, "out": {"v": sigma_wv_per_m(rv)}})
    P = presets()
    for pid in ("clear", "haze", "fog", "rain", "heavyRain"):
        for mor in (None, 500.0, 5000.0):
            s = P.preset_vector(pid)
            if mor is not None:
                s[MOR_BG] = mor
            s[RH] = 0.6
            d = derive(s)
            for lam in (550.0, 850.0, 905.0, 10000.0):
                add({"fn": "sigma_lambda", "args": {"s": [float(x) for x in s], "lam_nm": lam, "rho_v_gm3": 12.5},
                            "out": {"v": sigma_lambda(d, lam, 12.5), "kim": kim_factor(d, lam)}})
    for s in ("2026-10-05T15:00:00Z", "2026-10-05 07:30", "2026-10-05T23:59:59.5+08:00", "2026-02-29T00:00:00Z", "1900-12-31T00:00:00Z",
              "2026-10-05T15:00:00-05:30", "2026-13-01T00:00:00Z", "yesterday", "2099-12-31T23:59:59.999Z"):
        try:
            v: int | None = parse_utc_ms(s)
        except ValueError:
            v = None
        add({"fn": "parse_utc_ms", "args": {"s": s}, "out": {"v": v}})
    return out


def fixture() -> dict:
    return {"schema": "awr.test.env.d2_sky.v1", "tolerance": {"rtol": 1e-9, "atol": 1e-9}, "cases": cases()}


def write() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixture(), separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    write()
