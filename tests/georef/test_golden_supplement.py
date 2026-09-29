"""Supplementary uavNN/local and GPST/UTC golden from independent oracles (M02-AC-006, AC-009; D1-AC-13).

Cases in tests/georef/golden/local_time_supplement.json come from gen_supplement_golden.py (vector-form azimuthal
equidistant projection; datetime plus ISO leap-second dates), not from frames.py / time.py, so they check the
implementation instead of only locking it. Positions and angles use the mixed tolerance of AWR-03 §5.1 rule 8; time is
exact to the nanosecond. Also: rigid uavNN/local for metric-tangent backends and the PX4 error bound.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from georef_golden import close, flat

from awr.world.georef import frames as F
from awr.world.georef import time as T

GOLDEN = json.loads((Path(__file__).with_name("golden") / "local_time_supplement.json").read_text(encoding="utf-8"))


def test_local_px4_against_vector_oracle():
    tol = GOLDEN["tolerance"]
    n = 0
    for c in GOLDEN["local_px4"]:
        a = c["args"]
        if c["fn"] == "px4_reproject":
            got = dict(zip(("lat_deg", "lon_deg"), (float(v) for v in F.px4_reproject(a["x_n"], a["y_e"], a["ref_lat"], a["ref_lon"])),
                           strict=True))
        else:
            got = dict(zip(("x_n", "y_e"), (float(v) for v in F.px4_project(a["lat"], a["lon"], a["ref_lat"], a["ref_lon"])),
                           strict=True))
        for k, v in c["out"].items():
            kind = c["kinds"][k]
            if kind == "angle_deg" and k == "lon_deg":
                d = (got[k] - v + 180.0) % 360.0 - 180.0             # dateline wrap
                assert close(v + d, v, kind, tol), (c, got)
            else:
                assert close(got[k], v, kind, tol), (c, got)
        n += 1
    assert n >= 190


def test_time_against_datetime_oracle():
    fns = {"utc_to_gpst_ns": lambda a: {"gpst_ns": T.utc_to_gpst_ns(a["unix_utc_ns"])},
           "tai_minus_utc_s": lambda a: {"s": T.tai_minus_utc_s(a["unix_utc_ns"])},
           "gpst_to_utc_ns": lambda a: dict(zip(("unix_utc_ns", "leap_second"), T.gpst_to_utc_ns(a["gpst_ns"]), strict=True)),
           "gps_week_tow": lambda a: dict(zip(("week", "tow_ns"), T.gps_week_tow(a["gpst_ns"]), strict=True)),
           "gpst_to_tai_ptp_ns": lambda a: {"tai_ns": T.gpst_to_tai_ptp_ns(a["gpst_ns"])}}
    counts: dict[str, int] = {}
    for c in GOLDEN["time"]:
        assert fns[c["fn"]](c["args"]) == c["out"], c
        counts[c["fn"]] = counts.get(c["fn"], 0) + 1
    assert counts["gpst_to_utc_ns"] == 200 + 18 and counts["utc_to_gpst_ns"] == 81 + 200    # 18 insertions after 1980-01-06
    for c in GOLDEN["time"]:                                          # round trips of every sampled instant
        if c["fn"] == "gps_week_tow":
            assert T.gpst_ns_from_week_tow(c["out"]["week"], c["out"]["tow_ns"]) == c["args"]["gpst_ns"]
        if c["fn"] == "gpst_to_tai_ptp_ns":
            assert T.tai_ptp_to_gpst_ns(c["out"]["tai_ns"]) == c["args"]["gpst_ns"]


def test_rigid_local_frames():
    """AWR-03 §5.1 rule 6: metric-tangent backends (FleetSim, replay) use the exact rigid T_world_local = [[I, p_spawn]];
    PX4-class backends get a display-only rigid matrix whose error stays within the returned bound."""
    a = F.Anchor("synthetic", "WGS84", 31.8206, 117.2272, 30.0, 30.0)
    rng = np.random.default_rng(1)
    for _ in range(50):
        spawn = rng.uniform(-3000, 3000, 3)
        T, err = F.T_world_local_rigid(spawn, a, "metric_tangent")
        p = rng.uniform(-500, 500, 3)
        assert np.abs((T @ np.r_[p, 1.0])[:3] - (spawn + p)).max() <= 1e-9 and err(1e4) == 0.0
    o = F.Px4Origin(a.lat_deg + 0.001, a.lon_deg - 0.002, 35.0)
    T, err = F.T_world_local_rigid(o, a, "px4")
    for d in (100.0, 1000.0, 3000.0):
        for brg in np.linspace(0, 2 * math.pi, 8, endpoint=False):
            ned = np.array([d * math.cos(brg), d * math.sin(brg), -20.0])
            exact = F.world_from_px4_local(ned, o, a)
            approx = (T @ np.r_[ned[1], ned[0], -ned[2], 1.0])[:3]
            assert np.linalg.norm(exact - approx) <= err(d) + 1e-6, (d, brg)


def test_flat_helper_is_used():
    assert flat({"a": [1, 2], "b": 3}) == [1, 2, 3]
