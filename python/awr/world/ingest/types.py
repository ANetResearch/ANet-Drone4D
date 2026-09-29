"""Ingest 数据类型与异常（M03 §6.3、§6.17、§7.6）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np

# ---------------------------------------------------------------- 异常 → 退出码（M03 §7.6、16 §18.1）


class WorldpkgError(Exception):
    exit_code = 3
    error_code = "IO_ERROR"


class RawDataError(WorldpkgError):
    """原始数据缺失或字节数、sha256 不符（退出码 4）。"""

    exit_code = 4

    def __init__(self, msg: str, *, missing: bool = False):
        super().__init__(msg)
        self.error_code = "RAW_MISSING" if missing else "RAW_CHECKSUM"


class GateFailed(WorldpkgError):
    """error 级 ingest 门禁失败（退出码 2）。"""

    exit_code = 2
    error_code = "INGEST_GATE_FAILED"


class ValidationFailed(WorldpkgError):
    """发布前校验失败（退出码 1）。"""

    exit_code = 1
    error_code = "VALIDATE_FAILED"


class ConfigError(WorldpkgError):
    """参数、配置或格式错误（退出码 3）。"""

    exit_code = 3


class LockBusy(WorldpkgError):
    """同一世界已有构建持锁（退出码 3，BUILD_IN_PROGRESS）。"""

    exit_code = 3
    error_code = "IO_ERROR"


# ---------------------------------------------------------------- 配置


@dataclass(frozen=True, slots=True)
class Landmark:
    name: str
    lat_deg: float
    lon_deg: float
    base_msl_m: float          # 地标地面海拔


@dataclass(frozen=True, slots=True)
class IngestConfig:
    """一城一份（UrbanScene3D 的六份在 urbanscene3d.py 中以常量给出）。"""

    world_id: str
    source_kind: Literal["dataset", "reconstruction", "lio-map", "survey", "simulation"]
    units_to_m: float | None
    up_axis: Literal["+x", "-x", "+y", "-y", "+z", "-z"]
    level: bool | Literal["auto"]
    yaw_deg: float
    true_north: Literal["exact", "verified", "assumed", "unknown"]
    scale_status: str
    landmark: Landmark | None
    fallback_anchor: tuple[float, float, float] | None
    evidence: tuple[str, ...] = ()
    north_evidence: tuple[str, ...] = ()
    dedupe: Literal["off", "exact"] = "off"
    semantic: Literal["rules", "csf"] = "rules"


@dataclass(slots=True)
class RawCloud:
    xyz: np.ndarray                    # float32/float64 (N,3)，源单位
    normal: np.ndarray | None          # (N,3) 或 None
    class_las: np.ndarray | None = None
    files: list[dict] = field(default_factory=list)   # [{name, bytes, points, sha256, header_bytes}]


class IngestAdapter(Protocol):
    kind: str

    def config(self) -> IngestConfig: ...

    def load(self) -> RawCloud: ...

    def provenance(self) -> dict: ...


# ---------------------------------------------------------------- 结果


@dataclass(slots=True)
class GateResult:
    id: str
    name: str
    value: Any
    limit: Any
    passed: bool
    severity: Literal["error", "warn", "info"]

    def to_report(self) -> dict:
        return {"id": self.id, "name": self.name, "value": self.value, "limit": self.limit, "pass": bool(self.passed),
                "severity": self.severity}

    def to_coordinate(self) -> dict:
        return {"name": f"{self.id} {self.name}", "value": self.value, "limit": self.limit, "pass": bool(self.passed)}


@dataclass(slots=True)
class CloudStats:
    n_raw: int
    n_nonfinite: int
    n_dedup: int
    nn_median_m: float
    z_p1: float
    z_p99: float
    hag_p1: float
    hag_p99: float
    class_histogram: dict[str, int]
    normals_flipped_frac: float
    zero_normals_frac: float
    ground_frac: float
    tilt_raw_deg: float
    leveled_deg: float
    tilt_after_deg: float
    ground_plane_mad_m: float | None
    units_heuristic: float
    up_axis_scores: dict[str, float]
    peak_index: int
    peak_hag_m: float
    peak_enu_m: tuple[float, float, float]
    relief_p1p99_m: tuple[float, float]


@dataclass(slots=True)
class TerrainGrids:
    dtm: np.ndarray                    # float32 (H,W)，world z（已减原点）
    dtm_origin_xy: tuple[float, float]
    dtm_cell_m: float
    ground_type: Literal["dtm", "flat", "synthetic"]


@dataclass(slots=True)
class NormalizedCloud:
    world_id: str
    xyz: np.ndarray                    # float64 [N,3]，World ENU，m
    normal: np.ndarray | None          # float32 [N,3]，单位向量，已翻正
    cls: np.ndarray                    # uint8 [N]
    hag: np.ndarray                    # float32 [N]
    T_world_source: np.ndarray         # float64 [4,4]
    origin: np.ndarray                 # float64 [3]
    anchor: dict                       # coordinate.json anchor 对象（camelCase）
    terrain: TerrainGrids
    stats: CloudStats
    gates: list[GateResult]
    config: IngestConfig
    provenance: dict
    source_files: list[dict]
    timings: dict[str, float] = field(default_factory=dict)
    peak_rss_mb: dict[str, float] = field(default_factory=dict)
    grid_cache: Any = None             # (CellIndex 2 m, 原始顶面 f32, 网格原点)：法线修正与 DSM 共用（M03 O-4）


@dataclass(slots=True)
class StageContext:
    """CLI 用的无事件、无取消上下文；job-worker 以 JobContext 实现同一接口（M03 §6.14）。"""

    log_json: bool = False
    world_id: str = ""
    quiet: bool = False

    def progress(self, frac: float) -> None:
        return None

    def check_cancel(self) -> None:
        return None

    def heartbeat(self) -> None:
        return None

    def log(self, level: str, msg: str, **fields: Any) -> None:
        import json
        import sys
        import time

        if self.quiet:
            return
        if self.log_json:
            rec = {"t_wall_ns": str(time.time_ns()), "world_id": self.world_id, "level": level, "msg": msg, **fields}
            print(json.dumps(rec, ensure_ascii=False), file=sys.stderr, flush=True)
        else:
            extra = " ".join(f"{k}={v}" for k, v in fields.items())
            print(f"[{self.world_id}] {msg}{(' ' + extra) if extra else ''}", file=sys.stderr, flush=True)


PathLike = str | Path
