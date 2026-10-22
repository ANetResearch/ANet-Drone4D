"""Build a verified synthcity pedestrian fixture from the exact generator, not from DSM rooftops.

This adapter is intentionally synthcity-only. Other worlds need their own confirmed network.
Generic compilation lives in awr.sim.activities; generated binaries/plan JSON stay outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np

from awr.sim.activities.plans import ActivityPlan, compile_plan, digest
from awr.sim.targets.model import appearance_catalog
from awr.world.ingest import synthetic as syn


def make_network(world_dir: Path) -> tuple[dict, dict]:
    world = json.loads((world_dir / "world.json").read_text())
    spec = world["generator"]["params"]["synthetic"]
    source_sha = hashlib.sha256(Path(syn.__file__).read_bytes()).hexdigest()[:16]
    if spec["sourceSha256"] != source_sha or spec["version"] != syn.GENERATOR_VERSION or spec["sizeM"] != 1200:
        raise ValueError("Synthetic generator/version/scale differs from this world; do not guess streets")
    if world["id"] != "synthcity":
        raise ValueError("This adapter only supports synthcity")
    coordinate_raw = (world_dir / world["coordinate"]["href"]).read_bytes()
    if hashlib.sha256(coordinate_raw).hexdigest() != world["coordinate"]["sha256"]:
        raise ValueError("Coordinate file hash mismatch")
    # Generator frame is ENU in this package. Binding the exact coordinate digest guards later relocation.
    planner = syn._Planner(syn.SynthSpec(seed=spec["seed"]))
    plan = planner.run()
    radius, height = 0.45, 1.9
    nodes, edges, rejected = {}, [], []
    xs, ys = [120.0, 240.0, 360.0, 480.0], [-300.0, -180.0, -60.0, 60.0]

    def key(i, j, sx, sy):
        return f"n{i}{j}{'e' if sx > 0 else 'w'}{'n' if sy > 0 else 's'}"

    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            for sx in (-1, 1):
                for sy in (-1, 1):
                    px = x + sx * (planner.road_w(x, "x") / 2 + 3.6)
                    py = y + sy * (planner.road_w(y, "y") / 2 + 3.6)
                    nodes[key(i, j, sx, sy)] = {
                        "pos": [px, py, planner.ground_z_scalar(px, py)],
                        "label": f"街角 {i + 1}-{j + 1} {'东北' if sx > 0 and sy > 0 else '西北' if sy > 0 else '东南' if sx > 0 else '西南'}",
                        "destination": True,
                        "purpose": "步行到街角，短暂停留",
                    }

    # Continuous swept disk samples at <= 0.25 m plus radial footprint probes.
    def clear(a, b):
        p, q = np.array(nodes[a]["pos"]), np.array(nodes[b]["pos"])
        centers = np.linspace(p, q, max(2, math.ceil(math.dist(p, q) / 0.25) + 1))
        angles = np.arange(16) * math.pi / 8
        xy = np.concatenate(
            [centers[:, :2], *(centers[:, :2] + radius * np.array([math.cos(v), math.sin(v)]) for v in angles)]
        )
        z = np.array([planner.ground_z_scalar(*v) for v in xy])
        codes = planner.lu[
            np.floor((xy[:, 1] + planner.H) / syn.RES).astype(int),
            np.floor((xy[:, 0] + planner.H) / syn.RES).astype(int),
        ]
        if not np.isin(codes, [syn.U_PAVED, syn.U_ROAD, syn.U_MARK]).all():
            return "unsupported land use"
        if np.ptp(z) > 0.2:
            return "ground step"
        xyz = np.c_[xy, z + 0.8]
        for building in plan.buildings:
            for part in [*building.parts, *building.equip]:
                if part.inside(xyz[:, 0], xyz[:, 1], xyz[:, 2]).any():
                    return "building footprint"
        for tree in plan.trees:
            distance = np.hypot(centers[:, 0] - tree.x, centers[:, 1] - tree.y)
            if (distance < radius + 0.3).any():
                return "tree trunk"
            if tree.trunk < height and (distance < tree.r + radius).any():
                return "low vegetation"
        return None

    def add(a, b, kind):
        reason = clear(a, b)
        if reason:
            rejected.append({"from": a, "to": b, "reason": reason})
            return
        p, q = nodes[a]["pos"], nodes[b]["pos"]
        fractions = np.linspace(0, 1, max(2, math.ceil(math.dist(p, q) / 0.25) + 1))
        ground = [
            [float(u), planner.ground_z_scalar(p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u)] for u in fractions
        ]
        edges.append(
            {
                "from": a,
                "to": b,
                "kind": kind,
                "profile": "person",
                "bidirectional": True,
                "verified": True,
                "ground_profile": ground,
            }
        )

    for i in range(len(xs)):
        for j in range(len(ys)):
            for sy in (-1, 1):
                add(key(i, j, -1, sy), key(i, j, 1, sy), "crosswalk")
            for sx in (-1, 1):
                add(key(i, j, sx, -1), key(i, j, sx, 1), "crosswalk")
            if i + 1 < len(xs):
                for sy in (-1, 1):
                    add(key(i, j, 1, sy), key(i + 1, j, -1, sy), "sidewalk")
            if j + 1 < len(ys):
                for sx in (-1, 1):
                    add(key(i, j, sx, 1), key(i, j + 1, sx, -1), "sidewalk")
    network = {
        "version": "activity-network-experiment-1",
        "status": "verified",
        "world_id": world["id"],
        "world_content_version": world["contentVersion"],
        "coordinate_sha256": world["coordinate"]["sha256"],
        "adapter": {"name": "synthcity-generator", "source_sha256": source_sha, "seed": spec["seed"]},
        "radius_m": radius,
        "height_m": height,
        "nodes": nodes,
        "edges": edges,
    }
    report = {
        "accepted_edges": len(edges),
        "rejected_edges": rejected,
        "ground_source": "exact synthetic generator",
        "geometry_check": "0.25 m center samples + 16 perimeter samples, building footprints, trunks, low crowns",
        "limitations": [
            "sampled swept footprint, not formal continuous collision proof",
            "no vehicle traffic/right-of-way",
            "no invented shops or entrances; destination semantics are street-corner rests",
        ],
    }
    return network, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("world", type=Path)
    ap.add_argument("--root", type=Path, default=Path("."))
    ap.add_argument("--seed", type=int, default=20261022)
    args = ap.parse_args()
    root = args.root
    network, report = make_network(args.world)
    out = root / "var/target-activities"
    out.mkdir(parents=True, exist_ok=True)
    (out / "network.json").write_text(json.dumps(network, ensure_ascii=False, indent=2))
    catalog = appearance_catalog()
    targets = []
    accepted = []
    stats = []
    # Planned interacting walks are checked before admission. Rejection sampling is deterministic and bounded.
    ts = np.arange(0, 3600.001, 0.2)
    destinations = sorted(network["nodes"])
    for i in range(6):
        tid = f"walker-{i + 1:02d}"
        for variant in range(100):
            start = destinations[(i * 11 + variant * 7) % len(destinations)]
            doc = compile_plan(network, tid, args.seed, start, speed_mps=1.25 + 0.09 * i, variant=variant)
            plan = ActivityPlan(doc)
            xy = plan.positions(ts)[:, :2]
            if any(np.min(np.linalg.norm(xy - other, axis=1)) < 1.25 for other in accepted):
                continue
            accepted.append(xy)
            break
        else:
            raise ValueError(f"Cannot find collision-free plan for {tid}; no fallback")
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        sha = hashlib.sha256(raw).hexdigest()
        (out / f"{tid}-{sha}.json").write_bytes(raw)
        asset = catalog["human-1" if i % 2 == 0 else "human-3"]
        L, W, H = asset["dimensions_m"]
        targets.append(
            {
                "id": tid,
                "label": f"步行居民 {i + 1:02d}",
                "class": "person",
                "pos": doc["segments"][0]["p"],
                "motion": {"mode": "timeline"},
                "reaction_policy": "exogenous",
                "sensitivity": [],
                "sound": None,
                "size_m": {"l": L, "w": W, "h": H},
                "appearance": {"asset_id": asset["id"], "geometry_sha256": asset["geometry_sha256"]},
                "activity": {"plan_id": tid, "sha256": sha},
            }
        )
        stats.append(
            {
                "id": tid,
                "variant": variant,
                "distance_m": round(doc["distance_m"]),
                "visits": len(doc["visits"]),
                "span_m": np.round(np.ptp(xy, axis=0), 1).tolist(),
                "plan_sha256": sha,
                "distance_at_1500_m": plan.sample(1500)[3]["walked_m"],
                "completed_visits_at_1500": plan.sample(1500)[3]["visits"],
                "plan_duration_s": round(doc["duration_s"], 1),
            }
        )
    template = json.loads((root / "scenarios/sandbox/synthcity/t7_target_appearance.json").read_text())
    template.update(
        template_id="t8_scene_activity",
        name_zh="T8 场景约束步行（实验）",
        summary_zh="6 名行人按 seed 生成跨街区目的地与停留；受人行道和过街通道约束。车辆交通尚未接入。",
        first_feedback_zh="选择行人：查看目的地、活动阶段与路线",
        seed=args.seed,
        targets=targets,
    )
    template["camera"]["center_enu_m"] = [300, -120, 0]
    template["camera"]["range_m"] = 650
    path = root / "scenarios/sandbox/synthcity/t8_scene_activity.json"
    path.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
    report.update(
        seed=args.seed,
        network_sha256=digest(network),
        actors=stats,
        collision_sample_interval_s=0.2,
        collision_checked_until_s=3600,
        min_pair_distance_m=float(
            min(np.min(np.linalg.norm(a - b, axis=1)) for i, a in enumerate(accepted) for b in accepted[i + 1 :])
        ),
    )
    (out / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
