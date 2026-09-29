"""M04 结果类型、参数与异常（M04 §6.2.3、§7.1、§7.8；MS1 冻结签名）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, NamedTuple

import numpy as np

DERIVE_VERSION = "m04-derive@1"
NEG = np.float32(-1.0e9)

# 错误码（17 reasons.json）
PARAM_OUT_OF_RANGE = 110
BAD_REQUEST = 300
WORLD_NOT_READY = 123
SERVICE_UNAVAILABLE = 213
INTERNAL_ERROR = 320


class GeoLoadError(RuntimeError):
    """装载期错误（不走 reasons.json）：code ∈ GEO_MISSING_FILE、GEO_GRID_INVALID、GEO_SHA_MISMATCH、GEO_CACHE_MISS。"""

    def __init__(self, code: str, msg: str = ""):
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code


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

    def to_json(self) -> dict:
        return asdict(self)


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
