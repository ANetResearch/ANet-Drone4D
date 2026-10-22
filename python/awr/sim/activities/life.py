"""Deterministic, whole-population activity simulation on an admitted scene network.

All actors participate in every fixed step, regardless of the viewer/camera. The model
owns decisions, progress and waiting; a renderer never advances an actor's clock.
Indoor visits are explicit portal occupancy (interiors are not simulated).
"""

from __future__ import annotations

import copy
import hashlib
import heapq
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import bodies

VERSION = "scene-life-1"
DT = 0.1


@lru_cache(maxsize=1)
def runtime_fingerprint():
    """Old saved populations cannot silently replay under a changed motion engine."""
    return hashlib.sha256(Path(__file__).read_bytes() + Path(bodies.__file__).read_bytes()).hexdigest()


def stable_int(*values) -> int:
    return int(hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()[:16], 16)


def _polyline(points):
    p = np.asarray(points, dtype=float)
    if p.ndim != 2 or p.shape[1] != 3 or len(p) < 2 or not np.isfinite(p).all():
        raise ValueError("Invalid lane geometry")
    lengths = np.linalg.norm(np.diff(p, axis=0), axis=1)
    if np.any(lengths < 1e-5):
        raise ValueError("Zero-length lane segment")
    return p, np.r_[0.0, np.cumsum(lengths)]


@dataclass
class Actor:
    spec: dict
    pos: np.ndarray
    node: str
    yaw: float = 0.0
    speed: float = 0.0
    vel: np.ndarray = field(default_factory=lambda: np.zeros(3))
    edge: str | None = None
    progress: float = 0.0
    route: list[str] = field(default_factory=list)
    visit_index: int = 0
    destination: str = ""
    dwell_until: float = 0.0
    hidden: bool = False
    phase: str = "departing"
    reason: str = ""
    distance: float = 0.0
    visits: int = 0
    updates: int = 0
    stopped_s: float = 0.0
    max_unplanned_wait_s: float = 0.0
    no_progress_s: float = 0.0
    max_no_progress_s: float = 0.0
    reason_seconds: Counter = field(default_factory=Counter)

    @property
    def id(self):
        return self.spec["id"]

    @property
    def radius(self):
        return self.spec["radius_m"]


