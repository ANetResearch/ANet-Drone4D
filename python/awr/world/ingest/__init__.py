"""Ingest 规范化（M03 §6.4）；urbanscene3d.py 是唯一的 UrbanScene3D 适配器。

所有者：M03（AWR-03 §4.3）。

`ingest` 按需导入（PEP 562）：`awr.world.package.catalog` 经 `ingest.manifest` 导入本包，急切导入 pipeline 会把 terrain 与
scipy.ndimage 带进 api 进程（约 0.65 s），拖慢 api 重启后的重连（D1-AC-11a，INT-1）。
"""

from typing import TYPE_CHECKING, Any

from .types import (
    ConfigError,
    GateFailed,
    IngestAdapter,
    IngestConfig,
    Landmark,
    LockBusy,
    NormalizedCloud,
    RawCloud,
    RawDataError,
    StageContext,
    ValidationFailed,
    WorldpkgError,
)

if TYPE_CHECKING:
    from .pipeline import ingest


def __getattr__(name: str) -> Any:
    if name == "ingest":
        from .pipeline import ingest

        return ingest
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConfigError",
    "GateFailed",
    "IngestAdapter",
    "IngestConfig",
    "Landmark",
    "LockBusy",
    "NormalizedCloud",
    "RawCloud",
    "RawDataError",
    "StageContext",
    "ValidationFailed",
    "WorldpkgError",
    "ingest",
]
