"""Certify oriented vehicle envelopes against exact synthcity surfaces/solids.

Certificates are per asset dimensions and edge, never a global radius override.
Road cells touched by the conservatively expanded sphere row must ALL be road.
The 0.25 m sampling padding bounds interpolation between sampled path poses.
"""

import math

import numpy as np

from awr.sim.activities.bodies import discs
from awr.sim.targets.model import appearance_catalog
from awr.world.ingest import synthetic as syn


def junction_curve(p, q, incoming, outgoing, stop_radius):
    """Tangent leads + a quarter circle, without reversing hooks at either end."""
    p, q, d, d2 = map(lambda x: np.asarray(x, dtype=float), (p, q, incoming, outgoing))
    if np.allclose(d, d2):
        return [p, q]
    turn_sign = float(d[0] * d2[1] - d[1] * d2[0])
    if abs(turn_sign) < 0.99:
        raise ValueError("Only straight/left/right turns are admitted")
    inset = 6.0 if turn_sign > 0 else 9.0
    start = p + d * inset
    radius = stop_radius - inset + (4.5 if turn_sign > 0 else -4.5)
    if radius <= 0:
        raise ValueError("Junction lacks turning space")
    center = start + d2 * radius
    arc = [center - d2 * radius * math.cos(t) + d * radius * math.sin(t) for t in np.linspace(0, math.pi / 2, 97)]
    if not np.allclose(arc[-1][:2], (q - d2 * inset)[:2], atol=1e-8):
        raise ValueError("Turn endpoint does not meet its exit tangent")
    return [p, *arc, q]


def certify(network, planner, city):
    catalog = [a for a in appearance_catalog().values() if a["class"] == "vehicle"]
    counts = {a["id"]: 0 for a in catalog}
    rejections = {}
    for key, edge in network["edges"].items():
        if "vehicle" not in edge["profiles"]:
            continue
        p = np.asarray(edge["points"])
        segment = np.diff(p[:, :2], axis=0)
        yaw = np.arctan2(segment[:, 1], segment[:, 0])
        yaw = np.r_[yaw, yaw[-1]]
        direction = np.c_[np.cos(yaw), np.sin(yaw)]
        ds = np.linalg.norm(segment, axis=1)
        s = np.r_[0, np.cumsum(ds)]
        sample_s = np.linspace(0, s[-1], max(2, math.ceil(s[-1] / 0.5) + 1))
        before = np.c_[
            np.interp(np.maximum(0, sample_s - 1), s, p[:, 0]), np.interp(np.maximum(0, sample_s - 1), s, p[:, 1])
        ]
        after = np.c_[
            np.interp(np.minimum(s[-1], sample_s + 1), s, p[:, 0]),
            np.interp(np.minimum(s[-1], sample_s + 1), s, p[:, 1]),
        ]
        heading = np.unwrap(np.arctan2((after - before)[:, 1], (after - before)[:, 0]))
        curvature = np.max(np.abs(np.diff(heading)) / np.diff(sample_s))
        turn_radius = 1 / max(1e-9, curvature)
        edge["vehicle_bodies"] = {}
        edge["min_turn_radius_m"] = round(float(turn_radius), 3)
        # At a polyline knot either adjacent tangent can be used; include both.
        directions = [direction, np.vstack((direction[0], direction[:-1]))]
        for asset in catalog:
            length, width, height = asset["dimensions_m"]
            body = {"length": length, "width": width}
            if turn_radius < length:
                rejections[key + ":" + asset["id"]] = "turn radius below conservative one-body-length limit"
                continue
            offsets, radius = discs(
                {"class": "vehicle", "body_m": body, "radius_m": math.hypot(length, width) / 2 + 0.025}
            )
            xy = np.concatenate(
                [(p[:, :2, None] + d[:, :, None] * offsets).transpose(0, 2, 1).reshape(-1, 2) for d in directions]
            )
            # Path sampling and rotation interpolation over a <=0.25 m step.
            max_turn = float(np.max(np.abs(np.diff(np.unwrap(yaw)))))
            radius += max(0.25, float(ds.max()) / 2 + float(np.abs(offsets).max()) * max_turn + 0.025)
            # Certify every raster cell potentially touched by an envelope sphere.
            idx = np.floor((xy + planner.H) / syn.RES).astype(int)
            extent = math.ceil((radius + syn.RES * math.sqrt(2)) / syn.RES)
            touched = []
            for ix in range(-extent, extent + 1):
                for iy in range(-extent, extent + 1):
                    cells = idx + np.array([ix, iy])
                    cell_xy = (cells + 0.5) * syn.RES - planner.H
                    use = np.linalg.norm(cell_xy - xy, axis=1) <= radius + syn.RES / math.sqrt(2)
                    if use.any():
                        touched.append(cells[use])
            cells = np.unique(np.concatenate(touched), axis=0)
            why = None
            if (cells < 0).any() or (cells >= planner.n).any():
                why = "world bounds"
            elif not np.isin(planner.lu[cells[:, 1], cells[:, 0]], [syn.U_ROAD, syn.U_MARK]).all():
                why = "body leaves road surface"
            if why is None:
                codes = planner.lu[cells[:, 1], cells[:, 0]]
                ground_xy = (cells + 0.5) * syn.RES - planner.H
                ground = syn._ground_z(ground_xy[:, 0], ground_xy[:, 1], codes, planner)[0]
                if ground.max() - ground.min() > 0.3:
                    why = "ground step"
            if why is None:
                z0, z1 = float(p[:, 2].min()), float(p[:, 2].max() + height)
                for b in city.buildings:
                    for part in [*b.parts, *b.equip]:
                        if part.z1 < z0 or part.z0 > z1:
                            continue
                        if part.kind == "box":
                            x0, y0, x1, y1 = part.rect
                            nearest = np.clip(xy, [x0, y0], [x1, y1])
                            distance = np.linalg.norm(xy - nearest, axis=1)
                        else:
                            distance = np.linalg.norm(xy - np.array(part.center), axis=1) - max(part.r0, part.r1)
                        if (distance < radius).any():
                            why = "building swept envelope"
                            break
                    if why:
                        break
                if why is None:
                    for tree in city.trees:
                        r = 0.4 if tree.z + tree.trunk > z1 else max(0.4, tree.r)
                        if (np.linalg.norm(xy - np.array([tree.x, tree.y]), axis=1) < r + radius).any():
                            why = "vegetation swept envelope"
                            break
            if why:
                rejections[key + ":" + asset["id"]] = why
            else:
                edge["vehicle_bodies"][asset["id"]] = body
                counts[asset["id"]] += 1
    network["admission"]["vehicle_bodies"] = {
        "method": "covering sphere row; all touched road cells; vertical solids; conservative turn radius >= body length",
        "sample_padding_m": 0.25,
        "edge_counts": counts,
        "rejected": rejections,
    }
    return network