class LifeWorld:
    """A shared, fixed-step world. Recipe + version + seed + time define its state.

    Network adapters must certify ground/corridor geometry. Covering vehicle sphere
    rows and swept-distance checks prevent interpenetration; they are not tire physics.
    Signals and junction reservations model right of way, with first-arrival ordering.
    """

    def __init__(self, recipe: dict):
        self.doc = copy.deepcopy(recipe)
        if recipe.get("version") != VERSION or recipe.get("status") != "verified":
            raise ValueError("Life recipe must bind a verified network")
        self.nodes = recipe["nodes"]
        self.edges = recipe["edges"]
        self.facilities = recipe["facilities"]
        self.geometry = {}
        self.adjacent = {n: [] for n in self.nodes}
        for key, e in self.edges.items():
            if not e.get("verified") or e["from"] not in self.nodes or e["to"] not in self.nodes:
                raise ValueError("Unverified or disconnected lane")
            p, s = _polyline(e["points"])
            if not np.allclose(p[0], self.nodes[e["from"]]["pos"], atol=1e-8) or not np.allclose(
                p[-1], self.nodes[e["to"]]["pos"], atol=1e-8
            ):
                raise ValueError("Lane endpoints differ from nodes")
            if e.get("clearance_m", 0) <= 0 or e.get("speed_mps", 0) <= 0:
                raise ValueError("Missing lane clearance/speed")
            self.geometry[key] = p, s
            self.adjacent[e["from"]].append(key)
        for f in self.facilities.values():
            if f["node"] not in self.nodes or f.get("capacity", 0) < 1:
                raise ValueError("Invalid facility entrance/capacity")
            if f.get("indoor") and not f.get("portal_verified"):
                raise ValueError("Indoor facilities need an authored entrance portal")
        specs = recipe["actors"]
        if not 1 <= len(specs) <= 256 or len({s["id"] for s in specs}) != len(specs):
            raise ValueError("Invalid population size or duplicate identity")
        for a in specs:
            if a["start"] not in self.nodes or not 0.1 <= a["radius_m"] <= 10 or not 0 < a["speed_mps"] <= 25:
                raise ValueError("Invalid actor placement/footprint/speed")
            bodies.discs(a)  # Validate shape before accepting any route or state.
            if not a.get("stationary"):
                if len(a.get("itinerary", [])) < 2:
                    raise ValueError("Mobile actor requires at least two purposeful visits")
                previous = a["start"]
                for v in [*a["itinerary"], a["itinerary"][0]]:
                    f = self.facilities[v["facility"]]
                    if a["profile"] not in f["profiles"] or not 0 <= v["dwell_s"] <= 28800:
                        raise ValueError("Incompatible facility or dwell")
                    self.path(previous, f["node"], a)
                    previous = f["node"]
        self.reset()

    def reset(self):
        self.tick = 0
        self.actors = {
            s["id"]: Actor(s, np.array(self.nodes[s["start"]]["pos"], float), s["start"], yaw=s.get("yaw_rad", 0.0))
            for s in sorted(self.doc["actors"], key=lambda a: a["id"])
        }
        self.junctions: dict[str, set[str]] = {}
        self.queue_since: dict[tuple[str, str], int] = {}
        self.events = []
        self.population_history = []
        self.event_digest = hashlib.sha256()
        self.safety_interventions = 0
        self.min_clearance_m = math.inf
        self.min_swept_clearance_m = math.inf
        visible = list(self.actors.values())
        for i, a in enumerate(visible):
            for b in visible[i + 1 :]:
                if bodies.clearance(a.spec, a.pos, a.yaw, b.spec, b.pos, b.yaw) < 0.05:
                    raise ValueError(f"Overlapping initial placements: {a.id}, {b.id}")

    @staticmethod
    def admits_edge(e, actor):
        if actor["profile"] not in e["profiles"]:
            return False
        if actor.get("body_m"):
            return e.get("vehicle_bodies", {}).get(actor.get("asset_id")) == actor["body_m"]
        return actor["radius_m"] <= e["clearance_m"]

    def path(self, start: str, end: str, actor: dict) -> list[str]:
        queue = [(0.0, (), start)]
        best = {start: 0.0}
        while queue:
            distance, route, node = heapq.heappop(queue)
            if node == end:
                return list(route)
            if distance > best[node]:
                continue
            for eid in sorted(self.adjacent[node]):
                e = self.edges[eid]
                if not self.admits_edge(e, actor):
                    continue
                cost = distance + self.geometry[eid][1][-1] / min(e["speed_mps"], actor["speed_mps"])
                if cost < best.get(e["to"], math.inf):
                    best[e["to"]] = cost
                    heapq.heappush(queue, (cost, (*route, eid), e["to"]))
        raise ValueError(f"No admitted {actor['profile']} route: {start} -> {end}")

    def _event(self, a, kind, **extra):
        event = {"tick": self.tick, "actor": a.id, "kind": kind, **extra}
        self.event_digest.update(json.dumps(event, sort_keys=True).encode())
        self.events.append(event)
        if len(self.events) > 512:
            self.events.pop(0)

    def _route(self, a):
        visit = a.spec["itinerary"][a.visit_index % len(a.spec["itinerary"])]
        a.destination = visit["facility"]
        a.route = self.path(a.node, self.facilities[a.destination]["node"], a.spec)
        a.phase, a.reason = "travel", ""
        self._event(a, "depart", destination=a.destination, purpose=visit["purpose"])

    def _signal_open(self, e):
        signal = e.get("signal")
        if not signal:
            return True
        phase = (self.tick * DT + signal.get("offset_s", 0)) % signal["period_s"]
        return signal["green_s"][0] <= phase < signal["green_s"][1]

    def _exit_open(self, e, actor):
        """Do not enter a conflict area unless its exit has storage space.

        Otherwise a queue waiting for the next signal can block the intersection
        owner that is required to release that very signal/corner (gridlock).
        """
        end = np.asarray(e["points"][-1])
        tangent = end - np.asarray(e["points"][-2])
        exit_yaw = math.atan2(tangent[1], tangent[0])
        return all(
            b.id == actor.id
            or b.hidden
            or bodies.clearance(
                actor.spec,
                end,
                exit_yaw,
                b.spec,
                b.pos,
                b.yaw,
            )
            >= 0.2
            for b in self.actors.values()
        )

    def _pose(self, eid, progress):
        p, s = self.geometry[eid]
        j = min(len(s) - 2, max(0, int(np.searchsorted(s, progress, side="right")) - 1))
        u = min(1.0, max(0.0, (progress - s[j]) / (s[j + 1] - s[j])))
        v = p[j + 1] - p[j]
        return p[j] + v * u, math.atan2(v[1], v[0])

    @staticmethod
    def _swept_clearance(a0, a1, b0, b1, radii):
        r, v = a0 - b0, (a1 - a0) - (b1 - b0)
        t = min(1.0, max(0.0, -float(r @ v) / max(1e-12, float(v @ v))))
        return float(np.linalg.norm(r + v * t)) - radii

    def _step(self):
        self.tick += 1
        now = self.tick * DT
        actors = list(self.actors.values())
        # Every actor gets decisions, timers, metrics and motion on every step.
        proposals = {}
        continuations = {}
        for a in actors:
            a.updates += 1
            a.vel[:] = 0
            a.reason = ""
            if a.spec.get("stationary"):
                a.phase, a.reason = "stationary", a.spec.get("stationary_reason", "installed facility")
                continue
            if a.dwell_until > now:
                a.phase, a.reason = (
                    "visit",
                    a.spec["itinerary"][(a.visit_index - 1) % len(a.spec["itinerary"])]["purpose"],
                )
                continue
            if a.hidden:
                # Exit only when there is physical space at the same portal. No teleport.
                if any(
                    not b.hidden and b.id != a.id and bodies.clearance(a.spec, a.pos, a.yaw, b.spec, b.pos, b.yaw) < 0.1
                    for b in actors
                ):
                    a.phase, a.reason = "wait", "entrance occupied"
                    continue
                exits = [self.edges[e] for e in self.adjacent[a.node] if a.spec["profile"] in self.edges[e]["profiles"]]
                portal_zone = exits[0].get("junction") if len(exits) == 1 else None
                if portal_zone:
                    # Do not reveal an exiting occupant in front of an already admitted
                    # incoming visitor. Reserve the single portal corridor before exit.
                    if portal_zone in self.junctions or any(z == portal_zone for z, _ in self.queue_since):
                        a.phase, a.reason = "wait", "entrance occupied"
                        continue
                    self.junctions[portal_zone] = {a.id}
                a.hidden = False
                self._event(a, "exit", facility=a.destination)
            if not a.route and a.edge is None:
                if a.phase == "arrived":
                    f = self.facilities[a.destination]
                    occupied = sum(
                        b.id != a.id and b.destination == a.destination and b.dwell_until > now for b in actors
                    )
                    if occupied >= f["capacity"]:
                        a.reason = "facility capacity"
                        continue
                    visit = a.spec["itinerary"][a.visit_index % len(a.spec["itinerary"])]
                    a.dwell_until = now + visit["dwell_s"]
                    a.hidden = bool(f.get("indoor"))
                    a.visits += 1
                    a.visit_index += 1
                    a.phase, a.reason = "visit", visit["purpose"]
                    self._event(a, "enter" if a.hidden else "service", facility=a.destination, purpose=visit["purpose"])
                    continue
                self._route(a)
                if not a.route:
                    a.phase = "arrived"
                    continue
            if a.edge is None:
                eid = a.route[0]
                e = self.edges[eid]
                entry = e.get("facility_entry")
                if entry:
                    occupancy = sum(
                        b.id != a.id
                        and (
                            (b.destination == entry and (b.hidden or b.dwell_until > now))
                            or (b.edge is not None and self.edges[b.edge].get("facility_entry") == entry)
                        )
                        for b in actors
                    )
                    if occupancy >= self.facilities[entry]["capacity"]:
                        self.queue_since.pop((e.get("junction"), a.id), None)
                        a.phase, a.reason = "wait", "facility capacity"
                        continue
                if not self._signal_open(e):
                    a.phase, a.reason = "wait", "red signal"
                    continue
                zone = e.get("junction")
                if zone:
                    self.queue_since.setdefault((zone, a.id), self.tick)
                    if not self._exit_open(e, a):
                        a.phase, a.reason = "wait", "downstream occupied"
                        continue
                    waiting = sorted(
                        (tick, aid)
                        for (z, aid), tick in self.queue_since.items()
                        if z == zone
                        and self.actors[aid].route
                        and self._signal_open(self.edges[self.actors[aid].route[0]])
                        and self._exit_open(self.edges[self.actors[aid].route[0]], self.actors[aid])
                    )
                    owners = self.junctions.get(zone, set())
                    group = e.get("compatible_group")
                    compatible = bool(group) and all(
                        self.actors[oid].edge and self.edges[self.actors[oid].edge].get("compatible_group") == group
                        for oid in owners
                    )
                    if a.id not in owners and ((owners and not compatible) or (not owners and waiting[0][1] != a.id)):
                        a.phase, a.reason = "wait", "junction right of way"
                        continue
                    self.junctions.setdefault(zone, set()).add(a.id)

                a.edge, a.progress = eid, 0.0
                a.route.pop(0)
                a.phase = "travel"
            a.phase = "travel"
            eid = a.edge
            _p, s = self.geometry[eid]
            remaining = float(s[-1]) - a.progress
            accel = a.spec.get("acceleration_mps2", 1.5 if a.spec["profile"] == "vehicle" else 1.0)
            exit_speed = 0.0
            if a.spec["profile"] == "vehicle" and a.route:
                next_edge = self.edges[a.route[0]]
                next_yaw = self._pose(a.route[0], 0)[1]
                end_yaw = self._pose(eid, float(s[-1]))[1]
                aligned = abs(math.atan2(math.sin(next_yaw - end_yaw), math.cos(next_yaw - end_yaw))) < 0.15
                if aligned and not any(next_edge.get(k) for k in ("junction", "signal", "facility_entry")):
                    exit_speed = min(a.spec["speed_mps"], next_edge["speed_mps"])
            continuations[a.id] = exit_speed > 0
            desired = min(
                a.spec["speed_mps"],
                self.edges[eid]["speed_mps"],
                math.sqrt(max(0.0, exit_speed**2 + 2 * accel * remaining)),
            )
            speed = min(desired, a.speed + accel * DT)
            progress = min(float(s[-1]), a.progress + max(0.025, speed * DT))
            pos, yaw = self._pose(eid, progress)
            turn = math.atan2(math.sin(yaw - a.yaw), math.cos(yaw - a.yaw))
            limit = a.spec.get("turn_rate_rad_s", 1.2) * DT
            next_yaw = a.yaw + max(-limit, min(limit, turn))
            alignment_limit = limit if a.spec.get("body_m") else max(0.15, limit * 2)
            if abs(turn) > alignment_limit:
                a.phase, a.reason, a.speed = "orient", "align with corridor", 0.0
                proposals[a.id] = [a.pos, a.progress, 0.0, next_yaw]
                continue
            proposals[a.id] = [pos, progress, speed, next_yaw]

        # Joint swept-body constraint, solved over ALL visible actors. Order is stable and
        # queued actors gain priority. This is a conservative stop/yield safety layer.
        visible = [a for a in actors if not a.hidden]
        body_population = bodies.PopulationBodies(visible) if visible else None
        # Anticipate merges before bodies reach the conflict point. A purely reactive
        # stop-at-contact rule leaves two crossing streams face-to-face with neither
        # able to move. Look ahead along the admitted corridor, not through buildings.
        if len(visible) >= 2:
            future = []
            for a in visible:
                if a.id in proposals and a.edge:
                    horizon = 3.0 if a.spec["profile"] == "vehicle" else 2.0
                    future.append(
                        self._pose(
                            a.edge, min(float(self.geometry[a.edge][1][-1]), a.progress + proposals[a.id][2] * horizon)
                        )[0]
                    )
                else:
                    future.append(a.pos)
            # Forecast is a yielding heuristic; the full swept-yaw constraint below
            # still checks every actual tick, including rotations without translation.
            gaps = body_population.gaps(future, body_population.start_yaw)
            for i, j in np.argwhere(np.triu(gaps < 0.15, k=1)):
                a, b = visible[i], visible[j]
                moving = [x for x in (a, b) if x.id in proposals]
                if not moving:
                    continue
                separation = float(np.linalg.norm(a.pos - b.pos))

                def anticipation_priority(x, a=a, b=b, separation=separation):
                    other = b if x.id == a.id else a
                    separates = np.linalg.norm(proposals[x.id][0] - other.pos) > separation + 1e-7
                    owns = any(x.id in owners for owners in self.junctions.values())
                    return (not separates, not owns, -x.stopped_s, x.id)

                loser = max(moving, key=anticipation_priority)
                proposals.pop(loser.id, None)
                loser.phase, loser.reason, loser.speed = "wait", "yield before conflict", 0.0
        for _ in range(len(visible) + 1):
            if len(visible) < 2:
                break
            ends = np.array([proposals.get(a.id, [a.pos])[0] for a in visible])
            yaws = np.array([proposals[a.id][3] if a.id in proposals else a.yaw for a in visible])
            gaps = body_population.gaps(ends, yaws)
            collisions = np.argwhere(np.triu(gaps < 0.05, k=1))
            changed = False
            for i, j in collisions:
                a, b = visible[i], visible[j]
                moving = [
                    x
                    for x in (a, b)
                    if x.id in proposals
                    and (np.linalg.norm(proposals[x.id][0] - x.pos) > 1e-9 or abs(proposals[x.id][3] - x.yaw) > 1e-9)
                ]
                if not moving:
                    continue
                separation = float(np.linalg.norm(a.pos - b.pos))
                escaping = {
                    x.id
                    for x, other in ((a, b), (b, a))
                    if x.id in proposals and np.linalg.norm(proposals[x.id][0] - other.pos) > separation + 1e-7
                }

                # Existing junction holder goes first; otherwise longest waiting then ID.
                def priority(x, escaping=escaping):
                    owns = any(x.id in owners for owners in self.junctions.values())
                    # A follower must never freeze the person/vehicle ahead of it.
                    # Keep a separating motion before resolving crossing right of way.
                    return (x.id not in escaping, not owns, -x.stopped_s, x.id)

                loser = max(moving, key=priority)
                proposals.pop(loser.id, None)
                loser.phase, loser.reason, loser.speed = "wait", "dynamic obstacle", 0.0
                changed = True
                self.safety_interventions += 1
            if not changed:
                break

        if len(visible) >= 2:
            accepted_positions = [proposals[a.id][0] if a.id in proposals else a.pos for a in visible]
            accepted_yaws = [proposals[a.id][3] if a.id in proposals else a.yaw for a in visible]
            accepted_gaps = body_population.gaps(accepted_positions, accepted_yaws)
            self.min_swept_clearance_m = min(self.min_swept_clearance_m, float(accepted_gaps.min()))

        for a in actors:
            old = a.pos.copy()
            if a.id in proposals:
                a.pos, a.progress, a.speed, a.yaw = proposals[a.id]
                a.vel = (a.pos - old) / DT
                a.distance += float(np.linalg.norm(a.pos - old))
                if a.progress >= self.geometry[a.edge][1][-1] - 1e-8:
                    e = self.edges[a.edge]
                    a.node = e["to"]
                    zone = e.get("junction")
                    if zone:
                        owners = self.junctions.get(zone, set())
                        owners.discard(a.id)
                        if not owners:
                            self.junctions.pop(zone, None)
                        self.queue_since.pop((zone, a.id), None)
                    a.edge, a.progress = None, 0.0
                    if not continuations.get(a.id):
                        a.speed = 0.0
                    if not a.route:
                        a.phase = "arrived"
            else:
                a.speed = 0.0
            reason = a.reason or ("moving" if np.linalg.norm(a.vel) > 1e-6 else a.phase)
            a.reason_seconds[reason] += DT
            unexpected_stop = reason in (
                "dynamic obstacle",
                "junction right of way",
                "facility capacity",
                "entrance occupied",
                "downstream occupied",
                "yield before conflict",
            )
            a.stopped_s = a.stopped_s + DT if unexpected_stop else 0.0
            a.max_unplanned_wait_s = max(a.max_unplanned_wait_s, a.stopped_s)
            stalled = np.linalg.norm(a.vel) < 1e-6 and a.phase not in ("visit", "stationary")
            a.no_progress_s = a.no_progress_s + DT if stalled else 0.0
            a.max_no_progress_s = max(a.max_no_progress_s, a.no_progress_s)
        if self.tick % 600 == 0:
            self.population_history.append(
                {
                    "t_s": now,
                    "total": len(actors),
                    "moving": sum(int(np.linalg.norm(a.vel) > 1e-6) for a in actors),
                    "visiting": sum(a.phase == "visit" for a in actors),
                    "waiting": sum(a.phase in ("wait", "orient", "arrived") for a in actors),
                    "stationary": sum(a.phase == "stationary" for a in actors),
                }
            )
        if len(visible) >= 2:
            points = np.array([a.pos for a in visible])
            gaps = body_population.gaps(points, [a.yaw for a in visible], swept=False)
            self.min_clearance_m = min(self.min_clearance_m, float(gaps[np.triu_indices(len(visible), 1)].min()))

    def advance(self, t_s: float):
        if not math.isfinite(t_s) or t_s < 0:
            raise ValueError("Invalid simulation time")
        target = math.floor((t_s + 1e-7) / DT)
        if target < self.tick:
            self.reset()
        while self.tick < target:
            self._step()

    def sample(self, actor_id: str):
        a = self.actors[actor_id]
        route = ([a.edge] if a.edge else []) + a.route
        f = self.facilities.get(a.destination, {})
        route_points = [p for eid in route for p in self.edges[eid]["points"]]
        if len(route_points) > 128:
            route_points = [route_points[i] for i in np.linspace(0, len(route_points) - 1, 128, dtype=int)]
        state = {
            "phase": a.phase,
            "reason": a.reason,
            "destination": f.get("label", a.destination),
            "remaining_s": round(max(0, a.dwell_until - self.tick * DT), 1),
            "seed": self.doc["seed"],
            "walked_m": round(a.distance, 1),
            "visits": a.visits,
            "hidden": a.hidden,
            "duration_s": 0,
            "updates": a.updates,
            "max_unplanned_wait_s": round(a.max_unplanned_wait_s, 1),
            "route_enu_m": route_points,
        }
        return a.pos.copy(), a.yaw, a.vel.copy(), state

    def audit(self):
        return {
            "version": VERSION,
            "seed": self.doc["seed"],
            "ticks": self.tick,
            "event_sha256": self.event_digest.hexdigest(),
            "safety_interventions": self.safety_interventions,
            "population_history": self.population_history,
            "min_clearance_m": self.min_clearance_m if math.isfinite(self.min_clearance_m) else None,
            "min_swept_clearance_m": self.min_swept_clearance_m if math.isfinite(self.min_swept_clearance_m) else None,
            "actors": {
                a.id: {
                    "class": a.spec.get("class", a.spec["profile"]),
                    "stationary": bool(a.spec.get("stationary")),
                    "updates": a.updates,
                    "distance_m": round(a.distance, 3),
                    "visits": a.visits,
                    "max_unplanned_wait_s": round(a.max_unplanned_wait_s, 1),
                    "max_no_progress_s": round(a.max_no_progress_s, 1),
                    "reason_seconds": {k: round(v, 1) for k, v in sorted(a.reason_seconds.items())},
                }
                for a in self.actors.values()
            },
        }
