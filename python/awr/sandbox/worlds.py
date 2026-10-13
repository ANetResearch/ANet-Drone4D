"""沙盒世界表（M17-FR-015、FR-010；AWR-04 §4.4.2、§4.4.7、ADR-109；17 §17.6.1 `GET /worlds`）。

每个世界一行：名称、规模档（S/L）、白名单、可用性（公开模式 `allow_derive = false` 时以 M04 `cache_status` 探测派生缓存，
缺失即 `available: false, reason: derived_cache_missing`；本机模式只要求世界包存在）、世界成本（`configs/sandbox/world_costs.json`，
缺行时按规模档的保守初值）、授权说明（UrbanScene3D 六城"经数据集权利方授权使用"与 ECCV 2022 引用，ADR-109）、锚点时区与边界。
池启动时构建一次，`refresh()` 可在运维命令或部署后重探测。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .admission import DEFAULT_COST
from .model import WorldCost

__all__ = ["DATASET_URBAN", "L_WORLDS", "NAMES_ZH", "WorldInfo", "WorldTable", "load_costs"]

log = logging.getLogger("awr.sandbox.worlds")

NAMES_ZH = {"synthcity": "合成城市", "shenzhen": "深圳", "suzhou": "苏州", "newyork": "纽约", "chicago": "芝加哥",
            "shanghai": "上海", "sanfrancisco": "旧金山"}
ORDER = ("synthcity", "shenzhen", "suzhou", "newyork", "chicago", "shanghai", "sanfrancisco")
L_WORLDS = ("chicago", "shanghai", "sanfrancisco")       # AWR-04 §1.2 术语表
DATASET_URBAN = {"name": "UrbanScene3D", "terms_zh": "经数据集权利方授权使用（ADR-109）",
                 "citation": "Lin et al., Capturing, Reconstructing, and Simulating: the UrbanScene3D Dataset, ECCV 2022"}
DATASET_SYNTH = {"name": "synthcity", "terms_zh": "项目自带的合成城市（示意锚点）", "citation": ""}
TZ_FALLBACK = {"synthcity": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "shenzhen": "Asia/Shanghai",
               "suzhou": "Asia/Shanghai", "sanfrancisco": "America/Los_Angeles", "newyork": "America/New_York",
               "chicago": "America/Chicago"}


def load_costs(path: Path) -> dict[str, WorldCost]:
    """`awr.sandbox.world_costs.v1`；文件缺失或损坏时返回空表（调用方按规模档初值）。"""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("world cost table unreadable; using tier defaults", extra={"kv": {"path": str(path), "err": str(e)}})
        return {}
    out: dict[str, WorldCost] = {}
    for w in d.get("worlds") or [] if isinstance(d, dict) else []:
        try:
            out[str(w["world"])] = WorldCost(str(w["world"]), str(w.get("world_class") or "S"), float(w["anon_mb"]),
                                             float(w["ws_mb"]), float(w.get("c0", 0.15)), bool(w.get("available", True)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


@dataclass
class WorldInfo:
    world_id: str
    name_zh: str
    tier: str
    allowed: bool
    available: bool
    reason: str | None
    cost: WorldCost
    dataset: dict
    tz: str | None = None
    bounds_m: dict | None = None
    templates: list[str] = field(default_factory=list)

    def item(self, admit_now: bool) -> dict[str, Any]:
        """`GET /worlds` 的一行（`session.schema.json#/$defs/world_item`）。"""
        d: dict[str, Any] = {"world_id": self.world_id, "name_zh": self.name_zh, "tier": self.tier,
                             "available": self.available and self.allowed,
                             "reason": None if self.available and self.allowed else ("not_allowed" if not self.allowed
                                                                                        else "derived_cache_missing"),
                             "admit_now": bool(admit_now and self.available and self.allowed),
                             "cost": {"anon_mb": self.cost.anon_mb, "ws_mb": self.cost.ws_mb}, "dataset": dict(self.dataset),
                             "templates": list(self.templates)}
        if self.tz:
            d["anchor"] = {"tz": self.tz}
        if self.bounds_m:
            d["bounds_m"] = dict(self.bounds_m)
        return d


def _world_meta(worlds_dir: Path, wid: str) -> tuple[str | None, dict | None, bool]:
    """(中文名, 平面边界, 世界包存在)。"""
    p = worlds_dir / wid / "world.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, False
    name = d.get("nameZh") if isinstance(d.get("nameZh"), str) else None
    b = d.get("bounds") if isinstance(d.get("bounds"), dict) else None
    bounds = None
    if b and isinstance(b.get("min"), list) and isinstance(b.get("max"), list) and len(b["min"]) >= 2:
        bounds = {"xmin": float(b["min"][0]), "xmax": float(b["max"][0]), "ymin": float(b["min"][1]), "ymax": float(b["max"][1])}
    return name, bounds, True


class WorldTable:
    def __init__(self, worlds_dir: Path, costs: dict[str, WorldCost], *, allow: list[str] | None = None,
                 allow_derive: bool = True, probe: Callable[[Path, str], tuple[bool, str | None]] | None = None,
                 ids: list[str] | None = None) -> None:
        self.worlds_dir = Path(worlds_dir)
        self.costs = dict(costs)
        self.allow = list(allow) if allow else None
        self.allow_derive = allow_derive
        self.probe = probe
        self.ids = list(ids) if ids else None
        self.items: dict[str, WorldInfo] = {}
        self.refresh()

    def _probe(self, wid: str, exists: bool) -> tuple[bool, str | None]:
        if self.probe is not None:
            return self.probe(self.worlds_dir, wid)
        if not exists:
            return False, "world_invalid"
        if self.allow_derive:
            return True, None
        try:
            from awr.world.geometry import cache_status

            st = cache_status(self.worlds_dir / wid)
            return bool(st.available), st.reason
        except Exception as e:
            log.warning("cache probe failed", extra={"kv": {"world": wid, "err": str(e)}})
            return False, "derived_cache_missing"

    def refresh(self) -> None:
        ids = list(self.ids) if self.ids else list(ORDER)
        if self.ids is None and self.worlds_dir.is_dir():
            for p in sorted(self.worlds_dir.iterdir()):
                if p.is_dir() and not p.name.startswith((".", "_")) and (p / "world.json").exists() and p.name not in ids:
                    ids.append(p.name)
        items: dict[str, WorldInfo] = {}
        try:
            from awr.environment.sky.calendar import WORLD_TZ
        except Exception:
            WORLD_TZ = TZ_FALLBACK
        for wid in ids:
            name, bounds, exists = _world_meta(self.worlds_dir, wid)
            tier = "L" if wid in L_WORLDS else "S"
            cost = self.costs.get(wid)
            if cost is None:
                base = DEFAULT_COST[tier]
                cost = WorldCost(wid, tier, base.anon_mb, base.ws_mb, base.c0, True)
            tier = cost.world_class if cost.world_class in ("S", "L") else tier
            ok, why = self._probe(wid, exists)
            items[wid] = WorldInfo(world_id=wid, name_zh=NAMES_ZH.get(wid, name or wid), tier=tier,
                                   allowed=self.allow is None or wid in self.allow, available=ok, reason=why, cost=cost,
                                   dataset=DATASET_SYNTH if wid == "synthcity" else DATASET_URBAN,
                                   tz=WORLD_TZ.get(wid) or TZ_FALLBACK.get(wid), bounds_m=bounds)
        self.items = items
        self.costs = {w: i.cost for w, i in items.items()} | {w: c for w, c in self.costs.items() if w not in items}

    def get(self, wid: str) -> WorldInfo | None:
        return self.items.get(wid)

    def usable(self) -> list[WorldInfo]:
        return [w for w in self.items.values() if w.allowed and w.available]
