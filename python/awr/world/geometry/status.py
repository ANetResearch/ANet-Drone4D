"""派生缓存可用性探测（D2，AWR-04 §4.4.7 第 2 条；M17-FR-015、M17-AC-010；M10-FR-204）。

公开与沙盒部署以 `allow_derive = false` 运行：sandbox-pool 启动时逐世界调用 `cache_status`（或 `cache_status_all`），
缓存缺失、键不一致或条目损坏的世界在 `GET /api/sandbox/v1/worlds` 中标为 `available: false`
（`reason: derived_cache_missing`），创建返回 409 `507{why: cache_missing}`，绝不在线派生；`install.sh` 用
`python -m awr.world.geometry.cli check --all --deep` 做同一判定（退出码 4 表示有世界不可用）。

探测只读 `world.json`、对 `coordinate.json` 求 sha256 并校验条目的 manifest 与文件大小（`deep` 时再校验每个数组的
sha256），不打开 DSM、不派生、不导入 numba，单个世界毫秒级。键与 `open_world_query` 完全相同：
`sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION)` 取 8 位。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .cache import default_cache_root, derive_key, entry_bytes, entry_dir, read_manifest, verify_entry
from .types import DERIVE_VERSION, DERIVED_CACHE_MISSING, WORLD_INVALID, GeoLoadError, GeoParams

__all__ = ["CacheStatus", "WorldHeader", "cache_status", "cache_status_all", "read_world_header"]

REQUIRED_LAYERS = ("terrain.dtm", "terrain.dsm", "semantic.zones")


@dataclass(frozen=True)
class WorldHeader:
    world_id: str
    content_version: str
    coordinate_sha256: str
    layers: dict
    doc: dict


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def read_world_header(world_dir: Path) -> WorldHeader:
    """读取 `world.json`、检查几何图层就绪、校验坐标绑定（M04-FR-001 的前半段）；任一不符抛 GeoLoadError。"""
    world_dir = Path(world_dir)
    try:
        w = json.loads((world_dir / "world.json").read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise GeoLoadError("GEO_MISSING_FILE", str(world_dir / "world.json")) from e
    except (OSError, json.JSONDecodeError) as e:
        raise GeoLoadError("GEO_GRID_INVALID", f"world.json: {e}") from e
    layers = {L.get("id"): L for L in w.get("layers", [])}
    for lid in REQUIRED_LAYERS:
        L = layers.get(lid)
        if L is None or L.get("status") != "ready":
            raise GeoLoadError("GEO_MISSING_FILE", f"layer {lid} missing or not ready")
    coord_sha = (w.get("coordinate") or {}).get("sha256")
    cpath = world_dir / (w.get("coordinate") or {}).get("href", "coordinate.json")
    if not cpath.exists():
        raise GeoLoadError("GEO_MISSING_FILE", str(cpath))
    if _sha256(cpath) != coord_sha:
        raise GeoLoadError("GEO_SHA_MISMATCH", "coordinate.json sha256 != world.coordinate.sha256")
    return WorldHeader(world_id=str(w.get("id")), content_version=str(w.get("contentVersion")), coordinate_sha256=str(coord_sha),
                       layers=layers, doc=w)


@dataclass(frozen=True)
class CacheStatus:
    """一个世界的派生缓存可用性。

    `available` 为真当且仅当当前键的条目完整（`deep` 时含逐数组 sha256）。不可用时 `reason` 为
    `derived_cache_missing`（`detail`：`missing` 无该键条目、`stale` 只有其他键的条目（版本或参数不一致）、
    `corrupt` 条目文件缺失或大小、哈希不符）或 `world_invalid`（世界包本身不可用，`detail` 为 GeoLoadError 码）。
    `cache_bytes` 与 `grid25_bytes` 为 manifest 口径的字节数（按世界计一次的文件页工作集的保守上界，M17 `ws(w)` 的初值）。
    """

    world_id: str
    available: bool
    reason: str | None = None
    detail: str | None = None
    content_version: str | None = None
    coordinate_sha256: str | None = None
    derive_sha8: str | None = None
    derive_version: str = DERIVE_VERSION
    entry: str | None = None
    cache_bytes: int = 0
    grid25_bytes: int = 0
    checked: str = "shallow"
    other_entries: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def cache_status(world_dir: Path, cache_root: Path | None = None, params: GeoParams | None = None, *,
                 deep: bool = False) -> CacheStatus:
    world_dir = Path(world_dir)
    params = params or GeoParams()
    checked = "deep" if deep else "shallow"
    try:
        hdr = read_world_header(world_dir)
    except GeoLoadError as e:
        return CacheStatus(world_id=world_dir.name, available=False, reason=WORLD_INVALID, detail=e.code, checked=checked)
    key = derive_key(hdr.content_version, hdr.coordinate_sha256, params)
    root = Path(cache_root) if cache_root is not None else default_cache_root(world_dir)
    d = entry_dir(root, hdr.world_id, hdr.content_version, key)
    base = dict(world_id=hdr.world_id, content_version=hdr.content_version, coordinate_sha256=hdr.coordinate_sha256,
                derive_sha8=key, entry=str(d), checked=checked)
    wc = root / hdr.world_id
    others = sorted(p.name for p in wc.iterdir() if p.is_dir() and not p.name.startswith("tmp-") and p != d) \
        if wc.is_dir() else []
    if not d.is_dir():
        return CacheStatus(available=False, reason=DERIVED_CACHE_MISSING, detail="stale" if others else "missing",
                           other_entries=others, **base)
    m = read_manifest(d)
    ok = m is not None and m.get("derive_sha8", key) == key and verify_entry(d, deep=deep)
    if not ok:
        return CacheStatus(available=False, reason=DERIVED_CACHE_MISSING, detail="corrupt", other_entries=others, **base)
    b = entry_bytes(m)
    return CacheStatus(available=True, cache_bytes=b["total"], grid25_bytes=b["grid25"], other_entries=others, **base)


def cache_status_all(worlds_dir: Path, cache_root: Path | None = None, params: GeoParams | None = None, *,
                     ids: list[str] | None = None, deep: bool = False) -> list[CacheStatus]:
    """`worlds_dir` 下全部世界（或 `ids`）的探测结果，按世界 id 排序；隐藏目录与 `_shared` 等以 `_` 开头者不计。"""
    worlds_dir = Path(worlds_dir)
    if ids is None:
        ids = sorted(p.name for p in worlds_dir.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))
                     and (p / "world.json").exists())
    root = Path(cache_root) if cache_root is not None else None
    return [cache_status(worlds_dir / wid, root, params, deep=deep) for wid in ids]
