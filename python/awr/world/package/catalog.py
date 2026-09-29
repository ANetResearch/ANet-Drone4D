"""世界目录服务 `Catalog`（M03-FR-054、§6.12（3）；17 §4.3.2；供 M11 `worlds.py` 调用）。

只读 `world.json`、各根 `metadata.json` 与 `worlds/.status/`，按 mtime 缓存（刷新至多 1 Hz/城）；不做校验与重计算。
状态映射（12 §3.3.1 → 17 API）：READY → ready；BUILDING（持锁的 staging）→ building；ABSENT → missing；
ABSENT 且最近一次构建失败 → failed；INVALID → stale。已有 READY 包时重建失败，旧包保持 ready。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..ingest.manifest import load_data_config
from .publish import Publisher

WORLD_ID_RE = re.compile(r"^[a-z0-9-]{1,63}$")
REFRESH_MIN_S = 1.0


@dataclass
class WorldSummary:
    id: str
    name: str | None = None
    name_zh: str | None = None
    status: str = "missing"
    content_version: str | None = None
    scale_status: str | None = None
    anchor_kind: str | None = None
    georeferenced: bool | None = None
    points: int = 0
    bytes: int = 0
    octree_bytes: int = 0
    roots: int = 0
    node_count: int = 0
    levels_points: list[int] = field(default_factory=list)
    first_screen: dict = field(default_factory=lambda: {"points": 0, "bytes": 0})
    max_height_m: float | None = None
    qa: dict = field(default_factory=lambda: {"status": None, "messages": []})
    world_json_url: str = ""
    thumbnail_url: str | None = None
    default_scenario_id: str | None = None
    in_use: bool = False

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class WorldDetail(WorldSummary):
    coordinate_sha256: str | None = None
    bounds_m: list | None = None
    camera_home: dict | None = None
    layers: list[dict] = field(default_factory=list)


def _mtime(p: Path) -> int:
    try:
        return p.stat().st_mtime_ns
    except OSError:
        return 0


class Catalog:
    def __init__(self, worlds_dir: Path, *, builtin: list[str] | None = None, default_scenarios: dict[str, str] | None = None):
        self.worlds = Path(worlds_dir)
        if builtin is None:
            try:
                builtin = load_data_config().world_ids
            except Exception:
                builtin = []
        self.builtin = list(builtin)
        self.default_scenarios = default_scenarios or {}
        self._cache: dict[str, tuple[tuple, float, WorldDetail]] = {}
        self._list: tuple[float, list[WorldSummary]] | None = None

    def ids(self) -> list[str]:
        found = set(self.builtin)
        if self.worlds.is_dir():
            for p in self.worlds.iterdir():
                if p.is_dir() and WORLD_ID_RE.match(p.name) and (p / "world.json").exists():
                    found.add(p.name)
        return sorted(found, key=lambda w: (w not in self.builtin, self.builtin.index(w) if w in self.builtin else 0, w))

    def list(self) -> list[WorldSummary]:
        now = time.monotonic()
        if self._list is not None and now - self._list[0] < REFRESH_MIN_S:
            return self._list[1]
        out = []
        for wid in self.ids():
            d = self.get(wid)
            out.append(WorldSummary(**{k: getattr(d, k) for k in WorldSummary.__dataclass_fields__}))
        self._list = (now, out)
        return out

    def get(self, world_id: str) -> WorldDetail | None:
        if not WORLD_ID_RE.match(world_id):
            return None
        now = time.monotonic()
        hit = self._cache.get(world_id)
        if hit is not None and now - hit[1] < REFRESH_MIN_S:          # 1 Hz 刷新：1 s 内直接返回缓存
            return hit[2]
        wd = self.worlds / world_id
        st = self.worlds / ".status" / f"{world_id}.json"
        key = (_mtime(wd / "world.json"), _mtime(st), self._building(world_id))
        if hit is not None and hit[0] == key:
            self._cache[world_id] = (key, now, hit[2])
            return hit[2]
        if not (wd / "world.json").exists() and world_id not in self.builtin and not st.exists():
            return None
        d = self._load(world_id, wd, st, key[2])
        self._cache[world_id] = (key, now, d)
        return d

    def _building(self, world_id: str) -> bool:
        lock = self.worlds / ".locks" / f"{world_id}.lock"
        if not lock.exists():
            return False
        stg = self.worlds / ".staging"
        if not stg.is_dir() or not any(p.name.startswith(world_id + "-") for p in stg.iterdir()):
            return False
        return _PubProbe(self.worlds, world_id).is_locked()

    def _load(self, wid: str, wd: Path, st_path: Path, building: bool) -> WorldDetail:
        d = WorldDetail(id=wid, world_json_url=f"/worlds/{wid}/world.json", default_scenario_id=self.default_scenarios.get(wid))
        try:
            status = json.loads(st_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            status = None
        wj = wd / "world.json"
        w = None
        if wj.exists():
            try:
                w = json.loads(wj.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                w = None
        if w is None:
            d.status = "building" if building else ("failed" if status and status.get("status") == "failed" else "missing")
            return d
        cv = w.get("contentVersion")
        if building:
            d.status = "building"
        elif status and status.get("status") == "ready" and status.get("content_version") == cv:
            d.status = "ready"
        else:
            d.status = "stale"
        d.name, d.name_zh, d.content_version = w.get("name"), w.get("nameZh"), cv
        d.scale_status = w.get("scaleStatus")
        d.bytes = int(sum(f.get("bytes", 0) for f in w.get("files") or []))
        d.coordinate_sha256 = (w.get("coordinate") or {}).get("sha256")
        b = w.get("bounds") or {}
        d.bounds_m = [b.get("min"), b.get("max")] if b else None
        d.max_height_m = float(b["max"][2]) if b.get("max") else None
        d.camera_home = (w.get("camera") or {}).get("home")
        qa = w.get("qa") or {}
        d.qa = {"status": qa.get("status"), "messages": list(qa.get("messages") or [])[:20]}
        fs = (w.get("lod") or {}).get("firstScreen") or {}
        d.first_screen = {"points": int(fs.get("points", 0)), "bytes": int(fs.get("bytes", 0))}
        d.layers = [{"id": L.get("id"), "type": L.get("type"), "status": L.get("status"), "bytes": L.get("bytes")}
                    for L in w.get("layers", [])]
        try:
            c = json.loads((wd / "coordinate.json").read_text(encoding="utf-8"))
            d.anchor_kind = c["anchor"]["kind"]
            d.georeferenced = bool(c["anchor"]["georeferenced"])
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        pc = next((L for L in w.get("layers", []) if L.get("id") == "pointcloud.visual"), None)
        if pc:
            d.points = int(pc.get("points", 0))
            d.roots = len(pc.get("roots", []))
            per_root: list[list[int]] = []
            for r in pc.get("roots", []):
                try:
                    md = json.loads((wd / r["href"] / "metadata.json").read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, KeyError):
                    continue
                a = md.get("anet") or {}
                d.node_count += int(a.get("nodeCount", 0))
                ob = wd / r["href"] / "octree.bin"
                d.octree_bytes += ob.stat().st_size if ob.exists() else 0
                if a.get("levelsPoints"):
                    per_root.append([int(v) for v in a["levelsPoints"]])
            depth = max((len(p) for p in per_root), default=0)
            lp = [sum(p[min(L, len(p) - 1)] for p in per_root) for L in range(depth)]   # 森林逐层求和（浅根取末层值）
            d.levels_points = lp
        return d


class _PubProbe(Publisher):
    """只读锁探测（不创建目录）。"""

    def __init__(self, worlds_dir: Path, world_id: str):
        self.worlds = Path(worlds_dir)
        self.wid = world_id

