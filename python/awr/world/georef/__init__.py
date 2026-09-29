"""Frame conversions and time bases (M02, D1-core): the only implementation in the repository (AWR-03 §5.1 rule 8).

Re-exports the public symbols of awr.world.georef.frames, awr.world.georef.time and awr.world.georef.sim3
(M02 §9.1), plus the Mock GNSS generator. Only numpy, the standard library and the generated awr.contracts RNG table
are imported (M02-NFR-006). The V0.5 subpackages (lidar, ...) are imported explicitly by their users.
"""

from . import sim3, time
from .frames import *  # noqa: F403 - the package facade re-exports frames.__all__
from .frames import __all__ as _frames_all
from .mock_gnss import SIGMA_ENU_M, FixType, GnssObs, MockGnss
from .sim3 import TrajSim3Config, TrajSim3Report, collinearity_ratio, rot_err_deg, traj_sim3, umeyama
from .time import (
    GPS_EPOCH_UNIX_NS,
    SessionTimebase,
    StreamClock,
    SyncType,
    Timescale,
    gps_week_tow,
    gpst_minus_utc_s,
    gpst_ns_from_week_tow,
    gpst_to_tai_ptp_ns,
    gpst_to_utc_ns,
    leap_table_status,
    tai_minus_utc_s,
    tai_ptp_to_gpst_ns,
    to_gpst_ns,
    utc_to_gpst_ns,
)

__all__ = [
    *_frames_all,
    "GPS_EPOCH_UNIX_NS",
    "SIGMA_ENU_M",
    "FixType",
    "GnssObs",
    "MockGnss",
    "SessionTimebase",
    "StreamClock",
    "SyncType",
    "Timescale",
    "TrajSim3Config",
    "TrajSim3Report",
    "collinearity_ratio",
    "gps_week_tow",
    "gpst_minus_utc_s",
    "gpst_ns_from_week_tow",
    "gpst_to_tai_ptp_ns",
    "gpst_to_utc_ns",
    "leap_table_status",
    "rot_err_deg",
    "sim3",
    "tai_minus_utc_s",
    "tai_ptp_to_gpst_ns",
    "time",
    "to_gpst_ns",
    "traj_sim3",
    "umeyama",
    "utc_to_gpst_ns",
]
