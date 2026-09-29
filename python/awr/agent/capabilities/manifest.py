"""能力清单合成（`awr.agent.manifest.v1`；M14 §6.4.4；M14-FR-012、FR-013）。

合成规则：`skills` = 剧本成员 `capabilities` 与目录的交集，并上元能力；`physical.sensor` 取机型传感器 yaml（`detector.capability`
与能力 id 相同的那个：hfov 由内参 `2·atan(cx/fx)` 换算，`p0`、`r_fp_m` 取 `detector{}`），缺失时用目录默认值；
`limits` 取传感器 yaml、目录默认与机型 `wind_rating_mps` 三者中较严者。`manifest_sha256` 为规范 JSON（不含本字段）的 sha256。

机型数据只读 `vehicles/<model>/params.yaml` 与 `sensors/*.yaml`（数据文件，不 import `awr.sim`，10 §3）。
"""

from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Iterable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from ..runtime.evidence import canonical_json
from .catalog import COORD_CAPS, META_CAPS, Catalog, catalog

__all__ = ["VehicleData", "build_manifest", "coordinator_manifest", "manifest_sha256", "vehicle_data", "vehicles_root"]

SCHEMA = "awr.agent.manifest.v1"
_TAGS = {"thermal": ["sensor", "thermal", "verification"], "rgb": ["sensor", "rgb", "observation"],
         "relay": ["comm", "relay"], "agent": ["meta"], "task": ["meta"], "blackboard": ["coordination"]}


