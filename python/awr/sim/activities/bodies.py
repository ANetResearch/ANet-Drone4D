"""Conservative heading-aware vehicle envelopes and continuous pair clearance.

A row of overlapping spheres covers every point in the horizontal rectangle.
It is deliberately larger than the visual body, never a capsule inscribed in it.
For one tick, translation is linear and yaw follows its shortest arc; the sagitta
bound expands every sphere to cover curved centre motion between endpoints.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np


def discs(spec):
    body = spec.get("body_m")
    if body is None:
        return np.array([0.0]), float(spec["radius_m"])
    if spec.get("class") != "vehicle" or set(body) != {"length", "width"}:
        raise ValueError("Only validated rectangular vehicles support body_m")
    length, width = body["length"], body["width"]
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or x <= 0
        for x in (length, width)
    ):
        raise ValueError("Invalid vehicle body dimensions")
    if math.hypot(length, width) / 2 > spec["radius_m"] + 1e-6:
        raise ValueError("Body exceeds broad-phase radius")
    return rectangle_discs(length, width)


@lru_cache(maxsize=128)
def rectangle_discs(length, width):
    count = math.ceil(length / 1.2)
    segment = length / count
    offsets = np.linspace(-length / 2 + segment / 2, length / 2 - segment / 2, count)
    return offsets, math.hypot(segment / 2, width / 2) + 0.025


def centers(pos, yaw, offsets):
    return np.asarray(pos)[None, :] + offsets[:, None] * np.array([math.cos(yaw), math.sin(yaw), 0.0])


def clearance(a, ap, ay, b, bp, by):
    ao, ar = discs(a)
    bo, br = discs(b)
    ac, bc = centers(ap, ay, ao), centers(bp, by, bo)
    return float(np.linalg.norm(ac[:, None] - bc[None, :], axis=2).min()) - ar - br


class PopulationBodies:
    """Expanded body array shared by every pair, with no actor subsampling."""

    def __init__(self, actors):
        self.actors = actors
        self.owners = []
        self.offsets = []
        self.radii = []
        for index, a in enumerate(actors):
            offsets, radius = discs(a.spec)
            self.owners.extend([index] * len(offsets))
            self.offsets.extend(offsets)
            self.radii.extend([radius] * len(offsets))
        self.owners = np.asarray(self.owners, dtype=int)
        self.offsets = np.asarray(self.offsets)
        self.radii = np.asarray(self.radii)
        self.cross = self.owners[:, None] < self.owners[None, :]
        self.pairs = self.owners[:, None] * len(actors) + self.owners[None, :]
        self.start_yaw = np.array([a.yaw for a in actors])
        self.start = self.positions(np.array([a.pos for a in actors]), self.start_yaw)

    def positions(self, positions, yaws):
        headings = np.column_stack((np.cos(yaws), np.sin(yaws), np.zeros(len(yaws))))
        return positions[self.owners] + self.offsets[:, None] * headings[self.owners]

    def gaps(self, positions, yaws, *, swept=True):
        end = self.positions(np.asarray(positions), np.asarray(yaws))
        r = self.start[:, None] - self.start[None, :]
        if swept:
            delta = end - self.start
            v = delta[:, None] - delta[None, :]
            t = np.clip(-np.sum(r * v, axis=2) / np.maximum(1e-12, np.sum(v * v, axis=2)), 0, 1)
            distance = np.linalg.norm(r + v * t[:, :, None], axis=2)
            turn = np.arctan2(np.sin(np.asarray(yaws) - self.start_yaw), np.cos(np.asarray(yaws) - self.start_yaw))
            sagitta = np.abs(self.offsets) * (1 - np.cos(turn[self.owners] / 2))
            radii = self.radii + sagitta
        else:
            distance = np.linalg.norm(end[:, None] - end[None, :], axis=2)
            radii = self.radii
        distance -= radii[:, None] + radii[None, :]
        result = np.full(len(self.actors) ** 2, np.inf)
        np.minimum.at(result, self.pairs[self.cross], distance[self.cross])
        return result.reshape((len(self.actors), len(self.actors)))
