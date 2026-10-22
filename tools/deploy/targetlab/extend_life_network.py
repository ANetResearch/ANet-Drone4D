"""Admit additional target classes against synthcity geometry, never flat-ground fallbacks."""

import itertools
import math

import numpy as np

from awr.world.ingest import synthetic as syn


def extend(network, planner, city):
    nodes, edges, facilities = network["nodes"], network["edges"], network["facilities"]
    # Rerunning admission replaces these generated layers, rather than accumulating them.
    for table in (nodes, edges, facilities):
        for key in list(table):
            if key.startswith(("air-", "yard-")):
                del table[key]

    def clear(points, radius, height, *, air=False, paved=False):
        dense = np.concatenate(
            [np.linspace(a, b, max(2, math.ceil(math.dist(a, b) / 0.4) + 1)) for a, b in itertools.pairwise(points)]
        )
        xy = np.concatenate(
            [
                dense[:, :2],
                *(dense[:, :2] + radius * np.array([math.cos(a), math.sin(a)]) for a in np.arange(16) * math.pi / 8),
            ]
        )
        idx = np.floor((xy + planner.H) / syn.RES).astype(int)
        if (idx < 0).any() or (idx >= planner.n).any():
            return False
        codes = planner.lu[idx[:, 1], idx[:, 0]]
        if not air and not np.isin(codes, [syn.U_PAVED] if paved else [syn.U_PAVED, syn.U_LAWN]).all():
            return False
        ground = syn._ground_z(xy[:, 0], xy[:, 1], codes, planner)[0]
        zz = np.tile(dense[:, 2], 17)
        if not air and (np.abs(ground - zz) > 0.22).any():
            return False
        if air and (zz - ground < 2).any():
            return False
        for b in city.buildings:
            for part in [*b.parts, *b.equip]:
                for dz in (0.05, height / 2, height):
                    if part.inside(xy[:, 0], xy[:, 1], zz + dz).any():
                        return False
        for t in city.trees:
            dist = np.hypot(dense[:, 0] - t.x, dense[:, 1] - t.y)
            if not air and (dist < radius + 0.4).any():
                return False
            if (dist < t.r + radius).any() and (not air or np.min(dense[:, 2]) < t.z + t.trunk + 2 * t.r):
                return False
        return True

    def ground(p):
        return [float(p[0]), float(p[1]), planner.ground_z_scalar(*map(float, p[:2]))]

    def edge(key, a, b, points, profiles, radius, speed):
        edges[key] = {
            "from": a,
            "to": b,
            "points": [list(map(float, p)) for p in points],
            "profiles": profiles,
            "verified": True,
            "clearance_m": radius,
            "speed_mps": speed,
        }

    spawns = {
        "person": sorted(n for n in nodes if n.startswith("p") and n.endswith("a")),
        "vehicle": sorted(n for n in nodes if n.startswith("v") and n.endswith("out")),
        "dog": [],
        "machine": [],
        "uav": [],
        "object": [],
    }
    # Off-corridor outdoor pauses; stationary props must not occupy walking streams.
    for fid, f in list(facilities.items()):
        if not f.get("indoor"):
            continue
        if f["kind"] == "home" and "dog" not in f["profiles"]:
            f["profiles"].append("dog")
        a = fid + "-fork"
        b = fid + "-merge"
        p, q = np.array(nodes[a]["pos"]), np.array(nodes[b]["pos"])
        d = (q - p) / np.linalg.norm(q - p)
        side = np.array([d[1], -d[0], 0.0])
        for sign in (1, -1):
            rest = ground((p + q) / 2 + side * sign * 2.2)
            if not clear([p, rest, q], 0.35, 0.5):
                continue
            nid = fid + "-pet-rest"
            nodes[nid] = {"pos": rest}
            edge(nid + "-in", a, nid, [p, rest], ["dog"], 0.35, 1.5)
            edge(nid + "-out", nid, b, [rest, q], ["dog"], 0.35, 1.5)
            facilities[nid] = {
                "node": nid,
                "label": "户外嗅闻/休息点 " + fid[-2:],
                "kind": "pet_rest",
                "profiles": ["dog"],
                "capacity": 1,
                "indoor": False,
                "semantic_source": "authored outdoor rest area",
            }
            spawns["dog"].append(nid)
            break
        for sign in (1, -1):
            spot = ground((p + q) / 2 + side * sign * 4)
            if clear([spot, np.array(spot) + np.array([0.01, 0, 0])], 1.1, 1.2):
                nid = fid + "-prop"
                nodes[nid] = {"pos": spot}
                spawns["object"].append(nid)
                break
    # Low-altitude inspection corridors are separately checked in 3D. They do not
    # inherit the surface graph's signals, portals, or ground clearance certificate.
    air_nodes = set()
    for key, e in list(edges.items()):
        if e["profiles"] != ["vehicle"]:
            continue
        points = np.array(e["points"])
        points[:, 2] += 25
        if not clear(points, 0.65, 0.5, air=True):
            continue
        a, b = "air-" + e["from"], "air-" + e["to"]
        nodes[a] = {"pos": points[0].tolist()}
        nodes[b] = {"pos": points[-1].tolist()}
        edge("air-" + key, a, b, points, ["uav"], 0.65, 4)
        air_nodes.update((a, b))
    adjacency = {n: [] for n in air_nodes}
    reverse = {n: [] for n in air_nodes}
    for e in edges.values():
        if e["profiles"] == ["uav"]:
            adjacency[e["from"]].append(e["to"])
            reverse[e["to"]].append(e["from"])

    def reachable(graph, start):
        seen = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(graph[n])
        return seen

    components = []
    remaining = set(air_nodes)
    while remaining:
        n = min(remaining)
        component = reachable(adjacency, n) & reachable(reverse, n)
        components.append(component)
        remaining -= component
    connected = max(components, key=lambda c: (len(c), sorted(c)), default=set())
    for n in sorted(connected):
        if n.endswith("out"):
            spawns["uav"].append(n)
            facilities[n] = {
                "node": n,
                "label": "空中巡检点 " + n[4:],
                "kind": "inspection",
                "profiles": ["uav"],
                "capacity": 1,
                "indoor": False,
                "semantic_source": "authored inspection point at 25 m over road",
            }
    # A forklift is only admitted inside an obstacle-free paved work yard. Do not
    # send it through pedestrian facilities or pretend a grass field is a depot.
    for x in range(150, 451, 20):
        if spawns["machine"]:
            break
        for y in range(-270, 31, 20):
            angles = np.linspace(0, 2 * np.pi, 129)
            points = np.array([ground([x + 6 * np.cos(t), y + 6 * np.sin(t)]) for t in angles])
            if not clear(points, 1.85, 2.4, paved=True):
                continue
            for j in range(4):
                key = f"yard-{j}"
                nodes[key] = {"pos": points[j * 32].tolist()}
                facilities[key] = {
                    "node": key,
                    "label": ["装卸交接点", "搬运检查点", "卸货点", "设备检查点"][j],
                    "kind": "handling",
                    "profiles": ["machine"],
                    "capacity": 1,
                    "indoor": False,
                    "semantic_source": "authored synthetic logistics yard",
                }
            for j in range(4):
                p = points[j * 32 : (j + 1) * 32 + 1].copy()
                p[-1] = nodes[f"yard-{(j + 1) % 4}"]["pos"]
                edge(f"yard-edge-{j}", f"yard-{j}", f"yard-{(j + 1) % 4}", p, ["machine"], 1.85, 1.2)
            spawns["machine"] = ["yard-0", "yard-2"]
            break
    # Props are permanent obstacles, so reject any location intersecting ANY admitted
    # travel corridor, including door connectors. Surface support alone is insufficient.
    segments = []
    radii = []
    for e in edges.values():
        if e["profiles"] == ["uav"]:
            continue
        p = np.array(e["points"])
        segments.extend(itertools.pairwise(p))
        radii.extend([e.get("obstacle_exclusion_radius_m", e["clearance_m"])] * len(p[:-1]))
    if segments:
        seg = np.array(segments)
        a = seg[:, 0]
        delta = seg[:, 1] - a
        den = np.maximum(np.sum(delta * delta, axis=1), 1e-12)
        safe = []
        for n in spawns["object"]:
            point = np.array(nodes[n]["pos"])
            u = np.clip(np.sum((point - a) * delta, axis=1) / den, 0, 1)
            if np.min(np.linalg.norm(point - (a + delta * u[:, None]), axis=1) - np.array(radii)) > 1.25:
                safe.append(n)
        # Search additional supported roadside/courtyard pockets after all corridors
        # exist. A failed first candidate must not conceal a valid offset location.
        for x in range(150, 451, 10):
            if len(safe) >= 24:
                break
            for y in range(-270, 31, 10):
                if len(safe) >= 24:
                    break
                point = np.array(ground([x, y]))
                u = np.clip(np.sum((point - a) * delta, axis=1) / den, 0, 1)
                gap = np.min(np.linalg.norm(point - (a + delta * u[:, None]), axis=1) - np.array(radii))
                if not 1.4 < gap < 10:
                    continue
                if any(np.linalg.norm(point - np.array(nodes[n]["pos"])) < 5 for n in safe):
                    continue
                if not clear([point, point + np.array([0.01, 0, 0])], 1.1, 1.2):
                    continue
                n = f"prop-slot-{len(safe):02d}"
                nodes[n] = {"pos": point.tolist()}
                safe.append(n)
        spawns["object"] = safe
    network["spawn_nodes"] = spawns
    network["admission"]["extra_classes"] = {k: len(v) for k, v in spawns.items()}
    return network
