"""派生缓存可用性探测（D2，AWR-04 §4.4.7 第 2 条；M17-FR-015、M17-AC-010；M10-FR-204）。

公开与沙盒部署以 `allow_derive = false` 运行：sandbox-pool 启动时逐世界调用 `cache_status`（或 `cache_status_all`），
缓存缺失、键不一致或条目损坏的世界在 `GET /api/sandbox/v1/worlds` 中标为 `available: false`
（`reason: derived_cache_missing`），创建返回 409 `507{why: cache_missing}`，绝不在线派生；`install.sh` 用
`python -m awr.world.geometry.cli check --all --deep` 做同一判定（退出码 4 表示有世界不可用）。

探测只读 `world.json`、对 `coordinate.json` 求 sha256 并校验条目的 manifest 与文件大小（`deep` 时再校验每个数组的
sha256），不打开 DSM、不派生、不导入 numba，单个世界毫秒级。键与 `open_world_query` 完全相同：
`sha256(contentVersion, coordinate.sha256, GeoParams, DERIVE_VERSION)` 取 8 位。

D3 增补（M22-FR-016、M22-AC-008；AWR-05 §5.1.2、ADR-130、D3-AC-02）：`metric_ok` 是米制可用的唯一判定，
`ok = (scaleStatus ≠ relative) ∧ 标定证据完整`，`ranked_ok = ok ∧ scaleStatus ≠ assumed`；M23 事件编译在 `ok = false`
时返回 600 `WORLD_UNCALIBRATED`，排名运行另要求 `ranked_ok`。同样只读 JSON（`world.json`、`coordinate.json`、
`qa/report.json`），不导入任何 D3 包，D2 世界与 D3 GLB 世界共用。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .cache import default_cache_root, derive_key, entry_bytes, entry_dir, read_manifest, verify_entry
from .types import DERIVE_VERSION, DERIVED_CACHE_MISSING, WORLD_INVALID, GeoLoadError, GeoParams

__all__ = ["CALIBRATION_GATES", "RANK_EXCLUDED", "SCALE_STATUSES", "CacheStatus", "MetricStatus", "WorldHeader", "cache_status",
           "cache_status_all", "metric_ok", "read_world_header"]

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


# ---------------------------------------------------------------------------------------------------------------- 米制判定
# World Package 1.0 `scaleStatus` 七值，由弱到强（contracts world/common.schema.json `$defs/scaleStatus`）
SCALE_STATUSES = ("relative", "assumed", "landmark", "gnss", "lidar", "rtk", "survey")
# 排名运行不接受的尺度等级（AWR-05 §5.1.2 末段、ADR-130 v1.2 修订）
RANK_EXCLUDED = ("relative", "assumed")
# 与标定直接相关的 M03 门禁：G-04 调平后地面残差、G-05 调平后倾角、G-08 真北已核实时的地标证据（M03 §6.4）
CALIBRATION_GATES = ("G-04", "G-05", "G-08")
# GLB 世界（`profile = d3-glb`）额外要求的证据：配准方法与调平角（M22-FR-015）
GLB_PROFILE = "d3-glb"


@dataclass(frozen=True)
class MetricStatus:
    """`metric_ok` 的结果（M22 §7 `MetricStatus{ok, scale_status, ranked_ok, reason}`）。

    `reason` 为第一条不满足项的机器码（`ok` 为真而 `ranked_ok` 为假时为 `scale_not_rankable`），`missing` 为全部不满足项，
    按检查次序排列，供 600 的 `detail` 与 `/lab/worlds` 展示：
    `world_json_missing`、`world_json_invalid`、`scale_status_missing`（缺失或不在七值枚举内）、`coordinate_missing`、
    `coordinate_sha_mismatch`、`scale_status_inconsistent`（world.json 与 coordinate.json 不一致）、`scale_relative`、
    `scale_evidence_missing`（`source.evidence[]` 为空或 `unitsToMeters` 非正有限数）、`true_north_unknown`、
    `registration_missing`（GLB 世界无配准或方法为 none）、`levelling_missing`（GLB 世界无有限的 `leveledDeg`）、
    `qa_missing`、`qa_mismatch`（报告的 world_id、content_version 与 world.json 不符）、`qa_failed`、
    `calibration_gate_missing`、`calibration_gate_failed`（`CALIBRATION_GATES` 缺项或未通过）。
    """

    ok: bool
    scale_status: str | None
    ranked_ok: bool
    reason: str | None = None
    missing: tuple[str, ...] = ()
    world_id: str | None = None
    content_version: str | None = None

    def to_json(self) -> dict:
        return {"ok": self.ok, "scale_status": self.scale_status, "ranked_ok": self.ranked_ok, "reason": self.reason,
                "missing": list(self.missing), "world_id": self.world_id, "content_version": self.content_version}


def _read_json(p: Path) -> dict | None:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _finite(v: object, *, positive: bool = False) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and (v > 0 or not positive)


def _load_evidence(world: Path | str | dict) -> tuple[dict | None, dict | None, dict | None, list[str]]:
    """把 `metric_ok` 的输入归一为 (world.json, coordinate.json, qa/report.json, 装载期缺项)。"""
    if isinstance(world, dict):
        return world.get("world"), world.get("coordinate"), world.get("qa_report"), []
    d = Path(world)
    if not (d / "world.json").is_file():
        return None, None, None, ["world_json_missing"]
    w = _read_json(d / "world.json")
    if w is None:
        return None, None, None, ["world_json_invalid"]
    miss: list[str] = []
    cref = w.get("coordinate") if isinstance(w.get("coordinate"), dict) else {}
    cpath = d / str(cref.get("href") or "coordinate.json")
    c = _read_json(cpath) if cpath.is_file() else None
    if c is not None and _sha256(cpath) != cref.get("sha256"):
        miss.append("coordinate_sha_mismatch")
    qref = w.get("qa") if isinstance(w.get("qa"), dict) else {}
    qpath = d / str(qref.get("href") or "qa/report.json")
    q = _read_json(qpath) if qpath.is_file() else None
    return w, c, q, miss


def metric_ok(world: Path | str | dict) -> MetricStatus:
    """米制可用判定（M22-FR-016）：`ok = (scaleStatus ≠ relative) ∧ 标定证据完整`，`ranked_ok = ok ∧ scaleStatus ≠ assumed`。

    `world` 为世界包目录，或已解析的 `{"world": world.json, "coordinate": coordinate.json, "qa_report": qa/report.json}`
    （内存形式不校验 `coordinate.sha256` 绑定）。标定证据完整指（ADR-130：尺度、真北与调平的结果与证据留存于
    `coordinate.json` 与 `qa/report.json`）：
      1. `coordinate.json` 存在、与 `world.coordinate.sha256` 绑定一致，`scaleStatus` 与 world.json 相同；
      2. 尺度：`source.evidence[]` 非空，`source.unitsToMeters` 为正有限数；
      3. 真北：`trueNorth.confidence ≠ unknown`；
      4. GLB 世界（`profile = d3-glb`）：`registration.method` 存在且不为 `none`，`source.leveledDeg` 为有限数；
      5. `qa/report.json` 存在，`world_id`、`content_version`（若写出）与 world.json 一致，`status ≠ fail`，
         `coordinate.qa.status ≠ fail`，且 `CALIBRATION_GATES` 均存在并通过。
    本函数只判定证据是否齐备与一致，不重算门限量；门限本身由 M03 门禁与 M22 qa 判定并写入报告。
    """
    w, c, q, miss = _load_evidence(world)
    if w is None:
        return MetricStatus(False, None, False, miss[0] if miss else "world_json_missing", tuple(miss or ["world_json_missing"]))
    ss = w.get("scaleStatus")
    if ss not in SCALE_STATUSES:
        miss.append("scale_status_missing")
        ss = None
    if c is None:
        miss.append("coordinate_missing")
        c = {}
    elif c.get("scaleStatus") != w.get("scaleStatus"):
        miss.append("scale_status_inconsistent")
    if ss == "relative":
        miss.append("scale_relative")
    if c:
        src = c.get("source") if isinstance(c.get("source"), dict) else {}
        if not [e for e in (src.get("evidence") or []) if isinstance(e, str) and e.strip()] \
                or not _finite(src.get("unitsToMeters"), positive=True):
            miss.append("scale_evidence_missing")
        tn = c.get("trueNorth") if isinstance(c.get("trueNorth"), dict) else {}
        if tn.get("confidence") in (None, "unknown"):
            miss.append("true_north_unknown")
        if w.get("profile") == GLB_PROFILE:
            reg = c.get("registration") if isinstance(c.get("registration"), dict) else {}
            if reg.get("method") in (None, "none"):
                miss.append("registration_missing")
            if not _finite(src.get("leveledDeg")):
                miss.append("levelling_missing")
    if q is None:
        miss.append("qa_missing")
    else:
        if any(q.get(k) is not None and q.get(k) != w.get(wk) for k, wk in (("world_id", "id"), ("content_version", "contentVersion"))):
            miss.append("qa_mismatch")
        if q.get("status") == "fail" or (c.get("qa") or {}).get("status") == "fail":
            miss.append("qa_failed")
        gates = {g.get("id"): g for g in (q.get("gates") or []) if isinstance(g, dict)}
        if any(gid not in gates for gid in CALIBRATION_GATES):
            miss.append("calibration_gate_missing")
        if any(gates[gid].get("pass") is not True for gid in CALIBRATION_GATES if gid in gates):
            miss.append("calibration_gate_failed")
    ok = not miss
    ranked = ok and ss not in RANK_EXCLUDED
    reason = miss[0] if miss else None if ranked else "scale_not_rankable"
    return MetricStatus(ok, ss, ranked, reason, tuple(miss), w.get("id"), w.get("contentVersion"))
