"""Real-session time bases: UTC, TAI (PTP) and GPS time (M02 §6.4.3, §7.3; AWR-03 §5.2 rule 8).

The only place in the repository that converts between time scales. Integer nanoseconds throughout.
GPST - UTC = TAI - UTC - 19 s (18 s in 2026). PTP time is TAI counted from 1970-01-01.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from .leap_seconds import LEAP_TABLE, LEAP_TABLE_VALID_UNTIL_UNIX_S

NS = 1_000_000_000
GPS_EPOCH_UNIX_NS = 315_964_800 * NS          # 1980-01-06T00:00:00Z
TAI_MINUS_GPST_NS = 19 * NS
WEEK_NS = 604_800 * NS
_STARTS_NS = [t * NS for t, _ in LEAP_TABLE]


class Timescale(StrEnum):
    UTC = "utc"
    TAI = "tai"
    GPST = "gpst"
    HOST_MONO = "host_mono"


class SyncType(StrEnum):
    PTP = "ptp"
    GPS_PPS = "gps_pps"
    PX4_TIMESYNC = "px4_timesync"
    NONE = "none"
    SIM = "sim"


def tai_minus_utc_s(unix_utc_ns: int) -> int:
    """TAI - UTC at a UTC instant (10 s before 1972 by convention of this table)."""
    i = bisect.bisect_right(_STARTS_NS, int(unix_utc_ns)) - 1
    return LEAP_TABLE[max(i, 0)][1]


def gpst_minus_utc_s(unix_utc_ns: int) -> int:
    return tai_minus_utc_s(unix_utc_ns) - 19


def utc_to_gpst_ns(unix_utc_ns: int) -> int:
    return int(unix_utc_ns) - GPS_EPOCH_UNIX_NS + gpst_minus_utc_s(unix_utc_ns) * NS


def gpst_to_utc_ns(gpst_ns: int) -> tuple[int, bool]:
    """GPS time -> POSIX UTC ns. Inside an inserted leap second (23:59:60) returns (23:59:59.xxx, True)."""
    tai = int(gpst_ns) + GPS_EPOCH_UNIX_NS + TAI_MINUS_GPST_NS
    for i in range(len(LEAP_TABLE) - 1, -1, -1):
        t_i, off_i = LEAP_TABLE[i]
        if tai >= (t_i + off_i) * NS:
            return tai - off_i * NS, False
        if i > 0:
            off_prev = LEAP_TABLE[i - 1][1]
            if tai >= (t_i + off_prev) * NS:
                return tai - off_prev * NS - NS, True
    return tai - LEAP_TABLE[0][1] * NS, False


def tai_ptp_to_gpst_ns(tai_ns: int) -> int:
    return int(tai_ns) - GPS_EPOCH_UNIX_NS - TAI_MINUS_GPST_NS


def gpst_to_tai_ptp_ns(gpst_ns: int) -> int:
    return int(gpst_ns) + GPS_EPOCH_UNIX_NS + TAI_MINUS_GPST_NS


def gps_week_tow(gpst_ns: int) -> tuple[int, int]:
    """(week, time of week in ns)."""
    return divmod(int(gpst_ns), WEEK_NS)


def gpst_ns_from_week_tow(week: int, tow_ns: int) -> int:
    return int(week) * WEEK_NS + int(tow_ns)


def leap_table_status(now_unix_ns: int) -> Literal["valid", "expired"]:
    return "valid" if int(now_unix_ns) < LEAP_TABLE_VALID_UNTIL_UNIX_S * NS else "expired"


def to_gpst_ns(raw_ns: int, timescale: Timescale) -> int:
    if timescale == Timescale.GPST:
        return int(raw_ns)
    if timescale == Timescale.UTC:
        return utc_to_gpst_ns(raw_ns)
    if timescale == Timescale.TAI:
        return tai_ptp_to_gpst_ns(raw_ns)
    return int(raw_ns)  # host_mono: only the stream offset maps it (V0.5)


@dataclass(frozen=True)
class StreamClock:
    """Per-stream clock description (D1 stub; used from V0.5a)."""

    stream_id: str
    sync_type: SyncType
    timescale: Timescale
    offset_ns: int = 0
    drift_ppb: int = 0
    ref_raw_ns: int = 0


@dataclass(frozen=True)
class SessionTimebase:
    """t_sim_ns of a real session = GPST relative to t0 (AWR-03 §5.2 rule 8)."""

    t0_gpst_ns: int
    streams: Mapping[str, StreamClock] = field(default_factory=dict)

    def to_t_sim_ns(self, stream_id: str, raw_ns: int) -> int:
        c = self.streams[stream_id]
        g = to_gpst_ns(raw_ns, c.timescale)
        g += c.offset_ns + (int(raw_ns) - c.ref_raw_ns) * c.drift_ppb // NS
        return g - self.t0_gpst_ns
