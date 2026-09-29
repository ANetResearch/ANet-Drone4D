"""Supplementary golden for uavNN/local and GPST/UTC (M02-AC-006, AC-009; D1-AC-13), from INDEPENDENT oracles.

The main golden (`packages/contracts/golden/frames/{local_px4,time}.json`, M00) is produced by the implementation itself
and therefore only locks regressions. These cases come from formulations that share no code with `frames.py` /
`time.py`:
- azimuthal equidistant projection on the R = 6371000 m sphere written with unit vectors (angle = atan2(|a x b|, a . b),
  bearing measured in the tangent plane of the reference), and its inverse as a rotation of the reference vector;
- UTC <-> GPST from `datetime` arithmetic and the IERS leap-second list written as ISO dates (Bulletin C), counting
  inserted seconds instead of looking up POSIX-second tables.
Writes tests/georef/golden/local_time_supplement.json (proposed for merging into the M00 groups; request M02-to-M00).
Run: .venv/bin/python tests/georef/gen_supplement_golden.py
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).with_name("golden") / "local_time_supplement.json"
R_SPHERE = 6_371_000.0
LEAPS_ISO = ["1972-07-01", "1973-01-01", "1974-01-01", "1975-01-01", "1976-01-01", "1977-01-01", "1978-01-01", "1979-01-01",
             "1980-01-01", "1981-07-01", "1982-07-01", "1983-07-01", "1985-07-01", "1988-01-01", "1990-01-01", "1991-01-01",
             "1992-07-01", "1993-07-01", "1994-07-01", "1996-01-01", "1997-07-01", "1999-01-01", "2006-01-01", "2009-01-01",
             "2012-07-01", "2015-07-01", "2017-01-01"]          # TAI-UTC = 10 s from 1972-01-01, +1 at each date
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
GPS_EPOCH = dt.datetime(1980, 1, 6, tzinfo=dt.UTC)
NS = 1_000_000_000


def _unit(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    return np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])


def _ll(v):
    return math.degrees(math.atan2(v[2], math.hypot(v[0], v[1]))), math.degrees(math.atan2(v[1], v[0]))


def aeqd_forward(lat, lon, lat0, lon0):
    a, b = _unit(lat0, lon0), _unit(lat, lon)
    c = math.atan2(np.linalg.norm(np.cross(a, b)), float(a @ b))
    east = np.cross([0.0, 0.0, 1.0], a)
    east /= np.linalg.norm(east)
    north = np.cross(a, east)
    t = b - (a @ b) * a
    brg = math.atan2(float(t @ east), float(t @ north))
    return R_SPHERE * c * math.cos(brg), R_SPHERE * c * math.sin(brg)


def aeqd_inverse(x, y, lat0, lon0):
    a = _unit(lat0, lon0)
    east = np.cross([0.0, 0.0, 1.0], a)
    east /= np.linalg.norm(east)
    north = np.cross(a, east)
    c = math.hypot(x, y) / R_SPHERE
    if c == 0.0:
        return lat0, lon0
    d = (x * north + y * east) / math.hypot(x, y)
    return _ll(math.cos(c) * a + math.sin(c) * d)


def tai_minus_utc(t: dt.datetime) -> int:
    return 10 + sum(t >= dt.datetime.fromisoformat(d).replace(tzinfo=dt.UTC) for d in LEAPS_ISO)


def utc_to_gpst_ns(t: dt.datetime, frac_ns: int = 0) -> int:
    # GPST = elapsed SI seconds since the GPS epoch = POSIX difference + leap seconds inserted after 1980-01-06
    return int((t - GPS_EPOCH).total_seconds()) * NS + frac_ns + (tai_minus_utc(t) - 19) * NS


def main() -> None:
    rng = np.random.default_rng(20260929)
    cases = []
    refs = {"hefei": (31.8206, 117.2272), "shenzhen": (22.5160584, 113.9432472), "newyork": (40.7130611, -74.0023445),
            "sanfrancisco": (37.7791216, -122.4212116), "chicago": (41.8841302, -87.6225307), "high_lat": (69.65, 18.96),
            "south": (-33.86, 151.21), "dateline": (-17.7, 179.95)}
    for name, (la0, lo0) in refs.items():
        for d in (1.0, 100.0, 1000.0, 5000.0):
            for brg in rng.uniform(0.0, 2 * math.pi, 3):
                x, y = d * math.cos(brg), d * math.sin(brg)
                la, lo = aeqd_inverse(x, y, la0, lo0)
                cases.append({"fn": "px4_reproject", "ref": name, "args": {"x_n": x, "y_e": y, "ref_lat": la0, "ref_lon": lo0},
                              "out": {"lat_deg": la, "lon_deg": lo}, "kinds": {"lat_deg": "angle_deg", "lon_deg": "angle_deg"}})
                xf, yf = aeqd_forward(la, lo, la0, lo0)
                cases.append({"fn": "px4_project", "ref": name, "args": {"lat": la, "lon": lo, "ref_lat": la0, "ref_lon": lo0},
                              "out": {"x_n": xf, "y_e": yf}, "kinds": {"x_n": "position_m", "y_e": "position_m"}})
    tcases = []
    for d in LEAPS_ISO:
        t = dt.datetime.fromisoformat(d).replace(tzinfo=dt.UTC)
        for off in (-1, 0, 1):
            ts = t + dt.timedelta(seconds=off)
            u = int((ts - EPOCH).total_seconds()) * NS
            tcases.append({"fn": "utc_to_gpst_ns", "args": {"unix_utc_ns": u}, "out": {"gpst_ns": utc_to_gpst_ns(ts)}})
            tcases.append({"fn": "tai_minus_utc_s", "args": {"unix_utc_ns": u}, "out": {"s": tai_minus_utc(ts)}})
        # the inserted second 23:59:60 maps to GPST instants that have no POSIX second of their own
        before = t - dt.timedelta(seconds=1)
        g_leap = utc_to_gpst_ns(before) + NS + NS // 2        # half-way through 23:59:60
        if t > GPS_EPOCH:
            tcases.append({"fn": "gpst_to_utc_ns", "args": {"gpst_ns": g_leap},
                           "out": {"unix_utc_ns": int((before - EPOCH).total_seconds()) * NS + NS // 2, "leap_second": True}})
    for _ in range(200):
        s = int(rng.integers(int((GPS_EPOCH - EPOCH).total_seconds()), int((dt.datetime(2026, 12, 1, tzinfo=dt.UTC) - EPOCH).total_seconds())))
        ns = int(rng.integers(0, NS))
        t = EPOCH + dt.timedelta(seconds=s)
        g = utc_to_gpst_ns(t, ns)
        tcases.append({"fn": "utc_to_gpst_ns", "args": {"unix_utc_ns": s * NS + ns}, "out": {"gpst_ns": g}})
        tcases.append({"fn": "gpst_to_utc_ns", "args": {"gpst_ns": g}, "out": {"unix_utc_ns": s * NS + ns, "leap_second": False}})
        tcases.append({"fn": "gps_week_tow", "args": {"gpst_ns": g}, "out": {"week": g // (604800 * NS), "tow_ns": g % (604800 * NS)}})
        tcases.append({"fn": "gpst_to_tai_ptp_ns", "args": {"gpst_ns": g},
                       "out": {"tai_ns": g + int((GPS_EPOCH - EPOCH).total_seconds()) * NS + 19 * NS}})
    for week in (1023, 1024, 2047, 2048, 2400):                     # 10-bit week rollovers stay continuous in full weeks
        g = week * 604800 * NS + 123 * NS
        tcases.append({"fn": "gps_week_tow", "args": {"gpst_ns": g}, "out": {"week": week, "tow_ns": 123 * NS}})
    doc = {"schema": "awr.golden.frames.v1", "generator": "tests/georef/gen_supplement_golden.py", "seed": 20260929,
           "oracle": "independent: vector azimuthal equidistant on R = 6371000 m; datetime + ISO leap-second dates",
           "tolerance": {"rtol": 1e-9, "atol": {"position_m": 1e-6, "angle_rad": 1e-12}},
           "local_px4": cases, "time": tcases}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=None, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"{OUT}: {len(cases)} local cases, {len(tcases)} time cases")


if __name__ == "__main__":
    main()
