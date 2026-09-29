"""几何世界查询服务（M04）：WorldQuery（numpy DSM/DTM 实现）、Height_map、LOS、path_valid、zones 棱柱、探针服务。

所有者：M04（AWR-03 §4.3）。D1 进程不 import open3d，本包不使用 numba（M04-NFR-009）。
"""

from .grids import Grid
from .probe import BusQuery, GeoProbeServer
from .query import DsmWorldQuery, WorldQuery, open_world_query
from .types import (
    DERIVE_VERSION,
    CoarseResult,
    GeoError,
    GeoLoadError,
    GeoParams,
    GridView,
    Hit,
    PathValidResult,
    TransitProfile,
)
from .zones import Prism, ZoneIndex

__all__ = [
    "DERIVE_VERSION",
    "BusQuery",
    "CoarseResult",
    "DsmWorldQuery",
    "GeoError",
    "GeoLoadError",
    "GeoParams",
    "GeoProbeServer",
    "Grid",
    "GridView",
    "Hit",
    "PathValidResult",
    "Prism",
    "TransitProfile",
    "WorldQuery",
    "ZoneIndex",
    "open_world_query",
]
