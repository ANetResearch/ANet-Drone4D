"""time.py: leap table, GPST/UTC/TAI and week/tow (M02-AC-009; AWR-03 §5.2 rule 8; D1-AC-13)."""

from __future__ import annotations

import calendar
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from georef_golden import load_group

from awr.world.georef import time as T

NS = T.NS


def test_leap_table_28_entries():
    assert len(T.LEAP_TABLE) == 28
    assert T.LEAP_TABLE[0] == (calendar.timegm((1972, 1, 1, 0, 0, 0)), 10)
    assert T.LEAP_TABLE[-1] == (calendar.timegm((2017, 1, 1, 0, 0, 0)), 37)
    assert all(b[1] - a[1] == 1 for a, b in zip(T.LEAP_TABLE, T.LEAP_TABLE[1:], strict=False))


def test_gpst_minus_utc_2026_is_18s():
    assert T.gpst_minus_utc_s(calendar.timegm((2026, 9, 28, 12, 0, 0)) * NS) == 18


def test_insertion_boundaries_round_trip():
    for t_s, off in T.LEAP_TABLE[1:]:
        for dt in (-1, 0, 1):
            u = (t_s + dt) * NS
            back, leap = T.gpst_to_utc_ns(T.utc_to_gpst_ns(u))
            assert back == u and not leap
        g = T.utc_to_gpst_ns(t_s * NS) - NS // 2
        u, leap = T.gpst_to_utc_ns(g)
        assert leap and u == (t_s - 1) * NS + NS // 2, off


def test_week_tow_ptp():
    g = T.utc_to_gpst_ns(calendar.timegm((2026, 9, 28, 0, 0, 0)) * NS)
    w, tow = T.gps_week_tow(g)
    assert T.gpst_ns_from_week_tow(w, tow) == g and 0 <= tow < T.WEEK_NS
    assert T.tai_ptp_to_gpst_ns(T.gpst_to_tai_ptp_ns(g)) == g
    u = calendar.timegm((2026, 9, 28, 0, 0, 0)) * NS
    assert T.gpst_to_tai_ptp_ns(T.utc_to_gpst_ns(u)) - u == 37 * NS


def test_leap_table_status():
    assert T.leap_table_status(calendar.timegm((2026, 9, 28, 0, 0, 0)) * NS) == "valid"
    assert T.leap_table_status((T.LEAP_TABLE_VALID_UNTIL_UNIX_S + 1) * NS) == "expired"


def test_session_timebase_stub():
    tb = T.SessionTimebase(t0_gpst_ns=T.utc_to_gpst_ns(1_790_000_000 * NS),
                           streams={"lidar": T.StreamClock("lidar", T.SyncType.PTP, T.Timescale.TAI)})
    tai = T.gpst_to_tai_ptp_ns(tb.t0_gpst_ns) + 5 * NS
    assert tb.to_t_sim_ns("lidar", tai) == 5 * NS


def test_time_golden_exact():
    g = load_group("time")
    fns = {"tai_minus_utc_s": lambda a: {"s": T.tai_minus_utc_s(a["unix_utc_ns"])},
           "utc_to_gpst_ns": lambda a: {"gpst_ns": T.utc_to_gpst_ns(a["unix_utc_ns"])},
           "gpst_to_utc_ns": lambda a: dict(zip(("unix_utc_ns", "leap_second"), T.gpst_to_utc_ns(a["gpst_ns"]), strict=True)),
           "gpst_minus_utc_s": lambda a: {"s": T.gpst_minus_utc_s(a["unix_utc_ns"])},
           "gps_week_tow": lambda a: dict(zip(("week", "tow_ns"), T.gps_week_tow(a["gpst_ns"]), strict=True)),
           "gpst_to_tai_ptp_ns": lambda a: {"tai_ns": T.gpst_to_tai_ptp_ns(a["gpst_ns"])},
           "leap_table_status": lambda a: {"status": T.leap_table_status(a["now_unix_ns"])}}
    for c in g["cases"]:
        assert fns[c["fn"]](c["args"]) == c["out"], c