def vehicles_root() -> Path:
    env = os.environ.get("AWR_VEHICLES_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[4] / "vehicles"


def _val(x: Any) -> Any:
    return x.get("value") if isinstance(x, Mapping) and "value" in x else x


class VehicleData:
    """机型 profile 与传感器 yaml（只读）。"""

    def __init__(self, profile_id: str, params: Mapping[str, Any], sensors: Mapping[str, Mapping[str, Any]], model: str) -> None:
        self.profile_id = profile_id
        self.params = params
        self.sensors = sensors
        self.model = model

    def sensor_for(self, cap: str) -> Mapping[str, Any] | None:
        for s in self.sensors.values():
            if ((s.get("detector") or {}).get("capability")) == cap:
                return s
        return None

    @property
    def wind_rating_mps(self) -> float | None:
        v = _val(self.params.get("wind_rating_mps"))
        return float(v) if v is not None else None

    @property
    def has_battery(self) -> bool:
        return isinstance(self.params.get("battery"), Mapping)


@cache
def _profiles_index(root: str) -> dict[str, tuple[Path, str | None]]:
    idx: dict[str, tuple[Path, str | None]] = {}
    for p in sorted(Path(root).glob("*/params.yaml")):
        try:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        pid = d.get("id")
        if isinstance(pid, str):
            idx[pid] = (p, None)
        for vid in (d.get("variants") or {}):
            idx[str(vid)] = (p, str(vid))
    return idx


@cache
def vehicle_data(profile_id: str, root: str | None = None) -> VehicleData | None:
    r = root or str(vehicles_root())
    hit = _profiles_index(r).get(profile_id)
    if hit is None:
        return None
    path, variant = hit
    params = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if variant:
        params = {**params, **(params.get("variants", {}).get(variant) or {}), "id": variant}
    sensors: dict[str, Mapping[str, Any]] = {}
    for name, rel in (params.get("sensors") or {}).items():
        sp = path.parent / str(rel)
        try:
            sensors[str(name)] = yaml.safe_load(sp.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
    return VehicleData(profile_id, params, sensors, path.parent.name)


def _physical(entry: Mapping[str, Any], vd: VehicleData | None) -> dict[str, Any] | None:
    phys = entry.get("physical")
    if not isinstance(phys, Mapping):
        return None
    out = {k: (dict(v) if isinstance(v, Mapping) else (list(v) if isinstance(v, list) else v)) for k, v in phys.items()}
    sensor = dict(out.get("sensor") or {})
    limits = dict(out.get("limits") or {})
    s = vd.sensor_for(entry["id"]) if vd is not None else None
    if s is not None:
        fx, cx = s.get("fx"), s.get("cx")
        if isinstance(fx, (int, float)) and isinstance(cx, (int, float)) and fx > 0:
            sensor["hfov_deg"] = round(math.degrees(2.0 * math.atan(float(cx) / float(fx))), 3)
        if isinstance(s.get("width"), int) and isinstance(s.get("height"), int):
            sensor["res_px"] = [int(s["width"]), int(s["height"])]
        if isinstance(s.get("range_m"), (int, float)):
            sensor["range_m"] = float(s["range_m"])
        det = s.get("detector") or {}
        for k in ("p0", "r_fp_m"):
            if isinstance(det.get(k), (int, float)):
                sensor[k] = float(det[k])
        for k, v in (s.get("limits") or {}).items():
            if isinstance(v, (int, float)) and k in ("wind_mps", "rain_mmh", "visibility_m"):
                limits[k] = float(v) if k not in limits else (max if k == "visibility_m" else min)(float(limits[k]), float(v))
    if vd is not None and vd.wind_rating_mps is not None and "wind_mps" in limits:
        limits["wind_mps"] = min(float(limits["wind_mps"]), vd.wind_rating_mps)
    out["sensor"] = sensor
    out["limits"] = limits
    return out


def _skill(cat: Catalog, cap: str, vd: VehicleData | None) -> dict[str, Any]:
    e = cat.entries[cap]
    sk: dict[str, Any] = {"id": cap, "name_zh": e.get("name_zh", cap), "tags": list(e.get("tags") or _TAGS.get(cap.split(".")[0], [])),
                          "input_schema": e.get("input_schema") or {"type": "object"},
                          "output_metrics": list(e.get("output_metrics") or []),
                          "output_artifacts": list(e.get("output_artifacts") or []),
                          "physical": _physical(e, vd),
                          "trust": cat.trust(cap), "requires_lease": bool(e.get("requires_lease", False)),
                          "long_running": bool(e.get("long_running", False)),
                          "timeout_s": float(e.get("timeout_s", 600.0 if e.get("long_running") else 60.0)),
                          "max_concurrent": int(e.get("max_concurrent", 1)), "price": 0}
    return sk


def manifest_sha256(m: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json({k: v for k, v in m.items() if k != "manifest_sha256"})).hexdigest()


def build_manifest(*, aid: str, vehicle_id: str, world_id: str, profile_id: str, capabilities: Iterable[str],
                   network: str = "mock", cat: Catalog | None = None, vd: VehicleData | None = None) -> dict[str, Any]:
    cat = cat or catalog()
    vd = vd if vd is not None else vehicle_data(profile_id)
    caps = [c for c in dict.fromkeys(capabilities) if c in cat.entries and c not in META_CAPS]
    skills = [_skill(cat, c, vd) for c in sorted(caps)] + [_skill(cat, c, vd) for c in META_CAPS]
    m: dict[str, Any] = {"schema": SCHEMA,
                         "agent": {"aid": aid, "name": vehicle_id, "kind": "uav", "model": vd.model if vd else "",
                                   "profile_id": profile_id, "vehicle_id": vehicle_id, "world_id": world_id, "network": network},
                         "skills": skills, "state_capability": "agent.state"}
    m["manifest_sha256"] = manifest_sha256(m)
    return m


def coordinator_manifest(*, aid: str, world_id: str, network: str = "mock", cat: Catalog | None = None) -> dict[str, Any]:
    cat = cat or catalog()
    skills = [_skill(cat, c, None) for c in (*COORD_CAPS, "agent.describe")]
    m: dict[str, Any] = {"schema": SCHEMA,
                         "agent": {"aid": aid, "name": "gcs", "kind": "uav", "model": "", "profile_id": "",
                                   "vehicle_id": "gcs", "world_id": world_id, "network": network},
                         "skills": skills, "state_capability": "agent.state"}
    m["manifest_sha256"] = manifest_sha256(m)
    return m
