"""Author a scene-bound life network from synthcity's actual geometry.

Synthetic building uses are explicit scenario authoring, NOT inferred ground truth.
Entrances are certified exterior portals; interiors are capacity/dwell abstractions.
Other worlds must supply their own admitted graph/portals through the same contract.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
from build_activities import make_network
from vehicle_envelopes import junction_curve

from awr.sim.activities.compiler import audit_recipe, persist_recipe, populate, target_configs
from awr.sim.activities.life import VERSION
from awr.world.ingest import synthetic as syn


def build_network(world_dir):
    cached = Path("var/target-activities/network.json")
    old = json.loads(cached.read_text()) if cached.exists() else make_network(world_dir)[0]
    manifest = json.loads((world_dir / "world.json").read_text())
    if (
        old["world_content_version"] != manifest["contentVersion"]
        or old["coordinate_sha256"] != manifest["coordinate"]["sha256"]
        or old["adapter"]["source_sha256"] != hashlib.sha256(Path(syn.__file__).read_bytes()).hexdigest()[:16]
    ):
        raise ValueError("Cached network does not match the current world/generator")
    planner = syn._Planner(syn.SynthSpec(seed=old["adapter"]["seed"]))
    city = planner.run()
    nodes, edges, facilities, portals, rejected = {}, {}, {}, [], []

    def ground(p):
        return [float(p[0]), float(p[1]), planner.ground_z_scalar(float(p[0]), float(p[1]))]

    def clear(points, radius, *, vehicle=False):
        dense = []
        for p, q in itertools.pairwise(points):
            dense.extend(np.linspace(p, q, max(2, math.ceil(math.dist(p, q) / 0.3) + 1)))
        centers = np.array(dense)
        xy = np.concatenate(
            [
                centers[:, :2],
                *(centers[:, :2] + radius * np.array([math.cos(a), math.sin(a)]) for a in np.arange(16) * math.pi / 8),
            ]
        )
        codes = planner.lu[
            np.floor((xy[:, 1] + planner.H) / syn.RES).astype(int),
            np.floor((xy[:, 0] + planner.H) / syn.RES).astype(int),
        ]
        z = syn._ground_z(xy[:, 0], xy[:, 1], codes, planner)[0]
        allowed = (
            [syn.U_ROAD, syn.U_MARK] if vehicle else [syn.U_PAVED, syn.U_ROAD, syn.U_MARK, syn.U_LAWN, syn.U_BUILDING]
        )
        if not np.isin(codes, allowed).all():
            return "surface"
        if np.ptp(z) > 0.3:
            return "step"
        for b in city.buildings:
            for part in [*b.parts, *b.equip]:
                if part.inside(xy[:, 0], xy[:, 1], z + 0.8).any():
                    return "building"
        for tree in city.trees:
            d = np.hypot(centers[:, 0] - tree.x, centers[:, 1] - tree.y)
            if (d < radius + 0.3).any() or (tree.trunk < 1.9 and (d < tree.r + radius).any()):
                return "tree"
        return None

    def node(key, p):
        nodes[key] = {"pos": ground(p)}
        return key

    def edge(
        key, a, b, points, *, profiles=("person", "dog", "machine"), radius=0.45, speed=1.8, junction=None, signal=None
    ):
        # Include exact ground samples, including road/sidewalk transitions.
        dense = []
        for p, q in itertools.pairwise(points):
            segment = np.linspace(p, q, max(2, math.ceil(math.dist(p, q) / 0.25) + 1))
            dense.extend(segment[:-1])
        dense.append(points[-1])
        array = np.array(dense)
        codes = planner.lu[
            np.floor((array[:, 1] + planner.H) / syn.RES).astype(int),
            np.floor((array[:, 0] + planner.H) / syn.RES).astype(int),
        ]
        array[:, 2] = syn._ground_z(array[:, 0], array[:, 1], codes, planner)[0]
        dense = array.tolist()
        # Vehicle centre-line screening is followed by per-asset swept-body
        # certification. Do not reject a valid narrow turn using a long-car disc.
        admitted_radius = 1.1 if "vehicle" in profiles else radius
        reason = clear(dense, admitted_radius, vehicle="vehicle" in profiles)
        if reason:
            rejected.append({"edge": key, "reason": reason})
            return False
        edges[key] = {
            "from": a,
            "to": b,
            "points": dense,
            "profiles": list(profiles),
            "clearance_m": admitted_radius,
            "obstacle_exclusion_radius_m": 6.5 if "vehicle" in profiles else radius,
            "speed_mps": speed,
            "verified": True,
        }
        if junction:
            edges[key]["junction"] = junction
        if signal:
            edges[key]["signal"] = signal
        return True

    # Offset the opposing pedestrian streams. Connecting corner arcs are reserved.
    incoming = {k: [] for k in old["nodes"]}
    outgoing = {k: [] for k in old["nodes"]}
    sidewalk_segments = []
    for i, e in enumerate(old["edges"]):
        for reverse in (False, True):
            a, b = (e["to"], e["from"]) if reverse else (e["from"], e["to"])
            p, q = np.array(old["nodes"][a]["pos"]), np.array(old["nodes"][b]["pos"])
            d = (q - p) / np.linalg.norm(q - p)
            right = np.array([d[1], -d[0], 0]) * 0.65
            start, end = p + d * 1.6 + right, q - d * 1.6 + right
            key = f"p{i:03d}{'r' if reverse else 'f'}"
            na, nb = node(key + "a", start), node(key + "b", end)
            zone = "intersection-" + a[1:3] if e["kind"] == "crosswalk" else None
            signal = {"period_s": 64, "green_s": [0, 16]} if zone else None
            if edge(key, na, nb, [start, end], junction=zone, signal=signal):
                outgoing[a].append((na, start))
                incoming[b].append((nb, end))
                if e["kind"] == "crosswalk":
                    edges[key]["compatible_group"] = "pedestrian-crossing"
                if e["kind"] == "sidewalk":
                    sidewalk_segments.append((key, a, b, start, end))
    for original in old["nodes"]:
        center = np.array(old["nodes"][original]["pos"])
        for ni, p in incoming[original]:
            for no, q in outgoing[original]:
                # A bounded quadratic arc, fully rechecked against the actual geometry.
                if math.dist(p, q) < 0.1:
                    continue
                curve = [(1 - u) ** 2 * p + 2 * (1 - u) * u * center + u * u * q for u in np.linspace(0, 1, 17)]
                edge("join-" + ni + "-" + no, ni, no, curve, junction="corner-" + original)

    # Candidate facade entrances from generator parts. Use the exterior face nearest
    # a sidewalk; portal endpoint remains 0.55 m outside the solid building.
    used_edges = set()
    for bi, b in enumerate(city.buildings):
        if len(facilities) >= 18:
            break
        if not b.parts or b.parts[0].kind != "box":
            continue
        part = b.parts[0]
        x0, y0, x1, y1 = part.rect
        if not (130 < x0 < x1 < 470 and -290 < y0 < y1 < 50):
            continue
        faces = [
            ((x0, (y0 + y1) / 2), (-1, 0)),
            ((x1, (y0 + y1) / 2), (1, 0)),
            (((x0 + x1) / 2, y0), (0, -1)),
            (((x0 + x1) / 2, y1), (0, 1)),
        ]
        candidates = []
        for face, normal in faces:
            door = np.array(ground(np.array(face) + np.array(normal) * 0.6))
            for key, _a, _b, p, q in sidewalk_segments:
                opposite = key[:-1] + ("f" if key.endswith("r") else "r")
                if key in used_edges or opposite in used_edges or opposite not in edges:
                    continue
                d = q - p
                u = float(np.dot(door - p, d) / np.dot(d, d))
                if not 0.15 < u < 0.85:
                    continue
                entry = p + u * d
                length = math.dist(entry, door)
                if 2 < length < 35:
                    candidates.append((length, key, entry, door, face, normal))
        for length, key, entry, door, face, normal in sorted(candidates, key=lambda c: (c[0], c[1])):
            if clear([ground(entry), ground(door)], 0.45):
                continue
            fid = f"facility-{len(facilities) + 1:02d}"
            old_edge = edges[key]
            opposite = key[:-1] + ("f" if key.endswith("r") else "r")
            opposite_edge = edges[opposite]
            d = np.array(nodes[old_edge["to"]]["pos"]) - np.array(nodes[old_edge["from"]]["pos"])
            d = d / np.linalg.norm(d)
            enter = entry - d * 2.5
            leave = entry + d * 2.5
            fork = node(fid + "-fork", enter)
            merge = node(fid + "-merge", leave)
            entrance = node(fid + "-door", door)
            # Distinct entry/exit connectors: an incoming visitor never occupies the
            # only exit point and traps an indoor occupant behind their own queue.
            if clear([ground(enter), ground(door)], 0.45) or clear([ground(door), ground(leave)], 0.45):
                continue
            if not edge(key + "-a", old_edge["from"], fork, [nodes[old_edge["from"]]["pos"], enter]):
                continue
            if not edge(key + "-b", fork, merge, [enter, leave], junction="portal-" + fid):
                continue
            if not edge(key + "-c", merge, old_edge["to"], [leave, nodes[old_edge["to"]]["pos"]]):
                continue
            if not edge(fid + "-in", fork, entrance, [enter, door], junction="portal-" + fid):
                continue
            if not edge(fid + "-out", entrance, merge, [door, leave], junction="portal-" + fid):
                continue
            edges[fid + "-in"]["facility_entry"] = fid
            # The doorway connector crosses BOTH pedestrian streams. Reserve only
            # a short local segment of the opposite stream as well, otherwise three
            # actors can enter mutually blocking positions despite pairwise yielding.
            op = np.array(nodes[opposite_edge["from"]]["pos"])
            oq = np.array(nodes[opposite_edge["to"]]["pos"])
            od = (oq - op) / np.linalg.norm(oq - op)
            center = op + od * np.dot(entry - op, od)
            pre = node(fid + "-opposite-pre", center - od * 3.5)
            post = node(fid + "-opposite-post", center + od * 3.5)
            edge(opposite + "-a", opposite_edge["from"], pre, [op, nodes[pre]["pos"]])
            edge(opposite + "-b", pre, post, [nodes[pre]["pos"], nodes[post]["pos"]], junction="portal-" + fid)
            edge(opposite + "-c", post, opposite_edge["to"], [nodes[post]["pos"], oq])
            del edges[key]
            del edges[opposite]
            used_edges.update((key, opposite))
            kinds = ["home", "office", "shop", "cafe", "clinic", "home"]
            names = ["住宅", "办公楼", "商店", "咖啡店", "诊所", "住宅"]
            k = (len(facilities)) % len(kinds)
            facilities[fid] = {
                "node": entrance,
                "label": f"{names[k]} {len(facilities) + 1:02d}",
                "kind": kinds[k],
                "profiles": ["person"],
                "capacity": 12 if kinds[k] == "office" else 4,
                "indoor": True,
                "portal_verified": True,
                "semantic_source": "authored synthetic scenario",
                "frontage_center": ground(face),
                "normal_xy": list(normal),
                "portal_width_m": 1.4,
                "portal_height_m": 2.4,
            }
            portals.append({"facility": fid, "building_index": bi, "connector_m": length})
            break

    # Right-hand traffic, lane centres separated by nine metres. Junction curves
    # and crosswalks share reservations and non-overlapping signal windows.
    xs, ys = [120.0, 240.0, 360.0, 480.0], [-300.0, -180.0, -60.0, 60.0]
    directions = [
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
        np.array([-1.0, 0.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
    ]
    junction_nodes = {}
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            center = np.array(ground([x, y]))
            r = max(planner.road_w(x, "x"), planner.road_w(y, "y")) / 2 + 14
            for k, d in enumerate(directions):
                right = np.array([d[1], -d[0], 0]) * 4.5
                ni = node(f"v{i}{j}{k}in", center - d * r + right)
                no = node(f"v{i}{j}{k}out", center + d * r + right)
                junction_nodes[i, j, k] = (ni, no, r)
            for incoming_dir, d in enumerate(directions):
                ni, _, _ = junction_nodes[i, j, incoming_dir]
                p = np.array(nodes[ni]["pos"])
                for outgoing_dir, d2 in enumerate(directions):
                    if outgoing_dir == (incoming_dir + 2) % 4:
                        continue
                    _, no, _ = junction_nodes[i, j, outgoing_dir]
                    q = np.array(nodes[no]["pos"])
                    curve = junction_curve(p, q, d, d2, r)
                    edge(
                        f"vturn-{i}{j}-{incoming_dir}{outgoing_dir}",
                        ni,
                        no,
                        curve,
                        profiles=("vehicle",),
                        radius=3.2,
                        speed=5,
                        junction=f"intersection-{i}{j}",
                        signal={"period_s": 64, "green_s": [26, 60]},
                    )
    vehicle_links = []
    for i, _x in enumerate(xs):
        for j, _y in enumerate(ys):
            for k, d in enumerate(directions):
                ii, jj = i + int(d[0]), j + int(d[1])
                if (ii, jj, k) not in junction_nodes:
                    continue
                _, a, _ = junction_nodes[i, j, k]
                b, _, _ = junction_nodes[ii, jj, k]
                key = f"vlink-{i}{j}-{ii}{jj}"
                if edge(key, a, b, [nodes[a]["pos"], nodes[b]["pos"]], profiles=("vehicle",), radius=3.2, speed=8):
                    vehicle_links.append(key)
    # Off-flow lay-bys only on the 26 m avenues. Conservative body admission rejects
    # a bay if its envelope touches sidewalk/buildings; never invent a road surface.
    for key in vehicle_links:
        e = edges[key]
        p, q = np.array(nodes[e["from"]]["pos"]), np.array(nodes[e["to"]]["pos"])
        d = (q - p) / np.linalg.norm(q - p)
        right = np.array([d[1], -d[0], 0])
        mid = (p + q) / 2
        bay = mid + right * 5.5
        if clear([bay - d * 4, bay + d * 4], 2.7, vehicle=True):
            continue
        fid = f"parking-{len([f for f in facilities if f.startswith('parking')]) + 1:02d}"
        pre = node(fid + "-pre", mid - d * 30)
        post = node(fid + "-post", mid + d * 30)
        stop = node(fid + "-stop", bay)
        # Smooth pull-in and pull-out, with space beside the through lane.
        in_points = [
            (1 - u) ** 3 * np.array(nodes[pre]["pos"])
            + 3 * (1 - u) ** 2 * u * (mid - d * 20)
            + 3 * (1 - u) * u * u * (bay - d * 10)
            + u**3 * bay
            for u in np.linspace(0, 1, 33)
        ]
        out_points = [
            (1 - u) ** 3 * bay
            + 3 * (1 - u) ** 2 * u * (bay + d * 10)
            + 3 * (1 - u) * u * u * (mid + d * 20)
            + u**3 * np.array(nodes[post]["pos"])
            for u in np.linspace(0, 1, 33)
        ]
        if not edge(fid + "-in", pre, stop, in_points, profiles=("vehicle",), radius=2.7, speed=2.5):
            continue
        if not edge(fid + "-out", stop, post, out_points, profiles=("vehicle",), radius=2.7, speed=2.5):
            continue
        edge(key + "-a", e["from"], pre, [p, nodes[pre]["pos"]], profiles=("vehicle",), radius=3.2, speed=8)
        edge(key + "-b", pre, post, [nodes[pre]["pos"], nodes[post]["pos"]], profiles=("vehicle",), radius=3.2, speed=8)
        edge(key + "-c", post, e["to"], [nodes[post]["pos"], q], profiles=("vehicle",), radius=3.2, speed=8)
        del edges[key]
        facilities[fid] = {
            "node": stop,
            "label": f"路边停车位 {fid[-2:]}",
            "kind": "parking",
            "profiles": ["vehicle"],
            "capacity": 1,
            "indoor": False,
            "semantic_source": "authored avenue lay-by",
        }
    network = {
        **{k: old[k] for k in ("world_id", "world_content_version", "coordinate_sha256", "adapter")},
        "version": VERSION,
        "status": "verified",
        "nodes": nodes,
        "edges": edges,
        "facilities": facilities,
        "portals": portals,
        "admission": {
            "rejected_edges": rejected,
            "ground_source": "exact synthetic generator",
            "footprint_step_m": 0.25,
            "radial_samples": 16,
            "interiors": "portal occupancy, not interior geometry",
        },
    }
    from extend_life_network import extend
    from vehicle_envelopes import certify

    return certify(extend(network, planner, city), planner, city)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("world", type=Path)
    ap.add_argument("--seed", type=int, default=20261022)
    ap.add_argument("--people", type=int, default=12)
    ap.add_argument("--vehicles", type=int, default=4)
    ap.add_argument("--audit-seconds", type=float, default=1500)
    ap.add_argument("--network-only", action="store_true")
    args = ap.parse_args()
    out = Path("var/target-life")
    out.mkdir(parents=True, exist_ok=True)
    cache = out / "network.json"
    network = build_network(args.world)
    cache.write_text(json.dumps(network, ensure_ascii=False, separators=(",", ":")))
    print(
        json.dumps(
            {
                "nodes": len(network["nodes"]),
                "edges": len(network["edges"]),
                "facilities": [(k, f["kind"]) for k, f in network["facilities"].items()],
                "rejected": len(network["admission"]["rejected_edges"]),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if args.network_only:
        return
    doc = populate(network, args.seed, args.people, args.vehicles)
    if args.audit_seconds < 1500:
        raise ValueError("Admission requires at least 25 minutes")
    audit = audit_recipe(doc, args.audit_seconds)
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(json.dumps(audit, ensure_ascii=False, indent=2), flush=True)
    if not audit["admitted"]:
        raise SystemExit("Life population failed admission; template was not replaced")
    sha = persist_recipe(doc, out)
    targets = target_configs(doc, sha)
    template = json.loads(Path("scenarios/sandbox/synthcity/t8_scene_activity.json").read_text())
    template.update(
        template_id="t9_scene_life",
        name_zh="T9 场景生活与交通（实验）",
        summary_zh="共享活动时钟：居民通勤购物、设施出入、分向通行、交叉口让行与停车办事。",
        first_feedback_zh="选择目标查看当前活动、目的地与等待原因",
        seed=args.seed,
        targets=targets,
    )
    Path("scenarios/sandbox/synthcity/t9_scene_life.json").write_text(
        json.dumps(template, ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
