"""M04 结果类型、参数与异常（M04 §6.2.3、§7.1、§7.8；MS1 冻结签名；D2 增补：Grid25 视图与派生缓存可用性原因）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, NamedTuple

import numpy as np

# @2（D2，AWR-04 ADR-113 第 5 条）：派生集加入 Grid25（4 m 规划栅格及其地板、阻塞掩码），旧键的条目随之失效
DERIVE_VERSION = "m04-derive@2"
NEG = np.float32(-1.0e9)

# 派生缓存不可用的原因（AWR-04 §4.4.7 第 2 条；M17 `507{why: cache_missing}`、M10 `123{detail: derived_cache_missing}`）
DERIVED_CACHE_MISSING = "derived_cache_missing"
WORLD_INVALID = "world_invalid"

# 错误码（17 reasons.json）
PARAM_OUT_OF_RANGE = 110
BAD_REQUEST = 300
WORLD_NOT_READY = 123
SERVICE_UNAVAILABLE = 213
INTERNAL_ERROR = 320


class GeoLoadError(RuntimeError):
    """装载期错误（不走 reasons.json）：code ∈ GEO_MISSING_FILE、GEO_GRID_INVALID、GEO_SHA_MISMATCH、GEO_CACHE_MISS。

    `reason` 是给沙盒与共享规划服务的归类：GEO_CACHE_MISS 为 `derived_cache_missing`（缓存缺失、键不一致或条目损坏，
    `allow_derive = false` 时不在线派生），其余为 `world_invalid`（世界包本身不可用）。
    """

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code

    @property
    def reason(self) -> str:
        return DERIVED_CACHE_MISSING if self.code == "GEO_CACHE_MISS" else WORLD_INVALID


class GeoError(ValueError):
    """查询期错误：code 为 17 reasons.json 的数值码（110 参数越界、300 请求不合法等），detail 为字段或原因。"""

    def __init__(self, code: int, detail: str = ""):
        super().__init__(f"{code} {detail}".strip())
        self.code = int(code)
        self.detail = detail


@dataclass(frozen=True)
class GeoParams:
    """全部字段进入派生缓存键（M04 §6.5）。"""

    obs_ground_hag_m: float = 2.0
    close_k: int = 3
    hm_dilate_cells: int = 2
    hm_safe_m: float = 10.0
    pyr_min_cells: int = 64
    zone_raster_cell_m: float = 8.0
    # Grid25（M10-FR-034 的 Hf = max(grid_2p5d(4), dtm + alt_min_agl_m)，禁飞区与界外置 +inf 并 8 邻域外扩）：缺省高度的
    # 栅格整体进入派生缓存，其他高度由缓存中的地板与阻塞掩码合成（D2，ADR-113 第 5 条）
    grid25_res_m: float = 4.0
    grid25_alt_min_agl_m: float = 20.0
    grid25_zone_dilate: int = 1

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Grid25View:
    """2.5D 规划栅格（D2，M10-FR-205）：`a` 为 Hf（float64，行主序、第 0 行在南；禁飞区与界外格为 +inf），`base` 为叠加
    区域阻塞前的 Hf（无阻塞格时为 None；A* 用它恢复起终点格）。

    `source = "cache"`：缺省高度，两个数组都是派生缓存文件的 memmap（文件页，多进程共享，不计匿名页）；只读，
    `copy_on_write = True` 时为写时复制映射（写入只在本映射内私有化被写的页）。`source = "process"`：非缺省高度，
    由缓存中的地板与阻塞掩码在进程内合成（匿名页 2 × H × W × 8 B），可写。
    """

    a: np.ndarray
    base: np.ndarray | None
    x0_m: float
    y0_m: float
    cell_m: float
    alt_min_agl_m: float
    source: Literal["cache", "process"]

    @property
    def h(self) -> int:
        return int(self.a.shape[0])

    @property
    def w(self) -> int:
        return int(self.a.shape[1])

    @property
    def nbytes(self) -> int:
        return int(self.a.nbytes + (0 if self.base is None else self.base.nbytes))


class GridView(NamedTuple):
    """只读栅格视图（M08 contact 核、M13 足迹）：float32 (H, W) 只读 memmap，行主序，第 0 行在南。"""

    a: np.ndarray
    x0_m: float
    y0_m: float
    cell_m: float


@dataclass(frozen=True, slots=True)
class Hit:
    hit: bool
    point_enu_m: np.ndarray | None
    dist_m: float
    hit_kind: Literal["top", "side"] | None
    normal_enu: np.ndarray | None
    surface: Literal["dsm", "dtm", "none"]
    cell_rc: tuple[int, int] | None
    ground_z_m: float
    agl_m: float
    origin_inside: bool

    def to_json(self) -> dict:
        def v(a):
            return None if a is None else [float(x) for x in a]

        def f(x):
            return None if x is None or not np.isfinite(x) else float(x)

        return {"hit": bool(self.hit), "point_enu_m": v(self.point_enu_m), "dist_m": f(self.dist_m), "surface": self.surface,
                "hit_kind": self.hit_kind, "normal_enu": v(self.normal_enu), "ground_z_m": f(self.ground_z_m),
                "agl_m": f(self.agl_m), "origin_inside": bool(self.origin_inside)}


@dataclass(frozen=True, slots=True)
class CoarseResult:
    verdict: np.ndarray              # int8[n_seg]：0 PROVEN_SAFE，1 MAYBE，2 VIOLATION
    reasons: list[tuple[str, int]]   # (detail, seg_index)
    zone_deferred: np.ndarray        # bool[n_seg]
    ok: bool
    all_proven: bool

    def to_json(self) -> dict:
        return {"ok": bool(self.ok), "violations": [{"seg": int(i), "reason": r} for r, i in self.reasons],
                "verdicts": [int(v) for v in self.verdict], "zone_deferred": [bool(v) for v in self.zone_deferred]}


@dataclass(frozen=True, slots=True)
class PathValidResult:
    ok: bool
    seg: int
    reason: str | None               # PATH_OBSTACLE、PATH_CROSSES_ZONE、OUT_OF_BORDER
    point_enu_m: np.ndarray | None
    samples: int
    content_version: str
    derive_sha8: str


@dataclass(frozen=True, slots=True)
class TransitProfile:
    ok: bool
    reason: str | None               # GEO_CEILING
    z_cruise_m: float
    top_m: float
    waypoints: np.ndarray            # (4,3)
    length_m: float
    climb_m: float


PROVEN_SAFE, MAYBE, VIOLATION = 0, 1, 2
