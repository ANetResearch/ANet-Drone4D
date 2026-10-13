"""只重写已构建世界的 `world.json` 数据集说明（ADR-109 第 3 条；R-D2-28；d2-WP-16-to-M03 第 1、2 条；D2-AC-24）。

六城的数据集说明由适配器 `awr.world.ingest.urbanscene3d.DATASET` 给出；按 ADR-109 改写后，已构建的世界仍带 D1 文字
（"non-commercial research only"），世界面板随之显示"科研用途"。重建六城需要原始数据与数十分钟，而 `world.json` 是非内容文件
（`manifest.NON_CONTENT`，不进入 `files[]` 与 `contentVersion`），派生缓存键 `sha256(contentVersion, coordinate.sha256, GeoParams,
DERIVE_VERSION)` 也与它无关，因此这里只把 `dataset` 段换成适配器的当前文字：

- 保留构建时记录的 `sourceFiles`（原始文件的字节数与 sha256，`missing_reason` 的条件 ⑤ 依据它）与 world.json 的其余字段及键序；
- 只处理由 UrbanScene3D 适配器构建的六城（`dataset.name == "UrbanScene3D"`）；合成城市与其他世界跳过；
- 持有与构建相同的世界锁（`Publisher.lock`），先写同目录临时文件再原子改名；写后按 schema 与语义规则校验，失败时还原。

用法：`python -m awr.world.package.cli dataset [--check] [ids...]`（`make worlds-dataset`）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .jsonio import dumps, read_json


@dataclass(frozen=True)
class DatasetResult:
    world_id: str
    action: str          # updated | current | stale（--check）| skipped | missing | busy（构建锁被占用）| failed（校验失败，已还原）
    detail: str = ""


def expected_dataset(world_id: str, current: dict | None) -> dict | None:
    """该世界应有的 `dataset` 段；不归 UrbanScene3D 适配器管理时返回 None。"""
    from ..ingest.urbanscene3d import CITIES, DATASET

    cur = current or {}
    if world_id not in CITIES or cur.get("name") != DATASET["name"]:
        return None
    d = dict(DATASET)
    d["sourceFiles"] = list(cur.get("sourceFiles") or [])
    return d


def _write_atomic(p: Path, text: str) -> None:
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def refresh_world(worlds_dir: Path, world_id: str, *, check: bool = False) -> DatasetResult:
    from ..ingest.types import LockBusy
    from .publish import Publisher

    wd = Path(worlds_dir) / world_id
    wj = wd / "world.json"
    if not wj.is_file():
        return DatasetResult(world_id, "missing", "world.json 不存在")
    w = read_json(wj)
    want = expected_dataset(world_id, w.get("dataset"))
    if want is None:
        return DatasetResult(world_id, "skipped", "不是 UrbanScene3D 适配器构建的世界")
    if w.get("dataset") == want:
        return DatasetResult(world_id, "current")
    if check:
        return DatasetResult(world_id, "stale", "dataset 段与适配器的当前文字不同")
    try:
        with Publisher(worlds_dir, world_id).lock():
            return _rewrite_locked(wd, world_id)
    except LockBusy as e:
        return DatasetResult(world_id, "busy", str(e))


def _rewrite_locked(wd: Path, world_id: str) -> DatasetResult:
    from .validate import validate_world

    wj = wd / "world.json"
    old_text = wj.read_text(encoding="utf-8")   # 持锁后重读：期间可能有构建发布了新版本
    w = read_json(wj)
    want = expected_dataset(world_id, w.get("dataset"))
    if want is None:
        return DatasetResult(world_id, "skipped", "不是 UrbanScene3D 适配器构建的世界")
    if w.get("dataset") == want:
        return DatasetResult(world_id, "current")
    w["dataset"] = want
    _write_atomic(wj, dumps(w))
    rep = validate_world(wd, deep=False)
    if not rep.ok:
        _write_atomic(wj, old_text)
        e = rep.errors[0]
        return DatasetResult(world_id, "failed", f"{e['rule']} {e['where']}: {e['message']}（已还原）")
    return DatasetResult(world_id, "updated", f"contentVersion {w.get('contentVersion')} 不变")


def refresh_all(worlds_dir: Path, ids: list[str] | None = None, *, check: bool = False) -> list[DatasetResult]:
    from ..ingest.urbanscene3d import CITIES

    return [refresh_world(worlds_dir, wid, check=check) for wid in (ids or sorted(CITIES))]
