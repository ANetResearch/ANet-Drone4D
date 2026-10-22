"""Seeded errands on an explicit, verified pedestrian network; absolute simulation time playback.

Adapters supply confirmed corridors and destinations. This module never infers walkability from
flat ground or invents facilities. Persisted plans are private deployment artifacts, hash bound.
"""

from __future__ import annotations

import bisect
import hashlib
import heapq
import itertools
import json
import math
import os
import random
from functools import lru_cache
from pathlib import Path

import numpy as np

VERSION = "pedestrian-activity-1"


def digest(doc: dict) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def shortest_path(network: dict, start: str, end: str) -> list[str]:
    nodes = network["nodes"]
    adjacent = {k: [] for k in nodes}
    for edge in network["edges"]:
        if edge["profile"] != "person" or not edge["verified"]:
            continue
        a, b = edge["from"], edge["to"]
        cost = math.dist(nodes[a]["pos"], nodes[b]["pos"])
        adjacent[a].append((b, cost))
        if edge.get("bidirectional", False):
            adjacent[b].append((a, cost))
    queue = [(0.0, start, [start])]
    best = {start: 0.0}
    while queue:
        distance, node, path = heapq.heappop(queue)
        if node == end:
            return path
        if distance > best[node]:
            continue
        for nxt, cost in sorted(adjacent[node]):
            value = distance + cost
            if value < best.get(nxt, math.inf):
                best[nxt] = value
                heapq.heappush(queue, (value, nxt, [*path, nxt]))
    raise ValueError(f"No confirmed pedestrian route: {start} -> {end}")


def compile_plan(
    network: dict, actor_id: str, seed: int, start: str, *, duration_s=1500.0, speed_mps=1.5, variant=0
) -> dict:
    if network.get("status") != "verified" or not network.get("world_content_version"):
        raise ValueError("Unverified or unbound activity network")
    if not 0.3 <= speed_mps <= 2.0 or not 60 <= duration_s <= 7200:
        raise ValueError("Invalid pedestrian speed or duration")
    # Independent actor stream: adding/removing another actor never changes this itinerary.
    rng = random.Random(int(hashlib.sha256(f"{VERSION}:{seed}:{actor_id}:{variant}".encode()).hexdigest(), 16))
    nodes = network["nodes"]
    if start not in nodes:
        raise ValueError("Unknown start node")
    destinations = sorted(k for k, v in nodes.items() if v.get("destination"))
    edge_ground = {}
    for edge in network["edges"]:
        if not edge.get("verified") or edge["profile"] != "person":
            continue
        ground = edge.get("ground_profile")
        if ground:
            edge_ground[edge["from"], edge["to"]] = ground
            if edge.get("bidirectional"):
                edge_ground[edge["to"], edge["from"]] = [[1 - u, z] for u, z in reversed(ground)]
    segments, visits = [], []
    t, yaw, walked = 0.0, 0.0, 0.0
    current, previous = start, None

    def append(kind, end, p, q, heading, name, route):
        nonlocal t, yaw, walked
        if end <= t:
            return
        delta = math.atan2(math.sin(heading - yaw), math.cos(heading - yaw))
        segments.append(
            {
                "t0": t,
                "t1": end,
                "p": list(p),
                "q": list(q),
                "yaw0": yaw,
                "yaw1": yaw + delta,
                "phase": kind,
                "destination": name,
                "route": route,
                "walked0_m": walked,
            }
        )
        walked += math.dist(p, q)
        yaw += delta
        t = end

    while t < duration_s:
        candidates = []
        for dst in destinations:
            if dst in (current, previous):
                continue
            try:
                path = shortest_path(network, current, dst)
            except ValueError:
                continue
            distance = sum(math.dist(nodes[a]["pos"], nodes[b]["pos"]) for a, b in itertools.pairwise(path))
            if distance >= 100:
                candidates.append((dst, path))
        if not candidates:
            raise ValueError("Need at least three reachable destinations, with routes >= 100 m")
        destination, route = rng.choice(candidates)
        name = nodes[destination]["label"]
        for a, b in itertools.pairwise(route):
            p, q = nodes[a]["pos"], nodes[b]["pos"]
            heading = math.atan2(q[1] - p[1], q[0] - p[0])
            angle = abs(math.atan2(math.sin(heading - yaw), math.cos(heading - yaw)))
            # Stop/look before changing direction; no rotating a moving body sideways.
            append("orient", t + max(0.5, angle / 1.2), p, p, heading, name, route)
            length = math.dist(p, q)
            # Cubic ease has peak speed 1.5*distance/duration and continuous zero endpoint velocity.
            append("walk", t + 1.5 * length / speed_mps, p, q, heading, name, route)
            if (a, b) in edge_ground:
                segments[-1]["ground_profile"] = edge_ground[a, b]
        visits.append({"node": destination, "arrival_s": t, "purpose": nodes[destination]["purpose"]})
        append(
            "rest",
            t + rng.uniform(25, 75),
            nodes[destination]["pos"],
            nodes[destination]["pos"],
            yaw,
            name,
            [destination],
        )
        previous, current = current, destination
    # Complete the current trip rather than cutting it in mid-crossing at a nominal horizon.
    return {
        "version": VERSION,
        "actor_id": actor_id,
        "class": "person",
        "seed": seed,
        "variant": variant,
        "world_id": network["world_id"],
        "world_content_version": network["world_content_version"],
        "coordinate_sha256": network["coordinate_sha256"],
        "network_sha256": digest(network),
        "duration_s": t,
        "radius_m": network["radius_m"],
        "height_m": network["height_m"],
        "nodes": nodes,
        "segments": segments,
        "visits": visits,
        "distance_m": walked,
    }


