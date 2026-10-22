"""Read-only authoring replay. Every actor advances in a separate LifeWorld, never the live session."""

from __future__ import annotations

import gzip
import json
import os
import tempfile
from pathlib import Path

from .life import LifeWorld

FORMAT = 1
DURATION_S = 1500


def map_polyline(points: list) -> list:
    """Bound display-only road detail. The engine still receives every original 3D road point."""
    count = min(16, len(points))
    if count < 2:
        return points
    return [[round(float(v), 3) for v in points[round(i * (len(points) - 1) / (count - 1))][:2]] for i in range(count)]


def replay_population(recipe: dict, duration_s: int = DURATION_S) -> dict:
    if not 1 <= len(recipe["actors"]) <= 30 or not 1 <= duration_s <= DURATION_S:
        raise ValueError("Rehearsal exceeds population or duration limit")
    world = LifeWorld(recipe)
    tracks = {actor_id: [] for actor_id in world.actors}
    for second in range(duration_s + 1):
        world.advance(second)
        for actor_id, actor in world.actors.items():
            pos, yaw, velocity, state = world.sample(actor_id)
            tracks[actor_id].append(
                {
                    "t": second,
                    "pos": [round(float(v), 4) for v in pos],
                    "yaw": round(float(yaw), 6),
                    "speed": round(float(sum(v * v for v in velocity) ** 0.5), 4),
                    "distance": round(actor.distance, 4),
                    "hidden": state["hidden"],
                    "phase": state["phase"],
                    "destination": state["destination"],
                    "reason": state["reason"],
                }
            )
    return {"format": FORMAT, "duration_s": duration_s, "tracks": tracks}


def rehearsal(recipe: dict, sha: str, actor_id: str, root: Path) -> dict:
    """Cache the whole population by verified recipe hash; changing selection does not resimulate it."""
    actor = next((a for a in recipe["actors"] if a["id"] == actor_id), None)
    if actor is None:
        raise ValueError("Actor does not belong to the selected population")
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Invalid recipe hash")
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rehearsal-v{FORMAT}-{sha}.json.gz"
    data = None
    try:
        with gzip.open(path, "rb") as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if len(raw) <= 32 * 1024 * 1024:
            cached = json.loads(raw)
            if cached.get("format") == FORMAT and cached.get("duration_s") == DURATION_S:
                data = cached
    except (OSError, EOFError, ValueError):
        pass
    if data is None:
        data = replay_population(recipe)
        fd, temporary = tempfile.mkstemp(dir=root, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(gzip.compress(json.dumps(data, allow_nan=False).encode()))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {
        "recipe_sha256": sha,
        "actor_id": actor_id,
        "seed": recipe["seed"],
        "duration_s": data["duration_s"],
        "sample_s": 1,
        "samples": data["tracks"][actor_id],
        "roads": [map_polyline(e["points"]) for e in recipe["edges"].values()],
        "facilities": [
            {
                "id": fid,
                "label": f.get("label", fid),
                "pos": recipe["nodes"][f["node"]]["pos"],
                "indoor": bool(f.get("indoor")),
            }
            for fid, f in recipe["facilities"].items()
        ],
        "itinerary": actor.get("itinerary", []),
    }
