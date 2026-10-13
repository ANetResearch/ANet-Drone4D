"""几何世界查询服务（M04）：WorldQuery（numpy DSM/DTM 实现）、Height_map、LOS、path_valid、zones 棱柱、探针服务。

所有者：M04（AWR-03 §4.3）。D1 进程不 import open3d。D2（AWR-04 §3.2 M04 行）：Grid25 并入派生缓存
（`DsmWorldQuery.grid25`）；`los_batch` 的 numba 核（`kernels.py`，惰性导入，与 numpy 参考实现逐位一致）；
`allow_derive = false` 的可用性探测（`status.py`）。
"""

from .grids import Grid
from .probe import BusQuery, GeoProbeServer
from .query import DsmWorldQuery, WorldQuery, env_allow_derive, open_world_query, warm_kernels
from .status import CacheStatus, cache_status, cache_status_all
from .types import (
    DERIVE_VERSION,
    DERIVED_CACHE_MISSING,
    WORLD_INVALID,
    CoarseResult,
    GeoError,
    GeoLoadError,
    GeoParams,
    Grid25View,
    GridView,
    Hit,
    PathValidResult,
    TransitProfile,
)
from .zones import Prism, ZoneIndex

__all__ = [
    "DERIVED_CACHE_MISSING",
    "DERIVE_VERSION",
    "WORLD_INVALID",
    "BusQuery",
    "CacheStatus",
    "CoarseResult",
    "DsmWorldQuery",
    "GeoError",
    "GeoLoadError",
    "GeoParams",
    "GeoProbeServer",
    "Grid",
    "Grid25View",
    "GridView",
    "Hit",
    "PathValidResult",
    "Prism",
    "TransitProfile",
    "WorldQuery",
    "ZoneIndex",
    "cache_status",
    "cache_status_all",
    "env_allow_derive",
    "open_world_query",
    "warm_kernels",
]
