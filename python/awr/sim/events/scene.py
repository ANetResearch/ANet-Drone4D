"""场景目标引擎的只读基础审计；实验性诊断，不构成活动准入或新的世界格式。"""

from __future__ import annotations

import hashlib
import itertools
import json
from collections import Counter
from pathlib import Path

import numpy as np

from awr.sim.events.anchors import overlay_issues
from awr.sim.events.nav import CELL_M, NavGrid

_NAV_LAYERS = {
    "nav.person": "detour-navmesh@1",
    "nav.vehicle": "detour-navmesh@1",
    "nav.graph": "geojson/awr-navgraph@1",
}


def _local_file(root: Path, href: str) -> Path:
    if not isinstance(href, str) or not href or Path(href).is_absolute():
        raise ValueError("expected a world-relative file path")
    p = (root / href).resolve()
    if not p.is_relative_to(root.resolve()) or not p.is_file():
        raise ValueError(f"missing or out-of-package file: {href}")
    return p


def _json_object(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()

    def reject_constant(value):
        raise ValueError(f"non-finite JSON number: {value}")

    doc = json.loads(raw, parse_constant=reject_constant)
    if not isinstance(doc, dict):
        raise ValueError("expected a JSON object")
    return doc, hashlib.sha256(raw).hexdigest()


def audit_scene(world_dir: Path) -> dict:
    """核对已有世界/锚点契约，分别暴露缺失输入与未实现后端；不读大体积网格、不写世界。"""
    root = Path(world_dir)
    report = {"audit_version": "scene-foundations-1", "status": "blocked", "world_id": None,
              "files": {}, "layers": {}, "profiles": {}, "issues": []}

    def issue(code, message):
        report["issues"].append({"code": code, "message": message})

    try:
        world, digest = _json_object(_local_file(root, "world.json"))
        report["files"]["world.json"] = digest
        if not isinstance(world.get("id"), str) or not world["id"]:
            raise ValueError("world.id is missing")
        report["world_id"] = world["id"]
        layers = world.get("layers", [])
        if not isinstance(layers, list) or any(not isinstance(layer, dict) for layer in layers):
            raise ValueError("world.layers must be an array of objects")
    except (OSError, ValueError) as e:
        issue("WORLD_INVALID", str(e))
        return report

    coordinate_sha = None
    try:
        ref = world.get("coordinate")
        if not isinstance(ref, dict):
            raise ValueError("world.coordinate is missing")
        _, digest = _json_object(_local_file(root, ref.get("href")))
        if digest != ref.get("sha256"):
            raise ValueError("coordinate content differs from world.coordinate.sha256")
        coordinate_sha = digest
        report["files"][ref["href"]] = digest
    except (OSError, ValueError) as e:
        issue("COORDINATE_INVALID", str(e))
    if world.get("scaleStatus") not in ("landmark", "gnss", "lidar", "rtk", "survey"):
        issue("METRIC_REVIEW_REQUIRED", "Scale/calibration needs review; this audit does not certify metric accuracy.")

    for layer_id, fmt in _NAV_LAYERS.items():
        candidates = [layer for layer in layers if layer.get("id") == layer_id]
        entry = {"status": "missing"}
        report["layers"][layer_id] = entry
        try:
            if len(candidates) != 1:
                raise ValueError(f"expected one declared {layer_id} layer; got {len(candidates)}")
            layer = candidates[0]
            if layer.get("format") != fmt or layer.get("status") != "ready":
                raise ValueError(f"{layer_id} needs format {fmt} and ready status")
            _, digest = _json_object(_local_file(root, layer.get("href")))
            entry.update(status="unverified", href=layer["href"], sha256=digest)
            report["files"][layer["href"]] = digest
        except (OSError, ValueError) as e:
            entry["detail"] = str(e)
            issue("NAV_INPUT_UNAVAILABLE", f"{layer_id}: {e}")

    anchors = []
    try:
        candidates = [layer for layer in layers if layer.get("format") == "awr-anchors@1"]
        if len(candidates) > 1:
            raise ValueError("multiple anchor layers are ambiguous")
        if candidates and candidates[0].get("status") != "ready":
            raise ValueError("anchor layer is not ready")
        href = candidates[0].get("href") if candidates else "semantic/anchors.json"
        doc, digest = _json_object(_local_file(root, href))
        errors = sorted(overlay_issues(doc))
        if errors:
            raise ValueError("; ".join(errors))
        if "extends" in doc:
            raise ValueError("world anchors must be a world layer, not an event overlay")
        if doc["world_id"] != world["id"] or coordinate_sha is None or doc["coordinate_sha256"] != coordinate_sha:
            raise ValueError("anchor world/coordinate binding mismatch")
        ids = [a["id"] for a in doc["anchors"]]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate anchor ids")
        anchors = doc["anchors"]
        report["files"][href] = digest
    except (OSError, ValueError) as e:
        issue("ANCHORS_INVALID", str(e))

    for profile in ("person", "vehicle"):
        eligible = [a for a in anchors if a["profile"] == profile and a["status"] == "confirmed"
                    and a["quality"]["in_coverage"]]
        zones = sorted(a["id"] for a in eligible if a["kind"] == "zone")
        report["profiles"][profile] = {
            "confirmed_zone_candidates": zones,
            "confirmed_anchor_count": len(eligible),
            "categories": dict(sorted(Counter(a["category"] for a in eligible).items())),
            "status": "blocked",
        }
        if not zones:
            issue("ACTIVITY_REGION_MISSING", f"{profile}: no confirmed, in-coverage activity-zone candidates")

    # 即使输入文件存在，当前编译器也没有使用它们。不可凭文件存在宣布能力可用。
    issue("SCENE_ADAPTER_NOT_IMPLEMENTED",
          "Compiler uses highest-hit NavGrid; scene-ground binding, semantic permissions, entrance reachability "
          "and directed road topology are not integrated. Existing files remain unverified.")
    report["next_steps"] = [
        "Confirm profile-specific ground regions, surface levels, entrances and facility roles.",
        "Validate vehicle support/width, direction, turns, parking and network connectivity against scene geometry.",
        "Integrate the scene adapter before admitting autonomous activities; never invent missing ground.",
    ]
    return report


def audit_nav(nav: NavGrid, points: dict[str, tuple[float, float]] | None = None) -> dict:
    """4 连通统计与指定端点可达性；无随机采样、无自动吸附，只检查现有格网的拓扑连接。"""
    from scipy import ndimage

    if nav.walk.shape != (nav.ny, nav.nx) or nav.z.shape != nav.walk.shape:
        raise ValueError("NavGrid arrays do not match its dimensions")
    if nav.walk.dtype != np.bool_ or np.any(nav.walk & ~np.isfinite(nav.z)):
        raise ValueError("walkable cells must be boolean and have finite support heights")
    labels, count = ndimage.label(nav.walk, structure=[[0, 1, 0], [1, 1, 1], [0, 1, 0]])
    sizes = np.bincount(labels.ravel(), minlength=count + 1)[1:]
    points = points or {}
    endpoints = {}
    for key in sorted(points):
        xy = np.asarray(points[key], dtype=np.float64)
        if xy.shape != (2,) or not np.all(np.isfinite(xy)):
            raise ValueError(f"{key}: expected finite world XY in metres")
        c = nav.cell_of(*xy)
        component = int(labels.ravel()[c]) if c >= 0 else 0
        endpoints[key] = {"component": component, "on_walkable_surface": component != 0}
    routes = []
    for a, b in itertools.combinations(sorted(points), 2):
        component = endpoints[a]["component"]
        connected = bool(component and component == endpoints[b]["component"])
        routes.append({"from": a, "to": b, "connected": connected})
    return {"profile": nav.profile, "nav_sha256": nav.sha256, "component_count": int(count),
            "component_area_m2": [int(n) * CELL_M**2 for n in sizes],
            "walkable_area_m2": int(sizes.sum()) * CELL_M**2,
            "largest_component_fraction": float(sizes.max() / sizes.sum()) if count else 0.0,
            "points": endpoints, "routes": routes,
            "limitations": "Grid connectivity only; does not certify semantics, road direction or 3D body clearance."}
