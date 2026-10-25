"""Content-addressed life recipes and world/actor admission. No executable model output."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from functools import lru_cache
from pathlib import Path

from .life import LifeWorld, runtime_fingerprint


@lru_cache(maxsize=16)
def load_recipe(recipe_id: str, sha256: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", recipe_id) or not re.fullmatch(r"[a-f0-9]{64}", sha256):
        raise ValueError("Invalid life recipe reference")
    root = Path(os.environ.get("AWR_TARGET_LIFE_DIR", "var/target-life"))
    path = root / f"{recipe_id}-{sha256}.json"
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Life recipe exceeds 32 MiB")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("Life recipe hash mismatch")
    doc = json.loads(data)
    if doc.get("engine_sha256") != runtime_fingerprint():
        raise ValueError("Life engine version changed; regenerate the recipe")
    LifeWorld(doc)  # Validate once, before any actor is admitted.
    return doc


def bound_recipe(raw: dict, world):
    if raw.get("enabled", True) is not True:
        raise ValueError("Shared population actors must remain enabled")
    ref = raw["life"]
    doc = load_recipe(ref["recipe_id"], ref["sha256"])
    if world is None or any(
        doc[k] != getattr(world, attr, None)
        for k, attr in (
            ("world_id", "world_id"),
            ("world_content_version", "content_version"),
            ("coordinate_sha256", "coordinate_sha256"),
        )
    ):
        raise ValueError("Life world/coordinate version mismatch")
    spec = next((a for a in doc["actors"] if a["id"] == ref["actor_id"]), None)
    if spec is None or spec["class"] != raw["class"]:
        raise ValueError("Life actor identity/class mismatch")
    size = raw["size_m"]
    body = spec.get("body_m")
    too_large = (
        (size["l"] > body["length"] + 1e-6 or size["w"] > body["width"] + 1e-6)
        if body
        else math.hypot(size["l"], size["w"]) / 2 > spec["radius_m"] + 1e-6
    )
    if too_large or size["h"] > spec["height_m"] + 1e-6:
        raise ValueError("Actor exceeds admitted footprint")
    if list(raw["pos"]) != doc["nodes"][spec["start"]]["pos"]:
        raise ValueError("Life actor start is immutable; regenerate the recipe to relocate")
    return doc, spec
