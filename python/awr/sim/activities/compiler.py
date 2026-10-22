"""Seeded, scene-constrained population compilation; reused by CLI and authoring API."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from awr.sim.targets.model import appearance_catalog

from .life import LifeWorld, stable_int


def populate(network, seed=20261022, people=12, vehicles=4, *, extras=None, vehicle_types=None):
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("Seed must be an unsigned 32-bit integer")
    if (
        any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in (people, vehicles))
        or not 0 <= people + vehicles <= 256
    ):
        raise ValueError("Invalid population count")
    extras = {} if extras is None else extras
    if (
        not isinstance(extras, dict)
        or set(extras) - {"dog", "uav", "machine", "object"}
        or any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= 30 for n in extras.values())
    ):
        raise ValueError("Invalid additional target counts")
    if not 1 <= people + vehicles + sum(extras.values()) <= 256:
        raise ValueError("Population limit exceeded")
    vehicle_types = ["sedan", "suv", "hatchback"] if vehicle_types is None else vehicle_types
    if (
        not isinstance(vehicle_types, list)
        or not vehicle_types
        or any(k not in ("sedan", "suv", "hatchback", "van", "pickup", "bus", "truck") for k in vehicle_types)
    ):
        raise ValueError("Invalid vehicle types")
    # UI selections are a set: click order must not change a seed's population.
    vehicle_types = [k for k in ("sedan", "suv", "hatchback", "van", "pickup", "bus", "truck") if k in vehicle_types]
    doc = {
        **network,
        "seed": seed,
        "actors": [],
        "generator": {
            "compiler_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "network_sha256": hashlib.sha256(
                json.dumps(network, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        },
    }
    catalog = appearance_catalog()
    starts = sorted(n for n in doc["nodes"] if n.startswith("p") and n.endswith("a"))
    used = []
    vehicle_components = {}
    jobs = [
        *(("person", i) for i in range(people)),
        *(("vehicle", i) for i in range(vehicles)),
        *((cls, i) for cls in ("dog", "machine", "uav", "object") for i in range(extras.get(cls, 0))),
    ]
    for cls, index in jobs:
        vehicle = cls == "vehicle"
        prefix = {"person": "resident", "vehicle": "driver"}.get(cls, cls)
        aid = f"{prefix}-{index + 1:02d}"
        key = (
            vehicle_types[index % len(vehicle_types)]
            if vehicle
            else (
                f"human-{index % 12 + 1}"
                if cls == "person"
                else {
                    "dog": "dog-small",
                    "machine": "forklift",
                    "uav": "quadcopter",
                    "object": ["bench", "litter-bin", "hydrant", "bollard", "bicycle-rack", "parked-bicycle"][
                        index % 6
                    ],
                }[cls]
            )
        )
        asset = catalog[key]
        length, width, height = asset["dimensions_m"]
        radius = math.hypot(length, width) / 2 + 0.025 if vehicle else 0.45
        body = {"length": length, "width": width} if vehicle and "vehicle_bodies" in doc.get("admission", {}) else None
        component = None
        if body:
            if key not in vehicle_components:
                graph, reverse = {}, {}
                for e in doc["edges"].values():
                    if e.get("vehicle_bodies", {}).get(key) == body:
                        graph.setdefault(e["from"], []).append(e["to"])
                        reverse.setdefault(e["to"], []).append(e["from"])

                def reachable(g, start):
                    seen, todo = set(), [start]
                    while todo:
                        n = todo.pop()
                        if n not in seen:
                            seen.add(n)
                            todo.extend(g.get(n, []))
                    return seen

                components, remaining = [], set(graph) | set(reverse)
                while remaining:
                    n = min(remaining)
                    c = reachable(graph, n) & reachable(reverse, n)
                    components.append(c)
                    remaining -= c
                vehicle_components[key] = max(
                    components,
                    key=lambda c: (
                        sum(f["node"] in c and "vehicle" in f["profiles"] for f in doc["facilities"].values()),
                        len(c),
                        sorted(c),
                    ),
                    default=set(),
                )
            component = vehicle_components[key]
        facilities = [
            k
            for k, f in doc["facilities"].items()
            if cls in f["profiles"] and (component is None or f["node"] in component)
        ]
        if cls != "object" and len(facilities) < 3:
            raise ValueError(f"Insufficient admitted destinations for {aid}: {len(facilities)}")
        ordered = sorted(facilities, key=lambda k: stable_int(seed, aid, k))
        chosen = ordered[:3]
        # Reproducible selection uses actual facility semantics; no invented coordinates.
        if cls == "person":
            home = next((f for f in ordered if doc["facilities"][f]["kind"] == "home"), chosen[0])
            office = next((f for f in ordered if doc["facilities"][f]["kind"] == "office"), chosen[1])
            shop = next((f for f in ordered if doc["facilities"][f]["kind"] == "shop"), chosen[2])
            chosen = [office, shop, home] if index % 2 == 0 else [shop, office, home]
        destinations = []
        for _k, fid in enumerate(chosen):
            kind = doc["facilities"][fid]["kind"]
            purpose = {"home": "居家休息", "office": "工作/办事", "shop": "购物", "parking": "停车办事"}.get(
                kind,
                {"pet_rest": "嗅闻与休息", "inspection": "悬停巡检", "handling": "装卸与设备检查"}.get(
                    kind, "设施访问"
                ),
            )
            dwell = {"home": 120, "office": 180, "shop": 55, "parking": 35}.get(
                kind, {"pet_rest": 20, "inspection": 10, "handling": 30}.get(kind, 60)
            ) + stable_int(seed, aid, fid) % (60 if cls in ("person", "vehicle") else 20)
            if vehicle and key in ("bus", "truck", "van"):
                purpose = "站点上下客" if key == "bus" else "停靠装卸/交付"
                dwell = (15 if key == "bus" else 35) + stable_int(seed, aid, fid) % 30
            destinations.append({"facility": fid, "purpose": purpose, "dwell_s": dwell})
        candidates = sorted(n for n in doc["nodes"] if (n.startswith("v") and n.endswith("out"))) if vehicle else starts
        candidates = doc.get("spawn_nodes", {}).get(cls, candidates)
        if cls not in ("person", "vehicle"):
            candidates = doc.get("spawn_nodes", {}).get(cls, [])
            radius = {"dog": 0.35, "uav": 0.65, "machine": 1.85, "object": math.hypot(length, width) / 2 + 0.025}[cls]
        candidates = sorted(
            (n for n in candidates if component is None or n in component),
            key=lambda n: stable_int(seed, aid, "start", n),
        )
        actor = {
            "id": aid,
            "class": cls,
            "profile": cls,
            "radius_m": radius,
            "height_m": max(height, 1.9) if cls == "person" else height,
            "speed_mps": 6.5 + index * 0.25
            if vehicle
            else {"person": 1.25 + (index % 5) * 0.08, "dog": 1.1, "uav": 3, "machine": 1.2, "object": 1}[cls],
            "asset_id": asset["id"],
            "itinerary": destinations,
            "role": {
                "vehicle": "镇区办事司机",
                "person": "居民",
                "dog": "社区犬",
                "uav": "巡检无人机",
                "machine": "后勤叉车",
                "object": "公共设施",
            }[cls],
            "plate_text": f"皖A {10000 + stable_int(seed, aid) % 89999}" if vehicle else "",
        }
        if body:
            actor["body_m"] = body
            actor["role"] = {"bus": "公交司机", "truck": "后勤货运司机", "van": "配送司机", "pickup": "作业司机"}.get(
                key, actor["role"]
            )
        if cls == "object":
            actor.update(stationary=True, stationary_reason="固定安装的公共设施", itinerary=[])
        for start in candidates:
            p = np.array(doc["nodes"][start]["pos"])
            if any(np.linalg.norm(p - q) < radius + r + 2 for q, r in used):
                continue
            actor["start"] = start
            try:
                # Constructor validates full cycle and route class/clearance before admission.
                single = LifeWorld({**doc, "actors": [actor]})
                if body:
                    first = single.path(start, doc["facilities"][destinations[0]["facility"]]["node"], actor)
                    if first:
                        actor["yaw_rad"] = single._pose(first[0], 0)[1]
            except ValueError:
                continue
            break
        else:
            raise ValueError(f"Scene lacks a connected route and destination space for {aid} ({asset['name_en']})")
        doc["actors"].append(actor)
        used.append((p, radius))
    return doc


def target_configs(doc, sha):
    catalog = appearance_catalog()
    targets = []
    for a in doc["actors"]:
        asset = catalog[a["asset_id"]]
        length, width, height = asset["dimensions_m"]
        metadata = {"role": a["role"]}
        if a["class"] == "vehicle":
            metadata.update(vehicle_type=asset["id"], plate_text=a["plate_text"], plate_region="中国安徽")
        targets.append(
            {
                "id": a["id"],
                "label": a["role"] + " " + a["id"][-2:],
                "class": a["class"],
                "pos": doc["nodes"][a["start"]]["pos"],
                "motion": {"mode": "timeline"},
                "reaction_policy": "exogenous",
                "sensitivity": [],
                "sound": None,
                "size_m": {"l": length, "w": width, "h": height},
                "metadata": metadata,
                "appearance": {"asset_id": asset["id"], "geometry_sha256": asset["geometry_sha256"]},
                "life": {"recipe_id": "life", "sha256": sha, "actor_id": a["id"]},
            }
        )
    return targets


def audit_recipe(doc, duration_s=1500):
    world = LifeWorld(doc)
    world.advance(duration_s)
    audit = world.audit()
    failures = []
    for aid, a in world.actors.items():
        if a.updates != world.tick:
            failures.append({"actor": aid, "why": "missing population updates"})
        if not a.spec.get("stationary") and a.max_no_progress_s > 120:
            failures.append(
                {"actor": aid, "why": "travel stalled for over 120 s", "seconds": round(a.max_no_progress_s, 1)}
            )
        if not a.spec.get("stationary") and a.visits < 2:
            failures.append({"actor": aid, "why": "fewer than two completed visits during admission"})
    if min(world.min_clearance_m, world.min_swept_clearance_m) < 0.05 - 1e-6:
        failures.append({"why": "body clearance violation"})
    audit["admitted"] = not failures
    audit["failures"] = failures
    return audit


def persist_recipe(doc, root=None):
    # Content address is also the idempotency key; readers never see a partial write.
    root = Path(root or os.environ.get("AWR_TARGET_LIFE_DIR", "var/target-life"))
    root.mkdir(parents=True, exist_ok=True)
    from .life import runtime_fingerprint

    doc = {**doc, "engine_sha256": runtime_fingerprint()}
    raw = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    sha = hashlib.sha256(raw).hexdigest()
    path = root / f"life-{sha}.json"
    if not path.exists():
        import tempfile

        with tempfile.NamedTemporaryFile(dir=root, delete=False) as tmp:
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
            name = tmp.name
        os.replace(name, path)
    return sha