class ActivityPlan:
    def __init__(self, doc: dict):
        self.doc = doc
        self.segments = doc["segments"]
        if doc.get("version") != VERSION or not 1 <= len(self.segments) <= 10000:
            raise ValueError("Invalid activity plan version/size")
        self.starts = [s["t0"] for s in self.segments]
        end, pos = 0.0, self.segments[0]["p"]
        for s in self.segments:
            values = [s["t0"], s["t1"], *s["p"], *s["q"], s["yaw0"], s["yaw1"]]
            if not all(math.isfinite(v) for v in values) or s["t0"] != end or s["t1"] <= end or s["p"] != pos:
                raise ValueError("Discontinuous or invalid activity plan")
            if s["phase"] not in ("walk", "orient", "rest", "wait"):
                raise ValueError("Unknown activity phase")
            if s.get("ground_profile"):
                g = np.asarray(s["ground_profile"], float)
                if (
                    g.ndim != 2
                    or g.shape[1] != 2
                    or len(g) < 2
                    or not np.isfinite(g).all()
                    or not np.all(np.diff(g[:, 0]) > 0)
                    or g[0, 0] != 0
                    or g[-1, 0] != 1
                    or abs(g[0, 1] - s["p"][2]) > 1e-6
                    or abs(g[-1, 1] - s["q"][2]) > 1e-6
                ):
                    raise ValueError("Invalid ground profile")
            end, pos = s["t1"], s["q"]
        if self.starts[0] != 0 or end != doc["duration_s"]:
            raise ValueError("Invalid activity horizon")

    def positions(self, times):
        times = np.asarray(times, float)
        indices = np.clip(np.searchsorted(self.starts, times, side="right") - 1, 0, len(self.starts) - 1)
        starts = np.array(self.starts)[indices]
        ends = np.array([s["t1"] for s in self.segments])[indices]
        p = np.array([s["p"] for s in self.segments])[indices]
        q = np.array([s["q"] for s in self.segments])[indices]
        u = np.clip((times - starts) / (ends - starts), 0, 1)
        w = u * u * (3 - 2 * u)
        result = p + (q - p) * w[:, None]
        for i in np.unique(indices):
            ground = self.segments[i].get("ground_profile")
            if ground:
                g = np.asarray(ground)
                selected = indices == i
                result[selected, 2] = np.interp(w[selected], g[:, 0], g[:, 1])
        return result

    def sample(self, t_s: float):
        i = max(0, min(len(self.starts) - 1, bisect.bisect_right(self.starts, t_s) - 1))
        s = self.segments[i]
        dt = s["t1"] - s["t0"]
        u = max(0.0, min(1.0, (t_s - s["t0"]) / dt))
        w = u * u * (3 - 2 * u)
        p, q = np.asarray(s["p"], float), np.asarray(s["q"], float)
        pos = p + (q - p) * w
        vel = (q - p) * (6 * u * (1 - u) / dt)
        ground = s.get("ground_profile")
        if ground:
            g = np.asarray(ground)
            pos[2] = np.interp(w, g[:, 0], g[:, 1])
            k = max(0, min(len(g) - 2, int(np.searchsorted(g[:, 0], w, side="right")) - 1))
            vel[2] = (g[k + 1, 1] - g[k, 1]) / (g[k + 1, 0] - g[k, 0]) * (6 * u * (1 - u) / dt)
        yaw = s["yaw0"] + (s["yaw1"] - s["yaw0"]) * w
        state = {
            "phase": s["phase"] if t_s < self.doc["duration_s"] else "complete",
            "destination": s["destination"],
            "remaining_s": round(max(0, s["t1"] - t_s), 1),
            "seed": self.doc["seed"],
            "walked_m": round(s["walked0_m"] + float(np.linalg.norm(q - p)) * w, 1),
            "duration_s": self.doc["duration_s"],
            "visits": sum(v["arrival_s"] <= t_s for v in self.doc["visits"]),
            "route_enu_m": [self.doc["nodes"][n]["pos"] for n in s["route"]],
        }
        return pos, yaw, vel, state


@lru_cache(maxsize=128)
def load_plan(plan_id: str, sha256: str) -> ActivityPlan:
    # IDs are validated before reaching here. Content addressing prevents old snapshots loading a new plan.
    if not plan_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in plan_id) or len(sha256) != 64:
        raise ValueError("Invalid activity plan reference")
    root = Path(os.environ.get("AWR_TARGET_ACTIVITY_DIR", "var/target-activities"))
    raw = (root / f"{plan_id}-{sha256}.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ValueError("Activity artifact hash mismatch")
    return ActivityPlan(json.loads(raw))


def bound_plan(raw: dict, world) -> ActivityPlan | None:
    binding = raw.get("activity")
    if not binding:
        return None
    plan = load_plan(binding["plan_id"], binding["sha256"])
    doc = plan.doc
    if world is None or any(
        doc[k] != getattr(world, attr, None)
        for k, attr in (
            ("world_id", "world_id"),
            ("world_content_version", "content_version"),
            ("coordinate_sha256", "coordinate_sha256"),
        )
    ):
        raise ValueError("Activity world/coordinate version mismatch")
    size = raw["size_m"]
    if (
        raw["class"] != "person"
        or math.hypot(size["l"], size["w"]) / 2 > doc["radius_m"]
        or size["h"] > doc["height_m"]
    ):
        raise ValueError("Target exceeds verified corridor footprint")
    if list(raw["pos"]) != doc["segments"][0]["p"]:
        raise ValueError("Activity start must match plan; regenerate to relocate")
    return plan
